"""识别扫描（service.scan_activity_matrix）的用例。

不碰真浏览器：monkeypatch 成本表读取、活动列表、开提报页、资格探测。锁住的都是钱与时间：
活动列表只读一次、零候选的活动绝不开页、缓存命中不重探、fail-closed 的结论不落盘。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.activity import matrix as matrix_store
from app.activity import service, source

DOCUMENT = "https://www.kdocs.cn/l/test-costs"


def _cells(**values):
    return {column: {"text": str(value), "formula": ""} for column, value in values.items()}


def _snapshot(*rows):
    return {
        "cloud_url": DOCUMENT, "sheets": ["成本表"], "sheet": "成本表",
        "columns": [], "fields": {}, "warnings": [], "header_row": 6, "total": len(rows),
        "rows": list(rows),
    }


def _row(spu, label, daily, sale, issues=None, row_number=7):
    return {"row_number": row_number, "spu": spu, "sku": label, "daily": daily, "sale": sale,
            "purchase": "12", "values": {}, "issues": issues or [], "selectable": not issues}


@pytest.fixture
def pages(monkeypatch):
    """活动列表 + 开提报页 + 资格探测三处桩，返回调用记录。"""
    calls = {"open_page": [], "probe": [], "read_activities": 0}

    class _FakeEnrollPage:
        def __init__(self, name):
            self.name = name
            self.closed = False

        async def close(self):
            self.closed = True

    async def fake_read_activities(_page):
        calls["read_activities"] += 1
        return [
            {"name": "官方大促", "discount_rate": 0.9, "min_stock": 5},
            {"name": "65折档", "discount_rate": 0.65, "min_stock": None},
            {"name": "万人团", "discount_rate": None, "min_stock": None},
        ]

    async def fake_open_page(_activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        return _FakeEnrollPage(name)

    async def fake_probe(page, spu, on_step=None):
        # 记下探测时提报页是否已关：探测必须在页开着的时候做（关了就读不到搜索结果）
        calls["probe"].append((spu, page.closed))
        eligible = not spu.endswith("9")  # 固定判据：111 可报、其余不可报
        return {"queried": True, "detail_eligible": eligible,
                "failed_step": None, "note": "" if eligible else "详情页查询结果为 0"}

    monkeypatch.setattr(service.pipeline, "read_activities", fake_read_activities)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "probe_detail_eligibility", fake_probe)
    monkeypatch.setattr(service, "ensure_cdp_alive", AsyncMock(return_value=True))

    class _FakeActivityPage:
        pass

    return calls, _FakeActivityPage()


def _run(monkeypatch, pages, snapshot, spus="111 999", **kwargs):
    calls, activity_page = pages
    monkeypatch.setattr(source, "read_document", lambda *a, **k: snapshot)
    events = []
    result = asyncio.run(service.scan_activity_matrix(
        spus, DOCUMENT, "成本表", on_progress=events.append,
        activity_page=activity_page, **kwargs))
    return result, events, calls


def test_scan_reads_activities_once_and_skips_activities_without_candidates(monkeypatch, pages):
    """活动列表只读一次（不是每个 SPU 一次）；价格初筛零候选的活动绝不开页
    （活动页有近百个活动，为注定不报的格子开页是小时级浪费）。"""
    snapshot = _snapshot(_row("111", "默认", 60.0, 40.0), _row("999", "默认", 60.0, 59.0))
    result, events, calls = _run(monkeypatch, pages, snapshot)

    assert calls["read_activities"] == 1
    # 0.9 折：111 → 54 ≥ 40 过、999 → 54 < 59 不过；65折 两者都不过；万人团无折扣率
    assert calls["open_page"] == ["官方大促"]
    assert calls["probe"] == [("111", False)]  # 只探价格初筛通过的格子，且探测时页还开着
    assert result["counts"] == {"cells": 1, "eligible": 1, "ineligible": 0, "unknown": 0}

    start = next(e for e in events if e["type"] == "scan_start")
    assert start["activity_total"] == 3 and len(start["cells"]) == 6  # 2 SPU × 3 活动全量下发
    verdicts = {(c["spu"], c["activity"]): c["verdict"] for c in start["cells"]}
    assert verdicts[("999", "官方大促")] == "under_floor"   # 未达底价的格子也在矩阵里
    assert verdicts[("111", "万人团")] == "no_rate"
    cell = next(e for e in events if e["type"] == "activity_cell")
    assert cell["verdict"] == "eligible" and cell["from_cache"] is False
    done = next(e for e in events if e["type"] == "scan_done")
    assert done["eligible"] == 1 and done["probed"] == 1
    assert result["path"].endswith(f"activity-matrix-{matrix_store.today()}.json")


def test_scan_reuses_todays_eligibility_and_does_not_reprobe(monkeypatch, pages):
    """当天缓存命中即跳过探测（识别很贵）；`force=True` 时才全量重探。"""
    snapshot = _snapshot(_row("111", "默认", 60.0, 40.0))
    cached = matrix_store.empty()
    cached["eligibility"] = {"111": {"官方大促": {
        "eligible": False, "note": "详情页查询结果为 0", "scanned_at": "2026-09-24 10:00:00"}}}
    matrix_store.save(cached)

    result, events, calls = _run(monkeypatch, pages, snapshot, spus="111")
    assert calls["open_page"] == [] and calls["probe"] == []      # 命中缓存 → 一次页都没开
    assert result["counts"]["ineligible"] == 1 and result["from_cache"] == 1
    assert result["scanned"] == 0

    result, events, calls = _run(monkeypatch, pages, snapshot, spus="111", force=True)
    assert calls["open_page"] == ["官方大促"] and calls["probe"] == [("111", False)]
    assert result["scanned"] == 1


def test_scan_does_not_persist_fail_closed_probe(monkeypatch, pages):
    """探测 fail-closed（超时/非唯一）不落盘：一次卡顿不该被当成永久结论复用，下次重探。"""
    calls, activity_page = pages

    async def flaky_probe(page, spu, on_step=None):
        return {"queried": False, "detail_eligible": None, "failed_step": "query",
                "note": "搜索后定位行数=2（非唯一），保守跳过"}

    monkeypatch.setattr(service.pipeline, "probe_detail_eligibility", flaky_probe)
    snapshot = _snapshot(_row("111", "默认", 60.0, 40.0))
    result, events, _ = _run(monkeypatch, pages, snapshot, spus="111")

    cell = next(e for e in events if e["type"] == "activity_cell")
    assert cell["verdict"] == "probe_failed" and cell["eligible"] is None
    assert matrix_store.load()["eligibility"] == {}          # 没落盘
    assert result["counts"]["unknown"] == 1


def test_scan_reports_spu_missing_from_cost_table(monkeypatch, pages):
    """成本表里没有/整组无效的 SPU 不能整行消失：每格标 no_cost 并带上原因，
    否则操作者只会以为「没这个商品」。"""
    snapshot = _snapshot(_row("999", "SKU-A", 60.0, 40.0, issues=["同一 SPU 第 8 行数据无效，整组不可选"]))
    result, events, calls = _run(monkeypatch, pages, snapshot, spus="999")

    assert calls["read_activities"] == 1 and calls["open_page"] == []
    start = next(e for e in events if e["type"] == "scan_start")
    assert all(c["verdict"] == "no_cost" for c in start["cells"])
    assert "整组不可选" in start["cells"][0]["note"]


def test_scan_survives_one_activity_failing_to_open(monkeypatch, pages):
    """单个活动开页/探测异常不能让整轮识别白跑（识别是长跑且只读）：记 warning、继续下一个，
    已经探到的格子照常落盘。"""
    calls, activity_page = pages
    snapshot = _snapshot(_row("111", "默认", 60.0, 40.0))

    async def flaky_open(_activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        if name == "官方大促":
            raise RuntimeError("模拟开页炸了")
        return None

    monkeypatch.setattr(service.pipeline, "open_enroll_page", flaky_open)
    result, events, _ = _run(monkeypatch, pages, snapshot, spus="111")

    assert calls["open_page"] == ["官方大促"]  # 只有一个活动有候选，炸了就跳过
    warn = next(e for e in events if e["type"] == "log" and e["level"] == "warning")
    assert "官方大促" in warn["message"]
    assert result["counts"]["unknown"] == 1  # 该格仍是待识别，下次重探
    assert matrix_store.load()["eligibility"] == {}
