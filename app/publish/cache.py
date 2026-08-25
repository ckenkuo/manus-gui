# -*- coding: utf-8 -*-
"""发布管线的类目路径 / 属性选项磁盘缓存。

为什么要这一层（2026-08-22 真站实测的成本账）：
    阶段③ auto_cat 耗时 110.1s——5 级类目，每级先逐个点候选前瞻子类目、再调一次
        LLM，一共 5 次 LLM + 大量 DOM 点击。而按【已知路径】逐列直点（不前瞻、
        不调 LLM）走完同样 5 级只花 7.6s。
    阶段④ attrs 耗时 92.9s——大头在逐个必填行点开下拉、滚 rc-virtual-list 收集
        选项（每屏必须等 180ms，见 _read_active_options 的坑3 注释）。
两块的开销几乎全花在【重复发现已知信息】上：实际用到的类目就是恒定的那么一些，
而属性行与下拉选项由类目决定、与具体商品无关（实测：女童针织套头衫 33 行/16 必填，
女童长裤套装 42 行/17 必填），所以都能持久化下来复用。

【只缓存客观事实，不缓存判断】属性只存 label / required / options 这类「类目决定
的表单结构」，绝不存 LLM 的修改结论——属性值取决于具体商品（成分、图案、细节各
不相同），复用判断就是填错。每个商品仍照旧调一次 LLM 做匹配，省掉的只是读选项。

【不存 current】实测页面的 current 带着前一批已填的值。存进去会让下一个商品的
LLM 以为表单已经填好了，是最隐蔽的一类污染。

【失效策略：写入即校验，不一致就回落重读，不设过期时间】set_attr 本就逐项回读
校验（返回 status="error" 表示点不中，即选项已变），那正是发现缓存过期的天然时机。
此时只重读那一行的真实 options、回灌缓存、单独再问一次 LLM 重写该行，其余行不受
影响，不整体退回全量遍历。缓存失效的后果因此只是【变慢】而不是【变错】——这也是
本模块能安心全程 best-effort 吞异常的前提（与 service.load_state/save_state 同一
取向：坏了、缺了就当未命中，绝不阻断主流程）。

骨架照 app/cloud_docs.py（项目里既有的本地持久化登记簿）：模块级路径常量 + 锁 +
best-effort 读写。
"""
import hashlib
import json
import os
import re
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
_ATTRS_SUBDIR = "attrs"

MAX_PROMPT_PATHS = 60      # 进提示词的路径条数上限（按 last_used 降序截断）
_MAX_TITLE_SAMPLES = 3     # 每条路径留几个历史标题样本（喂提示词用）
_SCHEMA_VERSION = 1

_lock = threading.Lock()


def _categories_path() -> str:
    return os.path.join(CACHE_DIR, _CATEGORIES_NAME)


def _attrs_path(slug: str) -> str:
    return os.path.join(CACHE_DIR, _ATTRS_SUBDIR, f"{slug}.json")


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

def cat_slug(leaf: str, path: list, site: str = "") -> str:
    """类目 → 属性缓存的文件名 slug。空 leaf 返回 ""（调用方当未命中，不去猜）。

    【末尾必须带整条路径的短哈希，这不是保险而是必需】_CAT_PROMPT 规则 3 提到的
    「其他（...）」这类叶子类目名在多个分支下重复出现，「女童针织衫」之类的名字也
    会跨分支撞。只按叶子名分文件，撞了就是两套 options 混进一个文件，而
    _validate_attr_changes 的第 2 道闸（value 必须在 options 内）会因此放过一个
    页面上根本不存在的选项——点不中还留幽灵浮层。

    slug 里也带 site：同一类目在不同经营站点下选项集是否一致【未经验证】，现在加
    进去代价接近零，等攒了几十个文件再加就要处理迁移。
    """
    leaf = str(leaf or "").strip()
    if not leaf:
        return ""
    # 非法字符集与 pipeline.py 里拿颜色名当文件名那处保持同一份
    name = re.sub(r'[\\/:*?"<>|]', "_", leaf)
    # Windows 不允许文件名以点或空格结尾（open() 会静默落到另一个名字上）
    name = name.rstrip(". ")[:40] or "cat"
    digest = hashlib.md5(
        (" > ".join(path or []) + "|" + str(site or "")).encode("utf-8")
    ).hexdigest()[:8]
    prefix = re.sub(r'[\\/:*?"<>|]', "_", str(site or "").strip()).rstrip(". ")
    return f"{prefix}-{name}-{digest}" if prefix else f"{name}-{digest}"


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


def remember_category(path: list, title: str = "") -> None:
    """记住一条走通的类目路径（已存在则 hits+1、追加标题样本、刷 last_used）。

    写之前【重新读一次磁盘】再合并，不用调用方手上那份可能已过期的列表：两个进程
    同时跑时这样最多丢掉几个计数，不会丢掉整条路径记录。
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


# ---- 属性选项缓存 ------------------------------------------------------------

def load_attr_options(leaf: str, path: list, site: str = "") -> dict:
    """返回 {label: options}。未命中/损坏/catPath 不符 → {}。

    catPath 复核：slug 已带路径哈希，理论上不会串台，但文件被手工改过、或 schema
    升级后名字口径变了时，这道复核让它退化成「未命中」而不是喂错数据。
    """
    slug = cat_slug(leaf, path, site)
    if not slug:
        return {}
    data = _read_json(_attrs_path(slug))
    if not data:
        return {}
    cached_path = data.get("catPath")
    if isinstance(cached_path, list) and path and cached_path != list(path):
        logger.warning(f"属性缓存 catPath 不符，当未命中（{slug}）")
        return {}
    out = {}
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        label, opts = row.get("label"), row.get("options")
        if label and isinstance(opts, list) and opts:
            out[label] = opts
    return out


def save_attr_options(leaf: str, path: list, attrs: list, site: str = "") -> None:
    """把现场读到的属性行写入缓存，与已有内容【按 label 并集合并】。

    合并而非整文件覆盖：单次 dump_attrs 默认只读必填项，而平台会调整同一类目的必填
    集合（实测同类目从 18 必填变成 16），加上探查时可能用 required_only=False 读到
    非必填行，各次读到的行集并不相同，覆盖会把上次辛苦读到的行冲掉。合并让同一类目
    的缓存随使用逐步长全。

    【只收 options 非空、且确认读全的行】row-hidden / optional-skipped / open-failed
    三种都是空 options，写进去之后「命中但空」和「未命中」就分不开了。只存非空清单，
    则「文件里没有这个 label」永远等价于「这行要现场读」，dump_attrs 那个 guard 才能
    只用一行判断。
    optionsComplete 为 False（虚拟列表没滚到底）的行同样拒收：截断的清单进了缓存，
    之后每个同类目商品都会拿一份缺项的 options 去做 options 校验与主成分纤维匹配，
    而且没有任何环节会发现——比不缓存糟得多。字段缺失（老调用方不传）时视为完整，
    保持向后兼容。
    """
    slug = cat_slug(leaf, path, site)
    if not slug:
        return
    try:
        with _lock:
            old = _read_json(_attrs_path(slug)) or {}
            rows = {}
            for row in old.get("rows") or []:
                if isinstance(row, dict) and row.get("label"):
                    rows[row["label"]] = row
            for a in attrs or []:
                opts = a.get("options")
                if not a.get("label") or not isinstance(opts, list) or not opts:
                    continue
                if a.get("optionsComplete") is False:
                    logger.warning(
                        f"{a['label']} 的选项未读全（{len(opts)} 项），不进缓存")
                    continue
                # 【数值行的「选项」是只读单位，绝不能存】里料克重（g/m²) 那行是
                # 「输入框 + 只读单位下拉」的复合结构，误当下拉行读会得到 ['g/㎡']。
                # 存进来的后果不是点错选项，而是让该行在下一个同类目商品那里【看起来
                # 已填】（current 读成单位文本），LLM 与必填复扫都不会管它，数值框
                # 一直空着、保存卡「请输入产品属性」（2026-08-25 修复的缺陷）。
                # dump_attrs 现在已按 kind 跳过这类行，这里再挡一道：脏数据一旦落盘
                # 没有任何环节会发现，与 optionsComplete 那道闸是同一类考虑。
                if a.get("kind") == "number":
                    logger.warning(f"{a['label']} 是数值输入行，选项不进缓存")
                    continue
                # 【数值行的「选项」是只读单位，绝不能存】里料克重（g/m²) 那行是
                # 「输入框 + 只读单位下拉」的复合结构，误当下拉行读会得到 ['g/㎡']。
                # 存进来的后果不是点错选项，而是让该行在下一个同类目商品那里【看起来
                # 已填】（current 读成单位文本），LLM 与必填复扫都不会管它，数值框
                # 一直空着、保存卡「请输入产品属性」（2026-08-25 修复的缺陷）。
                # dump_attrs 现在已按 kind 跳过这类行，这里再挡一道：脏数据一旦落盘
                # 没有任何环节会发现，与 optionsComplete 那道闸是同一类考虑。
                rows[a["label"]] = {"label": a["label"],
                                    "required": bool(a.get("required")),
                                    "options": opts}
            if not rows:
                return
            _write_json(_attrs_path(slug), {
                "version": _SCHEMA_VERSION, "leaf": leaf,
                "catPath": list(path or []), "site": site or "",
                "updated_at": _now(), "rows": list(rows.values()),
            })
    except Exception as e:
        logger.warning(f"属性缓存写入失败（忽略）：{e}")


def update_attr_row(leaf: str, path: list, label: str, options: list,
                    site: str = "") -> None:
    """单行【覆盖式】更新（写入失败后回落重读的落点）。

    这里是覆盖不是并集：重读的结果就是当前真相，旧的过期选项必须消失，否则下一个
    同类目商品还会撞同一堵墙。
    """
    slug = cat_slug(leaf, path, site)
    if not slug or not label or not options:
        return
    try:
        with _lock:
            data = _read_json(_attrs_path(slug)) or {}
            rows = [r for r in (data.get("rows") or [])
                    if isinstance(r, dict) and r.get("label") != label]
            old = next((r for r in (data.get("rows") or [])
                        if isinstance(r, dict) and r.get("label") == label), {})
            rows.append({"label": label,
                         "required": bool(old.get("required")),
                         "options": list(options)})
            _write_json(_attrs_path(slug), {
                "version": _SCHEMA_VERSION, "leaf": leaf,
                "catPath": list(path or []), "site": site or "",
                "updated_at": _now(), "rows": rows,
            })
    except Exception as e:
        logger.warning(f"属性缓存单行更新失败（忽略）：{e}")


# ---- 统计与清理（UI / CLI 用）------------------------------------------------

def cache_stats() -> dict:
    """缓存现状统计（只读，给日志与 UI 面板用）。"""
    cats = load_categories()
    files, rows = [], 0
    attrs_dir = os.path.join(CACHE_DIR, _ATTRS_SUBDIR)
    try:
        names = sorted(os.listdir(attrs_dir))
    except Exception:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(os.path.join(attrs_dir, name)) or {}
        n = len([r for r in (data.get("rows") or []) if isinstance(r, dict)])
        rows += n
        files.append({"slug": name[:-5], "leaf": data.get("leaf") or "",
                      "site": data.get("site") or "",
                      "catPath": data.get("catPath") or [],
                      "rowCount": n, "updated_at": data.get("updated_at") or ""})
    return {"paths": len(cats), "attrCategories": len(files),
            "attrRows": rows, "categories": cats, "attrFiles": files}


def clear(slug: str = "") -> dict:
    """清缓存：给了 slug 只删那个属性文件，否则清空整个缓存目录。

    返回实际删掉了什么，供 UI 显示。删不掉只告警（文件被占用等），不抛。
    """
    removed = {"categories": False, "attrFiles": []}
    try:
        with _lock:
            if slug:
                p = _attrs_path(slug)
                if os.path.exists(p):
                    os.remove(p)
                    removed["attrFiles"].append(slug)
                return removed
            attrs_dir = os.path.join(CACHE_DIR, _ATTRS_SUBDIR)
            for name in (os.listdir(attrs_dir) if os.path.isdir(attrs_dir) else []):
                if name.endswith(".json"):
                    os.remove(os.path.join(attrs_dir, name))
                    removed["attrFiles"].append(name[:-5])
            cp = _categories_path()
            if os.path.exists(cp):
                os.remove(cp)
                removed["categories"] = True
    except Exception as e:
        logger.warning(f"清缓存失败（忽略）：{e}")
    return removed
