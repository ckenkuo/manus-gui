# -*- coding: utf-8 -*-
r"""成分类字段的多余行裁剪（不碰 CDP、不碰 LLM）。

钉住 2026-08-30 那批日志里反复出现的「错误：成分不能重复选择」：

前端判重的判据从编辑页 bundle（Layout-*.js）取证——selectPercent 属性逐行取
attrValueId，`if (n.includes(p)) return C(\`${t._label}不能重复选择\`)`，且这道闸排在
「百分比之和=100」之前。而管线写入只覆盖前 N 行、_ensure_comp_rows 又只加不减，
于是页面初始行数多于本轮重建结果时，多出来的旧行原样留着。

真站复现（rowid 173539495458369319，2026-08-30）：把「成分」撑到 3 行（第 3 行是
「棉」），再按 2 行写入（聚酯纤维 60% + 棉 40%），回读得到
['聚酯纤维(涤纶）', '棉', '棉'] —— 同字段两行同纤维，保存即被平台拦下。
"""

from publish_patching import patch_publish
import pytest

from app.publish.pipeline import _apply_attr_changes, _trim_comp_rows


class _FakeSession:
    """记录每次 eval_json 的 JS，并按脚本类型返回够用的结果。

    只需要区分三类：裁行脚本（含 icon_remove）、加行脚本（含 icon_add）、其余回读。
    行数用 self.rows 模拟，裁行脚本按 keep 目标削到位——判「裁到几行」的逻辑在 JS 里，
    这里只验证 Python 侧【有没有按正确的目标行数发出裁剪】。
    """

    def __init__(self, rows: int = 3):
        self.rows = rows
        self.trim_calls: list = []
        self.js_log: list = []

    async def eval_json(self, js, *a, **kw):
        self.js_log.append(js)
        if "icon_remove" in js:
            # 从脚本里取出 __KEEP__ 替换后的目标行数（`rows() > N`）
            import re
            m = re.search(r"rows\(\) > (\d+)", js)
            keep = int(m.group(1)) if m else 1
            before = self.rows
            self.rows = min(self.rows, keep)
            self.trim_calls.append({"keep": keep, "before": before,
                                    "after": self.rows})
            return {"before": before, "after": self.rows,
                    "trimmed": before - self.rows}
        return {}


@pytest.mark.asyncio
async def test_裁到本轮写入的行数():
    """页面 3 行、本轮只写 2 行 → 裁掉第 3 行。"""
    s = _FakeSession(rows=3)
    r = await _trim_comp_rows(s, "成分", 2)
    assert r["trimmed"] == 1 and r["after"] == 2, r
    assert s.trim_calls[0]["keep"] == 2


@pytest.mark.asyncio
async def test_行数已相等不裁():
    s = _FakeSession(rows=2)
    r = await _trim_comp_rows(s, "成分", 2)
    assert r["trimmed"] == 0 and r["after"] == 2, r


@pytest.mark.asyncio
async def test_keep小于1按1处理():
    """首行没有 icon_remove、删不掉，keep=0 只会空转，故一律按 1 兜底。"""
    s = _FakeSession(rows=3)
    await _trim_comp_rows(s, "成分", 0)
    assert s.trim_calls[0]["keep"] == 1


class _ApplySession(_FakeSession):
    """_apply_attr_changes 需要的最小面：set_attr 走 eval_json，这里一律当成功。"""

    async def eval_json(self, js, *a, **kw):
        r = await super().eval_json(js, *a, **kw)
        if "icon_remove" in js:
            return r
        # set_attr 的回读要拿到目标值才算 ok；这里不校验写入本身，返回空即可
        return {}


@pytest.mark.asyncio
async def test_写完成分后按最大row裁剪(monkeypatch):
    """两行成分写完 → 用 row=2 去裁，多余的第 3 行才会被削掉。"""
    from app.publish import pipeline

    async def _fake_set_attr(session, label, value, num=None, row_no=1, kind="select"):
        return {"status": "ok", "label": label, "value": value,
                "readback": {"label": label, "current": value}}

    patch_publish(monkeypatch, "pipeline", "set_attr", _fake_set_attr)
    s = _ApplySession(rows=3)
    changes = [
        {"label": "成分", "value": "聚酯纤维(涤纶）", "num": 60, "row": 1},
        {"label": "成分", "value": "棉", "num": 40, "row": 2},
    ]
    row_map = {"成分": {"label": "成分", "kind": "select", "hasPercent": True}}
    await _apply_attr_changes(s, changes, row_map, {"title": "t"}, None,
                              use_cache=False)
    assert s.trim_calls, "写完成分行后必须发一次裁剪"
    assert s.trim_calls[0]["keep"] == 2
    assert s.trim_calls[0]["after"] == 2


@pytest.mark.asyncio
async def test_非成分字段不裁(monkeypatch):
    """普通下拉行没有百分比，绝不该被当成成分字段去裁行。"""
    from app.publish import pipeline

    async def _fake_set_attr(session, label, value, num=None, row_no=1, kind="select"):
        return {"status": "ok", "label": label, "value": value,
                "readback": {"label": label, "current": value}}

    patch_publish(monkeypatch, "pipeline", "set_attr", _fake_set_attr)
    s = _ApplySession(rows=3)
    changes = [{"label": "织造方式", "value": "梭织", "num": None, "row": None}]
    row_map = {"织造方式": {"label": "织造方式", "kind": "select"}}
    await _apply_attr_changes(s, changes, row_map, {"title": "t"}, None,
                              use_cache=False)
    assert s.trim_calls == []


@pytest.mark.asyncio
async def test_数值行不当成成分裁(monkeypatch):
    """里料克重带的是物性数值（kind=number），与成分百分比无关。"""
    from app.publish import pipeline

    async def _fake_set_attr(session, label, value, num=None, row_no=1, kind="select"):
        return {"status": "ok", "label": label, "value": value,
                "readback": {"label": label, "current": value}}

    patch_publish(monkeypatch, "pipeline", "set_attr", _fake_set_attr)
    s = _ApplySession(rows=3)
    changes = [{"label": "里料克重（g/m²)", "value": "120", "kind": "number",
                "num": 120, "row": 1}]
    row_map = {"里料克重（g/m²)": {"label": "里料克重（g/m²)", "kind": "number"}}
    await _apply_attr_changes(s, changes, row_map, {"title": "t"}, None,
                              use_cache=False)
    assert s.trim_calls == []


@pytest.mark.asyncio
async def test_裁剪异常不影响写入(monkeypatch):
    """best-effort：裁不动只记日志，已写好的值不能被连坐。"""
    from app.publish import pipeline

    async def _fake_set_attr(session, label, value, num=None, row_no=1, kind="select"):
        return {"status": "ok", "label": label, "value": value,
                "readback": {"label": label, "current": value}}

    async def _boom(session, label, keep):
        raise RuntimeError("页面没了")

    patch_publish(monkeypatch, "pipeline", "set_attr", _fake_set_attr)
    patch_publish(monkeypatch, "pipeline", "_trim_comp_rows", _boom)
    s = _ApplySession(rows=3)
    changes = [{"label": "成分", "value": "棉", "num": 100, "row": 1}]
    row_map = {"成分": {"label": "成分", "kind": "select", "hasPercent": True}}
    applied, _refreshed, _comp = await _apply_attr_changes(
        s, changes, row_map, {"title": "t"}, None, use_cache=False)
    assert applied and applied[0]["result"] == "ok"
