"""店小秘发布操作：media.description。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
import os
from app.logger import logger
from app.publish import images
from app.publish.browser import BrowserSession, J
from app.publish.media import description_scripts as media_description_scripts


async def _desc_ensure_open(session: BrowserSession) -> dict:
    """确保描述编辑器已打开且只有一层，返回模块状态。

    所有描述写操作的前置：既保证编辑器在，又顺带做「不在编辑页」的早失败。

    【「没有编辑描述按钮」必须报出当前 URL】2026-08-24 实测（890843533224）：⑬ 逐张
    替换到第 11 张时，本进程的页签被【另一个进程】导航去了草稿列表，此后每一张都报
    「当前页面没有「编辑描述」按钮」——报错本身没错，但它把「页面被换走了」说成
    「可能不在编辑页」，五条一模一样的信息看不出是同一个外部原因，只会以为是选择器
    失效。eval 能跑通说明页签活着（真关了会抛 TargetClosedError），故区别只在 URL：
    带上它，「被导航走」与「编辑页结构变了」当场就能分开。
    """
    st = await session.eval_json(media_description_scripts._JS_DESC_STATE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    if not st.get("open"):
        if not st.get("hasButton"):
            url = ""
            try:
                url = session.page.url or ""
            except Exception:
                pass          # 只是错误信息的补充，取不到不影响判定
            if url and "popTemu/edit" not in url:
                return {"err": f"页签已不在编辑页（当前 {url}），"
                               "描述阶段无法继续。常见原因：另一个发布/采集进程"
                               "共用了这个页签并把它导航走了",
                        "url": url, "navigatedAway": True}
            return {"err": "当前页面没有「编辑描述」按钮，可能不在编辑页"
                           + (f"（当前 {url}）" if url else ""),
                    "url": url}
        opened = await session.eval_json(media_description_scripts._JS_DESC_OPEN)
        if opened.get("err") or not opened.get("opened"):
            logger.warning(f"描述编辑器打开失败：{opened}")
            return {"err": opened.get("err") or "编辑器打开失败（等待10秒后未出现，检查页面是否仍在编辑页）", "detail": opened}
        st = await session.eval_json(media_description_scripts._JS_DESC_STATE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    if (st.get("modalCount") or 0) > 1:
        extra = await session.eval_json(media_description_scripts._JS_DESC_CLOSE_EXTRA)
        logger.info(f"关掉多余的描述编辑器实例：{extra}")
        st = await session.eval_json(media_description_scripts._JS_DESC_STATE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    return st


async def desc_map(session: BrowserSession, info_path: str = "") -> dict:
    """阶段⑪ 只读：列出描述模块（序号 + URL + 是否已落店小秘图床）。

    序号从 1 起，与 desc_delete / desc_replace 的入参一致。
    info_path 给了就顺带把 product-info.json 里的 complianceNotes 带出来，
    供调用方（或人）按「哪张该删」对照——判断本身不在这里做，这个函数保持只读。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    # 尺寸一并带出：服装类下限 1340×1785 是硬红线，而「内容干净」与「尺寸达标」
    # 是两回事——一张干净的 900×1200 商品图内容上该 keep，却过不了保存校验
    # （2026-08-23 真站取证：描述区 10 张 1688 外链全部 1000×1000 / 900×1200）。
    # naturalWidth 为 0 表示还没加载完，按「读不到」处理，不当成不达标。
    sizes = st.get("sizes") or []
    mods = []
    for i, src in enumerate(st.get("srcs") or [], 1):
        wh = sizes[i - 1] if i - 1 < len(sizes) else None
        w, h = (wh or [0, 0])[:2]
        m = {"pos": i, "url": src, "onDxmHost": "dianxiaomi.com" in src}
        if w and h:
            m["size"] = f"{w}x{h}"
            # 描述图按【描述图自己的规则】判，不套服装 SKC 的 1340x1785
            # （见 images.check_desc_size 上方的截图取证）。键名沿用 tooSmall：
            # 下游（_replan_desc_by_url、_st_desc）都按它决定要不要重做。
            chk = images.check_desc_size(w, h)
            m["tooSmall"] = chk["ok"] is False
            if chk["ok"] is False:
                m["sizeReasons"] = chk["reasons"]
        mods.append(m)
    out = {"status": "ok", "count": len(mods), "modules": mods}
    if info_path and os.path.exists(info_path):
        try:
            with open(info_path, encoding="utf-8") as f:
                info = json.load(f)
            notes = info.get("complianceNotes") or {}
            out["complianceNotes"] = notes
            out["cleanFiles"] = notes.get("cleanFiles")
        except Exception as e:
            # 只是附带信息，读不到不影响主结果
            logger.warning(f"读 complianceNotes 失败：{e}")
    return out


async def desc_delete(session: BrowserSession, positions: list) -> dict:
    """阶段⑪ 删除指定序号的描述模块（序号从 1 起）。

    【倒序删除】positions 去重后从大到小删：删掉一个后 data-idx 会重排，
    从后往前删则每次只影响比它更大的下标，已处理过的不受影响。正序删会错位。

    【入参是图片序号，不是列表下标】pos 与左侧列表的 data-idx 在图文混排时不相等，
    删除前先建映射（见 _JS_DESC_IDX_MAP 里 2026-08-24 的实测记录）。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    n = st.get("count") or 0
    want = sorted({int(p) for p in positions}, reverse=True)
    bad = [p for p in want if p < 1 or p > n]
    if bad:
        # 先整体校验再动手：删一半才发现越界会留下难以判断的中间态
        return {"status": "error", "stage": "precheck",
                "err": f"序号越界 {bad}（当前共 {n} 个模块）"}

    # pos 是「第几张图」，data-idx 是左侧列表下标，图文混排时两者不等（见
    # _JS_DESC_IDX_MAP）。映射一次全批复用：倒序删只影响比它大的 data-idx，
    # 前面待删项的映射不受影响。
    mp = await session.eval_json(media_description_scripts._JS_DESC_IDX_MAP.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    if mp.get("err"):
        return {"status": "error", "stage": "idxmap", **mp}
    idx_map = mp.get("map") or []
    if len(idx_map) != n:
        # 对不上说明页面结构与预期不符，宁可不动手：错位删除会删掉不该删的图
        return {"status": "error", "stage": "idxmap",
                "err": f"图片模块数 {len(idx_map)} 与描述图数 {n} 不一致，"
                       f"拒绝删除以免错位", "detail": mp}

    log = []
    for pos in want:
        r = await session.eval_json(
            media_description_scripts._JS_DESC_DELETE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                           .replace("__IDX__", str(idx_map[pos - 1])))
        if r.get("err") or not r.get("deleted"):
            return {"status": "error", "stage": f"delete-pos{pos}",
                    "dataIdx": idx_map[pos - 1], "detail": r, "done": log}
        log.append({"pos": pos, "dataIdx": idx_map[pos - 1], "left": r.get("after")})
        await asyncio.sleep(0.4)

    final = await session.eval_json(media_description_scripts._JS_DESC_STATE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    return {"status": "ok", "deleted": [x["pos"] for x in log],
            "countBefore": n, "countAfter": final.get("count"), "log": log}


async def desc_text_map(session: BrowserSession) -> dict:
    """阶段⑬ 只读：列出描述区的【文字模块】（data-idx + 文本）。

    与 desc_map 分开：那个按 .desc-img-box img 枚举、序号是「第几张图」；
    文字模块不含图片盒子，用 data-idx 直接寻址，两套序号互不干扰
    （混用会错位，见 _JS_DESC_IDX_MAP 里 2026-08-24 删错图的实测记录）。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    r = await session.eval_json(
        media_description_scripts._JS_DESC_TEXT_MAP.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL))
    if r.get("err"):
        return {"status": "error", **r}
    items = r.get("items") or []
    return {"status": "ok", "count": len(items), "texts": items,
            "modCount": r.get("modCount")}


async def desc_text_delete_all(session: BrowserSession) -> dict:
    """阶段⑬ 删掉描述区【全部文字模块】。

    【为什么不再按内容分类】2026-09-01 用户要求描述区所有文字板块一律移除。此前是
    交 LLM 判「采集残留 JSON 删 / 尺码对照表英化保留」，现在不留保留分支：尺码信息
    已由阶段⑧ 的平台尺码表承载，描述区那份纯文本只是重复与中文残留来源。

    【倒序删除】data-idx 在删除后会重排，从大到小删则每次只影响比它更大的下标，
    已处理过的不受影响（正序删会错位，同 desc_delete）。

    单项失败不拖垮整体（best-effort）：记进 failed 继续，交调用方决定是否告警。
    描述文字不是必填项，删不掉就保留原文，比中断整个商品划算。

    返回里带上枚举到的 texts 原文：调用方要拿它做尺码取证（见 service._st_desc 的
    _size_evidence）。放在这里返回而不是让调用方自己再 desc_text_map 一次，是为了
    省掉一次页面 eval——删除后模块就没了，事后补读不到。
    """
    tm = await desc_text_map(session)
    if tm.get("status") != "ok":
        return {"status": "error", **tm}
    texts = [t for t in (tm.get("texts") or []) if str(t.get("idx") or "").strip()]
    idxs = sorted({str(t.get("idx")) for t in texts},
                  key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
    if not idxs:
        return {"status": "ok", "deleted": [], "failed": [], "found": 0,
                "texts": []}

    deleted, failed = [], []
    for idx in idxs:
        r = await session.eval_json(
            media_description_scripts._JS_DESC_DELETE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                           .replace("__IDX__", idx))
        if r.get("err") or not r.get("deleted"):
            failed.append({"idx": idx, **r})
            logger.warning(f"文字模块 idx={idx} 删除失败：{r.get('err') or r}")
        else:
            deleted.append(idx)
    return {"status": "ok" if not failed else "partial",
            "deleted": deleted, "failed": failed, "found": len(idxs),
            "texts": texts}


async def desc_text_apply(session: BrowserSession, plan: list) -> dict:
    """阶段⑬ 按计划处理文字模块：英化改写 / 删除 / 保留。

    plan: [{"idx": "0", "action": "translate"|"delete"|"keep",
            "text": "<translate 时的英文正文>", "reason": "..."}]
    idx 用 desc_text_map 返回的 data-idx 原值。

    【顺序：先全部 translate，再倒序 delete】两个动作都按 data-idx 寻址，而删除
    会让后续 data-idx 重排。先删就会把待翻译项的 idx 改掉，写到别的模块上去。
    删除本身倒序（大→小），理由同 desc_delete。

    单项失败不拖垮整体（best-effort）：记进 failed 继续，交调用方决定是否告警。
    描述文字不是必填项，改不动就保留原文，比中断整个商品划算。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}

    translated, deleted, failed, kept = [], [], [], []

    # 1) 先改写（此时 data-idx 还没被删除动作扰动）
    for p in plan:
        if (p.get("action") or "") != "translate":
            continue
        idx, text = str(p.get("idx")), (p.get("text") or "").strip()
        if not text:
            failed.append({"idx": idx, "err": "translate 但没给 text"})
            continue
        if len(text) > 500:
            text = text[:500]          # 面板上限，超了平台会静默截断
            logger.warning(f"文字模块 idx={idx} 译文超 500 字符，已截断")
        r = await session.eval_json(
            media_description_scripts._JS_DESC_TEXT_SET.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                             .replace("__IDX__", idx)
                             .replace("__TEXT__", J(text)))
        if r.get("status") == "ok" and r.get("filled"):
            translated.append({"idx": idx, "text": text[:60],
                               "reason": p.get("reason", "")})
        else:
            failed.append({"idx": idx, "action": "translate", **r})
            logger.warning(f"文字模块 idx={idx} 改写失败：{r.get('reason') or r}")

    # 2) 再倒序删除
    dels = sorted({str(p.get("idx")) for p in plan
                   if (p.get("action") or "") == "delete"},
                  key=lambda x: int(x) if str(x).isdigit() else 0, reverse=True)
    for idx in dels:
        r = await session.eval_json(
            media_description_scripts._JS_DESC_DELETE.replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                           .replace("__IDX__", idx))
        if r.get("err") or not r.get("deleted"):
            failed.append({"idx": idx, "action": "delete", **r})
            logger.warning(f"文字模块 idx={idx} 删除失败：{r.get('err') or r}")
        else:
            deleted.append(idx)

    kept = [str(p.get("idx")) for p in plan if (p.get("action") or "") == "keep"]
    return {"status": "ok" if not failed else "partial",
            "translated": translated, "deleted": deleted,
            "kept": kept, "failed": failed}


async def desc_save(session: BrowserSession) -> dict:
    """阶段⑪ 保存描述编辑器的改动。

    注意这只保存【描述编辑器】，整个商品还要再走一次阶段⑫ save 才落库。

    返回 status="validation-error" 而不是 error 的情形：保存后编辑页描述区仍有非
    店小秘图床的外链图。那说明这些图没被平台转存，发布时可能被拦——但也可能是本商品
    本来就没替换过描述图（外链是采集时的原始状态），故不当硬错误、交调用方判断。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    r = await session.eval_json(media_description_scripts._JS_DESC_SAVE
                                .replace("__MODAL__", media_description_scripts._JS_DESC_MODAL)
                                .replace("__MINW__", str(images.DESC_MIN_W))
                                .replace("__MINH__", str(images.DESC_MIN_H))
                                .replace("__RMIN__", str(images.DESC_RATIO_MIN))
                                .replace("__RMAX__", str(images.DESC_RATIO_MAX)))
    if r.get("err"):
        return {"status": "error", "stage": "save", **r}
    if r.get("stillOpen"):
        killed = await session.kill_stuck_modals()
        r["killedStuck"] = killed.get("removed")
        return {"status": "error", "stage": "close", "detail": r}
    if not r.get("descImgs"):
        return {"status": "error", "stage": "readback",
                "err": "保存后编辑页描述区没有图片", "detail": r}
    all_hosted = r["descImgs"] == r["dxmHosted"]
    small = r.get("tooSmall") or []
    notes = []
    if not all_hosted:
        notes.append("仍有非店小秘图床的外链图，发布可能被拦")
    if small:
        notes.append(f"仍有 {len(small)} 张图不符合描述图要求"
                     f"（宽高比 {images.DESC_RATIO_MIN}~{images.DESC_RATIO_MAX}、"
                     f"两边 >= {images.DESC_MIN_W}）")
    return {"status": "ok" if (all_hosted and not small) else "validation-error",
            "descImgs": r["descImgs"], "dxmHosted": r["dxmHosted"],
            "foreignHosts": r.get("foreignHosts"), "tooSmall": small,
            "note": "；".join(notes)}


async def ensure_desc_closed(session: BrowserSession) -> dict:
    """确认描述编辑器已关闭；没关就点「关闭」（含二次确认）。

    【为什么必须单独有这一步】描述编辑器是全屏 modal（实测 2560×1257），开着时它盖住
    整个编辑页，后续所有靠坐标点击的阶段全部失效——而失败信息是「瞄点未命中」，看着
    像滚动时序问题。2026-08-24 实测：跑完阶段⑬ 后编辑器留着，阶段⑦ SKC 换图连续两次
    报 open-space 失败，诊断才发现瞄点落在描述弹窗的 .page-content 上。

    不点「保存」：这里只负责关，改动该不该落库由调用方在 desc_save 里决定。
    也不用 kill_stuck_modals 暴力 remove——那会把正常打开、内容未保存的编辑器一起
    掀掉；只有点「关闭」走不通时才交由调用方去做那种兜底。
    """
    st = await session.eval_json(media_description_scripts._JS_DESC_CLOSE_IF_OPEN)
    if st.get("already"):
        return {"status": "ok", "wasOpen": False}
    if st.get("err"):
        return {"status": "error", "reason": st["err"]}
    if st.get("stillOpen"):
        return {"status": "error", "reason": "点了关闭但编辑器仍开着",
                "visibleMasks": st.get("visibleMasks")}
    return {"status": "ok", "wasOpen": True,
            "visibleMasks": st.get("visibleMasks")}
