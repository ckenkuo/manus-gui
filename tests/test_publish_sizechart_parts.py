# -*- coding: utf-8 -*-
"""套装分件尺码表的离线单测（mock 浏览器/LLM）。

2026-08-29 真站取证（1688 商品 1058585588864，T恤 + 牛仔背带裙两件套）：源详情图上
分开给了两张表——「部件：上衣」量肩宽/袖长/前衣长/胸围，「部件：连衣裙」量前衣长/腰围
——但看图回填只产出一份扁平 sizeMeasurements，于是平台的「尺码表」「尺码表2」填出
完全相同的数值（前衣长 42/45/48/51/54 + 胸围 52/54/56/58/60 出现在两张表里）。
尺码分类按件别分对了，数值却同源，等于给买家一份错尺码表。

本文件锁三件事：
  - 分件表按【尺码分类】配对，而不是按序号（源图部件序与包装清单件序可能相反）
  - 两张平台表取到的是不同部件的数值
  - 只有扁平表时行为不变（老路径不回归）
"""
import json
import re

import pytest

from app.publish import pipeline
from app.publish.extract import _merge_vision


# ---- 配对函数本身 ---------------------------------------------------------

def test_部件名归类到尺码分类关键词():
    f = pipeline._size_category_for_part
    assert f("上衣") == "上装"
    assert f("短袖T恤") == "上装"
    assert f("连衣裙") == "连衣裙"
    # 背带裙是上下连身，不能因为带「裙」就归半身裙
    assert f("牛仔背带裙") == "连体衣"
    assert f("裤子") == "下装"
    assert f("") is None
    assert f("帽子") is None


def test_按分类配对而不是按序号():
    """源图部件序（连衣裙在前）与包装清单件序（上衣在前）相反，是真站的实际情形。"""
    parts = [
        {"part": "连衣裙", "measurements": {"6-9M": {"前衣长": 42, "腰围": 54}}},
        {"part": "上衣", "measurements": {"6-9M": {"肩宽": 21, "胸围": 52}}},
    ]
    # 第 0 张表分类是「上装」：即使上衣排在源图第二个，也要取上衣那份
    meas, part = pipeline._pick_part_measurements(parts, "上装", 0)
    assert part == "上衣" and meas["6-9M"]["肩宽"] == 21
    # 第 1 张表分类是「连衣裙」：取连衣裙那份
    meas2, part2 = pipeline._pick_part_measurements(parts, "连衣裙", 1)
    assert part2 == "连衣裙" and meas2["6-9M"]["腰围"] == 54


def test_分类配不上时按序号退回且不同表拿不同份():
    """配不上也不能让两张表同源——同源就是这个 bug 本身。"""
    parts = [
        {"part": "A件", "measurements": {"80": {"衣长": 40}}},
        {"part": "B件", "measurements": {"80": {"裙长": 50}}},
    ]
    m0, p0 = pipeline._pick_part_measurements(parts, "马甲", 0)
    m1, p1 = pipeline._pick_part_measurements(parts, "马甲", 1)
    assert (p0, p1) == ("A件", "B件")
    assert m0 != m1


def test_从选中分类名反推关键词():
    f = pipeline._category_keyword_of
    assert f("女童装-连体衣") == "连体衣"
    assert f("男童装-上装") == "上装"
    assert f("女童装-半身裙") == "半身裙"
    assert f("") is None


# ---- 看图回填侧 -----------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_vision_收下分件表并丢掉脏条目():
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "sizeMeasurementsByPart": [], "complianceNotes": {}}
    vision = {"sizeMeasurementsByPart": [
        {"part": "上衣", "measurements": {"80": {"衣长": 40}}},
        {"part": "连衣裙", "measurements": {"80": {"裙长": 50}}},
        {"part": "", "measurements": {"80": {"腰围": 52}}},   # 缺部件名，丢
        {"part": "裤子"},                                      # 缺 measurements，丢
    ]}
    stat = await _merge_vision(info, vision, {})
    assert [e["part"] for e in info["sizeMeasurementsByPart"]] == ["上衣", "连衣裙"]
    assert stat["sizeMeasurementsByPart"] == 2


@pytest.mark.asyncio
async def test_merge_vision_只有一件时不留分件表():
    """一件等于没分件，留着只让下游多一条判空路径。"""
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "sizeMeasurementsByPart": [], "complianceNotes": {}}
    await _merge_vision(info, {"sizeMeasurementsByPart": [
        {"part": "上衣", "measurements": {"80": {"衣长": 40}}}]}, {})
    assert info["sizeMeasurementsByPart"] == []


# ---- 端到端（mock 会话）：两张表数值必须不同 ------------------------------

class _FakeSession:
    """按 JS 片段特征分派的假 CDP 会话；cat 是弹窗尺码分类的选中值（模拟平台预选）。"""

    def __init__(self, params, sizes, cat):
        self.params = params
        self.sizes = sizes
        self.cat = cat
        self.filled = None
        self.tpl_name = None

    async def eval_json(self, js: str):
        if "span.link" in js and "opened" in js:
            return {"opened": True}
        if "closed" in js and "ant-modal-close" in js:
            return {"closed": 0}
        if "keyword = " in js and "尺码分类" in js:
            return {"ok": True, "selected": self.cat, "source": "preset"}
        if "params" in js and "sizes" in js:
            return {"params": self.params, "sizes": self.sizes}
        if "tplName" in js:
            self.filled = js
            m = re.search(r'const tplName = (".*?");', js)
            self.tpl_name = json.loads(m.group(1)) if m else ""
            return {"ok": True, "empty": []}
        if "stillOpen" in js:
            return {"stillOpen": False}
        if "found" in js:
            return ({"found": True, "text": f"{self.tpl_name} 查看"}
                    if self.filled else {"found": True, "text": "添加尺码表"})
        return {}


def _write_set_info(tmp_path):
    """真站数据的最小复刻：源分件给了上衣表与连衣裙表，测量参数各不相同。"""
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps({
        "title": "外贸女童开衫牛仔背带裤套装两件套",
        "sizeChart": {},
        # 扁平表是两张被压成一份的产物（看图旧行为），分件表才是分开的事实
        "sizeMeasurements": {
            "6-9M": {"前衣长": 42, "腰围": 54, "肩宽": 21, "胸围": 52},
            "9-12M": {"前衣长": 45, "腰围": 56, "肩宽": 22, "胸围": 54},
        },
        "sizeMeasurementsByPart": [
            {"part": "连衣裙", "measurements": {
                "6-9M": {"前衣长": 42, "腰围": 54},
                "9-12M": {"前衣长": 45, "腰围": 56}}},
            {"part": "上衣", "measurements": {
                "6-9M": {"前衣长": 32, "胸围": 52, "肩宽": 21},
                "9-12M": {"前衣长": 34, "胸围": 54, "肩宽": 22}}},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_套装两张表各取自己那件的数值(tmp_path, monkeypatch):
    info = _write_set_info(tmp_path)

    async def boom(*a, **k):
        raise AssertionError("源分件表已齐全，不该调模型")

    monkeypatch.setattr("app.publish.llm.ask_json", boom)

    # 第 1 张表：分类「上装」，参数是上衣维度
    s1 = _FakeSession(["前衣长", "胸围"], ["6-9M", "9-12M"], "女童装-上装")
    r1 = await pipeline.add_sizechart(s1, info, which=0)
    assert r1["status"] == "ok" and r1["partUsed"] == "上衣"
    assert r1["data"]["6-9M"] == {"前衣长": 32, "胸围": 52}

    # 第 2 张表：分类「连体衣」（背带裙），参数是裙装维度
    s2 = _FakeSession(["前衣长", "腰围"], ["6-9M", "9-12M"], "女童装-连衣裙")
    r2 = await pipeline.add_sizechart(s2, info, which=1)
    assert r2["status"] == "ok" and r2["partUsed"] == "连衣裙"
    assert r2["data"]["6-9M"] == {"前衣长": 42, "腰围": 54}

    # bug 的核心判据：两张表的数值不能相同
    assert r1["data"] != r2["data"]
    assert r1["data"]["6-9M"]["前衣长"] != r2["data"]["6-9M"]["前衣长"]


@pytest.mark.asyncio
async def test_缺列估算时提示词点明是哪一件(tmp_path, monkeypatch):
    """两件都要估算时，若不告诉模型是哪一件，输入完全相同必然给出同一套值。"""
    info = _write_set_info(tmp_path)
    asked = {}

    async def fake_ask_json(prompt, what="判断", retries=3, stage=None):
        asked["prompt"] = prompt
        return {"6-9M": {"袖长": 9.5}, "9-12M": {"袖长": 10}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    s = _FakeSession(["前衣长", "袖长"], ["6-9M", "9-12M"], "女童装-上装")
    r = await pipeline.add_sizechart(s, info, which=0)

    assert r["status"] == "ok" and r["estimated"] == ["袖长"]
    assert "【上衣】" in asked["prompt"]
    # 源实测的前衣长（上衣那份的 32）不被模型值覆盖
    assert r["data"]["6-9M"]["前衣长"] == 32


@pytest.mark.asyncio
async def test_源只给扁平表时行为不变(tmp_path, monkeypatch):
    """老路径不回归：没有分件表就照旧共用同一份，partUsed 为空。"""
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps({
        "title": "秋季条纹针织衫",
        "sizeChart": {},
        "sizeMeasurements": {"80": {"衣长": 35, "胸围": 62},
                             "90": {"衣长": 38, "胸围": 66}},
    }, ensure_ascii=False), encoding="utf-8")

    async def boom(*a, **k):
        raise AssertionError("参数齐全不该调模型")

    monkeypatch.setattr("app.publish.llm.ask_json", boom)
    s = _FakeSession(["衣长", "胸围全围"], ["80", "90"], "女童装-上装")
    r = await pipeline.add_sizechart(s, str(p), which=0)

    assert r["status"] == "ok" and r["partUsed"] == ""
    assert r["measureSource"] == "source"
    assert r["data"]["80"] == {"衣长": 35, "胸围全围": 62}
