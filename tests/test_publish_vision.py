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


async def _no_llm(prompt, what="", retries=3, stage=None, **kw):
    """本地路径的哨兵：这些用例本该零调用，真发出去就是本地定序失灵了。"""
    raise AssertionError(f"不该调用 LLM（{what}）：本地应能定出尺码对应")


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


@pytest.mark.asyncio
async def test_重复图继承标注但用自己的文件名(tmp_path):
    """重复图要有自己的 file 名，否则按文件名查标注会查不到（曾经的 bug）。"""
    info = {"imageUnderstanding": {}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {}}
    vision = {"complianceNotes": {"files": [
        {"file": "main-01.jpg", "chinese": False, "clean": True, "kind": "平铺"}]}}
    await _merge_vision(info, vision, {"desc-01.jpg": "main-01.jpg"})
    by_name = {e["file"]: e for e in info["complianceNotes"]["files"]}
    assert set(by_name) == {"main-01.jpg", "desc-01.jpg"}
    dup = by_name["desc-01.jpg"]
    assert dup["duplicate"] is True and dup["duplicateOf"] == "main-01.jpg"
    # 重复图一律 clean=False：对阶段⑥⑦⑪ 来说它是「不该再用的图」
    assert dup["clean"] is False
    assert dup["kind"] == "平铺"  # 其余标注继承首见图
    assert info["complianceNotes"]["cleanFiles"] == ["main-01.jpg"]


@pytest.mark.asyncio
async def test_已有值不被模型覆盖():
    """人工补过的标注比模型可靠，不许被新一轮覆盖。"""
    info = {"imageUnderstanding": {"product": "人工写的"}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {"files": [{"file": "x.jpg"}]}}
    vision = {"imageUnderstanding": {"product": "模型写的"},
              "sizeChart": {"120": "身高120cm"},
              "complianceNotes": {"files": [{"file": "y.jpg", "clean": True}]}}
    await _merge_vision(info, vision, {})
    assert info["imageUnderstanding"]["product"] == "人工写的"
    assert info["complianceNotes"]["files"] == [{"file": "x.jpg"}]
    assert info["sizeChart"] == {"120": "身高120cm"}  # 空字段照填


@pytest.mark.asyncio
async def test_模型返回空字段不写坏原值():
    """模型说「图里没有尺码表」返回 {}，不该把字段变成 None 或删掉。"""
    info = {"imageUnderstanding": {}, "sizeChart": {},
            "sizeMeasurements": {}, "complianceNotes": {}}
    stat = await _merge_vision(info, {"sizeChart": {}, "sizeMeasurements": {}}, {})
    assert info["sizeChart"] == {} and info["sizeMeasurements"] == {}
    assert stat["sizeChart"] == 0


def test_image_ref_按魔术字节判mime(tmp_path):
    """extract.py 一律存成 .jpg，扩展名不可信；PNG 套 image/jpeg 会被网关判损坏图。"""
    png = _write(tmp_path, "main-01.jpg", _PNG)  # 扩展名是 jpg，实际是 PNG
    assert image_ref(png).startswith("data:image/png;base64,")
    jpg = _write(tmp_path, "main-02.jpg", _JPEG)
    assert image_ref(jpg).startswith("data:image/jpeg;base64,")


# ---- 无尺码列的纯数值行表（2026-09-02，offer 1075672160285 取证）-------------
# 那张表（desc-04.jpg）表头只有「衣长 胸围 肩宽 重量」、四行数值不写行首尺码，
# 模型认出了表却因 {尺码: {参数: 值}} 凑不出键而整表返回 {}，实测值被结构卡掉。

@pytest.mark.asyncio
async def test_数值码在本地定序不调模型(monkeypatch):
    """数值码本地取数字就能排，是确定性的，不该多花一次 LLM 调用。"""
    called = []

    async def fake_ask(prompt, what="", retries=3, stage=None, **kw):
        called.append(what)
        return {"mapping": {}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {},
            # 刻意乱序给，验证对齐按 norm_size 升序而不是列表原序
            "sizes": ["130码衣标码14号", "100码衣标码8号",
                      "120码衣标码12号", "110码衣标码10号"]}
    vision = {"sizeMeasurements": {}, "sizeMeasurementsRows": {
        "params": ["衣长", "胸围", "肩宽", "重量"],
        "rows": [[34, 33, 25, 123], [36, 35, 26, 132],
                 [38, 37, 27, 141], [40, 39, 28, 150]]}}
    stat = await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"]["100码衣标码8号"] == {
        "衣长": 34, "胸围": 33, "肩宽": 25, "重量": 123}
    assert info["sizeMeasurements"]["130码衣标码14号"] == {
        "衣长": 40, "胸围": 39, "肩宽": 28, "重量": 150}
    assert stat["sizeMeasurements"] == 4
    # 留取证痕迹：阶段⑨ 填出去的键是推来的，人工排查要能看出来
    assert info["sizeMeasurementsSource"] == "visionRows"
    assert called == []      # 本地定得出，一次调用都不发


@pytest.mark.asyncio
async def test_大码在前的表按数值走向倒序对齐(monkeypatch):
    """商家把大码写在上面真实存在；数值递减时不能假定首行是最小码。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["100", "110", "120"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [38], [34]]}}   # 从大到小
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {
        "120": {"衣长": 40}, "110": {"衣长": 38}, "100": {"衣长": 34}}


@pytest.mark.asyncio
async def test_行数与尺码数不等时拒绝对齐(monkeypatch):
    """错位填出去的是一份看着合理却每档都错的表，比留空更坏。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["100", "110", "120", "130"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[34], [36]]}}   # 2 行 vs 4 档
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {}
    assert "sizeMeasurementsSource" not in info


@pytest.mark.asyncio
async def test_有尺码键的表优先于行表推断(monkeypatch):
    """原生带尺码键的表更可靠，行表只在它没填上时兜底。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["100", "110"]}
    vision = {"sizeMeasurements": {"100": {"衣长": 34}, "110": {"衣长": 36}},
              "sizeMeasurementsRows": {"params": ["衣长"], "rows": [[99], [98]]}}
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {"100": {"衣长": 34}, "110": {"衣长": 36}}
    assert "sizeMeasurementsSource" not in info


@pytest.mark.asyncio
async def test_行表脏数据被丢弃(monkeypatch):
    """非数值单元格跳过；整行没有可用数值时不该产出空档。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["100", "110"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长", "胸围"],
        "rows": [[34, "约33"], ["-", "无"]]}}   # 第二行整行不可用
    await _merge_vision(info, vision, {})
    # 只剩 1 行可用 vs 2 档 → 数量不等，拒绝对齐
    assert info["sizeMeasurements"] == {}


# ---- 字母码等本地排不出的形态：交 LLM 做语义配对 ---------------------------
# 本地按 norm_size 排字母码会得到字典序（L 排在 S 前面），实测 S/M/L 配 59/60/61
# 会对齐成 L=59、M=60、S=61 整份颠倒。而手上已有衣长/胸围数值、尺码名里常带
# 「建议身高」，足以判断对应关系，故交模型语义配对而不是放弃。

@pytest.mark.asyncio
async def test_字母码交模型定对应(monkeypatch):
    """S/M/L 本地排不出，模型按数值走向给出对应后落库。"""
    seen = {}

    async def fake_ask(prompt, what="", retries=3, stage=None, **kw):
        seen["prompt"] = prompt
        return {"mapping": {"S": 1, "M": 2, "L": 3}, "reason": "衣长递增配SML"}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["S", "M", "L"], "title": "女装外套"}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[59], [60], [61]]}}
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {
        "S": {"衣长": 59}, "M": {"衣长": 60}, "L": {"衣长": 61}}
    assert info["sizeMeasurementsSource"] == "visionRows"
    # 提示词必须把数值和「大码可能在前」的提醒带上，否则模型无从判断
    assert "衣长=59" in seen["prompt"]
    assert "大码写在上面" in seen["prompt"]


@pytest.mark.asyncio
async def test_归一位次撞车时交模型(monkeypatch):
    """6M/6Y 归一后都含 6，本地排不出相对次序，交模型按月龄/岁语义判断。"""
    async def fake_ask(prompt, what="", retries=3, stage=None, **kw):
        return {"mapping": {"6M": 1, "6Y": 2}, "reason": "月龄小于岁"}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["6M", "6Y"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [80]]}}
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {"6M": {"衣长": 40}, "6Y": {"衣长": 80}}


@pytest.mark.asyncio
@pytest.mark.parametrize("resp,why", [
    ({"mapping": {"S": 1, "M": 1, "L": 3}}, "同一行配给多个尺码"),
    ({"mapping": {"S": 1, "M": 2}}, "缺一档没给对应"),
    ({"mapping": {"S": 1, "M": 2, "L": 9}}, "行号越界"),
    ({"mapping": {"S": 1, "M": 2, "L": "三"}}, "行号不是整数"),
    ({"mapping": {"S": 1, "M": 2, "XXL": 3}}, "尺码不在源尺码里"),
    ({"mapping": {}, "reason": "表里没有可判断的信息"}, "模型自称定不了"),
])
async def test_模型给的对应不可信时整体放弃(monkeypatch, resp, why):
    """半份对齐比没有更难排查，且会让同一张表混两种来源的数值，故不做部分采纳。"""
    async def fake_ask(prompt, what="", retries=3, stage=None, **kw):
        return resp

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["S", "M", "L"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[59], [60], [61]]}}
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {}, why
    assert "sizeMeasurementsSource" not in info


@pytest.mark.asyncio
async def test_身高码与年龄码混用交模型(monkeypatch):
    """80cm 与 2y 无换算关系（一个是长度、一个是时间），本地排不出可信序，交模型。"""
    asked = []

    async def fake_ask(prompt, what="", retries=3, stage=None, **kw):
        asked.append(what)
        return {"mapping": {"80": 1, "2y": 2}, "reason": "身高80cm小于2岁"}

    monkeypatch.setattr("app.publish.llm.ask_json", fake_ask)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["80", "2y"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [48]]}}
    await _merge_vision(info, vision, {})
    assert asked, "混用身高码与年龄码必须交模型，不能本地硬排"
    assert info["sizeMeasurements"] == {"80": {"衣长": 40}, "2y": {"衣长": 48}}


@pytest.mark.asyncio
async def test_月龄与岁不算混用_可本地排(monkeypatch):
    """月龄（m）与岁（y）都是时间单位，可换算（12m=1y），应该本地定序而非交模型。

    2026-09-02 首次实现时误把 m 和 y 当两种量纲，导致月龄岁混用也交 LLM。
    实际上它们是同量纲不同单位，岁换算成月后可比：6m < 12m < 2y=24m < 3y=36m。
    """
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["6m", "12m", "2y", "3y"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [44], [48], [52]]}}
    await _merge_vision(info, vision, {})
    # 能本地定序，不调 LLM，且结果正确（按时间轴从小到大）
    assert info["sizeMeasurements"] == {
        "6m": {"衣长": 40}, "12m": {"衣长": 44},
        "2y": {"衣长": 48}, "3y": {"衣长": 52}}



@pytest.mark.asyncio
async def test_月龄与岁不算混用_可本地排(monkeypatch):
    """月龄（m）与岁（y）都是时间单位，可换算（12m=1y），应该本地定序而非交模型。

    2026-09-02 首次实现时误把 m 和 y 当两种量纲，导致月龄岁混用也交 LLM。
    实际上它们是同量纲不同单位，岁换算成月后可比：6m < 12m < 2y=24m < 3y=36m。
    """
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["6m", "12m", "2y", "3y"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [44], [48], [52]]}}
    await _merge_vision(info, vision, {})
    # 能本地定序，不调 LLM，且结果正确（按时间轴从小到大）
    assert info["sizeMeasurements"] == {
        "6m": {"衣长": 40}, "12m": {"衣长": 44},
        "2y": {"衣长": 48}, "3y": {"衣长": 52}}



@pytest.mark.asyncio
@pytest.mark.parametrize("sizes,rows,expect", [
    (["3-6m", "6-9m", "9-12m"], [[40], [44], [48]],
     {"3-6m": 40, "6-9m": 44, "9-12m": 48}),
    (["1-2y", "2-3y", "3-4y"], [[50], [54], [58]],
     {"1-2y": 50, "2-3y": 54, "3-4y": 58}),
])
async def test_区间码按下界定序(monkeypatch, sizes, rows, expect):
    """区间不重叠时下界序与区间序一致；只抠数字会得到 23/69 这种拼接数。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": list(sizes)}
    await _merge_vision(info, {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": rows}}, {})
    assert {k: v["衣长"] for k, v in info["sizeMeasurements"].items()} == expect


@pytest.mark.asyncio
async def test_月龄码可按数值序对齐(monkeypatch):
    """月龄码归一出 3m/6m/9m 带数字，本地能排序，不该被推到模型那条路。"""
    monkeypatch.setattr("app.publish.llm.ask_json", _no_llm)
    info = {"imageUnderstanding": {}, "sizeChart": {}, "sizeMeasurements": {},
            "complianceNotes": {}, "sizes": ["9m", "3m", "6m"]}
    vision = {"sizeMeasurementsRows": {
        "params": ["衣长"], "rows": [[40], [44], [48]]}}
    await _merge_vision(info, vision, {})
    assert info["sizeMeasurements"] == {
        "3m": {"衣长": 40}, "6m": {"衣长": 44}, "9m": {"衣长": 48}}


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

    async def fake_ask(prompt, images, what="判断", system=None, stage=None, **kw):
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
    # sizeMeasurementsByPart=0：本用例的源只给了一张合表（分件表的用例在
    # test_publish_sizechart_parts.py）
    assert r["filled"] == {"imageUnderstanding": 2, "sizeChart": 1,
                           "sizeMeasurements": 1, "sizeMeasurementsByPart": 0,
                           "complianceNotes": 3, "cleanFiles": 1}
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

    async def fake_ask(prompt, images, what="判断", system=None, stage=None, **kw):
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
