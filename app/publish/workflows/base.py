"""来源发布流程的身份校验和共用数据契约。"""

from dataclasses import dataclass
import re

from app.publish.sources.base import detect_platform, source_id


class SourceMismatchError(ValueError):
    """任务、断点或商品数据属于不同来源。"""


def identity(info):
    if not isinstance(info, dict):
        raise SourceMismatchError("商品数据必须是 JSON 对象")
    source = info.get("source") or {}
    if not isinstance(source, dict):
        raise SourceMismatchError("商品数据的 source 必须是对象")
    platform = source.get("platform") or ""
    url = source.get("url") or source.get("canonicalUrl") or ""
    detected = detect_platform(url) if url else ""
    if platform and detected and platform != detected:
        raise SourceMismatchError("商品数据的 source.platform 与 source.url 不一致")
    platform = platform or detected
    if not platform and source.get("offerId") and not source.get("productId"):
        platform = "1688"
    product_id = str(source.get("productId") or source.get("offerId") or "")
    url_id = source_id(url, platform) if url else ""
    if product_id and url_id and product_id != url_id:
        raise SourceMismatchError("商品数据的商品 ID 与来源链接不一致")
    return platform, product_id or url_id


@dataclass(frozen=True)
class PublishWorkflow:
    platform: str
    name: str
    rules: object

    @property
    def workflow_id(self):
        return f"{self.platform}-dianxiaomi"

    def validate_info(self, info, url=""):
        platform, product_id = identity(info)
        if platform and platform != self.platform:
            raise SourceMismatchError(
                f"{self.name}不能使用 {platform} 商品数据")
        if info.get("workflow_id") not in (None, "", self.workflow_id):
            raise SourceMismatchError("商品数据属于另一条发布流程")
        if url:
            if detect_platform(url) != self.platform:
                raise SourceMismatchError(f"{self.name}与任务来源链接不一致")
            expected = source_id(url, self.platform)
            if expected and product_id and expected != product_id:
                raise SourceMismatchError("任务与 product-info.json 的商品 ID 不一致")

    def prepare_product(self, product):
        if product.platform != self.platform:
            raise SourceMismatchError(f"{self.name}收到了 {product.platform} 适配器的数据")
        self.validate_info({"source": {
            "platform": product.platform,
            "url": product.url,
            "productId": product.productId,
        }})
        return self.rules.prepare_product(product)

    def stages(self):
        stages = self.rules.build_stages()
        names = [stage.key for stage in stages]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.name}存在重复阶段")
        return stages

    def validate_task(self, task, state=None):
        from . import resolve_workflow

        state = state or {}
        info_path = task.get("info_path") or state.get("info_path")
        info = {}
        if info_path:
            from app.publish.state import _load_info

            info = _load_info(info_path)
        selected = resolve_workflow(task, state, info)
        if selected and selected.platform != self.platform:
            raise SourceMismatchError(f"{self.name}不能运行另一来源的任务")
        self.validate_info(info, task.get("url") or "")
        return {**task, "source_platform": self.platform}

    async def publish_one(self, session, task, store, **kwargs):
        from app.publish.service import _run_product

        return await _run_product(session, self.validate_task(task), store,
                                  workflow=self, **kwargs)

    async def run_batch(self, tasks, **kwargs):
        from app.publish.service import run_batch

        tasks = [self.validate_task(task) for task in tasks]
        return await run_batch(tasks, **kwargs)


@dataclass(frozen=True)
class Stage:
    key: str
    name: str
    run: object


def product_fields(product, composition):
    """只透视适配器已明确归一到颜色/尺码两维的 SKU。"""
    from app.publish.extract import pivot_skus

    skus, colors, sizes = pivot_skus(product.skuMap or [])
    return {"mainComposition": composition, "skus": skus,
            "colors": colors, "sizes": sizes}


def normalize_catalog_size(value):
    """保留零售源的版型、数字区间和单位，仅归一明确的通用尺码别名。"""
    value = str(value).strip()
    key = re.sub(r"[\s_-]+", "", value).lower()
    if key in {"均码", "均", "单码", "通用", "通用码", "f", "free", "freesize", "onesize"}:
        return "onesize"
    age = re.fullmatch(r"(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*(m|y|t)", value, re.I)
    if age:
        suffix = "m" if age[3].lower() == "m" else "y"
        return age[1] + ("-" + age[2] if age[2] else "") + suffix
    return value
