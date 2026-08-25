"""描述区文字模块处理的单测（阶段⑬ 新增能力）。

用户 2026-08-24 要求：描述长图里的文字板块也要处理——LLM 翻译，无意义则删除。
两类真实样本都进了测试：
- 该删：1688 关联商品 JSON 残留（真站探查 rowid 173539495454339053 的 data-idx=0）
- 该译：尺码对照表 `80【身高65-75cm】`（用户截图）

plan_desc_text 走 LLM，故 mock 掉 ask_json 只验证解析与兜底；
desc_text_apply 走浏览器，mock 掉 session 只验证顺序与失败隔离。
"""
import pytest

from app.publish import vision
from app.publish.pipeline import desc_text_apply


# ---- plan_desc_text：LLM 输出的解析与兜底 ---------------------------------

TEXTS = [
    {"idx": "0", "text": '{"styleType":"offer-type-1","items":"888688384773"}', "len": 50},
    {"idx": "3", "text": "80【身高65-75cm】\n90【身高75-85cm】", "len": 24},
]


@pytest.mark.asyncio
async def test_plan_desc_text_parses_actions(monkeypatch):
    async def fake_ask_json(prompt, **kw):
        return {"plan": [
            {"idx": "0", "action": "delete", "reason": "采集残留JSON"},
            {"idx": "3", "action": "translate",
             "text": "80 (Height 65-75cm)\n90 (Height 75-85cm)", "reason": "尺码对照"},
        ]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    r = await vision.plan_desc_text(TEXTS, {"title": "女童毛衣"})
    assert r["status"] == "ok"
    by = {p["idx"]: p for p in r["plan"]}
    assert by["0"]["action"] == "delete"
    assert by["3"]["action"] == "translate"
    assert "Height" in by["3"]["text"]


@pytest.mark.asyncio
async def test_plan_desc_text_missing_judgement_keeps(monkeypatch):
    """LLM 漏判的模块必须按 keep 处理——保守方向，不删不该删的。"""
    async def fake_ask_json(prompt, **kw):
        return {"plan": [{"idx": "0", "action": "delete", "reason": "垃圾"}]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    r = await vision.plan_desc_text(TEXTS, None)
    by = {p["idx"]: p for p in r["plan"]}
    assert by["3"]["action"] == "keep"


@pytest.mark.asyncio
async def test_plan_desc_text_translate_without_text_becomes_keep(monkeypatch):
    """说要译却没给正文时按 keep，不能把模块清空。"""
    async def fake_ask_json(prompt, **kw):
        return {"plan": [{"idx": "3", "action": "translate", "text": ""}]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    r = await vision.plan_desc_text([TEXTS[1]], None)
    assert r["plan"][0]["action"] == "keep"


@pytest.mark.asyncio
async def test_plan_desc_text_ignores_unknown_idx(monkeypatch):
    """LLM 编出不存在的 idx 要丢掉，否则会去点不存在的模块。"""
    async def fake_ask_json(prompt, **kw):
        return {"plan": [
            {"idx": "99", "action": "delete", "reason": "编的"},
            {"idx": "0", "action": "delete", "reason": "真的"},
        ]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    r = await vision.plan_desc_text([TEXTS[0]], None)
    assert [p["idx"] for p in r["plan"]] == ["0"]


@pytest.mark.asyncio
async def test_plan_desc_text_empty_input():
    r = await vision.plan_desc_text([], None)
    assert r["plan"] == []


@pytest.mark.asyncio
async def test_plan_desc_text_prompt_carries_原文(monkeypatch):
    """提示词里必须真的带上每个模块的 idx 与原文。

    2026-08-25 真站事故：拼好的 listing 变量忘了插进 prompt f-string，模型只收到
    「下面是 1 个文字模块原文」却看不到任何原文，回了 {"plan": [], "error": "未收到
    需要处理的模块原文"}；解析层按「漏判一律 keep」处理，于是描述区的
    `null null null` 被原样发布，全程零告警。原有测试全部 mock 掉 ask_json 且不看
    prompt，所以一条都没抓到——故这里直接断言 prompt 内容。
    """
    seen = {}

    async def fake_ask_json(prompt, **kw):
        seen["prompt"] = prompt
        return {"plan": [{"idx": "0", "action": "delete", "reason": "垃圾"},
                         {"idx": "3", "action": "keep"}]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    await vision.plan_desc_text(TEXTS, {"title": "女童毛衣"})
    p = seen["prompt"]
    assert "idx=0" in p and "idx=3" in p
    assert "offer-type-1" in p                 # 该删样本的原文
    assert "80【身高65-75cm】" in p             # 该译样本的原文


@pytest.mark.asyncio
async def test_plan_desc_text_all_unjudged_warns(monkeypatch):
    """一项都没判中时必须告警：全员 keep 与「模型认为都该留」外观相同，
    静默下去就发现不了提示词/解析层的故障（同上那次事故的第二道防线）。

    项目用 loguru，pytest 的 caplog 抓不到，故直接替掉 logger.warning。
    """
    warns = []

    async def fake_ask_json(prompt, **kw):
        return {"plan": [], "error": "未收到需要处理的模块原文"}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    monkeypatch.setattr(vision.logger, "warning", lambda msg, *a, **k: warns.append(str(msg)))
    r = await vision.plan_desc_text(TEXTS, None)
    assert all(p["action"] == "keep" for p in r["plan"])
    assert any("一项都没判中" in w for w in warns)


@pytest.mark.asyncio
async def test_plan_desc_text_null_placeholder_deletable(monkeypatch):
    """`null null null` 这类采集占位符要能被判删（提示词里已列为 delete 情形）。"""
    async def fake_ask_json(prompt, **kw):
        assert "null" in prompt                # 原文进了提示词
        return {"plan": [{"idx": "7", "action": "delete", "reason": "空占位符"}]}

    monkeypatch.setattr(vision, "ask_json", fake_ask_json)
    r = await vision.plan_desc_text(
        [{"idx": "7", "text": "null\nnull\nnull", "len": 14}], None)
    assert r["plan"][0]["action"] == "delete"


# ---- desc_text_apply：执行顺序与失败隔离 ---------------------------------

class FakeSession:
    """记录 eval_json 收到的 JS，按内容返回成功结果。"""

    def __init__(self, fail_idx=None):
        self.calls = []
        self.fail_idx = fail_idx

    async def eval_json(self, js):
        self.calls.append(js)
        # 从 JS 里回捞 data-idx，判断是改写还是删除
        import re
        m = re.search(r'using-item\[data-idx="(\d+)"\]', js)
        idx = m.group(1) if m else "?"
        is_set = "textarea" in js
        if idx == self.fail_idx:
            return {"status": "error", "reason": "textarea-not-found"} if is_set \
                else {"err": "模块上没有删除图标"}
        if is_set:
            # 回读要与写入值一致才算 filled
            t = re.search(r'const text = "(.*?)";', js, re.S)
            return {"status": "ok", "filled": True,
                    "readback": (t.group(1) if t else "")[:80]}
        return {"deleted": True, "targetId": idx}


@pytest.fixture(autouse=True)
def _stub_ensure_open(monkeypatch):
    """跳过真实的编辑器打开（那要连浏览器）。"""
    async def fake_open(session):
        return {"open": True, "count": 2}

    monkeypatch.setattr("app.publish.pipeline._desc_ensure_open", fake_open)


@pytest.mark.asyncio
async def test_apply_translate_then_delete_order():
    """必须先改写、再删除：删除会让 data-idx 重排，先删会把译文写到别的模块上。"""
    session = FakeSession()
    plan = [
        {"idx": "0", "action": "delete", "reason": "垃圾"},
        {"idx": "3", "action": "translate", "text": "Size chart", "reason": "尺码"},
    ]
    r = await desc_text_apply(session, plan)
    assert r["status"] == "ok"
    assert [t["idx"] for t in r["translated"]] == ["3"]
    assert r["deleted"] == ["0"]
    # 第一个 JS 必须是改写（含 textarea），删除在后
    assert "textarea" in session.calls[0]
    assert "textarea" not in session.calls[1]


@pytest.mark.asyncio
async def test_apply_deletes_in_reverse_order():
    """多个删除按 data-idx 从大到小——正序删会因重排而错位。"""
    session = FakeSession()
    plan = [{"idx": i, "action": "delete"} for i in ("0", "2", "5")]
    r = await desc_text_apply(session, plan)
    assert r["deleted"] == ["5", "2", "0"]


@pytest.mark.asyncio
async def test_apply_failure_isolated():
    """单项失败记进 failed 并继续，不拖垮整体（描述文字非必填）。"""
    session = FakeSession(fail_idx="0")
    plan = [
        {"idx": "0", "action": "translate", "text": "Will fail"},
        {"idx": "3", "action": "translate", "text": "Will pass"},
    ]
    r = await desc_text_apply(session, plan)
    assert r["status"] == "partial"
    assert [t["idx"] for t in r["translated"]] == ["3"]
    assert len(r["failed"]) == 1


@pytest.mark.asyncio
async def test_apply_translate_without_text_fails_not_clears():
    """没给 text 的 translate 记 failed，绝不能拿空串去覆盖模块。"""
    session = FakeSession()
    r = await desc_text_apply(session, [{"idx": "3", "action": "translate", "text": ""}])
    assert r["status"] == "partial"
    assert not session.calls          # 一次页面操作都不该发出
    assert "没给 text" in r["failed"][0]["err"]


@pytest.mark.asyncio
async def test_apply_truncates_over_500():
    """面板上限 500 字符，超了要先截断——否则平台静默截断，回读对不上判失败。"""
    session = FakeSession()
    long_text = "A" * 600
    r = await desc_text_apply(session, [{"idx": "3", "action": "translate",
                                         "text": long_text}])
    assert r["status"] == "ok"
    assert 'const text = "' + "A" * 500 + '"' in session.calls[0]


@pytest.mark.asyncio
async def test_apply_keep_does_nothing():
    session = FakeSession()
    r = await desc_text_apply(session, [{"idx": "3", "action": "keep"}])
    assert r["kept"] == ["3"]
    assert not session.calls
