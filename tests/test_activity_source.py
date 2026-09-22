import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.activity import service, source


DOCUMENT = "https://www.kdocs.cn/l/test-costs"


def cells(**values):
    return {column: {"text": str(value), "formula": ""} for column, value in values.items()}


class FakeSheet:
    """模拟 KdocsSheet：rows 是「原始（未展开）」数据，merged 描述纵向合并格。

    read_rows 的 expand_merged 语义在 Fake 侧复刻：白名单列的合并格把锚点值补进窗口内
    每个覆盖行；不在白名单的列（价格）永不补——这正是真实实现里「价格列不展开」的口径。
    """

    def __init__(self, rows, merged=None):
        self.rows = rows
        self.merged = merged or {}  # {列字母: {锚点行号: (值, 覆盖末行号)}}
        self.calls = []
        self.expand_calls = []

    def sheet_names(self):
        return ["成本表"]

    def data_end_row(self, sheet):
        return max(self.rows) - 1

    def read_rows(self, sheet, start, end, expand_merged=None):
        self.calls.append((start, end))
        self.expand_calls.append(set(expand_merged or ()))
        window = {number: dict(row) for number, row in self.rows.items() if start <= number <= end}
        for col in (expand_merged or ()):
            for anchor, (value, last_row) in self.merged.get(col, {}).items():
                for number in range(max(anchor, start), min(last_row, end) + 1):
                    if number != anchor:
                        window.setdefault(number, {}).setdefault(
                            col, {"text": value, "formula": ""})
        return window


@pytest.fixture
def cloud(monkeypatch):
    sheet = FakeSheet(
        rows={
            1: cells(A="商品成本核算"),
            6: cells(B="货号", C="SPU ID", E="销售价格", F="日常价", G="采购价"),
            # 一个 SPU 两个货号（不同价，合法多货号）；行8 的 C 由合并展开补上
            7: cells(B="SKU-A", C="123456789012345678", E="40.00", F="60.00", G="12.50"),
            8: cells(B="SKU-B", E="50", F="80", G="13.00"),
            507: cells(B="SKU-C", C="999", E="50", F="80", G="15"),
        },
        merged={"C": {7: ("123456789012345678", 8)}},
    )
    sheet.rows[7]["E"]["formula"] = "=20*2"
    monkeypatch.setattr(source, "KdocsSheet", lambda document: sheet)
    return sheet


def test_read_document_uses_calculated_values_and_reads_all_windows(cloud):
    snapshot = source.read_document(DOCUMENT, "成本表")
    assert snapshot["header_row"] == 6
    assert snapshot["fields"]["sale"] == "E"
    assert snapshot["total"] == 3
    assert snapshot["selectable"] == 2
    assert snapshot["rows"][0]["spu"] == "123456789012345678"
    assert snapshot["rows"][0]["sale"] == 40
    assert snapshot["rows"][0]["values"]["E"] == "40.00"
    assert (7, 506) in cloud.calls and (507, 507) in cloud.calls
    # 行8 的 SPU 由合并展开补出；表头探测（首个调用）不展开、数据批才带白名单
    assert snapshot["rows"][1]["spu"] == "123456789012345678"
    assert cloud.expand_calls[0] == set()
    assert any("C" in cols for cols in cloud.expand_calls[1:])


def test_same_spu_multiple_valid_skus_are_selectable(cloud):
    """同一 SPU 多行货号、价格不同是合法场景：两行都可选，read_costs 给逐货号 items。"""
    snapshot = source.read_document(DOCUMENT, "成本表")
    rows = snapshot["rows"][:2]
    assert all(row["selectable"] for row in rows)
    assert [row["sku"] for row in rows] == ["SKU-A", "SKU-B"]
    costs = source.read_costs(DOCUMENT, "成本表", ["123456789012345678"])
    items = costs["123456789012345678"]["items"]
    assert [(item["label"], item["daily"], item["sale"]) for item in items] == [
        ("SKU-A", 60.0, 40.0), ("SKU-B", 80.0, 50.0),
    ]
    assert [item["row_number"] for item in items] == [7, 8]


def test_one_invalid_row_makes_whole_spu_unselectable(cloud):
    """任一货号行价格无效 → 整组连坐不可选（提报是 SPU 级，不能只报部分货号）。"""
    cloud.rows[8]["E"]["text"] = "坏"
    snapshot = source.read_document(DOCUMENT, "成本表")
    rows = snapshot["rows"][:2]
    assert all(not row["selectable"] for row in rows)
    assert rows[1]["issues"] == ["销售底价无有效计算结果"]
    assert "同一 SPU 第 8 行" in rows[0]["issues"][-1]
    with pytest.raises(ValueError, match="价格无效"):
        source.read_costs(DOCUMENT, "成本表", ["123456789012345678"])


def test_merged_price_columns_are_not_filled(cloud):
    """价格列即使是合并单元格也不展开继承：行8 销售价合并缺失 → 该行无效、整组连坐。"""
    cloud.merged["E"] = {7: ("40.00", 8)}  # 表内价格列也合并了，但读取绝不展开价格列
    del cloud.rows[8]["E"]
    snapshot = source.read_document(DOCUMENT, "成本表")
    rows = snapshot["rows"][:2]
    assert all(not row["selectable"] for row in rows)
    assert "销售底价" in rows[1]["issues"][-1]


def test_duplicate_sku_label_makes_group_unselectable(cloud):
    """同一 SPU 下货号重复：货号维已乱（执行层按行数/价格匹配会算错），读文档期拦下。"""
    cloud.rows[8]["B"]["text"] = "SKU-A"
    snapshot = source.read_document(DOCUMENT, "成本表")
    rows = snapshot["rows"][:2]
    assert all(not row["selectable"] for row in rows)
    assert "货号「SKU-A」重复" in rows[0]["issues"][-1]
    assert "货号「SKU-A」重复" in rows[1]["issues"][-1]


@pytest.mark.parametrize("invalid", ["", "#VALUE!", "=20*2", "NaN", "inf", "0", "-5"])
def test_invalid_prices_cannot_enter_planning(cloud, invalid):
    cloud.rows[507]["E"]["text"] = invalid
    snapshot = source.read_document(DOCUMENT, "成本表")
    assert not snapshot["rows"][-1]["selectable"]
    with pytest.raises(ValueError):
        source.read_costs(DOCUMENT, "成本表", ["999"])


def test_missing_columns_are_visible_but_not_selectable(cloud):
    del cloud.rows[6]["F"]
    snapshot = source.read_document(DOCUMENT, "成本表")
    assert snapshot["warnings"] and "日常价" in snapshot["warnings"][0]
    assert snapshot["total"] == 3
    assert snapshot["selectable"] == 0


@pytest.mark.parametrize("document", ["D:/costs.xlsx", "", "https://example.com/a", "https://kdocs.cn.evil.test/a"])
def test_local_or_unrelated_document_is_rejected_before_reading(cloud, document):
    with pytest.raises(ValueError):
        source.read_document(document)
    assert cloud.calls == []


def test_sheet_listing_does_not_read_cells(cloud):
    assert source.read_document(DOCUMENT)["sheets"] == ["成本表"]
    assert cloud.calls == []
    with pytest.raises(ValueError, match="不存在"):
        source.read_document(DOCUMENT, "已删除")


def test_missing_selected_spu_aborts_before_connecting_temu(monkeypatch, cloud):
    connect = AsyncMock()
    monkeypatch.setattr(service, "ensure_cdp_alive", connect)
    events = []
    result = asyncio.run(service.run_activity_batch("888", DOCUMENT, "成本表", on_progress=events.append))
    assert result["fail"] == 1
    assert events[-1]["type"] == "aborted"
    connect.assert_not_called()


@pytest.mark.parametrize("stock_map", [None, {}, {"999": 0}])
def test_planning_reads_fresh_cloud_prices_without_stock_gate(monkeypatch, cloud, stock_map):
    cloud.rows[507]["E"]["text"] = "65"
    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", AsyncMock())
    monkeypatch.setattr(service.pipeline, "read_accel_state", AsyncMock(return_value="off"))
    monkeypatch.setattr(service.pipeline, "read_activities", AsyncMock(return_value=[
        {"name": "活动A", "discount_rate": 0.9, "min_stock": 5},
    ]))
    monkeypatch.setattr(service, "reset_pipeline_llms", lambda: None)
    read_stock = AsyncMock(side_effect=AssertionError("库存不应被读取"))
    monkeypatch.setattr(service.pipeline, "read_stock", read_stock)
    events = []
    result = asyncio.run(service.run_activity_batch(
        "999", DOCUMENT, "成本表", flux_page=object(), activity_page=object(), goods_page=object(),
        stock_map=stock_map, on_progress=events.append,
    ))
    assert result["done"] == 1
    assert result["results"][0]["skus"] == [
        {"label": "SKU-C", "daily": 80, "sale": 65, "purchase": "15"}]
    assert result["results"][0]["submit_price"] == 72  # 单货号兼容字段
    read_stock.assert_not_called()
    plan = next(event for event in events if event["type"] == "product_plan")
    assert plan["selected"] is True
    assert plan["stock_policy"] == "on_demand"
    assert plan["stock_ok"] is None


@pytest.mark.parametrize("rate,expect_selected", [(0.9, True), (0.6, False)])
def test_planning_multi_spu_skus_all_must_pass_floor(monkeypatch, cloud, rate, expect_selected):
    """多货号 SPU 的规划：活动入选 = 全部货号申报价都达各自底价（提报是 SPU 级，不能只报
    部分货号）。SKU-A 60/40、SKU-B 80/50：0.9 折全过 → sku_prices 两条；0.6 折 SKU-A 36<40
    穿底 → 整活动淘汰，skip_nomatch 的 note 须点明是哪个货号穿底。"""
    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", AsyncMock())
    monkeypatch.setattr(service.pipeline, "read_accel_state", AsyncMock(return_value="off"))
    monkeypatch.setattr(service.pipeline, "read_activities", AsyncMock(return_value=[
        {"name": "活动A", "discount_rate": rate, "min_stock": 5},
    ]))
    monkeypatch.setattr(service, "reset_pipeline_llms", lambda: None)
    events = []
    result = asyncio.run(service.run_activity_batch(
        "123456789012345678", DOCUMENT, "成本表",
        flux_page=object(), activity_page=object(), goods_page=object(),
        on_progress=events.append,
    ))
    row = result["results"][0]
    assert [s["label"] for s in row["skus"]] == ["SKU-A", "SKU-B"]
    if expect_selected:
        assert result["done"] == 1 and row["status"] == "done"
        prices = row["enrolled_activities"][0]["sku_prices"]
        assert [(p["label"], p["submit_price"]) for p in prices] == [
            ("SKU-A", 54.0), ("SKU-B", 72.0)]
        assert row["submit_price"] is None  # 多货号不落单值字段
        plan = next(event for event in events if event["type"] == "product_plan")
        assert plan["sku_count"] == 2 and plan["sale"] is None
        assert [s["label"] for s in plan["skus"]] == ["SKU-A", "SKU-B"]
    else:
        assert row["status"] == "skip_nomatch"
        assert "货号SKU-A" in row["note"] and "36.0" in row["note"] and "底价40" in row["note"]


@pytest.fixture(scope="module")
def webapp():
    spec = importlib.util.spec_from_file_location("webapp_activity_source", Path(__file__).resolve().parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cloud_preview_endpoint_and_failures(webapp, cloud, monkeypatch):
    with TestClient(webapp.app) as client:
        response = client.get("/activity/worklist", params={"cloud_url": DOCUMENT, "sheet": "成本表"})
        assert response.status_code == 200
        assert response.json()["total"] == 3
        assert client.get("/activity/worklist", params={"cloud_url": "D:/costs.xlsx"}).status_code == 400
        monkeypatch.setattr(source, "read_document", lambda *args: (_ for _ in ()).throw(RuntimeError("登录已过期")))
        response = client.get("/activity/worklist", params={"cloud_url": DOCUMENT})
        assert response.status_code == 502
        assert "登录已过期" in response.json()["error"]


def test_batch_requires_cloud_and_preserves_dry_run_default(webapp, monkeypatch):
    received = []

    async def run(spus, document, sheet, **kwargs):
        received.append((spus, document, sheet, kwargs["dry_run"], kwargs["live"]))
        return {}

    monkeypatch.setattr(webapp.activity_service, "run_activity_batch", run)
    with TestClient(webapp.app) as client:
        assert client.post("/activity/batch", json={"excel": "D:/costs.xlsx", "sheet": "成本表", "spus": "999"}).status_code == 400
        response = client.post("/activity/batch", json={"cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999"})
        assert response.status_code == 200
        client.get(f"/activity/batch/{response.json()['job_id']}/events")
    assert received == [("999", DOCUMENT, "成本表", True, False)]
