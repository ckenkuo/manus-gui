"""属性审核拆组并发的离线单测。

为什么值得单测：拆组是纯粹的提速手段（2026-08-24 实测 34 行一次问要 65 秒，
推理量随行数走，拆两组并发约减半），但它有一条会静默出错的红线——

成分类字段的所有行必须落在同一次调用里。_ATTR_PROMPT 规则 5 要求「同一字段所有
成分行百分比之和恰好 100」（平台硬性校验），劈到两次调用里两边各自凑 100%，
合起来 200% 必被平台拦，而拦下来的表现只是「保存失败」，看不出是拆组导致的。

不覆盖：真实 LLM 判断质量。这里只钉分组规则与合并语义。
"""
import pytest

from app.publish.pipeline import (
    _ATTR_SPLIT_MIN_ROWS,
    _ask_attr_review,
    _split_attr_rows,
)


def _row(label, num=None):
    return {"label": label, "current": "(请选择)", "required": True,
            "numValues": num, "options": ["A", "B"]}


def _rows(n, prefix="属性"):
    return [_row(f"{prefix}{i}") for i in range(n)]


# ---- 分组规则 -----------------------------------------------------------------

def test_行少时不拆():
    rows = _rows(_ATTR_SPLIT_MIN_ROWS - 1)
    assert _split_attr_rows(rows) == [rows]


def test_行多时拆两组且不丢行():
    rows = _rows(34)
    groups = _split_attr_rows(rows)
    assert len(groups) == 2
    flat = [r for g in groups for r in g]
    assert len(flat) == 34
    assert {r["label"] for r in flat} == {r["label"] for r in rows}


def test_两组行数均衡():
    """按下标对半砍会因成分字段占多行而切出一边倒的组，并发就白拆了。"""
    groups = _split_attr_rows(_rows(34))
    a, b = len(groups[0]), len(groups[1])
    assert abs(a - b) <= 1, f"分组不均衡：{a} vs {b}"


def test_同名成分行绝不跨组():
    """这条是拆组的红线：同一字段的成分行必须整组在一起（合计 100% 才算得出来）。"""
    rows = (_rows(28)
            + [_row("上装成分", 90), _row("上装成分", 10)]      # 主面料，两行
            + [_row("里衬成分", 60), _row("里衬成分", 40)])     # 另一块布料，两行
    groups = _split_attr_rows(rows)
    for label in ("上装成分", "里衬成分"):
        hit = [i for i, g in enumerate(groups)
               if any(r["label"] == label for r in g)]
        assert len(hit) == 1, f"{label} 被拆到了 {len(hit)} 个组里"
        n = sum(1 for r in groups[hit[0]] if r["label"] == label)
        assert n == 2, f"{label} 只有 {n} 行落在组内，另一行丢了"


def test_成分行占多数时也不拆开():
    """极端形状：大部分行都是同一个成分字段，仍不能为了均衡把它劈开。"""
    rows = _rows(10) + [_row("上装成分", 10) for _ in range(20)]
    groups = _split_attr_rows(rows)
    hit = [i for i, g in enumerate(groups) if any(r["label"] == "上装成分" for r in g)]
    assert len(hit) == 1
    assert sum(1 for r in groups[hit[0]] if r["label"] == "上装成分") == 20


# ---- 合并语义 -----------------------------------------------------------------

@pytest.fixture
def _info():
    return {"title": "测试商品", "attributes": {"材质": "棉"},
            "imageUnderstanding": {}}


@pytest.mark.asyncio
async def test_合并两组的changes与notes(monkeypatch, _info):
    """调用方拿到的形状必须与单次调用一致——它不该知道这里拆没拆。"""
    from app.publish import pipeline

    calls = []

    async def _fake(prompt, what="", stage=None):
        calls.append(what)
        i = len(calls)
        return {"changes": [{"label": f"改{i}", "value": "A"}],
                "notes": [f"存疑{i}"]}

    monkeypatch.setattr("app.publish.llm.ask_json", _fake)
    out = await pipeline._ask_attr_review(_rows(34), _info, None)
    assert len(calls) == 2
    assert [c["label"] for c in out["changes"]] == ["改1", "改2"]
    assert out["notes"] == ["存疑1", "存疑2"]


@pytest.mark.asyncio
async def test_行少时只调一次(monkeypatch, _info):
    from app.publish import pipeline

    calls = []

    async def _fake(prompt, what="", stage=None):
        calls.append(what)
        return {"changes": [], "notes": []}

    monkeypatch.setattr("app.publish.llm.ask_json", _fake)
    await pipeline._ask_attr_review(_rows(10), _info, None)
    assert calls == ["属性审核"]        # 不带「第N组」后缀


@pytest.mark.asyncio
async def test_一组失败则整体失败(monkeypatch, _info):
    """不拿成功的那组凑合：属性填一半比不填更糟（缺的行会静默留空带到发布）。"""
    from app.publish import pipeline

    async def _fake(prompt, what="", stage=None):
        if "第2组" in what:
            raise RuntimeError("端点抖了")
        return {"changes": [{"label": "改1", "value": "A"}], "notes": []}

    monkeypatch.setattr("app.publish.llm.ask_json", _fake)
    with pytest.raises(RuntimeError, match="端点抖了"):
        await pipeline._ask_attr_review(_rows(34), _info, None)


@pytest.mark.asyncio
async def test_每组都走attrs阶段的模型(monkeypatch, _info):
    """拆组不该绕开按阶段的模型选择（否则提速配置对拆出来的组失效）。"""
    from app.publish import pipeline

    stages = []

    async def _fake(prompt, what="", stage=None):
        stages.append(stage)
        return {"changes": [], "notes": []}

    monkeypatch.setattr("app.publish.llm.ask_json", _fake)
    await pipeline._ask_attr_review(_rows(34), _info, None)
    assert stages == ["attrs", "attrs"]


@pytest.mark.asyncio
async def test_每组的提示词只带自己那部分行(monkeypatch, _info):
    """组里不该混进别组的行：模型会对没给它管的行也提修改建议，白耗 token。"""
    from app.publish import pipeline

    seen = []

    async def _fake(prompt, what="", stage=None):
        seen.append(prompt)
        return {"changes": [], "notes": []}

    monkeypatch.setattr("app.publish.llm.ask_json", _fake)
    rows = _rows(34)
    await pipeline._ask_attr_review(rows, _info, None)
    # 每行恰好出现在一个组的提示词里
    for r in rows:
        hit = sum(1 for p in seen if f'"{r["label"]}"' in p)
        assert hit == 1, f'{r["label"]} 出现在 {hit} 个提示词里'
