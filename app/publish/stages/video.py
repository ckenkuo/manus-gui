"""店小秘发布共用能力：stages.video。各来源流程由 workflows/ 独立定义。"""

import asyncio
import os
from app.publish import video as videolib
from app.publish.browser import BrowserSession
from app.publish.media.video import delete_video, read_video_url, set_video


async def _st_video(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑬b 产品视频：把平台连带搬来的 1688 视频裁成 Temu 允许的比例后填回表单。

    【这一步为什么存在】视频不是我们传的，是阶段② 认领时平台从 1688 连带搬来的。
    1688 商品视频绝大多数是 9:16 竖屏，而 Temu 只收 1:1 / 3:4 / 16:9，于是发布时
    被打回「Video ratio should be 1:1 or 3:4 or 16:9」——而这个报错出现在阶段⑮
    发布【之后】：前 14 个阶段全绿、save 也落库了，最后一步才被弹回，且回执不说是
    哪个视频。故必须在发布前把关（2026-08-26 那批失败品实测：720×1280，比例 0.5625）。

    【纯增益路径，从不 fail】与 ⑤b 图片清理同一取向：
      - 批次开关 keep_video=False → 直接点「删除」丢弃视频，不做任何审核；
      - 没有视频 → skipped（大多数 1688 商品其实没视频）；
      - 视频已合规 → skipped，绝不重编码（重编码必然掉画质，对本来就合规的是倒扣分）；
      - 下载/转码/上传任一步失败 → 报 manual_check 但仍返回 ok，让流程继续走到 save。
    最后一条是刻意的：视频只是加分项，为它失败而让整个商品 fail 得不偿失——而原来
    那批品的实际后果只是「带着不合规视频去发布、被平台打回」，人工删掉视频即可发布。
    故这里失败时把话说清楚（哪个环节、什么原因），由人决定是删视频还是重试。

    【为什么读接口而不读页面】视频区 DOM 里没有真实地址（封面是内嵌 base64 占位图，
    Vue 3 的 setupState 也扒不到），只有 edit.json 的响应里有，见 read_video_url。
    """
    # 【批次级开关：不保留视频就直接删，连接口和下载都不用碰】keep_video=False 时
    # 用户已经决定整批不要视频，那么读 videoUrl / 下载 / 转码 / 直传全是白工——
    # 页面上有没有视频看 DOM 就知道（封面块的显隐），比读接口还快一个来回。
    if not ctx.get("keep_video", True):
        r = await delete_video(session)
        if r.get("status") != "ok":
            # 与本阶段其余分支同取向：从不 fail，报 manual_check 让人决定
            await emit({"type": "manual_check", "stage": "video",
                        "message": f"按批次开关要删视频但没删掉[{r.get('stage')}]："
                                   f"{r.get('err')}——页面上视频仍在，"
                                   f"发布时可能被平台按比例打回"})
            return {"status": "ok", "note": f"删除失败[{r.get('stage')}]，视频原样保留"}
        if r.get("already"):
            return {"status": "skipped", "note": "该商品没有视频，无需删除"}
        return {"status": "ok", "note": "按批次开关已删除产品视频（不做比例审核）"}

    rowid = ctx.get("rowid") or ""
    if not rowid:
        return {"status": "skipped", "note": "没有 rowid，读不到视频字段"}

    cur = await read_video_url(session, rowid)
    src_url = (cur.get("videoUrl") or "").strip()
    if not src_url:
        return {"status": "skipped", "note": "该商品没有视频"}

    workdir = os.path.join(ctx["workdir"], "video")
    os.makedirs(workdir, exist_ok=True)
    raw = os.path.join(workdir, "source.mp4")

    # 1) 下载。视频在淘宝 CDN 上，要跟 302 且带浏览器 UA（见 video.download_video）
    dl = await asyncio.to_thread(videolib.download_video, src_url, raw)
    if dl.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频下载失败，未处理（发布时可能被平台按比例打回）："
                               f"{dl.get('err')}"})
        return {"status": "ok", "note": f"下载失败，视频原样保留：{str(dl.get('err'))[:120]}"}

    # 2) 合规化。已合规会 action=skip 原样返回，不重编码
    norm = await asyncio.to_thread(videolib.normalize_video, raw)
    if norm.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频转码失败，未处理（发布时可能被平台按比例打回）："
                               f"{norm.get('err')}"})
        return {"status": "ok", "note": f"转码失败，视频原样保留：{str(norm.get('err'))[:120]}"}

    meta = norm.get("meta") or {}
    if norm.get("action") == "skip":
        # 源视频本来就合规：什么都不用改，连上传都省掉
        return {"status": "skipped",
                "note": f"视频已合规（{meta.get('w')}×{meta.get('h')} "
                        f"{norm.get('ratioName')}），未改动"}

    # 3) 直传 + 用「网络上传」把地址填回表单
    r = await set_video(session, norm["output"])
    if r.get("status") != "ok":
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频已裁好但没能填回表单[{r.get('stage')}]，"
                               f"页面上仍是原视频（发布可能被打回）：{str(r)[:150]}"})
        return {"status": "ok",
                "note": f"裁切成功但回填失败[{r.get('stage')}]，视频原样保留"}

    out = norm.get("outMeta") or {}
    dur = f"，截断到 {out.get('duration')}s" if norm.get("trimmed") else ""
    return {"status": "ok",
            "note": (f"{meta.get('w')}×{meta.get('h')}（{meta.get('ratio')}）→ "
                     f"{out.get('w')}×{out.get('h')}（{norm.get('ratioName')}）"
                     f"{dur}，{out.get('sizeMB')}MB")[:200]}
