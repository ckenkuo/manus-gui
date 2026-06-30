"""RAG 经验库：成功流程检索 + few-shot 注入。

公开 API（供 agent / main.py 调用，全部 best-effort，绝不中断主流程）：
- `is_enabled()`            — 特性总开关（[experience].enabled）。
- `retrieve_recipes(query)` — 检索相关经验（注入用）。
- `distill_recipe(agent)`   — 把成功运行提炼成候选 Recipe（只产出不落库）。
- `store`                   — RecipeStore 单例（落库/手动 CRUD）。
- `format_injection(recipes)` — 把经验渲染成注入 system_prompt 的文本块。

写入入口为「跑完交互确认」(main.py)：故此处只暴露 retrieve / distill / store，
落库的拍板留给 CLI 层，agent 层不耦合终端 IO。
"""

from typing import List, Optional

from app.config import PROJECT_ROOT, config
from app.experience.recorder import distill_recipe
from app.experience.store import Recipe, RecipeStore
from app.logger import logger

__all__ = [
    "is_enabled",
    "retrieve_recipes",
    "distill_recipe",
    "format_injection",
    "store",
    "Recipe",
    "RecipeStore",
]

# 经验库持久化路径：<project>/experience/recipes.jsonl
_RECIPES_PATH = PROJECT_ROOT / "experience" / "recipes.jsonl"

# 模块级单例：load() 廉价（个人级几十~几百条），import 时即重建索引。
store = RecipeStore(_RECIPES_PATH)


def is_enabled() -> bool:
    """经验库特性是否启用。"""
    return bool(config.experience and config.experience.enabled)


async def retrieve_recipes(query: str, k: Optional[int] = None) -> List[Recipe]:
    """检索与 query 最相关的经验。任何异常都吞掉并返回空。"""
    if not is_enabled() or not query:
        return []
    try:
        from app.experience.retriever import retrieve

        k = k or (config.experience.top_k if config.experience else 2)
        return await retrieve(store, query, k)
    except Exception as e:
        logger.warning(f"经验检索失败（忽略）：{e}")
        return []


def format_injection(recipes: List[Recipe]) -> str:
    """把检索到的经验渲染成追加进 system_prompt 的 few-shot 块。"""
    if not recipes:
        return ""

    parts: List[str] = [
        "\n\n## 历史成功经验（仅供参考，请按当前页面实际情况调整，"
        "不要照搬旧的元素 index/坐标）",
    ]
    for i, r in enumerate(recipes, 1):
        parts.append(f"\n### 经验 {i}：{r.task}")
        if r.steps:
            parts.append("步骤：")
            parts.extend(f"  {s}" for s in r.steps)
        if r.result_summary:
            parts.append(f"结果：{r.result_summary}")
        if r.tips:
            parts.append("关键提示：")
            parts.extend(f"  - {t}" for t in r.tips)
    return "\n".join(parts)
