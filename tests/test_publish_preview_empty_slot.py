"""⑦b 预览图空图位绝不能算进「均已满足」（2026-09-01 实测两单）。

取证：1067271196776、1051827161006 两单 save 被平台拒「错误：请上传预览图」，
而 ⑦b 之前判的是 skipped、note 还写「6 行预览图均已满足 1:1 且不小于 800x800」。
成因是状态判据只看得见 <img>：格子里压根没有 img 的行既进不了 bad（bad 要求
读到尺寸），也进不了 unknown（unknown 要求有 img 但 naturalWidth=0），于是被
当成合规行放过，错误一路延后到 ⑭ 才以「保存可能未生效」这种含糊结论暴露。

2026-09-12 起对补不上的空行【反选该规格】而不是停在「需人工补」：空预览图是平台
硬拒项，一行补不上整单就发不出去，而人工往往也没有图可补（认领没带图的多是源站的
占位/配件规格）。反选后平台重建变种表，那一行连同图位一起消失，其余规格照常发布。
故本文件钉的是：能补的补、补不上的反选、反选不掉的才如实判 fail。
"""

from publish_patching import patch_publish
import pytest

from app.publish import service


class _FakeSession:
    """反选与重读都要走 eval_json，这里按 JS 片段特征分派最小返回值。"""

    def __init__(self, uncheck=None, rows_after=None):
        # uncheck: 反选调用的返回值（默认「点到且已取消」）
        self.uncheck = uncheck if uncheck is not None else {
            "found": True, "wasChecked": True, "clicked": True,
            "checked": False, "group": "颜色"}
        self.rows_after = rows_after or []
        self.unchecked = []

    async def eval_json(self, js):
        if "const WANT = " in js:
            head = "const WANT = "
            i = js.index(head) + len(head)
            self.unchecked.append(js[i:js.index("\n", i)].strip().rstrip(";").strip('"'))
            return self.uncheck
        if "tbody" in js and "JSON.stringify({n:" in js:
            return {"n": len(self.rows_after)}
        return {}


async def _run(rows, monkeypatch, *, has_trigger=True, session=None,
               rows_after=None):
    """把 sku_preview_state 换成给定的行状态，跑 _st_sku_preview 取结论。

    rows_after 给了就作为「反选后重读」那一次的返回（模拟变种表重建的结果）。
    """
    seq = [rows] + ([rows_after] if rows_after is not None else [])

    async def fake_state(session_):
        cur = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"supported": has_trigger, "rows": cur,
                "previewIdx": 0, "colorIdx": 1}

    patch_publish(monkeypatch, "service", "sku_preview_state", fake_state)
    events = []

    async def emit(ev):
        events.append(ev)

    ctx = {"workdir": "."}
    res = await service._st_sku_preview(
        ctx, session or _FakeSession(rows_after=rows_after or []), emit)
    return res, events


@pytest.mark.asyncio
async def test_空图位补不上就反选该规格(monkeypatch):
    """两行空图位、无补图入口、也没有任何非空行可取图 → 两个规格都该被反选掉。

    这正是 2026-09-12 车贴那单的形态：认领没带预览图，页面上一张可用的图都没有。
    """
    rows = [{"i": 0, "color": "白色", "url": "", "w": 0, "h": 0, "empty": True,
             "bad": False, "hasFillSlot": False},
            {"i": 1, "color": "黑色", "url": "", "w": 0, "h": 0, "empty": True,
             "bad": False, "hasFillSlot": False}]
    s = _FakeSession()
    # 反选后变种表重建成空表（两个规格都不发了）
    res, events = await _run(rows, monkeypatch, session=s, rows_after=[])
    assert s.unchecked == ["白色", "黑色"], "两个规格都要反选"
    assert res["droppedSpecs"] == ["白色", "黑色"]
    assert "已反选" in res["note"]
    assert "均已满足" not in res["note"]
    # 反选成功、且重读后没有残留空行 → 不该再判 fail 拖死整单
    assert res["status"] == "ok"


@pytest.mark.asyncio
async def test_反选不掉时仍判fail并提示人工(monkeypatch):
    """整维只剩一个已勾选项时不能反选（清空会毁掉整张变种表），如实判 fail。"""
    rows = [{"i": 0, "color": "白色", "url": "", "w": 0, "h": 0, "empty": True,
             "bad": False, "hasFillSlot": False}]
    s = _FakeSession(uncheck={"found": True, "wasChecked": True, "clicked": False,
                              "checked": True, "err": "last-checked-in-group",
                              "group": "颜色", "checkedOptions": ["白色"]})
    res, events = await _run(rows, monkeypatch, session=s)
    assert res["status"] == "fail"
    assert res["droppedSpecs"] == []
    assert "请上传预览图" in res["note"]
    assert any(e.get("type") == "manual_check" for e in events)


@pytest.mark.asyncio
async def test_全部合规仍判skipped(monkeypatch):
    rows = [{"i": 0, "color": "白色", "url": "http://x/1.jpg",
             "w": 1785, "h": 1785, "empty": False, "bad": False}]
    res, _ = await _run(rows, monkeypatch)
    assert res["status"] == "skipped"
    assert "均已满足" in res["note"]


@pytest.mark.asyncio
async def test_尺寸持续未知应等待后阻止保存(monkeypatch):
    """图片加载失败不能报告全部合规。"""
    rows = [{"i": 0, "color": "白色", "url": "http://x/1.jpg",
             "w": 0, "h": 0, "empty": False, "bad": False}]
    res, events = await _run(rows, monkeypatch)
    assert res["status"] == "fail"
    assert any("读不到尺寸" in (e.get("message") or "") for e in events)
