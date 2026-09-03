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
import asyncio
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

    # 【2026-09-03 额外等待数据注入】navigate() 已改用 "load" 事件，但 1688 的
    # window.context 数据可能在 load 之后才异步注入。这里额外等待 2 秒，给数据注入
    # 脚本足够的执行时间。best-effort：如果 2 秒还不够，wait_for 会继续轮询 40 秒。
    await asyncio.sleep(2.0)

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
            # 【2026-09-03 增强错误诊断】如果 _JS_EXTRACT 返回了诊断信息，先打印出来
            if "diagnostic" in data:
                logger.error(f"页面数据结构诊断：{data['diagnostic']}")
                raise RuntimeError(
                    f"页面数据结构异常：window.context.result 存在但 data 字段不存在。\n"
                    f"诊断信息：{data['diagnostic']}\n"
                    f"这可能是 1688 改版或反爬机制导致的数据结构变化。"
                )

            # 旧版诊断（兼容没有 diagnostic 字段的情况）
            try:
                diag = await session.eval_json("""
                (() => {
                    const hasContext = typeof window.context !== 'undefined';
                    const hasResult = hasContext && typeof window.context.result !== 'undefined';
                    const loginBtn = document.querySelector('[class*="login"]') ||
                                     document.querySelector('a[href*="login"]');
                    const hasSlider = !!document.querySelector('[class*="slider"]') ||
                                     !!document.querySelector('[class*="verify"]') ||
                                     !!document.querySelector('#nc_1_wrapper');
                    return {
                        hasContext,
                        hasResult,
                        hasLoginBtn: !!loginBtn,
                        loginBtnText: loginBtn ? loginBtn.textContent.trim() : '',
                        hasSlider,
                        title: document.title,
                        url: location.href
                    };
                })()
                """)
                logger.error(f"页面诊断：{diag}")
            except Exception as e:
                logger.warning(f"诊断失败：{e}")

            raise RuntimeError(
                f"页面数据未就绪（未登录或被拦截？）\n"
                f"已等待 {timeout}s，window.context.result.data 仍不存在。\n"
                f"请检查：1) 是否已登录 1688  2) 是否有滑块验证  3) 页面是否正常加载"
            )
    logger.info(f"页面数据就绪：{data.get('subject')}")

    # 描述长图：先页面内 fetch（新形态 detailUrl 走这条即可），失败或抠不到图再
    # Python 直连——老形态 detailUrl 没有 CORS 头，页面内 fetch 必抛 Failed to fetch
    # （见 extract._fetch_desc_imgs_direct 的注释）。best-effort：拿不到就没有描述图。
    desc_imgs: list = []
    desc_text = ""
    detail_url = data.get("detailUrl")
    if detail_url:
        from app.publish.browser import J
        try:
            d = await session.eval_json(E._JS_DETAIL.replace("__URL__", J(detail_url)))
            desc_imgs = d.get("imgs") or []
            # 文字与图从【同一个响应体】剥出，故这里连着取：有些商家把整张尺码表打成
            # 明文而不是做成图（见 E.desc_text_of 的取证），只抠图会把它整段丢掉。
            desc_text = E.desc_text_of(d.get("html") or "")
            if not desc_imgs:
                logger.warning(f"详情接口页面内 fetch 未抠到图（status={d.get('status')} "
                               f"len={d.get('len')}），改直连重试")
        except Exception as e:
            logger.warning(f"详情接口页面内 fetch 失败：{e}；改直连重试")
        if not desc_imgs:
            try:
                desc_imgs, direct_text = E._fetch_desc_direct(detail_url)
                # 页面内那条已拿到文字时不覆盖：两条路径抠的是同一份内容，而先到的那份
                # 已经确认可读（直连走的是另一套编码判定，没必要再挑一次）
                desc_text = desc_text or direct_text
                logger.info(f"详情接口直连成功：描述图 {len(desc_imgs)} 张")
            except Exception as e:
                logger.warning(f"详情接口直连也失败（描述图为空）：{e}")
        if desc_text:
            logger.info(f"详情描述含文字内容 {len(desc_text)} 字（可能含明文尺码表）")

    main_imgs = [u for u in (data.get("images") or []) if isinstance(u, str)]
    # 属性走 extract.parse_attrs 的键名表切分（1688 独有：属性容器取 textContent
    # 后键值完全粘连，没有分隔符；另三家平台都给结构化键值对，不需要这步）
    attrs = E.parse_attrs(data.get("attrText"))

    # 【颜色缩略图】2026-09-03 新增：从页面颜色选择器提取每个颜色的缩略图 URL，
    # 供阶段⑦按源商品顺序对应、不再用 LLM 猜颜色归属（MJ20/MJ21 这类编码命名时
    # LLM 判断不准）。提取逻辑在 extract._JS_EXTRACT 里，这里只做【去重 + 验证】：
    #   - 同一个颜色名可能对应多个按钮（hover 状态/不同尺码共用颜色图），取第一个
    #   - 图 URL 必须是完整 http(s) 链接，data: 开头的 base64 占位图不要
    color_images_raw = data.get("colorImages") or []
    color_images = {}
    for item in color_images_raw:
        if not isinstance(item, dict):
            continue
        c = (item.get("color") or "").strip()
        img = (item.get("image") or "").strip()
        if c and img and img.startswith("http") and c not in color_images:
            color_images[c] = img
    if color_images:
        logger.info(f"提取到 {len(color_images)} 个颜色的缩略图：{list(color_images.keys())}")

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
        descText=desc_text,
        unitWeightKg=data.get("unitWeight"),
        videoUrl="",
        extra={
            "detailUrl": detail_url or "",
            # 颜色缩略图：{颜色名: 图URL}，阶段⑦优先用它对应、LLM 判断降级备选
            "colorImages": color_images,
            # 原始抽取结果留档：raw.json 落的就是它，排查页面改版时要看
            "raw": data,
        },
    )
