# -*- coding: utf-8 -*-
"""多平台来源适配层：把各平台商品详情页抽成同一份中间结构，交 extract 落盘。

【为什么要这一层，而不是在 extract.py 里加 if platform ==】
2026-08-27 实测采集箱 200 条草稿的来源分布：1688 占 124 条，其余 76 条分别是
拼多多 42（mobile.pinduoduo.com 40 + mobile.yangkeduo.com 2）、Temu 30、亚马逊 4。
这 76 条在 GUI 上一条都跑不通——前端把「无 offerId」的行只填 rowid（publish.html
的来源列逻辑），而 rowid 模式必须自带 product-info.json，于是它们卡死在阶段④
（service.py 的 _st_attrs 缺 info_path 直接 fail）。根因是 extract.py 无论收到什么
URL 都按 1688 处理（拼 detail.1688.com/offer/<id>.html、等 window.context）。

真正的差异【只在拿数据这一步】。extract.py 后半段——下图、main-NN.jpg 命名、
parse_main_composition、check_material_image、product-info.json 落盘、enrich_vision
视觉回填——全是平台无关的，且下游 11 个阶段全部只认落盘后的那份结构。故切法是把
「打开页面 + 抽出商品字段」抽成适配器，落盘与下游一个字不动。这样新增平台不必碰
pipeline.py（7774 行）里任何一处。

【中间结构 SourceProduct】刻意只有 8 个字段，与 product-info.json 不是一一对应：
    title / attributes / skuMap / mainImages / descImages / unitWeightKg / videoUrl / extra
适配器只管「这个平台的页面上有什么」，不管「店小秘要什么」——后者的转换
（属性键归一、SKU 透视、成分解析）留在 extract.py，因为那些规则是 Temu 侧的要求，
与来源平台无关。skuMap 用 1688 的既有形状 [{spec, price, stock}]，spec 是
「颜色>尺码」——不是偷懒，而是 pivot_skus 与阶段⑧ 的尺码勾选早就按这个形状写死了
（pipeline.py 的 skus 外层键即尺码），换形状要动阶段⑧⑨⑩⑪。

【平台识别只看域名，且必须是白名单】不做「含 6 位以上数字就当 offerId」这类猜测
（service.py 的 _task_key 就是这么写的，一个 temu URL 里任意数字都会被当 offerId）。
认不出来的域名一律抛 UnsupportedSourceError，让阶段① 明确失败并在 UI 上报出来——
静默按 1688 处理会打开一个不存在的 1688 页面，然后报「页面数据未就绪」，排查时
完全看不出真实原因。

【各平台的取数路径与实测结论】详见各适配器模块的 docstring。一句话概括：
    1688     window.context.result.data           服务端注入，最完整
    拼多多   window.rawData.store.initDataObj     goodsProperty 是干净的键值对
    Temu     window.rawData.store（顶层 goods/sku）同栈但路径不同，别复用拼多多的
    亚马逊   无内嵌 JSON，只能 DOM + 内联 script 里的 colorImages
"""
from app.publish.sources.base import (
    SourceProduct,
    UnsupportedSourceError,
    detect_platform,
    normalize_url,
    source_id,
)

__all__ = [
    "SourceProduct",
    "UnsupportedSourceError",
    "detect_platform",
    "normalize_url",
    "source_id",
    "get_adapter",
]


def get_adapter(platform: str):
    """按平台名取适配器模块。延迟导入：各适配器都要 import browser（Playwright），
    而 collectbox 只需要 detect_platform 判个域名，没必要为此拉起浏览器依赖链。
    """
    if platform == "1688":
        from app.publish.sources import alibaba1688 as m
    elif platform == "pdd":
        from app.publish.sources import pinduoduo as m
    elif platform == "temu":
        from app.publish.sources import temu as m
    elif platform == "amazon":
        from app.publish.sources import amazon as m
    else:
        raise UnsupportedSourceError(f"没有 {platform} 的来源适配器")
    return m
