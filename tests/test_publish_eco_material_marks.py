"""环保声明、材质成分说明、品牌标识三类判据的单测（2026-09-25 新增）。

背景：用户当天收到平台两条违规通知——「商品信息中存在不合规环保声明描述，如
eco-friendly、environmental friendly…」与「商品主图、详情图、SKU图、SKC图等含棉材质
宣传，但属性中材质描述与该材质不匹配，请核查」；另有一条裤子链接因模特鞋上的标识被判
侵权、一条童裤因口袋上的小方标被投诉。三类都并进了既有通路（claims 的词表与四条 rule、
阶段① 标注、图片质检、出图提示词、重试话术），这组测试盯的是它们各自最容易坏的地方：

  - 环保词表最容易出的事是【误伤】：Green 是颜色词、Natural 是正当的客观描述、
    Organic 在服装里是「有机棉」的品类写法，拦掉会把正常商品拦死
    （同 claims._TOP_NOUN_RE 那段「误伤品类词比漏拦一个生僻词更糟」的取向）；
  - 材质词表最容易出的事同样是误伤：包含匹配会把「棉签」「毛绒玩具」判成材质文字，
    英文别名 PET 会命中宠物商品文本里的 pet；
  - materialText / brandMark 是新字段，【必须参与 clean 判定】——只回报不透出、
    或透出了不进 bad，表现都是「质检判干净、带标带材质的图直接上架」；
  - 出图侧与质检侧的口径必须成对：只有出图要求抹、质检不查等于白抹；只有质检判、
    出图没要求就永远过不了。

这组测试全部离线（出图与视觉调用一律 monkeypatch）。
"""
import pytest

from app.publish import claims, images, titles, vision
from app.publish.stages import cleaning_rules


# ---- 环保声明：与「安抚 / PP棉」并进同一份禁词词表 -------------------------------

@pytest.mark.parametrize("text", [
    "Eco-Friendly Cotton Bag",
    "Environmentally Friendly",
    "Environmental Friendly",          # 用户通知里的原话，语法不标准但实测会出现
    "Planet-friendly packaging",
    "eco responsible",
    "Sustainable material",
    "100% Recycled Polyester",
    "Carbon Neutral Shipping",
    "Zero Waste",
    "环保面料",
    "可持续材料",
    "可降解",
    "低碳生活",
])
def test_环保声明中英文两侧都拦(text):
    assert claims.has_banned_term(text) is True


@pytest.mark.parametrize("text", [
    "Green Dress for Women",           # 颜色词，不是环保声明
    "Natural Color",
    "Organic Cotton Tee",              # 有机棉是正当品类写法
    "100% Cotton",                     # 材质名：材质词表只给图片侧用，不进文本闸门
    "Cotton Candy Plush Toy",
    # 下面两类是 review 抓出来的真误伤：都是正当品类/时长语义，拦掉会让标题两次生成
    # 都撞闸、整单发不出去（比漏拦一个词贵得多）
    "Recycling Bin for Kitchen",
    "Recycle Bin",
    "可回收垃圾桶",
    "续航可持续8小时",
    "可持续使用三年",
])
def test_环保词表不误伤颜色与客观描述(text):
    assert claims.has_banned_term(text) is False


@pytest.mark.parametrize("text", [
    "100% Recycled Polyester", "Recyclable Packaging",
    "Sustainable material", "可持续发展", "可持续性", "可回收材料",
])
def test_排除误伤后真环保声明照样拦(text):
    """否定前瞻只排掉已知的非声明搭配，不能把整类词放过。"""
    assert claims.has_banned_term(text) is True


def test_环保声明不算夸大宣传():
    """两类判据刻意分开：处置与日志都要能分清拦的是宣称还是禁词。"""
    assert claims.has_marketing_claim("Eco-Friendly Bag") is False
    assert claims.has_marketing_claim("Best Seller") is True


def test_环保命中词要能被报出来():
    """拒因要带上具体词，否则重试提示词没法把它喂回模型。"""
    assert "Eco-Friendly" in claims.banned_hits("Eco-Friendly Tote Bag")


# ---- 材质成分说明：图片侧词表（只用于质检兜底，不进文本闸门） ---------------------

@pytest.mark.parametrize("text,expect", [
    ("100% Cotton", True),
    ("Cotton 65% Polyester 35%", True),
    ("Soft linen fabric", True),
    ("成分：棉 100%", True),
    ("棉100%", True),
    ("面料：棉", True),
    # 以下都是误伤面：单字纤维名与英文缩写必须卡住上下文/词边界
    ("棉签 500 支装", False),
    ("棉花糖色连衣裙", False),
    ("毛绒玩具 plush toy", False),
    ("pet supplies for dog", False),
    ("item model number", False),
])
def test_材质词兜底判据(text, expect):
    assert bool(claims.material_hits(text)) is expect


def test_材质判据不进文本闸门():
    """标题、属性、描述里写材质名是正当的（材质属性填的就是它），文本侧拦等于拦死正常商品。"""
    assert claims.has_banned_term("100% Cotton T-Shirt") is False


# ---- 质检：两个新字段必须参与 clean 判定并透出、参与重烧历史 ---------------------

def _qc_response(**over):
    """一份字段齐全的质检响应（五个老布尔必答，否则会被判 error）。"""
    resp = {"residualChinese": False, "garbled": False, "watermark": False,
            "brokenSubject": False, "marketingClaim": False, "issues": ""}
    resp.update(over)
    return resp


@pytest.mark.asyncio
async def test_图上有材质说明判不过(monkeypatch):
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        assert "materialText" in prompt and "brandMark" in prompt
        return _qc_response(materialText=True, materialTexts=["100% Cotton"])
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False
    assert r["materialText"] is True
    assert r["materialTexts"] == ["100% Cotton"]
    assert "材质" in r["issues"]


@pytest.mark.asyncio
async def test_实物品牌标识判不过(monkeypatch):
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        return _qc_response(brandMark=True, brandMarkTexts=["Levi's"])
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and r["brandMark"] is True
    assert "品牌标识" in r["issues"]


@pytest.mark.asyncio
async def test_材质说明漏答为false时按原文翻严(monkeypatch):
    """模型布尔会抖，但它一旦列出原文，这件事就退化成确定性匹配（同 bannedTerm 的兜底）。"""
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        return _qc_response(materialText=False, materialTexts=["Cotton 65% Polyester 35%"])
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and r["materialText"] is True


@pytest.mark.asyncio
async def test_新字段漏答不判error(monkeypatch):
    """漏答当没有——进必答清单会让老响应漏答直接判 error、整批图退回带中文的原图。"""
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        return _qc_response()
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["status"] == "ok" and r["clean"] is True
    assert r["materialText"] is False and r["brandMark"] is False


def test_阶段标注的材质说明算脏图():
    """⑥⑦ 那条路不跑 check_cleaned，标注是它们唯一的判据。"""
    assert vision.is_dirty({"material": True}) is True
    assert vision.is_dirty({"clean": True}) is False
    # 排序键越小越优：材质说明比重复图严重（兜底时不选它），但比 logo 轻
    assert vision._dirty_score({"material": True}) > vision._dirty_score({"duplicate": True})
    assert vision._dirty_score({"material": True}) < vision._dirty_score({"logo": True})


@pytest.mark.asyncio
async def test_模型把环保声明归到宣称也要拦住(monkeypatch):
    """降级逻辑只查夸大宣传词表，会把归错类的禁词判成「模型过判」放行。

    阶段① 的 claim 判据里写着「环保声明也算」（extract._VISION_PROMPT_DETAIL），
    模型据此把 Eco-Friendly 填进 marketingClaimTexts、bannedTerm 填 false。若降级块
    只看 _claim_word_in，一个确定性命中的禁词（eco-friendly 就在词表里）会被放行，
    且 banned/material 都是 false → clean=True → 图原样上架——新加的闸被自己的降级
    逻辑绕过去。故降级前必须先按举证原文把禁词/材质两类翻严。
    """
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        return _qc_response(marketingClaim=True,
                            marketingClaimTexts=["Eco-Friendly"],
                            bannedTerm=False, issues="Eco-Friendly 属夸大宣传")
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and r["bannedTerm"] is True


@pytest.mark.asyncio
async def test_模型把材质说明归到宣称也要拦住(monkeypatch):
    """材质词同理：模型会把「100% Cotton」当成宣称填进 marketingClaimTexts。"""
    async def _fake(prompt, images_, what="", system=None, stage=None, **kw):
        return _qc_response(marketingClaim=True,
                            marketingClaimTexts=["100% Cotton"],
                            materialText=False, issues="品质宣称")
    monkeypatch.setattr(vision, "ask_json_with_images", _fake)
    r = await vision.check_cleaned("x.jpg")
    assert r["clean"] is False and r["materialText"] is True


def test_质检提示词要求的布尔个数与实际一致():
    """提示词写「六个」而骨架列了八个时，模型可能只填六个，被牺牲的恰是排在最后的
    materialText/brandMark——正是本次新增的两个判据（这条路径不依赖模型归类错误）。"""
    import inspect
    import re
    src = inspect.getsource(vision.check_cleaned)
    assert "八个布尔字段必须全部填写" in src
    listed = set(re.findall(
        r'"(residualChinese|garbled|watermark|brokenSubject|marketingClaim|'
        r'bannedTerm|materialText|brandMark)":', src))
    assert len(listed) == 8


def test_品牌与材质写进重烧历史():
    """调用方靠这些字段决定重烧话术说哪一类。"""
    text = vision._retry_history_text([
        {"attempt": 1, "issues": "图上有品牌标识", "brandMark": True,
         "brandMarkTexts": ["Levi's"], "materialText": True,
         "materialTexts": ["100% Cotton"], "hint": ""},
    ])
    assert "品牌标识" in text and "材质说明" in text
    assert "Levi's" in text and "100% Cotton" in text


# ---- 出图侧与重试话术：口径必须与质检成对 ----------------------------------------

def test_出图提示词不再把材质成分列为翻译保留():
    """2026-09-25 口径变更：图上材质会与属性里填的材质对不上，平台判违规。

    两条口径要成对改：老话术「商品实物上的印花、刺绣、织标要原样保留」现在只保留
    装饰性的那一半，品牌标识与材质说明改为要抹——只改一半会让出图与质检打架。
    """
    for p in (images.DEFAULT_TRANSLATE_PROMPT, images.DEFAULT_CLEAN_PROMPT,
              images.SIZECHART_TRANSLATE_PROMPT):
        assert claims.MARK_REMOVE_RULE in p
        assert "材质成分、工艺" not in p        # 老的「翻译保留」清单里不该再有它
        assert "织标要原样保留" not in p        # 老口径：实物上的一切都保留


def test_图上PP棉不再改名保留():
    """两条规则打架会让图永远过不了质检，必须由源头消歧。

    PP棉原先要求改写成规范材质名 Polyester Fiber（填充物是买家关心的信息），但改名
    产物仍是一条材质声明：既照样与属性里填的成分对不上，又必被 materialText 判脏——
    模型按 A 条改名、质检按 B 条判脏，重烧四发全栽在同一处、图被丢弃。
    2026-09-25 起图片侧统一为抹掉；属性侧（表单里那行填充物成分）仍走同义 option 替换，
    那是另一条链路，不受影响。
    """
    assert "也不要改写成 Polyester Fiber" in claims.BANNED_REMOVE_RULE
    hint = cleaning_rules._retry_hint("图上有 PP棉", cjk=False, banned=True)
    assert "也不要改写成 Polyester Fiber" in hint
    # 文本侧（标题/货号）的「安抚玩偶→Comfort Plush Toy」改名不受影响：那几处是生成
    # 字段、没有「图上的材质会与属性对不上」这回事，两条链路故意不同口径。
    import inspect
    assert "Comfort Plush Toy" in inspect.getsource(titles)


def test_重试话术点名实物标记():
    """实物上的标不在「叠加文案层」范围里，不点名这一发等于白烧。"""
    hint = cleaning_rules._retry_hint("图上有品牌标识", cjk=False, mark=True)
    assert "品牌标识" in hint and "材质成分说明" in hint
    # 分支是 if/return 互斥的：同时踩禁词与实物标记时只走 banned 那一支，
    # 而它那句「实物上的装饰性印花刺绣保留」若不带例外，等于反向暗示实物上的标也别动。
    both = cleaning_rules._retry_hint("图上有环保声明和品牌标识", cjk=False,
                                      banned=True, mark=True)
    assert "不在此列" in both and "品牌标识" in both
    # 末发「全抹掉」也要点名这两类，否则只有实物标的图在这一发之后仍带着标
    last = cleaning_rules._retry_hint("图上有品牌标识", cjk=False,
                                      mark=True, last_chance=True)
    assert "品牌标识" in last and "材质成分说明" in last


def test_标题禁令给出环保声明的处置方向():
    """必须给「怎么改」而不是只说「不许写」，否则两次生成全废、整阶段失败。

    环保声明是这里的特例：只许删、不许换说法（任何环保说法平台都不接受），
    提示词要说清这一点，否则模型会用 Sustainable 替掉 Eco-Friendly。
    """
    # 提示词内联在 titles 的生成函数里（与 8b 禁词红线同一段），故按模块源码断言
    import inspect
    text = inspect.getsource(titles)
    assert "环保声明" in text
    assert "不要用别的环保说法替代" in text
