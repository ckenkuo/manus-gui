"""白底主体提取（图搜查询图预处理）。

为什么要它：Temu 主图常是营销拼图——多角度小图、模特实拍、促销文案堆一起。
直接拿去 1688 以图搜图，引擎抽的是整张拼图的视觉特征，搜出一堆不相干货，
还得靠 1688 的 YOLO 主体框逐个点、逐个重搜来救（脆且慢）。

本模块用生图模型（packyapi 的 `gpt-image-2` /images/edits）把主图**重绘**成
「纯白背景 + 单一商品本体」的干净图，配合商品标题约束该保留哪一件。白底单品图
接近 1688 供货商主图的样子，以图搜的 recall 明显更好，也省掉框选 dance。

⚠️ 红线（务必守住）：生图是**重绘像素**、可能轻微改动商品外观。故此图**只当
【搜索查询图】**（决定 1688 返回哪些候选，即 recall）——判同款一律用**原图**
（决定精度）。这样生成误差只会漏采、不会误采。调用方 collect_one_product 已如此。

配置走独立环境变量（不塞进 app/llm.py，那是 DashScope/Anthropic 链路，且 config.py
会给空 key 回退 DASHSCOPE_API_KEY，塞进去会拿错 key）：
    PACKY_API_KEY       必填，packyapi 的 Bearer token；缺失则本模块整体降级（返回 None）
    PACKY_BASE_URL      选填，默认 https://www.packyapi.com/v1
    PACKY_IMAGE_MODEL   选填，默认 gpt-image-2
    PACKY_IMAGE_SIZE    选填，默认 1024x1024
    PACKY_IMAGE_QUALITY 选填，默认 medium（搜索查询图够用；成本杠杆，别默认 high）

任何失败（无 key/超时/HTTP 错/响应无图/写盘失败）均返回 None，调用方退回原图，
绝不阻断采集。
"""
import asyncio
import base64
import os
from typing import Optional

from app.logger import logger

_DEFAULT_BASE_URL = "https://www.packyapi.com/v1"
_DEFAULT_MODEL = "gpt-image-2"
_DEFAULT_SIZE = "1024x1024"
_DEFAULT_QUALITY = "medium"

# 无 key 时只警告一次，避免整批日志刷屏。
_warned_no_key = False


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


def _call_edit_api(src_img_path: str, title: str, read_timeout: int) -> Optional[bytes]:
    """同步调 packyapi /images/edits，返回白底图字节；失败返回 None。

    放在线程里跑（见 extract_white_bg 的 to_thread），故这里用同步 requests。
    """
    global _warned_no_key
    api_key = os.getenv("PACKY_API_KEY")
    if not api_key:
        if not _warned_no_key:
            logger.warning(
                "extract_white_bg：未设 PACKY_API_KEY，跳过白底提取、退回原图搜索。"
                "如需启用白底图搜，设置环境变量 PACKY_API_KEY。"
            )
            _warned_no_key = True
        return None

    import requests

    base = (os.getenv("PACKY_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/images/edits"
    model = os.getenv("PACKY_IMAGE_MODEL") or _DEFAULT_MODEL
    size = os.getenv("PACKY_IMAGE_SIZE") or _DEFAULT_SIZE
    quality = os.getenv("PACKY_IMAGE_QUALITY") or _DEFAULT_QUALITY

    fh = None
    try:
        fh = open(src_img_path, "rb")
        files = {"image": (os.path.basename(src_img_path), fh, "image/jpeg")}
        data = {
            "model": model,
            "prompt": _build_prompt(title),
            "size": size,
            "quality": quality,
            "output_format": "png",
            "response_format": "b64_json",
        }
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=(10, read_timeout),  # (连接, 读取)；生图慢，读取给足
        )
        if resp.status_code != 200:
            logger.warning(
                f"extract_white_bg：HTTP {resp.status_code}，退回原图。响应：{resp.text[:200]}"
            )
            return None
        return _extract_image_bytes(resp.json())
    except Exception as e:
        logger.warning(f"extract_white_bg：调用异常 {e}，退回原图")
        return None
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass


async def extract_white_bg(
    src_img_path: str, title: str = "", *, read_timeout: int = 90
) -> Optional[str]:
    """把商品主图提取成白底单品图，写到 <path>_white.png，返回该路径。

    - 生图调用放线程里（asyncio.to_thread），不阻塞浏览器所在的事件循环。
    - 任何失败（无 key/超时/HTTP 错/响应无图/写盘失败）返回 None，调用方退回原图。
    - 只当搜索查询图用；判同款用原图（见模块 docstring 红线）。
    """
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
