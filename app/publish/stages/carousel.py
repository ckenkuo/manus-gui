"""店小秘发布共用能力：stages.carousel。各来源流程由 workflows/ 独立定义。"""

import asyncio
import hashlib
import json
import os
import shutil
from app.logger import logger
from app.publish import extract, images, preferences, state, vision
from app.publish.browser import BrowserSession
from app.publish.media import space as media_space
from app.publish.media.carousel import (
    CAROUSEL_MAX_BYTES,
    CAROUSEL_MAX_PICKED,
    CAROUSEL_MIN_PICKED,
    CAROUSEL_MIN_SIDE,
    carousel_state,
    expand_carousel_pool,
    open_carousel_space,
    toggle_carousel,
)
from app.publish.upload import upload_many


# 单张生图超时（实测一张约 35s），与 ⑤b 的 CLEAN_TIMEOUT 同值：同一种「curl 生图」
# 调用、同一类图，没有理由在这里另定一套。
EN_TIMEOUT = 90


# ---- 候选池 → 本地文件 -------------------------------------------------------



async def _resolve_pool(items: list, prep: str, emit) -> dict:
    """下载页面实际图片，避免本地已英化文件与页面原图内容不一致。"""
    out, missing = {}, list(items)

    if missing:
        conc = preferences.get_image_concurrency()
        sem = asyncio.Semaphore(conc)

        async def _one(it: dict) -> tuple:
            name = f"pool-{it['i']:02d}.jpg"
            dst = os.path.join(prep, name)
            async with sem:
                try:
                    n = await asyncio.to_thread(extract._download_image, it["url"], dst)
                except Exception as e:
                    return it["i"], None, f"下载异常：{e}"[:80]
            if not n:
                return it["i"], None, "源站取不到（404 等）"
            return it["i"], {"file": name, "path": dst, "note": None,
                             "url": it.get("url") or ""}, ""

        logger.info(f"轮播候选：下载 {len(missing)} 张页面实际图片（并发 {conc}）")
        got = await asyncio.gather(*(_one(it) for it in missing))
        failed = 0
        for i, entry, err in got:
            if entry:
                out[i] = entry
            else:
                failed += 1
                logger.warning(f"轮播图第 {i + 1} 张源图取不到（{err}），本张不参与判定")
        if failed:
            await emit({"type": "log", "stage": "carousel",
                        "message": f"{failed} 张候选源图取不到（404 等），"
                                   "本张既不补勾也不参与质检"})
    return out


# ---- 备料：英化 / 合规化 -----------------------------------------------------

def _en_cache_path(workdir: str, url: str) -> str:
    """英化产物的落盘路径，按【源 URL 哈希】命名。

    【为什么不用格子上标】下标会随「新图插入候选列表」整体后移，同一个下标下次可能是
    另一张图；源 URL 是稳定标识。这与 ⑬ 的 _desc_cache_paths 同一理由。

    【必须与临时目录分开】carousel/ 是每次跑都整个清掉的备料目录，缓存产物落在那里面
    等于每轮都当缓存未命中、把同一张图重烧一遍生图。故另起 carousel-edit/，与 ⑬ 的
    desc-edit/ 同构。
    """
    h = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(workdir, "carousel-edit", f"{h}-en.jpg")


async def _english_one(local_path: str, url: str, workdir: str,
                       sizechart: bool = False) -> dict:
    """只生图一次；产物替换到页面后再质检，缓存也必须经过页面复检。"""
    out = _en_cache_path(workdir, url)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        sz = images.image_size(out)
        if sz and sz[0] == sz[1] and min(sz) >= CAROUSEL_MIN_SIDE:
            return {"ok": True, "path": out, "how": "cached"}
        logger.warning(f"轮播图英化产物尺寸已不合规（{sz}），当缓存未命中重做")
    prompt = (images.SIZECHART_TRANSLATE_PROMPT if sizechart
              else images.DEFAULT_TRANSLATE_PROMPT)
    prompt += " 将完整内容排入1:1画布，保留全部尺码、数值、单位和商品介绍，不裁掉信息。"
    try:
        ed = await asyncio.to_thread(
            images.edit_image, local_path, prompt=prompt, out_path=out,
            size="2048x2048", timeout=EN_TIMEOUT)
    except Exception as e:
        return {"ok": False, "why": f"英化失败：{e}"[:150]}
    return {"ok": True, "path": ed["output"], "how": "edited"}


def _to_carousel_size(path: str, out_path: str, preserve_info: bool = False) -> str:
    """把一张图收口到轮播图的尺寸口径：1:1、≥800、≤2M。返回产物路径。

    preserve_info 使用补白保留完整内容；生图产物本身已是 1:1，走这里只是复核。
    体积那条是页面原文「大小在2M以内」，
    此前全链路无人校验（upload_image 只查尺寸），超了就降 quality 重存一次。
    """
    sz = images.image_size(path)
    if not sz:
        return ""
    out = path
    if sz[0] != sz[1] or min(sz) < CAROUSEL_MIN_SIDE:
        if preserve_info:
            from PIL import Image, ImageOps
            with Image.open(path) as source:
                side = max(*source.size, images.MATERIAL_TARGET)
                square = ImageOps.pad(source.convert("RGB"), (side, side),
                                      method=Image.Resampling.LANCZOS, color="white")
                square.save(out_path, quality=85)
            out = out_path
        else:
            out = images.square_image(path, out_path=out_path)["output"]
    if os.path.getsize(out) > CAROUSEL_MAX_BYTES:
        # 下限传轮播图自己的口径：套服装闸门会把 2048 的产物先放大再缩，白糊一次
        out = images.compress(out, quality=70,
                              min_w=CAROUSEL_MIN_SIDE, min_h=CAROUSEL_MIN_SIDE)
        if os.path.getsize(out) > CAROUSEL_MAX_BYTES:
            logger.warning(f"轮播图产物压缩后仍超 2M：{out}")
            return ""
    return out


# ---- 挑补勾的信息图 ----------------------------------------------------------

def _pick_adds(cands: list, pool: dict, verdicts: dict, picked: list,
               add_max: int) -> list:
    """按信息价值挑图，仅去除内容完全相同的文件，避免漏掉相似版式中的不同参数。
    """
    ranked = []
    for it in cands:
        entry = pool.get(it["i"])
        if not entry:
            continue
        v = verdicts.get(entry["file"])
        if not v or not v.get("isInfo"):
            continue
        ranked.append((int(v.get("value") or 0), it, entry))
    # 信息价值高的优先（尺码/尺寸 3 > 材质/规格 2 > 其它说明 1）
    ranked.sort(key=lambda t: -t[0])

    def fingerprint(path: str) -> str:
        with open(path, "rb") as source:
            return hashlib.sha256(source.read()).hexdigest()

    chosen = []
    refs = {fingerprint(pool[it["i"]]["path"]) for it in picked if it["i"] in pool}
    for _value, it, entry in ranked:
        if len(chosen) >= add_max:
            break
        digest = fingerprint(entry["path"])
        if digest in refs:
            logger.info(f"轮播图第 {it['i'] + 1} 张是信息图，"
                        "但与已选用的图画面重复，不补勾")
            continue
        chosen.append((it, entry))
        refs.add(digest)
    return chosen


def _kind_of(entry: dict, verdict: dict) -> str:
    """这张图的信息图类目（提示词里给的那个词），两处来路取其一。"""
    return (entry.get("note") or {}).get("kind") or (verdict or {}).get("kind") or ""


# ---- 阶段主体 ---------------------------------------------------------------

async def _st_carousel(ctx: dict, session: BrowserSession, emit) -> dict:
    """补勾关键信息图，逐张英化质检；一次生图替换后复检，失败停止发布交人工。
    """
    # 【等图格渲染这件事只在 carousel_state 里做，本阶段不再补等重读】
    # 首跑（2026-09-12~13 夜间批）这里原本是「supported 为 None 就 sleep 2 秒再读一次」，
    # 但本文件没有 import asyncio，那句 sleep 一执行就是 NameError，被上层吞成阶段错误：
    # 补救分支形同虚设，报错落在第一次读的结果上（4 条 no-carousel-list / 零个图格）。
    # 修的时候没有把 import 补上了事——那样等待会分裂成两处（媒体层 8s 轮询 + 阶段层
    # 2s 死等 + 再来一轮 8s 轮询），既是两处各留一半的折中，「无条件 sleep 2 秒」也正是
    # _poll_until 那段注释要消灭的东西。真正的短板是媒体层的 timeout 给紧了，故只在
    # carousel_state 一处把上限调到位（见那边 docstring 的取证），这里退回单次读取。
    #
    # 【先展开候选列表】池子超过约 21 格时页面把后面的折叠掉、格子压根不渲染（真站实测
    # 21 -> 36），不展开就只能看见前 21 张候选——尺码表/产品介绍图若排在后面，永远
    # 补勾不到，而补勾正是本阶段的职责之一。
    st = await carousel_state(session)
    ex = await expand_carousel_pool(session)
    if ex.get("expanded"):
        await emit({"type": "log", "stage": "carousel",
                    "message": "候选列表有折叠，已展开，按全部候选判定"})
        st = await carousel_state(session)
    if st.get("supported") is None:
        # 证据不足（区块没渲染完/页面改版）：不当成「不支持」静默跳过，如实报出来。
        # 静默跳过的下场就是这次那单——发布时才被拒，回头还得从日志里反推。
        return {"status": "fail",
                "note": f"读不到产品轮播图区（{st.get('err') or '零个图格'}），"
                        "无法核对尺寸"}

    items = st.get("items") or []
    picked = [it for it in items if it.get("checked")]
    cands = [it for it in items if not it.get("checked")]
    replacements_path = os.path.join(ctx["workdir"], "carousel-replacements.json")
    try:
        with open(replacements_path, encoding="utf-8") as source:
            replacements = json.load(source)
        if not isinstance(replacements, dict):
            replacements = {}
    except (OSError, ValueError):
        replacements = {}
    selected_urls = {item["url"] for item in picked}
    cands = [item for item in cands
             if replacements.get(item["url"]) not in selected_urls]

    info = state._load_info(ctx["info_path"]) if ctx.get("info_path") else {}
    prep = os.path.join(ctx["workdir"], "carousel")
    shutil.rmtree(prep, ignore_errors=True)
    os.makedirs(prep, exist_ok=True)
    pool = await _resolve_pool(items, prep, emit)

    # ---- 判定：先挑补勾的信息图，再给出选用图的质检结论 ----
    # 【判定调用失败如实抛出去】判不了＝不知道有没有中文，而中文是硬红线，未知不能
    # 当安全（同 ⑤b「带中文的图不能一路发上真店」）。吞掉异常继续跑等于把「没看」
    # 伪装成「看过且干净」，故这里只补一句上下文再抛，由 service 记 fail 并留现场。
    verdicts = {}
    room = max(0, CAROUSEL_MAX_PICKED - len(picked))
    add_max = room

    async def _scan(targets: list, why: str) -> dict:
        """对一批格子发一次判定，返回 {文件名: 结论}。取不到图的格子不进输入。"""
        entries = [{"file": pool[t["i"]]["file"], "path": pool[t["i"]]["path"]}
                   for t in targets if t["i"] in pool]
        entries = [e for e in entries if os.path.isfile(e["path"])]
        if not entries:
            return {}
        try:
            r = await vision.plan_carousel(entries, info)
        except Exception as e:
            raise RuntimeError(f"轮播图{why}失败：{e}") from e
        return r.get("items") or {}

    if cands:
        verdicts.update(await _scan(cands, "信息图判定"))
    wanted = _pick_adds(cands, pool, verdicts, picked, len(cands))
    adds = wanted[:add_max]
    incomplete = [f"第 {it['i'] + 1} 张候选图未完成识别" for it in cands
                  if it["i"] not in pool or pool[it["i"]]["file"] not in verdicts]
    incomplete.extend(f"第 {it['i'] + 1} 张信息图超出轮播上限，需人工安排"
                      for it, _ in wanted[add_max:])

    target = [{"i": it["i"], "add": False} for it in picked] + \
             [{"i": it["i"], "add": True} for it, _ in adds]
    if not target:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "轮播图区没有可选用的图片，需人工补图"})
        return {"status": "fail", "note": "轮播图区没有可选用的图片，需人工补图"}

    # ---- 备料：脏的英化、不合规的裁方 ----
    ready, unknown, failed_adds, failed_kept = [], incomplete, [], []
    checked_urls = set()
    for t in target:
        i = t["i"]
        entry = pool.get(i)
        tag = f"第 {i + 1} 张"
        if not entry:
            unknown.append(tag)
            continue
        verdict = verdicts.get(entry["file"]) or {}
        try:
            qc = await vision.check_cleaned(entry["path"])
        except Exception as exc:
            unknown.append(f"{tag}英化质检异常：{exc}")
            continue
        dirty = (not qc["clean"] if isinstance(qc.get("clean"), bool)
                 and qc.get("status") != "error" else None)
        kind = _kind_of(entry, verdict)
        sizechart = "尺" in kind
        # 【尺寸判据取本地件的真实像素，不取页面上的 .img-size 文本】文本读不到时是
        # 「未知」，而下载件才是我们真正要上传的那个东西；读不出尺寸同样按未知处理
        # （未知不等于不合格，同 carousel_state 的取向）。
        sz = images.image_size(entry["path"])
        need_size = bool(sz) and (sz[0] != sz[1]
                                  or min(sz) < CAROUSEL_MIN_SIDE)

        if dirty is None:
            unknown.append(tag)
            continue
        # 【补勾的图必须走完】它的目的就是出现在轮播里，干净合规也得上传后勾选；
        # 已选用且干净合规的图才不动它（省一次上传与替换，画面本来就没变）。
        if not sz:
            unknown.append(f"{tag}读不到图片尺寸")
            continue
        if not dirty and not need_size and not t["add"] \
                and os.path.getsize(entry["path"]) <= CAROUSEL_MAX_BYTES:
            checked_urls.add(entry["url"])
            continue

        path = entry["path"]
        if dirty:
            await emit({"type": "log", "stage": "carousel",
                        "message": f"轮播图 {tag} 英化质检未通过"
                                   f"（{qc.get('issues') or ''}），"
                                   "生图英化后替换"})
            en = await _english_one(path, entry["url"], ctx["workdir"], sizechart)
            if not en.get("ok"):
                logger.warning(f"轮播图 {tag} {en.get('why')}")
                await emit({"type": "manual_check", "stage": "carousel",
                            "message": f"轮播图 {tag} 英化失败，保留现场交人工：{en.get('why')}"})
                (failed_adds if t["add"] else failed_kept).append(tag)
                continue
            path = en["path"]
        out_path = os.path.join(prep, f"pic{i:02d}.jpg")
        fixed = _to_carousel_size(path, out_path, preserve_info=True)
        if not fixed:
            await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"轮播图 {tag} 尺寸/体积处理失败，保留原图交人工"})
            (failed_adds if t["add"] else failed_kept).append(tag)
            continue
        ready.append({"i": i, "tag": tag, "path": fixed,
                      "sourceUrl": entry["url"],
                      "oldUrl": None if t["add"] else entry["url"],
                      "add": t["add"]})

    if unknown:
        # 尺寸/中文都判不了（图未加载、源图取不到、模型漏答）的格子不硬判，
        # 但要说出来：它可能藏着不合规或带中文的图，而本阶段确实没依据去动它。
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(unknown)} 项轮播图检查未完成"
                               f"（{'、'.join(unknown[:6])}），"
                               "未通过处理，暂停发布并交人工核对"})

    if failed_adds:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(failed_adds)} 张信息图处理失败、未补勾"
                               f"（{'、'.join(failed_adds[:4])}），需人工补图"})

    if not ready:
        status = "fail" if (failed_kept or failed_adds or unknown
                            or not CAROUSEL_MIN_PICKED <= len(picked) <= CAROUSEL_MAX_PICKED) else "ok"
        note = f"轮播图已选 {len(picked)} 张"
        if unknown:
            note += f"；{len(unknown)} 张读不出结论"
        if failed_kept:
            note = (f"{len(failed_kept)} 张已选轮播图未通过处理"
                    f"（{'、'.join(failed_kept[:4])}），原图仍在选用中，"
                    "发布会被拒，请人工换图")
        elif failed_adds:
            note += f"；{len(failed_adds)} 张信息图未能补勾"
        if not CAROUSEL_MIN_PICKED <= len(picked) <= CAROUSEL_MAX_PICKED:
            note += "；选用数量不满足平台要求，需人工补选/取消"
            await emit({"type": "manual_check", "stage": "carousel", "message": note})
        return {"status": status, "note": note[:200]}

    # ---- 直传图床：串行保序，与 ⑦ 同一理由（空间弹窗按入库时间排序）----
    up = await upload_many(session, [r["path"] for r in ready])
    uploaded = up.get("uploaded") or []
    if not uploaded:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(ready)} 张轮播图产物直传全失败，仍是原图"
                               "（发布会被拦，请人工换图）"})
        return {"status": "fail", "note": f"轮播图直传全失败：{str(up)[:150]}"}
    # 【按 path 剔除失败项，不能 zip 截断】upload_many 的 uploaded 只含成功项：
    # 直接 zip(ready, uploaded) 会把后一张的 fileId 安到前一张头上，再按 ready 的长度
    # 截尾，于是「该换的没换、不该取消的被取消」。按失败的 path 保序剔除才对得上。
    failed_paths = {f.get("path") for f in (up.get("failed") or [])}
    failed_up = [r["tag"] for r in ready if r["path"] in failed_paths]
    ready = [r for r in ready if r["path"] not in failed_paths]
    if len(ready) != len(uploaded) or any(not item.get("fileId") for item in uploaded):
        return {"status": "fail", "note": "轮播图上传回执不完整，停止替换并交人工核对"}
    for r, u in zip(ready, uploaded):
        r["fileId"] = u["fileId"]
    if failed_up:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(failed_up)} 张轮播图产物直传失败"
                               f"（{'、'.join(failed_up[:4])}），保持原图，请人工换图"})
    if not ready:
        return {"status": "fail", "note": "轮播图产物直传全失败，保持原图"}

    # ---- 把新图选进轮播图候选列表 ----
    opened = await open_carousel_space(session)
    if opened.get("err") or not opened.get("opened"):
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "打不开轮播图「选择图片」弹窗，不合规图仍是原图"
                               "（发布会被拦，请人工换图）"})
        return {"status": "fail",
                "note": f"打开选图弹窗失败[{opened.get('stage') or '?'}]："
                        f"{opened.get('err') or ''}"[:200]}

    picked_res = await media_space._pick_many_from_space(
        session, [r["fileId"] for r in ready])
    if picked_res.get("err"):
        # 弹窗可能还开着挡住后续操作，尽力关掉（best-effort，失败不影响错误返回）
        await media_space._close_space_modal(session)
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "在图片空间里选不中新传的轮播图，不合规图仍是原图"
                               "（发布会被拦，请人工换图）"})
        return {"status": "fail",
                "note": f"空间选图失败[{picked_res.get('stage') or '?'}]："
                        f"{picked_res.get('err') or ''}"[:200]}

    # ---- 插完重读一次：新图进了列表，旧下标全部作废 ----
    st2 = await carousel_state(session)
    items2 = st2.get("items") or []
    if not items2:
        return {"status": "fail",
                "note": f"选图后读不到轮播图区（{st2.get('err') or '零个图格'}），"
                        "无法核对替换结果"}

    # 新图按 fileId 文件名在列表里认（同 _pick_from_space：弹窗与列表的 URL 前缀
    # 不同但文件名一致）。认不到就不能瞎勾——宁可保留原图报人工。
    ok_tags, added = [], []
    expected_urls = {it["url"] for it in picked}
    for item in sorted(ready, key=lambda entry: entry["add"]):
        current = await carousel_state(session)
        current_items = current.get("items") or []
        file_id = item["fileId"].rsplit("/", 1)[-1]
        hit = next((entry for entry in current_items
                    if file_id in (entry.get("url") or "")), None)
        old_item = next((entry for entry in current_items
                         if entry.get("url") == item["oldUrl"] and entry.get("checked")), None)
        failures = failed_adds if item["add"] else failed_kept
        if hit is None or (not item["add"] and old_item is None):
            failures.append(item["tag"])
            continue
        removed = False
        if not hit.get("checked") and sum(bool(entry.get("checked")) for entry in current_items) >= CAROUSEL_MAX_PICKED:
            if old_item is None:
                failures.append(item["tag"])
                continue
            result = await toggle_carousel(session, old_item["i"], False)
            if result.get("stage") != "ok":
                failures.append(item["tag"])
                continue
            removed = True
        if not hit.get("checked"):
            result = await toggle_carousel(session, hit["i"], True)
            if result.get("stage") != "ok":
                if removed:
                    await toggle_carousel(session, old_item["i"], True)
                failures.append(item["tag"])
                continue
        added.append((item, hit))
        if old_item is not None and not removed:
            result = await toggle_carousel(session, old_item["i"], False)
            if result.get("stage") != "ok":
                failures.append(item["tag"])
                continue
        expected_urls.discard(item["oldUrl"])
        expected_urls.add(hit["url"])
        replacements = {source: hit["url"] if destination == item["sourceUrl"] else destination
                        for source, destination in replacements.items()}
        replacements[item["sourceUrl"]] = hit["url"]
        ok_tags.append(item["tag"])

    # ---- 收尾复核：勾选数与残留不合规图 ----
    st3 = await carousel_state(session)
    picked3 = [it for it in (st3.get("items") or []) if it.get("checked")]
    with open(replacements_path, "w", encoding="utf-8") as destination:
        json.dump(replacements, destination, ensure_ascii=False, indent=2)
    bad3 = [it for it in picked3 if it.get("bad")]
    if {it.get("url") for it in picked3} != expected_urls:
        unknown.append("最终勾选结果与计划不一致")
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "轮播图最终勾选结果与计划不一致，暂停发布并交人工核对"})
    for item in picked3:
        if item.get("url") in checked_urls:
            continue
        path = os.path.join(prep, f"final-{item['i']:02d}.jpg")
        try:
            downloaded = await asyncio.to_thread(extract._download_image, item["url"], path)
            qc = await vision.check_cleaned(path) if downloaded else {}
        except Exception as exc:
            qc = {"issues": str(exc)}
        if qc.get("clean") is not True or qc.get("status") == "error":
            tag = f"第 {item['i'] + 1} 张"
            failed_kept.append(tag)
            await emit({"type": "manual_check", "stage": "carousel",
                        "message": f"轮播图 {tag} 替换后英化复检未通过，停止自动处理并交人工："
                                   f"{qc.get('issues') or '无法取得质检结论'}"})
    if len(picked3) < CAROUSEL_MIN_PICKED:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"处理完轮播图只选用 {len(picked3)} 张，低于平台下限 "
                               f"{CAROUSEL_MIN_PICKED} 张，需人工补选"})
    if len(picked3) > CAROUSEL_MAX_PICKED:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"处理完轮播图选用 {len(picked3)} 张，超过平台上限 "
                               f"{CAROUSEL_MAX_PICKED} 张，需人工取消多余勾选"})
    if failed_kept:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(failed_kept)} 张已选轮播图仍带中文/不合规"
                               f"（{'、'.join(failed_kept[:6])}），发布可能被拒，"
                               "请人工换图"})

    n_add = sum(1 for r, _ in added if r["add"] and r["tag"] in ok_tags)
    note = (f"替换 {len(ok_tags) - n_add} 张、补勾信息图 {n_add} 张"
            f"（现选用 {len(picked3)} 张，残留不合规 {len(bad3)} 张）")
    if unknown:
        note += f"；{len(unknown)} 张读不出结论"
    if failed_kept:
        note += f"；{len(failed_kept)} 张未换成合规图：{'、'.join(failed_kept[:4])}"
    if failed_adds:
        note += f"；{len(failed_adds)} 张信息图未补勾"
    if failed_up:
        note += f"；{len(failed_up)} 张上传失败"
    blocked = (failed_kept or failed_adds or failed_up or unknown or bad3
               or st3.get("supported") is not True
               or not CAROUSEL_MIN_PICKED <= len(picked3) <= CAROUSEL_MAX_PICKED)
    return {"status": "fail" if blocked else "ok", "note": note[:200]}
