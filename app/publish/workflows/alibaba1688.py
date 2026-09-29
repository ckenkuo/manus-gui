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
    from app.publish.stages import carousel
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

    # 【⑦b 排在 ⑧ 之后不是笔误，2026-09-28 定】⑦b 的「补不上预览图的规格就反选掉」必须
    # 是动变种勾选的最后一步 —— ⑧ 的职责恰恰是「勾选状态与源 SKU 一致」，它会把 ⑦b 反选
    # 掉的规格重新勾回来，那些行就又是空图位，一路走到 ⑭ 才被平台拒「请上传预览图」。
    # 1688 offer 1067355258988（宠物保暖打底衫）实测：该单变种表第一维是【尺码】，⑦b 因
    # 补不上预览图反选的正是尺码 S/XL，⑧ 立刻把这两个尺码勾回来，反选等于白做。
    # ⑦b 反选到颜色维时与 ⑧ 不冲突（⑧ 只管尺码组），故此前一直没暴露。
    # 放 ⑧ 之后还有两个好处：一是 ⑨⑩a⑩⑪ 按行填数时拿到的是反选后的最终行集（变种表
    # 行数中途一变，按行回填的价/重/货号就全对不上了）；二是 ⑧ 重建变种表之后才动预览图，
    # ⑦b 换上的图不会再被下一次重建冲掉。
    # 【阶段 id 与名字保持 sku_preview/⑦b 不变】状态文件、续跑判定、四五个模块的注释与
    # 记忆库都按这个 id/记号认它，改名只会留下一堆对不上的记号。
    return (
        Stage("extract", "① 1688采集提炼", extracting._st_extract),
        Stage("claim", "② 1688商品认领", form._st_claim),
        Stage("auto_cat", "③ 产品类目", form._st_auto_cat),
        Stage("attrs", "④ 属性审核", form._st_attrs),
        Stage("titles", "⑤ 标题产地", form._st_titles),
        Stage("clean_images", "⑤b 图片清理", cleaning._st_clean_images),
        Stage("carousel", "⑤c 产品轮播图", carousel._st_carousel),
        Stage("material", "⑥ 素材图", material._st_material),
        Stage("drop_acc", "⑦a 剔配件色", skc._st_drop_acc),
        Stage("skc", "⑦ SKC颜色图", skc._st_skc),
        Stage("fix_sizes", "⑧ 尺码勾选", variants._st_fix_sizes),
        Stage("sku_preview", "⑦b SKU预览图", preview._st_sku_preview),
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
