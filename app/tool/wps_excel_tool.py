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

# 单元格引用（列字母+行号），带可选 $ 绝对标记；(?<![A-Za-z0-9_$]) 避开函数名尾随数字。
# 模块级常量：WpsExcelTool 是 Pydantic 模型，类体内 `_x = ...` 会被当私有属性吞掉，
# 故编译好的正则放模块级，避免变成 ModelPrivateAttr。
_CELL_REF_RE = re.compile(r"(?<![A-Za-z0-9_$])(\$?)([A-Z]{1,3})(\$?)(\d+)")

# 逻辑字段 → 表头标题判定。采集管道要往【任意布局】的 Sheet 写，绝不能再假设固定列序：
# 实测同一工作簿里 pawly美国/pawly全球/wintak/VibeMakers 各 Sheet 的列序都不同（SPU 在
# C 还是 D、图片在 D 还是 E、采购价在 I 还是 J…全不一样），旧代码把字段硬编码成 pawly全球
# 的列 → 换 Sheet 就整体错位（SPU 写进"产品图片"列、采购价写进"重量"列）。改为按【表头标题】
# 把每个逻辑字段解析到该 Sheet 的真实列。
# 规则顺序即认领优先级；一列至多归一个字段（先到先得）。pick='last' 用于"备注"——有的
# Sheet 有多列备注（前置那列常被挪作它用），取末列最稳。判定前对标题 strip()。
_FIELD_RULES = [
    ("spu", lambda t: "spu" in t.lower(), "first"),
    ("image", lambda t: ("产品图片" in t) or t == "图片", "first"),
    ("site", lambda t: t == "站点", "first"),
    ("category", lambda t: t in ("类目", "类别", "品类", "分类"), "first"),
    ("daily", lambda t: t == "日常价", "first"),
    ("sale", lambda t: t in ("销售价格", "销售价", "售价"), "first"),
    ("purchase", lambda t: t in ("采购价格", "采购价", "购入价格"), "first"),
    ("weight", lambda t: t == "重量", "first"),
    ("ros", lambda t: t.lower() == "ros", "first"),
    ("note", lambda t: t == "备注", "last"),
]


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

    # ---- 共享公式解析 ----------------------------------------------------
    # 本表（WPS 导出）把整列同型公式存成【共享公式】：仅一个「主单元格」带公式文本
    #   <f t="shared" ref="H8:H23" si="4">I8/G8</f>
    # 其余单元格只引用 si、自身不含文本、且是【自闭合】标签
    #   <f t="shared" si="4"/>
    # 旧 _inspect 用 `<f[^>]*>(.*?)</f>` 抓不到自闭合 <f/>，会退化去读 <v> 缓存【数值】，
    # 于是 H/L/N/P/Q/R 被当成硬编码值丢弃（write_product_row 只留 = 开头的），
    # 新行整列公式全丢 → 折扣/空运/成本/利润/毛利全空，且坏行变"最后行"后自我传染。
    # 下面按 OOXML 语义解析：建 si→主公式表，按相对偏移把主公式平移到目标行/列。

    @classmethod
    def _shared_formula_masters(cls, sheet_xml: str) -> Dict[str, tuple]:
        """扫全表主单元格，建 si → (主行号, 主列索引, 公式文本)。仅主单元格带文本。"""
        masters: Dict[str, tuple] = {}
        for m in re.finditer(
            r'<c r="([A-Z]+)(\d+)"[^>]*?><f\b([^>]*?)>([^<]*)</f>', sheet_xml
        ):
            col, row, attrs, text = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            si_m = re.search(r'si="(\d+)"', attrs)
            if si_m and "shared" in attrs and text.strip():
                masters.setdefault(
                    si_m.group(1),
                    (row, _col_to_idx(col), text.replace("&quot;", '"')),
                )
        return masters

    @classmethod
    def _shift_formula(cls, formula: str, drow: int, dcol: int) -> str:
        """把公式里所有【相对】单元格引用按 (drow, dcol) 平移；绝对引用($)与常数不动。"""
        def repl(mo: "re.Match") -> str:
            col_abs, col, row_abs, row = mo.group(1), mo.group(2), mo.group(3), mo.group(4)
            new_col = col if col_abs else _idx_to_col(_col_to_idx(col) + dcol)
            new_row = row if row_abs else str(int(row) + drow)
            return f"{col_abs}{new_col}{row_abs}{new_row}"

        return _CELL_REF_RE.sub(repl, formula)

    @classmethod
    def _resolve_cell_formula(
        cls, cell_xml: str, row: int, col_idx: int, masters: Dict[str, tuple]
    ) -> Optional[str]:
        """取单元格公式文本（不含=）：主单元格取原文；共享依赖单元格从主公式平移解析；
        非公式单元格返回 None。"""
        m = re.search(r"<f\b([^>]*?)(?:/>|>(.*?)</f>)", cell_xml, re.S)
        if not m:
            return None
        attrs = m.group(1)
        text = (m.group(2) or "").strip()
        si_m = re.search(r'si="(\d+)"', attrs)
        if "shared" in attrs and not text and si_m:
            master = masters.get(si_m.group(1))
            if not master:
                return None
            m_row, m_col, m_formula = master
            return cls._shift_formula(m_formula, row - m_row, col_idx - m_col)
        if text:
            return text.replace("&quot;", '"')
        return None

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

    @classmethod
    def read_header(cls, file_path: str, sheet_name: str) -> Dict[str, str]:
        """读某 Sheet 第一行表头：{列字母: 标题}。文件/表缺失或损坏返回 {}，绝不抛错。"""
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return {}
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                row_map = dict(cls._parse_rows(sheet_xml))
                header: Dict[str, str] = {}
                if "1" in row_map:
                    for cm in re.finditer(
                        r'<c r="([A-Z]+)1"[^>]*>.*?</c>', row_map["1"], re.S
                    ):
                        txt = cls._cell_text(cm.group(0), shared)
                        if txt:
                            header[cm.group(1)] = txt
                return header
        except Exception:
            return {}

    @classmethod
    def _resolve_fields_from_header(cls, header: Dict[str, str]) -> Dict[str, str]:
        """把逻辑字段解析到该 Sheet 的真实列：{字段名: 列字母}。见 _FIELD_RULES。

        一列至多归一个字段（先到先得，避免"备注"抢占别的列）；未命中的字段不在结果里。
        """
        roles: Dict[str, str] = {}
        used: set = set()
        for role, pred, pick in _FIELD_RULES:
            hits = sorted(
                [c for c, title in header.items() if c not in used and pred(title.strip())],
                key=_col_to_idx,
            )
            if not hits:
                continue
            col = hits[-1] if pick == "last" else hits[0]
            roles[role] = col
            used.add(col)
        return roles

    @classmethod
    def resolve_field_columns(cls, file_path: str, sheet_name: str) -> Dict[str, str]:
        """公开入口：读某 Sheet 表头并解析出 {逻辑字段: 列字母}。

        供采集管道按【真实列序】写入/判重，取代旧的硬编码列假设。文件/表不可读返回 {}。
        字段名见 _FIELD_RULES：spu/image/site/category/daily/sale/purchase/weight/ros/note。
        """
        return cls._resolve_fields_from_header(cls.read_header(file_path, sheet_name))

    @classmethod
    def spu_column(cls, file_path: str, sheet_name: str, default: str = "D") -> str:
        """该 Sheet 里 SPU 所在列字母；表头解析不出（表损坏/无 SPU 列）时退回 default。"""
        return cls.resolve_field_columns(file_path, sheet_name).get("spu", default)

    @classmethod
    def column_numeric_constants(
        cls,
        file_path: str,
        sheet_name: str,
        exclude_cols: Optional[set] = None,
        min_samples: int = 3,
        dominance: float = 0.6,
    ) -> Dict[str, float]:
        """扫描数据行(row>1)，找出各列里【每行都填同一个数字】的常量列，返回 {列: 数字}。

        为什么需要：不同 Sheet 的成本模型不同——除采购价/重量这类逐商品输入外，还有 ros、
        操作费、尾程这类【每行固定的数值常量输入】，下游公式(成本=采购价+操作费+尾程+…)依赖
        它们。旧代码只硬编码 ros=7，换到 pawly美国(ros=6、另有操作费=5/尾程=8)就让这些格留空、
        公式算错。此法从历史行学出这些常量并复制到新行，天然适配任意 Sheet。

        只认【纯数字】常量：文本型批次标注(编号筛选/总出货数)不返回，避免误抄。
        判定：某列非空且为纯数字的单元格里，出现最多的那个数字占比≥dominance 且样本≥min_samples，
        则该数字为列常量。公式单元格(_cell_text 返回 '=' 开头)天然被排除；共享公式的缓存数值
        逐行不同→占比达不到阈值，也不会被误判为常量。exclude_cols 里的列直接跳过。
        文件/表不可读返回 {}。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return {}
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                rows = cls._parse_rows(sheet_xml)
                return cls._scan_numeric_constants(
                    rows, shared, exclude_cols, min_samples, dominance
                )
        except Exception:
            return {}

    @classmethod
    def _scan_numeric_constants(
        cls,
        rows: List[tuple],
        shared: List[str],
        exclude_cols: Optional[set] = None,
        min_samples: int = 3,
        dominance: float = 0.6,
    ) -> Dict[str, float]:
        """column_numeric_constants 的内核（已拿到 rows/shared）：返回 {列: 常量数字}。

        供 _inspect 在同一次 sheet 扫描里顺带算出常量列，避免对 200MB 工作簿再开一遍 zip。
        """
        from collections import Counter

        exclude = set(exclude_cols or set())
        col_nums: Dict[str, Counter] = {}
        for rid, body in rows:
            if rid == "1":
                continue
            for cm in re.finditer(
                r'<c r="([A-Z]+)\d+"[^>]*?(?:/>|>.*?</c>)', body, re.S
            ):
                col = cm.group(1)
                if col in exclude:
                    continue
                txt = cls._cell_text(cm.group(0), shared).strip()
                if not txt or txt.startswith("="):
                    continue
                try:
                    col_nums.setdefault(col, Counter())[float(txt)] += 1
                except ValueError:
                    # 该列出现过非数字（文本标注）→ 标记为脏，永不当常量列
                    col_nums.setdefault(col, Counter())[float("nan")] += 1
        out: Dict[str, float] = {}
        for col, counter in col_nums.items():
            if any(v != v for v in counter):  # 含 NaN（曾有非数字）→ 跳过
                continue
            total = sum(counter.values())
            if total < min_samples:
                continue
            val, cnt = counter.most_common(1)[0]
            if cnt / total >= dominance:
                out[col] = val
        return out

    @classmethod
    def list_sheets(cls, file_path: str) -> List[str]:
        """返回工作簿里所有工作表名（按 workbook.xml 声明顺序）。

        供 UI 的 Sheet 下拉列举目标表。纯读 xl/workbook.xml，文件缺失/损坏/非 zip
        时返回空列表，绝不抛错（与 existing_key_values 同风格）。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                wb = zf.read("xl/workbook.xml").decode("utf-8")
            names = re.findall(r'<sheet[^>]*\bname="([^"]+)"', wb)
            # 表名里的 & < > " 在 XML 属性里是转义的，还原成显示名
            unescape = {
                "&amp;": "&", "&lt;": "<", "&gt;": ">",
                "&quot;": '"', "&apos;": "'",
            }
            out = []
            for n in names:
                for k, v in unescape.items():
                    n = n.replace(k, v)
                out.append(n)
            return out
        except Exception:
            return []

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

            # 样例：最后数据行的每列公式/值。公式列用共享公式解析器（见
            # _resolve_cell_formula）——旧逻辑抓不到自闭合 <f t="shared" si=.../>，
            # 会把公式列当数值丢，导致 append 出的新行整列公式缺失。
            masters = self._shared_formula_masters(sheet_xml)
            sample = {}

            def _row_cells(rid: int) -> List[tuple]:
                body = row_map.get(str(rid), "")
                return re.findall(r'<c r="([A-Z]+)%d"[^>]*?(?:/>|>.*?</c>)' % rid, body, re.S)

            def _cell_xml(rid: int, col: str) -> str:
                body = row_map.get(str(rid), "")
                mm = re.search(r'<c r="%s%d"[^>]*?(?:/>|>.*?</c>)' % (col, rid), body, re.S)
                return mm.group(0) if mm else ""

            if str(last_data) in row_map:
                for col, full in re.findall(
                    r'<c r="([A-Z]+)%d"[^>]*?(?:/>|>(.*?)</c>)' % last_data,
                    row_map[str(last_data)],
                    re.S,
                ):
                    cx = _cell_xml(last_data, col)
                    f = self._resolve_cell_formula(cx, last_data, _col_to_idx(col), masters)
                    if f is not None:
                        sample[col] = "=" + f
                    else:
                        vm = re.search(r"<v>(.*?)</v>", cx, re.S)
                        if vm:
                            sample[col] = self._cell_text(cx, shared)

            # 回填：最后数据行若是坏行（管道曾漏写整列公式），其公式列会缺失。
            # 向上找最近的健康行，把该有公式却在 sample 里缺席/非公式的列补齐，
            # 并平移到 last_data 行——这样 append 拿到的模板永远带全套公式，
            # 不会被坏的尾行传染。formula_cols_expected = 主公式表覆盖到的列全集。
            formula_cols_expected = {
                _idx_to_col(mc) for (_, mc, _) in masters.values()
            }
            missing = {
                c for c in formula_cols_expected
                if not str(sample.get(c, "")).startswith("=")
            }
            rid = last_data - 1
            while missing and rid > 1:
                for col in list(missing):
                    cx = _cell_xml(rid, col)
                    if not cx:
                        continue
                    f = self._resolve_cell_formula(cx, rid, _col_to_idx(col), masters)
                    if f is not None:
                        sample[col] = "=" + self._shift_formula(f, last_data - rid, 0)
                        missing.discard(col)
                rid -= 1

        # 逻辑字段 → 真实列（按表头解析，见 _FIELD_RULES）。采集管道据此按【本 Sheet 的
        # 真实列序】写入，不再假设固定列。SPU 列优先取解析结果，退化到含 SPU/ID 的列再到 D。
        field_cols = self._resolve_fields_from_header(header)
        spu_col = field_cols.get("spu") or next(
            (c for c, h in header.items() if "SPU" in h or "ID" in h.upper()), "D"
        )
        existing_spus = self.existing_key_values(file_path, sheet_name, spu_col)

        # 公式列 = sample 里 = 开头的列（排除图片列的 DISPIMG）。
        image_col = field_cols.get("image")
        formula_cols = {
            c for c, v in sample.items()
            if isinstance(v, str) and v.startswith("=") and c != image_col
        }
        # 逐商品输入列（采集逐条填的，不是常量）：这些列不当常量、也不该被常量覆盖。
        item_cols = {
            field_cols[k] for k in
            ("spu", "image", "site", "category", "daily", "sale", "purchase", "weight", "note")
            if k in field_cols
        }
        # 公式实际引用到、却既非公式列也非逐商品输入列的列 → 是【固定数值输入】(操作费/尾程/
        # ros 等)。这些列若新行留空，成本/利润公式会算错。从历史行学出它们的常量值并回填。
        # ros 是特例：它常逐商品变(6/7/8 混填)，学不出稳定常量，故若未学出则由管道兜底默认。
        referenced: set = set()
        for c in formula_cols:
            for m in re.finditer(r"(?<![A-Za-z$])([A-Z]{1,3})\d+", sample[c]):
                referenced.add(m.group(1))
        need_const_cols = referenced - formula_cols - item_cols
        all_consts = self._scan_numeric_constants(rows, shared, item_cols | formula_cols)
        constant_columns = {c: all_consts[c] for c in need_const_cols if c in all_consts}

        out = {
            "sheet": sheet_name,
            "part": part,
            "header_列标题": header,
            "字段列映射": field_cols,
            "常量输入列": constant_columns,
            "last_data_row_最后数据行": last_data,
            "next_row_建议插入行": last_data + 1,
            "sample_最后行公式与值": sample,
            "SPU列": spu_col,
            "已入库SPU数": len(existing_spus),
            "提示": "公式列含 = 开头；硬编码列为纯值。append 时按此结构传 column_values 与 formula_columns。"
            "字段列映射给出各逻辑字段（spu/image/purchase/weight/ros…）在本表的真实列，按它写勿硬编码。"
            "常量输入列给出公式依赖的固定数值列（操作费/尾程等）及其历史常量，追加新行时应一并写入。"
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

            # 样式表：写文本列时基于该列既有样式派生「自动换行」变体（见 _wrap_style_for）。
            # styles.xml 缺失/解析失败 → wrap_ctx.ok=False，文本列退回原样式、不换行、不报错。
            try:
                styles_xml = zf.read("xl/styles.xml").decode("utf-8")
            except KeyError:
                styles_xml = ""
            wrap_ctx = self._init_wrap_ctx(styles_xml)

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
                    # 模板行缺该列样式（坏行）时，从最近的带公式行回填，保住数字格式
                    # （R=毛利百分比、L/P 两位小数等）。
                    fstyle = cell_styles.get(col) or self._formula_cell_style(rows, col)
                    fsa = f' s="{fstyle}"' if fstyle else ""
                    cells.append(f'<c r="{ref}"{fsa}><f>{f}</f></c>')
                elif col in column_values:
                    val = column_values[col]
                    if isinstance(val, (int, float)):
                        cells.append(f'<c r="{ref}"{s_attr(col)}><v>{val}</v></c>')
                    else:
                        # 文本列（类目/备注/站点…）用【带自动换行】样式，长文本才会换行而非溢出。
                        # 空串不必派生新样式（无内容可换行），沿用原样式即可。
                        sval = str(val)
                        if sval:
                            wcol = self._wrap_style_for(wrap_ctx, col, cell_styles, rows)
                            sa = f' s="{wcol}"' if wcol else ""
                        else:
                            sa = s_attr(col)
                        cells.append(
                            f'<c r="{ref}"{sa} t="str"><v>{_xml_escape(val)}</v></c>'
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

            # 若为文本列派生了 wrap 变体样式，把新 xf 追加进 styles.xml 一并重写。
            new_styles = self._rebuild_styles(wrap_ctx, styles_xml)
            if new_styles is not None:
                changed["xl/styles.xml"] = new_styles.encode("utf-8")

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

    @staticmethod
    def _formula_cell_style(rows: List[tuple], col: str) -> Optional[str]:
        """从最近的一个【带公式】单元格取该列样式索引，供模板行缺该列时回填。

        模板行若是坏行（管道曾漏写整列公式），该列无 s= → 新公式会丢失数字格式
        （尤其 R=毛利 的百分比、L/P 的两位小数）。向下扫所有行找该列首个带公式且带
        s= 的单元格，取其样式，让追加的公式列显示格式与历史行一致。
        """
        for _, body in rows:
            m = re.search(r'<c r="%s\d+"\s+s="(\d+)"[^>]*?><f\b' % col, body)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _text_cell_style(rows: List[tuple], col: str) -> Optional[str]:
        """取该列首个【非公式】数据单元格的样式索引，供文本列缺模板样式时回填。

        为什么需要：写文本列（类目/备注/站点等）要基于该列既有样式派生「自动换行」变体。
        但模板行（最后数据行）常是坏行、该列无 s=（如本表 T 备注/B 类目在尾行为空样式），
        直接用默认样式会丢掉该列历史的字体/边框/数字格式。向前扫【数据行】取该列首个带 s= 且
        不含公式的单元格样式（类目列历史恒为 342、备注列为 28），据此派生 wrap 变体最贴合。

        【跳过表头行(row 1)】表头单元格本就开了 wrapText 但带加粗/居中的标题样式，若拿它当
        基样式会把标题样式套到数据格上（类目/备注变粗）。只从 row>1 的数据行取。
        """
        for rid, body in rows:
            if rid == "1":
                continue
            for m in re.finditer(
                r'<c r="%s\d+"\s+s="(\d+)"[^>]*?(?:/>|>(.*?)</c>)' % col, body, re.S
            ):
                if "<f" not in (m.group(2) or ""):
                    return m.group(1)
        return None

    # ---- 自动换行（wrapText）样式派生 -----------------------------------
    # 写入的文本列（类目/备注/站点…）此前不自动换行：新行逐列复制模板行的样式索引 s=，
    # 而本表数据行的这些样式本就没开 wrapText（只有表头行开了），加上尾部坏行常无 s=，
    # 于是长文本（如「玩具 / 毛绒玩具 / 毛绒公仔」、比价备注）溢出不换行。
    # 解决：写字符串单元格时，基于该列既有样式【克隆一个只多加 wrapText 的新 <xf>】，
    # 追加到 styles.xml 的 cellXfs 末尾（不动既有索引，故不影响其它单元格），用新索引。
    # 只影响本次新写的字符串格；数值/公式/嵌入图格不变。

    @staticmethod
    def _add_wrap_to_xf(xf: str) -> str:
        """克隆一个 cellXfs 里的 <xf>，仅追加 wrapText（自动换行），其余全保留。

        字体/颜色/边框/数字格式/水平垂直对齐原样不动；已含 wrapText 的原样返回。
        """
        if 'wrapText="1"' in xf:
            return xf
        if "<alignment" in xf:  # 已有对齐子元素 → 在其上补 wrapText
            return re.sub(r'(<alignment\b[^>]*?)\s*/>', r'\1 wrapText="1"/>', xf, count=1)
        # 无对齐子元素 → 补 applyAlignment + <alignment>（沿用本表 vertical=center 习惯）
        align = '<alignment vertical="center" wrapText="1"/>'
        sm = re.match(r"<xf\b([^>]*?)/>\s*$", xf)
        if sm:  # 自闭合 <xf .../>
            attrs = sm.group(1)
            if "applyAlignment" not in attrs:
                attrs += ' applyAlignment="1"'
            return f"<xf{attrs}>{align}</xf>"
        om = re.match(r"<xf\b([^>]*?)>(.*)</xf>\s*$", xf, re.S)
        if om:  # <xf ...>...</xf> 但无 alignment 子元素
            attrs, inner = om.group(1), om.group(2)
            if "applyAlignment" not in attrs:
                attrs += ' applyAlignment="1"'
            return f"<xf{attrs}>{align}{inner}</xf>"
        return xf  # 无法解析 → 保底不改

    def _init_wrap_ctx(self, styles_xml: str) -> dict:
        """解析 styles.xml 的 cellXfs，建派生 wrap 样式所需的上下文（不修改）。

        cellXfs 解析失败（缺 styles.xml / 结构异常）→ ok=False，后续退回原样式、不换行。
        """
        m = re.search(r"(<cellXfs[^>]*>)(.*?)(</cellXfs>)", styles_xml, re.S)
        if not m:
            return {"ok": False}
        xfs = re.findall(r"<xf\b[^>]*?/>|<xf\b[^>]*?>.*?</xf>", m.group(2), re.S)
        return {
            "ok": True,
            "full": m.group(0),
            "open_tag": m.group(1),
            "body": m.group(2),
            "close_tag": m.group(3),
            "xfs": xfs,
            "count": len(xfs),
            "new_xfs": [],
            "cache": {},  # 基样式索引 → wrap 变体索引（同基样式多列共用一个新 xf）
        }

    def _wrap_style_for(
        self, ctx: dict, col: str, cell_styles: Dict[str, str], rows: List[tuple]
    ) -> Optional[str]:
        """返回该文本列应使用的【带自动换行】样式索引；必要时新建 xf 记入 ctx。

        基样式优先取模板行该列样式，缺失退回扫历史行的文本单元格样式（_text_cell_style），
        再缺则用一个居中默认。基样式已开 wrapText 则直接复用，不新建。
        """
        if not ctx.get("ok"):
            return cell_styles.get(col)  # 无法改样式 → 退回原逻辑
        base_idx = cell_styles.get(col) or self._text_cell_style(rows, col)
        key = base_idx if base_idx is not None else "__none__"
        if key in ctx["cache"]:
            return ctx["cache"][key]
        xfs = ctx["xfs"]
        if base_idx is not None and base_idx.isdigit() and int(base_idx) < len(xfs):
            base_xf = xfs[int(base_idx)]
        else:
            base_xf = (
                '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" '
                'applyAlignment="1"><alignment vertical="center"/></xf>'
            )
        if 'wrapText="1"' in base_xf:  # 基样式已换行 → 直接复用
            ctx["cache"][key] = base_idx
            return base_idx
        new_idx = str(ctx["count"] + len(ctx["new_xfs"]))
        ctx["new_xfs"].append(self._add_wrap_to_xf(base_xf))
        ctx["cache"][key] = new_idx
        return new_idx

    @staticmethod
    def _rebuild_styles(ctx: dict, styles_xml: str) -> Optional[str]:
        """把 ctx 里新派生的 wrap 变体 xf 追加进 cellXfs，返回新 styles.xml；无新增返回 None。"""
        if not ctx.get("ok") or not ctx["new_xfs"]:
            return None
        new_count = ctx["count"] + len(ctx["new_xfs"])
        new_open = re.sub(r'count="\d+"', 'count="%d"' % new_count, ctx["open_tag"])
        new_full = new_open + ctx["body"] + "".join(ctx["new_xfs"]) + ctx["close_tag"]
        return styles_xml.replace(ctx["full"], new_full, 1)

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
