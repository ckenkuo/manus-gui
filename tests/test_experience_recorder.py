"""recorder 单测：紧凑转录构建 + distill JSON 解析（不联网）。"""

from app.experience import recorder


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, name, arguments):
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, role, content=None, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


class _Mem:
    def __init__(self, messages):
        self.messages = messages


class _Agent:
    def __init__(self, messages):
        self.memory = _Mem(messages)


def test_build_transcript_extracts_task_and_tools():
    agent = _Agent([
        _Msg("user", "查携程上海到北京的机票"),
        _Msg("assistant", "我先打开携程首页", tool_calls=[_TC("browser_use", '{"action":"go_to_url"}')]),
        _Msg("tool", "页面已打开"),
        _Msg("user", "下一步提示样板（应被跳过）"),
        _Msg("assistant", "读取票价", tool_calls=[_TC("browser_use", '{"action":"extract"}')]),
        _Msg("tool", "最低 ¥780"),
    ])
    task, transcript, tools = build = recorder.build_transcript(agent)

    assert task == "查携程上海到北京的机票"
    assert tools == ["browser_use"]  # 去重
    assert "[任务]" in transcript
    assert "[步骤1]" in transcript and "[步骤2]" in transcript
    assert "下一步提示样板" not in transcript  # 后续 user 样板被降噪


def test_parse_distill_json_strips_fence():
    raw = '```json\n{"steps": ["1. a"], "result_summary": "ok", "tips": []}\n```'
    data = recorder._parse_distill_json(raw)
    assert data["steps"] == ["1. a"]
    assert data["result_summary"] == "ok"


def test_parse_distill_json_plain():
    data = recorder._parse_distill_json('{"steps": ["x"]}')
    assert data["steps"] == ["x"]


def test_parse_distill_json_invalid_returns_none():
    assert recorder._parse_distill_json("not json at all") is None
    assert recorder._parse_distill_json("") is None
