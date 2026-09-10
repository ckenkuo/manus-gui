"""店小秘发布操作：attributes.workflow。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish import cache
from app.publish.attributes import (
    composition as attributes_composition,
    dropdowns as attributes_dropdowns,
    form as attributes_form,
    review as attributes_review,
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


async def _read_linkage_options(session: BrowserSession, new_rows: list, cat_path,
                                use_cache: bool, site: str) -> None:
    """给联动行就地补上 options（原地改 new_rows），规矩与主轮 dump_attrs 一致。

    缓存优先、未命中现场读并回灌；数值行不读选项——它行内的 select 是只读单位，
    点开读到的是单位清单（见 dump_attrs 里的 number-row 分支）。
    """
    cached = (cache.load_attr_options(cat_path[-1], cat_path, site)
              if use_cache and cat_path else {})
    for a in new_rows:
        a["options"] = []
        if a.get("kind") == "number":
            a["optionsEmptyReason"] = "number-row"
            continue
        hit = cached.get(a["label"])
        if hit:
            a["options"] = hit
            a["optionsFrom"] = "cache"
            continue
        opts, meta = await attributes_dropdowns._read_active_options(session, a["label"], with_meta=True)
        logger.info(f"读选项（联动行）：{a['label']} -> {len(opts)} 个"
                    + ("" if meta["complete"] else "（未滚到底，不进缓存）"))
        if opts:
            a["options"] = opts
            a["optionsFrom"] = "live"
            a["optionsComplete"] = meta["complete"]
        else:
            a["optionsEmptyReason"] = "open-failed"
        await asyncio.sleep(0.3)
    if use_cache and cat_path:
        cache.save_attr_options(cat_path[-1], cat_path, new_rows, site)


async def _fill_linkage_round(session: BrowserSession, new_rows: list, info: dict,
                              main_comp: Optional[dict], cat_path,
                              use_cache: bool, site: str) -> tuple:
    """补填一轮：读选项 → 问 LLM → 校验 → 写入，返回 (applied, compFailed)。

    与主轮共用 _validate_attr_changes 与 _apply_attr_changes：闸门（非必填留空、
    options 校验、数值量级、成分合计 100）一条都不能少，另写一套必然与主轮漂移。
    成分行同理走 row/num 那套，故这里不做「每行只问一次」的简化。

    【best-effort】读不到行、LLM 不给值、写入失败一律只记录不抛，交末尾复扫报人工。
    """
    await _read_linkage_options(session, new_rows, cat_path, use_cache, site)
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
        decision = await attributes_review._ask_attr_review(ask_rows, info, main_comp)
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
        session, valid, {a["label"]: a for a in new_rows}, info, cat_path,
        use_cache=use_cache, site=site)
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    logger.info(f"本轮补填完成：{ok_n}/{len(applied)} 项写入成功")
    return applied, comp_failed


async def _fill_linkage_rows(session: BrowserSession, pre_labels: set, info: dict,
                             main_comp: Optional[dict], cat_path,
                             use_cache: bool = True, site: str = "") -> dict:
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
        applied, comp_failed = await _fill_linkage_round(
            session, new_rows, info, main_comp, cat_path, use_cache, site)
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


async def check_attrs(session: BrowserSession, info_path: str,
                      apply: bool = False, required_only: bool = True,
                      cat_path=None, use_cache: bool = True,
                      site: str = "") -> dict:
    """阶段④：LLM 比对源商品信息与表单属性，产出修改清单（apply=True 时执行）。

    不导航——须紧跟 auto_cat 在同一页执行（类目决定属性行）。
    apply=False 是 dry-run：只出清单不动表单，供人工过目。这是本阶段的推荐用法，
    因为属性填错会一路带到发布，而 dry-run 几乎零成本。

    cat_path + use_cache 透传给 dump_attrs 用作 options 缓存的键（见那边的说明）。
    命中缓存的行若写入失败，走 _refresh_row_and_retry 只重读该行——缓存过期的表现
    就是「点不中」，而 set_attr 本来就会回读校验发现它。

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
    dump = await attributes_form.dump_attrs(session, required_only=required_only,
                            cat_path=cat_path, use_cache=use_cache, site=site)
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

    # 【2026-09-02 改】整合所有来源的成分信息（详情文字 > 详情图 > 源属性 > 默认值）
    main_comp = attributes_composition._merge_composition_sources(info)
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

    decision = await attributes_review._ask_attr_review(rows, info, main_comp)
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
              "cacheRead": dump.get("cacheRead") or 0,
              "activeRead": dump.get("activeRead") or 0,
              "mainComposition": main_comp or None}
    if not apply:
        result["applied"] = None
        return result

    row_map = {a["label"]: a for a in attrs}   # 查 optionsFrom / current / required
    applied, cache_refreshed, comp_failed = await attributes_review._apply_attr_changes(
        session, valid, row_map, info, cat_path, use_cache=use_cache, site=site)
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
        info, main_comp, cat_path, use_cache=use_cache, site=site)
    result["linkageFilled"] = fill.get("applied") or []
    result["linkageNewRequired"] = fill.get("newRequired") or []
    if fill.get("compFailed"):
        result["compFailed"] = list(comp_failed) + list(fill["compFailed"])

    # 末尾复扫：补填轮之后仍空着的必填行，一律报出来（含补填没救回来的）。
    # 这是本阶段交给调用方的唯一「还差什么」清单，service 层据此提示人工。
    rows3 = await session.eval_json(attributes_form._JS_LIST_ATTR_ROWS)
    result["unfilledRequired"] = [
        a["label"] for a in rows3.get("attrs", [])
        if a.get("required") and a.get("visible") is not False
        and str(a.get("current") or "").startswith("(")
    ]
    parked = await attributes_dropdowns._park_ghost_dropdowns(session)
    result["parkedGhosts"] = parked.get("parked", 0)
    return result
