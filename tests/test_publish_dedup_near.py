"""近重复图去重的离线单测（app/publish/images.ahash + extract._dedup_near）。

【为什么值得单测】md5 去重抓不到同一张摄影的不同裁切版本：2026-08-24 实测
product-957056453209 的 main-04（750×1000）是 main-06（1920×1920）的 3:4 中心裁切，
两张都被挂进同一个 SKC 颜色行，成品页上肉眼可见两张重复图。这类缺陷单测不覆盖就
只能靠人工看成品发现，代价极高。

不覆盖的部分：阈值 6 在多颜色商品上的泛化（同款不同色的平铺图构图高度一致，
可能落在阈值内）。那要真实多色样本，属真站验证。
"""
import os
import random

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.publish import images
from app.publish.extract import _dedup_near, dedup_images


def _photo(path: str, w: int, h: int, seed: int = 0) -> str:
    """造一张「像照片」的测试图：几个随机亮斑 + 高斯模糊。

    【夹具形状是调出来的，别随手简化】先试过纯色（ahash 全 0，任何两张都判重复）、
    横向渐变 + 方块（渐变主导哈希，裁切后距离反而涨到 10）、8×8 块状随机
    （不同图距离 28-38 很好，但裁切距离也是 28——高频细节被裁切破坏）。
    真实照片是【空间平滑的低频结构】，只有模糊斑点能同时满足两个要求：
      - 不同 seed 距离够大（实测最小 12）→ 不会被误判重复
      - 中心裁切后仍相近（实测 5）→ 能被判出重复
    这个 5 / 12 的分布与真实样本的 4 / 10 吻合，测试才有意义。
    """
    rnd = random.Random(seed * 977 + 13)
    im = Image.new("RGB", (w, h), (30, 30, 40))
    dr = ImageDraw.Draw(im)
    for _ in range(5):
        cx, cy = rnd.uniform(0.15, 0.85) * w, rnd.uniform(0.15, 0.85) * h
        r = rnd.uniform(0.18, 0.3) * min(w, h)
        v = rnd.randrange(90, 256)
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(v, v, v))
    im.filter(ImageFilter.GaussianBlur(min(w, h) * 0.04)).save(path)
    return path


def test_ahash_none_on_unreadable(tmp_path):
    """读不出的文件返回 None——绝不能当成重复（会静默丢图）。"""
    assert images.ahash(str(tmp_path / "nope.jpg")) is None
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert images.ahash(str(bad)) is None


def test_unreadable_never_judged_duplicate(tmp_path):
    """算不出哈希的图单独成组，不与任何图判重复。"""
    a = _photo(str(tmp_path / "main-01.jpg"), 400, 500, seed=0)
    bad = tmp_path / "main-02.jpg"
    bad.write_bytes(b"not an image")
    uniq, dupes = _dedup_near([a, str(bad)])
    assert dupes == {}
    assert len(uniq) == 2


def test_crop_variant_is_near_duplicate(tmp_path):
    """同一画面的裁切版本判近重复（md5 不同，只有 ahash 能抓）。"""
    big = _photo(str(tmp_path / "main-06.jpg"), 1200, 1200, seed=0)
    with Image.open(big) as im:
        # 3:4 中心裁切，模拟实测里 main-04 与 main-06 的关系
        w, h = im.size
        nw = round(h * 3 / 4)
        small = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h)).resize((750, 1000))
    crop = str(tmp_path / "main-04.jpg")
    small.save(crop)

    import hashlib
    assert (hashlib.md5(open(big, "rb").read()).hexdigest()
            != hashlib.md5(open(crop, "rb").read()).hexdigest()), "md5 应当不同，否则测不到 ahash"
    assert images.is_near_duplicate(big, crop)


def test_near_group_keeps_largest_area(tmp_path):
    """近重复组保留像素面积最大的那张——避开后续 fit_34 放大小图的画质损失。"""
    big = _photo(str(tmp_path / "main-06.jpg"), 1200, 1200, seed=0)
    with Image.open(big) as im:
        im.resize((300, 300)).save(str(tmp_path / "main-04.jpg"))
    small = str(tmp_path / "main-04.jpg")

    uniq, dupes = _dedup_near([small, big])   # 故意把小图放前面
    assert [os.path.basename(p) for p in uniq] == ["main-06.jpg"]
    assert dupes == {"main-04.jpg": "main-06.jpg"}


def test_main_wins_over_desc_despite_area(tmp_path):
    """main 优先压在面积之前：阶段⑥⑦ 只认 main-NN，留 desc 会让这批画面对它们不可见。"""
    desc_big = _photo(str(tmp_path / "desc-01.jpg"), 1200, 1200, seed=0)
    with Image.open(desc_big) as im:
        im.resize((400, 400)).save(str(tmp_path / "main-01.jpg"))
    main_small = str(tmp_path / "main-01.jpg")

    uniq, dupes = _dedup_near([main_small, desc_big])
    assert [os.path.basename(p) for p in uniq] == ["main-01.jpg"]
    assert dupes == {"desc-01.jpg": "main-01.jpg"}


def test_distinct_images_not_merged(tmp_path):
    """画面不同的图不该被合并（误杀比漏判严重：会让某颜色一张图都不剩）。"""
    a = _photo(str(tmp_path / "main-01.jpg"), 600, 800, seed=0)
    b = _photo(str(tmp_path / "main-02.jpg"), 600, 800, seed=3)
    uniq, dupes = _dedup_near([a, b])
    assert dupes == {}
    assert len(uniq) == 2


def test_order_preserved(tmp_path):
    """存活图的顺序按入参还原：enrich_vision 的编号清单依赖 main 在前的顺序。"""
    files = [_photo(str(tmp_path / f"main-{i:02d}.jpg"), 600, 800, seed=i) for i in range(1, 4)]
    uniq, dupes = _dedup_near(files)
    assert [os.path.basename(p) for p in uniq] == ["main-01.jpg", "main-02.jpg", "main-03.jpg"]


def test_dedup_images_rehops_md5_map(tmp_path):
    """md5 映射指向的首见文件若被近重复轮淘汰，映射要改指存活者。

    否则 complianceNotes 的 duplicateOf 指到一个不在 uniq 里的名字，
    阶段⑪ 顺着它找首见标注就找不到。
    """
    # main-01 是大图；desc-01 与 main-01 字节完全相同（md5 轮把 desc-01 → main-01）；
    # main-02 是 main-01 的放大版（面积更大，近重复轮会淘汰 main-01 保留 main-02）。
    big = _photo(str(tmp_path / "main-01.jpg"), 600, 800, seed=0)
    import shutil
    shutil.copy(big, str(tmp_path / "desc-01.jpg"))
    with Image.open(big) as im:
        im.resize((1200, 1600)).save(str(tmp_path / "main-02.jpg"))

    uniq, dupes = dedup_images(str(tmp_path))
    names = [os.path.basename(p) for p in uniq]
    assert names == ["main-02.jpg"], names
    # 三个映射都必须指向存活的 main-02
    assert dupes == {"main-01.jpg": "main-02.jpg", "desc-01.jpg": "main-02.jpg"}, dupes
