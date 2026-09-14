"""店小秘发布共用能力：stages.carousel。各来源流程由 workflows/ 独立定义。"""

import os
import shutil
from app.logger import logger
from app.publish import extract, images
from app.publish.browser import BrowserSession
from app.publish.media import space as media_space
from app.publish.media.carousel import (
    CAROUSEL_MIN_PICKED,
    CAROUSEL_MIN_SIDE,
    carousel_state,
    open_carousel_space,
    toggle_carousel,
)
from app.publish.upload import upload_many


async def _st_carousel(ctx: dict, session: BrowserSession, emit) -> dict:
    """阶段⑤c 产品轮播图合规化：把已勾选里不合规的图换成 1:1 且 >=800 的方图。

    设计动机与四处图位的关系见 app/publish/media/carousel.py 开头那段取证。这里
    只讲本阶段的流程取向：

    【只管已勾选的图，不管候选池】平台校验的是【选用】的那几张（拒绝文案出现在
    发布时，未勾选的图压根不参与）。候选池里那十几张非 1:1 的图不是问题，去动它们
    等于凭空多传十几张图。故 carousel_state 的 bad 判据带 checked 条件。

    【等量替换：先勾新图，再取消旧图】顺序刻意如此。页面约束是「最少选用3张」，
    若先取消再勾选，中途勾选数会掉到下限以下；某些前端会在那一刻弹提示或直接把
    操作吞掉。先加后减则全程不低于原数量，也就不触发下限校验。

    【替换失败就保留原图，不反选】这与 ⑦b 空图位的处置刻意不同：预览图空着会被拒
    「请上传预览图」，所以补不上宁可反选整行；而轮播图这里原图【还在且还勾着】，
    保留它至少数量是够的，反选反而可能把勾选数打到 3 以下，把「图太小」变成
    「图太少」。故失败一律保留原状 + 报 manual_check，交人工换图。
    """
    st = await carousel_state(session)
    if st.get("supported") is None:
        await asyncio.sleep(2.0)
        st = await carousel_state(session)
    if st.get("supported") is None:
        # 证据不足（区块没渲染完/页面改版）：不当成「不支持」静默跳过，如实报出来。
        # 静默跳过的下场就是这次那单——发布时才被拒，回头还得从日志里反推。
        return {"status": "fail",
                "note": f"读不到产品轮播图区（{st.get('err') or '零个图格'}），"
                        "无法核对尺寸"}

    items = st.get("items") or []
    picked = [it for it in items if it.get("checked")]
    bad = [it for it in picked if it.get("bad")]
    unknown = [it for it in picked if not it.get("known")]

    if unknown:
        # 尺寸文本没渲染出来的格子不判不合格，但要说出来：它可能藏着不合规图，
        # 发布仍会被拒，而本阶段确实没依据去动它。
        await emit({"type": "log", "stage": "carousel",
                    "message": f"{len(unknown)} 张已选轮播图读不到尺寸文本，"
                               "本阶段按「未知不等于不合格」放过"})

    if not bad:
        return {"status": "skipped",
                "note": f"已选 {len(picked)} 张轮播图均满足 1:1 且不小于 "
                        f"{CAROUSEL_MIN_SIDE}x{CAROUSEL_MIN_SIDE}"}

    await emit({"type": "log", "stage": "carousel",
                "message": f"已选 {len(picked)} 张轮播图里 {len(bad)} 张不合规"
                           f"（非 1:1 或小于 {CAROUSEL_MIN_SIDE}x{CAROUSEL_MIN_SIDE}），"
                           "逐张下载后做 1:1 合规化再换回"})

    prep = os.path.join(ctx["workdir"], "carousel")
    shutil.rmtree(prep, ignore_errors=True)
    os.makedirs(prep, exist_ok=True)

    # ---- 本地备料：下载 + 合规化（纯本地，失败的直接排除，不占后面的上传/勾选）----
    ready, fail_tags = [], []
    for it in bad:
        i = it["i"]
        tag = f"第 {i + 1} 张（{it.get('sizeText') or '?'}）"
        raw = os.path.join(prep, f"pic{i:02d}-raw.jpg")
        out = os.path.join(prep, f"pic{i:02d}.jpg")
        try:
            if not extract._download_image(it["url"], raw):
                logger.warning(f"轮播图 {tag} 源站取不到（404 等），保持原样")
                fail_tags.append(tag)
                continue
            sq = images.square_image(raw, out_path=out)
        except Exception as e:
            logger.warning(f"轮播图 {tag} 合规化失败，保持原样：{e}")
            fail_tags.append(tag)
            continue
        ready.append({"idx": i, "tag": tag, "path": sq["output"],
                      "outSize": sq["outSize"]})

    if not ready:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(bad)} 张轮播图不合规，但全部下载/合规化失败、"
                               "仍是原图（发布会被拦，请人工换图）"})
        return {"status": "fail",
                "note": f"{len(bad)} 张不合规轮播图备料全失败：{'、'.join(fail_tags[:6])}"[:200]}

    # ---- 直传图床：串行保序，与 ⑦ 同一理由（空间弹窗按入库时间排序）----
    up = await upload_many(session, [r["path"] for r in ready])
    uploaded = up.get("uploaded") or []
    if not uploaded:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(ready)} 张合规化后的轮播图直传全失败，仍是原图"
                               "（发布会被拦，请人工换图）"})
        return {"status": "fail", "note": f"轮播图直传全失败：{str(up)[:150]}"}
    # 上传是按 ready 顺序串行的，逐一对应回原格子下标；失败的那些留在 fail_tags
    for r, u in zip(ready, uploaded):
        r["fileId"] = u["fileId"]
    fail_tags.extend(r["tag"] for r in ready[len(uploaded):])
    ready = ready[:len(uploaded)]

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

    # ---- 等量替换：先勾新图，再取消旧图（顺序见 docstring）----
    st2 = await carousel_state(session)
    items2 = st2.get("items") or []
    if not items2:
        return {"status": "fail",
                "note": f"选图后读不到轮播图区（{st2.get('err') or '零个图格'}），"
                        "无法核对替换结果"}

    # 新图按 fileId 文件名在列表里认（同 _pick_from_space：弹窗与列表的 URL 前缀
    # 不同但文件名一致）。认不到就不能瞎勾——宁可保留原图报人工。
    ok_tags, added = [], []
    for r in ready:
        fid = r["fileId"].rsplit("/", 1)[-1]
        hit = next((it for it in items2 if fid in (it.get("url") or "")), None)
        if not hit:
            logger.warning(f"轮播图 {r['tag']} 新图已传但列表里认不到 fileId={fid}，"
                           "保持原图")
            fail_tags.append(r["tag"])
            continue
        if not hit.get("checked"):
            tg = await toggle_carousel(session, hit["i"], True)
            if tg.get("stage") != "ok":
                logger.warning(
                    f"轮播图 {r['tag']} 新图勾选失败[{tg.get('stage') or '?'}]："
                    f"{tg.get('err') or ''}，保持原图")
                fail_tags.append(r["tag"])
                continue
        added.append(r)

    # 只取消「新图已确实勾上」的那些旧图，一一对应，保证勾选总数不变
    for r in added:
        tg = await toggle_carousel(session, r["idx"], False)
        if tg.get("stage") != "ok":
            # 新图已勾上、旧图没取消掉：勾选数比原来多一张，且那张不合规图还在选用中。
            # 发布仍会被拒，必须如实报出来（超过 10 张上限时同样会被拦）。
            logger.warning(
                f"轮播图 {r['tag']} 旧图取消勾选失败[{tg.get('stage') or '?'}]："
                f"{tg.get('err') or ''}，不合规图仍在选用中")
            fail_tags.append(r["tag"])
            continue
        ok_tags.append(r["tag"])
        logger.info(f"轮播图 {r['tag']} 已换成 {r['outSize']}")

    # ---- 收尾复核：勾选数与残留不合规图 ----
    st3 = await carousel_state(session)
    picked3 = [it for it in (st3.get("items") or []) if it.get("checked")]
    bad3 = [it for it in picked3 if it.get("bad")]
    if len(picked3) < CAROUSEL_MIN_PICKED:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"替换后轮播图只选用 {len(picked3)} 张，低于平台下限 "
                               f"{CAROUSEL_MIN_PICKED} 张，需人工补选"})
    if fail_tags:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(fail_tags)} 张轮播图未能换成合规图"
                               f"（{'、'.join(fail_tags[:6])}），发布可能被拒尺寸，"
                               "请人工换图"})

    note = (f"{len(ok_tags)}/{len(bad)} 张不合规轮播图已换成方图"
            f"（现选用 {len(picked3)} 张，残留不合规 {len(bad3)} 张）")
    if fail_tags:
        note += f"；失败 {len(fail_tags)} 张：{'、'.join(fail_tags[:4])}"
    # 全失败才算 fail：换掉一部分也是实质进展，且原图都还在、数量没少
    return {"status": "ok" if ok_tags else "fail", "note": note[:200]}
