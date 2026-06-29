"""retriever 单测：RRF 融合 + 相关性闸门 + BM25 命中。

dense 通路的 embed_texts（联网）被 monkeypatch 成确定向量，使测试离线可跑。
"""

import asyncio

import numpy as np
import pytest

from app.experience import retriever
from app.experience.store import RecipeStore, Recipe


def _norm(vec):
    arr = np.asarray(vec, dtype=np.float32)
    return (arr / np.linalg.norm(arr)).tolist()


EMB_FLIGHT = _norm([1.0, 0.0, 0.0, 0.0])
EMB_HOTEL = _norm([0.0, 1.0, 0.0, 0.0])
EMB_WEATHER = _norm([0.0, 0.0, 1.0, 0.0])


def _seed_store(tmp_path):
    store = RecipeStore(tmp_path / "recipes.jsonl")
    asyncio.run(store.add(Recipe(task="查上海到北京的机票", steps=["搜索机票"], embedding=EMB_FLIGHT)))
    asyncio.run(store.add(Recipe(task="预订三亚的酒店", steps=["搜索酒店"], embedding=EMB_HOTEL)))
    asyncio.run(store.add(Recipe(task="查询明天的天气", steps=["查天气"], embedding=EMB_WEATHER)))
    return store


def _patch_embed(monkeypatch, vec):
    async def fake_embed(texts, model=None):
        return [vec for _ in texts]

    monkeypatch.setattr(retriever, "embed_texts", fake_embed)


def test_retrieve_ranks_relevant_first(tmp_path, monkeypatch):
    store = _seed_store(tmp_path)
    # query 向量贴近机票（dense），关键词也命中机票/北京（sparse）
    _patch_embed(monkeypatch, _norm([0.98, 0.1, 0.0, 0.0]))
    out = asyncio.run(retriever.retrieve(store, "查北京机票最低价", k=2))
    assert out, "应至少检索到一条经验"
    assert out[0].task == "查上海到北京的机票"


def test_gate_filters_unrelated(tmp_path, monkeypatch):
    store = _seed_store(tmp_path)
    # query 向量与所有经验正交（dense 余弦≈0），且无关键词重叠 → 闸门拦下
    _patch_embed(monkeypatch, _norm([0.0, 0.0, 0.0, 1.0]))
    out = asyncio.run(retriever.retrieve(store, "zxcvbnm qwerty", k=2))
    assert out == []


def test_bm25_only_fallback_when_dense_unavailable(tmp_path, monkeypatch):
    """dense 不可用（embed 抛错）时，应降级为仅 BM25 并仍能召回关键词命中的经验。"""
    store = _seed_store(tmp_path)

    async def boom(texts, model=None):
        raise RuntimeError("embedding 服务不可用")

    monkeypatch.setattr(retriever, "embed_texts", boom)
    out = asyncio.run(retriever.retrieve(store, "查北京机票", k=2))
    assert out, "降级仅 BM25 时仍应召回关键词命中的经验"
    assert any(r.task == "查上海到北京的机票" for r in out)


def test_empty_store_returns_empty(tmp_path, monkeypatch):
    store = RecipeStore(tmp_path / "recipes.jsonl")
    _patch_embed(monkeypatch, _norm([1.0, 0.0, 0.0, 0.0]))
    assert asyncio.run(retriever.retrieve(store, "任何查询", k=2)) == []


def test_rrf_accumulate_math():
    scores = {}
    # 两路排名：A 在 dense 第1、sparse 第2；B 在 dense 第2、sparse 第1
    retriever._rrf_accumulate(scores, [(0, 0.9), (1, 0.8)], rrf_k=60)
    retriever._rrf_accumulate(scores, [(1, 5.0), (0, 4.0)], rrf_k=60)
    # A: 1/61 + 1/62 ; B: 1/62 + 1/61 → 二者相等
    assert scores[0] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[1] == pytest.approx(1 / 62 + 1 / 61)
