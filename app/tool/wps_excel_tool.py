# -*- coding: utf-8 -*-
"""WPS DISPIMG 表的安全写入工具。

为什么不用 openpyxl：用户的「商品成本核算」表用 WPS 私有的 DISPIMG 机制把
商品图嵌入单元格（单元格存 `=_xlfn.DISPIMG("ID_x",1)` 公式 + `xl/cellimages.xml`
图片库 + `xl/media/` 图片）。openpyxl 的 load→save 不认识 cellimages.xml，保存时会
整个删掉它，破坏所有嵌入图（曾导致 217MB→167MB、丢 600+ 图）。

本工具全程只做 zip/XML 增量修改：复制所有未改部件，仅重写受影响的 sheet /
cellimages.xml / cellimages.xml.rels，并新增 media 图片。原有公式、样式、图片、
合并单元格全部原样保留。
"""

import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_output_dir
from app.tool.base import BaseTool, ToolResult


# OOXML 命名空间与列名工具 -------------------------------------------------

NS_ETC = "http://www.wps.cn/officeDocument/2017/etCustomData"


def _col_to_idx(col: str) -> int:
    """列字母 → 1-based 索引。A=1, Z=26, AA=27。"""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _idx_to_col(idx: int) -> str:
    """1-based 索引 → 列字母。"""
    s = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class WpsExcelTool(BaseTool):
    """安全读写 WPS DISPIMG 嵌入图表格，不破坏嵌入图与格式。

    动作（action）：
    - inspect：读取指定 sheet 的表头（列字母→标题）、最后一个真实数据行、
      以及若干样例行的公式，供 LLM 理解表结构与计算方式。
    - append_product_row：在最后一个数据行之后追加一行商品数据。逐格复制
      上一数据行的样式索引（s），公式列按相邻行模板替换行号生成（保留公式、
      对齐小数位/格式）。可选 image_path 同时把商品图嵌入图片列。
    """

    name: str = "wps_excel_tool"
    description: str = """安全读写 WPS（金山）含嵌入图（DISPIMG）的 Excel 表格，不破坏商品图、公式和单元格格式。
当目标 .xlsx 的单元格里出现 =_xlfn.DISPIMG(...) 公式时，**必须用本工具**而不是 openpyxl/pandas，否则会删掉所有嵌入图。
动作：
- inspect：理解表结构。返回每列字母对应的标题、最后一个真实数据行号、样例行的公式（据此学习计算方式）。
- append_product_row：在最后一行后插入一行新商品。自动复制上一行的格式与公式（保留小数位、公式引用自动改行号）。
  通过 column_values 传"列字母→硬编码值"（如采购价、重量、销售价、SPU、站点等手动数据），
  通过 formula_columns 传"列字母→公式模板"（用 {r} 占位当前行号，如 折扣列 "I{r}/G{r}"）。
  可选 image_path：本地图片路径，嵌入到 image_column 指定的图片列（DISPIMG 机制，随单元格走）。
每次写入前自动生成时间戳备份。"""

    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "append_product_row"],
                "description": "inspect=理解表结构；append_product_row=追加一行商品数据",
            },
            "file_path": {
                "type": "string",
                "description": "目标 .xlsx 的绝对路径",
            },
            "sheet_name": {
                "type": "string",
                "description": "工作表名称，如 'wintak童装货盘记录'",
            },
            "column_values": {
                "type": "object",
                "description": "append 时：列字母→硬编码值。例 {\"A\":\"秘鲁\",\"D\":9256117122,\"G\":87.7,\"I\":75,\"J\":25,\"K\":0.3,\"O\":7}",
            },
            "formula_columns": {
                "type": "object",
                "description": "append 时：列字母→公式模板（不含=号，用 {r} 表示当前行号）。例 {\"H\":\"I{r}/G{r}\",\"L\":\"K{r}*80+1\",\"N\":\"I{r}/O{r}\",\"P\":\"J{r}+L{r}+N{r}\",\"Q\":\"I{r}-P{r}\",\"R\":\"Q{r}/I{r}\"}",
            },
            "image_path": {
                "type": "string",
                "description": "append 时可选：本地商品图绝对路径，嵌入到 image_column 列",
            },
            "image_column": {
                "type": "string",
                "description": "append 时图片所在列字母，如 'F'。提供 image_path 时必填",
            },
        },
        "required": ["action", "file_path", "sheet_name"],
    }

    # ---- 内部：定位 sheet 对应的 xml 部件名 -----------------------------

    @staticmethod
    def _resolve_sheet_part(zf: zipfile.ZipFile, sheet_name: str) -> Optional[str]:
        wb = zf.read("xl/workbook.xml").decode("utf-8")
        m = re.search(
            r'<sheet[^>]*name="%s"[^>]*r:id="(rId\d+)"' % re.escape(sheet_name), wb
        )
        if not m:
            return None
        rid = m.group(1)
        rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        mt = re.search(r'<Relationship Id="%s"[^>]*Target="([^"]+)"' % rid, rels)
        if not mt:
            return None
        target = mt.group(1).lstrip("/")
        return target if target.startswith("xl/") else "xl/" + target

    @staticmethod
    def _parse_rows(sheet_xml: str) -> List[tuple]:
        return re.findall(r'<row r="(\d+)"[^>]*>(.*?)</row>', sheet_xml, re.S)

    @staticmethod
    def _shared_strings(zf: zipfile.ZipFile) -> List[str]:
        try:
            ss = zf.read("xl/sharedStrings.xml").decode("utf-8")
        except KeyError:
            return []
        # 每个 <si> 取其文本（拼接所有 <t>）
        out = []
        for si in re.findall(r"<si>(.*?)</si>", ss, re.S):
            texts = re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)
            out.append("".join(texts))
        return out

    @classmethod
    def _cell_text(cls, cell_xml: str, shared: List[str]) -> str:
        """取单元格显示文本：t="s" 查 sharedStrings，否则取 <v> 或 <f>。"""
        t = re.search(r'\st="([^"]+)"', cell_xml)
        v = re.search(r"<v>(.*?)</v>", cell_xml, re.S)
        f = re.search(r"<f[^>]*>(.*?)</f>", cell_xml, re.S)
        if t and t.group(1) == "s" and v:
            try:
                return shared[int(v.group(1))]
            except (ValueError, IndexError):
                return ""
        if f:
            return "=" + f.group(1)
        if v:
            return v.group(1)
        return ""

    @classmethod
    def _find_last_data_row(cls, rows: List[tuple], key_cols: List[str]) -> int:
        """最后一个真实数据行：key_cols 中任一列有 <v> 值的最大行号。"""
        last = 1
        for rid, body in rows:
            for col in key_cols:
                if re.search(r'<c r="%s%s"[^>]*>(?:<f[^>]*>.*?</f>)?<v>' % (col, rid), body, re.S):
                    last = max(last, int(rid))
                    break
        return last

    @classmethod
    def existing_key_values(
        cls, file_path: str, sheet_name: str, col: str = "D"
    ) -> set:
        """返回某列（默认 D=SPU）在数据行(row>1)里所有非空值的集合（字符串）。

        供批量采集在开跑前判断某 SPU 是否已入库、可跳过（幂等 / 断点续跑）。
        文件或工作表缺失、解析异常时返回空集，绝不抛错。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return set()
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                vals = set()
                for rid, body in cls._parse_rows(sheet_xml):
                    if rid == "1":
                        continue
                    m = re.search(r'<c r="%s%s"[^>]*>.*?</c>' % (col, rid), body, re.S)
                    if m:
                        txt = cls._cell_text(m.group(0), shared).strip()
                        if txt:
                            vals.add(txt)
                return vals
        except Exception:
            return set()

    # ---- inspect --------------------------------------------------------

    def _inspect(self, file_path: str, sheet_name: str) -> ToolResult:
        with zipfile.ZipFile(file_path) as zf:
            part = self._resolve_sheet_part(zf, sheet_name)
            if not part:
                return self.fail_response(f"找不到工作表 '{sheet_name}'")
            sheet_xml = zf.read(part).decode("utf-8")
            shared = self._shared_strings(zf)
            rows = self._parse_rows(sheet_xml)
            row_map = dict(rows)

            # 表头（第1行）：列字母→标题
            header = {}
            if "1" in row_map:
                for cm in re.finditer(r'<c r="([A-Z]+)1"[^>]*>.*?</c>', row_map["1"], re.S):
                    col = cm.group(1)
                    txt = self._cell_text(cm.group(0), shared)
                    if txt:
                        header[col] = txt

            # 最后数据行：用表头里像"SPU"/"ID"/"站点"的列，找不到就用 D/A
            key_cols = [c for c, h in header.items() if any(k in h for k in ("SPU", "ID", "站点", "货号"))]
            if not key_cols:
                key_cols = ["A", "D"]
            last_data = self._find_last_data_row(rows, key_cols)

            # 样例：最后数据行的每列公式/值
            sample = {}
            if str(last_data) in row_map:
                for cm in re.finditer(r'<c r="([A-Z]+)%d"[^>]*>.*?</c>' % last_data, row_map[str(last_data)], re.S):
                    col = cm.group(1)
                    fm = re.search(r"<f[^>]*>(.*?)</f>", cm.group(0), re.S)
                    if fm:
                        sample[col] = "=" + fm.group(1).replace("&quot;", '"')
                    else:
                        vm = re.search(r"<v>(.*?)</v>", cm.group(0), re.S)
                        if vm:
                            sample[col] = self._cell_text(cm.group(0), shared)

        # SPU 列（用于批量采集判重）：表头里含 SPU/ID 的列，找不到退化到 D
        spu_col = next(
            (c for c, h in header.items() if "SPU" in h or "ID" in h.upper()), "D"
        )
        existing_spus = self.existing_key_values(file_path, sheet_name, spu_col)

        out = {
            "sheet": sheet_name,
            "part": part,
            "header_列标题": header,
            "last_data_row_最后数据行": last_data,
            "next_row_建议插入行": last_data + 1,
            "sample_最后行公式与值": sample,
            "SPU列": spu_col,
            "已入库SPU数": len(existing_spus),
            "提示": "公式列含 = 开头；硬编码列为纯值。append 时按此结构传 column_values 与 formula_columns。"
            "批量采集前可用 SPU列/已入库SPU 判重跳过。",
        }
        return self.success_response(json.dumps(out, ensure_ascii=False, indent=2))

    # ---- append_product_row --------------------------------------------

    def _append(
        self,
        file_path: str,
        sheet_name: str,
        column_values: Dict[str, Any],
        formula_columns: Dict[str, str],
        image_path: Optional[str],
        image_column: Optional[str],
    ) -> ToolResult:
        src = Path(file_path)
        if not src.exists():
            return self.fail_response(f"文件不存在：{file_path}")

        # 写前自动时间戳备份：统一放到桌面输出目录的「Excel备份」子目录，
        # 不再堆在源文件（桌面）旁边把桌面撑爆。
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = get_output_dir("backup") / f"{src.stem}_备份{ts}{src.suffix}"
        shutil.copyfile(src, backup)

        with zipfile.ZipFile(src) as zf:
            part = self._resolve_sheet_part(zf, sheet_name)
            if not part:
                return self.fail_response(f"找不到工作表 '{sheet_name}'")
            sheet_xml = zf.read(part).decode("utf-8")
            rows = self._parse_rows(sheet_xml)
            row_map = dict(rows)

            key_cols = ["A", "D"]
            for c in column_values:
                key_cols.append(c)
            last_data = self._find_last_data_row(rows, list(set(key_cols)))
            tpl_body = row_map.get(str(last_data), "")
            # 紧接最后数据行的下一行。该行号可能已是预留空行（需替换其内容），
            # 也可能不存在（需新建插入到正确位置）。
            new_rid = last_data + 1
            target_exists = str(new_rid) in row_map

            # 模板每列样式索引
            cell_styles = dict(re.findall(r'<c r="([A-Z]+)\d+"\s+s="(\d+)"', tpl_body))

            changed: Dict[str, bytes] = {}
            new_media: Optional[tuple] = None  # (arcname, bytes)

            # 图片：追加 media + cellimages + rels，得到 DISPIMG ID
            disp_id = None
            if image_path:
                if not image_column:
                    return self.fail_response("提供 image_path 时必须提供 image_column")
                img = Path(image_path)
                if not img.exists():
                    return self.fail_response(f"图片不存在：{image_path}")
                cellimages = zf.read("xl/cellimages.xml").decode("utf-8")
                ci_rels = zf.read("xl/_rels/cellimages.xml.rels").decode("utf-8")

                ext = img.suffix.lower().lstrip(".")
                if ext == "jpg":
                    ext = "jpeg"
                img_nums = [int(n) for n in re.findall(r"media/image(\d+)\.", ci_rels)]
                new_img_n = (max(img_nums) + 1) if img_nums else 1
                arc_media = f"xl/media/image{new_img_n}.{ext}"

                rid_nums = [int(n) for n in re.findall(r'Id="rId(\d+)"', ci_rels)]
                new_rel_id = f"rId{max(rid_nums) + 1 if rid_nums else 1}"
                pic_ids = [int(n) for n in re.findall(r'<xdr:cNvPr id="(\d+)"', cellimages)]
                new_pic_id = max(pic_ids) + 1 if pic_ids else 2
                disp_id = f"ID_{ts}{new_img_n:08d}".ljust(36, "0")[:36]

                new_ci = (
                    '<etc:cellImage><xdr:pic><xdr:nvPicPr>'
                    f'<xdr:cNvPr id="{new_pic_id}" name="{disp_id}" descr="collected_image"/>'
                    '<xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
                    f'<a:blip r:embed="{new_rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
                    '</xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/>'
                    '<a:ext cx="514350" cy="514350"/></a:xfrm>'
                    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
                    "</xdr:pic></etc:cellImage>"
                )
                changed["xl/cellimages.xml"] = cellimages.replace(
                    "</etc:cellImages>", new_ci + "</etc:cellImages>"
                ).encode("utf-8")
                new_rel = (
                    f'<Relationship Id="{new_rel_id}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    f'Target="media/image{new_img_n}.{ext}"/>'
                )
                changed["xl/_rels/cellimages.xml.rels"] = ci_rels.replace(
                    "</Relationships>", new_rel + "</Relationships>"
                ).encode("utf-8")
                new_media = (arc_media, img.read_bytes())

            # 构造新行单元格
            def s_attr(col: str) -> str:
                return f' s="{cell_styles[col]}"' if col in cell_styles else ""

            cells = []
            cols_all = set(column_values) | set(formula_columns)
            if image_column and disp_id:
                cols_all.add(image_column)
            for col in sorted(cols_all, key=_col_to_idx):
                ref = f"{col}{new_rid}"
                if col == image_column and disp_id:
                    # DISPIMG 单元格：必须带 <v> 缓存值，WPS 才渲染
                    # 图片列样式从一个真实图片行取（模板行该列可能为空样式）
                    img_style = self._img_cell_style(rows, col) or cell_styles.get(col, "")
                    sa = f' s="{img_style}"' if img_style else ""
                    disp = f'_xlfn.DISPIMG(&quot;{disp_id}&quot;,1)'
                    cached = f'=DISPIMG(&quot;{disp_id}&quot;,1)'
                    cells.append(f'<c r="{ref}"{sa} t="str"><f>{disp}</f><v>{cached}</v></c>')
                elif col in formula_columns:
                    f = formula_columns[col].format(r=new_rid).lstrip("=")
                    f = _xml_escape(f)
                    cells.append(f'<c r="{ref}"{s_attr(col)}><f>{f}</f></c>')
                elif col in column_values:
                    val = column_values[col]
                    if isinstance(val, (int, float)):
                        cells.append(f'<c r="{ref}"{s_attr(col)}><v>{val}</v></c>')
                    else:
                        cells.append(
                            f'<c r="{ref}"{s_attr(col)} t="str"><v>{_xml_escape(val)}</v></c>'
                        )

            # 行开标签复制模板行（保留行高/样式）
            row_open_m = re.search(r'(<row r="%d"[^>]*>)' % last_data, sheet_xml)
            if row_open_m:
                row_open = row_open_m.group(1).replace(
                    'r="%d"' % last_data, 'r="%d"' % new_rid
                )
            else:
                row_open = f'<row r="{new_rid}">'
            new_row = row_open + "".join(cells) + "</row>"

            if target_exists:
                # 目标行号已是预留空行：整行替换，避免重复行号导致文件损坏
                sheet_new = re.sub(
                    r'<row r="%d"[^>]*>.*?</row>' % new_rid,
                    lambda _m: new_row,
                    sheet_xml,
                    count=1,
                    flags=re.S,
                )
            else:
                # 目标行号不存在：插入到最后数据行之后（紧邻其位置）
                anchor = re.search(r'(<row r="%d"[^>]*>.*?</row>)' % last_data, sheet_xml, re.S)
                if anchor:
                    sheet_new = sheet_xml.replace(
                        anchor.group(1), anchor.group(1) + new_row, 1
                    )
                else:
                    sheet_new = sheet_xml.replace("</sheetData>", new_row + "</sheetData>")
            sheet_new = re.sub(
                r'<dimension ref="(A1:[A-Z]+)\d+"/>',
                lambda m: f'<dimension ref="{m.group(1).split(":")[0]}:{_idx_to_col(max(_col_to_idx(re.match(r"[A-Z]+", m.group(1).split(":")[1]).group(0)), 1))}{new_rid}"/>',
                sheet_new,
            )
            changed[part] = sheet_new.encode("utf-8")

            # 写出新文件（临时名 → 替换）
            tmp = src.with_name(src.stem + f"_tmp{ts}" + src.suffix)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zf.infolist():
                    data = changed.get(item.filename)
                    if data is None:
                        data = zf.read(item.filename)
                    zout.writestr(item, data)
                if new_media:
                    zout.writestr(new_media[0], new_media[1])

        # 用临时文件替换原文件
        shutil.move(str(tmp), str(src))

        result = {
            "成功": True,
            "插入行号": new_rid,
            "工作表": sheet_name,
            "写入硬编码列": column_values,
            "写入公式列": {k: v.format(r=new_rid) for k, v in formula_columns.items()},
            "嵌入图片": bool(image_path),
            "图片列": image_column if image_path else None,
            "DISPIMG_ID": disp_id,
            "自动备份": str(backup),
            "提示": "请在 WPS 中打开确认新行的图片与格式。原有数据、图片、公式均未改动。",
        }
        return self.success_response(json.dumps(result, ensure_ascii=False, indent=2))

    @staticmethod
    def _img_cell_style(rows: List[tuple], col: str) -> Optional[str]:
        """从一个真实 DISPIMG 单元格取该列的样式索引（模板行该列可能为空）。"""
        for _, body in rows:
            m = re.search(r'<c r="%s\d+"\s+s="(\d+)"[^>]*t="str"><f>_xlfn\.DISPIMG' % col, body)
            if m:
                return m.group(1)
        return None

    async def execute(
        self,
        action: str,
        file_path: str,
        sheet_name: str,
        column_values: Optional[Dict[str, Any]] = None,
        formula_columns: Optional[Dict[str, str]] = None,
        image_path: Optional[str] = None,
        image_column: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        try:
            if action == "inspect":
                return self._inspect(file_path, sheet_name)
            elif action == "append_product_row":
                return self._append(
                    file_path,
                    sheet_name,
                    column_values or {},
                    formula_columns or {},
                    image_path,
                    image_column,
                )
            else:
                return self.fail_response(f"未知 action：{action}")
        except Exception as e:
            import traceback

            return self.fail_response(f"WpsExcelTool 执行失败：{e}\n{traceback.format_exc()}")
