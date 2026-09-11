"""阶段④ 属性选项的服务端来源。

【为什么不再从 DOM 下拉里逐行读（2026-09-11 真机取证，rowid 184807703145936681）】
点开下拉 + 滚虚拟列表读选项有两个毛病：

1. **会读到截断的清单，而且它自认为完整。** 面板会进入塌缩态
   （`getBoundingClientRect().width == 0`、`scrollHeight == clientHeight`、滚动容器
   根本滚不动），此时只渲染出首屏十来个选项，而滚动循环一步就判「到底」，
   `scrolledToEnd`/`complete` 仍为真——这份「真前缀、假完整」的清单会被
   缓存文件、之后每个同类目商品都拿缺项的 options 去校验、去喂 LLM，没有任何
   环节会发现。实测同一行：服务端权威 24 项，DOM 读取在 0/9/10/11/14/19/24 之间跳。
2. **慢。** 逐行开合下拉 + 滚动收集是 ④ 耗时的主体。

同一页面本来就带着权威数据（前端自己就用这两个接口渲染属性行，页面加载时那次
attributeList.json 有 94KB），所以直接问接口：

    POST /api/popTemuProduct/edit.json?id=<rowid>   → product.shopId、categoryIds（末段即叶子类目）
    POST /api/popTemuCategory/attributeList.json    body: categoryId=&shopId=

实测：水枪类目 颜色 116 / 主体材质 70 / 适用年龄段 24；**全新页签上 DOM 读取与服务端
逐项一致**（缺 0 多 0）——读取代码本身没错，错的是「在长生命周期页面上反复开合下拉」
这种做法本身。
"""

from app.logger import logger
from app.publish.browser import BrowserSession, fill_js


# 两个 POST 都走表单编码（与 shops._JS_WAREHOUSE_LIST 同域同头，该站接口换 JSON body 会 400）。
# 类目 id 不用调用方传：既然后端本来就要靠这一对参数才认，就从同一份 edit.json 里现取，
# 少一个可能与页面实际不一致的外部输入。
_JS_ATTR_OPTIONS = r"""(async () => {
  try {
    const e = await fetch('/api/popTemuProduct/edit.json?id=' + encodeURIComponent(__ROWID__),
                          {credentials: 'include'});
    if (!e.ok) return JSON.stringify({ok: false, status: e.status});
    const p = (((await e.json()).data) || {}).product || {};
    const ids = String(p.categoryIds || '').split('/').filter(Boolean);
    // 【类目 id 优先用调用方给的页面现值】edit.json 读的是服务端【已保存】的草稿，
    // 而阶段③ 是运行中改类目的——改完不保存，草稿里的 categoryIds 还是上一版。
    // 2026-09-11 实测：按它查出的是电子类目的 20 个属性（电池容量/插头规格/…），
    // 页面上真正要填的「面料类型」等动态属性一个都不在里面，阶段④ 因此判必填填不上。
    const categoryId = __CATID__ || ids[ids.length - 1];
    const shopId = p.shopId;
    if (!categoryId || !shopId) {
      return JSON.stringify({ok: false, reason: 'no-category-or-shop',
                             categoryIds: p.categoryIds || null, shopId: shopId || null});
    }
    const r = await fetch('/api/popTemuCategory/attributeList.json', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
      body: 'categoryId=' + encodeURIComponent(categoryId)
          + '&shopId=' + encodeURIComponent(shopId),
    });
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const d = await r.json();
    if (d.code !== 0) return JSON.stringify({ok: false, code: d.code, msg: d.msg || ''});
    const attrs = [];
    for (const it of (d.data || [])) {
      // values 是 JSON 字符串（形如 [{"vid":..,"value":"白色","lang2Value":{..}}]），
      // 单个属性解析失败不该拖垮整份清单，故逐项 try。
      let values = [];
      try {
        values = (JSON.parse(it.values) || []).map(v => v && v.value).filter(Boolean);
      } catch (err) { values = []; }
      attrs.push({name: it.name, values: values});
    }
    return JSON.stringify({ok: true, categoryId: categoryId, shopId: String(shopId),
                           attrs: attrs});
  } catch (err) { return JSON.stringify({ok: false, err: String(err).slice(0, 200)}); }
})"""


async def fetch_attr_options(session: BrowserSession, rowid: str,
                             category_id: str = "") -> dict:
    """取该类目下全部属性的可选值，返回 {属性名: [值…]}。

    rowid 是编辑页当前打开的那个草稿，店铺 id（shopId）从它现取。
    category_id 传【页面上当前生效】的叶子类目 id（阶段③ 选定后经 ctx 传下来的），
    留空才退回「按草稿已保存的类目查」——后者只在本次没改过类目时才正确，见 _JS_ATTR_OPTIONS
    里那段取证。类目没改的快路径（沿用草稿默认类目）正是靠留空走这条。

    【失败不抛、返回 {}】沿用项目辅助路径的 best-effort 取向：拿不到选项时上层会把
    该行标成读不到并如实报出来（阶段④ 因此判失败交兜底），**绝不回退去开下拉**
    ——那正是本模块要替掉的那条路，且它的失败方式（返回半截清单还自称完整）比
    读不到更糟。返回值里没有的属性名同样按「这行读不到选项」处理，由调用方标记。
    """
    if not rowid:
        logger.warning("取属性选项需要 rowid（草稿 id），本次拿不到选项")
        return {}
    data = await session.eval_json(fill_js(_JS_ATTR_OPTIONS, ROWID=str(rowid),
                                           CATID=str(category_id or "")))
    if not data.get("ok"):
        logger.warning(
            "取属性选项失败（本次没有选项可用）："
            + str(data.get("msg") or data.get("err") or data.get("status")
                  or data.get("reason") or "未知原因"))
        return {}
    out = {a["name"]: (a.get("values") or [])
           for a in (data.get("attrs") or []) if a.get("name")}
    # 类目 id 的来源要记进日志：查错类目时清单「看起来正常」（一样是一份属性名与
    # 可选值），只有这一行能把「拿的是页面现值还是草稿旧值」区分开。
    logger.info(f"属性选项取自服务端：类目 {data.get('categoryId')}"
                f"（{'页面现值' if category_id else '草稿已保存值'}）/ 店铺 "
                f"{data.get('shopId')}，共 {len(out)} 个属性")
    return out
