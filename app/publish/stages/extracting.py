"""店小秘发布共用能力：stages.extracting。各来源流程由 workflows/ 独立定义。"""

import os
from app.publish import extract, state, workflows
from app.publish.browser import BrowserSession


async def _st_extract(ctx: dict, session: BrowserSession, emit) -> dict:
    # 【显式 from_stage=extract 必须真的重提，不能被旧产物挡住】原先只判 info_path
    # 有值就 skipped，于是落盘产物本身错了的商品无路可走：状态文件回填 info_path
    # （见 publish_one 的 ctx 构造），指定 from_stage 也照样跳过。
    # 2026-08-28 offer 1014675972015 就撞在这里——单维 spec 的 bug 修好后，该商品
    # 磁盘上那份 skus={} 的 product-info.json 仍会被原样沿用，重跑几次都是同一个错。
    # from_stage 是人工显式判断（publish_one 里「显式指定起点不该被实况覆盖」同一取向），
    # 点名 ① 就是要重新抓一遍源数据。rowid 模式（无 url）没得可提，仍然跳过。
    redo = ctx.get("from_stage") == "extract" and ctx.get("url")
    if ctx.get("info_path") and not redo:
        info = state._load_info(ctx["info_path"])
        workflow = workflows.resolve_workflow(ctx, ctx.get("state"), info)
        if workflow is None:
            return {"status": "fail", "note": "商品来源不明确，请提供来源 URL 或 source_platform"}
        ctx["workflow"] = workflow
        ctx["source_platform"] = workflow.platform
        ctx["workflow_id"] = workflow.workflow_id
        ctx["workdir"] = os.path.dirname(os.path.abspath(ctx["info_path"]))
        return {"status": "skipped", "note": "任务自带 product-info.json"}
    if redo and ctx.get("info_path"):
        await emit({"type": "log", "stage": "extract", "level": "info",
                    "message": "指定从 ① 起重跑：重新抓取源数据并覆盖 product-info.json"})
    # 源站弹滑块/验证码时把提示转成 manual_check 事件：Web 页面会多出一条人工检查、
    # CLI 打「! 人工检查」行。1688 会原地等人拖滑块过关（extract.wait_human_verify），
    # 拼多多/Temu/亚马逊没有可拖的东西，只报错交人处理（见各适配器）。
    async def _on_manual(message: str) -> None:
        await emit({"type": "manual_check", "stage": "extract", "message": message})

    r = await extract.extract_product(ctx["url"], session=session, enrich=True,
                                      on_manual=_on_manual)
    if r.get("status") != "ok":
        return {"status": "fail", "note": f"提取失败: {r}"[:200]}
    ctx["info_path"] = r["infoPath"]
    ctx["workdir"] = r["outdir"]
    ctx["title"] = r.get("title") or ctx.get("title")
    workflow = workflows.resolve_workflow(ctx, ctx.get("state"), state._load_info(ctx["info_path"]))
    if workflow is None:
        return {"status": "fail", "note": "提取结果缺少商品来源"}
    ctx["workflow"] = workflow
    ctx["source_platform"] = workflow.platform
    ctx["workflow_id"] = workflow.workflow_id
    if r.get("visionError"):
        await emit({"type": "manual_check", "stage": "extract",
                    "message": f"视觉回填失败（图片阶段将现场看图）：{r['visionError'][:100]}"})
    return {"status": "ok",
            "note": f"属性 {r.get('attrCount')} 项 | 图 {r.get('mainImgs')}+{r.get('descImgs')}"}
