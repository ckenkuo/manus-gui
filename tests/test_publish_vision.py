"""阶段① 视觉回填的离线单测（mock LLM，不碰网络、不碰浏览器）。

测的是「模型答案 → product-info.json」这段确定性逻辑：md5 去重、main 优先、
重复图标注继承、已有值不被覆盖、图片按魔术字节判 mime。
真实看图质量没法离线断言，故 ask_json_with_images 整体 mock 掉。

为什么这几条值得钉住：去重顺序错了会把标注挂到 desc 上（阶段⑥ 按 main-NN 找素材图
就查不到）；重复图漏标 clean=False 会让阶段⑥⑦⑪ 把重复图当候选；覆盖已有值会把
人工标注抹掉——三条都是静默出错、要到发布被弹回才发现。
"""
import base64
import json
import os

import pytest

from app.publish.extract import _merge_vision, dedup_images, enrich_vision
from app.publish.llm import image_ref

# 最小合法图片字节：只要头部魔术字节对，本模块的逻辑就不读像素
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"A" * 40
_JPEG2 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"B" * 40
_PNG = b"\x89PNG\r\n\x1a\n" + b"C" * 40


def _write(d, name: str, data: bytes) -> str:
    p = os.path.join(str(d), name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_去重按md5且main优先(tmp_path):
    """desc 与 main 同图时首见文件必须是 main：阶段⑥ 素材图只认 main-NN。"""
    _write(tmp_path, "main-01.jpg", _JPEG)
    _write(tmp_path, "main-02.jpg", _JPEG2)
    _write(tmp_path, "desc-01.jpg", _JPEG)   # 与 main-01 同图
    _write(tmp_path, "desc-02.jpg", _PNG)    # 独有
    uniq, dupes = dedup_images(str(tmp_path))
    assert [os.path.basename(p) for p in uniq] == [
        "main-01.jpg", "main-02.jpg", "desc-02.jpg"]
    assert dupes == {"desc-01.jpg": "main-01.jpg"}


def test_去重忽略非规范文件名(tmp_path):
    """只认 main-NN / desc-NN；处理产物（如 main-01-34.jpg）不该混进看图清单。"""
    _write(tmp_path, "main-01.jpg", _JPEG)
    _write(tmp_path, "main-01-34.jpg", _JPEG2)
    _write(tmp_path, "product-info.json", b"{}")
    uniq, _ = dedup_images(str(tmp_path))
    assert [os.path.basename(p) for p in uniq] == ["main-01.jpg"]


def test_重复图继承标注但用自己的文件名(tmp_path):
    """重复图要有自己的 file 名，否则按文件名查标注会查不到（曾经的 bug）。"""
    info = {"imageUnderstanding": {}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {}}
    vision = {"complianceNotes": {"files": [
        {"file": "main-01.jpg", "chinese": False, "clean": True, "kind": "平铺"}]}}
    _merge_vision(info, vision, {"desc-01.jpg": "main-01.jpg"})
    by_name = {e["file"]: e for e in info["complianceNotes"]["files"]}
    assert set(by_name) == {"main-01.jpg", "desc-01.jpg"}
    dup = by_name["desc-01.jpg"]
    assert dup["duplicate"] is True and dup["duplicateOf"] == "main-01.jpg"
    # 重复图一律 clean=False：对阶段⑥⑦⑪ 来说它是「不该再用的图」
    assert dup["clean"] is False
    assert dup["kind"] == "平铺"  # 其余标注继承首见图
    assert info["complianceNotes"]["cleanFiles"] == ["main-01.jpg"]


def test_已有值不被模型覆盖():
    """人工补过的标注比模型可靠，不许被新一轮覆盖。"""
    info = {"imageUnderstanding": {"product": "人工写的"}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {"files": [{"file": "x.jpg"}]}}
    vision = {"imageUnderstanding": {"product": "模型写的"},
              "sizeChart": {"120": "身高120cm"},
              "complianceNotes": {"files": [{"file": "y.jpg", "clean": True}]}}
    _merge_vision(info, vision, {})
    assert info["imageUnderstanding"]["product"] == "人工写的"
    assert info["complianceNotes"]["files"] == [{"file": "x.jpg"}]
    assert info["sizeChart"] == {"120": "身高120cm"}  # 空字段照填


def test_模型返回空字段不写坏原值():
    """模型说「图里没有尺码表」返回 {}，不该把字段变成 None 或删掉。"""
    info = {"imageUnderstanding": {}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {}}
    stat = _merge_vision(info, {"sizeChart": {}, "sizeMeasurements": {}}, {})
    assert info["sizeChart"] == {} and info["sizeMeasurements"] == {}
    assert stat["sizeChart"] == 0


def test_image_ref_按魔术字节判mime(tmp_path):
    """extract.py 一律存成 .jpg，扩展名不可信；PNG 套 image/jpeg 会被网关判损坏图。"""
    png = _write(tmp_path, "main-01.jpg", _PNG)  # 扩展名是 jpg，实际是 PNG
    assert image_ref(png).startswith("data:image/png;base64,")
    jpg = _write(tmp_path, "main-02.jpg", _JPEG)
    assert image_ref(jpg).startswith("data:image/jpeg;base64,")


def test_image_ref_data_url原样返回():
    """已是 data URL 的原样透传，不去读盘。"""
    u = "data:image/png;base64,AAAA"
    assert image_ref(u) == u


def test_image_ref_远端url下载转base64(monkeypatch):
    """http(s) 外链必须下载转 data URL，不能透传。

    钉这条的原因：Kimi 端点对远程图片 URL 一律 400 unsupported image url，
    透传会让阶段⑬ 描述图（唯一直接传外链的地方）在切模型后必挂。
    """
    called = {}

    def fake_download(url, dst, retries=3):
        called["url"] = url
        with open(dst, "wb") as f:
            f.write(_PNG)
        return len(_PNG)

    monkeypatch.setattr("app.publish.extract._download_image", fake_download)
    ref = image_ref("https://cbu01.alicdn.com/a.jpg")
    assert called["url"] == "https://cbu01.alicdn.com/a.jpg"
    # mime 按下载到的真实字节判（URL 后缀是 .jpg，字节是 PNG）
    assert ref.startswith("data:image/png;base64,")
    assert base64.b64decode(ref.split(",", 1)[1]) == _PNG


def test_image_ref_远端url下载失败要抛(monkeypatch):
    """下载失败按主流程语义抛出，不静默返回原 URL——否则又变成透传。"""
    def boom(url, dst, retries=3):
        raise RuntimeError("连接重置")

    monkeypatch.setattr("app.publish.extract._download_image", boom)
    with pytest.raises(RuntimeError):
        image_ref("https://cbu01.alicdn.com/a.jpg")


@pytest.mark.asyncio
async def test_enrich_vision_落盘并统计(tmp_path, monkeypatch):
    """整条回填链路：读 info → 去重 → 问模型（mock）→ 合并 → 写回。"""
    _write(tmp_path, "main-01.jpg", _JPEG)
    _write(tmp_path, "main-02.jpg", _JPEG2)
    _write(tmp_path, "desc-01.jpg", _JPEG)  # 与 main-01 重复
    info_path = os.path.join(str(tmp_path), "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"title": "男童POLO套装", "colors": ["灰色"], "sizes": ["120cm"],
                   "attributes": {}, "imageUnderstanding": {}, "sizeChart": {},
                   "sizeMeasurements": {}, "complianceNotes": {}}, f,
                  ensure_ascii=False)

    seen = {}

    async def fake_ask(prompt, images, what="判断", system=None, stage=None):
        seen["images"] = images
        seen["prompt"] = prompt
        return {
            "imageUnderstanding": {"product": "两件套", "colors": {"灰色": "灰身藏青袖"}},
            "sizeChart": {"120": "身高115-125cm"},
            "sizeMeasurements": {"120": {"衣长": 48, "胸围": 78}},
            "complianceNotes": {"files": [
                {"file": "main-01.jpg", "chinese": True, "clean": False},
                {"file": "main-02.jpg", "chinese": False, "clean": True},
            ]},
        }

    monkeypatch.setattr("app.publish.llm.ask_json_with_images", fake_ask)
    r = await enrich_vision(info_path)

    # 只把唯一图交给模型（重复的 desc-01 不占 token）
    assert len(seen["images"]) == 2
    assert r["duplicates"] == {"desc-01.jpg": "main-01.jpg"}
    assert r["filled"] == {"imageUnderstanding": 2, "sizeChart": 1,
                           "sizeMeasurements": 1, "complianceNotes": 3,
                           "cleanFiles": 1}
    with open(info_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["sizeMeasurements"]["120"]["胸围"] == 78
    # 尺码键不带「码」字（阶段⑨ add_sizechart 直接拿它对齐弹窗尺码行）
    assert all(not k.endswith("码") for k in saved["sizeChart"])
    assert saved["complianceNotes"]["cleanFiles"] == ["main-02.jpg"]


@pytest.mark.asyncio
async def test_enrich_vision_超上限截断报truncated(tmp_path, monkeypatch):
    """长图商品撑爆 token 前先截断，且保留 main（信息密度高于 desc）。"""
    for i in range(1, 4):
        _write(tmp_path, f"main-0{i}.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + bytes([i]) * 30)
    _write(tmp_path, "desc-01.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"Z" * 30)
    info_path = os.path.join(str(tmp_path), "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"title": "t", "imageUnderstanding": {}, "sizeChart": {},
                   "sizeMeasurements": {}, "complianceNotes": {}}, f)

    sent = {}

    async def fake_ask(prompt, images, what="判断", system=None, stage=None):
        sent["n"] = len(images)
        return {}

    monkeypatch.setattr("app.publish.llm.ask_json_with_images", fake_ask)
    r = await enrich_vision(info_path, max_images=2)
    assert sent["n"] == 2
    assert r["uniqueImages"] == ["main-01.jpg", "main-02.jpg"]
    assert r["truncated"] == ["main-03.jpg", "desc-01.jpg"]


@pytest.mark.asyncio
async def test_enrich_vision_无图直接报错(tmp_path):
    """目录里没图就该失败，不该悄悄写个空壳回 product-info.json。"""
    info_path = os.path.join(str(tmp_path), "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"title": "t"}, f)
    with pytest.raises(RuntimeError, match="没有 main-NN"):
        await enrich_vision(info_path)
