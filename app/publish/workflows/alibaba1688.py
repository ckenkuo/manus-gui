"""1688 商品 → 店小秘发布：1688 属性解释规则。"""

from .base import Stage, product_fields

AGE_ATTR_KEYS = ("适合年龄段", "适用年龄", "年龄段", "适合身高", "适用身高")
MAP_CHILD_LETTER_SIZES = True


def normalize_size(value):
    from app.publish.size_rules import norm_size

    return norm_size(value, unwrap=False, diameter=False)


def clean_age_value(value):
    import re

    return re.split(r"主要下游平台|主要销售地区|下游平台|销售地区", str(value))[0].strip(" ,，")


def parse_composition(attrs):
    from app.publish.extract import _compose_from

    attrs = attrs or {}
    return _compose_from(
        attrs.get("主面料成分") or attrs.get("面料名称") or "",
        attrs.get("主面料成分含量") or "",
    )


def prepare_product(product):
    return product_fields(product, parse_composition(product.attributes))


async def publish_one(session, task, store, **kwargs):
    from . import get_workflow

    return await get_workflow("1688").publish_one(session, task, store, **kwargs)


async def run_batch(tasks, **kwargs):
    from . import get_workflow

    return await get_workflow("1688").run_batch(tasks, **kwargs)


def build_stages():
    from app.publish.stages import cleaning
    from app.publish.stages import description
    from app.publish.stages import extracting
    from app.publish.stages import form
    from app.publish.stages import material
    from app.publish.stages import preview
    from app.publish.stages import saving
    from app.publish.stages import shipping
    from app.publish.stages import skc
    from app.publish.stages import variants
    from app.publish.stages import video

    return (
        Stage("extract", "① 1688采集提炼", extracting._st_extract),
        Stage("claim", "② 1688商品认领", form._st_claim),
        Stage("auto_cat", "③ 产品类目", form._st_auto_cat),
        Stage("attrs", "④ 属性审核", form._st_attrs),
        Stage("titles", "⑤ 标题产地", form._st_titles),
        Stage("clean_images", "⑤b 图片清理", cleaning._st_clean_images),
        Stage("material", "⑥ 素材图", material._st_material),
        Stage("drop_acc", "⑦a 剔配件色", skc._st_drop_acc),
        Stage("skc", "⑦ SKC颜色图", skc._st_skc),
        Stage("sku_preview", "⑦b SKU预览图", preview._st_sku_preview),
        Stage("fix_sizes", "⑧ 尺码勾选", variants._st_fix_sizes),
        Stage("sizechart", "⑨ 尺码表", variants._st_sizechart),
        Stage("sku_code", "⑩a SKU货号", variants._st_sku_code),
        Stage("variant", "⑩ 变种信息", variants._st_variant),
        Stage("stock", "⑪ 库存SKU", variants._st_stock),
        Stage("shipping", "⑫ 运输信息", shipping._st_shipping),
        Stage("desc", "⑬ 描述长图", description._st_desc),
        Stage("video", "⑬b 产品视频", video._st_video),
        Stage("save", "⑭ 保存落库", saving._st_save),
        Stage("publish", "⑮ 立即发布", saving._st_publish),
    )
