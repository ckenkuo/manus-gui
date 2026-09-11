"""店小秘发布操作：sizechart.parameters。模块导航见 docs/publish-pipeline-refactor.md。"""

import re
from app.logger import logger
from typing import Optional


# ---- 测量参数名对齐（平台强制参数名 ← 源实测参数名）--------------------------
#
# 【为什么要这张表，纯子串包含为什么不够】2026-09-01 真站取证（offer 999389808041，
# 女童牛仔外套 + 连体裤两件套）：源详情图分件给了「部件：连体裤」的实测表，视觉阶段
# 把它读得完全正确（总衣长 59/64/69/74/79、腰围 48~56，跳码规则与供应商尺寸列都没混
# 进来）。但平台「女童装-下装」这一档强制的参数是【测量全围】与【摆长】，而原来的对齐
# 只做子串包含（`k in p or p in k`）：
#     测量全围 ←→ 腰围 / 胸围     两向都不含，不成立
#     摆长     ←→ 总衣长           两向都不含，不成立
# 于是对齐结果是空字典，整份识准的源数据被判为「一列都没有」全部丢弃，两列改交模型
# 凭空估算，填出摆长 32~44（源总衣长实际是 59~79，差了近一倍）。
#
# 同商品的第一张表（外套/上装）反而正常，纯属运气：「前衣长」含「衣长」、「胸围」被
# 「胸围全围」包含，子串刚好命中。也就是说这条路一直靠巧合工作，只是此前没撞上
# 一组两边字面完全不搭的参数名。
#
# 表的方向是【平台参数名 → 源参数名候选】，候选按优先级从高到低排：
# 「测量全围」在下装档量的是腰一圈，故腰围优先于下摆围、胸围只作最后兜底。
# 键用平台参数名的【裸词】（不含全围/半围后缀，见 _match_param 的归一），一个键覆盖
# 「腰围」「腰围全围」两种写法，免得同一档参数在表里写两遍。
_PARAM_SYNONYMS = {
    # 全围类：平台在下装档统一叫「测量全围」，具体量哪一圈由分类决定（下装量腰、
    # 上装量胸），故按分类语义排优先级
    "测量全围": ("腰围", "腰围(橡筋)", "下摆围", "坐围", "臀围", "胸围"),
    "胸围": ("胸围", "上胸围", "夹圈", "腰围"),
    "腰围": ("腰围", "腰围(橡筋)", "裤腰围", "坐围"),
    "臀围": ("臀围", "坐围", "腰围"),
    "领围": ("领围", "颈围", "领圈"),
    "大腿围": ("大腿围", "腿围", "髀围"),
    "脚口": ("脚口", "裤脚口", "脚围", "下摆围"),
    # 长度类：源侧的「总衣长/前衣长/后衣长」都是同一件的衣长量法，能互相顶
    "摆长": ("总衣长", "衣长", "前衣长", "上身长", "后衣长"),
    "衣长": ("衣长", "前衣长", "总衣长", "上身长", "后衣长"),
    "上衣长": ("上身长", "衣长", "前衣长", "总衣长"),
    "裙长": ("裙长", "衣长", "前衣长", "总衣长"),
    "连衣裙长": ("总衣长", "裙长", "衣长", "前衣长"),
    "裤长": ("裤长", "侧裤长", "长裤裤长", "裤长(不含吊带)", "九分裤长", "外侧长"),
    # 裤内长是【裆到脚口】，与外侧裤长不是同一段，故只认真正的内长量法，
    # 不拿侧裤长顶——顶了会让内长比实际长出一个裆深（约 20cm）
    "裤内长": ("裤内长", "内长", "内缝长", "下裆长"),
    "裆深": ("裆深", "前裆", "立裆"),
    "袖长": ("袖长", "袖长(含肩)", "长袖长"),
    "肩宽": ("肩宽", "总肩宽", "肩点至肩点"),
}


# 平台参数名里的量法后缀：比对前剥掉，让「腰围全围」与「腰围」归到同一个键。
# 只剥后缀不动词干（「测量全围」整体就是一个参数名，剥完剩「测量」反而没意义，
# 故它在 _PARAM_SYNONYMS 里按原样登记，由精确命中先行拦下）。
_RE_PARAM_SUFFIX = re.compile(r"(全围|半围|周长|围度)$")


# 剥后缀会剩下【非部位词】的整词：整体就是一个参数名，不能拆。
# 「测量全围」剥完剩「测量」——那不是身体部位，拿它去查同义词表必然落空
# （虽有 _match_param 里的原词兜底，但归一结果本身失真，跨函数复用时会踩坑）。
_PARAM_STEM_KEEP = ("测量全围", "测量半围")


def _param_stem(name: str) -> str:
    """平台/源参数名归一：去空格与量法后缀，供同义词表查键。"""
    t = re.sub(r"\s+", "", str(name or "")).strip()
    if t in _PARAM_STEM_KEEP:
        return t
    return _RE_PARAM_SUFFIX.sub("", t) or t


def _match_param(target: str, vals: dict) -> Optional[str]:
    """在源实测行 vals 里找出平台参数 target 该取哪个键；找不到返回 None。

    三级判据，越靠前越可信：
      1. 字面相同（含剥掉全围/半围后缀后相同）——最稳，直接用
      2. 同义词表按登记顺序取第一个命中的源键——顺序即优先级，见 _PARAM_SYNONYMS
      3. 子串包含——原有行为，留着兜住表里没登记的写法（「胸围」↔「胸围全围」这类
         本就靠它，撤掉等于回退）

    子串这级刻意放最后：它是最容易误配的一级（「裤长」会命中「裤内长」，而两者
    差一个裆深），前两级都没结论时才轮到它。
    """
    if target in vals:
        return target
    stem = _param_stem(target)
    for k in vals:
        if _param_stem(k) == stem:
            return k
    for cand in _PARAM_SYNONYMS.get(stem, ()) or _PARAM_SYNONYMS.get(target, ()):
        if cand in vals:
            return cand
        for k in vals:
            if _param_stem(k) == _param_stem(cand):
                return k
    cand = [k for k in vals if k and (k in target or target in k)]
    return cand[0] if cand else None


# ---- 源表「半围 / 全围」的识别与换算（腰围x2 / 20x2 / 平铺腰围）---------------
#
# 【为什么要认这些写法】2026-09-11 男童长裤取证：源详情图的尺码表表头写「腰围x2」
# 「臀围x2」、格子里写「20x2」「37x2」——商家说的是「这一列是平铺单面的半围，乘 2
# 才是绕一圈的全围」。平台参数（腰围全围/臀围全围/测量全围）要的正是全围，而管线
# 此前既不认标记、也不做换算，于是：
#   - 参数名「腰围x2」连 _match_param 都对不上（词表登记的是「腰围」，子串两向都不含），
#     白扔进一次模型名称映射；映射上之后又把 20 原样填进「腰围全围」——买家拿到的
#     腰围少一半，比缺值更糟（缺值至少会被 lacking 拦下报错）。
#   - 同一列前后不一：源表没覆盖的 80/90 码走估算、估的是全围（36/38），源覆盖的
#     100 码填的是半围（20），一张表里腰围从 38 掉到 20。故换算必须在【进对齐之前】
#     做，让勾选判断、名称映射、估算锚点看到的都是同一份全围数（见 editor 的调用点）。
#
# 【半围的写法】命中任一即认定该列是平铺半围（值乘 2 才是平台要的全围）：
#   乘 2 标记  腰围x2 / 臀围×2 / 腰围X2 / 腰围(x2) / 20x2（写在值里）
#   乘 2 字样  腰围乘2 / 腰围乘以2 / 腰围2倍
#   半围       腰围半围 / 半围腰围 / 腰围(半围)
#   二分之一   腰围1/2 / 腰围½ / 腰围二分之一
#   平铺/单面  平铺腰围 / 腰围(平铺) / 腰围单面 / 腰围单侧
# 「平铺」也当半围：平铺量的是对折后的宽度，本来就是绕一圈的一半（只对围度列成立——
# 长度列的「平铺裤长」就是裤长本身，故换算前先过 _RE_GIRTH 闸门）。
#
# 【全围的写法】全围 / 一圈 / 绕圈 / 周长 / 围度 / 圆周。作用有二：
#   ① 名字里两种信号打架时（「腰围(平铺后全围)」「腰围全围x2」这种），以全围为准、
#      绝不乘 2——「把全围当半围」会把买家尺码表放大一倍，比少一半更难被发现，
#      故宁可漏换也不错换；
#   ② _param_stem 的量法后缀表（全围|半围|周长|围度）剥的就是这些字样，故这里是
#      「什么算全围写法」的出处。（measurements 的全围量级校验另按平台参数名里有没有
#      「全围」两字判，比这里窄：平台参数实际都带全围后缀，暂没并过来。）
#
# 【只换围度类】名里带「围」或「口」的才换（腰围/臀围/胸围/领围/脚口/裤脚口/下摆围…）。
# 「衣长x2」「肩宽x2」这类不换——那不是半围的意思（多半是两层料/两件），猜着乘 2
# 会造出一个更离谱的错值；留着不动则最多是这一列走既有判据（值不可用就交估算）。
_HALF_NAME_MARKS = (
    ("乘2标记", re.compile(r"[xX×*]\s*2\s*倍?")),
    ("乘2字样", re.compile(r"乘\s*(?:以)?\s*2|2\s*倍")),
    ("半围", re.compile(r"半\s*围")),
    ("二分之一", re.compile(r"1\s*[/／]\s*2|½|二分之一")),
    ("平铺/单面", re.compile(r"平\s*铺|单\s*面|单\s*侧")),
)
_FULL_NAME_MARKS = re.compile(r"全\s*围|一\s*圈|绕\s*圈|周\s*长|围\s*度|圆\s*周")
_RE_GIRTH = re.compile(r"[围口]")
# 值里的写法：「20x2」是半围（乘 2 才是全围）；「20x2=40」商家自己算过了，40 就是全围
_RE_HALF_VALUE = re.compile(r"^(\d+(?:\.\d+)?)\s*[xX×*]\s*2\s*倍?$")
_RE_HALF_VALUE_DONE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*[xX×*]\s*2\s*[=＝]\s*(\d+(?:\.\d+)?)$")


def _half_marks(name: str) -> list:
    """参数名里命中的半围标记（用于日志留痕）；空列表 = 这列没写半围。"""
    t = str(name or "")
    return [label for label, rx in _HALF_NAME_MARKS if rx.search(t)]


def _is_full_marked(name: str) -> bool:
    """参数名里明确写了「全围/一圈/周长…」——那就不再当半围。"""
    return bool(_FULL_NAME_MARKS.search(str(name or "")))


def _strip_marks(name: str) -> str:
    """去掉参数名里的量法标记（x2/半围/平铺/全围/一圈…），剩下干净的部位名。"""
    t = str(name or "")
    for rx in (*[rx for _, rx in _HALF_NAME_MARKS], _FULL_NAME_MARKS):
        t = rx.sub("", t)
    # 「腰围(平铺)」「腰围(全围)」去掉词后剩下空括号，一并清掉
    t = re.sub(r"[（(\[]\s*[)）\]]", "", t)
    return re.sub(r"\s+", "", t).strip("-_·、,，")


def _clean_girth_name(name: str) -> str:
    """围度列名去净标记，得到能直接对齐的部位名：腰围x2/平铺腰围/腰围(全围) → 腰围。

    必须去干净：留着标记的名字连 _match_param 的字面与词表判据都过不了（词表登记的是
    「腰围」），只能掉到模型名称映射——那正是这单腰围填错一半的入口。
    但清完必须【还是个干净的围度词】才认，否则退回原名：
      - 「测量全围」清完剩「测量」——不是身体部位，清成它反而对不上平台参数名
        （platform 侧 _PARAM_STEM_KEEP 也是为它留的）；
      - 「腰围(平铺后全围)」这类不是「部位+标记」的写法，清完会剩「腰围(后)」这种残名，
        退回原名至少还是个能看的名字（对齐掉到模型映射那条路，值仍按全围照用）。
    """
    t = _strip_marks(name)
    if t and _RE_GIRTH.search(t) and not re.search(r"[（(\[][^)）\]]+[)）\]]", t):
        return t
    return str(name or "").strip()


def _split_half_value(v):
    """源数值 → (数值, 该值自己声明的量法)。解不出数时返回 (None, None)。

    量法三态：
      None   纯数字等，值本身没表态（是半围还是全围看参数名）
      "half" 「20x2」：20 是平铺半围，乘 2 才是全围
      "full" 「20x2=40」：商家已经算过了，40 就是全围（此时名字里带 x2 也不再乘）
    区间文本、带单位、空值等一律 (None, None) 交调用方原样带走——清洗不是本函数的事。
    """
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v, None
    t = str(v if v is not None else "").strip()
    m = _RE_HALF_VALUE_DONE.match(t)
    if m:
        return float(m.group(2)), "full"
    m = _RE_HALF_VALUE.match(t)
    if m:
        return float(m.group(1)), "half"
    return None, None


def normalize_half_marks(rows: dict) -> dict:
    """把源实测表里的平铺半围列换算成全围，返回新表（不改调用方那份）。

    换算规则（逐列判，判据顺序即优先级）：
      1. 非围度列（衣长/袖长/肩宽/重量…）一律原样带过，名字与值都不动；
      2. 值是「20x2=40」这种商家自己算过的写法：40 就是全围，照用、不再乘；
      3. 值是「20x2」：乘 2；
      4. 名字带半围标记（x2/半围/1/2/平铺/单面）且没写「全围/一圈/周长」：乘 2；
      5. 其余（裸「腰围」、明确写「腰围全围」）原值照用——源表写全围就是全围。
    【名字一律去净标记】不管换不换算，围度列名都清成部位名（腰围x2 / 腰围(全围) → 腰围）——
    标记留着只会让对齐掉到模型映射那条慢路上；清完不再是围度词的（「测量全围」）退回原名。

    只做换算、不做校验：换算后的值离不离谱由 measurements._check_measurements 管
    （它有全围量级那条），这里越权判会与那条判据分家。
    """
    out, converted = {}, {}
    for sz, row in (rows or {}).items():
        if not isinstance(row, dict):
            out[sz] = row
            continue
        vals = {}
        for p, v in row.items():
            if not _RE_GIRTH.search(str(p or "")):
                vals[p] = v
                continue
            num, unit = _split_half_value(v)
            if num is None:
                vals[p] = v
                continue
            name = _clean_girth_name(p)
            # 值先说了算（20x2=40 是商家自己乘好的，20x2 是半围）；值没表态才看名字的标记，
            # 且名字写了「全围/一圈」就当全围——把全围当半围会把尺码表放大一倍，宁可漏换
            marks = _half_marks(p)
            if unit == "half" or (unit is None and marks and not _is_full_marked(p)):
                num = float(num) * 2
                converted[p] = (name, "值里写了 x2" if unit == "half"
                                else "、".join(marks) + "标记")
            # 整数就写整数：与源表其它数值形态一致（20x2 → 40 而不是 40.0）
            vals[name] = int(num) if float(num).is_integer() else float(num)
        out[sz] = vals
    if converted:
        logger.info("源尺码表平铺半围已换算成全围（×2）：" + "、".join(
            f"{p}→{n}（{why}）" for p, (n, why) in sorted(converted.items())))
    return out


# 交模型做参数名映射时的提示词。词表兜不住的写法才走这里（见 _map_params_by_llm）。
_PARAM_MAP_PROMPT = """你是服装尺码表数据对齐助手。

平台尺码表要求填这几个测量参数：{targets}
源商品实测尺寸表里有这些参数：{sources}

请判断平台的每个参数应该取源表里的哪一个参数值（同一个量法的不同叫法，如
「摆长」与「总衣长」、「测量全围」与「腰围」）。规则：
1. 只在上面列出的源参数里选，不要编造名字；
2. 【量的不是同一段就不要映射】宁可留空交后续估算，也不要拿一个量法不同的值顶替——
   例如「裤内长」是裆到脚口，与「侧裤长」（腰到脚口）差一个裆深，不能互相顶；
   「胸围」与「腰围」是不同部位，只有源表里确实没有对应部位时才考虑；
3. 判不准的平台参数直接不要出现在结果里（留空是安全的，错配会给买家一份错尺码表）；
4. 【圆形商品例外】毯子/宠物窝等圆形商品的「长」与「宽」就是同一个量（都等于直径），
   源表有「直径」时两者都映射到它——这不是错配，是同一个值填两列。

只输出严格JSON，键是平台参数名、值是源参数名，不要其他文字：
{{"<平台参数>": "<源参数>"}}"""


async def _map_params_by_llm(targets: list, sources: list) -> dict:
    """交模型把词表兜不住的平台参数名映射到源参数名，返回 {平台参数: 源参数}。

    【为什么词表之外还要这一层】源参数名是 1688 商家在详情图上随手写的自由文本
    （腰围(橡筋)、裤长(不含吊带)、九分裤长…），穷举不完；而对齐失败的代价不是
    少填一列，是【整列被判缺、改用凭空估算的值】——那正是 999389808041 踩的坑。
    故词表命中不了时多问一次模型，比直接退到估算划算：映射只是选个名字，
    输入输出都极短（一次几百 token），而估算是整表重新造数。

    结果只保留「键在 targets 里、值在 sources 里」的项：模型偶尔会顺手编一个源里
    没有的参数名，放过去会让调用方按不存在的键取值、静默取空。

    best-effort：失败一律吞掉返回空映射（调用方随后照旧走估算），不让一个辅助的
    名称对齐把整个阶段⑨ 搞挂——这与项目「辅助路径坏了不影响主流程」的取向一致。
    """
    from app.publish.llm import ask_json

    try:
        data = await ask_json(
            _PARAM_MAP_PROMPT.format(targets="、".join(targets),
                                     sources="、".join(sources)),
            what="尺码表参数名映射", stage="sizechart")
    except Exception as e:
        logger.warning(f"尺码表参数名映射失败（照旧走估算兜底）：{e}")
        return {}
    out = {}
    for k, v in (data or {}).items():
        if k in targets and isinstance(v, str) and v in sources:
            out[k] = v
    if out:
        logger.info("尺码表参数名映射（模型）："
                    + "、".join(f"{k}←{v}" for k, v in out.items()))
    return out


# 交模型做「源表头 → 弹窗可选参数」的语义勾选判断。与 _map_params_by_llm 的分工：
# 那个是在【参数已确定要填】的前提下映射名字；这个是在【该勾哪几项】上做取舍——
# 弹窗那排「尺码参数」是可选复选框，源表头又是商家随手写的（裤长①/胸围③/肩带⑧），
# 精准/词表匹配经常勾不到正确的可选项，故交 LLM 按「是不是同一个身体部位」语义判断。
_PARAM_PICK_PROMPT = """你是服装尺码表整理助手。

商品：{title}
平台「添加尺码表」弹窗里，尺码参数是一排【可选复选框】，勾上哪几项表格就出现哪几列。
下面列出全部可选项（当前默认勾中项单独标注）：
{options}

源商品详情自带的实测尺码表，表头有这些列（商家随手写的，可能带序号/单位/别名）：
{sources}

请判断：哪些可选参数【源表里有对应的真实测量列】、应该勾上让源实测值填进去。规则：
1. 只从上面列出的可选项里挑，不要编造名字；
2. 按身体部位语义匹配，不看字面是否相同——「胸围③」对应「胸围全围」、「裤长①」对应「裤长」、
   「裤长②(内侧)」对应「裤内长」、「裤腿⑤」对应「大腿围全围」这类；
3. 【量的是不同部位就不勾】宁可少勾：源只有裤长/胸围/臀围时，不要勾「领围/肩宽/衣长」这些源没有的；
4. 「领围」这种平台默认勾中、但源表里根本没有该部位时，【绝不】因为它默认勾着就保留。

输出两部分（可选项原文照抄，勾不到的留空数组）：
{{"check": ["<源有对应、该勾上的参数>", ...], "uncheck": ["<默认勾中但源没有该部位、应取消的参数>", ...]}}"""


async def _pick_params_by_llm(title: str, available: list, src_params: list) -> dict:
    """问 LLM 源表头该勾弹窗里哪几项「尺码参数」，返回 {"check": [...], "uncheck": [...]}。

    【为什么要语义匹配】源表头是商家随手写的自由文本（裤长①/胸围③/肩带⑧/前裆⑥…），
    弹窗可选参数是另一套规范词（裤长/胸围全围/裤内长…），两套词汇不重合、精准匹配经常
    勾不到正确的可选项（真站取证 offer 1074392045040：源 8 列全在，却因没人勾「裤长/
    胸围全围」而全被浪费）。语义判断「是不是同一个部位」正是 LLM 的强项，与
    _map_params_by_llm 的取向一致（名称对齐问名字，比退到凭空估算划算）。

    结果只保留「确实是 available 里列出的可选项」的项：模型偶尔会顺手编一个可选项里
    没有的参数名，放过去会让勾选 JS 找不到复选框、白点一轮。

    best-effort：失败一律吞掉返回空（调用方随后照旧按当前勾中集填），不让一个辅助的
    勾选判断把整个阶段⑨ 搞挂。
    """
    from app.publish.llm import ask_json

    if not available or not src_params:
        return {}
    options = "、".join(
        f"{p['name']}{'（默认勾中）' if p.get('checked') else ''}" for p in available)
    try:
        data = await ask_json(
            _PARAM_PICK_PROMPT.format(
                title=title, options=options, sources="、".join(src_params)),
            what="尺码参数勾选判断", stage="sizechart")
    except Exception as e:
        logger.warning(f"尺码参数勾选判断失败（照旧按当前勾中集填）：{e}")
        return {}
    valid = {p["name"] for p in available}
    out = {}
    for key in ("check", "uncheck"):
        vals = [v for v in (data.get(key) or []) if isinstance(v, str) and v in valid]
        if vals:
            out[key] = vals
    if out:
        logger.info("尺码参数勾选判断（模型）：勾上 "
                    + "、".join(out.get("check", []) or ["（无）"])
                    + "；取消 " + "、".join(out.get("uncheck", []) or ["（无）"]))
    return out
