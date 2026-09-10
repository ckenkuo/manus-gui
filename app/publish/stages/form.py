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
        r = await collect_and_claim(session, ctx["url"], ctx["title"],
                                    ctx["store"], ctx["site"])
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
    # note 里记明走的是缓存还是遍历：缓存快路径选错了下游没有任何校验能发现，
    # 事后核对全靠这一条（见 _try_cached_category 的风险注释）
    src = "缓存" if r.get("source") == "cache" else "遍历"
    return {"status": "ok", "note": f"[{src}] {r.get('path') or ''}"}


async def _st_attrs(ctx: dict, session: BrowserSession, emit) -> dict:
    if not ctx.get("info_path"):
        return {"status": "fail", "note": "缺 product-info.json（rowid 模式必须带 info_path）"}
    r = await check_attrs(session, ctx["info_path"], apply=True,
                          cat_path=ctx.get("cat_path"),
                          use_cache=ctx.get("use_cache", True),
                          site=ctx.get("site") or "")
    if r.get("status") != "ok":
        return {"status": "fail", "note": (r.get("reason") or str(r))[:200]}
    applied = r.get("applied") or []
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    note = f"改 {ok_n}/{len(applied)} 项，LLM 拒 {len(r.get('rejected') or [])} 项"
    if r.get("cacheRead"):
        note += f"，缓存选项 {r['cacheRead']} 行"
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
        note += f"，仍空 {len(miss)} 项必填"
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
