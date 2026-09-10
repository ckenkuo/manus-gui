# -*- coding: utf-8 -*-
"""阶段⑩a SKU 货号英化（pipeline.fix_sku_codes）离线单测。

2026-08-23 真站取证（rowid 173539495453435641，8 行变种）：平台「一键生成」写进
货号列的是 `粉红色-80cm（适合身高70）`——中文 + 全角括号，而该列硬校验「不能包含
中文和中文符号」。且 8 行里只有 2 行有值，那 2 行的尺码还与所在行对不上
（尺码列「90」的行，货号写的是 `...100cm（适合身高90）`）。故本阶段完全重写货号，
只信页面自己的颜色/尺码两列。

这里钉住的是不依赖浏览器的那几层语义：token 清洗、非 ASCII 判定、词表去重与
并发翻译、逐行拼装与重名加序号、以及回读校验的失败出口。
"""

from publish_patching import patch_publish
import json

import pytest

from app.publish import pipeline


# ---- 纯函数：token 清洗与非 ASCII 判定 ---------------------------------------

def test_sku_token_只留字母数字并首字母大写():
    assert pipeline.sku_token("Navy Blue") == "NavyBlue"
    assert pipeline.sku_token("off-white") == "OffWhite"
    assert pipeline.sku_token("pink") == "Pink"
    # 数字开头的尺码保持原样（首字符不是字母，upper 不改变它）
    assert pipeline.sku_token("80cm") == "80cm"
    assert pipeline.sku_token("XL") == "XL"


def test_sku_token_中文与纯符号清成空():
    # 清完为空是「翻译没生效」的信号，调用方据此判失败
    assert pipeline.sku_token("粉红色") == ""
    assert pipeline.sku_token("（）-_") == ""
    assert pipeline.sku_token("") == ""


def test_has_cjk_覆盖中文与全角符号():
    assert pipeline.has_cjk("粉红色")
    assert pipeline.has_cjk("Pink-80cm（适合身高70）")   # 全角括号也算
    assert pipeline.has_cjk("Ｍ")                        # 全角字母
    assert not pipeline.has_cjk("Pink-80")
    assert not pipeline.has_cjk("")


# ---- fix_sku_codes：词表/拼装/校验 ------------------------------------------

class _FakeSession:
    """按 JS 片段特征分派返回值，记录填进去的 PLAN 供断言。"""

    def __init__(self, rows, fill_result=None):
        self.rows = rows
        self.fill_result = fill_result
        self.plan = None

    async def eval_json(self, js):
        if "const PLAN = " not in js:
            return {"rows": self.rows}
        # 填写那段：把 __PLAN__ 替换后的 JSON 抠回来核对
        head = "const PLAN = "
        i = js.index(head) + len(head)
        self.plan = json.loads(js[i:js.index(";\n", i)])
        if self.fill_result is not None:
            return self.fill_result
        return {"filled": len(self.plan), "mismatch": [], "bad": [],
                "sample": [[p["i"], p["code"]] for p in self.plan[:4]]}


def _fake_translate(mapping):
    """把 _translate_term 换成查表，避免单测真调 LLM。"""
    async def _t(term, kind, sem):
        return term, pipeline.sku_token(mapping[term])
    return _t


@pytest.mark.asyncio
async def test_中文颜色翻译后逐行拼装(monkeypatch):
    rows = [
        {"i": 0, "color": "粉红色", "size": "80", "cur": "粉红色-80cm（适合身高70）"},
        {"i": 1, "color": "粉红色", "size": "90", "cur": ""},
        {"i": 2, "color": "藏青色", "size": "80", "cur": ""},
    ]
    patch_publish(monkeypatch, "pipeline", "_translate_term",
                        _fake_translate({"粉红色": "Pink", "藏青色": "Navy"}))
    s = _FakeSession(rows)
    r = await pipeline.fix_sku_codes(s)
    assert r["status"] == "ok"
    assert r["codes"] == ["Pink-80", "Pink-90", "Navy-80"]
    # 平台原有的中文值不被沿用，整列重写
    assert all(not pipeline.has_cjk(c) for c in r["codes"])
    assert r["translated"] == {"粉红色": "Pink", "藏青色": "Navy"}


@pytest.mark.asyncio
async def test_按词去重只翻一次(monkeypatch):
    """4 行 2 个颜色 → 只该发 2 次翻译（按行翻会重复问同一个词）。"""
    rows = [
        {"i": 0, "color": "粉红色", "size": "80", "cur": ""},
        {"i": 1, "color": "粉红色", "size": "90", "cur": ""},
        {"i": 2, "color": "藏青色", "size": "80", "cur": ""},
        {"i": 3, "color": "藏青色", "size": "90", "cur": ""},
    ]
    calls = []

    async def _t(term, kind, sem):
        calls.append(term)
        return term, {"粉红色": "Pink", "藏青色": "Navy"}[term]

    patch_publish(monkeypatch, "pipeline", "_translate_term", _t)
    r = await pipeline.fix_sku_codes(_FakeSession(rows))
    assert r["status"] == "ok"
    assert sorted(calls) == ["粉红色", "藏青色"]      # 尺码是数字，不进翻译
    assert r["codes"] == ["Pink-80", "Pink-90", "Navy-80", "Navy-90"]


@pytest.mark.asyncio
async def test_已是英文的词不调翻译(monkeypatch):
    rows = [{"i": 0, "color": "Black", "size": "XL", "cur": ""}]

    async def _boom(term, kind, sem):
        raise AssertionError(f"不该翻译已是 ASCII 的词：{term}")

    patch_publish(monkeypatch, "pipeline", "_translate_term", _boom)
    r = await pipeline.fix_sku_codes(_FakeSession(rows))
    assert r["status"] == "ok"
    assert r["codes"] == ["Black-XL"]
    assert r["translated"] == {}


@pytest.mark.asyncio
async def test_不同中文译成同一英文时加序号(monkeypatch):
    """「粉色」「粉红色」都译 Pink，同尺码就撞号——货号是 SKU 唯一标识，必须错开。"""
    rows = [
        {"i": 0, "color": "粉色", "size": "80", "cur": ""},
        {"i": 1, "color": "粉红色", "size": "80", "cur": ""},
        {"i": 2, "color": "粉色", "size": "90", "cur": ""},
    ]
    patch_publish(monkeypatch, "pipeline", "_translate_term",
                        _fake_translate({"粉色": "Pink", "粉红色": "Pink"}))
    r = await pipeline.fix_sku_codes(_FakeSession(rows))
    assert r["status"] == "ok"
    assert r["codes"] == ["Pink-80", "Pink-80-2", "Pink-90"]
    assert len(set(r["codes"])) == len(r["codes"])


@pytest.mark.asyncio
async def test_翻译结果剥完为空则失败不写入(monkeypatch):
    """不兜底转拼音、不留中文：填错货号会一路带到发布，直接失败交上层重试。"""
    rows = [{"i": 0, "color": "粉红色", "size": "80", "cur": ""}]

    async def _t(term, kind, sem):
        return term, pipeline.sku_token("粉红")     # 模型没翻，剥完为空

    patch_publish(monkeypatch, "pipeline", "_translate_term", _t)
    s = _FakeSession(rows)
    r = await pipeline.fix_sku_codes(s)
    assert r["status"] == "error"
    assert "未能译成合法货号" in r["reason"]
    assert s.plan is None          # 没走到写入


@pytest.mark.asyncio
async def test_模型混着中文吐回时剥净可用(monkeypatch):
    """`粉Pink` 这种夹带中文的回答，sku_token 会剥成 Pink，不必因此判失败。"""
    rows = [{"i": 0, "color": "粉红色", "size": "80", "cur": ""}]

    async def _t(term, kind, sem):
        return term, pipeline.sku_token("粉Pink")

    patch_publish(monkeypatch, "pipeline", "_translate_term", _t)
    r = await pipeline.fix_sku_codes(_FakeSession(rows))
    assert r["status"] == "ok"
    assert r["codes"] == ["Pink-80"]


@pytest.mark.asyncio
async def test_回读发现非ascii残留报validation_error(monkeypatch):
    rows = [{"i": 0, "color": "粉红色", "size": "80", "cur": ""}]
    patch_publish(monkeypatch, "pipeline", "_translate_term", _fake_translate({"粉红色": "Pink"}))
    s = _FakeSession(rows, fill_result={
        "filled": 1, "mismatch": [],
        "bad": [{"i": 0, "v": "粉红色-80", "why": "non-ascii"}], "sample": []})
    r = await pipeline.fix_sku_codes(s)
    assert r["status"] == "validation-error"
    assert r["bad"]


@pytest.mark.asyncio
async def test_行序被重排时那行跳过并报出(monkeypatch):
    rows = [{"i": 0, "color": "粉红色", "size": "80", "cur": ""}]
    patch_publish(monkeypatch, "pipeline", "_translate_term", _fake_translate({"粉红色": "Pink"}))
    s = _FakeSession(rows, fill_result={
        "filled": 0, "mismatch": [{"i": 0, "why": "row-moved", "now": "藏青色/90"}],
        "bad": [], "sample": []})
    r = await pipeline.fix_sku_codes(s)
    assert r["status"] == "validation-error"
    assert r["mismatch"][0]["why"] == "row-moved"


@pytest.mark.asyncio
async def test_无变种行直接报错():
    r = await pipeline.fix_sku_codes(_FakeSession([]))
    assert r["status"] == "error"
    assert "variationSku" in r["reason"]


@pytest.mark.asyncio
async def test_读取失败原样报出():
    class _Err:
        async def eval_json(self, js):
            return {"err": "no-skuDataInfo"}

    r = await pipeline.fix_sku_codes(_Err())
    assert r["status"] == "error"
    assert "no-skuDataInfo" in r["reason"]


# ---- JS 片段的形状约束（与 test_publish_stock 同一套体检口径）----------------

def test_js_片段基本约束():
    assert "input[name=variationSku]" in pipeline._JS_READ_SKU_CODES
    assert "__PLAN__" in pipeline._JS_FILL_SKU_CODES
    # 必须只取第一个 tbody：第二个是库存/SKU分类表，那里没有货号列
    assert "querySelectorAll('tbody')[0]" in pipeline._JS_READ_SKU_CODES
    assert "querySelectorAll('tbody')[0]" in pipeline._JS_FILL_SKU_CODES
    # 写入前逐行核对颜色/尺码，防 Vue 重排把货号填到别的 SKU 上
    assert "row-moved" in pipeline._JS_FILL_SKU_CODES
    # 原生 setter + input/change 事件，Vue 才认（同变种信息那段）
    assert "getOwnPropertyDescriptor" in pipeline._JS_FILL_SKU_CODES
    # 不能出现 null 字节（正则字符区间写法在多层字符串里会被吃成 \x00）
    assert "\x00" not in pipeline._JS_FILL_SKU_CODES


def test_live_state_单独暴露货号合法性():
    """货号不能跟着 skuFilledRows 判：中文值也算「有值」。"""
    js = pipeline._JS_LIVE_STATE
    assert "skuCodeBad" in js and "skuCodeCount" in js
    assert "input[name=variationSku]" in js
    assert "charCodeAt(0) > 127" in js
    assert "\x00" not in js


def test_service_阶段表已登记货号阶段():
    """新增可选阶段要四处同步（阶段表/分发表/未保存表单集/CLI），漏一处就静默不跑。"""
    from app.publish import service

    assert ("sku_code", "⑩a SKU货号") in service.STAGES
    assert "sku_code" in service._STAGE_FUNCS
    # 只改未保存表单，重载即丢，续跑要按页面实况判
    assert "sku_code" in service._FORM_ONLY_STAGES
    # 必须排在 ⑩ 变种之前：读的时候行序要稳
    ids = [s for s, _ in service.STAGES]
    assert ids.index("sku_code") < ids.index("variant")
    assert ids.index("fix_sizes") < ids.index("sku_code")
