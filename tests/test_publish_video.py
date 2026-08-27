"""发布管线视频合规化的离线单测（纯 ffmpeg 几何处理，不碰网络）。

只测比例判定与裁切：这部分是发布流程的刚性依赖——比例不合规会在阶段⑮ 发布时被
Temu 打回（`Video ratio should be 1:1 or 3:4 or 16:9`），且前 14 个阶段全绿、
save 也落库了才报，回执还不说是哪个视频，故必须在发布前拦住。

测试视频用 ffmpeg 的 testsrc 现造，不依赖任何外部素材、不下载。
download_video 要真实网络，不在单测范围。
"""
import os
import subprocess

import pytest

from app.publish.video import (
    ALLOWED_RATIOS,
    MAX_SIZE_MB,
    RECOMMEND_MAX_SECONDS,
    check_video,
    crop_box,
    match_ratio,
    normalize_video,
    pick_target_ratio,
    probe_video,
    resolve_ffmpeg,
)


def _make(path: str, w: int, h: int, seconds: int = 2) -> str:
    """用 ffmpeg 的 testsrc 造一段测试视频（带音轨，贴近真实商品视频的形状）。"""
    ff = resolve_ffmpeg()
    subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=15:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        capture_output=True, timeout=180, check=True,
    )
    return path


# ---- 元数据读取 -------------------------------------------------------------

@pytest.mark.parametrize("w,h", [(720, 1280), (1080, 1080), (1920, 1080), (720, 960)])
def test_probe_读出真实宽高与时长(tmp_path, w, h):
    src = _make(str(tmp_path / f"s-{w}x{h}.mp4"), w, h)
    m = probe_video(src)
    assert m["ok"], m.get("reason")
    assert (m["w"], m["h"]) == (w, h)
    assert 1.5 < m["duration"] < 3.0, f"时长读错了：{m['duration']}"
    assert m["sizeMB"] > 0


def test_probe_文件不存在时明确报错(tmp_path):
    m = probe_video(str(tmp_path / "没有这个文件.mp4"))
    assert not m["ok"]
    assert "不存在" in m["reason"]


def test_probe_不是视频时明确报错(tmp_path):
    """非视频文件必须判不 ok，不能静默当成 0×0 混过去。"""
    p = tmp_path / "假视频.mp4"
    p.write_bytes(b"this is definitely not a video" * 50)
    m = probe_video(str(p))
    assert not m["ok"]
    assert m["reason"]


# ---- 比例判定 ---------------------------------------------------------------

@pytest.mark.parametrize("ratio,expect", [
    (1.0, "1:1"),
    (0.75, "3:4"),
    (16 / 9, "16:9"),
    (0.7507, "3:4"),        # 1340×1785 那种带零头的实际比例，容差内应认成 3:4
    (0.5625, None),         # 9:16 竖屏——1688 商品视频的常见形态，必须判不合规
    (0.8, None),            # 4:5
    (1.3333, None),         # 4:3（注意不是 16:9）
])
def test_match_ratio(ratio, expect):
    assert match_ratio(ratio) == expect


def test_match_ratio_读不到比例时不瞎认():
    assert match_ratio(None) is None
    assert match_ratio(0) is None


@pytest.mark.parametrize("src,expect", [
    (0.5625, "3:4"),        # 竖屏：3:4 只丢上下 25%，1:1 要丢 44%
    (0.75, "3:4"),
    (0.95, "1:1"),
    (1.0, "1:1"),
    (1.5, "16:9"),
    (16 / 9, "16:9"),
    (2.4, "16:9"),          # 超宽银幕：16:9 是最近的一档
])
def test_pick_target_ratio_选裁得最少的档位(src, expect):
    assert pick_target_ratio(src) == expect


def test_pick_target_ratio_读不到比例时按3比4():
    """源是 1688 服装视频，竖屏占绝大多数，且 3:4 属平台推荐档。"""
    assert pick_target_ratio(None) == "3:4"


# ---- 裁切框计算 -------------------------------------------------------------

@pytest.mark.parametrize("w,h,name", [
    (720, 1280, "3:4"),
    (720, 1280, "1:1"),
    (1920, 1080, "1:1"),
    (1080, 1080, "3:4"),
    (1079, 1281, "3:4"),    # 奇数源：出来的宽高必须都是偶数
    (1920, 1080, "16:9"),   # 已是目标比例：不该裁
])
def test_crop_box_出偶数且比例正确且不越界(w, h, name):
    target = ALLOWED_RATIOS[name]
    cw, ch, x, y = crop_box(w, h, target)
    assert cw % 2 == 0 and ch % 2 == 0, f"{cw}x{ch} 不是偶数，libx264 会直接报错"
    assert cw <= w and ch <= h, f"裁切框 {cw}x{ch} 超出源 {w}x{h}，ffmpeg 会报错"
    assert x >= 0 and y >= 0
    assert x + cw <= w and y + ch <= h
    # 偶数取整后允许一两个像素的偏差，故容差按像素折算
    assert abs(cw / ch - target) < 0.01, f"{cw}x{ch} 比例 {cw / ch} 不是 {name}"


def test_crop_box_居中裁切():
    """裁掉的部分必须上下/左右均分——商品主体一般在画面中心。"""
    cw, ch, x, y = crop_box(720, 1280, 0.75)
    assert (cw, ch) == (720, 960)
    assert x == 0 and y == 160, "上下应各裁 160px"


# ---- 合规校验 ---------------------------------------------------------------

def test_check_video_竖屏判不合规且说清原因(tmp_path):
    """1688 商品视频的典型形态（720×1280），就是线上那批发布失败的成因。"""
    src = _make(str(tmp_path / "竖屏.mp4"), 720, 1280)
    r = check_video(src)
    assert not r["ok"]
    assert "ratio" in r["issues"]
    assert "720×1280" in r["reason"] and "1:1 / 3:4 / 16:9" in r["reason"]


@pytest.mark.parametrize("w,h", [(1080, 1080), (720, 960), (1920, 1080)])
def test_check_video_三种允许比例都放行(tmp_path, w, h):
    src = _make(str(tmp_path / f"ok-{w}x{h}.mp4"), w, h)
    r = check_video(src)
    assert r["ok"], r["reason"]
    assert r["ratioName"] in ALLOWED_RATIOS


def test_check_video_超时长只告警不拦(tmp_path, monkeypatch):
    """平台原文是「建议时长在1分钟内」——建议不是硬规则，当硬规则会误拦一堆可发布的视频。"""
    src = _make(str(tmp_path / "长视频.mp4"), 1080, 1080, seconds=2)
    # 把建议上限压到 1s，让 2s 的测试视频触发告警，避免真造 60s 视频拖慢单测
    monkeypatch.setattr("app.publish.video.RECOMMEND_MAX_SECONDS", 1)
    r = check_video(src)
    assert r["ok"], "时长超建议值不该判不合规"
    assert "duration-warn" in r["issues"]


def test_check_video_读不出元数据判不合规(tmp_path):
    p = tmp_path / "坏文件.mp4"
    p.write_bytes(b"\x00" * 2048)
    r = check_video(str(p))
    assert not r["ok"]
    assert "unreadable" in r["issues"]


# ---- 转码 -------------------------------------------------------------------

@pytest.mark.parametrize("w,h", [
    (720, 1280),     # 9:16 竖屏 → 3:4
    (1080, 1920),    # 同比例更高清
    (640, 480),      # 4:3 横屏 → 1:1（0.75 的倒数 1.333，离 1.0 比离 1.778 近）
    (1000, 1000),    # 已是 1:1，应当 skip
])
def test_normalize_出来的视频一定合规(tmp_path, w, h):
    src = _make(str(tmp_path / f"n-{w}x{h}.mp4"), w, h)
    r = normalize_video(src, str(tmp_path / f"out-{w}x{h}.mp4"))
    assert r["status"] == "ok", r.get("err")
    # 无论走 crop 还是 skip，产物都必须过 check_video
    assert check_video(r["output"])["ok"]


def test_normalize_已合规不重编码(tmp_path):
    """重编码必然掉画质，对本来就合规的视频做一遍纯属倒扣分；也保证续跑幂等。"""
    src = _make(str(tmp_path / "已合规.mp4"), 1080, 1080)
    r = normalize_video(src, str(tmp_path / "out.mp4"))
    assert r["status"] == "ok"
    assert r["action"] == "skip"
    assert r["output"] == src, "skip 时应原样返回输入路径，不产生新文件"
    assert not os.path.exists(str(tmp_path / "out.mp4"))


def test_normalize_幂等(tmp_path):
    """对产物再跑一次必须 skip，不能反复裁切越裁越小。"""
    src = _make(str(tmp_path / "竖屏.mp4"), 720, 1280)
    r1 = normalize_video(src, str(tmp_path / "o1.mp4"))
    assert r1["action"] == "crop"
    r2 = normalize_video(r1["output"], str(tmp_path / "o2.mp4"))
    assert r2["action"] == "skip"
    assert probe_video(r1["output"])["w"] == probe_video(r2["output"])["w"]


def test_normalize_指定目标比例(tmp_path):
    src = _make(str(tmp_path / "竖屏.mp4"), 720, 1280)
    r = normalize_video(src, str(tmp_path / "out.mp4"), target_ratio="1:1")
    assert r["status"] == "ok"
    assert r["ratioName"] == "1:1"
    m = probe_video(r["output"])
    assert m["w"] == m["h"]


def test_normalize_目标比例不在允许集合时报错(tmp_path):
    src = _make(str(tmp_path / "竖屏.mp4"), 720, 1280)
    r = normalize_video(src, str(tmp_path / "out.mp4"), target_ratio="9:16")
    assert r["status"] == "error"
    assert r["action"] == "target"


def test_normalize_截断时长(tmp_path):
    """max_seconds 给了才截断；取前 N 秒——商品卖点一般在开头。"""
    src = _make(str(tmp_path / "长.mp4"), 720, 1280, seconds=4)
    r = normalize_video(src, str(tmp_path / "out.mp4"), max_seconds=2)
    assert r["status"] == "ok"
    assert r["trimmed"]
    assert probe_video(r["output"])["duration"] <= 2.5


def test_normalize_已合规但要截断时仍然转码(tmp_path):
    """比例已对、只是太长：不能因为 check_video 过了就 skip 掉截断。"""
    src = _make(str(tmp_path / "方形长.mp4"), 1080, 1080, seconds=4)
    r = normalize_video(src, str(tmp_path / "out.mp4"), max_seconds=2)
    assert r["status"] == "ok"
    assert r["action"] == "crop", "要截断时不该 skip"
    assert probe_video(r["output"])["duration"] <= 2.5


def test_normalize_源文件读不出时报错不产出文件(tmp_path):
    """宁可报「这个视频处理不了」，也不要留个半截文件让上传阶段拿去传。"""
    bad = tmp_path / "坏.mp4"
    bad.write_bytes(b"nope" * 100)
    out = tmp_path / "out.mp4"
    r = normalize_video(str(bad), str(out))
    assert r["status"] == "error"
    assert r["action"] == "probe"
    assert not os.path.exists(str(out))


def test_normalize_转码后回读校验(tmp_path):
    """ffmpeg 返回 0 不等于比例真对，必须回读——与 images 侧「写入即回读」同取向。"""
    src = _make(str(tmp_path / "竖屏.mp4"), 720, 1280)
    r = normalize_video(src, str(tmp_path / "out.mp4"))
    assert r["status"] == "ok"
    assert "outMeta" in r
    assert match_ratio(r["outMeta"]["ratio"]) == r["ratioName"]


def test_常量与平台规则一致():
    """规则值写错会让闸门形同虚设，故把平台原文的数值钉在测试里。"""
    assert set(ALLOWED_RATIOS) == {"1:1", "3:4", "16:9"}
    assert ALLOWED_RATIOS["3:4"] == 0.75
    assert MAX_SIZE_MB == 500
    assert RECOMMEND_MAX_SECONDS == 60
