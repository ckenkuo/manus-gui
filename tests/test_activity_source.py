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


def test_costs_from_snapshot_non_strict_keeps_present_spus(cloud):
    """识别扫描用宽松取价：缺价 SPU 只是不出现在结果里，不能让整张矩阵扫不出来；
    批次路径仍走 read_costs（strict）——缺价必须整批中止，这是既有安全设计。"""
    cloud.rows[8]["E"]["text"] = "坏"  # 整组连坐，123456789012345678 不可选
    snapshot = source.read_document(DOCUMENT, "成本表")
    costs = source.costs_from_snapshot(snapshot, ["123456789012345678", "999"], strict=False)
    assert list(costs) == ["999"] and costs["999"]["items"][0]["label"] == "SKU-C"
    with pytest.raises(ValueError, match="价格无效"):
        source.costs_from_snapshot(snapshot, ["123456789012345678", "999"])


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


def _planning_events(monkeypatch, cloud, spus, activities, selection=None):
    """跑一次 dry-run 规划，返回 (result, events)。"""
    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", AsyncMock())
    monkeypatch.setattr(service.pipeline, "read_accel_state", AsyncMock(return_value="off"))
    monkeypatch.setattr(service.pipeline, "read_activities", AsyncMock(return_value=activities))
    monkeypatch.setattr(service, "reset_pipeline_llms", lambda: None)
    events = []
    result = asyncio.run(service.run_activity_batch(
        spus, DOCUMENT, "成本表",
        flux_page=object(), activity_page=object(), goods_page=object(),
        dry_run=True, selection=selection, on_progress=events.append,
    ))
    return result, events


def test_planning_marks_unselected_cells_and_stats_follow_selection(monkeypatch, cloud):
    """识别矩阵勾选的格子：product_plan 如实把未勾的标成未入选，统计也只按勾选后的范围算——
    不能让页面显示「将报名 2 个活动」而执行遍只跑 1 个。"""
    cloud.rows[507]["E"]["text"] = "65"
    result, events = _planning_events(monkeypatch, cloud, "999", [
        {"name": "活动A", "discount_rate": 0.9, "min_stock": 5},
        {"name": "活动B", "discount_rate": 0.85, "min_stock": None},
    ], selection=[["999", "活动B"]])

    plans = [e for e in events if e["type"] == "product_plan"]
    assert [(p["activity"], p["selected"]) for p in plans] == [("活动A", False), ("活动B", True)]
    assert "未勾选" in plans[0]["reason"]
    assert [e["activity"] for e in result["results"][0]["enrolled_activities"]] == ["活动B"]
    assert result["results"][0]["status"] == "done" and result["done"] == 1
    # product_done 的 note 只能写勾选到的活动（真机半程实测踩过：说了 4 个实际只报 1 个）
    done_note = next(e for e in events if e["type"] == "product_done")["note"]
    assert "将报名 1 个活动：活动B" in done_note and "活动A" not in done_note


def test_no_selected_cell_skips_spu_entirely(monkeypatch, cloud):
    """一个格子都没勾的商品：直接 skip_nomatch、不进执行遍（连流量都不动），并在 note 里说明。"""
    cloud.rows[507]["E"]["text"] = "65"
    result, _events = _planning_events(monkeypatch, cloud, "999", [
        {"name": "活动A", "discount_rate": 0.9, "min_stock": 5},
    ], selection=[["999", "别的活动"]])

    assert result["results"][0]["status"] == "skip_nomatch"
    assert result["skip"] == 1 and result["done"] == 0
    assert "没有勾选" in result["results"][0]["note"]


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
    webapp.activity_jobs.clear()
    with TestClient(webapp.app) as client:
        assert client.post("/activity/batch", json={"excel": "D:/costs.xlsx", "sheet": "成本表", "spus": "999"}).status_code == 400
        response = client.post("/activity/batch", json={"cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999"})
        assert response.status_code == 200
        client.get(f"/activity/batch/{response.json()['job_id']}/events")
    assert received == [("999", DOCUMENT, "成本表", True, False)]


def _job_url(job_id: str) -> str:
    return f"/activity/batch/{job_id}/events"


def test_batch_accepts_explicit_half_run_and_cell_selection(webapp, monkeypatch):
    """半程/全程档位与识别矩阵勾选都能从 Web 传下来：Web 端原先进不了半程（live 恒等于
    not dry_run），操作者只能全程一步到底；selection 让「只报勾选的格子」成为可能。"""
    received = []

    async def run(spus, document, sheet, **kwargs):
        received.append((kwargs["dry_run"], kwargs["live"], kwargs["selection"]))
        return {}

    monkeypatch.setattr(webapp.activity_service, "run_activity_batch", run)
    webapp.activity_jobs.clear()
    with TestClient(webapp.app) as client:
        half = client.post("/activity/batch", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999",
            "dry_run": False, "live": False, "selection": [["999", "官方大促"]]})
        client.get(_job_url(half.json()["job_id"]))
        full = client.post("/activity/batch", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999", "dry_run": False, "live": True})
        client.get(_job_url(full.json()["job_id"]))
    assert received == [(False, False, [["999", "官方大促"]]), (False, True, None)]


def test_scan_starts_readonly_job_and_sse_replays_on_reconnect(webapp, monkeypatch):
    """识别起独立作业（同一 SSE 端点）；刷新页面重连同一作业仍能拿到完整事件并正常收尾
    ——旧实现的 _end 哨兵被上一个连接读走后，新连接会永久阻塞，页面刷新就卡死。"""
    calls = []

    async def fake_scan(spus, excel, sheet, **kwargs):
        calls.append((spus, excel, sheet, kwargs["force"]))
        await kwargs["on_progress"]({"type": "scan_start", "cells": [], "counts": {}})
        await kwargs["on_progress"]({"type": "scan_done", "eligible": 1})
        return {"day": "2026-09-24", "counts": {"eligible": 1}}

    monkeypatch.setattr(webapp.activity_service, "scan_activity_matrix", fake_scan)
    webapp.activity_jobs.clear()
    with TestClient(webapp.app) as client:
        bad = client.post("/activity/scan", json={"excel": "D:/x.xlsx", "sheet": "成本表", "spus": "999"})
        assert bad.status_code == 400
        response = client.post("/activity/scan", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999", "force": True})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        first = client.get(_job_url(job_id)).text
        assert "event: scan_start" in first and "event: scan_done" in first and "event: done" in first
        second = client.get(_job_url(job_id)).text  # 刷新页面 = 重连同一 job
        assert "event: scan_start" in second and "event: done" in second
        assert "event: error" not in second
    assert calls == [("999", DOCUMENT, "成本表", True)]


def test_scan_and_execution_are_mutually_exclusive_but_dry_run_is_not(webapp, monkeypatch):
    """识别与正式执行硬互斥（两者都靠 detail-new 页的对象 diff 认页签，并发会互相认错页）；
    纯 dry-run 批次不开提报页，识别期间照常可用。暂停/跳过接口在此一并验证。"""
    async def slow_scan(_spus, _excel, _sheet, **kwargs):
        await asyncio.sleep(0.3)
        return {}

    async def fake_run(_spus, _document, _sheet, **_kwargs):
        return {}

    monkeypatch.setattr(webapp.activity_service, "scan_activity_matrix", slow_scan)
    monkeypatch.setattr(webapp.activity_service, "run_activity_batch", fake_run)
    webapp.activity_jobs.clear()
    with TestClient(webapp.app) as client:
        job_id = client.post("/activity/scan", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999"}).json()["job_id"]
        blocked = client.post("/activity/batch", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999", "dry_run": False})
        assert blocked.status_code == 409 and "提报页" in blocked.json()["error"]
        assert client.post("/activity/batch", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999"}).status_code == 200  # dry-run 放行

        assert client.post(f"/activity/batch/{job_id}/pause", json={"paused": True}).json()["paused"] is True
        assert client.post(f"/activity/batch/{job_id}/pause", json={"paused": False}).json()["paused"] is False
        assert client.post("/activity/batch/不存在/pause", json={}).status_code == 404

        ok = client.post(f"/activity/batch/{job_id}/skip", json={"spu": "111", "activity": "活动A"})
        assert ok.status_code == 200 and ok.json()["skipped"] == [["111", "活动A"]]
        webapp.activity_jobs[job_id].control.lock_cell("111", "活动A")  # 已填价的格子不能反悔
        refused = client.post(f"/activity/batch/{job_id}/skip", json={"spu": "111", "activity": "活动A"})
        assert refused.status_code == 409 and "已填价" in refused.json()["error"]
        assert client.post("/activity/batch/不存在/skip",
                           json={"spu": "111", "activity": "活动A"}).status_code == 404


def test_reopen_phase_refuses_pause_over_http(webapp, monkeypatch):
    """收尾阶段（重开流量）经 HTTP 也拒绝暂停：此刻流量已关，挂起会把商品留在无流量在售状态。"""
    async def slow_scan(_spus, _excel, _sheet, **_kwargs):
        await asyncio.sleep(0.3)
        return {}

    monkeypatch.setattr(webapp.activity_service, "scan_activity_matrix", slow_scan)
    webapp.activity_jobs.clear()
    with TestClient(webapp.app) as client:
        job_id = client.post("/activity/scan", json={
            "cloud_url": DOCUMENT, "sheet": "成本表", "spus": "999"}).json()["job_id"]
        webapp.activity_jobs[job_id].control.set_phase("reopen")
        refused = client.post(f"/activity/batch/{job_id}/pause", json={"paused": True})
        assert refused.status_code == 409 and "收尾阶段" in refused.json()["error"]


def test_matrix_endpoint_reads_todays_file_without_browser(webapp, monkeypatch):
    """刷新页面后矩阵回读：只读当天落盘文件，不连 CDP、不读成本表。"""
    from app.activity import matrix as matrix_store

    data = matrix_store.empty()
    data.update(
        document=DOCUMENT, sheet="成本表",
        activities=[{"name": "官方大促", "discount_rate": 0.9, "min_stock": 5}],
        price={"999": {"items": [], "cells": {"官方大促": {
            "verdict": "pass", "submit_price": 72.0, "floor_price": 65.0, "within_floor": True,
            "sku_count": 1, "skus": [{"label": "SKU-C", "daily": 80.0, "sale": 65.0,
                                      "submit_price": 72.0}], "note": ""}}}},
        eligibility={},
    )
    data["counts"] = matrix_store.counts(data)
    matrix_store.save(data)

    def _no_browser(*_args, **_kwargs):
        raise AssertionError("回读矩阵不该碰浏览器或成本表")

    monkeypatch.setattr(webapp.activity_service.pipeline, "read_activities", _no_browser)
    with TestClient(webapp.app) as client:
        body = client.get("/activity/matrix",
                          params={"cloud_url": DOCUMENT, "sheet": "成本表"}).json()
        assert body["fresh"] is True and body["document"] == DOCUMENT
        assert body["counts"] == {"cells": 1, "eligible": 0, "ineligible": 0, "unknown": 1}
        assert body["cells"][0]["submit_price"] == 72.0 and body["cells"][0]["eligible"] is None
        # 换了另一份文档/工作表 → 缓存不算新鲜，前端据此提示重新识别
        assert client.get("/activity/matrix",
                          params={"cloud_url": "https://www.kdocs.cn/l/other"}).json()["fresh"] is False
        assert client.get("/activity/matrix",
                          params={"cloud_url": DOCUMENT, "sheet": "别的表"}).json()["fresh"] is False
