"""发布流程中断的飞书群机器人告警。

【为什么需要它】发布管线是长流程作业（单商品 19 阶段、含多次 LLM 与生图，
批次动辄跑几小时），人不会守在页面前。而中断有三类，事后翻日志才发现太晚：
  1. 批次级中止（aborted）：CDP 连不上、会话重连失败、缺店铺/站点——整批不再往下跑；
  2. 单商品 fail：某阶段抛错或校验未过，该商品停在半成品状态（草稿还在，可续跑）；
  3. 批次收尾时有失败（batch_done 的 fail>0）：给一份汇总，免得逐条翻。
这三类正是「需要人去看一眼」的时刻，故只在这三处发告警。阶段内的 manual_check
刻意不发——它每个商品都可能出好几条（尺码、描述图、视频…），大多不阻断流程，
发出来只会把告警群刷成日志流，真正的中断反而被埋掉。

【best-effort，绝不影响主流程】告警属本项目辅助路径（同 service._emit、config
的输出目录创建）：没配 webhook 就静默跳过，发送失败只 logger.warning。任何情况下
都不让一次群消息发不出去导致发布批次中断——那是本末倒置。

【为什么走 requests 而不是页面内 fetch】飞书 webhook 是纯服务端 HTTP，与店小秘
浏览器会话无关，不该占用 CDP 页签（页签在跑发布，插一发 fetch 还要担心跨域与
页面状态）。requests 是同步的，故对外只暴露 async 接口、内部 asyncio.to_thread
下沉到线程，不堵事件循环。

配置在 config/config.toml 的 [publish.alert] 段（gitignored，webhook 是凭证不进
版本库）：

    [publish.alert]
    enabled = true
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"

也可用环境变量 PUBLISH_ALERT_WEBHOOK 覆盖（临时切告警群时不必改配置文件）。
"""
import asyncio
import os
import socket
import time
from typing import Optional

from app.logger import logger

WEBHOOK_ENV = "PUBLISH_ALERT_WEBHOOK"
_TIMEOUT = 10          # 飞书 webhook 正常 200~500ms 返回；给 10s 足够，超了不值得再等
_NOTE_MAX = 300        # 阶段 note 有时是整段异常文本，卡片里截断，详情看日志


def load_alert_config() -> dict:
    """读 [publish.alert] 段，返回 {"enabled": bool, "webhook": str}。

    只读 config.toml、不回退 config.example.toml：example 里的 webhook 一定是空串
    （凭证不进版本库），回退过去没有任何意义。读不到/解析失败一律返回禁用态——
    告警是辅助路径，配置有问题不该让发布跑不起来。
    """
    webhook = (os.environ.get(WEBHOOK_ENV) or "").strip()
    enabled = True
    if not webhook:
        try:
            import tomllib

            from app.config import config_search_dirs
            for d in config_search_dirs():
                p = d / "config.toml"
                if not p.exists():
                    continue
                with open(p, "rb") as f:
                    data = tomllib.load(f)
                section = ((data.get("publish") or {}).get("alert") or {})
                webhook = str(section.get("webhook") or "").strip()
                if webhook:
                    enabled = bool(section.get("enabled", True))
                    break
        except Exception as e:
            logger.warning(f"读取 [publish.alert] 配置失败，本次不发告警：{e}")
            return {"enabled": False, "webhook": ""}
    return {"enabled": enabled and bool(webhook), "webhook": webhook}


def _trim(s, limit: int = _NOTE_MAX) -> str:
    s = str(s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _card(title: str, color: str, fields: list) -> dict:
    """拼交互卡片：标题带色块（red=中断、orange=有失败），正文是「键：值」多行。

    为什么用卡片而不是纯 text：中断信息有 5~7 个字段（店铺/站点/商品/阶段/原因…），
    text 消息在群里是一长条，手机上要横向找关键字；卡片有标题色块，一眼能从群里
    分辨出「这条是红的」。飞书两种格式都实测可用（2026-09-01 联调）。
    """
    lines = [f"**{k}**：{v}" for k, v in fields if str(v or "").strip()]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": color,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                                        "content": "\n".join(lines) or "（无详情）"}},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": f"manus-gui 发布管线 · {socket.gethostname()} · "
                                f"{time.strftime('%Y-%m-%d %H:%M:%S')}"}]},
            ],
        },
    }


def _post(webhook: str, payload: dict) -> None:
    """同步发一发（在线程里跑）。飞书业务错误走 HTTP 200 + code!=0，故要看响应体。"""
    import requests

    r = requests.post(webhook, json=payload, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {_trim(r.text, 200)}")
    try:
        body = r.json()
    except Exception:
        return  # 状态码已是 200，解析不出 JSON 不当失败（飞书偶发返回空体）
    if body.get("code") not in (0, None):
        raise RuntimeError(f"飞书返回 code={body.get('code')} msg={body.get('msg')}")


async def send(payload: dict) -> bool:
    """发一条原始 payload；成功返回 True。未配置或失败一律 False，不抛。"""
    cfg = load_alert_config()
    if not cfg["enabled"]:
        return False
    try:
        await asyncio.to_thread(_post, cfg["webhook"], payload)
        return True
    except Exception as e:
        logger.warning(f"飞书告警发送失败（忽略，不影响发布）：{e}")
        return False


async def send_text(text: str) -> bool:
    """纯文本告警（联调/自检用；业务告警走下面三个语义化入口）。"""
    return await send({"msg_type": "text", "content": {"text": _trim(text, 2000)}})


async def alert_product_fail(offer: str, title: str = "", stage: str = "",
                             note: str = "", store: str = "", site: str = "",
                             rowid: Optional[str] = None,
                             index: int = 0, total: int = 0,
                             elapsed_s: float = 0.0) -> bool:
    """单商品失败：报清楚「哪个商品卡在哪个阶段、什么原因」，人好直接续跑。

    stage 传的是中文阶段名（如「⑨ 尺码表」）而不是 id：告警是给人看的，
    阶段 id（sizechart）还要人再去对照 --list-stages。
    """
    return await send(_card(
        "发布中断：商品失败", "red",
        [("商品", offer), ("标题", _trim(title, 60)),
         ("进度", f"{index}/{total}" if total else ""),
         ("店铺/站点", f"{store} / {site}" if store or site else ""),
         ("卡在阶段", stage or "（未进入阶段）"),
         ("rowid", rowid or ""),
         ("耗时", f"{elapsed_s}s" if elapsed_s else ""),
         ("原因", _trim(note) or "（无 note，详见日志）")]))


async def alert_batch_aborted(reason: str, store: str = "", site: str = "",
                              done: int = 0, total: int = 0) -> bool:
    """批次级中止：整批不再往下跑，最需要人立刻介入（CDP 掉线、会话重连失败等）。"""
    return await send(_card(
        "发布中断：批次中止", "red",
        [("店铺/站点", f"{store} / {site}" if store or site else ""),
         ("已处理", f"{done}/{total}" if total else ""),
         ("原因", _trim(reason))]))


async def alert_batch_done(ok: int, fail: int, elapsed_s: float = 0.0,
                           store: str = "", site: str = "",
                           failures: Optional[list] = None) -> bool:
    """批次收尾汇总，只在 fail>0 时调用（全绿不发——没人需要为成功被打扰）。

    failures 是 [(offer, 阶段名)] 列表，直接列进卡片：一批里失败往往集中在同一个
    阶段（某类目属性变了、图床限流…），列出来一眼能看出是不是同一个根因。
    """
    lines = "、".join(f"{o}（{s or '未知阶段'}）" for o, s in (failures or [])[:10])
    if failures and len(failures) > 10:
        lines += f" 等 {len(failures)} 个"
    return await send(_card(
        "发布批次收尾：有失败商品", "orange",
        [("店铺/站点", f"{store} / {site}" if store or site else ""),
         ("结果", f"成功 {ok} / 失败 {fail}"),
         ("总耗时", f"{elapsed_s}s" if elapsed_s else ""),
         ("失败清单", _trim(lines, 800))]))


async def alert_batch_crash(error: str, store: str = "", site: str = "") -> bool:
    """批次入口抛出未捕获异常（run_batch 之外的崩溃，如浏览器会话打不开）。

    与 aborted 分开报：aborted 是管线预料到的可控中止（reason 可读），
    这里是没预料到的异常，值得在群里换个措辞让人知道要看 traceback。
    """
    return await send(_card(
        "发布中断：批次异常退出", "red",
        [("店铺/站点", f"{store} / {site}" if store or site else ""),
         ("异常", _trim(error)), ("提示", "详见服务端日志 traceback")]))
