"""店小秘发布共用能力：stages.saving。各来源流程由 workflows/ 独立定义。"""

from app.publish.browser import BrowserSession
from app.publish.persistence import publish_now
from app.publish.saving import save
from app.publish.stages import results as stages_results
from app.publish.media.preview import sku_preview_state
from app.publish.stock import _warehouse_ready, resolve_warehouse
from app.publish.stock_scripts import _JS_WH_STATE


async def _st_save(ctx: dict, session: BrowserSession, emit) -> dict:
    expected_warehouse = ctx.get("warehouse") or resolve_warehouse(ctx.get("site") or "")
    warehouse = await session.eval_json(_JS_WH_STATE)
    if not _warehouse_ready(warehouse, expected_warehouse):
        return {"status": "fail", "note": f"保存前站点仓库校验未过：{expected_warehouse}，请重跑⑪ 库存SKU"}
    preview = await sku_preview_state(session)
    if preview.get("supported") is None and preview.get("err") != "no-preview-column":
        return {"status": "fail", "note": "保存前无法读取预览图状态，请等待变种表加载后重试"}
    invalid = [row for row in preview.get("rows", []) if row.get("empty") or row.get("bad")]
    if invalid:
        rows = "、".join(f"第 {row['i'] + 1} 行「{row.get('color') or ''}」" for row in invalid[:8])
        return {"status": "fail", "note": f"保存前预览图校验未过：{rows}，请重跑⑦b SKU预览图"}
    r = await save(session, ctx["rowid"])
    if r.get("status") != "ok":
        # 【别把已经抓到的证据丢掉】save 的 validation-error 有两条来路：走 explain-error
        # 的回 redSections/fieldErrors/errors（没有 reason 键），走校验后 toast 与更新时间
        # 判定的才回 reason。原先只取 `red or reason`，红锚点这个名字没识别出来时，页面
        # 上明明抓到的字段错误整批被丢弃，结论拼成「校验未过：None」——查不下去
        # （2026-09-10 两单 984422638420、pdd-992556805954 就是这个）。
        red = "、".join(s["name"] for s in (r.get("redSections") or []))
        why = (red
               or "、".join(str(x) for x in (r.get("fieldErrors") or []))
               or "；".join(str(x) for x in (r.get("errors") or []))
               or r.get("reason"))
        await emit({"type": "manual_check", "stage": "save",
                    "message": f"保存校验未过：{why}——草稿未落库，处理后可续跑"})
        return {"status": "fail", "note": f"校验未过：{why}"[:200]}
    ut = r.get("updateTime") or {}
    return {"status": "ok",
            "note": f"已保存（未发布）更新时间 {ut.get('before')} → {ut.get('after')}"}


async def _st_publish(ctx: dict, session: BrowserSession, emit) -> dict:
    """阶段⑮：点「发布」→「立即发布」真正上架。

    【默认跳过】do_publish 没显式开就 skipped——发布不可逆，闸门必须在人手上
    （见模块头「发布闸门」）。前置是 ⑭ save 成功：草稿没落库点发布只会重复撞同一批
    前端校验，故这里再核一次状态，不满足就 skipped 而非 fail（不是本阶段的错）。
    """
    if not ctx.get("do_publish"):
        # 文案要指得出开关在哪：2026-08-25 用户走 Web 跑完问「为什么没自动点发布」，
        # 当时页面上确实没有开关、文案却写「UI 勾选后才执行」，等于让人去找一个不存在
        # 的复选框。现在两个入口都有开关，故两个都点明。
        return {"status": "skipped",
                "note": "未开启发布（Web 页「自动发布」开关 / CLI --publish）"}

    saved = ((ctx.get("state") or {}).get("stages", {}).get("save") or {}).get("status")
    if saved not in stages_results._DONE:
        return {"status": "skipped",
                "note": f"⑭ 保存未成功（{saved or '未跑'}），不发布"}

    r = await publish_now(session, ctx["rowid"], confirm=True)
    st = r.get("status")
    if st == "ok":
        return {"status": "ok",
                "note": f"已发布：{'；'.join(r.get('messages') or []) or '已离开编辑页'}"[:200]}

    if st == "validation-error":
        red = "、".join(s["name"] for s in (r.get("redSections") or []))
        await emit({"type": "manual_check", "stage": "publish",
                    "message": f"发布校验未过：{red or r.get('errors')}"
                               f"——草稿已落库未上架，处理后可续跑"})
        return {"status": "fail", "note": f"发布校验未过：{red or r.get('errors')}"[:200]}

    if st == "unknown":
        # 点下去了但没抓到成功提示：不敢判成功（可能真上架了），交人工看一眼
        await emit({"type": "manual_check", "stage": "publish",
                    "message": f"发布结果判据不足（已点「立即发布」但未捕获提示）："
                               f"{r.get('messages')}——请到列表确认是否已上架"})
        return {"status": "fail", "note": "发布结果判据不足，需人工确认"}

    await emit({"type": "manual_check", "stage": "publish",
                "message": f"发布失败：{str(r)[:200]}"})
    return {"status": "fail", "note": f"发布失败：{str(r)[:200]}"}
