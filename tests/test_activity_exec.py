"""活动管理执行遍（关流量→报名→开流量）编排单测。

不碰真浏览器：monkeypatch pipeline 的变更函数为 fake，断言编排顺序、活动维度分组、
live 门控（半程 live=False 只填不提交/不真关开；全程 live=True 才不可逆）。
"""
import asyncio
from pathlib import Path

import pytest

from app.activity import service


class FakePage:
    """占位页面对象（执行遍里只作为句柄透传给被 patch 的 pipeline 函数）。"""
    def __init__(self, tag=""):
        self.tag = tag
        self.closed = False
        self.context = self

    async def close(self):
        self.closed = True


def test_activity_table_uses_per_activity_status_cells():
    """计划表状态必须按 SPU+活动逐行维护，不能再把整个 SPU 状态 rowspan 合并。"""
    html = Path("templates/activity.html").read_text(encoding="utf-8")

    assert "let activityStates = {};" in html
    assert "activityStatusBadge(spu, p.activity)" in html
    assert 'type === "exec_log_verify"' in html
    assert "结果页成功·待记录" in html
    assert '<td rowspan="${span}">${planStatusBadge(spu)}</td>' not in html


def test_activity_page_visualizes_accel_close_and_open_flow():
    """流量关闭/开启不能只写滚动日志，须有按 SPU 的独立实时状态面板。"""
    html = Path("templates/activity.html").read_text(encoding="utf-8")

    assert 'id="accelFlowBody"' in html
    assert "let accelStates = {};" in html
    assert "function renderAccelFlow()" in html
    assert 'type === "exec_accel_step"' in html
    assert "关闭前按 SPU 确认" in html and "开启前按 SPU 确认" in html
    assert "正在关闭" in html and "无需重复开启" in html
    assert "accel-flow-note" in html
    assert "无成功报名活动，不新增开启流量" in html


def test_activity_log_search_waits_for_repaint_and_uses_editable_field():
    """报名记录页切全球后可能重绘；搜索 marker 必须按可编辑 placeholder 重试。"""
    script = pipeline._MARK_ACTIVITY_LOG_SPU_JS

    assert "多个|空格|逗号" in script
    assert "重绘成" in script
    assert "labelRect" in script
    assert "for _ in range(12)" in __import__("inspect").getsource(
        pipeline.read_activity_log_records
    )


def test_site_notification_panel_closer_is_scoped_to_all_messages():
    """站点通知面板关闭器必须先锁定“全部消息”面板，不能全页乱点 X。"""
    script = pipeline._CLOSE_SITE_NOTIFICATION_JS
    assert "全部消息" in script
    assert "startsWith('全部消息')" in script
    assert 'data-testid="beast-core-icon-close"' in script
    assert "dispatchEvent(new MouseEvent('click'" in script
    assert "[role=\"img\"]" in script
    assert "panelRect.right - 60" in script
    assert "trigger(close)" in script


class FakeConnectedPage(FakePage):
    def __init__(self, url="about:blank"):
        super().__init__(url)
        self.url = url
        self.goto_calls = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url


class FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])

    async def new_page(self):
        page = FakeConnectedPage()
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    async def close(self):
        self.closed = True


def _patch_cdp(monkeypatch, pages):
    context = FakeContext(pages)
    browser = FakeBrowser(context)

    class FakeChromium:
        async def connect_over_cdp(self, _url):
            return browser

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
            self.stopped = False

        async def stop(self):
            self.stopped = True

    playwright = FakePlaywright()

    class FakeStarter:
        async def start(self):
            return playwright

    monkeypatch.setattr(service, "async_playwright", lambda: FakeStarter())
    return context, browser, playwright


def test_connect_pages_opens_all_missing_tabs(monkeypatch):
    """流量/活动/商品页都缺失时自动打开，并全部标为本批 owned 页签。"""
    context, browser, playwright = _patch_cdp(monkeypatch, [])
    dismissed = []

    async def fake_dismiss(page):
        dismissed.append(page.url)
        return True

    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", fake_dismiss)
    result = asyncio.run(service._connect_pages("http://localhost:9222"))
    pw, got_browser, flux, activity, goods, owned = result

    assert pw is playwright and got_browser is browser
    assert [page.url for page in owned] == [
        service.pipeline.FLUX_URL,
        service.pipeline.ACTIVITY_URL,
        service.pipeline.GOODS_LIST_URL,
    ]
    assert (flux, activity, goods) == tuple(owned)
    assert dismissed == [page.url for page in owned]
    assert len(context.pages) == 3


def test_connect_pages_ignores_existing_tabs_and_opens_owned_tabs(monkeypatch):
    """已有页签状态不受管线控制；任务必须忽略它们并新建三个专用页签。"""
    existing = [
        FakeConnectedPage(service.pipeline.FLUX_URL),
        FakeConnectedPage(service.pipeline.ACTIVITY_URL),
        FakeConnectedPage(service.pipeline.GOODS_LIST_URL),
    ]
    context, _, _ = _patch_cdp(monkeypatch, existing)
    result = asyncio.run(service._connect_pages("http://localhost:9222"))
    _, _, flux, activity, goods, owned = result

    assert (flux, activity, goods) == tuple(owned)
    assert [page.url for page in owned] == [
        service.pipeline.FLUX_URL,
        service.pipeline.ACTIVITY_URL,
        service.pipeline.GOODS_LIST_URL,
    ]
    assert all(page not in existing for page in owned)
    assert all(page.goto_calls == [] for page in existing)
    assert all(page.closed is False for page in existing)
    assert len(context.pages) == 6


def _plan(spu, activities, accel_will_close, sale=46.5, accel_state=None):
    """构造一条规划遍结果（status=done）。activities: [(名, 申报价)]。sale=销售底价（重开
    加速器时加速价=sale+1）。"""
    if accel_state is None:
        accel_state = "on" if accel_will_close else "off"
    return {
        "spu": spu, "status": "done", "accel_will_close": accel_will_close, "sale": sale,
        "accel_state": accel_state,
        "enrolled_activities": [{"activity": n, "submit_price": p} for n, p in activities],
    }


def _patch_log(monkeypatch, pairs=(), complete=True):
    async def fake_read_log(_context, spus):
        records = [
            {
                "spu": str(spu), "activity": activity, "success": True,
                "enroll_status": 4, "enroll_id": f"{spu}-{activity}",
            }
            for spu, activity in pairs
        ]
        return {
            "records": records, "complete": complete,
            "queries": [{"spu": str(spu)} for spu in spus], "note": "测试记录页",
        }

    monkeypatch.setattr(service.pipeline, "read_activity_log_records", fake_read_log)


def _run(results, live, monkeypatch, runtime_states=None):
    """跑执行遍，返回 (summary, events, calls)。calls 记录每个变更函数的调用参数。"""
    calls = {
        "close": [], "open": [], "open_page": [], "fill": [], "submit": [],
        "read_log": [], "sequence": [],
    }
    events = []
    planned_states = {result["spu"]: result.get("accel_state", "unknown") for result in results}
    effective_states = {**planned_states, **(runtime_states or {})}

    async def fake_read_accel_state(_page, spu, search=False):
        return effective_states.get(spu, "unknown")

    async def fake_close(page, spu, allow=False):
        calls["sequence"].append("close")
        calls["close"].append((spu, allow))
        return {"state": "on", "closed": allow, "note": "半程" if not allow else "已停止"}

    async def fake_open(page, spu, allow=False, accel_price=None):
        calls["open"].append((spu, allow))
        calls.setdefault("open_price", []).append((spu, accel_price))
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        calls["sequence"].append("open_page")
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        calls["fill"].append((page.tag, spu, act, price, allow_submit))
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False):
        calls["submit"].append((page.tag, allow))
        return {"submitted": allow, "note": "已提交" if allow else "半程"}

    async def fake_read_log(_context, _spus):
        calls["read_log"].append(list(_spus))
        calls["sequence"].append("read_log")
        records = []
        if live:
            for plan in results:
                for item in plan.get("enrolled_activities", []):
                    records.append({
                        "spu": plan["spu"], "activity": item["activity"],
                        "success": True, "enroll_status": 4,
                        "enroll_id": f"{plan['spu']}-{item['activity']}",
                    })
        return {
            "records": records, "complete": True,
            "queries": [
                {"spu": str(spu), "total": sum(record["spu"] == str(spu) for record in records)}
                for spu in _spus
            ],
            "note": "测试记录",
        }

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "read_accel_state", fake_read_accel_state)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    monkeypatch.setattr(service.pipeline, "read_activity_log_records", fake_read_log)

    async def on_progress(ev):
        events.append(ev)

    summary = asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), live, on_progress)
    )
    return summary, events, calls


def test_groups_by_activity_across_spus(monkeypatch):
    """两个 SPU 都报同一活动 A，另有活动 B：A 应只开一次提报页、填两次、提交一次。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 20.0)], accel_will_close=False),
        _plan("222", [("活动A", 11.0)], accel_will_close=False),
    ]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    # A、B 各开一次提报页
    assert calls["open_page"] == ["活动A", "活动B"]
    # 活动A 填两次（111、222），活动B 填一次（111）
    a_fills = [c for c in calls["fill"] if c[2] == "活动A"]
    b_fills = [c for c in calls["fill"] if c[2] == "活动B"]
    assert {c[1] for c in a_fills} == {"111", "222"}
    assert {c[1] for c in b_fills} == {"111"}
    # 每活动提交一次
    assert len(calls["submit"]) == 2


def test_live_reads_activity_log_baseline_before_enrollment(monkeypatch):
    """正式执行先确认/关闭流量，再保存 /log total，之后才打开首个提报页。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]

    summary, _events, calls = _run(results, live=True, monkeypatch=monkeypatch)

    assert calls["read_log"] == [["111"], ["111"]]
    assert calls["sequence"].index("close") < calls["sequence"].index("read_log")
    assert calls["sequence"].index("read_log") < calls["sequence"].index("open_page")
    assert summary["log_baseline"]["queries"] == [{"spu": "111", "total": 1}]


def test_each_enroll_page_closed_after_activity(monkeypatch):
    """逐个活动：报完（无论成/败）都立刻关掉该提报页 tab，保证任意时刻只有一个 detail-new。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 20.0)], accel_will_close=False),
    ]
    opened = []

    async def fake_close(page, spu, allow=False):
        return {"state": "off", "closed": True, "note": ""}

    async def fake_open(page, spu, allow=False, accel_price=None):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        pg = FakePage(f"enroll:{name}")
        opened.append(pg)
        return pg

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        # 报名前该页必须是打开的（未被提前关）
        assert page.closed is False
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False):
        assert page.closed is False  # 提交时页仍开着
        return {"submitted": allow, "note": ""}

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)

    async def on_progress(ev):
        pass

    asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), True, on_progress)
    )
    # 两个活动各开一个提报页，且都在处理完后被关闭
    assert len(opened) == 2
    assert all(pg.closed for pg in opened)


def test_build_over_ref_note_backsolves_suggested_daily_price():
    """超参考价失败提示应反推平台认可日常价（参考价/折扣率），并与当前 Excel 日常价并排给出。"""
    # 实机数据：限时秒杀 8.5 折，申报价 7.0、参考价 6.65，Excel 日常价 8.24。
    over = pipeline.build_over_ref_note(7.0, 6.65, 8.24, 0.85)
    assert over["suggested_daily_price"] == 7.82  # 6.65 / 0.85
    assert over["current_daily_price"] == 8.24
    assert "平台认可日常价约 7.82" in over["note"]
    assert "当前 Excel 日常价 8.24" in over["note"]
    assert "7.0 高于提报页参考价 6.65" in over["note"]


def test_build_over_ref_note_without_discount_falls_back():
    """折扣率缺失/非法时不反推，退回原「请核对 Excel 日常价」文案，不得抛错或给错误建议。"""
    over = pipeline.build_over_ref_note(7.0, 6.65, 8.24, None)
    assert over["suggested_daily_price"] is None
    assert "疑 Excel 日常价与商品前端实际售价不一致" in over["note"]
    zero = pipeline.build_over_ref_note(7.0, 6.65, 8.24, 0)
    assert zero["suggested_daily_price"] is None


def test_over_ref_recorded_as_failed(monkeypatch):
    """填价时申报价超过提报页参考价（Excel 与前端售价不一致）→ 记入 summary['failed']
    并带原因，exec_fill/exec_done 事件如实回报，绝不误报成功。"""
    results = [_plan("111", [("活动A", 52.57)], accel_will_close=False)]
    events = []

    async def fake_close(page, spu, allow=False):
        return {"state": "off", "closed": True, "note": "no-op"}

    async def fake_open(page, spu, allow=False, accel_price=None):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        # 模拟超参考价：未填成功、over_ref 标记 + 参考价 + 原因
        return {"filled": False, "submitted": False, "over_ref": True,
                "ref_price": 47.31,
                "note": f"申报价 {price} 高于提报页参考价 47.31（疑 Excel 日常价与前端实际售价不一致）"}

    async def fake_submit(page, allow=False):
        return {"submitted": False, "note": "无已填 SPU"}

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    _patch_log(monkeypatch)

    async def on_progress(ev):
        events.append(ev)

    summary = asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), True, on_progress)
    )
    # failed 里逐条记录了该商品/活动/参考价/原因
    assert len(summary["failed"]) == 1
    f = summary["failed"][0]
    assert f["spu"] == "111" and f["activity"] == "活动A"
    assert f["over_ref"] is True and f["ref_price"] == 47.31 and "参考价" in f["reason"]
    # exec_fill 事件 ok=False 且带 over_ref
    fill_ev = next(e for e in events if e["type"] == "exec_fill")
    assert fill_ev["ok"] is False and fill_ev["over_ref"] is True
    # exec_done 带 failed 明细
    done_ev = next(e for e in events if e["type"] == "exec_done")
    assert done_ev["failed"] and done_ev["failed"][0]["spu"] == "111"


def test_empty_fill_skips_submit(monkeypatch):
    """某活动无任何 SPU 填成功（如搜索 0 行）→ 不调 submit_enroll_page（避免点 disabled
    「提交」按钮超时抛异常中断执行遍）。"""
    results = [_plan("111", [("活动A", 47.31)], accel_will_close=False)]
    submit_called = []

    async def fake_close(page, spu, allow=False):
        return {"state": "off", "closed": True, "note": "no-op"}

    async def fake_open(page, spu, allow=False, accel_price=None):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        return {"filled": False, "submitted": False, "note": "搜索后定位行数=0（非唯一），保守跳过"}

    async def fake_submit(page, allow=False):
        submit_called.append(allow)
        return {"submitted": allow, "note": ""}

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    _patch_log(monkeypatch)

    events = []

    async def on_progress(ev):
        events.append(ev)

    summary = asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), True, on_progress)
    )
    assert submit_called == []  # 空填 → 不提交
    en = next(e for e in events if e["type"] == "exec_enroll")
    assert en["ok"] is False and "记录后继续" in en["note"]
    assert summary["failed"] and summary["failed"][0]["spu"] == "111"


def test_first_activity_rpa_failure_continues_scanning_without_open(monkeypatch):
    """首活动 RPA 失败后记录原因并继续扫描；最终无成功记录时初始 off 不开流量。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False),
    ]
    calls = {"open_page": [], "submit": [], "open_accel": []}

    async def fake_open_page(activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        return {"filled": False, "submitted": False, "failed_step": "query",
                "note": "查询结果为 0 行"}

    async def fake_submit(page, allow=False):
        calls["submit"].append(allow)
        return {"submitted": allow, "note": ""}

    async def fake_open_accel(page, spu, allow=False, accel_price=None):
        calls["open_accel"].append(spu)
        return {"opened": allow, "note": ""}

    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open_accel)
    _patch_log(monkeypatch)

    async def on_progress(_event):
        pass

    summary = asyncio.run(
        service._run_execution_phases(
            results, FakePage("flux"), FakePage("activity"), True, on_progress
        )
    )

    assert calls["open_page"] == ["活动A", "活动B"]
    assert calls["submit"] == []
    assert calls["open_accel"] == []
    assert len(summary["scan_failures"]) == 2
    assert "halted" not in summary


def test_unverified_submit_uses_log_and_continues(monkeypatch):
    """提交反馈不明确时继续扫描；最终记录页两项成功后允许初始 off 商品开流量。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False),
    ]
    calls = {"open_page": [], "open_accel": []}

    async def fake_open_page(_activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(_page, _spu, _act, _price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        return {"filled": True, "detail_eligible": True, "note": "已填价"}

    async def fake_submit(_page, allow=False):
        return {
            "submitted": True, "verified": False,
            "note": "已点击提交并确认，未捕获明确结果提示",
        }

    async def fake_open_accel(_page, spu, allow=False, accel_price=None):
        calls["open_accel"].append(spu)
        return {"opened": allow, "note": ""}

    async def fake_read_accel_state(_page, _spu, search=False):
        return "off"

    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open_accel)
    monkeypatch.setattr(service.pipeline, "read_accel_state", fake_read_accel_state)
    _patch_log(monkeypatch, [("111", "活动A"), ("111", "活动B")])

    async def on_progress(_event):
        return None

    summary = asyncio.run(
        service._run_execution_phases(
            results, FakePage("flux"), FakePage("activity"), True, on_progress
        )
    )

    assert calls["open_page"] == ["活动A", "活动B"]
    assert calls["open_accel"] == ["111"]
    assert summary["enrolled_activities"]["活动A"] == ["111"]
    assert summary["enrolled_activities"]["活动B"] == ["111"]
    assert summary["failed"] == []


def test_success_feedback_without_log_record_fails_after_full_scan(monkeypatch):
    """即使提交提示成功也扫描所有活动；记录页没有成功记录时最终仍失败且不开流量。"""
    plan = _plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False)
    calls = {"open_page": [], "open_accel": []}

    async def fake_open_page(_activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(_page, _spu, _act, _price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        return {"filled": True, "detail_eligible": True, "note": "已填价"}

    async def fake_submit(_page, allow=False):
        return {"submitted": True, "verified": True, "note": "报名成功"}

    async def fake_open_accel(_page, spu, allow=False, accel_price=None):
        calls["open_accel"].append(spu)
        return {"opened": allow, "note": ""}

    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open_accel)
    _patch_log(monkeypatch)

    async def on_progress(_event):
        return None

    summary = asyncio.run(
        service._run_execution_phases(
            [plan], FakePage("flux"), FakePage("activity"), True, on_progress
        )
    )

    assert calls["open_page"] == ["活动A", "活动B"]
    assert calls["open_accel"] == []
    assert summary["enrolled_activities"]["活动A"] == []
    assert summary["enrolled_activities"]["活动B"] == []
    assert len(summary["failed"]) == 2
    assert "halted" not in summary


def test_detail_ineligible_skips_activity_and_continues(monkeypatch):
    """详情查询 0 行是业务不符合：跳过当前活动、继续下一个；后续成功后允许初始 off 开流量。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False),
    ]
    calls = {"open_page": [], "submit": [], "open_accel": []}

    async def fake_open_page(activity_page, name, timeout_s=25):
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        if act == "活动A":
            return {"filled": False, "submitted": False, "detail_eligible": False,
                    "note": "详情页查询结果为 0"}
        return {"filled": True, "submitted": False, "detail_eligible": True, "note": ""}

    async def fake_submit(page, allow=False):
        calls["submit"].append((page.tag, allow))
        return {"submitted": allow, "note": "已点击提交"}

    async def fake_open_accel(page, spu, allow=False, accel_price=None):
        calls["open_accel"].append(spu)
        return {"opened": allow, "note": ""}

    async def fake_read_accel_state(_page, _spu, search=False):
        return "off"

    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open_accel)
    monkeypatch.setattr(service.pipeline, "read_accel_state", fake_read_accel_state)
    _patch_log(monkeypatch, [("111", "活动B")])

    async def on_progress(_event):
        pass

    summary = asyncio.run(
        service._run_execution_phases(
            results, FakePage("flux"), FakePage("activity"), True, on_progress
        )
    )

    assert calls["open_page"] == ["活动A", "活动B"]
    assert calls["submit"] == [("enroll:活动B", True)]
    assert calls["open_accel"] == ["111"]
    assert summary["ineligible_activities"] == {"活动A": ["111"]}
    assert summary["enrolled_activities"]["活动B"] == ["111"]
    assert "halted" not in summary


def test_enroll_exception_still_reopens(monkeypatch):
    """报名遍抛异常 → 阶段三重开流量仍照跑（否则阶段一关掉的流量永久留在关闭）。"""
    results = [_plan("111", [("活动A", 47.31)], accel_will_close=True)]
    calls = {"open": []}

    async def fake_close(page, spu, allow=False):
        return {"state": "on", "closed": allow, "note": "已停止"}

    async def fake_open(page, spu, allow=False, accel_price=None):
        calls["open"].append((spu, allow, accel_price))
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        raise RuntimeError("模拟报名遍炸了")

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)

    async def on_progress(ev):
        pass

    summary = asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), True, on_progress)
    )
    # 关成功 → 111 记入 closed；报名遍抛错被吞、记 errors；阶段三仍重开 111
    assert "111" in summary["closed"]
    assert any("报名遍异常" in e for e in summary["errors"])
    assert calls["open"] == [("111", True, 47.5)]  # 底价（_plan 默认 sale=46.5）+1
    assert "111" in summary["reopened"]


def test_semi_run_does_not_submit_or_close(monkeypatch):
    """半程 live=False：填价时 allow_submit 恒 False；提交/关/开的 allow 恒 False（不可逆动作不触发）。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    summary, events, calls = _run(results, live=False, monkeypatch=monkeypatch)
    assert all(c[4] is False for c in calls["fill"])          # enroll allow_submit 全 False
    assert all(allow is False for _, allow in calls["submit"])  # submit 不点
    assert all(allow is False for _, allow in calls["close"])   # close 不真关
    assert all(allow is False for _, allow in calls["open"])    # open 不真开


def test_live_run_is_irreversible(monkeypatch):
    """全程 live=True：提交/关/开的 allow 恒 True（真提交、真关、真开）。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    assert calls["submit"] and all(allow is True for _, allow in calls["submit"])
    assert calls["close"] == [("111", True)]
    # 只重开我们关掉的（live 下 summary["closed"] 含 111）
    assert calls["open"] == [("111", True)]
    # 重开时加速价=底价+1（sale 默认 46.5 → 47.5）
    assert calls["open_price"] == [("111", 47.5)]
    assert "111" in summary["closed"]
    close_check = next(
        event for event in events
        if event["type"] == "exec_accel_step" and event["phase"] == "close"
        and event["step"] == "checking"
    )
    open_check = next(
        event for event in events
        if event["type"] == "exec_accel_step" and event["phase"] == "open"
        and event["step"] == "checking"
    )
    assert close_check["spu"] == "111" and open_check["spu"] == "111"


def test_initially_off_is_opened_after_enroll(monkeypatch):
    """初始关闭的 SPU 不需要关，但报名后也应开启流量加速。"""
    results = [
        _plan("111", [("活动A", 10.0)], accel_will_close=True),
        _plan("222", [("活动A", 11.0)], accel_will_close=False),  # 本就 off，不关但要开
    ]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    assert calls["close"] == [("111", True)]
    assert calls["open"] == [("111", True), ("222", True)]
    off_check = next(event for event in events if event["type"] == "exec_close" and event["spu"] == "222")
    assert off_check["ok"] is True and off_check["already_off"] is True
    assert off_check["state"] == "off" and "无需关闭" in off_check["note"]


def test_execution_rechecks_stale_off_state_before_enrollment(monkeypatch):
    """规划时 off、执行时已变 on：必须按临场状态关闭并在报名后恢复，不能使用旧状态。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=False)]

    summary, _events, calls = _run(
        results, live=True, monkeypatch=monkeypatch, runtime_states={"111": "on"}
    )

    assert calls["close"] == [("111", True)]
    assert summary["closed"] == ["111"]
    assert calls["open"] == [("111", True)]
    assert results[0]["accel_state"] == "on"


def test_unknown_accel_state_is_not_opened(monkeypatch):
    """初始状态未知时保守不关也不开，避免误操作。"""
    results = [
        _plan("111", [("活动A", 10.0)], accel_will_close=False, accel_state="unknown"),
    ]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    assert calls["close"] == []
    assert calls["open"] == []


def test_skip_plans_not_executed(monkeypatch):
    """skip_nomatch / 无入选活动的结果不进入执行遍。"""
    results = [
        {"spu": "111", "status": "skip_nomatch", "enrolled_activities": []},
        {"spu": "222", "status": "done", "enrolled_activities": []},  # done 但空计划
    ]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    assert calls["open_page"] == [] and calls["fill"] == []
    assert summary["closed"] == [] and summary["reopened"] == []


# ---- 24h 冷却提示检测（_detect_cooldown_toast）----
from app.activity import pipeline


class FakeToastPage:
    """假页面：evaluate 时对 body 文本做与真实 JS 等价的关键字判定。"""
    def __init__(self, body_text):
        self.body = body_text

    async def evaluate(self, js, arg=None):
        keys = arg or []
        txt = self.body
        if not any(k in txt for k in keys):
            return ""
        for line in txt.split("\n"):
            if any(k in line for k in keys) and ("关闭" in line or "开启" in line or "加速" in line):
                return " ".join(line.split())[:60]
        return "命中24小时冷却提示"


def test_cooldown_toast_detected():
    """弹「加速器开启后需满24小时才可手动关闭」→ 检出该文案。"""
    page = FakeToastPage("其它内容\n加速器开启后需满24小时才可手动关闭，请耐心等待\n底部")
    hit = asyncio.run(pipeline._detect_cooldown_toast(page, tries=1))
    assert "24小时" in hit and "关闭" in hit


def test_cooldown_toast_absent():
    """无冷却提示 → 返回空串（不误判）。"""
    page = FakeToastPage("流量加速状态：进行中\n停止流量加速\n流量加权档位")
    hit = asyncio.run(pipeline._detect_cooldown_toast(page, tries=1))
    assert hit == ""


def test_close_request_waits_until_spinner_stops(monkeypatch):
    """弹窗消失后继续等短暂宽限期，捕获稍后出现的成功 toast。"""
    class FakeClosePage:
        def __init__(self):
            self.states = iter([
                {"dialog_open": True, "loading": False, "status": "pending", "message": ""},
                {"dialog_open": True, "loading": True, "status": "pending", "message": ""},
                {"dialog_open": True, "loading": True, "status": "pending", "message": ""},
                {"dialog_open": False, "loading": False, "status": "pending", "message": ""},
                {"dialog_open": False, "loading": False, "status": "pending", "message": ""},
                {"dialog_open": False, "loading": False, "status": "pending", "message": ""},
                {"dialog_open": False, "loading": False, "status": "success", "message": "停止成功"},
            ])

        async def evaluate(self, _script):
            return next(self.states)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(pipeline._wait_close_request_completion(FakeClosePage(), tries=8))

    assert result["settled"] is True
    assert result["saw_loading"] is True
    assert result["status"] == "success"
    assert result["polls"] == 7


def test_close_request_captures_cooldown_toast():
    """转圈期间出现 24 小时提示时立即判为冷却拦截。"""
    class FakeClosePage:
        async def evaluate(self, _script):
            return {
                "dialog_open": True,
                "loading": True,
                "status": "cooldown",
                "message": "加速器开启后需满24小时才可手动关闭",
            }

    result = asyncio.run(pipeline._wait_close_request_completion(FakeClosePage(), tries=2))

    assert result["settled"] is True
    assert result["status"] == "cooldown"
    assert "24小时" in result["message"]


def test_super_tier_selector_supports_image_label_cards():
    """档位名画进背景图时，仍按三张价格卡从左到右选择最右侧超级档。"""
    selector = pipeline._SELECT_SUPER_TIER_JS
    assert "对应申报价格" in selector
    assert "getComputedStyle(node).cursor === 'pointer'" in selector
    assert "cards.length >= 3" in selector
    assert "cards[cards.length - 1].node.click()" in selector


def test_select_super_tier_waits_for_async_cards(monkeypatch):
    """抽屉先打开、档位卡后挂载时，轮询到第三次再成功。"""
    class FakeTierPage:
        def __init__(self):
            self.calls = 0

        async def evaluate(self, _script):
            self.calls += 1
            return self.calls == 3

    async def no_wait(_seconds):
        return None

    page = FakeTierPage()
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    selected = asyncio.run(pipeline._select_super_tier(page, tries=4))

    assert selected is True
    assert page.calls == 3


def test_open_accel_retries_same_final_button(monkeypatch):
    """前两次捕获火爆提示继续点，第三次捕获成功提示立即停止。"""
    calls = []
    feedback = iter([
        {"status": "busy", "message": "活动太火爆，请稍后再试"},
        {"status": "busy", "message": "活动太火爆，请稍后再试"},
        {"status": "success", "message": "流量加速成功"},
    ])

    async def fake_click(page, names):
        calls.append(names)
        return names == ("立即加速",)

    async def fake_feedback(page, tries=32):
        return next(feedback)

    monkeypatch.setattr(pipeline, "_click_first_button", fake_click)
    monkeypatch.setattr(pipeline, "_wait_accel_submit_feedback", fake_feedback)
    result = asyncio.run(pipeline._click_open_with_busy_retries(object()))

    assert result["clicks"] == 3
    assert result["status"] == "success"
    assert result["message"] == "流量加速成功"
    assert calls.count(("立即加速",)) == 3


def _patch_open_accel_full_path(monkeypatch, feedback, read_states):
    """把 _open_accel_once 完整路径里的页面交互全打桩，只保留最终判定逻辑受测。

    read_states：read_accel_state 每次调用按顺序返回的状态（precheck 先取一个 off，
    之后 toast unknown 兜底回读再取一个）。feedback：最终点「立即加速」后的反馈字典。
    """
    states = iter(read_states)

    async def fake_dismiss(_page):
        return False

    async def fake_read(_page, _spu, search=False):
        return next(states)

    async def fake_mark(_page, _arg=None):
        return True

    async def fake_click_marked(_page):
        return True

    async def fake_select_super(_page, tries=6):
        return True

    async def fake_set_prices(_page, _accel_price):
        return {"rows_total": 1, "rows_over": 0, "rows_filled": 1, "over_detail": ""}

    async def fake_click_first(_page, _names):
        return True

    async def fake_evaluate(_script, _arg=None):
        return True  # 「去获取」入口点击

    async def fake_busy_retries(_page, tries=3):
        return {"clicks": 1, **feedback}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", fake_dismiss)
    monkeypatch.setattr(pipeline, "read_accel_state", fake_read)
    monkeypatch.setattr(pipeline, "_select_super_tier", fake_select_super)
    monkeypatch.setattr(pipeline, "_set_accel_prices", fake_set_prices)
    monkeypatch.setattr(pipeline, "_click_marked", fake_click_marked)
    monkeypatch.setattr(pipeline, "_click_first_button", fake_click_first)
    monkeypatch.setattr(pipeline, "_click_open_with_busy_retries", fake_busy_retries)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)

    class FakeMarkPage:
        async def evaluate(self, _script, _arg=None):
            return True

    return FakeMarkPage()


def test_open_accel_retries_whole_flow_until_confirmed(monkeypatch):
    """外层重试：前两轮 opened=False，第三轮确认成功即返回，并记录尝试次数。"""
    outcomes = iter([
        {"opened": False, "note": "未捕获成功提示且回查流量页状态非加速中"},
        {"opened": False, "note": "未捕获成功提示且回查流量页状态非加速中"},
        {"opened": True, "state": "on", "note": "已开启加速"},
    ])
    calls = []

    async def fake_once(_page, spu, allow=False, accel_price=None):
        calls.append((spu, allow, accel_price))
        return dict(next(outcomes))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)

    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True, accel_price=41.0, tries=3)
    )
    assert result["opened"] is True
    assert result["open_attempts"] == 3
    assert "第 3 次尝试确认开启成功" in result["note"]
    assert len(calls) == 3


def test_open_accel_retry_is_idempotent_when_already_on(monkeypatch):
    """幂等安全：上一轮其实已开成时，下一轮 precheck 读到 on 会 no-op 成功，不重复开启。

    这里让首轮就返回 no-op 成功（模拟 precheck=on），断言只调用一次、不进入重试。
    """
    calls = []

    async def fake_once(_page, spu, allow=False, accel_price=None):
        calls.append(spu)
        return {"opened": True, "precheck_state": "on", "note": "本就在加速中，无需开启（no-op）"}

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True, accel_price=41.0, tries=3)
    )
    assert result["opened"] is True and result["open_attempts"] == 1
    assert len(calls) == 1


def test_open_accel_reports_failure_after_exhausting_retries(monkeypatch):
    """三轮都未确认成功时，如实报失败并在 note 标注已重试次数。"""
    async def fake_once(_page, spu, allow=False, accel_price=None):
        return {"opened": False, "note": "未捕获成功提示且回查流量页状态非加速中"}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)

    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True, accel_price=41.0, tries=3)
    )
    assert result["opened"] is False
    assert result["open_attempts"] == 3
    assert "重试 3 次仍未确认开启成功" in result["note"]


def test_open_accel_half_run_does_not_retry(monkeypatch):
    """半程 allow=False：不真开、opened 恒 False，整轮重试无意义，只跑一次。"""
    calls = []

    async def fake_once(_page, spu, allow=False, accel_price=None):
        calls.append((spu, allow))
        return {"opened": False, "note": "半程：未点「立即加速」（allow=False）"}

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=False, accel_price=41.0, tries=3)
    )
    assert result["opened"] is False
    assert len(calls) == 1


def test_open_accel_verifies_by_state_when_toast_unknown(monkeypatch):
    """toast 一闪而过没抓到成功提示（unknown）时，回读流量页确认状态=加速中即判成功。

    根因（2026-07-24 实机）：点「立即加速」后动作已执行、填价成功，但成功 toast 未被捕获
    → 旧代码直接判未开、上层报「流量加速失败」。持久的接口状态才是权威判据。
    """
    page = _patch_open_accel_full_path(
        monkeypatch,
        feedback={"status": "unknown", "message": "等待流量加速结果提示超时"},
        read_states=["off", "on"],  # precheck=off → 走完整路径；toast unknown 后回读=on
    )
    result = asyncio.run(
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_price=41.0)
    )
    assert result["opened"] is True
    assert result["row_state_snapshot"] == "on"
    assert "回查流量页确认状态=加速中" in result["note"]


def test_open_accel_stays_failed_when_toast_unknown_and_state_off(monkeypatch):
    """toast unknown 且回读状态仍非加速中时，不得误判成功，如实报失败。"""
    page = _patch_open_accel_full_path(
        monkeypatch,
        feedback={"status": "unknown", "message": "等待流量加速结果提示超时"},
        read_states=["off", "off"],  # 回读仍 off
    )
    result = asyncio.run(
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_price=41.0)
    )
    assert result["opened"] is False
    assert "回查流量页状态非加速中" in result["note"]


def test_popup_closer_clicks_merchant_helper_button():
    """阶段入口只点击商家助手的关闭按钮，不依赖具体随机弹窗结构。"""
    class FakePopupPage:
        def __init__(self):
            self.script = ""

        async def evaluate(self, script):
            self.script = script
            return True

    page = FakePopupPage()
    assert asyncio.run(pipeline.dismiss_all_page_popups(page)) is True
    assert "关闭所有弹窗" in page.script


def test_cooldown_close_does_not_block_enroll(monkeypatch):
    """关流量撞 24h 冷却（closed=False, cooldown=True）→ 该 SPU 报名仍照常继续（用户确认的设计）。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    events = []
    calls = {"fill": [], "submit": [], "open": []}

    async def fake_close(page, spu, allow=False):
        return {"state": "on", "closed": False, "cooldown": True, "note": "未关闭：24小时冷却"}

    async def fake_open(page, spu, allow=False):
        calls["open"].append(spu)
        return {"state": "on", "opened": True, "note": "no-op"}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, price, allow_submit=False, on_step=None,
                          daily_price=None, discount_rate=None):
        calls["fill"].append(spu)
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False):
        calls["submit"].append(allow)
        return {"submitted": allow, "note": ""}

    monkeypatch.setattr(service.pipeline, "close_accel", fake_close)
    monkeypatch.setattr(service.pipeline, "open_accel", fake_open)
    monkeypatch.setattr(service.pipeline, "open_enroll_page", fake_open_page)
    monkeypatch.setattr(service.pipeline, "enroll_activity", fake_enroll)
    monkeypatch.setattr(service.pipeline, "submit_enroll_page", fake_submit)

    async def on_progress(ev):
        events.append(ev)

    summary = asyncio.run(
        service._run_execution_phases(results, FakePage("flux"), FakePage("act"), True, on_progress)
    )
    # 关流量被冷却拦截：记入 cooldown、不记入 closed
    assert summary.get("cooldown") == ["111"]
    assert "111" not in summary["closed"]
    # 但报名照常：111 被填价 + 提交
    assert calls["fill"] == ["111"]
    assert calls["submit"] == [True]
    # exec_close 事件带 cooldown 标记
    close_ev = next(e for e in events if e["type"] == "exec_close")
    assert close_ev["cooldown"] is True
    # 未关成 → 阶段三不重开它（summary["closed"] 为空）
    assert calls["open"] == []


def test_enroll_marker_clears_previous_activity():
    """每次定位活动前必须删除旧标记，否则会一直点击首次活动的报名按钮。"""
    selector = pipeline._MARK_ENROLL_JS
    assert "removeAttribute('data-kiro-enroll')" in selector
    assert selector.index("removeAttribute('data-kiro-enroll')") < selector.index(
        "setAttribute('data-kiro-enroll', '1')"
    )


@pytest.mark.parametrize(
    ("product_info_text", "expected_state"),
    [
        ("SPU ID：9072868889 在售 官方大促 流量加速中", "on"),
        ("SPU ID：9072868889 在售 大促进阶 限时秒杀 官方大促", "off"),
    ],
)
def test_flux_spu_search_uses_product_info_cell(product_info_text, expected_state, monkeypatch):
    """按 SPU 查询须等接口 total=1；状态只看商品信息单元格是否含“流量加速中”。"""
    class FakeField:
        def __init__(self):
            self.value = ""

        async def fill(self, value):
            self.value = value

        async def input_value(self):
            return self.value

    class FakeButton:
        def __init__(self):
            self.clicked = False

        @property
        def first(self):
            return self

        async def click(self, timeout=None):
            self.clicked = True

    class FakeResponse:
        url = "https://agentseller.temu.com/api/flow/analysis/list"

        async def json(self):
            return {
                "result": {
                    "total": 1,
                    "pageItems": [{"productId": 9072868889, "flowGrowStatus": 1}],
                }
            }

    class FakeResponseInfo:
        def __init__(self, response):
            self.response = response

        @property
        def value(self):
            async def get_value():
                return self.response
            return get_value()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeFluxPage:
        def __init__(self):
            self.field = FakeField()
            self.button = FakeButton()

        async def bring_to_front(self):
            return None

        async def evaluate(self, script, *args):
            if not args:
                return True
            return {
                "found": True,
                "product_info_text": product_info_text,
                "row_text": f"{product_info_text} 查看详情",
            }

        def locator(self, _selector):
            return self.field

        def get_by_role(self, _role, name=None, exact=None):
            return self.button

        def expect_response(self, predicate, timeout=None):
            response = FakeResponse()
            assert predicate(response)
            return FakeResponseInfo(response)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeFluxPage()
    result = asyncio.run(pipeline._search_flux_product(page, "9072868889"))

    assert page.field.value == "9072868889" and page.button.clicked is True
    assert result["total"] == 1 and result["state"] == expected_state
    assert result["api_flow_status"] == 1
    assert "closest('td')" in pipeline._LOCATE_ROW_JS


def test_accel_classifier_uses_product_info_not_whole_row():
    """reload 校验也必须使用商品信息列，不能把相邻商品行的标签算进来。"""
    assert pipeline._classify_accel("SPU ID：9072868889 在售 大促进阶") == "off"
    assert pipeline._classify_accel("SPU ID：2801689369 在售 流量加速中") == "on"


def test_open_accel_prechecks_by_spu_and_noops_when_already_on(monkeypatch):
    """开启前必须按 SPU 搜索确认；当前已 on 时不得再点任何开启入口。"""
    calls = []

    async def fake_dismiss(_page):
        return False

    async def fake_read(_page, spu, search=False):
        calls.append((spu, search))
        return "on"

    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", fake_dismiss)
    monkeypatch.setattr(pipeline, "read_accel_state", fake_read)

    result = asyncio.run(
        pipeline._open_accel_once(FakePage("flux"), "9072868889", allow=True, accel_price=78.77)
    )

    assert calls == [("9072868889", True)]
    assert result["opened"] is True and result["precheck_state"] == "on"
    assert "无需开启" in result["note"]


def test_activity_rule_button_supports_non_dialog_fullscreen_layer():
    """活动详情可能是普通全屏 DIV；须从规则按钮向上找含目标活动名的弹层，不能只认 dialog class。"""
    script = pipeline._CLICK_ACTIVITY_RULE_JS
    assert "同意活动规则" in script
    assert "node.parentElement" in script
    assert "text.includes('活动详情')" in script
    assert "text.includes(target)" in script
    assert "node !== document.body" in script
    # 正文「加载中」期间活动名未渲染，须返回 loading 让调用方继续轮询，而不是误判 mismatch。
    assert "加载中" in script
    assert "status: 'loading'" in script


def test_open_enroll_page_waits_out_rule_popup_loading(monkeypatch):
    """规则弹窗正文「加载中」时不得整轮重试（实测 2026-07-23 白等 ~8s/活动）；
    应在本次尝试内继续轮询，等正文渲染出活动名后再绑定点击。"""
    class FakeDetailPage:
        def __init__(self, url):
            self.url = url
            self.closed = False

        async def wait_for_load_state(self, _state, timeout=None):
            return None

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.pages = []

    class FakeActivityPage:
        def __init__(self, context):
            self.url = pipeline.ACTIVITY_URL
            self.context = context
            self.created = None
            self.mark_calls = 0
            self.rule_calls = 0

        async def evaluate(self, script, arg=None):
            if script == pipeline._MARK_ENROLL_JS:
                self.mark_calls += 1
                return "目标活动行"
            if script == pipeline._CLICK_ACTIVITY_RULE_JS:
                self.rule_calls += 1
                if self.rule_calls < 3:
                    return {"status": "loading", "actual": "活动详情 加载中..."}
                self.created = FakeDetailPage(
                    "https://agentseller.temu.com/activity/marketing-activity/detail-new?type=5"
                )
                self.context.pages.append(self.created)
                return {"status": "clicked", "actual": arg}
            return None

    context = FakeContext()
    activity_page = FakeActivityPage(context)
    context.pages = [activity_page]

    async def no_wait(_seconds):
        return None

    async def no_dismiss(_page):
        return False

    async def matched(_page, expected_name, tries=20):
        return {"matched": True, "actual": expected_name, "ready": True}

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", no_dismiss)
    monkeypatch.setattr(pipeline, "verify_enroll_page_activity", matched)

    opened = asyncio.run(pipeline.open_enroll_page(activity_page, "官方大促", timeout_s=6))

    assert opened is activity_page.created
    # loading 被当失败时会在整轮重试里重新标记报名按钮；标记只来一次说明在首次尝试内等到点击。
    assert activity_page.mark_calls == 1
    assert activity_page.rule_calls == 3


def test_parse_activity_reads_registered_column():
    """活动行必须解析提交前“已报名”基线，供提交后做数字增量核验。"""
    empty = pipeline._parse_activity(
        "【营销热点】回归日常&周年庆大促85折专场 New 2026-07-31～2026-08-31 "
        "≤ 8.5折 ≥ 15个 0 报名40天后截止"
    )
    registered = pipeline._parse_activity(
        "限时秒杀 长期有效 ≤ 8.5折 ≥ 30个 2 报名"
    )
    dash = pipeline._parse_activity(
        "官方大促 长期有效 ≤ 9折 ≥ 30个 - 报名"
    )

    assert empty["registered_count"] == 0 and empty["registered_display"] == "0"
    assert registered["registered_count"] == 2
    assert dash["registered_count"] == 0 and dash["registered_display"] == "-"


def test_parse_activity_log_item_listed_not_exited_is_success():
    """用户实机规则 2026-07-24：报名记录出现在列表且非「已退出」即成功。1=进行中、3=进行中、
    4=报名成功待开始均为成功态；场次失败原因不再否决；仅文本/数值命中「已退出」才判失败。"""
    ongoing = pipeline._parse_activity_log_item({
        "productId": 2801689369,
        "activityThematicName": "【营销热点】夏季促销8折专场",
        "enrollStatus": 1,
        "assignSessionList": [{"sessionFailReason": None}],
    })
    ongoing_three = pipeline._parse_activity_log_item({
        "productId": 2801689369,
        "activityThematicName": "【营销热点】半托管活动85折专区",
        "enrollStatus": 3,
        "assignSessionList": [],
    })
    pending_start = pipeline._parse_activity_log_item({
        "productId": 9072868889,
        "activityThematicName": "【营销热点】回归日常&周年庆大促85折专场",
        "enrollStatus": 4,
        "enrollId": 3000020543885092,
        "assignSessionList": [{"sessionFailReason": None}],
    })
    failed_session = pipeline._parse_activity_log_item({
        "productId": 9072868889,
        "activityThematicName": "活动A",
        "enrollStatus": 4,
        "assignSessionList": [{"sessionFailReason": "类目不符合"}],
    })
    fixed_activity = pipeline._parse_activity_log_item({
        "productId": 9072868889,
        "activityThematicName": None,
        "activityTypeName": "限时秒杀",
        "enrollStatus": 4,
        "assignSessionList": [],
    })
    # 已退出=enrollStatus 6（2026-07-24 用户后台核对确认），数值命中即判退出，无需文本。
    exited = pipeline._parse_activity_log_item({
        "productId": 9072868889,
        "activityThematicName": "活动B",
        "enrollStatus": 6,
    })
    # 文本探测兜底：接口带「已退出」文案时也判退出。
    exited_by_text = pipeline._parse_activity_log_item({
        "productId": 9072868889,
        "activityThematicName": "活动C",
        "enrollStatus": 99,
        "enrollStatusDesc": "已退出",
    })

    assert ongoing["success"] is True
    assert ongoing_three["success"] is True
    assert pending_start["success"] is True
    assert pending_start["spu"] == "9072868889"
    assert failed_session["success"] is True
    assert failed_session["session_failures"] == ["类目不符合"]
    assert fixed_activity["activity"] == "限时秒杀"
    assert exited["success"] is False
    assert exited_by_text["success"] is False


def test_activity_log_pagination_collects_total_fourteen():
    """首屏 10/total 14 时必须再读第二页 4 条，不能把后四个活动标成查询不完整。"""
    first = {
        "total": 14,
        "list": [{"enrollId": index} for index in range(1, 11)],
    }
    requested_pages = []

    async def fetch_next(page_number):
        requested_pages.append(page_number)
        return {"total": 14, "list": [{"enrollId": index} for index in range(11, 15)]}

    result = asyncio.run(pipeline._collect_activity_log_pages(first, fetch_next))

    assert requested_pages == [2]
    assert result["expected_pages"] == 2 and result["pages_read"] == 2
    assert len(result["items"]) == 14 and result["complete"] is True


def test_activity_log_pagination_failure_is_incomplete_not_success():
    """下一页读取失败时保留首屏记录并明确 incomplete，不能假装已经查全。"""
    first = {
        "total": 14,
        "list": [{"enrollId": index} for index in range(1, 11)],
    }

    async def fetch_next(_page_number):
        raise RuntimeError("下一页请求超时")

    result = asyncio.run(pipeline._collect_activity_log_pages(first, fetch_next))

    assert len(result["items"]) == 10
    assert result["complete"] is False
    assert "下一页请求超时" in result["error"]


def test_verify_activity_registration_waits_for_count_increment(monkeypatch):
    """提交后刷新活动列表，只有“已报名”数字达到预期增量才返回权威成功。"""
    class FakeActivityListPage:
        def __init__(self):
            self.reloads = 0

        async def reload(self, wait_until=None, timeout=None):
            self.reloads += 1

    counts = iter([1, 1, 2])

    async def fake_read(_page):
        return [{"name": "活动A", "registered_count": next(counts)}]

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "read_activities", fake_read)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeActivityListPage()
    result = asyncio.run(
        pipeline.verify_activity_registration(page, "活动A", 1, 1, tries=3)
    )

    assert result["verified"] is True
    assert result["before"] == 1 and result["after"] == 2
    assert page.reloads == 3


def test_verify_enroll_page_activity_waits_for_expected_header(monkeypatch):
    """详情页先显示旧活动、随后切到目标活动时，须等目标页头出现才允许填价。"""
    class FakeActivityPage:
        def __init__(self):
            self.results = iter([
                {"matched": False, "actual": "限时秒杀", "ready": True},
                {"matched": True, "actual": "官方大促", "ready": True},
            ])

        async def evaluate(self, _script, _expected):
            return next(self.results)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.verify_enroll_page_activity(FakeActivityPage(), "官方大促", tries=2)
    )
    assert result == {"matched": True, "actual": "官方大促", "ready": True}


def test_verify_enroll_page_activity_rejects_mismatch(monkeypatch):
    """页头始终是其他活动时返回 matched=False，调用方必须关页且不填价。"""
    class FakeActivityPage:
        async def evaluate(self, _script, _expected):
            return {"matched": False, "actual": "限时秒杀", "ready": True}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.verify_enroll_page_activity(FakeActivityPage(), "官方大促", tries=2)
    )
    assert result["matched"] is False
    assert result["actual"] == "限时秒杀"


def test_open_enroll_page_preserves_preexisting_manual_detail_tab(monkeypatch):
    """任务只认本次新建详情页，不关闭操作者预先打开的手动测试页。"""
    class FakeDetailPage:
        def __init__(self, url):
            self.url = url
            self.closed = False

        async def wait_for_load_state(self, _state, timeout=None):
            return None

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.pages = []

    class FakeActivityPage:
        def __init__(self, context):
            self.url = pipeline.ACTIVITY_URL
            self.context = context
            self.created = None

        async def evaluate(self, script, arg=None):
            if script == pipeline._MARK_ENROLL_JS:
                return "目标活动行"
            if script == pipeline._CLICK_ACTIVITY_RULE_JS:
                self.created = FakeDetailPage(
                    "https://agentseller.temu.com/activity/marketing-activity/detail-new?type=1"
                )
                self.context.pages.append(self.created)
                return {"status": "clicked", "actual": arg}
            return None

    context = FakeContext()
    activity_page = FakeActivityPage(context)
    manual_page = FakeDetailPage(
        "https://agentseller.temu.com/activity/marketing-activity/detail-new?type=1"
    )
    context.pages = [activity_page, manual_page]

    async def no_wait(_seconds):
        return None

    async def no_dismiss(_page):
        return False

    async def matched(_page, expected_name, tries=20):
        return {"matched": True, "actual": expected_name, "ready": True}

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", no_dismiss)
    monkeypatch.setattr(pipeline, "verify_enroll_page_activity", matched)

    opened = asyncio.run(pipeline.open_enroll_page(activity_page, "官方大促", timeout_s=6))

    assert opened is activity_page.created
    assert manual_page.closed is False


def test_execution_product_done_requires_submit_and_accel():
    """规划 done 不算最终完成；全部活动已提交且流量步骤完成后才是 done。"""
    plans = [_plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False)]
    complete = {
        "enrolled_activities": {"活动A": ["111"], "活动B": ["111"]},
        "reopened": ["111"], "closed": [],
    }
    incomplete = {
        "enrolled_activities": {"活动A": ["111"], "活动B": []},
        "reopened": ["111"], "closed": [],
    }

    done = service._execution_product_results(plans, complete, live=True)[0]
    failed = service._execution_product_results(plans, incomplete, live=True)[0]

    assert done["status"] == "done" and done["submitted"] == 2 and done["accel_ok"] is True
    assert failed["status"] == "fail" and failed["submitted"] == 1


def test_execution_product_result_handles_detail_ineligible():
    """部分详情不符合不拖累已提交活动；全部详情不符合则为 skip 且不要求开流量。"""
    plans = [_plan("111", [("活动A", 10.0), ("活动B", 11.0)], accel_will_close=False)]
    mixed = {
        "enrolled_activities": {"活动A": [], "活动B": ["111"]},
        "ineligible_activities": {"活动A": ["111"]},
        "reopened": ["111"], "closed": [],
    }
    all_ineligible = {
        "enrolled_activities": {"活动A": [], "活动B": []},
        "ineligible_activities": {"活动A": ["111"], "活动B": ["111"]},
        "reopened": [], "closed": [],
    }

    mixed_result = service._execution_product_results(plans, mixed, live=True)[0]
    skipped_result = service._execution_product_results(plans, all_ineligible, live=True)[0]

    assert mixed_result["status"] == "done"
    assert mixed_result["submitted"] == 1 and mixed_result["ineligible"] == 1
    assert skipped_result["status"] == "skip"
    assert skipped_result["ineligible"] == 2 and skipped_result["accel_ok"] is False


def test_submit_clicks_page_button_once_and_scopes_confirmation(monkeypatch):
    """底部提交只点一次；二次确认必须限定在可见 dialog/modal 内。"""
    class FakeButton:
        def __init__(self):
            self.clicks = 0

        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            self.clicks += 1

    class FakeSubmitPage:
        def __init__(self):
            self.button = FakeButton()
            self.confirm_script = ""

        def get_by_role(self, role, name=None):
            assert role == "button" and name == "提交"
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, script, _can_confirm):
            self.confirm_script = script
            return {"status": "pending", "message": ""}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeSubmitPage()
    result = asyncio.run(pipeline.submit_enroll_page(page, allow=True, feedback_tries=2))

    assert page.button.clicks == 1
    assert "[role=dialog]" in page.confirm_script
    assert result["submitted"] is True and result["confirmed"] is False
    assert result["verified"] is False


def test_submit_waits_for_delayed_confirmation_and_success(monkeypatch):
    """二次确认延迟出现时继续轮询，只点一次确认，并以成功提示结束。"""
    class FakeButton:
        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            return None

    class FakeSubmitPage:
        def __init__(self):
            self.button = FakeButton()
            self.feedback = iter([
                {"status": "pending", "message": ""},
                {"status": "pending", "message": ""},
                {"status": "confirmation_clicked", "message": "确认提交"},
                {"status": "pending", "message": "二次确认请求处理中"},
                {"status": "success", "message": "报名成功"},
            ])
            self.can_confirm = []

        def get_by_role(self, _role, name=None):
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, _script, can_confirm):
            self.can_confirm.append(can_confirm)
            return next(self.feedback)

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeSubmitPage()
    result = asyncio.run(pipeline.submit_enroll_page(page, allow=True, feedback_tries=5))

    assert page.can_confirm == [True, True, True, False, False]
    assert result["submitted"] is True
    assert result["confirmed"] is True
    assert result["verified"] is True
    assert result["note"] == "报名成功"


def test_submit_failure_toast_is_not_reported_as_submitted(monkeypatch):
    """平台返回火爆/稍后再试等失败提示时，不能把“点过提交”当成提交成功。"""
    class FakeButton:
        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            return None

    class FakeSubmitPage:
        button = FakeButton()

        def get_by_role(self, _role, name=None):
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, _script, _can_confirm):
            return {"status": "failure", "message": "活动太火爆，请稍后再试"}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.submit_enroll_page(FakeSubmitPage(), allow=True, feedback_tries=2)
    )

    assert result["submitted"] is False
    assert result["verified"] is False
    assert "太火爆" in result["note"]


def test_submit_navigation_context_is_deferred_to_activity_log(monkeypatch):
    """点击提交后页面跳转销毁 JS 上下文，不得误判失败，应交给报名记录页最终核验。"""
    class FakeButton:
        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            return None

    class FakeSubmitPage:
        button = FakeButton()

        def get_by_role(self, _role, name=None):
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, _script, _can_confirm):
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, most likely because of a navigation"
            )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.submit_enroll_page(FakeSubmitPage(), allow=True, feedback_tries=2)
    )

    assert result["submitted"] is True
    assert result["verified"] is False
    assert result["navigated"] is True
    assert "报名记录页" in result["note"]


def test_submit_result_page_confirms_success_count(monkeypatch):
    """detail-new-result 的 successCount 与“已提交N个商品”应作为即时提交成功反馈。"""
    class FakeButton:
        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            return None

    class FakeSubmitPage:
        button = FakeButton()
        url = (
            "https://agentseller.temu.com/activity/marketing-activity/"
            "detail-new-result?type=13&successCount=1&thematicId=2607020000360010"
        )

        def get_by_role(self, _role, name=None):
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, _script, *_args):
            return "已提交1个商品\n可在【报名记录】中查看报名结果"

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.submit_enroll_page(FakeSubmitPage(), allow=True, feedback_tries=2)
    )

    assert result["submitted"] is True
    assert result["verified"] is True
    assert result["result_page"] is True
    assert result["success_count"] == 1
    assert "已提交 1 个商品" in result["note"]


def test_submit_unrecognized_confirmation_fails_closed(monkeypatch):
    """确认弹窗持续存在但按钮无法识别时保守失败，避免继续后续活动。"""
    class FakeButton:
        async def count(self):
            return 1

        async def is_disabled(self):
            return False

        async def click(self, timeout=None):
            return None

    class FakeSubmitPage:
        button = FakeButton()

        def get_by_role(self, _role, name=None):
            return type("Locator", (), {"first": self.button})()

        async def evaluate(self, _script, _can_confirm):
            return {"status": "confirmation_blocked", "message": "请确认报名信息"}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    result = asyncio.run(
        pipeline.submit_enroll_page(FakeSubmitPage(), allow=True, feedback_tries=6)
    )

    assert result["submitted"] is False
    assert result["confirmed"] is False
    assert "无法点击确认" in result["note"]
