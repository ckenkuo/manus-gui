# -*- coding: utf-8 -*-
"""⑬b 产品视频阶段的编排单测（mock 浏览器与 ffmpeg，不碰 CDP、不出网）。

测的是阶段的【决策】而不是几何运算（后者在 test_publish_video.py，转码前后回读都真跑）：
哪些情况 skipped、哪些情况报 manual_check 但仍返回 ok、note 里有没有把关键数字说清楚。

【为什么这些分支值得单测】这一阶段刻意设计成「纯增益、从不 fail」——一旦某个失败路径
写成了 fail，整个商品会因为一个加分项而中断，而它原本的后果只是「带着不合规视频发布
被平台打回」。这类「本该降级却中断」的回归在真站上极难发现（要恰好碰上下载失败），
故用单测把每条失败路径都钉住。
"""

from publish_patching import patch_publish
import os

import pytest

from app.publish import service


class _FakeSession:
    """只需要能被传进去；本阶段所有页面交互都被 monkeypatch 掉了。"""


@pytest.fixture
def _ctx(tmp_path):
    return {"rowid": "173539495455998193", "workdir": str(tmp_path)}


def _emit_collector():
    events = []

    async def emit(ev):
        events.append(ev)

    return events, emit


def _patch(monkeypatch, *, cur=None, dl=None, norm=None, setv=None):
    """按需替换阶段依赖的四个外部调用，未给的用「正常成功」的默认值。"""
    async def _read(session, rowid):
        return cur if cur is not None else {"status": "ok", "videoUrl": ""}

    patch_publish(monkeypatch, "service", "read_video_url", _read)
    monkeypatch.setattr(
        service.videolib, "download_video",
        lambda url, out, **kw: (dl if dl is not None
                                else {"status": "ok", "path": out, "sizeMB": 2.8}))
    monkeypatch.setattr(
        service.videolib, "normalize_video",
        lambda path, **kw: (norm if norm is not None else {
            "status": "ok", "action": "crop", "output": path + "-3x4.mp4",
            "meta": {"w": 720, "h": 1280, "ratio": 0.5625},
            "outMeta": {"w": 720, "h": 960, "ratio": 0.75, "sizeMB": 3.05,
                        "duration": 35.41},
            "ratioName": "3:4", "trimmed": False,
        }))

    async def _set(session, path, full_cid=None):
        return setv if setv is not None else {"status": "ok", "url": "https://x/a.mp4"}

    patch_publish(monkeypatch, "service", "set_video", _set)


# ---- skipped 的三种情形 -----------------------------------------------------

@pytest.mark.asyncio
async def test_没有rowid时跳过(monkeypatch, _ctx):
    _patch(monkeypatch)
    events, emit = _emit_collector()
    _ctx["rowid"] = ""
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "skipped"
    assert not events


@pytest.mark.asyncio
async def test_商品没有视频时跳过(monkeypatch, _ctx):
    """大多数 1688 商品其实没视频，这条是最常走的分支。"""
    _patch(monkeypatch, cur={"status": "ok", "videoUrl": ""})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "skipped"
    assert "没有视频" in r["note"]
    assert not events


@pytest.mark.asyncio
async def test_视频已合规时跳过且不上传(monkeypatch, _ctx):
    """重编码必然掉画质，对本来就合规的视频做一遍是倒扣分；连上传都该省掉。"""
    called = {"set_video": False}

    async def _set(session, path, full_cid=None):
        called["set_video"] = True
        return {"status": "ok"}

    _patch(monkeypatch,
           cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"},
           norm={"status": "ok", "action": "skip", "output": "a.mp4",
                 "meta": {"w": 1080, "h": 1080, "ratio": 1.0}, "ratioName": "1:1"})
    patch_publish(monkeypatch, "service", "set_video", _set)
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "skipped"
    assert "已合规" in r["note"]
    assert not called["set_video"], "已合规不该再上传"
    assert not events


# ---- 成功路径 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_竖屏视频裁切并回填(monkeypatch, _ctx):
    """线上那批失败品的形态：720×1280 → 720×960。note 要把前后尺寸都说清楚。"""
    _patch(monkeypatch, cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok"
    assert "720×1280" in r["note"] and "720×960" in r["note"]
    assert "3:4" in r["note"]
    assert not events, "成功路径不该报人工检查"


@pytest.mark.asyncio
async def test_截断时长写进note(monkeypatch, _ctx):
    _patch(monkeypatch,
           cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"},
           norm={"status": "ok", "action": "crop", "output": "o.mp4",
                 "meta": {"w": 720, "h": 1280, "ratio": 0.5625},
                 "outMeta": {"w": 720, "h": 960, "ratio": 0.75, "sizeMB": 9.9,
                             "duration": 60.0},
                 "ratioName": "3:4", "trimmed": True})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok"
    assert "截断" in r["note"]


# ---- 失败路径：全部降级为 ok + manual_check --------------------------------

@pytest.mark.asyncio
async def test_下载失败不拖垮商品(monkeypatch, _ctx):
    _patch(monkeypatch,
           cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"},
           dl={"status": "error", "err": "下载失败 rc=28: timeout"})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok", "视频是加分项，不该让整个商品 fail"
    assert "下载失败" in r["note"]
    assert [e for e in events if e["type"] == "manual_check"], "必须报人工检查"


@pytest.mark.asyncio
async def test_转码失败不拖垮商品(monkeypatch, _ctx):
    _patch(monkeypatch,
           cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"},
           norm={"status": "error", "action": "encode", "err": "ffmpeg 挂了"})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok"
    assert "转码失败" in r["note"]
    assert [e for e in events if e["type"] == "manual_check"]


@pytest.mark.asyncio
async def test_回填失败不拖垮商品(monkeypatch, _ctx):
    """裁好了但没填进表单：页面上仍是原视频，要说清楚这一点。"""
    _patch(monkeypatch,
           cur={"status": "ok", "videoUrl": "https://cdn/a.mp4"},
           setv={"status": "error", "stage": "open-modal", "detail": {}})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok"
    assert "回填失败" in r["note"]
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert msgs and "仍是原视频" in msgs[0]


# ---- 阶段注册与续跑 ---------------------------------------------------------

def test_阶段已注册在三处():
    """漏任何一处都会让这一步静默不跑：STAGES 决定顺序、_STAGE_FUNCS 决定实现、
    _FORM_ONLY_STAGES 决定续跑时认不认状态文件。"""
    ids = [s for s, _ in service.STAGES]
    assert "video" in ids
    assert "video" in service._STAGE_FUNCS
    assert "video" in service._FORM_ONLY_STAGES


def test_阶段顺序在desc之后save之前():
    """必须在 save 之前——它改的是未保存的表单字段；放到 save 之后成果不落库。"""
    ids = [s for s, _ in service.STAGES]
    assert ids.index("desc") < ids.index("video") < ids.index("save")
    assert ids.index("video") < ids.index("publish")


def test_续跑时视频阶段总要重跑():
    """videoUrl 不在 DOM 里，live_state 读不到它，没法按实况细判；
    而漏跑的代价是走完 15 个阶段才被平台打回。故无条件进重跑集，由阶段自己判跳过。"""
    live = {
        "rendered": True, "titleFilled": True, "attrImgCount": 6,
        "skuRowCount": 4, "sizechartAdded": True, "skuCodeCount": 4,
        "skuCodeBad": 0, "skuFilledRows": 4, "shippingSet": True,
        "descImgCount": 3, "descForeignCount": 0,
    }
    stale = service._stale_form_stages(live)
    assert "video" in stale, "一切正常的页面上，video 仍应在重跑集里"
    # 其它阶段在这份「全都好」的实况下不该被判 stale，证明上面不是因为全量返回
    assert "titles" not in stale and "skc" not in stale


def test_读不到实况时视频阶段也在重跑集():
    stale = service._stale_form_stages({"rendered": False})
    assert "video" in stale

# ---- read_video_url 的取值路径（回归闸）------------------------------------
# 【为什么单独钉住】视频字段挂在 data.product 下，不在顶层。最初那版只扫顶层键，
# 于是每个商品都被读成「没有视频」、⑬b 整段静默 skipped——功能等于没做，而表面上
# 一切正常（skipped 不是错误、日志里也不报警）。2026-08-26 真站验证才发现。
# 这类「静默失效」比报错危险得多，故用两条断言把递归下钻锁住。

def test_read_video_url_递归下钻而不是只扫顶层():
    from app.publish.pipeline import read_video_url
    import inspect
    src = inspect.getsource(read_video_url)
    assert "walk(" in src, (
        "必须递归找 video 字段：实测挂在 data.product 下，"
        "只扫顶层会把每个商品都读成「没有视频」、整段静默跳过"
    )
    assert "data.product" in src, "注释里要写明实测路径，便于下次排查"


def test_read_video_url_回报命中路径():
    """paths 是排查用的：字段层级变了时能一眼看出挪到哪去了。"""
    from app.publish.pipeline import read_video_url
    import inspect
    assert "paths" in inspect.getsource(read_video_url)
