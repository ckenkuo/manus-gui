"""店小秘发布共用能力：stages.form。各来源流程由 workflows/ 独立定义。"""

import asyncio
from app.logger import logger
from app.publish import state
from app.publish.attributes.workflow import check_attrs
from app.publish.browser import BrowserSession
from app.publish.category import auto_cat
from app.publish.claim import collect_and_claim
from app.publish.navigation import open_edit
from app.publish.stages import prewarm_access as stages_prewarm_access
from app.publish.titles import set_titles


async def _st_claim(ctx: dict, session: BrowserSession, emit) -> dict:
    note = ""
    if not ctx.get("rowid"):
        # 【Temu 源跳过「链接采集」，只做认领】店小秘服务端的链接采集不支持 Temu：
        # 2026-09-10 实测对 temu.com 链接返回「已执行1条，成功:0，跳过:0，失败:1」，
        # 采集记录压根不产生，随后认领必然报「采集列表未找到商品」——那句提示还会
        # 诱人反复重跑，而重跑永远不会成。Temu 商品要用店小秘的浏览器插件在官网上采
        # （用户既有做法，采完先进采集箱），那一趟人工本来就要打开商品页，与 ① 复用
        # 页签取数共用同一批页签。故这里只认领、不重复提交那个注定失败的链接。
        skip_crawl = ctx.get("source_platform") == "temu"
        try:
            r = await collect_and_claim(session, ctx["url"], ctx["title"],
                                        ctx["store"], ctx["site"],
                                        skip_crawl=skip_crawl)
        except RuntimeError as e:
            # 认领的前提是商品已在采集箱里。缺了就给一条能照着做的提示，而不是让
            # 「采集可能还没完成，稍后重跑」把人引向无限重试。
            if skip_crawl and "采集列表未找到商品" in str(e):
                raise RuntimeError(
                    "采集箱里没有这条 Temu 商品，无法认领——请先用店小秘浏览器插件在 "
                    f"Temu 官网采集它（采完会先进采集箱），再重跑本商品。原始信息：{e}"
                ) from e
            raise
        if not r.get("rowid"):
            return {"status": "fail", "note": "认领后未取到 rowid（采集列表同步延迟？可续跑）"}
        ctx["rowid"] = r["rowid"]
        note = f"新建草稿 rowid={r['rowid']}"
    else:
        note = f"rowid={ctx['rowid']}（任务自带，跳过采集认领）"
    # 此后直到 save 全程不刷新页面（open_edit 会丢未保存修改）
    await open_edit(session, ctx["rowid"])
    await asyncio.sleep(2)
    await session.fix_hidden_tab()
    return {"status": "ok", "note": note}


async def _st_auto_cat(ctx: dict, session: BrowserSession, emit) -> dict:
    # info 整份读进来（不只取 title）：类目判断除了标题还要年龄段/尺码线索，
    # 见 pipeline.cat_clues 上方的 2026-08-29 取证。读不到就只用标题，不失败。
    info = None
    if ctx.get("info_path"):
        try:
            info = state._load_info(ctx["info_path"])
        except Exception as e:
            logger.warning(f"读 product-info.json 取类目线索失败（只用标题判类目）：{e}")
    title = ctx.get("title") or (info or {}).get("title")
    if not title:
        return {"status": "fail", "note": "缺商品标题（LLM 判断类目要用）"}
    ctx["title"] = title
    r = await auto_cat(session, ctx["rowid"], title,
                       use_cache=ctx.get("use_cache", True),
                       site=ctx.get("site") or "",
                       info=info)  # 失败抛异常，交外层
    # 阶段④的属性缓存要按类目路径取，这里把它落进 ctx（并经回写元组进状态文件，
    # 续跑 from attrs 时才拿得到）
    ctx["cat_path"] = r.get("pathList") or []
    # 叶子 catId 同样落进 ctx：阶段④ 拿它查该类目的属性选项。类目是【运行中改的、
    # 没保存】，服务端只认草稿里已保存的那版——2026-09-11 实测按旧类目查出的是电子
    # 类属性，页面上要填的动态属性一个都没有。走默认类目快路径时它是空串（类目没变，
    # 按草稿查就是对的），拿不到就当空处理，绝不臆造一个 id。
    ctx["cat_id"] = str(r.get("leafCatId") or "")
    # note 里记明走的是默认类目/缓存还是遍历：两条快路径选错了下游没有任何校验能发现，
    # 事后核对全靠这一条（见 _try_cached_category 的风险注释）
    src = {"cache": "缓存", "default": "默认类目"}.get(r.get("source"), "遍历")
    return {"status": "ok", "note": f"[{src}] {r.get('path') or ''}"}


async def _st_attrs(ctx: dict, session: BrowserSession, emit) -> dict:
    if not ctx.get("info_path"):
        return {"status": "fail", "note": "缺 product-info.json（rowid 模式必须带 info_path）"}
    r = await check_attrs(session, ctx["info_path"], apply=True,
                          use_cache=ctx.get("use_cache", True),
                          rowid=str(ctx.get("rowid") or ""),
                          cat_id=str(ctx.get("cat_id") or ""),
                          site=str(ctx.get("site") or ""))
    if r.get("status") != "ok":
        return {"status": "fail", "note": (r.get("reason") or str(r))[:200]}
    applied = r.get("applied") or []
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    if r.get("source") == "default":
        # 这条路径一行都没动、一个下拉都没点：note 里写明，免得看日志的人以为漏跑了
        note = f"默认属性 {r.get('attrCount') or 0} 行经 LLM 判定均相符，未做改动"
    else:
        note = f"改 {ok_n}/{len(applied)} 项，LLM 拒 {len(r.get('rejected') or [])} 项"
        if r.get("keepCurrent"):
            # 【必须记进 note】「保持原值」= LLM 判定这行已经对了、不用改。它既不进
            # applied 也不进 rejected，原先是全阶段唯一一处「只发生在日志里、状态文件和
            # 数据库都看不到」的结果：note 才是写进状态文件、跨机可见的那一份，两台机器
            # 分别跑同一商品时，另一台上只有这一行能看出模型把哪些字段判成了不用动。
            # 对成分字段它还是个信号——主面料成分若被判「保持原值」，会连同该字段的
            # 合计校验一起被绕过（详见 _comp_total_problems 的两条来路），此时 note 里
            # 这个数字是现场唯一的旁证。
            note += f"，保持原值 {len(r['keepCurrent'])} 项"
    if r.get("optionsMissed"):
        # 【必须报出来】这些行在服务端属性清单里没有对应项，选项是空的——不报的话
        # 现场只剩一句「改 0/0 项」，看不出是模型没给值还是这行压根没选项可给。
        note += f"，{len(r['optionsMissed'])} 行无选项（{ '、'.join(r['optionsMissed'][:4]) }）"
    if r.get("cacheRefreshed"):
        note += f"，过期重读 {len(r['cacheRefreshed'])} 行"
    if r.get("linkageFilled"):
        # 联动新增的必填行（里料纹理选「光面」带出的里衬成分/里料克重）由 check_attrs
        # 补填轮处理，这里只把结果记进 note——不记的话看日志完全不知道跑过这一轮。
        lk = r["linkageFilled"]
        lk_ok = sum(1 for a in lk if a.get("result") == "ok")
        note += f"，联动补填 {lk_ok}/{len(lk)} 项"
    if r.get("compFailed"):
        # 成分组不做单行重试（会破坏合计 100%），交人工
        await emit({"type": "manual_check", "stage": "attrs",
                    "message": f"成分字段写入失败需人工核对：{'、'.join(r['compFailed'])}"})
    if r.get("unfilledRequired"):
        # 【必填项留空会直接卡保存】所以这条必须提示到人，不能像原先那样只放在返回值里
        # 没人看（2026-08-25 用户截图的里料克重/里衬成分就是这么漏过去的）。
        miss = r["unfilledRequired"]
        await emit({"type": "manual_check", "stage": "attrs",
                    "message": f"必填属性仍留空需人工补：{'、'.join(miss)}"})
        # 【如实判 fail，不再静默放过】原先这里只发提示就 return ok，结果是④放过、
        # 后面⑨⑩⑪⑫⑬⑬b 十几个阶段白跑，到⑭ save 才报「产品信息校验未过」，那时
        # 现场早离开属性页（2026-09-11 商品 1052052060281 的「颜色」即此；与⑦b预览图
        # 2026-09-01 两单是同一个静默放过模式，见 preview.py 的复盘）。判 fail 后交给
        # Manus ReAct 兜底：agent 用 dxm_attribute_open/options/click 补选，再复跑
        # dxm_stage_attrs —— 复跑走的还是本函数，故「必填是否还空」是主流程与兜底
        # 共用的唯一判据，补不上就一路 fail 到商品终止，不会带着空属性往下跑。
        # 【清单放 note 开头】service 与状态文件都会把 note 截到 200 字符（service.py
        # 的 note[:200]），改写统计被截掉无所谓，「还差哪几行」被截掉 agent 就没方向了。
        # unfilledRequired 另以结构化字段透出：recover_stage 把整个 failure 字典塞进
        # 给 agent 的 request，故那条路不受 200 字符限制。
        return {"status": "fail",
                "note": f"必填仍空：{'、'.join(miss)}；{note}"[:200],
                "unfilledRequired": miss}
    if r.get("badCompTotals"):
        # 【成分合计不对，同样如实判 fail】这是本阶段第二道收尾闸，与上面那道并列。
        # 平台对成分字段的硬校验是「百分比之和恰好 100」，合计不对保存必被拦下——不在这里
        # 判失败，就得等⑭ 才暴露，中间十几个阶段白跑，而那时现场早离开属性页。行级判据
        # 抓不到它：成分行的 current 是纤维名不是占位符，必填复扫扫不出来（2026-09-12
        # 商品 908737332112 的 117% 就是这么漏过去的，判据与两条来路见
        # attributes_form._comp_total_problems）。
        # 清单放 note 开头：note 会被截到 200 字符，改写统计被截掉无所谓，「哪个字段差多少」
        # 被截掉 agent 就没方向了；badCompTotals 另以结构化字段透出，不受 200 字符限制。
        bad = "、".join(f"{p['label']} {p['total']:g}%" for p in r["badCompTotals"])
        await emit({"type": "manual_check", "stage": "attrs",
                    "message": f"成分百分比合计不等于 100 需人工修正：{bad}"})
        return {"status": "fail", "note": f"成分合计不等于 100：{bad}；{note}"[:200],
                "badCompTotals": r["badCompTotals"]}
    return {"status": "ok", "note": note}


async def _st_titles(ctx: dict, session: BrowserSession, emit) -> dict:
    # 预热命中直接填，省掉这里最贵的一次调用（标题生成含最多 2 轮，实测单次可达 100s+）
    gen = await stages_prewarm_access._await_prewarm(ctx, "titles")
    generated = (gen or {}).get("generated") if (gen or {}).get("status") == "ok" else None
    r = await set_titles(session, ctx["info_path"], generated=generated)
    if r.get("status") != "ok":
        # 带上 err（具体拒因）：只报 title-generation-failed 时排查必须去翻日志，
        # 而阶段结果是写进状态文件、UI 也直接显示的那一份，原因得在这里就看得见。
        note = (r.get("reason") or "")
        if r.get("err"):
            note += f" | {r['err']}"
        return {"status": "fail", "note": note[:300]}
    g = r.get("generated") or {}
    return {"status": "ok", "note": f"英文 {(g.get('enTitle') or '')[:40]}"}
