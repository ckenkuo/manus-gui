import math
from typing import Dict, List, Optional, Union

import tiktoken
from openai import (
    APIError,
    AsyncAzureOpenAI,
    AsyncOpenAI,
    AuthenticationError,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.bedrock import BedrockClient
from app.config import LLMSettings, config
from app.exceptions import TokenLimitExceeded
from app.logger import logger  # Assuming a logger is set up in your app
from app.schema import (
    ROLE_VALUES,
    TOOL_CHOICE_TYPE,
    TOOL_CHOICE_VALUES,
    Message,
    ToolChoice,
)


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
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # Don't retry TokenLimitExceeded
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
                logger.warning(f"⚠️ Model {self.model} does NOT support images - visual understanding disabled")
                if has_images:
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

            if self.model in REASONING_MODELS:
                params["max_completion_tokens"] = self.max_tokens
            else:
                params["max_tokens"] = self.max_tokens
                params["temperature"] = (
                    temperature if temperature is not None else self.temperature
                )

            if not stream:
                # 非流式请求
                response = await self.client.chat.completions.create(
                    **params, stream=False
                )

                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("Empty or invalid response from LLM")

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
        retry=retry_if_exception_type(
            (OpenAIError, Exception, ValueError)
        ),  # Don't retry TokenLimitExceeded
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
                response = await self.client.chat.completions.create(**params)

                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("Empty or invalid response from LLM")

                self.update_token_count(response.usage.prompt_tokens)
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
