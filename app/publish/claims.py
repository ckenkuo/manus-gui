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
