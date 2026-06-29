"""基于视觉的 GUI 操作代理（few-shot 嵌入）。

与 browser_use 的 DOM 索引操作并列、平等的另一种交互方式，由 LLM 按页面
情况自主选择。尤其适合目标控件未出现在 DOM 元素列表中、或自定义控件
（日历、地图、画布、富文本、图片热区）难以被 DOM 树捕获的场景。

工作原理：截取当前页面视口截图 -> 发送给视觉模型（阿里云百炼 gui-plus）
-> 模型返回单一、精确的 GUI 原子操作（基于像素坐标）-> 调用方按坐标
驱动 Playwright 执行。

参考实现：CASE-gui-plus 中的 few-shot 调用方式。
"""

import json
import os
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

from app.config import config
from app.logger import logger


# 视觉模型的 few-shot 系统提示词（移植自参考实现 CASE-gui-plus）。
# 约束模型输出严格 JSON 的单一原子操作，基于截图中的视觉证据决策。
GUI_SYSTEM_PROMPT = """## 1. 核心角色 (Core Role)
你是一个顶级的AI视觉操作代理。你的任务是分析电脑屏幕截图，理解用户的指令，然后将任务分解为单一、精确的GUI原子操作。

## 2. [CRITICAL] JSON Schema & 绝对规则
你的输出**必须**是一个严格符合以下规则的JSON对象。**任何偏差都将导致失败**。
- **[R1] 严格的JSON**: 你的回复**必须**是且**只能是**一个JSON对象。禁止在JSON代码块前后添加任何文本、注释或解释。
- **[R2] 严格的Parameters结构**:`thought`对象的结构: "在这里用一句话简要描述你的思考过程。例如：用户想打开浏览器，我看到了桌面上的Chrome浏览器图标，所以下一步是点击它。"
- **[R3] 精确的Action值**: `action`字段的值**必须**是`## 3. 工具集`中定义的一个大写字符串（例如 `"CLICK"`, `"TYPE"`），不允许有任何前导/后置空格或大小写变化。
- **[R4] 严格的Parameters结构**: `parameters`对象的结构**必须**与所选Action在`## 3. 工具集`中定义的模板**完全一致**。键名、值类型都必须精确匹配。

## 3. 工具集 (Available Actions)
### CLICK
- **功能**: 单击屏幕。
- **Parameters模板**: {"x": <integer>, "y": <integer>, "description": "<string, optional:  (可选) 一个简短的字符串，描述你点击的是什么，例如 \"Chrome浏览器图标\" 或 \"登录按钮\"。>"}
### TYPE
- **功能**: 输入文本。
- **Parameters模板**: {"text": "<string>", "needs_enter": <boolean>}
### SCROLL
- **功能**: 滚动窗口。
- **Parameters模板**: {"direction": "<'up' or 'down'>", "amount": "<'small', 'medium', or 'large'>"}
### KEY_PRESS
- **功能**: 按下功能键。
- **Parameters模板**: {"key": "<string: e.g., 'enter', 'esc', 'alt+f4'>"}
### FINISH
- **功能**: 任务成功完成。
- **Parameters模板**: {"message": "<string: 总结任务完成情况>"}
### FAILE
- **功能**: 任务无法完成。
- **Parameters模板**: {"reason": "<string: 清晰解释失败原因>"}

## 4. 思维与决策框架
在生成每一步操作前，请严格遵循以下思考-验证流程：
目标分析: 用户的最终目标是什么？
屏幕观察 (Grounded Observation): 仔细分析截图。你的决策必须基于截图中存在的视觉证据。 如果你看不见某个元素，你就不能与它交互。
行动决策: 基于目标和可见的元素，选择最合适的工具。
构建输出:
a. 在thought字段中记录你的思考。
b. 选择一个action。
c. 精确复制该action的parameters模板，并填充值。
最终验证 (Self-Correction): 在输出前，最后检查一遍：我的回复是纯粹的JSON吗？action的值是否正确无误（大写、无空格）？parameters的结构是否与模板100%一致？例如，对于CLICK，是否有独立的x和y键，并且它们的值都是整数？"""


# 默认接入信息（阿里云百炼 DashScope，OpenAI 兼容模式）。
# 仅在 config 未配置 [llm.gui]/[llm.vision] 时，作为 env DASHSCOPE_API_KEY 的兜底。
_DEFAULT_GUI_MODEL = "qwen3.7-plus"
_DEFAULT_GUI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 单次原子操作的输出预算。够输出一个 JSON 操作（含 thought）即可，
# 设小了会把坐标截断（如 '"x": [703,' 处中断），导致 JSON 不合法。
_DEFAULT_GUI_MAX_TOKENS = 1024
# DashScope 部分视觉模型对 max_tokens 有硬上限（如 gui-plus 为 2048），超过会
# 返回 400 InvalidParameter。通用 LLM 配置常设到 4096/8192，若直接透传到
# [llm.gui] 会触发该错误，故在解析时统一钳制到此上限（单个操作 JSON 足够）。
_GUI_MAX_TOKENS_LIMIT = 2048
# 视觉循环里若温度=0，相似截图会得到逐字节相同的决策，导致同一坐标空转死循环。
# 给一个小温度让模型在重试时有机会跳出。仅作用于 gui_action 视觉调用。
_GUI_TEMPERATURE = 0.3
# 单次视觉调用的超时（秒）。无超时则模型/网络一旦卡住会无限等待（实测卡过 100+ 秒），
# 拖死整个 gui_action 循环。超时后抛出，由调用方转成明确错误并让外层改走 DOM。
_GUI_REQUEST_TIMEOUT = 60.0


def _resolve_gui_llm_settings() -> Dict[str, str]:
    """解析视觉模型的接入配置。

    优先级：config.toml 中的 [llm.gui] > [llm.vision] > 默认 gui-plus + 环境变量。
    """
    llm_settings = config.llm
    settings = llm_settings.get("gui") or llm_settings.get("vision")

    if settings is not None and settings.api_key and settings.base_url:
        return {
            "model": settings.model or _DEFAULT_GUI_MODEL,
            "base_url": settings.base_url,
            "api_key": settings.api_key,
            "max_tokens": min(
                settings.max_tokens or _DEFAULT_GUI_MAX_TOKENS,
                _GUI_MAX_TOKENS_LIMIT,
            ),
        }

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise ValueError(
            "未找到 GUI 视觉模型配置：请在 config.toml 添加 [llm.gui] 段，"
            "或设置环境变量 DASHSCOPE_API_KEY。"
        )
    return {
        "model": _DEFAULT_GUI_MODEL,
        "base_url": _DEFAULT_GUI_BASE_URL,
        "api_key": api_key,
        "max_tokens": _DEFAULT_GUI_MAX_TOKENS,
    }


def _parse_action(content: str) -> Dict[str, Any]:
    """解析视觉模型返回的 JSON 原子操作。

    模型偶尔会用 ```json ... ``` 包裹，做容错处理。
    """
    text = content.strip()
    if text.startswith("```"):
        # 去除 ```json 或 ``` 围栏
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip().strip("`").strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"视觉模型返回的不是合法 JSON：{content!r}") from e

    action = parsed.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError(f"视觉模型返回缺少有效的 action 字段：{parsed!r}")

    params = parsed.get("parameters", {}) or {}
    normalized_action = action.strip().upper()
    if normalized_action == "CLICK":
        params = _normalize_click_coords(params)

    return {
        "thought": parsed.get("thought", ""),
        "action": normalized_action,
        "parameters": params,
    }


def _normalize_click_coords(params: Dict[str, Any]) -> Dict[str, Any]:
    """把 CLICK 的坐标归一化为标量 x/y。

    Qwen-VL 系模型可能以多种格式返回坐标，需统一为整数 x/y：
    - 标量：{"x": 351, "y": 427}（理想情况，直接用）
    - point 列表：x 字段本身是 [x, y]，或单独的 "point"/"coordinate" 字段
    - bbox 列表：[x1, y1, x2, y2]，取中心点

    无法解析时保持原值，由执行层报出明确错误。
    """

    def _from_seq(seq):
        """从序列推断点坐标：长度2视为[x,y]，长度4视为bbox取中心。"""
        nums = [n for n in seq if isinstance(n, (int, float))]
        if len(nums) == 2:
            return float(nums[0]), float(nums[1])
        if len(nums) == 4:
            return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
        return None

    # 1) x 字段直接是列表（point 或 bbox）
    x_val = params.get("x")
    if isinstance(x_val, (list, tuple)):
        pt = _from_seq(x_val)
        if pt:
            return {**params, "x": pt[0], "y": pt[1]}

    # 2) 单独的 point / coordinate / bbox 字段
    for key in ("point", "coordinate", "coordinates", "bbox", "box"):
        seq = params.get(key)
        if isinstance(seq, (list, tuple)):
            pt = _from_seq(seq)
            if pt:
                return {**params, "x": pt[0], "y": pt[1]}

    # 3) 已是标量或无法解析，原样返回
    return params


def _build_user_text(task: str, history: Optional[list]) -> str:
    """把子目标与「已执行操作历史」拼成用户文本。

    在内部循环里，模型每一步都会重新看到最新截图。把历史回传给它，能让它
    基于当前截图判断目标是否已达成（输出 FINISH），而不是机械地重复点击。
    """
    if not history:
        return task

    lines = []
    for i, h in enumerate(history, 1):
        params = h.get("parameters", {})
        thought = h.get("thought", "")
        lines.append(f"{i}. {h.get('action', '')} {params} — {thought}")
    return (
        f"{task}\n\n## 你在本次任务中已执行过的操作历史\n"
        + "\n".join(lines)
        + "\n\n请基于【当前最新截图】判断：目标是否已经达成？"
        "若已达成（例如日期已选中、值已填入），请输出 FINISH，不要重复点击；"
        "若上一步操作没有产生预期变化，请重新观察截图、修正坐标后再操作。"
    )


async def query_gui_action(
    base64_image: str,
    task: str,
    image_mime: str = "image/png",
    history: Optional[list] = None,
    temperature: float = _GUI_TEMPERATURE,
) -> Dict[str, Any]:
    """调用视觉模型，根据截图与任务返回单一原子操作。

    Args:
        base64_image: 当前页面视口截图（base64 编码，无 data URL 前缀）。
        task: 自然语言描述的子目标，例如 "选择 1月30日 的出发日期"。
        image_mime: 截图 MIME 类型，默认为 image/png。
        history: 本次子任务内已执行的原子操作列表，用于让模型确认是否完成。
        temperature: 采样温度，默认小温度以避免确定性死循环。

    Returns:
        dict: {"thought": str, "action": str, "parameters": dict}

    Raises:
        ValueError: 配置缺失或模型返回不可解析。
    """
    gui_settings = _resolve_gui_llm_settings()
    image_data_url = f"data:{image_mime};base64,{base64_image}"

    messages = [
        {"role": "system", "content": GUI_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {"type": "text", "text": _build_user_text(task, history)},
            ],
        },
    ]

    client = AsyncOpenAI(
        api_key=gui_settings["api_key"],
        base_url=gui_settings["base_url"],
    )

    logger.info(f"🖼️ GUI 视觉操作：调用 {gui_settings['model']} 处理目标 -> {task}")
    completion = await client.chat.completions.create(
        model=gui_settings["model"],
        messages=messages,
        max_tokens=gui_settings["max_tokens"],
        temperature=temperature,
        timeout=_GUI_REQUEST_TIMEOUT,
    )
    choice = completion.choices[0]
    content = choice.message.content or ""
    logger.debug(f"🖼️ GUI 视觉模型原始返回：{content}")

    # 检测输出被 token 上限截断：此时 JSON 必然不完整，给出明确错误而非解析失败
    if getattr(choice, "finish_reason", None) == "length":
        raise ValueError(
            f"视觉模型输出被截断（max_tokens={gui_settings['max_tokens']} 不足），"
            f"请调大 [llm.gui].max_tokens。已收到片段：{content!r}"
        )

    result = _parse_action(content)
    logger.info(
        f"🖼️ GUI 视觉决策：{result['action']} | thought: {result['thought']}"
    )
    return result
