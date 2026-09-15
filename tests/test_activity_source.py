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
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def sheet_names(self):
        return ["成本表"]

    def data_end_row(self, sheet):
        return max(self.rows) - 1

    def read_rows(self, sheet, start, end):
        self.calls.append((start, end))
        return {number: row for number, row in self.rows.items() if start <= number <= end}


@pytest.fixture
def cloud(monkeypatch):
    sheet = FakeSheet({
        1: cells(A="商品成本核算"),
        6: cells(B="货号", C="SPU ID", E="销售价格", F="日常价", G="采购价"),
        7: cells(B="SKU-A", C="123456789012345678", E="40.00", F="60.00", G="12.50"),
        8: cells(B="SKU-B", C="123456789012345678", E="40", F="60", G="13.00"),
        507: cells(B="SKU-C", C="999", E="50", F="80", G="15"),
    })
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


def test_same_spu_conflicting_or_invalid_prices_are_not_selectable(cloud):
    cloud.rows[8]["E"]["text"] = "41"
    snapshot = source.read_document(DOCUMENT, "成本表")
    assert all(not row["selectable"] for row in snapshot["rows"][:2])
    assert "同一 SPU" in snapshot["rows"][0]["issues"][-1]
    with pytest.raises(ValueError, match="价格无效"):
        source.read_costs(DOCUMENT, "成本表", ["123456789012345678"])


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
    assert result["results"][0]["sale"] == 65
    assert result["results"][0]["submit_price"] == 72
    read_stock.assert_not_called()
    plan = next(event for event in events if event["type"] == "product_plan")
    assert plan["selected"] is True
    assert plan["stock_policy"] == "on_demand"
    assert plan["stock_ok"] is None


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
