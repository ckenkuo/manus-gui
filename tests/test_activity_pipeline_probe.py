"""probe_detail_eligibility 的用例。

它是「某商品在某活动里到底能不能报」的唯一判定，识别扫描与报名共用。这里锁死三件：
0 行=不可报（平台的正常业务结果）、1 行=可报、其余一律 fail-closed 返 None；
以及无论哪条分支都只做只读动作（搜索框 fill + 点「查询」，绝不勾选/填价/提交）。
"""
import asyncio

import pytest

from app.activity import pipeline


class _FakeInput:
    def __init__(self, page):
        self.page = page

    async def fill(self, value):
        self.page.filled = value


class _FakeRows:
    def __init__(self, count):
        self._count = count

    def filter(self, **_kwargs):
        return self

    async def count(self):
        return self._count


class _FakeProbePage:
    """搜索框定位结果 / 查询按钮是否点到 / 结果行数 三个开关模拟详情页。"""

    def __init__(self, search="ok", rows=1, query_clicked=True):
        self.search = search
        self.rows = rows
        self.query_clicked = query_clicked
        self.scripts = []
        self.filled = None

    async def evaluate(self, script, arg=None):
        self.scripts.append(script)
        if script == pipeline._SEARCH_SPU_JS:
            return self.search
        return self.query_clicked  # 另一处 evaluate 只有「点查询」这一段的脚本

    def locator(self, selector):
        if selector == '[data-kiro-spu="1"]':
            return _FakeInput(self)
        return _FakeRows(self.rows)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(pipeline.asyncio, "sleep", no_wait)


def _run(page, steps):
    async def on_step(event):
        steps.append(event)

    return asyncio.run(pipeline.probe_detail_eligibility(page, "2879383652", on_step=on_step))


def test_one_row_means_eligible():
    page = _FakeProbePage(rows=1)
    steps = []
    out = _run(page, steps)
    assert out == {"queried": True, "detail_eligible": True, "failed_step": None, "note": ""}
    assert page.filled == "2879383652"
    assert [(s["step"], s["ok"]) for s in steps] == [("input_spu", True), ("query", True)]
    # 只读：除搜索框 fill 与点「查询」外没有任何页面动作
    assert len(page.scripts) == 2


def test_zero_rows_means_ineligible_but_not_an_error():
    page = _FakeProbePage(rows=0)
    steps = []
    out = _run(page, steps)
    assert out["queried"] is True and out["detail_eligible"] is False
    assert out["failed_step"] is None  # 不是错误：平台正常业务结果，前端按 info 展示
    assert "详情页查询结果为 0" in out["note"]
    assert "列表页价格初筛通过不等于详情资格通过" in out["note"]
    assert steps[-1] == {"step": "query", "ok": True, "note": out["note"]}


def test_multiple_rows_fail_closed():
    out = _run(_FakeProbePage(rows=2), [])
    assert out["detail_eligible"] is None and out["failed_step"] == "query"
    assert "非唯一" in out["note"] and out["queried"] is False


def test_search_box_missing_is_failed_step_input_spu():
    steps = []
    out = _run(_FakeProbePage(search="no-label"), steps)
    assert out["failed_step"] == "input_spu" and out["queried"] is False
    assert "未定位到 SPU 搜索框" in out["note"]
    assert [(s["step"], s["ok"]) for s in steps] == [("input_spu", False)]


def test_query_button_missing_fails_before_counting():
    page = _FakeProbePage(query_clicked=False)
    out = _run(page, [])
    assert out["failed_step"] == "query" and "未点到「查询」按钮" == out["note"]
    assert out["detail_eligible"] is None
