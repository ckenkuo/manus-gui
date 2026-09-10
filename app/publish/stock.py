"""店小秘发布操作：stock。模块导航见 docs/publish-pipeline-refactor.md。"""

import json
import asyncio
import re
from app.logger import logger
from app.publish import accessories, stock_scripts
from app.publish.browser import BrowserSession, J
from typing import Optional


# 一批多少行。30 行来自实测速率：包装清单每行约 1~2s（点加号/删子行各 450ms + 每个
# 配件下拉展开与搜索），30 行最坏约 60s，离单次 eval 的 180s 上限还有三倍余量；
# SKU分类那段每行只有两次 setSel + 一次 setInp，30 行更快。再往大取就失去了分批的
# 意义（88 行整表跑了 190s，133 行直接撞 180s 超时，正是 2026-08-31 商品
# 1015843615215 的病根）。
_ROW_BATCH = 30


async def _fill_rows_batched(session: BrowserSession, js: str, what: str,
                             timeout: int = 180) -> dict:
    """把带 __START__/__END__ 行区间的填写 JS 分批执行，合并成单次调用的返回形状。

    【为什么必须分批而不是把超时调大】2026-08-31 商品 1015843615215（19 色 x 7 码 =
    133 行 SKU）在阶段⑪ 撞 180s 超时失败。这两段 JS 都是「逐行 DOM 操作 + 每步
    sleep」的结构，耗时与行数成正比：5 行约 20s、88 行 190s、133 行超过 180s。
    把 timeout 往上堆只是把红线后移，颜色再多一倍照样炸；且单次 eval 越长，中途
    遇到 Vue 重渲染/页面导航就整批白跑，没有任何已完成进度。分批后每批 30 行、
    单批耗时与商品无关，行数上限被彻底解耦。

    【为什么行区间在 JS 侧切、而不是 python 侧传 tr 列表】DOM 节点不能跨 eval 传递，
    每批必须自己重新 querySelectorAll。切片用【整表下标】而非「第 N 个匹配行」：
    包装清单列的判定（packTdOf）本身可能漏掉某些行，用匹配序号切片会让批与批之间
    的边界随判定结果漂移，整表下标是稳定的。

    【末批如何收敛】JS 回传 rowCount（整表 tr 数），据此算出总批数——首批跑完就知道
    还有几批，不必先单独发一次「读行数」的 eval。
    """
    start, merged, batches = 0, None, 0
    while True:
        r = await session.eval_json(
            js.replace("__START__", str(start)).replace("__END__", str(start + _ROW_BATCH)),
            timeout=timeout)
        if r.get("err"):
            return r
        batches += 1
        if merged is None:
            merged = dict(r)
        else:
            # processed 累加；bad/failed/sample 合并（sample 只留前几条，同 JS 侧口径）
            merged["processed"] = (merged.get("processed") or 0) + (r.get("processed") or 0)
            for k in ("bad", "failed"):
                if r.get(k):
                    merged[k] = (merged.get(k) or []) + r[k]
            if r.get("sample"):
                merged["sample"] = ((merged.get("sample") or []) + r["sample"])[:4]
            merged["total"] = r.get("total")
        row_count = r.get("rowCount") or 0
        start += _ROW_BATCH
        if start >= row_count:
            break
    if batches > 1:
        logger.info(f"{what}：{merged.get('rowCount')} 行分 {batches} 批填写"
                    f"（每批 {_ROW_BATCH} 行），处理 {merged.get('processed')} 行")
    return merged or {}


def resolve_warehouse(site: str = "") -> str:
    """按站点解析仓库名（阶段⑪「选择仓库」勾选哪一个）。

    【为什么仓库名不能写死「飞特COL仓库」】编辑页仓库下拉的选项集合因站点而异
    （bulkattr 已实测：哥伦比亚站「飞特COL仓库 / 哥伦比亚-新势力」、秘鲁站「飞特PE仓库」、
    美国站「嘉运美东仓库」）。写死一个名字，非该站点的商品在下拉里找不到匹配项，
    _JS_PICK_WAREHOUSE 报 no-option、整单未落库。故仓库名按站点查 config.toml 的
    [publish.warehouse_by_site] 映射，站点未配时退回 [publish].warehouse 的全局默认，
    再没有才用代码内兜底「飞特COL仓库」。

    site 取 --site 的站点名（如「美国」）。页面上「美国」与「美国站」两种写法都出现，
    这里归一去掉尾部「站」再查。
    """
    default = "飞特COL仓库"
    try:
        import tomllib

        from app.config import config_search_dirs
        for d in config_search_dirs():
            p = d / "config.toml"
            if not p.exists():
                continue
            with open(p, "rb") as f:
                data = tomllib.load(f)
            pub = data.get("publish") or {}
            default = str(pub.get("warehouse") or default)
            by_site = pub.get("warehouse_by_site") or {}
            if site:
                key = site[:-1] if site.endswith("站") else site
                if key in by_site:
                    return str(by_site[key])
    except Exception as e:
        logger.warning(f"读取 [publish] 站点仓库映射失败（退回默认）：{e}")
    return default


def _warehouse_ready(state: dict, warehouse: str) -> bool:
    normalize = lambda value: re.sub(r"仓库$", "仓", re.sub(r"\s+", "", str(value)))
    target = normalize(warehouse)
    return (bool(target) and any(normalize(value) == target for value in state.get("selected", []))
            and any(normalize(str(header).split('库存')[0]) == target
                    for header in state.get("stockHeaders", [])))


async def ensure_warehouse(session: BrowserSession, warehouse: str) -> dict:
    state = await session.eval_json(stock_scripts._JS_WH_STATE)
    if state.get("err"):
        return {"status": "error", "stage": "warehouse", "reason": state["err"], **state}
    if not _warehouse_ready(state, warehouse):
        picked = await session.eval_json(stock_scripts._JS_PICK_WAREHOUSE.replace("__WH__", J(warehouse)))
        if picked.get("err"):
            return {"status": "error", "stage": "warehouse-option",
                    "reason": f"站点仓库 {warehouse} 未选中：{picked['err']}，可用选项 {picked.get('available', [])}", **picked}
        for attempt in range(12):
            state = await session.eval_json(stock_scripts._JS_WH_STATE)
            if _warehouse_ready(state, warehouse):
                break
            await asyncio.sleep(0.5)
    if not _warehouse_ready(state, warehouse):
        return {"status": "error", "stage": "warehouse",
                "reason": "仓库选择未生效或站点库存列未生成，请同步仓库后重试", **state}
    return {"status": "ok", "warehouse": warehouse, **state}


async def set_stock(session: BrowserSession, info_path: str,
                    stock: str = "100", warehouse: str = "",
                    site: str = "", sku_judge: Optional[dict] = None,
                    cat_path: Optional[list] = None) -> dict:
    """阶段⑪：仓库/库存/SKU分类批量填写。

    完整流程（2026-08-18用户确认）：
    1. 选择仓库：勾选目标仓库（按站点解析，见 resolve_warehouse），**勾选后库存列才渲染**
    2. 填库存：统一值（默认100），等仓库勾选后 input[name=stock] 出现
    3. SKU分类：按标题+套装件数交 LLM 判断（单品/同款多件/混合套装 + 数量 + 单位）
    4. 包装清单：逐行填「配件名 + 件数」，件数之和必须等于第 3 步的数量（平台强校验，
       2026-08-27 商品 1051793179451 因此被打回，见 judge_sku_category）

    sku_judge：提前预热的 SKU 分类判断结果（见 service._run_prewarm），给了就不再
    问模型。它的输入与页面无关（只有标题和套装件数），故预热与现场同值。
    """
    warehouse = warehouse or resolve_warehouse(site)

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)

    # 1. 选择仓库
    wh = await ensure_warehouse(session, warehouse)
    if wh.get("status") != "ok":
        return wh

    # 2. 填库存（勾选仓库后才渲染）
    st = await session.eval_json(stock_scripts._JS_FILL_STOCK_ONLY.replace("__STOCK__", J(str(stock))))
    if st.get("err") or st.get("bad"):
        return {"status": "error", "stage": "stock", **st}

    # 3. SKU分类：交 LLM 判断（预热命中就直接用，见 judge_sku_category）
    # 预热结果也过一遍归一：预热是 2026-08-27 之前写的结构（可能没有 packing 字段），
    # 且缓存/续跑会带回老结果，不归一就会拿着空清单去填。
    judge = accessories._normalize_sku_judge(sku_judge, info) if sku_judge else await accessories.judge_sku_category(info, cat_path=cat_path)
    if sku_judge:
        logger.info("SKU分类沿用提前预热的判断结果，跳过本阶段 LLM 调用")
    cat, qty, unit = str(judge.get("skuCat", "1")), str(judge.get("qty", 1)), str(judge.get("unit", "1"))

    res = await _fill_rows_batched(session, stock_scripts._JS_FILL_STOCK_CAT
                                   .replace("__CAT__", J(cat))
                                   .replace("__QTY__", J(qty))
                                   .replace("__UNIT__", J(unit)),
                                   what="SKU分类")
    if res.get("err"):
        return {"status": "error", "stage": "sku-category", **res}

    # 4. 包装清单：件数和已由 _normalize_sku_judge 对齐到 qty，这里只负责填页面。
    # 页面回读若发现和不等（例如某项配件名没匹配上、行数没补齐），报 validation-error
    # 交上层重试——放过去等于让接口再打回一次。
    packing = judge.get("packing") or []
    pk = await _fill_rows_batched(
        session, stock_scripts._JS_FILL_PACKING.replace("__ITEMS__", J(packing)),
        what="包装清单", timeout=180)
    if pk.get("err"):
        return {"status": "error", "stage": "packing", **pk}
    if pk.get("failed"):
        logger.warning(f"包装清单有 {len(pk['failed'])} 处配件未选中：{pk['failed'][:3]}")

    ok = (res.get("processed", 0) > 0 and not res.get("bad")
          and pk.get("processed", 0) > 0 and not pk.get("bad") and not pk.get("failed"))
    return {"status": "ok" if ok else "validation-error",
            "warehouse": warehouse, "stock": stock,
            "skuCategory": {"cat": cat, "qty": qty, "unit": unit, "reason": judge.get("reason")},
            "packing": packing,
            "processed": res.get("processed"), "bad": res.get("bad"),
            "sample": res.get("sample"),
            "packingProcessed": pk.get("processed"), "packingBad": pk.get("bad"),
            "packingFailed": pk.get("failed"), "packingSample": pk.get("sample")}
