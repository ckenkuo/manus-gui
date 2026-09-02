"""测试尺码表参数名对齐层（_match_param + _PARAM_SYNONYMS）。

2026-09-01 实测取证（offer 999389808041）：源详情图按部件分开给了两张表，
视觉阶段识别完全正确（总衣长 59~79、腰围 48~56），但原有的纯子串对齐让
「测量全围 ←→ 腰围/胸围」与「摆长 ←→ 总衣长」全部落空（两向都不含），
识准的整份源数据被判为一列都没有、改交模型凭空估算，填出摆长 32~44
（源实际 59~79，差近一倍）。

本测试覆盖真站踩的坑 + 回归不该误配的组合。
"""
from app.publish.pipeline import _match_param, _param_stem


def test_param_stem():
    """参数名去空格括注与量法后缀。"""
    assert _param_stem("胸围全围") == "胸围"
    assert _param_stem("胸围") == "胸围"
    assert _param_stem("腰围(橡筋)") == "腰围(橡筋)"
    assert _param_stem("测量全围") == "测量全围"  # 整体是一个参数名，不剥


def test_exact_match_priority():
    """字面相同或剥后缀后相同：最高优先级，同义词表都不查。"""
    # 精确命中直接返回
    assert _match_param("裙长", {"裙长": 50, "衣长": 40}) == "裙长"
    # 剥后缀后相同
    assert _match_param("胸围全围", {"胸围": 60}) == "胸围"
    assert _match_param("胸围", {"胸围全围": 58}) == "胸围全围"


def test_synonym_table():
    """同义词表登记的映射：精确级之后、子串级之前。"""
    # 摆长 ← 总衣长：真站坑，纯子串对不上
    assert _match_param("摆长", {"总衣长": 79, "腰围": 56}) == "总衣长"
    # 测量全围 ← 腰围（下装档）：真站坑，优先级按分类语义排
    assert _match_param("测量全围", {"胸围": 56, "腰围": 48}) == "腰围"
    # 裤长 ← 侧裤长
    assert _match_param("裤长", {"侧裤长": 70}) == "侧裤长"


def test_substring_fallback():
    """子串包含：最后兜底，表里没登记的写法靠它。"""
    # 原有行为要保留：「胸围全围」含「胸围」
    assert _match_param("胸围全围", {"胸围": 60}) == "胸围"
    assert _match_param("上衣长", {"衣长": 45}) == "衣长"


def test_no_false_match():
    """量的不是同一段就不匹配：避免误配导致量级失真。"""
    # 裤内长（裆到脚口）与侧裤长/裤长（腰到脚口）差一个裆深，不能互顶
    assert _match_param("裤内长", {"侧裤长": 70, "裤长": 72}) is None
    # 词表里「裤内长」只认真正的内长量法，故纯子串兜底也匹配不到
    assert _match_param("裤内长", {"内长": 52}) == "内长"
    # 连衣裙长 ← 总衣长：词表登记了，子串兜底也能中
    assert _match_param("连衣裙长", {"总衣长": 80}) == "总衣长"


def test_real_case_999389808041():
    """真站取证 999389808041（连体裤）的两列：原先全部落空，现在都取到源实测值。"""
    src = {"总衣长": 79, "腰围": 56, "胸围": 56}
    assert _match_param("测量全围", src) == "腰围"
    assert _match_param("摆长", src) == "总衣长"
