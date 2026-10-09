"""发布管线共用的夸大宣传判据：词表、文本闸门、以及给模型看的提示词片段。

【为什么要单独一个模块】收窄夸大宣传这件事要在三类地方同时成立：
  1. 生成类文本（标题、货号、尺码译文）——生成后用正则硬闸拦；
  2. 图片英化（出图提示词）——要告诉生图模型「营销标语不翻译、直接抹掉」；
  3. 图片质检与复核（视觉模型）——要告诉它「什么算夸大宣称」并回一个布尔。
这三处的口径必须一致：判据在 A 处拦、提示词在 B 处没提，表现就是生图一遍遍把
营销词原位译成英文、质检一遍遍判不过，最后发数烧完退回原图（正是
stages.cleaning_rules._retry_hint 那段实测记录里的失败形态）。各写一份词表迟早漂移，
故词表与说法都只留这一处。

【判据的分界：叠加文案一律判，实物上只判两类】商品实物上的装饰性印花、刺绣、图案
即使印着 BEST 之类的字样，也是实物的一部分，选品阶段已人工确认过——图片侧的提示词
一律把它排除在外（同 vision.check_cleaned 对实物绣标的取向：误报的代价是好图被抹、
整单停摆；2026-08-26 那张棒球服绣标被当成品牌字退回原图就是这类误报）。

但 2026-09-25 用户收到平台的两类违规通知后定案，实物上有两类要【主动抹掉】，不再按
「实物的一部分」豁免：
  1. 品牌标识——商标、品牌名、品牌小标签（牛仔裤后腰红标、鞋侧标、口袋上的方标
     这类）。实测：一条裤子的链接被判侵权，侵权点其实是模特脚上那双鞋的标识；
     另一条童裤因口袋上一个小方标触发商标投诉。
  2. 材质成分说明——图上写「棉」「100% Cotton」这类材质文字。平台判据是「商品主图/
     详情图/SKU图/SKC图等含棉材质宣传，但属性中材质描述与该材质不匹配」，即图上
     材质与属性材质打架即违规；无论文字印在叠加文案层还是实物吊牌/织标上。
这两类的处置与叠加文案一致（抹掉、不保留），故并进四条 rule；出图侧写 MARK_REMOVE_RULE、
质检侧写 MARK_IMAGE_RULE，两边措辞同源、必须一起改。

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
    "  商品实物上的装饰性印花、刺绣、图案上的字母不算，哪怕印的正是这些词"
    "——那是实物的一部分，选品时已人工确认过。"
    "（实物上的品牌标识与材质成分说明不在这一问的范围内，它们另见标记那一项。）"
)

# 英化/清理出图时的处置口径：营销标语不翻译，直接连背景一起抹掉。
# 【为什么必须显式说「不要翻译」】DEFAULT_TRANSLATE_PROMPT 的主轴是「商品介绍类
# 文字都翻译保留」，不单独点出来，模型就把 BEST-SELLER 规规矩矩译成英文留在图上，
# 质检那关又必然判不过，白烧几发生图最后退回原图。
CLAIM_REMOVE_RULE = (
    "图上若有夸大宣传或绝对化宣称的文案（BEST-SELLER、Best Seller、Top Quality、High-Quality、"
    "Premium、Must Have、Amazing、Guaranteed、热卖、爆款、销量第一、最好、完美、"
    "顶级、必备、神器这类），【不要翻译、不要保留】，连同背景一起抹除干净、"
    "按周围画面自然补全；商品实物上的装饰性印花、刺绣、图案可以保留。"
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
# 【拦什么：安抚 / PP棉 / 环保声明，中英文两侧都拦】前两类 2026-09-20 用户定案、
# 第三类 2026-09-25 收到平台违规通知后补上。睡眠类词（sleep/nap/bedtime 这些）曾一度
# 进过这份词表，后按用户口径撤掉——它们与品类高度重合（睡衣/睡袋/Sleepwear 是正当
# 品类），拦了得不偿失。现在拦三类：
#   - 「安抚」类用途描述：安抚玩偶/安抚巾/Soothing/Comforter 这类说法；
#   - 「PP棉」填充物俗称：要写填充物就用规范材质名（聚酯纤维 / Polyester Fiber）；
#   - 不合规环保声明：eco-friendly / sustainable / 环保 / 可降解 这类（见下）。
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
_BANNED_EN_BASE = (
    r"\bsoothing\b|\bsoother\b|\bcomforters?\b"
    r"|\bpp[\s-]?cotton\b|\bpolypropylene\s+cotton\b"
)

# 中文侧：与英文同源的红线，用户明确要求「无论源头中文还是英文都不能过」。
_BANNED_ZH_BASE = r"安抚|[Pp][Pp]\s*棉|聚丙烯棉"

# 【2026-09-25 扩充第三类：不合规环保声明】用户当天收到平台违规通知「商品信息中
# 存在不合规环保声明描述，如 eco-friendly、environmental friendly…，请整改删除后
# 重新提交」。它与「安抚」同性质——平台一眼判定的硬红线、且没有必须保留的信息量
# （不像 PP棉 是买家关心的材质信息），故并入同一份词表、同一套处置：整句连同它说明
# 的信息一起删掉，**不许换成别的环保说法保留**（任何环保声明平台都不接受）。
#
# 【只认固定搭配，绝不拦裸 green / natural / organic】这三个词都有正当用法：
# green 是最常见的颜色词（绿裙子）、natural 是正当的客观描述（Natural Color）、
# organic 在服装里是「有机棉」的正当品类写法。拦掉它们会把正常商品拦死——同
# _TOP_NOUN_RE 那段「误伤品类词比漏拦一个生僻词更糟」的取向。故英文只认带环保前缀的
# 搭配：eco-/environment-/planet-/earth-/nature-friendly、eco-conscious/responsible、
# sustainable、biodegradable、recycled/recyclable、carbon neutral/footprint、
# zero waste、green packaging 这些；中文同理，拦「环保」「可持续」「可降解」这类
# 明确声明词，不拦单个「绿」字。
_ECO_EN_PAT = (
    r"\beco[\s-]?(?:friendly|conscious|responsible|sustainable|safe|packaging)\b"
    r"|\benvironment(?:al|ally)?[\s-]?friendly\b"
    r"|\b(?:planet|earth|nature)[\s-]?friendly\b"
    r"|\bsustainab(?:le|ly|ility)\b"
    r"|\bbio[\s-]?degradable\b|\bcompostable\b"
    # 【只认 recycled / recyclable，不认 recycle / recycling】后两个是回收行为与设施，
    # 「Recycling Bin」「Recycle Bin」是分类垃圾桶的正当品类名，拦掉会把整类商品拦死
    # （标题闸命中即拒、两次生成不过就硬失败）。而「Recycled / Recyclable」必然是产品
    # 属性声明（不存在「Recycled Bin」这种品类写法），保留。
    r"|\brecycl(?:ed|able)\b|\bupcycl(?:e|ed|ing)\b"
    r"|\bcarbon[\s-]?(?:neutral|footprint|offset|zero)\b|\blow[\s-]?carbon\b"
    r"|\bzero[\s-]?waste\b"
    r"|\bgreen[\s-]?(?:product|choice|living|energy|packaging|material|"
    r"manufactur\w*|initiative)\b"
)
_ECO_ZH_PAT = (
    r"环保|可降解|生物降解|可循环|再生材料|低碳|零浪费|零废弃|"
    r"生态友好|环境友好|对环境友好|地球友好|碳中和|"
    # 【「可持续」与「可回收」要排除非声明的搭配】它们在中文里另有正当用法：
    # 「续航可持续 8 小时」「可持续使用三年」说的是时长，「可回收垃圾桶」说的是品类
    # （同 Recycling Bin）。故加否定前瞻排掉这些形态，其余（可持续材料/发展、可回收材料）
    # 照旧拦——误伤的代价是标题两次生成都撞闸、整单发不出去，比漏一个词贵得多。
    r"可持续(?!\s*(?:\d|使用|运行|工作|续航|待机|播放|穿着))|"
    r"可回收(?!\s*(?:垃圾桶|回收桶|桶|箱|站|物))"
)

# 环保声明并入禁词词表：它与「安抚 / PP棉」走同一条通路（标题/货号/属性/描述文字的
# 文本闸门、阶段① 标注、图片质检、出图提示词、重试话术全都已接好），故只并进这两个
# pattern，不另起一套判据与新字段——另起一套要复制六处调用点，漏一处就是一个洞
# （同下面 has_banned_term「两类词都要能被 banned_hits 报出来」的取向）。
_BANNED_EN_PAT = _BANNED_EN_BASE + "|" + _ECO_EN_PAT
_BANNED_ZH_PAT = _BANNED_ZH_BASE + "|" + _ECO_ZH_PAT

_BANNED_EN_RE = re.compile(_BANNED_EN_PAT, re.I)
_BANNED_ZH_RE = re.compile(_BANNED_ZH_PAT)


def has_banned_term(text: str) -> bool:
    """这段文字是否含平台禁词（安抚 / PP棉 / 环保声明，中英文两侧同一套口径）。

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
    # 【PP棉的处置 2026-09-25 由「改名保留」改为「抹掉」】原先要求改写成规范材质名
    # Polyester Fiber、理由是「填充物是买家关心的材质信息」。但改名后它仍是一条材质
    # 声明：既照样可能与平台属性里填的成分对不上（平台判的就是这个），又会撞上下面
    # 材质成分说明那条（改名产物就是材质文字，质检必判 materialText=true），于是模型
    # 按 A 条改名、质检按 B 条判脏，重烧四发全栽在同一处、图被丢弃。两条要求打架时
    # 必须由源头消歧，故图片侧统一为「抹掉」——材质信息由平台属性承载（属性侧那条
    # 填充物成分仍照旧走 attributes.validation._dodge_banned_option 换规范名，不受影响）。
    "图上若有「PP棉」「pp棉」「聚丙烯棉」「PP Cotton」这类填充物俗称，"
    "【不要照译、也不要改写成 Polyester Fiber 这类规范材质名】，连同所在整句一起抹掉"
    "——图上任何材质成分说明都不再保留。"
    "图上若有环保声明（eco-friendly、environmental friendly、planet-friendly、"
    "sustainable、biodegradable、recycled、环保、可持续、可降解这类），"
    "【不要翻译、不要保留、也不要用别的环保说法替换】，连同所在整句文案一起抹除干净"
    "——任何环保声明平台都不接受，换一种说法同样违规。"
    "商品实物上的装饰性印花、刺绣、图案可以保留。"
)

BANNED_IMAGE_RULE = (
    "bannedTerm：图上（叠加的文案层或实物标签上）有没有【平台禁词】——"
    "「安抚」类用途描述（安抚、安抚玩偶、Soothing、Soother、Comforter）、"
    "「PP棉」类填充物俗称（PP棉、pp棉、PP 棉、聚丙烯棉、PP Cotton、"
    "Polypropylene Cotton）、以及【不合规环保声明】（eco-friendly、"
    "environmental friendly、planet-friendly、eco responsible、sustainable、"
    "biodegradable、recycled、carbon neutral、环保、可持续、可降解、可回收、"
    "生态友好这类）都算。\n"
    "  规范材质名不算禁词：聚酯纤维、聚丙烯纤维/丙纶、Polyester Fiber、"
    "Polypropylene Fiber；Comfort（舒适）作为客观描述也不算，只有 Comforter 才算。"
    "（规范材质名不算禁词，但仍属于【材质成分说明】，照样要按标记那一项判 true。）\n"
    "  不算禁词的还有普通的颜色词与客观描述：Green（绿色）、Natural（自然色）、"
    "Organic（有机的）单个出现时不算，只有「环保声明」那种成句的宣称才算"
    "（如 Eco-Friendly、Environmentally Friendly）。\n"
    "  判为 bannedTerm=true 时，bannedTermTexts 必须列全实际可见的原文。\n"
    "  商品实物上的装饰性印花、刺绣、图案上的字母不算"
    "（实物上的品牌标识与材质说明另见标记那一项）。"
)


# ---- 实物上的品牌标识与材质成分说明：2026-09-25 用户定案新增 ------------------
#
# 【为什么要单开一类，而不是并进上面的夸大宣传/禁词】它判的是【实物】——原先实物上的
# 一切都按「实物的一部分、选品已人工确认过」豁免（见本模块 docstring 的分界说明）。
# 2026-09-25 平台的两条违规通知说明这个豁免开得太宽：
#   - 品牌标识：一条裤子链接被判侵权，侵权点其实是模特脚上那双鞋的标识；另一条童裤
#     因口袋上的小方标触发商标投诉。用户定案「图片有这种小标签都要去掉」。
#   - 材质成分说明：图上写「棉」而平台材质属性填的是别的成分时，平台判「含棉材质宣传
#     但属性中材质描述不匹配」。用户定案「图片如果翻译后出现棉或者其他材质的意思，
#     可以去掉」——一律去掉，不做「与属性比对」那种更复杂的判据（属性里已有权威成分，
#     图上的材质文字净是风险，抹掉不丢有效信息）。
#
# 【出图侧与质检侧的措辞必须成对改】只有出图要求抹、质检不查，等于白抹；只有质检判、
# 出图没要求，模型不知道为什么被打回。故两条 rule 一起放这里。
#
# 【为什么不说「实物上的一切都要抹」】踩过坑：2026-08 那次把实物吊牌上的中文纳入判据，
# 21 张商品图 21 张被带成「有中文」（见 vision.plan_carousel 的 chinese 判据注释），
# 误报是压倒性的。故这里只点名【品牌标识】与【材质成分说明】两类，且明确写出哪些
# 照旧保留（装饰性印花、刺绣、图案），别让模型扩大化。
MARK_REMOVE_RULE = (
    "图上若出现下面两类内容，【不要翻译、不要保留】，连同它们的标签、底色块与边框"
    "一起抹除干净、按周围画面自然补全：\n"
    "  ① 品牌标识：商标、品牌名、品牌 logo，以及商品实物上的品牌小标签"
    "（牛仔裤后腰的红标、鞋侧标、口袋上的品牌方标这类）——它们会构成商标侵权；\n"
    "  ② 材质成分说明：「棉」「纯棉」「100% Cotton」「聚酯纤维」「Cotton 65% "
    "Polyester 35%」这类写在图上或实物标签、吊牌、织标上的材质文字——图上材质会与"
    "平台材质属性里填的成分对不上。\n"
    "  实物上的小布标、小旗标、彩色织标（缝在口袋、腰头、侧缝、袖口、鞋侧的那种，"
    "即使看不出是哪个品牌）按 ① 处理，一并抹掉。\n"
    "  商品实物上的装饰性印花、刺绣、图案（不含品牌标识的）照旧保留。"
)

MARK_IMAGE_RULE = (
    "实物标记：图上有没有【品牌标识】与【材质成分说明】这两类内容——"
    "品牌标识指商标、品牌名、品牌 logo，以及实物上的品牌小标签（牛仔裤后腰红标、"
    "鞋侧标、口袋上的品牌方标这类），判 brandMark=true 并把可见的品牌名或标识名"
    "列进 brandMarkTexts；"
    "材质成分说明指「棉」「纯棉」「100% Cotton」「聚酯纤维」这类材质文字"
    "（无论写在实物标签、吊牌、织标上，还是叠加的文案层上），"
    "判 materialText=true 并把原文列进 materialTexts。\n"
    "  不算标记的：装饰性印花、刺绣、图案上的字母（不含品牌标识的）；"
    "尺寸数值、件数、工艺、使用说明这类非材质文字；尺码标、洗水标、成分标。\n"
    # 【按形态判，不要求认出品牌名】2026-09-25 实测：用户那张童裤图（554×547 的转发
    # 截图）口袋侧缝上缝着一个红色小旗标，模型没报——不是判据没生效（同一张图放大后
    # 它报出了口袋上的品牌方标），而是那个标小到字完全不可辨、模型认不出是哪个品牌。
    # 但用户的要求是「图片有这种小标签都要去掉」，判据是【形态】不是【名字】：彩色小布标、
    # 印着字母或图案的方标本身就是商标风险，看得清名字只是加分项。故显式要求按形态判，
    # 并允许它照抄看不清这个事实——不然模型只会把「认得出的品牌」报出来、
    # 把认不出的那些（恰恰是最多的）放过。
    "  实物上的小标签要特别看：缝在口袋、腰头、侧缝、袖口、鞋侧这些位置的小方标、"
    "小旗标、彩色织标，【即使上面的字小到看不清、认不出是哪个品牌】，只要形态是品牌"
    "标识（彩色小布标、印着字母或图案的方标）就判 brandMark=true；"
    "brandMarkTexts 里写你看到的字，实在看不清就照实写「看不清的小方标」并注明位置。\n"
    "  判 true 时必须列全实际可见的原文；没有就返回空数组。"
)


# 材质文字词表：图片侧给质检做文本兜底，标题侧做硬闸（2026-10-09 起
# titles._material_hits 复用的就是这一份，见那里的说明）。**属性与描述侧不受限**：
# 平台属性里的材质字段填的就是「棉」「聚酯纤维」，在那两处拦材质等于把正常商品拦死
# ——这与禁词「中英文两侧都拦」的取向相反，别照搬到属性/描述上去。
#
# 【改这条口径要两侧一起改】标题侧加闸与图片侧抹除判的是同一件事（材质宣传与属性
# 材质对不上），词表共用一份正是为了不漂移：扩词表会同时影响出图/质检与标题闸。
#
# 【清单与 attributes.composition._FIBER_SYNONYMS 同源，但不复用那份实现】那份判的是
# 「两个写法是不是同一根纤维」，用包含匹配，并把 PET 列为聚酯纤维的别名；照搬过来会
# 出事：包含匹配会把「棉签」「毛绒玩具」判成材质文字，英文别名 PET 卡不住词边界、
# 会把宠物商品文本里的 pet 判成材质。故这里英文一律卡 \b 词边界、中文只认明确写法或
# 「数字% + 纤维字」的形态。宁可漏判一个生僻写法（看图的是模型，它自己认得出），
# 也不误伤正常文案——同 _TOP_NOUN_RE 那段「误伤比漏拦更糟」的取向。
_MATERIAL_EN_RE = re.compile(
    r"\b(?:cotton|polyester|nylon|spandex|elastane|lycra|wool|cashmere|silk|"
    r"linen|ramie|acrylic|rayon|viscose|modal|lyocell|tencel|acetate|"
    r"polyamide|polypropylene)\b", re.I)
_MATERIAL_ZH_RE = re.compile(
    r"纯棉|全棉|棉质|棉纤维|涤纶|聚酯纤维|锦纶|尼龙|氨纶|粘纤|粘胶|腈纶|羊毛|羊绒|"
    r"蚕丝|真丝|亚麻|苎麻|莫代尔|天丝|莱赛尔|醋酸纤维|珊瑚绒|摇粒绒|法兰绒|"
    # 单字纤维名（棉/涤/毛/麻/丝/绒）只在两种上下文里认：与百分比相邻，或跟在
    # 「面料/成分/材质」标签词后面。「棉100%」「成分：棉」「100% 涤」都是商品图上
    # 最常见的写法；反过来单独一个「棉」字不认——那会命中「棉签」「棉花糖色」这类
    # 与材质无关的文案（同 _MATERIAL_EN_RE 对 pet 的处理取向）。
    r"\d+\s*%\s*(?:棉|涤|毛|麻|丝|绒)|(?:棉|涤|毛|麻|丝|绒)\s*\d+\s*%|"
    r"(?:面料|成分|材质|材质成分)\s*[:：]?\s*(?:棉|涤|毛|麻|丝|绒)")


def material_hits(text: str) -> list:
    """这段文字里出现的材质词（去重、保序），供质检兜底复核与日志。

    【为什么要它】同 banned_hits：视觉模型对「这算不算材质说明」的判断会抖，而它一旦
    把原文列出来，这件事就退化成确定性匹配、不必再信它的布尔。方向是单向的（只把
    materialText=false 复核成 true，不反过来），漏判的代价是图上带材质文字发出去，
    误判的代价只是多烧一发。
    """
    s = str(text or "")
    if not s:
        return []
    out = []
    for r in (_MATERIAL_EN_RE, _MATERIAL_ZH_RE):
        for m in r.finditer(s):
            w = m.group(0).strip()
            if w and w.lower() not in {x.lower() for x in out}:
                out.append(w)
    return out
