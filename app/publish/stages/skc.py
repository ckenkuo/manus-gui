"""店小秘发布共用能力：stages.skc。各来源流程由 workflows/ 独立定义。"""

import os
import shutil
from app.logger import logger
from app.publish import extract, images, state, vision
from app.publish.browser import BrowserSession
from app.publish.media.skc import (
    SKC_ROW_MIN_IMAGES,
    _skc_row_matches,
    _skc_row_state,
    skc_image_support,
    skc_replace_row,
)
from app.publish.variant_colors import drop_accessory_colors


async def _skc_size_fallback(ctx: dict, session: BrowserSession, emit,
                             colors: list, done_rows: list) -> list:
    """尺寸兜底：把没换成图的颜色行里破线的图，就地下载 + fit_34 + 整行替换。

    【为什么需要这一层】阶段⑦ 的换图靠视觉判「哪张图属于哪个颜色」，判不出来时整行
    被跳过（plan_skc 把该色塞进 uncertain_rows，_st_skc 只对 rows 换图）。于是那一行
    留在页面上的就是 1688 原始图，往往低于服装类下限 1340×1785，一路带到阶段⑫ save
    报「服装类图片尺寸不能小于 1340px * 1785px」——而这个报错页面是静默的，只有区块
    变红，极难定位到是哪一行的哪张图。

    2026-08-24 真站取证（rowid 173539495453435641）：两个颜色里粉红色换图成功
    （1340×1787），咖啡色因视觉分不出图被跳过，6 张全是 cbu01.alicdn.com 的
    1000×1000 / 1200×1200，保存被拦。状态文件当时如实记着「1/2 行完成（失败：咖啡色）」
    ——阶段没骗人，是失败后没人兜底。

    【内容判断失败 ≠ 尺寸可以不管】这与描述图那条兜底（plan_desc 的 needsUpscale）
    同源：颜色归属判不出来是内容问题，尺寸达标是平台硬校验，两者正交。这里不试图
    重新判归属（那正是失败的那一步），只把该行【现有的】图原地做合规化——画面一张
    不换、顺序一张不动，只补像素与比例。

    只在【纯增益】方向动手：读不到尺寸、行里图本来就达标、或下载/合规化失败，
    一律保持原样并报人工确认，绝不把行搞成空的。
    """
    fixed = []
    for kw in colors:
        st = await _skc_row_state(session, kw)
        if st.get("err"):
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行读不到状态，尺寸兜底跳过：{st['err']}"})
            continue
        small = st.get("tooSmall") or []
        # 【张数不足不在这里补，别加 count < 下限 的判据】本兜底只把该行现有图下载重做
        # 合规化后原样重挂，张数一张不增——对「行内只有 1~2 张」毫无帮助，白跑一轮。
        # 张数补齐必须在换图之前用同款其它主图凑（见 _pad_row_images），这里只管尺寸。
        # 另外行内图数不足时同样【要】报出来，否则保存被拦时无从定位，见下方 thin 分支。
        if kw in done_rows:
            continue                      # 已经换过图的行不动（尺寸由上传闸门保证）
        if (st.get("count") or 0) < SKC_ROW_MIN_IMAGES:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行只有 {st.get('count')} 张图，低于每行下限 "
                                   f"{SKC_ROW_MIN_IMAGES} 张，保存会被拦（请人工补图）"})
        if not small:
            continue                      # 该行图都达标，不必动
        # 该行【全部】图都要重做：整行替换是「挂新图再删旧图」，只补破线那几张会让
        # 达标的旧图被一并删掉（skc_replace_row 的语义是整行换）
        urls = st.get("urls") or []
        prep = os.path.join(ctx["workdir"], f"skc-fix-{kw}")
        shutil.rmtree(prep, ignore_errors=True)
        os.makedirs(prep, exist_ok=True)
        ok_files = 0
        for i, u in enumerate(urls, 1):
            dst = os.path.join(prep, f"{i:02d}.jpg")
            try:
                if not extract._download_image(u, dst):
                    logger.warning(f"「{kw}」行第 {i} 张源站取不到（404 等），跳过该张")
                    continue
                images.fit_34(dst, out_path=dst)   # 3:4 + ≥1340×1785，两条硬规则一起满足
                ok_files += 1
            except Exception as e:
                logger.warning(f"「{kw}」行第 {i} 张兜底处理失败，跳过该张：{e}")
        if not ok_files:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行 {len(small)} 张图低于 "
                                   f"{images.CLOTH_MIN_W}x{images.CLOTH_MIN_H}，"
                                   "但一张都没处理成功，仍是原图（保存会被拦）"})
            continue
        r = await skc_replace_row(session, kw, prep)
        if r.get("status") == "ok":
            fixed.append(kw)
            logger.info(f"「{kw}」行尺寸兜底完成：{ok_files} 张重做合规化"
                        f"（原有 {len(small)} 张破线）")
        else:
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行尺寸兜底替换失败：{str(r)[:120]}"})
    return fixed


def _pad_row_images(picked: list, info: dict, workdir: str) -> tuple:
    """把某颜色行的选图补到每行下限，返回（补齐后的路径列表, 补进来的文件名）。

    【为什么必须补】平台要求每行 3~10 张，而视觉按颜色归属给每行只分到 1~2 张是
    常态——一件衣服的某个颜色不会有 6 张独立照片（实测 product-985713733384：
    8 张主图分 4 个颜色，每行 1~2 张）。不补齐就是换完保存被拦，而那个报错是静默的
    （只有区块变红）。

    【补什么】同款其它主图：平铺、细节、材质图这类不体现颜色差异的图，挂在任何
    颜色行下都说得通，这也是人工发布时的做法。优先干净图（无水印/无中文），
    排除重复图与尺码表/工厂图这类非商品图。

    【顺序】原选图在前、补进来的在后——首位仍是该颜色的主图，符合
    skc_replace_row「按文件名排序挂图」的约定（调用方会重命名成 main-NN）。
    """
    if len(picked) >= SKC_ROW_MIN_IMAGES:
        return picked, []
    notes = vision._notes_by_file(info)
    # 阶段①已把不达标的主图标了 sizeWarning，补图时优先躲开它们：
    # batch_fit34 能放大到达标，但放大会掉画质，有原生达标图就别用小图
    size_bad = {e.get("file") for e in
                (((info or {}).get("images") or {}).get("mainDetail") or [])
                if isinstance(e, dict) and e.get("sizeWarning") and e.get("file")}

    def _note(path: str) -> dict:
        return notes.get(os.path.basename(path)) or {}

    have = set(picked)
    cands = [p for p in vision._main_files(workdir)
             if p not in have
             and not _note(p).get("duplicate")
             and (_note(p).get("kind") or "") not in vision._SKIP_KINDS]

    def _key(p: str) -> tuple:
        # 干净图优先，其余按脏度——与 plan_skc 同一套排序取向；
        # 尺寸不达标的排在最后（只有达标图不够时才轮到它们，由 batch_fit34 放大兜底）
        return (os.path.basename(p) in size_bad,
                not _note(p).get("clean"),
                vision._dirty_score(_note(p)))

    need = SKC_ROW_MIN_IMAGES - len(picked)
    # 【不合规图优先不用，凑不够才放回】带中文/水印/他人 logo 是 Temu 的硬红线，
    # 原实现只把它们按 _dirty_score 排到后面、照样补进颜色行——等于 plan_skc 那边
    # 刚排除掉，这里又补回来。现在先只用合规图，仍不足行下限才放回（整行不足 3 张
    # 会让阶段⑫ save 被静默拦下，两害相权），放回的由 _st_skc 逐行查出来报人工确认。
    added = sorted([p for p in cands if not vision.is_dirty(_note(p))], key=_key)[:need]
    if len(added) < need:
        extra = sorted([p for p in cands if vision.is_dirty(_note(p))],
                       key=_key)[:need - len(added)]
        if extra:
            logger.warning(f"合规图只够补 {len(added)} 张，凑不满每行下限 "
                           f"{SKC_ROW_MIN_IMAGES} 张，被迫补 {len(extra)} 张仍带"
                           f"中文/水印/logo 的图：{[os.path.basename(p) for p in extra]}")
            added += extra
    return picked + added, [os.path.basename(p) for p in added]


async def _st_drop_acc(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑦a 剔配件色：源里只有单一尺码的颜色（头饰/配件之类）直接反选，不发它的 SKC/SKU。

    【为什么排在 ⑦ 之前】反选让平台重建变种表，该色的 SKC 图位与 SKU 行一起消失，
    于是 ⑦/⑦b 天然不会碰它——不必在那两个阶段各自判「这个颜色跳不跳」。

    判据与取证见 pipeline._JS_VARIANT_ROW_FILL 上方注释：源颜色名与页面色板名永远
    对不上（源「紫精灵头纱」↔ 页面「红色」），故只能按变种表里「哪些行有源数据」认。
    只认「别的颜色都是多行齐全、只有它是单行」，各色齐平的商品一个都不剔。
    """
    r = await drop_accessory_colors(session)
    if r.get("status") == "ok":
        await emit({"type": "log", "stage": "drop_acc",
                    "message": f"已剔除配件色 {r['dropped']}（只有单一尺码，"
                               f"不发其 SKC/SKU），变种表剩 {r.get('rowCount')} 行"})
        note = f"已反选配件色 {'、'.join(r['dropped'])}"
        if r.get("missed"):
            # 部分没剔掉：主流程照走，但要让人知道页面上还留着哪些
            await emit({"type": "manual_check", "stage": "drop_acc",
                        "message": f"配件色 {r['missed']} 未能反选（页面颜色："
                                   f"{r.get('pageColors')}），需人工确认是否要发"})
            note += f"；{len(r['missed'])} 个未能反选"
        return {"status": "ok", "note": note}
    if r.get("status") == "skipped":
        return {"status": "skipped", "note": r.get("reason") or ""}
    # error：源颜色名与页面对不上，或反选没生效。这不该拦整单——配件色多发一个
    # 不影响其它 SKU 的正确性，故报 manual_check 后按 skipped 放行（best-effort 取向）。
    await emit({"type": "manual_check", "stage": "drop_acc",
                "message": f"配件色剔除未完成：{r.get('reason')}"
                           f"（页面颜色：{r.get('pageColors')}），需人工确认"})
    return {"status": "skipped", "note": f"未剔除：{r.get('reason')}"}


async def _st_skc(ctx: dict, session: BrowserSession, emit) -> dict:
    info = state._load_info(ctx["info_path"])
    # 【先问页面支不支持按颜色配图】2026-08-29 真站取证（1014675972015 仿真花）：
    # 该类目变种属性区没有任何图片位（无 tr、无「选择图片」按钮、无 .single-image），
    # 颜色只是一列复选框。此时 ⑦ 无事可做，六行全报「找不到颜色行」纯属噪音，
    # 还会白烧一次 plan_skc 的视觉调用与整轮补图。与 ⑧⑨ 同一性质，见
    # pipeline.skc_image_support 上方的取证记录。
    sup = await skc_image_support(session)
    if sup.get("supported") is False:
        await emit({"type": "log", "stage": "skc",
                    "message": f"本类目变种属性区不支持按颜色配图"
                               f"（{sup.get('checkboxes')} 个颜色复选框、无图片位），"
                               "跳过 SKC 颜色图"})
        return {"status": "skipped", "note": "本类目无 SKC 颜色图位"}
    plan = await vision.plan_skc(info, ctx["workdir"], min_clean=SKC_ROW_MIN_IMAGES)
    rows = plan.get("rows") or []
    colors = [c for c in (info.get("colors") or []) if c]
    if plan.get("dirtyUsed"):
        logger.warning(f"SKC 候选池合规图不足，被迫放回：{plan['dirtyUsed']}"
                       "（哪一行真用上了，见下面逐行的人工确认）")
    notes_by_file = vision._notes_by_file(info)
    if not rows:
        await emit({"type": "manual_check", "stage": "skc",
                    "message": "视觉未给出任何颜色行选图，SKC 颜色图保持原样"})
        # 一行都没换 ≠ 尺寸不用管：留在页面上的 1688 原始图往往破线，
        # 阶段⑫ save 会被静默拦下（见 _skc_size_fallback）
        fx = await _skc_size_fallback(ctx, session, emit, colors, [])
        if fx:
            return {"status": "ok",
                    "note": f"未换图，但按尺寸兜底重做了 {len(fx)} 行：{'、'.join(fx)}"}
        return {"status": "skipped", "note": plan.get("reason") or "无可替换行"}
    # skipped_rows：续跑时页面上已经是本轮图片、无需重换的行。它必须与 ok_rows 一起
    # 传给尺寸兜底——跳过的行图是达标的，再被兜底重做一遍就白干了。
    ok_rows, fail_rows, skipped_rows, dirty_rows = [], [], [], []
    for row in rows:
        kw = row["keyword"]
        if row.get("uncertain"):
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行的图片归属判断没把握，已按最优猜测继续"})
        # 按 skc_replace_row「文件名排序挂图」约定：主图命名 main-01.jpg 落首位、免拖拽
        prep = os.path.join(ctx["workdir"], f"skc-{kw}")
        os.makedirs(prep, exist_ok=True)
        for old in os.listdir(prep):
            op = os.path.join(prep, old)
            # 上一轮 batch_fit34 的产物 skc-34 就落在 prep 里，是子目录：
            # os.remove 删目录在 Windows 抛 PermissionError（WinError 5）导致整阶段挂掉；
            # 而且旧产物不清干净，本轮颜色行图数变少时残留旧图会被 skc_replace_row 一并挂上
            if os.path.isdir(op):
                shutil.rmtree(op, ignore_errors=True)
            else:
                os.remove(op)
        # 补到下限 3 张：视觉按颜色只分到 1~2 张是常态，不补齐换完保存会被静默拦下
        picked, padded = _pad_row_images(row["images"], info, ctx["workdir"])
        if padded:
            logger.info(f"「{kw}」行只分到 {len(row['images'])} 张，"
                        f"补 {len(padded)} 张同款图到下限：{padded}")
            await emit({"type": "log", "stage": "skc",
                        "message": f"「{kw}」行按颜色只分到 {len(row['images'])} 张，"
                                   f"补 {len(padded)} 张同款图凑够每行下限 "
                                   f"{SKC_ROW_MIN_IMAGES} 张"})
        # 【复制兜底：源商品图本身太少时复制主图凑够下限】_pad_row_images 已把同款
        # 其它主图（含被迫放回的脏图）都补进来了，仍不足 3 张说明全商品没有第 3 张
        # 可用图。用户明确要求：这种时候复制一张已有图放后面凑数，而不是跳过留原图
        # 等保存被拦。复制的是 picked[0]（plan_skc 保证首位是该颜色主图），只补到
        # 下限就停；下面 enumerate 会把同一路径 copy 成两张内容相同的 main-NN。
        if picked and len(picked) < SKC_ROW_MIN_IMAGES:
            dup = SKC_ROW_MIN_IMAGES - len(picked)
            logger.info(f"「{kw}」行只有 {len(picked)} 张可用图，"
                        f"复制主图 {os.path.basename(picked[0])} 补 {dup} 张凑够下限")
            await emit({"type": "log", "stage": "skc",
                        "message": f"「{kw}」行只有 {len(picked)} 张可用图，"
                                   f"复制主图补 {dup} 张凑够每行下限 "
                                   f"{SKC_ROW_MIN_IMAGES} 张"})
            picked = picked + [picked[0]] * dup
        if not picked:
            # 一张可用图都没有（workdir 下无非重复、非尺码表/工厂图的主图），复制也
            # 无从下手，只能人工处理
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行没有任何可用图，"
                                   f"补不到每行下限 {SKC_ROW_MIN_IMAGES} 张，"
                                   "换图跳过（保存会被拦，请人工补图）"})
            fail_rows.append(kw)
            continue
        # 【最终挂上去的图逐行查一次合规】图有三个来源（plan_skc 分的、_pad_row_images
        # 补的、vision._usable 被迫放回的），只在某一处报必然漏。中文是 Temu 最硬的
        # 红线，挂上去要等到阶段⑮ 发布被打回才知道，那时已经看不出是哪行的哪张图。
        # 这里【不拦】：拦了整行就凑不够 3 张、save 反而被静默弹回（见 _pad_row_images）。
        row_dirty = [os.path.basename(p) for p in picked
                     if vision.is_dirty(notes_by_file.get(os.path.basename(p)) or {})]
        if row_dirty:
            dirty_rows.append(kw)
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行有 {len(row_dirty)} 张图仍带中文/水印/"
                                   f"logo（合规图凑不够 {SKC_ROW_MIN_IMAGES} 张，"
                                   f"已按最优可用继续）：{'、'.join(row_dirty)}"})
        for i, src in enumerate(picked, 1):
            shutil.copy(src, os.path.join(prep, f"main-{i:02d}.jpg"))
        fitted = images.batch_fit34(prep)

        # 续跑跳过：上一轮换成功时把 fileId 清单落进了 ctx["skc_done"]，
        # 若页面上就是那一批图（数量/托管/尺寸/逐个 fileId 全中），本行不必重换。
        # 判据刻意要求有清单——宽判据认不出「张数相同但内容不是这一批」，
        # 误判会把错图留在页面上，代价高于白跑一轮，理由见 _skc_row_matches。
        want = len([f for f in sorted(os.listdir(fitted["outdir"]))
                    if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))])
        prev_ids = (ctx.get("skc_done") or {}).get(kw) or []
        live = await _skc_row_state(session, kw)
        if not live.get("err"):
            m = _skc_row_matches(live, prev_ids, want)
            if m["done"]:
                skipped_rows.append(kw)
                logger.info(f"「{kw}」行跳过换图：{m['reason']}")
                await emit({"type": "log", "stage": "skc",
                            "message": f"「{kw}」行已是本轮图片，跳过换图（{m['reason']}）"})
                continue
            if prev_ids:
                logger.info(f"「{kw}」行需重换：{m['reason']}")

        r = await skc_replace_row(session, kw, fitted["outdir"])
        if r.get("status") == "ok":
            ok_rows.append(kw)
            # fileId 清单落进 ctx，由 run_product 的回写逻辑持久化，供下轮判跳过
            ctx.setdefault("skc_done", {})[kw] = r.get("fileIds") or []
        else:
            fail_rows.append(kw)
            # 换图失败的行上一轮的清单已经不作数了（行内是新旧混杂的中间态），
            # 留着会让下一轮拿旧清单去比对——比不中而已，但清掉更诚实
            (ctx.get("skc_done") or {}).pop(kw, None)
            await emit({"type": "manual_check", "stage": "skc",
                        "message": f"「{kw}」行换图失败：{str(r)[:120]}"})
    # 没换成图的行（视觉分不出归属、或换图失败）仍可能留着破线的原始图：
    # 内容判断失败不等于尺寸可以不管，这里只补像素与比例，画面一张不换。
    # 已跳过的行同样算「已完成」，不能再被兜底重做一遍。
    fixed = await _skc_size_fallback(ctx, session, emit, colors, ok_rows + skipped_rows)

    note = f"{len(ok_rows) + len(skipped_rows)}/{len(rows)} 行完成"
    if skipped_rows:
        note += f"（其中 {len(skipped_rows)} 行已是本轮图片、跳过：{'、'.join(skipped_rows)}）"
    if fail_rows:
        note += f"（失败：{'、'.join(fail_rows)}）"
    if fixed:
        note += f"；尺寸兜底重做 {len(fixed)} 行：{'、'.join(fixed)}"
    if dirty_rows:
        # 进 note 是为了留在状态文件里：事后被 Temu 打回时能直接对上是哪几行
        note += f"；{len(dirty_rows)} 行含不合规图：{'、'.join(dirty_rows)}"
    return {"status": "ok", "note": note}
