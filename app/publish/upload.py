"""图片直传店小秘图床（wxalbum），发布管线阶段⑥⑦⑪的公共基础。

从 skill 的 scripts/dianxiaomi_edit.py 的 cmd_upload_image（1329 行）移植。

为什么走 API 直传而不用页面的文件选择框：
文件选择框是原生 OS 对话框，CDP 下要么走 DOM.setFileInputFiles（店小秘的上传控件
是自绘的、拿不到真 input），要么模拟键盘敲路径（时序脆）。直传把「传文件」和
「页面选图」彻底解耦——传完图片就进了图床，后续三个阶段统一走「空间图片」弹窗选图，
一套机制服务三处。

三步流程（入参形态是 2026-08-17 抓包实测得来，别凭猜改字段名）：
    1) 页面内 fetch POST /api/cos/getSign.json —— 带 cookie 拿 COS 签名
       入参 bucket=wxalbum&region=ap-shanghai&fileName=<名>
    2) curl.exe PUT 文件字节到签名 URL —— Authorization: sign，【不需要】店小秘 cookie
    3) 页面内 fetch POST /api/cos/cosDxmCallBack.json —— 登记入库，图片才在空间里可见
       入参 bucket=wxalbum&fullCid=<账号cid>&fileId=<id>&fileName=<名>&isNeedTree=0&fileSize=<字节>

为什么第 2 步必须用 curl.exe 子进程（别顺手改成 httpx/requests）：
与 images.py 的 Packy 调用同一个坑——COS PUT 在 requests/urllib 的 TLS 指纹下被拦。
另一个理由是文件字节不必进 Python 内存、也不必 base64 塞进 evaluate（大图会把
CDP 消息撑爆）。--data-binary @<路径> 由 curl 直接流式读盘。

为什么第 1、3 步在页面内 fetch 而不在 Python 侧发：
这两个接口靠店小秘登录 cookie 鉴权。页面内 fetch 天然带 cookie（credentials:include），
Python 侧要先把 cookie 从浏览器捞出来再拼请求，多一层且会随会话刷新失效。

【full_cid 是账号级常量，绝不硬编码】原脚本写死了 5153348-，换账号图片会传上去但
登记不到自己名下（表现为「上传成功但空间里找不到」）。这里从 [publish].full_cid 读，
抓包 cosDxmCallBack.json 的 fullCid 参数可得。
"""
import os
import subprocess
from typing import Optional

from app.logger import logger
from app.publish.images import check_cloth_size
from app.publish.browser import J, BrowserSession

# 图床最终 URL 前缀：fileId 本身带前导斜杠，故这里不补
WXALBUM_HOST = "https://wxalbum-10001658-file.dianxiaomi.com"

# COS 直传的固定入参（2026-08-17 抓包）：bucket 与 region 都是账号无关的常量
COS_BUCKET = "wxalbum"
COS_REGION = "ap-shanghai"

# ---- 视频直传（阶段⑥b 用）----------------------------------------------------
# 【视频复用图片这套三步，只换 bucket——2026-08-26 从前端 chunk 溯源确认】
# 编辑页视频组件（懒加载 chunk utils-zkclcStz.js）的「本地上传」分支调的就是
# imgUpload chunk 导出的同一个上传函数，配置是 {upSize: 100MB, type: "smtmedia"}。
# 故不必另接接口：getSign.json → PUT 签名 URL → cosDxmCallBack.json 原样走一遍。
# （/api/video/uploadVideo.json 确实存在，但那是【视频库】页面的接口，编辑页不走它。）
VIDEO_BUCKET = "smtmedia"
VIDEO_REGION = "ap-guangzhou"
# CDN 域名替换：前端拿到签名 URL 后把 cos.ap-guangzhou 换成 picgz 当对外地址
# （imgUpload chunk 里的 `rp` → `to` 映射，smtmedia 那一档）
VIDEO_URL_FROM = "cos.ap-guangzhou"
VIDEO_URL_TO = "picgz"
# popTemu 平台只允许 mp4（前端 upVideoType 白名单，各平台不同，popTemu 就这一种）
VIDEO_ALLOWED_EXT = (".mp4",)
# 【两个体积上限不是一回事，别混】Temu 平台侧是 500M（video.MAX_SIZE_MB），
# 而店小秘编辑页【本地上传】的前端校验是 100M（chunk 里的 upSize，页面提示原文
# 「本地上传视频限制100M内」）。我们走 COS 直传绕过了前端校验，故 100M 不是硬墙；
# 但超了就说明这条路与页面行为不一致，值得告警——真出问题时能立刻想到这里。
VIDEO_LOCAL_UPLOAD_WARN_MB = 100



def resolve_full_cid() -> str:
    """取账号级 fullCid：环境变量 DXM_FULL_CID > config.toml 的 [publish].full_cid。

    与 images.resolve_packy_key 同构（同样不留内置兜底值）：原脚本硬编码的
    5153348- 只对那一个账号成立，留着比没有更坏——换账号后上传会「成功」但图片
    登记到别人名下，空间弹窗里找不到，报错信息完全指不到根因。
    """
    cid = os.environ.get("DXM_FULL_CID")
    if cid:
        return cid
    try:
        import tomllib

        from app.config import config_search_dirs
        for d in config_search_dirs():
            p = d / "config.toml"
            if not p.exists():
                continue
            with open(p, "rb") as f:
                data = tomllib.load(f)
            cid = (data.get("publish") or {}).get("full_cid") or ""
            if cid:
                return cid
    except Exception as e:
        logger.warning(f"读取 [publish].full_cid 失败：{e}")
    raise RuntimeError(
        "缺少店小秘 fullCid：设环境变量 DXM_FULL_CID，"
        "或在 config/config.toml 的 [publish] 段配 full_cid"
        "（抓包 /api/cos/cosDxmCallBack.json 的 fullCid 参数可得，形如 5153348-）"
    )


# 第 1 步：页面内取 COS 签名。
#
# 【两种 bucket 的响应结构不同，必须双取】wxalbum 把有效载荷套了两层 data
# （j.data.data，不是笔误，是店小秘的响应约定）；而 smtmedia（视频）的 j.data.data
# 是 null、sign/url/fileId 直接挂在 j.data 上（2026-08-26 实测）。只读 j.data.data
# 会在视频路径上静默读到空值，报成「取签名失败」而看不出是结构差异。
# 故按 `j.data.data || j.data` 兜——两种结构都能取到。
_JS_GET_SIGN = r"""(async () => {
  const body = 'bucket=__BUCKET__&region=__REGION__&fileName=' + encodeURIComponent(__FNAME__);
  const r = await fetch('/api/cos/getSign.json', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
  const j = await r.json();
  const dd = j.data || {};
  const d = dd.data || dd;
  return JSON.stringify({code: j.code, sign: d.sign, url: d.url, fileId: d.fileId,
                         msg: j.msg || dd.msg});
})()"""

# 第 3 步：回调登记入库。isNeedTree=0 表示不挂到分组树上（挂了要额外传目录 id）。
_JS_CALLBACK = r"""(async () => {
  const body = 'bucket=__BUCKET__&fullCid=' + encodeURIComponent(__CID__)
    + '&fileId=' + encodeURIComponent(__FILEID__)
    + '&fileName=' + encodeURIComponent(__FNAME__)
    + '&isNeedTree=0&fileSize=' + __FSIZE__;
  const r = await fetch('/api/cos/cosDxmCallBack.json', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
  const j = await r.json();
  // videoId 只在视频（smtmedia）回调里有值，组件把它当 dxmVideoId 用；
  // 图片回调没有这个字段，取到 undefined 不影响图片路径。
  const d = j.data || {};
  return JSON.stringify({code: j.code, msg: j.msg,
                         videoId: d.videoId != null ? d.videoId : d.id});
})()"""


def _content_type(fname: str) -> str:
    """按扩展名给 Content-Type。

    只区分 png 与 jpeg：图片合规化（images.py）的产物只有这两类，
    传错 Content-Type 时 COS 不报错但 CDN 回图会带错的 mime，浏览器可能不渲染。
    """
    low = fname.lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


async def upload_image(session: BrowserSession, file_path: str,
                       full_cid: Optional[str] = None,
                       skip_size_check: bool = False,
                       min_w: Optional[int] = None,
                       min_h: Optional[int] = None) -> dict:
    """把本地图片直传店小秘图床，返回可直接填进表单的图片 URL。

    调用前提：session 已停在店小秘任意已登录页面（第 1、3 步要靠页面 cookie）。
    不导航——与其它阶段一致，导航由调用方决定，免得把编辑页的未保存改动冲掉。

    返回 {"status": "ok", "fileId": ..., "url": ..., "callback": {...}}；
    任一步失败返回 {"status": "error", "stage": <失败的步骤>, ...}，
    由调用方决定重试还是中止（属主流程，不在这里吞异常）。

    【上传即把关：服装类 1340×1785 是硬红线】precheck 阶段拦不达标的图，理由见
    images.check_cloth_size 上方注释——平台只在 save 时校验且完全静默，让小图传进
    图床再被弹回，排查成本远高于在这里直接拒掉。skip_size_check=True 可跳过
    （非服装类素材、或调用方已自行校验时用），但默认必查：上传是所有图片进平台的
    唯一入口，把关放这里才不会被某条新增调用路径绕过。

    min_w/min_h 按用途覆盖那道闸的下限（不传＝服装的 1340×1785）：描述图的平台
    要求只有「两边 >= 480、比例 0.5~2」，套服装下限会把达标的图拒掉。
    不传下限时按【严格大于】判（服装那条平台连等于也拦），传了下限则放行等于下限
    （描述图明文是「>= 480」），理由见 check_cloth_size 的 strict 参数。
    """
    if not os.path.exists(file_path):
        return {"status": "error", "stage": "precheck", "err": f"文件不存在: {file_path}"}
    if not skip_size_check:
        # 【下限按用途传，不能一律套服装的 1340×1785】2026-08-29 实测
        # （草稿 173539495458370139 第 10 张描述图）：描述图的平台要求只是
        # 「两边 >= 480、比例 0.5~2」（images.check_desc_size），而这里默认
        # 套服装闸门，于是一张合规的 480×480 描述图被拒两轮、页面上留着 1688
        # 原始外链，⑬ 最终以「1 张不符合要求」整单失败——被拒的图其实是达标的。
        # 闸门本身要留（它挡住了小图静默弹回，见下方 docstring），只是下限要
        # 跟着用途走：素材/SKC 图不传参照旧默认，描述图传 DESC_MIN_W/H。
        # 【口径按用途分】没传下限＝走服装默认那条（素材图/SKC 图）：平台把它当
        # 「不能小于 1340×1785」判、连等于也拦，故用 strict=True；传了下限＝调用方
        # 按自己的明文规则来（描述图「两边 >= 480」），放行等于下限。见 check_cloth_size。
        kw = {k: v for k, v in (("min_w", min_w), ("min_h", min_h)) if v is not None}
        chk = check_cloth_size(file_path, strict=not kw, **kw)
        if not chk["ok"]:
            logger.error(f"图片尺寸不达标，拒绝上传：{os.path.basename(file_path)} {chk['reason']}")
            return {"status": "error", "stage": "size-check",
                    "err": chk["reason"], "size": chk["size"]}
    cid = full_cid or resolve_full_cid()
    fname = os.path.basename(file_path)
    fsize = os.path.getsize(file_path)

    # 1. 取签名
    js = (_JS_GET_SIGN
          .replace("__BUCKET__", COS_BUCKET)
          .replace("__REGION__", COS_REGION)
          .replace("__FNAME__", J(fname)))
    sign = await session.eval_json(js)
    if not sign.get("sign") or not sign.get("url"):
        logger.error(f"取 COS 签名失败：{sign}")
        return {"status": "error", "stage": "getSign", **sign}

    # 签名 URL 常以协议相对形式返回（//host/path），curl 不认，要补 https:
    raw_url = sign["url"]
    put_url = ("https:" + raw_url) if raw_url.startswith("//") else raw_url

    # 2. PUT 文件字节到 COS（curl.exe 子进程，理由见模块 docstring）
    proc = await _curl_put(put_url, file_path, sign["sign"], _content_type(fname))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[:200]
        logger.error(f"COS PUT 失败 rc={proc.returncode}: {err}")
        return {"status": "error", "stage": "cos-put", "err": err}

    # 3. 回调登记（不登记则图片在 COS 上但空间弹窗里看不到）
    js2 = (_JS_CALLBACK
           .replace("__BUCKET__", COS_BUCKET)
           .replace("__CID__", J(cid))
           .replace("__FILEID__", J(sign["fileId"]))
           .replace("__FNAME__", J(fname))
           .replace("__FSIZE__", str(fsize)))
    cb = await session.eval_json(js2)
    final_url = WXALBUM_HOST + sign["fileId"]
    ok = cb.get("code") == 0
    if not ok:
        logger.error(f"图片登记入库失败：{cb}")
    else:
        logger.info(f"图片直传完成：{fname} -> {final_url}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "callback",
            "fileId": sign["fileId"], "url": final_url,
            "fileName": fname, "fileSize": fsize, "callback": cb}


async def _curl_put(put_url: str, file_path: str, sign: str, ctype: str,
                    timeout: int = 120):
    """在线程池里跑 curl.exe PUT，避免阻塞事件循环。

    用 asyncio.to_thread 而不是 create_subprocess_exec：Windows 上后者要求
    ProactorEventLoop，而本项目的 web 侧（uvicorn）事件循环策略不由这里决定，
    to_thread 对循环类型无要求，行为一致。

    timeout 可调是为视频加的：图片那档 120s 够用，而视频最大 500M，
    按保守带宽估要几分钟（见 upload_video 里按体积算超时那段）。

    【瞬态网络故障重试】Windows 自带 curl.exe 版本（如 7.55.1）不支持
    --retry-all-errors（curl 7.71+ 才引入，传该参数会直接报错 option is unknown 退出）。
    因此将重试逻辑移至 Python 循环中（首次 + 2 次重试），同样能覆盖 DNS 解析失败（error 6）、
    连接重置、连接拒绝（error 7）等各类瞬态故障。
    """
    import asyncio
    import time

    def _run():
        cmd = [
            "curl.exe", "-sS", "-X", "PUT", "--max-time", str(timeout),
            "-H", f"Authorization: {sign}",
            "-H", f"Content-Type: {ctype}",
            "--data-binary", f"@{file_path}", put_url,
        ]
        last_proc = None
        for attempt in range(3):
            try:
                last_proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
                if last_proc.returncode == 0:
                    return last_proc
                err_snippet = last_proc.stderr.decode("utf-8", "replace")[:100]
                logger.warning(
                    f"COS PUT 第 {attempt + 1} 次尝试失败 rc={last_proc.returncode}: {err_snippet}，准备重试..."
                )
            except subprocess.TimeoutExpired:
                logger.warning(f"COS PUT 第 {attempt + 1} 次尝试超时，准备重试...")
            if attempt < 2:
                time.sleep(1)
        if last_proc is None:
            return subprocess.CompletedProcess(
                args=cmd, returncode=-1, stdout=b"",
                stderr=b"curl timeout expired on all retry attempts",
            )
        return last_proc

    return await asyncio.to_thread(_run)


async def upload_many(session: BrowserSession, paths: list,
                      full_cid: Optional[str] = None) -> dict:
    """按给定顺序逐张直传，返回 {"uploaded": [...], "failed": [...]}。

    刻意【串行】而不并发：阶段⑦ 依赖上传顺序决定行内图片顺序（颜色专属图命名
    01.jpg 就自然落在首位、免拖拽，见 todo 阶段⑦）。并发会打乱登记入库的先后，
    空间弹窗按入库时间排序，顺序一乱就得回去拖图。
    """
    cid = full_cid or resolve_full_cid()
    uploaded, failed = [], []
    for p in paths:
        res = await upload_image(session, p, full_cid=cid)
        if res.get("status") == "ok":
            uploaded.append(res)
        else:
            failed.append({"path": p, **res})
    return {"status": "ok" if not failed else "partial",
            "uploaded": uploaded, "failed": failed,
            "total": len(paths), "okCount": len(uploaded)}


# ---- 视频直传 ---------------------------------------------------------------

async def upload_video(session: BrowserSession, file_path: str,
                       full_cid: Optional[str] = None,
                       skip_ratio_check: bool = False) -> dict:
    """把本地视频直传店小秘图床（smtmedia bucket），返回可填进表单的视频 URL。

    与 upload_image 走的是【同一套】COS 三步（前端 chunk 溯源确认，见 VIDEO_BUCKET
    上方注释），只换 bucket/region，并处理两处视频特有的差异：
      1. 签名响应结构不同（smtmedia 不套 data.data，见 _JS_GET_SIGN 注释）；
      2. 对外 URL 要把 cos.ap-guangzhou 换成 picgz（前端的 rp→to 映射）。

    【上传即把关：宽高比是硬红线】与 upload_image 用 check_cloth_size 拦小图同构。
    比例不合规的视频传上去，前 14 个阶段全绿、save 也落库，直到阶段⑮ 发布才被打回
    「Video ratio should be 1:1 or 3:4 or 16:9」，且回执不说是哪个视频——排查成本
    远高于在这里直接拒掉。skip_ratio_check=True 可跳过（调用方已自行校验时用），
    但默认必查：上传是视频进平台的唯一入口，把关放这里才不会被新增调用路径绕过。

    返回 {"status": "ok", "fileId", "url", "originUrl", "videoId", ...}；
    任一步失败返回 {"status": "error", "stage": <失败的步骤>, ...}，由调用方决定重试。
    """
    if not os.path.exists(file_path):
        return {"status": "error", "stage": "precheck", "err": f"文件不存在: {file_path}"}
    fname = os.path.basename(file_path)
    # popTemu 只收 mp4（前端 upVideoType 白名单）。传别的扩展名 COS 会收下，
    # 但平台侧解析不了，最终仍在发布时报错，故在这里就拦住。
    if not fname.lower().endswith(VIDEO_ALLOWED_EXT):
        return {"status": "error", "stage": "precheck",
                "err": f"popTemu 只接受 {'/'.join(VIDEO_ALLOWED_EXT)} 视频，当前是 {fname}"}
    if not skip_ratio_check:
        from app.publish.video import check_video
        chk = check_video(file_path)
        if not chk["ok"]:
            logger.error(f"视频不合规，拒绝上传：{fname} {chk['reason']}")
            return {"status": "error", "stage": "video-check",
                    "err": chk["reason"], "meta": chk.get("meta")}
    cid = full_cid or resolve_full_cid()
    fsize = os.path.getsize(file_path)
    size_mb = fsize / (1024 * 1024)
    if size_mb > VIDEO_LOCAL_UPLOAD_WARN_MB:
        logger.warning(
            f"视频 {size_mb:.1f}MB 超过编辑页本地上传的前端限制 "
            f"{VIDEO_LOCAL_UPLOAD_WARN_MB}MB（我们走 COS 直传不受它约束，"
            f"但平台侧仍有 500MB 硬上限）：{fname}"
        )

    # 1. 取签名（bucket=smtmedia）
    js = (_JS_GET_SIGN
          .replace("__BUCKET__", VIDEO_BUCKET)
          .replace("__REGION__", VIDEO_REGION)
          .replace("__FNAME__", J(fname)))
    sign = await session.eval_json(js)
    if not sign.get("sign") or not sign.get("url"):
        logger.error(f"取视频 COS 签名失败：{sign}")
        return {"status": "error", "stage": "getSign", **sign}

    raw_url = sign["url"]
    put_url = ("https:" + raw_url) if raw_url.startswith("//") else raw_url

    # 2. PUT 视频字节到 COS。超时按体积放宽：视频可到 500M，图片那档 120s 不够
    #    （按 1MB/s 的保守下限估，再加 120s 底座）
    timeout = max(300, int(fsize / (1024 * 1024)) + 120)
    proc = await _curl_put(put_url, file_path, sign["sign"], "video/mp4", timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[:200]
        logger.error(f"视频 COS PUT 失败 rc={proc.returncode}: {err}")
        return {"status": "error", "stage": "cos-put", "err": err}

    # 3. 回调登记（不登记则视频在 COS 上但平台侧没有记录）
    js2 = (_JS_CALLBACK
           .replace("__BUCKET__", VIDEO_BUCKET)
           .replace("__CID__", J(cid))
           .replace("__FILEID__", J(sign["fileId"]))
           .replace("__FNAME__", J(fname))
           .replace("__FSIZE__", str(fsize)))
    cb = await session.eval_json(js2)
    # 对外地址：前端把签名 URL 里的 cos.ap-guangzhou 换成 picgz 当 videoUrl 用；
    # originUrl 保留替换前的原值（组件两个都存）
    origin_url = put_url
    final_url = origin_url.replace(VIDEO_URL_FROM, VIDEO_URL_TO)
    ok = cb.get("code") == 0
    if not ok:
        logger.error(f"视频登记入库失败：{cb}")
    else:
        logger.info(f"视频直传完成：{fname} {fsize / 1024 / 1024:.2f}MB -> {final_url}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "callback",
            "fileId": sign["fileId"], "url": final_url, "originUrl": origin_url,
            "videoId": cb.get("videoId"),
            "fileName": fname, "fileSize": fsize, "callback": cb}
