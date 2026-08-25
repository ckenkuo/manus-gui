# -*- coding: utf-8 -*-
"""发布页 Web 层测试：/publish 页面渲染 + /publish/batch 的 price 透传。

为什么值得单测：这层原先一个渲染测试都没有，而 templates/publish.html 是手改的
（申报价输入框、栅格宽度、localStorage 回填三处），模板里少一个 id 前端就静默报
undefined、按钮点了没反应——跑一次批次才发现。另一半是 price 这条新参数的接线：
路由忘了收、或前端忘了发，表现都是「申报价怎么填都是 188.88」，日志上看不出异常。

app.py 与 app 包同名，普通 import 会被包遮蔽，故按文件路径加载（同 test_orders_web）。
"""
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def webapp():
    spec = importlib.util.spec_from_file_location("webapp_publish", str(ROOT / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_publish"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(webapp):
    return TestClient(webapp.app)


def test_publish_page_renders(client):
    r = client.get("/publish")
    assert r.status_code == 200
    assert "selStore" in r.text and "selSite" in r.text


def test_申报价输入框在页面上(client):
    """输入框 + 占位符 + 提交与回填三处都要在：少一处就是「填了不生效」。"""
    t = client.get("/publish").text
    assert 'id="inputPrice"' in t
    assert 'placeholder="188.88"' in t, "占位符要显示默认值，否则用户不知道留空填什么"
    assert 'price: $("inputPrice").value.trim()' in t, "提交时必须带上 price"
    assert "LS_PRICE" in t, "申报价要记住上次输入"


def test_控件行栅格不超过12列(client):
    """一排控件的 md 宽度合计必须 = 12。

    原先是 3+3+2+3+2=13（超一列会把「开始发布」挤到下一行），加申报价时把
    「从阶段续跑」收窄到 2 才腾出位置。这条断言防的是后续再加控件时又超宽。
    """
    import re
    t = client.get("/publish").text
    # 只取任务表单那一排：从 selStore 所在的 row 到该 row 结束
    start = t.index('<div class="row g-2 align-items-end mb-2">')
    end = t.index("按阶段指定模型：默认折叠", start)
    widths = [int(m) for m in re.findall(r'class="col-(?:6|12) col-md-(\d+)"', t[start:end])]
    assert sum(widths) == 12, f"控件行合计 {sum(widths)} 列（应为 12）：{widths}"


def test_batch_把price透传给service(client, webapp, monkeypatch):
    """路由必须把 price 原样交给 run_batch；不传时是空串（默认值由管线决定）。"""
    seen = {}

    async def _fake_run_batch(tasks, **kw):
        seen.update(kw)
        seen["tasks"] = tasks
        return {"ok": 1, "fail": 0, "batch": 1}

    monkeypatch.setattr(webapp.publish_service, "run_batch", _fake_run_batch)

    r = client.post("/publish/batch", json={
        "tasks": [{"url": "https://detail.1688.com/offer/1.html"}],
        "store": "Pawly", "site": "美国", "price": "66.5",
    })
    assert r.status_code == 200 and r.json().get("job_id")
    # 作业是 create_task 起的，消费一次 SSE 让它跑起来
    with client.stream("GET", f"/publish/batch/{r.json()['job_id']}/events") as s:
        for _ in s.iter_lines():
            break
    assert seen.get("price") == "66.5"


def test_batch_不给price时透传空串(client, webapp, monkeypatch):
    """前端留空 → 空串，不在 Web 层兜一份默认值（否则改口径要同步两处）。"""
    seen = {}

    async def _fake_run_batch(tasks, **kw):
        seen.update(kw)
        return {"ok": 0, "fail": 0, "batch": 1}

    monkeypatch.setattr(webapp.publish_service, "run_batch", _fake_run_batch)

    r = client.post("/publish/batch", json={
        "tasks": [{"rowid": "1", "info_path": "x.json"}],
        "store": "Pawly", "site": "美国",
    })
    with client.stream("GET", f"/publish/batch/{r.json()['job_id']}/events") as s:
        for _ in s.iter_lines():
            break
    assert seen.get("price") == ""


def test_自动发布开关在页面上(client):
    """开关 + 提交 + 记忆三处都要在：少一处就是「勾了不生效」或「每次要重勾」。"""
    t = client.get("/publish").text
    assert 'id="chkDoPublish"' in t
    # 默认勾选是产品决定（用户 2026-08-25 要求默认自动发布）
    i = t.index('id="chkDoPublish"')
    assert "checked" in t[i:i + 200], "自动发布开关必须默认勾选"
    assert 'do_publish: $("chkDoPublish").checked' in t, "提交时必须带上 do_publish"
    assert "LS_PUBLISH" in t, "开关状态要记住"
    assert 'id="publishHintText"' in t, "顶部提示条要随开关切换文案"


def test_自动发布默认不因localStorage缺失而关闭(client):
    """判据必须是「显式存过 0 才关」，不能用 truthy 判——读不到时会退成关，
    等于默认值随浏览器状态漂移（首次访问、隐私模式都读不到）。"""
    t = client.get("/publish").text
    assert 'localStorage.getItem(LS_PUBLISH) !== "0"' in t


def test_开着自动发布要先确认(client):
    """⑮ 不可逆且开关默认开，故必须有一次确认——这是默认值的配套约束。"""
    t = client.get("/publish").text
    i = t.index("async function startBatch()")
    body = t[i:i + 3000]
    assert "confirm(" in body and "chkDoPublish" in body


def test_batch_默认开启发布(client, webapp, monkeypatch):
    """不传 do_publish 时默认 True——UI 常规路径就是自动发布。"""
    seen = {}

    async def _fake_run_batch(tasks, **kw):
        seen.update(kw)
        return {"ok": 0, "fail": 0, "batch": 1}

    monkeypatch.setattr(webapp.publish_service, "run_batch", _fake_run_batch)
    r = client.post("/publish/batch", json={
        "tasks": [{"rowid": "1", "info_path": "x.json"}],
        "store": "Pawly", "site": "美国",
    })
    with client.stream("GET", f"/publish/batch/{r.json()['job_id']}/events") as s:
        for _ in s.iter_lines():
            break
    assert seen.get("do_publish") is True


def test_batch_关掉开关时不发布(client, webapp, monkeypatch):
    """显式 false 必须透传下去，否则关了开关照样上架——那是不可逆的误伤。"""
    seen = {}

    async def _fake_run_batch(tasks, **kw):
        seen.update(kw)
        return {"ok": 0, "fail": 0, "batch": 1}

    monkeypatch.setattr(webapp.publish_service, "run_batch", _fake_run_batch)
    r = client.post("/publish/batch", json={
        "tasks": [{"rowid": "1", "info_path": "x.json"}],
        "store": "Pawly", "site": "美国", "do_publish": False,
    })
    with client.stream("GET", f"/publish/batch/{r.json()['job_id']}/events") as s:
        for _ in s.iter_lines():
            break
    assert seen.get("do_publish") is False
