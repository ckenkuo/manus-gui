# -*- coding: utf-8 -*-
"""多来源适配层（app/publish/sources/）的离线测试。

为什么每条都值得单测——它们锁的都是「跑起来才发现、且发现时已经错了一批」的静默故障：

  1. **平台识别的白名单边界**。认错平台会用错的适配器去读页面，表现是「页面数据未
     就绪」——看不出真实原因是域名判错了。尤其 amazon 的各国后缀（.com.au / .co.jp）
     与拼多多的老域名 yangkeduo，漏掉就是整批来源静默不可用。

  2. **商品 ID 必须锚定参数名，不能「随便找 6 位以上数字」**。service._task_key 原先
     就是那么写的：拼多多长链接里的 refer_page_id 时间戳会先被命中，于是两个不同商品
     撞到同一个状态文件、互相覆盖进度。这是本次改动修掉的真实 bug，必须钉住。

  3. **工作目录/状态键的平台前缀**。拼多多 goods_id 与 1688 offerId 都是纯数字且位数
     重叠，不带前缀就会撞目录、图互相覆盖。而 1688 自己【必须不带前缀】——既有历史
     目录与状态文件全是老形态，加了前缀等于所有历史进度失配、续跑从头再来。

  4. **单规格商品也要产出一条 skuMap**。阶段⑧ 读 skus 的外层键当尺码去勾选，空 skus
     会让 pipeline 直接报「product-info.json 无 skus 数据」，而阶段⑧ 挂 → 尺码行不生成
     → ⑨⑩⑪ 全部无处可填。亚马逊/Temu 的单规格商品是常态，这条不能漏。

  5. **图片 URL 必须剥掉 CDN 缩放参数**。拼多多 topGallery 的 url 带
     ?imageMogr2/.../1300x9999，不剥就下到压缩版；描述图撞 Temu 1340×1785 硬红线时
     直接不够（见记忆 temu-cloth-image-min-size-gate）。

  6. **亚马逊重量单位不认就必须给 None**。只抽数字会把「12 ounces」读成 12 千克，
     差 350 倍，要到平台称重才发现。宁可让阶段⑩ 用 LLM 预估，也不填一个确定的错值。

  7. **1688 的单规格商品也必须补成两维**。1688 时代采的全是服装（必然「颜色 × 尺码」），
     故本适配器长期原样透传 specAttrs。2026-08-28 offer 1014675972015（手工编织水果
     花束摆件）的 6 个 SKU 的 specAttrs 全是裸颜色名，被 extract.pivot_skus 的
     「不含 > 就 continue」整条丢掉，落盘 skus={} / colors=[] / sizes=[]，随后阶段⑦
     报「视觉未给出任何颜色行选图」、阶段⑧ 报「无 skus 数据」而未落库。家居/玩具/
     饰品在 1688 上很常见，这条与另三家的 _spec_to_pair 是同一个契约。

全程离线：不连 CDP、不碰真站。需要页面数据的用例用假 session 喂实测样本
（样本取自 2026-08-27 对真站的只读探测）。
"""
import asyncio

import pytest

from app.publish import extract as E
from app.publish.sources import base
from app.publish.sources import alibaba1688, amazon, pinduoduo, temu
from app.publish.sources.base import SourceProduct, UnsupportedSourceError


# ---- 平台识别 ---------------------------------------------------------------

@pytest.mark.parametrize("url,want", [
    # 1688：桌面与移动域
    ("https://detail.1688.com/offer/1051793179451.html", "1688"),
    ("https://m.1688.com/offer/987654321098.html", "1688"),
    # 拼多多：新域名 + 老域名 yangkeduo（实测采集箱里两者都有）
    ("https://mobile.pinduoduo.com/goods.html?goods_id=985357680144", "pdd"),
    ("https://mobile.yangkeduo.com/goods.html?goods_id=994437651298", "pdd"),
    # Temu：区域-语言路径段不影响域名判定
    ("https://www.temu.com/co-en/girls-dresses-g-605936466903004.html", "temu"),
    ("https://www.temu.com/jp-zh-Hans/foo-g-606214515739511.html", "temu"),
    # 亚马逊：各国后缀
    ("https://www.amazon.com/dp/B0DR3JT314", "amazon"),
    ("https://www.amazon.com.au/dp/B0B9BJL45T?language=en_AU", "amazon"),
    ("https://www.amazon.co.jp/gp/product/B0DR3JT314", "amazon"),
    ("https://www.amazon.de/dp/B0B54QHP7H", "amazon"),
])
def test_识别平台(url, want):
    assert base.detect_platform(url) == want


@pytest.mark.parametrize("url", [
    "https://www.taobao.com/item.htm?id=123456",
    "https://item.jd.com/100012043978.html",
    "https://www.notamazon.com/dp/B0DR3JT314",   # 名字里含 amazon 但不是它的域
    "not-a-url",
    "",
])
def test_不支持的来源要抛而不是当1688(url):
    """认不出的域名必须抛 UnsupportedSourceError。

    【为什么不能静默按 1688 处理】原先 extract 就是这样：任何 URL 都拼
    detail.1688.com/offer/<抽到的数字>.html 去打开，于是一个淘宝链接会打开一个
    不存在的 1688 页面，然后报「页面数据未就绪（未登录或被拦截？）」——排查时
    完全看不出真实原因是「这个平台没适配」。
    """
    with pytest.raises(UnsupportedSourceError):
        base.detect_platform(url)


def test_宽松版识别不抛只给空串():
    """批量场景（列一整页采集箱）用宽松版：一行认不出不该让整次扫描失败。"""
    assert base.platform_of("https://www.taobao.com/item.htm?id=1") == ""
    assert base.platform_of("") == ""
    assert base.platform_of("https://detail.1688.com/offer/123456.html") == "1688"


# ---- 商品 ID 抽取 -----------------------------------------------------------

@pytest.mark.parametrize("url,want", [
    ("https://detail.1688.com/offer/1051793179451.html", "1051793179451"),
    ("https://mobile.pinduoduo.com/goods.html?goods_id=985357680144", "985357680144"),
    ("https://www.temu.com/co-en/x-g-605936466903004.html", "605936466903004"),
    ("https://www.amazon.com.au/dp/B0B9BJL45T?language=en_AU", "B0B9BJL45T"),
    ("https://www.amazon.co.jp/gp/product/B0DR3JT314", "B0DR3JT314"),
])
def test_抽商品ID(url, want):
    assert base.source_id(url) == want


def test_拼多多长链接不能被时间戳劫持():
    """实测真实链接：goods_id 在前，后面跟着 refer_page_id 里的 13 位时间戳。

    【这是本次改动修掉的真实 bug】service._task_key 原先用
    re.search(r"(\\d{6,})", url)，对这条链接会命中 goods_id 的值还算走运——但
    换个参数顺序（_oak_rcto 在前）就会抽到别的数字。而状态键抽错的后果是两个商品
    共用一份进度文件：A 跑到阶段⑨，B 一进来就以为自己也跑到⑨了，直接跳过前八个阶段。
    """
    url = ("https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"
           "&_oak_rcto=YWJkoUxjBIobsrSI9GnrA7uvkTSsmvDeQaiYUDjBkmkvOHxCJBTk5hFtufD1Hm8Z"
           "&refer_page_id=10015_1786254742569_vndt2lqm60&refer_page_sn=10015"
           "&uin=LXGC2BLIM7V2LHTH6Y47SDWULI_GEXDA")
    assert base.source_id(url) == "985357680144"


def test_temu带跟踪参数也要抽对():
    url = ("https://www.temu.com/co-en/2026-new--girls-princess-dresses-for-birthday"
           "-first-birthday-celebrations-g-605936466903004.html?_x_sessn_id=3ne2ojcrhn"
           "&refer_page_id=10017_1787817058751_yjd9f4p2zd")
    assert base.source_id(url) == "605936466903004"


def test_抽不到ID给空串不抛():
    """认得出平台但抽不出 ID 时给空串：调用方对这两种情形处置不同（阶段① 要报错，
    采集箱列表只是少显示一个 ID），故不在这里替它决定。"""
    assert base.source_id("https://detail.1688.com/") == ""
    assert base.source_id("https://www.temu.com/co-en/") == ""


# ---- URL 归一 ---------------------------------------------------------------

def test_归一剥掉跟踪参数():
    """归一是为了让同一商品的两次抓取看起来是同一个来源（日志/source.url 可复现）。"""
    long_url = ("https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"
                "&_oak_search_term=%E5%B1%B1%E7%AB%B9&refer_page_sn=10015")
    assert base.normalize_url(long_url) == \
        "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"


def test_拼多多归一保留原域名():
    """【两个域名不是纯别名，风控策略不同】2026-08-27 实测：把 mobile.pinduoduo.com
    的链接改写成 mobile.yangkeduo.com 再导航，会撞「请在拼多多 App 打开」的验证中间页、
    SSR 数据压根不注入；原域名直接可读。故只丢跟踪参数、不动 host。
    """
    # 用真实位数的 goods_id（12 位）：ID 规则要求 6 位以上，太短的假 ID 抽不出来，
    # 此时 normalize_url 按约定原样返回（见 test_归一认不出时原样返回）
    assert base.normalize_url(
        "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144&x=2"
    ) == "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"
    assert base.normalize_url(
        "https://mobile.yangkeduo.com/goods.html?goods_id=994437651298&x=2"
    ) == "https://mobile.yangkeduo.com/goods.html?goods_id=994437651298"


def test_temu归一保留区域段():
    """区域-语言段决定页面语言与【币种】（实测 jp-zh-Hans 给日元）。

    丢掉会跳到默认区域，抓到的价格换了币种——而价格是选品比价的依据，
    换了币种却不自知比抓不到还糟。
    """
    url = "https://www.temu.com/jp-zh-Hans/foo-g-606214515739511.html?x=1"
    assert base.normalize_url(url) == "https://www.temu.com/jp-zh-Hans/g-606214515739511.html"


def test_亚马逊归一保留站点域名():
    """amazon.com.au 与 amazon.com 是不同站点、不同价格库存，同 ASIN 在别国站点
    可能压根不存在。归一到 .com 会抓错商品。"""
    url = "https://www.amazon.com.au/dp/B0B9BJL45T?language=en_AU"
    assert base.normalize_url(url) == "https://www.amazon.com.au/dp/B0B9BJL45T"


def test_归一认不出时原样返回():
    """归一坏了不该阻断提取（best-effort 取向，本项目既定模式）。"""
    assert base.normalize_url("https://www.taobao.com/x") == "https://www.taobao.com/x"


# ---- 工作目录与状态键的平台前缀 ----------------------------------------------

def test_1688工作目录不带平台前缀():
    """【必须保持老形态】既有工作目录全是 product-<offerId>/，加前缀会让所有历史
    目录与状态文件里的 workdir 失配——续跑时找不到已下好的图，白重下一遍。"""
    assert E.workdir_for("1051793179451", "1688").endswith("product-1051793179451")
    # 默认参数也是 1688（老调用点 workdir_for(offer_id) 不带第二个参数）
    assert E.workdir_for("1051793179451").endswith("product-1051793179451")


@pytest.mark.parametrize("platform,pid,tail", [
    ("pdd", "985357680144", "product-pdd-985357680144"),
    ("temu", "605936466903004", "product-temu-605936466903004"),
    ("amazon", "B0B9BJL45T", "product-amazon-B0B9BJL45T"),
])
def test_新平台工作目录带前缀(platform, pid, tail):
    """拼多多 goods_id 与 1688 offerId 都是纯数字、位数重叠，撞号就是两个商品
    共用一个目录、图互相覆盖。故新平台必须带前缀。"""
    assert E.workdir_for(pid, platform).endswith(tail)


def test_状态键按平台隔离():
    """同上：状态键撞号会让 B 商品读到 A 的进度，直接跳过没跑的阶段。"""
    from app.publish import service as S

    assert S._task_key({"url": "https://detail.1688.com/offer/1051793179451.html"}) \
        == "1051793179451"
    assert S._task_key({"url": "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"}) \
        == "pdd-985357680144"
    assert S._task_key({"url": "https://www.temu.com/co-en/x-g-605936466903004.html"}) \
        == "temu-605936466903004"
    assert S._task_key({"url": "https://www.amazon.com.au/dp/B0B9BJL45T"}) \
        == "amazon-B0B9BJL45T"
    assert S._task_key({"rowid": "173539495456771263"}) == "rowid-173539495456771263"


def test_状态键对不支持的来源要报错():
    """认不出平台就抛，别静默拿一个乱抽的数字当状态键。"""
    from app.publish import service as S

    with pytest.raises(ValueError):
        S._task_key({"url": "https://www.taobao.com/item.htm?id=123456"})
    with pytest.raises(ValueError):
        S._task_key({})


# ---- SKU 规格折叠（各平台共用的「颜色>尺码」契约）-----------------------------
# 阶段⑧ 读 pivot 后的外层键（尺码）去页面上勾复选框，故 spec 必须是两维「颜色>尺码」。
# 单维/无维商品若给不出尺码，阶段⑧ 一个都勾不上 → 尺码行不生成 → ⑨⑩⑪ 全部无处可填。

@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_双维规格原样折成颜色大于尺码(mod):
    specs = [{"k": "款式", "v": "柠檬-剥剥乐系列"}, {"k": "尺寸", "v": "10cm"}]
    assert mod._spec_to_pair(specs) == "柠檬-剥剥乐系列>10cm"


@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_单维规格补均码(mod):
    """单维时把它当颜色维、尺码补「均码」：均码在 pipeline 的别名表里有
    （见记忆 publish-size-onesize-alias），能匹配到平台的 One-Size 选项。"""
    assert mod._spec_to_pair([{"k": "风格", "v": "来图定制全身6cm"}]) \
        == "来图定制全身6cm>均码"


@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_无规格也要给一条(mod):
    """完全无规格商品（specs 空）不能产出空 spec，否则 pivot 后 skus 为空、阶段⑧ 挂。"""
    assert mod._spec_to_pair([]) == "默认>均码"
    assert mod._spec_to_pair(None) == "默认>均码"


@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_三维规格不丢第三维(mod):
    """平台允许三维规格。静默丢掉第三维会让不同 SKU 撞成同一个 spec，
    pivot 后互相覆盖（价格随机取到其中一个），且总 SKU 数凭空变少。"""
    specs = [{"k": "颜色", "v": "红"}, {"k": "尺码", "v": "M"}, {"k": "款式", "v": "长袖"}]
    assert mod._spec_to_pair(specs) == "红>M/长袖"


# ---- 1688 的 spec 归一（norm_spec）------------------------------------------
# 另三家的入口是结构化 specs 列表，1688 拿到的是已拼好的字符串 specAttrs，
# 故归一函数签名不同、但契约完全一致：产出必须是两维「颜色>尺码」。

def test_1688双维规格原样保留():
    """服装商品（1688 的绝大多数）本就是两维，归一不能把它改坏。"""
    assert alibaba1688.norm_spec("6633-灰色>100码") == "6633-灰色>100码"


def test_1688单维裸颜色名要补均码():
    """2026-08-28 真站样本 offer 1014675972015（手工编织水果花束摆件）的 6 条 spec。

    这是本次修复钉住的 bug：不补两维就被 pivot_skus 丢光，阶段⑦⑧ 双挂而未落库。
    """
    assert alibaba1688.norm_spec("【心想事橙】橙子花筒（life盆）")         == "【心想事橙】橙子花筒（life盆）>均码"


def test_1688空spec也要给一条():
    assert alibaba1688.norm_spec("") == "默认>均码"
    assert alibaba1688.norm_spec(None) == "默认>均码"


def test_1688的HTML转义大于号要还原():
    """specAttrs 里的分隔符实测是 HTML 实体 &gt;（_JS_EXTRACT 已 replace 一次，
    这里再兜一次：漏还原会让两维 spec 被当成单维，白补一个「均码」出来）。"""
    assert alibaba1688.norm_spec("灰色&gt;100码") == "灰色>100码"


def test_1688三维规格不丢第三维():
    assert alibaba1688.norm_spec("红>M>长袖") == "红>M/长袖"


def test_1688空维段不产出空颜色或空尺码():
    """卖家规格值为空时 specAttrs 会出现「红>」这种残缺形态。
    直接 split 会得到尺码维空串，pivot 后 sizes 里混一个空键、阶段⑧ 拿它去页面找
    复选框必然找不到。故空段要剔掉、退回单维补均码。"""
    assert alibaba1688.norm_spec("红>") == "红>均码"
    assert alibaba1688.norm_spec(">M") == "M>均码"


def test_1688抽取脚本使用有效的context回退表达式():
    """window.context 缺失时也要返回 found=false，而不是脚本语法错误。"""
    assert "const ctx = window.context || {};" in E._JS_EXTRACT
    assert "window.context||)" not in E._JS_EXTRACT


# ---- 价格取向 ---------------------------------------------------------------

def test_拼多多取拼团价而非划线价():
    """groupPrice 是买家实付，normalPrice 是划线原价。拿原价当成本会把利润算低、
    误杀本来可做的品。"""
    assert pinduoduo._price_of({"groupPrice": "11", "normalPrice": "21.89"}) == 11.0
    # 拼团价缺失时才退划线价
    assert pinduoduo._price_of({"groupPrice": "", "normalPrice": "21.89"}) == 21.89
    assert pinduoduo._price_of({}) is None


def test_temu取实售价而非划线价():
    assert temu._price_of({"salePrice": 426, "normalPrice": 558}) == 426.0
    assert temu._price_of({"salePrice": 0, "normalPrice": 558}) == 558.0
    assert temu._price_of({}) is None


def test_temu币种从价格串符号推():
    """localInfo.currency 缺失时靠符号兜。【不能默认 USD】默认一个币种会让日元价
    被当美元读，成本差 100 多倍，而这个错误表现为「这个品利润高得离谱」，
    比报错难发现得多。"""
    assert temu._guess_currency("", ["426円"]) == "JPY"
    assert temu._guess_currency("", ["$12.99"]) == "USD"
    assert temu._guess_currency("", ["€9,99"]) == "EUR"
    # 接口给了就优先用接口的
    assert temu._guess_currency("jpy", ["$12.99"]) == "JPY"
    # 都推不出来给空串，绝不猜
    assert temu._guess_currency("", ["12.99"]) == ""
    assert temu._guess_currency("", []) == ""


# ---- 图片 URL 剥参数 ---------------------------------------------------------

@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_剥掉CDN缩放参数拿原图(mod):
    """拼多多 topGallery 的 url 实测带 ?imageMogr2/quality/90/thumbnail/1300x9999>。
    不剥就下到压缩版：素材图勉强够 800x800，但描述图撞 Temu 的 1340×1785 硬红线时
    直接不够（见记忆 temu-cloth-image-min-size-gate）。"""
    url = ("https://img.pddpic.com/mms-material-img/2026-03-25/5335c5e1.jpeg"
           "?imageMogr2/quality/90/thumbnail/1300x9999%3E")
    assert mod.strip_img_params(url) == \
        "https://img.pddpic.com/mms-material-img/2026-03-25/5335c5e1.jpeg"


@pytest.mark.parametrize("mod", [pinduoduo, temu])
def test_干净URL不被动到(mod):
    """路径里的 hash 是图片标识，动了就 404——只能剥 query，不能碰路径。"""
    url = "https://img.kwcdn.com/product/fancy/00c80dc9-0dbd-4364-b302-0ab9571f285f.jpg"
    assert mod.strip_img_params(url) == url
    assert mod.strip_img_params("") == ""


# ---- 成分解析策略（来源平台键名差异）----------------------------------------

def test_拼多多成分解析剥占位前缀与括号别名():
    """拼多多「面料/材质」值形如「其它/涤纶（聚酯纤维）」：不清洗会命中 _COMP_NON_FIBER
    的「其它」、把真正的纤维「涤纶」也一起清掉。故先剥前缀与括号别名再进框架。"""
    r = pinduoduo.parse_composition(
        {"面料/材质": "其它/涤纶（聚酯纤维）", "成分含量": "70%（含）-80%（不含）"})
    assert r["fiber"] == "涤纶" and r["percent"] == 70
    assert r["fiberText"] == "涤纶"


def test_拼多多成分解析无占位前缀():
    r = pinduoduo.parse_composition({"面料/材质": "棉", "成分含量": "90%"})
    assert r["fiber"] == "棉" and r["percent"] == 90


def test_拼多多成分解析整段都是占位词时交LLM():
    """「面料/材质」只有「其它」这类占位词时，fiber 置空、保留含量事实交阶段④推断，
    而不是清洗成空串被当成「源没写」走默认聚酯纤维 100%。"""
    r = pinduoduo.parse_composition({"面料/材质": "其它", "成分含量": "70%"})
    assert r["fiber"] == "" and r["percent"] == 70
    assert r["fiberText"] == "其它" and r.get("assumed")


def test_拼多多成分解析缺失走默认兜底():
    r = pinduoduo.parse_composition({})
    assert r["fiber"] == "聚酯纤维" and r["percent"] == 100 and r.get("assumed")


def test_1688成分解析走默认键名():
    """1688 的 parse_composition 是 extract.parse_main_composition 的转发，键名仍用
    「主面料成分」「主面料成分含量」，行为不因拆策略而变。"""
    r = alibaba1688.parse_composition(
        {"主面料成分": "棉", "主面料成分含量": "90%（含）-95%（不含）（%）"})
    assert r["fiber"] == "棉" and r["percent"] == 90


# ---- 亚马逊重量解析 ---------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("0.72 公斤", 0.72),
    ("720 克", 0.72),
    ("1.5 pounds", 0.6804),
    ("8 ounces", 0.2268),
    ("350 g", 0.35),
    ("2.2 lb", 0.9979),
    ("1,5 公斤", 1.5),          # 欧洲站用逗号做小数点
])
def test_亚马逊重量按单位换算(raw, want):
    """【必须认单位】只抽数字会把「8 ounces」读成 8 千克，差 35 倍，
    包裹重量填出去要到平台称重才发现。"""
    got = amazon._parse_weight_kg({"商品重量": raw})
    assert got == pytest.approx(want, abs=0.001)


def test_千克不被克截获():
    """单位名要按长度降序比：「千克」里含「克」，比错了 0.72 千克会变 0.00072 千克。"""
    assert amazon._parse_weight_kg({"商品重量": "0.72 千克"}) == pytest.approx(0.72)
    assert amazon._parse_weight_kg({"商品重量": "0.72 公斤"}) == pytest.approx(0.72)


def test_亚马逊重量认不出单位给None():
    """认不出单位时【绝不赌一个默认单位】——让阶段⑩ 用 LLM 预估，
    比按错单位填一个确定的错值好。"""
    assert amazon._parse_weight_kg({"商品重量": "0.72"}) is None
    assert amazon._parse_weight_kg({"商品重量": "很轻"}) is None
    assert amazon._parse_weight_kg({}) is None


def test_亚马逊重量越界当解析错误():
    """跨境小包 0.001~50kg。越界说明解析错了（如把「1188」个月的年龄上限读成重量），
    宁可不给也不填一个荒谬值。"""
    assert amazon._parse_weight_kg({"商品重量": "5000 公斤"}) is None
    assert amazon._parse_weight_kg({"商品重量": "0 公斤"}) is None


def test_亚马逊重量按键名优先级():
    """商品重量优先于包装重量：包装重含缓冲材料，比商品本身重。"""
    attrs = {"包装重量": "1.2 公斤", "商品重量": "0.72 公斤"}
    assert amazon._parse_weight_kg(attrs) == pytest.approx(0.72)


# ---- 中间结构 SourceProduct -------------------------------------------------

def test_raw留档沿用1688键名():
    """raw.json 的键名沿用 1688 时代的形状（subject/images/unitWeight），
    这样既有档与新平台的档能用同一套眼睛看，排查时不必先分辨这是哪个平台。"""
    p = SourceProduct(platform="pdd", url="u", productId="1", title="T",
                      mainImages=["a.jpg"], descImages=["b.jpg"], unitWeightKg=0.5,
                      skuMap=[{"spec": "红>M", "price": 1.0, "stock": 9}])
    raw = p.as_raw()
    assert raw["subject"] == "T"
    assert raw["images"] == ["a.jpg"]
    assert raw["unitWeight"] == 0.5
    assert raw["platform"] == "pdd"
    assert raw["skuMap"][0]["spec"] == "红>M"


def test_默认值不共享可变对象():
    """dataclass 的可变默认值必须用 default_factory，否则两个实例共享同一个 dict/list
    ——一个商品的属性会串到另一个商品上。"""
    a, b = SourceProduct(), SourceProduct()
    a.attributes["x"] = 1
    a.mainImages.append("img")
    assert b.attributes == {}
    assert b.mainImages == []


# ---- 适配器接口一致性 -------------------------------------------------------

def test_四个平台都有fetch():
    """get_adapter 拿到的模块必须都提供 async fetch(session, url, on_manual, timeout)
    ——extract 只按这一个接口调，签名不齐会在真站上才炸。"""
    from app.publish import sources

    for platform in ("1688", "pdd", "temu", "amazon"):
        mod = sources.get_adapter(platform)
        assert asyncio.iscoroutinefunction(mod.fetch), platform
        import inspect
        params = list(inspect.signature(mod.fetch).parameters)
        assert params[:2] == ["session", "url"], platform
        assert "on_manual" in params and "timeout" in params, platform


def test_未知平台取适配器要抛():
    from app.publish import sources

    with pytest.raises(UnsupportedSourceError):
        sources.get_adapter("taobao")


# ---- fetch() 走通：用假 session 喂 2026-08-27 实测的真实样本 --------------------
# 【为什么必须测「复用已打开页签」这条路】Temu 实测会把对商品页的重新导航 302 到
# login.html?login_scene=2——同一个 URL、同一个浏览器、页签里正开着完整数据也照样跳。
# 故适配器必须先试 adopt_open_page 再退回 navigate；写反了的表现是「用户明明开着页面，
# 却报数据未就绪」，而且只在真站上才暴露。

@pytest.fixture(autouse=True)
def 人工等待缩到毫秒级(monkeypatch):
    """把人工等待闸门的时长缩到毫秒。

    【为什么要 autouse】真实值是 600 秒（要给人留够登录/输验证码的时间），任何一个
    走到「被拦」分支又忘了改常量的用例，都会把整个测试套件挂十分钟——这种慢是会
    传染的：后来者加用例时不会想到有这么个雷。故在这里一次性钉死，用例只在需要
    更长/更短时自己再覆盖。
    """
    monkeypatch.setattr(base, "WAIT_HUMAN_TIMEOUT", 0.05)
    monkeypatch.setattr(base, "WAIT_HUMAN_INTERVAL", 0.01)
    monkeypatch.setattr(base, "WAIT_HUMAN_REMIND", 0.02)


class _FakeSession:
    """最小假 session：按调用顺序回放 eval_json/wait_for 的返回值，并记录动作。

    只实现适配器实际用到的方法（adopt_open_page/navigate/wait_for/eval_json/page）。
    不继承 BrowserSession：那会拉起 Playwright，而这些用例要纯离线。

    recover_after：被拦后第几次轮询开始「人工已处理好」——用来测 wait_human 的
    等待-恢复路径。None 表示永不恢复（测超时分支）。
    """

    def __init__(self, data, blocked=None, has_open_tab=False, recover_after=None):
        self._data = data
        self._blocked = blocked or {"blocked": False}
        self._has_open_tab = has_open_tab
        self._recover_after = recover_after
        self._probe_calls = 0
        self.actions = []          # 依次记下 adopt / navigate，供断言取数路径

    class _Page:
        async def bring_to_front(self):
            return None

    @property
    def page(self):
        return self._Page()

    async def adopt_open_page(self, must_include):
        self.actions.append(("adopt", must_include))
        return {"ok": self._has_open_tab, "url": "https://x/" + must_include}

    async def navigate(self, url, **kw):
        self.actions.append(("navigate", url))
        return {"ok": True, "url": url}

    async def wait_for(self, code, pred, timeout=40):
        # 人工恢复后 wait_for 也要给出有数据的结果（真实情况下页面已恢复）
        if self._recover_after is not None and self._probe_calls >= self._recover_after:
            return self._recovered_data()
        return self._data

    def _recovered_data(self):
        d = dict(self._data)
        d["found"] = True
        return d

    async def eval_json(self, code, **kw):
        self._probe_calls += 1
        if self._recover_after is not None and self._probe_calls >= self._recover_after:
            # 人工处理完：不再 blocked，且就绪标记为真（两个平台的键名都给上）
            return {"blocked": False, "hasData": True, "hasTitle": True}
        return self._blocked


# 实测样本（截取自 2026-08-27 对真站的只读探测，字段名与真站一字不差）
_PDD_SAMPLE = {
    "found": True, "goodsId": "985357680144",
    "title": "可爱仿真水果毛绒玩具可剥解压捏捏挂件含香颗粒可拆卸玩偶礼物",
    "props": [{"key": "品牌", "values": ["无品牌/无注册商标"]},
              {"key": "材质", "values": ["毛绒"]},
              {"key": "填充物", "values": ["PP棉"]}],
    "skus": [{"specs": [{"k": "款式", "v": "柠檬-剥剥乐系列"},
                        {"k": "尺寸", "v": "10cm（剥剥乐系列带香味）"}],
              "groupPrice": "11", "normalPrice": "21.89", "quantity": 100, "weight": 0},
             {"specs": [{"k": "款式", "v": "山竹-剥剥乐系列"},
                        {"k": "尺寸", "v": "10cm（剥剥乐系列带香味）"}],
              "groupPrice": "11", "normalPrice": "21.89", "quantity": 96, "weight": 0}],
    "mainImages": ["https://img.pddpic.com/a.jpeg?imageMogr2/quality/90/thumbnail/1300x9999%3E"],
    "descImages": ["https://img.pddpic.com/b.jpeg"],
    "videos": [], "cats": [15083, 15102, 15233], "mallName": "奶绒毛绒小铺",
    "minGroupPrice": "11", "maxGroupPrice": "11", "linePrice": "22.89",
}

_TEMU_SAMPLE = {
    "found": True, "goodsId": "606214515739511",
    "title": "定制亚克力宠物冰箱贴，采用可爱的卡通动物造型",
    "props": [{"key": "产地", "values": ["Zhejiang，China"]},
              {"key": "材料", "values": ["亚克力"]},
              {"key": "节假日", "values": ["圣诞节", "万圣节", "复活节"]}],
    "skus": [{"specs": [{"k": "风格", "v": "来图定制全身6cm"}],
              "salePrice": 426, "normalPrice": 558, "salePriceStr": "426円",
              "stock": 416, "thumbUrl": "https://img.kwcdn.com/t.jpg"}],
    "mainImages": ["https://img.kwcdn.com/product/fancy/dca58f4b.jpg"],
    "descImages": ["https://img.kwcdn.com/product/fancy/00c80dc9.jpg"],
    "videos": [], "sizeGuide": {"show": 0}, "cats": [9711, 9712],
    "mallName": "LazySlowSheep", "currency": "", "region": "jp-zh-Hans",
}

_AMZ_SAMPLE = {
    "found": True, "asin": "B0DR3JT314", "currentAsin": "B0DR3JT314",
    "title": "American Girl My First Samantha 娃娃和纸板书",
    "attrs": {"品牌": "American Girl", "玩具公仔类型": "洋娃娃",
              "商品重量": "0.72 公斤",
              "商品尺寸 长 x 宽 x 高": "20.3长度 x 10.2宽度 x 39.4高度 厘米"},
    "mainImages": ["https://m.media-amazon.com/images/I/81c55a0VbKL._AC_SL1500_.jpg"],
    "descImages": ["https://m.media-amazon.com/images/S/aplus/x.jpg"],
    "bullets": ["一个 13.5 英寸毛绒娃娃", "《你好》萨曼莎纸板书"],
    "descText": "Samantha 于 1904 年在纽约长大", "price": "JPY11,149",
    "variations": {"B0DR3JT314": ["Samantha"], "B0DR3L6CPX": ["Kirsten"]},
    "videoUrl": "", "host": "www.amazon.com", "lang": "zh-cn",
}


@pytest.mark.asyncio
async def test_拼多多fetch产出结构():
    s = _FakeSession(_PDD_SAMPLE)
    p = await pinduoduo.fetch(s, "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144")
    assert p.platform == "pdd" and p.productId == "985357680144"
    assert p.attributes["填充物"] == "PP棉"
    assert [sk["spec"] for sk in p.skuMap] == [
        "柠檬-剥剥乐系列>10cm（剥剥乐系列带香味）", "山竹-剥剥乐系列>10cm（剥剥乐系列带香味）"]
    assert p.skuMap[0]["price"] == 11.0          # 拼团价，不是 21.89 划线价
    # 图片 URL 的 imageMogr2 缩放参数必须已剥掉
    assert p.mainImages == ["https://img.pddpic.com/a.jpeg"]
    # sku.weight 全为 0（卖家不填）时给 None，交阶段⑩ 预估——不能填 0 克
    assert p.unitWeightKg is None


@pytest.mark.asyncio
async def test_多值属性折成逗号串():
    """阶段④ 把整个 attributes dumps 进提示词；统一成字符串能让 1688 那边写好的
    具名键读取（attrs["套装类型"] 之类）在新平台上也不会拿到 list 而炸。"""
    s = _FakeSession(_TEMU_SAMPLE)
    p = await temu.fetch(s, "https://www.temu.com/jp-zh-Hans/x-g-606214515739511.html")
    assert p.attributes["节假日"] == "圣诞节,万圣节,复活节"


@pytest.mark.asyncio
async def test_temu优先复用已打开页签():
    """页签开着就直接读，不导航——Temu 重新导航会被跳到 login.html。"""
    s = _FakeSession(_TEMU_SAMPLE, has_open_tab=True)
    p = await temu.fetch(s, "https://www.temu.com/jp-zh-Hans/x-g-606214515739511.html")
    assert p.productId == "606214515739511"
    # 只 adopt、绝不 navigate；且按商品 ID 找页签（用户开的链接带一长串 _oak_* 参数，
    # 与任务里填的那条几乎不会字节相同，但 -g-<id> 这段稳定）
    assert s.actions == [("adopt", "-g-606214515739511")]


@pytest.mark.asyncio
async def test_temu没开着页签才导航():
    s = _FakeSession(_TEMU_SAMPLE, has_open_tab=False)
    await temu.fetch(s, "https://www.temu.com/jp-zh-Hans/x-g-606214515739511.html")
    assert [a[0] for a in s.actions] == ["adopt", "navigate"]


@pytest.mark.asyncio
async def test_temu币种从价格串推出日元():
    """接口没给 currency 时靠「426円」推出 JPY。默认 USD 会让日元价被当美元读，
    成本差 100 多倍，且表现为「利润高得离谱」而不是报错。"""
    s = _FakeSession(_TEMU_SAMPLE, has_open_tab=True)
    p = await temu.fetch(s, "https://www.temu.com/jp-zh-Hans/x-g-606214515739511.html")
    assert p.extra["currency"] == "JPY"


@pytest.mark.asyncio
async def test_亚马逊fetch产出结构():
    s = _FakeSession(_AMZ_SAMPLE, has_open_tab=True)
    p = await amazon.fetch(s, "https://www.amazon.com/dp/B0DR3JT314")
    assert p.platform == "amazon" and p.productId == "B0DR3JT314"
    # 重量从属性表解析出来（0.72 公斤），阶段⑩ 不必再问 LLM
    assert p.unitWeightKg == pytest.approx(0.72)
    # 单 SKU：跨 ASIN 变体只留档不采（一页只有一个商品，见适配器 docstring）
    assert p.skuMap == [{"spec": "默认>均码", "price": None, "stock": None}]
    assert set(p.extra["variations"]) == {"B0DR3JT314", "B0DR3L6CPX"}
    # 五点描述与长描述并进 attributes：那是把商品文案送进 ④⑤ 阶段的唯一通道
    assert "13.5 英寸" in p.attributes["商品要点"]
    assert p.attributes["商品描述"].startswith("Samantha")


@pytest.mark.asyncio
async def test_被拦时报错带可操作提示():
    """被拦必须与「页面改版」分开报，且报错要说清人该做什么。

    【为什么必须带处置提示】阶段① 失败时 UI 上显示的就是这句话。只给
    「login.html」用户不知道该做什么；写明「登录/把页面打开」才有用。
    """
    s = _FakeSession({"found": False},
                     blocked={"blocked": True, "detail": "login.html", "hasData": False})
    with pytest.raises(RuntimeError, match="登录|打开"):
        await temu.fetch(s, "https://www.temu.com/jp-zh-Hans/x-g-606214515739511.html")


@pytest.mark.asyncio
async def test_登录墙与验证码给不同提示():
    """两种拦截的人工处置完全不同：登录墙要登录账号，验证码要输字符。
    提示给错会让人白折腾（对着验证码页去找登录入口）。"""
    signin = _FakeSession({"found": False}, blocked={
        "blocked": True, "detail": "https://www.amazon.com/ap/signin", "hasTitle": False})
    with pytest.raises(RuntimeError, match="登录亚马逊账号"):
        await amazon.fetch(signin, "https://www.amazon.com/dp/B0DR3JT314")

    captcha = _FakeSession({"found": False}, blocked={
        "blocked": True, "detail": "Enter the characters you see below", "hasTitle": False})
    with pytest.raises(RuntimeError, match="验证码"):
        await amazon.fetch(captcha, "https://www.amazon.com/dp/B0DR3JT314")


@pytest.mark.asyncio
async def test_人工处理完成后自动继续(monkeypatch):
    """【核心行为：等人，不直接失败】人往往就在这台机器前，登录/输验证码几秒就好，
    而报错要重跑整个商品、已下的几十张图白丢。1688 侧早就是这个取向
    （extract.wait_human_verify），这里四家对齐。
    """
    # 给足几轮轮询的余量（autouse fixture 给的 0.05s 只够一轮）
    monkeypatch.setattr(base, "WAIT_HUMAN_TIMEOUT", 5.0)
    # 第 2 次轮询时人已处理好 → 不该抛异常，应重读数据并正常产出
    s = _FakeSession(dict(_PDD_SAMPLE, found=False),
                     blocked={"blocked": True, "detail": "验证", "hasData": False},
                     has_open_tab=True, recover_after=2)
    p = await pinduoduo.fetch(
        s, "https://mobile.pinduoduo.com/goods.html?goods_id=985357680144")
    assert p.productId == "985357680144"
    assert len(p.skuMap) == 2


@pytest.mark.asyncio
async def test_等待超时才判失败():
    """等不到（人不在、走开了）就必须失败，不能无限挂着——批次要能继续往下走。"""
    s = _FakeSession({"found": False}, has_open_tab=True,
                     blocked={"blocked": True, "detail": "验证", "hasData": False})
    with pytest.raises(RuntimeError, match="超时|仍未就绪"):
        await pinduoduo.fetch(s, "https://mobile.pinduoduo.com/goods.html?goods_id=1")


@pytest.mark.asyncio
async def test_没被拦但没数据要说清是改版():
    s = _FakeSession({"found": False}, blocked={"blocked": False}, has_open_tab=True)
    with pytest.raises(RuntimeError, match="未就绪"):
        await pinduoduo.fetch(s, "https://mobile.pinduoduo.com/goods.html?goods_id=1")


@pytest.mark.asyncio
async def test_人工提示回调被调用():
    """on_manual 是 service 层发 manual_check 事件的通道：Web 页面会多出一条人工检查、
    CLI 打「! 人工检查」行。它是辅助路径，回调自己抛异常不该让提取多失败一次。"""
    called = []
    s = _FakeSession({"found": False},
                     blocked={"blocked": True, "detail": "验证", "hasData": False})
    with pytest.raises(RuntimeError):
        await pinduoduo.fetch(s, "https://mobile.pinduoduo.com/goods.html?goods_id=1",
                              on_manual=lambda m: called.append(m))
    assert called and "拼多多" in called[0]


@pytest.mark.asyncio
async def test_人工提示回调抛异常也照样报原错():
    def boom(_):
        raise RuntimeError("提示通道坏了")

    s = _FakeSession({"found": False},
                     blocked={"blocked": True, "detail": "验证", "hasData": False})
    # 报出来的必须是「被拦」这个原因，不是回调自己的异常
    with pytest.raises(RuntimeError, match="被拦"):
        await pinduoduo.fetch(s, "https://mobile.pinduoduo.com/goods.html?goods_id=1",
                              on_manual=boom)
