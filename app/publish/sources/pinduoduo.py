# -*- coding: utf-8 -*-
"""拼多多商品详情页适配器。

【取数路径】window.rawData.store.initDataObj.goods —— 服务端注入的 SSR 数据，
2026-08-27 在 CDP 浏览器上对 mobile.pinduoduo.com/goods.html?goods_id=985357680144
实测。与 1688 的 window.context.result.data 是同类东西（都是首屏 SSR 注入），故
等待策略照 1688 那套：轮询等对象挂上，不靠 DOM 渲染完成。

【拼多多与 Temu 同栈但路径不同，别复用】两家都把数据挂在 window.rawData.store
（同一个前端框架的痕迹），但拼多多在 store.initDataObj.goods 下，Temu 直接在
store 顶层（store.goods / store.sku）。实测两边的字段名也不同（拼多多
goodsProperty 里是 key/values，Temu 是 key/values + refPid/customProperty）。
写成一个适配器只会在某一天平台单方面改版时同时炸掉两个平台。

【字段实测结论】
  goodsProperty  干净的 [{key, values: [...]}]，比 1688 强得多——1688 的属性要从
                 textContent 里按已知键名表切（extract.parse_attrs 那 40 行），
                 拼多多直接给结构化键值对，故本适配器不需要任何切分启发式。
                 实测样本：品牌/材质/玩具类型/产地/填充物/适用性别 6 项。
  skus[].specs   [{spec_key, spec_value}]，双维（款式 × 尺寸）。注意 spec_key 是
                 【卖家自定义的】——实测「款式/尺寸」，不是固定的「颜色/尺码」。
                 故不能按键名找颜色维，只能按位置（见 _spec_to_pair）。
  groupPrice     拼团价（字符串），是买家实付价，即选品比价该用的价。normalPrice
                 是划线原价，不能拿它算成本。
  topGallery     轮播图，url 带 ?imageMogr2/... 的缩放参数——【必须剥掉】，否则下到
                 的是 1300px 压缩版而不是原图（阶段⑥ 的素材图要求 >=800x800，
                 压缩版勉强够但白丢分辨率；描述图撞 Temu 的 1340×1785 硬红线时
                 就直接不够了，见记忆 temu-cloth-image-min-size-gate）。
  detailGallery  详情长图，带 width/height，url 干净不带参数。
  viewImageData  与 topGallery 同源但已剥参数的版本——【不用它】：实测它只有 9 条
                 与 topGallery 等长，但没有 aspectRatio，且是「查看大图」用的，
                 平台改版时更容易变。剥参数自己做，规则透明。
"""
import re

from app.logger import logger
from app.publish.browser import BrowserSession
from app.publish.sources.base import SourceProduct, source_id, wait_human

# 商品数据挂载点。取到 goods 就算就绪——mall/queries 那些同级键与提取无关。
_JS_EXTRACT = r"""(() => {
  const st = ((window.rawData || {}).store || {});
  const g = ((st.initDataObj || {}).goods || {});
  if (!g.goodsID && !g.goodsId) return JSON.stringify({found: false});
  const num = v => (v == null || v === '' ? null : Number(v));
  return JSON.stringify({
    found: true,
    goodsId: String(g.goodsID || g.goodsId || ''),
    title: g.goodsName || g.goodsDesc || '',
    // 属性：[{key, values:[...]}] → 交 Python 侧折成 {键: 值}
    props: (g.goodsProperty || []).map(p => ({
      key: p.key || '', values: (p.values || []).map(String)})),
    // SKU：specs 是 [{spec_key, spec_value}]，价格取拼团价（买家实付）
    skus: (g.skus || []).map(s => ({
      specs: (s.specs || []).map(x => ({k: x.spec_key || '', v: x.spec_value || ''})),
      groupPrice: s.groupPrice, normalPrice: s.normalPrice,
      quantity: num(s.quantity), weight: num(s.weight),
      thumbUrl: s.thumbUrl || '',
    })),
    mainImages: (g.topGallery || []).map(x => (x && (x.url || x)) || '').filter(Boolean),
    descImages: (g.detailGallery || []).map(x => (x && (x.url || x)) || '').filter(Boolean),
    // 视频：videoGallery 实测多为空数组，有值时形如 [{videoUrl|url}]
    videos: (g.videoGallery || []).concat(g.descVideoGallery || [])
      .map(x => (x && (x.videoUrl || x.url || x.video_url)) || '').filter(Boolean),
    // 价格区间：单规格商品 skus 里也有价，但这两个是页面展示口径，留档便于对账
    minGroupPrice: g.minGroupPrice, maxGroupPrice: g.maxGroupPrice,
    linePrice: g.linePrice,
    cats: [g.catID1, g.catID2, g.catID3, g.catID4].filter(Boolean),
    mallName: (((st.initDataObj || {}).mall || {}).mallName) || '',
  });
})()"""

# 图片 URL 上的缩放/裁剪参数：拼多多用七牛的 imageMogr2 语法挂在 query 上。
# 剥掉才拿到原图（实测 topGallery 的 url 带 ?imageMogr2/quality/90/thumbnail/1300x9999>）。
# 【只剥 query 不动路径】路径里的 hash 是图片标识，动了就 404。
_IMG_PARAM_RE = re.compile(r"\?(?:imageMogr2|imageView2|x-oss-process)[^#]*", re.I)

# 拼多多详情页的反爬形态：整页跳到 login 或弹「请在拼多多 App 中打开」。
# 与 1688 的滑块不同——拼多多 H5 详情页对未登录也放开大部分数据，实测直接可读；
# 故这里命中即【提示人工并原地等】（wait_human），人要做的不是拖滑块而是登录、
# 换网络或在 App 里打开——等到页面恢复就自动继续，等不到才判失败。
_JS_BLOCKED = r"""(() => {
  const href = location.href;
  const urlHit = /\/login|\/verify|captcha/i.test(href);
  const t = (document.body ? (document.body.innerText || '') : '').slice(0, 2000);
  const words = ['请在拼多多','访问频繁','验证','拼多多APP','安全校验'];
  const hasData = !!(((window.rawData||{}).store||{}).initDataObj);
  const wordHit = (!hasData && words.find(w => t.includes(w))) || '';
  return JSON.stringify({blocked: !!(urlHit || wordHit), hasData: hasData,
                         detail: String(wordHit || (urlHit ? href : '')),
                         url: href, title: document.title || ''});
})()"""


def strip_img_params(url: str) -> str:
    """剥掉图片 CDN 的缩放参数，拿原图。空值原样返回。"""
    return _IMG_PARAM_RE.sub("", url or "")


def _spec_to_pair(specs: list) -> str:
    """把 [{k: 款式, v: 柠檬}, {k: 尺寸, v: 10cm}] 折成 skuMap 的 spec「颜色>尺码」。

    【为什么按位置而不按键名】拼多多的 spec_key 是卖家自定义的：实测样本是
    「款式 × 尺寸」，别的商品可能是「颜色 × 尺码」「口味 × 规格」。按键名找
    颜色维必然在某个商品上失配，而失配的表现是整个 skuMap 的 spec 缺一半、
    阶段⑧ 的尺码勾选拿不到尺码（pipeline 会报「product-info.json 无 skus 数据」）。

    位置约定沿用 1688 的「颜色>尺码」：第一维当颜色（SKC 分色维），第二维当尺码。
    实测拼多多详情页上第一维就是渲染在上面那行（款式/颜色），与 1688 一致。

    【单规格商品】只有一维时把它当颜色维、尺码给「均码」：
    阶段⑧ 要在页面上勾尺码，没有尺码就一个都勾不上；「均码」在别名表里有
    （见记忆 publish-size-onesize-alias 与 pipeline.norm_size），能匹配到平台的
    One-Size 选项。完全无规格（specs 空）时同理，颜色维给「默认」。
    """
    vals = [(s.get("v") or "").strip() for s in (specs or []) if (s.get("v") or "").strip()]
    if not vals:
        return "默认>均码"
    if len(vals) == 1:
        return f"{vals[0]}>均码"
    # 超过两维时把第二维之后的并进尺码维（用「/」连），不丢信息也不破坏两维形状。
    # 实测未见三维商品，但拼多多允许卖家建三维规格，静默丢掉第三维会让不同 SKU
    # 撞成同一个 spec、pivot 后互相覆盖（价格随机取到其中一个）。
    return f"{vals[0]}>{'/'.join(vals[1:])}"


def _price_of(sku: dict) -> float:
    """取买家实付价（拼团价优先）。两个都没有返回 None 让下游按缺价处理。

    groupPrice 是拼团价即实付，normalPrice 是划线原价——拿原价当成本会把利润算低、
    误杀本来可做的品。故顺序不能反。
    """
    for key in ("groupPrice", "normalPrice"):
        v = sku.get(key)
        if v in (None, "", 0, "0"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _clean_pdd_fiber(raw: str) -> str:
    """兼容入口，拼多多数据规则归独立发布管线所有。"""
    from app.publish.workflows.pinduoduo import clean_fiber

    return clean_fiber(raw)


def parse_composition(attrs: dict) -> dict:
    """兼容入口，转交拼多多发布管线解析成分。"""
    from app.publish.workflows.pinduoduo import parse_composition as parse

    return parse(attrs)


async def fetch(session: BrowserSession, url: str,
                on_manual=None, timeout: float = 40.0) -> SourceProduct:
    """打开拼多多详情页并抽出 SourceProduct。只读：不点任何按钮。

    session 由调用方（extract）负责开关，与 1688 适配器同一套所有权约定。
    on_manual 是人工提示通道，被反爬拦住时用它通知人（拼多多这边人工要做的是
    换 IP 或在 App 里打开，不是拖滑块，故提示文案与 1688 不同）。
    """
    # 先复用已打开的同商品页签（用户开着就直接读，省一次导航也省一次风控判定）；
    # 没开着才导航。拼多多实测导航是可行的（与 Temu 不同），故这里只是优化不是必需。
    gid = source_id(url, "pdd")
    if not (gid and (await session.adopt_open_page(f"goods_id={gid}")).get("ok")):
        logger.info(f"打开拼多多详情页提取：{url}")
        r = await session.navigate(url)
        if not r.get("ok"):
            raise RuntimeError(f"导航失败: {r}")

    data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"), timeout=timeout)
    if not data.get("found"):
        # 数据没就绪：先看是不是被拦，好把「反爬」与「页面结构变了」分开报
        probe = {}
        try:
            probe = await session.eval_json(_JS_BLOCKED)
        except RuntimeError as e:
            logger.debug(f"拼多多反爬检测执行失败（按未拦截处理）：{e}")
        if probe.get("blocked"):
            detail = str(probe.get("detail") or "未知")
            # 登录墙与风控验证的处置不同：前者要人登录，后者多半要换网络或在 App 里开
            if "login" in detail.lower():
                why = "被跳转到登录页"
                hint = "请在该 Chrome 窗口里登录拼多多账号，登录后流程会自动继续"
            else:
                why = f"命中风控（{detail[:40]}）"
                hint = ("请在该 Chrome 窗口里手动打开该商品页确认能正常浏览"
                        "（可能需要换网络或登录），处理好后流程会自动继续")
            message = f"拼多多访问被拦（{why}）：{hint}"
            # 【等人处理，不直接失败】人往往就在这台机器前，登录/换网络几秒就好，
            # 而报错要重跑整个商品（已下的图白丢）。与 1688 侧同一取向。
            if await wait_human(session, _JS_BLOCKED, "hasData", message,
                                on_manual=on_manual):
                data = await session.wait_for(_JS_EXTRACT, lambda d: d.get("found"),
                                              timeout=timeout)
            if not data.get("found"):
                raise RuntimeError(f"{message}（等待超时或仍未就绪）")
        else:
            raise RuntimeError("拼多多页面数据未就绪（window.rawData.store.initDataObj "
                               "缺失，可能是页面改版或未完全加载）")

    logger.info(f"拼多多页面数据就绪：{data.get('title')}")

    # 属性：[{key, values}] → {键: 值}。多值用「,」连——阶段④ 把整个 attributes
    # dumps 进提示词，列表与字符串对模型没区别，但统一成字符串能让 1688 那边写好的
    # 具名键读取（attrs["套装类型"] 之类）在拼多多来源上也不会拿到 list 而炸。
    attrs: dict = {}
    for p in data.get("props") or []:
        key = (p.get("key") or "").strip()
        vals = [v.strip() for v in (p.get("values") or []) if v and v.strip()]
        if key and vals:
            attrs[key] = ",".join(vals)

    skus = []
    for s in data.get("skus") or []:
        skus.append({"spec": _spec_to_pair(s.get("specs")),
                     "price": _price_of(s),
                     "stock": s.get("quantity")})

    # 单件重量：拼多多 sku.weight 实测恒为 0（卖家不填），故这里不硬凑一个 0——
    # 给 None 让阶段⑩ 转 LLM 预估（0 会被当成「已知重 0 克」直接填进包裹信息）。
    weights = [s.get("weight") for s in (data.get("skus") or [])
               if s.get("weight") not in (None, 0)]
    unit_kg = None
    if weights:
        # 拼多多的 weight 单位是克（实测字段值域与 1688 的 unitWeight 千克不同）。
        # 多 SKU 时取【最大值】：包裹重量报低了平台按实重收，差价由卖家承担；
        # 报高一点只是运费预估保守，没有实质损失。
        unit_kg = max(weights) / 1000.0

    videos = [v for v in (data.get("videos") or []) if v]
    prod = SourceProduct(
        platform="pdd",
        url=url,
        productId=str(data.get("goodsId") or ""),
        title=(data.get("title") or "").strip(),
        attributes=attrs,
        skuMap=skus,
        mainImages=[strip_img_params(u) for u in (data.get("mainImages") or [])],
        descImages=[strip_img_params(u) for u in (data.get("descImages") or [])],
        unitWeightKg=unit_kg,
        videoUrl=videos[0] if videos else "",
        extra={"goodsId": data.get("goodsId"), "cats": data.get("cats") or [],
               "mallName": data.get("mallName") or "",
               "minGroupPrice": data.get("minGroupPrice"),
               "maxGroupPrice": data.get("maxGroupPrice"),
               "linePrice": data.get("linePrice")},
    )
    logger.info(f"拼多多提取完成：属性 {len(attrs)} 项 / SKU {len(skus)} 个 / "
                f"主图 {len(prod.mainImages)} 张 / 详情图 {len(prod.descImages)} 张")
    return prod
