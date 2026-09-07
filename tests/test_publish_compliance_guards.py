"""合规加固逻辑的单测：配料推断、包装量级闸、尺码校验、人群档位。

这四处都是 2026-08-24 按「全自动但数据要站得住」的要求新加的，共同特点是
**失效时静默**（推断拿不到依据就退写死值、校验不生效就照填），所以回归价值高。
参照 dimsCm 那个死键的教训：接线没接上比没写更糟，测试要覆盖「真的拿到依据」。
"""
import pytest

from app.publish.pipeline import (
    _check_measurements,
    _fiber_key,
    _merge_same_fiber,
    _check_pack_est,
    _guess_size_kind,
    _infer_filler,
    _strip_dated,
)


# ---- 配料纤维推断 ---------------------------------------------------------

OPTS = ["聚酯纤维(涤纶）", "氨纶", "棉", "腈纶", "锦纶", "粘纤", "羊毛"]


def test_infer_filler_from_explicit_second_fiber():
    """源属性明写了第二种纤维时，直接用它（最硬依据）。"""
    attrs = {"主面料成分": "棉", "面料辅料": "含氨纶弹力面料"}
    v, why = _infer_filler(attrs, "棉", OPTS, "棉")
    assert v == "氨纶"
    assert "氨纶" in why


def test_infer_filler_from_fabric_feature():
    """源属性只有面料特征（微弹）时，按特征推导到氨纶。"""
    attrs = {"面料": "微弹", "厚薄": "加厚"}
    v, why = _infer_filler(attrs, "棉", OPTS, "棉")
    assert v == "氨纶"
    assert why


def test_infer_filler_fleece_to_polyester():
    """摇粒绒/珊瑚绒推导到聚酯纤维。"""
    attrs = {"面料工艺": "珊瑚绒加厚"}
    v, _ = _infer_filler(attrs, "棉", OPTS, "棉")
    assert v == "聚酯纤维(涤纶）"


def test_infer_filler_excludes_main_fiber():
    """推断结果不能与主纤维相同（同字段两行同纤维平台必拦）。"""
    attrs = {"面料": "纯棉柔软", "主面料成分": "棉"}
    v, _ = _infer_filler(attrs, "棉", OPTS, "棉")
    assert v != "棉"


def test_infer_filler_no_basis_returns_none():
    """毫无依据时返回 None，由调用方退候选表——不能自己编。"""
    attrs = {"货号": "197", "货源类别": "现货"}
    v, why = _infer_filler(attrs, "腈纶", OPTS, "腈纶")
    assert v is None and why is None


def test_infer_filler_handles_empty_attrs():
    assert _infer_filler({}, "棉", OPTS, "棉") == (None, None)
    assert _infer_filler(None, "棉", OPTS, "棉") == (None, None)


# ---- 包装尺寸/重量量级闸 --------------------------------------------------

def test_pack_est_ok():
    est = {"长": 30, "宽": 24, "高": 5, "重量": 400}
    assert _check_pack_est(est, need_dims=True, need_weight=True) == []


def test_pack_est_rejects_all_tiny():
    """模型偶发返回 1x1x1，原先会被原样填进申报字段。"""
    est = {"长": 1, "宽": 1, "高": 1, "重量": 400}
    bad = _check_pack_est(est, need_dims=True, need_weight=True)
    assert any("2cm" in b for b in bad)


def test_pack_est_rejects_out_of_range():
    est = {"长": 500, "宽": 24, "高": 5, "重量": 400}
    assert any("长=500" in b for b in _check_pack_est(est, True, True))
    est2 = {"长": 30, "宽": 24, "高": 5, "重量": 5}
    assert any("重量=5" in b for b in _check_pack_est(est2, True, True))


def test_pack_est_missing_fields():
    assert _check_pack_est({}, need_dims=True, need_weight=False)
    assert _check_pack_est({"长": 1}, need_dims=True, need_weight=False)


def test_pack_est_skips_unneeded():
    """源已给重量时不校验重量列。"""
    est = {"长": 30, "宽": 24, "高": 5}
    assert _check_pack_est(est, need_dims=True, need_weight=False) == []


# ---- 尺码表估算校验 -------------------------------------------------------

SIZES = ["90cm", "100cm", "110cm"]


def test_measurements_ok():
    est = {"90cm": {"衣长": 40, "胸围全围": 62},
           "100cm": {"衣长": 43, "胸围全围": 66},
           "110cm": {"衣长": 46, "胸围全围": 70}}
    assert _check_measurements(est, SIZES, ["衣长", "胸围全围"]) == []


def test_measurements_detects_reverse():
    """后一档小于前一档（反向）必须被抓到。"""
    est = {"90cm": {"衣长": 46}, "100cm": {"衣长": 43}, "110cm": {"衣长": 48}}
    bad = _check_measurements(est, SIZES, ["衣长"])
    assert any("反向" in b for b in bad)


def test_measurements_detects_half_girth():
    """全围填成半围会差一倍，按与衣长的比例抓。"""
    est = {"90cm": {"衣长": 40, "胸围全围": 31},
           "100cm": {"衣长": 43, "胸围全围": 33},
           "110cm": {"衣长": 46, "胸围全围": 35}}
    bad = _check_measurements(est, SIZES, ["衣长", "胸围全围"])
    assert any("半围" in b for b in bad)


def test_measurements_ignores_missing():
    """缺值不算问题（缺值由 lacking 那条路径管）。"""
    est = {"90cm": {"衣长": 40}}
    assert _check_measurements(est, SIZES, ["衣长"]) == []


# ---- 人群档位判断 ---------------------------------------------------------

def test_size_kind_kid_by_cm():
    k = _guess_size_kind("女童毛衣针织衫", ["90cm", "100cm", "110cm"])
    assert "童装" in k["expert"]


def test_size_kind_adult_by_letters():
    """成人女装不能套童装档差（童装按身高 10cm 一档，成人按胸围 4cm）。"""
    k = _guess_size_kind("女士韩版圆领刺绣针织开衫", ["S", "M", "L", "XL"])
    assert "成人" in k["expert"]


def test_size_kind_generic():
    k = _guess_size_kind("针织开衫", ["均码"])
    assert k["expert"] and k["fit"] and k["step"]


def test_size_kind_pet_supply():
    """非服装（宠物窝）走 _NONAPPAREL_KIND 数据映射，返回几何推理身份而非按字母码误判成人。"""
    k = _guess_size_kind("四季通用", ["S", "M", "L"], cat="pet_supply")
    assert k["nonapparel"] is True
    assert "宠物" in k["expert"]


# ---- 尺码表模板名去年份（回归） -------------------------------------------

def test_strip_dated_keeps_season():
    """年份要剥，季节词是真实卖点必须留。"""
    assert _strip_dated("韩女童毛衣2026新款冬季加厚针织衫") == "韩女童毛衣冬季加厚针织衫"


# ---- 纤维同义归一与同行合并（2026-08-24 用户实测报错） -------------------

def test_fiber_key_polyester_synonyms():
    """涤纶 / 聚酯纤维 / 聚酯纤维(涤纶）必须判为同一根纤维。

    _norm_fiber 只去括号，三者归一后互不相等，去重会失效并填出
    「涤纶 80% + 聚酯纤维 20%」——平台必拦，正解是一行 100%。
    """
    keys = {_fiber_key("聚酯纤维(涤纶）"), _fiber_key("聚酯纤维"),
            _fiber_key("涤纶"), _fiber_key("聚酯纤维（涤纶）")}
    assert len(keys) == 1


@pytest.mark.parametrize("a,b", [
    ("氨纶", "莱卡"),
    ("锦纶", "尼龙"),
    ("粘纤", "粘胶纤维"),
    ("蚕丝", "真丝"),
])
def test_fiber_key_other_synonyms(a, b):
    assert _fiber_key(a) == _fiber_key(b)


@pytest.mark.parametrize("a,b", [
    ("棉", "聚酯纤维"),
    ("氨纶", "锦纶"),
    ("羊毛", "羊绒"),
    ("腈纶", "棉"),
])
def test_fiber_key_distinct(a, b):
    """不同纤维不能被误判为同一根（否则会错误合并两种真实成分）。"""
    assert _fiber_key(a) != _fiber_key(b)


def test_merge_same_fiber_polyester_80_20():
    """用户实测的那个 case：涤纶 80% + 聚酯纤维 20% → 一行 100%。"""
    opts = ["聚酯纤维(涤纶）", "氨纶", "棉"]
    items = [{"label": "成分", "value": "聚酯纤维(涤纶）", "num": 80, "row": 1},
             {"label": "成分", "value": "聚酯纤维", "num": 20, "row": 2}]
    out = _merge_same_fiber(items, opts)
    assert len(out) == 1
    assert out[0]["num"] == 100
    # 值必须取 options 里存在的那个写法，否则表单点不中
    assert out[0]["value"] == "聚酯纤维(涤纶）"


def test_merge_same_fiber_keeps_distinct():
    """真正不同的两种纤维必须保留两行，不能被合并。"""
    opts = ["棉", "氨纶"]
    items = [{"label": "成分", "value": "棉", "num": 95, "row": 1},
             {"label": "成分", "value": "氨纶", "num": 5, "row": 2}]
    out = _merge_same_fiber(items, opts)
    assert len(out) == 2
    assert [r["num"] for r in out] == [95, 5]


def test_merge_same_fiber_renumbers_rows():
    """合并后 row 必须连续重排（表单按 row 定位行）。"""
    opts = ["聚酯纤维(涤纶）", "棉"]
    items = [{"label": "成分", "value": "涤纶", "num": 50, "row": 1},
             {"label": "成分", "value": "棉", "num": 30, "row": 2},
             {"label": "成分", "value": "聚酯纤维", "num": 20, "row": 3}]
    out = _merge_same_fiber(items, opts)
    assert [r["row"] for r in out] == [1, 2]
    poly = next(r for r in out if _fiber_key(r["value"]) == _fiber_key("涤纶"))
    assert poly["num"] == 70


def test_merge_single_row_untouched():
    items = [{"label": "成分", "value": "腈纶", "num": 100, "row": 1}]
    out = _merge_same_fiber(items, ["腈纶"])
    assert out == [{"label": "成分", "value": "腈纶", "num": 100, "row": 1}]
