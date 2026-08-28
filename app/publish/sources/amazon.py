# -*- coding: utf-8 -*-
"""亚马逊商品详情页适配器（走 DOM + 内联 script，没有内嵌 JSON 可用）。

【与其它三个平台的根本差别，务必先读】
1688/拼多多/Temu 都把整份商品数据 SSR 注入到 window 上（window.context /
window.rawData.store），一次 evaluate 就拿全。亚马逊【没有】这种挂载点——
2026-08-27 在 CDP 浏览器上对 amazon.com/dp/B0DR3JT314 实测：
    window 顶层最大的对象是 ue（性能监控，28KB）、fwcim（风控），没有商品数据
    没有 __NEXT_DATA__ / __NUXT__ / rawData
    没有 JSON-LD（script[type="application/ld+json"] 为空数组）
故只能三路取数：
    标题/属性/五点  DOM（#productTitle、#prodDetails 表、#feature-bullets）
    主图            内联 script 里的 'colorImages' JSON（含 hiRes 1500px 原图）
    描述图          A+ 模块的 img（#aplus）
这条路比 SSR 脆——亚马逊的 DOM id 会随实验分桶变（同一 ASIN 不同用户可能拿到
不同版式），故每一路都写了多个候选选择器，且【任一路失败不阻断其余路】
（标题拿不到才算失败，属性/描述图为空只是降级）。

【变体是跨 ASIN 的，一页只有一个 SKU —— 这是与所有中国平台的结构性差异】
实测 dimensionValuesDisplayData 给的是 {ASIN: [风格名]} 的 5 条映射，即
「Samantha / Kirsten / Molly / Addy / Josefina」是 5 个独立 ASIN 各自一个商品页，
不是一页里的 5 个 SKU。这与 1688「一页 N 色 × M 码」完全不同。
处置：本适配器【只采当前 ASIN】，产出单条 skuMap（「默认>均码」），把变体表原样
记进 extra.variations 留档。理由——
  - 采全部变体要逐个 ASIN 导航，那是「批量采集」的职责（一个链接一个商品），
    不该由单商品提取悄悄膨胀成 5 次抓取（用户看到的是 1 个任务却跑了 5 倍时间）
  - 阶段⑧ 的尺码勾选需要「颜色×尺码」两维，跨 ASIN 变体喂进去也拼不出那个形状
用户要采多个变体时，把各 ASIN 链接分别填进任务清单即可（每行一个，本来就支持）。

【属性表三种版式都要读】实测这个 ASIN 只有 #prodDetails 有货（37 项），
#productOverview_feature_div 与 #detailBullets_feature_div 都是空的；换个类目
（服装类）实测是反过来的。故三处全读、合并，不假定哪一处一定有。

【重量与尺寸能直接拿到，比 1688 强】#prodDetails 里实测有
    "商品重量": "0.72 公斤"        → packInfo.unitWeightKg（阶段⑩ 直接用，不必问 LLM）
    "商品尺寸 长 x 宽 x 高": "20.3长度 x 10.2宽度 x 39.4高度 厘米"
故这里解析重量单位（公斤/克/磅/盎司都见过），见 _parse_weight_kg。
"""
import re

from app.logger import logger
from app.publish.browser import BrowserSession
from app.publish.sources.base import SourceProduct, source_id, wait_human

# 主取数：DOM 三路 + colorImages。
# 【为什么把 colorImages 的解析放在 JS 里】那段内联 script 是
# 'colorImages': { 'initial': A.$.parseJSON('[...]') } —— 里面是【被转义过的 JSON 串】。
# 在 JS 里 JSON.parse 一次即可；搬到 Python 侧要先反转义 \' 与 \\，规则重复且易错。
_JS_EXTRACT = r"""(() => {
  const txt = el => el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const one = sels => { for (const s of sels) { const e = document.querySelector(s);
    if (e && txt(e)) return txt(e); } return ''; };

  const title = one(['#productTitle', '#title span', 'h1#title']);
  if (!title) return JSON.stringify({found: false, reason: 'no-title'});

  // ---- 属性：三种版式合并（哪一处有货随类目/实验分桶变，见模块 docstring）----
  const attrs = {};
  const put = (k, v) => {
    k = (k || '').replace(/[\s:：‏‎‎‏]+$/g, '').replace(/^[\s‎‏]+/, '').trim();
    v = (v || '').replace(/\s+/g, ' ').trim();
    // 排除噪音行：亚马逊把「用户评分」「热销排名」也塞在同一张表里，且值里带
    // 整段 JS（实测「用户评分」的值含 dpAcrHasRegisteredArcLinkClickAction 脚本），
    // 这些进了 attributes 只会污染阶段④ 的提示词
    if (!k || !v || v.length > 300) return;
    if (/评分|排名|Best Sellers Rank|Customer Reviews/i.test(k)) return;
    if (!(k in attrs)) attrs[k] = v;
  };
  // 版式一：productOverview 表格（服装类常见）
  document.querySelectorAll('#productOverview_feature_div tr').forEach(tr => {
    const td = tr.querySelectorAll('td');
    if (td.length >= 2) put(txt(td[0]), txt(td[1]));
  });
  // 版式二：detailBullets 列表（图书/玩具常见）
  document.querySelectorAll('#detailBullets_feature_div li span.a-list-item').forEach(li => {
    const b = li.querySelector('span.a-text-bold');
    if (!b) return;
    put(txt(b), txt(li).replace(txt(b), ''));
  });
  // 版式三：prodDetails / techSpec 表（th+td）
  document.querySelectorAll(
    '#prodDetails tr, #productDetails_techSpec_section_1 tr, '
    + '#productDetails_detailBullets_sections1 tr, #technicalSpecifications_section_1 tr'
  ).forEach(tr => {
    const th = tr.querySelector('th'), td = tr.querySelector('td');
    if (th && td) put(txt(th), txt(td));
  });

  // ---- 主图：内联 script 的 colorImages（hiRes 是 1500px 原图）----
  let mainImages = [];
  const scripts = Array.from(document.querySelectorAll('script:not([src])'))
    .map(s => s.textContent || '');
  const ib = scripts.find(t => t.includes("'colorImages'") || t.includes('"colorImages"'));
  if (ib) {
    const m = ib.match(/['"]colorImages['"]\s*:\s*\{\s*['"]initial['"]\s*:\s*A\.\$\.parseJSON\('([\s\S]*?)'\)/)
      || ib.match(/['"]colorImages['"]\s*:\s*\{\s*['"]initial['"]\s*:\s*(\[[\s\S]*?\])\s*\}/);
    if (m) {
      try {
        const arr = JSON.parse(m[1].replace(/\\'/g, "'"));
        // hiRes 优先，缺了退 large（部分图只有 large）；thumb 一律不要（40px）
        mainImages = arr.map(x => x && (x.hiRes || x.large)).filter(Boolean);
      } catch (e) {}
    }
  }
  // 兜底：colorImages 解析不到时用 landingImage 的 data-a-dynamic-image
  // （那是个 {url: [w,h]} 字典，取最大的一张）——只有一张，但比没有好
  if (!mainImages.length) {
    const li = document.querySelector('#landingImage, #imgBlkFront, #main-image');
    const dyn = li && li.getAttribute('data-a-dynamic-image');
    if (dyn) {
      try {
        const d = JSON.parse(dyn);
        const best = Object.keys(d).sort((a, b) => (d[b][0] || 0) - (d[a][0] || 0))[0];
        if (best) mainImages = [best];
      } catch (e) {}
    } else if (li && li.src) { mainImages = [li.src]; }
  }

  // ---- 描述图：A+ 模块 ----
  // data-src 优先于 src：A+ 是懒加载的，未进视口的 img.src 是 1x1 占位图
  const descImages = Array.from(document.querySelectorAll(
      '#aplus img, #aplus_feature_div img, #aplusBrandStory_feature_div img'))
    .map(i => i.getAttribute('data-src') || i.src || '')
    .filter(u => u && !/^data:/.test(u) && !/\.gif($|\?)/i.test(u));

  // ---- 五点描述：源标题之外最主要的商品文案，进 attributes 供阶段④⑤ 读 ----
  const bullets = Array.from(document.querySelectorAll(
      '#feature-bullets li span.a-list-item'))
    .map(s => txt(s)).filter(Boolean);
  const descText = one(['#productDescription p', '#productDescription']);

  // ---- 价格 ----
  const price = one(['#corePrice_feature_div .a-offscreen', '#apex_desktop .a-offscreen',
    '#price_inside_buybox', '#priceblock_ourprice', '.priceToPay .a-offscreen',
    '#corePriceDisplay_desktop_feature_div .a-offscreen']);

  // ---- 跨 ASIN 变体表（只留档，不采：见模块 docstring）----
  let variations = null, currentAsin = '';
  const tw = scripts.find(t => t.includes('dimensionValuesDisplayData'));
  if (tw) {
    const m = tw.match(/["']dimensionValuesDisplayData["']\s*:\s*(\{[\s\S]*?\})\s*,\s*["']/);
    if (m) { try { variations = JSON.parse(m[1]); } catch (e) {} }
    const m2 = tw.match(/["']currentAsin["']\s*:\s*["'](\w+)["']/);
    if (m2) currentAsin = m2[1];
  }

  // ---- 视频：亚马逊的视频在 videoBlock 的 data-video-url ----
  const vid = document.querySelector('[data-video-url]');

  return JSON.stringify({
    found: true, title: title, attrs: attrs,
    mainImages: mainImages, descImages: descImages,
    bullets: bullets, descText: descText, price: price,
    variations: variations, currentAsin: currentAsin,
    videoUrl: vid ? (vid.getAttribute('data-video-url') || '') : '',
    asin: (location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1] || '',
    host: location.host, lang: document.documentElement.lang || '',
  });
})()"""

# 亚马逊的机器人拦截：整页变成 "Enter the characters you see below" 或 /errors/validateCaptcha
_JS_BLOCKED = r"""(() => {
  const href = location.href;
  const urlHit = /validateCaptcha|\/errors\/|\/ap\/signin/i.test(href);
  const t = (document.body ? (document.body.innerText || '') : '').slice(0, 1500);
  const words = ['Enter the characters you see below', 'Type the characters',
                 'Sorry, we just need to make sure', 'automated access', '请输入您看到的字符'];
  const hasTitle = !!document.querySelector('#productTitle');
  const wordHit = (!hasTitle && words.find(w => t.includes(w))) || '';
  return JSON.stringify({blocked: !!(urlHit || wordHit), hasTitle: hasTitle,
                         detail: String(wordHit || (urlHit ? href : '')),
                         url: href, title: document.title || ''});
})()"""

# 重量解析：实测「0.72 公斤」，另见过「12.3 grams」「1.5 pounds」「8 ounces」「350 g」。
# 各单位到千克的换算。中文单位与英文单位都要认——同一 ASIN 换语言参数
# （?language=en_AU vs zh）给的单位文案就不同。
_WEIGHT_UNITS = (
    (("千克", "公斤", "kilogram", "kilograms", "kg"), 1.0),
    (("克", "gram", "grams", "g"), 0.001),
    (("磅", "pound", "pounds", "lb", "lbs"), 0.45359237),
    (("盎司", "ounce", "ounces", "oz"), 0.028349523),
)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

# 属性表里表示重量的键名（各语言/版式）。按顺序试，先命中的胜。
# 【不要用「含『重』字就算」】「净重」「毛重」可以，但「重要提示」这类键会误命中。
_WEIGHT_KEYS = ("商品重量", "产品重量", "净重", "重量", "Item Weight", "Product Dimensions Weight",
                "Weight", "Shipping Weight", "包装重量")

# 尺寸键名，解析出来只留档（阶段⑩ 的包裹尺寸对服装类是固定 30x25x3，
# 见记忆 publish-declare-price-and-pack-dims；非服装类才问 LLM）
_DIM_KEYS = ("商品尺寸 长 x 宽 x 高", "产品尺寸", "商品尺寸", "Product Dimensions",
             "Item Dimensions LxWxH", "Package Dimensions")


def _parse_weight_kg(attrs: dict) -> float:
    """从属性表里解析单件重量（千克）。解析不出返回 None（交阶段⑩ 预估）。

    【为什么必须认单位】只抽数字会把「12 ounces」读成 12 千克，包裹重量差 350 倍，
    而阶段⑩ 填出去后要到平台称重才发现。故没认出单位时【一律返回 None】而不是
    赌一个默认单位——让 LLM 预估比按错单位填确定的错值好。
    """
    for key in _WEIGHT_KEYS:
        raw = (attrs.get(key) or "").strip()
        if not raw:
            continue
        m = _NUM_RE.search(raw)
        if not m:
            continue
        try:
            val = float(m.group().replace(",", "."))
        except ValueError:
            continue
        low = raw.lower()
        for names, factor in _WEIGHT_UNITS:
            # 单位名按长度降序比：先试「千克」再试「克」，否则「千克」会被「克」截获
            for name in sorted(names, key=len, reverse=True):
                if name.lower() in low:
                    kg = val * factor
                    # 合理性闸：跨境小包 0.001~50kg，越界说明解析错了（宁可不给）
                    if 0.001 <= kg <= 50:
                        return round(kg, 4)
                    logger.warning(f"亚马逊重量解析越界，忽略：{key}={raw!r} → {kg}kg")
                    return None
        logger.warning(f"亚马逊重量未认出单位，交阶段⑩ 预估：{key}={raw!r}")
        return None
    return None


async def fetch(session: BrowserSession, url: str,
                on_manual=None, timeout: float = 40.0) -> SourceProduct:
    """打开亚马逊详情页并抽出 SourceProduct。只读。

    只采当前 ASIN 的单个 SKU（跨 ASIN 变体只留档，理由见模块 docstring）。
    """
    # 先复用已打开的同 ASIN 页签（省一次导航，也少一次触发机器人验证的机会）；
    # 没开着才导航。亚马逊实测导航可行，故这只是优化。
    asin = source_id(url, "amazon")
    if not (asin and (await session.adopt_open_page(asin)).get("ok")):
        logger.info(f"打开亚马逊详情页提取：{url}")
        r = await session.navigate(url)
        if not r.get("ok"):
            raise RuntimeError(f"导航失败: {r}")

    data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"), timeout=timeout)
    if not data.get("found"):
        probe = {}
        try:
            probe = await session.eval_json(_JS_BLOCKED)
        except RuntimeError as e:
            logger.debug(f"亚马逊反爬检测执行失败（按未拦截处理）：{e}")
        if probe.get("blocked"):
            detail = str(probe.get("detail") or "未知")
            # /ap/signin 是登录墙，validateCaptcha 是验证码，两者处置不同
            if "signin" in detail.lower():
                why, hint = "需要登录", "请在该 Chrome 窗口里登录亚马逊账号，登录后流程会自动继续"
            else:
                why = f"要求人机验证（{detail[:40]}）"
                hint = "请切到该 Chrome 窗口手动输入验证码，过关后流程会自动继续"
            message = f"亚马逊访问被拦（{why}）：{hint}"
            # 【等人处理，不直接失败】理由同另两家：输个验证码或登录几秒就好，
            # 报错则要重跑整个商品。判据字段是 hasTitle（亚马逊没有内嵌数据可判）。
            if await wait_human(session, _JS_BLOCKED, "hasTitle", message,
                                on_manual=on_manual):
                data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"),
                                              timeout=timeout)
            if not data.get("found"):
                raise RuntimeError(f"{message}（等待超时或仍未就绪）")
        else:
            raise RuntimeError(
                f"亚马逊页面数据未就绪（{data.get('reason') or '无 #productTitle'}，"
                f"可能是页面改版或商品已下架）")

    attrs = dict(data.get("attrs") or {})
    logger.info(f"亚马逊页面数据就绪：{(data.get('title') or '')[:60]} / 属性 {len(attrs)} 项")

    # 五点描述与长描述并进 attributes：阶段④ 的属性审核与阶段⑤ 的标题生成都只读
    # attributes 与 title，这是把商品文案送进那两个阶段的唯一通道。用带书名号的键名
    # 避免与平台属性键撞名。
    bullets = [b for b in (data.get("bullets") or []) if b]
    if bullets:
        attrs["商品要点"] = " | ".join(bullets[:8])
    if data.get("descText"):
        attrs["商品描述"] = str(data["descText"])[:800]

    unit_kg = _parse_weight_kg(attrs)

    # 单 SKU：跨 ASIN 变体不采（见模块 docstring）。价格是带币种符号的字符串
    # （如 "JPY11,149"），故不塞进 skuMap 的 price（那里是数值），只留档到 extra。
    skus = [{"spec": "默认>均码", "price": None, "stock": None}]

    dims = next((attrs[k] for k in _DIM_KEYS if attrs.get(k)), "")
    variations = data.get("variations") or {}
    if len(variations) > 1:
        logger.info(f"亚马逊该商品有 {len(variations)} 个跨 ASIN 变体，本次只采当前 ASIN "
                    f"{data.get('currentAsin') or data.get('asin')}；"
                    f"要采其它变体请把各自链接分别加进任务清单")

    prod = SourceProduct(
        platform="amazon",
        url=url,
        productId=str(data.get("asin") or data.get("currentAsin") or ""),
        title=(data.get("title") or "").strip(),
        attributes=attrs,
        skuMap=skus,
        mainImages=[u for u in (data.get("mainImages") or []) if u],
        descImages=[u for u in (data.get("descImages") or []) if u],
        unitWeightKg=unit_kg,
        videoUrl=data.get("videoUrl") or "",
        extra={"asin": data.get("asin") or data.get("currentAsin") or "",
               "host": data.get("host") or "", "lang": data.get("lang") or "",
               "priceText": data.get("price") or "",
               "dimensionsText": dims,
               # 跨 ASIN 变体表：{ASIN: [维度值]}，留档供人工决定要不要逐个采
               "variations": variations},
    )
    logger.info(f"亚马逊提取完成：属性 {len(attrs)} 项 / 主图 {len(prod.mainImages)} 张 / "
                f"描述图 {len(prod.descImages)} 张"
                + (f" / 重量 {unit_kg}kg" if unit_kg else " / 重量未知（阶段⑩ 预估）"))
    return prod
