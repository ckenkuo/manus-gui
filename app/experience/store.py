"""经验库的向量 CRUD 层。

架构：**内存权威列表 + 整文件原子重写 + 重建索引**。jsonl 是唯一真相源，
启动时全量 load 重建 Faiss/BM25；所有写（C/U/D）共用一条 `_persist_and_rebuild`
路径，不分叉。这绕开了 Faiss `IndexFlatIP` 不支持 `remove_ids` 的删除难题
（从不原地删，只重建）。规模为个人级（几十~几百条），重建成本可忽略。

降级：faiss / rank-bm25 / jieba 任一缺失都不影响 jsonl CRUD 与去重（去重用 numpy）；
dense 缺 faiss 时回退 numpy 点积，sparse 缺 jieba/bm25 时该通路禁用。
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from pydantic import BaseModel, Field

from app.logger import logger

# 可选依赖，best-effort 导入；缺失则对应通路降级。
try:
    import faiss
except Exception:  # pragma: no cover - 取决于安装环境
    faiss = None
try:
    from rank_bm25 import BM25Okapi
except Exception:  # pragma: no cover
    BM25Okapi = None
try:
    import jieba
except Exception:  # pragma: no cover
    jieba = None


# 新经验与已有 task 余弦超过此阈值视为同一任务（去重/覆盖）。
DEDUP_COSINE_THRESHOLD = 0.95


class Recipe(BaseModel):
    """一条沉淀下来的成功经验。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task: str
    steps: List[str] = Field(default_factory=list)
    result_summary: str = ""
    tips: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    step_count: int = 0
    created_at: str = ""
    embedding: List[float] = Field(default_factory=list)


def _tokenize(text: str) -> Optional[List[str]]:
    """中文分词（jieba）。jieba 不可用时返回 None，表示 sparse 通路不可用。"""
    if jieba is None:
        return None
    return [t for t in jieba.lcut(text or "") if t.strip()]


class RecipeStore:
    """经验的持久化与索引。所有写操作经 `asyncio.Lock` 串行化。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._recipes: List[Recipe] = []
        self._lock = asyncio.Lock()
        # 派生索引（从 _recipes 重建）
        self._emb_mat: Optional[np.ndarray] = None  # (N, dim) 归一化向量，按 _recipes 下标对齐
        self._dim: Optional[int] = None
        self._index = None  # faiss.IndexFlatIP（可用时）
        self._bm25 = None  # BM25Okapi（可用时）
        self.load()

    # ---------- 读 ----------

    def all(self) -> List[Recipe]:
        return list(self._recipes)

    def get(self, recipe_id: str) -> Optional[Recipe]:
        return next((r for r in self._recipes if r.id == recipe_id), None)

    def __len__(self) -> int:
        return len(self._recipes)

    # ---------- 写（C/U/D，全部经 _persist_and_rebuild）----------

    async def add(self, recipe: Recipe) -> Recipe:
        """新增或覆盖（去重：同任务留 step_count 更少的那条，持平留新的）。

        Returns:
            实际生效的 Recipe（覆盖时可能是旧条目，被丢弃时返回传入的 recipe）。
        """
        async with self._lock:
            if not recipe.id:
                recipe.id = uuid.uuid4().hex[:12]
            if not recipe.created_at:
                recipe.created_at = datetime.now().isoformat(timespec="seconds")
            recipe.step_count = recipe.step_count or len(recipe.steps)

            self._validate_dim(recipe)

            dup_idx = self._find_duplicate(recipe)
            if dup_idx is not None:
                old = self._recipes[dup_idx]
                if recipe.step_count <= old.step_count:
                    self._recipes[dup_idx] = recipe  # 留更精炼（或持平更新）的
                    logger.info(f"经验去重：覆盖同任务旧条目 {old.id}（{old.step_count}→{recipe.step_count} 步）")
                    self._persist_and_rebuild()
                    return recipe
                logger.info(f"经验去重：保留更精炼的旧条目 {old.id}，丢弃新条目")
                return old

            self._recipes.append(recipe)
            self._persist_and_rebuild()
            return recipe

    async def delete(self, recipe_id: str) -> bool:
        """按 id 删除。命中返回 True。"""
        async with self._lock:
            before = len(self._recipes)
            self._recipes = [r for r in self._recipes if r.id != recipe_id]
            if len(self._recipes) == before:
                return False
            self._persist_and_rebuild()
            return True

    # ---------- 检索辅助（供 retriever 调用，索引下标对齐 _recipes）----------

    def dense_search(self, query_vec: List[float], top_n: int) -> List[Tuple[int, float]]:
        """返回 [(下标, 余弦)]，按余弦降序。faiss 可用走 faiss，否则 numpy 点积。"""
        if self._emb_mat is None or not query_vec:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        if q.shape[0] != self._dim:
            logger.warning(f"dense_search 维度不一致：query {q.shape[0]} vs 库 {self._dim}")
            return []
        top_n = min(top_n, len(self._recipes))
        if self._index is not None:
            scores, idxs = self._index.search(q.reshape(1, -1), top_n)
            return [(int(i), float(s)) for i, s in zip(idxs[0], scores[0]) if i >= 0]
        sims = self._emb_mat @ q
        order = np.argsort(sims)[::-1][:top_n]
        return [(int(i), float(sims[i])) for i in order]

    def sparse_search(self, query: str, top_n: int) -> List[Tuple[int, float]]:
        """BM25 检索，返回 [(下标, 分数)]，按分数降序（仅正分）。"""
        if self._bm25 is None:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = np.asarray(self._bm25.get_scores(tokens), dtype=np.float32)
        order = np.argsort(scores)[::-1][: min(top_n, len(self._recipes))]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]

    # ---------- 内部 ----------

    def load(self) -> None:
        """从 jsonl 全量加载并重建索引。文件不存在则空库。"""
        self._recipes = []
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._recipes.append(Recipe.model_validate_json(line))
            except Exception as e:
                logger.error(f"加载经验库失败（{self.path}）：{e}")
                self._recipes = []
        self._rebuild_indices()
        logger.info(f"经验库已加载：{len(self._recipes)} 条（{self.path}）")

    def _validate_dim(self, recipe: Recipe) -> None:
        """维度一致性校验，防止混用不同 embedding 模型污染索引。"""
        if recipe.embedding and self._dim is not None:
            if len(recipe.embedding) != self._dim:
                raise ValueError(
                    f"embedding 维度不一致：新 {len(recipe.embedding)} vs 库 {self._dim}，"
                    "疑似混用了不同的 embedding 模型。"
                )

    def _find_duplicate(self, recipe: Recipe) -> Optional[int]:
        """返回与 recipe 余弦 > 阈值的已有条目下标；无则 None。"""
        if not recipe.embedding or self._emb_mat is None:
            return None
        q = np.asarray(recipe.embedding, dtype=np.float32)
        if q.shape[0] != self._dim:
            return None
        sims = self._emb_mat @ q
        best = int(np.argmax(sims))
        return best if float(sims[best]) > DEDUP_COSINE_THRESHOLD else None

    def _persist_and_rebuild(self) -> None:
        """整文件原子重写 jsonl，然后重建全部派生索引。"""
        self._persist()
        self._rebuild_indices()

    def _persist(self) -> None:
        """临时文件 + os.replace 原子替换，防写一半崩坏。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in self._recipes:
                f.write(r.model_dump_json() + "\n")
        os.replace(tmp, self.path)

    def _rebuild_indices(self) -> None:
        """从 _recipes 重建 emb 矩阵、Faiss、BM25（均按下标对齐 _recipes）。"""
        # 1) embedding 矩阵：以首条非空向量定维度，缺失向量零填充以保持下标对齐。
        self._dim = next((len(r.embedding) for r in self._recipes if r.embedding), None)
        if self._dim:
            mat = np.zeros((len(self._recipes), self._dim), dtype=np.float32)
            for i, r in enumerate(self._recipes):
                if r.embedding and len(r.embedding) == self._dim:
                    mat[i] = np.asarray(r.embedding, dtype=np.float32)
            self._emb_mat = mat
        else:
            self._emb_mat = None

        # 2) Faiss（可用时）
        self._index = None
        if faiss is not None and self._emb_mat is not None:
            try:
                index = faiss.IndexFlatIP(self._dim)
                index.add(self._emb_mat)
                self._index = index
            except Exception as e:  # pragma: no cover
                logger.warning(f"Faiss 索引重建失败，回退 numpy：{e}")
                self._index = None

        # 3) BM25（jieba + rank-bm25 都可用时）
        self._bm25 = None
        if BM25Okapi is not None and jieba is not None and self._recipes:
            corpus = []
            for r in self._recipes:
                text = r.task + " " + " ".join(r.steps)
                corpus.append(_tokenize(text) or [])
            try:
                self._bm25 = BM25Okapi(corpus)
            except Exception as e:  # pragma: no cover
                logger.warning(f"BM25 索引重建失败：{e}")
                self._bm25 = None
