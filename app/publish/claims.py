"""发布管线共用的夸大宣传判据：词表、文本闸门、以及给模型看的提示词片段。

【为什么要单独一个模块】收窄夸大宣传这件事要在三类地方同时成立：
  1. 生成类文本（标题、货号、尺码译文）——生成后用正则硬闸拦；
  2. 图片英化（出图提示词）——要告诉生图模型「营销标语不翻译、直接抹掉」；
  3. 图片质检与复核（视觉模型）——要告诉它「什么算夸大宣称」并回一个布尔。
这三处的口径必须一致：判据在 A 处拦、提示词在 B 处没提，表现就是生图一遍遍把
营销词原位译成英文、质检一遍遍判不过，最后发数烧完退回原图（正是
stages.cleaning_rules._retry_hint 那段实测记录里的失败形态）。各写一份词表迟早漂移，
故词表与说法都只留这一处。

【判据只针对「后期叠加的文案」】商品实物上的印花、刺绣、织标即使印着 BEST 之类的
字样，也是实物的一部分，选品阶段已人工确认过——图片侧的提示词一律把它排除在外
（同 vision.check_cleaned 对实物绣标的取向：误报的代价是好图被抹、整单停摆）。

【为什么不做「疑似就拦」的模糊匹配】词表走的是精确词 + 必要的品类词掩码。真正的
坑不是漏拦一个生僻夸大词，而是误伤品类词把正常商品拦死：Tank Top / Essential Oil /
Table Top 这类搭配里的词根本不是宣称，靠掩码摘掉后再查禁词，闸门不因此变松
（营销用法的 Top Quality、Must Have 都不在掩码名单里）。
"""

import re


# 「修饰词 + Top(s)」是品类词或部件名（Tank Top 工字背心 / Crop Top 露脐上衣 /
# Table Top 台面），不是 Temu 禁的「顶级/第一」宣称。下面的 \btop\b 不区分这两种用法，
# 2026-09-11 实测女童背心（1041397908049）两轮 6 个候选全因 "Tank Top"/"Summer Top"
# 被拦死（title-generation-failed）。故查禁词前先把这些已知搭配整体摘掉。
# 【名单之外仍按禁词拦】营销用法的 "Top" 在名词前（Top Quality / Top Rated）或独立
# 出现（Our Top Pick 的 Our 不在名单），都不受这个掩码影响，闸门没有变松。
_TOP_NOUN_RE = re.compile(
    r"\b(?:tank|crop|tube|halter|bandeau|camisole?|bikini|vest|peplum|corset|"
    r"bralette|bra|polo|knit(?:ted)?|ribbed|smocked|ruffled?|lace|mesh|denim|"
    r"satin|silk(?:y)?|cotton|linen|wool(?:en)?|fleece|thermal|seamless|"
    r"sleeveless|short[- ]sleeve|long[- ]sleeve|spaghetti[- ]strap|backless|"
    r"strapless|padded|sports?|yoga|swim|lounge|sleep|maternity|nursing|"
    r"basic|casual|summer|winter|plus[- ]size|"
    # 家具/台面类：Table Top、Counter Top 这些是部件名，同样不是「顶级」宣称
    r"table|counter|desk|bench|bed|roof|stove|dresser)\s+tops?\b",
    re.I)

# 同一类掩码的其余品类词：这些搭配整体是品类名，其中的词不作宣称处理。
# essential oil 精油、magic tape 魔术贴、super glue 强力胶、hot water bottle 热水袋
# ——它们与 Essential/Magical/Superb/Hot Sale 的宣称用法不是一件事。
_CATEGORY_MASK_RE = re.compile(
    r"\bessential\s+oils?\b"
    r"|\bmagic\s+(?:tape|eraser|cube|sand|clay|water)\b"
    r"|\bsuper\s+(?:glue|single|market)\b"
    r"|\bhot\s+(?:water|pot|plate|pad|glue|air|dog|melt)\b",
    re.I)

# 英文夸大宣传词表，按平台罚点分组（Temu「禁止绝对化用语与未经证实的宣称」）。
# 【分组只为可读】任一命中即拦，顺序无关。
_EN_CLAIM_PATS = (
    # 最高级 / 绝对化
    r"\b(?:best|perfect|flawless|ultimate|ideal|finest|greatest|superb)\b",
    r"\b(?:unbeatable|unmatched|unrivaled|unparalleled|incomparable)\b",
    r"\b(?:top|leading|premier|superior)\b",
    r"\b(?:top|high)[\s-]?(?:quality|grade|notch|tier|rated|end)\b",
    r"\b(?:world|professional|commercial)[\s-]?(?:class|grade)\b",
    r"\bmost\s+(?:popular|loved|wanted|advanced|durable|comfortable|"
    r"reliable|trusted|beautiful)\b",
    # 第一 / 排名
    r"\b(?:number[\s-]?one|#\s*1|no\.?\s*1)\b",
    r"\baward[\s-]?winning\b",
    # 销量 / 热度宣称（平台无从核实，是「BEST-SELLER」这类角标的主要形态）
    r"\b(?:best|top|hot|fast)[\s-]?sell(?:er|ers|ing)?\b",
    r"\bbestsellers?\b",
    r"\bhot\s*(?:sale|item|deal|pick|style)\b",
    r"\b(?:trending|viral|popular|craze)\b",
    r"\b(?:millions?|thousands?|\d[\d,]*\+?)\s*(?:sold|buyers?|reviews?)\b",
    # 必备 / 必买
    r"\bmust[\s-]?(?:have|haves|buy|own|see|get|try)\b",
    r"\b(?:essential|necessary|indispensable)\b",
    # 品质 / 身份夸大
    r"\b(?:premium|luxury|luxurious|deluxe|exquisite|high[\s-]?end)\b",
    r"\b(?:exclusive|unique|one[\s-]?of[\s-]?a[\s-]?kind|irreplaceable)\b",
    # 情绪化 / 效果夸大
    r"\b(?:amazing|incredible|unbelievable|revolutionary|sensational)\b",
    r"\b(?:stunning|gorgeous|fabulous|marvelous|spectacular|breathtaking)\b",
    r"\b(?:extraordinary|magical|miracle|miraculous|insane|crazy\s+good)\b",
    r"\b(?:game[\s-]?chang(?:er|ing)|life[\s-]?changing)\b",
    # 保证 / 承诺（能不能兑现不由商品页决定）
    r"\b(?:guaranteed?|risk[\s-]?free|satisfaction\s+guaranteed)\b",
    r"\b100\s*%\s*(?:satisfaction|guaranteed?|perfect|best|safe)\b",
)

# 中文夸大宣传词表。中文标题虽只进店小秘的中文字段，但它是后续人工复制、活动报名
# 文案的来源，同样按一套口径收窄。
# 【注意「超值/特价」类归价格宣称】那类由各调用处的价格闸负责，这里不重复列。
# 【写成一条交替式、不要写成元组】这里曾是只含一个长字符串的元组，被
# "|".join(_ZH_CLAIM_PATS) 逐【字符】拆开，生成 `好|物|||神|器…` 这种带空交替的
# 正则——空交替让 search 恒匹配空串，于是每一条标题都被判「含夸大宣传」，命中词还
# 是单个汉字。现在直接给 pattern，不再经过 join。
_ZH_CLAIM_PAT = (
    r"好物|神器|必备|必买|最好|最佳|最优|第一|唯一|完美|极致|顶级|顶尖|顶配|首选|"
    r"王牌|王者|冠军|一流|领先|无敌|无与伦比|独一无二|独家|绝无|史上|全网|全球领先|"
    r"爆款|爆卖|热卖|畅销|销冠|网红|万人|超强|超能|震撼|逆天|奢华|高端大气|"
    r"天下第一|举世|空前|绝对|百分百|保证效果|万能"
)

_EN_CLAIM_RE = tuple(re.compile(p, re.I) for p in _EN_CLAIM_PATS)
_ZH_CLAIM_RE = re.compile(_ZH_CLAIM_PAT)


def has_marketing_claim(text: str) -> bool:
    """这段文字是否含夸大宣传/绝对化/销量排名宣称。

    先摘掉品类词搭配（Tank Top / Essential Oil 这类，理由见 _TOP_NOUN_RE），
    再查中英文两套词表，任一命中即为 True。
    """
    s = str(text or "")
    if not s:
        return False
    s = _TOP_NOUN_RE.sub(" ", s)
    s = _CATEGORY_MASK_RE.sub(" ", s)
    return (any(r.search(s) for r in _EN_CLAIM_RE)
            or bool(_ZH_CLAIM_RE.search(s)))


def hit_words(text: str) -> list:
    """命中的夸大宣传词（去重、保序），只用于报错文案与日志。

    【为什么要它】两次生成都不合格时日志只能报「含主观营销用语」，看不出是哪个词——
    而重试提示词要把具体的词喂回模型才有效（同 titles 的 last_reasons 取向）。
    """
    s = _CATEGORY_MASK_RE.sub(" ", _TOP_NOUN_RE.sub(" ", str(text or "")))
    out = []
    for r in _EN_CLAIM_RE:
        for m in r.finditer(s):
            w = m.group(0).strip()
            if w and w.lower() not in {x.lower() for x in out}:
                out.append(w)
    for m in _ZH_CLAIM_RE.finditer(s):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


# ---- 给模型看的提示词片段（三处图片链路共用，改口径只改这里）--------------------

# 视觉模型判「图上有没有夸大宣传」时的判据说明。刻意把实物排除在外：一张衣服胸前
# 印着装饰性英文的实拍图若被判成营销宣称，代价是好图被抹平文字甚至整单停摆
# （同 vision.check_cleaned 对实物绣标的取向）。
_IMAGE_STYLE_WORD = r"(?:super\s+)?cute|lovely|fun|cool|wonderful"
_IMAGE_STYLE_TEXT_RE = re.compile(rf"(?:{_IMAGE_STYLE_WORD})", re.I)
_IMAGE_STYLE_REASON_RE = re.compile(
    rf"(?:含有?|存在)?\s*[\"'“‘]?\s*(?:{_IMAGE_STYLE_WORD})\s*[!！]?[\"'”’]?\s*"
    r"(?:属(?:于)?|为|是)?\s*(?:情绪(?:化)?|主观)?(?:夸大|夸张|营销)"
    r"(?:宣传|类|用语|宣称|文案|词|标语)*[。.!！]?", re.I)


def is_style_only_image_claim(issues: str, claim_texts=None) -> bool:
    """只豁免普通风格词，兼容旧质检文案；混合或不完整证据不豁免。"""
    reason = str(issues or "").strip()
    if claim_texts is not None:
        if not isinstance(claim_texts, list):
            return False
        if not claim_texts:
            return bool(_IMAGE_STYLE_REASON_RE.fullmatch(reason))
        if not all(isinstance(text, str) and _IMAGE_STYLE_TEXT_RE.fullmatch(
                text.strip(" \t\r\n\"'“”‘’!！.")) for text in claim_texts):
            return False
        english_reason = re.sub(r"[^\x00-\x7f]", " ", reason)
        if (has_marketing_claim(reason) or has_marketing_claim(english_reason)
                or re.search(r"中文|乱码|水印|变形|缺块|网址", reason)):
            return False
        return True
    return bool(_IMAGE_STYLE_REASON_RE.fullmatch(reason))


_IMAGE_STYLE_BOUNDARY = (
    "普通外观、风格和趣味描述不属于夸大宣称：Cute、Lovely、Fun、Cool、Wonderful，"
    "以及仅表达可爱外观的 Super Cute，不应仅因主观、情绪化或使用感叹号就判违规。"
    "例如 Cute Glider Blaster 是可爱造型玩具的描述，应保留。"
    "须按完整短语与语境判断，不可从 Super Cute 等短语拆出 Cute 当禁词。"
    "这不是整张图的豁免：Cute 与 Best Seller、High-Quality、Guaranteed 等宣称同时出现时，"
    "仍须处理那些销量、品质或保证宣称；中文、乱码、水印另按各自规则检查。"
)


IMAGE_CLAIM_RULE = (
    "marketingClaim：叠加的文案层里有没有【夸大宣传或绝对化宣称】——"
    "销量与排名类（BEST-SELLER、Best Seller、热卖、爆款、销量第一、TOP1、#1）、"
    "最高级与品质夸大类（Best、Perfect、Top Quality、High-Quality、Premium、Luxury、最好、完美、顶级）、"
    "必备类（Must Have、Essential、必备、神器）、"
    "情绪夸大类（Amazing、Incredible、Unique、独一无二、震撼）、"
    "无从核实的保证（Guaranteed、100% Satisfaction、保证效果）都算。\n"
    "  客观描述不算：材质成分、尺寸数值、件数、功能说明、使用方法、注意事项、"
    "款式名与品类名（Tank Top、Essential Oil 这类词里的 Top/Essential 不算宣称）。\n"
    + "  " + _IMAGE_STYLE_BOUNDARY + "\n"
    "  判为 marketingClaim=true 时，issues 必须引用实际可见的完整宣称并说明属于哪类；"
    "不能只以普通形容词为拒绝理由。\n"
    "  商品实物本身的印花、刺绣、织标、吊牌上的字样一律不算，哪怕印的正是这些词"
    "——那是实物的一部分，选品时已人工确认过。"
)

# 英化/清理出图时的处置口径：营销标语不翻译，直接连背景一起抹掉。
# 【为什么必须显式说「不要翻译」】DEFAULT_TRANSLATE_PROMPT 的主轴是「商品介绍类
# 文字都翻译保留」，不单独点出来，模型就把 BEST-SELLER 规规矩矩译成英文留在图上，
# 质检那关又必然判不过，白烧几发生图最后退回原图。
CLAIM_REMOVE_RULE = (
    "图上若有夸大宣传或绝对化宣称的文案（BEST-SELLER、Best Seller、Top Quality、High-Quality、"
    "Premium、Must Have、Amazing、Guaranteed、热卖、爆款、销量第一、最好、完美、"
    "顶级、必备、神器这类），【不要翻译、不要保留】，连同背景一起抹除干净、"
    "按周围画面自然补全；商品实物上的印花、刺绣、织标要原样保留。"
    + _IMAGE_STYLE_BOUNDARY
)


# ---- 平台禁词：与夸大宣传无关的另一类硬红线 ------------------------------------
#
# 【为什么不并进 _EN_CLAIM_PATS / _ZH_CLAIM_PAT】那两套是「夸大宣传」判据，命中后的
# 处置口径是「抹掉标语、保留商品信息」；这一套是用户 2026-09-20 定案的禁词，性质完全
# 不同——睡眠类词说的是商品用途，不是宣称，却一样不许出现在买家可见的商品信息里。
# 混进同一个函数会让日志分不清「拦的是宣称还是禁词」，两类的重试话术也不一样。
#
# 【必须独立成函数、跑在原文上，不能复用 has_marketing_claim】那个函数先过
# _TOP_NOUN_RE / _CATEGORY_MASK_RE 两道掩码，掩码会把「修饰词 + Top」「essential oil」
# 这类搭配整段摘掉，落在里头的禁词就跟着逃了。故 has_banned_term 一律对【未经掩码的
# 原文】做匹配。
#
# 【拦什么：安抚 / PP棉，中英文两侧都拦】2026-09-20 用户定案。睡眠类词（sleep/nap/
# bedtime 这些）曾一度进过这份词表，后按用户口径撤掉——它们与品类高度重合
# （睡衣/睡袋/Sleepwear 是正当品类），拦了得不偿失。现在只拦这两类：
#   - 「安抚」类用途描述：安抚玩偶/安抚巾/Soothing/Comforter 这类说法；
#   - 「PP棉」填充物俗称：要写填充物就用规范材质名（聚酯纤维 / Polyester Fiber）。
# 两侧都拦是因为它们不像睡眠词那样有正当品类用法：中文侧「安抚」「PP棉」本身就是该换掉
# 的写法，英文侧 Soothing / PP Cotton 同理。
#
# 【为什么 Comforter 要拦而 Comfort 不拦】Comforter 在美式英语里是「被子/棉被」，也是
# 「安抚物」的常见译法，两义都指向要换掉的表达；而 Comfort（舒适）是正当的客观描述，
# 也是本项目给「安抚玩偶」的推荐替代译法（Comfort Plush Toy），拦了就没有出路了。
#
# 【PP棉的写法要覆盖变体】源标题里实测有「PP棉」「pp棉」「PP 棉」「聚丙烯棉」四种写法，
# 英文侧还有 PP Cotton / PPCotton（货号里连写）。故中文用 [Pp][Pp]\s*棉 容忍空格，
# 英文用 pp[\s-]?cotton 容忍空格与连字符。
# 【不拦「聚丙烯纤维/丙纶」「聚酯纤维」】那是平台 options 里的规范写法、也是 PP 棉的
# 化学名，属性行要靠它们顶替 pp棉（见 attributes.validation._dodge_banned_option），
# 一并拦掉会让「填充物成分」这类必填行无值可选、整个商品卡在保存。
_BANNED_EN_PAT = (
    r"\bsoothing\b|\bsoother\b|\bcomforters?\b"
    r"|\bpp[\s-]?cotton\b|\bpolypropylene\s+cotton\b"
)

# 中文侧：与英文同源的红线，用户明确要求「无论源头中文还是英文都不能过」。
_BANNED_ZH_PAT = r"安抚|[Pp][Pp]\s*棉|聚丙烯棉"

_BANNED_EN_RE = re.compile(_BANNED_EN_PAT, re.I)
_BANNED_ZH_RE = re.compile(_BANNED_ZH_PAT)


def has_banned_term(text: str) -> bool:
    """这段文字是否含平台禁词（安抚 / PP棉，中英文两侧同一套口径）。

    与 has_marketing_claim 刻意分开：判据不同、处置不同、日志要能分清（见上方说明）。
    【不过掩码】直接对原文匹配，理由见 _BANNED_EN_PAT 上方那段。
    """
    s = str(text or "")
    if not s:
        return False
    return bool(_BANNED_EN_RE.search(s) or _BANNED_ZH_RE.search(s))


def banned_hits(text: str) -> list:
    """命中的禁词（去重、保序），只用于报错文案与日志。

    【为什么要它】同 hit_words：拒因不带上具体词，两次生成都不合格时日志只能报
    「含禁词」，既看不出撞的是哪个，重试提示词也没法把它喂回模型。
    """
    s = str(text or "")
    out = []
    for r in (_BANNED_EN_RE, _BANNED_ZH_RE):
        for m in r.finditer(s):
            w = m.group(0).strip()
            if w and w.lower() not in {x.lower() for x in out}:
                out.append(w)
    return out


# 给模型看的提示词片段。与 CLAIM_REMOVE_RULE 并列而不是合并：那条讲的是「夸大宣传
# 抹掉」，这条讲的是「这些词连同它说明的信息一起不要出现」，两件事模型要分开听懂。
# 【PP棉这类填充物说明要「换写法」而不是整段抹掉】它与睡眠词的处置不同：填充物是买家
# 关心的材质信息（也是 Temu 属性里的必填项），整段抹掉就丢了有效信息。故这里要求改写成
# 规范材质名。「安抚」不同——它是用途宣称、没有必须保留的信息量，直接抹掉即可。
BANNED_REMOVE_RULE = (
    "图上若有「安抚」类用途描述（安抚、安抚玩偶、Soothing、Comforter 这类），"
    "【不要翻译、不要保留】，连同背景一起抹除干净、按周围画面自然补全。"
    "图上若有「PP棉」「pp棉」「聚丙烯棉」「PP Cotton」这类填充物俗称，"
    "不要照译，改写成规范材质名 Polyester Fiber（或 Polyester Fiber Filled）——"
    "填充物是买家关心的材质信息，不要整段删掉。"
    "商品实物上的印花、刺绣、织标要原样保留。"
)

BANNED_IMAGE_RULE = (
    "bannedTerm：叠加的文案层里有没有【平台禁词】——"
    "「安抚」类用途描述（安抚、安抚玩偶、Soothing、Soother、Comforter）、"
    "以及「PP棉」类填充物俗称（PP棉、pp棉、PP 棉、聚丙烯棉、PP Cotton、"
    "Polypropylene Cotton）都算。\n"
    "  规范材质名不算禁词：聚酯纤维、聚丙烯纤维/丙纶、Polyester Fiber、"
    "Polypropylene Fiber；Comfort（舒适）作为客观描述也不算，只有 Comforter 才算。\n"
    "  判为 bannedTerm=true 时，bannedTermTexts 必须列全实际可见的原文。\n"
    "  商品实物本身的印花、刺绣、织标、吊牌上的字样一律不算。"
)
