"""OpenAI Responses 协议（api_type = "openai-response"）的离线单测。

为什么值得单测：这条协议与 chat completions 有两处形态差异，写错都【不报错】
而是静默失效——图片被丢掉、或者拿到 reasoning 的自言自语当正文。这类错误在真站
表现为「模型答得莫名其妙」，排查时极容易怀疑提示词。故把转换规则固化下来。

不覆盖：真实网关行为（要 key 和网络）。协议可用性已在 2026-08-20 真站验证。
"""
import pytest

from app.llm import LLM


def _stub(api_type: str = "openai-response") -> LLM:
    """造一个只带 api_type 的 LLM 实例，用于测纯转换逻辑。

    走 object.__new__ 绕开 LLM.__new__ 的单例登记与 __init__ 的网络客户端构造：
    这几个方法只依赖 self.api_type，不需要真实连接；且不污染 LLM._instances
    （污染了会让同进程里后续真实取 LLM 时拿到这个残废实例）。
    """
    obj = object.__new__(LLM)
    obj.api_type = api_type
    return obj


def test_system_转成顶层instructions():
    """system 角色不能进 input，必须单独作为 instructions 传。

    放进 input 里网关不报错，但会被当成普通用户消息、指令效力下降。
    """
    s = _stub()
    instructions, items = s._to_response_input([
        {"role": "system", "content": "你是审核助手"},
        {"role": "user", "content": "看图"},
    ])
    assert instructions == "你是审核助手"
    assert len(items) == 1 and items[0]["role"] == "user"
    # system 不该出现在 input 里
    assert all(i["role"] != "system" for i in items)


def test_多条system换行拼接():
    s = _stub()
    instructions, _ = s._to_response_input([
        {"role": "system", "content": "规则一"},
        {"role": "system", "content": "规则二"},
        {"role": "user", "content": "问题"},
    ])
    assert "规则一" in instructions and "规则二" in instructions


def test_图片转成input_image且url是字符串():
    """chat 的 image_url 是 {"url": ...} 对象，responses 的是【字符串】。

    传成对象不报错，图会被静默丢掉——这是本协议最容易踩的一处。
    """
    s = _stub()
    _, items = s._to_response_input([{
        "role": "user",
        "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        ],
    }])
    parts = items[0]["content"]
    assert parts[0] == {"type": "input_text", "text": "这是什么"}
    img = parts[1]
    assert img["type"] == "input_image"
    assert img["image_url"] == "data:image/jpeg;base64,AAA", "image_url 必须是字符串"


def test_纯字符串content也转成input_text():
    s = _stub()
    _, items = s._to_response_input([{"role": "user", "content": "纯文本"}])
    assert items[0]["content"] == [{"type": "input_text", "text": "纯文本"}]


def test_正文只取message项跳过reasoning():
    """output 混着 reasoning 和 message，直接取 output[0] 会拿到思考摘要。"""
    resp = {"output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "我在想…"}]},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": '{"ok":true}'}]},
    ]}
    assert LLM._from_response_output(resp) == '{"ok":true}'
    assert "我在想" not in LLM._from_response_output(resp)


def test_多段output_text拼接():
    resp = {"output": [
        {"type": "message", "content": [
            {"type": "output_text", "text": "前半"},
            {"type": "output_text", "text": "后半"},
        ]},
    ]}
    assert LLM._from_response_output(resp) == "前半后半"


def test_只有reasoning时返回空串():
    """正文为空的典型成因是 max_output_tokens 被 reasoning 吃光。

    这里只断言返回空串；调用方 _call_response_api 负责把它转成带
    reasoning_tokens 的报错（否则会被误当成提示词问题）。
    """
    resp = {"output": [{"type": "reasoning", "summary": [{"text": "想了很久"}]}]}
    assert LLM._from_response_output(resp) == ""


@pytest.mark.parametrize("api_type,expected", [
    ("openai-response", True),
    ("openai_response", True),
    ("responses", True),
    ("OPENAI-RESPONSE", True),
    ("openai", False),
    ("azure", False),
    ("", False),
])
def test_协议开关识别(api_type, expected):
    assert _stub(api_type).use_response_api is expected


# ---- 空正文的就地抬额度重发 -------------------------------------------------------
# 【为什么这条协议也必须有】chat/completions 那两条路都做了就地抬额度重发
# （_RETRY_TOKEN_SCALE / _EMPTY_STOP_TOKEN_SCALE），而这里原先只抛 ValueError 交退避。
# 偏偏本协议是 grok 网关专用，而 grok 是发布管线的默认档（publish.llm._DEFAULT_CHOICE），
# 等于默认配置反而没有这层保护——审计时才发现这个口子。

def _api_stub(texts, max_tokens=1000):
    """造一个能跑 _call_response_api 的 LLM，responses.create 按序返回预设正文。

    texts 里每项是该次返回的正文（"" 表示空正文），记录每次请求的
    max_output_tokens 以便断言确实翻倍了。
    """
    obj = object.__new__(LLM)
    obj.api_type = "openai-response"
    obj.model = "grok-4.6"
    obj.max_tokens = max_tokens
    obj.temperature = None
    obj.total_input_tokens = 0
    obj.total_completion_tokens = 0
    obj.update_token_count = lambda *a, **k: None
    sent = []

    class _Resp:
        def __init__(self, text):
            self._text = text

        def model_dump(self):
            out = {"usage": {"input_tokens": 10, "output_tokens": 5,
                             "output_tokens_details": {"reasoning_tokens": 5}}}
            if self._text:
                out["output"] = [{"type": "message",
                                  "content": [{"type": "output_text",
                                               "text": self._text}]}]
            else:
                # 空正文的真实形状：只有 reasoning 项，没有 message
                out["output"] = [{"type": "reasoning", "summary": []}]
            return out

    class _Responses:
        async def create(self, **kw):
            sent.append(kw)
            return _Resp(texts[min(len(sent) - 1, len(texts) - 1)])

    class _Client:
        responses = _Responses()

    obj.client = _Client()
    return obj, sent


@pytest.mark.asyncio
async def test_responses空正文翻倍重发一次():
    llm, sent = _api_stub(["", '{"ok":true}'], max_tokens=1000)
    out = await llm._call_response_api([{"role": "user", "content": "问"}])
    assert out == '{"ok":true}'
    assert len(sent) == 2                            # 只重发一次，不是走退避
    assert sent[0]["max_output_tokens"] == 1000
    assert sent[1]["max_output_tokens"] == 2000      # _EMPTY_STOP_TOKEN_SCALE = 2.0


@pytest.mark.asyncio
async def test_responses翻倍后仍空才抛():
    """抬过仍空才是提示词/模型问题，报错要带上抬到了多少，免得又去怀疑额度。"""
    llm, sent = _api_stub(["", ""], max_tokens=1000)
    with pytest.raises(ValueError, match="抬高额度后仍返回空正文"):
        await llm._call_response_api([{"role": "user", "content": "问"}])
    assert len(sent) == 2
    assert "max_output_tokens=2000" in str(sent) or sent[1]["max_output_tokens"] == 2000


@pytest.mark.asyncio
async def test_responses有正文时不多发一次():
    """常路不该因为新增分支多打一次请求（每次都要重传整批图）。"""
    llm, sent = _api_stub(["正常答案"], max_tokens=1000)
    assert await llm._call_response_api([{"role": "user", "content": "问"}]) == "正常答案"
    assert len(sent) == 1
