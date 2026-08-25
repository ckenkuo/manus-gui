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


# 第 1 步：页面内取 COS 签名。签名接口把有效载荷套了两层 data（j.data.data），
# 这不是笔误，是店小秘的响应约定。
_JS_GET_SIGN = r"""(async () => {
  const body = 'bucket=__BUCKET__&region=__REGION__&fileName=' + encodeURIComponent(__FNAME__);
  const r = await fetch('/api/cos/getSign.json', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body});
  const j = await r.json();
  const d = (j.data && j.data.data) || {};
  return JSON.stringify({code: j.code, sign: d.sign, url: d.url, fileId: d.fileId, msg: j.msg});
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
  return JSON.stringify({code: j.code, msg: j.msg});
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
                       skip_size_check: bool = False) -> dict:
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
    """
    if not os.path.exists(file_path):
        return {"status": "error", "stage": "precheck", "err": f"文件不存在: {file_path}"}
    if not skip_size_check:
        chk = check_cloth_size(file_path)
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


async def _curl_put(put_url: str, file_path: str, sign: str, ctype: str):
    """在线程池里跑 curl.exe PUT，避免阻塞事件循环。

    用 asyncio.to_thread 而不是 create_subprocess_exec：Windows 上后者要求
    ProactorEventLoop，而本项目的 web 侧（uvicorn）事件循环策略不由这里决定，
    to_thread 对循环类型无要求，行为一致。
    """
    import asyncio

    def _run():
        return subprocess.run(
            ["curl.exe", "-sS", "-X", "PUT", "--max-time", "120",
             "-H", f"Authorization: {sign}",
             "-H", f"Content-Type: {ctype}",
             "--data-binary", f"@{file_path}", put_url],
            capture_output=True, timeout=140,
        )

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
