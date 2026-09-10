"""店小秘发布操作：sizechart.measurements。模块导航见 docs/publish-pipeline-refactor.md。"""

import re
import json
import math
from app.logger import logger
from app.publish import packaging, size_rules
from typing import Optional


# 非服装品类标签 → 尺码表估算的「专家身份」参数。
# 数据驱动：加新品类的专属工作流只在 dict 加一行；未映射标签落 other 通用兜底。
# 所有条目都带 nonapparel=True——非服装走几何推理提示词（直径→长宽、体重→净重）。
_NONAPPAREL_KIND = {
    "pet_supply": {"expert": "宠物用品尺寸专家",
                   "fit": "宠物窝/垫/床类（圆形或方形）",
                   "step": "尺寸随码数递增，圆形商品直径即长=宽",
                   "nonapparel": True},
    "home": {"expert": "家居用品尺寸专家",
             "fit": "家居/家具类（长宽高）",
             "step": "尺寸随码数递增",
             "nonapparel": True},
    "toy": {"expert": "玩具尺寸专家",
            "fit": "玩具/模型类",
            "step": "尺寸随码数递增",
            "nonapparel": True},
    "floral_decor": {"expert": "家居装饰尺寸专家",
                     "fit": "仿真花/摆件/装饰类",
                     "step": "尺寸随码数递增",
                     "nonapparel": True},
    "shoe": {"expert": "鞋类尺码专家",
             "fit": "鞋靴类（鞋码）",
             "step": "尺码随码数递增",
             "nonapparel": True},
    "bag": {"expert": "箱包尺寸专家",
            "fit": "箱包类（长宽高）",
            "step": "尺寸随码数递增",
            "nonapparel": True},
    "other": {"expert": "商品尺寸专家",
              "fit": "通用商品（长宽高/重量）",
              "step": "尺寸随码数递增",
              "nonapparel": True},
}


def _guess_size_kind(title: str, sizes: list, cat: str = "apparel") -> dict:
    """按【品类标签】与尺码字样推断估算提示词的专家身份。

    cat 是 classify_category 的品类标签。非 apparel（宠物/家居/玩具…）直接查
    _NONAPPAREL_KIND 数据映射，不再用词表猜——那是 LLM 意图识别的结果，避免
    「每加一个非服装品类就补词表」的膨胀（2026-09-06 宠物窝 sizechart 失败后收敛）。
    apparel 才按尺码字样细分童装/成人（年龄段与品类是正交维度，这里只做服装内档位）。
    只影响提示词措辞，判错不会写坏数据，故不做严格校验、也不报错。
    """
    if cat != "apparel":
        return dict(_NONAPPAREL_KIND.get(cat, _NONAPPAREL_KIND["other"]))
    joined = " ".join(str(x) for x in (sizes or []))
    is_kid = bool(re.search(r"\b(80|90|100|110|120|130|140|150|160)\s*cm", joined, re.I)
                  or re.search(r"童|宝宝|婴|幼|kid|child|toddler|girls?|boys?", title, re.I))
    is_adult = bool(re.search(r"\b(XS|S|M|L|XL|XXL|XXXL)\b", joined)
                    or re.search(r"女士|男士|成人|women|men\b", title, re.I))
    # 童装标题里也常出现 girls，故童装判据优先（成人女装不会带 cm 身高档）
    if is_kid and not re.search(r"\b(80|90|100|110|120|130|140|150|160)\s*cm", joined, re.I) and is_adult:
        is_kid = False
    if is_kid:
        return {"expert": "童装尺码专家", "fit": "常见童装版型",
                "step": "衣长约差 3~4cm、胸围全围约差 4~5cm"}
    if is_adult:
        return {"expert": "成人服装尺码专家", "fit": "常见成人版型",
                "step": "胸围全围约差 4cm、衣长约差 2cm"}
    return {"expert": "服装尺码专家", "fit": "该品类常见版型",
            "step": "梯度均匀、不出现跳档"}


def _check_measurements(est: dict, sizes: list, need: list, normalize_size=None) -> list:
    """校验估算表：单调递增 + 全围量级。返回问题描述列表（空 = 通过）。

    提示词里「单调递增」「全围不是半围」原本没有任何程序校验，而本项目已有
    共识「提示词是软约束，必须在校验层拦」（见 set_titles 的年份闸注释）。
    这里只做能客观判定的两条，不猜绝对值对不对：
      - 递增：按 sizes 给定顺序（源尺码本身有序），后一档不得小于前一档
      - 全围量级：全围类参数若小于同尺码衣长的 0.6 倍，极可能填了半围
    """
    def _row(sz):
        """两种键都试：调用方可能传归一键（"90"）也可能传原始尺码（"90cm"）。
        只按归一键取会在传原始键时静默跳过全部校验——那比不校验更危险。"""
        return (est.get((normalize_size or size_rules.norm_size)(sz)) or est.get(str(sz)) or {})

    problems = []
    for size in sizes:
        row = _row(size)
        missing = [param for param in need
                   if not isinstance(row.get(param), (int, float))
                   or isinstance(row.get(param), bool)
                   or not math.isfinite(row[param]) or row[param] <= 0]
        if missing:
            problems.append(f"尺码 {size} 缺少有效正数参数：{'、'.join(missing)}")
    for p in need:
        seq = []
        for sz in sizes:
            v = _row(sz).get(p)
            if isinstance(v, (int, float)):
                seq.append((sz, float(v)))
        for (s1, v1), (s2, v2) in zip(seq, seq[1:]):
            if v2 < v1:
                problems.append(f"{p} 在 {s1}->{s2} 反向（{v1}->{v2}），应随尺码递增")
                break
    # 全围疑似半围：与同尺码【衣长】比。
    # 【阈值 0.95 不是 0.6】上装成衣的胸围全围通常 >= 衣长（童装 90cm：衣长约 40、
    # 胸围全围约 60+）；填成半围才会明显小于衣长。原先取 0.6 倍太松——半围 31
    # 对衣长 40 是 0.78 倍，直接漏过，等于这条校验形同虚设。
    # 只拿「衣长」作参照：裤长/裙长远大于腰围全围，用它们比会误报。
    for sz in sizes:
        row = _row(sz)
        length = next((float(v) for k, v in row.items()
                       if str(k).strip() in ("衣长", "上衣长") and isinstance(v, (int, float))),
                      None)
        if not length:
            continue
        for k, v in row.items():
            if ("全围" in str(k) and isinstance(v, (int, float))
                    and float(v) < length * 0.95):
                problems.append(
                    f"{sz} 的 {k}={v} 明显小于衣长 {length}，疑似填了半围（应为绕一圈的全围）")
    return problems


async def _estimate_measurements(title: str, size_ref: dict, sizes: list,
                                 need: list, known: dict,
                                 part: str = "",
                                 src_rows: Optional[dict] = None,
                                 cat_path=None, normalize_size=None) -> dict:
    """按身高体重参考 + 已有实测列，估算弹窗缺的那几个测量参数。

    走 llm.ask_json：它内部用 get_llm()，自动跟随发布页 UI 的模型下拉，顺带白拿
    JSON 解析与三次重试。本管线原先有个写死 [llm.publish] 段的 _llm_publish，与
    下拉脱钩（用户切了 DeepSeek，标题/包装/SKU分类三处还在打 grok，撞上 Packy
    503 限流才暴露），2026-08-23 已全部并到 ask_json 上、该函数删除。

    把已有实测列一起塞进提示词（known）是为了让估算与实测同档：源已给衣长 44 时，
    模型报的胸围应当是同一件衣服的胸围，而不是脱开源数据另算一套版型。

    part：套装分件填表时【本张表是哪一件】（上衣/连衣裙…）。不带这个，套装的两张表
    都要估算时模型看到的输入完全相同（标题是整个套装的），必然给出同一套值——那正是
    「两张尺码表参数一样」的另一半成因（见 _pick_part_measurements 的真站取证）。

    src_rows：源实测的【对齐前原始行】（键已过 norm_size，参数名是商家原写法）。
    known 是对齐【之后】的表，未对齐上的列已经被剔掉了——2026-09-01 取证
    （offer 999389808041 的连体裤）源明明给了总衣长 59~79，因为平台叫「摆长」没对上，
    模型连这条唯一可靠的档位锚点都看不到，于是把摆长估成 32~44。故把原始行也一并给出：
    参数名对不上不等于数据没价值，同一件衣服的其它量法就是最好的量级参照。
    """
    from app.publish.llm import ask_json

    normalize_size = normalize_size or size_rules.norm_size

    ref_text = "\n".join(f"- {k}：{v}" for k, v in size_ref.items()
                         if k != "note") or "（无源参考）"
    known_text = "\n".join(
        f"- {s}：" + "、".join(f"{p}{v}" for p, v in (known.get(normalize_size(s)) or {}).items())
        for s in sizes if known.get(normalize_size(s))
    ) or "（无）"
    # 源原始行里【已对齐过的参数不再重复列】：known_text 已经写过它们，重复只是
    # 拉长提示词。剩下的正是「源量了、但平台参数名对不上」的那些列，它们是估算的档位锚点。
    aligned_names = {p for s in sizes for p in (known.get(normalize_size(s)) or {})}
    raw_lines = []
    for s, row in (src_rows or {}).items():
        rest = {p: v for p, v in row.items() if p not in aligned_names}
        if rest:
            raw_lines.append(f"- {s}：" + "、".join(f"{p}{v}" for p, v in rest.items()))
    raw_text = ("\n源商品同一件商品的其它实测量法（参数名与平台不同，不要照抄名字，"
                "只作量级与档差参照——估算值必须与它们协调、不能量级失真）：\n"
                + "\n".join(raw_lines) + "\n") if raw_lines else ""
    # 【品类识别：服装走词表、非服装走 LLM】现场 cat_path 可用（auto_cat 已判类目），
    # 传下去让 classify_category 更准；续跑拿不到就退标题。品类决定 _guess_size_kind
    # 的身份，非服装（宠物/家居/玩具）走几何推理提示词。
    cat = await packaging.classify_category(title, cat_path)
    kind = _guess_size_kind(title, sizes, cat)
    # 【非服装换一套措辞与推理规则】宠物窝的「宽/净重/长」，源数据常写成「直径」
    # 「长*宽*高」与「适用体重」，服装提示词的「试穿参考/全围」对它们毫无意义，
    # 模型看到直径也不会知道长=宽=直径（2026-09-06 宠物窝两单 sizechart 全卡在
    # 估算补不齐宽/净重/长）。故非服装换成几何推理引导。
    nonapparel = bool(kind.get("nonapparel"))
    # 部件提示放在标题之后、参考数据之前：先让模型知道「只看这一件」再读数据
    part_text = (f"本次只估算这个套装里的【{part}】这一件，测量值必须是这一件的"
                 f"（不要按套装里另一件的版型给）。\n"
                 if part else "")
    if nonapparel:
        ref_label = "源商品提供的参考信息（适用体重/直径/长宽高）："
        known_label = "源商品已给出的实测尺寸（同一件商品，估算须与之协调）："
        meas_label = "实际测量值（单位cm，净重单位克）"
        rules = (f"1. 符合{kind['fit']}，数值随尺码单调递增、梯度合理"
                 f"（{kind['step']}）；\n"
                 "2. 源数据或尺码名里带「直径/铺开直径/展开直径」时，圆形商品"
                 "长=宽=直径（如直径55cm即长55、宽55；尺码名「【直径 60cm…】」"
                 "就是该尺码直径60cm——尺码名里的直径同样是硬数据，不要脱离它"
                 "凭空编矩形尺寸）；\n"
                 "3. 源数据是「长*宽*高」格式时，按顺序拆成对应维度；\n"
                 "4. 「净重」是商品自重（克），不是宠物体重——从适用体重（斤）或"
                 "包装重量推理；\n"
                 "5. 每个尺码都要给，且只给上面列出的参数；\n")
    else:
        ref_label = "源商品提供的试穿参考（身高/体重）："
        known_label = "源商品已给出的实测尺寸（同一件衣服，估算须与之协调）："
        meas_label = "实际成衣测量值（单位cm）"
        rules = (f"1. 符合{kind['fit']}，数值随尺码单调递增、梯度合理"
                 f"（相邻尺码{kind['step']}）；\n"
                 "2. 全围类参数（胸围/腰围/臀围全围）是绕一圈的全围，不是半围——"
                 "半围写成全围会差一倍，这是买家退货的高发成因；\n"
                 "3. 每个尺码都要给，且只给上面列出的参数；\n")
    prompt = (
        f"你是{kind['expert']}。商品：{title}\n"
        f"{part_text}"
        f"{ref_label}\n{ref_text}\n\n"
        f"{known_label}\n{known_text}\n"
        f"{raw_text}\n"
        f"请给出尺码 {'/'.join(sizes)} 的{meas_label}："
        f"{'、'.join(need)}。\n"
        f"要求：{rules}"
        "输出键必须逐字使用页面尺码，不能改成源表的小号/大号，也不能增加包装层。"
        "源表无法确定对应档位时只作为量级参考，不能声称某档就是页面均码。"
        "每行的所有参数都必须是大于零的有限数值，不得遗漏。"
        "只输出严格JSON，将以下结构的 null 全部替换为测量数值："
        + json.dumps({size: {param: None for param in need} for size in sizes}, ensure_ascii=False)
    )
    data = await ask_json(prompt, what="尺码表测量值估算", stage="sizechart")
    est = {normalize_size(k): v for k, v in data.items() if isinstance(v, dict)}

    # 校验不过就把具体问题回喂重生成一次。全自动路线：不转人工、不中断，
    # 重试后仍不过也照用（估算值本身是兜底数据，卡住整个商品代价更大），
    # 但把问题写进 warning 留痕，便于事后按日志抽查。
    # 【键名对不上与数值不准要一样触发重生成】2026-09-09 pdd 928028669617：模型
    # 把尺码键改写成自有形态（没逐字回写「【直径 60cm+22朵玫瑰】」这类长键），
    # norm 后一行都对不上尺码行，_check_measurements 查不到任何数值、报不出问题，
    # 于是不重试、直接在对齐处死档。把「有尺码行没被覆盖」也列为问题，借下面既有
    # 的重生成把「键名逐字一致」的要求回喂一次。重试后的复算同样要带上这条，
    # 否则键名仍错的 est2 会查出零问题、被当成修好照用。
    def _uncovered_rows(e):
        return [str(s) for s in sizes
                if not (e.get(normalize_size(s)) or e.get(str(s)))]

    def _all_problems(e):
        ps = _check_measurements(e, sizes, need, normalize_size=normalize_size)
        miss = _uncovered_rows(e)
        if miss:
            ps.append("这些尺码没给到（输出键必须与上面列出的尺码名逐字一致）："
                      + "、".join(miss))
        return ps

    problems = _all_problems(est)
    if problems:
        logger.warning("尺码估算校验未过，重生成一次：" + "；".join(problems[:3]))
        retry = await ask_json(
            prompt + "\n\n上次输出有这些问题，请修正后重新给全表："
            + "；".join(problems) + "\n注意全围是绕一圈的周长，数值必须随尺码递增。",
            what="尺码表测量值估算(重试)", stage="sizechart",
        )
        est2 = {normalize_size(k): v for k, v in retry.items() if isinstance(v, dict)}
        left = _all_problems(est2)
        # 先比覆盖行数（键对不上=下游缺行硬失败），覆盖相同才比问题数（数值疑点
        # 本来就是「照用留痕」的软问题，不该让软问题否决一次键名修好的重试）
        miss1, miss2 = len(_uncovered_rows(est)), len(_uncovered_rows(est2))
        if miss2 < miss1 or (miss2 == miss1 and len(left) < len(problems)):
            est = est2
            problems = left
        if problems:
            logger.warning("尺码估算重试后仍有疑点（照用，已留痕）："
                           + "；".join(problems[:3]))
    return est
