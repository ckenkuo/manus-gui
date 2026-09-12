"""店小秘发布操作：attributes.review。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
from app.logger import logger
from app.publish.attributes import dropdowns as attributes_dropdowns, form as attributes_form
from app.publish.browser import BrowserSession
from app.publish.workflows import source_name
from typing import Optional


# 属性审核提示词：七条规则全部来自原 skill 的实战积累，别精简。
# 规则4（只填必填）是「填得多错得多」的直接对策，2026-08-24 起口径收紧为「非必填未填的
# 一律留空、源商品写了也不填」（原先给源里有值的非必填行开的口子已撤，见 dump_attrs）；
# 规则5（成分和=100）对应平台硬校验；
# 规则6（里料纹理）单独点出「一体绒不算单独里衬」——这条错过就会联动出一堆必填行。
#
# 【规则2 的「物性字段以源参数为准」是 2026-09-11 补的】此前规则2 只写了「装饰/图案/
# 细节类以图片理解摘要为准」，而材质类型/织造方式/材料/面料类型这批字段既不是装饰、
# 也不归规则5 的成分，正落在两条规则的缝里，只能由模型每次自行取舍：图片看着是亮光
# 铝膜 → 判「非纺织品材质」，源参数标的是聚酯纤维 → 判「保持纺织品材质」，两次都
# 说得通。而这是【整组口径的源头】，它一翻，材料/面料类型/克重跟着翻——实测同一商品
# 同一输入，一次改 9 项、一次改 2 项（商品 1044382261282，见该次取证的 drift 实验：
# 同输入连调两次仅 1 项分歧，说明漂移不是模型乱，是这个决策点本身没有依据）。
# 补上优先级后这类字段有唯一依据，不再随采样倒向。
# 【改规则1/2 的这条口径时，_ATTR_DEFAULT_PROMPT 规则1 必须同步改】两条路径的判据是
# 对齐的（见 _ATTR_DEFAULT_PROMPT 的注释）：只改一边，快路径会拿图片口径把预填值判成
# 「不正确」、白白落回完整审核，等于每次多花一轮 LLM。
_ATTR_PROMPT = """你是跨境电商商品属性审核助手。下面是 {platform} 源商品信息和店小秘 Temu 半托管表单的属性现状。
任务：判断表单每个属性是否与商品实际相符，产出需要修改的清单。
规则：
1. 修改值必须从该行 options 列表中选取，禁止编造；options 为空且没有合适选项时不要改。
2. current 与商品明显矛盾的必须改（这是本任务的核心职责，不属于"拿不准"）：
   例如圆领套头毛衣的"细节"却是"露肩"、一体绒商品的"里料纹理"却是"无里料"——
   这类要直接从 options 中选语义最接近的一项改掉。装饰/图案/细节类以图片理解摘要为准，
   在 options 里找包含关键特征的精确选项（如图示蝴蝶结在胸前→"前蝴蝶结"而非"后蝴蝶结"）。
   【物性字段与上相反：一律以「源商品参数」为准】材质类型、织造方式、材料、面料类型、
   平方克重这类"商品由什么做的"的字段，先信源商品参数；源里没给该字段时才参考图片
   理解摘要与品类常识。两者不一致时按源参数填，并在 notes 里记一句备查
   （例：源标注聚酯纤维、图片看着像亮光铝膜 → 材质类型仍按"纺织品材质"、材料按涤纶/
   聚酯纤维填，不因图片改判）。不要按图片"看着像什么"去改这类字段：它们是整组口径的
   源头，材质一翻，成分/材料/面料类型/克重会跟着一起翻，代价远大于装饰类字段。
   源参数里没写全时，用表单已有的预填值（那是认领时按源商品映射来的）继续对齐，
   不要因为"拿不准"就换成图片口径。
3. 【保持原值策略】current 已正确或确实拿不准（源信息和图片都无法判断）时：
   - 如果 current 不是 "(请选择)" 等占位符（即页面已有预填值），输出一条 change，
     value 设为 current 的值，reason 注明"保持原值"——这样即使后续写入失败重试，
     也能明确知道要保持这个值，而不是留空。
   - 如果 current 是 "(请选择)" 且确实拿不准，放 notes 说明情况，不输出 change。
   （kind="checkbox" 的多选行例外：current 是顿号连接的多个值，不能整串照抄当 value——
     要保持原值就为其中每个值各输出一条 change，详见规则 9。）
4. 填写范围：只填【必填项】（required=true），且**必填项必须填全**——
   平台对必填项是硬校验，留空会直接卡在保存、整个商品发不出去。
   current 为 "(请选择)" 的必填项一律要给值；options 里实在没有对应项时，
   选语义最接近的一项，不要留空。
   【源头无信息时怎么填】按字段性质分两类：
   (a) 客观物性（里料克重 g/m²、里衬成分、里料纹理、版型、季节、厚薄等）：
       按该品类的面料常识给行业通行值即可，这类不构成对买家的承诺。
   (b) 对买家的功能性承诺（护理说明能否机洗/干洗、安全等级、功效宣称等）：
       源商品没写时选 options 里**最保守**的那一项（如洗涤只选「可手洗」
       而不选「可机洗且可干洗」，拿不准的护理方式不要许诺），
       并在 notes 里记一句便于事后抽查。填错这类要担责且买家可证伪。
   非必填项（required=false）当前未填的一律留空，即使「源商品参数」里给了对应值也不要填
   —— 这是硬规则，不要为了信息完整去补。非必填项【已有值】且与商品明显矛盾时才纠正
   （那是改错，不属于多填）。
5. 成分类属性（上装成分/下装成分/里衬成分等带百分比的字段）：同一字段所有成分行的
   百分比之和必须恰好等于 100（平台硬性校验）。源商品只给主成分含量（如棉90%）时，
   差额按常见配比补足：优先加一行「聚酯纤维(涤纶）」凑到 100%；options 没有聚酯纤维时
   再按属性自洽推导（如面料=微弹→加「氨纶」）。多成分输出多条同 label 的 change，
   用 row 字段（从 1 起）区分第几行：row=1 覆盖当前行，row>=2 是新增成分行。
   上装成分/下装成分等【主面料】字段的第 1 行纤维必须选与「源主面料成分」一致的选项
   （options 里的写法可能带括号注解，选语义相同的那个），其 num 由程序按源含量覆盖，
   你给的数值不作准；里衬/里料/辅料/填充类字段不受源主面料含量约束。
   【源没给含量时】不要自己拆成两行去编比例：程序会按「该纤维 100%」单行写入
   （源连主面料成分都没写时按聚酯纤维 100%）。此时主面料字段只需给第 1 行。
   【源主面料成分是占位词/非纤维时】（main_composition 的 fiber 为空、assumed 会说明，
   如「其它」「牛仔布」这类）：不要照搬占位词，依据「源商品参数」里的面料名称/工艺 +
   图片理解摘要 + 品类常识推断具体纤维（如面料名称=牛仔布 → 牛仔通常棉为主，给出
   「棉 + 聚酯纤维」这类常见配比并合计 100%），reason 注明「源成分无效，按面料/品类推断」。
   【按款式区分的成分】若「源主面料成分」给出了 byVariant（不同颜色/款式成分不同），
   根据表单中已填的颜色/款式，从 byVariant 里匹配对应的成分组。匹配规则：
   - 先看表单「颜色」字段 current 值是否与 byVariant 某个键部分匹配（如表单填「卡通工装衣」，
     byVariant 有「卡通卫衣」，两者都含「卡通」就算匹配）；
   - 匹配不到时用 byVariant 第一组（商家第一个写的往往是主款）；
   - byVariant 为空或整个字段缺失时按 main_composition 的 fiber/percent 处理。
6. 里料纹理/里衬类字段有联动必填：选「光面」「绒面/PU」等会动态新增必填行
   （里衬成分、里料克重），填错风险大。源商品没有明确单独里衬时一律选「无里料/无内衬」。
   特别注意：「一体绒」是绒与面料一体成型，不算单独里衬，必须选「无里料/无内衬」。
   含"针织""毛衣"时织造方式应为针织类。
7. 源商品的"风格"若在 options 中没有，保留当前值。
7b.【按销售站点定的字段：以「发布站点」为准，不看源商品】插头规格、工作电压/额定电压、
   电源频率、语言/语音版本、插座类型这类字段由【目标市场的电气与法规标准】决定，
   与源商品在国内卖什么规格无关——1688/国内源商品几乎必然是中规两插 220V，照抄到
   北美站就是错的（买家插不上、平台合规风险）。按发布站点选 options 里对应的那一项：
   美国/加拿大/墨西哥/哥伦比亚等美洲站 → A 型或 B 型美规插头、110V/120V；
   英国 → 英规三插 230V；欧陆（德法西意荷波等）→ 欧规 C/E/F 型、220V~230V；
   日本 → 日规 A 型 100V；澳新 → 澳规 I 型 230V；沙特/阿联酋等中东 → 英规 G 型 230V。
   站点未给或该站点在 options 里没有对应项时，才退回保持原值并在 notes 里记一句。
   注意这类字段之间要自洽：插头选了美规就不要把电压留在 220V。
8. 【数值输入行】kind="number" 的行（如里料克重、含绒量）没有 options，是纯数字输入框：
   value 只给**纯数字**，不要带单位、不要给选项文本（numHint.unit 告诉你单位，numHint.placeholder 可能有取值提示）。
   源商品没写时按该品类常识给行业通行值（如童装梭织里布 60~90 g/m²、摇粒绒 180~260 g/m²），
   这类是客观物性、不构成对买家的承诺，必须填而不能留空——留空平台会拦，整个商品发不出去。
9. 【多选复选框行】kind="checkbox" 的行（如"颜色"）不是下拉，是一组复选框，可多选。
   options 是全部可选值（可能上百个），current 是【已勾选项用顿号连接】，一项都没勾时
   是 "(请选择)"。产出方式：**一条 change 只给一个值**，要选多个就输出多条同 label 的
   change。给的是【该行的完整目标集合】——写入会把该行重设成你给的这些值（没给的会被
   取消勾选），所以别只补一个新值了事，要把这一行该有的值一次想全。
   取值依据源商品的主色/主材质等：源里有多值时选主要的一到几个，不要全部照搬；
   current 里已有明显矛盾的值（如源商品只有黑白两色、表单却勾着蓝绿）要一并纠正掉。

商品标题：{title}
发布站点（目标销售市场，规则 7b 的唯一依据）：{site}
源商品参数（{platform}）：{src_attrs}
源主面料成分（已从源参数/详情文字/详情图解析，百分比以此为准）：{main_composition}
图片理解摘要：{image_understanding}
表单属性现状：{rows}

只输出JSON: {{"changes": [{{"label": "属性名", "value": "选项文本", "num": 数值或null, "row": 行号或null, "reason": "一句话理由"}}], "notes": ["需要人工判断的存疑点"]}}"""


# 默认属性判断提示词：只判「表单里预填的值对不对」，不产出替代值、也不需要 options。
#
# 【为什么能省时间】判 ok=true 时调用方跳过整个阶段④——一个下拉都不点（读选项正是本
# 阶段耗时主体，33 项要 3-5 分钟，见 dump_attrs）、不跑完整审核、不逐项写入、不跑联动
# 补填轮。代价只有这一次判断，而它的输入没有 options、输出没有 changes 清单，比
# _ATTR_PROMPT 那次调用轻得多。
#
# 【为什么口径是「明显矛盾才算错」，而不是「拿不准就算错」】完整审核的规则 3 本来就
# 规定：current 已有预填值、源信息和图片都判断不了时【保持原值】。若这里反过来把
# 「拿不准」判成不正确，快路径就永远不会命中（表单上一半字段源商品根本没给值），整条
# 路径等于白写。故口径与 _ATTR_PROMPT 规则 2/3 严格对齐：明显矛盾 → 不正确；拿不准
# 但已有值 → 保持。判错方向的代价不对称写在规则 6 里，讲给模型听。
_ATTR_DEFAULT_PROMPT = """你是跨境电商商品属性审核助手。下面是 {platform} 源商品信息和店小秘 Temu 半托管表单里【已经预填好】的属性值。
任务：判断这些预填值是否全都与商品相符。只判断对错，不要给出替代值。
规则：
1. current 与商品实际【明显矛盾】的必须判不正确：例如圆领套头毛衣的"细节"却是"露肩"、一体绒商品的"里料纹理"却是"无里料"。装饰/图案/细节类以图片理解摘要为准。
   但材质类型/织造方式/材料/面料类型/平方克重这类"商品由什么做的"物性字段【以源商品参数为准】：预填值只要与源参数一致就算正确，与图片摘要不一致也【不算矛盾】（源参数说聚酯纤维、图片看着像铝膜时，预填的"纺织品材质""梭织"仍然正确）。
2. 主面料成分（上装成分/下装成分等）行必须与「源主面料成分」一致：纤维种类或比例对不上的，判不正确。
3. 里料纹理/里衬类：源商品没有明确单独里衬时，预填值不是「无里料/无内衬」的判不正确（"一体绒"与面料一体成型，不算单独里衬）。
4. 数值输入行（里料克重等）的值要落在该品类常识范围内，明显离谱的判不正确。
4b.【按销售站点定的字段以「发布站点」为准】插头规格、工作电压/额定电压、电源频率、
   插座类型这类由目标市场电气标准决定的字段，预填值与发布站点不符的判【不正确】——
   预填值是认领时按源商品映射来的，国内源商品几乎必然是中规两插 220V，发到北美站
   就是错的。对照：美洲站（美国/加拿大/墨西哥/哥伦比亚等）A/B 型美规 110V/120V；
   英国英规三插 230V；欧陆欧规 C/E/F 型 220V~230V；日本日规 A 型 100V；
   澳新澳规 I 型 230V；中东英规 G 型 230V。站点未给时这类字段按规则 5 处理。
5. 源信息与图片摘要都判断不了该字段时，按「保持原值」处理，不算不正确（列表里给出的每一行都已有预填值）。
6. 【最重要】判「正确」会让程序跳过整个属性填写阶段、直接拿这些预填值去发布；判「不正确」只是让程序多花一两分钟重新逐项审核。故只要有任何一行与商品实际明显矛盾，就必须判不正确，不要为了省事放过；反过来，也不要把"拿不准但已有值"的行算作不正确，否则这次判断就没有意义了。

商品标题：{title}
发布站点（目标销售市场，规则 4b 的唯一依据）：{site}
源商品参数（{platform}）：{src_attrs}
源主面料成分（已从源参数/详情文字/详情图解析）：{main_composition}
图片理解摘要：{image_understanding}
表单预填属性值：{rows}

只输出JSON: {{"ok": true 或 false, "issues": [{{"label": "属性名", "why": "一句话说明哪里不对"}}], "reason": "<一句话总述>"}}"""


async def _retry_row(session: BrowserSession, change: dict) -> dict:
    """某行写入失败后，原值再试一次。

    【为什么只剩「再试一次」】原先它叫 _refresh_row_and_retry，多做两件事：重读这一行
    的下拉真实选项、把重读结果回灌缓存，值若已不在新选项里就单独问一次 LLM 重选。
    那三件事全是为「缓存里的选项可能过期」而存在的。2026-09-11 起选项直接取自服务端
    接口（见 attributes/server_options），跑批当次现取、不存在过期，于是重读与回灌都
    失去意义；而「重读 DOM 选项」恰恰是那条会读到截断清单、且慢的路，正好一并去掉。

    留下的这一次重试仍有价值：set_attr 内部已自愈重试过一轮，但那次撞的是同一刻的
    游离 DOM；这里隔了前面的开合下拉与若干项写入，页面状态已刷新，能救回偶发的点击
    落空。成分类行（num 不为 None）【不该进这个函数】——单换一根纤维会破坏
    _validate_attr_changes 第 4 道闸保证的「同字段合计 100%」，平台硬性校验必拦。
    """
    label = change["label"]
    r = await attributes_form.set_attr(session, label, change["value"], None, 1)
    return {"status": r.get("status"), "value": change["value"],
            "readback": attributes_form._readback_current(r),
            "reason": "写入失败，原值重试"}


# 属性审核拆组的行数阈值：超过就分两组并发问。
#
# 【为什么要拆】2026-08-24 实测：34 行一次问，推理型模型烧 7523 completion token
# 要 65 秒，而推理量基本随行数走——拆两组并发后墙钟约减半。输入侧不是瓶颈
# （34 行连 options 才 4KB / 5131 input token），所以拆组几乎不增加成本。
#
# 阈值 20 而不是更小：行数少时拆组省下的时间抵不过多一次调用的固定开销
# （建连 + 首 token 延迟），而且组越小模型能看到的上下文越少、判断越容易漂。
_ATTR_SPLIT_MIN_ROWS = 20


def _split_attr_rows(rows: list) -> list:
    """把属性行分成若干组供并发审核，返回 [组1, 组2, ...]；不值得拆时返回 [rows]。

    【成分类字段必须整组落在同一次调用里】_ATTR_PROMPT 规则 5 要求「同一字段所有
    成分行的百分比之和恰好 100」（平台硬性校验），模型得同时看到该字段的全部行才
    算得出来。把「上装成分」的两行劈到两次调用里，两边各自凑 100%，合起来 200%
    必被平台拦。故先把同名字段的行绑成不可分的整体，再按整体分配到组。

    其余行之间没有跨行约束（每行只看自己的 options 与源值），可以任意切分。
    分配用「轮流放入当前较小的组」而不是按下标对半砍：成分字段可能占好几行，
    按下标砍容易切出一个 20 行 + 一个 8 行的组，并发就白拆了。
    """
    if len(rows) < _ATTR_SPLIT_MIN_ROWS:
        return [rows]

    # 同 label 的行绑成一个不可分单元（成分类字段会有多行同名）
    units: dict = {}
    order: list = []
    for r in rows:
        label = r.get("label")
        if label not in units:
            units[label] = []
            order.append(label)
        units[label].append(r)

    groups: list = [[], []]
    # 先放行数多的单元，避免大单元最后进来把两组撑得一边倒
    for label in sorted(order, key=lambda k: -len(units[k])):
        target = min(groups, key=len)
        target.extend(units[label])
    return [g for g in groups if g]


async def _ask_attr_review(rows: list, info: dict, main_comp: Optional[dict],
                           site: str = "") -> dict:
    """问 LLM 要属性修改清单；行多时拆两组并发问，结果合并后返回。

    site 是发布站点（中文站点名，如「哥伦比亚」），规则 7b 要用它——插头规格/工作电压
    这类字段由目标市场的电气标准决定，源商品信息里推不出来。留空则该组字段退回保持原值。

    返回形状与单次调用一致（{"changes": [...], "notes": [...]}），故调用方不必知道
    这里拆没拆——校验（_validate_attr_changes）拿到的仍是完整清单，四道闸照旧
    在全量 attrs 上跑一遍。

    【一组失败就整体失败】不做「用成功的那组凑合」的兜底：属性填一半比不填更糟，
    缺的那些行会静默留空一路带到发布。asyncio.gather 默认就是这个语义（任一异常
    立即上抛），上层 service 会把该阶段标失败、可续跑。
    """
    import json as _json

    from app.publish.llm import ask_json

    def _prompt(part: list) -> str:
        return _ATTR_PROMPT.format(
            platform=source_name(info),
            title=info.get("title"),
            site=site or "（未给出，按规则 7b 保持原值）",
            src_attrs=_json.dumps(info.get("attributes", {}), ensure_ascii=False),
            main_composition=_json.dumps(main_comp or {}, ensure_ascii=False),
            image_understanding=_json.dumps(
                info.get("imageUnderstanding", {}), ensure_ascii=False),
            rows=_json.dumps(part, ensure_ascii=False),
        )

    groups = _split_attr_rows(rows)
    if len(groups) == 1:
        logger.info(f"属性现状 {len(rows)} 行已备齐，调用 LLM 审核（推理模型可能要几分钟）…")
        return await ask_json(_prompt(rows), what="属性审核", stage="attrs")

    sizes = "+".join(str(len(g)) for g in groups)
    logger.info(f"属性现状 {len(rows)} 行已备齐，拆 {len(groups)} 组（{sizes}）并发审核…")
    parts = await asyncio.gather(*(
        ask_json(_prompt(g), what=f"属性审核（第{i}组 {len(g)} 行）", stage="attrs")
        for i, g in enumerate(groups, 1)))

    changes: list = []
    notes: list = []
    for p in parts:
        changes.extend(p.get("changes") or [])
        notes.extend(p.get("notes") or [])
    return {"changes": changes, "notes": notes}


async def _ask_default_attr_review(rows: list, info: dict,
                                   main_comp: Optional[dict], site: str = "") -> dict:
    """问 LLM「表单里这些预填值是否全都与商品相符」，返回 {"ok", "issues", "reason"}。

    site（发布站点）必须与 _ask_attr_review 一起给：规则 4b 与那边的 7b 是对齐的一对。
    只给完整审核那边、这边不给，快路径就会把「中规两插发北美站」这类预填值判成正确、
    整个阶段④ 短路，7b 根本没机会跑。

    与 _ask_attr_review 的两点差别：不喂 options（省掉读选项那一大块）、不要求给替代值
    （输出短、推理少）。拆组的做法照搬——理由与阈值见 _split_attr_rows。

    【拆组后任一组判不正确即整体不正确】不做「用多数组的结论凑合」的兜底，与
    _ask_attr_review 的「一组失败就整体失败」同取向：只要有一行该改而没改，就得落回
    逐项审核——这正是规则 7 讲的代价不对称。

    解析取向同 _pick_cached_category：只有明确 true 才算「相符」，缺字段或给了别的值
    一律当不相符落回逐项审核。
    """
    import json as _json

    from app.publish.llm import ask_json

    def _prompt(part: list) -> str:
        return _ATTR_DEFAULT_PROMPT.format(
            platform=source_name(info),
            title=info.get("title"),
            site=site or "（未给出，这类字段按规则 5 处理）",
            src_attrs=_json.dumps(info.get("attributes", {}), ensure_ascii=False),
            main_composition=_json.dumps(main_comp or {}, ensure_ascii=False),
            image_understanding=_json.dumps(
                info.get("imageUnderstanding", {}), ensure_ascii=False),
            rows=_json.dumps(part, ensure_ascii=False),
        )

    def _is_ok(p: dict) -> bool:
        v = p.get("ok")
        return v is True or (isinstance(v, str) and v.strip().lower() == "true")

    groups = _split_attr_rows(rows)
    if len(groups) == 1:
        logger.info(f"默认属性 {len(rows)} 行已备齐，调用 LLM 判断是否全相符…")
        parts = [await ask_json(_prompt(rows), what="默认属性判断", stage="attrs")]
    else:
        sizes = "+".join(str(len(g)) for g in groups)
        logger.info(f"默认属性 {len(rows)} 行拆 {len(groups)} 组（{sizes}）并发判断…")
        parts = await asyncio.gather(*(
            ask_json(_prompt(g), what=f"默认属性判断（第{i}组 {len(g)} 行）", stage="attrs")
            for i, g in enumerate(groups, 1)))

    issues: list = []
    reasons: list = []
    for p in parts:
        if isinstance(p.get("issues"), list):
            issues.extend(p["issues"])
        if p.get("reason"):
            reasons.append(str(p["reason"]))
    return {"ok": all(_is_ok(p) for p in parts), "issues": issues,
            "reason": "；".join(reasons)}


async def _apply_attr_changes(session: BrowserSession, changes: list, row_map: dict,
                              info: dict) -> tuple:
    """逐项把校验通过的修改写进表单，返回 (applied, cacheRefreshed, compFailed)。

    从 check_attrs 里摘出来【只为了能跑第二轮】：改「里料纹理」这类字段会联动新增
    必填行（里衬成分、里料克重），那些行在第一轮 dump_attrs 时还不存在，因此第一轮
    的清单里必然没有它们——摘成函数后，补填轮可以原样复用这里的重试与成分保护，
    不必另写一套写入逻辑（另写一套就会与这里的行为漂移）。

    原先的 cat_path / use_cache / site 三个参数服务于「缓存选项过期 → 重读该行 →
    回灌缓存」那条路，选项改服务端后整条路不成立（见 _retry_row），随之删除。
    """
    applied: list = []
    applied: list = []
    cache_refreshed: list = []
    comp_failed: list = []
    # 【复选框组先按 label 归并】它的语义是「把该行设成这个集合」，而 changes 是一条
    # change 一个值。逐条写只能做成增量（每跑一次多勾几项，续跑越积越错），故先收成
    # {label: [值…]} 再整组一次性重设，见 attributes_form.set_attr_checkbox。
    cb_groups: dict = {}
    for c in changes:
        kind = c.get("kind") or (row_map.get(c["label"], {}).get("kind") or "select")
        if kind == "checkbox":
            cb_groups.setdefault(c["label"], []).append(c["value"])
    for c in changes:
        # kind 必须透传：数值行（里料克重等）走 input 直填，下拉流程对它无效。
        # 优先用校验层标好的 kind，兜底查表单行的 kind——漏传会让数值行静默走
        # 下拉分支、必然失败，正是本次修复要消除的断点。
        kind = c.get("kind") or (row_map.get(c["label"], {}).get("kind") or "select")
        if kind == "checkbox":
            continue                      # 归并到下方整组重设，不在这里逐条写
        r = await attributes_form.set_attr(session, c["label"], c["value"],
                           c.get("num"), c.get("row") or 1, kind=kind)
        rec = {"label": c["label"], "value": c["value"],
               "num": c.get("num"), "row": c.get("row"),
               # kind 必须进 rec：下方 comp_rows 统计成分行时靠 a["kind"]!=number 排除
               # 数值行（里料克重等），漏存会让数值行被误当成分行触发裁行（2026-09-08 补）。
               "kind": kind,
               "result": r.get("status"),
               "readback": attributes_form._readback_current(r)}
        # 【写失败一律补一次原值重试，不再看选项来源】原先的触发闸是 optionsFrom == "cache"
        # （只有「缓存来的、可能过期」的行才值得重读），选项改服务端后没有过期一说，
        # 那条闸永远不成立、等于把重试整个关掉。现在按「失败就再试一次」办：
        # 重试本身廉价，而它的价值（隔开一次开合下拉、绕开点击落在游离 DOM 上）与
        # 选项来自哪里无关。
        if r.get("status") == "error":
            # 写失败的 reason 要记下来：row-not-found / option-not-rendered / no-panel
            # 各不相同，不记就只剩一个 result:error，排查时看不出卡在哪一步。
            logger.warning(f"{c['label']} 写入失败，原值重试一次: "
                           f"{r.get('reason') or r.get('stage') or r.get('err') or ''}")
            if c.get("num") is not None:
                # 成分字段写入失败：记录到待重试列表，在本轮所有项写完后整组重试
                # （不能立即重试，因为同一成分字段的其他行可能还没写，整组状态不完整）
                if c["label"] not in comp_failed:
                    comp_failed.append(c["label"])
            elif kind != "number":
                # 【数值行不进重试】_retry_row 里的 set_attr 不传 kind，数值行进去会被
                # 按下拉流程重试（卷重这类行根本没有下拉）——必然失败还白花一次开合。
                # 它的失败就如实记成 error。
                fix = await _retry_row(session, c)
                rec["refresh"] = fix
                cache_refreshed.append(c["label"])
                if fix.get("status") == "ok":
                    rec["result"] = "ok"
                    rec["value"] = fix.get("value")
                    rec["readback"] = fix.get("readback")
        applied.append(rec)
        # 项间等待：原先无条件 1s。它防的是「动态增删行重渲染期间立刻动下一项会点击
        # 落空」——而增删行只发生在成分类字段（走 _ensure_comp_rows 加行，即带 num 的
        # 那些项）。普通下拉行不改变行集，不需要这段等待，且下一项的
        # _open_attr_dropdown 本身就是幂等打开 + 二次重试。故按项分流：
        # 成分项保留 1s，普通项 0.15s。实测每单写 6-13 项，多数是普通项。
        await asyncio.sleep(1.0 if c.get("num") is not None else 0.15)

    # ---- 复选框组：整组一次性重设 ------------------------------------------
    # 放在主循环之后：上面那几段（成分裁行、整组重试）都按 num 分流，复选框行的 num
    # 恒为 None，不会被它们碰到；而 set_attr_checkbox 的一次调用就是一个 rec，与
    # 「一条 change 一个 rec」的既有形状一致，note 里的「改 X/Y 项」照常统计。
    for label, values in cb_groups.items():
        r = await attributes_form.set_attr_checkbox(session, label, values)
        applied.append({"label": label, "value": values, "num": None, "row": None,
                        "kind": "checkbox", "result": r.get("status"),
                        "readback": attributes_form._readback_current(r)})
        if r.get("status") != "ok":
            # best-effort：写失败不影响其它项已写好的值，如实记进 applied 交末尾复扫
            # 与阶段④ 的 fail 判定（必填仍空会照常报出来），不在这里抛。
            logger.warning(f"{label} 复选框组重设为 {values} 失败：{r.get('reason') or r}")
        await asyncio.sleep(0.15)

    # ---- 成分类字段收尾：把多余的旧行裁掉 ----------------------------------
    # 【为什么必须有这一步】写入只覆盖前 N 行，而页面初始行数由认领时搬来的源数据
    # 决定，可能【多于】本轮要写的行数；_ensure_comp_rows 又只加不减，多出来的旧行
    # 就带着旧纤维留在表单里。它与新写的某一行撞同一根纤维时，平台按 attrValueId
    # 判重报「XX成分不能重复选择」，保存整单卡死（2026-08-30 实测复现，取证见
    # _trim_comp_rows 的 docstring）。
    #
    # 【2026-09-03 修正】目标行数必须只计算写入成功（result == "ok"）的成分行，而不是
    # changes 里的所有成分行：如果某个成分字段的所有行都写失败（options 过期等原因），
    # 页面实际还是旧值（比如 3 行），但按失败的 change 算出 keep=2 去裁，会错误裁掉
    # 实际有效的旧行，反而可能制造重复（旧行部分残留 + 下次续跑写入新值 = 新旧混杂）。
    # 只有写入成功的字段才能确定「页面现在就是我们要的 N 行」，才能安全裁到 N 行。
    # 写入失败的字段保持原样不裁，交 comp_failed 报人工——这才是真正的 best-effort。
    comp_rows: dict = {}
    comp_written: dict = {}  # 该字段在 applied 里的成分行总数（含写失败的）
    for a in applied:
        # 只统计成分行（num 非空且非数值行）
        if a.get("num") is None or a.get("kind") == "number":
            continue
        label = a["label"]
        comp_written[label] = comp_written.get(label, 0) + 1
        if a.get("result") == "ok":
            comp_rows[label] = max(comp_rows.get(label, 1), int(a.get("row") or 1))
    # 【2026-09-09 修】成分字段是「合计必须 100%」的一组，只要组内任一行写失败，
    # 该组就是残缺的（如第 1 行 95% 成功、第 2 行 5% 失败 → 裁到 1 行只剩 95% ≠ 100%，
    # 保存被平台拒「单个材料属性的百分比之和需等于100」，2026-09-08 商品 1071043490490
    # 实测）。故「成功行数 < 应写行数」的字段整组跳过裁行，保持原样交 comp_failed
    # 整组重试 / 末尾复扫报人工，绝不让残缺百分比静默带进 save。
    for label in list(comp_rows):
        if comp_rows.get(label, 0) < comp_written.get(label, 0):
            logger.warning(
                f"{label} 部分成分行写入失败（成功 {comp_rows[label]}/应写 "
                f"{comp_written[label]} 行），跳过裁行，整组交人工核对")
            comp_rows.pop(label, None)
    for label, keep in comp_rows.items():
        # best-effort：裁不动只记日志，绝不影响已写好的值（辅助路径的一贯取向）
        try:
            t = await attributes_form._trim_comp_rows(session, label, keep)
        except Exception as e:
            logger.warning(f"{label} 多余成分行裁剪异常（忽略）：{e}")
            continue
        if t.get("err"):
            logger.warning(f"{label} 多余成分行裁剪未执行：{t['err']}")
        elif t.get("trimmed"):
            logger.info(f"{label} 裁掉 {t['trimmed']} 个多余成分行"
                        f"（{t['before']} → {t['after']} 行，避免平台判「重复选择」）")

    # ---- 成分字段整组重试：写完发现整组没落上的成分字段 ---------------------
    # 【为什么要整组重试】成分是一组（合计必须100%），单行重试会破坏总和，故必须整组重写。
    # 【为什么不再重读选项】原先这里要重读第 1 行的下拉选项、回灌缓存后再校验，理由是
    # 「缓存里的选项可能过期」。2026-09-11 起选项直接取自服务端接口（见
    # attributes/server_options），跑批当次现取、不存在过期，重读与回灌都失去意义；
    # 校验改用该行已有的 options（就是本轮的权威清单）。
    if comp_failed:
        comp_retry_ok = []
        for label in list(set(comp_failed)):  # 去重：同一字段多行都失败时只记录一次
            try:
                opts = row_map.get(label, {}).get("options") or []
                if not opts:
                    logger.warning(f"{label} 整组重试：该行没有选项（服务端清单里没有），跳过")
                    continue
                # 从 changes 里提取该字段的所有行，用已有选项重新校验
                comp_changes = [c for c in changes if c["label"] == label]
                if not comp_changes:
                    continue
                # 重新校验：只校验这一组，复用 _validate_attr_changes 的成分校验逻辑
                # 构造最小 attrs 和 opt_map 给校验函数
                mini_attrs = [{"label": label, "options": opts,
                              "hasPercent": row_map.get(label, {}).get("hasPercent")}]
                mini_opt_map = {label: opts}
                validated, rejected_retry = [], []
                for c in comp_changes:
                    if c.get("value") in opts:
                        validated.append(c)
                    else:
                        rejected_retry.append(c)
                # 如果有行的值不在新选项里，整组放弃（需要重新问 LLM，不在本函数范围）
                if rejected_retry:
                    logger.warning(f"{label} 整组重试：{len(rejected_retry)} 行值不在新选项内，放弃")
                    continue
                # 重新校验合计100%（复用核心逻辑）
                total = sum(c.get("num") or 0 for c in validated)
                if total != 100:
                    logger.warning(f"{label} 整组重试：合计 {total}% ≠ 100%，放弃")
                    continue
                # 整组重写
                logger.info(f"{label} 整组重试：选项已更新（{len(opts)} 项），重写 {len(validated)} 行")
                retry_ok = True
                for c in validated:
                    kind = c.get("kind") or "select"
                    r = await attributes_form.set_attr(session, label, c["value"],
                                      c.get("num"), c.get("row") or 1, kind=kind)
                    if r.get("status") != "ok":
                        retry_ok = False
                        logger.warning(f"{label} 行{c.get('row')} 重写仍失败：{r.get('reason')}")
                        break
                    await asyncio.sleep(1.0)  # 成分行间等待
                if retry_ok:
                    comp_retry_ok.append(label)
                    logger.info(f"{label} 整组重试成功，从 comp_failed 移除")
                    # 更新 applied 里的记录为成功
                    for a in applied:
                        if a["label"] == label:
                            a["result"] = "ok"
                            a["retried"] = True
            except Exception as e:
                logger.warning(f"{label} 整组重试异常（忽略）：{e}")
        # 从 comp_failed 移除重试成功的
        comp_failed = [lbl for lbl in comp_failed if lbl not in comp_retry_ok]
        if comp_retry_ok:
            cache_refreshed.extend(comp_retry_ok)

    return applied, cache_refreshed, comp_failed
