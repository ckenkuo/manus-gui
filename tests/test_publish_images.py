"""发布管线图片合规化的离线单测（纯 Pillow，不碰网络、不碰 Packy API）。

只测几何处理：这部分是发布流程的刚性依赖（尺寸不合规会被 Temu 静默弹回），
且完全确定性、可离线验证。AI 编辑部分要真实 API key + 网络，不在单测范围。
"""
import os
import re

import pytest
from PIL import Image

from app.publish.images import (
    CLOTH_MIN_H,
    CLOTH_MIN_W,
    SKC_RATIO,
    batch_fit34,
    compress,
    fit_34,
    image_size,
    pick_size,
    square_image,
)


def _make(path: str, w: int, h: int, color=(120, 60, 30)) -> str:
    """造一张纯色测试图。"""
    Image.new("RGB", (w, h), color).save(path)
    return path


@pytest.mark.parametrize("w,h", [
    (800, 800),      # 正方形（1688 主图常见尺寸）
    (750, 1000),     # 已是 3:4，只需放大
    (1200, 600),     # 太宽 → 补高
    (600, 1600),     # 太高 → 补宽
    (2000, 2000),    # 已够大的正方形
])
def test_fit34_满足比例与最小尺寸(tmp_path, w, h):
    """fit_34 出图必须同时满足 3:4 比例和 ≥1340×1785 两条硬规则。"""
    src = _make(str(tmp_path / f"src-{w}x{h}.jpg"), w, h)
    r = fit_34(src, str(tmp_path / f"out-{w}x{h}.jpg"))
    assert r["status"] == "ok"
    ow, oh = image_size(r["output"])
    assert abs(ow / oh - SKC_RATIO) < 0.01, f"{ow}x{oh} 比例不是 3:4"
    assert ow >= CLOTH_MIN_W and oh >= CLOTH_MIN_H, f"{ow}x{oh} 未达 1340x1785"


def test_fit34_补白边不裁切内容(tmp_path):
    """太宽的图补高时，原内容应完整保留、上下补白，而不是被裁掉。"""
    # 左半红右半蓝，补高后中间那行应仍是红蓝各半（内容没被裁）
    im = Image.new("RGB", (1200, 600), (255, 0, 0))
    im.paste(Image.new("RGB", (600, 600), (0, 0, 255)), (600, 0))
    src = str(tmp_path / "half.jpg")
    im.save(src)
    r = fit_34(src, str(tmp_path / "half-34.jpg"))
    with Image.open(r["output"]) as out:
        ow, oh = out.size
        mid = out.crop((0, oh // 2, ow, oh // 2 + 1)).resize((2, 1))
        left, right = mid.getpixel((0, 0)), mid.getpixel((1, 0))
    assert left[0] > 200 and left[2] < 60, f"左半应是红色，实际 {left}"
    assert right[2] > 200 and right[0] < 60, f"右半应是蓝色，实际 {right}"
    # 顶部应是补的白边
    with Image.open(r["output"]) as out:
        assert out.getpixel((out.width // 2, 2))[0] > 240, "顶部应补白边"


def test_square_素材图裁方并放大(tmp_path):
    """素材图要 1:1 且 ≥800×800；默认 target=1785 同时满足服装类 ≥1340×1785。"""
    src = _make(str(tmp_path / "rect.jpg"), 750, 1000)
    r = square_image(src, str(tmp_path / "rect-sq.jpg"))
    ow, oh = image_size(r["output"])
    assert ow == oh, f"{ow}x{oh} 不是正方形"
    assert ow >= 800, f"{ow} 小于素材图下限 800"
    assert ow >= CLOTH_MIN_W and oh >= CLOTH_MIN_H, f"{ow}x{oh} 未达服装类下限"


def test_square_已达标不缩小(tmp_path):
    """已经比 target 大的正方形不该被缩小到 target（缩小可能掉回红线以下）。"""
    src = _make(str(tmp_path / "big.jpg"), 2400, 2400)
    r = square_image(src, str(tmp_path / "big-sq.jpg"))
    ow, oh = image_size(r["output"])
    assert ow == oh == 2400, f"已达标的图被改成了 {ow}x{oh}"


def test_compress_先放大后限长边(tmp_path):
    """小图要先放大到红线，不能因为长边上限就把它缩回红线以下。"""
    src = _make(str(tmp_path / "small.jpg"), 400, 500)
    out = compress(src)
    ow, oh = image_size(out)
    assert ow >= CLOTH_MIN_W and oh >= CLOTH_MIN_H, f"{ow}x{oh} 未达最小尺寸"
    assert max(ow, oh) <= 3840, f"{ow}x{oh} 超长边上限"


def test_compress_png转jpg并删原文件(tmp_path):
    """PNG 输入应转成 JPEG 控体积，原 PNG 删除。"""
    src = str(tmp_path / "x.png")
    Image.new("RGB", (1500, 2000), (10, 200, 10)).save(src)
    out = compress(src)
    assert out.endswith(".jpg")
    assert os.path.exists(out)
    assert not os.path.exists(src), "原 PNG 应被删除"


def test_pick_size_按比例选():
    """按原图宽高比选最接近的允许尺寸。"""
    assert pick_size(800, 800) == "1024x1024"
    assert pick_size(750, 1000) == "1024x1536"      # 0.75 最近 0.667
    # 16:9 有三个精确等比的候选（1536x864 / 2048x1152 / 3840x2160），min 取先命中的
    assert pick_size(1920, 1080) in ("1536x864", "2048x1152", "3840x2160")
    assert pick_size(1080, 1920) == "2160x3840"     # 竖 16:9 只有一个候选


def test_pick_size_不降采样档():
    """素材图这一路要求出图不小于原图，避免「降采样出图 + compress 插值放大」。

    2026-08-22 实测：1276x1276 按纯比例只选中 1024x1024（比原图小），成图被
    compress 拉到 1785，背景纹理明显变糊、小字消失。
    """
    assert pick_size(1276, 1276) == "1024x1024"                      # 旧行为不变
    assert pick_size(1276, 1276, no_downscale=True) == "2048x2048"    # 升到够大的档
    # 已经够大就不再往上升（省钱：生成像素直接翻倍）
    assert pick_size(900, 900, no_downscale=True) == "1024x1024"
    # 同比例里没有够大的档时取最大档，不报错
    assert pick_size(5000, 5000, no_downscale=True) == "2048x2048"


def test_batch_fit34_跳过不匹配文件(tmp_path):
    """批量处理只挑 pattern 前缀的图，其余跳过；单张坏图不中断整批。"""
    _make(str(tmp_path / "main-01.jpg"), 800, 800)
    _make(str(tmp_path / "main-02.jpg"), 900, 1200)
    _make(str(tmp_path / "desc-01.jpg"), 750, 1000)   # 不匹配 pattern，应跳过
    (tmp_path / "main-bad.jpg").write_text("not an image", encoding="utf-8")
    r = batch_fit34(str(tmp_path), pattern="main-")
    assert r["total"] == 3, f"应处理 3 个 main- 文件，实际 {r['total']}"
    assert r["done"] == 2, f"应成功 2 个（坏图失败），实际 {r['done']}"
    assert r["status"] == "partial"
    for item in r["results"]:
        if item.get("status") == "ok":
            ow, oh = image_size(item["output"])
            assert abs(ow / oh - SKC_RATIO) < 0.01


# ---- 硬性尺寸闸门（check_cloth_size）----------------------------------------
# 2026-08-23 真站取证（rowid 173539495453435641 保存报错）：描述区 11 张图里 10 张
# 仍是 1688 原始外链，尺寸 1000×1000 与 900×1200，全部低于 1340×1785，save 被静默
# 弹回。唯一达标的是已走过 compress 的英化产物 1340×2010。故闸门要能精确拦住前两种。

def test_闸门拦真站那两种描述图尺寸(tmp_path):
    from app.publish.images import check_cloth_size

    for w, h in [(1000, 1000), (900, 1200)]:
        p = _make(str(tmp_path / f"d{w}x{h}.jpg"), w, h)
        r = check_cloth_size(p)
        assert not r["ok"], f"{w}x{h} 应被拦下"
        assert r["size"] == f"{w}x{h}"
        # 报错要说清差多少，否则排查时只知道「不合规」没法动手
        assert "1340" in r["reason"] and "1785" in r["reason"]
        assert f"{CLOTH_MIN_W - w}px" in r["reason"]


def test_闸门放行达标图(tmp_path):
    from app.publish.images import check_cloth_size

    # 恰好等于下限要放行（>= 而不是 >）
    for w, h in [(1340, 1785), (1340, 2010), (2000, 3000)]:
        p = _make(str(tmp_path / f"o{w}x{h}.jpg"), w, h)
        r = check_cloth_size(p)
        assert r["ok"], f"{w}x{h} 应放行：{r['reason']}"


def test_闸门对非图片文件也判不过(tmp_path):
    """读不出尺寸不能默认放行：传上去照样会在 save 时静默弹回。"""
    from app.publish.images import check_cloth_size

    p = tmp_path / "notimage.jpg"
    p.write_bytes(b"this is not an image")
    r = check_cloth_size(str(p))
    assert not r["ok"] and r["size"] is None
    assert "读不出" in r["reason"]


@pytest.mark.parametrize("w,h", [
    (1340, 5000),    # 窄而极长：放大后长边超 3840，按 max_dim 缩会把宽压破 1340
    (800, 4000),     # 描述长图的典型形状
    (5000, 1200),    # 反向：极宽，缩下来会把高压破 1785
])
def test_compress_长边上限不得压破最小尺寸(tmp_path, w, h):
    """max_dim 只是体积保护，最小尺寸是硬红线——冲突时红线优先。

    这条以前是错的：先放大到达标、再按 max_dim 等比缩，1340×5000 会缩成
    1029×3840，宽已破线。描述长图天生窄而极长，是高发场景。
    """
    src = _make(str(tmp_path / f"long-{w}x{h}.jpg"), w, h)
    out = compress(src)
    ow, oh = image_size(out)
    assert ow >= CLOTH_MIN_W and oh >= CLOTH_MIN_H, \
        f"{w}x{h} 压成 {ow}x{oh}，破了 {CLOTH_MIN_W}x{CLOTH_MIN_H} 红线"


def test_compress_常规图仍受长边上限约束(tmp_path):
    """下限保护只在会破线时才让步，不该把 max_dim 整体废掉。"""
    from app.publish.images import MAX_DIM

    # 5000×5000 缩到 3840×3840 既不超上限也不破下限，应正常生效
    src = _make(str(tmp_path / "big.jpg"), 5000, 5000)
    out = compress(src)
    ow, oh = image_size(out)
    assert max(ow, oh) <= MAX_DIM
    assert ow >= CLOTH_MIN_W and oh >= CLOTH_MIN_H


# ---- desc_save 的落库前尺寸关口 ---------------------------------------------

@pytest.mark.asyncio
async def test_desc_save_尺寸不达标也判validation_error():
    """图床与尺寸是两条独立校验：已转存到店小秘的图也可能仍是 900×1200。

    只查图床看不出这种情况，而它照样会让阶段⑫ save 静默弹回
    （2026-08-23 真站实测：描述区 10 张图都在 1688 外链且全部破线）。
    """
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            if "smt-desc-content" in js and "保存" in js:
                # 全部已转存到店小秘图床，但有两张尺寸不达标
                return {"stillOpen": False, "descImgs": 3, "dxmHosted": 3,
                        "tooSmall": [{"pos": 1, "size": "900x1200"},
                                     {"pos": 2, "size": "1000x1000"}],
                        "foreignHosts": []}
            return {"open": True, "hasButton": True, "modalCount": 1,
                    "count": 3, "srcs": ["a", "b", "c"], "sizes": [], "usingCount": 0}

    r = await pipeline.desc_save(_S())
    assert r["status"] == "validation-error"
    assert len(r["tooSmall"]) == 2
    assert "1340x1785" in r["note"]


@pytest.mark.asyncio
async def test_desc_save_全达标且已转存才算ok():
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            if "smt-desc-content" in js and "保存" in js:
                return {"stillOpen": False, "descImgs": 3, "dxmHosted": 3,
                        "tooSmall": [], "foreignHosts": []}
            return {"open": True, "hasButton": True, "modalCount": 1,
                    "count": 3, "srcs": ["a", "b", "c"], "sizes": [], "usingCount": 0}

    r = await pipeline.desc_save(_S())
    assert r["status"] == "ok" and r["note"] == ""


def test_desc_save_js_带尺寸下限占位符():
    """占位符没被替换会让 JS 里出现字面 __MINW__，判据直接失效。"""
    from app.publish.pipeline import _JS_DESC_SAVE

    assert "__MINW__" in _JS_DESC_SAVE and "__MINH__" in _JS_DESC_SAVE
    assert "naturalWidth" in _JS_DESC_SAVE
    # 未加载完（0）不能当成不达标，否则会误报一堆
    assert "if (w && h" in _JS_DESC_SAVE


# ---- 描述编辑器必须让出屏幕 --------------------------------------------------
# 2026-08-24 实测：跑完阶段⑬ 后描述编辑器（全屏 modal，2560×1257）留着，阶段⑦ SKC
# 换图连续两次报 open-space「瞄点未命中」——看着像滚动时序脆点，诊断 elementFromPoint
# 才发现瞄点落在描述弹窗的 .page-content 上。故收尾要显式确认关闭，且失败信息要能
# 指向真正的原因。

@pytest.mark.asyncio
async def test_ensure_desc_closed_未开时直接放过():
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            return {"already": True}

    r = await pipeline.ensure_desc_closed(_S())
    assert r["status"] == "ok" and r["wasOpen"] is False


@pytest.mark.asyncio
async def test_ensure_desc_closed_关掉后遮罩也清了():
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            return {"stillOpen": False, "visibleMasks": 0}

    r = await pipeline.ensure_desc_closed(_S())
    assert r["status"] == "ok" and r["wasOpen"] is True


@pytest.mark.asyncio
async def test_ensure_desc_closed_关不掉要报错():
    """关不掉必须报出来：它会让后续每一个坐标点击阶段静默失败。"""
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            return {"stillOpen": True, "visibleMasks": 1}

    r = await pipeline.ensure_desc_closed(_S())
    assert r["status"] == "error" and "仍开着" in r["reason"]


def test_skc瞄点失败要报出遮挡物():
    """只说「瞄点未命中」会把排查引向时序，必须带上是什么盖着。"""
    from app.publish.pipeline import _JS_SKC_BTN_POS

    assert "blockers" in _JS_SKC_BTN_POS
    assert "ant-modal-wrap" in _JS_SKC_BTN_POS
    assert "atClass" in _JS_SKC_BTN_POS


def test_skc行图数上下限常量():
    """表头写的是「图片(3-10张)」，两头都是保存时的硬校验。"""
    from app.publish.pipeline import SKC_ROW_MAX_IMAGES, SKC_ROW_MIN_IMAGES

    assert SKC_ROW_MIN_IMAGES == 3 and SKC_ROW_MAX_IMAGES == 10


@pytest.mark.asyncio
async def test_skc新图超上限在入口拦下(monkeypatch, tmp_path):
    """新图超过 10 张时入口就拦，不动页面：多出来的会静默挂不进去。"""
    from app.publish import pipeline

    for i in range(11):
        _make(str(tmp_path / f"{i:02d}.jpg"), CLOTH_MIN_W, CLOTH_MIN_H)

    called = {"state": 0}

    async def fake_state(session, kw):
        called["state"] += 1
        return {"count": 4, "urls": [], "sizes": [], "tooSmall": []}

    monkeypatch.setattr(pipeline, "_skc_row_state", fake_state)
    r = await pipeline.skc_replace_row(None, "咖啡色", str(tmp_path))
    assert r["status"] == "error" and r["stage"] == "precheck", r
    assert called["state"] == 0, "入口拦下时不该去读页面状态"


@pytest.mark.asyncio
async def test_skc新图不足下限不在入口拦(monkeypatch, tmp_path):
    """新图少于 3 张【不能】在这里拦——多颜色商品每行只分到 1~2 张是常态。

    2026-08-24 曾在这里拦「< 3 张」，结果 product-985713733384 的 4 个颜色行全被拦死，
    还把「入口拦下」虚报成「换图失败」，掩盖了真失败。下限由 service 那一层负责：
    先 _pad_row_images 补齐，补不上再报人工。
    """
    from app.publish import pipeline

    for i in range(1):                 # 只有 1 张
        _make(str(tmp_path / f"{i:02d}.jpg"), CLOTH_MIN_W, CLOTH_MIN_H)

    async def fake_state(session, kw):
        return {"count": 0, "srcs": [], "urls": [], "sizes": [], "tooSmall": [],
                "foreign": []}

    async def fake_upload(session, path, full_cid=None, **kw):
        return {"status": "ok", "fileId": "wxalbum/x.jpg", "url": "https://x/x.jpg"}

    async def fake_open(session, kw):
        return {"opened": True}

    async def fake_pick_many(session, fids):
        # 2026-08-26 起按批挂图（一次弹窗勾多张），注入点从 _pick_from_space 换到这里
        return {"stage": "ok", "picked": list(fids), "counted": len(fids)}

    monkeypatch.setattr(pipeline, "_skc_row_state", fake_state)
    monkeypatch.setattr(pipeline, "upload_image", fake_upload)
    monkeypatch.setattr(pipeline, "_skc_open_space", fake_open)
    monkeypatch.setattr(pipeline, "_pick_many_from_space", fake_pick_many)
    r = await pipeline.skc_replace_row(None, "米色马甲", str(tmp_path))
    # 走到了 verify-row（假 state 恒返回空 srcs），关键是【没有】被 precheck 拦
    assert r.get("stage") != "precheck", r


# ---- 描述模块删除：pos 与 data-idx 的映射 ------------------------------------
# 2026-08-24 真站取证（rowid 173539495454339053）：描述区图文混排，19 个模块里
# data-idx=0 是「文字」模块（存 offer JSON、不含图片盒子），18 张图对应 data-idx
# 1..18。原实现直接拿 pos-1 当 data-idx，删 pos 3/2 实际删掉 pos 2/1，删 pos 1
# 命中文字模块——图片数不变被判「删除失败」，而前两张已经删错了对象。


def test_desc_idxmap_js_只收含图模块():
    """映射必须按模块自身 data-idx 建立，且只收含图片盒子的模块。"""
    from app.publish.pipeline import _JS_DESC_IDX_MAP

    assert "desc-img-box img" in _JS_DESC_IDX_MAP
    assert "getAttribute('data-idx')" in _JS_DESC_IDX_MAP
    # 必须限定在内容区容器内，否则会把左侧列表项也扫进来
    assert "smt-content-center" in _JS_DESC_IDX_MAP


def test_desc_delete_js_按dataid判成败():
    """判据不能只看图片计数：删到非图片模块时计数不变，会误判成失败。"""
    from app.publish.pipeline import _JS_DESC_DELETE

    assert "data-id" in _JS_DESC_DELETE
    assert "targetId" in _JS_DESC_DELETE


@pytest.mark.asyncio
async def test_desc_delete_图文混排时按映射删而不是pos减一():
    """核心回归：文字模块占了 data-idx=0，删 pos 1 必须点 data-idx=1。"""
    from app.publish import pipeline

    clicked = []

    class _S:
        async def eval_json(self, js):
            if "const map = []" in js:              # _JS_DESC_IDX_MAP
                # 首项是文字模块，18 张图落在 data-idx 1..18
                return {"map": [str(i) for i in range(1, 19)], "modCount": 19}
            if "icon_delete" in js:                 # _JS_DESC_DELETE
                m = re.search(r"data-idx=\"' \+ (\d+)", js) or \
                    re.search(r"'\.using-item\[data-idx=\"' \+ (\d+)", js)
                # 占位符已被替换成字面量，直接从 JS 里抠出来
                m = re.search(r"\+ (\d+) \+", js)
                clicked.append(m.group(1) if m else None)
                return {"deleted": True, "before": 18, "after": 17}
            return {"open": True, "hasButton": True, "modalCount": 1,
                    "count": 18, "srcs": ["u%d" % i for i in range(18)],
                    "sizes": [], "usingCount": 19}

    r = await pipeline.desc_delete(_S(), [1, 3])
    assert r["status"] == "ok", r
    # 倒序删：先 pos 3 -> data-idx 3，再 pos 1 -> data-idx 1（不是 2 和 0）
    assert clicked == ["3", "1"], clicked


@pytest.mark.asyncio
async def test_desc_delete_映射数与图数不符时拒删():
    """结构与预期不符时宁可不动手——错位删除会删掉不该删的图。"""
    from app.publish import pipeline

    class _S:
        async def eval_json(self, js):
            if "const map = []" in js:
                return {"map": ["1", "2"], "modCount": 3}   # 只映射到 2 个，实际 18 张
            if "icon_delete" in js:
                raise AssertionError("映射对不上时不该点删除")
            return {"open": True, "hasButton": True, "modalCount": 1,
                    "count": 18, "srcs": [], "sizes": [], "usingCount": 19}

    r = await pipeline.desc_delete(_S(), [1])
    assert r["status"] == "error" and r["stage"] == "idxmap"
