# -*- coding: utf-8 -*-
"""数值输入型属性行（里料克重 g/m² 等）的识别与联动补填单测。

钉住 2026-08-25 修掉的缺陷：这类行在店小秘上是【输入框 + 只读单位下拉】的复合结构，
原实现按「行内有没有 .ant-select」判 kind，于是把克重行判成下拉行，连锁三个后果：

  1. current 读成单位文本「g/㎡」——这行看起来【已填】，LLM 不会填、必填复扫也不报，
     数值框一直空着，保存卡「请输入产品属性」；
  2. options 读成单位清单 ['g/㎡'] 并落进类目缓存，之后每个同类目商品都中同一颗雷；
  3. 写入走下拉分支去点单位下拉，必然填不上。

以及第二个独立缺陷：改「里料纹理」联动新增的必填行（里衬成分、里料克重）在第一轮
dump_attrs 时还不存在，主轮清单里必然没有它们；原实现只把它们记进 linkageNewRequired
就返回，没有任何调用方消费——检测到了却没人填。

真站取证（2026-08-25，rowid 173539495454695591）的控件序：
    里料克重（g/m²)  input[text] → select[g/㎡]      数值行：输入框在前
    里衬成分         select[请选择] → input[百分比]   下拉行：下拉在前
    成分             select[棉] → input[80] → select[聚酯纤维] → input[20]
故 kind 判据只能是「控件序里第一个控件是不是输入框」。
"""

from publish_patching import patch_publish
import pytest

from app.publish import cache, pipeline


PATH = ["服装、鞋靴和珠宝饰品", "女童时尚", "女童服装",
        "女童连衣裙", "女童休闲连衣裙"]


# ---- kind 判据（控件序，不是「有无 select」）--------------------------------
#
# 完整 DOM 行为无法离线跑（要真站的 antd 复合行），故这里对 JS 判据做结构断言：
# 钉住的是「不许退回按 hasSelect 判」这条不变式，实站验证记在提交说明里。

def test_kind判据用控件序而非有无select():
    js = pipeline._JS_LIST_ATTR_ROWS
    assert "firstIsInput" in js, "kind 判据应基于控件序里的第一个控件"
    assert "kind = firstIsInput ? 'number' : 'select'" in js
    # 原缺陷写法一处都不许留
    assert "kind: hasSelect ? 'select' : 'number'" not in js
    assert "hasSelect" not in js


def test_数值行的current只读输入框不读单位():
    """current 读 selection-item 就会把单位当成已填值，这是原缺陷的核心。"""
    js = pipeline._JS_LIST_ATTR_ROWS
    i = js.index("current: kind === 'number'")
    branch = js[i:i + 200]
    assert "numHint && numHint.value" in branch
    assert "(请输入)" in branch


def test_数值行单位取输入框之后的只读下拉():
    """unit 是模型判断量级的唯一线索；克重行的单位在 select 里，不在 input-suffix。"""
    js = pipeline._JS_LIST_ATTR_ROWS
    assert "unitFromSel" in js
    assert "seq.slice(1).find" in js, "单位应取输入框之后的那个 select"


# ---- 假 session 与行构造 -----------------------------------------------------

class _FakeSession:
    """只实现被测路径用到的两个方法，按预设应答属性行枚举。"""

    def __init__(self, attrs):
        self.rows = {"found": True, "attrs": attrs}

    async def eval_json(self, js, *a, **kw):
        if "productBasicInfo" in js and "ant-form-item" in js:
            return self.rows
        return {}

    async def wait_for(self, js, pred, **kw):
        return self.rows


def _num_row(label="里料克重（g/m²)", current="(请输入)", required=True):
    return {"label": label, "required": required, "current": current,
            "kind": "number", "numValues": [], "visible": True,
            "numHint": {"placeholder": "", "unit": "g/㎡", "value": ""}}


def _sel_row(label, current="(请选择)", required=True):
    return {"label": label, "required": required, "current": current,
            "kind": "select", "numValues": [], "visible": True, "numHint": None}


def _async(v):
    async def _f():
        return v
    return _f()


@pytest.fixture
def _stub_dom(monkeypatch):
    """把碰浏览器的辅助全替掉，只留缓存与 kind 分流这两条判断路径。"""
    async def _sleep(*a, **kw):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _sleep)
    patch_publish(monkeypatch, "pipeline", "_expand_attr_section", lambda *a, **kw: _async({}))
    patch_publish(monkeypatch, "pipeline", "_park_ghost_dropdowns",
                        lambda *a, **kw: _async({"parked": 0}))


# ---- dump_attrs 不给数值行读选项 --------------------------------------------

@pytest.mark.asyncio
async def test_数值行不点开下拉读选项(_stub_dom, monkeypatch):
    """行内那个 select 是只读单位，点开读到的是单位、不是可选值，白花一次开合。"""
    read = []

    async def _read(session, label, with_meta=False):
        read.append(label)
        meta = {"complete": True, "virtual": False,
                "scrollHeight": 0, "scrolledToEnd": True}
        return (["梭织", "针织"], meta) if with_meta else ["梭织", "针织"]

    patch_publish(monkeypatch, "pipeline", "_read_active_options", _read)
    s = _FakeSession([_num_row(), _sel_row("织造方式")])

    d = await pipeline.dump_attrs(s, cat_path=PATH)
    assert read == ["织造方式"], "数值行不该被点开读选项"
    num = next(a for a in d["attrs"] if a["kind"] == "number")
    assert num["options"] == []
    assert num["optionsEmptyReason"] == "number-row"


def test_数值行的单位不进缓存():
    """脏数据一旦落盘没有任何环节会发现，故缓存写入侧再挡一道。"""
    cache.save_attr_options("女童休闲连衣裙", PATH, [
        dict(_num_row(), options=["g/㎡"]),
        dict(_sel_row("织造方式"), options=["梭织", "针织"]),
    ])
    got = cache.load_attr_options("女童休闲连衣裙", PATH)
    assert "里料克重（g/m²)" not in got, "单位被当成选项存进缓存了"
    assert got["织造方式"] == ["梭织", "针织"]


# ---- 联动补填轮 --------------------------------------------------------------

@pytest.mark.asyncio
async def test_联动新增必填行会被补填(_stub_dom, monkeypatch):
    """里料纹理选「光面」带出的里衬成分/里料克重，必须真的填上而不只是报出来。"""
    s = _FakeSession([_sel_row("里衬成分"), _num_row()])
    asked = {}

    async def _ask(rows, info, main_comp):
        asked["rows"] = rows
        return {"changes": [
            {"label": "里衬成分", "value": "聚酯纤维(涤纶）", "num": 100, "row": 1},
            {"label": "里料克重（g/m²)", "value": "80"},
        ], "notes": []}

    async def _apply(session, changes, row_map, info, cat_path, **kw):
        return ([{"label": c["label"], "value": c["value"], "result": "ok"}
                 for c in changes], [], [])

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)
    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes", _apply)
    patch_publish(monkeypatch, "pipeline", "_read_active_options",
                        lambda *a, **kw: _async(
                            (["聚酯纤维(涤纶）", "棉"], {"complete": True})))

    r = await pipeline._fill_linkage_rows(
        s, {"里料纹理", "织造方式"}, {"title": "连衣裙"}, None, PATH)

    assert r["newRequired"] == ["里衬成分", "里料克重（g/m²)"]
    assert len(r["applied"]) == 2 and all(a["result"] == "ok" for a in r["applied"])
    # kind/numHint 必须一起喂给 LLM，否则数值行会拿到选项文本
    num_ask = next(x for x in asked["rows"] if x["label"] == "里料克重（g/m²)")
    assert num_ask["kind"] == "number" and num_ask["numHint"]["unit"] == "g/㎡"


@pytest.mark.asyncio
async def test_主轮已有的行不算联动新增(_stub_dom, monkeypatch):
    """pre_labels 里的行由主轮负责，补填轮不许重复动它（会与主轮的重试打架）。"""
    s = _FakeSession([_sel_row("里衬成分")])
    called = []

    async def _ask(*a, **kw):
        called.append(1)
        return {}

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)
    r = await pipeline._fill_linkage_rows(
        s, {"里衬成分"}, {"title": "x"}, None, PATH)
    assert r == {"applied": [], "newRequired": [], "compFailed": []}
    assert called == []


@pytest.mark.asyncio
async def test_有预填值的联动行也会被审但不改(_stub_dom, monkeypatch):
    """2026-09-03 起：联动行无论有无预填值都扫、交 LLM 审，LLM 无改动时不碰页面。

    预填值可能是平台默认（如「里衬成分」默认棉），默认值未必合适，不能靠「已填就不扫」
    跳过——那会让错的默认值一路带到保存。
    """
    s = _FakeSession([_sel_row("里衬成分", current="棉")])
    called = []

    async def _ask(*a, **kw):
        called.append(1)
        return {}  # LLM 判断预填值「棉」合适，无 change

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)
    r = await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert called == [1]                     # 扫到并审过，不因预填值跳过
    assert r["applied"] == []                # 无 change，不写入
    assert r["newRequired"] == ["里衬成分"]   # 仍记录扫到的行，交末尾复扫


@pytest.mark.asyncio
async def test_补填问LLM失败不抛(_stub_dom, monkeypatch):
    """best-effort：补填是增益路径，坏了交末尾 unfilledRequired 报人工，不能中断阶段。"""
    s = _FakeSession([_sel_row("里衬成分")])

    async def _boom(*a, **kw):
        raise RuntimeError("LLM 挂了")

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _boom)
    patch_publish(monkeypatch, "pipeline", "_read_active_options",
                        lambda *a, **kw: _async((["棉"], {"complete": True})))

    r = await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert r["applied"] == []
    assert r["newRequired"] == ["里衬成分"]      # 仍要报出来交人工


@pytest.mark.asyncio
async def test_补填走同一套校验闸(_stub_dom, monkeypatch):
    """编造的选项在补填轮也要被拒——闸门与主轮共用，不能另写一套。"""
    s = _FakeSession([_sel_row("里衬成分")])
    patch_publish(monkeypatch, "pipeline", "_ask_attr_review",
                        lambda *a, **kw: _async(
                            {"changes": [{"label": "里衬成分", "value": "编造纤维"}],
                             "notes": []}))
    patch_publish(monkeypatch, "pipeline", "_read_active_options",
                        lambda *a, **kw: _async((["棉", "亚麻"], {"complete": True})))
    called = []

    async def _apply(*a, **kw):
        called.append(1)
        return ([], [], [])

    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes", _apply)
    r = await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert called == [], "编造的值不该走到写入"
    assert r["applied"] == []


# ---- readback 形状统一（2026-08-25 阶段④崩溃）--------------------------------
#
# 真实崩溃：联动补填写「里料克重（g/m²)」→ set_attr 走数值分支 → readback 是
# input.value 字符串 → _apply_attr_changes 按 r["readback"]["current"] 取值 →
# AttributeError: 'str' object has no attribute 'get'，整个阶段④失败、商品未落库。

@pytest.mark.asyncio
async def test_数值行的readback与下拉行同形状(monkeypatch):
    """契约层的修法：set_attr 两条分支的 readback 必须都是带 current 的字典。"""
    class _S:
        async def eval_json(self, js, *a, **kw):
            return {"status": "ok", "before": "", "readback": "80"}

    r = await pipeline.set_attr(_S(), "里料克重（g/m²)", "80", kind="number")
    assert isinstance(r["readback"], dict), "数值行也要给字典，否则调用方取值即崩"
    assert r["readback"]["current"] == "80"


@pytest.mark.asyncio
async def test_数值行写入失败时readback也已归一(monkeypatch):
    """error 分支同样带 readback（回读不符时透出实际值），不能漏归一。"""
    class _S:
        async def eval_json(self, js, *a, **kw):
            return {"status": "error", "reason": "readback-mismatch",
                    "before": "", "readback": ""}

    r = await pipeline.set_attr(_S(), "里料克重（g/m²)", "80", kind="number")
    assert r["status"] == "error"
    assert r["readback"] == {"label": "里料克重（g/m²)", "current": ""}


@pytest.mark.asyncio
async def test_写入数值行不再抛属性错误(_stub_dom, monkeypatch):
    """端到端钉住崩溃点：_apply_attr_changes 拿到数值行结果要能正常记录。"""
    class _NumSession:
        async def eval_json(self, js, *a, **kw):
            return {"status": "ok", "readback": "80"}

    # 必须先抓住原函数：替换后在替身里再调 pipeline.set_attr 会调到自己、无限递归
    _orig = pipeline.set_attr

    async def _set(session, label, value, num=None, row=1, kind="select"):
        # 走真实数值分支（未归一时它返回字符串，下面记录 readback 时就会炸）
        return await _orig(_NumSession(), label, value, kind=kind)

    patch_publish(monkeypatch, "pipeline", "set_attr", _set)
    changes = [{"label": "里料克重（g/m²)", "value": "80", "kind": "number"}]
    row_map = {"里料克重（g/m²)": _num_row()}
    applied, refreshed, comp_failed = await pipeline._apply_attr_changes(
        None, changes, row_map, {"attributes": {}}, PATH)
    assert applied[0]["result"] == "ok"
    assert applied[0]["readback"] == "80", "诊断字段要照旧填上，不是简单吞成 None"
    assert comp_failed == []


def test_readback取值助手容得下三种形状():
    """诊断字段绝不该有能力中断主流程：字典/字符串/缺失都要安全返回。"""
    f = pipeline._readback_current
    assert f({"readback": {"current": "梭织"}}) == "梭织"
    assert f({"readback": "80"}) == "80"
    assert f({"readback": None}) is None
    assert f({}) is None


# ---- 多轮联动补填（里料纹理非「无内衬/无里料」时必然继续联动）------------------
#
# 里料纹理只要不选「无内衬/无里料」就会带出必须继续选的行，而【补填自己写的值会再
# 联动】：里衬成分选定后平台带出其百分比/克重行。单轮补填只能填第一层，第二层静默
# 留空到保存才炸。这几个用例钉住「循环追到收敛」以及两个终止条件。

class _MultiRoundSession:
    """按调用轮次返回不同的属性行，模拟「填完一层又冒出一层」。

    rounds 是每轮 _JS_LIST_ATTR_ROWS 应答的行列表；耗尽后一直返回最后一份
    （真实页面也是这样：不再联动就维持现状）。
    """

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0

    async def eval_json(self, js, *a, **kw):
        if "productBasicInfo" in js and "ant-form-item" in js:
            i = min(self.calls, len(self.rounds) - 1)
            self.calls += 1
            return {"found": True, "attrs": self.rounds[i]}
        return {}


@pytest.fixture
def _stub_round(monkeypatch):
    """把读选项与 LLM 替成确定性应答，只留轮次控制流。"""
    patch_publish(monkeypatch, "pipeline", "_read_active_options",
                        lambda *a, **kw: _async((["棉", "聚酯纤维"],
                                                 {"complete": True})))

    async def _ask(rows, info, main_comp):
        # 每行都给一个 options 里的合法值，成分行凑满 100%
        changes = []
        for r in rows:
            if r["kind"] == "number":
                changes.append({"label": r["label"], "value": "80",
                                "kind": "number"})
            else:
                changes.append({"label": r["label"], "value": "棉", "num": 100})
        return {"changes": changes, "notes": []}

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)


@pytest.mark.asyncio
async def test_第二层联动行也会被补填(_stub_dom, _stub_round, monkeypatch):
    """核心用例：里衬成分填上后冒出的「里衬成分含量」必须也填，不能留给人工。"""
    r1 = [_sel_row("里衬成分")]
    r2 = [_sel_row("里衬成分", current="棉"), _num_row("里衬成分含量")]
    s = _MultiRoundSession([r1, r2, r2])

    async def _apply(session, changes, row_map, info, cat_path, **kw):
        return ([{"label": c["label"], "result": "ok"} for c in changes], [], [])

    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes", _apply)
    r = await pipeline._fill_linkage_rows(
        s, {"里料纹理"}, {"title": "x"}, None, PATH)
    filled = [a["label"] for a in r["applied"]]
    assert "里衬成分" in filled
    assert "里衬成分含量" in filled, "第二层联动行没补上，保存时会卡必填"
    assert r["newRequired"] == ["里衬成分", "里衬成分含量"]


@pytest.mark.asyncio
async def test_不再冒新行就停止不白转(_stub_dom, _stub_round, monkeypatch):
    """收敛即退出：多余轮次每轮都要花 1.5s + 一次 LLM，不能空转到上限。"""
    r1 = [_sel_row("里衬成分")]
    done = [_sel_row("里衬成分", current="棉")]
    s = _MultiRoundSession([r1, done, done, done])
    asked = []

    async def _ask(rows, info, main_comp):
        asked.append([r["label"] for r in rows])
        return {"changes": [{"label": r["label"], "value": "棉", "num": 100}
                            for r in rows], "notes": []}

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)
    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes",
                        lambda session, changes, *a, **kw: _async(
                            ([{"label": c["label"], "result": "ok"}
                              for c in changes], [], [])))
    await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert len(asked) == 1, f"收敛后仍在问 LLM：{asked}"


@pytest.mark.asyncio
async def test_一轮全失败就停止追加轮次(_stub_dom, _stub_round, monkeypatch):
    """写不进去时再转轮次只是白烧 LLM——同一批 label 已进 seen，连扫都扫不到。"""
    rows = [_sel_row("里衬成分"), _num_row("里衬成分含量")]
    s = _MultiRoundSession([rows, rows, rows, rows])
    rounds = []

    async def _apply(session, changes, *a, **kw):
        rounds.append(1)
        return ([{"label": c["label"], "result": "error"} for c in changes], [], [])

    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes", _apply)
    r = await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert len(rounds) == 1, "一项都没写成功还继续转轮次"
    assert r["newRequired"] == ["里衬成分", "里衬成分含量"]   # 仍要报出来交人工


@pytest.mark.asyncio
async def test_轮次有上限不会无界循环(_stub_dom, _stub_round, monkeypatch):
    """每轮都冒新行的极端情况（联动比实测更深）必须有界，否则本阶段耗时失控。"""
    def _round(i):
        return [_sel_row(f"联动行{i}")]

    s = _MultiRoundSession([_round(i) for i in range(10)])
    rounds = []

    async def _apply(session, changes, *a, **kw):
        rounds.append(1)
        return ([{"label": c["label"], "result": "ok"} for c in changes], [], [])

    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes", _apply)
    r = await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert len(rounds) == pipeline._LINKAGE_MAX_ROUNDS
    assert len(r["newRequired"]) == pipeline._LINKAGE_MAX_ROUNDS


@pytest.mark.asyncio
async def test_已见过的行不重复补(_stub_dom, _stub_round, monkeypatch):
    """差集对累积 seen 取：写失败的行每轮都还在页面上，只跟上一轮比会反复重填。"""
    stuck = [_sel_row("里衬成分"), _num_row("里料克重（g/m²)")]
    # 第二轮：里衬成分填上了，克重仍空（写失败），另冒一个新行
    r2 = [_sel_row("里衬成分", current="棉"), _num_row("里料克重（g/m²)"),
          _sel_row("里布工艺")]
    s = _MultiRoundSession([stuck, r2, r2])
    asked = []

    async def _ask(rows, info, main_comp):
        asked.append([r["label"] for r in rows])
        return {"changes": [{"label": r["label"], "value": "棉", "num": 100}
                            for r in rows if r["kind"] != "number"], "notes": []}

    patch_publish(monkeypatch, "pipeline", "_ask_attr_review", _ask)
    patch_publish(monkeypatch, "pipeline", "_apply_attr_changes",
                        lambda session, changes, *a, **kw: _async(
                            ([{"label": c["label"], "result": "ok"}
                              for c in changes], [], [])))
    await pipeline._fill_linkage_rows(s, set(), {"title": "x"}, None, PATH)
    assert asked[1] == ["里布工艺"], f"第二轮把见过的行又问了一遍：{asked[1]}"


# ---- 数值行的行定位与占位符转义 ----------------------------------------------

def test_数值行写入带label文本兜底():
    """同批先写的下拉行会触发重渲染，只认 data-attr-label 会丢行、该行永远填不上。"""
    js = pipeline._JS_SET_ATTR_NUM
    assert "const pick = ()" in js, "取行应收敛成一个带兜底的函数"
    assert ".attr-label" in js, "兜底要按内层 span 取动态属性行的名字"
    assert "setAttribute('data-attr-label'" in js, "找到后要补打标记"
    # 三处取行（首次、回读轮询、末次）都必须走 pick，不许留裸 querySelector
    assert js.count("pick()") >= 3
    assert 'document.querySelector(\'[data-attr-label="__LABEL__"]\')' not in js


def test_数值行占位符走J转义():
    """属性名含「（g/m²)」，裸拼进 JS 字符串字面量遇引号/反斜杠就破语法。"""
    js = pipeline._JS_SET_ATTR_NUM
    assert "__LABELQ__" in js and "__VALUEQ__" in js
    # 裸占位符一处都不许留（J() 自带引号，故占位符处不能再包引号）
    assert "'__LABEL__'" not in js and '"__LABEL__"' not in js
    assert "'__VALUE__'" not in js and '"__VALUE__"' not in js


@pytest.mark.asyncio
async def test_带引号的属性名不会破JS语法():
    """回归钉子：标签里的引号必须被转义，否则整段 eval 语法错、该行填不上。"""
    seen = {}

    class _S:
        async def eval_json(self, js, *a, **kw):
            seen["js"] = js
            return {"status": "ok", "readback": "80"}

    await pipeline.set_attr(_S(), '里料克重"特殊"（g/m²)', "80", kind="number")
    js = seen["js"]
    assert '\\"' in js, "引号没被转义"
    # 转义后仍是一段合法的 JS 字符串字面量：JSON 解析它应还原出原标签
    import json as _j
    import re as _re
    lit = _re.search(r"const LB = (\".*?\");", js)
    assert lit, "没找到注入的字面量"
    assert _j.loads(lit.group(1)) == '里料克重"特殊"（g/m²)'


def test_数值行取行不拼CSS属性字面量():
    """回归钉子（2026-08-25）：拼 [data-attr-label=里料克重（g/m²)] 是非法选择器。

    属性名带全角括号/引号时 querySelector 直接抛 SyntaxError，阶段④整个异常、
    商品未落库。取行改成枚举 [data-attr-label] 再按 getAttribute 比对，
    不把用户数据拼进选择器语法里。
    """
    js = pipeline._JS_SET_ATTR_NUM
    assert "'.ant-form-item[data-attr-label=' + " not in js, "不许把标签拼进选择器"
    assert "querySelectorAll('.ant-form-item[data-attr-label]')" in js
    assert "getAttribute('data-attr-label') === LB" in js


@pytest.mark.asyncio
async def test_全角括号属性名的取行JS语法合法(monkeypatch):
    """真跑一遍 JS 语法检查：用 Node 解析整段 eval，语法错就当场失败。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("环境没有 node，跳过语法解析")
    seen = {}

    class _S:
        async def eval_json(self, js, *a, **kw):
            seen["js"] = js
            return {"status": "ok", "readback": "80"}

    await pipeline.set_attr(_S(), "里料克重（g/m²)", "80", kind="number")
    # new Function 只解析不执行，能抓出 SyntaxError；DOM API 不会被调用
    probe = "new Function(" + _j_dumps(seen["js"]) + ")"
    r = subprocess.run([node, "-e", probe], capture_output=True, text=True)
    assert r.returncode == 0, f"JS 语法非法：{r.stderr}"


def _j_dumps(s: str) -> str:
    import json
    return json.dumps(s)


def test_数值行不带optionsFrom缓存标记():
    """钉住「数值行走不进 _refresh_row_and_retry」这个前提。

    那个函数内部两处 set_attr 硬写 (None, 1) 且不传 kind，数值行进去必然按下拉流程
    重试、白花一次 LLM。它现在进不去，靠的是 dump_attrs / 联动读选项两处都在设
    optionsFrom 之前就对 kind == 'number' 提前 continue。这道断言防止以后挪动那两个
    continue 时静默打开这条错路——真要放开，就得先给 set_attr 补 kind 透传。
    """
    import inspect
    from app.publish import pipeline as P
    src = inspect.getsource(P._refresh_row_and_retry)
    assert "kind=" not in src, "这个函数还没支持 kind，前提断言需要同步更新"
    for fn in (P.dump_attrs, P._read_linkage_options):
        s = inspect.getsource(fn)
        i_num = s.index('a.get("kind") == "number"')
        i_cache = s.index('a["optionsFrom"] = "cache"')
        assert i_num < i_cache, "数值行的 continue 必须在设 optionsFrom 之前"
