"""店小秘发布操作：packaging。模块导航见 docs/publish-pipeline-refactor.md。"""

from app.logger import logger


# 非服装类包裹的兜底尺寸/重量：模型两次都给出越界值时用它，保证流程不停
# （服装类不走模型，见 _APPAREL_DIMS）。取常见快递袋尺度，宁可略偏保守（不低报运费）。
_PACK_FALLBACK = {"长": 30, "宽": 24, "高": 5, "重量": 400}


# 服装类包裹的确定性申报尺寸（cm，长x宽x高）。
# 【为什么服装类不问模型】2026-08-25 用户确认：服装压平装快递袋，尺寸只由袋规格
# 决定、与款式无关，本店一律 30x25x3；让模型逐个商品去估，只会在同一批服装里估出
# 一堆互不相同的数（同款不同色都能差出 5cm），既不更准也不可复核。
# 需要按实物体积算的（玩具、鞋盒、家居用品等有刚性包装的品类）才交模型估。
_APPAREL_DIMS = ("30", "25", "3")


# 服装类判定词：命中类目路径或标题即按服装处理。
# 只用于选「固定尺寸 or 模型估算」，判错的代价是尺寸偏差而非流程中断，故不做校验。
#
# 【不收「套装」这类量词】它跨品类通用（积木套装、餐具套装都是它），单靠它判服装
# 会把带刚性包装的品类误判成压平袋装、高只填 3cm。判服装要靠品类本身的词。
_APPAREL_WORDS = (
    "服装", "服饰", "童装", "女装", "男装", "内衣", "内裤", "上装", "下装",
    "外套", "夹克", "卫衣", "毛衣", "衬衫", "T恤", "背心", "吊带", "裤", "裙",
    "连体衣", "泳装", "泳衣", "睡衣", "家居服", "袜", "围巾", "手套",
    # 「衫」是高频服装特征字，覆盖针织衫/POLO衫/T恤衫/汗衫等一大批，且几乎只出现在
    # 服装词里（排除词里没有含「衫」的），加一个字能省掉这些服装词走 LLM 品类识别。
    "衫",
)


# 排除词：命中这些就不按服装处理，优先于 _APPAREL_WORDS。
# 「帽/袜」等配件本身贴着服装类，但硬壳收纳、玩具、鞋盒这些一旦沾上服装词
# （如「玩具服装」「鞋袜收纳盒」）就会被固定成 30x25x3，实际体积差一截。
_APPAREL_EXCLUDE = ("玩具", "积木", "鞋盒", "收纳", "餐具", "水杯", "保温杯",
                    "文具", "家具", "电器", "礼盒", "模型")


def _is_apparel(cat_path, title: str) -> bool:
    """判断是否服装类（决定包裹尺寸走固定值还是模型估算）。

    类目路径优先：那是店小秘表单里已生效的真实类目，比标题可靠。
    类目为空（续跑时状态文件没回填到）才退到标题匹配。
    排除词先判：宁可多问一次模型（几秒），也不要把带刚性包装的品类按压平袋装填。
    """
    blob = " ".join(str(x) for x in (cat_path or [])) or (title or "")
    if any(w in blob for w in _APPAREL_EXCLUDE):
        return False
    return any(w in blob for w in _APPAREL_WORDS)


# 品类标签集合（classify_category 的输出域）。
# 词表快路径判服装返回 "apparel"，LLM 意图识别作为兜底也能输出 "apparel"（词表漏判
# 的服装词如「针织衫」靠它兜住）或非服装细分标签。开放集合：下游 _NONAPPAREL_KIND
# 按标签映射到工作流，未映射标签落 other 通用兜底，新增标签不破坏任何调用方。
_CATEGORY_TAGS = ("apparel", "pet_supply", "home", "toy", "floral_decor", "shoe", "bag", "other")


async def classify_category(title: str, cat_path=None) -> str:
    """统一品类识别：服装走词表快速路径，词表未命中走 LLM 意图识别。

    【为什么分层】服装词（服装/服饰/裤/裙/卫衣…）大多是封闭集合，词表又快又稳；但
    服装词表本身也难穷尽（针织衫/POLO衫/马甲…），故词表判非服装后再交一次轻量 LLM，
    让它既能兜住漏判的服装（输出 apparel），又能细分开放集合的非服装品类（宠物窝/
    鱼缸/帐篷…），避免每加一个就补词表。set_variant 的同步路径 _is_apparel 仍独立。

    返回品类标签字符串：_CATEGORY_TAGS 之一。
    """
    if _is_apparel(cat_path, title):
        return "apparel"
    return await _llm_classify_nonapparel(title, cat_path)


async def _llm_classify_nonapparel(title: str, cat_path=None) -> str:
    """LLM 意图识别品类（词表快速路径未命中时），输出 _CATEGORY_TAGS 里的标签。

    提示词只列「标签 → 代表词」的少量示例做引导，不枚举具体品类词：模型按语义把
    「鱼缸」归 pet_supply、「针织衫」归 apparel、「帐篷」归 home/other。输出不落在
    _CATEGORY_TAGS 里时落 other（兜底，别让一个非法标签带下水）。
    """
    from app.publish.llm import ask_json

    clue = " > ".join(str(x) for x in (cat_path or [])) if cat_path else ""
    prompt = (
        "你是商品品类识别助手。判断这个商品的品类，只输出一个品类标签。\n\n"
        "可选标签（括号内是代表词示例，不是穷举，按语义归位即可）：\n"
        "- apparel（服装、服饰、针织衫、T恤、裤、裙、袜、围巾等穿戴类）\n"
        "- pet_supply（宠物窝、猫窝、狗床、宠物垫、猫爬架、猫砂盆、鸟笼、鱼缸等宠物生活用品）\n"
        "- home（家居、家纺、家具、收纳、厨房用品、灯具等家居类）\n"
        "- toy（玩具、积木、玩偶、拼图、模型等）\n"
        "- floral_decor（仿真花、假花、花束、摆件、装饰画、饰品、挂饰等装饰类）\n"
        "- shoe（鞋、靴、拖鞋等鞋类）\n"
        "- bag（包、箱包、背包、行李箱等箱包类）\n"
        "- other（以上都不贴切）\n\n"
        f"商品标题：{title}\n"
        + (f"类目路径线索：{clue}\n" if clue else "")
        + "\n只输出JSON：{\"category\": \"<标签>\"}"
    )
    data = await ask_json(prompt, what="品类识别", stage="category")
    tag = str((data or {}).get("category") or "").strip().lower()
    return tag if tag in _CATEGORY_TAGS else "other"


def _order_dims(dims) -> list:
    """把长宽高按【长 >= 宽 >= 高】降序排好，返回三个字符串。

    平台对尺寸列有硬校验「尺寸长宽高需要满足长≥宽≥高」，不符时每一行都挂红字、
    save 被拒（2026-08-28 真站取证：模型估的 25x20x30 高 30 > 宽 20，六行全红）。

    【为什么排序是正解而不是回喂模型重估】这三个数描述的是同一个盒子，哪条边叫「长」
    纯粹是命名，降序重排不改变申报体积、不影响体积重运费，也不丢信息；而让模型重估
    是拿一次不确定的调用去换一个确定的算术结果。
    非数值原样返回不排（交给下游的 len/量级闸报错，不在这里吞掉异常输入）。
    """
    vals = list(dims or [])
    try:
        nums = [float(str(x).strip()) for x in vals]
    except (TypeError, ValueError):
        return [str(x).strip() for x in vals]
    order = sorted(nums, reverse=True)
    if order == nums:
        return [str(x).strip() for x in vals]
    # 整数就不带小数点（页面尺寸框都是整数 cm），与原来的写法保持一致
    out = [str(int(v)) if float(v).is_integer() else str(v) for v in order]
    logger.info(f"尺寸按平台要求重排为长>=宽>=高：{'x'.join(str(v) for v in vals)}"
                f" → {'x'.join(out)}cm")
    return out


def _check_pack_est(est: dict, need_dims: bool, need_weight: bool) -> list:
    """包装估算量级闸，返回问题列表（空 = 通过）。

    只拦明显离谱的量级，不判断具体数值准不准（那要看实物）：
      - 三边各自 1~150cm，且不能三边全 <= 2（模型偶发返回 1x1x1）
      - 重量 10~30000g
    快递包裹超过 150cm 单边基本不存在，低于 1cm 更不可能，这两头都是模型
    输出异常而非真实取值。
    """
    problems = []
    if need_dims:
        try:
            dims = [float(est[k]) for k in ("长", "宽", "高")]
        except (KeyError, TypeError, ValueError):
            return ["尺寸字段缺失或非数值"]
        for k, v in zip(("长", "宽", "高"), dims):
            if not (1 <= v <= 150):
                problems.append(f"{k}={v}cm 超出 1~150cm 合理范围")
        if all(v <= 2 for v in dims):
            problems.append(f"三边全 <= 2cm（{dims}），不是真实包裹尺度")
    if need_weight:
        try:
            w = float(est["重量"])
        except (KeyError, TypeError, ValueError):
            return problems + ["重量字段缺失或非数值"]
        if not (10 <= w <= 30000):
            problems.append(f"重量={w}g 超出 10~30000g 合理范围")
    return problems


async def estimate_pack(info: dict, need_dims: bool = True,
                        need_weight: bool = True) -> dict:
    """让模型估算包裹尺寸/重量，返回 {"长","宽","高","重量"}（越界已兜底）。

    【为什么从 set_variant 里抽出来】输入只有 title，与店小秘页面无关，可以在
    ② 认领之前就先跑（见 service._run_prewarm）。量级闸与越界重试原样留在这里，
    预热与现场共用同一份判定——另写一套简版必然与这里漂移。

    need_dims / need_weight 说明本次要哪些字段：它们决定提示词措辞与
    _check_pack_est 的校验范围。预热时【一律按超集问】（那时 cat_path 还没有，
    服装类是否走固定尺寸判不了），现场再按实际所需校验一遍：多问的字段不用即可，
    真缺字段才补问一次。
    """
    from app.publish.llm import ask_json

    title = info.get("title", "")
    prompt = (
            f"你是跨境电商打包专家。商品：{title}。\n"
            "请预估单个包裹打包后的"
            + ("尺寸 长x宽x高（cm）" if need_dims else "")
            + ("和" if (need_dims and need_weight) else "")
            + ("重量（g）" if need_weight else "")
            + "。\n"
            # 【包装形式不能写死成快递袋】原提示词固定写「opp袋/快递袋」，而走到
            # 这里的都已是非服装类（服装走 _APPAREL_DIMS 固定值）：玩具/鞋类/家居
            # 用品多数带彩盒或硬壳，按袋装估会把高估成 3~5cm，与实际体积差一截。
            "要求：先判断这个品类的真实包装形式（快递袋、彩盒、纸箱、含注塑外壳等），"
            "再按商品本体的实际体积给出含包装的外尺寸；有刚性包装的不得按压平袋装估。"
            "数值为整数。\n"
            # 【不许写「偏紧凑」】原提示词这么写，是系统性向下偏置：申报尺寸/重量
            # 低报会压低体积重运费，属虚假申报。要的是准，不是小。
            "不要刻意压小或放大，低报运费属虚假申报、高报自己吃亏。\n"
            '只输出严格JSON：{"长":x,"宽":y,"高":z,"重量":w}'
    )
    est = await ask_json(prompt, what="包装尺寸重量估算", stage="variant")
    # 量级闸：原先无任何范围校验，模型返回 {"长":1,"宽":1,"高":1} 会原样填进
    # 申报字段。全自动路线下不转人工，越界就回喂问题重试一次。
    bad = _check_pack_est(est, need_dims=need_dims, need_weight=need_weight)
    if bad:
        logger.warning("包装估算越界，重生成一次：" + "；".join(bad))
        est2 = await ask_json(
            prompt + "\n\n上次输出不合理：" + "；".join(bad)
            + "\n请按真实快递包裹尺度重新给值。",
            what="包装尺寸重量估算(重试)", stage="variant",
        )
        if not _check_pack_est(est2, need_dims=need_dims, need_weight=need_weight):
            est = est2
        else:
            logger.warning("包装估算重试后仍越界，按通用快递包裹常识兜底")
            est = {**est2, **_PACK_FALLBACK}
    return est
