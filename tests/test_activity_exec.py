"""活动管理执行遍（关流量→报名→开流量）编排单测。

不碰真浏览器：monkeypatch pipeline 的变更函数为 fake，断言编排顺序、活动维度分组、
live 门控（半程 live=False 只填不提交/不真关开；全程 live=True 才不可逆）。
"""
import asyncio
from pathlib import Path

import pytest

from app.activity import service
from app.activity.control import ActivityControl


@pytest.fixture(autouse=True)
def fast_log_retries(monkeypatch):
    monkeypatch.setattr(service, "LOG_VERIFY_RETRY_DELAYS", (0, 0, 0))


class FakePage:
    """占位页面对象（执行遍里只作为句柄透传给被 patch 的 pipeline 函数）。"""
    def __init__(self, tag=""):
        self.tag = tag
        self.closed = False
        self.context = self

    async def close(self):
        self.closed = True


class _EvalPage:
    """只实现 evaluate 的假页面：ret 是返回值，boom=True 时抛异常。"""
    def __init__(self, ret=False, boom=False):
        self.ret = ret
        self.boom = boom
        self.calls = 0

    async def evaluate(self, *_args, **_kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("页面还没就绪")
        return self.ret


def test_click_assistant_close_all_popups_is_best_effort():
    """插件按钮探测是 best-effort：点到返 True，没按钮返 False，页面报错也只返 False。

    订单采集的逐页循环靠它清运营弹窗，绝不能因为插件没装/页面瞬时报错就中断整批。
    """
    import asyncio
    from app.activity.pipeline import click_assistant_close_all_popups as click_close

    # 按钮存在并点到
    assert asyncio.run(click_close(_EvalPage(ret=True))) is True
    # 按钮不存在（插件未装/未注入）
    assert asyncio.run(click_close(_EvalPage(ret=False))) is False
    # evaluate 抛异常：吞掉，不往上抛
    assert asyncio.run(click_close(_EvalPage(boom=True))) is False
    # page 为 None 直接返回，不去碰属性
    assert asyncio.run(click_close(None)) is False


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


def test_activity_page_has_recognition_matrix_and_pause_controls():
    """识别矩阵（行=SPU、列=活动、逐格勾选）与暂停/继续、半程全程档位必须都在页面上，
    且勾选态要能从 localStorage 恢复——否则刷新页面等于把选择重做一遍。"""
    html = Path("templates/activity.html").read_text(encoding="utf-8")

    assert 'id="matrixTable"' in html and 'id="matrixBody"' in html
    assert "function renderMatrix()" in html and "function matrixColumns()" in html
    assert "data-matrix-pick" in html and "data-matrix-row" in html and "data-matrix-col" in html
    assert "cellSelection" in html and "activity-matrix-selection-v1" in html
    assert 'id="switchAllActivities"' in html and 'id="switchForceScan"' in html
    assert 'id="btnScan"' in html and 'id="btnExecSel"' in html
    assert "function startScan()" in html and "function subscribeJob(" in html
    # 暂停：按钮两态 + 后端 paused/resumed/pause_refused 三个事件都要处理
    assert 'id="btnPause"' in html and "function togglePause()" in html
    assert 'type === "paused"' in html and 'type === "resumed"' in html
    assert 'type === "pause_refused"' in html
    # 执行中逐格跳过 + 半程/全程档位（Web 端原先只能全程）
    assert 'type === "exec_cell_skip"' in html and 'id="execLevel"' in html
    assert "半程（填价不提交）" in html and "全程（正式提交）" in html


def test_activity_log_full_fetch_groups_locally_and_fail_closed():
    """报名记录页平台搜索已失效（2026-09-30 实测搜 SPU/goodsId/商品名全 0）：
    改为拉全量分页、本地按 productId 分组；全量不完整时所有 SPU 均 fail-closed。"""
    source = __import__("inspect").getsource(pipeline.read_activity_log_records)
    # 新路线：开页等列表接口首响应 + 翻页拉全量，不再操作 SPU 搜索框
    assert '"/marketing/enroll/list" in response.url' in source
    assert "_MARK_ACTIVITY_LOG_SPU_JS" not in source

    item_a = {"productId": 111, "activityThematicName": "活动A", "enrollStatus": 4}
    item_b = {"productId": 222, "activityThematicName": "活动B", "enrollStatus": 6}
    item_other = {"productId": 999, "activityThematicName": "活动C", "enrollStatus": 4}

    collected_ok = {"complete": True, "page_size": 10, "error": None}
    records, queries = pipeline._group_log_items_by_spu(
        [item_a, item_b, item_other], ["111", "222", "333"], collected_ok)
    assert {q["spu"]: q["total"] for q in queries} == {"111": 1, "222": 1, "333": 0}
    # 查询完整但确实没记录（333）：total=0 且 complete=True——与「查询不完整」分开
    assert all(q["complete"] for q in queries)
    assert [(r["spu"], r["activity"]) for r in records] == [("111", "活动A"), ("222", "活动B")]

    # 全量不完整：所有 SPU 一并 fail-closed，哪怕本地已匹配到记录
    collected_bad = {"complete": False, "page_size": 10, "error": "翻页失败"}
    _, queries_bad = pipeline._group_log_items_by_spu(
        [item_a], ["111", "333"], collected_bad)
    assert [q["complete"] for q in queries_bad] == [False, False]
    assert queries_bad[0]["error"] == "翻页失败"


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


# 测试用的区域域名：全球是实测的 agentseller.temu.com，改动后 URL 由「区域 host + 路径」
# 运行时拼出，故测试也按这个口径构造期望值，不再引用已删除的 *_URL 常量。
GLOBAL_HOST = "agentseller.temu.com"
US_HOST = "agentseller-us.temu.com"


def _region_tabs(active="全球"):
    """_ACTIVE_REGION_JS 的返回形状：顶栏区域标签 + 当前激活的那个。"""
    return {"labels": ["全球", "美国", "欧区"], "active": active, "ambiguous": []}


class FakeConnectedPage(FakePage):
    def __init__(self, url="about:blank", region_active="全球"):
        super().__init__(url)
        self.url = url
        self.goto_calls = []
        self._region_active = region_active

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url

    async def evaluate(self, _js, *args):
        # 区域读取是 _connect_pages 的前置步骤；其它 evaluate 在这些用例里用不到
        return _region_tabs(self._region_active)


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


def _expected_urls(host):
    return [
        f"https://{host}{service.pipeline.FLUX_PATH}",
        f"https://{host}{service.pipeline.ACTIVITY_PATH}",
    ]


def test_connect_pages_opens_all_missing_tabs(monkeypatch):
    """自动打开流量和活动页，不再打开库存商品页。

    context 里必须有一个用户页签供确认区域——_connect_pages 现在会先确认区域再开页面。
    """
    context, browser, playwright = _patch_cdp(
        monkeypatch, [FakeConnectedPage(f"https://{GLOBAL_HOST}/")])
    dismissed = []

    async def fake_dismiss(page):
        dismissed.append(page.url)
        return True

    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", fake_dismiss)
    result = asyncio.run(service._connect_pages("http://localhost:9222"))
    pw, got_browser, flux, activity, goods, owned = result

    assert pw is playwright and got_browser is browser
    assert [page.url for page in owned] == _expected_urls(GLOBAL_HOST)
    assert (flux, activity) == tuple(owned)
    assert goods is None
    assert dismissed == [page.url for page in owned]


def test_connect_pages_opens_tabs_in_user_selected_region(monkeypatch):
    """核心：用户选的是美国区，本批页面就必须开在美国域，不能回落到全球域。

    区域切换换域名（全球 agentseller.temu.com / 美国 agentseller-us.temu.com），旧代码
    的写死全球域 URL 会把操作者选定的美国区悄悄换掉，报名/开加速器打在错误的一批商品上。
    """
    context, _, _ = _patch_cdp(monkeypatch, [
        FakeConnectedPage(f"https://{US_HOST}/", region_active="美国")])

    async def fake_dismiss(_page):
        return True

    monkeypatch.setattr(service.pipeline, "dismiss_all_page_popups", fake_dismiss)
    _, _, _, _, _, owned = asyncio.run(
        service._connect_pages("http://localhost:9222"))

    assert [page.url for page in owned] == _expected_urls(US_HOST)
    assert all(GLOBAL_HOST not in page.url for page in owned)


def test_connect_pages_aborts_when_region_pages_disagree(monkeypatch):
    """两个用户页签处在不同区域 → 中止，绝不擅自挑一个区域作业。"""
    from app.temu_region import RegionUnconfirmed

    _patch_cdp(monkeypatch, [
        FakeConnectedPage(f"https://{GLOBAL_HOST}/", region_active="全球"),
        FakeConnectedPage(f"https://{US_HOST}/", region_active="美国"),
    ])
    with pytest.raises(RegionUnconfirmed):
        asyncio.run(service._connect_pages("http://localhost:9222"))


def test_connect_pages_aborts_when_no_seller_page_open(monkeypatch):
    """没有任何后台页签 → 无从确认区域，中止而不是默认全球域。"""
    from app.temu_region import RegionUnconfirmed

    _patch_cdp(monkeypatch, [])
    with pytest.raises(RegionUnconfirmed):
        asyncio.run(service._connect_pages("http://localhost:9222"))


def test_connect_pages_ignores_existing_tabs_and_opens_owned_tabs(monkeypatch):
    """已有页签状态不受管线控制；任务必须忽略它们并新建两个专用页签。

    「忽略」的唯一例外是只读一次区域（不 goto、不点击、不关闭），断言里仍校验这一点。
    """
    existing = [FakeConnectedPage(u) for u in _expected_urls(GLOBAL_HOST)]
    context, _, _ = _patch_cdp(monkeypatch, existing)
    result = asyncio.run(service._connect_pages("http://localhost:9222"))
    _, _, flux, activity, goods, owned = result

    assert (flux, activity) == tuple(owned)
    assert goods is None
    assert [page.url for page in owned] == _expected_urls(GLOBAL_HOST)
    assert all(page not in existing for page in owned)
    assert all(page.goto_calls == [] for page in existing)
    assert all(page.closed is False for page in existing)
    assert len(context.pages) == 4


def _plan(spu, activities, accel_will_close, sale=46.5, accel_state=None, discount=0.7,
          daily=None):
    """构造一条规划遍结果（status=done）。activities: [(名, 申报价)]。sale=销售底价（重开
    加速器时加速价至少 sale+1）。单货号：label 固定「默认」，执行层只认 skus/sku_prices 列表。
    discount=成本表折扣列（定加速档位）：默认 0.7 → 超级档，加速价=max(sale+1,
    底价折扣价÷0.9 向上取整)；sale=46.5 时 46.5×0.7=32.55→÷0.9=36.17 < 47.5，断言
    仍落在 sale+1 上。discount=None 表示折扣列读不到（discount_missing）。
    daily=日常价，缺省与 sale 同值（要验证「最低折扣价÷0.9 抬过底价+1」时才传更大的）。"""
    if accel_state is None:
        accel_state = "on" if accel_will_close else "off"
    sku = {"label": "默认", "daily": daily if daily is not None else sale,
           "sale": sale, "discount": discount}
    return {
        "spu": spu, "status": "done", "accel_will_close": accel_will_close,
        "accel_state": accel_state,
        "discount_missing": discount is None,
        "skus": [{**sku, "purchase": ""}],
        "enrolled_activities": [
            {"activity": n, "sku_prices": [{**sku, "submit_price": p}]}
            for n, p in activities
        ],
    }


def _accel_prices(price, sale=None):
    """单货号加速价列表（price = 底价+1），对应 service 阶段三构造的 accel_prices。"""
    sale = price - 1 if sale is None else sale
    return [{"label": "默认", "daily": sale, "sale": sale, "price": price}]


def _patch_log(monkeypatch, pairs=(), complete=True):
    reads = 0

    async def fake_read_log(_context, spus):
        nonlocal reads
        reads += 1
        records = [
            {
                "spu": str(spu), "activity": activity, "success": True,
                "enroll_status": 4, "enroll_id": f"{spu}-{activity}",
            }
            for spu, activity in (pairs if reads > 1 else [])
        ]
        return {
            "records": records, "complete": complete,
            "queries": [{"spu": str(spu)} for spu in spus], "note": "测试记录页",
        }

    monkeypatch.setattr(service.pipeline, "read_activity_log_records", fake_read_log)


def _run(results, live, monkeypatch, runtime_states=None, control=None, as_task=False,
         open_result=None, close_result=None):
    """跑执行遍，返回 (summary, events, calls)。calls 记录每个变更函数的调用参数。

    as_task=True 时不真正跑完，而是把协程返回给调用方自己调度（暂停/恢复类用例要
    在批次中途观察状态，必须能在等待期间做别的动作）。
    control 给定时透传给执行遍（暂停闸门 + 逐格跳过）。
    """
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
        if close_result is not None:
            return dict(close_result)
        return {"state": "on", "closed": allow, "note": "半程" if not allow else "已停止"}

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        calls["open"].append((spu, allow))
        calls.setdefault("open_price", []).append((spu, accel_prices))
        calls.setdefault("open_tier", []).append((spu, tier))
        if open_result is not None:
            return dict(open_result)
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        calls["sequence"].append("open_page")
        calls["open_page"].append(name)
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        calls["fill"].append((page.tag, spu, act, sku_prices, allow_submit))
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False, expected_spus=None):
        calls["submit"].append((page.tag, allow))
        calls.setdefault("submit_expected", []).append((page.tag, allow, expected_spus))
        return {"submitted": allow, "note": "已提交" if allow else "半程"}

    async def fake_read_log(_context, _spus):
        calls["read_log"].append(list(_spus))
        calls["sequence"].append("read_log")
        records = []
        if live and len(calls["read_log"]) > 1:
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

    coro = service._run_execution_phases(
        results, FakePage("flux"), FakePage("act"), live, on_progress, control=control
    )
    if as_task:
        return coro, events, calls
    return asyncio.run(coro), events, calls


def test_groups_by_activity_across_spus(monkeypatch):
    """两个 SPU 都报同一活动 A，另有活动 B：逐 SPU 开页、填完立即单独提交（2026-10-03 起
    废弃活动末统一提交——提报页每次搜索都重渲结果表格，上一 SPU 的勾选随旧行卸载）。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 20.0)], accel_will_close=False),
        _plan("222", [("活动A", 11.0)], accel_will_close=False),
    ]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    # 逐 SPU 一张干净提报页：活动A 开两次（111、222 各一），活动B 开一次
    assert calls["open_page"] == ["活动A", "活动A", "活动B"]
    # 活动A 填两次（111、222），活动B 填一次（111）
    a_fills = [c for c in calls["fill"] if c[2] == "活动A"]
    b_fills = [c for c in calls["fill"] if c[2] == "活动B"]
    assert {c[1] for c in a_fills} == {"111", "222"}
    assert {c[1] for c in b_fills} == {"111"}
    # 每 SPU 填完立即单独提交：共 3 次提交，不再按活动统一提交
    assert len(calls["submit"]) == 3
    # 每次提交都带 expected_spus（提交前勾选核对 + 结果页数量对账）
    assert all(c[2] for c in calls["submit_expected"])


def test_live_reads_activity_log_baseline_before_enrollment(monkeypatch):
    """正式执行先确认/关闭流量，再保存 /log total，之后才打开首个提报页。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]

    summary, _events, calls = _run(results, live=True, monkeypatch=monkeypatch)

    assert calls["read_log"] == [["111"], ["111"]]
    assert calls["sequence"].index("close") < calls["sequence"].index("read_log")
    assert calls["sequence"].index("read_log") < calls["sequence"].index("open_page")
    assert summary["log_baseline"]["queries"] == [{"spu": "111", "total": 0}]


def test_each_enroll_page_closed_after_activity(monkeypatch):
    """逐个活动：报完（无论成/败）都立刻关掉该提报页 tab，保证任意时刻只有一个 detail-new。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 20.0)], accel_will_close=False),
    ]
    opened = []

    async def fake_close(page, spu, allow=False):
        return {"state": "off", "closed": True, "note": ""}

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        pg = FakePage(f"enroll:{name}")
        opened.append(pg)
        return pg

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        # 报名前该页必须是打开的（未被提前关）
        assert page.closed is False
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False, expected_spus=None):
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


def test_compute_submit_price_truncates_at_midpoint():
    """申报价按平台口径向下取整（不是四舍五入，也不是内置 round 的银行家舍入）：
    188.5 × 0.25 = 47.125 若四舍五入得 47.13，就比平台参考价高 1 分、必被拒（真机实测 2026-09-24）。"""
    calc = pipeline.compute_submit_price(188.5, 0.25, 47.12)
    assert calc["submit_price"] == 47.12
    assert calc["within_floor"] is True
    # 实机压线案：188.88 × 0.85 = 160.548，平台上限只有 160.54 < 底价 160.548 → 该活动报不了
    edge = pipeline.compute_submit_price(188.88, 0.85, 160.548)
    assert edge["submit_price"] == 160.54 and edge["within_floor"] is False


def test_match_enroll_rows_needs_each_daily_covered_not_equal_counts():
    """提报页价格行是平台 SKC 粒度（款式×尺码），可以是货号数的整数倍（实测 2026-09-24：
    3 货号的商品 6 个价格行）。判定改为「每个页面行日常价都在成本表里 + 每个货号都被覆盖」，
    逐行按该行自身日常价填价，所以多出来的行不影响正确性。"""
    sku_prices = [
        {"label": "30cm", "daily": 74.72, "submit_price": 47.07},
        {"label": "40cm", "daily": 188.88, "submit_price": 160.55},
        {"label": "50cm", "daily": 188.88, "submit_price": 160.55},
    ]
    rows = [
        {"idx": i, "daily": daily, "ref": None}
        for i, daily in enumerate([74.72, 74.72, 188.88, 188.88, 188.88, 188.88])
    ]
    matched = pipeline.match_enroll_rows(sku_prices, rows)
    assert matched is not None and set(matched) == set(range(6))
    # 每个 SKC 行按自己的日常价拿到申报价：30cm 行 47.07，40/50cm 行 160.55
    assert [matched[i]["submit_price"] for i in range(2)] == [47.07, 47.07]
    assert [matched[i]["submit_price"] for i in range(2, 6)] == [160.55] * 4


def test_match_enroll_rows_rejects_missing_or_unknown_daily():
    """有货号的日常价没有任何页面行覆盖（会漏填）→ None；页面日常价在成本表里没有 → None。"""
    sku_prices = [
        {"label": "30cm", "daily": 74.72, "submit_price": 47.07},
        {"label": "40cm", "daily": 188.88, "submit_price": 160.55},
    ]
    # 只覆盖了 188.88，30cm 无行覆盖 → 该货号会漏填，整批不填
    assert pipeline.match_enroll_rows(
        sku_prices, [{"idx": 0, "daily": 188.88}, {"idx": 1, "daily": 188.88}]) is None
    # 页面出现成本表里没有的日常价 → 说明成本表与平台不一致，整批不填
    assert pipeline.match_enroll_rows(
        sku_prices, [{"idx": 0, "daily": 99.0}, {"idx": 1, "daily": 74.72}]) is None


def test_accel_price_groups_take_highest_floor_per_daily_tier():
    """加速价按【日常价档】合并，取该档最高底价+1。

    真机实测（2026-09-25）：对话框按日常价档列行，3 货号只有 2 档（40cm/50cm 共用 188.88），
    同一档平台只让填一个价——取最高底价+1 才不会低于档内任一货号的底价（用户拍板口径）。"""
    prices = [
        {"label": "30厘米/0.2kg", "daily": 74.72, "sale": 45.0, "price": 46.0},
        {"label": "40厘米/0.4kg", "daily": 188.88, "sale": 95.0, "price": 96.0},
        {"label": "50厘米/0.8kg", "daily": 188.88, "sale": 160.548, "price": 161.55},
    ]
    groups = pipeline.accel_price_groups(prices)
    assert set(groups) == {74.72, 188.88}
    assert groups[188.88]["price"] == 161.55          # 取该档最高底价+1
    assert groups[188.88]["labels"] == ["40厘米/0.4kg", "50厘米/0.8kg"]
    assert groups[74.72]["price"] == 46.0


def test_accel_price_groups_use_platform_daily_as_tier_key():
    """档的键用【平台日常价】：表里可能被人工填错（实测 8791757215 两个货号都写 163.23，
    平台却是 163.23 / 188.88），用表里的价分组只有 1 档，而对话框给 2 行 → 必中止。
    用平台价分档后，两行各自能算出「该档最高底价+1」，也各自不超各自的上限。"""
    prices = [
        {"label": "70cm0.2kg", "daily": 163.23, "sale": 63.0, "price": 64.0,
         "platform_daily": 188.88},
        {"label": "90cm0.4kg", "daily": 163.23, "sale": 97.938, "price": 98.94,
         "platform_daily": 163.23},
    ]
    groups = pipeline.accel_price_groups(prices)
    assert set(groups) == {163.23, 188.88}
    assert groups[163.23]["price"] == 98.94 and groups[188.88]["price"] == 64.0
    rows = [  # 实测的两行：参考申报价格 + 让价 = 平台日常价
        {"idx": 0, "ref": 145.77, "daily": 163.23, "text": "参考申报价格：¥145.77（让价¥17.46）"},
        {"idx": 1, "ref": 168.67, "daily": 188.88, "text": "参考申报价格：¥168.67（让价¥20.21）"},
    ]
    matched = pipeline.match_accel_rows(prices, rows)
    assert matched[0]["price"] == 98.94 and matched[0]["price"] <= rows[0]["ref"]
    assert matched[1]["price"] == 64.0 and matched[1]["price"] <= rows[1]["ref"]
    # 没有 platform_daily（提报页没读到）时退回表里的日常价分组
    fallback = pipeline.accel_price_groups(
        [{"label": "A", "daily": 100.0, "sale": 50.0, "price": 51.0}])
    assert set(fallback) == {100.0}


def test_match_accel_rows_matches_rows_by_daily_tier():
    """对话框行按「参考申报价格 + 让价」算出的日常价对上价格档（实测 30.27+44.45=74.72、
    168.67+20.21=188.88）。行数≠档数、某行日常价算不出、对不上任何档 → None 中止不开。"""
    prices = [
        {"label": "30厘米/0.2kg", "daily": 74.72, "sale": 45.0, "price": 46.0},
        {"label": "40厘米/0.4kg", "daily": 188.88, "sale": 95.0, "price": 96.0},
        {"label": "50厘米/0.8kg", "daily": 188.88, "sale": 160.548, "price": 161.55},
    ]
    rows = [
        {"idx": 0, "ref": 30.27, "daily": 74.72, "text": "参考申报价格：¥30.27（让价¥44.45）"},
        {"idx": 1, "ref": 168.67, "daily": 188.88, "text": "参考申报价格：¥168.67（让价¥20.21）"},
    ]
    matched = pipeline.match_accel_rows(prices, rows)
    assert matched[0]["price"] == 46.0 and matched[1]["price"] == 161.55
    # 单档单行：读不出日常价也没有错配对象，直接对应（单货号商品的老路径）
    one = [{"label": "默认", "daily": 46.5, "sale": 46.5, "price": 47.5}]
    assert pipeline.match_accel_rows(one, [{"idx": 0, "text": "任意文本"}])[0]["price"] == 47.5
    # 行数与档数不等 → None
    assert pipeline.match_accel_rows(prices, rows[:1]) is None
    # 某行算不出日常价（没有让价文案）→ None
    assert pipeline.match_accel_rows(prices, [
        {"idx": 0, "ref": 30.27, "daily": None}, {"idx": 1, "ref": 168.67, "daily": 188.88}]) is None
    # 行日常价对不上成本表的任何档 → None
    assert pipeline.match_accel_rows(prices, [
        {"idx": 0, "ref": 30.27, "daily": 99.0}, {"idx": 1, "ref": 168.67, "daily": 188.88}]) is None
    # 两行撞同一档 → None
    assert pipeline.match_accel_rows(prices, [
        {"idx": 0, "ref": 30.27, "daily": 74.72}, {"idx": 1, "ref": 30.27, "daily": 74.72}]) is None


class _FakeAccelDialogPage:
    """模拟「调整申报价」对话框：evaluate 返回标记行，locator(...).first.fill 记录逐行填价。"""

    def __init__(self, rows):
        self.rows = rows
        self.filled = {}

    async def evaluate(self, _script, *_args):
        return self.rows

    def locator(self, selector):
        idx = int(selector.rsplit('="', 1)[1].rstrip('"]'))
        page = self

        class _Input:
            @property
            def first(self):
                return self

            async def click(self):
                return None

            async def fill(self, value):
                page.filled[idx] = value

            async def input_value(self):
                return page.filled.get(idx, "")

        return _Input()


def test_set_accel_prices_count_mismatch_and_match_failed_fill_nothing():
    """行数与日常价档数不等 → count_mismatch；行对不上任何档 → match_failed；
    两者都不填任何价（调用方据此中止不开加速器），note 里带行文本摘要。"""
    prices = [
        {"label": "40cm", "daily": 188.88, "sale": 95, "price": 96.0},
        {"label": "50cm", "daily": 188.88, "sale": 160.55, "price": 161.55},
        {"label": "30cm", "daily": 74.72, "sale": 45, "price": 46.0},
    ]
    # 3 档只给 1 行 → 对不上
    page = _FakeAccelDialogPage([{"idx": 0, "ref": None, "daily": 188.88, "text": "一行"}])
    result = asyncio.run(pipeline._set_accel_prices(page, prices))
    assert result["match"] == "count_mismatch" and result["rows_filled"] == 0
    assert page.filled == {}

    # 行数对上了但日常价算不出 → match_failed
    page = _FakeAccelDialogPage([
        {"idx": 0, "ref": 30.27, "daily": None, "text": "毛绒"},
        {"idx": 1, "ref": 168.67, "daily": None, "text": "木偶"}])
    result = asyncio.run(pipeline._set_accel_prices(page, prices))
    assert result["match"] == "match_failed" and result["rows_filled"] == 0
    assert page.filled == {}
    assert "毛绒" in result["rows_desc"]  # 行文本摘要供人工核对对话框里到底是什么行


def test_set_accel_prices_fills_each_daily_tier_with_its_cap():
    """逐档填「该档最高底价+1」，并校验 ≤ 该档上限；上限低于底价+1 的档不填、计入 over_detail。"""
    prices = [
        {"label": "30厘米/0.2kg", "daily": 74.72, "sale": 45.0, "price": 46.0},
        {"label": "40厘米/0.4kg", "daily": 188.88, "sale": 95.0, "price": 96.0},
        {"label": "50厘米/0.8kg", "daily": 188.88, "sale": 160.548, "price": 161.55},
    ]
    page = _FakeAccelDialogPage([
        {"idx": 0, "ref": 30.27, "daily": 74.72, "text": "参考申报价格：¥30.27（让价¥44.45）"},
        {"idx": 1, "ref": 168.67, "daily": 188.88, "text": "参考申报价格：¥168.67（让价¥20.21）"},
    ])
    result = asyncio.run(pipeline._set_accel_prices(page, prices))
    assert result["match"] == "ok"
    # 30.27 档上限 30.27 < 46.0（该档底价+1）→ 该档不填；188.88 档 161.55 ≤ 168.67 → 填
    assert page.filled == {1: "161.55"}
    assert result["rows_filled"] == 1 and result["rows_over"] == 1
    over = result["over_detail"][0]
    assert over["daily"] == 74.72 and over["price"] == 46.0 and over["ref"] == 30.27
    assert "上限低于该档最高底价+1" in over["reason"]


def test_match_enroll_rows_labels_all_skus_sharing_one_daily_price():
    """同日常价的多个货号共用一条页面价格行：label 必须把货号都列出来。
    真机实测教训（2026-09-24）：3 货号 6 行的页面上，只取一个货号当 label，40cm 的行
    会被写成「货号50厘米/0.8kg」，失败文案张冠李戴。"""
    sku_prices = [
        {"label": "30厘米/0.2kg", "daily": 74.72, "submit_price": 63.51},
        {"label": "40厘米/0.4kg", "daily": 188.88, "submit_price": 160.55},
        {"label": "50厘米/0.8kg", "daily": 188.88, "submit_price": 160.55},
    ]
    rows = [{"idx": i, "daily": d, "ref": 160.54}
            for i, d in enumerate([74.72, 74.72, 188.88, 188.88, 188.88, 188.88])]
    matched = pipeline.match_enroll_rows(sku_prices, rows)
    assert matched[0]["label"] == "30厘米/0.2kg"
    assert matched[2]["label"] == "40厘米/0.4kg、50厘米/0.8kg"
    assert matched[2]["labels"] == ["40厘米/0.4kg", "50厘米/0.8kg"]
    assert matched[2]["submit_price"] == 160.55  # 同日常价 → 同申报价，取谁都一样


def test_summarize_over_ref_dedupes_rows_of_same_sku():
    """超参考价文案按「货号 + 参考价」去重并在货号后标行数：提报页一个货号两行，
    不去重同一句话会重复好几遍，看不出到底几行有问题。"""
    item = {"label": "40厘米/0.4kg、50厘米/0.8kg", "daily": 188.88, "submit_price": 160.55}
    summary = pipeline.summarize_over_ref(
        [(item, 160.54), (item, 160.54), (item, 160.54), (item, 160.54)], 0.85)
    assert len(summary["notes"]) == 1
    note = summary["notes"][0]
    assert note.startswith("货号40厘米/0.4kg、50厘米/0.8kg（4 行）：")
    assert "申报价 160.55 高于提报页参考价 160.54" in note
    assert "平台认可日常价约 188.87" in note and "当前 Excel 日常价 188.88" in note
    assert note.count("——已跳过未报名") == 1  # 别在 build_over_ref_note 的结尾后再接一条尾巴
    assert summary["ref_price"] == 160.54 and summary["daily_price"] == 188.88
    assert summary["suggested_daily_price"] == 188.87
    # 单个货号单行时与改造前逐字一致（不标行数）
    single = pipeline.summarize_over_ref(
        [({"label": "默认", "daily": 8.24, "submit_price": 7.0}, 6.65)], 0.85)
    assert single["notes"] == [
        "货号默认：申报价 7.0 高于提报页参考价 6.65（按活动折扣 0.85 反推：平台认可日常价约 7.82，"
        "当前 Excel 日常价 8.24，请把该商品日常价核对为约 7.82 后重报）——已跳过未报名"]


def test_over_ref_recorded_as_failed(monkeypatch):
    """填价时申报价超过提报页参考价（Excel 与前端售价不一致）→ 记入 summary['failed']
    并带原因，exec_fill/exec_done 事件如实回报，绝不误报成功。"""
    results = [_plan("111", [("活动A", 52.57)], accel_will_close=False)]
    events = []

    async def fake_close(page, spu, allow=False):
        return {"state": "off", "closed": True, "note": "no-op"}

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        # 模拟超参考价：未填成功、over_ref 标记 + 参考价 + 原因
        return {"filled": False, "submitted": False, "over_ref": True,
                "ref_price": 47.31,
                "note": f"申报价 {sku_prices[0]['submit_price']} 高于提报页参考价 47.31（疑 Excel 日常价与前端实际售价不一致）"}

    async def fake_submit(page, allow=False, expected_spus=None):
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

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        return {"state": "off", "opened": allow, "note": ""}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        return {"filled": False, "submitted": False, "note": "搜索后定位行数=0（非唯一），保守跳过"}

    async def fake_submit(page, allow=False, expected_spus=None):
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

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        return {"filled": False, "submitted": False, "failed_step": "query",
                "note": "查询结果为 0 行"}

    async def fake_submit(page, allow=False, expected_spus=None):
        calls["submit"].append(allow)
        return {"submitted": allow, "note": ""}

    async def fake_open_accel(page, spu, allow=False, accel_prices=None, tier="super"):
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

    async def fake_enroll(_page, _spu, _act, _sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        return {"filled": True, "detail_eligible": True, "note": "已填价"}

    async def fake_submit(_page, allow=False, expected_spus=None):
        return {
            "submitted": True, "verified": False,
            "note": "已点击提交并确认，未捕获明确结果提示",
        }

    async def fake_open_accel(_page, spu, allow=False, accel_prices=None, tier="super"):
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

    async def fake_enroll(_page, _spu, _act, _sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        return {"filled": True, "detail_eligible": True, "note": "已填价"}

    async def fake_submit(_page, allow=False, expected_spus=None):
        return {"submitted": True, "verified": True, "note": "报名成功"}

    async def fake_open_accel(_page, spu, allow=False, accel_prices=None, tier="super"):
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

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        if act == "活动A":
            return {"filled": False, "submitted": False, "detail_eligible": False,
                    "note": "详情页查询结果为 0"}
        return {"filled": True, "submitted": False, "detail_eligible": True, "note": ""}

    async def fake_submit(page, allow=False, expected_spus=None):
        calls["submit"].append((page.tag, allow))
        return {"submitted": allow, "note": "已点击提交"}

    async def fake_open_accel(page, spu, allow=False, accel_prices=None, tier="super"):
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

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        calls["open"].append((spu, allow, accel_prices))
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
    # 底价（_plan 默认 sale=46.5）+1；accel_prices 是逐货号列表，断言取其中 price
    assert [(c[0], c[1], [p["price"] for p in c[2]]) for c in calls["open"]] == [("111", True, [47.5])]
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
    # 重开时加速价=底价+1（sale 默认 46.5 → 47.5）；accel_prices 是逐货号列表
    assert [(c[0], [p["price"] for p in c[1]]) for c in calls["open_price"]] == [("111", [47.5])]
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


def test_tier_selector_supports_image_label_cards():
    """档位名画进背景图时，按三张价格卡从左到右的位置序（普通/高级/超级 = 0/1/2）选档。"""
    selector = pipeline._SELECT_TIER_JS
    assert "对应申报价格" in selector
    assert "getComputedStyle(node).cursor === 'pointer'" in selector
    assert "cards.length >= 3" in selector
    assert "cards[Math.min(pos, cards.length - 1)].node.click()" in selector


def test_select_tier_waits_for_async_cards(monkeypatch):
    """抽屉先打开、档位卡后挂载时，轮询到第三次再成功。"""
    class FakeTierPage:
        def __init__(self):
            self.calls = 0
            self.args_seen = []

        async def evaluate(self, _script, args=None):
            self.calls += 1
            self.args_seen.append(args)
            return self.calls == 3

    async def no_wait(_seconds):
        return None

    page = FakeTierPage()
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    selected = asyncio.run(pipeline._select_tier(page, "super", tries=4))

    assert selected is True
    assert page.calls == 3
    assert page.args_seen[0] == ["超级", 2]  # (档名文字, 位置序号)


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
    之后点完最终按钮会间隔回查若干次）。用尽后一直返回最后一个，模拟状态不会再变。
    feedback：最终点「立即加速」后的反馈字典。
    """
    states = list(read_states)
    seen = {"n": 0}

    async def fake_dismiss(_page):
        return False

    async def fake_read(_page, _spu, search=False):
        idx = min(seen["n"], len(states) - 1)
        seen["n"] += 1
        return states[idx]

    async def fake_mark(_page, _arg=None):
        return True

    async def fake_click_marked(_page):
        return True

    async def fake_select_tier(_page, _tier, tries=6):
        return True

    async def fake_set_prices(_page, _accel_prices):
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
    monkeypatch.setattr(pipeline, "_select_tier", fake_select_tier)
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

    async def fake_once(_page, spu, allow=False, accel_prices=None, tier="super"):
        calls.append((spu, allow, accel_prices))
        return dict(next(outcomes))

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)

    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True,
                            accel_prices=_accel_prices(41.0), tries=3)
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

    async def fake_once(_page, spu, allow=False, accel_prices=None, tier="super"):
        calls.append(spu)
        return {"opened": True, "precheck_state": "on", "note": "本就在加速中，无需开启（no-op）"}

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True,
                            accel_prices=_accel_prices(41.0), tries=3)
    )
    assert result["opened"] is True and result["open_attempts"] == 1
    assert len(calls) == 1


def test_open_accel_reports_failure_after_exhausting_retries(monkeypatch):
    """三轮都未确认成功时，如实报失败并在 note 标注已重试次数。"""
    async def fake_once(_page, spu, allow=False, accel_prices=None, tier="super"):
        return {"opened": False, "note": "未捕获成功提示且回查流量页状态非加速中"}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)

    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=True,
                            accel_prices=_accel_prices(41.0), tries=3)
    )
    assert result["opened"] is False
    assert result["open_attempts"] == 3
    assert "重试 3 次仍未确认开启成功" in result["note"]


def test_open_accel_half_run_does_not_retry(monkeypatch):
    """半程 allow=False：不真开、opened 恒 False，整轮重试无意义，只跑一次。"""
    calls = []

    async def fake_once(_page, spu, allow=False, accel_prices=None, tier="super"):
        calls.append((spu, allow))
        return {"opened": False, "note": "半程：未点「立即加速」（allow=False）"}

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2801689369", allow=False,
                            accel_prices=_accel_prices(41.0), tries=3)
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
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_prices=_accel_prices(41.0))
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
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_prices=_accel_prices(41.0))
    )
    assert result["opened"] is False
    assert "回查该 SPU 非加速中" in result["note"]
    assert "已如实记为【未开启】" in result["note"]


def test_open_accel_success_toast_with_lagging_state_is_accepted_but_flagged(monkeypatch):
    """平台给了成功提示、但回查还没显示加速中 → 按【受理】记成功，并在 note 里如实标出延迟。

    实测 2026-09-25 时序：18:29 点「立即加速」拿到成功提示，18:30~18:50 回查仍是 off，
    19:0x 再看已是「流量加速中」——**平台生效有延迟（约半小时）**。若拿即时回查当判据，
    会把已受理的记成未开，还会触发外层整轮重试反复点「立即加速」。"""
    page = _patch_open_accel_full_path(
        monkeypatch,
        feedback={"status": "success", "message": "流量加速成功，可在“查看详情-近期流量加速效果”中查看明细"},
        read_states=["off", "off", "off", "off"],
    )
    result = asyncio.run(
        pipeline._open_accel_once(page, "8791757215", allow=True, accel_prices=_accel_prices(64.0))
    )
    assert result["opened"] is True
    assert result["row_state_snapshot"] == "off"
    assert "平台生效有延迟" in result["note"]
    assert "活动申报上限仍按加速价算" in result["note"]


def test_open_accel_without_success_toast_and_state_off_is_not_opened(monkeypatch):
    """平台没给成功提示、回查也非加速中 → 才是真的没开成，如实记未开启。"""
    page = _patch_open_accel_full_path(
        monkeypatch,
        feedback={"status": "unknown", "message": "等待流量加速结果提示超时"},
        read_states=["off", "off", "off", "off"],
    )
    result = asyncio.run(
        pipeline._open_accel_once(page, "8791757215", allow=True, accel_prices=_accel_prices(64.0))
    )
    assert result["opened"] is False
    assert "已如实记为【未开启】" in result["note"]


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
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_prices=_accel_prices(41.0))
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
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_prices=_accel_prices(41.0))
    )
    assert result["opened"] is False
    assert "回查该 SPU 非加速中" in result["note"]
    assert "已如实记为【未开启】" in result["note"]


def test_open_accel_success_toast_with_unreadable_state_falls_back_to_toast(monkeypatch):
    """状态读不到（unknown）时退回 toast 判据——旧行为保留，不因回查失败把已开成的记成失败。"""
    page = _patch_open_accel_full_path(
        monkeypatch,
        feedback={"status": "success", "message": "流量加速成功"},
        read_states=["off", "unknown", "unknown", "unknown"],
    )
    result = asyncio.run(
        pipeline._open_accel_once(page, "2801689369", allow=True, accel_prices=_accel_prices(41.0))
    )
    assert result["opened"] is True
    assert "平台生效有延迟" in result["note"]


# ---- 加速器三档与限流（用户规则 2026-09-29）--------------------------------------

@pytest.mark.parametrize("discount,tier", [
    (0.7, "super"), (0.75, "super"), (0.6, "super"),      # 75折及以下 → 超级档
    (0.85, "normal"),                                      # 85折 → 普通档
    (0.8, "advanced"), (0.9, "advanced"), (0.95, "advanced"),  # 其余按区间就近 → 高级档
    (None, None), (0, None), (1.5, None), ("", None),      # 读不到/越界 → None（fail-closed）
])
def test_accel_tier_for_discount(discount, tier):
    assert pipeline.accel_tier_for_discount(discount) == tier


def test_open_accel_does_not_retry_throttled(monkeypatch):
    """限流是平台状态（确定性），外层整轮重试无意义——与 match_failed 一样直接返回。"""
    calls = []

    async def fake_once(_page, spu, allow=False, accel_prices=None, tier="super"):
        calls.append(spu)
        return {"opened": False, "throttled": True, "note": "商品限流，只做活动报名"}

    monkeypatch.setattr(pipeline, "_open_accel_once", fake_once)
    result = asyncio.run(
        pipeline.open_accel(FakePage("flux"), "2879383652", allow=True,
                            accel_prices=_accel_prices(47.5), tier="super", tries=3)
    )
    assert result["throttled"] is True
    assert len(calls) == 1


def test_discount_missing_keeps_traffic_untouched(monkeypatch):
    """折扣列读不到 → 定不了档位与加速价：阶段一不关（关了开不回来是最严重的不可逆后果），
    阶段三不开；活动照常报名。逐品结果不算失败，note 如实写「流量保持现状」。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True, discount=None)]
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)

    assert calls["close"] == []          # 没关
    assert calls["open"] == []           # 也没开
    assert calls["fill"]                 # 活动照常报名
    close_ev = next(e for e in events if e["type"] == "exec_close")
    assert close_ev["ok"] is False and "折扣列读不到" in close_ev["note"]
    product = next(e for e in events if e["type"] == "exec_product_done")
    assert product["status"] == "done"   # 活动报上了就算成，不算失败
    assert product["accel_ok"] is True
    assert "折扣列读不到" in product["note"]


def test_super_tier_price_is_max_of_floor_plus_1_and_lowest_price_over_0_9(monkeypatch):
    """超级档自定义价 = max(底价+1, 最低折扣价÷0.9 向上取整)：最低折扣价 = 日常价×折扣列
    折扣率、向下取整到分。日常价100×0.7=70 → 70÷0.9=77.77…→77.78 > 底价+1=61，按 77.78 填。"""
    results = [_plan("111", [("活动A", 70.0)], accel_will_close=True,
                     daily=100.0, sale=60.0, discount=0.7)]
    summary, _events, calls = _run(results, live=True, monkeypatch=monkeypatch)

    assert calls["open_tier"] == [("111", "super")]
    assert [(c[0], [p["price"] for p in c[1]]) for c in calls["open_price"]] == [("111", [77.78])]
    assert "111" in summary["reopened"]


def test_normal_tier_opens_without_custom_price(monkeypatch):
    """85折 → 普通档：平台默认申报价、无自定义价入口，accel_prices=None 传给开启层。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True, discount=0.85)]
    _summary, _events, calls = _run(results, live=True, monkeypatch=monkeypatch)

    assert calls["open_tier"] == [("111", "normal")]
    assert calls["open_price"] == [("111", None)]


def test_advanced_tier_for_unnamed_discount(monkeypatch):
    """未点名的折扣率按区间就近归档：9折/8折（及 >85 折的）都归高级档。"""
    for discount in (0.9, 0.8, 0.95):
        results = [_plan("111", [("活动A", 10.0)], accel_will_close=True, discount=discount)]
        _summary, _events, calls = _run(results, live=True, monkeypatch=monkeypatch)
        assert calls["open_tier"] == [("111", "advanced")]
        assert calls["open_price"] == [("111", None)]


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

    async def fake_open(page, spu, allow=False, accel_prices=None, tier="super"):
        calls["open"].append(spu)
        return {"state": "on", "opened": True, "note": "no-op"}

    async def fake_open_page(activity_page, name, timeout_s=25):
        return FakePage(f"enroll:{name}")

    async def fake_enroll(page, spu, act, sku_prices, allow_submit=False, on_step=None,
                          discount_rate=None):
        calls["fill"].append(spu)
        return {"filled": True, "submitted": False, "note": ""}

    async def fake_submit(page, allow=False, expected_spus=None):
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


def test_selection_excludes_unchecked_cells_and_their_traffic(monkeypatch):
    """识别矩阵勾选的 (SPU,活动) 裁剪：没勾的格子不报；一个格子都没勾的 SPU 连流量都不动。"""
    results = [
        _plan("111", [("活动A", 10.0), ("活动B", 20.0)], accel_will_close=True),
        _plan("222", [("活动A", 11.0)], accel_will_close=True),
    ]
    for res in results:
        service._filter_result(res, [["111", "活动B"]], None)
    assert [e["activity"] for e in results[0]["enrolled_activities"]] == ["活动B"]
    assert results[1]["status"] == "skip_nomatch"  # 222 一格没勾 → 不进执行遍
    assert "没有勾选" in results[1]["note"]

    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch)
    assert [c[0] for c in calls["close"]] == ["111"]  # 222 的流量保持原样
    assert calls["open_page"] == ["活动B"]
    assert {c[1] for c in calls["fill"]} == {"111"}
    assert [c[0] for c in calls["open"]] == ["111"]


def test_skip_cell_during_execution_records_without_submitting(monkeypatch):
    """执行中逐格跳过：该格不填价、本活动没有可提交商品 → 不提交；
    且跳过不算失败（对账豁免），否则会出假失败。本 SPU 初始 off、本批零提交，但记录页
    显示活动A 已有生效报名 → 2026-09-30 起照常补开流量，故最终状态是 done（旧口径是
    skip 且完全不碰流量）。跳过本身仍不是失败：它只是「本批不报这个活动」。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=False)]
    control = ActivityControl()
    control.request_skip("111", "活动A")

    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch, control=control)

    assert calls["fill"] == [] and calls["submit"] == []
    skip_ev = next(e for e in events if e["type"] == "exec_cell_skip")
    assert skip_ev["spu"] == "111" and skip_ev["where"] == "queued"
    # 前端据 exec_enroll.status 渲染（activity_results["status"] 之后会被对账统一改写为
    # log_verified/not_verified，那是既有的对账口径，不在这里断言）
    enroll_ev = next(e for e in events if e["type"] == "exec_enroll")
    assert enroll_ev["status"] == "skipped_by_user" and enroll_ev["skipped"] == ["111"]
    assert enroll_ev["ok"] is False and "按操作者指令跳过" in enroll_ev["note"]
    assert summary["skipped_cells"] == [["111", "活动A"]]
    assert summary["failed"] == []           # 跳过不是失败
    assert summary["errors"] == []           # 也不进「报名记录未确认」
    # 已有生效报名 → 补开流量；这条即使整批零提交也照样走
    assert summary["opened_by_existing"] == {"111": ["活动A"]}
    assert calls["open"] == [("111", True)]
    assert summary["product_results"][0]["status"] == "done"


def test_skip_is_refused_after_cell_filled(monkeypatch):
    """已填价的格子拒绝跳过：活动级一次提交已经把它报上去了，假装跳过只会误导操作者。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=False)]
    control = ActivityControl()
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch, control=control)
    assert calls["submit"]  # 照常提交
    assert control.is_locked("111", "活动A") is True
    assert control.request_skip("111", "活动A") == {"accepted": False, "reason": "already_filled"}
    assert not any(e["type"] == "exec_cell_skip" for e in events)


def test_pause_blocks_before_any_change_then_resumes(monkeypatch):
    """暂停在安全边界生效：挂起期间不点任何不可逆按钮；恢复后整批照跑、流量照开。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    control = ActivityControl()
    control.set_paused(True)

    async def scenario():
        coro, events, calls = _run(
            results, live=True, monkeypatch=monkeypatch, control=control, as_task=True)
        task = asyncio.create_task(coro)
        for _ in range(200):  # 等闸门真正停住（发过 paused 事件才算停住）
            if any(e["type"] == "paused" for e in events):
                break
            await asyncio.sleep(0.01)
        assert any(e["type"] == "paused" for e in events)
        assert calls["close"] == [] and calls["submit"] == []  # 挂起期间零不可逆动作
        assert not task.done()
        control.set_paused(False)
        return await asyncio.wait_for(task, timeout=5), events, calls

    summary, events, calls = asyncio.run(scenario())
    pause_ev = next(e for e in events if e["type"] == "paused")
    assert pause_ev["scope"] == "close"
    assert [e["type"] for e in events if e["type"] in {"paused", "resumed"}] == ["paused", "resumed"]
    assert calls["close"] == [("111", True)] and calls["open"] == [("111", True)]
    assert "111" in summary["reopened"]


def test_pause_requested_after_enroll_is_refused_and_traffic_still_restored(monkeypatch):
    """阶段二末尾才点暂停（竞态）：阶段三强制继续并如实发 pause_refused——
    绝不把已关闭的流量留在关闭态（这是本管线最严重的不可逆后果）。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    control = ActivityControl()

    async def fake_enroll_all(by_activity, activity_page, live, on_progress, summary, control=None):
        control.set_paused(True)  # 恰在报名遍收尾、阶段三入场前被置位

    monkeypatch.setattr(service, "_enroll_by_activity", fake_enroll_all)
    summary, events, calls = _run(results, live=True, monkeypatch=monkeypatch, control=control)

    refused = next(e for e in events if e["type"] == "pause_refused")
    assert refused["reason"] == "reopen_phase"
    assert control.paused is False and control.phase == "done"
    assert calls["close"] == [("111", True)]   # 关过
    assert calls["open"] == [("111", True)]    # 并成功开回来
    assert "111" in summary["reopened"]


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
        pipeline._open_accel_once(FakePage("flux"), "9072868889", allow=True,
                                  accel_prices=_accel_prices(78.77))
    )

    assert calls == [("9072868889", True)]
    assert result["opened"] is True and result["precheck_state"] == "on"
    assert "无需开启" in result["note"]


def test_throttled_product_is_not_opened_via_enroll_entry(monkeypatch):
    """限流品的加速器列是「商品流量待关注 / 您可加速提效」+「报名流量加速器 / 调价提效」
    （真机实测 2026-09-24）。用户 2026-09-29 定：这种品停止流量加速动作、只做活动报名——
    「报名流量加速器」绝不能当开启入口点，入口只试「立即开启」。"""
    tried = []

    async def fake_dismiss(_page):
        return False

    async def fake_read(_page, spu, search=False):
        return "off"

    class FakeFlux:
        async def evaluate(self, script, arg=None):
            if script is pipeline._MARK_ROW_ACTION_JS:
                tried.append(arg[1])
                return None  # 行内没有「立即开启」
            if script is pipeline._ROW_TEXT_JS:
                return "商品流量待关注 / 您可加速提效 / 报名流量加速器"
            return None

    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", fake_dismiss)
    monkeypatch.setattr(pipeline, "read_accel_state", fake_read)

    result = asyncio.run(pipeline._open_accel_once(FakeFlux(), "111", allow=False))

    assert tried == ["立即开启"]  # 只认正常入口，绝不试「报名流量加速器」
    assert result["throttled"] is True
    assert result["opened"] is False
    assert "限流" in result["note"]


def test_open_accel_without_entry_reports_accel_column_text(monkeypatch):
    """「立即开启」没有、行文本也不是限流现场时，note 要带上加速器列的现场文本：
    平台没给入口和页面结构变了是两回事，只报「未定位到」操作者分不出来。"""
    async def fake_dismiss(_page):
        return False

    async def fake_read(_page, spu, search=False):
        return "off"

    class FakeFlux:
        async def evaluate(self, script, arg=None):
            if script is pipeline._MARK_ROW_ACTION_JS:
                return None
            if script is pipeline._ROW_TEXT_JS:
                return "流量加速机会已用完"
            return None

    monkeypatch.setattr(pipeline, "dismiss_all_page_popups", fake_dismiss)
    monkeypatch.setattr(pipeline, "read_accel_state", fake_read)

    result = asyncio.run(pipeline._open_accel_once(FakeFlux(), "111", allow=False))
    assert "未定位到该行的加速器入口" in result["note"]
    assert "流量加速机会已用完" in result["note"]
    assert result["opened"] is False and not result.get("throttled")
    assert result.get("entry_word") is None


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
            self.url = f"https://{GLOBAL_HOST}{pipeline.ACTIVITY_PATH}"
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


def test_sweep_late_detail_tabs_closes_only_new_ones(monkeypatch):
    """放弃后平台才迟到的提报页必须被关掉：它们会一直堆着（用户看到的「开了多个」），
    还会让下一次运行的区域确认失败（提报页没有顶栏区域切换器）。只关本次新出现的页签。"""
    class P:
        def __init__(self, url):
            self.url = url
            self.closed = False

        async def close(self):
            self.closed = True

    class Ctx:
        def __init__(self, pages):
            self.pages = pages

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    manual = P("https://agentseller.temu.com/activity/marketing-activity/detail-new?manual")
    listing = P("https://agentseller.temu.com/activity/marketing-activity")
    ctx = Ctx([listing, manual])
    before = list(ctx.pages)
    late = P("https://agentseller.temu.com/activity/marketing-activity/detail-new?type=13")
    ctx.pages.append(late)  # 本函数调用之后才出现的

    closed = asyncio.run(pipeline.sweep_late_detail_tabs(ctx, before, tries=3, interval=0))

    assert closed == [late.url]
    assert late.closed is True
    assert manual.closed is False           # 操作者原本打开的那个不动
    # 没有迟到页时等到超时返回空，不关任何东西
    ctx2 = Ctx([listing, manual])
    assert asyncio.run(
        pipeline.sweep_late_detail_tabs(ctx2, list(ctx2.pages), tries=2, interval=0)) == []


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

    async def fetch_next(before_page):
        # 2026-10-03 起 fetch_next 收「翻页前页码」、返回 (result, 实际到达页码)：
        # beast 分页超窗口时下一页是跳页块，点击次数不等于页码。
        requested_pages.append(before_page)
        return {"total": 14, "list": [{"enrollId": index} for index in range(11, 15)]}, 2

    result = asyncio.run(pipeline._collect_activity_log_pages(first, fetch_next))

    assert requested_pages == [1]
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
            self.url = f"https://{GLOBAL_HOST}{pipeline.ACTIVITY_PATH}"
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


def test_match_enroll_rows_prefers_sku_label_over_platform_daily_price():
    """日常价以表为准（用户 2026-09-24 定）：平台显示的日常价可能过期，不能因为与表不一致
    就整单不报。真机实测（2026-09-24）8791757215：表里两个货号都写 163.23，平台那行显示
    188.88，旧逻辑按日常价配对直接整单 fail-closed。现在按平台货号字段（归一化后与表一致）
    配对，价格仍按表算。"""
    sku_prices = [
        {"label": "90cm0.4kg", "daily": 163.23, "submit_price": 138.74},
        {"label": "70cm0.2kg", "daily": 163.23, "submit_price": 138.74},
    ]
    rows = [  # 平台两行的日常价 163.23 / 188.88，与表对不上；货号字段能对上
        {"idx": 0, "label": "90cm04kg", "daily": 163.23, "ref": 138.74},
        {"idx": 1, "label": "70cm02kg", "daily": 188.88, "ref": 160.54},
    ]
    matched = pipeline.match_enroll_rows(sku_prices, rows)
    assert matched[0]["label"] == "90cm0.4kg" and matched[1]["label"] == "70cm0.2kg"
    assert matched[1]["submit_price"] == 138.74      # 价按表算，不按平台显示的 188.88
    assert matched[1]["matched_by"] == "label"
    # 平台货号读不到时退回日常价配对（这类页面平台价通常与表一致）
    plain = [{"idx": 0, "daily": 163.23, "ref": 138.74}, {"idx": 1, "daily": 163.23, "ref": 138.74}]
    fallback = pipeline.match_enroll_rows(sku_prices, plain)
    assert fallback[0]["matched_by"] == "daily"
    # 两条键都对不上 → fail-closed
    assert pipeline.match_enroll_rows(
        sku_prices, [{"idx": 0, "label": "别的货号", "daily": 999.0}]) is None
    # 货号重复（身份不可判）时不拿它当键，退回日常价
    dup = [{"label": "同款", "daily": 163.23, "submit_price": 1.0},
           {"label": "同款", "daily": 163.23, "submit_price": 2.0}]
    assert pipeline.match_enroll_rows(dup, plain)[0]["matched_by"] == "daily"


def test_norm_sku_label_strips_punctuation():
    assert pipeline.norm_sku_label("70cm0.2kg") == "70cm02kg"
    assert pipeline.norm_sku_label("70cm02kg") == "70cm02kg"
    assert pipeline.norm_sku_label("  30厘米/0.2kg ") == "30厘米02kg"
    assert pipeline.norm_sku_label(None) == ""


def test_submit_without_feedback_captures_page_scene(monkeypatch):
    """点了提交却既无二次确认也无成功/失败提示时（实测 2026-09-25 平台侧最终 0 条记录），
    必须把页面现场（行内报错文本 + URL）写进 note——否则只剩「已点击提交」没法定位原因。"""
    class FakeSubmitPage:
        url = "https://agentseller.temu.com/activity/marketing-activity/detail-new?x=1"

        def __init__(self):
            self.clicked = 0

        def get_by_role(self, _role, name=None):
            page = self

            class _Btn:
                @property
                def first(self):
                    return self

                async def count(self):
                    return 1

                async def is_disabled(self):
                    return False

                async def click(self, timeout=None):
                    page.clicked += 1

            return _Btn()

        async def evaluate(self, script, *_args):
            if script is pipeline._SUBMIT_SCENE_JS:
                return "无资格参加该活动 / 库存不足 @ /activity/marketing-activity/detail-new"
            return {"status": "pending", "message": ""}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeSubmitPage()
    result = asyncio.run(pipeline.submit_enroll_page(page, allow=True, feedback_tries=2))

    assert page.clicked == 1
    assert result["submitted"] is True and result["verified"] is False
    assert "无资格参加该活动" in result["scene"]
    assert "提交后页面现场" in result["note"]


def test_off_spu_with_failed_reopen_says_why_not_a_generic_failure(monkeypatch):
    """初始 off 的商品：报名成功但加速器没开起来时，逐品文案要说清「报名已提交，但加速器未开启：
    <现场原因>」。真机 2026-09-25 踩到：加速器因「档上限低于底价」按设计中止，报告却写成
    「流量最终状态未完成」，看起来像整单失败。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=False)]
    summary, _events, _calls = _run(
        results, live=True, monkeypatch=monkeypatch,
        open_result={"opened": False, "state": "off",
                     "note": "加速价按日常价档校验后中止不开：日常价 74.72 档 上限 30.27 低于底价 45"},
    )

    outcome = summary["product_results"][0]
    assert outcome["status"] == "fail"          # 目标没达成，仍如实记 fail
    assert outcome["submitted"] == 1
    assert "报名已提交，但加速器未开启" in outcome["note"]
    assert "上限 30.27 低于底价 45" in outcome["note"]
    assert "流量最终状态未完成" not in outcome["note"]


def test_on_spu_not_reopened_still_reports_traffic_not_restored(monkeypatch):
    """初始 on 且被本管线关掉的商品：没恢复开启时文案是「流量未恢复开启」——这是最严重的一类，
    不能和「本来 off、只是没开起来」混为一谈。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    summary, _events, _calls = _run(
        results, live=True, monkeypatch=monkeypatch,
        open_result={"opened": False, "state": "on", "note": "回查流量页状态非加速中"},
    )
    outcome = summary["product_results"][0]
    assert outcome["status"] == "fail"
    assert "流量未恢复开启" in outcome["note"]


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
            self.url = f"https://{GLOBAL_HOST}{pipeline.ACTIVITY_PATH}"
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


def test_sweep_late_detail_tabs_closes_only_new_ones(monkeypatch):
    """放弃后平台才迟到的提报页必须被关掉：它们会一直堆着（用户看到的「开了多个」），
    还会让下一次运行的区域确认失败（提报页没有顶栏区域切换器）。只关本次新出现的页签。"""
    class P:
        def __init__(self, url):
            self.url = url
            self.closed = False

        async def close(self):
            self.closed = True

    class Ctx:
        def __init__(self, pages):
            self.pages = pages

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    manual = P("https://agentseller.temu.com/activity/marketing-activity/detail-new?manual")
    listing = P("https://agentseller.temu.com/activity/marketing-activity")
    ctx = Ctx([listing, manual])
    before = list(ctx.pages)
    late = P("https://agentseller.temu.com/activity/marketing-activity/detail-new?type=13")
    ctx.pages.append(late)  # 本函数调用之后才出现的

    closed = asyncio.run(pipeline.sweep_late_detail_tabs(ctx, before, tries=3, interval=0))

    assert closed == [late.url]
    assert late.closed is True
    assert manual.closed is False           # 操作者原本打开的那个不动
    # 没有迟到页时等到超时返回空，不关任何东西
    ctx2 = Ctx([listing, manual])
    assert asyncio.run(
        pipeline.sweep_late_detail_tabs(ctx2, list(ctx2.pages), tries=2, interval=0)) == []


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

    async def fetch_next(before_page):
        # 2026-10-03 起 fetch_next 收「翻页前页码」、返回 (result, 实际到达页码)：
        # beast 分页超窗口时下一页是跳页块，点击次数不等于页码。
        requested_pages.append(before_page)
        return {"total": 14, "list": [{"enrollId": index} for index in range(11, 15)]}, 2

    result = asyncio.run(pipeline._collect_activity_log_pages(first, fetch_next))

    assert requested_pages == [1]
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
            self.url = f"https://{GLOBAL_HOST}{pipeline.ACTIVITY_PATH}"
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


def test_match_enroll_rows_prefers_sku_label_over_platform_daily_price():
    """日常价以表为准（用户 2026-09-24 定）：平台显示的日常价可能过期，不能因为与表不一致
    就整单不报。真机实测（2026-09-24）8791757215：表里两个货号都写 163.23，平台那行显示
    188.88，旧逻辑按日常价配对直接整单 fail-closed。现在按平台货号字段（归一化后与表一致）
    配对，价格仍按表算。"""
    sku_prices = [
        {"label": "90cm0.4kg", "daily": 163.23, "submit_price": 138.74},
        {"label": "70cm0.2kg", "daily": 163.23, "submit_price": 138.74},
    ]
    rows = [  # 平台两行的日常价 163.23 / 188.88，与表对不上；货号字段能对上
        {"idx": 0, "label": "90cm04kg", "daily": 163.23, "ref": 138.74},
        {"idx": 1, "label": "70cm02kg", "daily": 188.88, "ref": 160.54},
    ]
    matched = pipeline.match_enroll_rows(sku_prices, rows)
    assert matched[0]["label"] == "90cm0.4kg" and matched[1]["label"] == "70cm0.2kg"
    assert matched[1]["submit_price"] == 138.74      # 价按表算，不按平台显示的 188.88
    assert matched[1]["matched_by"] == "label"
    # 平台货号读不到时退回日常价配对（这类页面平台价通常与表一致）
    plain = [{"idx": 0, "daily": 163.23, "ref": 138.74}, {"idx": 1, "daily": 163.23, "ref": 138.74}]
    fallback = pipeline.match_enroll_rows(sku_prices, plain)
    assert fallback[0]["matched_by"] == "daily"
    # 两条键都对不上 → fail-closed
    assert pipeline.match_enroll_rows(
        sku_prices, [{"idx": 0, "label": "别的货号", "daily": 999.0}]) is None
    # 货号重复（身份不可判）时不拿它当键，退回日常价
    dup = [{"label": "同款", "daily": 163.23, "submit_price": 1.0},
           {"label": "同款", "daily": 163.23, "submit_price": 2.0}]
    assert pipeline.match_enroll_rows(dup, plain)[0]["matched_by"] == "daily"


def test_norm_sku_label_strips_punctuation():
    assert pipeline.norm_sku_label("70cm0.2kg") == "70cm02kg"
    assert pipeline.norm_sku_label("70cm02kg") == "70cm02kg"
    assert pipeline.norm_sku_label("  30厘米/0.2kg ") == "30厘米02kg"
    assert pipeline.norm_sku_label(None) == ""


def test_submit_without_feedback_captures_page_scene(monkeypatch):
    """点了提交却既无二次确认也无成功/失败提示时（实测 2026-09-25 平台侧最终 0 条记录），
    必须把页面现场（行内报错文本 + URL）写进 note——否则只剩「已点击提交」没法定位原因。"""
    class FakeSubmitPage:
        url = "https://agentseller.temu.com/activity/marketing-activity/detail-new?x=1"

        def __init__(self):
            self.clicked = 0

        def get_by_role(self, _role, name=None):
            page = self

            class _Btn:
                @property
                def first(self):
                    return self

                async def count(self):
                    return 1

                async def is_disabled(self):
                    return False

                async def click(self, timeout=None):
                    page.clicked += 1

            return _Btn()

        async def evaluate(self, script, *_args):
            if script is pipeline._SUBMIT_SCENE_JS:
                return "无资格参加该活动 / 库存不足 @ /activity/marketing-activity/detail-new"
            return {"status": "pending", "message": ""}

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)
    page = FakeSubmitPage()
    result = asyncio.run(pipeline.submit_enroll_page(page, allow=True, feedback_tries=2))

    assert page.clicked == 1
    assert result["submitted"] is True and result["verified"] is False
    assert "无资格参加该活动" in result["scene"]
    assert "提交后页面现场" in result["note"]


def test_off_spu_with_failed_reopen_says_why_not_a_generic_failure(monkeypatch):
    """初始 off 的商品：报名成功但加速器没开起来时，逐品文案要说清「报名已提交，但加速器未开启：
    <现场原因>」。真机 2026-09-25 踩到：加速器因「档上限低于底价」按设计中止，报告却写成
    「流量最终状态未完成」，看起来像整单失败。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=False)]
    summary, _events, _calls = _run(
        results, live=True, monkeypatch=monkeypatch,
        open_result={"opened": False, "state": "off",
                     "note": "加速价按日常价档校验后中止不开：日常价 74.72 档 上限 30.27 低于底价 45"},
    )

    outcome = summary["product_results"][0]
    assert outcome["status"] == "fail"          # 目标没达成，仍如实记 fail
    assert outcome["submitted"] == 1
    assert "报名已提交，但加速器未开启" in outcome["note"]
    assert "上限 30.27 低于底价 45" in outcome["note"]
    assert "流量最终状态未完成" not in outcome["note"]


def test_on_spu_not_reopened_still_reports_traffic_not_restored(monkeypatch):
    """初始 on 且被本管线关掉的商品：没恢复开启时文案是「流量未恢复开启」——这是最严重的一类，
    不能和「本来 off、只是没开起来」混为一谈。"""
    results = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    summary, _events, _calls = _run(
        results, live=True, monkeypatch=monkeypatch,
        open_result={"opened": False, "state": "on", "note": "回查流量页状态非加速中"},
    )
    outcome = summary["product_results"][0]
    assert outcome["status"] == "fail"
    assert "流量未恢复开启" in outcome["note"]


def test_cooldown_locked_accel_explains_why_reporting_must_fail():
    """加速器处于 24h 锁定期（关不掉）→ 前端售价按加速价、活动申报上限被压低、报名必被平台拒。
    这时 SPU 级结论要直说这条因果，而不是只报「提交 0」。

    真机 2026-09-25：SPU 8791757215 的关闭被平台拒（「加速器开启后需满24小时才可手动关闭」），
    随后提报页参考价被压到加速价×0.9（89.04/57.6），按表里日常价算的 146.9 必被拒。"""
    plans = [_plan("111", [("活动A", 10.0)], accel_will_close=True)]
    summary = {
        "closed": [], "reopened": [], "cooldown": ["111"],
        "enrolled_activities": {"活动A": []}, "ineligible_activities": {},
        "activity_results": {}, "errors": [],
        "accel_checks": {"111": {"state": "on", "closed": False, "cooldown": True,
                                 "note": "未关闭：加速器开启后需满24小时才可手动关闭"}},
        "failed": [{"spu": "111", "activity": "活动A", "over_ref": True,
                    "reason": "申报价 146.9 高于提报页参考价 89.04"}],
    }
    outcome = service._execution_product_results(plans, summary, True)[0]
    assert outcome["status"] == "fail"
    assert "24 小时锁定期" in outcome["note"]
    assert "报名必被平台拒" in outcome["note"]
