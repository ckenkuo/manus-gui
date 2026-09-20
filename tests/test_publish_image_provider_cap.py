# -*- coding: utf-8 -*-
"""出图通道解析与像素封顶的单测（2026-09-20 计费实测定下的判据）。

锁三条容易回归的事实，全部不打网络（只 mock 配置读取）：
  1. zzlye 按次固定计费，绝不能被封顶——封了省不到钱，只会让 compress 放大倍数更高、
     织标小字更糊（实测 1024² 与 2048² 的用量增量都是 3）；
  2. Packy 按尺寸计费要封到 ~1MP（实测 1024² 增量 2.94、2048² 增量 10.42），
     且压完必须保持原比例（压变形会毁描述长图的混排）；
  3. 压完不能跌破服务端像素下限 SIZE_MIN_PIXELS——破了请求必然被打回，
     宁可多花一点也不能发出注定失败的请求。
"""
import pytest

from app.publish import images


@pytest.fixture
def 钉通道(monkeypatch):
    """把 [publish] 配置钉成指定通道，避免读真实 config.toml（各机 key 不同）。"""
    def _pin(provider, **extra):
        monkeypatch.setattr(images, "_publish_conf",
                            lambda: {"image_provider": provider,
                                     "zzlye_api_key": "k", "packy_api_key": "k", **extra})
    return _pin


def test_zzlye按次计费不封顶(钉通道):
    """压尺寸一分钱不省，只会更糊——尺寸必须原样透传。"""
    钉通道("zzlye")
    assert images._provider()["max_pixels"] is None
    for sz in ("1344x1792", "2448x1792", "3840x2160"):
        assert images._cap_size(sz) == sz


def test_packy按尺寸计费要封到1MP(钉通道):
    """实测 2048² 增量 10.42 是 1024² 的 3.5 倍，大档必须压下来。"""
    钉通道("packy")
    assert images._provider()["max_pixels"] == 1_050_000
    out = images._cap_size("2448x1792")
    w, h = (int(x) for x in out.split("x"))
    assert w * h <= 1_060_000, f"封顶后仍有 {w * h} 像素"
    # 比例偏移只允许 16 倍数取整带来的那一点（压变形会毁描述长图混排）
    assert abs(w / h - 2448 / 1792) < 0.01


def test_封顶不得跌破服务端像素下限(钉通道):
    """SIZE_MIN_PIXELS 是服务端硬约束，破了请求被打回——宁可多花也不能发注定失败的请求。"""
    钉通道("packy", image_max_pixels=100)   # 荒谬的小上限
    out = images._cap_size("2448x1792")
    w, h = (int(x) for x in out.split("x"))
    assert w * h >= images.SIZE_MIN_PIXELS
    assert w % images.SIZE_MULTIPLE == 0 and h % images.SIZE_MULTIPLE == 0


def test_配置可覆盖上限且0表示不封(钉通道):
    钉通道("zzlye", image_max_pixels=800_000)
    assert images._provider()["max_pixels"] == 800_000
    钉通道("packy", image_max_pixels=0)
    assert images._provider()["max_pixels"] is None
    assert images._cap_size("2448x1792") == "2448x1792"


def test_未知通道名直接报错(钉通道):
    """静默退回默认会让「配置没生效」完全看不出来（出图照常成功、账单记在另一家）。"""
    钉通道("zzly")
    with pytest.raises(RuntimeError, match="image_provider"):
        images._provider()


def test_两家key字段互不混用(钉通道):
    """拿错分组的 key 会得 503「分组下模型无可用渠道」，各家必须读各自字段。"""
    assert images.PROVIDERS["zzlye"]["key_field"] == "zzlye_api_key"
    assert images.PROVIDERS["packy"]["key_field"] == "packy_api_key"
