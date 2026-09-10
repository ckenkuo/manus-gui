"""店小秘发布共用能力：preferences。各来源流程由 workflows/ 独立定义。"""

import json
import os
from app.logger import logger


PREFS_PATH = os.path.join("workspace", "publish_prefs.json")


# 生图并发的默认值与上限（实际值由用户在发布页配置，见 get_image_concurrency）
IMAGE_CONCURRENCY_DEFAULT = 30

IMAGE_CONCURRENCY_MAX = 64


def load_prefs() -> dict:
    try:
        with open(PREFS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs: dict) -> None:
    """把 prefs 里的键【合并】进偏好文件，未提及的键原样保留。

    【为什么必须是合并而不是覆盖】这个文件同时存着两类东西：run_batch 每次成功启动
    都会记的 store/site，以及用户在页面上单独设过一次就该长期生效的生图并发数
    （imageConcurrency）。原先是整体覆盖，run_batch 一跑就把并发配置抹掉、静默回落
    默认值——用户改过的设置在下一次跑批后自己消失，且没有任何提示。
    """
    try:
        merged = {**load_prefs(), **prefs}
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"prefs 写入失败（忽略）：{e}")


def get_image_concurrency() -> int:
    """生图（gpt-image-2）并发数：读用户配置，非法值回落默认，并夹到 1~上限。

    【为什么要让用户自己配】Packy 侧对 gpt-image-2 的实际并发上限随网关档位与本机
    出网链路（VPN）变化：链路好时 30 并发能把 ⑤b 与 ⑬ 的生图墙压到接近单张耗时，
    链路差时高并发只会互相挤占带宽、集体超时，反而比小并发更慢。这个最佳值只有
    用户的实际环境能测出来，写死任何一个数都会在另一种环境里是错的。

    默认 30 是常规链路的经验值（原先写死 3 是「Packy 限流未知先保守」的临时取值，
    实测远未触及上限）；上限 64 只为挡住手改配置时的荒谬值（几百并发必然全线超时）。
    """
    raw = load_prefs().get("imageConcurrency")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return IMAGE_CONCURRENCY_DEFAULT
    return max(1, min(IMAGE_CONCURRENCY_MAX, n))


def set_image_concurrency(n) -> int:
    """设生图并发数并落盘，返回实际生效值（越界被夹住时与入参不同）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError(f"生图并发数必须是整数，收到 {n!r}")
    if n < 1 or n > IMAGE_CONCURRENCY_MAX:
        raise ValueError(f"生图并发数需在 1~{IMAGE_CONCURRENCY_MAX} 之间，收到 {n}")
    save_prefs({"imageConcurrency": n})
    return n
