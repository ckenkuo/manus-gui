"""店小秘发布操作：attributes.composition。模块导航见 docs/publish-pipeline-refactor.md。"""

import re
from app.logger import logger
from typing import Optional


# 成分补差的候选填充纤维（按优先级）。聚酯纤维是最常见的混纺配料；
# options 里没有时退到氨纶（弹性面料常见）。都没有则拒绝该组、交人工。
#
# 【2026-08-21 补】候选必须剔除该组已占用的纤维：源主成分本身就是聚酯纤维 90% 时，
# 原实现会再补一行聚酯纤维 10%，同一字段两行同纤维，平台校验必拦。
_COMP_FILLERS = ("聚酯纤维(涤纶）", "聚酯纤维", "氨纶", "棉")


# 成分字段里哪些算「主面料」：只有主面料行的百分比能用源商品的主面料成分含量覆盖。
# 里衬/里料/辅料/填充/内衬是另一块布料，与源主面料含量无关，必须排除。
_COMP_SUB_MARKS = ("里衬", "里料", "内衬", "辅料", "填充", "内里")


def _is_main_comp_label(label: str) -> bool:
    """判断某成分字段是否属于主面料（上装成分/下装成分/材质 等）。"""
    if not label or ("成分" not in label and "材质" not in label):
        return False
    return not any(m in label for m in _COMP_SUB_MARKS)


def _norm_fiber(name: str) -> str:
    """纤维名归一：只留中文，去掉半/全角括号与注解差异。

    源属性写「聚酯纤维（涤纶）」（全角括号），表单 options 写「聚酯纤维(涤纶）」
    （半角左括号 + 全角右括号，平台自己就不对称），直接字符串比必然不等。
    """
    return "".join(ch for ch in (name or "") if "一" <= ch <= "鿿")


# 纤维同义组：同一根纤维在源属性/表单 options 里的不同写法。
# 【为什么不能只靠去括号归一】_norm_fiber 只删括号，「聚酯纤维(涤纶）」「聚酯纤维」
# 「涤纶」归一后是三个不同字符串，去重全部失效——实测会填出「涤纶 80% + 聚酯纤维
# 20%」，同一字段两行同纤维，平台必拦（2026-08-24 用户报错）。正确结果是合并成
# 一行 100% 涤纶。每组第一个元素当规范键，仅用于判同、不用于填表。
_FIBER_SYNONYMS = (
    ("聚酯纤维", "涤纶", "涤", "聚酯", "PET", "polyester"),
    ("氨纶", "莱卡", "弹性纤维", "spandex", "elastane", "lycra"),
    ("锦纶", "尼龙", "聚酰胺纤维", "nylon", "polyamide"),
    ("粘纤", "粘胶纤维", "黏胶纤维", "粘胶", "人造棉", "viscose", "rayon"),
    ("腈纶", "聚丙烯腈纤维", "acrylic"),
    ("棉", "棉纤维", "cotton"),
    ("羊毛", "毛", "wool"),
    ("羊绒", "山羊绒", "cashmere"),
    ("莫代尔", "modal"),
    ("亚麻", "麻", "linen"),
    ("蚕丝", "真丝", "桑蚕丝", "silk"),
    ("竹纤维", "竹浆纤维"),
    ("醋酸纤维", "醋酸", "acetate"),
)


def _fiber_key(name: str) -> str:
    """纤维同义归一：返回规范键，用于判断两个写法是否同一根纤维。

    先按 _norm_fiber 去括号（表单写法括号半全角不对称），再查同义组。
    命中同义组里任一别名就返回该组第一个元素作为键；没命中就返回去括号结果
    （未知纤维仍能自比，只是享受不到同义合并）。

    注意匹配用「包含」而非相等：表单选项常写「聚酯纤维(涤纶）」这种带注解的形式。
    别名按长度降序试，避免「棉」抢在「棉纤维」之前误命中更长的名字。
    """
    norm = _norm_fiber(name)
    if not norm:
        return ""
    low = (name or "").lower()
    for group in _FIBER_SYNONYMS:
        for alias in sorted(group, key=len, reverse=True):
            a = _norm_fiber(alias)
            if (a and a in norm) or (not a and alias.lower() in low):
                return group[0]
    return norm


def _merge_same_fiber(items: list, opts: list) -> list:
    """把同一根纤维的多行合并成一行（百分比相加），并重排 row。

    这是「涤纶 80% + 聚酯纤维 20%」的正解：不是并列两行，而是一行 100% 涤纶。
    值取组内在 options 里存在的写法（表单只认 options 内的值），都不在则取首个。
    """
    groups: dict = {}
    order: list = []
    for it in items:
        k = _fiber_key(it.get("value", ""))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    merged = []
    for idx, k in enumerate(order, start=1):
        rows = groups[k]
        if len(rows) == 1:
            merged.append({**rows[0], "row": idx})
            continue
        total = sum(float(r.get("num") or 0) for r in rows)
        value = next((r["value"] for r in rows if r.get("value") in opts),
                     rows[0].get("value"))
        pcts = "+".join(str(r.get("num")) for r in rows)
        merged.append({**rows[0], "value": value, "num": int(total), "row": idx,
                       "reason": f"同一纤维 {len(rows)} 行已合并（{pcts}={int(total)}%）"})
        logger.info(f"成分合并：{[r.get('value') for r in rows]} → {value} {int(total)}%")
    return merged


def _match_fiber(src_fiber: str, values: list) -> Optional[str]:
    """在候选值里找与源主纤维对应的那一项，找不到返回 None（不猜）。

    先按归一后完全相等匹配；不中再退到包含关系（源「聚酯纤维」对上表单
    「聚酯纤维(涤纶）」），包含命中多项时取最短的，避免「棉」误配到长名纤维上。
    """
    src = _norm_fiber(src_fiber)
    if not src:
        return None
    exact = [v for v in values if _norm_fiber(v) == src]
    if exact:
        return exact[0]
    part = [v for v in values if src in _norm_fiber(v)]
    if part:
        return min(part, key=lambda v: len(_norm_fiber(v)))
    return None


# 按源属性推断配料纤维的依据表：(属性文本匹配正则, 目标纤维关键词)。
# 顺序即优先级，越靠前依据越硬。这是为了少走 _COMP_FILLERS 那条纯编造的路。
_COMP_HINTS = (
    (r"氨纶|弹力|微弹|莱卡|spandex", "氨纶"),
    (r"摇粒绒|珊瑚绒|法兰绒|抓绒|涤纶|涤|聚酯", "聚酯纤维"),
    (r"锦纶|尼龙|nylon", "锦纶"),
    (r"粘纤|粘胶|人造棉|莫代尔|viscose", "粘纤"),
    (r"羊毛|羊绒|马海毛", "腈纶"),
    (r"棉", "棉"),
)


# 常见纤维名，用于在源属性里找「第二种纤维」——源自己写了两种纤维时那是最硬的依据。
_FIBER_NAMES = ("聚酯纤维", "涤纶", "氨纶", "锦纶", "尼龙", "棉", "腈纶",
                "粘纤", "粘胶", "莫代尔", "羊毛", "羊绒", "亚麻", "麻",
                "蚕丝", "真丝", "竹纤维", "醋酸")


def _infer_filler(attrs: dict, main_fiber: str, opts: list, used_key: str):
    """按源属性推断配料纤维，返回 (选项文本, 依据说明) 或 (None, None)。

    先找源属性里明写的第二种纤维（最硬依据），再按面料/工艺特征推导。
    两条都不中时返回 None，由调用方退到 _COMP_FILLERS。
    """
    blob = " ".join(str(v) for v in (attrs or {}).values())[:800]

    # (a) 源属性里明写的第二种纤维
    for nm in _FIBER_NAMES:
        if nm in blob and _fiber_key(nm) != used_key:
            hit = _match_fiber(nm, opts)
            if hit:
                return hit, f"源属性提到「{nm}」"

    # (b) 按面料/工艺特征推导
    for pat, fiber in _COMP_HINTS:
        if re.search(pat, blob, re.I) and _fiber_key(fiber) != used_key:
            hit = _match_fiber(fiber, opts)
            if hit:
                return hit, f"源属性含「{re.search(pat, blob, re.I).group(0)}」推导"
    return None, None


def _rebuild_main_comp(label: str, items: list, opts: list, main_comp: dict) -> tuple:
    """按源商品的主面料成分含量重建某个主面料成分字段的成分行，返回 (rows, reject)。

    这是「成分比例不靠模型」的落点。源页面只给一个确定事实——主面料纤维 + 其含量
    （见 extract.parse_main_composition），所以能确定性构造的也只有两行：
      第 1 行 = 源主纤维 @ 源含量；第 2 行 = 剩余份额给一种配料纤维。
    模型给的百分比一律丢弃（它在两次调用间会从 55/45 漂到 90/10），但模型选的
    【配料纤维种类】保留——那是它看图/看参数得出的定性判断（如微弹→氨纶），
    比写死的候选表更贴近实物，只是数值不由它定。

    源主纤维在 options 里找不到对应项时返回 (None, None)，交回原有路径，不硬凑。

    【2026-08-25 起 main_comp 恒非空】parse_main_composition 在源没写含量、甚至没写
    主面料成分时也会给出「某纤维 100%」并打 assumed 标记；此时 pct=100，下面单行返回、
    不进补差分支——正是用户要的「源没写就全部写一种纤维 100%」。
    """
    target = _match_fiber(main_comp.get("fiber", ""), opts)
    if not target:
        return None, None
    pct = int(main_comp["percent"])
    rows = [{"label": label, "value": target, "num": pct, "row": 1,
             "reason": (main_comp.get("assumed")
                        or f"源主面料成分含量 {main_comp.get('raw') or pct}，按源值写入")}]
    if pct >= 100:
        return rows, None
    # 配料纤维三级依据（越靠前越硬）：
    #   1. 模型选的（它看图/看参数得出的定性判断）
    #   2. 源属性推断（明写的第二种纤维，或面料/工艺特征）
    #   3. 写死的候选表——纯编造，只在前两条都不中时用
    # 用 _fiber_key 判同（同义归一）：_norm_fiber 只去括号，认不出涤纶=聚酯纤维
    used = _fiber_key(target)
    basis = "配料种类沿用模型判断"
    # 【2026-09-08 增】聚焦推理纠正主纤维时（源「棉混纺」→主「棉」），模型在【原错误前提】下
    # 选的那根配料（如它以为主涤纶→辅尼龙）不可沿用；改用聚焦推理一并归约的 fillFiber 优先，
    # 映射失败/同纤维再退回原「沿用模型判断」逻辑。
    fill_fiber = main_comp.get("fillFiber")
    if fill_fiber and _fiber_key(fill_fiber) != used and fill_fiber in opts:
        second = fill_fiber
        basis = "配料种类由成分归一推理给出"
    else:
        second = next((i["value"] for i in items
                       if _fiber_key(i.get("value", "")) != used and i.get("value") in opts), None)
    if not second:
        second, why = _infer_filler(main_comp.get("srcAttrs") or {}, target, opts, used)
        if second:
            basis = why
    if not second:
        second = next((o for o in _COMP_FILLERS
                       if o in opts and _fiber_key(o) != used), None)
        if second:
            # 这一行是全流程唯一「凭常见配比编出来」的成分数据，留痕便于抽查
            basis = "无源依据，按最常见混纺配料填充"
            logger.warning(f"成分补差无源依据，{label} 第2行填「{second}」"
                           f"{100 - pct}%（源只给了 {target} {pct}%）")
    if not second:
        return None, {"label": label,
                      "rejectReason": f"主成分 {pct}% 需补差但 options 无可用配料纤维"}
    rows.append({"label": label, "value": second, "num": 100 - pct, "row": 2,
                 "reason": f"补差 {100 - pct}% 凑足 100%（{basis}）"})
    return rows, None


def _merge_composition_sources(info: dict) -> dict:
    """整合所有来源的成分信息，按优先级返回最佳数据源。

    【2026-09-02 新增】成分信息现在有四个可能来源，优先级从高到低：
      1. compositionFromText（详情文字，商家白纸黑字写的，最可靠）
      2. compositionFromVision（详情图 OCR，有一定误差但比源属性完整）
      3. mainComposition（源属性「主面料成分含量」，单一值，可能不完整）
      4. 默认值（聚酯纤维 100%，兜底）

    返回结构与 mainComposition 一致（供 _ask_attr_review 使用）：
      {"fiber": 纤维名, "percent": 百分比, "raw": 原文,
       "byVariant": {款式名: {纤维: 百分比}},  // 可选，按款式区分时才有
       "source": 数据来源标记,
       "srcAttrs": 源属性字典}

    【按款式区分的成分】只有 compositionFromText / compositionFromVision 可能给出
    byVariant（详情里商家写了「卡通款 35棉65涤 / 花边款 82棉18涤」这种），
    mainComposition 永远是单一值。阶段④写成分时，LLM 会根据表单已填的颜色/款式
    从 byVariant 里匹配对应的成分组。
    """
    # 优先级1：详情文字
    text_comp = info.get("compositionFromText")
    if isinstance(text_comp, dict):
        main = text_comp.get("main") or {}
        # 转成 mainComposition 的结构（fiber/percent），取 main 里的首个纤维
        fibers = list(main.items())
        if fibers:
            fiber, pct = fibers[0]
            result = {"fiber": fiber, "percent": pct,
                      "raw": f"详情文字：{fiber} {pct}%",
                      "source": "descText",
                      "srcAttrs": info.get("attributes") or {}}
            # byVariant 原样透传（阶段④ LLM 会用）
            if text_comp.get("byVariant"):
                result["byVariant"] = text_comp["byVariant"]
            return result
        # main 为空但有 byVariant 时，仍需返回（让 LLM 从 byVariant 里匹配）
        if text_comp.get("byVariant"):
            # 取 byVariant 第一个款式的首个纤维作为默认值
            first_variant = next(iter(text_comp["byVariant"].values()), {})
            fibers_var = list(first_variant.items())
            if fibers_var:
                fiber, pct = fibers_var[0]
                result = {"fiber": fiber, "percent": pct,
                          "raw": f"详情文字（按款式）：{fiber} {pct}%",
                          "source": "descText",
                          "srcAttrs": info.get("attributes") or {},
                          "byVariant": text_comp["byVariant"]}
                return result

    # 优先级2：详情图识别
    vision_comp = info.get("compositionFromVision")
    if isinstance(vision_comp, dict):
        main = vision_comp.get("main") or {}
        fibers = list(main.items())
        if fibers:
            fiber, pct = fibers[0]
            result = {"fiber": fiber, "percent": pct,
                      "raw": f"详情图识别：{fiber} {pct}%",
                      "source": "vision",
                      "srcAttrs": info.get("attributes") or {}}
            if vision_comp.get("byVariant"):
                result["byVariant"] = vision_comp["byVariant"]
            return result
        # main 为空但有 byVariant 时，仍需返回（让 LLM 从 byVariant 里匹配）
        if vision_comp.get("byVariant"):
            # 取 byVariant 第一个款式的首个纤维作为默认值
            first_variant = next(iter(vision_comp["byVariant"].values()), {})
            fibers_var = list(first_variant.items())
            if fibers_var:
                fiber, pct = fibers_var[0]
                result = {"fiber": fiber, "percent": pct,
                          "raw": f"详情图识别（按款式）：{fiber} {pct}%",
                          "source": "vision",
                          "srcAttrs": info.get("attributes") or {},
                          "byVariant": vision_comp["byVariant"]}
                return result

    # 优先级3：源属性（原有逻辑）
    main_comp = info.get("mainComposition")
    if main_comp and main_comp.get("fiber"):
        return {**main_comp, "srcAttrs": info.get("attributes") or {}}

    # 优先级4：兜底（原有逻辑，按默认纤维处理）
    from app.publish.workflows import composition_for
    main_comp = composition_for(info)
    return {**main_comp, "srcAttrs": info.get("attributes") or {}}


# ④b 主纤维归约提示词：把源「主面料成分」里的合成词/占位词（「棉混纺」「涤棉」「牛仔布」）
# 归约成确定主纤维。为何单开一次而非并进 _ATTR_PROMPT：见 _resolve_main_fiber 的说明。
_COMP_NORM_PROMPT = """你是跨境服装的主纤维归约助手。源商品的「主面料成分」是某个无法
直接映射到标准纤维选项的合成词/占位词（如「棉混纺」「涤棉」「牛仔布」），需要你结合
材质名与品类常识推断出它的【主纤维】。

源商品标题：{title}
源商品参数（{platform}）：{src_attrs}
源主面料成分原文：{raw}
图片理解摘要：{image_understanding}

推断要点：
1. 「X混纺」指以 X 为主的混纺（「棉混纺」→ 主纤维「棉」；「涤棉混纺」按面料名称/图与占比判主次）。
   「牛仔/针织/梭织」是织物组织不是纤维，主纤维按品类常识（牛仔多为棉；针织/梭织看面料名称）。
2. 只给【一根】主纤维：优先取「主面料成分」里最明确的那根，其次「面料名称/工艺」，
   都没有按该品类常识选最常见的那根（跨境服装最常是聚酯纤维）。
3. 主纤维从下面常见纤维里选语义最贴近的一种：{fibers}
4. 若源是混纺（如「X混纺」「X+Y」），除主纤维外还要推断一根【配料纤维】放进 filler（取语义
   最贴近的一种；源实际是单一成分、无混纺时 filler 留空）。配料纤维也从下面常见纤维里选：{fibers}

只输出JSON: {{"fiber": "主纤维名", "filler": "配料纤维名或空", "basis": "一句话依据（引用具体字段/特征）"}}"""


async def _resolve_main_fiber(info: dict, main_comp: dict, comp_opts: list) -> dict:
    """源主面料成分是「棉混纺」这类合成词/占位词、融合后仍解析不出确定 fiber 时，
    单开一次聚焦 LLM 推理把主纤维归约出来并回填 main_comp.fiber，供 _rebuild_main_comp
    做确定性比例覆盖（比例不再靠模型编）。

    【为什么单开，不并进 _ATTR_PROMPT】阶段④审核一次处理几十个字段，成分只是顺带推断
    的对象——「棉混纺」被推成「涤纶65+尼龙35」就是这么来的（2026-09-08 offer
    911772292056 实测）。单开只喂成分相关线索，模型不受无关字段干扰；结果回填后仍由
    _rebuild_main_comp 按源含量/默认值确定百分比，只把【主纤维种类】交给模型定。

    【不硬编码合成词表】「X混纺」「X+Y混纺」「涤棉」这类组合无穷，程序无法穷举，也没必要。
    把「这词到底是什么主纤维」交给大模型，配材质名/工艺/品类/图片摘要推理，再用
    _match_fiber 把模型给的通用名映射到表单 options 里的写法（命中才回填）。

    best-effort：推理异常或归约结果落不到 comp_opts 时，返回 main_comp 原样——
    主流程照旧走「模型给数 + 合计校验」，绝不让这里失败中断发布。
    """
    import json as _json

    from app.publish.llm import ask_json

    # 取「成分词原文」当推理线索：main_comp.raw 记的是【含量】原文（如 "65%"），
    # 真正要拆解的是成分词本身（如「棉混纺」「牛仔布」）——它已由各适配器的
    # parse_composition 存进 main_comp.fiberText，故直接读它，不再从 attributes 里按
    # 中文键名（「主面料成分」）挑（键名是平台相关的，拼多多用「面料/材质」）。
    raw = (main_comp.get("fiberText") or (main_comp.get("raw") or "")).strip()
    if not raw:
        return main_comp
    # 源商品参数传全量 attributes（键名原样、不做跨平台归一，与 _ATTR_PROMPT 同口径）
    src_attrs = info.get("attributes") or {}
    prompt = _COMP_NORM_PROMPT.format(
        platform=(info.get("source") or {}).get("platformName") or "1688",
        title=info.get("title"),
        src_attrs=_json.dumps(src_attrs, ensure_ascii=False),
        raw=raw,
        image_understanding=_json.dumps(
            info.get("imageUnderstanding", {}), ensure_ascii=False),
        fibers="、".join(_FIBER_NAMES),
    )
    try:
        data = await ask_json(prompt, what="成分纤维归一", stage="comp_norm")
    except Exception as _e:
        logger.warning(f"成分纤维归约失败（{_e}），按源占位词走原有「模型给数+合计校验」")
        return main_comp
    fiber = (data or {}).get("fiber") or ""
    if not fiber:
        return main_comp
    target = _match_fiber(fiber, comp_opts)  # 映射到表单 options 写法，命中才回填
    if not target:
        logger.warning(f"成分归约到「{fiber}」在 options 里没有可落地写法，按源占位词走原路径")
        return main_comp
    new = dict(main_comp)
    new["fiber"] = target
    # 配料纤维也由聚焦推理一并归约并回填，供 _rebuild_main_comp 补差时优先用——
    # 避免沿用模型在「原错误主纤维」前提下瞎挑的第二根（如把「棉混纺」配成尼龙）。
    filler_raw = (data or {}).get("filler") or ""
    if filler_raw:
        filler_target = _match_fiber(filler_raw, comp_opts)
        if filler_target and _fiber_key(filler_target) != _fiber_key(target):
            new["fillFiber"] = filler_target
    if not (new.get("percent") and 0 < new["percent"] < 100):
        new["percent"] = 100
    new["assumed"] = (f"源主面料成分是「{raw}」"
                      f"（{data.get('basis') or '合成词'}），主纤维由 LLM 依材质名归约为「{target}」")
    logger.info(f"成分纤维归一：源「{raw}」→ 主纤维「{target}」"
                f"（{data.get('basis') or '依材质/品类'}）")
    return new
