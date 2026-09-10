# -*- coding: utf-8 -*-
"""⑬b 视频「丢弃」路线的单测：批次开关 keep_video=False 时直接点删除。

【为什么这几条值得钉住】这条路线是给「整批不要视频、换速度」用的，它的失效方式
全是静默的：
  - 开关没接进 ctx（run_batch → publish_one → ctx 三段任一漏传），表现是开关关了
    却照旧走下载+转码，白花几十秒到几分钟，日志里看不出异常；
  - 走到丢弃分支却还去读接口/下载，同上；
  - 删除失败被写成 fail，会让一个加分项拖垮整个商品（与保留路线「从不 fail」的
    取向相反，见 service._st_video 的 docstring）。
故这里逐条覆盖，不测 DOM 几何（那部分靠真站探查，见
workspace/_probe_video_delete_click.py 的取证记录）。
"""

from publish_patching import patch_publish
import pytest

from app.publish import service


class _FakeSession:
    """页面交互全被 monkeypatch，故只要能被传进去。"""


@pytest.fixture
def _ctx(tmp_path):
    # keep_video=False：丢弃路线
    return {"rowid": "173539495447642153", "workdir": str(tmp_path),
            "keep_video": False}


def _emit_collector():
    events = []

    async def emit(ev):
        events.append(ev)

    return events, emit


def _patch_discard(monkeypatch, ret):
    """替换 delete_video，并给保留路线的三个依赖装上「一被调用就失败」的哨兵，
    用来证明丢弃路线确实没走那条路。"""
    calls = {"delete": 0, "read": 0, "download": 0, "normalize": 0, "set": 0}

    async def _del(session):
        calls["delete"] += 1
        return ret

    async def _read(session, rowid):
        calls["read"] += 1
        return {"status": "ok", "videoUrl": "https://cdn/a.mp4"}

    def _dl(url, out, **kw):
        calls["download"] += 1
        return {"status": "ok", "path": out}

    def _norm(path, **kw):
        calls["normalize"] += 1
        return {"status": "ok", "action": "skip", "meta": {}, "ratioName": "1:1"}

    async def _set(session, path, full_cid=None):
        calls["set"] += 1
        return {"status": "ok"}

    patch_publish(monkeypatch, "service", "delete_video", _del)
    patch_publish(monkeypatch, "service", "read_video_url", _read)
    patch_publish(monkeypatch, "service", "set_video", _set)
    monkeypatch.setattr(service.videolib, "download_video", _dl)
    monkeypatch.setattr(service.videolib, "normalize_video", _norm)
    return calls


@pytest.mark.asyncio
async def test_开关关掉时删除视频(monkeypatch, _ctx):
    calls = _patch_discard(monkeypatch, {"status": "ok", "already": False})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok"
    assert "删除" in r["note"]
    assert calls["delete"] == 1
    assert not events, "正常删掉不该报人工检查"


@pytest.mark.asyncio
async def test_丢弃路线不读接口不下载不转码(monkeypatch, _ctx):
    """省掉的正是这条路线的全部收益：读接口一个来回 + 下载 + ffmpeg + 直传。"""
    calls = _patch_discard(monkeypatch, {"status": "ok", "already": False})
    _, emit = _emit_collector()
    await service._st_video(_ctx, _FakeSession(), emit)
    assert calls["read"] == 0, "丢弃视频不需要知道地址"
    assert calls["download"] == 0 and calls["normalize"] == 0
    assert calls["set"] == 0


@pytest.mark.asyncio
async def test_本来没视频时跳过(monkeypatch, _ctx):
    """delete_video 靠封面块的显隐判断，本来就空就回 already。"""
    _patch_discard(monkeypatch, {"status": "ok", "already": True})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "skipped"
    assert "没有视频" in r["note"]
    assert not events


@pytest.mark.asyncio
async def test_删除失败降级为ok并报人工检查(monkeypatch, _ctx):
    """与保留路线同取向：视频是加分项，不该为它让整个商品 fail。"""
    _patch_discard(monkeypatch, {"status": "error", "stage": "verify",
                                "err": "点了但封面块仍在"})
    events, emit = _emit_collector()
    r = await service._st_video(_ctx, _FakeSession(), emit)
    assert r["status"] == "ok", "删除失败不该拖垮整个商品"
    assert "删除失败" in r["note"]
    msgs = [e["message"] for e in events if e["type"] == "manual_check"]
    assert msgs and "视频仍在" in msgs[0]


@pytest.mark.asyncio
async def test_开关缺省时走保留路线(monkeypatch, tmp_path):
    """ctx 里没有 keep_video（老状态文件/漏传）必须按默认「保留」走，
    不能因为读不到键就静默把视频删掉——那是不可逆方向上的错误默认。"""
    calls = _patch_discard(monkeypatch, {"status": "ok", "already": False})
    ctx = {"rowid": "1", "workdir": str(tmp_path)}  # 刻意不给 keep_video
    _, emit = _emit_collector()
    await service._st_video(ctx, _FakeSession(), emit)
    assert calls["delete"] == 0, "缺省必须保留视频，不能默认删除"
    assert calls["read"] == 1


# ---- 开关的接线（三段任一漏传都会让开关静默失效）---------------------------

def test_publish_one与run_batch都收keep_video():
    import inspect
    for fn in (service.publish_one, service.run_batch):
        sig = inspect.signature(fn)
        assert "keep_video" in sig.parameters, f"{fn.__name__} 少了 keep_video"
        assert sig.parameters["keep_video"].default is True, "默认必须是保留"


def test_publish_one把keep_video塞进ctx():
    import inspect
    src = inspect.getsource(service._run_product)
    assert '"keep_video": keep_video' in src


def test_run_batch把keep_video透传给publish_one():
    import inspect
    assert "keep_video=keep_video" in inspect.getsource(service.run_batch)


def test_keep_video不进状态文件():
    """与 use_cache/price/do_publish 同理：「这批要不要视频」属于本次运行的决定，
    续跑不该继承上次的取向。"""
    import inspect
    src = inspect.getsource(service._run_product)
    # 状态回写的白名单元组里不该出现 keep_video
    assert 'state["keep_video"]' not in src
    assert "keep_video" not in src.split('for k in (')[-1].split(')')[0]


# ---- delete_video 本身的取证（DOM 结构与安全性）----------------------------

def test_delete_video按文本认删除链接():
    """两个 a 的 class 都只是 link、层级还不对称（播放多套一层 div），
    只有文本可靠；且必须 trim 后严格等值，用 includes 会误命中别处的删除。"""
    from app.publish.pipeline import _JS_DELETE_VIDEO
    assert ".video-operate-box" in _JS_DELETE_VIDEO
    assert "=== '删除'" in _JS_DELETE_VIDEO
    assert "includes('删除')" not in _JS_DELETE_VIDEO


def test_delete_video按封面块显隐判成败():
    """判据是 .video-operate-img 变 display:none（2026-08-29 逐帧采样取证），
    不能按「有没有 img」判——封面容器里始终有一张 base64 占位图。"""
    from app.publish.pipeline import _JS_DELETE_VIDEO
    assert ".video-operate-img" in _JS_DELETE_VIDEO
    assert "display" in _JS_DELETE_VIDEO
