import math
from typing import Dict, List, Optional, Union

import tiktoken
from openai import (
    APIError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.bedrock import BedrockClient
from app.config import LLMSettings, config
from app.exceptions import EmptyContentTruncated, TokenLimitExceeded
from app.logger import logger  # Assuming a logger is set up in your app
from app.schema import (
    ROLE_VALUES,
    TOOL_CHOICE_TYPE,
    TOOL_CHOICE_VALUES,
    Message,
    ToolChoice,
)


def _worth_retry_text(exc: BaseException) -> bool:
    """纯文本请求是否值得退避重试。

    排除三类确定性失败——它们重发只会原样再失败一次，却各要等掉一轮指数退避：
      - BadRequestError（400）：请求本身不合法，同 _worth_retry 的理由。
      - EmptyContentTruncated：推理链吃光额度，ask 内部已就地抬额度重发过一次
        （见 _RETRY_TOKEN_SCALE），到这一步说明抬了也没用。2026-08-22 属性审核
        实测：原先这类失败会走满 6 次退避，单次白等 2 分 17 秒。
      - TokenLimitExceeded：输入本身超限，重发一样超。原先装饰器里注释写着
        "Don't retry TokenLimitExceeded"，但 retry_if_exception_type 的元组里带着
        Exception（等于什么都重试），这条豁免形同虚设——顺手做实。
    """
    return not isinstance(
        exc, (BadRequestError, EmptyContentTruncated, TokenLimitExceeded))


def _worth_retry(exc: BaseException) -> bool:
    """带图请求是否值得重试：400 与「抬额度后仍返空」一律不重试。

    【为什么单独判】原先 retry_if_exception_type 里写了 Exception，等于什么都重试。
    2026-08-22 实测：Kimi 端点收到远程图片 URL 直接回 400
    "unsupported image url"，这是请求本身不合法、重发多少次都一样，却被退避重试
    6 次——白等几十秒、日志里同一条错误刷 6 遍，最后仍包成 RetryError 抛出，
    真正的原因反而被埋掉。故 400 视作确定性失败立即抛出，其余（限流/超时/网关
    抖动）照旧重试。

    EmptyContentTruncated 同样排除，理由与 _worth_retry_text 那条完全一样：
    ask_with_images 内部已就地抬过一次额度（见 _RETRY_TOKEN_SCALE），到这一步说明
    抬了也没用。带图重发还要把整批 base64 再传一遍（阶段⑬ 单次 11 张图），走满 6 次
    退避比纯文本更贵。
    """
    return not isinstance(exc, (BadRequestError, EmptyContentTruncated))


# 截断返空时就地重发用的额度倍数。1.5 是够用的经验值：deepseek 档配 32000，抬到
# 48000 足以让推理链跑完还剩下正文的量；给更大只是多烧钱（真不够时抬两倍也救不回来，
# 那属于提示词/模型选择问题，见 EmptyContentTruncated 的注释）。
_RETRY_TOKEN_SCALE = 1.5

# 【finish_reason="stop" 且正文空】另一码事：模型正常收尾却什么都没说，不是额度被
# 推理链吃光。2026-08-26 阶段⑦ 分色选图（8 张图）实测 completion_tokens=101、
# max_tokens=32000，额度根本没用完。原先这条只抛 ValueError 交给退避重试，日志里留
# 一行看不出根因的 "Validation error"，那次白等 18 秒才靠重试救回来。
#
# 就地重发一次而不是直接交退避：这类返空多半是单次抖动，重发即可，省掉一轮退避。
# 顺带把额度翻倍——理由不是「不够用」（明显够），而是有些端点在额度充裕时反而更愿意
# 产出正文，且翻倍不花额外的钱（没用到的额度不计费，只有真产出的 token 才计）。
# 抬过一次仍空就交给退避重试：那时才可能是提示词问题，与截断那条的处置刻意不同
# （截断抛 EmptyContentTruncated 直接放弃，因为额度确实是瓶颈、重发必然同样结果）。
_EMPTY_STOP_TOKEN_SCALE = 2.0


def _is_truncated_empty(response) -> bool:
    """是不是「推理链吃光额度导致正文为空」这种确定性失败。

    判据是 finish_reason == "length" 且 content 为空：单看 content 空分不清是被
    截断（抬额度有救）还是模型真没话说（重发/改提示词才有救），两者处置完全相反。
    """
    if not response.choices:
        return False
    ch = response.choices[0]
    return ch.finish_reason == "length" and not (ch.message.content or "").strip()


def _empty_response_detail(response, max_tokens: int) -> str:
    """给「响应为空」的报错补上 finish_reason 与 usage。

    【为什么值得单独抽出来】2026-08-22 属性审核踩的坑：推理型模型（deepseek 视觉档、
    grok、kimi）先产 reasoning 链再产 content，额度不够时把 max_tokens 全耗在推理上、
    content 返回空，端点侧表现为 finish_reason="length"。原先只抛一句
    "Empty or invalid response from LLM"，日志里分不清是被截断（该调高 max_tokens）
    还是模型真的没话说（该改提示词）——白等两分多钟又只能靠猜。
    """
    if not response.choices:
        return "no choices"
    finish = response.choices[0].finish_reason
    used = getattr(response.usage, "completion_tokens", "?")
    detail = (f"finish_reason={finish}, completion_tokens={used}, "
              f"max_tokens={max_tokens}")
    if finish == "length":
        detail += "（额度耗尽在推理链上，需调高 max_tokens）"
    return detail


REASONING_MODELS = ["o1", "o3-mini"]
# 启用了 thinking 模式的模型，不支持 tool_choice="required" 或指定具体 function
THINKING_MODELS = [
    "qwen3.7-plus",  # DashScope 思考模式模型
]
MULTIMODAL_MODELS = [
    "gpt-4-vision-preview",
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
    "qwen-vl-plus",  # DashScope 视觉模型
    "qwen-vl-max",  # DashScope 视觉模型
    "qwen/qwen2.5-vl-72b-instruct",  # DashScope 视觉模型
    "qwen3-vl-plus",  # DashScope 视觉模型（同款图片匹配用；不加则 ask 丢图、ask_with_images 抛 ValueError）
    # 主模型 qwen3.7-plus 也是多模态：2026-07-03 对 DashScope 端点实发商品图，能准确
    # 描述图中主体（颜色/形状/品类）。此前误判为纯文本，导致 ask 静默丢图、每次文本调用
    # 还误报"does NOT support images"。加入后：视觉档/GUI 档（均配 qwen3.7-plus）真正可用，
    # agent 兜底也能看页面截图（注意截图占 token，靠 max_input_tokens=60000 兜底）。
    "qwen3.7-plus",  # DashScope 思考+多模态主模型（实测可看图）
    "grok-4.6",  # packycode Grok 4.6 多模态模型（支持图像理解）
    # Kimi Code k3：官方文档（kimi.com/code/docs）对 k3-256k 注明「不支持视频输入」，
    # 反向说明 k3 支持图/视频输入，故登记。但 coding 端点（api.kimi.com/coding/v1）
    # 是否接受 base64 data URL 图未实测——不行就把 [llm.publish] 整体换回 grok。
    "k3",  # 2026-08-21 探针实测：base64 图可用（红色测试图答「红色」）
    "kimi-for-coding",  # Kimi K2.7 Code，同日实测文本与 base64 图均可用
    "k3-256k",  # Kimi k3 256K 档，同日实测文本与 base64 图均可用
    "kimi-for-coding-highspeed",  # Kimi K2.7 高速档，同日实测文本与 base64 图均可用
    # DeepSeek 视觉（api-docs.deepseek.com/zh-cn/guides/vision）：官方 chat/completions
    # 协议，content 用 text + image_url 两段，与本文件既有拼装完全一致，故无需另写分支。
    # 2026-08-22 实测：base64 与远程 URL 都收（少数几家两种都行的），temperature 0.0 可用。
    # 发布管线仍统一走 base64（image_ref 转码），不依赖服务端出网取图。
    # 2026-09-10：官方模型 ID 统一为 deepseek-flash，旧名 deepseek-v4-flash-vision-exp
    # 对应模型已下线。注意第三方网关（如 Packy cf.api.fan）上的 deepseek-v4-flash /
    # v4-pro 实测都返回 400「Model do not support image input」，不能拿来顶这一档。
    "deepseek-flash",
]


class TokenCounter:
    # Token 常量
    BASE_MESSAGE_TOKENS = 4
    FORMAT_TOKENS = 2
    LOW_DETAIL_IMAGE_TOKENS = 85
    HIGH_DETAIL_TILE_TOKENS = 170

    # 图像处理常量
    MAX_SIZE = 2048
    HIGH_DETAIL_TARGET_SHORT_SIDE = 768
    TILE_SIZE = 512

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def count_text(self, text: str) -> int:
        """计算文本字符串的 token 数"""
        return 0 if not text else len(self.tokenizer.encode(text))

    def count_image(self, image_item: dict) -> int:
        """
        根据细节级别和尺寸计算图像的 token 数

        对于 "low" 细节：固定 85 tokens
        对于 "high" 细节：
        1. 缩放到适合 2048x2048 正方形
        2. 将最短边缩放到 768px
        3. 计算 512px 瓦片数量（每个 170 tokens）
        4. 添加 85 tokens
        """
        detail = image_item.get("detail", "medium")

        # 对于低细节，始终返回固定 token 数
        if detail == "low":
            return self.LOW_DETAIL_IMAGE_TOKENS

        # 对于中等细节（OpenAI 中的默认值），使用高细节计算
        # OpenAI 没有为中等细节指定单独的计算方法

        # 对于高细节，如果可用，则基于尺寸计算
        if detail == "high" or detail == "medium":
            # 如果在 image_item 中提供了尺寸
            if "dimensions" in image_item:
                width, height = image_item["dimensions"]
                return self._calculate_high_detail_tokens(width, height)

        return (
            self._calculate_high_detail_tokens(1024, 1024) if detail == "high" else 1024
        )

    def _calculate_high_detail_tokens(self, width: int, height: int) -> int:
        """根据尺寸计算高细节图像的 token 数"""
        # 步骤 1：缩放到适合 MAX_SIZE x MAX_SIZE 正方形
        if width > self.MAX_SIZE or height > self.MAX_SIZE:
            scale = self.MAX_SIZE / max(width, height)
            width = int(width * scale)
            height = int(height * scale)

        # 步骤 2：缩放使最短边为 HIGH_DETAIL_TARGET_SHORT_SIDE
        scale = self.HIGH_DETAIL_TARGET_SHORT_SIDE / min(width, height)
        scaled_width = int(width * scale)
        scaled_height = int(height * scale)

        # 步骤 3：计算 512px 瓦片数量
        tiles_x = math.ceil(scaled_width / self.TILE_SIZE)
        tiles_y = math.ceil(scaled_height / self.TILE_SIZE)
        total_tiles = tiles_x * tiles_y

        # 步骤 4：计算最终 token 数
        return (
            total_tiles * self.HIGH_DETAIL_TILE_TOKENS
        ) + self.LOW_DETAIL_IMAGE_TOKENS

    def count_content(self, content: Union[str, List[Union[str, dict]]]) -> int:
        """计算消息内容的 token 数"""
        if not content:
            return 0

        if isinstance(content, str):
            return self.count_text(content)

        token_count = 0
        for item in content:
            if isinstance(item, str):
                token_count += self.count_text(item)
            elif isinstance(item, dict):
                if "text" in item:
                    token_count += self.count_text(item["text"])
                elif "image_url" in item:
                    token_count += self.count_image(item)
        return token_count

    def count_tool_calls(self, tool_calls: List[dict]) -> int:
        """计算工具调用的 token 数"""
        token_count = 0
        for tool_call in tool_calls:
            if "function" in tool_call:
                function = tool_call["function"]
                token_count += self.count_text(function.get("name", ""))
                token_count += self.count_text(function.get("arguments", ""))
        return token_count

    def count_message_tokens(self, messages: List[dict]) -> int:
        """计算消息列表中的 token 总数"""
        total_tokens = self.FORMAT_TOKENS  # 基础格式 tokens

        for message in messages:
            tokens = self.BASE_MESSAGE_TOKENS  # 每条消息的基础 tokens

            # 添加角色 tokens
            tokens += self.count_text(message.get("role", ""))

            # 添加内容 tokens
            if "content" in message:
                tokens += self.count_content(message["content"])

            # 添加工具调用 tokens
            if "tool_calls" in message:
                tokens += self.count_tool_calls(message["tool_calls"])

            # 添加 name 和 tool_call_id tokens
            tokens += self.count_text(message.get("name", ""))
            tokens += self.count_text(message.get("tool_call_id", ""))

            total_tokens += tokens

        return total_tokens


class LLM:
    _instances: Dict[str, "LLM"] = {}

    def __new__(
        cls, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if config_name not in cls._instances:
            instance = super().__new__(cls)
            instance.__init__(config_name, llm_config)
            cls._instances[config_name] = instance
        return cls._instances[config_name]

    def __init__(
        self, config_name: str = "default", llm_config: Optional[LLMSettings] = None
    ):
        if not hasattr(self, "client"):  # 仅在尚未初始化时初始化
            llm_config = llm_config or config.llm
            llm_config = llm_config.get(config_name, llm_config["default"])
            self.model = llm_config.model
            self.max_tokens = llm_config.max_tokens
            self.temperature = llm_config.temperature
            self.api_type = llm_config.api_type
            self.api_key = llm_config.api_key
            self.api_version = llm_config.api_version
            self.base_url = llm_config.base_url

            # 添加 token 计数相关属性
            self.total_input_tokens = 0
            self.total_completion_tokens = 0
            self.max_input_tokens = (
                llm_config.max_input_tokens
                if hasattr(llm_config, "max_input_tokens")
                else None
            )

            # 初始化 tokenizer
            try:
                self.tokenizer = tiktoken.encoding_for_model(self.model)
            except KeyError:
                # 如果模型不在 tiktoken 的预设中，使用 cl100k_base 作为默认值
                self.tokenizer = tiktoken.get_encoding("cl100k_base")

            if self.api_type == "azure":
                self.client = AsyncAzureOpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    api_version=self.api_version,
                )
            elif self.api_type == "aws":
                self.client = BedrockClient()
            else:
                self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

            self.token_counter = TokenCounter(self.tokenizer)

    @property
    def use_response_api(self) -> bool:
        """当前配置是否走 OpenAI Responses 协议（/v1/responses）而非 chat completions。

        由 api_type = "openai-response" 显式开启。为什么需要这个开关：
        2026-08-20 实测 packycode 网关（cf.api.fan）的 grok-4.5/4.6 只支持
        /v1/responses，打 /v1/chat/completions 直接 400 protocol_not_supported
        （纯文本和带图都一样）。同一批模型此前是支持 chat 的，网关侧改过——所以
        这不能按模型名硬编码，必须配置可切。
        """
        return (self.api_type or "").lower() in ("openai-response", "openai_response",
                                                 "responses")

    def _to_response_input(self, messages: List[dict]) -> tuple:
        """把 chat 格式的 messages 转成 Responses 协议的 (instructions, input)。

        两处形态差异（2026-08-20 实测网关行为）：
          - system 角色不进 input，要单独作为顶层 instructions 传；多条 system 用
            换行拼接。放进 input 里网关不报错但会被当普通用户消息，指令效力下降。
          - content 的类型名不同：chat 的 text/image_url → responses 的
            input_text/input_image，且 input_image 的 image_url 是【字符串】而不是
            chat 那样的 {"url": ...} 对象。写错不报错，图会被静默丢掉。
        """
        instructions = []
        items = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                if isinstance(content, str):
                    instructions.append(content)
                elif isinstance(content, list):
                    instructions.extend(c.get("text", "") for c in content
                                        if isinstance(c, dict) and c.get("text"))
                continue
            parts = []
            if isinstance(content, str):
                parts.append({"type": "input_text", "text": content})
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        parts.append({"type": "input_text", "text": c.get("text", "")})
                    elif c.get("type") == "image_url":
                        iu = c.get("image_url") or {}
                        url = iu.get("url") if isinstance(iu, dict) else iu
                        if url:
                            parts.append({"type": "input_image", "image_url": url})
            if parts:
                items.append({"role": role or "user", "content": parts})
        return ("\n\n".join(instructions), items)

    @staticmethod
    def _from_response_output(resp) -> str:
        """从 Responses 协议的响应里取出正文文本。

        output 是数组，混着 reasoning 项和 message 项，正文只在
        message.content[].output_text.text 里。【必须按 type 过滤】——直接取
        output[0] 会拿到 reasoning 的思考摘要（推理模型总是先产 reasoning），
        表现为「返回了一堆自言自语而不是答案」。
        """
        data = resp.model_dump() if hasattr(resp, "model_dump") else resp
        chunks = []
        for item in (data.get("output") or []):
            if item.get("type") != "message":
                continue
            for ci in (item.get("content") or []):
                if ci.get("type") == "output_text" and ci.get("text"):
                    chunks.append(ci["text"])
        return "".join(chunks).strip()

    async def _call_response_api(self, messages: List[dict],
                                 temperature: Optional[float] = None) -> str:
        """走 /v1/responses 发一次非流式请求并返回正文。

        max_output_tokens 用 self.max_tokens：推理模型先产 reasoning 再产正文
        （实测一次带图判断就吃掉 1062 个 reasoning token），给小了正文会是空串。
        故 [llm.publish] 的 max_tokens=16000 这个值在这条协议下同样要保留。
        """
        instructions, items = self._to_response_input(messages)
        params = {
            "model": self.model,
            "input": items,
            "max_output_tokens": self.max_tokens,
        }
        if instructions:
            params["instructions"] = instructions
        # temperature 对部分推理模型是非法参数，故仅在显式给值时才带上
        temp = temperature if temperature is not None else self.temperature
        if temp is not None:
            params["temperature"] = temp

        resp = await self.client.responses.create(**params)
        text = self._from_response_output(resp)
        usage = (resp.model_dump() if hasattr(resp, "model_dump") else resp).get("usage") or {}
        self.update_token_count(usage.get("input_tokens", 0),
                                usage.get("output_tokens", 0))
        if not text:
            # 【这条协议同样要就地抬额度重发一次】chat/completions 那两条路都做了
            # （见 _RETRY_TOKEN_SCALE / _EMPTY_STOP_TOKEN_SCALE），而这里原先只抛
            # ValueError 交退避重试——偏偏本协议是 grok 网关专用，而 grok 是发布管线的
            # 默认档（publish.llm._DEFAULT_CHOICE），等于默认配置反而没有这层保护。
            #
            # 倍数取 _EMPTY_STOP_TOKEN_SCALE（翻倍）而不是 1.5：Responses 协议下拿不到
            # finish_reason，分不清「被 reasoning 吃光」还是「模型没话说」，只能按更宽的
            # 那个来。翻倍不额外花钱——没用到的额度不计费。
            bigger = int(self.max_tokens * _EMPTY_STOP_TOKEN_SCALE)
            det = usage.get("output_tokens_details") or {}
            logger.warning(
                f"Responses API 返回空正文（output_tokens={usage.get('output_tokens')}, "
                f"reasoning_tokens={det.get('reasoning_tokens')}）"
                f"，把额度翻倍到 {bigger} 就地重发一次"
            )
            resp = await self.client.responses.create(
                **{**params, "max_output_tokens": bigger})
            text = self._from_response_output(resp)
            usage2 = (resp.model_dump() if hasattr(resp, "model_dump")
                      else resp).get("usage") or {}
            self.update_token_count(usage2.get("input_tokens", 0),
                                    usage2.get("output_tokens", 0))
            if not text:
                det2 = usage2.get("output_tokens_details") or {}
                raise ValueError(
                    f"Responses API 抬高额度后仍返回空正文"
                    f"（output_tokens={usage2.get('output_tokens')}, "
                    f"reasoning_tokens={det2.get('reasoning_tokens')}, "
                    f"max_output_tokens={bigger}）；多半是提示词或模型选择问题"
                )
        return text

    def count_tokens(self, text: str) -> int:
        """计算文本中的 token 数"""
        if not text:
            return 0
        return len(self.tokenizer.encode(text))

    def count_message_tokens(self, messages: List[dict]) -> int:
        return self.token_counter.count_message_tokens(messages)

    def update_token_count(self, input_tokens: int, completion_tokens: int = 0) -> None:
        """更新 token 计数"""
        # 仅在设置了 max_input_tokens 时跟踪 tokens
        self.total_input_tokens += input_tokens
        self.total_completion_tokens += completion_tokens
        logger.info(
            f"Token usage: Input={input_tokens}, Completion={completion_tokens}, "
            f"Cumulative Input={self.total_input_tokens}, Cumulative Completion={self.total_completion_tokens}, "
            f"Total={input_tokens + completion_tokens}, Cumulative Total={self.total_input_tokens + self.total_completion_tokens}"
        )

    def check_token_limit(self, input_tokens: int) -> bool:
        """检查是否超过 token 限制"""
        if self.max_input_tokens is not None:
            return (self.total_input_tokens + input_tokens) <= self.max_input_tokens
        # 如果未设置 max_input_tokens，始终返回 True
        return True

    def get_limit_error_message(self, input_tokens: int) -> str:
        """生成 token 限制超出的错误消息"""
        if (
            self.max_input_tokens is not None
            and (self.total_input_tokens + input_tokens) > self.max_input_tokens
        ):
            return f"Request may exceed input token limit (Current: {self.total_input_tokens}, Needed: {input_tokens}, Max: {self.max_input_tokens})"

        return "Token limit exceeded"

    @staticmethod
    def format_messages(
        messages: List[Union[dict, Message]], supports_images: bool = False
    ) -> List[dict]:
        """
        通过将消息转换为 OpenAI 消息格式来格式化 LLM 的消息。

        Args:
            messages: 可以是 dict 或 Message 对象的消息列表
            supports_images: 指示目标模型是否支持图像输入的标志

        Returns:
            List[dict]: OpenAI 格式的格式化消息列表

        Raises:
            ValueError: 如果消息无效或缺少必需字段
            TypeError: 如果提供了不支持的消息类型

        Examples:
            >>> msgs = [
            ...     Message.system_message("You are a helpful assistant"),
            ...     {"role": "user", "content": "Hello"},
            ...     Message.user_message("How are you?")
            ... ]
            >>> formatted = LLM.format_messages(msgs)
        """
        formatted_messages = []

        for message in messages:
            # 将 Message 对象转换为字典
            if isinstance(message, Message):
                message = message.to_dict()

            if isinstance(message, dict):
                # 如果消息是字典，确保它具有必需字段
                if "role" not in message:
                    raise ValueError("Message dict must contain 'role' field")

                # 如果存在 base64 图像且模型支持图像，则处理它们
                if supports_images and message.get("base64_image"):
                    # 初始化或将内容转换为适当格式
                    if not message.get("content"):
                        message["content"] = []
                    elif isinstance(message["content"], str):
                        message["content"] = [
                            {"type": "text", "text": message["content"]}
                        ]
                    elif isinstance(message["content"], list):
                        # 将字符串项转换为适当的文本对象
                        message["content"] = [
                            (
                                {"type": "text", "text": item}
                                if isinstance(item, str)
                                else item
                            )
                            for item in message["content"]
                        ]

                    # 将图像添加到内容中
                    message["content"].append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{message['base64_image']}"
                            },
                        }
                    )

                    # 删除 base64_image 字段
                    del message["base64_image"]
                # 如果模型不支持图像但消息有 base64_image，则优雅处理
                elif not supports_images and message.get("base64_image"):
                    # 仅删除 base64_image 字段并保留文本内容
                    del message["base64_image"]

                if "content" in message or "tool_calls" in message:
                    formatted_messages.append(message)
                # else: 不包含该消息
            else:
                raise TypeError(f"Unsupported message type: {type(message)}")

        # 验证所有消息都有必需字段
        for msg in formatted_messages:
            if msg["role"] not in ROLE_VALUES:
                raise ValueError(f"Invalid role: {msg['role']}")

        return formatted_messages

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        # 400 与「抬额度后仍返空」不重试（见 _worth_retry_text）：都是确定性失败，
        # 走满退避只是白等。其余（限流/超时/网关抖动/单次返空）照旧重试。
        retry=retry_if_exception(_worth_retry_text),
    )
    async def ask(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        stream: bool = True,
        temperature: Optional[float] = None,
    ) -> str:
        """
        向 LLM 发送提示并获取响应。

        Args:
            messages: 对话消息列表
            system_msgs: 可选的要前置的系统消息
            stream (bool): 是否流式传输响应
            temperature (float): 响应的采样温度

        Returns:
            str: 生成的响应

        Raises:
            TokenLimitExceeded: 如果超过 token 限制
            ValueError: 如果消息无效或响应为空
            OpenAIError: 如果 API 调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 检查模型是否支持图像
            supports_images = self.model in MULTIMODAL_MODELS

            # 调试信息：检查是否有图像输入
            has_images = any(
                isinstance(msg, dict) and msg.get("base64_image")
                or isinstance(msg, Message) and msg.base64_image
                for msg in (system_msgs or []) + messages
            )

            if supports_images:
                logger.info(f"👁️ Vision model enabled: {self.model} (supports images)")
                if has_images:
                    logger.info(f"📷 Image detected in messages - will be sent to vision model")
                else:
                    logger.debug(f"📷 No image in current messages")
            else:
                # 只有在有图片但模型不支持时才告警（对齐 ask_tool 的逻辑）；纯文本调用
                # （如 judge_price 读价走 qwen3.7-plus）无需喊，否则每次文本调用都刷一条
                # 误导性的"does NOT support images"警告。
                if has_images:
                    logger.warning(f"⚠️ Model {self.model} does NOT support images - visual understanding disabled")
                    logger.warning(f"⚠️ Images detected but will be ignored (model doesn't support vision)")

            # 使用图像支持检查格式化系统和用户消息
            if system_msgs:
                system_msgs = self.format_messages(system_msgs, supports_images)
                messages = system_msgs + self.format_messages(messages, supports_images)
            else:
                messages = self.format_messages(messages, supports_images)

            # 计算输入 token 数
            input_tokens = self.count_message_tokens(messages)

            # 检查是否超过 token 限制
            if not self.check_token_limit(input_tokens):
                error_message = self.get_limit_error_message(input_tokens)
                # 引发一个不会被重试的特殊异常
                raise TokenLimitExceeded(error_message)

            params = {
                "model": self.model,
                "messages": messages,
            }

            # Responses 协议分流：该协议没有 stream 之外的形态差异要处理，
            # 故不论调用方要不要 stream 都走非流式（本项目的单发判断点都不用流式；
            # agent 主循环走的是 ask_tool，不经过这里）。
            if self.use_response_api:
                if stream:
                    logger.info("Responses 协议下忽略 stream=True，按非流式返回")
                return await self._call_response_api(messages, temperature)

            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            if not stream:
                # 非流式请求。截断返空时就地抬额度重发一次（见 _RETRY_TOKEN_SCALE）
                response = await self.client.chat.completions.create(
                    **params, stream=False
                )
                if _is_truncated_empty(response):
                    bigger = int(self.max_tokens * _RETRY_TOKEN_SCALE)
                    logger.warning(
                        f"正文被推理链挤空（{_empty_response_detail(response, self.max_tokens)}）"
                        f"，就地把额度抬到 {bigger} 重发一次"
                    )
                    key = ("max_completion_tokens" if self.model in REASONING_MODELS
                           else "max_tokens")
                    response = await self.client.chat.completions.create(
                        **{**params, key: bigger}, stream=False
                    )
                    if _is_truncated_empty(response):
                        # 抬过一次仍被挤空：额度不是差一点，是提示词或模型选得不对。
                        # 抛专用类型让退避重试跳过它（重发只会再白烧一遍推理链）。
                        raise EmptyContentTruncated(
                            "抬高 max_tokens 后正文仍为空: "
                            + _empty_response_detail(response, bigger)
                        )

                if not response.choices or not response.choices[0].message.content:
                    # 正常收尾却返空：就地把额度翻倍重发一次再说
                    # （见 _EMPTY_STOP_TOKEN_SCALE）
                    bigger = int(self.max_tokens * _EMPTY_STOP_TOKEN_SCALE)
                    logger.warning(
                        f"正文为空但并非被截断（"
                        f"{_empty_response_detail(response, self.max_tokens)}）"
                        f"，把额度翻倍到 {bigger} 就地重发一次"
                    )
                    key = ("max_completion_tokens" if self.model in REASONING_MODELS
                           else "max_tokens")
                    response = await self.client.chat.completions.create(
                        **{**params, key: bigger}, stream=False
                    )
                    if not response.choices or not response.choices[0].message.content:
                        raise ValueError(
                            "Empty or invalid response from LLM: "
                            + _empty_response_detail(response, bigger)
                        )

                # 更新 token 计数
                self.update_token_count(
                    response.usage.prompt_tokens, response.usage.completion_tokens
                )

                return response.choices[0].message.content

            # 流式请求，对于流式传输，在发出请求之前更新估计的 token 计数
            self.update_token_count(input_tokens)

            response = await self.client.chat.completions.create(**params, stream=True)

            collected_messages = []
            completion_text = ""
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or ""
                collected_messages.append(chunk_message)
                completion_text += chunk_message
                print(chunk_message, end="", flush=True)

            print()  # 流式传输后的换行
            full_response = "".join(collected_messages).strip()
            if not full_response:
                raise ValueError("Empty response from streaming LLM")

            # 估计流式响应的完成 tokens
            completion_tokens = self.count_tokens(completion_text)
            logger.info(
                f"Estimated completion tokens for streaming response: {completion_tokens}"
            )
            self.total_completion_tokens += completion_tokens

            return full_response

        except TokenLimitExceeded:
            # 重新抛出 token 限制错误而不记录日志
            raise
        except EmptyContentTruncated as e:
            # 不打 exception 堆栈：这不是代码出错，是额度/提示词配得不对，
            # 一行说清即可（掉到下面的 except Exception 会报成 "Unexpected error"）
            logger.error(f"正文被推理链挤空且抬额度无效：{e}")
            raise
        except ValueError:
            logger.exception(f"Validation error")
            raise
        except OpenAIError as oe:
            logger.exception(f"OpenAI API error")
            if isinstance(oe, AuthenticationError):
                logger.error("Authentication failed. Check API key.")
            elif isinstance(oe, RateLimitError):
                logger.error("Rate limit exceeded. Consider increasing retry attempts.")
            elif isinstance(oe, APIError):
                logger.error(f"API error: {oe}")
            raise
        except Exception:
            logger.exception(f"Unexpected error in ask")
            raise

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        # 400 不重试（见 _worth_retry）：请求不合法，重发无意义
        retry=retry_if_exception(_worth_retry),
    )
    async def ask_with_images(
        self,
        messages: List[Union[dict, Message]],
        images: List[Union[str, dict]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        向 LLM 发送带有图像的提示并获取响应。

        Args:
            messages: 对话消息列表
            images: 图像 URL 或图像数据字典列表
            system_msgs: 可选的要前置的系统消息
            stream (bool): 是否流式传输响应
            temperature (float): 响应的采样温度

        Returns:
            str: 生成的响应

        Raises:
            TokenLimitExceeded: 如果超过 token 限制
            ValueError: 如果消息无效或响应为空
            OpenAIError: 如果 API 调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 对于 ask_with_images，我们总是将 supports_images 设置为 True，因为
            # 此方法应该只使用支持图像的模型调用
            if self.model not in MULTIMODAL_MODELS:
                raise ValueError(
                    f"Model {self.model} does not support images. Use a model from {MULTIMODAL_MODELS}"
                )

            # 使用图像支持格式化消息
            formatted_messages = self.format_messages(messages, supports_images=True)

            # 确保最后一条消息来自用户以附加图像
            if not formatted_messages or formatted_messages[-1]["role"] != "user":
                raise ValueError(
                    "The last message must be from the user to attach images"
                )

            # 处理最后一条用户消息以包含图像
            last_message = formatted_messages[-1]

            # 如果需要，将内容转换为多模态格式
            content = last_message["content"]
            multimodal_content = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else content
                if isinstance(content, list)
                else []
            )

            # 将图像添加到内容中
            for image in images:
                if isinstance(image, str):
                    multimodal_content.append(
                        {"type": "image_url", "image_url": {"url": image}}
                    )
                elif isinstance(image, dict) and "url" in image:
                    multimodal_content.append({"type": "image_url", "image_url": image})
                elif isinstance(image, dict) and "image_url" in image:
                    multimodal_content.append(image)
                else:
                    raise ValueError(f"Unsupported image format: {image}")

            # 使用多模态内容更新消息
            last_message["content"] = multimodal_content

            # 如果提供了系统消息，则添加它们
            if system_msgs:
                all_messages = (
                    self.format_messages(system_msgs, supports_images=True)
                    + formatted_messages
                )
            else:
                all_messages = formatted_messages

            # 计算 tokens 并检查限制
            input_tokens = self.count_message_tokens(all_messages)
            if not self.check_token_limit(input_tokens):
                raise TokenLimitExceeded(self.get_limit_error_message(input_tokens))

            # Responses 协议分流：_to_response_input 会把上面构造好的 chat 形态
            # （text/image_url）转成 input_text/input_image，故这里不必另写一套拼装。
            if self.use_response_api:
                if stream:
                    logger.info("Responses 协议下忽略 stream=True，按非流式返回")
                return await self._call_response_api(all_messages, temperature)

            # 设置 API 参数
            params = {
                "model": self.model,
                "messages": all_messages,
                "stream": stream,
            }

            # 添加模型特定参数
            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            # 处理非流式请求
            if not stream:
                # 截断返空时就地抬额度重发一次，与 ask 同一套判据与倍数
                # （见 _RETRY_TOKEN_SCALE）。【为什么带图这条路也必须有】看图判断的
                # 输出通常比纯文本短，原以为不会被推理链挤空，2026-08-26 实测
                # 阶段⑦ 分色选图（deepseek 视觉档，8 张图）照样返空——只是那次是
                # finish_reason=stop 走了退避重试。带图请求重发一次要重新上传整批
                # base64（阶段⑬ 单次 11 张图），走满 6 次退避的代价比纯文本更高，
                # 故能就地对症解决的先在这里解决。
                response = await self.client.chat.completions.create(**params)
                used_tokens = self.max_tokens
                if _is_truncated_empty(response):
                    bigger = int(self.max_tokens * _RETRY_TOKEN_SCALE)
                    logger.warning(
                        f"带图请求正文被推理链挤空（"
                        f"{_empty_response_detail(response, self.max_tokens)}）"
                        f"，就地把额度抬到 {bigger} 重发一次"
                    )
                    key = ("max_completion_tokens" if self.model in REASONING_MODELS
                           else "max_tokens")
                    response = await self.client.chat.completions.create(
                        **{**params, key: bigger}
                    )
                    used_tokens = bigger
                    if _is_truncated_empty(response):
                        # 抬过一次仍空是确定性失败，抛专用类型让退避重试跳过它
                        # （重发只会再白传一遍图、再烧一遍推理链）
                        raise EmptyContentTruncated(
                            "抬高 max_tokens 后带图请求正文仍为空: "
                            + _empty_response_detail(response, bigger)
                        )

                if not response.choices or not response.choices[0].message.content:
                    # 正常收尾却返空：同 ask 那条，就地把额度翻倍重发一次
                    # （2026-08-26 阶段⑦ 分色选图报的正是这个，见
                    # _EMPTY_STOP_TOKEN_SCALE 的实测记录）
                    bigger = int(used_tokens * _EMPTY_STOP_TOKEN_SCALE)
                    logger.warning(
                        f"带图请求正文为空但并非被截断（"
                        f"{_empty_response_detail(response, used_tokens)}）"
                        f"，把额度翻倍到 {bigger} 就地重发一次"
                    )
                    key = ("max_completion_tokens" if self.model in REASONING_MODELS
                           else "max_tokens")
                    response = await self.client.chat.completions.create(
                        **{**params, key: bigger}
                    )
                    used_tokens = bigger
                    if not response.choices or not response.choices[0].message.content:
                        raise ValueError(
                            "Empty or invalid response from LLM: "
                            + _empty_response_detail(response, bigger)
                        )

                # 【completion_tokens 必须一起记】原先只传 prompt_tokens，于是发布
                # 管线所有视觉阶段的日志都显示 Completion=0（2026-08-26 实测那批
                # 11 张描述图的质检调用无一例外），对耗时/花费账时会误判成「看图不
                # 花输出 token」，也让 reset_token_counters 清的那份累计量失真。
                self.update_token_count(
                    response.usage.prompt_tokens,
                    getattr(response.usage, "completion_tokens", 0) or 0,
                )
                return response.choices[0].message.content

            # 处理流式请求
            self.update_token_count(input_tokens)
            response = await self.client.chat.completions.create(**params)

            collected_messages = []
            async for chunk in response:
                chunk_message = chunk.choices[0].delta.content or ""
                collected_messages.append(chunk_message)
                print(chunk_message, end="", flush=True)

            print()  # 流式传输后的换行
            full_response = "".join(collected_messages).strip()

            if not full_response:
                raise ValueError("Empty response from streaming LLM")

            return full_response

        except TokenLimitExceeded:
            raise
        except ValueError as ve:
            logger.error(f"Validation error in ask_with_images: {ve}")
            raise
        except OpenAIError as oe:
            logger.error(f"OpenAI API error: {oe}")
            if isinstance(oe, AuthenticationError):
                logger.error("Authentication failed. Check API key.")
            elif isinstance(oe, RateLimitError):
                logger.error("Rate limit exceeded. Consider increasing retry attempts.")
            elif isinstance(oe, APIError):
                logger.error(f"API error: {oe}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in ask_with_images: {e}")
            raise

    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # Don't retry TokenLimitExceeded
    )
    async def ask_tool(
        self,
        messages: List[Union[dict, Message]],
        system_msgs: Optional[List[Union[dict, Message]]] = None,
        timeout: int = 300,
        tools: Optional[List[dict]] = None,
        tool_choice: TOOL_CHOICE_TYPE = ToolChoice.AUTO,  # type: ignore
        temperature: Optional[float] = None,
        **kwargs,
    ) -> ChatCompletionMessage | None:
        """
        使用函数/工具请求 LLM 并返回响应。

        Args:
            messages: 对话消息列表
            system_msgs: 可选的要前置的系统消息
            timeout: 请求超时时间（秒）
            tools: 要使用的工具列表
            tool_choice: 工具选择策略
            temperature: 响应的采样温度
            **kwargs: 额外的完成参数

        Returns:
            ChatCompletionMessage: 模型的响应

        Raises:
            TokenLimitExceeded: 如果超过 token 限制
            ValueError: 如果工具、tool_choice 或消息无效
            OpenAIError: 如果 API 调用在重试后失败
            Exception: 对于意外错误
        """
        try:
            # 验证 tool_choice
            if tool_choice not in TOOL_CHOICE_VALUES:
                raise ValueError(f"Invalid tool_choice: {tool_choice}")

            # 思考模式模型不支持 tool_choice="required" 或指定具体 function
            if self.model in THINKING_MODELS and tool_choice != ToolChoice.AUTO and tool_choice != ToolChoice.NONE:
                logger.warning(
                    f"🧠 Thinking model '{self.model}' does not support tool_choice='{tool_choice}', "
                    f"falling back to 'auto'"
                )
                tool_choice = ToolChoice.AUTO

            # 检查模型是否支持图像
            supports_images = self.model in MULTIMODAL_MODELS

            # 调试信息：检查是否有图像输入
            has_images = any(
                isinstance(msg, dict) and msg.get("base64_image")
                or isinstance(msg, Message) and msg.base64_image
                for msg in (system_msgs or []) + messages
            )

            if supports_images:
                logger.info(f"👁️ Vision model enabled for tool calling: {self.model}")
                if has_images:
                    logger.info(f"📷 Image detected in tool call messages - will be sent to vision model")
            else:
                # 只有在有图片但模型不支持时，才输出警告
                # 如果没有图片，就不需要警告（模型不支持图片但不影响正常使用）
                if has_images:
                    logger.warning(f"⚠️ Model {self.model} does NOT support images for tool calling")
                    logger.warning(f"⚠️ Images detected but will be ignored (model doesn't support vision)")

            # 格式化消息
            if system_msgs:
                system_msgs = self.format_messages(system_msgs, supports_images)
                messages = system_msgs + self.format_messages(messages, supports_images)
            else:
                messages = self.format_messages(messages, supports_images)

            # 计算输入 token 数
            input_tokens = self.count_message_tokens(messages)

            # 如果有工具，计算工具描述的 token 数
            tools_tokens = 0
            if tools:
                for tool in tools:
                    tools_tokens += self.count_tokens(str(tool))

            input_tokens += tools_tokens

            # 检查是否超过 token 限制
            if not self.check_token_limit(input_tokens):
                error_message = self.get_limit_error_message(input_tokens)
                # 引发一个不会被重试的特殊异常
                raise TokenLimitExceeded(error_message)

            # 如果提供了工具，则验证它们
            if tools:
                for tool in tools:
                    if not isinstance(tool, dict) or "type" not in tool:
                        raise ValueError("Each tool must be a dict with 'type' field")

            # 设置完成请求
            params = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "timeout": timeout,
                **kwargs,
            }

            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            params["stream"] = False  # 对于工具请求，始终使用非流式传输
            response: ChatCompletion = await self.client.chat.completions.create(
                **params
            )

            # 检查响应是否有效
            if not response.choices or not response.choices[0].message:
                print(response)
                # raise ValueError("Invalid or empty response from LLM")
                return None

            # 更新 token 计数
            self.update_token_count(
                response.usage.prompt_tokens, response.usage.completion_tokens
            )

            return response.choices[0].message

        except TokenLimitExceeded:
            # Re-raise token limit errors without logging
            raise
        except ValueError as ve:
            logger.error(f"Validation error in ask_tool: {ve}")
            raise
        except OpenAIError as oe:
            logger.error(f"OpenAI API error: {oe}")
            if isinstance(oe, AuthenticationError):
                logger.error("Authentication failed. Check API key.")
            elif isinstance(oe, RateLimitError):
                logger.error("Rate limit exceeded. Consider increasing retry attempts.")
            elif isinstance(oe, APIError):
                error_msg = str(oe)
                logger.error(f"API error: {error_msg}")
                # 如果是 404 错误，提供更详细的诊断信息
                if "404" in error_msg or "not found" in error_msg.lower():
                    logger.error(f"Model: {self.model}, Base URL: {self.base_url}")
                    logger.error("Possible issues:")
                    logger.error("1. Model name might be incorrect")
                    logger.error("2. API endpoint might be wrong")
                    logger.error("3. Model might not support tools/function calling")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in ask_tool: {e}")
            raise
