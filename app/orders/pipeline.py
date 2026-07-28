# -*- coding: utf-8 -*-
"""订单登记管线的确定性步骤：页面翻页勾选 → 触发官方导出 → 解析 xlsx → 抓主图。

为什么走「导出订单」而不是抓接口或逐字段读 DOM：
  - 订单列表请求跑在 Web Worker（`blob:https://agentseller.temu.com/...`）里，还叠了
    商家助手扩展的 `xhr-interceptor.js`。页面级 Playwright 和页面级 CDP `Network.*`
    都拿不到响应体（实测枚举了 98 条请求 / 71 种 URL，订单接口确实不在其中）；自造
    fetch 直连又过不了 anti-content 动态签名。
  - 逐字段读 DOM 要跟一堆构建期 hash 类名（`_3AHRHYjy` 这种）赛跑，改版就全崩。
  官方导出是唯一稳的路：一次点击拿全量、字段名固定、`--` 表示空值。

主图是唯一必须从 DOM 拿的字段（导出文件里没有图列），且图片是 IntersectionObserver
懒加载——表格在内部滚动容器里，`window.scrollTo` 完全无效，必须逐行 scrollIntoView。

全流程零大模型：确定性脚本 + 官方导出，没有任何 function-calling。别把它 agent 化。
"""
import asyncio
import inspect
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.collect.pipeline import _download_main_image
from app.logger import logger

# ---- 已实测确认的选择器（2026-07-27，agentseller.temu.com）------------------
# beast-core 是 Temu 后台的组件库，类名带 `_123` 版本后缀（如 PGT_totalText_123）。
# 一律用 [class*="前缀"] 前缀匹配，避免后缀跳版本就全崩。
_PAGINATION = 'ul[data-testid="beast-core-pagination"]'
_PAGE_NEXT = 'li[data-testid="beast-core-pagination-next"]'
_TOTAL_TEXT = '[class*="PGT_totalText"]'
# 表头全选框：真实 <input> 是 0×0 + opacity:0 的隐形元素（beast-core 的惯用做法，靠外层
# label 显示），点它必然 30s 超时报「element is not visible」。所以【状态读 input、点击点
# label】：label 带稳定的 data-testid，不像 CBX_*_123 那样跟版本号跳。
_HEAD_CHECKBOX = 'table thead input[type="checkbox"]'
_HEAD_CHECKBOX_CLICK = 'table thead label[data-testid="beast-core-checkbox"]'

# 「共有 247 条」/「已选订单：40」
_RE_TOTAL = re.compile(r"共有\s*([\d,]+)\s*条")
_RE_SELECTED = re.compile(r"已选订单[:：]\s*([\d,]+)")

# 导出字段设置弹窗的分组名。序号固定为
# 0订单号(disabled,强制) 1订单信息 2子订单信息 3商品信息 4收货信息 5运单信息 6操作节点
# 7过滤已取消的商品，默认全勾。**收货信息必须取消**：登记表用不到，而它带的是买家 PII
# （姓名/电话/邮箱/身份证号/税号/地址），不该落到本地表。该设置会被后台记住，所以每次
# 都要先读当前状态再决定点不点（幂等）。
_EXPORT_GROUP_EXCLUDE = "收货信息"

# 导出文件的 19 个列名（表头第 1 行，sheet 名 `sheet1`）。
EXPORT_COLUMNS = [
    "订单号", "站点", "订单状态", "子订单号", "应履约件数", "商品名称",
    "SKUID", "SKCID", "SPUID", "SKU货号", "商品属性",
    "运单号", "物流商", "发货仓",
    "订单创建时间", "要求最晚发货时间", "实际发货时间", "预计送达时间", "实际签收时间",
]

# 导出文件里的空值是字符串 `--`，不是空单元格。
_EMPTY_TOKENS = {"--", "-", "—", "None", "null"}


@dataclass
class OrderRow:
    """一条【子订单】级记录——导出文件按子订单展开（20 个订单导出 22 行）。"""

    order_no: str = ""          # 订单号 PO-045-...
    site: str = ""              # 站点（已归一化，去掉尾字「站」）
    status: str = ""            # 订单状态
    sub_order_no: str = ""      # 子订单号 045-...
    qty: str = ""               # 应履约件数
    goods_name: str = ""
    sku_id: str = ""
    skc_id: str = ""
    spu_id: str = ""
    sku_code: str = ""          # SKU货号
    attrs: str = ""             # 商品属性 → 登记表的「尺码」
    tracking_no: str = ""       # 运单号（待发货态为空）
    carrier: str = ""
    warehouse: str = ""
    created_at: str = ""        # 订单创建时间
    latest_ship_at: str = ""    # 要求最晚发货时间
    shipped_at: str = ""
    eta: str = ""               # 预计送达时间
    received_at: str = ""
    # 以下两个来自 DOM，按子订单号 join 进来（导出文件没有）
    image_url: str = ""
    # 成交单价：商家助手插件注入 DOM，**不是 Temu 官方字段**（官方导出 19 列里没有价格
    # 列）。2026-07-28 实机确认回填有约一天延迟：当天下的单页面上连「成交单价」标签都没
    # 有，前一天的都有。故 plan_writes 默认 require_price=True，无价的留到下批。
    deal_price: str = ""
    image_path: str = ""        # 下载落地后的本地路径


# ---- 离线部分（可用样本文件单测，不需要浏览器）-----------------------------


def _clean(v: Any) -> str:
    """单元格值归一化：`--` 之类的占位空值统一成空串。"""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in _EMPTY_TOKENS else s


def normalize_site(raw: Any) -> str:
    """站点归一化：导出的「哥伦比亚站」→「哥伦比亚」。

    登记表里同一站点混着两种写法（实测「哥伦比亚」4379 行 vs「哥伦比亚站」30 行），
    主流是不带「站」。统一去掉尾字，既对得上表内主流写法，也让 sheet_map 只需配一种。
    """
    s = _clean(raw)
    return s[:-1] if len(s) > 1 and s.endswith("站") else s


def parse_export_xlsx(path: str) -> List[OrderRow]:
    """解析官方导出的订单 xlsx → [OrderRow]。

    按【表头标题】取列，不认列序：导出字段设置一改（比如以后又勾上某个分组），列序会变。
    表头缺列直接抛异常——字段对不上就别往登记表写。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if not rows:
        raise ValueError(f"导出文件没有任何行：{path}")

    header = [_clean(c) for c in rows[0]]
    idx = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in ("订单号", "站点", "子订单号", "商品属性", "订单创建时间") if c not in idx]
    if missing:
        raise ValueError(f"导出文件缺列 {missing}；实际表头={header}")

    def cell(row: tuple, name: str) -> str:
        i = idx.get(name)
        return _clean(row[i]) if i is not None and i < len(row) else ""

    out: List[OrderRow] = []
    for row in rows[1:]:
        if not any(_clean(c) for c in row):
            continue
        order_no = cell(row, "订单号")
        if not order_no:
            continue
        out.append(
            OrderRow(
                order_no=order_no,
                site=normalize_site(cell(row, "站点")),
                status=cell(row, "订单状态"),
                sub_order_no=cell(row, "子订单号"),
                qty=cell(row, "应履约件数"),
                goods_name=cell(row, "商品名称"),
                sku_id=cell(row, "SKUID"),
                skc_id=cell(row, "SKCID"),
                spu_id=cell(row, "SPUID"),
                sku_code=cell(row, "SKU货号"),
                attrs=cell(row, "商品属性"),
                tracking_no=cell(row, "运单号"),
                carrier=cell(row, "物流商"),
                warehouse=cell(row, "发货仓"),
                created_at=cell(row, "订单创建时间"),
                latest_ship_at=cell(row, "要求最晚发货时间"),
                shipped_at=cell(row, "实际发货时间"),
                eta=cell(row, "预计送达时间"),
                received_at=cell(row, "实际签收时间"),
            )
        )
    return out


# 登记表列标题 → 取值。一个字段可能有多种标题写法（` StoreD` 表用「站点」而非
# 「站点区分」），故用标题集合匹配。这里【只列管线能填的字段】：国内发出时间/采购日期/
# 采购费用/Y2头程费用/采购订单号/物流情况/产品图2 是人工后续填的，管线一律留空不碰。
_FIELD_SOURCES: List[tuple] = [
    ({"订单店铺", "店铺"}, lambda o, ctx: ctx.get("store_value", "")),
    ({"站点区分", "站点"}, lambda o, ctx: o.site),
    ({"订单号"}, lambda o, ctx: o.order_no),
    ({"尺码"}, lambda o, ctx: o.attrs),
    ({"平台物流跟踪号", "平台跟踪号"}, lambda o, ctx: o.tracking_no),
    ({"平台创建时间"}, lambda o, ctx: o.created_at),
    ({"平台成交价"}, lambda o, ctx: o.deal_price),
]


def resolve_title_column(header: Dict[str, str], titles) -> Optional[str]:
    """在表头里找标题命中 titles（字符串或集合）的列字母；找不到返回 None。"""
    want = {titles} if isinstance(titles, str) else set(titles)
    for col, title in sorted(header.items(), key=lambda kv: _col_key(kv[0])):
        if title.strip() in want:
            return col
    return None


def image_column(header: Dict[str, str]) -> Optional[str]:
    """图片列：优先精确「产品图片」，否则取首个含「产品图」的列。

    为什么不能只按「含产品图」：`StoreA全球1` 同时有「产品图片」和「产品图2」，
    后者是人工补图位，管线只写前者。
    """
    exact = resolve_title_column(header, "产品图片")
    if exact:
        return exact
    for col, title in sorted(header.items(), key=lambda kv: _col_key(kv[0])):
        if "产品图" in title.strip():
            return col
    return None


def _col_key(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def build_row_values(
    order: OrderRow, header: Dict[str, str], store_value: str
) -> Dict[str, str]:
    """按目标 Sheet 的【真实表头】生成 {列字母: 值}。

    绝不硬编码列号：13 个登记 Sheet 的列序各不相同（`StoreA全球1` 订单号在 C，
    `牛仔裤` 在 B）。表头没有的字段自然不写，多出来的列留空给人工填。
    """
    ctx = {"store_value": store_value}
    values: Dict[str, str] = {}
    for col, title in header.items():
        t = title.strip()
        for titles, getter in _FIELD_SOURCES:
            if t in titles:
                values[col] = getter(order, ctx)
                break
    return values


def dedupe_key(
    order: OrderRow, header: Dict[str, str], titles: List[str], store_value: str
) -> Optional[tuple]:
    """按判重列标题生成本条订单的键，与 existing_key_tuples 的口径一致（strip 后比较）。

    任一判重列在该 Sheet 找不到 → 返回 None，调用方据此判定「这个 Sheet 没法判重」
    并跳过整个 Sheet，而不是无键硬写出一堆重复行。
    """
    values = build_row_values(order, header, store_value)
    key: List[str] = []
    for title in titles:
        col = resolve_title_column(header, title)
        if not col:
            return None
        key.append(str(values.get(col, "")).strip())
    return tuple(key)


def resolve_sheet(store: str, site: str, sheet_map: List[dict]) -> Optional[dict]:
    """按 (店铺, 站点) 查目标 Sheet 配置；未命中返回 None（调用方跳过并报告，不臆测）。

    店铺按【子串】匹配：页面右上角显示的店铺名可能带后缀（"StoreA Store"），
    配置里只写主名 "StoreA"。站点按归一化后精确匹配。
    """
    s_site = normalize_site(site)
    for item in sheet_map or []:
        cfg_store = str(item.get("store", "")).strip()
        if not cfg_store:
            continue
        if cfg_store not in store and store not in cfg_store:
            continue
        sites = [normalize_site(x) for x in (item.get("sites") or [])]
        if sites and s_site not in sites:
            continue
        return item
    return None


def to_jpeg_url(url: str) -> str:
    """把主图 URL 的 avif 输出参数改成 jpeg。

    kwcdn 默认发 `?imageView2/2/w/800/q/70/format/avif`，而 Excel/WPS 不认 avif，
    嵌进去就是一个空白格。顺手把宽度压到 800、质量提到 85（登记表里图是缩略展示）。
    """
    if not url:
        return url
    if "format/avif" in url:
        url = url.replace("format/avif", "format/jpeg")
    elif "imageView2" in url and "format/" not in url:
        url = url.rstrip("/") + "/format/jpeg"
    return url


# ---- 页面部分（需要真浏览器，实机验证）-------------------------------------

# 店铺名候选选择器：右上角店铺切换器。类名是构建期 hash，只能靠 data-testid / 语义定位，
# 全都可能失配——所以 detect_store 拿不到就返回空串，由调用方要求显式指定，绝不猜。
_STORE_SELECTORS = [
    '[data-testid="beast-core-dropdown"] [class*="mallName"]',
    '[class*="mallName"]',
    '[class*="shopName"]',
    'header [class*="mall"]',
]


# 页面自身的状态数据：window.rawData.store.authUser.mallList[].mallName。
# 优先于 DOM 选择器——实测店铺名挂在构建期 hash 类名（如 `_Wrz4-O9w`）上，改版即失效，
# 而这份 rawData 是商家后台自己渲染时注入的，跟着接口字段走，稳得多。
_STORE_JS = """
() => {
  try {
    const list = window.rawData?.store?.authUser?.mallList;
    if (!Array.isArray(list) || list.length !== 1) return '';
    return String(list[0]?.mallName || '');
  } catch (e) { return ''; }
}
"""


async def detect_store(page) -> str:
    """读当前登录店铺名：优先页面状态数据，回退 DOM 选择器；拿不到返回空串。

    为什么允许失败：店铺名决定订单落到哪张表，猜错就是把订单写进别人家的 Sheet——
    比不写更糟且要人工回滚。所以这里只做尝试，service 层在拿不到时要求显式传 store，
    宁可停下问，不做默认值。

    `mallList` 只有一个店时才认：多店账号无法从这份列表判断「当前是哪个」，那种情况交给
    DOM（页头显示的是当前店），DOM 也读不到就返回空串让调用方显式指定。
    """
    try:
        name = (await page.evaluate(_STORE_JS) or "").strip()
        if name and len(name) <= 40:
            logger.info(f"识别到当前店铺：{name}（来自页面 rawData）")
            return name
    except Exception as e:
        logger.warning(f"读 rawData 店铺名失败，回退 DOM：{e}")

    for sel in _STORE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            txt = (await loc.inner_text() or "").strip()
            # 取首行：切换器里常把店铺名和「切换店铺」等提示排在一起
            txt = txt.splitlines()[0].strip() if txt else ""
            if txt and len(txt) <= 40:
                logger.info(f"识别到当前店铺：{txt}（选择器 {sel}）")
                return txt
        except Exception as e:
            logger.warning(f"读店铺名失败（选择器 {sel}）：{e}")
    logger.warning("没能从页面识别店铺名——需要调用方显式指定 store")
    return ""


async def read_pagination(page) -> Dict[str, Any]:
    """读分页状态：{total, page, page_size, has_next}。

    total 取自「共有 N 条」；当前页/每页取自分页容器的
    `data-status="beast-core-pagination-{pageSize}-{page}"`。
    """
    ul = page.locator(_PAGINATION).first
    if await ul.count() == 0:
        raise RuntimeError("找不到分页容器，页面可能没加载完或改版")

    status = await ul.get_attribute("data-status") or ""
    m = re.search(r"beast-core-pagination-(\d+)-(\d+)", status)
    page_size = int(m.group(1)) if m else 0
    cur = int(m.group(2)) if m else 0

    total = 0
    tt = ul.locator(_TOTAL_TEXT).first
    if await tt.count():
        mt = _RE_TOTAL.search(await tt.inner_text() or "")
        if mt:
            total = int(mt.group(1).replace(",", ""))

    nxt = ul.locator(_PAGE_NEXT).first
    has_next = False
    if await nxt.count():
        cls = await nxt.get_attribute("class") or ""
        has_next = "PGT_disabled" not in cls

    return {"total": total, "page": cur, "page_size": page_size, "has_next": has_next}


async def selected_count(page) -> Optional[int]:
    """读「已选订单：N」计数；页面没这个文案时返回 None（不当失败）。"""
    try:
        body = await page.inner_text("body")
    except Exception as e:
        logger.warning(f"读已选计数失败：{e}")
        return None
    m = _RE_SELECTED.search(body or "")
    return int(m.group(1).replace(",", "")) if m else None


async def select_all_on_page(page) -> None:
    """勾选表头全选框 = 全选【当前页】（不是全部）。

    「跨页勾选」开关默认是开的，实测翻页后已勾选状态保留并累积（第1页勾20 → 翻到
    第2页再勾 → 累计40），所以逐页勾完再一次性导出可行。
    """
    box = page.locator(_HEAD_CHECKBOX).first
    if await box.count() == 0:
        raise RuntimeError("找不到表头全选框")
    if await box.is_checked():
        return

    # 点可见的 label（隐形 input 点不动，见 _HEAD_CHECKBOX_CLICK 注释）
    click_target = page.locator(_HEAD_CHECKBOX_CLICK).first
    if await click_target.count() == 0:
        click_target = box  # label 结构变了就退回直点 input，让报错停在原来的位置
    await click_target.click()
    await page.wait_for_timeout(400)

    # 校验勾上了：这一步失败就意味着后面导出的是空集或漏页，必须当场停下
    if not await box.is_checked():
        raise RuntimeError("点了全选框但状态没变成已勾选，页面可能改版")


async def goto_next_page(page, timeout_ms: int = 20000) -> bool:
    """点「下一页」并等页码真的变了。已在尾页返回 False。"""
    ul = page.locator(_PAGINATION).first
    before = await ul.get_attribute("data-status")
    nxt = ul.locator(_PAGE_NEXT).first
    if await nxt.count() == 0:
        return False
    if "PGT_disabled" in (await nxt.get_attribute("class") or ""):
        return False

    await nxt.click()
    waited = 0
    while waited < timeout_ms:
        await asyncio.sleep(0.25)
        waited += 250
        if await ul.get_attribute("data-status") != before:
            # 页码变了还要等表格行渲染出来，否则接着抓图会抓到上一页的残留
            try:
                await page.wait_for_selector("table tbody tr", timeout=10000)
            except Exception as e:
                logger.warning(f"翻页后等表格行超时：{e}")
            await page.wait_for_timeout(600)
            return True
    raise RuntimeError("点了下一页但分页状态没变，翻页失败")


# 逐行触发懒加载：表格在内部滚动容器里，window.scrollTo 对它无效（实测
# scrollHeight 只有 1059，滚不动），必须对每个 tr 调 scrollIntoView。
_SCROLL_ROWS_JS = r"""
async () => {
  const trs = Array.from(document.querySelectorAll('table tbody tr'));
  for (const tr of trs) {
    tr.scrollIntoView({block: 'center'});
    await new Promise(r => setTimeout(r, 260));
  }
  return trs.length;
}
"""

# 按【包含 img 且包含「子订单号：」的最小块】取 (子订单号 → 图 URL)。
# 为什么不按整行取：一行可含多个子订单，整行的 innerText 里第一个匹配到的往往是主订单号
# （去掉 PO- 前缀后长得跟子订单号一模一样），照抓必然错配。做法是从 img 往上找【第一个】
# 含「子订单号：」的祖先，并且要求该祖先只含【一个】子订单号——含多个说明已经爬到行级
# 容器、无法归属，宁可跳过也不错配。
_GRAB_IMAGES_JS = r"""
() => {
  const out = [];
  const skipped = [];
  const imgs = Array.from(document.querySelectorAll('table tbody img'));
  for (const img of imgs) {
    const src = img.getAttribute('src') || '';
    if (!src || src.startsWith('data:')) continue;
    let el = img, box = null, sub = null;
    for (let i = 0; i < 8; i++) {
      el = el.parentElement;
      if (!el) break;
      const t = el.innerText || '';
      const hits = t.match(/子订单号[:：]\s*[\d-]+/g);
      if (!hits || !hits.length) continue;
      if (hits.length === 1) {
        sub = hits[0].replace(/子订单号[:：]\s*/, '');
        box = el;
      }
      break;
    }
    if (!sub) { skipped.push(src.slice(0, 80)); continue; }
    const txt = box.innerText || '';
    let price = '';
    const pm = txt.match(/成交单价[:：]\s*([\d.]+)/);
    if (pm) {
      price = pm[1];
    } else {
      const infoEl = document.querySelector(`[id$="-${sub}-info"]`);
      if (infoEl) {
        const m2 = (infoEl.innerText || '').match(/([\d.]+)/);
        if (m2) price = m2[1];
      }
    }
    out.push({
      sub: sub,
      url: src,
      price: price,
      loaded: (img.className || '').includes('loaded') ||
              img.getAttribute('data-state') === 'succ',
    });
  }
  return {items: out, skipped: skipped};
}
"""


async def grab_page_images(page) -> Dict[str, Dict[str, str]]:
    """滚完当前页所有行触发懒加载，返回 {子订单号: {url, price, loaded}}。"""
    n_rows = await page.evaluate(_SCROLL_ROWS_JS)
    res = await page.evaluate(_GRAB_IMAGES_JS)
    items = res.get("items") or []
    skipped = res.get("skipped") or []
    if skipped:
        logger.warning(f"有 {len(skipped)} 张图归不到子订单（已跳过）：{skipped[:3]}")

    out: Dict[str, Dict[str, str]] = {}
    for it in items:
        sub = str(it.get("sub") or "").strip()
        if not sub:
            continue
        out[sub] = {
            "url": to_jpeg_url(str(it.get("url") or "")),
            "price": str(it.get("price") or ""),
            "loaded": bool(it.get("loaded")),
        }
    unloaded = [s for s, v in out.items() if not v["loaded"]]
    logger.info(
        f"本页 {n_rows} 行，抓到 {len(out)} 个子订单的图"
        + (f"，其中 {len(unloaded)} 张尚未加载完" if unloaded else "")
    )
    return out


async def sweep_pages(
    page,
    on_page: Optional[Callable[[dict], None]] = None,
    max_pages: int = 200,
) -> Dict[str, Any]:
    """逐页：触发懒加载抓图 → 全选本页 → 翻页。返回 {images, pages, total, selected, truncated}。

    勾选累积依赖页面的「跨页勾选」开关（默认开）。收尾会校验「已选订单」计数是否等于
    总条数，不等就抛异常交上层重试——少勾了就导不全，宁可整批重来。

    **但被 max_pages 截断时不校验**：那是「只跑前 N 页」的冒烟模式，已选本就少于总数，
    导出的也正是这批选中的单。否则只要 max_pages 小于实际页数就必然中止，这个参数等于死的。
    truncated=True 会一路传到汇总，让人一眼看出「这批不是全量」。
    """
    images: Dict[str, Dict[str, str]] = {}
    info = await read_pagination(page)
    total = info["total"]
    logger.info(f"待发货共 {total} 条，每页 {info['page_size']}，当前第 {info['page']} 页")

    pages = 0
    cur = info  # max_pages<=0 时循环体不执行，收尾仍要有分页快照可读
    while pages < max_pages:
        pages += 1
        cur = await read_pagination(page)
        page_imgs = await grab_page_images(page)
        images.update(page_imgs)
        await select_all_on_page(page)
        sel = await selected_count(page)
        if on_page:
            # 回调可能是协程函数（service 层的 _emit 是 async），返回值是 awaitable 就 await。
            # 不这么做的话进度事件会变成「创建了协程但从没 await」，SSE 里一条页进度都收不到。
            ret = on_page({
                "page": cur["page"],
                "pages_done": pages,
                "images": len(images),
                "selected": sel,
                "total": total,
            })
            if inspect.isawaitable(ret):
                await ret
        if not cur["has_next"]:
            break
        await goto_next_page(page)

    sel = await selected_count(page)
    # 还有下一页却已停下 = 被 max_pages 截断（正常翻完的出口是 has_next 为假）
    truncated = pages >= max_pages and bool(cur.get("has_next"))
    if truncated:
        logger.warning(
            f"只跑了前 {pages} 页（max_pages={max_pages}），已选 {sel}/{total} 条，"
            f"本批不是全量——跳过「已选==总数」校验"
        )
    elif total and sel is not None and sel != total:
        raise RuntimeError(f"已选 {sel} 条 != 总数 {total} 条，勾选不全，不做导出")

    return {"images": images, "pages": pages, "total": total, "selected": sel,
            "truncated": truncated}


async def trigger_export(page, out_dir: str, timeout_ms: int = 180000) -> str:
    """点「导出订单」→ 弹窗取消勾「收货信息」→ 「确认导出」→ 接住下载，返回落地路径。

    两个坑：
      - 未勾选订单时「导出订单」按钮是 disabled（class 带 BTN_disabled），点了毫无反应，
        很容易误判成流程走通。这里显式检查 disabled 并抛错。
      - **绝不能用 CDP `Page.setDownloadBehavior`**：它和 Playwright 的 expect_download
        抢同一个落盘路径，互相截断，产出 0 字节的「不是 zip 文件」。纯用 expect_download。
    """
    os.makedirs(out_dir, exist_ok=True)

    btn = page.locator('button:has-text("导出订单"), [class*="BTN_"]:has-text("导出订单")').first
    if await btn.count() == 0:
        raise RuntimeError("找不到「导出订单」按钮")
    cls = await btn.get_attribute("class") or ""
    if "BTN_disabled" in cls or await btn.is_disabled():
        raise RuntimeError("「导出订单」按钮是 disabled 状态——说明一条订单都没勾上")
    await btn.click()
    await page.wait_for_timeout(1200)

    # 弹窗里取消「收货信息」（买家 PII）。后台会记住上次设置，故先读状态再决定点不点。
    excluded = False
    label = page.locator(f'label:has-text("{_EXPORT_GROUP_EXCLUDE}")').first
    if await label.count():
        cb = label.locator('input[type="checkbox"]').first
        if await cb.count() and await cb.is_checked():
            await cb.click()
            await page.wait_for_timeout(300)
        excluded = await cb.count() > 0 and not await cb.is_checked()
    if not excluded:
        logger.warning(
            f"没能确认「{_EXPORT_GROUP_EXCLUDE}」已取消勾选——导出文件可能含买家隐私字段，"
            "落表前请人工核对导出列"
        )

    confirm = page.locator('button:has-text("确认导出"), [class*="BTN_"]:has-text("确认导出")').first
    if await confirm.count() == 0:
        raise RuntimeError("找不到「确认导出」按钮（导出字段设置弹窗没弹出？）")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(out_dir, f"订单导出_{ts}.xlsx")
    async with page.expect_download(timeout=timeout_ms) as dl_info:
        await confirm.click()
    download = await dl_info.value
    await download.save_as(dst)

    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    if size < 1024:
        raise RuntimeError(f"导出文件异常小（{size} 字节）：{dst}")
    logger.info(f"导出文件已落地：{dst}（{size} 字节）")
    return dst


def join_images(orders: List[OrderRow], images: Dict[str, Dict[str, str]]) -> Dict[str, int]:
    """按子订单号把 DOM 抓到的图 URL / 成交单价 join 到订单行上。返回命中统计。"""
    hit_img = hit_price = 0
    for o in orders:
        got = images.get(o.sub_order_no)
        if not got:
            continue
        if got.get("url"):
            o.image_url = got["url"]
            hit_img += 1
        if got.get("price"):
            o.deal_price = got["price"]
            hit_price += 1
    miss = [o.sub_order_no for o in orders if not o.image_url]
    if miss:
        logger.warning(f"{len(miss)} 条子订单没匹配到图：{miss[:5]}")
    return {"image": hit_img, "price": hit_price, "miss": len(miss)}


def download_images(orders: List[OrderRow], out_dir: str) -> Dict[str, int]:
    """下载主图到本地。单张失败只记 warning——图是辅助字段，不该拖垮整批入库。

    下载必须带浏览器头 + 重试（复用采集管线的 _download_main_image）：裸 requests 会被
    kwcdn 当 bot 拦，表现为连接重置（WinError 10054）或 403。
    """
    os.makedirs(out_dir, exist_ok=True)
    ok = fail = 0
    for o in orders:
        if not o.image_url:
            continue
        name = f"{o.sub_order_no or o.order_no}.jpg"
        dst = os.path.join(out_dir, name)
        try:
            if not os.path.exists(dst):
                _download_main_image(o.image_url, dst)
            o.image_path = dst
            ok += 1
        except Exception as e:
            logger.warning(f"子订单 {o.sub_order_no} 主图下载失败（该行不带图入库）：{e}")
            fail += 1
    return {"ok": ok, "fail": fail}
