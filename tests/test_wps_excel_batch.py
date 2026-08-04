# -*- coding: utf-8 -*-
"""WpsExcelTool 批量追加与行解析单测（离线，合成最小 WPS 工作簿，不碰真实表）。

订单登记管线要往 102MB、单批 200+ 行的表里写，两件事必须先在这里钉死：
1. 自闭合空行 `<row r="4" ht="41" customHeight="1"/>` 的解析与替换——真实表末尾全是这种
   预留空行，旧正则会把它的内容一路吞到下一个带内容行，末行定位和写入位置都会错。
2. 批量路径的一致性——一次备份、一次读写 zip、N 行 N 图，且不能撞掉已有的 media 图片。
"""
import re
import zipfile
from pathlib import Path

import pytest

from app.tool import wps_excel_tool as wet
from app.tool.wps_excel_tool import WpsExcelTool


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="jpeg" ContentType="image/jpeg"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    "</Types>"
)

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)

WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="订单表" sheetId="1" r:id="rId1"/>'
    '<sheet name="牛仔裤" sheetId="2" r:id="rId2"/>'
    '<sheet name="插入表" sheetId="3" r:id="rId3"/></sheets>'
    # 两条 definedName：插入表那条要随插行平移，牛仔裤那条必须原样不动
    "<definedNames>"
    '<definedName name="_xlnm._FilterDatabase" localSheetId="2" hidden="1">'
    "插入表!$B$1:$H$3</definedName>"
    '<definedName name="别处区域" localSheetId="1">牛仔裤!$A$1:$D$3</definedName>'
    "</definedNames></workbook>"
)

WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet2.xml"/>'
    '<Relationship Id="rId3" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet3.xml"/></Relationships>'
)

# 表头在第 1 行；row 2/3 是数据；row 4-6 是【自闭合预留空行】；row 7 有单元格但无值；row 8+ 不存在。
# 这样一次批量写 5 行正好覆盖「替换自闭合行 / 替换带内容行 / 尾部插入新行」三条路径。
SHEET1 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<dimension ref="A1:G1048576"/>'
    "<sheetData>"
    '<row r="1" ht="20">'
    '<c r="A1" s="1" t="str"><v>订单店铺</v></c>'
    '<c r="B1" s="1" t="str"><v>站点区分</v></c>'
    '<c r="C1" s="1" t="str"><v>订单号</v></c>'
    '<c r="D1" s="1" t="str"><v>尺码</v></c>'
    '<c r="G1" s="1" t="str"><v>产品图片</v></c></row>'
    '<row r="2" ht="41" customHeight="1">'
    '<c r="A2" s="2" t="str"><v>StoreA全球</v></c>'
    '<c r="B2" s="3" t="str"><v>哥伦比亚</v></c>'
    '<c r="C2" s="4" t="str"><v>PO-045-1</v></c>'
    '<c r="D2" s="5" t="str"><v>杏色 / 3-4Y</v></c>'
    '<c r="G2" s="7" t="str">'
    '<f>_xlfn.DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</f>'
    '<v>=DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</v></c></row>'
    # D3 尾部带软换行实体 &#10;——真实登记表里手工录入的尺码大量如此，判重必须先还原再 strip
    '<row r="3" ht="41" customHeight="1">'
    '<c r="A3" s="2" t="str"><v>StoreA全球</v></c>'
    '<c r="C3" s="4" t="str"><v>PO-045-2</v></c>'
    '<c r="D3" s="5" t="str"><v>粉色 &amp; 白色 / 5-6Y&#10;</v></c></row>'
    '<row r="4" ht="41" customHeight="1"/>'
    '<row r="5" ht="41" customHeight="1"/>'
    '<row r="6" ht="41" customHeight="1"/>'
    '<row r="7" ht="41" customHeight="1"><c r="A7" s="2"/></row>'
    "</sheetData></worksheet>"
)

# 第 1 行是跨列大标题、第 2 行才是真表头——对应真实登记表里的「牛仔裤」「童装」两个 Sheet。
SHEET2 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<dimension ref="A1:D100"/>'
    "<sheetData>"
    '<row r="1"><c r="A1" s="1" t="str"><v>2026年牛仔裤订单登记</v></c></row>'
    '<row r="2">'
    '<c r="A2" s="1" t="str"><v>订单店铺</v></c>'
    '<c r="B2" s="1" t="str"><v>订单号</v></c>'
    '<c r="C2" s="1" t="str"><v>尺码</v></c>'
    '<c r="D2" s="1" t="str"><v>备注</v></c></row>'
    '<row r="3">'
    '<c r="A3" s="2" t="str"><v>StoreA</v></c>'
    '<c r="B3" s="2" t="str"><v>PO-211-9</v></c>'
    '<c r="C3" s="2" t="str"><v>32</v></c></row>'
    "</sheetData></worksheet>"
)

# 专供「插到表头下方」用例：照抄真实 StoreA全球1 的结构特征——合并区、autoFilter、
# 到底的条件格式区、跨行共享公式，外加一个远在 900 行的纯格式空行（真表在 1045692 起，
# 隔得很远才不会跟下移后的行号撞上）。
SHEET3 = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<dimension ref="A1:I1048576"/>'
    "<sheetData>"
    '<row r="1" ht="20">'
    '<c r="B1" s="1" t="str"><v>订单店铺</v></c>'
    '<c r="C1" s="1" t="str"><v>订单号</v></c>'
    '<c r="D1" s="1" t="str"><v>尺码</v></c>'
    '<c r="G1" s="1" t="str"><v>产品图片</v></c>'
    '<c r="I1" s="1" t="str"><v>平台创建时间</v></c></row>'
    '<row r="2" ht="41" customHeight="1">'
    '<c r="C2" s="4" t="str"><v>PO-OLD-1</v></c>'
    '<c r="D2" s="5" t="str"><v>杏色 / 3-4Y</v></c>'
    '<c r="G2" s="7" t="str">'
    '<f t="shared" ref="G2:G3" si="0">'
    "_xlfn.DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</f>"
    '<v>=DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</v></c></row>'
    '<row r="3" ht="41" customHeight="1">'
    '<c r="C3" s="4" t="str"><v>PO-OLD-2</v></c>'
    '<c r="D3" s="5" t="str"><v>蓝色 / 5-6Y</v></c>'
    '<c r="G3" s="7" t="str"><f t="shared" si="0"/>'
    '<v>=DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</v></c></row>'
    '<row r="900" customFormat="1" spans="2:9"/>'
    "</sheetData>"
    '<autoFilter ref="B1:H3"/>'
    '<mergeCells count="2">'
    '<mergeCell ref="C2:C3"/><mergeCell ref="F2:F3"/></mergeCells>'
    '<conditionalFormatting sqref="C1:C2 C4:C1048576">'
    '<cfRule type="duplicateValues" dxfId="0" priority="1"/></conditionalFormatting>'
    "</worksheet>"
)

STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<cellXfs count="8">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyAlignment="1">'
    '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="2" xfId="0"/>'
    '<xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="3" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="4" xfId="0"/>'
    "</cellXfs></styleSheet>"
)

CELLIMAGES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" '
    'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<etc:cellImage><xdr:pic><xdr:nvPicPr>'
    '<xdr:cNvPr id="2" name="ID_OLD0000000000000000000000000001" descr="old"/>'
    '<xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed="rId1"/>'
    '<a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr/>'
    "</xdr:pic></etc:cellImage></etc:cellImages>"
)

CI_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
    'Target="media/image1.jpeg"/></Relationships>'
)


@pytest.fixture
def book(tmp_path, monkeypatch):
    """合成一份含 DISPIMG 的最小 WPS 工作簿；备份目录改到 tmp，不污染桌面输出目录。"""
    path = tmp_path / "登记表.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", WORKBOOK)
        zf.writestr("xl/_rels/workbook.xml.rels", WB_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", SHEET1)
        zf.writestr("xl/worksheets/sheet2.xml", SHEET2)
        zf.writestr("xl/worksheets/sheet3.xml", SHEET3)
        zf.writestr("xl/styles.xml", STYLES)
        zf.writestr("xl/cellimages.xml", CELLIMAGES)
        zf.writestr("xl/_rels/cellimages.xml.rels", CI_RELS)
        zf.writestr("xl/media/image1.jpeg", b"\xff\xd8old-jpeg")
        # image5 只存在于包里、不在 cellimages.rels 里（模拟浮动图占号），新图不得撞上它
        zf.writestr("xl/media/image5.png", b"\x89PNGfloating")

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    monkeypatch.setattr(wet, "get_output_dir", lambda *_a, **_k: backup_dir)
    return path


def _sheet(path: Path, part: str = "xl/worksheets/sheet1.xml") -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(part).decode("utf-8")


def _make_img(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8" + name.encode())
    return str(p)


# ---- 行解析 ---------------------------------------------------------------


def test_parse_rows_keeps_self_closing_rows_separate():
    """自闭合空行必须各自成行、内容为空，不能吞掉后面的行。"""
    rows = WpsExcelTool._parse_rows(SHEET1)

    assert [r for r, _ in rows] == ["1", "2", "3", "4", "5", "6", "7"]
    assert dict(rows)["4"] == ""
    assert dict(rows)["5"] == ""
    # 曾经的坑：row 4 的 body 一路吞到 row 7 的 </row>，把别人的单元格算进自己头上
    assert "<row" not in dict(rows)["4"]
    assert "A7" not in dict(rows)["4"]


def test_find_last_data_row_ignores_valueless_rows():
    """只有样式没有值的 row 7 不算数据行；末行是 3。"""
    rows = WpsExcelTool._parse_rows(SHEET1)
    assert WpsExcelTool._find_last_data_row(rows, ["A", "C", "D"]) == 3


def test_find_last_data_row_falls_back_to_header_row():
    """一条数据都没有时返回 header_row，调用方 +1 才不会覆盖表头。"""
    rows = WpsExcelTool._parse_rows(SHEET2)
    assert WpsExcelTool._find_last_data_row(rows, ["Z"], header_row=2) == 2


def test_detect_header_row(book):
    """表头行按「填得最满的那行」判定：sheet1 在第 1 行，牛仔裤在第 2 行。"""
    assert WpsExcelTool.detect_header_row(str(book), "订单表") == 1
    assert WpsExcelTool.detect_header_row(str(book), "牛仔裤") == 2


def test_read_header_respects_header_row(book):
    """表头在第 2 行的 Sheet 要能读出真表头，而不是大标题。"""
    assert WpsExcelTool.read_header(str(book), "牛仔裤") == {"A": "2026年牛仔裤订单登记"}
    assert WpsExcelTool.read_header(str(book), "牛仔裤", header_row=2) == {
        "A": "订单店铺", "B": "订单号", "C": "尺码", "D": "备注",
    }


def test_xml_unescape_restores_entities():
    """实体还原：&amp; 最后处理，否则 &amp;lt; 会被二次解成 <。"""
    assert wet._xml_unescape("蓝色 / 120&#10;") == "蓝色 / 120\n"
    assert wet._xml_unescape("A&amp;B") == "A&B"
    assert wet._xml_unescape("&amp;lt;tag&amp;gt;") == "&lt;tag&gt;"
    assert wet._xml_unescape("&lt;b&gt;&quot;x&quot;&apos;") == '<b>"x"\''
    assert wet._xml_unescape("&#x4E2D;文") == "中文"
    assert wet._xml_unescape("无实体") == "无实体"


def test_existing_key_tuples_combo_key(book):
    """(订单号, 尺码) 组合键判重：同订单号不同尺码是两条，不能被并成一条。

    D3 在 XML 里是 `粉色 &amp; 白色 / 5-6Y&#10;`，判重键必须是还原并 strip 后的
    `粉色 & 白色 / 5-6Y`，才能跟导出文件里的同一尺码对上、不重复写行。
    """
    got = WpsExcelTool.existing_key_tuples(str(book), "订单表", ["C", "D"])
    assert got == {
        ("PO-045-1", "杏色 / 3-4Y"),
        ("PO-045-2", "粉色 & 白色 / 5-6Y"),
    }

    # 单列入口行为不变（内部已改走组合键实现）
    assert WpsExcelTool.existing_key_values(str(book), "订单表", col="C") == {
        "PO-045-1", "PO-045-2",
    }
    # 表头在第 2 行时，表头本身不得混进候选集
    assert WpsExcelTool.existing_key_values(str(book), "牛仔裤", col="B", header_row=2) == {
        "PO-211-9"
    }


# ---- 批量追加 -------------------------------------------------------------


def test_append_rows_covers_reserved_insert_and_images(book, tmp_path):
    """一次写 5 行：替换 3 个自闭合空行 + 替换 1 个无值行 + 尾部插入 1 行，带 3 张图。"""
    rows_data = []
    for i in range(5):
        item = {
            "values": {
                "A": "StoreA全球",
                "B": "哥伦比亚",
                "C": f"PO-045-新{i}",
                "D": f"尺码{i}",
            }
        }
        if i < 3:
            item["image_column"] = "G"
            item["image_path"] = _make_img(tmp_path, f"p{i}.jpg")
        rows_data.append(item)

    res = WpsExcelTool().append_rows(str(book), "订单表", rows_data)

    assert res["written"] == 5
    assert (res["first_row"], res["last_row"]) == (4, 8)
    assert res["images"] == 3
    assert Path(res["backup"]).exists()

    xml = _sheet(book)
    rows = WpsExcelTool._parse_rows(xml)
    rids = [r for r, _ in rows]
    assert rids == ["1", "2", "3", "4", "5", "6", "7", "8"], "行号不得重复或错序"

    body = dict(rows)
    # 原有数据行原样不动
    assert "PO-045-1" in body["2"] and "ID_OLD0000000000000000000000000001" in body["2"]
    assert "PO-045-2" in body["3"]
    # 新行落在 4..8，内容与引用正确
    for i, rid in enumerate(["4", "5", "6", "7", "8"]):
        assert f"PO-045-新{i}" in body[rid]
        assert f'<c r="C{rid}"' in body[rid]
        assert f"尺码{i}" in body[rid]
    # 预留空行的行高（给嵌入图留的 41）必须保住
    assert '<row r="4" ht="41" customHeight="1">' in xml
    # 尾部插入行沿用模板行开标签，同样带行高
    assert '<row r="8" ht="41" customHeight="1">' in xml
    # 图片列样式取自历史 DISPIMG 单元格（s=7），不是模板行的空样式
    assert '<c r="G4" s="7" t="str"><f>_xlfn.DISPIMG(' in xml
    # 无图的第 4/5 行不应出现图片列
    assert '<c r="G7"' not in xml and '<c r="G8"' not in xml


def test_append_rows_media_numbering_avoids_existing_files(book, tmp_path):
    """新图编号要避开包里【实际存在】的 image5.png，不能只看 cellimages.rels。"""
    rows_data = [
        {"values": {"C": "PO-1"}, "image_column": "G", "image_path": _make_img(tmp_path, "a.jpg")},
        {"values": {"C": "PO-2"}, "image_column": "G", "image_path": _make_img(tmp_path, "b.jpg")},
    ]
    WpsExcelTool().append_rows(str(book), "订单表", rows_data)

    with zipfile.ZipFile(book) as zf:
        names = zf.namelist()
        assert zf.read("xl/media/image5.png") == b"\x89PNGfloating", "已有浮动图被覆盖"
        assert zf.read("xl/media/image1.jpeg") == b"\xff\xd8old-jpeg"
        assert "xl/media/image6.jpeg" in names and "xl/media/image7.jpeg" in names
        assert len(names) == len(set(names)), "zip 里出现重名部件"

        ci = zf.read("xl/cellimages.xml").decode("utf-8")
        rels = zf.read("xl/_rels/cellimages.xml.rels").decode("utf-8")

    # 新增 2 条 cellImage，DISPIMG ID 各不相同且与单元格公式一一对应
    assert ci.count("<etc:cellImage>") == 3
    # ID 形如 ID_20260726_143000000000006 补零到 36 位（含时间戳的下划线），沿用单行版格式
    ids = re.findall(r'<xdr:cNvPr id="(\d+)" name="(ID_[0-9A-Z_]+)"', ci)
    assert len(ids) == 3 and len({i for i, _ in ids}) == 3, "cNvPr id 不得重复"
    assert len({n for _, n in ids}) == 3, "DISPIMG ID 不得重复"
    assert all(len(n) == 36 for _, n in ids[1:]), "新 ID 须补零到 36 位（旧样本 ID 是测试常量）"

    rel_ids = re.findall(r'Id="(rId\d+)"', rels)
    assert len(rel_ids) == len(set(rel_ids)) == 3, "关系 Id 不得重复"

    xml = _sheet(book)
    for _, name in ids[1:]:
        assert f'_xlfn.DISPIMG(&quot;{name}&quot;,1)' in xml


def test_append_rows_skips_empty_values_and_keeps_dimension(book, tmp_path):
    """空值不落单元格；dimension 只扩不缩。"""
    WpsExcelTool().append_rows(
        str(book),
        "订单表",
        [{"values": {"A": "StoreA全球", "B": "", "C": "PO-9", "D": None}}],
    )

    xml = _sheet(book)
    assert '<c r="B4"' not in xml and '<c r="D4"' not in xml
    assert '<c r="A4"' in xml and '<c r="C4"' in xml
    assert '<dimension ref="A1:G1048576"/>' in xml


def test_append_rows_writes_header_row_aware(book, tmp_path):
    """表头在第 2 行的 Sheet：首条数据落在第 4 行（末行 3 之后），不覆盖表头。"""
    res = WpsExcelTool().append_rows(
        str(book),
        "牛仔裤",
        [{"values": {"A": "StoreA", "B": "PO-211-10", "C": "34"}}],
        header_row=2,
    )

    assert (res["first_row"], res["last_row"]) == (4, 4)
    xml = _sheet(book, "xl/worksheets/sheet2.xml")
    assert "2026年牛仔裤订单登记" in xml and "PO-211-9" in xml
    assert '<c r="B4" s="2" t="str"><v>PO-211-10</v></c>' in xml or "PO-211-10" in xml


def test_append_rows_text_cells_get_wrap_style(book, tmp_path):
    """文本列基于该列既有样式派生 wrapText 变体，新 xf 追加到 cellXfs 末尾且计数同步。"""
    WpsExcelTool().append_rows(
        str(book), "订单表", [{"values": {"D": "很长很长的尺码文本 / 需要换行"}}]
    )

    with zipfile.ZipFile(book) as zf:
        styles = zf.read("xl/styles.xml").decode("utf-8")
    xfs = re.findall(r"<xf\b[^>]*?/>|<xf\b[^>]*?>.*?</xf>", styles, re.S)
    count = int(re.search(r'<cellXfs count="(\d+)"', styles).group(1))

    assert count == len(xfs) == 9, "应新增 1 个 wrap 变体且 count 同步"
    assert 'wrapText="1"' in xfs[8]
    # 派生自 D 列历史样式 s=5（borderId=3），原样式不得被改写
    assert 'borderId="3"' in xfs[8]
    assert xfs[5] == '<xf numFmtId="0" fontId="0" fillId="0" borderId="3" xfId="0"/>'
    assert f'<c r="D4" s="8" t="str">' in _sheet(book)


def test_append_rows_empty_input_is_noop(book):
    """空批次直接返回，不备份、不动文件。"""
    before = book.read_bytes()
    res = WpsExcelTool().append_rows(str(book), "订单表", [])

    assert res == {"written": 0, "first_row": None, "last_row": None,
                   "images": 0, "backup": None}
    assert book.read_bytes() == before


# ---- 插到表头下方（订单登记表要求「新的在上面」）---------------------------


def _s3(path: Path) -> str:
    return _sheet(path, "xl/worksheets/sheet3.xml")


def _new_row(order_no: str, size: str, created: str) -> dict:
    return {"values": {"C": order_no, "D": size, "I": created}}


def test_insert_at_top_puts_new_rows_under_header(book):
    """新行落在表头正下方，原有数据整体下移，行内单元格引用同步改号。"""
    res = WpsExcelTool().append_rows(
        str(book), "插入表",
        [_new_row("PO-NEW-1", "红色 / 7-8Y", "2026-07-28 10:00:00"),
         _new_row("PO-NEW-2", "黑色 / 9-10Y", "2026-07-28 09:00:00")],
        insert_at_top=True,
    )
    xml = _s3(book)

    assert (res["first_row"], res["last_row"], res["written"]) == (2, 3, 2)
    # 新行按入参顺序占 2、3 行
    assert re.search(r'<c r="C2"[^>]*><v>PO-NEW-1</v></c>', xml)
    assert re.search(r'<c r="C3"[^>]*><v>PO-NEW-2</v></c>', xml)
    # 原 2、3 行下移到 4、5 行，单元格 r 也跟着改
    assert '<c r="C4" s="4" t="str"><v>PO-OLD-1</v></c>' in xml
    assert '<c r="C5" s="4" t="str"><v>PO-OLD-2</v></c>' in xml
    assert "PO-OLD-1" not in xml.split('<row r="4"')[0], "原数据不能还留在上面"
    # 文档顺序必须仍是行号升序，否则 WPS 判损坏；末尾纯格式空行也跟着下移
    assert [int(n) for n in re.findall(r'<row r="(\d+)"', xml)] == [1, 2, 3, 4, 5, 902]


def test_insert_at_top_shifts_ranges_and_shared_formula(book):
    """合并区/筛选区/条件格式/共享公式 ref 全部随插行平移，到底的区域不越界。"""
    WpsExcelTool().append_rows(
        str(book), "插入表",
        [_new_row("PO-NEW-1", "红色", "2026-07-28 10:00:00"),
         _new_row("PO-NEW-2", "黑色", "2026-07-28 09:00:00")],
        insert_at_top=True,
    )
    xml = _s3(book)

    assert '<mergeCell ref="C4:C5"/><mergeCell ref="F4:F5"/>' in xml
    assert '<autoFilter ref="B1:H5"/>' in xml, "表头端留在第 1 行，尾端跟着数据走"
    # C1 的 1 小于插入位不动；C2→C4、C4→C6；到底的 1048576 卡住不越界
    assert 'sqref="C1:C4 C6:C1048576"' in xml
    assert '<f t="shared" ref="G4:G5" si="0">' in xml
    assert '<row r="902" customFormat="1" spans="2:9"/>' in xml, "纯格式空行也整体下移"


def test_insert_at_top_keeps_cfrule_formula_in_step_with_sqref():
    """条件格式公式里的绝对引用必须跟 sqref 一起平移，相对引用（求值锚点）不能动。

    真表那条「订单号重复标红」规则就是 sqref + SUMPRODUCT($C$1:$C$10…) 配套的，
    只改 sqref 会让整段高亮错位。
    """
    tail = (
        '<autoFilter ref="C1:P281"/>'
        '<conditionalFormatting sqref="C1:C10 C200:C1048576"><cfRule type="expression">'
        "<formula>AND(SUMPRODUCT(1*(($C$1:$C$10)=(C1)))"
        "+SUMPRODUCT(1*(($C$200:$C$1048576)=(C1)))&gt;1,NOT(ISBLANK(C1)))</formula>"
        "</cfRule></conditionalFormatting>"
    )
    out = WpsExcelTool._shift_tail_refs(tail, 2, 3)

    assert 'sqref="C1:C13 C203:C1048576"' in out
    assert "$C$1:$C$13" in out and "$C$203:$C$1048576" in out
    assert "(C1)" in out and "ISBLANK(C1)" in out, "相对引用是求值锚点，不许动"
    assert '<autoFilter ref="C1:P284"/>' in out


def test_insert_at_top_shifts_only_this_sheets_defined_name(book):
    """workbook.xml 里指向本表的 definedName 平移，别的表那条纹丝不动。"""
    WpsExcelTool().append_rows(
        str(book), "插入表", [_new_row("PO-NEW-1", "红色", "2026-07-28 10:00:00")],
        insert_at_top=True,
    )
    with zipfile.ZipFile(book) as zf:
        wbx = zf.read("xl/workbook.xml").decode("utf-8")

    assert "插入表!$B$1:$H$4</definedName>" in wbx
    assert "牛仔裤!$A$1:$D$3</definedName>" in wbx


def test_append_mode_leaves_ranges_untouched(book):
    """默认追加模式一行都不许平移——老管线（比价采集）还在用它。"""
    WpsExcelTool().append_rows(
        str(book), "插入表", [_new_row("PO-NEW-1", "红色", "2026-07-28 10:00:00")]
    )
    xml = _s3(book)

    assert re.search(r'<c r="C4"[^>]*><v>PO-NEW-1</v></c>', xml)
    assert '<mergeCell ref="C2:C3"/>' in xml
    assert '<autoFilter ref="B1:H3"/>' in xml
    assert '<c r="C2" s="4" t="str"><v>PO-OLD-1</v></c>' in xml


def test_insert_at_top_shifts_reserved_rows_too(book):
    """紧贴数据下方的预留空行也一起下移（对齐 Excel 插入行语义），不许撞号。"""
    WpsExcelTool().append_rows(
        str(book), "订单表", [_new_row("PO-X", "红色", "2026-07-28")],
        insert_at_top=True,
    )
    xml = _sheet(book)

    assert [int(n) for n in re.findall(r'<row r="(\d+)"', xml)] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert '<c r="C3" s="4" t="str"><v>PO-045-1</v></c>' in xml, "原第 2 行数据下移到第 3 行"
    assert '<row r="5" ht="41" customHeight="1"/>' in xml, "预留空行保住自闭合与行高"


def test_insert_at_top_drops_only_blank_rows_pushed_past_limit(book):
    """被挤出 1048576 的纯格式空行可以丢；换成带值的行就必须抛错中止。"""
    tool = WpsExcelTool()
    xml = 'x<row r="2"><c r="A2"><v>1</v></c></row><row r="1048576"/></sheetData>'

    out = tool._shift_for_insert(xml, tool._row_positions(xml), 1, 1)
    assert '<row r="1048576"/>' not in out and '<row r="3">' in out

    dirty = 'x<row r="2"><c r="A2"><v>1</v></c></row><row r="1048576"><c r="A1048576"><v>9</v></c></row></sheetData>'
    with pytest.raises(ValueError, match="挤出 1048576"):
        tool._shift_for_insert(dirty, tool._row_positions(dirty), 1, 1)


def test_shift_ref_rows_rules():
    """区域平移：小于插入位的端点不动，到底的端点卡在 1048576，多段 sqref 逐段处理。"""
    f = WpsExcelTool._shift_ref_rows
    assert f("C1:C10", 2, 3) == "C1:C13"
    assert f("C200:C1048576", 2, 3) == "C203:C1048576"
    assert f("C1:C2 C4:C1048576", 2, 2) == "C1:C4 C6:C1048576"
    assert f("$B$1:$H$3", 2, 1) == "$B$1:$H$4"


def test_shift_data_region_tolerates_dispimg_but_rejects_cell_refs():
    """DISPIMG 的图片 ID 含「字母+数字」，不能被误判成单元格引用；真引用则必须抛错。"""
    ok = (
        '<row r="2"><c r="G2" t="str">'
        "<f>_xlfn.DISPIMG(&quot;ID_OLD0000000000000000000000000001&quot;,1)</f>"
        '<v>x</v></c></row>'
    )
    moved = WpsExcelTool._shift_data_region(ok, 2, 2)
    assert '<row r="4">' in moved and '<c r="G4"' in moved

    with pytest.raises(ValueError, match="公式引用错位"):
        WpsExcelTool._shift_data_region(
            '<row r="2"><c r="K2"><f>SUM(K3:K9)</f><v>1</v></c></row>', 2, 2
        )


def test_shift_data_region_ignores_self_closing_shared_formula():
    """共享公式从属格 `<f t="shared" si="0"/>` 是自闭合的，不能被当成开标签。

    实机踩过：把它当开标签会一路吃到下一个 </f>，中间的 <v>=DISPIMG(...)</v> 缓存值
    被误判成「带单元格引用的公式」，整批写入被守卫拦死。
    """
    region = (
        '<row r="2"><c r="G2" t="str"><f t="shared" si="0"/>'
        "<v>=DISPIMG(&quot;ID_63AB511EEBE14F33AB0A33ABE9730BF9&quot;,1)</v></c></row>"
        '<row r="3"><c r="G3" t="str">'
        '<f t="shared" ref="G3:G4" si="1">'
        "_xlfn.DISPIMG(&quot;ID_10A6E28B6494458D8104CFC80D88F5C5&quot;,1)</f>"
        "<v>x</v></c></row>"
    )
    moved = WpsExcelTool._shift_data_region(region, 2, 3)

    assert '<row r="5">' in moved and '<row r="6">' in moved
    assert '<c r="G5"' in moved and '<c r="G6"' in moved
    assert 'ref="G6:G7"' in moved


# ---- 插列（订单登记表要在「尺码」右侧加「数量」）---------------------------


def test_insert_column_shifts_cells_ranges_and_writes_header(book):
    """在「尺码」(D) 右侧插列：D 及左侧不动，E 起全部右移，各类区域引用同步平移。"""
    res = WpsExcelTool.insert_column_after(str(book), "插入表", "尺码", "数量")
    xml = _s3(book)

    assert res["inserted"] is True and res["column"] == "E"
    assert Path(res["backup"]).exists(), "改结构前必须留备份"
    # 表头：D 原样，E 是新列（样式抄左邻表头格 s="1"），原 G/I 右移成 H/J
    assert '<c r="D1" s="1" t="str"><v>尺码</v></c>' in xml
    assert '<c r="E1" s="1" t="str"><v>数量</v></c>' in xml
    assert '<c r="H1" s="1" t="str"><v>产品图片</v></c>' in xml
    assert '<c r="J1" s="1" t="str"><v>平台创建时间</v></c>' in xml
    # 数据格随之右移，样式跟着走
    assert '<c r="H2" s="7" t="str">' in xml and '<c r="G2"' not in xml
    assert '<c r="D2" s="5" t="str"><v>杏色 / 3-4Y</v></c>' in xml, "尺码列本身不能动"
    # 共享公式 ref、合并区、筛选区、dimension、spans 全部平移；C 列条件格式不动
    assert '<f t="shared" ref="H2:H3" si="0">' in xml
    assert '<mergeCell ref="C2:C3"/><mergeCell ref="G2:G3"/>' in xml
    assert '<autoFilter ref="B1:I3"/>' in xml
    assert '<dimension ref="A1:J1048576"/>' in xml
    assert 'sqref="C1:C2 C4:C1048576"' in xml, "C 在插入位左侧，不该动"
    assert '<row r="900" customFormat="1" spans="2:10"/>' in xml


def test_new_column_cells_inherit_left_neighbor_style(book):
    """刚插出来的列整表一个单元格都没有，学不到样式 → 取模板行左邻列的（同 Excel 语义）。

    不做的话数量格没有 s=、跟左右邻差一圈边框，肉眼一看就是「补上去的」。
    """
    WpsExcelTool.insert_column_after(str(book), "插入表", "尺码", "数量")
    WpsExcelTool().append_rows(
        str(book), "插入表",
        [{"values": {"C": "PO-NEW-1", "D": "红色", "E": 2}}],
        insert_at_top=True,
    )
    xml = _s3(book)

    # 模板行 D 是 s="5"，新列 E 跟着它
    assert '<c r="E2" s="5"><v>2</v></c>' in xml


def test_insert_column_is_idempotent(book):
    """已经有「数量」列就直接返回，不动文件、不做备份——重跑不能插出第二列。"""
    WpsExcelTool.insert_column_after(str(book), "插入表", "尺码", "数量")
    before = _s3(book)

    res = WpsExcelTool.insert_column_after(str(book), "插入表", "尺码", "数量")

    assert res["inserted"] is False and res["column"] == "E"
    assert res["backup"] is None
    assert _s3(book) == before


def test_insert_column_reads_header_row_two(book):
    """表头在第 2 行的 Sheet（牛仔裤/童装那种）也要认对表头行。"""
    res = WpsExcelTool.insert_column_after(
        str(book), "牛仔裤", "尺码", "数量", header_row=2
    )
    xml = _sheet(book, "xl/worksheets/sheet2.xml")

    assert res["inserted"] is True and res["column"] == "D"
    assert '<c r="D2" s="1" t="str"><v>数量</v></c>' in xml
    assert '<c r="E2" s="1" t="str"><v>备注</v></c>' in xml, "原备注列右移"
    assert '<row r="1"><c r="A1" s="1" t="str"><v>2026年牛仔裤订单登记</v></c></row>' in xml


def test_insert_column_rejects_missing_anchor(book):
    """找不到锚点列就抛错：位置猜不得，插错列等于整表错位。"""
    with pytest.raises(ValueError, match="找不到"):
        WpsExcelTool.insert_column_after(str(book), "插入表", "不存在的列", "数量")


def test_shift_ref_cols_rules():
    """列平移规则：插入位左侧不动、右侧后移，$ 保留，到底的列号卡在 XFD 不越界。"""
    assert WpsExcelTool._shift_ref_cols("B1:H3", 5) == "B1:I3"
    assert WpsExcelTool._shift_ref_cols("C1:C1048576", 5) == "C1:C1048576"
    assert WpsExcelTool._shift_ref_cols("$D$2:$F$9", 5) == "$D$2:$G$9"
    assert WpsExcelTool._shift_ref_cols("C1:C9 F1:F9", 5) == "C1:C9 G1:G9"
    assert WpsExcelTool._shift_ref_cols("XFD1", 5) == "XFD1"


def test_shift_head_cols_new_col_inherits_left_range():
    """左邻列是多列段的末列时，新 <col> 要插在它之后并继承它的样式（<col> 必须升序）。"""
    head = (
        '<worksheet><dimension ref="A1:H9"/><cols>'
        '<col min="1" max="4" width="12" style="55"/>'
        '<col min="5" max="8" width="20" style="99"/></cols>'
    )
    out = WpsExcelTool._shift_head_cols(head, 5, WpsExcelTool._col_style(head, 4), 9.0)

    assert re.findall(r'<col [^>]*/>', out) == [
        '<col min="1" max="4" width="12" style="55"/>',
        '<col min="5" max="5" width="9.0" style="55" customWidth="1"/>',
        '<col min="6" max="9" width="20" style="99"/>',
    ]
    assert '<dimension ref="A1:I9"/>' in out


def test_shift_head_cols_extends_range_spanning_insert_point():
    """跨过插入位的段只把 max +1：新列并进这一段，自然继承它的样式与列宽。"""
    head = '<worksheet><cols><col min="3" max="8" width="20" style="99"/></cols>'

    out = WpsExcelTool._shift_head_cols(head, 5, "99", 9.0)

    assert re.findall(r'<col [^>]*/>', out) == ['<col min="3" max="9" width="20" style="99"/>']


def test_shift_head_cols_survives_sheet_without_cols():
    """没有 <cols> 定义的表（新列拿默认列宽）不该报错——数据平移才是要紧事。"""
    out = WpsExcelTool._shift_head_cols(
        '<worksheet><dimension ref="A1:H9"/><sheetViews><sheetView topLeftCell="F2">'
        '<selection activeCell="F2" sqref="F2"/></sheetView></sheetViews>',
        5, "", 9.0,
    )

    assert "<col " not in out
    assert '<dimension ref="A1:I9"/>' in out
    assert 'topLeftCell="G2"' in out and 'sqref="G2"' in out, "视口与选区也要跟着移"


def test_shift_data_cols_rejects_real_cell_refs_in_formula():
    """DISPIMG 放行，带真单元格引用的公式一律中止——移了列不改公式就是静默算错。"""
    ok = (
        '<row r="2" spans="1:9"><c r="G2" t="str">'
        '<f>_xlfn.DISPIMG(&quot;ID_8C37B8B84F0292F3A9CB7081234567AB&quot;,1)</f>'
        "<v>x</v></c></row>"
    )
    moved = WpsExcelTool._shift_data_cols(ok, 5)
    assert '<c r="H2"' in moved and 'spans="1:10"' in moved

    with pytest.raises(ValueError, match="公式引用错位"):
        WpsExcelTool._shift_data_cols('<row r="2"><c r="G2"><f>SUM(A2:F2)</f></c></row>', 5)


def test_insert_column_shifts_only_this_sheets_defined_name(book):
    """筛选区缓存 definedName 只动本表那条，别的表不能被带着改。"""
    WpsExcelTool.insert_column_after(str(book), "插入表", "尺码", "数量")
    with zipfile.ZipFile(book) as zf:
        wbx = zf.read("xl/workbook.xml").decode("utf-8")

    assert "插入表!$B$1:$I$3" in wbx
    assert "牛仔裤!$A$1:$D$3" in wbx, "别的表那条原样不动"
