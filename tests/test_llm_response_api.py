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
