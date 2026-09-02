"""阶段⑩ 申报价与包裹尺寸的单测（2026-08-25 用户结论落地）。

两条结论都是「确定性优先于模型」：
  - 包裹尺寸：服装压平装快递袋，规格固定 30x25x3，不逐商品问模型；玩具等有刚性
    包装的品类才交模型按实际体积估。
  - 申报价：默认 188.88，UI/CLI 可覆盖；非法输入退默认值而不是让它写进申报字段。

回归价值在「服装类真的没调 LLM」——这类接线一旦失效就是静默的（模型照样返回一组
数、流程照样绿），只能靠断言 ask_json 没被调用来证明。参照 dimsCm 那个死键的教训。
"""
import json

import pytest

from app.publish import pipeline
from app.publish.pipeline import (
    DECLARE_PRICE_DEFAULT,
    _APPAREL_DIMS,
    _is_apparel,
    normalize_declare_price,
)


# ---- 服装类判定 -----------------------------------------------------------

def test_apparel_by_cat_path():
    """类目路径优先：那是编辑页已生效的真实类目。"""
    assert _is_apparel(["童装/婴幼装", "上装", "卫衣"], "")
    assert _is_apparel(["女装", "连衣裙"], "")


def test_apparel_by_title_when_cat_missing():
    """类目为空（续跑没回填）时退到标题判定。"""
    assert _is_apparel(None, "儿童秋冬加厚卫衣两件套")
    assert _is_apparel([], "女士针织长裤")


def test_non_apparel_needs_real_volume():
    """玩具/家居等按实际体积算的品类不能命中服装分支。"""
    assert not _is_apparel(["玩具", "积木"], "儿童益智积木套装")
    assert not _is_apparel(None, "不锈钢保温杯 500ml")
    assert not _is_apparel(["家居用品", "收纳"], "折叠收纳箱")


def test_cat_path_wins_over_title():
    """类目说是玩具就按玩具走，哪怕标题里带「套装」这类服装词。"""
    assert not _is_apparel(["玩具", "毛绒玩具"], "毛绒玩偶套装")


def test_套装不算服装词():
    """「套装」跨品类通用（积木套装/餐具套装），单靠它不能判服装——
    否则类目缺失时会把彩盒装的品类固定成 30x25x3。"""
    assert not _is_apparel(None, "儿童益智积木套装 200 颗")
    assert not _is_apparel(None, "陶瓷餐具套装 6 件")
    # 品类词在时仍判服装（「卫衣两件套」靠的是「卫衣」而不是「套」）
    assert _is_apparel(None, "儿童卫衣两件套")


def test_排除词优先于服装词():
    """标题同时含服装词和刚性包装品类词时按非服装走：宁可多问一次模型。"""
    assert not _is_apparel(None, "鞋袜收纳盒 家用防尘")
    assert not _is_apparel(["玩具"], "过家家玩具服装道具")
    assert not _is_apparel(None, "儿童保温杯 卡通款")


# ---- 申报价归一 -----------------------------------------------------------

def test_price_default_when_blank():
    """UI 留空 / CLI 不给 → 默认 188.88。"""
    assert normalize_declare_price("") == "188.88"
    assert normalize_declare_price(None) == "188.88"
    assert normalize_declare_price("   ") == DECLARE_PRICE_DEFAULT


def test_price_keeps_user_value():
    assert normalize_declare_price("99") == "99"
    assert normalize_declare_price("24.5") == "24.5"
    assert normalize_declare_price("188.88") == "188.88"


def test_price_strips_currency_and_trailing_zeros():
    """带币种符号、多余小数位都要归一：不归一会让页面回读比对全判成 bad。"""
    assert normalize_declare_price("￥188") == "188"
    assert normalize_declare_price("188.0") == "188"
    assert normalize_declare_price("188.888") == "188.89"


def test_price_illegal_falls_back_to_default():
    """非法值退默认而不是抛异常：定价填错不该让整个商品发布中断。"""
    for bad in ("abc", "0", "-5", "999999"):
        assert normalize_declare_price(bad) == DECLARE_PRICE_DEFAULT


# ---- set_variant 取值优先级（不真连浏览器） -------------------------------

class _FakeSession:
    """只收 eval_json 的假会话：把 JS 里的常量原样回读成「填写成功」。"""

    def __init__(self):
        self.js = ""

    async def eval_json(self, js):
        self.js = js
        return {"count": 3, "bad": [], "sample": []}

    async def wait_for(self, code, predicate, timeout=30, interval=1.5):
        """变种表渲染等待：假会话里表头视为已齐备（真站竞态见 set_variant 的说明）。"""
        return {"ready": True, "heads": ["申报价格 (CNY)", "尺寸(cm)", "重量(g)"]}


def _info(tmp_path, **over):
    info = {"title": "儿童加厚卫衣两件套", "packInfo": {}, **over}
    p = tmp_path / "product-info.json"
    p.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.mark.asyncio
async def test_apparel_dims_skip_llm(tmp_path, monkeypatch):
    """服装类：尺寸取固定值，且【一次 LLM 都不能调】（重量走源 packInfo）。"""
    called = []

    async def _boom(*a, **kw):
        called.append(kw.get("what"))
        raise AssertionError("服装类不该调用 LLM 估算包装尺寸")

    monkeypatch.setattr(pipeline, "ask_json", _boom, raising=False)
    import app.publish.llm as pub_llm
    monkeypatch.setattr(pub_llm, "ask_json", _boom)

    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, packInfo={"unitWeightKg": 0.42}),
        cat_path=["童装/婴幼装", "上装"])
    assert r["status"] == "ok"
    assert r["dims"] == list(_APPAREL_DIMS) == ["30", "25", "3"]
    assert r["weight"] == "420"
    assert not called


@pytest.mark.asyncio
async def test_apparel_dims_ignore_source_packinfo(tmp_path, monkeypatch):
    """源 packInfo 给了整箱口径的 dimsCm 也不采信：服装固定规格优先。"""
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, packInfo={"unitWeightKg": 0.4, "dimsCm": [60, 40, 30]}),
        cat_path=["女装", "卫衣"])
    assert r["dims"] == ["30", "25", "3"]


@pytest.mark.asyncio
async def test_explicit_dims_win_over_apparel(tmp_path, monkeypatch):
    """显式 dims 参数最高优先：人工核过实物时要能压过固定规格。"""
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, packInfo={"unitWeightKg": 0.4}),
        dims="35x28x6", cat_path=["童装"])
    assert r["dims"] == ["35", "28", "6"]


@pytest.mark.asyncio
async def test_non_apparel_asks_llm(tmp_path, monkeypatch):
    """非服装类（玩具）：源没给尺寸就交 LLM 按实际体积估，且提示词要提包装形式。"""
    seen = {}

    async def _ask(prompt, what="", stage="", **kw):
        seen["prompt"] = prompt
        return {"长": 22, "宽": 18, "高": 12, "重量": 650}

    import app.publish.llm as pub_llm
    monkeypatch.setattr(pub_llm, "ask_json", _ask)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, title="儿童益智积木套装 200 颗"),
        cat_path=["玩具", "积木"])
    # 尺寸原样采用模型给的值；重量被材积重量闸上调（22x18x12÷6 = 792g > 650g），
    # 见 set_variant 里那段平台硬校验的说明——这里断言的是「模型的尺寸没被改动」。
    assert r["dims"] == ["22", "18", "12"] and r["weight"] == "793"
    # 提示词必须让模型自己判包装形式：写死「快递袋」会把彩盒类的高估成 3~5cm
    assert "包装形式" in seen["prompt"] and "刚性包装" in seen["prompt"]


@pytest.mark.asyncio
async def test_volume_weight_gate_adjusts_weight(tmp_path, monkeypatch):
    """材积重量 > 实际重量时上调重量、尺寸不动（平台硬校验，2026-09-01 真站取证）。

    服装类固定 30x25x3cm 材积重恒为 375g，而宠物衣服源 packInfo 只有 50g，
    必然触发接口报错「材积重量大于实际重量，无法录入」。
    """
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, title="宠物衣服狗狗纱裙", packInfo={"unitWeightKg": 0.05}),
        cat_path=["宠物用品", "狗服饰及配件"])
    assert r["dims"] == ["30", "25", "3"]      # 尺寸不动
    assert r["weight"] == "376"                # 375g 材积重向上取整 +1


@pytest.mark.asyncio
async def test_volume_weight_gate_keeps_compliant_weight(tmp_path, monkeypatch):
    """材积重量已 <= 实际重量时，重量原样不动（闸门不该无条件改值）。"""
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    # 30x25x3 材积重 375g，源重量 800g 已达标
    r = await pipeline.set_variant(
        sess, _info(tmp_path, title="儿童加厚卫衣", packInfo={"unitWeightKg": 0.8}),
        cat_path=["童装"])
    assert r["dims"] == ["30", "25", "3"] and r["weight"] == "800"


@pytest.mark.asyncio
async def test_price_default_and_msrp(tmp_path, monkeypatch):
    """不传 price 时申报价 188.88，建议售价按 ÷7 折算（去掉多余的 0）。"""
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, packInfo={"unitWeightKg": 0.4}), cat_path=["童装"])
    assert r["price"] == "188.88"
    assert r["msrp"] == str(round(188.88 / 7, 2)).rstrip("0").rstrip(".")


@pytest.mark.asyncio
async def test_price_override_normalized(tmp_path, monkeypatch):
    """UI 填了带币种符号的值：写进页面前已归一成纯数字串。"""
    import app.publish.llm as pub_llm

    async def _boom(*a, **kw):
        raise AssertionError("不该调用 LLM")

    monkeypatch.setattr(pub_llm, "ask_json", _boom)
    sess = _FakeSession()
    r = await pipeline.set_variant(
        sess, _info(tmp_path, packInfo={"unitWeightKg": 0.4}),
        price="￥66.0", cat_path=["童装"])
    assert r["price"] == "66"
    assert '"66"' in sess.js


# ---- service 层接线（申报价与类目必须真的传下去）---------------------------
# 这两条是「接线没接上就静默失效」的典型：price 没传下去照样按 188.88 跑完、
# cat_path 没传下去照样有尺寸（只是变成模型估的），日志上都看不出问题。

@pytest.mark.asyncio
async def test_service_传递price与cat_path(monkeypatch):
    """_st_variant 必须把 ctx 里的 price / cat_path 原样交给 set_variant。"""
    from app.publish import service

    seen = {}

    async def _fake_set_variant(session, info_path, price="", dims=None,
                                weight=None, cat_path=None, pack_est=None):
        seen.update(price=price, cat_path=cat_path, info_path=info_path,
                    pack_est=pack_est)
        return {"status": "ok", "price": "99", "dims": ["30", "25", "3"],
                "weight": "420", "msrp": "14.14", "rowCount": 3}

    monkeypatch.setattr(service, "set_variant", _fake_set_variant)

    async def _emit(ev):
        return None

    ctx = {"info_path": "x.json", "price": "99", "cat_path": ["童装", "上装"]}
    r = await service._st_variant(ctx, object(), _emit)
    assert seen["price"] == "99"
    assert seen["cat_path"] == ["童装", "上装"]
    # note 要带上尺寸与重量，否则跑批时无从判断服装分支有没有生效
    assert "申报价 99" in r["note"] and "30x25x3cm" in r["note"]


@pytest.mark.asyncio
async def test_service_不自带默认价(monkeypatch):
    """ctx 没给 price 时透传空串，默认值只由 normalize_declare_price 决定。

    service 侧再写一遍 188.88 就成了第二个真相来源，改口径时必漏一处。
    """
    from app.publish import service

    seen = {}

    async def _fake_set_variant(session, info_path, price="", **kw):
        seen["price"] = price
        return {"status": "ok", "price": DECLARE_PRICE_DEFAULT,
                "dims": ["30", "25", "3"], "weight": "420", "rowCount": 3}

    monkeypatch.setattr(service, "set_variant", _fake_set_variant)

    async def _emit(ev):
        return None

    await service._st_variant({"info_path": "x.json"}, object(), _emit)
    assert seen["price"] == "", "service 不该自己填默认申报价"
