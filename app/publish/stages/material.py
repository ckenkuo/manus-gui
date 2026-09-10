"""店小秘发布共用能力：stages.material。各来源流程由 workflows/ 独立定义。"""

import os
from app.publish import images, state, vision
from app.publish.browser import BrowserSession
from app.publish.media.materials import set_material


async def _st_material(ctx: dict, session: BrowserSession, emit) -> dict:
    info = state._load_info(ctx["info_path"])
    # 【⑥⑦ 刻意不预热】pick_material 在 complianceNotes 非空时完全不看图、只按标注选
    # （见 vision.pick_material），而那份标注正是 ⑤b 清理改写的对象：在 ① 之后预热拿到的
    # 是「无干净图 → 兜底取最不脏的一张 + uncertain」，等于把 ⑤b 的成果作废、还多报一次
    # 人工确认。plan_skc 的单色分支同样按 clean 排序决定首位主图。
    # 提速改由「把 ⑤b 清理整段提前」实现（见 _start_prewarm）：生图与 ②③ 重叠，而这里
    # 保持现场判断、读到的是清理后的标注。
    plan = await vision.pick_material(info, ctx["workdir"])
    if plan.get("status") != "ok":
        return {"status": "fail", "note": plan.get("reason") or "无可用素材图"}
    if plan.get("uncertain"):
        await emit({"type": "manual_check", "stage": "material",
                    "message": f"素材图选择没把握：{plan.get('reason')}（已用 "
                               f"{os.path.basename(plan['image'])} 继续）"})
    sq = images.square_image(plan["image"],
                             out_path=os.path.join(ctx["workdir"], "material-square.jpg"))
    r = await set_material(session, sq["output"])
    if r.get("status") != "ok":
        return {"status": "fail", "note": f"替换失败[{r.get('stage')}]: {str(r)[:150]}"}
    return {"status": "ok",
            "note": f"{os.path.basename(plan['image'])} → {sq['outSize']}（{plan.get('reason') or ''}）"[:200]}
