"""源站取不到的描述图不能让模型判断错位（2026-09-02 实测 offer 654598552346）。

【为什么要这组测试】404 容错的第一版把「取不到的图」在 llm.ask_json_with_images
里静默滤掉，于是 28 张少传 1 张、提示词仍写 pos 1~28，模型看到的第 16 张其实是
页面第 17 张——其后每张的判断整体错位一位，两张超比例长图（790x1847、790x2423）
被漏判成 keep，最后卡在 desc_save 的比例校验上。错位是静默的：日志只说「跳过 1 张」，
看不出结论落到了别的图上。故这里把「位次必须与传图一一对应」钉死。
"""
import json

import pytest

from app.publish import vision


_DATA = "data:image/jpeg;base64,AAAA"


def _mods(n: int) -> list:
    """造 n 张描述模块，pos 从 1 起（同 pipeline.desc_map 的产物形状）。"""
    return [{"pos": i, "url": f"https://cbu01.alicdn.com/{i}.jpg",
             "onDxmHost": False} for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_取不到的图不参与判断且不错位(monkeypatch):
    """第 3 张取不到时：只传 4 张、listing 里不出现 pos 3、其余 pos 保持页面真实序号。"""
    seen = {}

    def fake_ref(url: str) -> str:
        return "" if url.endswith("/3.jpg") else _DATA

    async def fake_ask(prompt, images, what="", system=None, stage=None, **kw):
        # keep 侧中文复核（plan_desc 收尾的第二遍调用）：本用例不验它，
        # 一律回全干净，且不能覆盖上面记录的初判调用现场
        if "复核" in what:
            return {"dirty": []}
        seen["prompt"] = prompt
        seen["n_images"] = len(images)
        # 模型按 listing 里给的 pos 回答（真实序号，跳过 3）
        return {"actions": [{"pos": p, "action": "keep"} for p in (1, 2, 4, 5)]}

    monkeypatch.setattr(vision, "image_ref", fake_ref)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_ask)

    r = await vision.plan_desc(_mods(5), {"title": "t"})

    assert r["status"] == "ok"
    # 传给模型的图数与 listing 里的 pos 数一致——这是「不错位」的充要条件
    assert seen["n_images"] == 4
    assert "pos=3" not in seen["prompt"]
    for p in (1, 2, 4, 5):
        assert f"pos={p}" in seen["prompt"]
    # 取不到的那张按保留处理，并单独报出来供人工换图
    assert r["unreachable"] == [3]
    assert 3 in r["keep"]


@pytest.mark.asyncio
async def test_取不到的图不进尺寸兜底(monkeypatch):
    """取不到的图即使标了 tooSmall 也不能改判 replace：它下载不到原图，放大无从下手。"""
    mods = _mods(2)
    mods[0]["tooSmall"] = True
    mods[0]["sizeReasons"] = ["宽高比 0.428 超出 0.5~2.0"]
    mods[1]["tooSmall"] = True
    mods[1]["sizeReasons"] = ["790x1847 小于 480x480"]

    def fake_ref(url: str) -> str:
        return "" if url.endswith("/1.jpg") else _DATA

    async def fake_ask(prompt, images, what="", system=None, stage=None, **kw):
        # keep 侧中文复核（plan_desc 收尾的第二遍调用）：本用例不验它，一律回全干净
        if "复核" in what:
            return {"dirty": []}
        return {"actions": [{"pos": 2, "action": "keep"}]}

    monkeypatch.setattr(vision, "image_ref", fake_ref)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_ask)

    r = await vision.plan_desc(mods, {"title": "t"})

    # pos 1 取不到 → 留在 keep、不进 replace；pos 2 取得到且不达标 → 兜底成 replace
    assert 1 in r["keep"]
    assert all(x["pos"] != 1 for x in r["replace"])
    assert [x["pos"] for x in r["replace"]] == [2]


@pytest.mark.asyncio
async def test_兜底理由用真实判据不写死尺寸(monkeypatch):
    """超比例的长条图理由要说「宽高比」，不能一律说成「像素不够」——否则查不出真因。"""
    mods = _mods(1)
    mods[0]["tooSmall"] = True
    mods[0]["size"] = "790x1847"
    mods[0]["sizeReasons"] = ["宽高比 0.428 超出 0.5~2.0"]

    async def fake_ask(prompt, images, what="", system=None, stage=None, **kw):
        # keep 侧中文复核（plan_desc 收尾的第二遍调用）：本用例不验它，一律回全干净
        if "复核" in what:
            return {"dirty": []}
        return {"actions": [{"pos": 1, "action": "keep"}]}

    monkeypatch.setattr(vision, "image_ref", lambda u: _DATA)
    monkeypatch.setattr(vision, "ask_json_with_images", fake_ask)

    r = await vision.plan_desc(mods, {"title": "t"})

    assert len(r["replace"]) == 1
    assert "宽高比" in r["replace"][0]["reason"]
    assert "1340" not in r["replace"][0]["reason"]


@pytest.mark.asyncio
async def test_全部取不到时不问模型(monkeypatch):
    """一张都取不到：直接返回 error，别白烧一次视觉调用。"""
    called = {"n": 0}

    async def fake_ask(prompt, images, what="", system=None, stage=None, **kw):
        called["n"] += 1
        return {"actions": []}

    monkeypatch.setattr(vision, "image_ref", lambda u: "")
    monkeypatch.setattr(vision, "ask_json_with_images", fake_ask)

    r = await vision.plan_desc(_mods(3), {"title": "t"})

    assert r["status"] == "error"
    assert called["n"] == 0
    assert r["keep"] == [1, 2, 3]      # 全部留原图交人工
    assert r["unreachable"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_ask_json_with_images_拒绝空引用(monkeypatch):
    """llm 层不再静默丢图：拿到空引用一律抛，逼调用方自己处理位次。"""
    from app.publish import llm

    monkeypatch.setattr(llm, "image_ref", lambda i: "" if "bad" in i else _DATA)
    with pytest.raises(RuntimeError, match="取不到"):
        await llm.ask_json_with_images("p", ["ok.jpg", "bad.jpg"], what="测试")
