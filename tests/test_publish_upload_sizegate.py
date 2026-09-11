# -*- coding: utf-8 -*-
"""上传前尺寸闸门的接入测试（upload_image 的 precheck，不碰网络、不碰 CDP）。

2026-08-23 真站取证（rowid 173539495453435641 保存报错）：描述区 10 张图仍是 1688
原始外链（1000×1000 / 900×1200），全部低于服装类下限 1340×1785，save 时被静默弹回
——平台不弹 toast，只滚到出错区块、右侧锚点变红，极难定位是哪张图。

原先这条规则只靠 fit_34 / square_image / compress「出图达标」保证，约束写在各调用点
的注释里（「传进来的图必须已做过合规化，本函数不代做」）。约定不是校验：CLI 直传原图、
或某条新增路径忘了合规化，都能一路传进图床。故收敛到 upload_image —— 所有图片进平台
的唯一入口，在那里把关才不会被绕过。

这里钉住的是接入语义：拦住时不发起任何网络动作、错误可定位、放行时不误伤、
以及 skip_size_check 的显式逃生口仍在。
"""
import pytest
from PIL import Image

from app.publish import upload


def _img(path, w, h):
    Image.new("RGB", (w, h), (150, 120, 90)).save(path)
    return str(path)


class _SpySession:
    """记录有没有被调用过——闸门拦下时一次 eval 都不该发生。"""

    def __init__(self):
        self.calls = 0

    async def eval_json(self, js):
        self.calls += 1
        raise AssertionError("闸门应在任何页面动作之前拦下，不该走到取签名")


@pytest.mark.asyncio
async def test_小图被拦且不发起上传(tmp_path, monkeypatch):
    """真站那两种描述图尺寸，必须在 precheck 就被拒。"""
    monkeypatch.setattr(upload, "resolve_full_cid", lambda: "test-cid-")
    for w, h in [(1000, 1000), (900, 1200)]:
        s = _SpySession()
        r = await upload.upload_image(s, _img(tmp_path / f"s{w}x{h}.jpg", w, h))
        assert r["status"] == "error"
        assert r["stage"] == "size-check"
        assert r["size"] == f"{w}x{h}"
        # 差多少像素要写清楚，否则只知道「不合规」没法动手改
        assert "1340" in r["err"] and "1785" in r["err"]
        # 关键：一次页面 eval 都没发生，图片没进图床
        assert s.calls == 0


@pytest.mark.asyncio
async def test_文件不存在仍优先报文件问题(tmp_path, monkeypatch):
    """尺寸闸门不该把「文件不存在」盖成「尺寸不达标」，那会误导排查方向。"""
    monkeypatch.setattr(upload, "resolve_full_cid", lambda: "test-cid-")
    s = _SpySession()
    r = await upload.upload_image(s, str(tmp_path / "nope.jpg"))
    assert r["status"] == "error"
    assert r["stage"] == "precheck"
    assert "文件不存在" in r["err"]
    assert s.calls == 0


@pytest.mark.asyncio
async def test_skip_size_check_可显式跳过(tmp_path, monkeypatch):
    """逃生口仍在：跳过后会继续走取签名（这里用 Spy 抛错来证明「走过去了」）。"""
    monkeypatch.setattr(upload, "resolve_full_cid", lambda: "test-cid-")
    s = _SpySession()
    small = _img(tmp_path / "small.jpg", 800, 800)
    with pytest.raises(AssertionError, match="不该走到取签名"):
        await upload.upload_image(s, small, skip_size_check=True)
    assert s.calls == 1        # 确实越过闸门进了第 1 步


@pytest.mark.asyncio
async def test_贴线图被拦_服装口径(tmp_path, monkeypatch):
    """服装那条平台口径是「严格大于」：1340×1785 会被发布拦下，故闸门也不放行。

    2026-09-05 实测（1071736188944）宽恰好 1340 的图穿过闸门、到发布才被平台以
    「服装类图片尺寸不能小于1340px*1785px」拒掉。原先这里断言「恰好等于下限放行」，
    与平台实测相反，已改（描述图的 >= 480 是另一套规则，走显式传下限那条路）。
    """
    monkeypatch.setattr(upload, "resolve_full_cid", lambda: "test-cid-")
    s = _SpySession()
    p = _img(tmp_path / "edge.jpg", 1340, 1785)
    r = await upload.upload_image(s, p)
    assert r["status"] == "error" and r["stage"] == "size-check"
    assert s.calls == 0


@pytest.mark.asyncio
async def test_达标图放行进入取签名(tmp_path, monkeypatch):
    """严格大于下限才放行。"""
    monkeypatch.setattr(upload, "resolve_full_cid", lambda: "test-cid-")
    s = _SpySession()
    ok_img = _img(tmp_path / "ok.jpg", 1341, 1786)
    with pytest.raises(AssertionError, match="不该走到取签名"):
        await upload.upload_image(s, ok_img)
    assert s.calls == 1


def test_闸门就是images那一个():
    """upload 侧不许另抄一份判据：两处漂移会让「过了单测但真站仍被弹回」。"""
    from app.publish.images import CLOTH_MIN_H, CLOTH_MIN_W, check_cloth_size

    assert upload.check_cloth_size is check_cloth_size
    assert (CLOTH_MIN_W, CLOTH_MIN_H) == (1340, 1785)
