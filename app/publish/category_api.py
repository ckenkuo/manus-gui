"""阶段③ 类目候选的服务端来源。

【为什么从接口取候选，而不是逐级点弹窗读列】原先每级要做两件靠 UI 的事：读当前列的
候选项，再对每个候选「点一下看子级、读完复位」（`_lookahead_children`，每候选
`sleep 0.8`）。前者与接口逐项一致（2026-09-11 实测一级候选 25 个、名字与顺序完全相同），
后者接口直接就能给——不必点、不必等、也不必再复位，而复位失败本就是那条路的已知隐患
（探测留下的脏状态会让后续按索引点击错位）。

选中仍然要靠 UI：接口只负责「有哪些候选、哪个是叶子」，把选中路径落到表单上还是
`_click_cat_in_column` 逐列点。接口给的名字与 UI 列里的名字实测同源，故按名字点击安全。

接口（与页面自己渲染类目树用的是同两个，POST + 表单编码）：

    POST /api/popTemuProduct/edit.json?id=<rowid>     → product.shopId
    POST /api/popTemuCategory/list.json               body: shopId=[&categoryParentId=<catId>]
         → 该级候选 [{catId, catName, isLeaf, isHidden, catLevel, ...}]
"""

from app.logger import logger
from app.publish.browser import BrowserSession, fill_js


_JS_SHOP_ID = r"""(async () => {
  try {
    const e = await fetch('/api/popTemuProduct/edit.json?id=' + encodeURIComponent(__ROWID__),
                          {credentials: 'include'});
    if (!e.ok) return JSON.stringify({ok: false, status: e.status});
    const p = (((await e.json()).data) || {}).product || {};
    return JSON.stringify({ok: true, shopId: p.shopId ? String(p.shopId) : ''});
  } catch (err) { return JSON.stringify({ok: false, err: String(err).slice(0, 200)}); }
})"""


# 取某一级的候选。parentId 为空串即取一级类目。
_JS_CATEGORY_LEVEL = r"""(async (parentId) => {
  try {
    const body = 'shopId=' + encodeURIComponent(__SHOPID__)
               + (parentId ? '&categoryParentId=' + encodeURIComponent(parentId) : '');
    const r = await fetch('/api/popTemuCategory/list.json', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
      body: body,
    });
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const d = await r.json();
    if (d.code !== 0) return JSON.stringify({ok: false, code: d.code, msg: d.msg || ''});
    return JSON.stringify({ok: true, rows: (d.data || []).map(x => ({
      catId: String(x.catId), catName: x.catName || '',
      isLeaf: !!x.isLeaf, isHidden: !!x.isHidden}))});
  } catch (err) { return JSON.stringify({ok: false, err: String(err).slice(0, 200)}); }
})"""


# 前瞻：一次并发取多个父级的候选（每个父级一次请求，Promise.all 并起来只占一个往返）。
_JS_CATEGORY_CHILDREN = r"""(async (parentIds) => {
  try {
    const one = async (pid) => {
      const r = await fetch('/api/popTemuCategory/list.json', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
        body: 'shopId=' + encodeURIComponent(__SHOPID__)
            + '&categoryParentId=' + encodeURIComponent(pid),
      });
      if (!r.ok) return [];
      const d = await r.json();
      if (d.code !== 0) return [];
      return (d.data || []).map(x => x.catName || '').filter(Boolean);
    };
    const lists = await Promise.all(parentIds.map(one));
    const out = {};
    parentIds.forEach((pid, i) => { out[pid] = lists[i] || []; });
    return JSON.stringify({ok: true, byParent: out});
  } catch (err) { return JSON.stringify({ok: false, err: String(err).slice(0, 200)}); }
})"""


async def fetch_shop_id(session: BrowserSession, rowid: str) -> str:
    """取该草稿所属店铺的 shopId（类目接口的必填参数）。取不到返回空串。"""
    if not rowid:
        logger.warning("取店铺 id 需要 rowid（草稿 id）")
        return ""
    data = await session.eval_json(fill_js(_JS_SHOP_ID, ROWID=str(rowid)))
    if not data.get("ok") or not data.get("shopId"):
        logger.warning(f"取店铺 id 失败："
                       f"{data.get('err') or data.get('status') or '响应里没有 shopId'}")
        return ""
    return data["shopId"]


async def fetch_children(session: BrowserSession, shop_id: str,
                         parent_id: str = "") -> list:
    """取某一级类目的候选，返回 [{"catId", "catName", "isLeaf", "isHidden"}]。

    parent_id 为空串即取一级类目。取不到时返回空列表（调用方据此收尾）。
    【不过滤 isHidden】实测一级候选与弹窗那一列逐项一致（含顺序），多一层过滤反而
    会与 UI 对不上；真点到隐藏项会在点击那一步如实报错，不会静默选错。
    """
    if not shop_id:
        return []
    data = await session.eval_json(
        fill_js(_JS_CATEGORY_LEVEL, SHOPID=str(shop_id)), arg=str(parent_id or ""))
    if not data.get("ok"):
        logger.warning(f"取类目候选失败（父级 {parent_id or '一级'}）："
                       f"{data.get('msg') or data.get('err') or data.get('status')}")
        return []
    rows = [r for r in (data.get("rows") or []) if r.get("catName")]
    logger.info(f"类目候选（父级 {parent_id or '一级'}）：{len(rows)} 个")
    return rows


async def fetch_children_map(session: BrowserSession, shop_id: str,
                             parent_ids: list) -> dict:
    """前瞻用：一次取多个父级的子类目名，返回 {父 catId: [子类目名…]}。"""
    ids = [str(p) for p in (parent_ids or []) if p]
    if not shop_id or not ids:
        return {}
    data = await session.eval_json(
        fill_js(_JS_CATEGORY_CHILDREN, SHOPID=str(shop_id)), arg=ids)
    if not data.get("ok"):
        logger.warning(f"取前瞻子类目失败（忽略，只影响判断质量）："
                       f"{data.get('err')}")
        return {}
    return {k: (v or []) for k, v in (data.get("byParent") or {}).items()}
