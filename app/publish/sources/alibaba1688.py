# -*- coding: utf-8 -*-
"""1688 适配器：把 extract.py 里既有的 1688 取数逻辑包成统一的 fetch() 接口。

【为什么是薄包装而不是把代码搬过来】extract.py 里那两段 JS（_JS_EXTRACT 抓
window.context、_JS_DETAIL 抓描述图接口）、属性切分表 KNOWN_ATTR_KEYS、反爬闸门
wait_human_verify，全是对真站逐个试出来的，且已有单测覆盖
（test_publish_antibot.py、test_publish_desc_cors_fallback.py、test_publish_attrs.py
都直接 import extract 的符号）。搬动它们等于把这些验证过的逻辑重写一遍，收益为零、
风险全在。故本模块只做「调 extract 的现成函数 + 折成 SourceProduct」。

反过来说：1688 之所以没有独立的取数实现，正是因为 extract.py 本身就是它的实现。
新平台（拼多多/Temu/亚马逊）才需要各自写取数 JS。
"""
from app.logger import logger
from app.publish.browser import BrowserSession
from app.publish.sources.base import SourceProduct, source_id

# 1688 详情页的 url_hint：新建会话时用它挑页签（见 BrowserSession.open）
URL_HINT = "https://detail.1688.com"


def norm_spec(spec: str) -> str:
    """把 1688 的 specAttrs 归一成 skuMap 约定的「颜色>尺码」两维形状。

    【为什么 1688 也需要这一步】SourceProduct 的契约要求 spec 是「颜色>尺码」
    （见 sources/base.py），拼多多/Temu/亚马逊三个适配器都在自己那边把单规格商品
    补成「X>均码」，只有本适配器原样把 window.context 的 specAttrs 传出去——因为
    1688 时代采的全是服装，规格必然是「颜色 × 尺码」两维，单维商品一次都没出现过。

    2026-08-28 实测 offer 1014675972015（手工编织水果花束摆件，非服装）：6 个 SKU 的
    specAttrs 全是裸颜色名「【心想事橙】橙子花筒（life盆）」，一个 `>` 都没有。
    extract.pivot_skus 对不含 `>` 的 spec 是 `continue`（静默丢），于是
    product-info.json 落成 skus={} / colors=[] / sizes=[]，接着：
      阶段⑦ plan_skc 拿到空 colors → rows=[] → 报「视觉未给出任何颜色行选图」；
      阶段⑧ fix_sizes 拿到空 skus  → 报「product-info.json 无 skus 数据」→ 未落库。
    家居/玩具/饰品这类无尺码商品在 1688 上很常见，故必须在这里补齐。

    尺码维补「均码」而不是留空：阶段⑧ 要在页面上勾尺码，没有尺码就一个都勾不上，
    而「均码」在 pipeline._SIZE_ALIASES 里归一到 onesize、能命中平台的 One-Size 选项
    （见记忆 publish-size-onesize-alias）。这与拼多多/Temu 的处置完全一致。

    三维及以上（1688 允许卖家建多维规格）把第二维之后并进尺码维用「/」连：
    静默丢掉第三维会让不同 SKU 撞成同一个 spec，pivot 后互相覆盖。
    """
    t = str(spec or "").replace("&gt;", ">").strip()
    if not t:
        return "默认>均码"
    parts = [x.strip() for x in t.split(">") if x.strip()]
    if not parts:
        return "默认>均码"
    if len(parts) == 1:
        return f"{parts[0]}>均码"
    return f"{parts[0]}>{'/'.join(parts[1:])}"


async def fetch(session: BrowserSession, url: str,
                on_manual=None, timeout: float = 40.0) -> SourceProduct:
    """打开 1688 详情页并抽出 SourceProduct。只读。

    与其它三个适配器的差别：本平台【有反爬人工闸门】（滑块），命中时原地等人过关
    最多 10 分钟（见 extract.wait_human_verify）。拼多多/Temu/亚马逊都没有可拖的
    滑块，故那三家等的是【登录/验证码/把页面打开】（见各适配器的 wait_human 调用），
    但「停下来喊人、原地等、不直接失败」这个取向四家一致。

    【offerId 从 URL 抽而不是从页面数据取】_JS_EXTRACT 返回的字段里没有 offerId
    （它抓的是 gallery/productPackInfo/description 那几个区块）；原 extract_product
    也是用 re 从入参 URL 抽的。这里沿用同一来源，避免两处抽法不一致导致工作目录名
    与状态文件键对不上。
    """
    # 延迟导入：extract 会 import sources（取适配器），顶层互相 import 会成环
    from app.publish import extract as E

    logger.info(f"打开 1688 详情页提取：{url}")
    r = await session.navigate(url)
    if not r.get("ok"):
        raise RuntimeError(f"导航失败: {r}")

    # 反爬闸门放在等数据【之前】：被拦时 window.context 压根不存在，先干等 40s 再
    # 报「数据未就绪」纯属浪费——那 40s 里人本来就能把滑块拖完
    await E.wait_human_verify(session, url, on_manual=on_manual)
    data = await session.wait_for(E._JS_EXTRACT, lambda d: d.get("found"), timeout=timeout)
    if not data.get("found"):
        # 滑块也可能在首屏之后才弹（导航时那一刻还干净），故这里再判一次：
        # 等到人工过关后重读一次数据，而不是直接判失败
        if await E.wait_human_verify(session, url, on_manual=on_manual):
            data = await session.wait_for(
                E._JS_EXTRACT, lambda d: d.get("found"), timeout=timeout)
        if not data.get("found"):
            raise RuntimeError("页面数据未就绪（未登录或被拦截？）")
    logger.info(f"页面数据就绪：{data.get('subject')}")

    # 描述长图：先页面内 fetch（新形态 detailUrl 走这条即可），失败或抠不到图再
    # Python 直连——老形态 detailUrl 没有 CORS 头，页面内 fetch 必抛 Failed to fetch
    # （见 extract._fetch_desc_imgs_direct 的注释）。best-effort：拿不到就没有描述图。
    desc_imgs: list = []
    detail_url = data.get("detailUrl")
    if detail_url:
        from app.publish.browser import J
        try:
            d = await session.eval_json(E._JS_DETAIL.replace("__URL__", J(detail_url)))
            desc_imgs = d.get("imgs") or []
            if not desc_imgs:
                logger.warning(f"详情接口页面内 fetch 未抠到图（status={d.get('status')} "
                               f"len={d.get('len')}），改直连重试")
        except Exception as e:
            logger.warning(f"详情接口页面内 fetch 失败：{e}；改直连重试")
        if not desc_imgs:
            try:
                desc_imgs = E._fetch_desc_imgs_direct(detail_url)
                logger.info(f"详情接口直连成功：描述图 {len(desc_imgs)} 张")
            except Exception as e:
                logger.warning(f"详情接口直连也失败（描述图为空）：{e}")

    main_imgs = [u for u in (data.get("images") or []) if isinstance(u, str)]
    # 属性走 extract.parse_attrs 的键名表切分（1688 独有：属性容器取 textContent
    # 后键值完全粘连，没有分隔符；另三家平台都给结构化键值对，不需要这步）
    attrs = E.parse_attrs(data.get("attrText"))

    return SourceProduct(
        platform="1688",
        url=url,
        productId=source_id(url, "1688"),
        title=data.get("subject") or "",
        attributes=attrs,
        # spec 过 norm_spec 补两维：单规格商品（家居/玩具类）的 specAttrs 是裸颜色名，
        # 原样传下去会被 pivot_skus 整条丢掉，见 norm_spec 的实测说明
        skuMap=[{**s, "spec": norm_spec(s.get("spec"))}
                for s in (data.get("skuMap") or []) if isinstance(s, dict)],
        mainImages=main_imgs,
        descImages=list(desc_imgs),
        unitWeightKg=data.get("unitWeight"),
        videoUrl="",
        extra={
            "detailUrl": detail_url or "",
            # 原始抽取结果留档：raw.json 落的就是它，排查页面改版时要看
            "raw": data,
        },
    )
