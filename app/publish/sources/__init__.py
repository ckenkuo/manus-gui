"""来源适配器只负责读取源站。

1688、拼多多、Temu、亚马逊的发布阶段顺序与数据解释分别归
app.publish.workflows 下各来源管线所有。extract 负责通用落盘，
不再把非 1688 来源回退到 1688 数据规则。
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
