"""测试阶段①b：从详情纯文字里抽尺码表（extract.desc_text_of / enrich_desc_text）。

2026-09-01 真站取证（offer 971999094281 韩系牛仔外套）：商家把整张尺码表直接打在详情
文字里而不是做成图，详情接口响应体里就是「S 衣长59 胸围118 袖长57 肩宽52」三行明文。
而两条取数路径原先都只正则抠 <img>、文字整段丢弃，于是 sizeMeasurements 落成空表、
阶段⑨ 的四个参数全靠模型凭空估算——准确值一直在手上。
"""
import json
import os

import pytest

from app.publish.extract import (_clean_meas, _decode_desc, desc_text_of,
                                 enrich_desc_text)

# 真站样本（offer 971999094281 的详情接口响应体，原样保留 \r\n 与缩进）
_REAL_HTML = (
    'var offer_details={"content":"\n'
    '\\r\\n        \\r\\n        <p><span>S 衣长59 胸围118 袖长57 肩宽52</span></p>\n'
    '<p><span>M 衣长60 胸围122 袖长58 肩宽53</span></p>\n'
    '<p><span>L 衣长61 胸围128 袖长59 肩宽54</span></p>\n'
    '\\r\\n        \\r\\n        \\r\\n    \n'
    '"};'
)

# 纯图详情的真站样本（抽样里多数商品是这种形态）：剥完只剩排版残渣
_IMG_ONLY_HTML = (
    'var offer_details={"content":"\\r\\n\n'
    '{&quot;styleType&quot;:&quot;offer-type-1&quot;,&quot;items&quot;:&quot;946015259959,'
    '944216581371&quot;,&quot;usemap&quot;:&quot;_sdmap_0&quot;}\\r\\n\n'
    '<img src="https://cbu01.alicdn.com/img/ibank/a.jpg">\n'
    '&nbsp;&nbsp;\nnull\nnull"};'
)


def test_desc_text_of_real_sample():
    """真站文字样本：三行尺码原样留下，排版残渣与标签全清掉。"""
    txt = desc_text_of(_REAL_HTML)
    assert txt.split("\n") == [
        "S 衣长59 胸围118 袖长57 肩宽52",
        "M 衣长60 胸围122 袖长58 肩宽53",
        "L 衣长61 胸围128 袖长59 肩宽54",
    ]
    # 赋值语句头尾与 \r\n 字面量都不该残留
    assert "offer_details" not in txt and "\\r\\n" not in txt


def test_desc_text_of_image_only():
    """纯图详情：剥完是空串（图 URL 由 _RE_DESC_IMG 那条路径管，不进文字）。"""
    assert desc_text_of(_IMG_ONLY_HTML) == ""


def test_desc_text_of_empty():
    """空输入不抛：拿不到响应体与「没有文字详情」同一处置。"""
    assert desc_text_of("") == ""
    assert desc_text_of(None) == ""
    assert desc_text_of("   \n  \r\n ") == ""


def test_decode_desc_both_encodings():
    """老端点 GB18030 / 新端点 UTF-8 都要解出可读中文，声明编码错了也要纠回来。

    原先写死 gb18030 是成立的（那时只抠 ASCII 的图片 URL），现在要带中文文字回来，
    解错会让「衣长」变成「琛ｉ暱」这类乱码汉字——它既不抛异常也不产生 U+FFFD。
    """
    s = "S 衣长59 胸围118 袖长57 肩宽52"
    assert _decode_desc(s.encode("utf-8"), "utf-8") == s
    assert _decode_desc(s.encode("gb18030"), "gb18030") == s
    # 声明与实际不符的两个方向都要纠回来
    assert _decode_desc(s.encode("utf-8"), "gb18030") == s
    assert _decode_desc(s.encode("gb18030"), "utf-8") == s
    # 无声明
    assert _decode_desc(s.encode("utf-8"), None) == s
    assert _decode_desc(s.encode("gb18030"), None) == s


def test_clean_meas_filters_dirty():
    """脏条目丢掉而不是修补：这份数据会被当成商家实测值直接填进平台。"""
    got = _clean_meas({
        "S": {"衣长": "59", "胸围": 118.0, "袖长": None, "": 5},
        "M": {"衣长": 60},
        "": {"衣长": 61},          # 尺码键空
        "L": {},                   # 一个有效参数都没有
        "XL": "不是字典",
    })
    # 整数写整数（与视觉回填的形态一致），不可转数的值与空键都丢
    assert got == {"S": {"衣长": 59, "胸围": 118}, "M": {"衣长": 60}}


def _write_info(tmp_path, **kw) -> str:
    info = {"title": "测试商品", "sizes": ["S", "M", "L"],
            "sizeMeasurements": {}, "sizeChart": {}}
    info.update(kw)
    p = os.path.join(tmp_path, "product-info.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    return p


@pytest.mark.asyncio
async def test_skip_when_no_text(tmp_path, monkeypatch):
    """纯图详情（descText 空）直接跳过，一次 LLM 调用都不发。"""
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr("app.publish.llm.ask_json", spy)
    r = await enrich_desc_text(_write_info(str(tmp_path), descText=""))
    assert r["status"] == "skipped" and not calls


@pytest.mark.asyncio
async def test_skip_when_no_size_hint(tmp_path, monkeypatch):
    """有文字但一个尺码相关词都没有：也不必问模型（省钱闸）。

    多数纯文字详情写的是发货/洗涤/售后说明，发一次调用只为得到两个空表。
    """
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr("app.publish.llm.ask_json", spy)
    r = await enrich_desc_text(_write_info(
        str(tmp_path), descText="本店48小时内发货，支持七天无理由退换，请勿漂白。"))
    assert r["status"] == "skipped" and not calls
    assert "没有尺码相关词" in r["reason"]


@pytest.mark.asyncio
async def test_extract_real_case(tmp_path, monkeypatch):
    """真站那三行文字：抽出的实测表落进 info。"""
    async def fake(*a, **k):
        return {"sizeMeasurements": {
            "S": {"衣长": 59, "胸围": 118, "袖长": 57, "肩宽": 52},
            "M": {"衣长": 60, "胸围": 122, "袖长": 58, "肩宽": 53},
            "L": {"衣长": 61, "胸围": 128, "袖长": 59, "肩宽": 54}},
            "sizeChart": {}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake)
    p = _write_info(str(tmp_path), descText=desc_text_of(_REAL_HTML))
    r = await enrich_desc_text(p)
    assert r["status"] == "ok" and r["filled"]["sizeMeasurements"] == 3
    with open(p, encoding="utf-8") as f:
        info = json.load(f)
    assert info["sizeMeasurements"]["S"]["衣长"] == 59
    # 来源要留痕：人工复核时要能分辨这份数据是文字抽的还是识图来的
    assert info["sizeMeasurementsSource"] == "descText"


@pytest.mark.asyncio
async def test_text_overrides_vision(tmp_path, monkeypatch):
    """文字优先：商家明文比识图可信（没有 OCR 误差），故覆盖视觉已填的值。"""
    async def fake(*a, **k):
        return {"sizeMeasurements": {"S": {"衣长": 59}}, "sizeChart": {}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake)
    p = _write_info(str(tmp_path), descText="S 衣长59",
                    sizeMeasurements={"S": {"衣长": 44}})   # 识图读错的值
    await enrich_desc_text(p)
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["sizeMeasurements"] == {"S": {"衣长": 59}}


@pytest.mark.asyncio
async def test_keep_vision_when_not_overwrite(tmp_path, monkeypatch):
    """overwrite=False 时保住已有值：人工标注比任何模型都可靠。"""
    async def fake(*a, **k):
        return {"sizeMeasurements": {"S": {"衣长": 59}}, "sizeChart": {}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake)
    p = _write_info(str(tmp_path), descText="S 衣长59",
                    sizeMeasurements={"S": {"衣长": 44}})
    await enrich_desc_text(p, overwrite=False)
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["sizeMeasurements"] == {"S": {"衣长": 44}}


@pytest.mark.asyncio
async def test_empty_result_never_clears(tmp_path, monkeypatch):
    """抽到空表时绝不拿空去清掉已有值（文字里没尺码表是常态）。"""
    async def fake(*a, **k):
        return {"sizeMeasurements": {}, "sizeChart": {}}

    monkeypatch.setattr("app.publish.llm.ask_json", fake)
    p = _write_info(str(tmp_path), descText="尺码说明详见图片",
                    sizeMeasurements={"S": {"衣长": 44}})
    r = await enrich_desc_text(p)
    assert r["status"] == "ok" and r["filled"]["sizeMeasurements"] == 0
    with open(p, encoding="utf-8") as f:
        assert json.load(f)["sizeMeasurements"] == {"S": {"衣长": 44}}
