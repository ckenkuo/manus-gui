"""店小秘发布共用能力：stages.video。各来源流程由 workflows/ 独立定义。"""

import asyncio
import os
from app.logger import logger
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
        # 【源地址不在店小秘图床时，比例虽合规也要转存一次】"已合规就什么都不做、连上传
        # 都省掉"这条优化是为 1688 定的：它的视频在淘宝 CDN 上，店小秘拉得动。但 Temu 源
        # 的视频在 goods-vod.kwcdn.com，**店小秘拉它会被限流**——2026-09-10 实测发布时
        # 报「上传视频接口报错:上传视频信息失败 connect timed out」，店小秘自己的提示也是
        # 「建议视频保存在本地，然后选择从本地上传」。也就是说：比例不是问题，**地址本身
        # 才是**。故这里仍走一次直传图床 + 回填（用的是刚下载到本地的那个文件），
        # 把地址换到店小秘域名下。已在店小秘图床的（1688 那类本就没问题）照旧跳过。
        if "dianxiaomi.com" in src_url:
            return {"status": "skipped",
                    "note": f"视频已合规（{meta.get('w')}×{meta.get('h')} "
                            f"{norm.get('ratioName')}），未改动"}
        logger.info(f"视频已合规但源地址不在店小秘图床（{src_url[:70]}），仍转存一次")

    # 3) 直传 + 用「网络上传」把地址填回表单
    r = await set_video(session, norm["output"])
    if r.get("status") != "ok":
        # 【失败文案要按环节说、别一律讲成「裁切成功」】没裁过的（已合规转存）说成
        # 「裁切成功但回填失败」是语义错误，人照着这句去查裁切根本没有产物。
        # 判据取 norm 的 action，不 parse 文案（见 [[publish-stage-fail-message-by-stage]]）。
        did = "已合规（未裁切）" if norm.get("action") == "skip" else "裁切成功"
        await emit({"type": "manual_check", "stage": "video",
                    "message": f"视频{did}但没能填回表单[{r.get('stage')}]，"
                               f"页面上仍是原视频（发布可能被打回）：{str(r)[:150]}"})
        return {"status": "ok",
                "note": f"{did}但回填失败[{r.get('stage')}]，视频原样保留"}

    # 【note 分两种说法，因为这两支干的事不同】已合规那支没有新产物、尺寸前后一样，
    # 打成「720×720 → 720×720」等于让人怀疑白转了一遍；它的成果是【地址换到店小秘
    # 图床】（源在 goods-vod.kwcdn.com 时店小秘拉不动，见上面那段取证），文案就该说
    # 这件事。真裁过的才用「前 → 后」的对比。
    out = norm.get("outMeta") or meta
    dur = f"，截断到 {out.get('duration')}s" if norm.get("trimmed") else ""
    if norm.get("action") == "skip":
        note = (f"已合规（{out.get('w')}×{out.get('h')} {norm.get('ratioName')}，"
                f"{out.get('sizeMB')}MB），未裁切，已转存到店小秘图床")
    else:
        note = (f"{meta.get('w')}×{meta.get('h')}（{meta.get('ratio')}）→ "
                f"{out.get('w')}×{out.get('h')}（{norm.get('ratioName')}）"
                f"{dur}，{out.get('sizeMB')}MB")
    return {"status": "ok", "note": note[:200]}
