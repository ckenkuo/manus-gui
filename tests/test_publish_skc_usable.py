"""plan_skc 里 _usable 兜底的离线单测（单色分支不发视觉请求，纯本地可测）。

【为什么值得单测】原实现过滤后为空就 `return out or paths`，把重复图和尺码表图
一起静默放回。静默是它最坏的性质：SKC 行挂上重复图或尺码表图，人只能看成品才发现。
2026-08-24 改成两级兜底 + 告警，这里把三条分支都钉住。
"""
import os

import pytest

from app.publish import vision


def _info(colors: list, notes: list) -> dict:
    return {"colors": colors, "title": "测试商品",
            "complianceNotes": {"files": notes}}


def _mk(tmp_path, names: list) -> str:
    for n in names:
        (tmp_path / n).write_bytes(b"\xff\xd8\xff\xe0stub")
    return str(tmp_path)


@pytest.mark.asyncio
async def test_正常情况只留可用图(tmp_path):
    """有干净图时，重复图与尺码表图都不该进候选。"""
    d = _mk(tmp_path, ["main-01.jpg", "main-02.jpg", "main-03.jpg"])
    info = _info(["图色"], [
        {"file": "main-01.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-02.jpg", "duplicate": True, "duplicateOf": "main-01.jpg"},
        {"file": "main-03.jpg", "kind": "尺码表"},
    ])
    r = await vision.plan_skc(info, d)
    picked = [os.path.basename(p) for p in r["rows"][0]["images"]]
    assert picked == ["main-01.jpg"], picked


@pytest.mark.asyncio
async def test_全是重复图时放回重复图但仍排尺码表(tmp_path, caplog):
    """第一级兜底：一张画面重复好过一张图都没有，但尺码表图仍不能要。"""
    d = _mk(tmp_path, ["main-01.jpg", "main-02.jpg", "main-03.jpg"])
    info = _info(["图色"], [
        {"file": "main-01.jpg", "duplicate": True, "duplicateOf": "x.jpg"},
        {"file": "main-02.jpg", "duplicate": True, "duplicateOf": "x.jpg"},
        {"file": "main-03.jpg", "kind": "尺码表"},
    ])
    r = await vision.plan_skc(info, d)
    picked = [os.path.basename(p) for p in r["rows"][0]["images"]]
    assert picked == ["main-01.jpg", "main-02.jpg"], picked
    assert "main-03.jpg" not in picked, "尺码表图不该被放回"


@pytest.mark.asyncio
async def test_全是尺码表时才放回全量(tmp_path):
    """第二级兜底：连非商品图都得用上（整行留 1688 破线原图更糟），但要有告警。"""
    d = _mk(tmp_path, ["main-01.jpg", "main-02.jpg"])
    info = _info(["图色"], [
        {"file": "main-01.jpg", "kind": "尺码表"},
        {"file": "main-02.jpg", "kind": "工厂图"},
    ])
    r = await vision.plan_skc(info, d)
    picked = [os.path.basename(p) for p in r["rows"][0]["images"]]
    assert picked == ["main-01.jpg", "main-02.jpg"], picked


@pytest.mark.asyncio
async def test_无标注时原样返回(tmp_path):
    """没跑过视觉回填（notes 为空）时不做任何过滤。"""
    d = _mk(tmp_path, ["main-01.jpg", "main-02.jpg"])
    r = await vision.plan_skc({"colors": ["图色"]}, d)
    picked = [os.path.basename(p) for p in r["rows"][0]["images"]]
    assert picked == ["main-01.jpg", "main-02.jpg"], picked


@pytest.mark.asyncio
async def test_单色分支不发视觉请求(tmp_path, monkeypatch):
    """单色时所有主图都属这唯一颜色，没有「哪张归哪色」可判——不该发请求。

    2026-08-22 实测踩坑：单色商品照样发请求，模型按「含水印的图不要选」把 6 张全否掉、
    返回空 images，整个阶段被跳过还多一次人工确认。
    """
    async def boom(*a, **kw):
        raise AssertionError("单色分支不该调用视觉模型")

    monkeypatch.setattr(vision, "ask_json_with_images", boom)
    d = _mk(tmp_path, ["main-01.jpg"])
    r = await vision.plan_skc(_info(["图色"], [
        {"file": "main-01.jpg", "clean": True, "kind": "平铺"}]), d)
    assert r["status"] == "ok" and len(r["rows"]) == 1
