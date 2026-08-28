# -*- coding: utf-8 -*-
"""Temu 商品详情页适配器（把 Temu 当【货源】采，不是发布目标）。

【注意语义】本项目里 Temu 有两个完全不同的身份，别搞混：
  - 发布目标：店小秘 → Temu 半托管，是 pipeline.py 那 15 个阶段在填的表单
  - 货源来源：本模块——从 temu.com 的买家侧详情页采别人的在售商品做选品
本模块只做后者。它与 app/temu_region.py（卖家后台的区域/域名逻辑）没有关系：
那边接的是 agentseller.temu.com 后台，这边是 www.temu.com 买家页。

【取数路径】window.rawData.store 顶层 —— 2026-08-27 在 CDP 浏览器实测。
    store.goods         商品主体（goodsName/gallery/detailList/catId1-4/thumbUrl）
    store.sku           SKU 数组（specs/salePrice/skuId/specShowImageUrl）
    store.goodsProperty 属性 [{key, values}]
    store.productDetail 详情长图，floorList[].items[].url
    store.sizeGuide     尺码表（实测 {show: 0} 表示该品无尺码表）
    store.mall          店铺信息
【与拼多多同挂 window.rawData.store 但路径不同】拼多多在 store.initDataObj.goods 下，
Temu 直接在 store 顶层。共用一个适配器会在任一平台改版时同时炸两个，故分开写。

【区域-语言路径段决定语言与币种】URL 形如 /co-en/...-g-<id>.html 或 /jp-zh-Hans/...。
实测 jp-zh-Hans 页面的价格是日元（sku.normalPriceStr = "426円"）。故：
  - 归一 URL 时【必须保留】该段（见 base.normalize_url）
  - 抽出的价格【带币种】记在 extra.currency 里，不做汇率换算——阶段⑩ 的申报价
    是人给的（默认 188.88，见记忆 publish-declare-price-and-pack-dims），源价格只是
    选品参考，擅自换算反而引入一个看不见的汇率假设

【价格取 salePrice 不取 normalPrice】salePrice 是实际售价（含促销），normalPrice 是
划线价。实测样本 salePrice=426 / normalLinePrice=558，取错会把成本算高。
【价格单位是「最小货币单位」还是「元」要按币种判】实测日元站 salePrice=426 对应
「426円」——日元无小数，故这里是整数元。别照 1688 那样无脑当元处理，见 _price_of。
"""
import re

from app.logger import logger
from app.publish.browser import BrowserSession
from app.publish.sources.base import SourceProduct, source_id, wait_human

_JS_EXTRACT = r"""(() => {
  const st = ((window.rawData || {}).store || {});
  const g = st.goods || {};
  const gid = String(st.goodsId || g.goodsId || '');
  if (!gid) return JSON.stringify({found: false});
  const num = v => (v == null || v === '' ? null : Number(v));
  // 详情长图：productDetail 可能是对象也可能是 JSON 串（实测是对象，但 SSR
  // 注入的同名字段在别的页面形态下见过字符串，故两种都收）
  let pd = st.productDetail;
  if (typeof pd === 'string') { try { pd = JSON.parse(pd); } catch (e) { pd = null; } }
  const descImgs = [];
  ((pd || {}).floorList || []).forEach(fl => {
    (fl.items || []).forEach(it => { if (it && it.url) descImgs.push(it.url); });
  });
  // 轮播主图：gallery 是 [{url|imageUrl}]，bannerList/detailList 是备用形态
  const gal = (g.gallery || g.bannerList || []).map(
    x => (x && (x.url || x.imageUrl || x.image || x)) || '').filter(Boolean);
  return JSON.stringify({
    found: true,
    goodsId: gid,
    title: g.goodsName || '',
    props: (st.goodsProperty || []).map(p => ({
      key: p.key || '', values: (p.values || []).map(String)})),
    skus: (st.sku || []).map(s => ({
      specs: (s.specs || []).map(x => ({k: x.specKey || '', v: x.specValue || ''})),
      salePrice: num(s.salePrice), normalPrice: num(s.normalPrice),
      salePriceStr: s.salePriceStr || s.normalPriceStr || '',
      stock: num(s.stockQuantity), thumbUrl: s.specShowImageUrl || s.thumbUrl || '',
    })),
    mainImages: gal,
    descImages: descImgs,
    // 视频：Temu 买家页的视频挂在 galleryStore 或 goods.customImageList，
    // 实测样本无视频，故两处都试、拿不到给空
    videos: [].concat(
      ((st.galleryStore || {}).videoList || []).map(x => (x && (x.videoUrl || x.url)) || ''),
      ((g.customImageList || []).map(x => (x && x.videoUrl) || ''))
    ).filter(Boolean),
    sizeGuide: st.sizeGuide || null,
    cats: [g.catId1, g.catId2, g.catId3, g.catId4].filter(Boolean),
    mallName: (((st.mall || {}).mallData || {}).mallName) || '',
    // 币种线索：优先 localInfo.currency，其次从价格串里的符号推（见 Python 侧）
    currency: ((st.localInfo || {}).currency) || '',
    region: location.pathname.split('/')[1] || '',
  });
})()"""

# Temu 的人机校验：实测跳到 /bgn_verification 或首页带 refer_page_name=bgn_verification。
# 与 1688 滑块不同，Temu 这个是「长按确认」或干脆是登录墙；两种都提示人工并原地等
# （wait_human），处理完自动继续。
_JS_BLOCKED = r"""(() => {
  const href = location.href;
  const urlHit = /verification|captcha|\/login/i.test(href);
  const t = (document.body ? (document.body.innerText || '') : '').slice(0, 2000);
  const words = ['Verify', 'verification', 'unusual traffic', '安全验证', 'Press and hold'];
  const hasData = !!(((window.rawData || {}).store || {}).goodsId);
  const wordHit = (!hasData && words.find(w => t.includes(w))) || '';
  return JSON.stringify({blocked: !!(urlHit || wordHit), hasData: hasData,
                         detail: String(wordHit || (urlHit ? href : '')),
                         url: href, title: document.title || ''});
})()"""

# 图片 CDN 参数：Temu 用 img.kwcdn.com，缩放挂在路径后缀或 query 上。
# 实测 detailList 的 url 干净（https://img.kwcdn.com/product/fancy/<uuid>.jpg），
# 但 gallery 里见过带 ?imageView2 的形态，故与拼多多同款剥法。
_IMG_PARAM_RE = re.compile(r"\?(?:imageMogr2|imageView2|x-oss-process)[^#]*", re.I)

# 价格币种 → 是否「无小数」（价格数值即整数元，不需要除 100）。
# 【为什么要这张表】各站币种的最小单位不同：日元/韩元无小数位，美元/欧元有两位。
# 实测日元站 salePrice=426 就是 426 円（不是 4.26），说明 Temu 给的是「显示单位」
# 而不是「最小货币单位」。故这里默认按显示单位处理，不做除法——这张表只用来
# 在日志里标明币种，不参与换算（留着是因为一旦发现某站给的是分，改这里最省事）。
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK"}

# 价格串里的币种符号 → 币种码。localInfo.currency 缺失时靠它兜。
_CURRENCY_SIGNS = (
    ("円", "JPY"), ("¥", "JPY"), ("₩", "KRW"), ("€", "EUR"), ("£", "GBP"),
    ("R$", "BRL"), ("MX$", "MXN"), ("A$", "AUD"), ("C$", "CAD"), ("$", "USD"),
)


def strip_img_params(url: str) -> str:
    return _IMG_PARAM_RE.sub("", url or "")


def _guess_currency(explicit: str, price_strs: list) -> str:
    """定币种：接口给了就用，否则从价格串的符号推，都没有给空串。

    给空串而不是默认 USD：默认一个币种会让日元价被当美元读，成本差 100 多倍，
    而这个错误在选品比价里表现为「这个品利润高得离谱」——比报错难发现得多。
    """
    if explicit:
        return str(explicit).upper()
    for s in price_strs:
        for sign, code in _CURRENCY_SIGNS:
            if sign in (s or ""):
                return code
    return ""


def _spec_to_pair(specs: list) -> str:
    """Temu 的 specs → skuMap 的「颜色>尺码」。规则同拼多多适配器（见那边的说明）。

    Temu 的 specKey 也是卖家自定义的（实测样本是「风格」单维），故同样按位置取，
    单维时尺码补「均码」让阶段⑧ 有东西可勾。
    """
    vals = [(s.get("v") or "").strip() for s in (specs or []) if (s.get("v") or "").strip()]
    if not vals:
        return "默认>均码"
    if len(vals) == 1:
        return f"{vals[0]}>均码"
    return f"{vals[0]}>{'/'.join(vals[1:])}"


def _price_of(sku: dict) -> float:
    """取实际售价（salePrice 优先于 normalPrice）。

    单位按「显示单位」处理，不除 100——实测日元站 salePrice=426 即 426 円
    （见模块 docstring 的 _ZERO_DECIMAL 那段说明）。
    """
    for key in ("salePrice", "normalPrice"):
        v = sku.get(key)
        if v in (None, "", 0):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


async def fetch(session: BrowserSession, url: str,
                on_manual=None, timeout: float = 40.0) -> SourceProduct:
    """抽 Temu 买家侧详情页的 SourceProduct。只读。

    【取数策略：先复用已打开的页签，再退回导航】2026-08-27 实测 Temu 会把对商品页的
    重新导航 302 到 login.html?login_scene=2（哪怕同一个 URL、同一个浏览器、页签里
    正开着完整数据）。用户从站内点进去的那个页签带着导航不可复现的会话上下文，
    故优先直接读它；页签不在时才 navigate，此时可能撞登录墙、由下面的判据报出来。
    这是 Temu 独有的处置，另三家都能直接导航（见各自适配器）。
    """
    gid = source_id(url, "temu")
    adopted = False
    if gid:
        # 按商品 ID 找页签而不是整个 URL 相等：用户打开的链接带一长串 _oak_* 参数，
        # 与任务里填的那条几乎不会字节相同，但 -g-<id> 这段是稳定的。
        r = await session.adopt_open_page(f"-g-{gid}")
        adopted = bool(r.get("ok"))
    if not adopted:
        logger.info(f"打开 Temu 详情页提取：{url}")
        r = await session.navigate(url)
        if not r.get("ok"):
            raise RuntimeError(f"导航失败: {r}")

    data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"), timeout=timeout)
    if not data.get("found"):
        probe = {}
        try:
            probe = await session.eval_json(_JS_BLOCKED)
        except RuntimeError as e:
            logger.debug(f"Temu 反爬检测执行失败（按未拦截处理）：{e}")
        if probe.get("blocked"):
            # 实测最常见的形态就是被 302 到 login.html（见 fetch 的取数策略说明）：
            # 这时不该让人去「完成校验」，而是让人把商品页在浏览器里打开着再跑。
            detail = str(probe.get("detail") or "未知")
            is_login = "login" in detail.lower()
            if is_login:
                hint = ("请在该 Chrome 窗口里手动打开这个商品页（从站内点进去最稳，"
                        "必要时先登录 Temu 账号），保持页签开着再重跑本商品")
            else:
                hint = "请切到该 Chrome 窗口手动完成校验（通常是长按按钮或刷新），过关后重跑"
            # 【拦截原因用人话，不贴 URL】原先直接把 login.html?from=... 的长串塞进
            # 提示里，截断后是「https://www.temu.com/login.html?from=https%3A%2F%2F」
            # 这种半截 URL，占满一行还什么都没说明。
            why = "被跳转到登录页" if is_login else f"命中校验（{detail[:40]}）"
            message = f"Temu 取数被拦（{why}）：{hint}"
            # 【等人处理，不直接失败】理由同拼多多适配器：人就在机器前，登录或把商品页
            # 点开只要几秒；报错则要重跑整个商品。
            if await wait_human(session, _JS_BLOCKED, "hasData", message,
                                on_manual=on_manual):
                data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"),
                                              timeout=timeout)
            if not data.get("found"):
                raise RuntimeError(f"{message}（等待超时或仍未就绪）")
        else:
            raise RuntimeError(
                "Temu 页面数据未就绪（window.rawData.store.goodsId 缺失）。"
                + ("" if adopted else
                   "本次是新导航打开的——Temu 常把重新导航跳到登录页，"
                   "请在该 Chrome 窗口里把商品页打开着再跑（见适配器 fetch 的说明）"))

    logger.info(f"Temu 页面数据就绪：{(data.get('title') or '')[:60]}")

    attrs: dict = {}
    for p in data.get("props") or []:
        key = (p.get("key") or "").strip()
        vals = [v.strip() for v in (p.get("values") or []) if v and v.strip()]
        if key and vals:
            attrs[key] = ",".join(vals)

    skus = []
    price_strs = []
    for s in data.get("skus") or []:
        skus.append({"spec": _spec_to_pair(s.get("specs")),
                     "price": _price_of(s),
                     "stock": s.get("stock")})
        if s.get("salePriceStr"):
            price_strs.append(s["salePriceStr"])

    currency = _guess_currency(data.get("currency") or "", price_strs)
    if not currency:
        logger.warning("Temu 页面未给出币种，源价格仅作参考（不要直接当人民币成本读）")

    videos = [v for v in (data.get("videos") or []) if v]
    # 尺码表：Temu 给的是 {show: 0/1}，show=0 表示该品无尺码表。有尺码表时的完整
    # 结构要另发接口取（买家页只给个开关），故这里只留档不解析——阶段⑨ 缺源尺码表
    # 时会走模型全量估算（pipeline 的 gen="model" 分支），不会因此失败。
    size_guide = data.get("sizeGuide") or {}

    prod = SourceProduct(
        platform="temu",
        url=url,
        productId=str(data.get("goodsId") or ""),
        title=(data.get("title") or "").strip(),
        attributes=attrs,
        skuMap=skus,
        mainImages=[strip_img_params(u) for u in (data.get("mainImages") or [])],
        descImages=[strip_img_params(u) for u in (data.get("descImages") or [])],
        # Temu 买家页不给商品重量（那是卖家后台字段），一律 None 交阶段⑩ 预估
        unitWeightKg=None,
        videoUrl=videos[0] if videos else "",
        extra={"goodsId": data.get("goodsId"), "cats": data.get("cats") or [],
               "mallName": data.get("mallName") or "",
               "currency": currency, "region": data.get("region") or "",
               "sizeGuideShow": size_guide.get("show") if isinstance(size_guide, dict) else None},
    )
    logger.info(f"Temu 提取完成：属性 {len(attrs)} 项 / SKU {len(skus)} 个 / "
                f"主图 {len(prod.mainImages)} 张 / 详情图 {len(prod.descImages)} 张"
                + (f" / 币种 {currency}" if currency else ""))
    return prod
