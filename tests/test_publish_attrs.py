"""属性修改清单二次校验的离线单测（纯逻辑，不碰浏览器、不碰 LLM）。

_validate_attr_changes 是 LLM 与真实表单之间的闸门：LLM 会编造选项、会违反
「非必填不填」策略、会给出合计不等于 100 的成分比例。这些都得在写入前拦下来，
因为属性填错会一路带到发布。
"""
import pytest

from app.publish.extract import COMP_DEFAULT_FIBER, parse_main_composition
from app.publish.pipeline import _validate_attr_changes


def _attr(label, current="(请选择)", required=True, options=None, nums=None):
    return {"label": label, "current": current, "required": required,
            "options": options or [], "numValues": nums or []}


def test_编造的选项被拒():
    attrs = [_attr("织造方式", options=["梭织", "针织"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "织造方式", "value": "手工编织"}], attrs)
    assert valid == []
    assert len(rejected) == 1
    assert "不在 options 内" in rejected[0]["rejectReason"]


def test_options_内的值通过():
    attrs = [_attr("织造方式", options=["梭织", "针织"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "织造方式", "value": "针织"}], attrs)
    assert len(valid) == 1 and valid[0]["value"] == "针织"
    assert rejected == []


def test_非必填且未填的拒():
    """非必填 + 当前未填 → 留空（options 为空，即该行被 dump_attrs 按策略跳过）。"""
    attrs = [_attr("袖型", current="(请选择)", required=False, options=[])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "袖型", "value": "常规袖"}], attrs)
    assert valid == []
    assert "按策略留空" in rejected[0]["rejectReason"]


def test_非必填未填即使有options也拒():
    """2026-08-24 起口径：非必填未填一律留空，源商品写了也不填。

    这条守的是闸门【不看 options 只看必填与否】：探查场景 required_only=False 会给
    所有行读上 options，旧写法（options 非空即放行）在那种场景等于把闸门整个打开。
    """
    attrs = [_attr("图案", current="(请选择)", required=False, options=["纯色", "条纹"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "图案", "value": "纯色"}], attrs)
    assert valid == []
    assert "按策略留空" in rejected[0]["rejectReason"]


def test_非必填但已填的可以改():
    """已有值说明平台/采集填过，与商品矛盾时该纠正——不属于「多填」。"""
    attrs = [_attr("袖型", current="泡泡袖", required=False, options=["常规袖", "泡泡袖"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "袖型", "value": "常规袖"}], attrs)
    assert len(valid) == 1
    assert rejected == []


def test_表单没有的属性行被拒():
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "不存在的属性", "value": "x"}], [_attr("织造方式", options=["梭织"])])
    assert valid == []
    assert "表单没有这个属性行" in rejected[0]["rejectReason"]


def test_成分合计100_原样通过():
    attrs = [_attr("上装成分", current="棉",
                   options=["棉", "聚酯纤维(涤纶）", "氨纶"])]
    valid, *_ = _validate_attr_changes([
        {"label": "上装成分", "value": "棉", "num": 55, "row": 1},
        {"label": "上装成分", "value": "聚酯纤维(涤纶）", "num": 45, "row": 2},
    ], attrs)
    assert sum(c["num"] for c in valid) == 100
    assert len(valid) == 2


def test_成分不足100_自动补聚酯纤维():
    attrs = [_attr("上装成分", current="棉",
                   options=["棉", "聚酯纤维(涤纶）", "氨纶"])]
    valid, *_ = _validate_attr_changes([
        {"label": "上装成分", "value": "棉", "num": 90, "row": 1},
    ], attrs)
    assert sum(c["num"] for c in valid) == 100
    filler = [c for c in valid if c["value"] == "聚酯纤维(涤纶）"]
    assert len(filler) == 1
    assert filler[0]["num"] == 10
    assert filler[0]["row"] == 2, "补差行必须排在已有行之后"


def test_成分不足100_无聚酯纤维时退到氨纶():
    attrs = [_attr("上装成分", current="棉", options=["棉", "氨纶"])]
    valid, *_ = _validate_attr_changes([
        {"label": "上装成分", "value": "棉", "num": 95, "row": 1},
    ], attrs)
    assert sum(c["num"] for c in valid) == 100
    assert any(c["value"] == "氨纶" and c["num"] == 5 for c in valid)


def test_成分不足100_无填充纤维时整组拒():
    attrs = [_attr("上装成分", current="棉", options=["棉", "羊毛"])]
    valid, rejected, _ = _validate_attr_changes([
        {"label": "上装成分", "value": "棉", "num": 80, "row": 1},
    ], attrs)
    assert valid == [], "补不齐 100% 就不能写入，否则平台校验必拦"
    assert any("无可用填充纤维" in r.get("rejectReason", "") for r in rejected)


def test_成分超过100_整组拒():
    attrs = [_attr("上装成分", current="棉",
                   options=["棉", "聚酯纤维(涤纶）"])]
    valid, rejected, _ = _validate_attr_changes([
        {"label": "上装成分", "value": "棉", "num": 70, "row": 1},
        {"label": "上装成分", "value": "聚酯纤维(涤纶）", "num": 50, "row": 2},
    ], attrs)
    assert valid == []
    assert any(">100%" in r.get("rejectReason", "") for r in rejected)


def test_多行按row升序排列():
    """先覆盖第 1 行再加新行，否则加行时行号对不上。"""
    attrs = [_attr("上装成分", current="棉",
                   options=["棉", "聚酯纤维(涤纶）", "氨纶"])]
    valid, *_ = _validate_attr_changes([
        {"label": "上装成分", "value": "氨纶", "num": 5, "row": 3},
        {"label": "上装成分", "value": "棉", "num": 60, "row": 1},
        {"label": "上装成分", "value": "聚酯纤维(涤纶）", "num": 35, "row": 2},
    ], attrs)
    rows = [c["row"] for c in valid]
    assert rows == sorted(rows), f"row 未升序: {rows}"


def test_成分校验不影响无num的普通字段():
    """只有带 num 的成分类才走 100% 校验，普通下拉不该被牵连。"""
    attrs = [_attr("织造方式", current="梭织", options=["梭织", "针织"]),
             _attr("上装成分", current="棉", options=["棉", "聚酯纤维(涤纶）"])]
    valid, *_ = _validate_attr_changes([
        {"label": "织造方式", "value": "针织"},
        {"label": "上装成分", "value": "棉", "num": 100, "row": 1},
    ], attrs)
    assert any(c["label"] == "织造方式" for c in valid)
    assert sum(c.get("num") or 0 for c in valid) == 100


# ---- 源主面料成分含量的确定性覆盖（2026-08-21）--------------------------------
# 背景：成分百分比原先完全由 LLM 单次判断决定，两次调用会从 55/45 漂到 90/10。
# 源页面的「主面料成分含量」是确定事实（如 "90%（含）-95%（不含）（%）"），
# 必须由程序按源值写入，模型只保留「配料纤维选哪种」这个定性判断。

_FIBER_OPTS = ["棉", "聚酯纤维(涤纶）", "氨纶", "羊毛"]


def test_含量区间取下界():
    """区间取下界：不虚标含量（合规），且必然给补差留出份额。"""
    r = parse_main_composition({"主面料成分": "聚酯纤维（涤纶）",
                                "主面料成分含量": "90%（含）-95%（不含）（%）"})
    assert r["percent"] == 90
    assert r["fiber"] == "聚酯纤维（涤纶）"


def test_含量单值形态():
    r = parse_main_composition({"主面料成分": "棉", "主面料成分含量": "55（%）"})
    assert r == {"fiber": "棉", "percent": 55, "raw": "55（%）", "fiberText": "棉"}


def test_含量缺失时按单一成分100():
    """源给了纤维但没给含量：该纤维 100%，不再返回空让模型去编比例
    （2026-08-25 用户结论）。0% / 非法百分比同样视为「没写」。"""
    for raw in (None, "（%）", "0%"):
        attrs = {"主面料成分": "棉"}
        if raw is not None:
            attrs["主面料成分含量"] = raw
        r = parse_main_composition(attrs)
        assert r["fiber"] == "棉" and r["percent"] == 100
        assert r.get("assumed")


def test_连纤维都没有时按默认聚酯纤维100():
    """源什么都没写：聚酯纤维 100%（跨境服装最常见面料），并打 assumed 标记。"""
    r = parse_main_composition({})
    assert r["fiber"] == COMP_DEFAULT_FIBER == "聚酯纤维"
    assert r["percent"] == 100 and r.get("assumed")


def test_主成分按源值覆盖模型漂移的百分比():
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 55, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 45, "row": 2}]
    valid, *_ = _validate_attr_changes(
        changes, attrs,
        {"fiber": "聚酯纤维（涤纶）", "percent": 90, "raw": "90%（含）-95%（不含）（%）"})
    assert valid[0]["value"] == "聚酯纤维(涤纶）", "第1行纤维须为源主纤维"
    assert valid[0]["num"] == 90, "百分比须为源含量，不采纳模型给的 55"
    assert sum(c["num"] for c in valid) == 100


def test_源主纤维全角括号能对上表单半角写法():
    """源写「聚酯纤维（涤纶）」，表单 options 是「聚酯纤维(涤纶）」，直接比必不等。"""
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    valid, *_ = _validate_attr_changes(
        [{"label": "上装成分", "value": "棉", "num": 100, "row": 1}], attrs,
        {"fiber": "聚酯纤维（涤纶）", "percent": 95, "raw": "95%"})
    assert valid[0]["value"] == "聚酯纤维(涤纶）"


def test_主成分100时不加补差行():
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    valid, *_ = _validate_attr_changes(
        [{"label": "上装成分", "value": "棉", "num": 60, "row": 1}], attrs,
        {"fiber": "棉", "percent": 100, "raw": "100%"})
    assert len(valid) == 1 and valid[0]["num"] == 100


def test_补差沿用模型选的配料纤维种类():
    """数值由源定，配料【种类】仍听模型——它看过图和面料工艺，比写死候选表准。"""
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 70, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 30, "row": 2}]
    valid, *_ = _validate_attr_changes(
        changes, attrs, {"fiber": "棉", "percent": 95, "raw": "95%"})
    assert [c["value"] for c in valid] == ["棉", "氨纶"]
    assert [c["num"] for c in valid] == [95, 5]


def test_源主纤维不在options时退回老路径():
    """竹纤维这类 options 里没有的，不硬凑；按模型给数走合计校验。"""
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 55, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 45, "row": 2}]
    valid, rejected, _ = _validate_attr_changes(
        changes, attrs, {"fiber": "竹纤维", "percent": 90, "raw": "90%"})
    assert [c["num"] for c in valid] == [55, 45]
    assert rejected == []


def test_里衬成分不受源主面料含量约束():
    """里衬是另一块布料，与主面料含量无关，必须走自己的合计校验。"""
    attrs = [_attr("里衬成分", current="棉", options=_FIBER_OPTS)]
    valid, *_ = _validate_attr_changes(
        [{"label": "里衬成分", "value": "棉", "num": 100, "row": 1}], attrs,
        {"fiber": "聚酯纤维（涤纶）", "percent": 90, "raw": "90%"})
    assert valid[0]["value"] == "棉" and valid[0]["num"] == 100


def test_补差纤维不与主纤维重复():
    """主成分本身就是聚酯纤维时再补一行聚酯纤维，同字段两行同纤维必被平台拦。"""
    attrs = [_attr("上装成分", current="棉", options=_FIBER_OPTS)]
    valid, *_ = _validate_attr_changes(
        [{"label": "上装成分", "value": "聚酯纤维(涤纶）", "num": 90, "row": 1}], attrs)
    assert sum(c["num"] for c in valid) == 100
    fibers = [c["value"] for c in valid]
    assert len(set(fibers)) == len(fibers), f"补差与主纤维重复: {fibers}"


def test_主成分需补差但无可用配料时整组拒():
    attrs = [_attr("上装成分", current="羊毛", options=["羊毛"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "上装成分", "value": "羊毛", "num": 100, "row": 1}], attrs,
        {"fiber": "羊毛", "percent": 90, "raw": "90%"})
    assert valid == []
    assert any("无可用配料纤维" in r.get("rejectReason", "") for r in rejected)


# ---- 数值输入型属性行（里料克重 g/m² 等） ----------------------------------
#
# 2026-08-24 用户截图的自动化断点：这类行没有下拉、options 恒为空，原先
#   1) dump_attrs 枚举时被 `!it.querySelector('.ant-select')` 整行跳过
#   2) 就算给了值也过不了 `value in options` 那道闸
# 结果该行永远空着，保存报「请输入产品属性」，整条自动化断掉。

def _num_attr(label, current="(请输入)", required=True, unit="g/m²"):
    """构造一个数值输入型属性行（kind=number，无 options）。"""
    return {"label": label, "current": current, "required": required,
            "kind": "number", "options": [], "numValues": [],
            "numHint": {"placeholder": "", "unit": unit, "value": ""}}


def test_数值行接受纯数字():
    attrs = [_num_attr("里料克重")]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里料克重", "value": "80"}], attrs)
    assert rejected == []
    assert len(valid) == 1
    assert valid[0]["value"] == "80"
    assert valid[0]["kind"] == "number"


def test_数值行剥掉单位():
    """LLM 常带单位（"120g/m²"），输入框只收数字，必须剥。"""
    attrs = [_num_attr("里料克重")]
    valid, *_ = _validate_attr_changes(
        [{"label": "里料克重", "value": "120g/m²"}], attrs)
    assert valid[0]["value"] == "120"


def test_数值行不走options闸():
    """关键回归：options 为空时数值行必须放行，不能被「不在 options 内」拒掉。

    这正是原先的断点——走 options 闸的话每个值都会被拒，该行永远填不上。
    """
    attrs = [_num_attr("里料克重")]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里料克重", "value": "75"}], attrs)
    assert valid, "数值行被 options 闸拦住了，自动化会断在保存"
    assert not any("options" in r.get("rejectReason", "") for r in rejected)


def test_数值行拒非数字():
    attrs = [_num_attr("里料克重")]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里料克重", "value": "适中"}], attrs)
    assert valid == []
    assert "不是数字" in rejected[0]["rejectReason"]


@pytest.mark.parametrize("bad", ["0", "-5", "999999"])
def test_数值行拒越界(bad):
    attrs = [_num_attr("里料克重")]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里料克重", "value": bad}], attrs)
    assert valid == [], f"{bad} 应被量级闸拦下"


def test_数值行取num字段():
    """LLM 可能把数值放 num 而不是 value，两处都要认。"""
    attrs = [_num_attr("里料克重")]
    valid, *_ = _validate_attr_changes(
        [{"label": "里料克重", "value": "", "num": 90}], attrs)
    assert valid and valid[0]["value"] == "90"


def test_数值行非必填未填仍拒():
    """数值行也受「非必填未填一律留空」策略约束——分流不能绕过第 1 闸。"""
    attrs = [_num_attr("含绒量", required=False)]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "含绒量", "value": "80"}], attrs)
    assert valid == []
    assert "按策略留空" in rejected[0]["rejectReason"]


def test_下拉行不受数值分流影响():
    """回归：kind 缺省或为 select 时仍走 options 闸。"""
    attrs = [_attr("织造方式", options=["梭织", "针织"])]
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "织造方式", "value": "88"}], attrs)
    assert valid == []
    assert "不在 options 内" in rejected[0]["rejectReason"]


# ---- 源没写成分时的确定性默认（2026-08-25 用户结论）--------------------------
# 结论原文：「成分，网站里一般只有一个含量，剩下的要靠猜；它如果没有写就全部写
# 聚酯纤维 100%，如果有写就按商品信息来」。落点是 parse_main_composition 给出
# percent=100 的单行结果，_rebuild_main_comp 据此单行写入、不进补差分支。

def test_源无成分信息时整组写默认纤维100():
    """源属性里连主面料成分都没有：整组按聚酯纤维 100%，不采纳模型编的两行比例。"""
    attrs = [_attr("上装成分", current="(请选择)", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 60, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 40, "row": 2}]
    valid, rejected, _ = _validate_attr_changes(
        changes, attrs, parse_main_composition({}))
    assert len(valid) == 1, "默认规则下只写一行，不该留下模型编的第二行"
    assert valid[0]["value"] == "聚酯纤维(涤纶）"
    assert valid[0]["num"] == 100
    assert rejected == []


def test_源有纤维但没写含量时该纤维100():
    """源写了「棉」却没给含量：棉 100%，不去猜剩余份额。"""
    attrs = [_attr("上装成分", current="(请选择)", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 70, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 30, "row": 2}]
    valid, *_ = _validate_attr_changes(
        changes, attrs, parse_main_composition({"主面料成分": "棉"}))
    assert [c["value"] for c in valid] == ["棉"]
    assert [c["num"] for c in valid] == [100]


def test_源有含量时仍按源值补差():
    """有含量就按商品信息来：90% 源值 + 10% 补差，仍是两行。"""
    attrs = [_attr("上装成分", current="(请选择)", options=_FIBER_OPTS)]
    changes = [{"label": "上装成分", "value": "棉", "num": 50, "row": 1},
               {"label": "上装成分", "value": "氨纶", "num": 50, "row": 2}]
    valid, *_ = _validate_attr_changes(
        changes, attrs,
        parse_main_composition({"主面料成分": "棉", "主面料成分含量": "90%"}))
    assert [c["num"] for c in valid] == [90, 10]
    assert valid[0]["value"] == "棉"


def test_默认纤维不与补差候选撞成同一根():
    """默认主成分是聚酯纤维时 pct=100，不进补差分支，天然不会出现两行同纤维。"""
    attrs = [_attr("上装成分", current="(请选择)", options=_FIBER_OPTS)]
    valid, *_ = _validate_attr_changes(
        [{"label": "上装成分", "value": "棉", "num": 90, "row": 1}],
        attrs, parse_main_composition({}))
    values = [c["value"] for c in valid]
    assert len(values) == len(set(values)) == 1


# ---- 行定位不许把属性名拼进 CSS 选择器 ---------------------------------------

def test_下拉侧五处定位不拼CSS属性字面量():
    """回归钉子（2026-08-25）：属性名带全角括号/引号会让 querySelector 抛 SyntaxError。

    数值行那处已因「里料克重（g/m²)」实测炸过（阶段④异常、商品未落库）；下拉侧原先
    靠给属性值补一对引号侥幸躲过全角括号，但名字里真出现 " 同样抛错、出现 \ 则静默
    miss。属性名由平台下发，故五处一律改成枚举 [data-attr-label] 再按值比对。

    【2026-09-11 收拢到一处】读/点/滚动点三处的「行定位」原先各自内联一份，改成按
    aria 关联取本行浮层时合并成了 _js_own_panel；它们仨的源码里已不含定位语句，故
    改查那个共享函数。判据本身（不许拼选择器、必须枚举比对）一字未改。
    """
    import inspect
    from app.publish import pipeline as P
    from app.publish.attributes import dropdowns as D
    srcs = [inspect.getsource(f) for f in (
        P._visible_dropdown_near, P._open_attr_dropdown, D._js_own_panel)]
    for src in srcs:
        assert "[data-attr-label=' + " not in src, "不许把标签拼进选择器"
        assert "querySelectorAll('.ant-form-item[data-attr-label]')" in src
        assert "getAttribute('data-attr-label') === __LABEL__" in src
        # J('"' + label + '"') 这种「给属性值补引号」的写法不该再出现
        assert "J('\"' + label + '\"')" not in src


# ---- 成分行漏 num 时必须补满 100% ---------------------------------------------

def _comp_row(label="里衬成分", opts=None):
    """枚举出的「纤维 + 百分比」复合行（hasPercent=True 是结构判据）。"""
    return {"label": label, "required": True, "current": "(请选择)",
            "kind": "select", "hasPercent": True,
            "options": opts or ["棉Cotton", "聚酯纤维(涤纶）", "氨纶"]}


def test_成分行漏num按独占补满():
    """回归钉子（2026-08-25 用户截图）：纤维选上了、百分比空着，平台报「请完善里衬成分信息」。

    原因是分组判据写成 `if c.get("num")`——模型漏给 num 时该行不进分组，「合计=100」
    闸整个不执行，set_attr 的 `num is not None` 也不成立、百分比框不填。
    """
    for ch in ({"label": "里衬成分", "value": "棉Cotton"},
               {"label": "里衬成分", "value": "棉Cotton", "num": None},
               {"label": "里衬成分", "value": "棉Cotton", "num": 0}):
        valid, rejected, _ = _validate_attr_changes([dict(ch)], [_comp_row()], None)
        assert not rejected, f"不该驳回：{rejected}"
        assert len(valid) == 1, f"不该另补一根纤维：{valid}"
        assert valid[0]["value"] == "棉Cotton", "模型选中的纤维必须保住"
        assert valid[0]["num"] == 100, f"该行要独占 100%，实际 {valid[0]['num']}"


def test_成分行多行漏num则均分():
    """两行都没给含量时均分，余数给第一行，合计必须精确等于 100。"""
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里衬成分", "value": "棉Cotton", "row": 1},
         {"label": "里衬成分", "value": "氨纶", "row": 2}],
        [_comp_row()], None)
    assert not rejected
    assert sum(v["num"] for v in valid) == 100
    assert {v["value"] for v in valid} == {"棉Cotton", "氨纶"}


def test_成分行给了num仍走原补差():
    """已有 num 的行不受影响：60% 仍按原逻辑补一根填充纤维凑 100。"""
    valid, *_ = _validate_attr_changes(
        [{"label": "里衬成分", "value": "棉Cotton", "num": 60}], [_comp_row()], None)
    assert sum(v["num"] for v in valid) == 100
    got = {v["value"]: v["num"] for v in valid}
    assert got["棉Cotton"] == 60, "模型给的含量不许被改写"


def test_纯下拉行不被当成成分行():
    """里料纹理这类没有百分比框的行，hasPercent 为假，不该被塞进成分分组补 num。"""
    plain = {"label": "里料纹理", "required": True, "current": "(请选择)",
             "kind": "select", "options": ["光面", "绒面/PU", "无里料/无内衬"]}
    valid, rejected, _ = _validate_attr_changes(
        [{"label": "里料纹理", "value": "光面"}], [plain], None)
    assert not rejected and len(valid) == 1
    assert valid[0].get("num") is None, "纯下拉行不该被补出 num"


def test_枚举给出hasPercent结构信号():
    """行是不是成分行必须由 DOM 结构决定，不能由模型输出的完整性决定。"""
    from app.publish import pipeline
    js = pipeline._JS_LIST_ATTR_ROWS
    assert "hasPercent" in js
    # 判据：下拉打头（非数值行）且其后还有可填 input
    assert "!firstIsInput" in js and "seq.slice(1).some(el => el.tagName === 'INPUT')" in js
