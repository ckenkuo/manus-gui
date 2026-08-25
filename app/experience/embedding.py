"""文本向量化（DashScope text-embedding-v4，OpenAI 兼容接口）。

建库与查询使用同一模型与同一归一化方式（L2），归一化后向量点积即余弦相似度，
配合 store 里的 `IndexFlatIP`/numpy 点积构成 dense 检索通路。

接入复用 [app/tool/gui_agent.py] 的 DashScope 模式：从 `config.llm["default"]`
取 api_key/base_url（项目实测主模型即 DashScope 兼容端点）。
"""

from typing import List, Optional

import numpy as np
from openai import AsyncOpenAI

from app.config import config
from app.logger import logger


def _resolve_endpoint() -> tuple[str, str]:
    """从默认 LLM 配置解析 embedding 接入的 api_key / base_url。"""
    settings = config.llm.get("default")
    if settings is None or not settings.api_key or not settings.base_url:
        raise ValueError(
            "未找到可用的 embedding 接入配置：请确保 config.toml 的 [llm] 段"
            "（api_key/base_url）有效。"
        )
    return settings.api_key, settings.base_url


def _default_model() -> str:
    exp = config.experience
    return exp.embedding_model if exp else "text-embedding-v4"


async def embed_texts(
    texts: List[str], model: Optional[str] = None
) -> List[List[float]]:
    """把若干文本编码为 L2 归一化向量。

    Args:
        texts: 待编码文本列表（非空）。
        model: 覆盖默认 embedding 模型。

    Returns:
        与 texts 等长的归一化向量列表。失败时抛异常，由调用方 best-effort 兜底。
    """
    if not texts:
        return []

    api_key, base_url = _resolve_endpoint()
    model = model or _default_model()

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    resp = await client.embeddings.create(model=model, input=texts)
    # DashScope 兼容接口可能不保证返回顺序，按 index 排序后再取向量。
    items = sorted(resp.data, key=lambda d: d.index)

    normalized: List[List[float]] = []
    for item in items:
        vec = np.asarray(item.embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        normalized.append((vec / norm).tolist() if norm > 0 else vec.tolist())

    logger.debug(f"embed_texts: {len(normalized)} 条向量，维度 {len(normalized[0]) if normalized else 0}")
    return normalized
