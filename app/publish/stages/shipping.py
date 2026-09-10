"""店小秘发布共用能力：stages.shipping。各来源流程由 workflows/ 独立定义。"""

from app.publish.browser import BrowserSession
from app.publish.shipping import set_shipping


async def _st_shipping(ctx: dict, session: BrowserSession, emit) -> dict:
    r = await set_shipping(session)  # 不给 deadline：按 SKILL.md 规则选最长时效
    if r.get("status") != "ok":
        return {"status": "fail",
                "note": f"[{r.get('stage')}] {(r.get('reason') or '')}"[:200]}
    return {"status": "ok",
            "note": f"时效 {r.get('deadline')} | 模板 {r.get('freightTemplate')}"}
