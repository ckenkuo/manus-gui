# -*- coding: utf-8 -*-
"""协作文档链接登记簿：本地持久化「名字 + 链接」，供各管线 UI 下次直接选取。

背景：订单页/采集页都允许直接粘贴金山文档（kdocs）协作链接当目标工作簿，但
prefs 只记「最近一次」，换一个链接旧的就丢了。这里单独存一份登记表——凡是被
用到过的链接都留下来，用户可以给链接起名字（如「wintop订单登记表」），下次
开页从下拉候选里直接选，不必再找链接粘贴。

存 workspace/cloud_docs.json，与 orders_prefs.json / collect_prefs.json 并列；
所有读写都是 best-effort（坏了只告警、不阻断任何管线）。
"""
import json
import threading
from datetime import datetime
from typing import List, Optional

from app.config import config
from app.logger import logger

CLOUD_DOCS = config.workspace_root / "cloud_docs.json"

_lock = threading.Lock()


def _load() -> List[dict]:
    """读登记簿原始列表；缺失/损坏返回 []。"""
    if not CLOUD_DOCS.exists():
        return []
    try:
        data = json.loads(CLOUD_DOCS.read_text(encoding="utf-8"))
        docs = data.get("docs") if isinstance(data, dict) else None
        if isinstance(docs, list):
            return [d for d in docs if isinstance(d, dict) and d.get("url")]
    except Exception:
        pass
    return []


def _save(docs: List[dict]) -> None:
    CLOUD_DOCS.parent.mkdir(parents=True, exist_ok=True)
    CLOUD_DOCS.write_text(
        json.dumps({"docs": docs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_docs() -> List[dict]:
    """登记簿列表（[{name, url, last_used}]），按最近使用倒序，供 UI 下拉候选。"""
    docs = _load()
    docs.sort(key=lambda d: d.get("last_used") or "", reverse=True)
    return docs


def remember(url: str, name: str = "") -> None:
    """登记/更新一条链接。已存在则刷新 last_used；给了 name 才覆盖旧名字（自动
    登记时不抹掉用户起过的名字）。写失败只告警、不抛错。
    """
    url = str(url or "").strip()
    if not url:
        return
    name = str(name or "").strip()
    try:
        with _lock:
            docs = _load()
            entry: Optional[dict] = next(
                (d for d in docs if d.get("url") == url), None
            )
            if entry is None:
                entry = {"name": "", "url": url}
                docs.append(entry)
            if name:
                entry["name"] = name
            entry["last_used"] = datetime.now().isoformat(timespec="seconds")
            _save(docs)
    except Exception as e:
        logger.warning(f"登记协作文档链接失败（忽略）：{e}")


def remove(url: str) -> bool:
    """从登记簿删掉一条链接；返回是否真的删到了。写失败只告警、返回 False。"""
    url = str(url or "").strip()
    try:
        with _lock:
            docs = _load()
            kept = [d for d in docs if d.get("url") != url]
            if len(kept) == len(docs):
                return False
            _save(kept)
            return True
    except Exception as e:
        logger.warning(f"删除协作文档链接失败（忽略）：{e}")
        return False
