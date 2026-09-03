# -*- coding: utf-8 -*-
"""阶段⑨尺码表测量值来源的离线单测（mock 浏览器/LLM）。

2026-08-22 真站踩坑（商品 984360345330 套头衫）：源尺码表只有「衣长/裤长」两列
（店家套了套装模板），店小秘弹窗按类目强制「衣长/胸围全围/袖长」。原实现是
「有 sizeMeasurements 就必须全齐，否则报错让人工清空该字段走兜底」——源给了一半
反倒比完全没有更糟，10 个尺码全进 missing 列表、整个商品未落库。

改成按【参数列】组合：源给的列照用（实测值比估算准），弹窗要而源没有的列交模型补。
本文件锁三件事：源实测值不被模型值覆盖、缺列会真去问模型、模型走 UI 选择的那个。
"""
import json
import re

import pytest

from app.publish import pipeline


class _FakeSession:
    """按 JS 片段特征分派返回值的假 CDP 会话，只覆盖阶段⑨用到的几个 eval。"""

    def __init__(self, params, sizes):
        self.params = params
        self.sizes = sizes
        self.filled = None
        self.tpl_name = None

    async def eval_json(self, js: str):
        # 【分派特征跟着实现改】2026-08-27 定位方式由 .skuAttrSizeChart 类改成按 label
        # 文字取 form-item（套装商品有两张表，那个类只挂在第一张上，见
        # pipeline._JS_SIZECHART_LOCATE）。原先按 "skuAttrSizeChart" 分派的这一支于是
        # 再也匹配不上，开弹窗恒返回 {}，7 个用例全报「添加尺码表弹窗未打开」——
        # 假会话没跟上实现的锅，不是实现回归。改用 span.link（点入口这个动作的特征）。
        if "span.link" in js and "opened" in js:
            return {"opened": True}
        if "closed" in js and "ant-modal-close" in js:
            return {"closed": 0}
        if "keyword = " in js and "尺码分类" in js:
            return {"ok": True, "selected": "上装", "source": "preset"}
        if "params" in js and "sizes" in js:
            return {"params": self.params, "sizes": self.sizes}
        if "tplName" in js:
            # 填表：记录传进 JS 的模板名与 data，回报填充完整
            self.filled = js
            m = re.search(r"const tplName = (\".*?\");", js)
            self.tpl_name = json.loads(m.group(1)) if m else ""
            return {"ok": True, "empty": []}
        if "stillOpen" in js:
            return {"stillOpen": False}
        if "found" in js:
            # 首次查状态返回「未添加」，填完后回读返回带模板名的表单文本
            return ({"found": True, "text": f"{self.tpl_name} 查看"}
                    if self.filled else {"found": True, "text": "添加尺码表"})
        return {}


async def _boom(*a, **k):
    raise AssertionError("本用例不该调模型")


def _write_info(tmp_path, meas):
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps({
        "title": "秋季条纹针织衫",
        "sizeChart": {"80": "身高65-75cm", "90": "身高75-85cm"},
        "sizeMeasurements": meas,
    }, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_源缺参数列时只补缺的列且不覆盖实测值(tmp_path, monkeypatch):
    # 真站数据的最小复刻：源只有衣长/裤长，弹窗要衣长/胸围全围
    info = _write_info(tmp_path, {
        "80": {"衣长": 35, "裤长": 47},
        "90": {"衣长": 38, "裤长": 52},
    })
    asked = {}

    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        asked["prompt"] = prompt
        # 故意连衣长也一起返回错值，验证不会盖掉源实测值
        return {"80": {"胸围全围": 66, "衣长": 999},
                "90": {"胸围全围": 70, "衣长": 999}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    session = _FakeSession(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["status"] == "ok"
    assert r["measureSource"] == "source+model"
    assert r["estimated"] == ["胸围全围"]
    assert r["data"]["80"] == {"衣长": 35, "胸围全围": 66}
    assert r["data"]["90"]["衣长"] == 38
    # 已有实测列进了提示词，估算才会与之同档
    assert "衣长35" in asked["prompt"]


@pytest.mark.asyncio
async def test_源参数齐全时不调模型(tmp_path, monkeypatch):
    info = _write_info(tmp_path, {
        "80": {"衣长": 35, "胸围": 62},
        "90": {"衣长": 38, "胸围": 66},
    })

    async def boom(*a, **k):
        raise AssertionError("参数齐全不该调模型")

    monkeypatch.setattr("app.publish.llm.ask_json", boom)
    # 弹窗参数名「胸围全围」，源给「胸围」——模糊对齐后算齐全
    session = _FakeSession(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["measureSource"] == "source"
    assert r["estimated"] == []
    assert r["data"]["80"] == {"衣长": 35, "胸围全围": 62}


@pytest.mark.asyncio
async def test_源完全没有实测值时整表交模型(tmp_path, monkeypatch):
    info = _write_info(tmp_path, {})

    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        assert "（无）" in prompt  # 无已有实测列
        return {"80": {"衣长": 35, "胸围全围": 62},
                "90": {"衣长": 38, "胸围全围": 66}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    session = _FakeSession(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["measureSource"] == "model"
    assert r["estimated"] == ["衣长", "胸围全围"]
    assert r["data"]["90"] == {"衣长": 38, "胸围全围": 66}


@pytest.mark.asyncio
async def test_源键带建议身高描述也能对上页面尺码(tmp_path, monkeypatch):
    # norm_size 归一后才对得上：源键 80cm建议身高70-80cm ↔ 页面 80
    info = _write_info(tmp_path, {
        "80cm建议身高70-80cm": {"衣长": 35, "胸围": 62},
        "90cm建议身高80-90cm": {"衣长": 38, "胸围": 66},
    })

    async def boom(*a, **k):
        raise AssertionError("归一后应算齐全，不该调模型")

    monkeypatch.setattr("app.publish.llm.ask_json", boom)
    session = _FakeSession(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["measureSource"] == "source"
    assert r["data"]["80"]["衣长"] == 35


@pytest.mark.asyncio
async def test_模型漏了某尺码时报错不填半残表(tmp_path, monkeypatch):
    info = _write_info(tmp_path, {"80": {"衣长": 35}})

    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        return {"80": {"胸围全围": 62}}  # 漏了 90

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    session = _FakeSession(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["status"] == "error"
    assert "90" in r["reason"]
    assert session.filled is None  # 没去填表


@pytest.mark.asyncio
async def test_分类未预选且多选项时报错不瞎挑(tmp_path, monkeypatch):
    """没预选值又有多个候选时宁可报错——替用户挑错分类会填出整张错档尺码表。"""
    info = _write_info(tmp_path, {"80": {"衣长": 35}, "90": {"衣长": 38}})

    class _S(_FakeSession):
        async def eval_json(self, js):
            if "keyword = " in js and "尺码分类" in js:
                return {"ok": False, "reason": "no-preset-and-ambiguous",
                        "cur": "", "options": ["上装", "下装"]}
            return await super().eval_json(js)

    monkeypatch.setattr("app.publish.llm.ask_json", _boom)
    session = _S(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["status"] == "error"
    assert "no-preset-and-ambiguous" in r["reason"]
    assert session.filled is None


@pytest.mark.asyncio
async def test_默认不指定分类关键词(tmp_path, monkeypatch):
    """category 默认 None：JS 里 keyword 落成 null，走「跟随平台预选」那条路。

    2026-08-22 之前默认写死 "上装"，在女童连衣裙类目下必然 option-not-found。
    """
    info = _write_info(tmp_path, {"80": {"衣长": 35, "胸围": 62},
                                  "90": {"衣长": 38, "胸围": 66}})
    seen = {}

    class _S(_FakeSession):
        async def eval_json(self, js):
            if "keyword = " in js and "尺码分类" in js:
                seen["js"] = js
                return {"ok": True, "selected": "女童装-连衣裙", "source": "preset"}
            return await super().eval_json(js)

    monkeypatch.setattr("app.publish.llm.ask_json", _boom)
    session = _S(["衣长", "胸围全围"], ["80", "90"])

    r = await pipeline.add_sizechart(session, info)

    assert r["status"] == "ok"
    assert "const keyword = null;" in seen["js"]
    assert r["category"] == "女童装-连衣裙"
    assert r["categorySource"] == "preset"
