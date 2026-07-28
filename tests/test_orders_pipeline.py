# -*- coding: utf-8 -*-
"""订单登记管线【离线部分】单测：解析官方导出 xlsx、站点归一化、字段/判重列映射。

页面部分（翻页勾选、触发导出、抓图）不在此覆盖——那要真浏览器，靠实机 dry-run 验证。
这里钉死的是「导出文件 → 登记表行」这段纯数据变换，它决定会往 102MB 的表里写什么。

样本文件 workspace/orders_sample/sample_export.xlsx 是 gitignored 的实机导出（22 行 /
20 个订单），缺失时相关用例 skip，不让 CI 因为没样本就红。
"""
import asyncio
from pathlib import Path

import pytest

from app.orders import pipeline as P
from app.orders.pipeline import OrderRow

SAMPLE = Path("workspace/orders_sample/sample_export.xlsx")
needs_sample = pytest.mark.skipif(not SAMPLE.exists(), reason="缺实机导出样本")

# StoreA全球1 的真实表头（实测），列序刻意不连续：订单号在 C 而非 A
STORE_A_HEADER = {
    "A": "订单店铺", "B": "站点区分", "C": "订单号", "D": "尺码",
    "E": "平台物流跟踪号", "F": "国内发出时间", "G": "产品图片", "H": "产品图2",
    "I": "平台创建时间", "J": "采购日期", "K": "采购费用", "L": "平台成交价",
    "M": "Y2头程费用", "N": "采购订单号", "O": "物流情况",
}


def _order(**kw) -> OrderRow:
    base = dict(
        order_no="PO-045-119", site="哥伦比亚", sub_order_no="045-118",
        attrs="杏色 / 3-4Y", created_at="2026-07-27 10:16:38",
    )
    base.update(kw)
    return OrderRow(**base)


# ---- 站点归一化 / 空值 ------------------------------------------------------


def test_normalize_site_strips_trailing_zhan():
    """导出的「哥伦比亚站」要归一成表内主流写法「哥伦比亚」。"""
    assert P.normalize_site("哥伦比亚站") == "哥伦比亚"
    assert P.normalize_site("美国") == "美国"
    assert P.normalize_site(" 秘鲁站 ") == "秘鲁"
    # 单字「站」不该被吃成空串
    assert P.normalize_site("站") == "站"
    # `--` 是导出文件的空占位
    assert P.normalize_site("--") == ""
    assert P.normalize_site(None) == ""


def test_clean_normalizes_placeholder_empties():
    assert P._clean("--") == ""
    assert P._clean("—") == ""
    assert P._clean(" 杏色 / 3-4Y ") == "杏色 / 3-4Y"
    assert P._clean(0) == "0", "数字 0 是有效值，不能当空"


def test_to_jpeg_url():
    """Excel/WPS 不认 avif，嵌进去是空白格。"""
    assert P.to_jpeg_url(
        "https://img.kwcdn.com/a-goods.jpeg?imageView2/2/w/800/q/70/format/avif"
    ).endswith("format/jpeg")
    # 没带 format 参数的 imageView2 URL 要补上
    assert P.to_jpeg_url("https://img.kwcdn.com/a-goods.jpeg?imageView2/2/w/800").endswith(
        "/format/jpeg"
    )
    # 非 imageView2 的 URL 原样不动，别乱拼参数
    plain = "https://img.kwcdn.com/a-goods.jpeg"
    assert P.to_jpeg_url(plain) == plain
    assert P.to_jpeg_url("") == ""


# ---- 解析导出 xlsx ---------------------------------------------------------


@needs_sample
def test_parse_export_sample():
    """实机样本：22 行子订单级记录，字段对得上、`--` 已归一化为空。"""
    rows = P.parse_export_xlsx(str(SAMPLE))

    assert len(rows) == 22
    # 按子订单展开：20 个订单 → 22 行，说明有订单含多个子订单
    assert len({r.order_no for r in rows}) == 20

    # 不钉死具体单号：真实单号属业务数据，不入库。改为按不变量断言——
    # 取任一哥伦比亚单，校验字段形态而非字面值。
    o = next(r for r in rows if r.site == "哥伦比亚")
    assert not o.site.endswith("站"), "站点必须已去掉尾字「站」"
    assert o.status == "待发货"
    assert o.order_no.startswith("PO-") and o.sub_order_no
    assert o.attrs, "商品属性非空（形如「颜色 / 尺码」，也有仅款式无尺码的）"
    assert o.created_at.startswith("2026-") and len(o.created_at) == 19
    assert o.spu_id.isdigit()
    # 待发货态这些列在导出里全是 `--`
    assert (o.tracking_no, o.carrier, o.warehouse, o.shipped_at) == ("", "", "", "")
    # DOM 才有的字段，解析阶段还是空
    assert (o.image_url, o.deal_price, o.image_path) == ("", "", "")

    assert all(r.order_no and r.sub_order_no for r in rows), "订单号/子订单号不得为空"


def test_parse_export_rejects_missing_columns(tmp_path):
    """表头缺关键列直接抛错——字段对不上就别往登记表写。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["订单号", "站点", "订单状态"])  # 缺 子订单号/商品属性/订单创建时间
    ws.append(["PO-1", "美国站", "待发货"])
    p = tmp_path / "bad.xlsx"
    wb.save(p)

    with pytest.raises(ValueError, match="缺列"):
        P.parse_export_xlsx(str(p))


def test_parse_export_is_column_order_agnostic(tmp_path):
    """按表头标题取列，不认列序：导出字段设置一改列序就会变。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["商品属性", "订单创建时间", "子订单号", "站点", "订单号", "运单号"])
    ws.append(["粉色 / 5-6Y", "2026-07-27 09:25:52", "045-999", "秘鲁站", "PO-159-1", "--"])
    ws.append([None, None, None, None, None, None])  # 全空行要被跳过
    p = tmp_path / "reordered.xlsx"
    wb.save(p)

    rows = P.parse_export_xlsx(str(p))
    assert len(rows) == 1
    assert (rows[0].order_no, rows[0].site, rows[0].attrs) == ("PO-159-1", "秘鲁", "粉色 / 5-6Y")
    assert rows[0].tracking_no == ""


# ---- 列映射 / 判重键 -------------------------------------------------------


def test_build_row_values_uses_real_header():
    """按真实表头生成 {列字母: 值}；人工填的列一律不碰。"""
    o = _order(tracking_no="", deal_price="63.99")
    values = P.build_row_values(o, STORE_A_HEADER, store_value="StoreA全球")

    assert values["A"] == "StoreA全球"      # 配置常量，≠ 登录店铺名
    assert values["B"] == "哥伦比亚"
    assert values["C"] == "PO-045-119"
    assert values["D"] == "杏色 / 3-4Y"     # 商品属性 → 尺码
    assert values["E"] == ""                # 待发货无运单号
    assert values["I"] == "2026-07-27 10:16:38"
    assert values["L"] == "63.99"           # 成交价 best-effort
    # 人工后续填的列：管线不得写入，连空串都不占
    for col in ("F", "J", "K", "M", "N", "O"):
        assert col not in values, f"{col}({STORE_A_HEADER[col]}) 应由人工填，管线不该碰"
    # 图片列不走 values（走 DISPIMG 嵌图）
    assert "G" not in values and "H" not in values


def test_image_column_prefers_exact_title():
    """`StoreA全球1` 同时有「产品图片」和「产品图2」，只能写前者。"""
    assert P.image_column(STORE_A_HEADER) == "G"
    # 只有「产品图2」这类变体时退回首个含「产品图」的列
    assert P.image_column({"C": "订单号", "E": "产品图2"}) == "E"
    assert P.image_column({"A": "订单号"}) is None


def test_resolve_title_column_handles_alias_and_order():
    """字段有多种标题写法（` StoreD` 表用「站点」而非「站点区分」）。"""
    assert P.resolve_title_column(STORE_A_HEADER, {"站点区分", "站点"}) == "B"
    assert P.resolve_title_column({"B": "站点", "C": "订单号"}, {"站点区分", "站点"}) == "B"
    assert P.resolve_title_column(STORE_A_HEADER, "不存在的列") is None
    # 多列命中时取列序最靠前的那个（AA 排在 B 之后，不能按字符串比较）
    assert P.resolve_title_column({"AA": "订单号", "B": "订单号"}, "订单号") == "B"


def test_dedupe_key_matches_workbook_reading():
    """判重键 = (订单号, 尺码)，与 existing_key_tuples 读表口径一致（strip 后比较）。"""
    o = _order(attrs=" 杏色 / 3-4Y ")
    key = P.dedupe_key(o, STORE_A_HEADER, ["订单号", "尺码"], "StoreA全球")
    assert key == ("PO-045-119", "杏色 / 3-4Y")

    # 同订单不同尺码必须是两个键，否则同订单的其余子订单会被当重复丢掉
    k2 = P.dedupe_key(_order(attrs="杏色 / 9-12M"), STORE_A_HEADER, ["订单号", "尺码"], "StoreA全球")
    assert k2 != key


def test_dedupe_key_returns_none_when_column_absent():
    """判重列在该 Sheet 找不到 → None，调用方据此跳过整个 Sheet，而不是硬写重复行。"""
    header = {"A": "订单店铺", "B": "订单号"}  # 没有「尺码」列
    assert P.dedupe_key(_order(), header, ["订单号", "尺码"], "StoreA") is None


# ---- Sheet 映射 ------------------------------------------------------------


SHEET_MAP = [
    {"store": "StoreA", "sites": ["哥伦比亚", "秘鲁"], "sheet": "StoreA全球1",
     "store_value": "StoreA全球"},
    {"store": "StoreC", "sites": ["美国"], "sheet": "StoreC美国",
     "store_value": "StoreC"},
]


def test_resolve_sheet_matches_store_and_site():
    got = P.resolve_sheet("StoreA", "哥伦比亚站", SHEET_MAP)
    assert got and got["sheet"] == "StoreA全球1" and got["store_value"] == "StoreA全球"
    # 店铺名带后缀也要认（页面右上角可能显示 "StoreA Store"）
    assert P.resolve_sheet("StoreA Store", "秘鲁", SHEET_MAP)["sheet"] == "StoreA全球1"
    assert P.resolve_sheet("StoreC", "美国", SHEET_MAP)["sheet"] == "StoreC美国"


def test_resolve_sheet_returns_none_when_unmapped():
    """未命中映射必须返回 None（调用方跳过并报告），绝不臆测落点。

    StoreA + 美国 就是典型陷阱：`StoreA牛仔裤` 虽是 StoreA 的美国单，但列结构完全不同
    （有 Y1/Y2、XM、货号），不能想当然当作 StoreA 美国站的落点。
    """
    assert P.resolve_sheet("StoreA", "美国", SHEET_MAP) is None
    assert P.resolve_sheet("陌生店铺", "哥伦比亚", SHEET_MAP) is None
    assert P.resolve_sheet("StoreA", "哥伦比亚", []) is None


# ---- join 图片 / 成交价 ----------------------------------------------------


def test_join_images_by_sub_order():
    """图与成交价按子订单号 join；缺图只统计不报错（图是辅助字段）。"""
    a = _order(sub_order_no="045-1")
    b = _order(sub_order_no="045-2")
    c = _order(sub_order_no="045-3")
    images = {
        "045-1": {"url": "https://img.kwcdn.com/1.jpeg", "price": "63.99", "loaded": True},
        "045-2": {"url": "https://img.kwcdn.com/2.jpeg", "price": "", "loaded": True},
    }

    stat = P.join_images([a, b, c], images)

    assert (a.image_url, a.deal_price) == ("https://img.kwcdn.com/1.jpeg", "63.99")
    assert (b.image_url, b.deal_price) == ("https://img.kwcdn.com/2.jpeg", "")
    assert (c.image_url, c.deal_price) == ("", ""), "没抓到图的行保持空，不得串到别人的图"
    assert stat == {"image": 2, "price": 1, "miss": 1}


def test_download_images_tolerates_single_failure(tmp_path, monkeypatch):
    """单张图下载失败只记 warning，该行不带图入库，不拖垮整批。"""
    calls = []

    def fake_dl(url, dst, *a, **kw):
        calls.append(url)
        if "bad" in url:
            raise RuntimeError("connection reset")
        Path(dst).write_bytes(b"\xff\xd8jpg")

    monkeypatch.setattr(P, "_download_main_image", fake_dl)
    ok_row = _order(sub_order_no="045-1", image_url="https://x/good.jpeg")
    bad_row = _order(sub_order_no="045-2", image_url="https://x/bad.jpeg")
    no_img = _order(sub_order_no="045-3")

    stat = P.download_images([ok_row, bad_row, no_img], str(tmp_path / "img"))

    assert stat == {"ok": 1, "fail": 1}
    assert ok_row.image_path and Path(ok_row.image_path).exists()
    assert bad_row.image_path == "" and no_img.image_path == ""
    assert len(calls) == 2, "没有 image_url 的行不该触发下载"


class _FakePage:
    """最小页面替身：只实现 detect_store 用到的 evaluate/locator。"""

    def __init__(self, raw="", dom=None, boom=False):
        self._raw, self._dom, self._boom = raw, dom or {}, boom

    async def evaluate(self, js, *a):
        if self._boom:
            raise RuntimeError("页面已关闭")
        return self._raw

    def locator(self, sel):
        page = self

        class _Loc:
            first = property(lambda s: s)

            async def count(s):
                return 1 if sel in page._dom else 0

            async def inner_text(s):
                return page._dom.get(sel, "")

        return _Loc()


def test_detect_store_prefers_page_state():
    """优先读 rawData：店铺名挂在构建期 hash 类名上，DOM 选择器改版即失效。"""
    page = _FakePage(raw="StoreA", dom={'[class*="mallName"]': "错的名字"})

    assert asyncio.run(P.detect_store(page)) == "StoreA"


def test_detect_store_falls_back_to_dom_when_state_empty():
    """rawData 拿不到（多店账号或字段改名）时回退 DOM，取首行。"""
    page = _FakePage(raw="", dom={'[class*="mallName"]': "StoreB\n切换店铺"})

    assert asyncio.run(P.detect_store(page)) == "StoreB"


def test_detect_store_returns_empty_rather_than_guessing():
    """两条路都读不到就返回空串——service 会据此中止并要求显式指定，绝不猜落点。"""
    assert asyncio.run(P.detect_store(_FakePage(boom=True))) == ""


class _CheckboxPage:
    """页面替身：模拟 beast-core 的「隐形 input + 可见 label」结构。

    真实结构是 <input> 0×0 opacity:0（点它必然 30s 超时报 not visible），外层 label
    才可点。这里让点 input 直接抛错，钉住「必须点 label」。
    """

    def __init__(self, has_label=True, click_works=True):
        self.has_label, self.click_works = has_label, click_works
        self.checked = False
        self.clicked: list = []

    def locator(self, sel):
        page = self
        is_label = "label" in sel

        class _Loc:
            first = property(lambda s: s)

            async def count(s):
                return 1 if (is_label and page.has_label) or not is_label else 0

            async def is_checked(s):
                return page.checked

            async def click(s):
                if not is_label:
                    raise AssertionError("点了隐形 input——它 0×0 opacity:0，实机必超时")
                page.clicked.append(sel)
                if page.click_works:
                    page.checked = True

        return _Loc()

    async def wait_for_timeout(self, ms):
        return None


def test_select_all_clicks_visible_label_not_hidden_input():
    """必须点可见的 label：隐形 input 点不动，实机上就是这里卡了 30s 超时。"""
    page = _CheckboxPage()

    asyncio.run(P.select_all_on_page(page))

    assert page.checked is True
    assert "label" in page.clicked[0]


def test_select_all_verifies_state_changed():
    """点了但没勾上要当场抛：否则后面导出的是空集或漏页，静默失败最糟。"""
    page = _CheckboxPage(click_works=False)

    with pytest.raises(RuntimeError, match="状态没变成已勾选"):
        asyncio.run(P.select_all_on_page(page))


def test_select_all_is_idempotent():
    """已勾选就别再点——再点一次会取消全选。"""
    page = _CheckboxPage()
    page.checked = True

    asyncio.run(P.select_all_on_page(page))

    assert page.clicked == []


def test_sweep_pages_awaits_async_on_page(monkeypatch):
    """on_page 是协程函数时必须被 await。

    service 层的进度回调是 async（要往 SSE 队列 put），早先这里直接同步调用，协程建了
    却没 await，结果 UI 上一条页进度都收不到——只在真机跑时才暴露，所以补这条守住。
    """
    import asyncio

    pages = [
        {"total": 4, "page": 1, "page_size": 2, "has_next": True},
        {"total": 4, "page": 2, "page_size": 2, "has_next": False},
    ]
    seq = iter(pages)
    cur = {"v": pages[0]}

    async def fake_read(page):
        return cur["v"]

    async def fake_next(page):
        cur["v"] = next(seq_after)

    seq_after = iter(pages[1:])
    async def fake_grab(page):
        return {f"045-{cur['v']['page']}": {"url": "u", "price": "1"}}

    async def fake_select(page):
        return None

    async def fake_count(page):
        return 2 * cur["v"]["page"]

    monkeypatch.setattr(P, "read_pagination", fake_read)
    monkeypatch.setattr(P, "grab_page_images", fake_grab)
    monkeypatch.setattr(P, "select_all_on_page", fake_select)
    monkeypatch.setattr(P, "selected_count", fake_count)
    monkeypatch.setattr(P, "goto_next_page", fake_next)

    got = []

    async def on_page(info):        # 协程回调
        got.append(info)

    res = asyncio.run(P.sweep_pages(object(), on_page=on_page))

    assert [i["page"] for i in got] == [1, 2], "两页都要推进度事件"
    assert got[-1]["selected"] == 4 and res["pages"] == 2
    assert len(res["images"]) == 2
    assert res["truncated"] is False, "正常翻完不算截断"


def _patch_sweep(monkeypatch, total=100, page_size=20):
    """给 sweep_pages 造一个「永远还有下一页」的分页，用来验 max_pages 截断。"""
    cur = {"page": 1}

    async def fake_read(page):
        return {"total": total, "page": cur["page"], "page_size": page_size,
                "has_next": cur["page"] * page_size < total}

    async def fake_next(page):
        cur["page"] += 1

    async def fake_grab(page):
        return {f"045-{cur['page']}": {"url": "u", "price": "1"}}

    # 已勾选数按「勾过几页」算，不能按当前页号——goto_next_page 会先把页号加上去，
    # 用页号会多算一页（真实语义是勾过的页数 × 每页条数）。
    picked = {"pages": 0}

    async def fake_select(page):
        picked["pages"] += 1

    async def fake_count(page):
        return page_size * picked["pages"]

    monkeypatch.setattr(P, "read_pagination", fake_read)
    monkeypatch.setattr(P, "grab_page_images", fake_grab)
    monkeypatch.setattr(P, "select_all_on_page", fake_select)
    monkeypatch.setattr(P, "selected_count", fake_count)
    monkeypatch.setattr(P, "goto_next_page", fake_next)


async def _none():
    return None


def test_sweep_pages_truncated_skips_total_check(monkeypatch):
    """max_pages 截断时不校验「已选==总数」。

    否则只要 max_pages 小于实际页数就必然抛「勾选不全」，这个冒烟参数等于死的——
    实机就是这么撞上的（248 条 5 页，--max-pages 2 直接中止）。
    """
    _patch_sweep(monkeypatch, total=100)

    res = asyncio.run(P.sweep_pages(object(), max_pages=2))

    assert res["pages"] == 2 and res["selected"] == 40
    assert res["truncated"] is True, "截断标记要传出去，别让人误读成全量"


def test_sweep_pages_still_guards_incomplete_selection(monkeypatch):
    """没被截断却已选 != 总数 → 仍要抛：少勾了就导不全，宁可整批重来。"""
    _patch_sweep(monkeypatch, total=100)

    async def short_count(page):
        return 37        # 翻完了却只勾到 37 条

    monkeypatch.setattr(P, "selected_count", short_count)

    with pytest.raises(RuntimeError, match="勾选不全"):
        asyncio.run(P.sweep_pages(object(), max_pages=5))


def test_sweep_pages_handles_zero_max_pages(monkeypatch):
    """max_pages<=0 时循环体不执行，收尾也不能 NameError。"""
    _patch_sweep(monkeypatch, total=100)

    res = asyncio.run(P.sweep_pages(object(), max_pages=0))

    assert res["pages"] == 0 and res["images"] == {}
