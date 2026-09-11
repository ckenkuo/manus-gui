"""店小秘发布共用能力：stages.description。各来源流程由 workflows/ 独立定义。"""

import os
from app.logger import logger
from app.publish import images, state, vision
from app.publish.browser import BrowserSession
from app.publish.media.description import (
    desc_delete,
    desc_map,
    desc_save,
    desc_text_delete_all,
    ensure_desc_closed,
)
from app.publish.media.description_replace import desc_replace
from app.publish.stages import (
    description_images as stages_description_images,
    description_text as stages_description_text,
    prewarm_access as stages_prewarm_access,
    prewarm_plans as stages_prewarm_plans,
)


async def _st_desc(ctx: dict, session: BrowserSession, emit) -> dict:
    m = await desc_map(session, ctx["info_path"])
    if m.get("status") != "ok":
        return {"status": "fail", "note": (m.get("err") or "desc_map 失败")[:200]}
    mods = m.get("modules") or []
    # 【不能在这里因「无图片」就跳过】mods 只统计图片模块，而描述区可能只放了文字
    # （尺码对照表之类）。原先在这里 return skipped，会让那种商品的文字完全不被清除：
    # 采集残留的垃圾 JSON、中文尺码表原样发到海外站。
    # 故图片为空只跳过图片处理，文字照删；两者都没有才是真的 skipped（见下方护栏）。
    info_for_desc = state._load_info(ctx["info_path"])

    # 【先处理文字模块，再处理图片】描述区是图文混排的，删文字模块会让 data-idx
    # 重排；而图片侧按源 URL 现查 pos（_resolve_desc_pos）、删除时内部重建 idx
    # 映射，不受影响。反序则要多读一遍 data-idx。
    #
    # 【文字模块默认一律删除，实测尺寸缺失时尺码文字英化保留】2026-09-01 用户要求
    # 描述区所有文字板块移除（此前是交 LLM 判「采集残留 JSON 删 / 尺码对照表英化
    # 保留」）。2026-09-04 再细化：当 ①b/视觉 没识别到实测尺寸（sizeMeasurements 为空、
    # ⑨ 靠估算）时，描述区里的尺码文字是买家唯一准确来源，改为英化 + cm→英寸后保留，
    # 其余照删；已识别到实测尺寸时（平台尺码表已有真实数据）仍全部删除。
    #
    text_note, n_text_deleted = "", 0
    try:
        if not info_for_desc.get("sizeMeasurements"):
            text_note, n_text_deleted = await stages_description_text._keep_size_text(session, info_for_desc, emit)
        else:
            tr = await desc_text_delete_all(session)
            if tr.get("status") == "error":
                logger.warning(f"文字模块枚举失败（保留原文，继续图片处理）：{str(tr)[:150]}")
                text_note = "文字模块处理异常"
            elif tr.get("found"):
                n_text_deleted = len(tr.get("deleted") or [])
                text_note = f"文字模块 删 {n_text_deleted}"
                if tr.get("failed"):
                    text_note += f" / 失败 {len(tr['failed'])}"
                    await emit({"type": "manual_check", "stage": "desc",
                                "message": f"文字模块删除有 {len(tr['failed'])} 项未成功"
                                           f"（原文仍留在页面上）：{str(tr['failed'])[:150]}"})
            logger.info(f"描述文字模块处理完成：{text_note}")
    except Exception as e:
        # 文字模块不是必填内容，删不掉就留着，不能拖垮整个描述阶段
        logger.warning(f"文字模块处理异常（保留原文，继续图片处理）：{e}")
        text_note = "文字模块处理异常"

    # 文字模块删除后图片模块的 data-idx 已重排，故 desc_map 要重读一次拿最新状态
    # （判据用实际删除数，不再匹配 text_note 文案——文案一改判断就失效）
    if n_text_deleted:
        m2 = await desc_map(session, ctx["info_path"])
        if m2.get("status") == "ok" and m2.get("modules"):
            mods = m2["modules"]

    if not mods:
        # 只有文字模块：文字已处理完，保存收尾，不进图片分支
        if not text_note:
            return {"status": "skipped", "note": "描述区无模块"}
        # allow_empty：能走到这个分支就说明描述区【没有图片模块】（mods 为空），
        # 故 desc_save 的「读回必须有图」校验在这里必然是假失败。2026-09-10 实测
        # Temu 女装衬衫（源详情图 0 张、描述区只有文字模块）就卡在这，整个 ⑬ 判 fail。
        # 此分支真正要验的是「文字模块处理掉了」，那由上面的 text_note 与保存结果共同担着。
        sv = await desc_save(session, allow_empty=True)
        if sv.get("status") != "ok":
            return {"status": "fail", "note": f"desc_save 失败：{str(sv)[:150]}"}
        await ensure_desc_closed(session)
        return {"status": "ok", "note": text_note}

    # 预热的规划按【URL 集合是否一致】决定能否复用：预热是照 raw.json 的 descImages
    # 出的计划，而认领后描述区挂的就是那批 1688 外链（desc_save 的校验项「仍有外链图
    # 未转存」即此，2026-08-23 真站取证）。但上面的文字模块处理可能删掉模块、页面也
    # 可能被人动过，故不能假定两边一定相同——集合对不上就现场重出计划，pos 一律以
    # mods 为准（预热给的 pos 是源顺序，与页面序号可能差一截）。
    pre = await stages_prewarm_access._await_prewarm(ctx, "desc")
    plan = None
    if pre and pre.get("plan"):
        page_urls = {m.get("url") for m in mods if m.get("url")}
        pre_urls = set()
        for p in pre["plan"].get("replace") or []:
            pre_urls.add(p.get("url"))
        for m in stages_prewarm_plans._desc_modules_from_raw(ctx["workdir"]):
            pre_urls.add(m["url"])
        if page_urls and page_urls <= pre_urls:
            plan = stages_prewarm_plans._replan_desc_by_url(pre["plan"], mods)
            logger.info(f"描述图规划沿用提前预热的结果（{len(mods)} 张按页面序号重挂）")
        else:
            logger.info("页面描述图与预热时不一致（源图有增减），现场重出规划")
    if plan is None:
        plan = await vision.plan_desc(mods, info_for_desc)
    deleted, replaced = 0, 0
    if plan["delete"]:
        d = await desc_delete(session, plan["delete"])
        if d.get("status") != "ok":
            return {"status": "fail", "note": f"删描述模块失败：{str(d)[:150]}"}
        deleted = len(d.get("deleted") or [])
    # 【英化产物按源 URL 落盘复用】gpt-image-2 每张都是一次生图调用，是本阶段最贵的
    # 一步。而 ⑬ 的成果只活在未保存的表单里，save 没成功就得整段重跑（见模块头
    # 「状态文件记的是跑过」那段）——重跑时若连图也重新生成，等于白烧一遍生图钱。
    # 故产物落 desc-edit/<url哈希>-en.jpg，存在且质检过就直接复用。
    # 缓存命中不再重复质检：check_cleaned 也是一次视觉调用，而落盘的前提就是它已通过。
    #
    # 【备料与替换分两段】生图不碰页面，替换必须逐张串行（要现查 pos）。故先并发把
    # 所有产物烧好（_prewarm_desc_images），下面的循环里每张都已在本地，只剩定位与
    # 替换两个页面动作。原先两者写在同一循环里，生图被迫串行——单张约 35s，是本阶段
    # 耗时的主体。备料放在 desc_delete 【之后】：删掉的图不在 replace 计划里，不会白烧；
    # 而备料不动页面，删除后的 pos 漂移对它没有影响。
    #
    # 【备料前必须按页面实况筛一遍】原实现「先定位再出图」不只是为了拿准 pos，还是一道
    # 省钱闸：源图已不在描述区时直接跳过，省掉一次 gpt-image-2 调用。前移备料若不筛，
    # 这道闸就失效了（tests/test_publish_service.py 的「源图已不在描述区时不生图」正是
    # 为此把守）。这里用刚读到的 mods 过滤——它就是页面实况，不必再多发一次 desc_map；
    # 下面循环里每张仍会各自重查一次 pos（删除后序号会前移，见 _resolve_desc_pos）。
    live_urls = {m.get("url") for m in mods if m.get("url")}
    to_prepare = [r for r in plan["replace"] if r.get("url") in live_urls]
    if len(to_prepare) != len(plan["replace"]):
        skipped_n = len(plan["replace"]) - len(to_prepare)
        logger.info(f"描述图备料跳过 {skipped_n} 张：源图已不在描述区（省掉同等次数的生图调用）")
    prepared = await stages_description_images._prewarm_desc_images(ctx["workdir"], to_prepare, emit)

    reused, upscaled = 0, 0
    # 【替换轮抽成闭包，为的是失败后能整轮重试】2026-08-28 实测（pdd-994437651298）：
    # 6 张计划替换的图【全部】卡在同一个页面遮挡上（「更换图片」链接被 fixed 顶栏压住），
    # 于是 6 张 1688 原图原样留在描述区 —— desc_save 回读既报「外链未转存」又报 6 张
    # 1200x1200 破了服装硬红线 1340x1785，而流程照旧放行，最后带着这些图发了出去。
    # 遮挡本身已在 pipeline 侧修（见 _desc_aim_replace_link），但那类页面态问题不可能
    # 一次穷尽；本项目的取向是「靠自动重试保数据可靠性，不加人工卡点」，故这里补一轮
    # 重试：重试前先关掉编辑器再重开，让面板与滚动位置回到初始态（同一态下重试没意义）。
    failed_urls: list = []
    # fatal 指「页签被导航走」这类对后续每一张都成立的错误。它必须抑制重试：页面已经
    # 不在编辑页，重开编辑器与重试都无从下手，只会把 test_fatal时中断整段而不是逐张重试
    # 盯着的那种噪音再来一遍（2026-08-24 实测 890843533224 的 7 条同样报错）。
    fatal_hit = False

    async def _replace_round(items: list) -> None:
        """把 items（plan["replace"] 的子集）逐张替换，累计计数并记下失败的源 URL。"""
        nonlocal replaced, reused, upscaled, fatal_hit
        failed_urls.clear()
        for rep_i, rep in enumerate(items):
            # 【先定位再替换】计划里的 pos 是删图前的序号，必须按源 URL 现查当前序号
            # （见 _resolve_desc_pos）。定位到 desc_replace 之间不会再有页面操作，故这个
            # 序号仍然有效；真有漂移也由 desc_replace 的 expect_url 闸门拦住。
            pos = rep["pos"]
            cur_pos, perr, fatal = await stages_description_images._resolve_desc_pos(session, rep["url"])
            if perr:
                # 【页签被导航走时立刻收工，别逐张重试】2026-08-24 实测（890843533224）：
                # 替换到第 11 张时页签被另一个进程导航去了草稿列表，剩下 7 张各自重试一次、
                # 各报一条同样的「定位失败」——7 条噪音掩盖了唯一的根因，而且每次重试都
                # 要重开编辑器、白等一轮。这类错误对后续每一张都成立，逐张试没有意义。
                if fatal:
                    fatal_hit = True
                    await emit({"type": "manual_check", "stage": "desc",
                                "message": f"{perr}；本阶段剩余 "
                                           f"{len(items) - rep_i}"
                                           f" 张全部保留，请恢复编辑页后从 desc 阶段续跑"})
                    break
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"原第 {pos} 张定位失败（保留原图）：{perr}"})
                continue
            if cur_pos != pos:
                logger.info(f"描述图序号前移：计划 pos {pos} -> 当前 pos {cur_pos}"
                            f"（已删 {deleted} 张）")
            # 报给人看的序号一律用当前序号，前移过的额外标出计划序号——只报计划 pos 会
            # 让人按它去页面上数图，数到的是另一张
            tag = (f"第 {cur_pos} 张" if cur_pos == pos
                   else f"第 {cur_pos} 张（计划 pos {pos}）")
            # 备料结果按源 URL 取（与 _desc_cache_paths 同一个键）。缺项出现在备料轮整个
            # 没跑起来、或该图当时不在页面上（被上面的 live_urls 筛掉）——两种情况都已经
            # 走到「定位成功」这一步了，说明此刻它确实在页面上，就地补一张即可。
            # 补料复用同一个函数，不另写一套逻辑（另写必然与备料轮漂移）。
            got = prepared.get(rep["url"])
            if got is None:
                got = await stages_description_images._prepare_desc_image(ctx["workdir"], rep)
            if not got.get("ok"):
                # 【图片下载失败时检查尺码上下文】2026-09-02：单张描述图下载失败（如404）
                # 不应让整个商品失败。如果该图可能是尺码表且已有文本或实测尺寸，提示影响较小。
                why = got.get('why') or '未知原因'
                hint = ""
                if "下载" in why or "404" in why:
                    # 检查是否有尺码上下文：descText 或 sizeMeasurements 存在时，
                    # 单张图失败的影响较小（尺码信息已从其他渠道获取）
                    has_size_context = (info_for_desc.get("descText") or
                                       info_for_desc.get("sizeMeasurements"))
                    if has_size_context:
                        hint = "；已有文本尺码信息，影响较小"
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{tag}{why}（保留原图）{hint}"})
                continue
            out_img = got["path"]
            how = got.get("how")
            if how == "cached":
                reused += 1
                logger.info(f"描述图{tag}复用已有英化产物：{os.path.basename(out_img)}")
            elif how == "upscaled":
                upscaled += 1
                logger.info(f"描述图{tag}按尺寸放大：{got.get('note')}")
            rr = await desc_replace(session, cur_pos, out_img, expect_url=rep["url"])
            if rr.get("status") == "ok":
                replaced += 1
            else:
                failed_urls.append(rep["url"])
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{tag}替换失败（保留原图）：{str(rr)[:120]}"})

    await _replace_round(plan["replace"])
    if failed_urls and not fatal_hit:
        # 【重开编辑器再重试】遮挡与选中态都是页面态，同一态下重试必然同样失败：
        # 2026-08-28 那次 6 张各自「重点一次 + 轮询 3.2s」全打在顶栏上，白等 40s。
        # 关掉重开会丢掉未保存的改动，故【必须先 desc_save 把已换成的落下来】。
        retry = [r for r in plan["replace"] if r["url"] in set(failed_urls)]
        logger.info(f"描述图有 {len(retry)} 张未替换成功，先保存已换成的再重开编辑器重试一轮")
        await emit({"type": "log", "stage": "desc",
                    "message": f"{len(retry)} 张未替换成功，重开描述编辑器重试一轮"})
        keep = await desc_save(session)
        if keep.get("status") == "error":
            # 保存不了就别关编辑器（关掉等于丢弃已换成的那几张），直接放弃重试
            logger.warning(f"重试前保存失败，放弃重试：{str(keep)[:150]}")
        else:
            closed = await ensure_desc_closed(session)
            if closed.get("status") != "ok":
                logger.warning(f"重试前关闭编辑器失败，仍尝试重试：{closed.get('reason')}")
            await _replace_round(retry)

    # ---- keep 的图也要转存到店小秘图床 --------------------------------------
    # 【这是「仍有外链图未转存」的真正成因】认领时平台按外链原样挂 1688 图，只有被
    # 替换过的才落图床；判 keep 的图从来没人动过，于是 desc_save 每轮都报外链未转存
    # （2026-08-30 实测：10 张描述图全在 cbu01，而当轮只删 3 换 1）。放在替换轮之后：
    # 替换过的图已经在图床上，这里只捡剩下的 keep 图，不重复劳动。
    # fatal_hit（页签被导航走）时跳过——页面已经不在编辑页，转存同样无从下手。
    rehosted = 0
    if not fatal_hit:
        try:
            rh = await stages_description_images._rehost_desc_keeps(ctx, session, mods, plan.get("keep") or [],
                                         emit)
            rehosted = rh.get("done") or 0
            if rh.get("failed"):
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{len(rh['failed'])} 张 keep 的描述图未能转存"
                                       f"（保留 1688 外链）：{str(rh['failed'][:2])[:150]}"})
        except Exception as e:
            # best-effort：转存不成只是外链留着（保存时是告警而非硬错误），
            # 绝不能让它把已经换好的那些图连坐掉
            logger.warning(f"描述图转存异常（保留外链，继续收尾）：{e}")

    if not deleted and not replaced and not rehosted:
        # 【这条早退路径也必须先关编辑器】2026-08-28 实测（1014675972015 仿真花）：
        # 9 张描述图全部替换失败（描述专属菜单未展开），走到这里 return skipped，
        # 把描述编辑器【留在页面上】。它是全屏 modal，于是 ⑭ save 点下去后：
        #   - 页面弹的是描述编辑器自己的「Temu产品描述批量操作/保存/关闭」弹窗，
        #     按钮文案不是 save 预期的「继续编辑」，关不掉、遮罩一直挡着；
        #   - 那条「错误：产品信息中有错误，请检查」toast 也是编辑器弹的，
        #     而 save 只看 .ant-form-item-explain-error，抓不到它；
        # 最后 save 报出「无校验错误但草稿更新时间未变」这种查不下去的结论，整单未落库。
        # 下方正常路径的注释早就写明「关编辑器必须在所有 return 之前」，只有这一条
        # 早退漏了——一张都没换成时恰恰最需要关（编辑器一定是开着的）。
        closed = await ensure_desc_closed(session)
        if closed.get("status") != "ok":
            await emit({"type": "manual_check", "stage": "desc",
                        "message": f"描述编辑器未能关闭（会挡住 ⑭ 保存）："
                                   f"{closed.get('reason')}"})
        return {"status": "skipped", "note": f"{len(mods)} 张全部保留"}
    s = await desc_save(session)
    # 【关编辑器必须在所有 return 之前】它是全屏 modal，开着会盖住整个编辑页，后续
    # ⑦⑧⑩⑪⑭ 全部点不中，且报的是「瞄点未命中」——看着像时序问题，实际是被遮住
    # （2026-08-24 实测：阶段⑦ 连续两次 open-space 失败，诊断才发现瞄点落在描述弹窗的
    # .page-content 上）。下面尺寸破线那条会让本阶段 fail，而 fail 后同一浏览器会话
    # 仍可能被续跑复用，故不能把关闭动作留在 return 之后。
    closed = await ensure_desc_closed(session)
    if closed.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "desc",
                    "message": f"描述编辑器未能关闭（会挡住后续阶段的点击）："
                               f"{closed.get('reason')}"})
    if s.get("status") == "validation-error":
        # 外链未转存与尺寸不达标是两回事，分别报出来——只说「外链」会让人以为
        # 尺寸没问题（本商品实际是尺寸那条，见 desc_save 的回读注释）
        parts = []
        if s.get("foreignHosts"):
            parts.append(f"仍有外链图未转存：{s['foreignHosts']}")
        if s.get("tooSmall"):
            parts.append(f"仍有图不符合描述图要求（比例 {images.DESC_RATIO_MIN}~"
                         f"{images.DESC_RATIO_MAX}、两边 >= {images.DESC_MIN_W}）："
                         f"{s['tooSmall']}")
        # 【把成因一起报出来】替换失败会连带这两条：本阶段每有一张图没换成，它的
        # 1688 原始外链就还挂在页面上，「未转存」与「尺寸破线」于是同时出现
        # （2026-08-26 那批：3 张失败 → 恰好 3 张破线 + cbu01.alicdn.com 外链）。
        # 只报校验结果会让人去查转存链路，而真正要看的是上面那几条替换失败的原因。
        #
        # 【但「未转存」不止这一个成因】判 keep 的图本来也全是 1688 外链，与替换成败
        # 无关（2026-08-30 取证，见 _rehost_desc_keeps）。那条链路已在上面补了转存，
        # 故这里的归因只在【确有替换失败】时才给，替换全成功却仍报外链时不要乱指方向。
        if kept_original := len(plan["replace"]) - replaced:
            parts.append(f"根因很可能是本阶段有 {kept_original} 张图未替换成功"
                         f"（它们的 1688 原始外链仍在页面上），请看上面各张的失败原因")
        elif s.get("foreignHosts"):
            parts.append("本阶段计划内的图都已处理完，外链应来自 keep 图转存未成功"
                         "，请看上面的转存失败原因")
        await emit({"type": "manual_check", "stage": "desc",
                    "message": "描述保存后 " + ("；".join(parts) or str(s)[:120])})
        # 【尺寸不合规必须让本阶段 fail，不能只报一条提示就放行】2026-08-28 实测
        # （pdd-994437651298）：6 张图不合规时阶段⑬ 照样返回 ok，一路走到「立即发布」
        # 并按列表取证判定发布成功——等于带着不合规的图发了出去，而这条闸门是保存时
        # 才静默校验的，发出去迟早被弹回。
        # 【判据是描述图自己那套，不是服装 1340x1785】2026-08-28 用户截图取证后改。
        # 原先套服装红线，1000x1000（合格）被判 fail，整单卡死且每跑白烧一轮生图。
        # 外链未转存【不】单独当硬错误：desc_save 的注释已说明，本商品可能本来就没
        # 替换过描述图，外链是采集时的原始状态，那种情况不该拖垮整个商品。
        if s.get("tooSmall"):
            return {"status": "fail",
                    "note": f"{len(s['tooSmall'])} 张描述图不符合要求"
                            f"（宽高比 {images.DESC_RATIO_MIN}~{images.DESC_RATIO_MAX}、"
                            f"两边 >= {images.DESC_MIN_W}）："
                            f"{str(s['tooSmall'])[:120]}"}
    elif s.get("status") != "ok":
        return {"status": "fail", "note": f"desc_save 失败：{str(s)[:150]}"}
    return {"status": "ok", "note": _desc_note(deleted, replaced, text_note,
                                               upscaled, reused, rehosted)}


def _desc_note(deleted: int, replaced: int, text_note: str,
               upscaled: int, reused: int, rehosted: int = 0) -> str:
    """拼阶段⑬ 的结论文案（抽出来只为让 _st_desc 的收尾路径不重复这段拼接）。"""
    note = f"删 {deleted} 张 / 替换 {replaced} 张"
    if rehosted:
        note += f" / 转存 {rehosted} 张"
    if text_note:
        note += f" | {text_note}"
    extra = []
    if upscaled:
        extra.append(f"{upscaled} 张仅放大未动画面")
    if reused:
        extra.append(f"{reused} 张复用已有产物，省了生图")
    if extra:
        note += "（" + "；".join(extra) + "）"
    return note
