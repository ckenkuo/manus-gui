"""标题合规闸门单测：年份、价格/优惠宣称、以及不该误伤的正常标题。

这些闸门是「提示词软约束 + 校验硬拦」的第二道，回归价值高：
2026-08-24 实测 offer 855602801145 生成了 "Under 10USD"（真实售价约 24USD），
提示词层没拦住，硬闸才是最后防线。
"""
import re

import pytest

# 闸门是 set_titles 的内部闭包，测试里按同一套正则复刻——若实现改了这里会先红，
# 提醒同步（内部闭包无法 import，直接测函数需要跑真站会话）。
_YEAR_PATS = (
    r"(19|20)\d{2}",
    r"\b\d{2}(FW|AW|SS|SP)\b",
    r"New Arrival|Latest|This Year",
)
_PRICE_PATS = (
    r"\$\s*\d",
    r"\d+\s*(USD|usd|dollars?)",
    r"\bunder\s*\d",
    r"\b(cheap|cheapest|budget|bargain|lowest|affordable)\b",
    r"\b(sale|discount|deal|clearance|promo|coupon)\b",
    r"\d+\s*%\s*off|\boff\b\s*\d+\s*%",
    r"\bfree\s*(shipping|delivery|gift)\b",
    r"\b(buy\s*\d+\s*get|bogo)\b",
)


def _has_year(t: str) -> bool:
    return any(re.search(p, t, re.I) for p in _YEAR_PATS)


def _has_price_claim(t: str) -> bool:
    return any(re.search(p, t, re.I) for p in _PRICE_PATS)


@pytest.mark.parametrize("title", [
    "2026 New Girls Knit Sweater Warm Winter Set for Toddler Kids",
    "Girls 26FW Knit Sweater Warm Winter Outfit Value Pack Set Kid",
    "New Arrival Girls Knit Sweater Warm Winter Outfit Pack Set Ki",
    "Latest Toddler Girls Knit Sweater Set Warm Fall Winter Outfit",
])
def test_year_blocked(title):
    assert _has_year(title), f"年份应被拦：{title}"


@pytest.mark.parametrize("title", [
    # 实测踩到的那条：真实售价约 24USD，模型写 Under 10USD
    "2Pcs Toddler Girls Knit Sweater Pants Set Warm Fall Winter Under 10USD",
    "Girls Knit Sweater Set Warm Winter Outfit Under $10 for Kids",
    "Cheap Toddler Girls Knit Sweater Set Warm Fall Winter Outfit",
    "Girls Knit Sweater Set Warm Winter 50% Off Limited Kids Wear",
    "Free Shipping Toddler Girls Knit Sweater Set Warm Fall Winter",
    "Budget Friendly Girls Knit Sweater Set Warm Winter Kids Wear",
    "Girls Knit Sweater Set Warm Winter Outfit Clearance Sale Kids",
])
def test_price_claim_blocked(title):
    assert _has_price_claim(title), f"价格/优惠宣称应被拦：{title}"


@pytest.mark.parametrize("title", [
    # 最终采用的干净标题
    "2Pcs Toddler Girls Knit Sweater Pants Set Warm Fall Winter Kids",
    "Cute Heart Toddler Girls Knit Sweater Set Winter Warm 2Pcs",
    "No More Cold Toddler Girls Thick Knit Sweater Set Fall Winter",
    # 尺码数字 24/26 不是年份，不能误伤
    "Girls Knit Sweater Set Size 24 26 Warm Winter Outfit for Kids",
    # Value Pack / Set of 2 是数量策略，不是价格宣称
    "Value Pack 2Pcs Girls Knit Sweater Warm Winter Outfit for Kid",
])
def test_clean_titles_pass(title):
    assert not _has_year(title), f"不该判为含年份：{title}"
    assert not _has_price_claim(title), f"不该判为价格宣称：{title}"


@pytest.mark.parametrize("zh,blocked", [
    ("2024冬季女童韩版加厚保暖圆领爱心针织衫喇叭裤两件套中小童", True),
    ("女童毛衣2026新款冬季加厚针织衫", True),
    ("女童秋冬加厚针织两件套 包邮特价", True),
    ("女童秋冬加厚针织两件套 圆领爱心毛衣+黑色喇叭裤 中小童保暖洋气", False),
])
def test_zh_title_gate(zh, blocked):
    hit = (_has_year(zh) or _has_price_claim(zh)
           or bool(re.search(r"新款|新品|上新", zh))
           or bool(re.search(r"包邮|免邮|特价|清仓|折扣|秒杀|亏本|甩卖", zh)))
    assert hit == blocked, f"中文闸判定不符预期：{zh}"


def test_strip_dated():
    """_strip_dated 用于尺码表模板名，年份必须剥掉、季节词必须保留。"""
    from app.publish.pipeline import _strip_dated

    assert _strip_dated("韩女童毛衣2026新款冬季加厚针织衫") == "韩女童毛衣冬季加厚针织衫"
    assert _strip_dated("2026年新品女童针织衫") == "女童针织衫"
    # 无年份无新款词时原样返回
    assert _strip_dated("女童加厚毛衣冬季保暖") == "女童加厚毛衣冬季保暖"


# ---- 品牌违禁词表 ------------------------------------------------------------
# 这组用例来自一次真实的连锁失败：2026-08-25 offer 971877978455 阶段⑤ 两次重试全废，
# 报 title-generation-failed。原因是旧实现把源标题前 3 字无条件当品牌词，该商品源标题
# 是「儿童秋冬季徳绒无骨家居服套装…」，于是「儿童秋」进了违禁表——任何正常重写的中文
# 标题都含「儿童」，闸门必然拦死。品牌属性值「无功能保暖」也不是品牌而是功能描述。


def test_描述性假品牌不进违禁表():
    from app.publish.pipeline import _brand_words

    # 「无功能保暖」剔掉非品牌词后不剩实词，判为描述而非品牌
    assert _brand_words("无功能保暖", "儿童秋冬季徳绒无骨家居服套装两件套") == []
    assert _brand_words("其他品牌", "女童冬季加厚针织两件套") == []
    assert _brand_words("", "男童春秋纯棉睡衣套装") == []


def test_中文源标题开头不再当品牌():
    """核心回归：中文标题必须能带「儿童」这类品类词，不能被前 3 字规则拦掉。"""
    from app.publish.pipeline import _brand_words

    words = _brand_words("无功能保暖", "儿童秋冬季徳绒无骨家居服套装两件套")
    zh = "儿童秋冬德绒无骨家居服两件套 亲肤保暖男女童秋衣秋裤"
    assert not any(w in zh for w in words), f"正常中文标题被误拦：{words}"


def test_真品牌仍然拦得住():
    from app.publish.pipeline import _brand_words

    # 有实词的品牌属性值照旧进表
    assert "南极人" in _brand_words("南极人", "儿童秋冬家居服套装")
    # 源标题开头的英文商标 token 也要拦（真商标才会用英文打头）
    words = _brand_words("", "MODAL 儿童秋冬家居服套装")
    assert "MODAL" in words
    # 少于 3 个字符的英文开头不猜（可能是尺码/规格缩写）
    assert _brand_words("", "XL 儿童秋冬家居服套装") == []
