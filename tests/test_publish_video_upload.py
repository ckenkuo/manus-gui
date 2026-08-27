# -*- coding: utf-8 -*-
"""视频直传店小秘图床（smtmedia bucket）的离线单测：不连 CDP、不开浏览器、不出网。

锁住三类容易静默出错的不变量：

1. **签名响应的双结构兼容**。wxalbum 把有效载荷套两层 data（j.data.data），而
   smtmedia 的 data.data 是 null、字段直接挂在 data 上（2026-08-26 实测）。
   原 _JS_GET_SIGN 只读 j.data.data，照搬到视频会静默读到空值、报成「取签名失败」
   而看不出是结构差异。这里用两种响应形状分别驱动同一段 JS 的 Python 侧等价逻辑。

2. **上传即把关**。比例不合规的视频传上去，前 14 个阶段全绿、save 也落库，直到
   阶段⑮ 发布才被打回，且回执不说是哪个视频。故 upload_video 默认必查比例，
   且必须在【取签名之前】就拒掉（否则白占一次签名、白传一次字节）。

3. **对外 URL 的域名替换**。前端把签名 URL 里的 cos.ap-guangzhou 换成 picgz 当
   videoUrl 用；漏了这步填进表单的地址平台侧取不到。
"""
import os
import subprocess

import pytest

from app.publish.upload import (
    VIDEO_ALLOWED_EXT,
    VIDEO_BUCKET,
    VIDEO_REGION,
    VIDEO_URL_FROM,
    VIDEO_URL_TO,
    upload_video,
)
from app.publish.video import resolve_ffmpeg


def _make_video(path: str, w: int, h: int, seconds: int = 1) -> str:
    """造一段测试视频（与 test_publish_video 同一手法，不依赖外部素材）。"""
    ff = resolve_ffmpeg()
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=15:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path],
        capture_output=True, timeout=180, check=True,
    )
    return path


class _FakeSession:
    """BrowserSession 的最小替身：按调用顺序回放预置的 eval_json 结果。

    同时记录每次执行的 JS 原文，供断言「bucket/region 有没有换对」。
    """

    def __init__(self, results: list):
        self._results = list(results)
        self.calls = []

    async def eval_json(self, code: str, timeout: int = 90, retries: int = 3) -> dict:
        self.calls.append(code)
        return self._results.pop(0) if self._results else {}


@pytest.fixture
def _no_curl(monkeypatch):
    """把 COS PUT 换成永远成功的假子进程：单测不真的上传字节。"""
    class _P:
        returncode = 0
        stdout = b""
        stderr = b""

    async def _fake(*a, **kw):
        return _P()

    monkeypatch.setattr("app.publish.upload._curl_put", _fake)
    return _P


@pytest.fixture
def _cid(monkeypatch):
    """固定 fullCid，免得单测依赖本机 config.toml。"""
    monkeypatch.setattr("app.publish.upload.resolve_full_cid", lambda: "5153348-")


# ---- 前置校验 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_文件不存在直接拒(tmp_path, _cid):
    s = _FakeSession([])
    r = await upload_video(s, str(tmp_path / "没有.mp4"))
    assert r["status"] == "error"
    assert r["stage"] == "precheck"
    assert not s.calls, "不该为不存在的文件去取签名"


@pytest.mark.asyncio
async def test_非mp4直接拒(tmp_path, _cid):
    """popTemu 的 upVideoType 白名单只有 mp4；传别的 COS 会收下但平台解析不了。"""
    src = _make_video(str(tmp_path / "v.mp4"), 720, 960)
    other = str(tmp_path / "v.mov")
    os.rename(src, other)
    s = _FakeSession([])
    r = await upload_video(s, other)
    assert r["status"] == "error"
    assert r["stage"] == "precheck"
    assert "mp4" in r["err"]
    assert not s.calls


@pytest.mark.asyncio
async def test_比例不合规在取签名前就拒(tmp_path, _cid):
    """这是本模块最重要的一条：拦在阶段⑮ 发布之前，且不白占签名。"""
    src = _make_video(str(tmp_path / "竖屏.mp4"), 720, 1280)
    s = _FakeSession([])
    r = await upload_video(s, src)
    assert r["status"] == "error"
    assert r["stage"] == "video-check"
    assert "720×1280" in r["err"]
    assert not s.calls, "比例不合规不该发起任何页面请求"


@pytest.mark.asyncio
async def test_skip_ratio_check可跳过比例校验(tmp_path, _cid, _no_curl):
    """调用方已自行校验时用；跳过后应当照常走完三步。"""
    src = _make_video(str(tmp_path / "竖屏.mp4"), 720, 1280)
    s = _FakeSession([
        {"code": 0, "sign": "SIGN", "url": "//x.cos.ap-guangzhou.myqcloud.com/a.mp4",
         "fileId": "/smtmedia/a.mp4"},
        {"code": 0, "msg": "", "videoId": 99},
    ])
    r = await upload_video(s, src, skip_ratio_check=True)
    assert r["status"] == "ok"
    assert len(s.calls) == 2


# ---- 三步流程 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_用视频bucket与region取签名(tmp_path, _cid, _no_curl):
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    s = _FakeSession([
        {"code": 0, "sign": "SIGN", "url": "//x.cos.ap-guangzhou.myqcloud.com/a.mp4",
         "fileId": "/smtmedia/a.mp4"},
        {"code": 0, "msg": "", "videoId": 123},
    ])
    r = await upload_video(s, src)
    assert r["status"] == "ok"
    assert f"bucket={VIDEO_BUCKET}" in s.calls[0]
    assert f"region={VIDEO_REGION}" in s.calls[0]
    assert VIDEO_BUCKET == "smtmedia" and VIDEO_REGION == "ap-guangzhou"
    # 回调也必须用视频 bucket，不能沿用 wxalbum
    assert f"bucket={VIDEO_BUCKET}" in s.calls[1]


@pytest.mark.asyncio
async def test_对外URL做cdn域名替换(tmp_path, _cid, _no_curl):
    """漏了 cos.ap-guangzhou → picgz，填进表单的地址平台侧取不到。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    s = _FakeSession([
        {"code": 0, "sign": "SIGN",
         "url": "//smtmedia-1251220924.cos.ap-guangzhou.myqcloud.com/v/a.mp4",
         "fileId": "/smtmedia/v/a.mp4"},
        {"code": 0, "msg": "", "videoId": 7},
    ])
    r = await upload_video(s, src)
    assert r["status"] == "ok"
    assert VIDEO_URL_FROM not in r["url"], "签名 URL 的 cos 域名没换成 CDN 域名"
    assert VIDEO_URL_TO in r["url"]
    # originUrl 保留替换前的原值（组件两个都存）
    assert VIDEO_URL_FROM in r["originUrl"]
    assert r["originUrl"].startswith("https:"), "协议相对 URL 必须补 https:"


@pytest.mark.asyncio
async def test_回调的videoId回传(tmp_path, _cid, _no_curl):
    """组件把回调返回的 videoId 当 dxmVideoId 用，必须原样带出来。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    s = _FakeSession([
        {"code": 0, "sign": "S", "url": "//h/a.mp4", "fileId": "/smtmedia/a.mp4"},
        {"code": 0, "msg": "", "videoId": 456789},
    ])
    r = await upload_video(s, src)
    assert r["videoId"] == 456789


@pytest.mark.asyncio
async def test_取签名失败时不继续(tmp_path, _cid, _no_curl):
    """签名拿不到就别 PUT——否则 curl 会往一个空 URL 发请求，错误信息指不到根因。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    s = _FakeSession([{"code": -1, "sign": None, "url": None, "msg": "bucket 不允许"}])
    r = await upload_video(s, src)
    assert r["status"] == "error"
    assert r["stage"] == "getSign"
    assert len(s.calls) == 1, "取签名失败后不该再发回调"


@pytest.mark.asyncio
async def test_PUT失败时不登记(tmp_path, _cid, monkeypatch):
    """字节没传上去就登记，会在平台侧留一条指向空文件的记录。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)

    class _P:
        returncode = 56
        stdout = b""
        stderr = b"Recv failure"

    async def _fail(*a, **kw):
        return _P()

    monkeypatch.setattr("app.publish.upload._curl_put", _fail)
    s = _FakeSession([
        {"code": 0, "sign": "S", "url": "//h/a.mp4", "fileId": "/smtmedia/a.mp4"},
    ])
    r = await upload_video(s, src)
    assert r["status"] == "error"
    assert r["stage"] == "cos-put"
    assert len(s.calls) == 1


@pytest.mark.asyncio
async def test_登记失败判error(tmp_path, _cid, _no_curl):
    """不登记则视频在 COS 上但平台侧没记录，必须报出来而不是当成功。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    s = _FakeSession([
        {"code": 0, "sign": "S", "url": "//h/a.mp4", "fileId": "/smtmedia/a.mp4"},
        {"code": -1, "msg": "登记失败"},
    ])
    r = await upload_video(s, src)
    assert r["status"] == "error"
    assert r["stage"] == "callback"


@pytest.mark.asyncio
async def test_PUT超时按体积放宽(tmp_path, _cid, monkeypatch):
    """视频最大 500M，图片那档 120s 不够；超时必须随体积增长。"""
    src = _make_video(str(tmp_path / "ok.mp4"), 720, 960)
    seen = {}

    class _P:
        returncode = 0
        stdout = b""
        stderr = b""

    async def _spy(put_url, file_path, sign, ctype, timeout=120):
        seen["timeout"] = timeout
        seen["ctype"] = ctype
        return _P()

    monkeypatch.setattr("app.publish.upload._curl_put", _spy)
    s = _FakeSession([
        {"code": 0, "sign": "S", "url": "//h/a.mp4", "fileId": "/smtmedia/a.mp4"},
        {"code": 0, "msg": "", "videoId": 1},
    ])
    await upload_video(s, src)
    assert seen["timeout"] >= 300, "视频上传超时不该沿用图片的 120s"
    assert seen["ctype"] == "video/mp4"


# ---- 签名响应的双结构兼容 ---------------------------------------------------
# _JS_GET_SIGN 在浏览器里跑，单测不开浏览器，故这里直接锁住那段 JS 的取值表达式
# 必须是「双取」形态。这不是重复实现，是防止有人改回只读 j.data.data 的回归闸。

def test_取签名JS双取兼容两种结构():
    from app.publish.upload import _JS_GET_SIGN
    assert "dd.data || dd" in _JS_GET_SIGN, (
        "smtmedia 的 sign/url/fileId 直接挂在 data 上（没有 data.data 那层），"
        "只读 j.data.data 会在视频路径上静默读到空值"
    )


def test_回调JS取videoId():
    from app.publish.upload import _JS_CALLBACK
    assert "videoId" in _JS_CALLBACK, "组件要用回调返回的 videoId 当 dxmVideoId"


def test_常量与前端配置一致():
    """这些值来自前端 chunk 的 bucket 表，写错会让上传静默传到别的 bucket。"""
    assert VIDEO_BUCKET == "smtmedia"
    assert VIDEO_REGION == "ap-guangzhou"
    assert VIDEO_URL_FROM == "cos.ap-guangzhou"
    assert VIDEO_URL_TO == "picgz"
    assert VIDEO_ALLOWED_EXT == (".mp4",)
