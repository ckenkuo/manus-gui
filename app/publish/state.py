"""店小秘发布共用能力：state。各来源流程由 workflows/ 独立定义。"""

import json
import os
import time
from app.logger import logger
from app.publish.sources import base as sources_base


STATE_DIR = os.path.join("workspace", "publish-state")


# ---- 状态持久化（断点续跑）---------------------------------------------------

def _state_path(key: str) -> str:
    return os.path.join(STATE_DIR, f"{key}.json")


def load_state(key: str) -> dict:
    """读单品状态；没有或坏了返回空壳（best-effort，不阻断主流程）。"""
    state = {"key": key, "stages": {}, "status": "new"}
    try:
        with open(_state_path(key), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("stages"), dict):
            state.update(data)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"状态文件损坏，当全新跑（{_state_path(key)}）：{e}")
    return state


def save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_state_path(state["key"]), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"状态文件写入失败（忽略）：{e}")


def _task_key(task: dict) -> str:
    """单品状态键：url 模式取「平台-商品ID」，rowid 模式取 rowid-<rowid>。

    【2026-08-27 从「随便找 6 位以上数字」改成按平台抽】原先是
    `re.search(r"(\\d{6,})", url)`，它不看域名：一个拼多多长链接里的
    refer_page_id 时间戳（10015_1786254742569_...）会先被命中，于是两个不同商品
    可能撞到同一个状态文件、互相覆盖进度。现在走 sources.source_id，各平台锚定
    自己的参数名/路径段（goods_id= / -g-<id>.html / /dp/<ASIN>）。

    【1688 的键保持纯 offerId 不带平台前缀】既有状态文件全是这个形态，加前缀会让
    所有历史进度失配（续跑时读不到上次跑到哪，等于从头再来）。新平台带前缀是必须的：
    拼多多 goods_id 与 1688 offerId 都是纯数字且位数重叠，撞号就是两个商品共用一份
    状态文件。
    """
    if task.get("url"):
        url = task["url"]
        platform = sources_base.platform_of(url)
        if not platform:
            raise ValueError(f"来源平台暂不支持（认不出域名）：{url}")
        pid = sources_base.source_id(url, platform)
        if not pid:
            raise ValueError(
                f"无法从 url 解析商品 ID（{sources_base.platform_name(platform)}）：{url}")
        return pid if platform == "1688" else f"{platform}-{pid}"
    if task.get("rowid"):
        return f"rowid-{task['rowid']}"
    raise ValueError(f"任务必须带 url 或 rowid：{task}")


def _load_info(info_path: str) -> dict:
    with open(info_path, encoding="utf-8") as f:
        return json.load(f)
