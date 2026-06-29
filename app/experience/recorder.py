"""把一次成功运行的轨迹提炼（distill）成一条可复用经验。

只产出候选 Recipe、**不落库**（落库交给 main.py 的交互确认入口）。全程 best-effort：
任何异常都返回 None + 日志，绝不影响主流程。

输入：先把 `agent.memory.messages` 压成紧凑文本转录——每步只留
`思考摘要 + 工具名 + 语义关键参数 + 关键结果片段`，剥掉 base64 截图与整页 DOM 列表。
模型：固定用 `config.llm["default"]` 文本模型（不要用 agent.llm，浏览器跑完时它
可能停在视觉模型上）。tools_used / step_count 程序化补全，不让 LLM 产出。
"""

import json
import re
from datetime import datetime
from typing import List, Optional, Tuple

from app.config import config
from app.experience.embedding import embed_texts
from app.experience.store import Recipe
from app.llm import LLM
from app.logger import logger
from app.schema import Message

# 转录截断阈值（字符），控制喂给 LLM 的体量。
_THOUGHT_MAX = 300
_ARGS_MAX = 200
_RESULT_MAX = 300


DISTILL_SYSTEM_PROMPT = """你是一个"任务经验提炼器"。输入是一段 AI agent 完成某任务的执行轨迹，
你要提炼成一份可复用的高层操作经验，供将来相似任务参考。

## 输出格式（严格 JSON，只输出 JSON 本身，禁止任何额外文字或 ``` 代码块标记）
{
  "steps": ["1. ...（本例: ...）", "2. ...", ...],
  "result_summary": "一句话：最终达成了什么，含关键结果（如查到的票价/订单号）",
  "tips": ["关键踩坑对策1", "关键踩坑对策2", ...]
}

## 提炼原则（决定经验有没有用，逐条遵守）
1. 【去噪，只留成功路径】轨迹里的试错、走错回退、重复无效操作一律剔除，
   只保留真正通向成功的有效步骤。下次的 agent 要靠你这份经验少走弯路。
2. 【抹掉易过期的机械细节】绝对不要写 DOM 元素索引（如 [5]）、像素坐标（x/y）、
   截图里的具体位置——这些下次必然失效，照搬反而误导。
3. 【关键踩坑对策放进 tips】把"为什么这么做"的非显而易见的判断、坑与解法
   单独写进 tips 列表（例：某类站点需先接管已登录浏览器；日期用专门的日期
   选择工具比视觉点击稳）。全局性教训也放这。这是经验最值钱的部分，务必提取。
4. 【正确颗粒度】每步是一个有意义的子目标（打开页面/填表单/选日期/提交/读结果），
   不是每一次点击。一般 5~12 步。
5. 【泛化+本例括注】步骤用占位描述，但在括号里附本例的具体值。
   例："在出发地、到达地输入框分别填入对应城市（本例：上海、北京）"，
   而不是写死"输入上海"、也不是完全不给例子。
6. 步骤用中文，动词开头，可执行、可复述。"""


def _truncate(text: Optional[str], limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def build_transcript(agent) -> Tuple[str, str, List[str]]:
    """把 agent 记忆压成紧凑转录。

    Returns:
        (task, transcript, tools_used)
        task: 首条 user 消息（原始请求）。
        transcript: 喂给 LLM 的紧凑文本。
        tools_used: 按出现顺序去重的工具名（程序化统计）。
    """
    task = ""
    lines: List[str] = []
    tools_used: List[str] = []
    step = 0
    task_captured = False

    for msg in agent.memory.messages:
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)

        if role == "user":
            if not task_captured:
                task = (content or "").strip()
                task_captured = True
                lines.append(f"[任务] {task}")
            # 后续 user 消息是循环下发的 next_step_prompt 样板，跳过以降噪。
            continue

        if role == "assistant":
            step += 1
            thought = _truncate(content, _THOUGHT_MAX)
            lines.append(f"[步骤{step}] 思考: {thought}")
            for tc in getattr(msg, "tool_calls", None) or []:
                name = tc.function.name
                if name not in tools_used:
                    tools_used.append(name)
                lines.append(f"  调用: {name}({_truncate(tc.function.arguments, _ARGS_MAX)})")

        elif role == "tool":
            # tool 消息的 base64 截图在独立字段，不在 content，天然不会带进来。
            lines.append(f"  结果: {_truncate(content, _RESULT_MAX)}")

    return task, "\n".join(lines), tools_used


def _parse_distill_json(text: str) -> Optional[dict]:
    """剥 ```json 围栏后 json.loads；失败返回 None。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def distill_recipe(agent) -> Optional[Recipe]:
    """从成功运行的 agent 提炼一条候选经验（不落库）。失败返回 None。"""
    try:
        task, transcript, tools_used = build_transcript(agent)
        if not task or not transcript:
            logger.warning("distill：轨迹为空，跳过。")
            return None

        # 固定用 default 文本模型，避免误用浏览器残留的视觉模型。
        llm = LLM(config_name="default")
        raw = await llm.ask(
            messages=[Message.user_message(transcript)],
            system_msgs=[Message.system_message(DISTILL_SYSTEM_PROMPT)],
            stream=False,
            temperature=0.0,
        )

        data = _parse_distill_json(raw)
        if not data or not data.get("steps"):
            logger.warning(f"distill：LLM 输出解析失败或无 steps。原始：{_truncate(raw, 200)}")
            return None

        steps = [str(s) for s in data.get("steps", [])]
        tips = [str(t) for t in data.get("tips", []) if str(t).strip()]
        result_summary = str(data.get("result_summary", "")).strip()

        # task 向量（best-effort；失败则空向量，仍可入库走 BM25）
        embedding: List[float] = []
        try:
            vecs = await embed_texts([task])
            embedding = vecs[0] if vecs else []
        except Exception as e:
            logger.warning(f"distill：task 向量化失败，经验将仅参与 BM25：{e}")

        return Recipe(
            task=task,
            steps=steps,
            result_summary=result_summary,
            tips=tips,
            tools_used=tools_used,
            step_count=len(steps),
            created_at=datetime.now().isoformat(timespec="seconds"),
            embedding=embedding,
        )
    except Exception as e:
        logger.warning(f"distill 失败（忽略）：{e}")
        return None
