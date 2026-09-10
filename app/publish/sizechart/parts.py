"""店小秘发布操作：sizechart.parts。模块导航见 docs/publish-pipeline-refactor.md。"""

from app.logger import logger
from typing import Optional


# 配件件别 → 尺码分类关键词（用于给套装的两张尺码表各自选对分类）。
#
# 【为什么要按件别分派、不能两张都跟随平台预选】2026-08-27 实测两件套裙套装：弹窗的
# 尺码分类预选值两张都是「女童装-半身裙」，于是两张表的测量参数都是裙长/腰围全围。
# 平台的数量校验能过，但上衣那件量的是衣长/胸围——两张一样等于给买家一份错尺码表。
# 下拉里实际有 6 个选项（半身裙/下装/连衣裙/马甲/连体衣/上装），故按件别显式指定。
#
# 值是【关键词】而非完整分类名：完整名带类目前缀（女童装-/男童装-），前缀随类目变，
# _JS_SET_SIZECHART_CAT 用 includes 匹配，给关键词即可（同尺码分类不写死的既有取向）。
_ACCESSORY_SIZE_CATEGORY = {
    # 上装
    "便服上衣": "上装", "西装上衣": "上装", "露腰上衣": "上装", "T恤": "上装",
    "衬衫": "上装", "无袖衬衫": "上装", "卫衣": "上装", "毛衣": "上装",
    "风衣": "上装", "夹克": "上装", "防寒夹克": "上装", "大衣": "上装",
    "背心": "上装", "吊带背心": "上装", "内衣背心": "上装", "防寒背心": "上装",
    "工作服": "上装", "保暖内衣": "上装",
    # 马甲单列（平台有专门分类）
    "马甲": "马甲",
    # 下装
    "长裤": "下装", "短裤": "下装", "睡裤": "下装", "裙裤": "下装",
    "内裤": "下装", "泳裤": "下装", "婴儿训练裤": "下装", "纸尿裤": "下装",
    # 半身裙
    "半身裙": "半身裙", "衬裙": "半身裙",
    # 连衣裙
    "连衣裙": "连衣裙", "睡裙": "连衣裙", "长袍": "连衣裙",
    # 连体衣（含连体式泳衣/睡衣、背带裤这类上下连身的）
    "连体睡衣": "连体衣", "连体泳衣": "连体衣", "护胸背带裤": "连体衣",
    "吊带裤": "连体衣", "游泳衣": "连体衣", "防护服": "连体衣",
    "保暖内衣裤": "连体衣", "睡衣": "上装",
}


def _size_category_for(accessory: str) -> Optional[str]:
    """配件件别对应的尺码分类关键词；映射不到返回 None（表示跟随平台预选）。

    映射不到时【不猜】：宁可跟随平台按类目预选的值，也不要拿一个错分类去选——
    分类决定强制测量参数，选错会填出一张维度对不上实物的表（同 add_sizechart 里
    「尺码分类不要写死关键词」那条的取向）。
    """
    return _ACCESSORY_SIZE_CATEGORY.get((accessory or "").strip())


# 部件名（源尺码表图里的「部件：上衣/连衣裙」写法）→ 尺码分类关键词。
#
# 【为什么另建一张表、不复用 _ACCESSORY_SIZE_CATEGORY】那张表的键是平台包装清单词表里的
# 确切词（便服上衣/护胸背带裤…），而部件名是【1688 商家在图上随手写的】自由文本
# （上衣/T恤/短袖/背带裙/裤子/下装…），两套词汇不重合。这里只做「部件名 → 分类关键词」
# 的粗粒度归类，用于把分件实测表与两张平台尺码表配对，故按包含匹配、词短的在后面。
_PART_SIZE_CATEGORY_KEYWORDS = [
    # 连体衣优先：「背带裤」「连体」这类是上下连身，若先匹配到「裤」会归错
    ("连体衣", ("连体", "背带裤", "背带裙", "吊带裤", "工装裤裙", "爬服", "哈衣")),
    ("连衣裙", ("连衣裙", "长裙", "公主裙", "礼服")),
    ("半身裙", ("半身裙", "短裙", "包臀裙", "百褶裙", "裙子")),
    ("马甲", ("马甲",)),
    ("下装", ("裤", "下装", "打底裤", "短裤", "长裤")),
    ("上装", ("上衣", "上装", "t恤", "衬衫", "卫衣", "毛衣", "外套", "夹克",
              "开衫", "背心", "吊带", "短袖", "长袖", "针织衫", "打底衫")),
    # 「裙」放最后：前面几条更具体的都没中时才按裙处理（半身裙是童装里更常见的形态）
    ("半身裙", ("裙",)),
]


def _size_category_for_part(part: str) -> Optional[str]:
    """源图部件名对应的尺码分类关键词；判不出返回 None。"""
    t = (part or "").strip().lower()
    if not t:
        return None
    for cat, words in _PART_SIZE_CATEGORY_KEYWORDS:
        if any(w in t for w in words):
            return cat
    return None


def _category_keyword_of(selected: str) -> Optional[str]:
    """从平台实际选中的完整分类名（「女童装-连体衣」）反推分类关键词。

    调用方传 category=None 时走的是「跟随平台预选」，此时手上没有关键词，但配对分件
    实测表需要它。选中值是页面回读的事实，比重新猜一遍可靠。
    """
    t = (selected or "").strip()
    if not t:
        return None
    # 长的在前：候选之间虽不互相包含，但类目前缀（女童装-）也带「装」字，按长度取最稳
    for cat in sorted({c for c, _ in _PART_SIZE_CATEGORY_KEYWORDS}, key=len, reverse=True):
        if cat in t:
            return cat
    return None


async def _pick_part_measurements(parts: list, category: Optional[str],
                                  which: int, selected: str = "") -> tuple[dict, str]:
    """从分件实测表里挑出第 which 张平台尺码表该用的那一份，返回（实测表, 部件名）。

    selected：平台弹窗里实际选中并回读的完整分类名（「男童装-马甲」），词表配不上
    时喂给模型配对——完整名比 category 关键词多带类目前缀，信息更全。

    【为什么必须按部件挑、不能两张表都吃同一份】2026-08-29 真站取证（1688 商品
    1058585588864，T恤+牛仔背带裙两件套）：源详情图上明明分开给了两张表——
    「部件：上衣」量肩宽/袖长/前衣长/胸围，「部件：连衣裙」量前衣长/腰围——而原实现
    只读扁平的 sizeMeasurements（看图时两张表被压成一份），于是平台的「尺码表」与
    「尺码表2」填出【完全相同的数值】：两张都是 前衣长 42/45/48/51/54 + 胸围
    52/54/56/58/60。尺码分类倒是分对了（上装 / 连体衣），但数值同源，等于给买家一份
    错尺码表——上衣的衣长被套到了连衣裙上。这与 _ACCESSORY_SIZE_CATEGORY 上方那段
    「两张一样等于给买家一份错尺码表」是同一个坑的另一半（分类分了、数值没分）。

    配对按【尺码分类关键词】：分类是本张表实际强制的测量维度，用它配比用序号配
    可靠——包装清单的件序（便服上衣 + 半身裙）与源图部件序（连衣裙 在前、上衣 在后）
    并不保证一致，本商品就正好是反的。词表配不上时【升模型配对】（部件名是商家随手
    写的自由文本，词表永远穷举不完，见 _match_part_by_llm 的取证）；模型也判不出才
    退回按序号取，那至少还能让两张表拿到不同的数据（比同源好），并由调用方把实际
    用了哪个部件报进结果供人工复核。
    """
    if not parts:
        return {}, ""
    if len(parts) == 1:
        # 只有一张分件表时没什么可配的，两张平台表都用它（与序号兜底的结果一致，
        # 但省掉一次必然没有信息量的模型调用）
        e = parts[0]
        return e.get("measurements") or {}, e.get("part", "")
    if category:
        for e in parts:
            if _size_category_for_part(e.get("part", "")) == category:
                return e.get("measurements") or {}, e.get("part", "")
        # 词表配不上：升模型按「量的是不是同一件」配对
        idx = await _match_part_by_llm(selected or category, parts)
        if idx is not None:
            e = parts[idx]
            return e.get("measurements") or {}, e.get("part", "")
    idx = which if which < len(parts) else len(parts) - 1
    e = parts[idx]
    return e.get("measurements") or {}, e.get("part", "")


# 交模型做部件配对时的提示词。词表兜不住的部件名才走这里（见 _match_part_by_llm）。
_PART_MATCH_PROMPT = """你是服装套装尺码表的部件配对助手。

套装商品的每一件要在平台各填一张尺码表。当前要填的这张表，平台实际选中的尺码分类是：{category}
源商品详情里按部件分开给了 {n} 张实测尺寸表：
{part_lines}

请判断这张平台尺码表应该取哪一张源部件表。规则：
1. 按「量的是不是同一件」判断：部件名是商家随手写的自由文本，与平台分类叫法可能
   不同（如平台分类「马甲」，源部件写的是「上衣」）；
2. 部件名判不准时看测量参数：量胸围/衣长/肩宽的是上半身件，量腰围/臀围/裤长/裙长
   的是下半身件，上下身都量的是连身件；
3. 没有一张源表对应这件（如套装有 3 件、源只给了 2 张表），index 回答 -1，不要硬配。

只输出严格JSON，不要其他文字：{{"index": 数字}}（index 是上面源部件表的序号，从 0 开始）"""


async def _match_part_by_llm(category: str, parts: list) -> Optional[int]:
    """分类词表配不上任何源部件时，问模型这张表该取哪张分件实测表；判不出返回 None。

    【为什么不继续补词表、要升模型】部件名是 1688 商家在图上随手写的自由文本
    （坎肩/小褂/马甲/背心都可能是同一件），词表永远穷举不完——2026-09-08 商品
    1050772789299（牛仔马甲+长裤两件套）：平台分类选了「马甲」，源部件写的是
    「上衣」，词表配不上掉到序号兜底，给马甲表拿了【裤子】的实测值，与平台要的
    胸围全围/衣长一列都对不上、整表改走凭空估算。这与 _map_params_by_llm 同一
    取向：配对只是选个序号，输入输出都极短（一次几百 token），比拿错件划算。
    喂模型的线索除了部件名还有每张表的测量参数名：参数名比部件名诚实（量臀围/
    裤长的必是下装），部件名写成「部件A」也配得对。

    best-effort：失败一律吞掉返回 None（调用方随后掉序号兜底），同 _map_params_by_llm。
    """
    from app.publish.llm import ask_json

    lines = []
    for i, e in enumerate(parts):
        params = sorted({p for row in (e.get("measurements") or {}).values()
                         for p in (row or {})})
        lines.append(f"{i}. 部件「{e.get('part', '')}」（参数：{'、'.join(params)}）")
    try:
        data = await ask_json(
            _PART_MATCH_PROMPT.format(category=category, n=len(parts),
                                      part_lines="\n".join(lines)),
            what="尺码表部件配对", stage="sizechart")
        idx = int((data or {}).get("index", -1))
    except Exception as e:
        logger.warning(f"尺码表部件配对失败（掉序号兜底）：{e}")
        return None
    if 0 <= idx < len(parts):
        logger.info(f"尺码表部件配对（模型）：分类「{category}」→ 源部件"
                    f"「{parts[idx].get('part', '')}」")
        return idx
    return None
