"""店小秘发布操作：attributes.workflow。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish.attributes import (
    composition as attributes_composition,
    dropdowns as attributes_dropdowns,
    form as attributes_form,
    review as attributes_review,
    server_options as attributes_server_options,
    validation as attributes_validation,
)
from app.publish.browser import BrowserSession
from typing import Optional


# 联动补填的最大轮数：一轮 = 重扫新增必填行 → 读选项 → 问 LLM → 写入。
#
# 【为什么必须循环而不是跑一轮】里料纹理只要不选「无内衬/无里料」，就会联动出必须
# 继续选的行，而【补填自己写的值同样会再联动】：给「里衬成分」选上纤维后平台会带出
# 该成分的百分比/克重行，那些行在上一轮重扫时还不在 DOM 里。原实现只跑一轮，第二层
# 行一律落进 unfilledRequired 交人工——与「发布流程全自动优先」的取向相反。
#
# 上限 3 而不是无界：每轮固定要花「1.5s 等渲染 + 读选项 + 一次 LLM + 逐项写入」，
# 无界会让本阶段耗时失控；实测联动最深就是「里料纹理 → 里衬成分 → 其百分比」这两层，
# 留一轮余量。到顶仍有空行就交末尾复扫报人工，那是设计内的出口而非失败。
_LINKAGE_MAX_ROUNDS = 3


async def _scan_linkage_rows(session: BrowserSession, seen: set) -> list:
    """重扫表单，挑出 seen 之外的必填行（即刚联动出来的那些）。

    【差集要对累积的 seen 取而不是只对上一轮】第二层联动行是第 N 轮写入才冒出来的，
    只跟上一轮比会把早先见过的行反复挑出来重填（写失败的行更是每轮都中），既浪费
    LLM 调用又可能把已填对的值改掉。

    【2026-09-03 改动】原逻辑只扫 current 以"("开头的空行，忽略了有预填值的联动行。
    但联动行可能有平台默认值（如"里衬成分"默认某个常见纤维），如果这个默认值不合适，
    就会一直错下去。新逻辑扫【所有】新出现的必填行（无论有无预填值），交给 LLM 审核：
    - 预填值合适 → LLM 输出"保持原值"（走 keep_current 路径，不实际写入）
    - 预填值不合适 → LLM 给出正确值并写入
    - LLM 拿不准且无预填值（"(请选择)"）→ 放 notes，到末尾复扫报人工
    """
    # 联动行在写入后才渲染出来，且重渲染要时间，故等一下再读
    await asyncio.sleep(1.5)
    rows = await session.eval_json(attributes_form._JS_LIST_ATTR_ROWS)
    return [a for a in rows.get("attrs", [])
            if a["label"] not in seen and a["label"] != "产品属性"
            and a.get("required") and a.get("visible") is not False]


async def _read_linkage_options(session: BrowserSession, new_rows: list,
                                rowid: str, cat_id: str = "") -> None:
    """给联动行就地补上 options（原地改 new_rows），规矩与主轮 dump_attrs 一致。

    联动新增的必填行仍是【同一类目】的属性，所以与主轮共用同一份服务端清单
    （见 attributes/server_options）；数值行不读选项——它行内的 select 是只读单位。
    """
    server_opts = await attributes_server_options.fetch_attr_options(
        session, rowid, cat_id)
    for a in new_rows:
        a["options"] = []
        if a.get("kind") == "number":
            a["optionsEmptyReason"] = "number-row"
            continue
        opts = server_opts.get(a["label"]) or []
        if opts:
            a["options"] = opts
            a["optionsFrom"] = "server"
        else:
            a["optionsEmptyReason"] = "server-missing"
            logger.warning(f"联动行「{a['label']}」在服务端清单里没有对应项，读不到选项")


async def _fill_linkage_round(session: BrowserSession, new_rows: list, info: dict,
                              main_comp: Optional[dict], rowid: str = "",
                              cat_id: str = "", site: str = "") -> tuple:
    """补填一轮：读选项 → 问 LLM → 校验 → 写入，返回 (applied, compFailed)。

    与主轮共用 _validate_attr_changes 与 _apply_attr_changes：闸门（非必填留空、
    options 校验、数值量级、成分合计 100）一条都不能少，另写一套必然与主轮漂移。
    成分行同理走 row/num 那套，故这里不做「每行只问一次」的简化。

    【best-effort】读不到行、LLM 不给值、写入失败一律只记录不抛，交末尾复扫报人工。
    """
    await _read_linkage_options(session, new_rows, rowid, cat_id)
    # 问 LLM：喂法与主轮完全一致（同一套提示词、同样带 kind/numHint），只是行少。
    # 【提示词不能换】规则 5（成分合计 100）、规则 8（数值行只给纯数字）对里衬成分和
    # 里料克重恰好都适用，换一套简版提示词等于把这两条闸的前提抽掉。
    ask_rows = [{"label": a["label"], "current": a.get("current"),
                 "required": bool(a.get("required")),
                 "kind": a.get("kind") or "select",
                 "numHint": a.get("numHint"),
                 "numValues": a.get("numValues"), "options": a.get("options", [])}
                for a in new_rows]
    try:
        # site 对本轮尤其要紧：「插头规格」「工作电压」正是「供电方式=插头供电」联动
        # 出来的行（2026-09-12 商品 pdd-250293857545），它们只能按发布站点判。
        decision = await attributes_review._ask_attr_review(ask_rows, info, main_comp, site)
    except Exception as e:
        logger.warning(f"联动行补填问 LLM 失败（忽略，交末尾复扫报人工）：{e}")
        return [], []
    valid, rejected, keep_current = attributes_validation._validate_attr_changes(
        decision.get("changes", []), new_rows, main_comp)
    if rejected:
        logger.warning(f"联动行补填驳回 {len(rejected)} 项："
                       + "、".join(f"{r.get('label')}({r.get('rejectReason')})"
                                   for r in rejected))
    if keep_current:
        logger.info(f"联动行补填保持原值 {len(keep_current)} 项："
                    + "、".join(k.get("label") for k in keep_current))
    if not valid:
        return [], []
    applied, _refreshed, comp_failed = await attributes_review._apply_attr_changes(
        session, valid, {a["label"]: a for a in new_rows}, info)
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    logger.info(f"本轮补填完成：{ok_n}/{len(applied)} 项写入成功")
    return applied, comp_failed


async def _fill_linkage_rows(session: BrowserSession, pre_labels: set, info: dict,
                             main_comp: Optional[dict], rowid: str = "",
                             cat_id: str = "", site: str = "") -> dict:
    """补填「联动新增的必填行」，循环追到不再冒新行为止。

    为什么必须单独一轮而不能并进主轮：这些行是【改了别的行才出现的】——阶段④开头
    dump_attrs 扫表单时它们根本不在 DOM 里，LLM 看不到、也就不会给值。原实现扫出来
    只记进 linkageNewRequired 就返回了，没有任何调用方消费，于是这两行一直空着，
    保存卡在「请输入产品属性」（2026-08-25 用户截图）。

    【为什么是多轮】里料纹理只要不选「无内衬/无里料」就必然联动，而补填写进去的值
    会再带出下一层行（里衬成分选定后出现其百分比）。单轮只能填第一层，第二层静默留空
    到保存才炸。故循环重扫，直到收敛或到 _LINKAGE_MAX_ROUNDS 上限。

    两个终止条件都必要：
      - 扫不到新行 → 真收敛，正常出口；
      - 扫到新行但本轮一项都没写成功 → 停。再转一轮扫到的还是同一批（label 已进
        seen，其实连扫都扫不到了），继续只是白烧 LLM 调用。
    """
    seen = set(pre_labels)
    all_applied: list = []
    all_new: list = []
    all_comp_failed: list = []
    for rnd in range(1, _LINKAGE_MAX_ROUNDS + 1):
        new_rows = await _scan_linkage_rows(session, seen)
        if not new_rows:
            if rnd > 1:
                logger.info(f"联动补填第 {rnd} 轮无新增行，已收敛")
            break
        labels = [a["label"] for a in new_rows]
        seen.update(labels)
        all_new.extend(labels)
        logger.info(f"联动新增必填行 {len(labels)} 条（第 {rnd}/{_LINKAGE_MAX_ROUNDS} 轮），"
                    f"开始补填：{'、'.join(labels)}")
        # 【cat_id 必须透传】漏传它，_read_linkage_options 会按「草稿已保存的类目」查
        # 服务端属性清单，而阶段③ 是运行中改类目、还没保存的——查回来的是上一版类目的
        # 选项。2026-09-12 商品 pdd-250293857545 实测：主轮按页面现值查类目 12989（20 个
        # 属性），本轮却按草稿旧值查了 13624（30 个属性），联动行「插头规格」拿着旧清单
        # 里的值去点，一律 option-not-rendered 写不上，末尾复扫报必填留空。这与
        # server_options._JS_ATTR_OPTIONS 注释里 2026-09-11 记的是同一个坑，主轮当时
        # 修好了、本轮漏了。
        applied, comp_failed = await _fill_linkage_round(
            session, new_rows, info, main_comp, rowid, cat_id, site)
        all_applied.extend(applied)
        all_comp_failed.extend(comp_failed)
        if not any(a.get("result") == "ok" for a in applied):
            logger.warning(f"第 {rnd} 轮补填无一项成功，停止追加轮次（交末尾复扫报人工）")
            break
    else:
        # 跑满轮次而不是靠「扫不到新行」退出：最后一轮仍在冒新行，说明联动比实测更深，
        # 是否还有残留由 check_attrs 末尾的必填复扫认定。这里只留痕。
        logger.warning(f"联动补填已跑满 {_LINKAGE_MAX_ROUNDS} 轮仍在冒新行，"
                       "是否留空交末尾复扫认定")
    return {"applied": all_applied, "newRequired": all_new,
            "compFailed": all_comp_failed}


async def _try_default_attrs(session: BrowserSession, info: dict,
                             main_comp: Optional[dict],
                             site: str = "") -> Optional[dict]:
    """默认属性快路径：草稿预填的属性值若全都与商品相符，整个阶段④都不用跑。

    【为什么值得单开一条】编辑页草稿本来就带着属性值（认领时店小秘按源商品映射的），
    它们对的时候，读选项（33 项要 3-5 分钟，见 dump_attrs）、完整审核、逐项写入、
    联动补填轮全是白跑。这里一个下拉都不点：读一次表单现状 + 一次轻量判断。

    落回原有流程的五种情形：读不到行、有空必填行（默认不完整，必须读 options 才能填）、
    判断调用失败、判出有明显矛盾的行、判断期间表单又长出了新行（见末尾复扫那段）。
    落回时【什么都没改过】——本函数只读不写，原流程从零开始跑，不留任何需要清理的状态。

    返回值与 check_attrs 的成功返回【同构】，多一个 source="default"；applied/proposed
    一律为空（本路径不写任何东西）。
    """
    # 这条路径只读现状、一个下拉都不碰，选项来源改服务端后更是不涉及选项
    # （见 dump_attrs 的 skip_options 早退分支）。
    dump = await attributes_form.dump_attrs(session, skip_options=True)
    attrs = [a for a in (dump.get("attrs") or []) if a.get("visible") is not False]
    if not attrs:
        return None
    # 未填的判据与主流程一致：current 以 "(" 开头即占位符（"(请选择)" 等）
    def _filled(a) -> bool:
        cur = str(a.get("current") or "").strip()
        return bool(cur) and not cur.startswith("(")

    missing = [a["label"] for a in attrs if a.get("required") and not _filled(a)]
    if missing:
        logger.info(f"默认属性缺 {len(missing)} 项必填"
                    f"（{'、'.join(missing[:6])}{'…' if len(missing) > 6 else ''}），"
                    "走逐项审核")
        return None
    # 只判「已有值」的行：非必填且空着的行按策略一律留空（见 _ATTR_PROMPT 规则 4），
    # 本就不该填，不能因为它们没值就把整条快路径判死——那会让判据反过来惩罚正确行为
    # （表单上大半非必填行本来就该是空的）。
    rows = [{"label": a["label"], "current": a.get("current"),
             "required": bool(a.get("required")), "kind": a.get("kind") or "select",
             "numValues": a.get("numValues")}
            for a in attrs if _filled(a)]
    if not rows:
        return None
    try:
        verdict = await attributes_review._ask_default_attr_review(
            rows, info, main_comp, site)
    except Exception as e:
        # 与两条类目快路径同一取向：加速手段自己的 LLM 调用失败不该拖垮整个阶段
        logger.warning(f"默认属性判断失败，落回逐项审核：{e}")
        return None
    if not verdict.get("ok"):
        bad = "、".join(str(i.get("label")) for i in (verdict.get("issues") or [])[:6])
        logger.info(f"默认属性不适用（{verdict.get('reason')}）"
                    + (f"，涉及：{bad}" if bad else "") + "，走逐项审核")
        return None
    logger.info(f"沿用草稿默认属性：{len(rows)} 行经 LLM 判定均相符，"
                "跳过读选项、逐项写入与联动补填")
    # 末尾必填复扫照跑：它是交给调用方的「还差什么」清单，判据必须与主流程一致。
    # 本路径没有写入，故不会有联动新增行；一个下拉都没点过，也不会有幽灵浮层要清。
    rows3 = await session.eval_json(attributes_form._JS_LIST_ATTR_ROWS)
    attrs3 = [a for a in rows3.get("attrs", []) if a["label"] != "产品属性"]
    # 【复扫还要当一次「表单有没有长出新行」的哨兵】开头那次 dump 已经改成等行集稳定
    # （见 _rows_settled），但稳定只是「静默了一轮」，不保证拿到的是终态：类目刚改完时
    # 动态属性行按接口返回逐批挂上，两批之间完全可能静默超过一轮。判定要通过一次 LLM
    # （实测 21s），这段时间足够剩下的行全部到齐——拿它当第二道闸，只要冒出一行开头
    # 没见过的，就说明刚才判的不是完整表单，快路径的前提不成立，落回完整流程重判。
    # 白花一次 LLM 换「绝不漏判必填行」，这个代价是值的：漏判的下场是下游十几个阶段
    # 白跑、到 save 才炸（2026-09-11 商品 1044382261282 的「面料类型」即此）。
    # 【两边必须同口径：都用「除分组标题外的全部行」】上面 attrs 滤掉了 visible=False
    # 的行，若这里拿它当已知集，表单本来就有隐藏行（「材质」依赖「是否纺织品」这类
    # 开关字段，未选时不显示）时 grown 恒非空，快路径每次都被判「长出了新行」而落回，
    # 等于把这条优化整个废掉。隐藏行变可见不构成漏判：本路径不填任何值，而末尾复扫
    # （unfilled）扫的是全部行，真变成必填且空照样报得出来。
    known = {a["label"] for a in (dump.get("attrs") or [])}
    grown = [a["label"] for a in attrs3 if a["label"] not in known]
    if grown:
        logger.info(f"默认属性判定期间表单又出现 {len(grown)} 行"
                    f"（{'、'.join(grown[:6])}{'…' if len(grown) > 6 else ''}），"
                    "快路径前提不成立，走逐项审核")
        return None
    # 【成分合计也在「预填值是否相符」之内】_ATTR_DEFAULT_PROMPT 的判据是「预填值与商品
    # 是否明显矛盾」，规则 2 只管主面料成分的纤维与比例对不对得上源参数，管不到合计；
    # 而本路径【一行都不写】，阶段④ 里的 _rebuild_main_comp 源值重建也不会跑，认领带来的
    # 旧行（如 90+5+22=117%）会被原样放行、一路带到保存被平台拦。故这里判出合计不对就
    # 当「前提不成立」落回完整流程——完整流程按源主面料成分含量重建这几行，正好是它该
    # 干的活（不是判 fail 交人工：这一条本就有确定性解法，别把能自动修的推给人）。
    bad = attributes_form._comp_total_problems(attrs3)
    if bad:
        logger.info("默认属性不适用（成分百分比合计不等于 100："
                    + "、".join(f"{p['label']} {p['total']:g}%" for p in bad)
                    + "），走逐项审核")
        return None
    unfilled = [a["label"] for a in attrs3
                if a.get("required") and a.get("visible") is not False
                and str(a.get("current") or "").startswith("(")]
    return {"status": "ok", "source": "default", "attrCount": len(rows),
            "proposed": [], "rejected": [], "keepCurrent": [],
            "notes": [], "cacheRead": 0, "activeRead": 0,
            "mainComposition": main_comp or None,
            "applied": [], "cacheRefreshed": [], "compFailed": [],
            "linkageFilled": [], "linkageNewRequired": [],
            # 恒为空：合计不对的在上面的 early-return 里就落回完整流程了，能走到这儿的
            # 表单必然已过复验。留着这个键只为与 check_attrs 的成功返回同构，调用方
            # （stages.form._st_attrs）不必按 source 分叉读它。
            "badCompTotals": [],
            "unfilledRequired": unfilled, "parkedGhosts": 0}


async def check_attrs(session: BrowserSession, info_path: str,
                      apply: bool = False, required_only: bool = True,
                      use_cache: bool = True, rowid: str = "",
                      cat_id: str = "", site: str = "") -> dict:
    """阶段④：LLM 比对源商品信息与表单属性，产出修改清单（apply=True 时执行）。

    不导航——须紧跟 auto_cat 在同一页执行（类目决定属性行）。
    apply=False 是 dry-run：只出清单不动表单，供人工过目。这是本阶段的推荐用法，
    因为属性填错会一路带到发布，而 dry-run 几乎零成本。

    apply=True 且 use_cache=True 时先试默认属性快路径（_try_default_attrs）：草稿里
    预填的属性值全都与商品相符就整个阶段短路——一个下拉都不点。不适用/判不正确则
    落回下面这条完整流程，一行逻辑都不跳。

    选项来自服务端接口（见 attributes/server_options），rowid 是要传给它的草稿 id，
    cat_id 是页面上当前生效的叶子类目 id（阶段③ 选定后经 ctx 传下来）——类目是运行中
    改的、还没保存，接口只能按已保存的类目回答，不传就会查出上一版类目的属性清单。
    写入失败的行走 _retry_row 原值再试一次，不重读选项（选项当次现取、不存在过期）。

    site 是发布站点（中文站点名），喂给两条路径的 LLM 判据：插头规格、工作电压这类字段
    由目标市场的电气标准决定，源商品信息里推不出来——1688/国内源商品必然是中规两插
    220V，照抄到北美站买家插不上（2026-09-12 商品 pdd-250293857545 的哥伦比亚站即此）。

    成分比例防漂移（原脚本踩的坑）：LLM 两次调用结果会漂移（55/45 变成 90/10）。
    2026-08-21 起改成【源值确定性覆盖】：主面料成分字段的纤维与百分比按
    product-info.json 的 mainComposition（源页面「主面料成分含量」解析结果）重建，
    模型给的数值一律丢弃，故不再需要调用方事后回读修正。源没写含量时（mainComposition
    为 {}）仍退回模型给数 + 合计 100% 校验的老路径。
    """
    import json as _json

    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = _json.load(f)
    # 【2026-09-02】整合所有来源的成分信息（详情文字 > 详情图 > 源属性 > 默认值）。
    # 只依赖 info，故提到 dump_attrs 之前——下面的默认属性快路径要用它做判断依据。
    main_comp = attributes_composition._merge_composition_sources(info)

    # 默认属性快路径：草稿预填值全都相符时整个阶段④直接返回（见 _try_default_attrs）。
    # 【只在 apply=True 时走】dry-run 的用途就是出完整清单供人工过目，不该省这一步。
    # 【受 use_cache 控制】与类目那边同口径：勾掉「使用缓存」是要全量重判，草稿预填值
    # 也是「既有值」，不该被信任。
    # 这里传的是【未归约】的 main_comp：归约主纤维要读表单 options（见下方 _resolve_
    # main_fiber），排在快路径之后；对判断而言它只是背景信息，归不归约不影响判据。
    if apply and use_cache:
        hit = await _try_default_attrs(session, info, main_comp, site)
        if hit:
            return hit

    dump = await attributes_form.dump_attrs(session, required_only=required_only,
                                            rowid=rowid, cat_id=cat_id)
    attrs = dump["attrs"]
    if not attrs:
        # 属性行空着喂给 LLM 只会逼它瞎编（2026-08-21 实测 grok 收到空表单后
        # 开始「去工作区找字段」并吐出 shell 调用残骸），不如直接失败可续跑
        return {"status": "fail",
                "reason": "编辑页属性行读出来是空的（页面未渲染完或类目丢失？），可续跑"}
    # kind/numHint 必须一起喂：数值输入行（里料克重 g/m²）没有 options，模型看不到
    # kind 就会给个选项文本或带单位的字符串，两者都会被数值校验拒掉、该行填不上。
    rows = [{"label": a["label"], "current": a.get("current"),
             "required": bool(a.get("required")),
             "kind": a.get("kind") or "select",
             "numHint": a.get("numHint"),
             "numValues": a.get("numValues"), "options": a.get("options", [])}
            for a in attrs]

    # 【2026-09-08 新增】源主面料成分是「棉混纺」这类合成词/占位词、融合后仍解析不出
    # 确定主纤维（fiber 空）且非按款式区分时，单开一次聚焦 LLM 推理归约主纤维
    # （不硬编码词表）；归约出选项写法后回填，走 _rebuild_main_comp 的确定性比例覆盖。
    # options 取「含成分」的主面料字段（上装/下装成分）——表单里「材质」可能是弹力档位
    # 而非纤维列表（见 _is_main_comp_label 说明），若只剩材质字段再回退取它。
    if main_comp and not main_comp.get("fiber") and not main_comp.get("byVariant"):
        comp_opts = next((a["options"] for a in attrs
                          if "成分" in a["label"] and a.get("options")
                          and attributes_composition._is_main_comp_label(a["label"])), None)
        if not comp_opts:
            comp_opts = next((a["options"] for a in attrs
                              if attributes_composition._is_main_comp_label(a["label"]) and a.get("options")), None)
        if comp_opts:
            main_comp = await attributes_composition._resolve_main_fiber(info, main_comp, comp_opts)
    comp_src = main_comp.get("source", "attributes")
    if comp_src == "descText":
        logger.info(f"成分取自详情文字：{main_comp.get('fiber')} {main_comp.get('percent')}%"
                    + (f"，另有 {len(main_comp.get('byVariant', {}))} 款式分组"
                       if main_comp.get('byVariant') else ""))
    elif comp_src == "vision":
        logger.info(f"成分取自详情图识别：{main_comp.get('fiber')} {main_comp.get('percent')}%"
                    + (f"，另有 {len(main_comp.get('byVariant', {}))} 款式分组"
                       if main_comp.get('byVariant') else ""))

    decision = await attributes_review._ask_attr_review(rows, info, main_comp, site)
    valid, rejected, keep_current = attributes_validation._validate_attr_changes(
        decision.get("changes", []), attrs, main_comp)
    logger.info(
        f"LLM 审核完成：建议改 {len(valid)} 项"
        + (f"，保持原值 {len(keep_current)} 项" if keep_current else "")
        + (f"，驳回 {len(rejected)} 项" if rejected else "")
        + ("，开始逐项写入…" if apply and valid else ""))
    result = {"status": "ok", "attrCount": len(rows), "proposed": valid,
              "rejected": rejected, "keepCurrent": keep_current,
              "notes": decision.get("notes", []),
              "serverAttrs": dump.get("serverAttrs") or 0,
              "optionsMissed": dump.get("optionsMissed") or [],
              "mainComposition": main_comp or None}
    if not apply:
        result["applied"] = None
        return result

    row_map = {a["label"]: a for a in attrs}   # 查 optionsFrom / current / required
    applied, cache_refreshed, comp_failed = await attributes_review._apply_attr_changes(
        session, valid, row_map, info)
    result["applied"] = applied
    result["cacheRefreshed"] = cache_refreshed
    result["compFailed"] = comp_failed

    # ---- 联动补填轮 --------------------------------------------------------
    # 改「里料纹理」为「光面」会联动新增必填行（里衬成分、里料克重 g/m²）。这些行在
    # 第一轮 dump_attrs 时【还不存在】，所以第一轮的清单里必然没有它们，留空就卡保存。
    # 原实现只把它们记进 linkageNewRequired 交出去，而 service 层并不消费这个字段
    # （2026-08-25 用户截图：里料克重、里衬成分两行空着）——检测到了却没人填。
    # 故这里补一轮「重扫 → 读选项 → 问 LLM → 写入」，闸门与主轮完全共用。
    #
    # 【补填是多轮的】里料纹理只要不选「无内衬/无里料」就必然联动，而补填写进去的值
    # 会再带出下一层行（里衬成分选定后出现其百分比）。故 _fill_linkage_rows 内部循环
    # 重扫直到不再冒新行，上限 _LINKAGE_MAX_ROUNDS 轮防耗时失控；仍有残留的由下面
    # 的必填复扫报人工。别改回「只跑一轮」——那会让第二层行静默留空到保存才炸。
    # 【pre_labels 只收第一轮可见的行】隐藏行（visible=False，如「材质」依赖「是否纺
    # 织品」这类开关字段、开关未选时行不显示）第一轮 dump_attrs 会跳过它们的 options、
    # 主轮 LLM 无从填值；若把它们的 label 也塞进 pre_labels（即 seen），补填轮会因
    # 「label 已在 seen」而永远不重扫——等主轮改选开关、这些行联动显示出来时，就只能
    # 留空到末尾复扫报人工（2026-09-04 猫窝「材质」漏填即此）。只收可见行，隐藏行
    # 会在联动显示后进入补填轮正常补上。
    fill = await _fill_linkage_rows(
        session, {a["label"] for a in attrs if a.get("visible") is not False},
        info, main_comp, rowid, cat_id, site)
    result["linkageFilled"] = fill.get("applied") or []
    result["linkageNewRequired"] = fill.get("newRequired") or []
    if fill.get("compFailed"):
        result["compFailed"] = list(comp_failed) + list(fill["compFailed"])

    # 末尾复扫：补填轮之后仍空着的必填行，一律报出来（含补填没救回来的）。
    # 这是本阶段交给调用方的唯一「还差什么」清单，service 层据此提示人工。
    rows3 = await session.eval_json(attributes_form._JS_LIST_ATTR_ROWS)
    attrs3 = rows3.get("attrs", [])
    result["unfilledRequired"] = [
        a["label"] for a in attrs3
        if a.get("required") and a.get("visible") is not False
        and str(a.get("current") or "").startswith("(")
    ]
    # 成分合计复验：与「必填是否留空」并列的第二道收尾闸。上面那道只看 current 是不是
    # 占位符，而成分行的 current 是纤维名（有值），百分比再离谱也扫不出来——2026-09-12
    # 商品 908737332112 的 117% 就是这么穿过整个阶段④ 的。判据与两条来路见
    # attributes_form._comp_total_problems。
    result["badCompTotals"] = attributes_form._comp_total_problems(attrs3)
    parked = await attributes_dropdowns._park_ghost_dropdowns(session)
    result["parkedGhosts"] = parked.get("parked", 0)
    return result
