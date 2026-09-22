"""白底主体提取（图搜查询图预处理）。

为什么要它：Temu 主图常是营销拼图——多角度小图、模特实拍、促销文案堆一起。
直接拿去 1688 以图搜图，引擎抽的是整张拼图的视觉特征，搜出一堆不相干货，
还得靠 1688 的 YOLO 主体框逐个点、逐个重搜来救（脆且慢）。

本模块用生图模型（zzlye 的 `gpt-image-2.5-flare` /images/edits）把主图**重绘**成
「纯白背景 + 单一商品本体」的干净图，配合商品标题约束该保留哪一件。白底单品图
接近 1688 供货商主图的样子，以图搜的 recall 明显更好，也省掉框选 dance。

2026-09-20 从 packyapi 的 gpt-image-2 切到 zzlye 的 gpt-image-2.5-flare（与发布管线
[publish].image_provider 同一家、同一档，$0.03/次按次固定计费）。实测这家：
requests 直连不被拦（不必像发布管线那样走 curl 绕 Cloudflare 指纹）、收 output_format、
返回同时给 b64_json 与 url。
【尺寸不必管】这家基础档有 ~157 万像素预算，请求 1024x1024 实得 1254x1254——白底图
只当搜索查询图，尺寸大小无所谓（发布管线那边要过服装尺寸红线才在意，见
app/publish/images.py 的 _cap_size）。

⚠️ 红线（务必守住）：生图是**重绘像素**、可能轻微改动商品外观。故此图**只当
【搜索查询图】**（决定 1688 返回哪些候选，即 recall）——判同款一律用**原图**
（决定精度）。这样生成误差只会漏采、不会误采。调用方 collect_one_product 已如此。

配置走 config.toml 的 [collect_image] 段（2026-08-20 起不再读环境变量：同一个 key
两处维护时环境变量优先级更高、会静默盖掉配置值，看配置是新 key 实际生效是旧 key，
排查时完全看不出来）。刻意不塞进 [llm.*]：那是 DashScope/Anthropic 链路，且 config.py
的段间合并会把 [llm] 的 key 补给缺 key 的段，塞进去会拿错 key。
    api_key             主 key，出图中转的 Bearer token（更便宜）；
    api_key_expensive   备用 key（更贵但稳定）；主 key 余额耗尽（"没有可用token"）
                        或调用失败时自动切到它兜底。两者都缺才整体降级（返回 None）
    base_url            选填，默认 https://api.zzlye.xyz/v1
    model               选填，默认 gpt-image-2.5-flare
    size                选填，默认 1024x1024
    quality             选填，默认 medium（搜索查询图够用；成本杠杆，别默认 high）

任何失败（无 key/超时/HTTP 错/响应无图/写盘失败）均返回 None，调用方退回原图，
绝不阻断采集。
"""
import asyncio
import base64
import os
from typing import Optional

from app.logger import logger

_DEFAULT_BASE_URL = "https://api.zzlye.xyz/v1"
_DEFAULT_MODEL = "gpt-image-2.5-flare"
_DEFAULT_SIZE = "1024x1024"
_DEFAULT_QUALITY = "medium"

# 无 key 时只警告一次，避免整批日志刷屏。
_warned_no_key = False

# 中转余额耗尽的错误特征（packyapi 实测）：服务端把它包成 500 + invalid_request_error，
# 消息体含"没有可用token"。这不是临时抖动，重试同一个 key 必然还失败——命中即
# 立刻放弃该 key、切备用 key，别在坏 key 上退避空转（曾白等 11s×每商品）。
_BALANCE_EXHAUSTED_MARKERS = ("没有可用token", "没有可用 token", "insufficient", "no available token")


def _is_balance_exhausted(resp) -> bool:
    """判断响应是否为"额度/余额耗尽"（而非可重试的限流/抖动）。"""
    if resp is None:
        return False
    body = ""
    try:
        body = (resp.text or "").lower()
    except Exception:
        return False
    return any(m.lower() in body for m in _BALANCE_EXHAUSTED_MARKERS)


# 白底提取的页面可切偏好，独立一个文件而【不并进 collect_prefs.json】：那个文件的
# save_prefs 是整体覆盖式写入（重建 data 字典、不合并旧键），塞进去后每次 run_batch
# 都会把这里的设置抹掉——发布侧 preferences.save_prefs 的注释记着同一个坑
# （run_batch 一跑就把并发配置静默重置成默认值，用户改过的设置自己消失还没提示）。
_PREFS_PATH = os.path.join("workspace", "collect_image_prefs.json")


def _load_prefs() -> dict:
    """读页面偏好。读失败返回空 dict——本模块任何失败都退回原图、不阻断采集。"""
    try:
        import json
        with open(_PREFS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_prefs(patch: dict) -> None:
    """把 patch 【合并】进偏好文件（未提及的键原样保留，理由见 _PREFS_PATH 注释）。"""
    try:
        import json
        merged = {**_load_prefs(), **patch}
        os.makedirs(os.path.dirname(_PREFS_PATH), exist_ok=True)
        with open(_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"白底提取偏好写入失败（忽略）：{e}")


def get_model() -> str:
    """页面所选的出图模型（空串=跟随 config.toml 的 [collect_image].model）。"""
    v = _load_prefs().get("model")
    return str(v).strip() if v else ""


def set_model(name) -> str:
    """设出图模型并落盘。空串/None 表示清除、回到跟随配置文件。

    不校验模型名：中转站随时上新（zzlye 现有 15 个），写死白名单会让新模型没法试；
    填错的代价是服务端报「无可用渠道」，够清楚（同发布页出图模型那个输入框的取向）。
    """
    name = "" if name is None else str(name).strip()
    _save_prefs({"model": name})
    return name


def get_enabled() -> bool:
    """白底提取是否启用（默认启用）。

    【为什么要这个开关】它每商品一次生图、是采集的主要变动成本，而收益只是把以图搜的
    recall 提上去。批量试跑或排查搜索问题时要能一键关掉，不必去改 config.toml 清 key
    （清了下次还得填回来）。
    """
    v = _load_prefs().get("enabled")
    return True if v is None else bool(v)


def set_enabled(on) -> bool:
    """设启用开关并落盘。"""
    _save_prefs({"enabled": bool(on)})
    return bool(on)


def _conf() -> dict:
    """读统一配置源的 [collect_image] 段。

    统一源（app/config.py 的 get_config_section）配了 [config_store] 走 MySQL
    配置中心、否则读本地 config.toml，自带 30s TTL——改配置最多晚 30 秒生效、
    仍不必重启，与 resolve_packy_key 的取向一致。读失败按 best-effort 吞掉
    （返回空 dict）——本模块任何失败都退回原图、不阻断采集，这里不该是唯一的
    例外（配置中心挂掉的 ConfigStoreError 同样吞掉走原图）。
    """
    try:
        from app.config import get_config_section

        return get_config_section("collect_image")
    except Exception as e:
        logger.warning(f"读取 [collect_image] 配置失败：{e}")
    return {}


def _resolve_api_keys() -> list[tuple[str, str]]:
    """按优先级返回可用的 (label, key) 列表：主 key（便宜）在前，备用 key（贵但稳）兜底。

    去重（两项配成同一个 key 时只留一个），保持顺序。
    """
    conf = _conf()
    keys: list[tuple[str, str]] = []
    seen: set = set()
    for label, field in (("主", "api_key"), ("备用", "api_key_expensive")):
        v = conf.get(field)
        if v and v not in seen:
            seen.add(v)
            keys.append((label, v))
    return keys


def _build_prompt(title: str) -> str:
    """白底提取提示词。强调「保持真实外观、不要改动」以尽量压低生图幻觉。"""
    hint = f"（商品名：{title.strip()}）" if title and title.strip() else ""
    return (
        f"提取这张电商商品图中的【主体商品】{hint}，只保留这一件商品本身，"
        "放在纯白色背景（#FFFFFF）正中、完整清晰。去掉所有促销文字、价格标签、"
        "水印、模特人物、多角度小图、边框和装饰元素。务必保持商品真实的外观、"
        "颜色、图案、材质和比例——不要改变、不要美化、不要添加原图没有的部分。"
        "输出一张干净的白底商品主图。"
    )


def _extract_image_bytes(payload: dict) -> Optional[bytes]:
    """从 /images/edits 响应取图字节：优先 b64_json，兜底 url 下载。"""
    data = (payload or {}).get("data")
    if not isinstance(data, list) or not data:
        return None
    item = data[0] or {}
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
    url = item.get("url")
    if url:
        try:
            import requests

            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.warning(f"extract_white_bg：下载生成图失败 {e}")
            return None
    return None


def _call_edit_api_one_key(
    label: str, api_key: str, src_img_path: str, title: str, read_timeout: int
) -> tuple[Optional[bytes], bool]:
    """用单个 key 调出图中转的 /images/edits。

    返回 (图字节 or None, 该 key 是否余额耗尽)。余额耗尽时不重试，交由上层切备用 key。
    """
    import time

    import requests

    conf = _conf()
    base = (conf.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/images/edits"
    # prefs 优先于 config.toml：页面上选了模型却不生效会让人摸不着头脑
    # （同发布管线 images._provider 的优先级取向）
    model = get_model() or conf.get("model") or _DEFAULT_MODEL
    size = conf.get("size") or _DEFAULT_SIZE
    quality = conf.get("quality") or _DEFAULT_QUALITY

    # ⚠️ 不要传 response_format：gpt-image 系默认就返回 b64_json（无此参数概念），
    # _extract_image_bytes 也已同时兜底 b64_json / url。在 packyapi 上实测过这个坑：
    # 主 key（自有池）能通融这个参数，而备用 key（api_key_expensive）走 OpenAI 纯正
    # Images 兼容层，会以 400 unknown_parameter 直接拒掉——主 key 余额耗尽切备用后必炸。
    # 换到 zzlye 后同样不传：传了只有害无益，且双 key 仍可能指向不同兼容层。
    # output_format=png 则是两家都收的（2026-09-20 在 zzlye 上实测 HTTP 200）。
    data = {
        "model": model,
        "prompt": _build_prompt(title),
        "size": size,
        "quality": quality,
        "output_format": "png",
    }
    try:
        # 429（限流）/5xx（服务端抖动）是临时错误 → 退避重试；其余状态码直接降级。
        # 但"没有可用token"（余额耗尽）虽被服务端包成 500，重试无意义 → 立即返回
        # exhausted=True 让上层切备用 key，不在坏 key 上空转退避。
        # 文件句柄每次重开（requests 上传后流已消费）。
        resp = None
        for attempt in range(1, 4):  # 最多 3 次：0s / 3s / 8s
            with open(src_img_path, "rb") as fh:
                files = {"image": (os.path.basename(src_img_path), fh, "image/jpeg")}
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data=data,
                    timeout=(10, read_timeout),  # (连接, 读取)；生图慢，读取给足
                )
            if resp.status_code == 200:
                break
            if _is_balance_exhausted(resp):
                logger.warning(
                    f"extract_white_bg：{label} key 余额耗尽（没有可用token），"
                    "不重试、切备用 key。"
                )
                return None, True
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = (0, 3, 8)[attempt]
                logger.warning(
                    f"extract_white_bg：{label} key HTTP {resp.status_code}"
                    f"（限流/服务端抖动），{wait}s 后重试（{attempt}/3）"
                )
                time.sleep(wait)
                continue
            break
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "无响应"
            body = resp.text[:200] if resp is not None else ""
            logger.warning(
                f"extract_white_bg：{label} key HTTP {code}，退回原图。响应：{body}"
            )
            return None, False
        return _extract_image_bytes(resp.json()), False
    except Exception as e:
        logger.warning(f"extract_white_bg：{label} key 调用异常 {e}，退回原图")
        return None, False


def _call_edit_api(src_img_path: str, title: str, read_timeout: int) -> Optional[bytes]:
    """同步调出图中转的 /images/edits，返回白底图字节；失败返回 None。

    按优先级尝试主 key（便宜）→ 备用 key（贵但稳）：主 key 余额耗尽或失败即切下一个。
    放在线程里跑（见 extract_white_bg 的 to_thread），故这里用同步 requests。
    """
    global _warned_no_key
    keys = _resolve_api_keys()
    if not keys:
        if not _warned_no_key:
            logger.warning(
                "extract_white_bg：[collect_image] 未配 api_key / api_key_expensive，"
                "跳过白底提取、退回原图搜索。如需启用白底图搜，配置其中之一。"
            )
            _warned_no_key = True
        return None

    for label, api_key in keys:
        img_bytes, _exhausted = _call_edit_api_one_key(
            label, api_key, src_img_path, title, read_timeout
        )
        if img_bytes:
            return img_bytes
        # 任何失败（余额耗尽/限流耗尽/异常）都切下一个 key 兜底；都试完仍无 → None。
    return None


async def extract_white_bg(
    src_img_path: str, title: str = "", *, read_timeout: int = 90
) -> Optional[str]:
    """把商品主图提取成白底单品图，写到 <path>_white.png，返回该路径。

    - 生图调用放线程里（asyncio.to_thread），不阻塞浏览器所在的事件循环。
    - 任何失败（无 key/超时/HTTP 错/响应无图/写盘失败）返回 None，调用方退回原图。
    - 页面上关掉开关（get_enabled 为假）时直接返回 None，与「没配 key」同一条降级路径。
    - 只当搜索查询图用；判同款用原图（见模块 docstring 红线）。
    """
    if not get_enabled():
        return None
    if not src_img_path or not os.path.exists(src_img_path):
        return None
    try:
        img_bytes = await asyncio.to_thread(
            _call_edit_api, src_img_path, title, read_timeout
        )
    except Exception as e:
        logger.warning(f"extract_white_bg：线程执行异常 {e}，退回原图")
        return None
    if not img_bytes:
        return None
    out_path = os.path.splitext(src_img_path)[0] + "_white.png"
    try:
        with open(out_path, "wb") as f:
            f.write(img_bytes)
    except Exception as e:
        logger.warning(f"extract_white_bg：写盘失败 {e}，退回原图")
        return None
    logger.info(f"extract_white_bg：已提取白底图 → {out_path}")
    return out_path
