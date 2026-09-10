"""⑦b 预览图空图位必须判 fail，不能算进「均已满足」（2026-09-01 实测两单）。

取证：1067271196776、1051827161006 两单 save 被平台拒「错误：请上传预览图」，
而 ⑦b 之前判的是 skipped、note 还写「6 行预览图均已满足 1:1 且不小于 800x800」。
成因是状态判据只看得见 <img>：格子里压根没有 img 的行既进不了 bad（bad 要求
读到尺寸），也进不了 unknown（unknown 要求有 img 但 naturalWidth=0），于是被
当成合规行放过，错误一路延后到 ⑭ 才以「保存可能未生效」这种含糊结论暴露。
"""

from publish_patching import patch_publish
import pytest

from app.publish import service


class _FakeSession:
    pass


async def _run(rows, monkeypatch, *, has_trigger=True):
    """把 sku_preview_state 换成给定的行状态，跑 _st_sku_preview 取结论。"""

    async def fake_state(session):
        return {"supported": has_trigger, "rows": rows,
                "previewIdx": 0, "colorIdx": 1}

    patch_publish(monkeypatch, "service", "sku_preview_state", fake_state)
    events = []

    async def emit(ev):
        events.append(ev)

    ctx = {"workdir": "."}
    res = await service._st_sku_preview(ctx, _FakeSession(), emit)
    return res, events


@pytest.mark.asyncio
async def test_空图位判fail而不是skipped(monkeypatch):
    # 两行都是空图位：有换图入口但一张图都没有
    rows = [{"i": 0, "color": "白色", "url": "", "w": 0, "h": 0, "empty": True,
             "bad": False},
            {"i": 1, "color": "黑色", "url": "", "w": 0, "h": 0, "empty": True,
             "bad": False}]
    res, events = await _run(rows, monkeypatch)
    assert res["status"] == "fail"
    # 结论必须点明是空图位与平台会拒的原因，不能是「均已满足」
    assert "为空" in res["note"]
    assert "均已满足" not in res["note"]
    assert "请上传预览图" in res["note"]
    # 同时要有 manual_check 事件提示人工补图
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
