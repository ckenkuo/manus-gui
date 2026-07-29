# -*- coding: utf-8 -*-
"""订单登记 Web 层测试：/orders 页面 + /orders/config + /orders/batch(SSE)。

为什么值得单测：这层是「dry_run 默认 True」这条安全语义的最后一道闸。前端忘了传
dry_run、或路由把默认写成 False，就会在用户只想试跑时真写 102MB 的登记表。这里直接
断言「不传 dry_run 时 service 收到的是 True」。

app.py 与 app 包同名，普通 import 会被包遮蔽，故按文件路径加载。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def webapp():
    spec = importlib.util.spec_from_file_location("webapp", str(ROOT / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(webapp):
    return TestClient(webapp.app)


def test_orders_page_renders(client):
    r = client.get("/orders")
    assert r.status_code == 200
    # 安全语义必须出现在页面上：默认试跑的开关 + 不可逆告警
    assert "switchWrite" in r.text
    assert "正式写入" in r.text
    assert "试跑" in r.text


def test_index_has_orders_entry(client):
    assert 'href="/orders"' in client.get("/").text


def test_worklist_echoes_selection(client, monkeypatch, webapp):
    """首屏要回显可选工作簿/Sheet 与店铺候选，并如实报路径是否存在。"""
    svc = webapp.orders_service
    monkeypatch.setattr(
        svc, "load_orders_config",
        lambda: {
            "workbook": str(ROOT / "不存在的登记表.xlsx"),
            "dedupe_by": ["订单号", "尺码"],
            "sheet_map": [
                {"store": "StoreA", "sites": ["秘鲁"], "sheet": "StoreA全球1"},
                {"store": "StoreB", "sites": ["德国"], "sheet": "StoreB欧区"},
            ],
        },
    )
    monkeypatch.setattr(svc, "load_prefs", lambda: {})
    monkeypatch.setattr(svc, "list_workbooks", lambda: ["D:/a.xlsx"])
    monkeypatch.setattr(svc.WpsExcelTool, "list_sheets", classmethod(lambda cls, p: []))

    d = client.get("/orders/worklist").json()
    assert d["workbook_exists"] is False   # 路径不存在要如实回显，前端据此禁用「开始」
    assert d["dedupe_by"] == ["订单号", "尺码"]
    assert d["store_options"] == ["StoreA", "StoreB"]
    # 当前生效工作簿即便不在扫描目录里，也要并进候选，否则回显不出选中态
    assert d["workbook"] in d["workbooks"]
    assert d["sheet"] == "" and d["sheet_info"] == {}


def _sse_events(text: str):
    """把 SSE 原文拆成 [(event, data_dict)]。"""
    out = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln]
        ev = next((ln[7:] for ln in lines if ln.startswith("event: ")), "")
        data = next((ln[6:] for ln in lines if ln.startswith("data: ")), "{}")
        out.append((ev, json.loads(data)))
    return out


def _fake_runner(seen: dict):
    """替身 run_orders_batch：记录入参，推几个事件，返回可辨识的汇总。"""

    async def _run(store="", dry_run=True, workbook="", list_url="",
                   on_progress=None, max_pages=200, sheet="", require_price=False,
                   incremental=True):
        seen.update({"store": store, "dry_run": dry_run, "max_pages": max_pages,
                     "workbook": workbook, "sheet": sheet,
                     "require_price": require_price, "incremental": incremental})
        await on_progress({"type": "started", "dry_run": dry_run, "workbook": "W"})
        await on_progress({"type": "plan", "sheet": "S", "pending": 2, "dup": 1,
                           "no_key": False, "with_image": 2})
        summary = {"dry_run": dry_run, "aborted": "", "pending": 2, "written_rows": 0}
        await on_progress({"type": "done", **summary})
        return summary

    return _run


@pytest.fixture()
def no_prefs(monkeypatch, webapp):
    """屏蔽偏好落盘：单测不该往 workspace 写 orders_prefs.json。"""
    monkeypatch.setattr(webapp.orders_service, "save_prefs", lambda **kw: None)


def test_batch_defaults_to_dry_run(client, monkeypatch, webapp, no_prefs):
    """不传 dry_run 时必须是试跑——这条默认值错了就会误写登记表。"""
    seen: dict = {}
    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _fake_runner(seen))
    job_id = client.post("/orders/batch", json={}).json()["job_id"]
    events = _sse_events(client.get(f"/orders/batch/{job_id}/events").text)
    assert seen["dry_run"] is True
    assert seen["max_pages"] == 200
    assert seen["require_price"] is False, "默认允许成交价为空，无价订单照常登记"
    assert seen["incremental"] is True, "默认增量采集（单表模式下才真正生效）"
    names = [ev for ev, _ in events]
    # service 的收尾 done 转发成 batch_done，SSE 自己的 done 只出现一次
    assert "batch_done" in names
    assert names.count("done") == 1
    assert names[-1] == "done"
    assert events[-1][1]["dry_run"] is True


def test_batch_passes_selection(client, monkeypatch, webapp, no_prefs):
    """页面上选的店铺/工作簿/Sheet 必须原样传到 service。"""
    seen: dict = {}
    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _fake_runner(seen))
    r = client.post("/orders/batch", json={
        "store": "StoreA", "dry_run": False, "max_pages": 3,
        "workbook": "D:/wb.xlsx", "sheet": "StoreA全球1",
    })
    client.get(f"/orders/batch/{r.json()['job_id']}/events")
    assert seen == {
        "store": "StoreA", "dry_run": False, "max_pages": 3,
        "workbook": "D:/wb.xlsx", "sheet": "StoreA全球1",
        "require_price": False, "incremental": True,
    }


def test_batch_incremental_can_be_disabled(client, monkeypatch, webapp, no_prefs):
    """页面上关掉增量开关要原样传到 service。"""
    seen: dict = {}
    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _fake_runner(seen))
    r = client.post("/orders/batch", json={"incremental": False})
    client.get(f"/orders/batch/{r.json()['job_id']}/events")

    assert seen["incremental"] is False


def test_batch_require_price_opt_in(client, monkeypatch, webapp, no_prefs):
    """显式传 allow_no_price=False 才恢复「无价留到下批」的严格口径。"""
    seen: dict = {}
    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _fake_runner(seen))
    r = client.post("/orders/batch", json={"allow_no_price": False})
    client.get(f"/orders/batch/{r.json()['job_id']}/events")

    assert seen["require_price"] is True


def test_batch_remembers_selection(client, monkeypatch, webapp):
    """选择要落盘成偏好，下次开页回填。"""
    saved: dict = {}
    monkeypatch.setattr(webapp.orders_service, "save_prefs",
                        lambda **kw: saved.update(kw))
    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _fake_runner({}))
    r = client.post("/orders/batch", json={
        "store": "StoreB", "workbook": "D:/wb.xlsx", "sheet": "StoreB欧区",
    })
    client.get(f"/orders/batch/{r.json()['job_id']}/events")
    assert saved == {"store": "StoreB", "workbook": "D:/wb.xlsx", "sheet": "StoreB欧区"}


def test_batch_exception_surfaces_as_aborted(client, monkeypatch, webapp, no_prefs):
    """service 抛异常不能让 SSE 干等：要收到 aborted 再收 done。"""

    async def _boom(**kwargs):
        raise RuntimeError("CDP 挂了")

    monkeypatch.setattr(webapp.orders_service, "run_orders_batch", _boom)
    job_id = client.post("/orders/batch", json={}).json()["job_id"]
    events = _sse_events(client.get(f"/orders/batch/{job_id}/events").text)
    assert events[0][0] == "aborted"
    assert "CDP 挂了" in events[0][1]["reason"]
    assert events[-1][0] == "done"


def test_events_unknown_job(client):
    events = _sse_events(client.get("/orders/batch/nope/events").text)
    assert events == [("error", {"reason": "job not found"})]
