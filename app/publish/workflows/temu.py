"""Temu 商品 → 店小秘发布：保留源属性，成分按 Temu 自己的键解析。"""

import re

from .base import Stage, product_fields

AGE_ATTR_KEYS = ("适用年龄", "适用人群", "年龄段", "Age", "Age Range")
MAP_CHILD_LETTER_SIZES = False

# 成分属性键名。【只有这两个，是拿 12 条实测商品统计出来的，不是猜的】
# Temu 把含量和纤维写在【同一个值】里（「成分/封面成分 = 100% 涤纶」），与 1688/拼多多
# 「纤维一个键、含量另一个键」不同。**刻意不收「材质」「面料」「配件材质」**：实测
# 「材质=铝合金」（8 次，五金件）、「面料=微弹」（弹性描述）、「配件材质=橡胶」，收进来
# 会产出源里根本没有的假纤维。同理不收 1688/拼多多的键（主面料成分/面料材质），
# 那正是 tests/test_publish_workflows.py 的 test_composition_does_not_leak_between_sources
# 要防的串味。都没有时返回 {} 交阶段④ 的 LLM 判断。
COMP_ATTR_KEYS = ("成分/封面成分", "成分")
# 值里的百分比段：「100%」「95.5 %」都收
_PCT_IN_VALUE = re.compile(r"\d+(?:\.\d+)?\s*%")


# 尾部括号补充：「Asian L(L)」括号里只是把 L 又写了一遍，而店小秘页面选项是干净的
# 「Asian L」。2026-09-10 实测女装衬衫（源 ['Asian L(L)','Asian M(M)','Asian S(S)']
# vs 页面 ['Asian L','Asian M','Asian S','Asian Tall L',...]）因没剥而全部匹配不上，
# 整单卡在阶段⑧、一个尺码都没勾。只剥【尾部】——括号在中间时可能是主体信息
# （如「(套装)M」），不碰。这与 size_rules.norm_size 只取引导符前第一段的取向一致。
# 注：本函数与 amazon 共用 base.normalize_catalog_size，故剥括号放在这边、不进那个
# 共用件（amazon 侧的尺码格式未实测过，不连同改）。
_TAIL_PAREN = re.compile(r"\s*[（(【\[〔][^）)】\]〕]*[）)】\]〕]\s*$")


def normalize_size(value):
    from .base import normalize_catalog_size

    return normalize_catalog_size(_TAIL_PAREN.sub("", str(value or "")))


def clean_age_value(value):
    return str(value).strip()


def _split_fiber_value(value: str):
    """把「100% 涤纶」这类值拆成 (纤维名, 含量原文)。

    Temu 的成分值实测有两种形态：单成分「100% 涤纶」，多成分
    「68% 莱赛尔（天丝）,21% 聚酯纤维,10% Viscose,1% 弹性纤维」。
    多成分只取第一段——共享框架 _compose_from 只认「一个纤维 + 一个含量」，主面料成分
    这一行要的也是主成分，其余份额由阶段④ 依源 attributes 补差。

    前后顺序不固定（也见过「涤纶 100%」），故按百分比所在位置切，不假设谁在前。
    纤维名里的括号是别名/补充（「莱赛尔（天丝）」），剥掉——与拼多多适配器同一取向。
    """
    first = re.split(r"[,，、;；]", value or "")[0].strip()
    m = _PCT_IN_VALUE.search(first)
    pct = m.group() if m else ""
    fiber = (first[:m.start()] + first[m.end():]) if m else first
    fiber = re.sub(r"[（(].*?[)）]", "", fiber).strip(" ,，、/")
    return fiber, pct


def parse_composition(attrs):
    """解析 Temu 的成分属性；没有成分键时返回 {} 交阶段④ 的 LLM 判断。

    【为什么不套用 1688/拼多多那套默认值】那两家的源页面有稳定的成分键，缺失时按
    「聚酯纤维 100%」兜底是合理推断；Temu 的属性键名由卖家自填、各商品不同，硬兜底
    会给出一个源里根本没有的确定性值，比让 LLM 看着全部属性判更差。
    """
    from app.publish.extract import _compose_from

    attrs = attrs or {}
    value = ""
    for key in COMP_ATTR_KEYS:
        candidate = str(attrs.get(key) or "").strip()
        if candidate:
            value = candidate
            break
    if not value:
        return {}
    fiber, pct = _split_fiber_value(value)
    return _compose_from(fiber, pct)


def prepare_product(product):
    return product_fields(product, parse_composition(product.attributes))


async def publish_one(session, task, store, **kwargs):
    from . import get_workflow

    return await get_workflow("temu").publish_one(session, task, store, **kwargs)


async def run_batch(tasks, **kwargs):
    from . import get_workflow

    return await get_workflow("temu").run_batch(tasks, **kwargs)


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
        Stage("extract", "① Temu采集提炼", extracting._st_extract),
        Stage("claim", "② Temu商品认领", form._st_claim),
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
