"""发布失败现场采集：把「跑到哪、跳过哪些、页面报了什么」随错误一起上报。

为什么需要它：多台 PC 并发跑发布，失败时错误上报只带一条 note（见 app/error_report.py），
在开发机上只能看到「某某校验未过」。而定位问题真正需要的信息大多已经在那台机器上了——
断点文件里有各阶段的结论与 Manus 兜底的工具调用历史，「本次计划跑哪些阶段、哪些按页面
实况判为需重跑」则连断点文件都不记、只在 SSE 事件流里一闪而过（事件不落盘，随进程消失）。
这些信息从不离开那台机器，跨机排查时等于不存在。本模块把它们采成一份 JSON 加一张失败页
截图，交给 error_report.report_snapshot 写库。

【依赖方向只能是「发布 → 本模块 → error_report」，不能反向】采集要碰 CDP 会话与断点文件，
若写进 error_report，则采集/订单/活动三条根本不碰浏览器的管线也会被迫 import Playwright。

best-effort（项目既定模式）：采不到就少几个字段，任何一步炸只 logger.warning。调用点全在
失败 return 之前，异常漏出去会把原来那条失败 note 冲掉，那是比少几个字段严重得多的事故。
"""

import asyncio
import base64
import json
import time
from typing import Any, Optional

from app import error_report
from app.logger import logger
from app.publish import state as publish_state
from app.publish.browser import recent_toasts
from app.publish.stages.results import _DONE

# ctx 白名单。【绝不能整体 json.dumps】ctx 里有 "state"（与断点文件同一个对象，会重复
# 一大份）、prewarm 与 prewarm_task（LLM 产物与 asyncio.Task，不可序列化）。
_CTX_KEYS = ("workflow_id", "source_platform", "url", "title", "rowid", "info_path",
             "workdir", "cat_path", "cat_id", "store", "site", "use_cache", "do_publish",
             "price", "keep_video", "warehouse", "from_stage")

# 断点文件里值得跨机看的部分；skc_done 单独排除的理由见 _condense_state。
# cat_id 在这里是因为兜底 agent 复跑阶段④ 要用它查属性选项（见 attributes/server_options）。
_STATE_KEYS = ("key", "status", "failed_stage", "title", "rowid", "info_path", "workdir",
               "cat_path", "cat_id", "source_platform", "workflow_id", "updated_at")

_HISTORY_STEPS = 20
_HISTORY_TEXT = 500

# 页面只读快照。选择器沿用 agent_tools.observe 里那套（已验证可用），不另造没验过的。
_JS_PAGE = r"""(() => ({
  url: location.href, title: document.title,
  text: (document.body.innerText || '').slice(0, 6000),
  dialogs: [...document.querySelectorAll('[role=dialog],.ant-modal')]
    .filter(el => el.getClientRects().length)
    .map(el => el.innerText.slice(0, 2000)).slice(0, 5)
}))()"""


def _brief(value: Any, limit: int = 500) -> Any:
    """把任意值压成可序列化的短字符串；None 与数值原样返回（缺席和空串要分得清）。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return (value if isinstance(value, str) else str(value))[:limit]


def _condense_recovery(recovery: dict) -> dict:
    """瘦身 Manus 兜底记录：history 只留最后 20 步、每步观察截 500 字符。

    原始每步 2000 字符，全灌进去会顶掉其他现场；20 步够看出模型试过哪个方向了。
    """
    recovery = recovery or {}
    history = []
    for item in (recovery.get("history") or [])[-_HISTORY_STEPS:]:
        item = item or {}
        step = {"tool": _brief(item.get("tool"), 100)}
        if item.get("error"):
            step["error"] = _brief(item["error"], _HISTORY_TEXT)
        elif item.get("result") is not None:
            step["result"] = _brief(item["result"], _HISTORY_TEXT)
        history.append(step)
    out = {key: _brief(recovery.get(key)) for key in ("status", "error") if recovery.get(key)}
    out["history"] = history
    if recovery.get("initial_failure") is not None:
        out["initial_failure"] = recovery["initial_failure"]
    return out


def _pick_recovery(state: dict, stage: str) -> Optional[dict]:
    """取失败阶段的兜底记录；该阶段没有就退到最后一个有记录的阶段。

    【为什么要退】商品可能「先在某阶段失败、兜底跑通、又在后一阶段失败」，此时失败
    阶段自己没有 recovery，而前一次兜底的过程恰恰是判断模型试过什么的关键。
    """
    stages = (state or {}).get("stages") or {}
    picked = (stages.get(stage) or {}).get("recovery")
    if picked:
        return _condense_recovery(picked)
    for value in reversed(list(stages.values())):
        if (value or {}).get("recovery"):
            return _condense_recovery(value["recovery"])
    return None


def _condense_state(state: dict) -> dict:
    """断点文件瘦身版：留排查用得上的，丢掉只服务本机缓存的。

    丢掉 skc_done（{颜色: [fileId…]} 只用来判断某行是否已是本批图，跨机排查用不上，
    却是全文件里最占地方的一块）；本地全量文件仍在 state_path 指的位置。
    """
    state = state or {}
    kept = {key: _brief(state.get(key)) for key in _STATE_KEYS if state.get(key) is not None}
    stages = {}
    for sid, value in (state.get("stages") or {}).items():
        value = value or {}
        stages[sid] = {"status": value.get("status"),
                       "elapsed_s": value.get("elapsed_s"),
                       "note": str(value.get("note") or "")[:200]}
    if stages:
        kept["stages"] = stages
    return kept


def _fit(snapshot: dict, max_kb: int) -> dict:
    """超限时按固定顺序降级，并把降级动作记进 trimmed。

    【只截字段，绝不截序列化后的 JSON 字符串】后者会产出坏 JSON，存进库反而比少几个
    字段更糟——排查时拿到一段解析不了的东西。
    """

    def size() -> int:
        return len(json.dumps(snapshot, ensure_ascii=False, default=str).encode("utf-8"))

    limit = max_kb * 1024
    if max_kb <= 0 or size() <= limit:
        return snapshot

    trimmed = []
    for item in ((snapshot.get("recovery") or {}).get("history") or []):
        if "result" in item:
            item["result_len"] = len(str(item.pop("result")))
    trimmed.append("recovery.history.result")
    if size() <= limit:
        snapshot["trimmed"] = trimmed
        return snapshot

    stages = (snapshot.get("state") or {}).get("stages")
    if stages:
        snapshot["state"]["stages_count"] = len(snapshot["state"].pop("stages"))
        trimmed.append("state.stages")
    if size() <= limit:
        snapshot["trimmed"] = trimmed
        return snapshot

    text = (snapshot.get("page") or {}).get("text")
    if text:
        snapshot["page"]["text_len"] = len(snapshot["page"].pop("text"))
        trimmed.append("page.text")
    snapshot["trimmed"] = trimmed
    snapshot["trimmed_bytes"] = size()
    return snapshot


def build_snapshot(*, exit_tag: str, key: str, task: dict, state: dict, stage: str = "",
                   stage_label: str = "", note: str = "", ctx: Optional[dict] = None,
                   run_ids: Optional[list] = None, stale_form: Optional[set] = None,
                   from_stage: str = "", elapsed_s: float = 0.0, store: str = "",
                   site: str = "", index: int = 0, total: int = 0,
                   instance: str = "", page: Optional[dict] = None,
                   shot_meta: Optional[dict] = None, max_kb: int = 256) -> dict:
    """把一次失败的全部现场组装成可 JSON 序列化的 dict（纯函数，离线可直调自检）。

    state 由调用方从断点文件重读后传入：出口 D 刚 save_state 过、出口 C 的内存 state 与
    磁盘一致、出口 E 的内存 state 已随栈帧销毁——三种情况磁盘副本都是唯一权威现场，
    统一从磁盘读，六个出口的调用方式就完全一致。
    """
    task = task or {}
    ctx = ctx or {}
    return _fit({
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "exit_tag": exit_tag,
        "key": key, "stage": stage, "stage_label": stage_label,
        "note": str(note or "")[:2000],
        "index": index, "total": total, "elapsed_s": elapsed_s,
        "store": store, "site": site,
        "machine": error_report.MACHINE, "instance": instance,
        "task": {k: _brief(v) for k, v in task.items()},
        "ctx": {k: _brief(ctx.get(k)) for k in _CTX_KEYS if ctx.get(k) is not None},
        # 「本次计划跑哪些、哪些按页面实况判为重跑、哪些被断点文件跳过」原先只活在
        # _run_product 的局部变量里，事件流一过就没了，这里是它唯一的落地处。
        "run": {
            "from_stage": from_stage or "",
            "planned": list(run_ids or [])[:20],
            "stale_form": sorted(stale_form or [])[:20],
            "skipped_by_state": [sid for sid, value in (state.get("stages") or {}).items()
                                 if (value or {}).get("status") in _DONE][:20],
        },
        "state": _condense_state(state),
        "recovery": _pick_recovery(state, stage),
        # 【必须拷一层】_fit 超限降级时会 pop 掉这里的 "text" 换成 text_len，直接引用调用方
        # 的 dict 就等于把它改坏——本函数对外声称是纯函数（离线可直调自检），不能有这个
        # 暗坑：调用方随手复用一个 page 对象，第二次调用就再也取不到正文了。
        "page": dict(page or {}),
        "shot": shot_meta or {},
        "state_path": publish_state._state_path(key),
    }, max_kb)


async def _capture_shot(session, max_kb: int) -> tuple[Optional[bytes], dict]:
    """截当前工作页。截不到只返回原因，绝不为截图失败干扰上报。

    两个已知的坑：
    - 图像 base64 在 CDP 返回体的**里层** data.data，不是外层（agent_tools 那边踩过）。
    - Chrome 窗口被最小化或完全遮挡时可能拿到黑帧/旧帧。故把字节数一并交出去：几 KB
      就基本是空白页，看库的人一眼能判，不用把图拉下来才发现是废的。
    """
    if session is None or not session.is_alive():
        return None, {"error": "CDP 会话不可用，未截图"}
    try:
        captured = await session.cdp("Page.captureScreenshot", {"format": "png"})
        image = (captured.get("data") or {}).get("data")
        if not image:
            return None, {"error": str(captured.get("err") or "CDP 未返回图像数据")}
        png = base64.b64decode(image)
    except Exception as e:
        return None, {"error": f"截图异常：{e}"}
    if max_kb > 0 and len(png) > max_kb * 1024:
        return None, {"bytes": len(png),
                      "error": f"截图 {len(png)} 字节超过上限 {max_kb} KB，未上传"}
    return png, {"bytes": len(png)}


async def _capture_page(session) -> dict:
    """读当前页面的只读快照（URL、正文、可见弹窗、报错 toast）。

    【刻意不用 navigation.live_state】它带一个最长 40×300ms 的渲染等待循环，会把失败
    上报拖慢十几秒；这里只要「当时页面长什么样」，eval 一次就够。
    """
    if session is None or not session.is_alive():
        return {"error": "CDP 会话不可用，未读页面"}
    try:
        page = await session.eval_json(_JS_PAGE) or {}
    except Exception as e:
        return {"error": f"读取页面失败：{e}"}
    try:
        page["toasts_bad"] = [str(t)[:300] for t in recent_toasts(bad_only=True)[-10:]]
        page["edit_page_open"] = bool(session.edit_page_open)
    except Exception as e:
        page["toasts_error"] = str(e)
    return page


async def report_failure(*, session=None, exit_tag: str, key: str, task: dict,
                         stage: str = "", stage_label: str = "", note: str = "",
                         traceback: str = "", ctx: Optional[dict] = None,
                         run_ids: Optional[list] = None,
                         stale_form: Optional[set] = None, from_stage: str = "",
                         elapsed_s: float = 0.0, store: str = "", site: str = "",
                         index: int = 0, total: int = 0) -> int:
    """单商品失败出口的唯一上报入口：采现场 → 写主表 + 明细表，返回主表 id（0 = 没写成）。

    返回 0 的用途：调用方（service._alert_hook）据此补写主表那行，于是「上报未启用或
    快照链路整个失败」时原来那条文本记录不会丢；写成了钩子就跳过，一个失败只留一行。

    主表字段与旧的直接 report 调用逐字段同构（stage 传人类可读的阶段名而不是阶段 id），
    否则 stage 列会新旧两种口径混杂，既有按 stage 聚合的查询会静默失真。阶段 id 记在
    快照 JSON 的 stage 里。
    """
    # 【整体兜一层】调用点都在「原来那条失败 return」的紧前面，这里漏出去任何异常都会
    # 把那句真实的失败 note 冲成另一个异常，比少传几个字段严重得多。内部各步的失败
    # （配错、断点文件损坏、CDP 不可用、写库失败）本身已有各自的容错，这一层兜的是
    # 没想到的那些——比如 session 不是真会话（测试里的替身）导致 is_alive 直接抛。
    try:
        cfg = error_report.load_config()
        if not cfg["enabled"]:
            return 0
        state = publish_state.load_state(key)
        page, shot_meta, shot = {}, {"error": "未采集"}, None
        if cfg["snapshot"]:
            page = await _capture_page(session)
            if cfg["screenshot"]:
                shot, shot_meta = await _capture_shot(session, cfg["screenshot_max_kb"])
        snapshot = build_snapshot(
            exit_tag=exit_tag, key=key, task=task, state=state, stage=stage,
            stage_label=stage_label, note=note, ctx=ctx, run_ids=run_ids,
            stale_form=stale_form, from_stage=from_stage, elapsed_s=elapsed_s,
            store=store, site=site, index=index, total=total,
            instance=cfg["instance"], page=page, shot_meta=shot_meta,
            max_kb=cfg["snapshot_max_kb"])
        return await error_report.report_snapshot(
            "publish", level="error", stage=stage_label, item=key, message=note,
            traceback=traceback, snapshot=snapshot, shot=shot, exit_tag=exit_tag)
    except Exception as e:
        logger.warning(f"失败现场上报异常（忽略）：{e}")
        return 0
