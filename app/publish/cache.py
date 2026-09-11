# -*- coding: utf-8 -*-
"""发布管线的类目路径磁盘缓存。

为什么要这一层（2026-08-22 真站实测的成本账）：
    阶段③ auto_cat 耗时 110.1s——5 级类目，每级先逐个点候选前瞻子类目、再调一次
        LLM，一共 5 次 LLM + 大量 DOM 点击。而按【已知路径】逐列直点（不前瞻、
        不调 LLM）走完同样 5 级只花 7.6s。
开销几乎全花在【重复发现已知信息】上：实际用到的类目就是恒定的那么一些，故把
「哪条类目路径用过、用于哪些标题」持久化下来，下次同类商品直接拿它当候选走快路径。

【只缓存客观事实，不缓存判断】这里只存路径与历史标题；选哪条仍由 _pick_cached_category
每次问一次 LLM（用历史标题当判据），复用判断就是选错类目。

【2026-09-11：属性选项缓存整块已删】它原先缓存「每个类目的属性行与下拉选项」以省掉
阶段④ 逐行开下拉读选项的开销。选项改由服务端接口现取（见 attributes/server_options）
后，这份缓存既无必要、又曾是「真前缀、假完整」截断清单的落盘处，连同它的失效重读
逻辑（_refresh_row_and_retry 的旧形态）一并移除。


骨架照 app/cloud_docs.py（项目里既有的本地持久化登记簿）：模块级路径常量 + 锁 +
best-effort 读写。
"""
import json
import os
import threading
import time
from typing import Optional

from app.config import config
from app.logger import logger

# 【只有这一个是模块级路径根】categories.json 与 attrs/ 都由下面的函数拼出来。
# 若把子路径也定成模块级常量，单测 monkeypatch 这个根就只改了根、子常量在 import
# 期已经指向真实 workspace/，测试会写脏真目录（照 service._state_path 的写法）。
CACHE_DIR = str(config.workspace_root / "publish-cache")

_CATEGORIES_NAME = "categories.json"

MAX_PROMPT_PATHS = 60      # 进提示词的路径条数上限（按 last_used 降序截断）
_MAX_TITLE_SAMPLES = 3     # 每条路径留几个历史标题样本（喂提示词用）
_SCHEMA_VERSION = 1

_lock = threading.Lock()


def _categories_path() -> str:
    return os.path.join(CACHE_DIR, _CATEGORIES_NAME)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: str) -> Optional[dict]:
    """读一个缓存文件；缺失/损坏一律返回 None（调用方当未命中）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning(f"缓存文件损坏，当未命中（{path}）：{e}")
        return None


def _write_json(path: str, data: dict) -> None:
    """临时文件 + os.replace 原子替换，写失败只告警。

    为什么不直接 open(path, 'w')：json.dump 中途进程被杀会留下截断的 JSON，
    下次读虽然能当未命中兜住，但等于把已经攒好的几十条路径一次清零。os.replace
    在同一盘符上是原子的，读者永远只会看到完整的旧文件或完整的新文件。
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"缓存写入失败（忽略）：{path}：{e}")


# ---- 类目路径缓存 ------------------------------------------------------------

def load_categories() -> list:
    """已知类目路径记录，按 last_used 降序。缺失/损坏返回 []。

    每次调用都重读磁盘、不做进程内缓存：批次里商品 A 新写进去的路径，商品 B 要
    立刻能看见；并发跑的 publish_inspect 写入也才能自动可见。文件只有几十 KB。
    """
    data = _read_json(_categories_path()) or {}
    paths = data.get("paths")
    if not isinstance(paths, list):
        return []
    out = [p for p in paths
           if isinstance(p, dict) and isinstance(p.get("path"), list) and p["path"]]
    # 次级键 seq 是必需的而不是锦上添花：last_used 精度只到秒，而同一批次里连续几个
    # 商品命中不同类目完全是常态（实测同秒内写入 5 条），只按 last_used 排序会退化成
    # 不稳定序，「取最近 N 条」就可能把刚用过的那条截掉。seq 单调递增，同秒也能分出
    # 先后。老文件没有这个键 → 取 0，退回按 last_used 排，不影响读取。
    out.sort(key=lambda p: (p.get("last_used") or "", p.get("seq") or 0,
                            p.get("hits") or 0),
             reverse=True)
    return out


def prompt_paths(limit: int = MAX_PROMPT_PATHS) -> list:
    """给提示词用的路径清单（load_categories 截断到 limit 条）。

    截断按 last_used 降序，被截掉的尾部按定义就是最久没用到的、命中概率最低的。
    真发生截断时记一笔日志，方便日后判断是否需要更聪明的预筛。
    """
    all_paths = load_categories()
    if len(all_paths) > limit:
        logger.info(f"已知类目路径 {len(all_paths)} 条，取最近 {limit} 条进提示词")
        return all_paths[:limit]
    return all_paths


def cat_ids_for(path: list) -> list:
    """取某条已知路径此前记下的各级 catId（与 path 逐级对应；老条目没有，返回 []）。

    阶段④ 要按【页面上当前生效】的类目查属性选项，而阶段③ 改完类目不保存，服务端
    只认已保存的那版（见 attributes/server_options）。命中缓存路径时靠这里把叶子
    catId 交出去，就不必再回接口按路径名反查。
    """
    want = [str(x).strip() for x in (path or []) if str(x).strip()]
    if not want:
        return []
    for entry in load_categories():
        if entry.get("path") == want:
            ids = entry.get("catIds")
            return [str(x) for x in ids] if isinstance(ids, list) else []
    return []


def remember_category(path: list, title: str = "",
                      cat_ids: Optional[list] = None) -> None:
    """记住一条走通的类目路径（已存在则 hits+1、追加标题样本、刷 last_used）。

    写之前【重新读一次磁盘】再合并，不用调用方手上那份可能已过期的列表：两个进程
    同时跑时这样最多丢掉几个计数，不会丢掉整条路径记录。

    cat_ids 是这条路径各级的类目 id（走逐级遍历时接口候选里现成的）。只在它与 path
    级数完全对得上时才落盘：错位的一串 id 比没有更糟——阶段④ 会拿它去查一个别的
    类目的属性清单，而查出来的东西「看起来正常」（一样是属性名与可选值）。
    """
    path = [str(x).strip() for x in (path or []) if str(x).strip()]
    if not path:
        return
    try:
        with _lock:
            data = _read_json(_categories_path()) or {}
            paths = data.get("paths")
            if not isinstance(paths, list):
                paths = []
            entry = next((p for p in paths
                          if isinstance(p, dict) and p.get("path") == path), None)
            if entry is None:
                entry = {"path": path, "leaf": path[-1], "titles": [],
                         "hits": 0, "first_seen": _now()}
                paths.append(entry)
            entry["leaf"] = path[-1]
            ids = [str(x) for x in (cat_ids or []) if str(x or "").strip()]
            if len(ids) == len(path):
                entry["catIds"] = ids
            entry["hits"] = int(entry.get("hits") or 0) + 1
            entry["last_used"] = _now()
            # 同秒内写入多条时用它分先后（见 load_categories 的排序注释）
            entry["seq"] = max((int(p.get("seq") or 0) for p in paths), default=0) + 1
            title = str(title or "").strip()
            if title:
                titles = [t for t in (entry.get("titles") or []) if t != title]
                entry["titles"] = ([title] + titles)[:_MAX_TITLE_SAMPLES]
            _write_json(_categories_path(),
                        {"version": _SCHEMA_VERSION, "updated_at": _now(),
                         "paths": paths})
    except Exception as e:
        logger.warning(f"记类目路径失败（忽略）：{e}")


# ---- 统计与清理（UI / CLI 用）------------------------------------------------

def cache_stats() -> dict:
    """缓存现状（只读，给日志与 UI 面板用）。

    【2026-09-11 起只剩类目路径】属性选项缓存整块已删——选项改由服务端接口现取
    （见 attributes/server_options），不再需要落盘复用。
    """
    cats = load_categories()
    return {"paths": len(cats), "categories": cats}


def clear() -> dict:
    """清空类目路径缓存。返回实际删掉了什么，供 UI 显示。

    删不掉只告警（文件被占用等），不抛。
    """
    removed = {"categories": False}
    try:
        with _lock:
            cp = _categories_path()
            if os.path.exists(cp):
                os.remove(cp)
                removed["categories"] = True
    except Exception as e:
        logger.warning(f"清缓存失败（忽略）：{e}")
    return removed
