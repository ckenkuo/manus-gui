"""店小秘发布操作：attributes.review。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
from app.logger import logger
from app.publish import cache
from app.publish.attributes import dropdowns as attributes_dropdowns, form as attributes_form
from app.publish.browser import BrowserSession
from app.publish.workflows import source_name
from typing import Optional


# 属性审核提示词：七条规则全部来自原 skill 的实战积累，别精简。
# 规则4（只填必填）是「填得多错得多」的直接对策，2026-08-24 起口径收紧为「非必填未填的
# 一律留空、源商品写了也不填」（原先给源里有值的非必填行开的口子已撤，见 dump_attrs）；
# 规则5（成分和=100）对应平台硬校验；
# 规则6（里料纹理）单独点出「一体绒不算单独里衬」——这条错过就会联动出一堆必填行。
_ATTR_PROMPT = """你是跨境电商商品属性审核助手。下面是 {platform} 源商品信息和店小秘 Temu 半托管表单的属性现状。
任务：判断表单每个属性是否与商品实际相符，产出需要修改的清单。
规则：
1. 修改值必须从该行 options 列表中选取，禁止编造；options 为空且没有合适选项时不要改。
2. current 与商品明显矛盾的必须改（这是本任务的核心职责，不属于"拿不准"）：
   例如圆领套头毛衣的"细节"却是"露肩"、一体绒商品的"里料纹理"却是"无里料"——
   这类要直接从 options 中选语义最接近的一项改掉。装饰/图案/细节类以图片理解摘要为准，
   在 options 里找包含关键特征的精确选项（如图示蝴蝶结在胸前→"前蝴蝶结"而非"后蝴蝶结"）。
3. 【保持原值策略】current 已正确或确实拿不准（源信息和图片都无法判断）时：
   - 如果 current 不是 "(请选择)" 等占位符（即页面已有预填值），输出一条 change，
     value 设为 current 的值，reason 注明"保持原值"——这样即使后续写入失败重试，
     也能明确知道要保持这个值，而不是留空。
   - 如果 current 是 "(请选择)" 且确实拿不准，放 notes 说明情况，不输出 change。
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
8. 【数值输入行】kind="number" 的行（如里料克重、含绒量）没有 options，是纯数字输入框：
   value 只给**纯数字**，不要带单位、不要给选项文本（numHint.unit 告诉你单位，numHint.placeholder 可能有取值提示）。
   源商品没写时按该品类常识给行业通行值（如童装梭织里布 60~90 g/m²、摇粒绒 180~260 g/m²），
   这类是客观物性、不构成对买家的承诺，必须填而不能留空——留空平台会拦，整个商品发不出去。

商品标题：{title}
源商品参数（{platform}）：{src_attrs}
源主面料成分（已从源参数/详情文字/详情图解析，百分比以此为准）：{main_composition}
图片理解摘要：{image_understanding}
表单属性现状：{rows}

只输出JSON: {{"changes": [{{"label": "属性名", "value": "选项文本", "num": 数值或null, "row": 行号或null, "reason": "一句话理由"}}], "notes": ["需要人工判断的存疑点"]}}"""


# 单行重选的窄提示词：只给一行的信息，用重读到的真实 options 重新选一个值。
# 规则只从 _ATTR_PROMPT 抽相关的两条（必须从 options 里选、拿不准就不改），
# 并允许 value=null 明确表示「没有合适的，别动这行」。
_ATTR_ROW_PROMPT = """你是跨境电商商品属性审核助手。表单某一行的下拉选项已经变化，需要按最新选项重新选值。

规则：
1. 值必须从下面的 options 列表里原样选取，禁止编造、禁止改写措辞。
2. 没有语义合适的选项时返回 {{"value": null}}——保持原样比填错好。

商品标题：{title}
属性名：{label}
该行当前值：{current}
是否必填：{required}
源商品对应参数值：{src_value}
最新可选项：{options}

只输出JSON: {{"value": "选项文本或null", "reason": "<一句话理由>"}}"""


async def _refresh_row_and_retry(session: BrowserSession, change: dict, row: dict,
                                 cat_path, title: str = "", src_value: str = "",
                                 use_cache: bool = True, site: str = "") -> dict:
    """某行按缓存 options 写入失败时，只重读这一行的真实选项再试一次。

    为什么只重读一行而不整体退回全量遍历：变的是【那一个下拉】，其余行的缓存仍然
    有效；整体回退要重花 90s 去证明另外二十多行没变。

    两条分支的分工：
      - 重读后目标值仍在 options 里 → 缓存没过期，失败是点击/重渲染问题，直接原值
        再试一次，不白花一次 LLM 调用（set_attr 内部已自愈重试过一次，但这里隔了一次
        真实的下拉开合，DOM 状态已刷新）。
      - 目标值确实没了 → 选项集真的变了，用新 options 单独问一次 LLM 重选。

    成分类行（num 不为 None）【不该进这个函数】，由调用方拦住：_read_active_options
    内部调 _open_attr_dropdown 时不传 sel_idx（走默认 0），成分第 2 行根本读不到；
    且单换一根纤维会破坏 _validate_attr_changes 第 4 道闸保证的「同字段合计 100%」，
    平台硬性校验必拦。
    """
    from app.publish.llm import ask_json

    label = change["label"]
    opts, meta = await attributes_dropdowns._read_active_options(session, label, with_meta=True)
    if not opts:
        return {"status": "error", "value": change.get("value"), "options": [],
                "askedLLM": False, "readback": None,
                "reason": "重读选项为空（行隐藏或下拉点不开）"}

    # 【回灌缓存必须在分支之前】不管这次能不能救回来，那份过期数据都得换掉，
    # 否则下一个同类目商品还会撞同一堵墙。这是「写入即校验」策略真正起作用的地方。
    # 但截断的清单不回灌——那会把过期数据换成缺项数据，同样没人能发现。
    if use_cache and cat_path and meta["complete"]:
        cache.update_attr_row(cat_path[-1], cat_path, label, opts, site)

    if change.get("value") in opts:
        r = await attributes_form.set_attr(session, label, change["value"], None, 1)
        return {"status": r.get("status"), "value": change["value"], "options": opts,
                "askedLLM": False, "readback": attributes_form._readback_current(r),
                "reason": "选项未变，原值重试"}

    data = await ask_json(
        _ATTR_ROW_PROMPT.format(
            title=title or "", label=label, current=row.get("current") or "",
            required=bool(row.get("required")), src_value=src_value or "（源未给）",
            options=json.dumps(opts, ensure_ascii=False)),
        what=f"属性单行重选（{label}）", stage="attrs",
    )
    value = data.get("value")
    reason = str(data.get("reason", ""))
    # 模型照样会编造，必须再过一遍 options 闸
    if not value or value not in opts:
        return {"status": "error", "value": value, "options": opts, "askedLLM": True,
                "readback": None,
                "reason": f"LLM 重选的值不在 options 内或为空（{reason}）"}
    r = await attributes_form.set_attr(session, label, value, None, 1)
    return {"status": r.get("status"), "value": value, "options": opts,
            "askedLLM": True, "readback": attributes_form._readback_current(r),
            "reason": reason}


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


async def _ask_attr_review(rows: list, info: dict, main_comp: Optional[dict]) -> dict:
    """问 LLM 要属性修改清单；行多时拆两组并发问，结果合并后返回。

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


async def _apply_attr_changes(session: BrowserSession, changes: list, row_map: dict,
                              info: dict, cat_path, use_cache: bool = True,
                              site: str = "") -> tuple:
    """逐项把校验通过的修改写进表单，返回 (applied, cacheRefreshed, compFailed)。

    从 check_attrs 里摘出来【只为了能跑第二轮】：改「里料纹理」这类字段会联动新增
    必填行（里衬成分、里料克重），那些行在第一轮 dump_attrs 时还不存在，因此第一轮
    的清单里必然没有它们——摘成函数后，补填轮可以原样复用这里的重试与成分保护，
    不必另写一套写入逻辑（另写一套就会与这里的行为漂移）。
    """
    src_attrs = info.get("attributes") or {}
    applied: list = []
    cache_refreshed: list = []
    comp_failed: list = []
    for c in changes:
        # kind 必须透传：数值行（里料克重等）走 input 直填，下拉流程对它无效。
        # 优先用校验层标好的 kind，兜底查表单行的 kind——漏传会让数值行静默走
        # 下拉分支、必然失败，正是本次修复要消除的断点。
        kind = c.get("kind") or (row_map.get(c["label"], {}).get("kind") or "select")
        r = await attributes_form.set_attr(session, c["label"], c["value"],
                           c.get("num"), c.get("row") or 1, kind=kind)
        rec = {"label": c["label"], "value": c["value"],
               "num": c.get("num"), "row": c.get("row"),
               # kind 必须进 rec：下方 comp_rows 统计成分行时靠 a["kind"]!=number 排除
               # 数值行（里料克重等），漏存会让数值行被误当成分行触发裁行（2026-09-08 补）。
               "kind": kind,
               "result": r.get("status"),
               "readback": attributes_form._readback_current(r)}
        # 【触发闸是 optionsFrom == "cache"，不是 use_cache】现场刚读来的选项立刻点不中，
        # 重读大概率还是同一份、救不回来；缓存来的可能隔了好几天，重读才有意义。
        # 这样非缓存路径保持零改动。
        if r.get("status") == "error":
            # live 行写失败原先静默成 result:error，排查点不中（如填充纺织纤维成分）时
            # 看不到是 row-not-found / option-not-rendered / dropdown-too-far 里的哪一种。
            # 缓存行下面会走重读/重试，这里只补 live 行的 reason 日志，不改行为。
            src = row_map.get(c["label"], {}).get("optionsFrom")
            if src != "cache":
                logger.warning(f"{c['label']} 写入失败（{src or '无来源'}行，不重试）: "
                               f"{r.get('reason') or r.get('stage') or r.get('err') or ''}")
        if (r.get("status") == "error"
                and row_map.get(c["label"], {}).get("optionsFrom") == "cache"):
            if c.get("num") is not None:
                # 成分字段写入失败：记录到待重试列表，在本轮所有项写完后整组重试
                # （不能立即重试，因为同一成分字段的其他行可能还没写，整组状态不完整）
                if c["label"] not in comp_failed:
                    comp_failed.append(c["label"])
            else:
                fix = await _refresh_row_and_retry(
                    session, c, row_map[c["label"]], cat_path,
                    title=info.get("title") or "",
                    src_value=str(src_attrs.get(c["label"], "")),
                    use_cache=use_cache, site=site)
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

    # ---- 成分字段整组重试：options 过期导致写入失败的成分字段 ---------------
    # 【为什么要整组重试】成分是一组（合计必须100%），单行重试会破坏总和；且第2、3行
    # 的下拉选项技术上读不到（_read_active_options 不传 sel_idx 默认读第1行）。
    # 正解是重读第1行选项（所有成分行共用同一份纤维列表）→ 用新选项重新校验整组 →
    # 整组重写。只有缓存过期才值得重试（现场刚读的立刻失败，重读也是同一份）。
    if comp_failed and use_cache and cat_path:
        comp_retry_ok = []
        for label in list(set(comp_failed)):  # 去重：同一字段多行都失败时只记录一次
            try:
                # 重读第1行的选项（成分字段所有行共用同一份纤维列表）
                opts, meta = await attributes_dropdowns._read_active_options(session, label, with_meta=True)
                if not opts:
                    logger.warning(f"{label} 整组重试：重读选项为空，跳过")
                    continue
                # 回灌缓存（与 _refresh_row_and_retry 同理，不管能否救回都要换掉过期数据）
                if meta["complete"]:
                    cache.update_attr_row(cat_path[-1], cat_path, label, opts, site)
                # 从 changes 里提取该字段的所有行，用新选项重新校验
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
