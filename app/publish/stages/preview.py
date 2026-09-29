"""店小秘发布共用能力：stages.preview。各来源流程由 workflows/ 独立定义。"""

import json
import asyncio
import os
import shutil
from typing import Optional
from app.logger import logger
from app.publish import claims, extract, images, variant_colors, vision, size_rules
from app.publish.browser import BrowserSession
from app.publish.media.preview import PREVIEW_MIN_SIDE, sku_preview_replace_row, sku_preview_state


def _color_key(s: str) -> str:
    """颜色比对键：先过颜色维判伪的归一口径（剥空白/连字符/标点、转小写），再剥
    Unicode So 装饰符。两侧形态天然不对称——colorImages 的键是 1688 颜色选择器名
    （源形态，卖家会把标记拼进去，2026-09-28 实测 `莓红✦★`），而店小秘页面列值
    通常渲染成干净名；不归一，「商家给没给这个颜色配图」的闸会在符号差上整维失配。
    """
    return size_rules._strip_decoration(variant_colors._norm_color_name(s))


def _pick_fill_source(row: dict, rows: list, color_files: dict,
                      workdir: str, prep: str, notes: dict = None) -> str:
    """给一个空预览格找源图，返回本地文件路径（找不到返回空串）。

    取源优先级（越靠前越贴近「这一行本该有的那张图」）：
      1. 同颜色的源图：colorImages[本行颜色].mainFile，阶段① 已下载到本地；
      2. 同颜色其它行页面上已挂的图：同一个颜色的另一行（多尺码商品里同色行共用
         一张预览图，那张就是本行缺的）；
      3. 【2026-09-12 新增】任意一行页面上已挂的图：单维类目（车贴/桌布/型号维）里
         每行就是一个独立规格，第 2 条按颜色找必然落空，而「同一款商品的另一张
         预览图」仍比空图位强——空图位是平台硬拒（「请上传预览图」），挂上同款别的
         图只是辨识度差一点。这一条也是「同一款有一样的 SKC 就用同一张图」的落地：
         同款各行的图本就同源，取哪一张都不算挂错款。

    【取源按「本行颜色」而不是行标识】行标识是变种表第一维，可能是尺码/型号维——
    拿它查 colorImages 的颜色键必落空（2026-09-28 offer 1067355258988 宠物保暖打底衫
    实况：第一维是尺码，S/XL 两行空位因此全部只能退化到第 3 条挂错色图、还先被
    known_colors 闸全部反选）。本行的真实颜色在 row.colorDim（JS 按「颜色」表头列取的，
    与第一维列不同）；单维类目/旧数据取不到 colorDim 时退回行标识，维持原行为。

    notes 是 complianceNotes.files 的 {文件名: 标注}，只用来把【永久不可用】的主图从
    第 3 条兜底里摘掉（审核拒收、清不干净的图，见 vision.is_unusable）。传空就是原来的
    行为：这一条兜底刻意不看 clean/chinese，因为它取的是【已经过 ⑤b 清理的本地主图】，
    而空图位是平台硬拒项，挂同款任意一张也比空着好（见上面第 3 条的理由）。unusable 是
    唯一的例外——那张图 ⑤b 压根没能清，挂上去就是把带中文的图发上真店。
    """
    i = row["i"]
    # 颜色维值优先：行标识可能是尺码/型号维，见 docstring「取源按本行颜色」；
    # color_files 的键是归一后的源侧名（见调用方构造），查找同样要过 _color_key，
    # 否则页面干净名（`杏白色`）对不上源侧带标记键（`杏白色✦★`）。
    color = row.get("colorDim") or row.get("color") or ""
    mf = color_files.get(_color_key(color))
    if mf:
        p = os.path.join(workdir, mf)
        if os.path.exists(p):
            return p
    # 页面已挂的图：先找同颜色行，再退到任意非空行（同行颜色维值同样优先）
    peers = [x for x in rows if x.get("url") and not x.get("empty")]
    peer = next((x for x in peers
                 if (x.get("colorDim") or x.get("color")) == color), None)
    if peer is None and peers:
        peer = peers[0]
        logger.info(f"预览图第 {i + 1} 行「{color}」没有同规格源图，"
                    f"退用同款第 {peer['i'] + 1} 行「{peer.get('color')}」的图"
                    f"（空图位会被平台硬拒，同款图只是辨识度差一点）")
    if peer is None:
        return ""
    # 预览图兜底也必须复用已通过清理阶段的本地主图，不能直接回源下载未英化图片。
    local_mains = sorted(
        os.path.join(workdir, name) for name in os.listdir(workdir)
        if name.lower().startswith("main-")
        and name.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        and not vision.is_unusable((notes or {}).get(name) or {})
    )
    if local_mains:
        return local_mains[0]
    raw = os.path.join(prep, f"fill{i:02d}-raw.jpg")
    return raw if extract._download_image(peer["url"], raw) else ""


class PreviewImageCleanError(RuntimeError):
    """⑦b 回源预览图英化链路失败的专用异常（区别于「生成图质检未过」）。

    【为什么要分两类】_clean_downloaded_preview 的 3 次尝试可能栽在两种完全不同
    的环节：出图链路异常（API 错误、结果图下载失败如 curl rc=35、配置中心断连）
    与生成图质检不过。原先两者都返回空串，调用方一律报「英化质检未通过」——出图
    失败被讲成图片问题，正是阶段失败文案按环节分类要消灭的语义错误
    （2026-09-28 1067355258988 实录：shyfai 结果 CDN TLS 握手失败被报成
    「英化质检未通过（…均为否）」，排查方向被带偏）。约定：本异常表示前者，
    空串仍表示后者（质检真实未过）。
    """


async def _clean_downloaded_preview(path: str, prep: str, issues: str = "") -> str:
    """回源下载的预览图必须先英化并通过质检。

    issues 是调用方已经拿到的质检结论（语言关那边会先判一次脏），非空时并进首发提示词：
    与 ⑤c 的 _english_one 同源做法——把「这张图具体哪里不行」喂回去比原样重发命中率高，
    省一发白烧的生图。传空（bad 行的几何通道没有质检结论可用）就是原来的行为。
    """
    if not path or "-raw" not in os.path.basename(path):
        return path
    output = os.path.join(prep, os.path.splitext(os.path.basename(path))[0] + "-clean.png")
    # 营销标语的处置与中文相反（不翻译、直接抹掉），故要显式带上 CLAIM_REMOVE_RULE：
    # 只说「中文翻译成英文」时，纯英文的 BEST-SELLER 角标既不在翻译范围、也不在移除
    # 范围，模型会原样留下，而 check_cleaned 的 marketingClaim 那关必然判不过。
    prompt = ("将图片中的所有中文文字翻译成自然英文并原位替换，保留商品主体、构图和颜色；"
              "移除水印、店铺名和第三方 logo。" + claims.CLAIM_REMOVE_RULE
              + claims.BANNED_REMOVE_RULE + claims.MARK_REMOVE_RULE)
    if issues:
        prompt += f" 本张图已知的问题：{str(issues)[:200]}。请一并修正。"
    # last_exc 记录最近一次的出图异常：3 次尝试耗尽时按「最后栽在哪」分类——
    # 最后一步是出图异常就抛 PreviewImageCleanError，是质检未过就返回空串，
    # 调用方据此给不同环节的错误文案（见 PreviewImageCleanError docstring）。
    last_exc: Optional[Exception] = None
    for _ in range(3):
        try:
            # 超时取出图链路统一值（原先写死 90，盖不住服务端并发时的 30~79s，
            # 会在【已出图并计费】之后才被本地掐断，见 images.EDIT_TIMEOUT）；
            # 并发也过那一层的全局闸门，不由本阶段自己限流
            edited = await images.edit_image_async(
                path, prompt=prompt, out_path=output,
                no_downscale=True, timeout=images.EDIT_TIMEOUT)
            last_exc = None
            qc = await vision.check_cleaned(edited["output"])
            if qc.get("clean"):
                return edited["output"]
            # 加码话术按上一发的实际问题分类（同 cleaning_rules._retry_hint 的取向）：
            # 对营销标语说「清除中文」是无效的，它压根没有中文。2026-09-25 起类别变多，
            # 按判罚最直接的取第一条命中的（夸大宣传与禁词是实罚项、品牌标识是侵权、
            # 材质说明是与属性打架），都没命中的才是老口径的中文/乱码。
            if qc.get("marketingClaim"):
                prompt += (" 上一版仍有夸大宣传文案，请把那些标语连同背景一起彻底抹除、"
                           "按周围画面补全。")
            elif qc.get("bannedTerm"):
                prompt += (" 上一版仍有平台禁词或环保声明（安抚、PP棉、eco-friendly、"
                           "sustainable 这类），请连同所在整句文案一起彻底抹除、"
                           "按周围画面补全。")
            elif qc.get("brandMark") or qc.get("materialText"):
                prompt += (" 上一版仍有品牌标识或材质成分说明（商标、实物上的品牌小标签、"
                           "「棉」「100% Cotton」这类），请把它们连同标签一起彻底抹除、"
                           "按周围画面补全。")
            else:
                prompt += " 上一版仍有中文或乱码，请彻底清除所有中文字符并保持商品不变。"
        except Exception as e:
            last_exc = e
            continue
    if last_exc is not None:
        raise PreviewImageCleanError(
            f"英化出图链路 3 次尝试未成功，最后一次栽在出图异常"
            f"（{type(last_exc).__name__}: {str(last_exc)[:120]}）") from last_exc
    return ""


async def _drop_unfixable_rows(session: BrowserSession, emit, rows: list) -> dict:
    """把补不上预览图的行所属规格反选掉，返回 {"dropped": [...], "kept": [...]}。

    【为什么是反选而不是报人工】空预览图是平台硬拒项（save 报「请上传预览图」），
    只要有一行补不上，整单就发不出去。而这些行往往本就不该发：认领没带图的规格多是
    源站那边的占位/配件规格（与 ⑦a 剔配件色同一类问题，只是判据不同——⑦a 看「有没有
    源数据」，这里看「有没有图」）。反选掉它，平台重建变种表时这一行连同图位一起消失，
    其余规格照常发布，比整单卡死好。

    【绝不反选到「只剩它自己」】某维只剩一个已勾选项时不能再反选——变种维不能为空，
    平台会把整张变种表清掉。这种情况如实报人工。

    【这一步必须是动变种勾选的最后一步】调用它的 ⑦b 现在排在 ⑧ 尺码勾选【之后】（见
    workflows/alibaba1688.build_stages 的取证）：⑧ 的职责是「勾选状态与源 SKU 一致」，
    它会把这里刚反选掉的规格原封不动勾回来，勾回来那行又是空图位，整单照样卡在 ⑭。
    2026-09-28 宠物保暖打底衫（offer 1067355258988）那单变种表第一维是尺码，反选掉的
    正是尺码 S/XL，⑧ 一勾回来反选等于白做。⑨⑩a⑩⑪ 也都在 ⑦b 之后，故它们按行填数时
    拿到的是反选后的最终行集——变种表行数中途一变，按行回填的价/重/货号就全对不上。

    反选完等变种表稳定（走 variant_colors 的共用件），调用方据此重读页面。
    """
    names, seen = [], set()
    for r in rows:
        c = (r.get("color") or "").strip()
        if c and c not in seen:
            seen.add(c)
            names.append(c)
    if not names:
        return {"dropped": [], "kept": [f"第 {r['i'] + 1} 行" for r in rows]}
    dropped, kept = [], []
    for name in names:
        r = await variant_colors.uncheck_variant_option(
            session, name, why="预览图缺失且找不到可用源图，不发这个规格")
        if r.get("status") == "ok":
            dropped.append(name)
            await emit({"type": "log", "stage": "sku_preview",
                        "message": f"规格「{name}」预览图补不上，已反选不发它"
                                   "（留着会让整单被平台拒「请上传预览图」）"})
        else:
            kept.append(name)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"规格「{name}」预览图为空、也没能反选掉"
                                   f"（{r.get('reason')}），保存会报「请上传预览图」，"
                                   "需人工补图或手动取消勾选"})
    if dropped:
        await variant_colors.wait_variant_table_stable(session)
    return {"dropped": dropped, "kept": kept}


async def _st_sku_preview(ctx: dict, session: BrowserSession, emit) -> dict:
    """阶段⑦b SKU 预览图：把变种信息表每行不合规的预览图就地合规化后换回。

    【为什么单独成一个阶段，而不并进 ⑦】两者的容器与语义都不同：⑦ 是
    #skuAttrsInfo（变种属性区）每颜色 3~10 张的展示图，⑦b 是 #skuDataInfo
    （变种信息表）第一列每 SKU 一张的预览图。玩具类那单证实了两者正交——
    ⑦ 因该类目无图位而正确 skipped，⑦b 却被平台拒（详见 pipeline 里
    「阶段⑦b SKU 预览图」段落开头的取证记录）。

    【本阶段排在 ⑧ 尺码勾选之后，不是排错了】原先它在 ⑦ 之后、⑧ 之前，结果是：它反选
    掉的规格（预览图补不上、发出去必被平台拒）被 ⑧ 按「勾选状态与源 SKU 一致」重新勾
    回来，反选等于没做（2026-09-28 offer 1067355258988 实况）。挪到 ⑧ 之后，反选才是
    「预览图缺失的规格不发」的最后一步；顺带 ⑧ 重建变种表之后才换图，这次换上的图不再
    被下一次重建冲掉。阶段 id（sku_preview）与显示名（⑦b）都保持不变——续跑状态文件、
    各处注释与记忆库都按这个 id 认它。详见 workflows/alibaba1688.build_stages。

    【不重判归属，只补几何】认领时店小秘已按 SKU 把每行图带过来了，归属本来就对。
    这里下载该行现有的图、square_image 成 1:1 后原位换回：画面一张不换、行序一动
    不动。与 _skc_size_fallback 同一取向，零 LLM 调用。

    只在【纯增益】方向动手：读不到尺寸、行本来就达标、下载或合规化失败，一律保持
    原样并报人工确认，绝不把行搞成空的。
    """
    st = await sku_preview_state(session)
    for attempt in range(6):
        pending = [row for row in st.get("rows", []) if not row.get("empty")
                   and not (row.get("w") and row.get("h"))]
        if not pending:
            break
        await asyncio.sleep(0.5)
        st = await sku_preview_state(session)
    if st.get("supported") is False and not st.get("rows"):
        await emit({"type": "log", "stage": "sku_preview",
                    "message": "变种信息表没有预览图可换（无行内 trigger），跳过"})
        return {"status": "skipped", "note": "本类目变种表无预览图入口"}
    if st.get("supported") is None:
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"变种信息表预览图列读不到（{st.get('err') or '零行'}）"})
        return {"status": "skipped" if st.get("err") == "no-preview-column" else "fail",
                "note": st.get("err") or "变种表未渲染，请等待加载后重试"}

    rows = st.get("rows") or []
    prev_idx, color_idx = st.get("previewIdx"), st.get("colorIdx")
    bad = [r for r in rows if r.get("bad") and r.get("url")]
    # 【bad 行按有无换图入口分流】变种表只有颜色主行有 trigger（换图入口），其余行
    # 共享主图、无独立入口。无 trigger 的行 sku_preview_replace_row 报「该行没有
    # 预览图 trigger」，无法自动换图，单独记 manual_check 交人工核对、不判 fail
    # （2026-09-06 两单宠物窝 0/19、0/6 全卡在这里，整单被这一处拖死）。
    bad_trigger = [r for r in bad if r.get("hasTrigger") or r.get("hasFillSlot")]
    bad_inherited = [r for r in bad if not (r.get("hasTrigger") or r.get("hasFillSlot"))]
    # 空图位（本行有换图入口却一张图都没有）：平台会拒「请上传预览图」，是确定性的
    # 不合格。这里【没有源图可下载合规化】——认领本该把每行的图带过来，没带过来时
    # 本阶段无从凭空造图，故只能如实判 fail 让人处理，绝不能算进「均已满足」。
    # 2026-09-01 两单（1067271196776、1051827161006）就是被静默放过后，
    # 到 ⑭ 才以「保存可能未生效」暴露，排查方向被带偏。
    empty = [r for r in rows if r.get("empty")]
    unknown = [r for r in rows
               if not r.get("bad") and not r.get("empty")
               and not (r.get("w") and r.get("h"))]
    if unknown:
        # 尺寸未知的行不动，但要说出来：保存被拦时能立刻想到这里（同 _skc_size_fallback）
        await emit({"type": "manual_check", "stage": "sku_preview",
                    "message": f"{len(unknown)} 行预览图读不到尺寸（图未加载完），"
                               "未做合规化，若发布报预览图尺寸请人工确认"})
    # 【空位补图】空图位（认领没带图 / 平台重建丢图）不再是「只能人工」：空格点击
    # 就能出「空间图片」菜单（2026-09-08 真站取证，见 pipeline 的 FILL_SPACE），
    # 有源图就能自动补上。源图按行的颜色从 colorImages 取 mainFile（本地已下载），
    # 取不到回退到同规格其它非空行、再退到同款任意行的 url 下载——空位缺的正是
    # 「该行应有的一张图」（offer 1011303528447 狗裙子 XL 行即此），取源细则见
    # _pick_fill_source。
    #
    # 【补不上就反选该规格，不再停在「需人工补」】2026-09-12 定：空预览图是平台硬拒项，
    # 一行补不上整单就发不出去，而人工往往也没有图可补（认领没带图的多是源站的占位/
    # 配件规格）。故取不到源图、合规化失败、补图交互失败、以及压根没有补图入口的空行，
    # 统统进 unfixable 交 _drop_unfixable_rows 反选——反选后平台重建变种表，这一行连同
    # 图位一起消失，其余规格照常发布。
    fillable = [r for r in empty if r.get("hasFillSlot")]
    # 空行且无补图入口：自动补不了，直接进反选集
    unfixable = [r for r in empty if not r.get("hasFillSlot")]

    # 【已有图的行（非空、非 bad）也要走语言关，见下面那一段】它们同样要下载原图判脏，
    # 故工作目录的创建条件里也要算上。
    with_image = [r for r in rows if r.get("url") and not r.get("empty") and not r.get("bad")]
    prep = os.path.join(ctx["workdir"], "sku-preview")
    need_work = bool(fillable or bad_trigger or with_image)
    if need_work:
        shutil.rmtree(prep, ignore_errors=True)
        os.makedirs(prep, exist_ok=True)

    ok_rows, fail_rows = [], []
    if not (fillable or unfixable or bad_trigger or bad_inherited or with_image):
        if unknown:
            return {"status": "fail", "note": f"{len(unknown)} 行预览图读不到尺寸，请等待图片加载后重试"}
        note = (f"{len(rows)} 行预览图均已满足 1:1 且不小于 "
                f"{PREVIEW_MIN_SIDE}x{PREVIEW_MIN_SIDE}")
        return {"status": "skipped", "note": note}

    if fillable:
        # 源颜色图映射：colorImages[颜色].mainFile 是本地已下载文件名，优先用；读不到
        # 或该颜色没映射就退化到同色行 url，不因读文件失败就判整段失败（best-effort）。
        color_files = {}
        # 【配图颜色集合】colorImages 的全部 key＝商家在源站配过图的颜色。空位行的颜色
        # 不在里面，说明商家建了 SKU 却没上传图（如毛绒玩偶的「小鬼皮壳」「木乃伊皮壳」），
        # 这种不能用别的颜色图去补——挂错图会被平台罚，直接反选（见下方循环）。
        # 【集合键与源图键都归一】源侧键是 1688 颜色选择器名、可能带卖家标记（`莓红✦★`），
        # 而页面列值通常渲染成干净名——不归一，闸与源图查找会在符号差上整维失配
        # （2026-09-28 实测，见 _color_key）。
        known_colors = set()
        # 主图标注：只给 _pick_fill_source 用来跳过永久不可用的图（见那边的说明）
        notes_by_file = {}
        try:
            with open(ctx["info_path"], encoding="utf-8") as f:
                info = json.load(f)
            known_colors = {_color_key(c) for c in (info.get("colorImages") or {})}
            for c, v in (info.get("colorImages") or {}).items():
                if isinstance(v, dict) and v.get("mainFile"):
                    color_files[_color_key(c)] = v["mainFile"]
            notes_by_file = vision._notes_by_file(info)
        except Exception as e:
            logger.warning(f"读 colorImages 失败（空位补图退化为同色行 url）：{e}")
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"{len(fillable)} 行预览图为空，尝试按颜色源图自动补图"})
        for r in fillable:
            i, color = r["i"], r.get("color") or ""
            tag = f"第 {i + 1} 行" + (f"「{color}」" if color else "")
            # 【没配图的颜色直接反选，不退化成同款图】商家建了 SKU 却不上传图（2026-09-16
            # 毛绒玩偶 975173698426：颜色维 11 个里「小鬼皮壳」「木乃伊皮壳」两个在源站
            # 颜色选择器里 hasImg=false），用别的颜色图补会挂错图、被平台罚款。仅当
            # colorImages 提取到（非空）才做这个判断——提取得空说明是页面结构/提取问题
            # 而非「商家没配图」，此时退回 _pick_fill_source 的同款退化逻辑，不误伤。
            # 【闸按「本行真实颜色」判，不是行标识】行标识是变种表第一维，尺码维商品里
            # 装的是尺码名——拿它查颜色键必然全落空，闸会从「商家没给颜色配图才反选」
            # 退化成「所有尺码都反选」（2026-09-28 offer 1067355258988 宠物保暖打底衫实况：
            # S/XL 空位全进 unfixable，而商家明明给颜色配了图，本来走 _pick_fill_source
            # 是能补上的）。取不到颜色维（单维类目/旧数据）时退回行标识，维持原行为。
            probe = r.get("colorDim") or color
            if probe and known_colors and _color_key(probe) not in known_colors:
                logger.warning(f"预览图 {tag} 颜色「{color}」商家未配图，直接反选该规格")
                unfixable.append(r)
                continue
            out = os.path.join(prep, f"fill{i:02d}.jpg")
            # 取源：同规格源图 → 同规格其它行的图 → 同款任意行的图（见 _pick_fill_source）
            src_path = _pick_fill_source(r, rows, color_files, ctx["workdir"], prep,
                                         notes_by_file)
            if not src_path:
                # 一张可用源图都没有：这个规格发不出去（空图位被平台硬拒），
                # 交下面统一反选，不再报「需人工补」——人工也没有图可补。
                logger.warning(f"预览图 {tag} 空位找不到任何可用源图，改为反选该规格")
                unfixable.append(r)
                continue
            if "-raw" in os.path.basename(src_path):
                try:
                    src_path = await _clean_downloaded_preview(src_path, prep)
                except PreviewImageCleanError as e:
                    # 出图链路异常（API/结果下载/配置中心）≠ 质检未过，按环节分开报
                    # （PreviewImageCleanError docstring 的既定分类要求）
                    logger.warning(f"预览图 {tag} 回源图英化出图失败，改为反选该规格：{e}")
                    unfixable.append(r)
                    await emit({"type": "manual_check", "stage": "sku_preview",
                                "message": f"{tag} 回源预览图英化出图失败，已阻止上传"
                                           f"（{str(e)[:100]}）"})
                    continue
                if not src_path:
                    unfixable.append(r)
                    await emit({"type": "manual_check", "stage": "sku_preview",
                                "message": f"{tag} 回源预览图英化质检未通过，已阻止上传"})
                    continue
            try:
                sq = images.square_image(src_path, out_path=out)
            except Exception as e:
                logger.warning(f"预览图 {tag} 空位补图合规化失败，改为反选该规格：{e}")
                unfixable.append(r)
                continue
            rep = await sku_preview_replace_row(
                session, i, sq["output"], prev_idx,
                color_idx=color_idx, expect_color=color, fill_empty=True)
            if rep.get("status") == "ok":
                ok_rows.append(tag)
                logger.info(f"预览图 {tag} 空位已补图 {sq['outSize']}")
            else:
                # 【补图失败也反选，不留空图位】页面交互失败（菜单没展开/选图对不上）
                # 与「没源图」在结果上是同一件事：这一行仍是空的，带着它保存必被拒。
                logger.warning(
                    f"预览图 {tag} 空位补图失败[{rep.get('stage') or '?'}]，改为反选该规格："
                    f"{rep.get('err') or rep.get('detail') or ''} "
                    f"| fileId={(rep.get('fileId') or '')[-40:]}")
                unfixable.append(r)

    # 【补不上的空行统一反选】放在 fillable 循环之后：那个循环会把「没源图/合规化失败/
    # 补图交互失败」的行也追加进 unfixable，一起处置比分两处各判一次清楚。
    dropped_specs = []
    if unfixable:
        tags = "、".join(
            f"第 {r['i'] + 1} 行" + (f"「{r.get('color')}」" if r.get("color") else "")
            for r in unfixable[:8])
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"{len(unfixable)} 行预览图补不上（{tags}），"
                               "按规格反选掉不发它们（留着整单会被平台拒「请上传预览图」）"})
        dr = await _drop_unfixable_rows(session, emit, unfixable)
        dropped_specs = dr["dropped"]
        # 反选没成功的规格仍是空图位，如实计入失败（它会让 save 被拒，必须暴露）
        fail_rows.extend(f"「{name}」预览图空且未能反选" for name in dr["kept"])
        if dropped_specs:
            # 【反选后必须重读页面，不能用旧的行下标继续】变种表被平台整表重建，行数变少、
            # 序号前移，按旧下标换图会把图挂到别的 SKU 上（同 sku_preview_replace_row
            # 行序核对要防的事）。重读后重算 bad 分组，本轮接着处理剩下的尺寸不合规行。
            st = await sku_preview_state(session)
            rows = st.get("rows") or []
            prev_idx, color_idx = st.get("previewIdx"), st.get("colorIdx")
            bad = [r for r in rows if r.get("bad") and r.get("url")]
            bad_trigger = [r for r in bad
                           if r.get("hasTrigger") or r.get("hasFillSlot")]
            bad_inherited = [r for r in bad
                             if not (r.get("hasTrigger") or r.get("hasFillSlot"))]
            still_empty = [r for r in rows if r.get("empty")]
            await emit({"type": "log", "stage": "sku_preview",
                        "message": f"反选后变种表剩 {len(rows)} 行，"
                                   f"其中 {len(bad)} 行尺寸不合规、"
                                   f"{len(still_empty)} 行仍是空图位"})
            if still_empty:
                # 反选生效了却还有空行：这些行属于【没被反选掉的规格】（多维类目里
                # 另一维的组合行）。它们仍会让 save 被拒，如实报出来交人工。
                tags = "、".join(f"第 {r['i'] + 1} 行「{r.get('color') or ''}」"
                                 for r in still_empty[:8])
                fail_rows.extend(f"第 {r['i'] + 1} 行仍空" for r in still_empty)
                await emit({"type": "manual_check", "stage": "sku_preview",
                            "message": f"反选后仍有 {len(still_empty)} 行预览图为空"
                                       f"（{tags}），保存会报「请上传预览图」，需人工处理"})

    if bad_trigger or bad_inherited:
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"{len(rows)} 行预览图里 {len(bad)} 行不合规"
                               f"（非 1:1 或小于 {PREVIEW_MIN_SIDE}x{PREVIEW_MIN_SIDE}），"
                               "逐行下载后做 1:1 合规化再换回"})
    if bad_inherited:
        tags = "、".join(
            f"第 {r['i'] + 1} 行" + (f"「{r.get('color')}」" if r.get("color") else "")
            for r in bad_inherited[:8])
        await emit({"type": "manual_check", "stage": "sku_preview",
                    "message": f"{len(bad_inherited)} 行预览图不合规且无换图入口"
                               f"（继承主图）：{tags}。这些行共享主图、无法单独换图，"
                               "替换主图后应自动更新，若发布仍报预览图尺寸请人工核对"})
        fail_rows.extend(f"第 {row['i'] + 1} 行「{row.get('color') or ''}」无换图入口"
                         for row in bad_inherited)
    for r in bad_trigger:
        i, color = r["i"], r.get("color") or ""
        tag = f"第 {i + 1} 行" + (f"「{color}」" if color else "")
        raw = os.path.join(prep, f"row{i:02d}-raw.jpg")
        out = os.path.join(prep, f"row{i:02d}.jpg")
        try:
            if not extract._download_image(r["url"], raw):
                logger.warning(f"预览图 {tag} 源站取不到（404 等），保持原样")
                await emit({"type": "manual_check", "stage": "sku_preview",
                            "message": f"{tag} 预览图 {r['w']}x{r['h']} 不合规，"
                                       f"但源图已从源站失效、取不到，仍是原图"
                                       f"（发布会被拦，请人工换图）"})
                fail_rows.append(tag)
                continue
            # 【先质检再决定要不要出图】几何不合规不等于画面脏：2026-09-19 判据从
            # 「比例容差」改成严格 w==h 之后，「画面本来干净、只差几 px 不方」的图开始
            # 进这条通道（商品 1081125452313 那张 1206x1204 即此）。无条件走
            # _clean_downloaded_preview 等于为纯几何问题白烧一次出图——出图依赖代理、
            # 有内容审核，失败反而把本来只需 PIL 裁方就能修好的行拖成 fail。
            # 判干净就只做 square_image（画面一个像素不改，同下面语言关「判干净就完全
            # 不碰」的取向）；判脏或判不出结论才英化——后者与原行为一致。
            qc = await vision.check_cleaned_twice(raw)
            if qc.get("clean") is True:
                logger.info(f"预览图 {tag} 画面已干净（{r['w']}x{r['h']} 仅几何不合规），"
                            "只做 1:1 合规化、不动画面")
            else:
                try:
                    cleaned = await _clean_downloaded_preview(
                        raw, prep, issues=qc.get("issues") or "")
                except PreviewImageCleanError as e:
                    # 出图链路异常按环节直报（理由同 fill 通道那处 try）
                    logger.warning(f"预览图 {tag} 回源图英化出图失败，保持原样：{e}")
                    fail_rows.append(tag)
                    await emit({"type": "manual_check", "stage": "sku_preview",
                                "message": f"{tag} 预览图 {r['w']}x{r['h']} 不合规，"
                                           f"但英化出图失败、仍是原图：{str(e)[:100]}"})
                    continue
                if not cleaned:
                    fail_rows.append(tag)
                    await emit({"type": "manual_check", "stage": "sku_preview",
                                "message": f"{tag} 回源预览图英化质检未通过，已阻止上传"})
                    continue
                raw = cleaned
            sq = images.square_image(raw, out_path=out)
        except Exception as e:
            # 下载或合规化失败：该行保持原样（原图还挂着，不会变空）
            logger.warning(f"预览图 {tag} 合规化失败，保持原样：{e}")
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图 {r['w']}x{r['h']} 不合规，"
                                   f"但合规化失败、仍是原图（发布会被拦）：{str(e)[:80]}"})
            fail_rows.append(tag)
            continue
        rep = await sku_preview_replace_row(
            session, i, sq["output"], prev_idx,
            color_idx=color_idx, expect_color=color, fill_empty=not r.get("hasTrigger"))
        if rep.get("status") == "ok":
            ok_rows.append(tag)
            logger.info(f"预览图 {tag} 已换成 {sq['outSize']}（原 {r['w']}x{r['h']}）")
        else:
            fail_rows.append(tag)
            # 【失败必须落日志，不能只发 manual_check 事件】2026-09-01 排查
            # 890185900190（4 行「红色」全失败）时，manual_check 既不写日志文件也不
            # 进告警（刻意的，见 alert 那处：manual_check 会刷屏），于是事后只知道
            # 「0/4 行已合规化」，拿不到 rep 里的 stage —— 到底是 open-space、pick
            # 还是 readback 无从判断，只能重跑一次才能定位。
            # 替换失败是整单发布会被拦的硬问题，值一条 warning。
            logger.warning(
                f"预览图 {tag} 替换失败[{rep.get('stage') or '?'}]："
                f"{rep.get('err') or rep.get('detail') or ''} "
                f"| fileId={(rep.get('fileId') or '')[-40:]} "
                f"| srcBefore={(rep.get('srcBefore') or '')[-40:]} "
                f"| srcAfter={(rep.get('srcAfter') or '')[-40:]}")
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图替换失败[{rep.get('stage')}]："
                                   f"{str(rep)[:120]}"})

    # ---- 语言关：几何已合规的预览图同样要判「有没有中文/水印」 ----
    # 【为什么必须补这一段】本阶段此前只做几何合规（bad = 非 1:1 或短边 < 800），
    # 「尺寸本来就合格、但图上是中文/1688 水印」的行【一次都不进英化通道】：2026-09-19
    # 编辑页 id=184807703152217257 实测，变种表 6 行的预览图都是 cbu01.alicdn 上的同一
    # 张 1440×1440 源图，图上「萌龙弹射飞机」和 shop…1688.com 水印俱全，而 bad=False，
    # 于是本阶段 0.0s 返回「6 行预览图均已满足 1:1 且不小于 800x800」——那张图在买家页
    # 选规格时直接可见，中文又是 Temu 最硬的红线，不改就是一路发上真店。同批
    # 1062937493650 的四行是清一色英文源图，所以看到的是「有的翻了、有的没翻」：翻的是
    # 几何不合规、顺路走 bad 通道的；没翻的正是这一段原先压根不看的那批。
    #
    # 【按 URL 去重：一个 URL 只【英化】一次，但组内每一行都要各自替换】同一张源图常被
    # 多行共用（上面那单 6 行一个 URL），逐行英化等于把同一张图质检/生图六遍。
    # 【别把「去重」理解成「换一行就够」】2026-09-19 实况取证（1051894953703，正是这段
    # 代码的首跑）：6 行是【各自】挂着自己的那张图引用、各自有换图入口的，换掉第一行后
    # 2~6 行仍指向 alicdn 源图——「其余行共享主图、会跟着主行变」那种形态只适用于没有
    # 入口的继承行（见 bad_inherited）。故组内凡是有入口的行都要逐个替换；一个入口都没有
    # 的组才报人工（那种组才是继承主图、本阶段换不了）。
    #
    # 【判干净就完全不碰】省一次上传与替换，画面本来就没变；判脏才英化——与 ⑤c「判脏
    # 才换」同一取向，也与本函数开头「只在纯增益方向动手」一致。而「判干净」等于放行这
    # 张原图，故判据必须与 ⑤c 同口径（vision.check_cleaned_twice 双检取严，理由见那边）。
    handled = {r.get("url") for r in bad_trigger}
    groups: dict = {}
    for r in rows:
        url = r.get("url") or ""
        if not url or r.get("empty") or r.get("bad") or url in handled:
            continue
        groups.setdefault(url, []).append(r)
    clean_groups, lang_failed = [], []
    if groups:
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"{len(groups)} 组预览图是源站原图，逐组判定是否含中文/水印，"
                               "脏的英化后替换"})
    for url, group in groups.items():
        head = group[0]
        tag = (f"第 {head['i'] + 1} 行"
               + (f"「{head.get('color')}」" if head.get("color") else "")
               + (f"等 {len(group)} 行" if len(group) > 1 else ""))
        # 组内凡是有换图入口的行都要替换（理由见上面那段实况取证）
        targets = [r for r in group if r.get("hasTrigger") or r.get("hasFillSlot")]
        raw = os.path.join(prep, f"lang{head['i']:02d}-raw.jpg")
        try:
            if not extract._download_image(url, raw):
                raise RuntimeError("源站取不到（404 等）")
            qc = await vision.check_cleaned_twice(raw)
        except Exception as e:
            # 判不了脏就是「不知道有没有中文」，而未知在这条红线上不能当安全（同 ⑤c 的
            # unknown 处置），故如实计失败交人工，不静默放过。
            logger.warning(f"预览图 {tag} 源图取不到或判不了脏，保持原样：{e}")
            lang_failed.append(tag)
            fail_rows.append(tag)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图没能判定是否含中文/水印"
                                   f"（{str(e)[:80]}），未做英化、仍是源图，请人工核对"})
            continue
        if qc.get("clean") is True:
            clean_groups.append(tag)
            continue
        if not targets:
            # 整组都是继承主图、没有换图入口的行：本阶段换不了，如实报出来（同
            # bad_inherited 的处境，只是这里判的是语言不是尺寸）
            logger.warning(f"预览图 {tag} 判为脏（{qc.get('issues')}）"
                           "但整组没有换图入口，无法自动英化")
            lang_failed.append(tag)
            fail_rows.append(tag)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图含中文/水印，但整组没有换图入口、无法自动"
                                   f"英化（{qc.get('issues') or ''}），需人工换图"})
            continue
        try:
            cleaned = await _clean_downloaded_preview(raw, prep, issues=qc.get("issues") or "")
        except PreviewImageCleanError as e:
            # 出图链路异常（API/结果下载/配置中心）≠ 质检未过，按环节分开报
            # （PreviewImageCleanError docstring 的既定分类要求；2026-09-28 实录：
            # 结果 CDN TLS 握手失败被报成「英化质检未通过」）
            logger.warning(f"预览图 {tag} 英化出图失败，原图照旧：{e}")
            lang_failed.append(tag)
            fail_rows.append(tag)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图判脏需英化，但出图失败、仍是源图"
                                   f"（{str(e)[:100]}），请人工换图"})
            continue
        if not cleaned:
            logger.warning(f"预览图 {tag} 英化质检未通过（{qc.get('issues')}），原图照旧")
            lang_failed.append(tag)
            fail_rows.append(tag)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图英化质检未通过、仍是源图"
                                   f"（{qc.get('issues') or ''}），请人工换图"})
            continue
        out = os.path.join(prep, f"lang{head['i']:02d}.jpg")
        try:
            sq = images.square_image(cleaned, out_path=out)
        except Exception as e:
            logger.warning(f"预览图 {tag} 英化产物合规化失败，保持原样：{e}")
            lang_failed.append(tag)
            fail_rows.append(tag)
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{tag} 预览图英化产物合规化失败、仍是源图：{str(e)[:80]}"})
            continue
        # 英化产物组内共用（只生一次图），但替换要逐行走：本组每行都各挂各的图引用
        done = 0
        for r in targets:
            row_tag = (f"第 {r['i'] + 1} 行"
                       + (f"「{r.get('color')}」" if r.get("color") else ""))
            rep = await sku_preview_replace_row(
                session, r["i"], sq["output"], prev_idx,
                color_idx=color_idx, expect_color=r.get("color") or "",
                fill_empty=not r.get("hasTrigger"))
            if rep.get("status") == "ok":
                ok_rows.append(row_tag)
                done += 1
                continue
            lang_failed.append(row_tag)
            fail_rows.append(row_tag)
            # 同 bad 行那处：失败必须落日志，manual_check 事件不写日志文件
            logger.warning(
                f"预览图 {row_tag} 英化后替换失败[{rep.get('stage') or '?'}]："
                f"{rep.get('err') or rep.get('detail') or ''} "
                f"| fileId={(rep.get('fileId') or '')[-40:]} "
                f"| srcBefore={(rep.get('srcBefore') or '')[-40:]} "
                f"| srcAfter={(rep.get('srcAfter') or '')[-40:]}")
            await emit({"type": "manual_check", "stage": "sku_preview",
                        "message": f"{row_tag} 预览图英化后替换失败[{rep.get('stage')}]"
                                   f"（仍是带中文的源图）：{str(rep)[:120]}"})
        if done:
            logger.info(f"预览图 {tag} 是带中文/水印的源图，已英化替换 {done}/{len(targets)} 行")

    note = f"{len(ok_rows)} 行预览图已处理"
    if clean_groups:
        note += f"；另有 {len(clean_groups)} 组源图画面本已干净、未改动"
    if lang_failed:
        note += f"；{len(lang_failed)} 组源图未能英化（{'、'.join(lang_failed[:4])}）"
    if unknown:
        fail_rows.extend(f"第 {row['i'] + 1} 行图片未加载" for row in unknown)
    # 反选掉的规格要进 note：变种表少了行是本阶段主动做的，不写出来后面看行数对不上
    if dropped_specs:
        note += f"；已反选补不上预览图的规格 {'、'.join(dropped_specs)}（不发它们）"
    if bad_inherited:
        note += f"（另有 {len(bad_inherited)} 行继承图无换图入口，已提醒人工核对）"
    if fail_rows:
        note += f"（失败：{'、'.join(fail_rows)}）"
    if fail_rows and any("仍空" in x or "未能反选" in x for x in fail_rows):
        note += "；预览图为空，保存会提示「请上传预览图」"
    return {"status": "ok" if not fail_rows else "fail", "note": note,
            "droppedSpecs": dropped_specs}
