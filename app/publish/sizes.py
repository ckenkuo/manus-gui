"""店小秘发布操作：sizes。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
from app.logger import logger
from app.publish import category, size_rules, variant_colors, variant_dom, workflows
from app.publish.browser import BrowserSession, J


async def fix_sizes(session: BrowserSession, info_path: str,
                    max_rounds: int = 25) -> dict:
    """阶段⑧：尺码勾选修正，使勾选状态与源商品 SKU 一致。

    读 product-info.json 的 skus 键提取源尺码列表，逐个点击复选框直到勾选状态匹配，
    然后等 SKU 表重生成稳定（行数连续两轮不变）。

    不填表格（货号/价格/重量），那些由后续阶段处理。max_rounds 防止死循环。
    """
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    # 源尺码键与页面复选框文本都过 norm_size 再比，避免「110cm建议身高100-110cm」这类
    # 带描述的键匹配不上页面的「110」（见 norm_size 的实测说明）
    normalize_size = workflows.size_normalizer(info)
    rules = workflows.rules_for(info)
    wanted = sorted({normalize_size(k) for k in (info.get("skus") or {})})
    if not wanted:
        # 报错要指回真因：skus 为空的现实成因是【源 spec 不是「颜色>尺码」两维】被
        # pivot_skus 整条丢掉（2026-08-28 offer 1014675972015 手工编织摆件，6 条 spec
        # 全是裸颜色名），而不是源商品真没规格。只说「无 skus 数据」时人会去翻页面，
        # 而该翻的是 raw.json 的 skuMap。故把 raw.json 里的条数一起报出来。
        raw_n = variant_colors._raw_sku_count(info_path)
        extra = (f"（raw.json 有 {raw_n} 条 SKU，说明 spec 不是「颜色>尺码」两维、"
                 f"被 pivot_skus 丢弃，需重跑阶段① 提取）" if raw_n else "（raw.json 也无 SKU）")
        return {"status": "error",
                "reason": f"product-info.json 无 skus 数据{extra}"}

    # 1) 【先校验再动手】源尺码在页面选项里一个都找不到时立刻报错，不许往下走。
    # 2026-08-24 实测（offer 846106032776「均码」× 成人女装英文尺码）：没有这道闸，
    # 下面的循环会把页面原本勾着的尺码逐个取消（它们「不在 wanted 里」），最后
    # 返回 status=ok 只带一句 warning，阶段⑨ 才以「请先选择尺码」暴露——典型的
    # 先破坏再失败。归一函数补别名只能覆盖已知写法，这道闸兜住所有未知写法。
    states0 = await session.eval_json(variant_dom._JS_SIZE_GROUP_STATES)
    if not states0:
        # 空数组有两种成因，处置相反：本类目无尺码维 → skipped；区块没渲染 → error。
        # 判据取结构信号（见 _JS_SIZE_GROUP_PRESENCE 上方的真站取证），不靠「没找到
        # 就当没有」——后者会把类目失效导致的未渲染也放过，让整单带着空变种表走到 save。
        pres = await session.eval_json(variant_dom._JS_SIZE_GROUP_PRESENCE)
        if pres.get("section") and pres.get("checkboxes") and not pres.get("sizeItemCount"):
            logger.info(
                f"本类目无尺码维（变种属性区 {pres.get('checkboxes')} 个复选框全是颜色，"
                f"属性行 {pres.get('labels')}），阶段⑧ 无事可做")
            return {"status": "skipped",
                    "reason": "本类目没有尺码属性行（非服装类目，如仿真花/玩具/饰品），"
                              "无需勾选尺码",
                    "wantedSizes": wanted,
                    "pageLabels": pres.get("labels") or []}
        return {"status": "error",
                "reason": f"未找到尺码复选框组（变种属性区未渲染？）：{pres}"[:300]}
    page_norms = {normalize_size(s["t"]) for s in states0}
    hit = [w for w in wanted if w in page_norms]
    size_mapping = {}  # 字母码→页面尺码映射（映射成功时回填 result，供 service 展示）
    if not hit:
        # 【把「类目选错」与「尺码写法没覆盖」两种成因分开报】2026-08-29 实测
        # （offer 1055568943470）：源是月龄码 6-9m/2-3y，页面全是 Asian S/M/L
        # 成人码——两套体系互斥，这不是归一漏了某种写法，而是阶段③ 把童装判进了
        # 女士牛仔两件套。原文案只列两串尺码，人会去补别名表，而该改的是类目。
        # 故用 size_tier 对两侧各判一次档位，档位相反时直接指认真因与修法。
        src_tier = size_rules.size_tier(list(info.get("skus") or {}))
        page_tier = size_rules.size_tier([s["t"] for s in states0])
        mismatch = bool(src_tier and page_tier and src_tier != page_tier)
        # 【2026-09-09 增】字母码童装：先试 LLM 把字母码映射到页面身高码，映射成功
        # 就直接按映射后的尺码勾选，不再停在报错交人工（见 _map_letter_sizes docstring）。
        if (mismatch and src_tier == "adult"
                and (rules is None or rules.MAP_CHILD_LETTER_SIZES)
                and size_rules._age_is_child(info.get("attributes"))):
            size_mapping = size_rules._map_letter_sizes(info, [s["t"] for s in states0])
            mapped = sorted({normalize_size(v) for v in size_mapping.values() if v})
            # 映射必须覆盖全部源尺码（漏档、或两个字母码撞到同一页面码，都会让 SKU 表
            # 静默少行，比映射失败更隐蔽），且映射值都要落在页面选项里。
            if (mapped and len(mapped) == len(wanted)
                    and all(m in page_norms for m in mapped)):
                wanted = mapped
                hit = [w for w in wanted if w in page_norms]
                logger.info(
                    f"字母码童装尺码映射成功："
                    f"{'、'.join(f'{k}→{v}' for k, v in size_mapping.items())}，"
                    f"按映射后 {wanted} 勾选")
        if not hit:
            # 映射没救回来（或不是字母码童装），走报错
            head = "源尺码在页面选项里全部不存在，未改动任何勾选"
            if mismatch:
                # 真因放在最前面：service 层的 note 只留 200 字符，坠在尾巴上的
                # 结论会被截掉，人看到的仍是两串尺码。
                names = {"baby": "婴幼童/童装码", "adult": "成人码"}
                cur_cat = await category.read_current_category(session)
                if src_tier == "adult" and size_rules._age_is_child(info.get("attributes")):
                    head = ("源尺码在页面选项里全部不存在，未改动任何勾选。【源用字母码表示"
                            "童装码，不是类目选错】源「适合年龄段」明确是童装，但源尺码键用了"
                            "成人字母码（S/M/L/XL），与页面童装类目的身高码/岁码体系不互译，"
                            "自动映射也未成功；应按源尺码表的衣长人工映射到页面身高码"
                            "（如 S→100、M→110）后重跑阶段⑧，不要重跑类目。当前类目："
                            + ((cur_cat or "读不到")[:60]))
                else:
                    head = ("源尺码在页面选项里全部不存在，未改动任何勾选。【真因是类目选错，不是尺码别名】"
                            + "源商品是" + names[src_tier] + "、页面类目是" + names[page_tier]
                            + "，两套尺码体系互斥；应重跑阶段③ 类目并关掉类目缓存。当前类目："
                            + ((cur_cat or "读不到")[:60]))
            return {"status": "error",
                    "reason": f"{head}。源 {wanted} / 页面 {sorted(page_norms)[:12]}",
                    "wantedSizes": wanted,
                    "srcTier": src_tier, "pageTier": page_tier,
                    "catMismatch": mismatch,
                    "pageSizes": [s["t"] for s in states0]}

    # 1.5) 【剔伪变种】源商品会把非可售规格混进颜色/尺码维（尺码维「2XL:尺寸参考选项图」、
    # 颜色维「短袖款式随机」），认领后被带进变种表。它不是可售规格：留在表里会拖出
    # 无换图入口的伪行，⑦b 报「该行没有预览图 trigger」、⑦a 因它不在另一维而反选不到。
    # 故在勾选对齐前先反选，让平台重建变种表，伪行连同 SKC 图位一起消失。
    # 判据交 LLM 做语义判断，不写死词表；best-effort，识别失败就当没有，不拦整单。
    # 详见 _drop_fake_variants docstring。
    dropped_fake = await variant_colors._drop_fake_variants(session, states0, info_path)

    # 2) 勾选修正
    toggles = []
    for _ in range(max_rounds):
        states = await session.eval_json(variant_dom._JS_SIZE_GROUP_STATES)
        if not states:
            return {"status": "error", "reason": "未找到尺码复选框组（不在编辑页？）"}
        # 找第一个状态不符的：wanted 里的应该勾上，不在 wanted 里的应该不勾
        bad = next((s for s in states if (normalize_size(s["t"]) in wanted) != bool(s["c"])), None)
        if not bad:
            break  # 全部正确，退出循环
        # 点击按页面【原始文本】定位（_JS_CLICK_SIZE_CB 是文本匹配，不能传归一值）
        await session.eval_json(variant_dom._JS_CLICK_SIZE_CB.replace("__T__", J(bad["t"])))
        toggles.append(bad["t"])
        # 等变种表重建（原固定 sleep(1.2)）：轮询等行数变化，上限 1.2s
        n_before = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
        for _ in range(12):  # 1.2s / 0.1s
            await asyncio.sleep(0.1)
            n_now = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
            if n_now != n_before:
                break  # 行数变了 = 重建开始了

    # 验证：再读一次，确认全部正确
    states = await session.eval_json(variant_dom._JS_SIZE_GROUP_STATES)
    still_bad = [s["t"] for s in states if (normalize_size(s["t"]) in wanted) != bool(s["c"])]
    if still_bad:
        return {"status": "error", "reason": f"尺码勾选未修正: {still_bad}", "toggled": toggles}
    missing = [w for w in wanted if w not in [normalize_size(s["t"]) for s in states]]

    # 3) 等 SKU 表重生成稳定（行数连续两轮不变说明 Vue 渲染完了）
    last_n, stable = -1, 0
    for _ in range(20):
        n = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
        if n == last_n:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_n = n
        await asyncio.sleep(0.6)

    final_count = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
    result = {"status": "ok", "wantedSizes": wanted, "toggled": toggles,
              "rowCount": final_count}
    if size_mapping:
        # 字母码童装的映射结果单出一个键：它不是源规格名、但勾选是照它来的，
        # 让 service 的 note 与日志能看见「这次把什么字母码映射成了什么身高码」。
        result["sizeMapping"] = size_mapping
    if dropped_fake and (dropped_fake.get("colors") or dropped_fake.get("sizes")):
        # 剔掉的伪变种单出一个键：它不是源规格、不算 missing，但要让调用方与日志
        # 能看到「这次从变种组里拿掉了什么」——它本该消失，不是异常。
        result["droppedFake"] = dropped_fake
    if missing:
        # missing 单出一个键：调用方（service._st_fix_sizes）要据此发 manual_check，
        # 从 warning 字符串里再解析回来既脆又没必要（2026-08-29 起）。
        result["missing"] = missing
        result["warning"] = f"源尺码在页面复选框中不存在: {missing}"
    return result
