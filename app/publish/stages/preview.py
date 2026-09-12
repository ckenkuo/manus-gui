"""店小秘发布共用能力：stages.preview。各来源流程由 workflows/ 独立定义。"""

import json
import asyncio
import os
import shutil
from app.logger import logger
from app.publish import extract, images, variant_colors
from app.publish.browser import BrowserSession
from app.publish.media.preview import PREVIEW_MIN_SIDE, sku_preview_replace_row, sku_preview_state


def _pick_fill_source(row: dict, rows: list, color_files: dict,
                      workdir: str, prep: str) -> str:
    """给一个空预览格找源图，返回本地文件路径（找不到返回空串）。

    取源优先级（越靠前越贴近「这一行本该有的那张图」）：
      1. 同规格的源图：colorImages[本行规格名].mainFile，阶段① 已下载到本地；
      2. 同规格其它行页面上已挂的图：同一个规格名的另一行（多尺码商品里同色行共用
         一张预览图，那张就是本行缺的）；
      3. 【2026-09-12 新增】任意一行页面上已挂的图：单维类目（车贴/桌布/型号维）里
         每行就是一个独立规格，第 2 条按规格名找必然落空，而「同一款商品的另一张
         预览图」仍比空图位强——空图位是平台硬拒（「请上传预览图」），挂上同款别的
         图只是辨识度差一点。这一条也是「同一款有一样的 SKC 就用同一张图」的落地：
         同款各行的图本就同源，取哪一张都不算挂错款。

    取不到才轮到调用方去反选这个规格（见 _st_sku_preview 的处置）。
    """
    i, color = row["i"], row.get("color") or ""
    mf = color_files.get(color)
    if mf:
        p = os.path.join(workdir, mf)
        if os.path.exists(p):
            return p
    # 页面已挂的图：先找同规格行，再退到任意非空行
    peers = [x for x in rows if x.get("url") and not x.get("empty")]
    peer = next((x for x in peers if x.get("color") == color), None)
    if peer is None and peers:
        peer = peers[0]
        logger.info(f"预览图第 {i + 1} 行「{color}」没有同规格源图，"
                    f"退用同款第 {peer['i'] + 1} 行「{peer.get('color')}」的图"
                    f"（空图位会被平台硬拒，同款图只是辨识度差一点）")
    if peer is None:
        return ""
    raw = os.path.join(prep, f"fill{i:02d}-raw.jpg")
    return raw if extract._download_image(peer["url"], raw) else ""


async def _drop_unfixable_rows(session: BrowserSession, emit, rows: list) -> dict:
    """把补不上预览图的行所属规格反选掉，返回 {"dropped": [...], "kept": [...]}。

    【为什么是反选而不是报人工】空预览图是平台硬拒项（save 报「请上传预览图」），
    只要有一行补不上，整单就发不出去。而这些行往往本就不该发：认领没带图的规格多是
    源站那边的占位/配件规格（与 ⑦a 剔配件色同一类问题，只是判据不同——⑦a 看「有没有
    源数据」，这里看「有没有图」）。反选掉它，平台重建变种表时这一行连同图位一起消失，
    其余规格照常发布，比整单卡死好。

    【绝不反选到「只剩它自己」】某维只剩一个已勾选项时不能再反选——变种维不能为空，
    平台会把整张变种表清掉。这种情况如实报人工。

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

    prep = os.path.join(ctx["workdir"], "sku-preview")
    need_work = bool(fillable or bad_trigger)
    if need_work:
        shutil.rmtree(prep, ignore_errors=True)
        os.makedirs(prep, exist_ok=True)

    ok_rows, fail_rows = [], []
    if not (fillable or unfixable or bad_trigger or bad_inherited):
        if unknown:
            return {"status": "fail", "note": f"{len(unknown)} 行预览图读不到尺寸，请等待图片加载后重试"}
        note = (f"{len(rows)} 行预览图均已满足 1:1 且不小于 "
                f"{PREVIEW_MIN_SIDE}x{PREVIEW_MIN_SIDE}")
        return {"status": "skipped", "note": note}

    if fillable:
        # 源颜色图映射：colorImages[颜色].mainFile 是本地已下载文件名，优先用；读不到
        # 或该颜色没映射就退化到同色行 url，不因读文件失败就判整段失败（best-effort）。
        color_files = {}
        try:
            with open(ctx["info_path"], encoding="utf-8") as f:
                info = json.load(f)
            for c, v in (info.get("colorImages") or {}).items():
                if isinstance(v, dict) and v.get("mainFile"):
                    color_files[c] = v["mainFile"]
        except Exception as e:
            logger.warning(f"读 colorImages 失败（空位补图退化为同色行 url）：{e}")
        await emit({"type": "log", "stage": "sku_preview",
                    "message": f"{len(fillable)} 行预览图为空，尝试按颜色源图自动补图"})
        for r in fillable:
            i, color = r["i"], r.get("color") or ""
            tag = f"第 {i + 1} 行" + (f"「{color}」" if color else "")
            out = os.path.join(prep, f"fill{i:02d}.jpg")
            # 取源：同规格源图 → 同规格其它行的图 → 同款任意行的图（见 _pick_fill_source）
            src_path = _pick_fill_source(r, rows, color_files, ctx["workdir"], prep)
            if not src_path:
                # 一张可用源图都没有：这个规格发不出去（空图位被平台硬拒），
                # 交下面统一反选，不再报「需人工补」——人工也没有图可补。
                logger.warning(f"预览图 {tag} 空位找不到任何可用源图，改为反选该规格")
                unfixable.append(r)
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

    note = f"{len(ok_rows)} 行预览图已处理"
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
