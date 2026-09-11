"""推理链吃光 max_tokens 导致正文返空时的处置：离线单测。

为什么值得单测：这是 2026-08-24 耗时排查里最贵的一个失败模式，而它的三种情形处置
完全相反，混在一起会白等好几分钟：
  - finish_reason="length" 且正文空 → 额度不够，抬高 max_tokens 重发【有救】
  - 抬过一次仍空 → 提示词/模型选得不对，抛专用异常且【不再退避重试】
  - finish_reason="stop" 且正文空 → 模型真没话说，走原有 ValueError 路径【可重试】

实测账（logs/20260822231128.log）：原先第 2 种情形会走满 6 次指数退避，单次
白等 2 分 17 秒，日志里只留一句 "Validation error" 看不出根因。

不覆盖：真实端点行为（要 key 和网络）。这里只钉判据与重试语义。
"""
import pytest

from app.exceptions import EmptyContentTruncated, TokenLimitExceeded
from app.llm import LLM, _is_truncated_empty, _worth_retry_text


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Usage:
    prompt_tokens = 100
    completion_tokens = 200


class _Resp:
    """够 _is_truncated_empty / ask 用的最小响应形状。"""

    def __init__(self, content, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = _Usage()


# ---- 判据 ---------------------------------------------------------------------

def test_判据_length加空正文才算截断():
    assert _is_truncated_empty(_Resp("", "length")) is True
    assert _is_truncated_empty(_Resp("   ", "length")) is True   # 只有空白也算空
    # 有正文：即使被截断也不算「返空」，调用方拿到的是可用内容
    assert _is_truncated_empty(_Resp('{"a":1}', "length")) is False
    # 正常收尾但正文空：模型真没话说，不是额度问题，处置不同
    assert _is_truncated_empty(_Resp("", "stop")) is False
    assert _is_truncated_empty(_Resp("", None)) is False


def test_判据_没有choices不算截断():
    r = _Resp("", "length")
    r.choices = []
    assert _is_truncated_empty(r) is False


# ---- 重试谓词 -----------------------------------------------------------------

def test_谓词_确定性失败不退避重试():
    from openai import BadRequestError

    # 三类确定性失败：重发只会原样再失败，却各要等掉一轮指数退避
    assert _worth_retry_text(EmptyContentTruncated("抬了也没用")) is False
    assert _worth_retry_text(TokenLimitExceeded("输入超限")) is False
    err = BadRequestError.__new__(BadRequestError)   # 绕开需要 response 的构造
    assert _worth_retry_text(err) is False
    # 其余（限流/超时/网关抖动/单次返空）照旧重试
    assert _worth_retry_text(ValueError("Empty response")) is True
    assert _worth_retry_text(RuntimeError("网关 502")) is True


def test_谓词_带图那条也要排除抬额度后仍返空():
    """_worth_retry 原先只排 400，于是带图路径新增的 EmptyContentTruncated 会被
    退避重试 6 次——每次都要把整批 base64 再传一遍，正是这个异常要避免的浪费。"""
    from openai import BadRequestError

    from app.llm import _worth_retry

    assert _worth_retry(EmptyContentTruncated("抬了也没用")) is False
    err = BadRequestError.__new__(BadRequestError)
    assert _worth_retry(err) is False
    # 限流/超时/网关抖动/单次返空照旧重试（那次阶段⑦ 返空就是靠重试救回来的）
    assert _worth_retry(ValueError("Empty or invalid response")) is True


# ---- ask 的就地抬额度重发 -------------------------------------------------------

def _ask_stub(responses, max_tokens=1000, model="deepseek-flash"):
    """造一个只跑 ask 非流式分支的 LLM，client 换成按序返回预设响应的假货。

    走 object.__new__ 绕开单例登记与真实网络客户端构造（同 test_llm_response_api
    的 _stub 理由），并记录每次请求的 max_tokens 以便断言确实抬高了。
    """
    obj = object.__new__(LLM)
    obj.model = model
    obj.max_tokens = max_tokens
    obj.temperature = 0.0
    obj.api_type = "openai"
    obj.max_input_tokens = None
    obj.total_input_tokens = 0
    obj.total_completion_tokens = 0
    obj.tokenizer = None
    sent = []

    class _Completions:
        async def create(self, **kw):
            sent.append(kw)
            # 预设用尽后重复最后一个：可重试的异常（如正文空的 ValueError）会让
            # 装饰器继续退避重发，桩不该因此抛 IndexError 掩盖真正要断言的行为
            return responses[min(len(sent) - 1, len(responses) - 1)]

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    obj.client = _Client()
    # count_message_tokens / update_token_count 依赖 tokenizer，这里都替成常量
    obj.count_message_tokens = lambda msgs: 10
    obj.count_tokens = lambda text: 10
    obj.update_token_count = lambda *a, **k: None
    obj.check_token_limit = lambda n: True
    # 形参名必须是 supports_images：ask positionally 传，ask_with_images 按关键字传
    obj.format_messages = staticmethod(
        lambda msgs, supports_images=False: [dict(m) for m in msgs])
    return obj, sent


@pytest.mark.asyncio
async def test_ask_截断返空就地抬额度重发成功():
    """第一发被挤空、抬到 1.5 倍后拿到正文——调用方不该看到任何异常。"""
    llm, sent = _ask_stub([_Resp("", "length"), _Resp('{"ok":true}', "stop")],
                          max_tokens=1000)
    out = await llm.ask([{"role": "user", "content": "问"}], stream=False)
    assert out == '{"ok":true}'
    assert len(sent) == 2                       # 只重发一次，不是走 6 次退避
    assert sent[0]["max_tokens"] == 1000
    assert sent[1]["max_tokens"] == 1500        # _RETRY_TOKEN_SCALE = 1.5


@pytest.mark.asyncio
async def test_ask_抬额度后仍空抛专用异常():
    """抬过一次还空：抛 EmptyContentTruncated，且不再重发第三次。"""
    llm, sent = _ask_stub([_Resp("", "length"), _Resp("", "length")], max_tokens=1000)
    with pytest.raises(EmptyContentTruncated, match="仍为空"):
        # 装饰器的 retry 谓词会放行这个异常（_worth_retry_text 已单测），
        # 故这里拿到的是原始异常而不是 RetryError
        await llm.ask([{"role": "user", "content": "问"}], stream=False)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_ask_推理模型抬的是max_completion_tokens():
    """REASONING_MODELS 走 max_completion_tokens，重发时不能抬错字段名。

    抬错了等于没抬：请求里仍是原额度，第二发照样被挤空。
    """
    from app.llm import REASONING_MODELS

    model = REASONING_MODELS[0]
    llm, sent = _ask_stub([_Resp("", "length"), _Resp("答", "stop")],
                          max_tokens=1000, model=model)
    out = await llm.ask([{"role": "user", "content": "问"}], stream=False)
    assert out == "答"
    assert sent[0]["max_completion_tokens"] == 1000
    assert sent[1]["max_completion_tokens"] == 1500
    assert "max_tokens" not in sent[1]


@pytest.mark.asyncio
async def test_ask_正常收尾的空正文翻倍重发一次():
    """finish_reason="stop" 且正文空是另一码事：不是额度被推理链吃光，但仍值得
    就地翻倍重发一次（多半是单次抖动，省掉一轮退避），见 _EMPTY_STOP_TOKEN_SCALE。

    与截断那条的处置刻意不同：截断抛 EmptyContentTruncated 直接放弃（额度确实是
    瓶颈），这条重发拿到正文就正常返回。
    """
    llm, sent = _ask_stub([_Resp("", "stop"), _Resp("答", "stop")], max_tokens=1000)
    out = await llm.ask([{"role": "user", "content": "问"}], stream=False)
    assert out == "答"
    assert len(sent) == 2
    assert sent[0]["max_tokens"] == 1000
    assert sent[1]["max_tokens"] == 2000        # _EMPTY_STOP_TOKEN_SCALE = 2.0


@pytest.mark.asyncio
async def test_ask_翻倍后仍空才抛可重试的ValueError():
    """翻倍也没救回来：这时才可能是提示词问题，交退避重试（ValueError 可重试）。

    绕开装饰器直接调 __wrapped__：带装饰器会因 ValueError 可重试而走 6 次指数退避
    （真等十几秒），把断言点淹掉。退避语义本身由 _worth_retry_text 那条覆盖。
    """
    llm, sent = _ask_stub([_Resp("", "stop"), _Resp("", "stop")], max_tokens=1000)
    with pytest.raises(ValueError, match="Empty or invalid response"):
        await LLM.ask.__wrapped__(llm, [{"role": "user", "content": "问"}], stream=False)
    assert len(sent) == 2                       # 就地重发过一次，不是一次就放弃


@pytest.mark.asyncio
async def test_ask_有正文时不多发一次():
    """一发就拿到正文的常路：不该因为新增分支多打一次请求。"""
    llm, sent = _ask_stub([_Resp("正常答案", "stop")], max_tokens=1000)
    assert await llm.ask([{"role": "user", "content": "问"}], stream=False) == "正常答案"
    assert len(sent) == 1


# ---- ask_with_images 的就地抬额度重发 --------------------------------------------
# 【为什么带图这条路也要有】原先只有纯文本 ask 做了就地抬额度重发，带图那条只会抛
# ValueError 然后走满 6 次退避——而带图重发要重新上传整批 base64（阶段⑬ 单次 11 张），
# 代价比纯文本更高。2026-08-26 实测阶段⑦ 分色选图（8 张图）返空一次，日志里只留一句
# "Validation error in ask_with_images"。

def _vision_stub(responses, max_tokens=1000):
    """同 _ask_stub，但模型换成多模态白名单里的（否则 ask_with_images 直接拒）。"""
    from app.llm import MULTIMODAL_MODELS

    llm, sent = _ask_stub(responses, max_tokens=max_tokens,
                          model=MULTIMODAL_MODELS[0])
    return llm, sent


@pytest.mark.asyncio
async def test_带图请求截断返空也就地抬额度重发():
    llm, sent = _vision_stub([_Resp("", "length"), _Resp('{"ok":true}', "stop")],
                             max_tokens=1000)
    out = await llm.ask_with_images(
        messages=[{"role": "user", "content": "看图"}],
        images=["data:image/jpeg;base64,AAAA"], stream=False)
    assert out == '{"ok":true}'
    assert len(sent) == 2                       # 只重发一次，不是走 6 次退避
    assert sent[0]["max_tokens"] == 1000
    assert sent[1]["max_tokens"] == 1500        # _RETRY_TOKEN_SCALE = 1.5


@pytest.mark.asyncio
async def test_带图请求抬额度后仍空抛专用异常():
    """抬过一次还空：不该再白传一遍图、白烧一遍推理链。"""
    llm, sent = _vision_stub([_Resp("", "length"), _Resp("", "length")],
                             max_tokens=1000)
    with pytest.raises(EmptyContentTruncated, match="仍为空"):
        await llm.ask_with_images(
            messages=[{"role": "user", "content": "看图"}],
            images=["data:image/jpeg;base64,AAAA"], stream=False)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_带图请求正常收尾返空也翻倍重发一次():
    """2026-08-26 阶段⑦ 分色选图报的正是这个：finish_reason=stop、
    completion_tokens=101、max_tokens=32000——额度根本没用完，靠退避重试白等 18 秒。
    """
    llm, sent = _vision_stub([_Resp("", "stop"), _Resp('{"rows":[]}', "stop")],
                             max_tokens=1000)
    out = await llm.ask_with_images(
        messages=[{"role": "user", "content": "看图"}],
        images=["data:image/jpeg;base64,AAAA"], stream=False)
    assert out == '{"rows":[]}'
    assert len(sent) == 2
    assert sent[1]["max_tokens"] == 2000        # _EMPTY_STOP_TOKEN_SCALE = 2.0


@pytest.mark.asyncio
async def test_带图请求要记completion_tokens():
    """原先只传 prompt_tokens，于是所有视觉阶段的日志都显示 Completion=0
    （2026-08-26 实测那批 11 张描述图的质检调用无一例外），对花费账时会误判。"""
    llm, sent = _vision_stub([_Resp("答", "stop")], max_tokens=1000)
    seen = []
    llm.update_token_count = lambda inp, comp=0: seen.append((inp, comp))
    await llm.ask_with_images(
        messages=[{"role": "user", "content": "看图"}],
        images=["data:image/jpeg;base64,AAAA"], stream=False)
    assert seen == [(100, 200)]                 # _Usage 的 prompt/completion
