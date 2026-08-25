"""SKC 行选图补到下限的离线单测（_pad_row_images）。

【为什么需要补齐】平台要求每行 3~10 张，而视觉按颜色归属给每行只分到 1~2 张是常态
——一件衣服的某个颜色不会有 6 张独立照片。2026-08-24 实测 product-985713733384：
8 张主图分 4 个颜色，每行 1~2 张。不补齐就是换完保存被静默拦下（只有区块变红）。

【为什么补同款其它主图是合理的】平铺、细节、材质图不体现颜色差异，挂在任何颜色行下
都说得通，这也是人工发布时的做法。
"""
import os

import pytest

from app.publish.service import _pad_row_images
from app.publish.pipeline import SKC_ROW_MIN_IMAGES


def _mk(tmp_path, n: int) -> list:
    out = []
    for i in range(1, n + 1):
        f = tmp_path / f"main-{i:02d}.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0stub")
        out.append(str(f))
    return out


def _info(notes: list) -> dict:
    return {"colors": ["米色马甲", "灰色开衫"], "complianceNotes": {"files": notes}}


def test_一张补到下限(tmp_path):
    """只分到 1 张时补 2 张，凑够 3 张。"""
    files = _mk(tmp_path, 6)
    info = _info([{"file": f"main-{i:02d}.jpg", "clean": True, "kind": "平铺"}
                  for i in range(1, 7)])
    out, added = _pad_row_images([files[0]], info, str(tmp_path))
    assert len(out) == SKC_ROW_MIN_IMAGES
    assert added == ["main-02.jpg", "main-03.jpg"]


def test_原选图保持在最前(tmp_path):
    """首位必须仍是该颜色的主图：补进来的排在后面。"""
    files = _mk(tmp_path, 6)
    info = _info([{"file": f"main-{i:02d}.jpg", "clean": True, "kind": "平铺"}
                  for i in range(1, 7)])
    out, added = _pad_row_images([files[4]], info, str(tmp_path))   # 选的是 main-05
    assert os.path.basename(out[0]) == "main-05.jpg", out
    assert "main-05.jpg" not in added


def test_已够下限不补(tmp_path):
    files = _mk(tmp_path, 6)
    info = _info([{"file": f"main-{i:02d}.jpg", "clean": True, "kind": "平铺"}
                  for i in range(1, 7)])
    out, added = _pad_row_images(files[:3], info, str(tmp_path))
    assert len(out) == 3 and added == []


def test_排除重复图与尺码表(tmp_path):
    """补齐不能把重复图或尺码表图拉进来——那等于把已知的坏图挂上去。"""
    files = _mk(tmp_path, 5)
    info = _info([
        {"file": "main-01.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-02.jpg", "duplicate": True, "duplicateOf": "main-01.jpg"},
        {"file": "main-03.jpg", "kind": "尺码表"},
        {"file": "main-04.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-05.jpg", "clean": True, "kind": "平铺"},
    ])
    out, added = _pad_row_images([files[0]], info, str(tmp_path))
    assert added == ["main-04.jpg", "main-05.jpg"], added


def test_干净图优先(tmp_path):
    """有水印/中文的图排在干净图后面——与 plan_skc 同一套排序取向。"""
    files = _mk(tmp_path, 4)
    info = _info([
        {"file": "main-01.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-02.jpg", "clean": False, "chinese": True, "kind": "平铺"},
        {"file": "main-03.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-04.jpg", "clean": True, "kind": "平铺"},
    ])
    out, added = _pad_row_images([files[0]], info, str(tmp_path))
    assert added == ["main-03.jpg", "main-04.jpg"], added
    assert "main-02.jpg" not in added


def test_可用图不足时如实返回(tmp_path):
    """全库可用图都凑不够 3 张时不硬凑：返回实际张数，由调用方报人工。"""
    files = _mk(tmp_path, 2)
    info = _info([
        {"file": "main-01.jpg", "clean": True, "kind": "平铺"},
        {"file": "main-02.jpg", "kind": "尺码表"},
    ])
    out, added = _pad_row_images([files[0]], info, str(tmp_path))
    assert len(out) < SKC_ROW_MIN_IMAGES, out
    assert added == []


def test_无标注时也能补(tmp_path):
    """没跑过视觉回填（notes 为空）时按文件名序补。"""
    files = _mk(tmp_path, 5)
    out, added = _pad_row_images([files[0]], {"colors": ["米色马甲"]}, str(tmp_path))
    assert len(out) == SKC_ROW_MIN_IMAGES
    assert added == ["main-02.jpg", "main-03.jpg"]
