# -*- coding: utf-8 -*-
"""订单登记编排层单测（离线）：分表计划、判重、dry-run 门控、护栏中止、汇总契约。

不碰真浏览器也不碰真登记表：collect_orders 被 patch 成 fake，登记表用合成的最小
WPS 工作簿（复用 test_wps_excel_batch 的构造思路，走真实 append_rows 落盘）。

重点钉死三件事：
  1. dry_run=True 绝对不写文件——写登记表不可逆，方案要求人工确认后才开写。
  2. 未命中 sheet_map / 判重列缺失的，一律跳过并进汇总，绝不臆测落点。
  3. 判重同时防「已入库」和「同批内重复」。
"""
import asyncio
import re
import zipfile

import pytest

from app.orders import pipeline as P
from app.orders import service as S
from app.orders.pipeline import OrderRow
from app.tool import wps_excel_tool as wet
from app.tool.wps_excel_tool import WpsExcelTool

SHEET_MAP = [
    {"store": "StoreA", "sites": ["哥伦比亚", "秘鲁"], "sheet": "StoreA全球1",
     "store_value": "StoreA全球"},
]

# 表头在第 1 行，订单号在 C（刻意不从 A 开始），G 是图片列；row 2 已有一条入库数据
SHEET_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<dimension ref="A1:I1048576"/><sheetData>'
    '<row r="1"><c r="A1" s="1" t="str"><v>订单店铺</v></c>'
    '<c r="B1" s="1" t="str"><v>站点区分</v></c>'
    '<c r="C1" s="1" t="str"><v>订单号</v></c>'
    '<c r="D1" s="1" t="str"><v>尺码</v></c>'
    '<c r="E1" s="1" t="str"><v>平台物流跟踪号</v></c>'
    '<c r="F1" s="1" t="str"><v>国内发出时间</v></c>'
    '<c r="G1" s="1" t="str"><v>产品图片</v></c>'
    '<c r="H1" s="1" t="str"><v>平台创建时间</v></c>'
    '<c r="I1" s="1" t="str"><v>平台成交价</v></c></row>'
    '<row r="2" ht="41" customHeight="1">'
    '<c r="A2" s="2" t="str"><v>StoreA全球</v></c>'
    '<c r="C2" s="2" t="str"><v>PO-045-已入库</v></c>'
    '<c r="D2" s="2" t="str"><v>杏色 / 3-4Y</v></c></row>'
    '<row r="3" ht="41" customHeight="1"/>'
    "</sheetData></worksheet>"
)

STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"/>'
    "</cellXfs></styleSheet>"
)


@pytest.fixture
def workbook(tmp_path, monkeypatch):
    """合成最小登记表；备份目录改到 tmp，不污染桌面输出目录。"""
    path = tmp_path / "登记表.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="jpeg" ContentType="image/jpeg"/></Types>',
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
            '/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="StoreA全球1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument'
            '/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        zf.writestr("xl/worksheets/sheet1.xml", SHEET_XML)
        zf.writestr("xl/styles.xml", STYLES)

    bk = tmp_path / "backup"
    bk.mkdir()
    monkeypatch.setattr(wet, "get_output_dir", lambda *a, **k: bk)
    return path


def _order(no: str, size: str, **kw) -> OrderRow:
    # deal_price 默认给值＝正常态（页面已回填成交单价）。默认口径已允许空价，
    # 空价路径由 test_*_deal_price / 严格模式用例专门覆盖。
    base = dict(
        order_no=no, site="哥伦比亚", sub_order_no=f"sub-{no}-{size}",
        attrs=size, created_at="2026-07-27 10:16:38", deal_price="71.16",
    )
    base.update(kw)
    return OrderRow(**base)


# ---- 配置载入 --------------------------------------------------------------


def test_load_orders_config_falls_back_to_example():
    """config.toml 缺 [orders] 段时必须回退到随仓库分发的 example，否则功能全死。

    只断言「拿得到完整形状的配置」，不断言具体店铺/Sheet 名——那取决于本机
    config.toml 填了什么（仓库里的 example 是脱敏占位值），钉字面值会因人而异地红。
    """
    cfg = S.load_orders_config()

    assert cfg.get("workbook") and cfg.get("list_url")
    assert cfg.get("dedupe_by") == ["订单号", "尺码"]
    assert isinstance(cfg.get("sheet_map"), list) and cfg["sheet_map"]
    first = cfg["sheet_map"][0]
    assert first.get("store") and first.get("sheet") and first.get("sites")


# ---- 计划与判重 ------------------------------------------------------------


def test_plan_writes_groups_dedupes_and_maps(workbook):
    """分表 + 判重（已入库 / 同批重复）+ 未映射跳过，一次覆盖。"""
    orders = [
        _order("PO-045-已入库", "杏色 / 3-4Y"),          # 已在表里 → dup
        _order("PO-045-新1", "杏色 / 3-4Y", image_path=""),
        _order("PO-045-新1", "杏色 / 9-12M"),            # 同订单不同尺码 → 两条都要写
        _order("PO-045-新1", "杏色 / 9-12M"),            # 同批完全重复 → dup
        _order("PO-211-美国", "32", site="美国"),         # StoreA+美国未配置 → unmapped
    ]

    plans, unmapped, _ = S.plan_writes(orders, str(workbook), "StoreA", SHEET_MAP)

    assert set(plans) == {"StoreA全球1"}
    plan = plans["StoreA全球1"]
    assert len(plan.rows) == 2, "只应排入两条新纪录"
    assert plan.dup == 2, "已入库 1 + 同批重复 1"
    assert plan.no_key is False
    assert plan.header_row == 1 and plan.image_col == "G"
    assert plan.store_value == "StoreA全球"

    # 值按真实表头列字母落位，人工填的列不碰
    v = plan.rows[0]["values"]
    assert v["A"] == "StoreA全球" and v["C"] == "PO-045-新1" and v["D"] == "杏色 / 3-4Y"
    assert v["H"] == "2026-07-27 10:16:38"
    assert "F" not in v, "国内发出时间是人工填的列"
    # 没有本地图 → 不带 image 参数
    assert "image_path" not in plan.rows[0]

    assert len(unmapped) == 1
    assert unmapped[0]["order_no"] == "PO-211-美国" and "未配置" in unmapped[0]["reason"]


def test_plan_writes_marks_no_key_when_dedupe_column_missing(workbook, monkeypatch):
    """判重列缺失 → no_key 且不排任何行：没法判重就意味着重复跑会堆重复行。"""
    plans, _, _ = S.plan_writes(
        [_order("PO-1", "32")], str(workbook), "StoreA", SHEET_MAP,
        dedupe_by=["订单号", "根本不存在的列"],
    )

    plan = plans["StoreA全球1"]
    assert plan.no_key is True
    assert plan.rows == []


def test_plan_writes_attaches_image_only_when_downloaded(workbook, tmp_path):
    """只有真下到本地图才带 image_column；没图的行照常入库（图是辅助字段）。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"\xff\xd8jpg")
    orders = [
        _order("PO-有图", "32", image_path=str(img)),
        _order("PO-无图", "34"),
    ]

    plans, _, _ = S.plan_writes(orders, str(workbook), "StoreA", SHEET_MAP)
    rows = plans["StoreA全球1"].rows

    assert rows[0]["image_column"] == "G" and rows[0]["image_path"] == str(img)
    assert "image_column" not in rows[1]


# ---- dry-run 门控与写入 ----------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_output(tmp_path, monkeypatch):
    """把 service 的输出目录劫到 tmp。

    autouse 是刻意的：dry-run 会往 get_output_dir("orders_plan") 落「待写计划」CSV，
    不隔离就会把测试数据写进用户桌面的真实输出目录（已实际漏过一次）。
    """
    out = tmp_path / "输出"
    out.mkdir()
    monkeypatch.setattr(S, "get_output_dir", lambda kind="": out)


def _patch_collect(monkeypatch, orders, store="StoreA"):
    """把 run_orders_batch 的外部依赖全换成假的，并钉死配置。

    配置也要 patch：否则 run_orders_batch 会去读本机 config.toml 的 [orders]，
    用例结果就随开发机填了什么而变（脱敏后仓库里的 example 是占位映射，更对不上）。
    需要自定义映射的用例自己再 patch 一次覆盖即可。
    """
    seen_watermark: dict = {}

    async def fake_collect(list_url, store="", on_progress=None, max_pages=200,
                           known_order_nos=None):
        # 记下 service 算出来的水位，供增量用例断言（单表模式才该非空）
        seen_watermark["known"] = set(known_order_nos or ())
        return {
            "orders": orders, "store": store or "StoreA",
            "export_file": "X.xlsx", "total": len(orders), "pages": 1,
            "join": {"image": 0, "price": 0, "miss": len(orders)},
        }

    monkeypatch.setattr(S, "load_orders_config", lambda: {
        "list_url": "https://x", "dedupe_by": ["订单号", "尺码"],
        "sheet_map": SHEET_MAP,
    })
    monkeypatch.setattr(S, "collect_orders", fake_collect)
    monkeypatch.setattr(S, "ensure_cdp_alive", lambda *a, **k: _true())
    monkeypatch.setattr(P, "download_images", lambda o, d: {"ok": 0, "fail": 0})
    return seen_watermark


async def _true():
    return True


def _run(**kw) -> dict:
    events: list = []

    async def on_progress(ev):
        events.append(ev)

    res = asyncio.run(S.run_orders_batch(on_progress=on_progress, **kw))
    res["_events"] = events
    return res


def test_dry_run_does_not_touch_workbook(workbook, monkeypatch):
    """dry_run=True 走完全部只读步骤，但绝不写文件——写登记表不可逆。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32"), _order("PO-新B", "34")])
    before = workbook.read_bytes()

    res = _run(dry_run=True, workbook=str(workbook), list_url="https://x")

    assert workbook.read_bytes() == before, "dry-run 不得改动登记表一个字节"
    assert res["dry_run"] is True
    assert res["pending"] == 2 and res["written_rows"] == 0
    assert res["written"]["StoreA全球1"] == {"dry_run": True, "would_write": 2}
    # 给人核对的预览要带真实表头标题
    assert "订单号" in res["sheets"]["StoreA全球1"]["preview"][0]
    types = [e["type"] for e in res["_events"]]
    assert "writing" not in types and "written" not in types
    assert types[0] == "started" and types[-1] == "done"


def test_real_run_writes_rows(workbook, monkeypatch):
    """dry_run=False 才落盘，行数与判重口径一致，且能被再次读出来。"""
    _patch_collect(monkeypatch, [
        _order("PO-045-已入库", "杏色 / 3-4Y"),   # 判重跳过
        _order("PO-新A", "32"),
        _order("PO-新B", "34"),
    ])

    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    assert res["written_rows"] == 2 and res["dup_skipped"] == 1
    assert res["failed_sheets"] == []
    assert res["purchase"]["rows"] == 2
    from app.tool.wps_excel_tool import WpsExcelTool

    keys = WpsExcelTool.existing_key_tuples(str(workbook), "StoreA全球1", ["C", "D"])
    assert ("PO-新A", "32") in keys and ("PO-新B", "34") in keys
    assert ("PO-045-已入库", "杏色 / 3-4Y") in keys

    # 重复跑一次：全部判重跳过，不再新增
    res2 = _run(dry_run=False, workbook=str(workbook), list_url="https://x")
    assert res2["written_rows"] == 0 and res2["dup_skipped"] == 3
    assert res2["purchase"]["rows"] == 0


# ---- 「数量」列：表里本来没有，写入模式下自动插到「尺码」右侧 ---------------


def test_write_mode_inserts_quantity_column_and_fills_it(workbook, monkeypatch):
    """写入模式：先给目标表插「数量」列，再把应履约件数写进去（数字，能求和）。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32", qty="2")])

    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    assert res["written_rows"] == 1
    header = WpsExcelTool.read_header(str(workbook), "StoreA全球1")
    assert header["E"] == "数量", "插在「尺码」(D) 右侧"
    assert header["H"] == "产品图片", "原 G 起的列整体右移"
    row = WpsExcelTool.read_row_by_key(
        str(workbook), "StoreA全球1", key="PO-新A", cols={"qty": "E"}, key_col="C",
    )
    assert row["qty"] == "2"
    # 必须是数值单元格（没有 t="str"）——这一列要能直接求和、筛出多件单
    with zipfile.ZipFile(workbook) as zf:
        xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert re.search(r'<c r="E[2-9]\d*"[^>]*><v>2</v></c>', xml)
    assert not re.search(r'<c r="E[2-9]\d*"[^>]*t="str"', xml), "数量格不能是文本"
    # 事件里要报出来，插列前的备份路径也要给（改结构不可逆）
    qty_ev = [e for e in res["_events"] if e["type"] == "qty_column"]
    assert qty_ev and qty_ev[0]["sheets"][0]["column"] == "E"
    assert qty_ev[0]["sheets"][0]["backup"]


def test_quantity_column_insert_is_idempotent_across_batches(workbook, monkeypatch):
    """连跑两批不能插出第二列「数量」，判重也不能因为多了一列而失效。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32", qty="2")])
    _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    _patch_collect(monkeypatch, [_order("PO-新A", "32", qty="2"),
                                 _order("PO-新B", "34", qty="1")])
    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    header = WpsExcelTool.read_header(str(workbook), "StoreA全球1")
    assert [t for t in header.values() if t == "数量"] == ["数量"]
    assert res["written_rows"] == 1 and res["dup_skipped"] == 1, "首批那条要判成已入库"
    assert not [e for e in res["_events"] if e["type"] == "qty_column"]


def test_dry_run_shows_quantity_without_touching_structure(workbook, monkeypatch):
    """dry-run 只读：不插列，但预览/CSV 里要带虚拟「数量*」列供核对多件单。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32", qty="3")])
    before = workbook.read_bytes()

    res = _run(dry_run=True, workbook=str(workbook), list_url="https://x")

    assert workbook.read_bytes() == before
    preview = res["sheets"]["StoreA全球1"]["preview"][0]
    assert preview["数量*"] == 3
    # 位置要紧跟「尺码」，人对着看才顺
    titles = list(preview)
    assert titles[titles.index("尺码") + 1] == "数量*"


def test_ensure_qty_columns_only_touches_sheets_this_batch_writes(workbook, monkeypatch):
    """只给本批真有订单落进去的表插列——改结构不可逆，不碰无关的表。"""
    orders = [_order("PO-新A", "32"), _order("PO-211", "34", site="美国")]

    got = S.ensure_qty_columns(orders, str(workbook), "StoreA", SHEET_MAP)

    assert [item["sheet"] for item in got] == ["StoreA全球1"], "未映射的美国单不该触发插列"
    assert got[0]["inserted"] is True and got[0]["column"] == "E"


def test_ensure_qty_columns_survives_insert_failure(workbook, monkeypatch):
    """插列失败只告警：那张表数量列留空，不能连累整批订单登记不了。"""
    def boom(*_a, **_k):
        raise RuntimeError("表结构对不上")

    monkeypatch.setattr(S.WpsExcelTool, "insert_column_after", boom)

    got = S.ensure_qty_columns([_order("PO-新A", "32")], str(workbook), "StoreA", SHEET_MAP)

    assert got == [{"sheet": "StoreA全球1", "inserted": False, "reason": "表结构对不上"}]
    assert "数量" not in WpsExcelTool.read_header(str(workbook), "StoreA全球1").values()


def test_write_failure_is_reported_not_swallowed(workbook, monkeypatch):
    """写失败要进汇总与事件（表被 WPS 独占锁定是常见情形），不静默。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    def boom(*a, **k):
        raise PermissionError("文件被占用")

    monkeypatch.setattr(wet.WpsExcelTool, "append_rows", boom)
    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    assert res["failed_sheets"] == ["StoreA全球1"]
    assert res["written_rows"] == 0
    assert "文件被占用" in res["written"]["StoreA全球1"]["error"]
    assert any(e["type"] == "write_failed" for e in res["_events"])


# ---- 护栏中止 --------------------------------------------------------------


def test_aborts_when_workbook_missing(monkeypatch, tmp_path):
    _patch_collect(monkeypatch, [])
    res = _run(dry_run=True, workbook=str(tmp_path / "不存在.xlsx"), list_url="https://x")

    assert "登记表不存在" in res["aborted"]
    assert res["written_rows"] == 0


def test_lock_precheck_only_blocks_write_mode(workbook, monkeypatch):
    """表被 WPS 占用时：写入模式提前中止（别让人白等翻页导出），dry-run 照常跑完。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32")])
    monkeypatch.setattr(S, "excel_write_locked", lambda p: True)

    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")
    assert "正被占用" in res["aborted"] and res["written_rows"] == 0
    # 中止发生在采集之前：不该有任何采集/解析事件
    assert [e["type"] for e in res["_events"]] == ["aborted"]

    dry = _run(dry_run=True, workbook=str(workbook), list_url="https://x")
    assert dry["aborted"] == "" and dry["pending"] == 1


def test_aborts_when_cdp_down(workbook, monkeypatch):
    _patch_collect(monkeypatch, [_order("PO-A", "32")])

    async def dead(*a, **k):
        return False

    monkeypatch.setattr(S, "ensure_cdp_alive", dead)
    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    assert "CDP 不可用" in res["aborted"] and res["written_rows"] == 0


def test_aborts_when_collect_fails(workbook, monkeypatch):
    """采集失败（含店铺识别不到）要中止且不写任何东西。"""
    async def boom(*a, **k):
        raise RuntimeError("识别不到当前登录店铺名，已中止")

    monkeypatch.setattr(S, "ensure_cdp_alive", lambda *a, **k: _true())
    monkeypatch.setattr(S, "collect_orders", boom)
    before = workbook.read_bytes()

    res = _run(dry_run=False, workbook=str(workbook), list_url="https://x")

    assert "识别不到当前登录店铺名" in res["aborted"]
    assert workbook.read_bytes() == before


# ---- 显式指定 Sheet（UI 上用户自己选落点）----------------------------------


def test_explicit_sheet_overrides_site_routing(workbook, monkeypatch):
    """显式给 sheet：所有订单都进这张表，不再按站点查 sheet_map。

    这是 UI「目标 Sheet」下拉的语义——用户已自己判断过落点。这里刻意给两个站点各异、
    且都不在 sheet_map 里的订单，若仍走站点分流，它们会全落进「未映射跳过」。
    """
    _patch_collect(monkeypatch, [
        _order("PO-新A", "32", site="不存在的站点"),
        _order("PO-新B", "34", site="另一个怪站点"),
    ])

    res = _run(dry_run=True, workbook=str(workbook), list_url="https://x",
               store="StoreA", sheet="StoreA全球1")

    assert res["pending"] == 2 and res["unmapped_skipped"] == 0
    assert list(res["sheets"]) == ["StoreA全球1"]


def test_explicit_sheet_reuses_configured_store_value(workbook, monkeypatch):
    """「订单店铺」列照抄 sheet_map 里同名 Sheet 的 store_value（表内既有写法）。

    StoreA 的表里写的是「StoreA全球」而不是登录店铺名「StoreA」，直接写登录名会和历史行不一致。
    """
    monkeypatch.setattr(S, "load_orders_config", lambda: {
        "sheet_map": [{"store": "StoreA", "sites": ["秘鲁"],
                       "sheet": "StoreA全球1", "store_value": "StoreA全球"}],
    })
    _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(dry_run=True, workbook=str(workbook), list_url="https://x",
               store="StoreA", sheet="StoreA全球1")

    assert res["sheets"]["StoreA全球1"]["preview"][0]["订单店铺"] == "StoreA全球"


def test_explicit_sheet_without_store_aborts(workbook, monkeypatch):
    """指定 Sheet 却不给店铺要中止：店铺名要写进「订单店铺」列，没有就没法合成映射。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(dry_run=True, workbook=str(workbook), list_url="https://x",
               sheet="StoreA全球1")

    assert "必须同时指定店铺" in res["aborted"]
    assert [e["type"] for e in res["_events"]] == ["aborted"]


def test_explicit_sheet_map_falls_back_to_typed_store():
    """sheet_map 里没有这张表时，「订单店铺」列就用用户填的店铺名。"""
    got = S._explicit_sheet_map("StoreF", "美国袜子", {"sheet_map": []})

    assert got == [{"store": "StoreF", "sites": [], "sheet": "美国袜子",
                    "store_value": "StoreF"}]
    # sites 为空＝不限站点，这是 resolve_sheet 的通配语义
    assert P.resolve_sheet("StoreF", "任意站点", got)["sheet"] == "美国袜子"


# ---- 首屏选择（工作簿/Sheet 列表与可写性）----------------------------------


def test_inspect_sheet_reports_writable(workbook):
    info = S.inspect_sheet(str(workbook), "StoreA全球1")

    assert info["writable"] is True and info["reason"] == ""
    assert info["dedupe_cols"] == {"订单号": "C", "尺码": "D"}
    assert "订单号" in info["titles"]


def test_inspect_sheet_flags_missing_dedupe_column(workbook):
    """缺判重列必须在选之前就报——否则跑完几分钟导出才发现整表被跳过、一行不写。"""
    info = S.inspect_sheet(str(workbook), "StoreA全球1", ["订单号", "不存在的列"])

    assert info["writable"] is False and "不存在的列" in info["reason"]


def test_inspect_sheet_survives_missing_sheet(workbook):
    info = S.inspect_sheet(str(workbook), "没有这张表")

    assert info["writable"] is False and info["reason"]


def test_worklist_status_filters_reserved_and_validates_sheet(workbook, monkeypatch):
    """WpsReserved_* 不是数据表不给选；上次选的 Sheet 不在本工作簿里则视为未选。"""
    # 本用例钉的是本地工作簿路径的行为；本机 config.toml 可能已开协作文档模式，
    # 显式钉死本地后端，避免用例结果依赖机器配置
    monkeypatch.setattr(S, "cloud_backend", lambda cfg: None)
    monkeypatch.setattr(S, "list_workbooks", lambda: [str(workbook)])
    monkeypatch.setattr(S, "load_prefs", lambda: {"sheet": "别的工作簿的表"})
    monkeypatch.setattr(
        S.WpsExcelTool, "list_sheets",
        classmethod(lambda cls, p: ["StoreA全球1", "WpsReserved_CellImgList"]),
    )

    st = S.get_worklist_status(workbook=str(workbook))

    assert st["sheets"] == ["StoreA全球1"]
    assert st["sheet"] == "" and st["sheet_info"] == {}
    assert st["workbook_exists"] is True


def test_prefs_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "ORDERS_PREFS", tmp_path / "orders_prefs.json")
    S.save_prefs(store="StoreB", workbook="D:/wb.xlsx", sheet="StoreB欧区")

    assert S.load_prefs() == {
        "store": "StoreB", "workbook": "D:/wb.xlsx", "sheet": "StoreB欧区",
        "cloud_url": "",
    }


def test_prefs_cloud_link_goes_to_cloud_url(tmp_path, monkeypatch):
    """协作文档链接单独存 cloud_url，且显式选本地路径会清掉旧链接。"""
    monkeypatch.setattr(S, "ORDERS_PREFS", tmp_path / "orders_prefs.json")
    S.save_prefs(store="Pawly", workbook="https://www.kdocs.cn/l/abc123",
                 sheet="Pawly全球1")
    assert S.load_prefs() == {
        "store": "Pawly", "workbook": "", "sheet": "Pawly全球1",
        "cloud_url": "https://www.kdocs.cn/l/abc123",
    }
    S.save_prefs(store="Pawly", workbook="D:/wb.xlsx", sheet="")
    assert S.load_prefs()["cloud_url"] == ""


def test_worklist_explicit_cloud_link(monkeypatch):
    """UI 粘贴协作文档链接：走云端分支列出该文档的 Sheet，不回退 config/本地。"""
    class FakeCloud:
        def sheet_names(self):
            return ["Pawly全球1", "WINTAK"]

    seen = {}

    def fake_backend(cfg, cloud_url=""):
        seen["url"] = cloud_url
        return FakeCloud()

    monkeypatch.setattr(S, "cloud_backend", fake_backend)
    monkeypatch.setattr(S, "load_orders_config", lambda: {"dedupe_by": ["订单号", "尺码"]})
    monkeypatch.setattr(S, "load_prefs", lambda: {})

    st = S.get_worklist_status(workbook="https://www.kdocs.cn/l/new-doc")
    assert seen["url"] == "https://www.kdocs.cn/l/new-doc"
    assert st["cloud"] is True
    assert st["workbook"] == "https://www.kdocs.cn/l/new-doc"
    assert st["sheets"] == ["Pawly全球1", "WINTAK"]
    assert st["workbook_exists"] is True

    # 显式传本地路径 → 本地模式（cloud_backend 不该被用于目标）
    monkeypatch.setattr(S, "list_workbooks", lambda: [])
    monkeypatch.setattr(
        S.WpsExcelTool, "list_sheets",
        classmethod(lambda cls, p: ["Sheet1"]),
    )
    st = S.get_worklist_status(workbook="D:/local.xlsx")
    assert "cloud" not in st
    assert st["sheets"] == ["Sheet1"]


def test_load_prefs_tolerates_garbage(tmp_path, monkeypatch):
    """偏好文件损坏不能让整页打不开。"""
    p = tmp_path / "orders_prefs.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(S, "ORDERS_PREFS", p)

    assert S.load_prefs() == {}


def _plan_with_rows(n=3):
    """造一个带 preview 的最小写入计划（dump_plan_csv 只吃 rows/preview）。"""
    plan = S.SheetPlan(sheet="StoreA全球1", store_value="StoreA全球")
    for i in range(n):
        plan.rows.append({"values": {"C": f"PO-{i}"}})
        plan.preview.append({"订单号": f"PO-{i}", "尺码": "杏色 / 3-4Y", "_图片": "有"})
    return plan


def test_dump_plan_csv_writes_all_rows(tmp_path):
    """日志只打 3 行，CSV 必须是全量——否则人工没法逐行核对。"""
    plans = {"StoreA全球1": _plan_with_rows(74)}
    files = S.dump_plan_csv(plans, str(tmp_path), "20260727")

    assert len(files) == 1
    import csv as _csv
    with open(files[0], encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.reader(f))
    assert rows[0] == ["序号", "订单号", "尺码", "_图片"]
    assert len(rows) == 75          # 表头 + 74 行
    assert rows[1][:2] == ["1", "PO-0"] and rows[-1][:2] == ["74", "PO-73"]


def test_dump_plan_csv_skips_empty_and_survives_failure(tmp_path):
    """空计划不产文件；落盘失败只告警不抛（辅助产物不能拖垮主流程）。"""
    empty = {"运单号": S.SheetPlan(sheet="运单号", store_value="X")}
    assert S.dump_plan_csv(empty, str(tmp_path), "20260727") == []
    # 目录不存在 → 写失败，但必须静默返回空列表
    assert S.dump_plan_csv({"a": _plan_with_rows()}, str(tmp_path / "无此目录"), "s") == []


# ---- 成交价：默认允许为空，严格模式才留到下批 ------------------------------


def test_plan_writes_allows_empty_deal_price_by_default(workbook):
    """默认口径（2026-07-29 确认）：成交单价为空照常登记，不产生 unpriced。

    插件回填成交单价约有一天延迟（2026-07-28 实机：当天 14 单全部无「成交单价」标签），
    等价会让当天的单整批积压，故这一格允许留空。
    """
    orders = [
        _order("PO-045-有价", "杏色 / 3-4Y"),
        _order("PO-045-无价", "杏色 / 9-12M", deal_price=""),
        _order("PO-045-空白价", "杏色 / 5-6Y", deal_price="   "),
    ]

    plans, unmapped, unpriced = S.plan_writes(orders, str(workbook), "StoreA", SHEET_MAP)

    assert len(plans["StoreA全球1"].rows) == 3, "三条都要能写，空价不拦"
    assert unpriced == [] and unmapped == []


def test_plan_writes_defers_unpriced_in_strict_mode(workbook):
    """require_price=True 才恢复旧口径：无价单独归 unpriced，不混进 unmapped。"""
    orders = [
        _order("PO-045-有价", "杏色 / 3-4Y"),
        _order("PO-045-无价", "杏色 / 9-12M", deal_price=""),
        _order("PO-045-空白价", "杏色 / 5-6Y", deal_price="   "),
    ]

    plans, unmapped, unpriced = S.plan_writes(
        orders, str(workbook), "StoreA", SHEET_MAP, require_price=True
    )

    assert len(plans["StoreA全球1"].rows) == 1, "严格模式下只有带价那条能写"
    assert unmapped == [], "无价不是映射问题，别混进 unmapped"
    assert [u["order_no"] for u in unpriced] == ["PO-045-无价", "PO-045-空白价"]
    assert "成交单价" in unpriced[0]["reason"]


def test_empty_deal_price_reaches_workbook(workbook, monkeypatch):
    """端到端：默认口径下无价订单也进表，成交价那一格留空。"""
    _patch_collect(monkeypatch, [
        _order("PO-045-有价", "杏色 / 3-4Y"),
        _order("PO-045-无价", "杏色 / 9-12M", deal_price=""),
    ])
    res = _run(store="StoreA", dry_run=False, workbook=str(workbook), sheet="StoreA全球1")

    assert res["unpriced_skipped"] == 0
    assert res["written_rows"] == 2, "两条都写"
    assert not any(e["type"] == "unpriced" for e in res["_events"])
    body = zipfile.ZipFile(workbook).read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "PO-045-无价" in body and "PO-045-有价" in body


# ---- 增量采集：水位读取与启用条件 ------------------------------------------


def test_read_known_order_nos_reads_registered_orders(workbook):
    """水位＝该 Sheet「订单号」列已登记的值，按真实表头定位列、不硬编码列字母。"""
    known = S.read_known_order_nos(str(workbook), "StoreA全球1")

    assert "PO-045-已入库" in known


def test_read_known_order_nos_returns_empty_on_missing_sheet(workbook):
    """表不存在 → 空集（调用方据此退全量），绝不抛错。"""
    assert S.read_known_order_nos(str(workbook), "不存在的表") == set()


def test_incremental_uses_watermark_in_single_sheet_mode(workbook, monkeypatch):
    """单表模式：水位传给 collect_orders，汇总里标 enabled。"""
    seen = _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(store="StoreA", dry_run=True, workbook=str(workbook),
               sheet="StoreA全球1")

    assert "PO-045-已入库" in seen["known"], "单表模式必须把已登记订单号当水位传下去"
    assert res["incremental"]["enabled"] is True
    assert res["incremental"]["known"] >= 1


def test_incremental_disabled_when_sheet_map_routing(workbook, monkeypatch):
    """按 sheet_map 分流（未指定 sheet）→ 不用水位：多张表新旧不同，取并集会漏单。"""
    seen = _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(store="StoreA", dry_run=True, workbook=str(workbook))

    assert seen["known"] == set(), "分流模式绝不能传水位"
    assert res["incremental"]["enabled"] is False
    assert "分流" in res["incremental"]["reason"]


def test_incremental_can_be_turned_off(workbook, monkeypatch):
    """显式关掉增量 → 即使单表模式也不读水位。"""
    seen = _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(store="StoreA", dry_run=True, workbook=str(workbook),
               sheet="StoreA全球1", incremental=False)

    assert seen["known"] == set()
    assert res["incremental"]["enabled"] is False
    assert "关闭" in res["incremental"]["reason"]


def test_watermark_event_is_emitted(workbook, monkeypatch):
    """水位要作为事件上报，UI/CLI 才能显示「增量已启用/为何全量」。"""
    _patch_collect(monkeypatch, [_order("PO-新A", "32")])

    res = _run(store="StoreA", dry_run=True, workbook=str(workbook),
               sheet="StoreA全球1")

    wm = next(e for e in res["_events"] if e["type"] == "watermark")
    assert wm["enabled"] is True and wm["sheet"] == "StoreA全球1"


def test_unpriced_never_reaches_workbook_in_strict_mode(workbook, monkeypatch):
    """端到端严格模式：无价订单不进表，且汇总/事件里都能看到它被留下了。"""
    _patch_collect(monkeypatch, [
        _order("PO-045-有价", "杏色 / 3-4Y"),
        _order("PO-045-无价", "杏色 / 9-12M", deal_price=""),
    ])
    res = _run(store="StoreA", dry_run=False, workbook=str(workbook),
               sheet="StoreA全球1", require_price=True)

    assert res["unpriced_skipped"] == 1
    assert res["written_rows"] == 1, "只写带价那条"
    assert any(e["type"] == "unpriced" for e in res["_events"])
    body = zipfile.ZipFile(workbook).read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "PO-045-无价" not in body and "PO-045-有价" in body
