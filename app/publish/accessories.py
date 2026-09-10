"""店小秘发布操作：accessories。模块导航见 docs/publish-pipeline-refactor.md。"""

from app.logger import logger
from app.publish import packaging
from typing import Optional


# 平台配件词表里服装相关的确切名（2026-08-27 逐词搜索实测所得）。
#
# 【为什么把词表写进提示词、而不是只让模型给通用词】页面侧对通用词做包含匹配，但
# 「上衣」搜出「西装上衣/便服上衣/露腰上衣」三项且都是 4 字，长度分不出优劣，按首项
# 会把牛仔花苞上衣归成「西装上衣」。让模型直接从真实词表里挑，选型这一步才有语义
# 判断参与。
#
# 【实测不在词表里的常用词，必须靠词表纠偏】「外套」「袜子」「套装」「棉服」「羽绒」
# 「校服」「开衫」「打底」「发带」「肚兜」搜索全返回空——模型不给词表就会张口给
# 「外套」，页面侧再怎么包含匹配也匹配不到，直接 option-not-found。对应的真实词是
# 夹克/防寒夹克/大衣、短袜/中筒袜/长筒袜。
#
# 【词表不能只收服装——2026-08-28 补非服装项】原先这里只有服装词，注释还写着
# 「只收服装类会用到的」，而提示词要求「配件名必须从下面词表里挑」、兜不住时「给一个
# 最接近的服装通用词」。offer 1014675972015（手工编织水果花束摆件，类目仿真花）于是
# 被判成「便服上衣x1」——仿真花填成上衣。
#
# 同日真站取证（草稿 173539495451708963 的配件下拉，只读开出来读选项）：该下拉
# 【不按类目过滤】，是全局可搜列表。默认列出「请选择配件、电源适配器、说明书、螺丝刀、
# 摆件、内裤、长袍、斗篷、电池、胶水」，搜「仿真」返回 仿真花/仿真植物/仿真叶/
# 仿真水果/仿真花瓣/仿真树枝/仿真花环…，搜「摆件」返回「摆件」，搜「装饰」返回
# 装饰品/装饰画/装饰带…。即平台早就有这些词，是我们的词表把选择面锁死在服装里了。
#
# 故按「服装 + 非服装」两段收词：非服装段收家居装饰/仿真花艺/通用附件这三类实测存在的
# 词。仍然只收【搜索确认存在】的词，不臆造——词表的作用是让模型在真实可选项里挑，
# 塞进不存在的词会让页面侧 option-not-found、白跑一轮重试。
_PACKING_ACCESSORY_WORDS = [
    "便服上衣", "西装上衣", "露腰上衣", "T恤", "衬衫", "无袖衬衫", "背心", "吊带背心",
    "内衣背心", "防寒背心", "马甲", "卫衣", "毛衣", "风衣", "夹克", "防寒夹克", "大衣",
    "连衣裙", "半身裙", "衬裙", "睡裙", "长裤", "短裤", "裙裤", "睡裤", "吊带裤",
    "护胸背带裤", "内裤", "连裤袜", "紧身裤袜", "长袍", "斗篷", "披肩", "睡衣",
    "连体睡衣", "保暖内衣", "保暖内衣裤", "塑身衣", "雨衣", "游泳衣", "连体泳衣",
    "分体泳衣", "泳裤", "帽子", "婴儿帽子", "头巾", "耳罩", "发夹", "腰带", "束腰带",
    "围巾", "手套", "连指手套", "露指手套", "分指手套", "领带", "领结", "蝴蝶结",
    "短袜", "中筒袜", "长筒袜", "压力袜", "工作服", "防护服", "围兜",
    "婴儿训练裤", "纸尿裤", "鞋垫", "鞋带", "凉鞋", "拖鞋", "运动鞋", "高跟鞋",
    # ---- 非服装（2026-08-28 逐词搜索实测存在）----
    # 仿真花艺/绿植：仿真花类目的商品主体就在这一段
    "仿真花", "仿真植物", "仿真叶", "仿真水果", "仿真花瓣", "仿真树枝", "仿真花环",
    "仿真羽毛", "仿真鸟", "仿真鱼",
    # 家居装饰摆件
    "摆件", "装饰品", "装饰画", "装饰带", "装饰牌", "装饰棒", "装饰纸", "装饰绳",
    "装饰灯", "花瓶", "花盆", "礼花筒",
    # 通用附件（各类目都可能带的随货物件）
    "说明书", "电池", "胶水", "电源适配器", "螺丝刀",
]


async def judge_sku_category(info: dict, cat_path: Optional[list] = None) -> dict:
    """判 SKU 分类 + 包装清单，返回 {"skuCat","qty","unit","packing":[{name,qty}],"reason"}。

    【为什么从 set_stock 里抽出来】它只看标题与套装件数/类型，与仓库、库存那两步
    的页面状态无关，因此可以在 ② 认领之前先跑（见 service._run_prewarm）。
    选项编码（1/2/3）的含义与 _JS_FILL_STOCK_CAT 的下拉序号绑定，改这里必须同步改那边。

    【为什么包装清单要和 SKU分类同一次问】2026-08-27 商品 1051793179451（两件套裙套装）
    发布被接口打回：「Mixed-Set SKU Accessories Num Sum Not Equal to Number of Pieces」
    ——平台强校验【包装清单件数之和 == SKU分类填的数量】，而原先包装清单是占位、
    一件都不填（和为 0），单件商品 qty=1 时也不符，只是历史商品恰好都没被拦到。
    两者必须一致，交给同一次判断产出，才不会出现「分类说 2 件、清单只列 1 件」的
    自相矛盾；产出后再由 _normalize_sku_judge 做一次硬对齐（模型仍可能算错和）。

    配件名优先从 _PACKING_ACCESSORY_WORDS（真实词表）里挑，模型给了通用词也能落地
    ——页面侧做包含匹配（见 _JS_FILL_PACKING 的 pickAccessory）。
    """
    from app.publish.llm import ask_json

    title = info.get("title", "")
    attrs = info.get("attributes") or {}
    img_prod = str((info.get("imageUnderstanding") or {}).get("product") or "").strip()
    elem = str(attrs.get("元素") or "").strip()
    tz_pieces = attrs.get("套装件数")
    tz_type = attrs.get("套装类型")

    # 【套装件数不能盲目默认为单件】1688 源商品极少有名为「套装件数」的属性，
    # 缺失时硬填「单件」会误导大模型把两件套判成单品（2026-09-07 商品 1013502117778 取证：
    # 标题短袖+背带裤两件套、类目女童牛仔两件套，却因「套装件数：单件」被模型判为 skuCat=1）。
    # 缺失时明确标注未注明，并把视觉理解、元素属性与类目一并喂给模型。
    pieces_desc = str(tz_pieces).strip() if tz_pieces else "未注明（请结合标题、图片视觉理解与类目综合判断）"
    type_desc = str(tz_type).strip() if tz_type else "未注明"

    info_lines = [
        f"- 标题：{title}",
        f"- 套装件数：{pieces_desc}",
        f"- 套装类型：{type_desc}",
    ]
    if elem:
        info_lines.append(f"- 元素/属性：{elem}")
    if cat_path:
        info_lines.append(f"- 平台类目：{' > '.join(str(c) for c in cat_path)}")
    if img_prod:
        info_lines.append(f"- 图片视觉理解：{img_prod}")
    info_block = "\n".join(info_lines)

    # 【品类识别：服装走词表、非服装走 LLM】预热时 cat_path 不可用，只能 title-only。
    # 按品类给包装清单的选词引导，替代原先笼统的「不是服装不要选服装词」——服装走
    # 既有细则，非服装按实际品类指路，避免仿真花/宠物窝被逼成服装词。
    cat = await packaging.classify_category(title, cat_path)
    if cat == "apparel":
        cat_guidance = ""
    elif cat == "pet_supply":
        cat_guidance = ("本商品是宠物窝/垫/床类，包装清单选宠物相关项或随货附件"
                        "（说明书/胶水等），不要选服装词。\n")
    elif cat == "home":
        cat_guidance = ("本商品是家居用品，包装清单选随货附件（说明书/电池/胶水等）"
                        "或装饰类对应项，不要选服装词。\n")
    elif cat == "toy":
        cat_guidance = ("本商品是玩具，包装清单选随货附件（说明书/电池/胶水等），"
                        "不要选服装词。\n")
    elif cat == "floral_decor":
        cat_guidance = ("本商品是仿真花艺/摆件/饰品，包装清单选「仿真花」「摆件」"
                        "「装饰品」等，不要选服装词。\n")
    elif cat == "shoe":
        cat_guidance = ("本商品是鞋类，包装清单选鞋类相关项，不要选服装词。\n")
    elif cat == "bag":
        cat_guidance = ("本商品是箱包，包装清单选随货附件，不要选服装词。\n")
    else:  # other
        cat_guidance = ("本商品不是服装时不要选服装词，按实际品类选；词表里实在没有"
                        "对应项时，才给一个最接近的同品类通用词（不要跨品类硬凑）。\n")
    prompt = (
        "你是跨境电商 Listing 专家。店小秘 Temu 半托管发布时需要为每个 SKU 填写「SKU分类」和「包装清单」。\n\n"
        f"商品信息：\n{info_block}\n\n"
        "SKU分类选项：1=单品（一个SKU只含一件商品） 2=同款多件（多件相同商品） 3=混合套装（多件不同商品组合）\n"
        "单位选项：1=件 2=双 3=包\n\n"
        "包装清单：逐项列出这个 SKU 实际装了哪些件，每项给配件名和件数。\n"
        "硬性要求：packing 里所有 qty 相加必须【正好等于】上面 SKU分类 的 qty（平台强校验，不符会发布失败）。\n"
        "配件名【必须】从下面平台词表里挑最贴合的一项，不在表内的词平台选不中：\n"
        + "、".join(_PACKING_ACCESSORY_WORDS) + "\n"
        "注意易错项：普通款上衣选「便服上衣」（正式/西装款才选「西装上衣」）；"
        "外套按款式选「夹克」「防寒夹克」或「大衣」（没有「外套」这一项）；"
        "袜子按长度选「短袜」「中筒袜」「长筒袜」（没有「袜子」这一项）。\n"
        # 【按品类给包装清单选词引导】见上方 cat_guidance——非服装不再笼统一句
        # 「不是服装不要选服装词」，而是按品类指路（2026-08-28 仿真花摆件曾被逼成
        # 「便服上衣」，故词表已含非服装段、且这里按品类引导）。
        f"{cat_guidance}"
        "示例：牛仔上衣+牛仔裙两件套 → skuCat=3, qty=2, "
        'packing=[{"name":"便服上衣","qty":1},{"name":"半身裙","qty":1}]；'
        '单件连衣裙 → skuCat=1, qty=1, packing=[{"name":"连衣裙","qty":1}]。\n\n'
        "请判断这个商品的 SKU分类（含数量、单位）与包装清单。\n"
        '只输出严格JSON：{"skuCat":"1|2|3","qty":数字,"unit":"1|2|3",'
        '"packing":[{"name":"配件名","qty":数字}],"reason":"一句话理由"}'
    )
    judge = await ask_json(prompt, what="SKU分类与包装清单判断", stage="stock")
    return _normalize_sku_judge(judge, info)


# 表外常用词 → 表内确切词（2026-08-27 实测这些词平台搜不到，模型却很爱给）。
# 只收「表外且搜索确认返回空」的词，表内词不进这里——键值同名会白绕一层。
_PACKING_ALIAS = {
    "外套": "夹克", "棉服": "防寒夹克", "棉衣": "防寒夹克", "羽绒服": "防寒夹克",
    "冲锋衣": "夹克", "皮衣": "夹克", "西装": "西装上衣", "上衣": "便服上衣",
    "袜子": "中筒袜", "短袜子": "短袜", "长袜": "长筒袜",
    "裤子": "长裤", "裙子": "半身裙", "半裙": "半身裙",
    "开衫": "毛衣", "针织衫": "毛衣", "打底衫": "T恤", "打底裤": "紧身裤袜",
    "校服": "工作服", "发带": "头巾", "肚兜": "围兜", "口水巾": "围兜",
    "运动服": "便服上衣", "背带裤": "护胸背带裤", "泳衣": "游泳衣",
    # 上下连身的婴幼童常见形态：平台词表里【没有】「连体裤/连体衣/爬服」这些词，
    # 归不动就会一路掉到「长裤」（真站取证 999389808041：源部件是连体裤，包装清单
    # 判成长裤 → 尺码分类取到「下装」，而源部件名判出的是「连体衣」，两边配不上、
    # 靠序号兜底才碰巧配对）。护胸背带裤是词表里语义最近的连身项，故统一归到它。
    "连体裤": "护胸背带裤", "连体衣": "护胸背带裤", "爬服": "护胸背带裤",
    "哈衣": "护胸背带裤", "连身衣": "护胸背带裤", "背带裙": "护胸背带裤",
    # 源属性「套装类型」给的合成词（见 product-info.json 的 attributes），只是补项
    # 兜底用；单件商品的清单仍由模型按标题逐项列。
    "裙套装": "半身裙", "裤套装": "长裤", "短裤套装": "短裤", "背带裤套装": "护胸背带裤",
}


def _canon_accessory(name: str) -> str:
    """把配件名归到平台词表里的确切词；已在表内或无从归一时原样返回。

    页面侧还有一层包含匹配兜底（见 _JS_FILL_PACKING），但那层挑不出「便服上衣 vs
    西装上衣」这种等长候选，且「外套」这类表外词它一个都匹配不到。故先在这里归一。
    """
    n = (name or "").strip()
    if not n or n in _PACKING_ACCESSORY_WORDS:
        return n
    if n in _PACKING_ALIAS:
        return _PACKING_ALIAS[n]
    # 表内词包含该名（「上衣」→「便服上衣」）时取最短候选：限定词最少、最通用
    hits = sorted((w for w in _PACKING_ACCESSORY_WORDS if n in w), key=len)
    if hits:
        return hits[0]
    # 反向：该名包含表内词（「牛仔夹克」→「夹克」）。取最长命中，「保暖内衣裤」优于「内衣」。
    rev = sorted((w for w in _PACKING_ACCESSORY_WORDS if w in n), key=len, reverse=True)
    if rev:
        return rev[0]
    # 到这里归不动就原样返回，交页面侧的包含匹配再试一次，仍不中则 option-not-found
    # 上报重试。【不做逐字猜】按单字命中会把「裙套装」判成「西装上衣」（末字「装」）、
    # 「护腕」判成「防护服」——按字符猜品类没有语义依据，错得比报错更难查。
    return n


# 标题关键词 → 词表内确切配件名。只在【模型没给清单】的兜底路径上用（见
# _normalize_sku_judge），正常路径由模型按提示词里的词表挑。
#
# 【为什么按关键词而不是让模型再来一发】兜底路径的前提就是那一发已经没给出可用结果，
# 同一个提示词再问一次没有理由变好；而这里只需要一个「不跨品类」的粗判，关键词足够。
# 顺序有讲究：先匹配更具体的词（仿真花 > 花 > 摆件），避免「仿真花束」被「花瓶」抢走。
_TITLE_ACCESSORY_HINTS = [
    ("仿真花", ("仿真花", "假花", "花束", "绢花")),
    ("仿真植物", ("仿真植物", "仿真绿植", "假植物", "仿真盆栽")),
    ("仿真水果", ("仿真水果", "假水果")),
    ("仿真花环", ("花环",)),
    ("花瓶", ("花瓶",)),
    ("花盆", ("花盆", "盆栽盆")),
    ("摆件", ("摆件", "桌面装饰", "办公桌面", "盆栽")),
    ("装饰画", ("装饰画", "挂画")),
    ("装饰灯", ("装饰灯", "氛围灯", "串灯")),
    ("装饰品", ("装饰", "饰品", "挂饰")),
]


# 品类线索完全不足时的兜底配件名。取「说明书」而不是任何具体品类词：它在平台词表内，
# 且各品类随货都可能带，填错的语义代价最小（原先写死「便服上衣」，非服装商品会被
# 张冠李戴成上衣，见 _normalize_sku_judge 里的说明）。
PACKING_FALLBACK_ACCESSORY = "说明书"


def _guess_accessory_by_title(title: str) -> str:
    """从标题猜一个词表内的配件名；猜不出返回空串（调用方据此明确失败，不硬填）。"""
    t = str(title or "")
    for word, keys in _TITLE_ACCESSORY_HINTS:
        if any(k in t for k in keys):
            return word
    return ""


def _normalize_sku_judge(judge: dict, info: dict) -> dict:
    """把模型给的 SKU分类/包装清单对齐成平台能过校验的形状。

    【为什么必须在判断层硬对齐、而不是信模型】平台校验的是「包装清单件数之和 ==
    SKU分类数量」，这是个算术约束，模型给 packing 时算错和是常事（列了 3 项但 qty
    仍写 2）。这里不造 fallback 分支，只做归一：
    - 配件名过 _canon_accessory 归到平台词表（模型爱给「外套」这类表外词）
    - packing 缺失或全空 → 按分类数量补一项（名字取源「套装类型」再归一）
    - 件数和与 qty 不等 → 以 packing 的实际和为准，反过来修正 qty（清单是实物构成，
      更接近事实；改 qty 只动一个数字，改 packing 要凭空猜拆分方式）
    单件商品同样要列 1 项：qty=1 而清单为空，和为 0 也不等于 1，一样会被打回。
    """
    out = dict(judge or {})
    attrs = info.get("attributes") or {}
    try:
        qty = int(str(out.get("qty", 1)).strip() or 1)
    except ValueError:
        qty = 1
    qty = max(1, qty)

    items = []
    for it in (out.get("packing") or []):
        name = _canon_accessory(str((it or {}).get("name", "")))
        if not name:
            continue
        try:
            n = int(str((it or {}).get("qty", 1)).strip() or 1)
        except ValueError:
            n = 1
        items.append({"name": name, "qty": max(1, n)})

    if not items:
        # 源「套装类型」多是「裙套装」这类词，归一后能落到「半身裙」。
        # 【兜底词不能写死「便服上衣」】2026-08-28：非服装商品（仿真花摆件）走到这里会被
        # 补成上衣。改为「套装类型 → 标题猜品类 → 通用兜底」三级，前两级都只给词表内的词。
        #
        # 【为什么最后仍要给一个词、而不是留空】平台强校验「清单件数和 == SKU分类数量」，
        # 空清单的和是 0，qty=1 也不相等，一样会被打回（见本文件上方那段真站取证与
        # test_publish_packing 的「件数和永远等于 qty 是不变量」）。留空只会把一次
        # 明确的失败换成另一次，还丢掉了「和必须相等」这条不变量。
        # 故兜底词取「说明书」：它在平台词表内、且是各品类随货都可能有的中性附件，
        # 比拿「便服上衣」去套一个仿真花至少不会张冠李戴。真拿不准时人工复核这一项即可。
        guess = (_canon_accessory(str(attrs.get("套装类型")
                                     or attrs.get("商品类别") or ""))
                 or _guess_accessory_by_title(info.get("title", ""))
                 or PACKING_FALLBACK_ACCESSORY)
        items = [{"name": guess, "qty": qty}]
        logger.warning(f"包装清单模型未给，按 SKU分类数量补一项：{guess} x{qty}"
                       + ("（品类线索不足，取中性附件兜底，建议人工复核这一项）"
                          if guess == PACKING_FALLBACK_ACCESSORY else ""))

    total = sum(x["qty"] for x in items)
    if total != qty:
        logger.warning(f"包装清单件数和 {total} 与 SKU分类数量 {qty} 不一致，以清单为准改 qty")
        qty = total

    out["qty"] = qty
    out["packing"] = items
    return out
