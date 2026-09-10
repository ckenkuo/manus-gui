"""店小秘发布操作：size_rules。模块导航见 docs/publish-pipeline-refactor.md。"""

import re


# ---- 类目判断的辅助线索（年龄段 / 尺码档位）----------------------------------
# 【为什么标题不够】见 size_tier 上方的 2026-08-29 取证：1688 标题把「女」和「宝宝」
# 并列，模型在第 2 级把婴幼童读成女士。而同一份 product-info.json 里躺着三个比标题
# 措辞硬得多的信号，此前一个都没喂给类目判断：
#   1. attributes 的「适合年龄段」——1688 卖家自己填的枚举值
#   2. sizes 的写法档位——月龄码/岁码与成人码互斥（size_tier）
#   3. 「适合身高」里的厘米区间
# 故把它们拼成一段线索附在标题后面。刻意【只做提示不做否决】：线索为空时提示词退化
# 成原样，判断质量不受影响；线索存在时它是硬约束（提示词规则 2 里写明）。
_AGE_ATTR_KEYS = ("适合年龄段", "适用年龄", "年龄段", "适合身高", "适用身高")


# ---- 阶段⑧ 尺码勾选（fix_sizes）--------------------------------------------

# 【单一尺码的同义写法必须映射到同一个键】2026-08-24 真站取证（offer 846106032776，
# 成人女装针织开衫）：源 skus 键是中文「均码」，而成人女装类目的页面选项全是英文
# —— `one-size`（带连字符）、`Asian One-size`、`Petite One-size` 等 37 项。
# 「均」与 `one-size` 字面上永远对不上，于是 fix_sizes 把页面原本勾着的尺码取消完、
# 还返回 ok，阶段⑨ 才以「错误：请先选择尺码」暴露出来。
#
# 只收【确定同义】的写法：均码/单码/通用/F/free size/one size/onesize。
# 刻意不收 `Asian One-size` / `Petite One-size` / `Tall …` —— 那些是平台的版型变体
# （亚洲版/娇小版/高挑版），与通用均码不是一回事，混进来会让程序在多个选项间乱勾。
_SIZE_ALIASES = {
    # 「码」后缀会先被剥掉，故单字形态（均/单）也要各自登记
    "均": "onesize", "均码": "onesize", "单": "onesize", "单码": "onesize",
    "通用": "onesize",
    "通用码": "onesize", "f": "onesize", "free": "onesize", "freesize": "onesize",
    "onesize": "onesize", "one-size": "onesize", "one size": "onesize",
}


def norm_size(s: str, *, unwrap=True, diameter=True) -> str:
    """尺码名归一，供源 SKU 键与页面复选框文本【两侧共用】后再比较。

    只剥后缀不够。2026-08-21 实测：1688 的尺码键会把身高建议塞进同一个键
    （`110cm建议身高100-110cm`），页面复选框却只有 `110`——原先只 `re.sub` 尾部
    `cm码` 的做法归一出 `110cm建议身高100-110`，与页面永远匹配不上，
    于是 fix_sizes 把已勾选的尺码全部取消后才报错（先破坏再失败）。

    故改为：先砍掉「建议身高/参考身高/适合身高」等描述性尾巴，再取第一段
    数字（童装尺码主体就是数字），然后过一遍同义别名表（均码 ↔ one-size，
    见 _SIZE_ALIASES 上方的实测说明），最后才回落到剥 cm/码 后缀（保留 M/XL）。
    """
    t = str(s).strip()
    # 描述性尾巴：建议/参考/适合 + 身高/体重…，以及括号补充说明。
    # 【方括号与冒号也是引导符】2026-09-01 实测三单（1051830242405 源键 `L【`、
    # 668802700848 源键 `L:60克`、1065904018146 源键 `M 胸围33背长29Г`）：1688 商家
    # 把规格/克重/测量值直接拼在尺码键里，引导符不止圆括号。页面选项就是干净的
    # S/M/L/XL，三单的字母码全在页面上，却因归一没剥掉尾巴而报「源尺码在页面选项里
    # 全部不存在」——尺码一个都没勾、整单卡在阶段⑧。故把 `【[〔` 与 `:：` 一并纳入。
    parts = re.split(r"[（(【\[〔]|[:：]|建议|参考|适合|推荐", t)
    t = parts[0].strip()
    # 【开头就是引导符时，判别信息在引导符【后面】】2026-09-09 实测 pdd 928028669617
    # （拼多多玫瑰花毯，尺码键 `【直径 60cm+22朵玫瑰】`）：整名被【】包住，上面取 [0]
    # 得空串，三个尺码归一后全是 ''——阶段⑨ 尺码表两侧键匹配全灭（模型估算值无处
    # 安放、报「对齐后仍缺」）；若模型逐字回写键名，三行还会坍缩进同一个 '' 键
    # （静默把最大码的值填给所有行，更糟）。故头部为空时改取引导符后的一段，
    # 并剥掉残留的闭合括号。
    if unwrap and not t and len(parts) > 1:
        t = re.sub(r"[）)】\]〕]+$", "", parts[1].strip()).strip()
    # 月龄/岁码必须【连区间带单位】一起保留，不能只取第一段数字。
    # 2026-08-29 实测（草稿 173539495458370139，女童牛仔两件套）：该类目 54 个
    # 尺码选项里同时有 6M、6-9M、6-12M、6Y、6-7Y——只取第一段数字时这五个全部
    # 归一成 "6"，于是源尺码 6-9m 把它们【全都勾上】，13 个框对应 5 个源尺码，
    # 而 fix_sizes 的收敛判据（每个框的勾选态 == 归一值是否在 wanted 里）恰好
    # 全部成立，返回 status=ok，SKU 表凭空多出 8 行。
    # 故月龄（m/月）与岁（y/t/岁）分开、区间两端都留：6-9m / 6m / 6y / 2-3y。
    # T 码（美式学步童装 3T）按岁归一到 y，那本就是同一档（3T ≈ 3Y）。
    my = re.match(r"^(\d{1,2})\s*(?:-\s*(\d{1,2}))?\s*(m|月|个月|y|t|岁|yr|years?)$", t, re.I)
    if my:
        unit = "m" if my.group(3).lower() in ("m", "月", "个月") else "y"
        return (my.group(1) + ("-" + my.group(2) if my.group(2) else "")) + unit
    # 童装身高码主体：110cm → 110、110-120 → 110（2026-08-21 起的既有行为，
    # 身高码在页面上只有单值 90/100/110/120/130，没有上面那种区间碰撞）
    m = re.match(r"^(\d+)", t)
    if m:
        return m.group(1)
    # 【直径类尺码名取 cm 数作判别】（仍是上面花毯案例）：剥完外层括号剩
    # 「直径 60cm+22朵玫瑰」，文字开头走不到数字分支，整串唯一判别信息就是
    # cm 数（60/110/160，玫瑰朵数是营销文案）；模型在阶段⑨ 回写键名时又常
    # 改写成 `60cm`、`直径60cm` 这类自有形态。统一归到 cm 数后这些形态全部
    # 收敛。串里出现两个不同 cm 数时无法判别（如「直径60cm 边宽10cm」），不取。
    cms = {n for n in re.findall(r"(\d{2,3})\s*(?:cm|厘米)", t, re.I)}
    if diameter and len(cms) == 1:
        return cms.pop()
    # 字母码（M/XL/XXL）：只剥 cm 与「码」后缀
    t = re.sub(r"(cm)?码?$", "", t, flags=re.I).strip()
    # 【字母码后粘连测量值尾巴】2026-09-08 实测（offer 1063010884595 狗狗衣服，源键
    # `L背30`/`M背27`/`S背23`/`XL背35`/`XXL背38`；851024284153 源键 `M胸40背30约
    # 5-6斤左右`）：宠物服装按背长分码，商家把背长/胸围/体重直接拼在尺码字母后面、
    # 不带空格冒号括号，上面的引导符一个都命中不了。判据取「开头是尺码字母码且后面
    # 紧跟中文」——字母码后跟空格走下面那个分支、后跟中文才进这里整段剥尾巴。
    m = re.match(r"^([A-Za-z]+)(?=[一-鿿])", t)
    if m and m.group(1).lower() in _ADULT_SIZE_TOKENS:
        t = m.group(1)
    # 【空格后跟测量描述时才取首段】2026-09-01 实测（1065904018146 猫咪绝育服，
    # 源键 `M 胸围33背长29Г`——末尾那个 Г 是商家把「厘米」的 cm 打成了西里尔字母）：
    # 页面就是干净的 S/M/L，靠上面剥后缀剥不掉空格后的整段测量值。
    # 但【不能无条件按空格取第一段】：成人女装类目的页面选项有 `Asian One-size`、
    # `Petite One-size`、`Asian Tall XL` 这类版型变体（见 _SIZE_ALIASES 上方取证），
    # 无条件取首段会把它们全归一成 Asian/Petite，多个不同选项撞成同一个值，
    # 复现 6M/6-9M 那类「一个源尺码勾中多个框却仍判收敛」的假 ok。
    # 故只在【首段本身就是纯字母码】时才丢弃其余段：M/XL 是尺码、Asian 不是。
    if " " in t or "　" in t:
        head = re.split(r"[\s　]+", t)[0].strip()
        if head.lower() in _ADULT_SIZE_TOKENS:
            t = head
    # 同义别名（均码/one-size 等）：别名表命中就用统一键，否则保留原样。
    # 别名比对忽略大小写与内部空格/连字符（`One Size`、`one-size` 都要能命中），
    # 但返回值保留原大小写形态的字母码（M/XL 走不到这一步的替换）。
    key = re.sub(r"[\s_-]+", "", t).lower()
    if key in _SIZE_ALIASES:
        return _SIZE_ALIASES[key]
    # 「均」被上面的「码」后缀剥出来时字面就是「均」，也要能命中
    if t in _SIZE_ALIASES:
        return _SIZE_ALIASES[t]
    return t


# ---- 尺码档位（婴幼童 / 成人）判定 -------------------------------------------
# 【为什么要判档位】2026-08-29 真站取证（offer 1055568943470，1688 标题「棕色女亚马逊
# 牙雅跨境现货坑条短袖上衣夏季宝宝花朵印花牛仔裤棉」）：标题里「女」与「宝宝」并列，
# 类目遍历第 1 级已判出「女宝宝服装…符合婴儿服饰及鞋靴」，第 2 级却反转成「女士时尚」，
# 一路走到叶子「女士牛仔两件套」。错类目下阶段④ 属性照样填满 11/11、阶段⑦ SKC 也正常
# （成人女装与童装属性行大量重合），整条链上第一个对年龄段敏感的环节是阶段⑧ 的尺码
# 交叉比对——于是错误延后约 5 分钟才以「源尺码在页面选项里全部不存在」暴露，报错文案
# 还把人往「尺码归一没覆盖某种写法」的方向带。
#
# 月龄码/岁码（6-9m、2-3y、90cm）与成人码（S/M/L、Asian One-size）是【互斥】的两套
# 体系，它们本身就是最硬的年龄段信号，比标题措辞可靠得多。故把它抽成显式判定，
# 一处喂给类目提示词做事前约束，一处给阶段⑧ 的报错做事后指认。
_RE_MONTH_SIZE = re.compile(r"^\d{1,2}\s*-?\s*\d{0,2}\s*(m|月|个月)$", re.I)

_RE_YEAR_SIZE = re.compile(r"^\d{1,2}\s*-?\s*\d{0,2}\s*(y|t|岁|yr|year)s?$", re.I)

_ADULT_SIZE_TOKENS = {"xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl", "4xl",
                      "onesize", "one-size", "free"}


def size_tier(sizes) -> str:
    """按尺码写法判商品档位：`baby`（婴幼童）/ `adult`（成人）/ `""`（判不出）。

    判据取【只有一种档位会出现的写法】，不做模糊打分：
      - 月龄码 `6-9m` / 岁码 `2-3y` / 厘米体高码 `90`、`110cm` → baby
      - 纯字母码 S/M/L/XL、one-size（含 `Asian L`、`Petite One-size` 这类平台版型
        变体的尾段）→ adult
    数字码 80~150 是童装身高码（成人码不用三位数字），>150 不认（可能是腰围/鞋码），
    判不出就返回 ""——上层一律按「没有这个信号」处理，绝不据此否定 LLM 的判断。
    """
    baby = adult = 0
    for raw in (sizes or []):
        t = str(raw).strip()
        if not t:
            continue
        # 【借 norm_size 剥掉商家拼进来的规格尾巴】2026-09-01：源键形如 `L:60克`、
        # `M 胸围33背长29Г` 时，下面的字母码判据取整串比对，一个都命中不了 →
        # 返回 ""，于是阶段⑧ 报错时那句「真因是类目选错」的指认永远不触发。
        #
        # 【但归一命中别名表时必须弃用归一值】norm_size 会把中文「均码」折叠成
        # `onesize`，而 onesize 在 _ADULT_SIZE_TOKENS 里 —— 直接用归一值会把「均码」
        # 判成 adult。均码童装成人都在用，是刻意的中立值，本函数对它就该返回 ""
        # 而不是猜一个档位（否则会据此否定 LLM 的类目判断）。故只在归一【没走进
        # 别名表】时采纳它：剥尾缀是纯增益，同义折叠会丢掉档位中立这个信息。
        n = norm_size(t)
        if n and n not in _SIZE_ALIASES.values():
            t = n
        if _RE_MONTH_SIZE.match(t) or _RE_YEAR_SIZE.match(t):
            baby += 1
            continue
        m = re.match(r"^(\d{2,3})", t)
        if m:
            if 60 <= int(m.group(1)) <= 150:
                baby += 1
            continue
        # 字母码：取最后一段（`Asian Tall XL` → `XL`），命中成人码表才算
        tail = re.split(r"[\s_]+", t)[-1].strip().lower()
        if tail in _ADULT_SIZE_TOKENS or re.sub(r"[\s_-]+", "", t).lower() in _ADULT_SIZE_TOKENS:
            adult += 1
    if baby and not adult:
        return "baby"
    if adult and not baby:
        return "adult"
    return ""


def _has_older_than_one_year(sizes) -> bool:
    """尺码里是否出现「2 岁及以上」的岁码（2y / 3T / 5岁 / 7-8岁…）。

    【为什么单独判】size_tier 把岁码一律归进 baby（婴幼童），但类目树上「婴儿」与
    「女童/男童」是平级分支：岁码 ≥2 岁（2-3y、3T、5岁、6岁…）的商品是童装，绝不属于
    婴儿——婴儿类目只在 ≤1 岁（1y/1岁/12m 及以下）时成立。2026-09-04 实测（尺码 5岁/
    6岁/7岁/8岁 被分进婴儿类目）就是缺了这道「非婴儿」信号。
    只认岁码（y/t/岁/yr/year），月龄码（m/月）与身高码不在此判——月龄码 18-24m 与
    身高码 90cm 这类灰色地带留给 LLM，这里只掐最硬、最无争议的岁码。
    """
    for raw in (sizes or []):
        t = str(raw).strip()
        if not _RE_YEAR_SIZE.match(t):
            continue
        # _RE_YEAR_SIZE 的数字部分不是捕获组（它的 group(1) 是单位），这里单独抠
        # 首段数字判岁数：5岁→5、2-3y→2、7-8岁→7，>1 即为童装而非婴儿。
        num = re.match(r"\d{1,2}", t)
        if num and int(num.group(0)) > 1:
            return True
    return False


def _age_is_child(attrs) -> bool:
    """源属性里的年龄段是否明确是童装，用于纠正 size_tier 把童装字母码误判成成人码。

    【为什么要这一层】size_tier 只按尺码写法判档：S/M/L/XL 判成人、90cm/2-3y 判婴幼童。
    但 1688 童装商家常用字母码表示童装码（2026-09-08 offer 1022239913110 女童仿兔毛
    外套：源尺码 S/M/L/XL、衣长仅 27~43cm，却标「适合年龄段: 中小童 3~8岁，100~140cm」）。
    此时 size_tier 判出「成人码」，fix_sizes 就误报「类目选错」，实际类目是对的、只是
    尺码键用了字母码。「适合年龄段」属性是比尺码写法更硬的信号——它直接说「给几岁穿」。

    只认 _AGE_ATTR_KEYS 里的年龄段键，且成人信号优先：命中「成人/女士/男士」就不是童装
    （这些词极少出现在「适合年龄段」值里，出现了就是明确的成人语义）；命中「童/儿/婴/
    幼/岁/月龄」才判童装。判不出返回 False——上层一律按「没有这个信号」处理，绝不据此
    否定 size_tier 的结论。
    """
    for k in _AGE_ATTR_KEYS:
        v = str((attrs or {}).get(k) or "").strip()
        if not v:
            continue
        if any(m in v for m in ("成人", "女士", "男士", "中老年")):
            return False
        if any(m in v for m in ("童", "儿", "婴", "幼", "岁", "月龄", "个月")):
            return True
    return False


# ---- 字母码童装 → 页面身高码 的确定性映射 -----------------------------------
# 【为什么确定性而不是 LLM】2026-09-09 实测 LLM 映射泛化性差：档数不匹配时跳档、
# 大童场景忽略年龄段机械铺满（「8~14岁 130~160cm」被映射到 90~130），比不映射更
# 危险。而「档数恰好匹配」这个唯一可靠的场景，用确定性算法就能 100% 做对，根本
# 不需要 LLM。故只做确定性映射：源字母码档数 == 身高范围内的页面身高码档数 时才
# 映射，否则交人工。有歧义时绝不猜测——猜错把大童错配成小童，代价远大于交人工。

# 字母码大小顺序（S < M < L < XL < XXL ...）。norm_size 归一再小写后查这张表。
_LETTER_ORDER = {"xs": 0, "s": 1, "m": 2, "l": 3, "xl": 4, "xxl": 5, "2xl": 5,
                 "xxxl": 6, "3xl": 6, "xxxxl": 7, "4xl": 7}


# 「适合年龄段」里的身高范围（100~140cm / 100-140厘米）。只认带 cm/厘米 单位的范围，
# 不带单位的「3~8岁」是岁数不是身高，不能拿去当身高码。
_RE_HEIGHT_RANGE = re.compile(r"(\d{2,3})\s*[~\-—–到至]\s*(\d{2,3})\s*(?:cm|厘米|CM)")


def _parse_height_range(attrs) -> tuple:
    """从年龄段属性里解析身高范围 (lo, hi)，解析不出返回 (None, None)。"""
    for k in _AGE_ATTR_KEYS:
        v = str((attrs or {}).get(k) or "")
        m = _RE_HEIGHT_RANGE.search(v)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi and 60 <= lo <= 180 and 60 <= hi <= 180:
                return lo, hi
    return None, None


def _parse_discrete_heights(attrs) -> list:
    """从年龄段属性里解析「离散身高码列表」（100cm,110cm,120cm,130cm）。

    与 _parse_height_range 的分工：范围「100~140cm」走那边，离散「100,110,120,130」
    走这里。判据是「按逗号/顿号/空格等切出的多个 token 全都是身高码」——范围只有
    一个 token（含 ~ 连字符），不会被误判成离散列表；含岁数/文字 token 的整段也
    直接放弃（避免把「中小童(3~8岁，100~140cm)」里的 100/140 当离散码误映射）。
    """
    for k in _AGE_ATTR_KEYS:
        v = str((attrs or {}).get(k) or "")
        tokens = [t for t in re.split(r"[,，、/;；\s]+", v) if t]
        if len(tokens) < 2:
            continue
        heights = []
        ok = True
        for t in tokens:
            m = re.fullmatch(r"(\d{2,3})\s*(?:cm|厘米|CM)?", t.strip())
            if not m:
                ok = False
                break
            n = int(m.group(1))
            if not (80 <= n <= 170):
                ok = False
                break
            heights.append(n)
        if ok:
            return sorted(set(heights))
    return []


def _page_height_codes(page_sizes) -> list:
    """页面尺码选项里的纯身高码（整数 80~170），升序去重；岁码/月龄码被排除。"""
    out = set()
    for s in (page_sizes or []):
        n = norm_size(s)
        m = re.match(r"^(\d{2,3})$", n)
        if m and 80 <= int(m.group(1)) <= 170:
            out.add(int(m.group(1)))
    return sorted(out)


def _sort_letter_sizes(src_sizes) -> list:
    """字母码按 S<M<L<XL 顺序排序，返回排序后的原始尺码列表；含非字母码返回 []。"""
    pairs = []
    for s in (src_sizes or []):
        n = norm_size(s).lower()
        if n not in _LETTER_ORDER:
            return []
        pairs.append((_LETTER_ORDER[n], str(s)))
    pairs.sort(key=lambda p: p[0])
    return [s for _, s in pairs]


# 岁数范围（3~8岁 / 8-14岁），只认「岁/周岁/y/yr/year」单位——不带岁单位的
# 「100~140」是身高不是岁数，绝不能拿来当岁数（会被下面 _parse_height_range 收走）。
_RE_AGE_RANGE = re.compile(r"(\d{1,2})\s*[~\-—–到至]\s*(\d{1,2})\s*(?:岁|周岁|y|yr|year)", re.I)


def _age_to_height(age) -> int:
    """岁数 → 标准身高 cm（近似：2~14 岁 height ≈ age*7 + 75）。

    儿童身高是统计值、各标准源差 ±3cm，这里只需近似值——下游用它框出整十档
    （96~131 框住 100/110/120/130），±3cm 的误差不改变取整十结果，除非刚好压边界。
    """
    return round(age * 7 + 75)


def _parse_age_range(attrs) -> tuple:
    """从年龄段属性里解析岁数范围 (age_lo, age_hi)，解析不出返回 (None, None)。"""
    for k in _AGE_ATTR_KEYS:
        v = str((attrs or {}).get(k) or "")
        m = _RE_AGE_RANGE.search(v)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 2 <= lo <= hi <= 16:
                return lo, hi
    return None, None


def _slope_ok(info, letters, heights) -> bool:
    """衣长斜率校验：源衣长随对应身高增长的斜率是否落在童装上衣合理范围。

    只做【粗校验】拦住离谱错误（衣长数据与身高码完全不匹配、或拿错字段），拦不住
    「偏移一档」的细微偏差——但偏移一档时 SKU 表尺码整体偏移、后续阶段与人工复核仍
    有机会发现。没有衣长数据时放行（best-effort：衣长是辅助信号，不是映射正确性前提）。
    """
    meas = (info or {}).get("sizeMeasurements") or {}
    lens = []
    for s in letters:
        m = meas.get(s) or meas.get(norm_size(s)) or {}
        v = None
        for k in ("衣长", "裙长", "总长"):
            if isinstance(m.get(k), (int, float)):
                v = float(m.get(k))
                break
        if v is None:
            for val in m.values():
                if isinstance(val, (int, float)):
                    v = float(val)
                    break
        if v is None:
            return True  # 该尺码没衣长数据，跳过校验
        lens.append(v)
    if len(lens) != len(letters) or len(heights) != len(letters):
        return True
    dh = heights[-1] - heights[0]
    dl = lens[-1] - lens[0]
    if dh <= 0 or dl <= 0:
        return False
    slope = dl / dh
    # 童装上衣衣长斜率：身高每增 1cm，衣长增 0.25~0.8cm（短款上衣/披风到连衣裙）
    return 0.25 <= slope <= 0.8


def _map_letter_sizes(info: dict, page_sizes: list) -> dict:
    """字母码童装 → 页面身高码的映射，分三级：离散码、身高范围、岁数对照消歧。

    第 0 级（离散身高码，置信度高）：商家在「适合身高」等字段逐码列出建议身高
    （100cm,110cm,120cm,130cm），档数匹配且都在页面选项里时，按 S<M<L<XL 与身高码
    从小到大一一对应。

    第一级（身高范围，置信度高）：商家写的「适合年龄段」身高范围（100~130cm）里的
    页面身高码档数恰好等于源字母码档数时，一一对应。

    第二级（岁数对照消歧，置信度中）：前两级档数都不匹配（如 100~140 是 5 档、源
    只有 4 档）时，改用「岁数→标准身高」换算重算身高子区间，再要求档数匹配 + 源
    衣长斜率合理（见 _slope_ok）。这一级是概率正确而非 100% 确定——商家写的岁数
    可能有 ±1 岁误差、对照公式有 ±3cm 误差，故只作兜底，斜率不合理的仍交人工。

    三级都失败、解析不出身高/岁数、含非字母码，一律返回 {}——调用方交人工。绝不
    在有歧义时硬猜，避免把大童错配成小童。
    """
    attrs = (info or {}).get("attributes") or {}
    src_sizes = list((info or {}).get("sizes")
                     or list(((info or {}).get("skus") or {}).keys()))
    heights = _page_height_codes(page_sizes)
    letters = _sort_letter_sizes(src_sizes)
    if not letters or not heights:
        return {}
    # 第 0 级：离散身高码列表（商家逐码列出建议身高，信息最硬），档数匹配且都在页面里
    disc = _parse_discrete_heights(attrs)
    if disc and len(disc) == len(letters) and all(h in heights for h in disc):
        return {letter: str(h) for letter, h in zip(letters, disc)}
    # 第一级：商家写的身高范围，档数恰好匹配 → 确定性映射
    lo, hi = _parse_height_range(attrs)
    if lo is not None:
        in_range = [h for h in heights if lo <= h <= hi]
        if len(in_range) == len(letters):
            return {letter: str(h) for letter, h in zip(letters, in_range)}
    # 第二级：岁数对照消歧，档数匹配 + 衣长斜率合理才映射
    age_lo, age_hi = _parse_age_range(attrs)
    if age_lo is not None:
        h_lo, h_hi = _age_to_height(age_lo), _age_to_height(age_hi)
        in_range2 = [h for h in heights if h_lo <= h <= h_hi]
        if len(in_range2) == len(letters) and _slope_ok(info, letters, in_range2):
            return {letter: str(h) for letter, h in zip(letters, in_range2)}
    return {}
