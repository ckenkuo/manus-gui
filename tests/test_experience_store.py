"""RecipeStore 向量 CRUD 单测。

只依赖 numpy（去重/对齐），不依赖 faiss/bm25/jieba，因此在装依赖前即可运行：
    pytest tests/test_experience_store.py -v
"""

import asyncio

import numpy as np
import pytest

from app.experience.store import RecipeStore, Recipe


def _norm(vec):
    arr = np.asarray(vec, dtype=np.float32)
    return (arr / np.linalg.norm(arr)).tolist()


# 三个测试向量（4 维）：A 与 A' 余弦≈0.989（>0.95，同任务）；B 与 A 正交（不同任务）。
EMB_A = _norm([1.0, 0.0, 0.0, 0.0])
EMB_A_DUP = _norm([1.0, 0.15, 0.0, 0.0])
EMB_B = _norm([0.0, 1.0, 0.0, 0.0])


def _recipe(task, steps, emb, **kw):
    return Recipe(task=task, steps=steps, embedding=emb, **kw)


def _store(tmp_path):
    return RecipeStore(tmp_path / "recipes.jsonl")


def test_add_then_reload_persists(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.add(_recipe("查机票", ["1. 打开首页", "2. 搜索"], EMB_A)))

    reloaded = _store(tmp_path)  # 新实例从 jsonl 重新加载
    assert len(reloaded) == 1
    r = reloaded.all()[0]
    assert r.task == "查机票"
    assert r.steps == ["1. 打开首页", "2. 搜索"]
    assert r.step_count == 2  # 程序化补全 len(steps)
    assert r.id and r.created_at


def test_dedup_keeps_fewer_steps(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.add(_recipe("查上海到北京机票", ["s1", "s2", "s3", "s4", "s5"], EMB_A)))
    # 同任务（余弦>0.95）且步数更少 → 覆盖
    asyncio.run(store.add(_recipe("查机票 上海北京", ["a1", "a2", "a3"], EMB_A_DUP)))

    assert len(store) == 1
    kept = store.all()[0]
    assert kept.step_count == 3
    assert kept.steps == ["a1", "a2", "a3"]


def test_dedup_discards_when_more_steps(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.add(_recipe("查机票", ["a1", "a2", "a3"], EMB_A)))
    # 同任务但步数更多 → 丢弃新条目，保留旧的
    result = asyncio.run(store.add(_recipe("查机票 dup", ["b1", "b2", "b3", "b4", "b5"], EMB_A_DUP)))

    assert len(store) == 1
    assert store.all()[0].step_count == 3
    assert result.step_count == 3  # 返回的是被保留的旧条目


def test_distinct_task_appends(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.add(_recipe("查机票", ["s1"], EMB_A)))
    asyncio.run(store.add(_recipe("订酒店", ["s1"], EMB_B)))  # 正交 → 新增
    assert len(store) == 2


def test_delete(tmp_path):
    store = _store(tmp_path)
    r = asyncio.run(store.add(_recipe("查机票", ["s1"], EMB_A)))
    assert asyncio.run(store.delete(r.id)) is True
    assert len(store) == 0
    assert asyncio.run(store.delete("nonexistent")) is False
    # 删除已持久化
    assert len(_store(tmp_path)) == 0


def test_dim_mismatch_raises(tmp_path):
    store = _store(tmp_path)
    asyncio.run(store.add(_recipe("查机票", ["s1"], EMB_A)))  # 4 维
    with pytest.raises(ValueError):
        asyncio.run(store.add(_recipe("订酒店", ["s1"], _norm([1.0, 0.0]))))  # 2 维


def test_get_and_all(tmp_path):
    store = _store(tmp_path)
    r = asyncio.run(store.add(_recipe("查机票", ["s1"], EMB_A)))
    assert store.get(r.id).task == "查机票"
    assert store.get("missing") is None
    assert len(store.all()) == 1
