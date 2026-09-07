# -*- coding: utf-8 -*-
"""统一品类识别（classify_category）的离线单测：服装词表快速路径 + 非服装 LLM 意图识别。

2026-09-06 宠物窝 sizechart 失败后收敛：品类识别从「词表枚举」改为「分层」——
服装二分保留词表（封闭稳定、set_variant 同步需要），非服装细分走 LLM 意图识别
（开放集合、避免每加一个品类就补词表的膨胀）。本文件锁分层边界与兜底行为。
"""
import pytest

from app.publish import pipeline as P


@pytest.mark.asyncio
async def test_服装词表快速路径不调LLM():
    """标题命中 _APPAREL_WORDS 直接判 apparel，零 LLM 调用（同步词表路径）。"""
    assert await P.classify_category("女士针织长裤") == "apparel"
    assert await P.classify_category("儿童卫衣两件套") == "apparel"
    # 「衫」是高频服装特征字，覆盖针织衫/POLO衫等词表漏判的服装词
    assert await P.classify_category("秋季条纹针织衫") == "apparel"


@pytest.mark.asyncio
async def test_服装cat_path优先于标题():
    """cat_path 里的类目词命中服装时，即使标题是非服装词也判 apparel。"""
    assert await P.classify_category("四季通用", cat_path=["女装", "连衣裙"]) == "apparel"


def test_排除词优先于服装词():
    """_APPAREL_EXCLUDE 先判：鞋袜收纳盒含「袜」但被「收纳」排除（同步 _is_apparel）。"""
    assert P._is_apparel(None, "鞋袜收纳盒 家用防尘") is False


@pytest.mark.asyncio
async def test_非服装走LLM识别(monkeypatch):
    """词表未命中的非服装走 LLM，返回 LLM 给出的标签。"""
    calls = {}

    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        calls["what"] = what
        calls["prompt"] = prompt
        return {"category": "pet_supply"}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    assert await P.classify_category("宠物狗窝四季通用可拆洗") == "pet_supply"
    assert calls["what"] == "品类识别"
    assert "狗窝" in calls["prompt"]


@pytest.mark.asyncio
async def test_LLM返回非法标签落other(monkeypatch):
    """LLM 吐出不在 _CATEGORY_TAGS 里的标签时落 other，别让非法标签带下水。"""
    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        return {"category": "宠物"}  # 中文标签，非法

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    # 「保温杯」命中 _APPAREL_EXCLUDE，判非服装走 LLM
    assert await P.classify_category("不锈钢保温杯 500ml") == "other"


@pytest.mark.asyncio
async def test_LLM漏category字段落other(monkeypatch):
    async def fake_ask_json(prompt, what="判断", retries=3, stage=None, **kw):
        return {}  # 没给 category 键

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask_json)
    assert await P.classify_category("不锈钢保温杯 500ml") == "other"


def test_guess_size_kind按品类标签映射():
    """_guess_size_kind 的 cat 参数：非 apparel 查 _NONAPPAREL_KIND 数据映射。"""
    k = P._guess_size_kind("四季通用", ["S", "M", "L"], cat="pet_supply")
    assert k["nonapparel"] is True
    assert k["expert"] == "宠物用品尺寸专家"
    # 未映射标签落 other 通用兜底
    k2 = P._guess_size_kind("四季通用", ["S", "M", "L"], cat="unknown_tag")
    assert k2["nonapparel"] is True
    assert k2["expert"] == P._NONAPPAREL_KIND["other"]["expert"]


def test_guess_size_kind默认apparel走服装年龄段():
    """cat 默认 apparel：走原有童装/成人年龄段判断，不返回 nonapparel（回归闸）。"""
    k = P._guess_size_kind("女童毛衣针织衫", ["90cm", "100cm", "110cm"])
    assert "nonapparel" not in k
    assert "童装" in k["expert"]


def test_非服装标签全带nonapparel():
    """所有非服装工作流条目都带 nonapparel=True——非服装走几何推理提示词。"""
    for tag, kind in P._NONAPPAREL_KIND.items():
        assert kind.get("nonapparel") is True, tag
        assert kind.get("expert") and kind.get("fit") and kind.get("step"), tag
