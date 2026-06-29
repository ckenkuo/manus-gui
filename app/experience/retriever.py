"""混合检索：dense（向量）+ sparse（BM25）→ RRF 融合 → 相关性闸门。

RRF（Reciprocal Rank Fusion）：`score = Σ 1/(rrf_k + rank)`，无需归一化/调参即可
融合两路异质排名。融合后用相关性下限闸过滤，防止给无关任务硬塞经验。

降级：dense 不可用（embedding 失败/无 faiss）→ 仅 BM25；BM25 不可用 → 仅 dense；
两路都空 → 返回空。任何异常都不向上抛（由 __init__ 的 retrieve_recipes 兜底）。
"""

from typing import List

from app.config import config
from app.experience.embedding import embed_texts
from app.experience.store import Recipe, RecipeStore
from app.logger import logger

# 每路取的候选数：比最终 k 多取一些，给 RRF 融合留空间。
_CANDIDATE_MULTIPLIER = 3
_MIN_CANDIDATES = 10


def _rrf_accumulate(scores: dict, ranked: List, rrf_k: int) -> None:
    """把一路排名按 RRF 累加进 scores（key=下标）。ranked: [(idx, score), ...]。"""
    for rank, (idx, _score) in enumerate(ranked):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (rrf_k + rank + 1)


async def retrieve(store: RecipeStore, query: str, k: int) -> List[Recipe]:
    """检索最多 k 条相关经验。"""
    if len(store) == 0 or not query.strip():
        return []

    cfg = config.experience
    rrf_k = cfg.rrf_k if cfg else 60
    min_score = cfg.min_score if cfg else 0.35
    top_n = max(k * _CANDIDATE_MULTIPLIER, _MIN_CANDIDATES)

    # dense 通路（best-effort，失败则空）。embed 是唯一 await，先做完再快照，
    # 避免快照与 dense/sparse 返回的下标因并发 add 错位。
    dense: List = []
    try:
        vecs = await embed_texts([query])
    except Exception as e:
        logger.warning(f"dense 检索不可用，降级仅 BM25：{e}")
        vecs = []

    # await 之后一次性快照：recipes 与下面两路 search 返回的下标对齐同一状态。
    recipes = store.all()
    if vecs:
        dense = store.dense_search(vecs[0], top_n)
    sparse = store.sparse_search(query, top_n)

    if not dense and not sparse:
        return []

    # RRF 融合
    fused: dict = {}
    _rrf_accumulate(fused, dense, rrf_k)
    _rrf_accumulate(fused, sparse, rrf_k)

    dense_cos = {idx: score for idx, score in dense}
    dense_set = {idx for idx, _ in dense}
    sparse_set = {idx for idx, _ in sparse}
    dense_available = bool(dense)

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    # 相关性闸门：
    #   - dense 可用：dense 余弦达标 或 同时命中两路；
    #   - dense 不可用（降级仅 BM25）：命中 BM25 即视为可用（已是词面强匹配）。
    out: List[Recipe] = []
    for idx, _fused_score in ordered:
        if len(out) >= k:
            break
        if idx >= len(recipes):  # 快照与索引极端错位的防御
            continue
        if dense_available:
            passes_gate = dense_cos.get(idx, 0.0) >= min_score or (
                idx in dense_set and idx in sparse_set
            )
        else:
            passes_gate = idx in sparse_set
        if passes_gate:
            out.append(recipes[idx])

    logger.info(f"经验检索：候选 {len(fused)} 条，过闸门后注入 {len(out)} 条")
    return out
