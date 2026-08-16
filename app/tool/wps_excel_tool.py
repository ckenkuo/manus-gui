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
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_output_dir
from app.logger import logger
from app.tool.base import BaseTool, ToolResult


# OOXML 命名空间与列名工具 -------------------------------------------------

NS_ETC = "http://www.wps.cn/officeDocument/2017/etCustomData"

# 单元格引用（列字母+行号），带可选 $ 绝对标记；(?<![A-Za-z0-9_$]) 避开函数名尾随数字。
# 模块级常量：WpsExcelTool 是 Pydantic 模型，类体内 `_x = ...` 会被当私有属性吞掉，
# 故编译好的正则放模块级，避免变成 ModelPrivateAttr。
_CELL_REF_RE = re.compile(r"(?<![A-Za-z0-9_$])(\$?)([A-Z]{1,3})(\$?)(\d+)")

# 逻辑字段 → 表头标题判定。采集管道要往【任意布局】的 Sheet 写，绝不能再假设固定列序：
# 实测同一工作簿里 storeA美国/storeA全球/storeB/StoreC 各 Sheet 的列序都不同（SPU 在
# C 还是 D、图片在 D 还是 E、采购价在 I 还是 J…全不一样），旧代码把字段硬编码成 storeA全球
# 的列 → 换 Sheet 就整体错位（SPU 写进"产品图片"列、采购价写进"重量"列）。改为按【表头标题】
# 把每个逻辑字段解析到该 Sheet 的真实列。
# 规则顺序即认领优先级；一列至多归一个字段（先到先得）。pick='last' 用于"备注"——有的
# Sheet 有多列备注（前置那列常被挪作它用），取末列最稳。判定前对标题 strip()。
_FIELD_RULES = [
    ("spu", lambda t: "spu" in t.lower(), "first"),
    # 货号列＝逐 SKU 标识（2026-08-11 起清单是一个 SKU 一行，同 SPU 多行靠它区分）。
    # 【必须排在 spu 之后】一列至多归一个字段、先到先得，"SPU货号"这类标题该归 spu。
    ("sku", lambda t: t == "货号" or t.endswith("货号") or t.upper() == "SKU", "first"),
    ("image", lambda t: ("产品图片" in t) or t == "图片", "first"),
    ("site", lambda t: t == "站点", "first"),
    ("category", lambda t: t in ("类目", "类别", "品类", "分类"), "first"),
    ("daily", lambda t: t == "日常价", "first"),
    ("discount", lambda t: t == "折扣", "first"),
    ("sale", lambda t: t in ("销售价格", "销售价", "售价"), "first"),
    ("purchase", lambda t: t in ("采购价格", "采购价", "购入价格"), "first"),
    ("weight", lambda t: t == "重量", "first"),
    ("ros", lambda t: t.lower() == "ros", "first"),
    ("note", lambda t: t == "备注", "last"),
]

# 商品采集对 Sheet 列分三类：
# 1. 平台清单有来源的字段由 _FIELD_RULES 动态映射后直接写（见 _ITEM_INPUT_FIELDS）；
# 2. 历史行是公式的列一律仿公式（公式是本表的计算逻辑，抄逻辑不是抄数据）；
# 3. 剩下的非公式列里，只有下列成本/计算语义的列才照抄历史固定值，其它保持空白。
#
# 【为什么第 3 类必须留一张标题表】曾想过全按数据证据判定（「该列历史值恒定就抄」），
# 实测行不通：同一张表里「尾程运费=25」和「加速器参考价格=84」在数据形态上完全一样
# （都是逐行填的数字、采样里都只出现在完整历史行），只有标题语义能区分哪个该抄、
# 哪个必须留空等人填。所以这张表是刻意保留的语义闸，不是偷懒的硬编码。
_COLLECT_MIMIC_TITLES = {
    "折扣", "空运头程", "尾程运费", "广告", "ros", "成本", "利润", "毛利", "操作费",
}
# 上表的常见写法变体：各 Sheet 表头是人手打的，「操作费」会写成「操作费用」、
# 「ros」会写成「ROS(%)」、「空运头程」会写成「空运头程费」。精确等值匹配下这些
# 列会被整体漏掉——公式依赖的输入变空白，成本/利润当场算错，而且很难看出来。
_MIMIC_TRIM_SUFFIXES = ("费用", "费", "用", "金额", "率")


def _norm_title(raw: str) -> str:
    """表头标题归一化：全角转半角、去括号注释、去空白与常见符号、转小写。

    「ROS(%)」「空运头程 费」「尾程运费（美元）」归一后分别是 ros / 空运头程费 /
    尾程运费，才能跟 _COLLECT_MIMIC_TITLES 比得上。
    """
    s = unicodedata.normalize("NFKC", str(raw or "")).strip().lower()
    s = re.sub(r"[（(\[【][^）)\]】]*[)）\]】]", "", s)  # 去掉括号里的单位/注释
    return re.sub(r"[\s%￥$、,，:：/\\-]+", "", s)


def _is_mimic_title(raw: str) -> bool:
    """该表头是否属于「允许照抄历史固定值」的成本/计算列。

    判法是「白名单项是标题前缀，且余下的只是无意义尾巴」：`操作费用` = 操作费 + 用，
    `毛利率` = 毛利 + 率。反过来按「标题去后缀」判会漏（操作费用去掉『费用』只剩
    『操作』，白名单里没有）。前缀判也天然挡住了误命中：`折扣参数` 余下是『参数』、
    `成本核算` 余下是『核算』，都不在尾巴表里；`加速器参考价格`/`叠加折扣1` 压根
    不以任何白名单项开头。
    """
    s = _norm_title(raw)
    if not s:
        return False
    if s in _COLLECT_MIMIC_TITLES:
        return True
    for title in _COLLECT_MIMIC_TITLES:
        if s.startswith(title) and s[len(title):] in _MIMIC_TRIM_SUFFIXES:
            return True
    return False


def _collect_mimic_columns(header: Dict[str, str]) -> set:
    """返回允许照抄历史固定值的列；匹配表头语义，不依赖固定列字母。"""
    return {col for col, raw_title in header.items() if _is_mimic_title(raw_title)}


# 逐商品写值的逻辑字段：这些列的值每行都来自平台清单/比价结果，历史行即便有公式
# 也【不能】仿写覆盖。实测教训：Leoaqr 表的「销售价格」列历史是 =J*K（参考价×折扣），
# 若当普通公式仿走，新行的申报价就会被一个依赖空白列的公式顶掉，显示成 0。
_ITEM_INPUT_FIELDS = (
    "spu", "sku", "image", "site", "category", "daily", "sale",
    "purchase", "weight", "note",
)


def item_input_columns(fields: Dict[str, str]) -> set:
    """{逻辑字段: 列} → 逐商品写值的列集合（见 _ITEM_INPUT_FIELDS）。"""
    return {fields[k] for k in _ITEM_INPUT_FIELDS if fields.get(k)}


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


def _xml_unescape(text: str) -> str:
    """XML 预定义实体与数字字符引用还原成真实字符。

    为什么必须做：登记表里手工录入的尺码常带软换行，sharedStrings 存的是
    `蓝色 / 120&#10;`。判重时拿它跟导出文件里的 `蓝色 / 120` 比永远不相等，
    同一行每次跑都会被当成新订单重写一遍。&amp; 必须最后还原，否则
    `&amp;lt;` 会被二次解成 `<`。
    """
    if "&" not in text:
        return text
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
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
                "description": "工作表名称，如 'storeB童装货盘记录'",
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
        """解析 sheetData → [(行号字符串, 行内容)]；自闭合空行 <row .../> 的内容为空串。

        为什么不能只写 `<row r="(\\d+)"[^>]*>(.*?)</row>`：WPS 表尾常有整片只带行高的
        预留空行 `<row r="283" ht="41" customHeight="1"/>`，`[^>]*` 会连自闭合的 `/` 一起
        吃掉、把它后面那个 `>` 当成开标签结束，于是这一行的 body 一路吞到【下一个带内容
        行】的 `</row>`——既漏掉中间所有空行，又把别人的单元格算进它头上。订单登记表
        实测 3080 个 <row> 只解析出 3062 个，末行定位与判重随之失真。
        """
        out: List[tuple] = []
        for m in re.finditer(
            r'<row\s+r="(\d+)"[^>]*?(?:/>|>(.*?)</row>)', sheet_xml, re.S
        ):
            out.append((m.group(1), m.group(2) or ""))
        return out

    @staticmethod
    def _row_positions(sheet_xml: str) -> Dict[int, tuple]:
        """一次扫描建 {行号: (起始偏移, 结束偏移, 开标签)}，供批量追加定位替换/插入点。

        开标签统一归一成带 `>` 的形式：自闭合空行 `<row r="283" ht="41"/>` 归一为
        `<row r="283" ht="41">`，这样复用它就能保留预留空行的行高（ht="41" 正是给
        嵌入图留的高度），而不必从模板行猜。
        """
        pos: Dict[int, tuple] = {}
        for m in re.finditer(
            r'<row\s+r="(\d+)"[^>]*?(?:/>|>.*?</row>)', sheet_xml, re.S
        ):
            text = m.group(0)
            if text.endswith("/>"):
                open_tag = text[:-2].rstrip() + ">"
            else:
                open_tag = re.match(r"<row\b[^>]*>", text).group(0)
            pos[int(m.group(1))] = (m.start(), m.end(), open_tag)
        return pos

    @staticmethod
    def _shift_ref_rows(ref: str, at: int, n: int) -> str:
        """把区域字符串里 >= at 的行号统一 +n，用于插行后修正 mergeCell / 条件格式 / 筛选区。

        规则等同 Excel「在 at 行插入 n 行」：行号 < at 的端点不动（表头那一端要留在第 1 行），
        >= at 的端点后移。上限死死卡在 1048576——条件格式的 `C200:C1048576` 这种到底的区域
        再加就越界，整个 sqref 会被 WPS 判为非法而丢掉整条规则。
        `sqref` 可能是空格分隔的多段，`definedName` 里还带 `$`，都要吃得下。
        """
        def one(mo: "re.Match") -> str:
            row = int(mo.group(2))
            return mo.group(1) + str(min(row + n, 1048576) if row >= at else row)

        return " ".join(
            re.sub(r"(\$?[A-Z]+\$?)(\d+)", one, seg)
            for seg in ref.split()
        )

    @classmethod
    def _shift_data_region(cls, region: str, at: int, n: int) -> str:
        """把一段 sheetData 切片里所有行号 +n（行本身的 r、单元格的 r、共享公式的 ref）。

        只对「表头下一行起的连续整段」用，所以段内行号必然 >= at，无需再筛。
        为什么不顺手平移公式文本里的引用：本表 362 个公式全是 DISPIMG（参数是图片 ID，
        与行号无关）。真出现带单元格引用的公式就直接抛错——移了行不改公式等于静默算错，
        比中止危险得多。
        """
        # 负向预查排掉自闭合的 `<f t="shared" si="0"/>`（共享公式的从属格）：把它当开标签
        # 会一路吃到下一个 </f>，把中间那些 <v>=DISPIMG(...) 缓存值误当成公式内容
        for mo in re.finditer(r"<f(?![^>]*/>)[^>]*>(.*?)</f>", region, re.S):
            # 先摘掉整个 DISPIMG(...) 调用：它的参数是 ID_8C37B8B8... 这种十六进制串，
            # 只摘函数名会把 ID 里的「字母+数字」误判成单元格引用
            expr = re.sub(r"DISPIMG\([^)]*\)", "", mo.group(1))
            if re.search(r"\$?[A-Z]{1,3}\$?\d+", expr):
                raise ValueError(
                    f"插行会让公式引用错位（公式片段：{mo.group(1)[:60]}），已中止，"
                    "请改用追加模式或先人工处理该公式"
                )
        region = re.sub(
            r'(<row\s+r=")(\d+)', lambda m: m.group(1) + str(int(m.group(2)) + n), region
        )
        region = re.sub(
            r'(<c\s+r="[A-Z]+)(\d+)', lambda m: m.group(1) + str(int(m.group(2)) + n), region
        )
        return re.sub(
            r'(\sref=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_rows(m.group(2), at, n) + m.group(3),
            region,
        )

    @classmethod
    def _shift_tail_refs(cls, tail: str, at: int, n: int) -> str:
        """平移 `</sheetData>` 之后的引用：mergeCell / autoFilter / 条件格式 sqref，
        外加 cfRule 公式里的【绝对】行号。

        cfRule 公式里两种引用混着用：绝对的（`$C$1:$C$10`，指向 sqref 圈定的那几段，
        必须跟着动）和相对的（`C1`，代表「当前被求值的单元格」，锚在 sqref 左上角、
        行号是 1 不能动）。只动带 `$` 的那种正是 Excel 插行的语义——只改 sqref 不改公式，
        登记表那条「订单号重复就标红」的规则会整段错位。
        """
        def abs_row(mo: "re.Match") -> str:
            row = int(mo.group(2))
            return mo.group(1) + (str(min(row + n, 1048576)) if row >= at else mo.group(2))

        tail = re.sub(
            r'(\s(?:ref|sqref)=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_rows(m.group(2), at, n) + m.group(3),
            tail,
        )
        return re.sub(
            r"(<formula>)(.*?)(</formula>)",
            lambda m: m.group(1)
            + re.sub(r"(\$[A-Z]+\$)(\d+)", abs_row, m.group(2))
            + m.group(3),
            tail,
            flags=re.S,
        )

    @classmethod
    def _shift_for_insert(
        cls, sheet_xml: str, row_pos: Dict[int, tuple], header_row: int, n: int
    ) -> str:
        """为「表头下插 n 行」腾位：表头以下所有行号 +n，表尾区域引用同步平移。

        语义对齐 Excel 的「插入行」：表头下方的一切（数据行、预留空行、底部纯格式空行）
        整体下移。被挤出 1048576 上限的行直接丢弃——但只准丢纯格式空行，一旦有带值的行
        会被挤掉就抛错中止：悄悄吃掉数据比写失败危险得多。
        （登记表底部那 2781 个纯格式空行在 1045692 起，按每批百来行算要几十批才够挤，
        真挤到了丢的也只是空行的行高样式。）
        """
        at = header_row + 1
        movable = sorted(r for r in row_pos if r >= at)
        if not movable:
            return sheet_xml
        over = [r for r in movable if r + n > 1048576]
        for r in over:
            s, e, _ = row_pos[r]
            if "<v>" in sheet_xml[s:e]:
                raise ValueError(
                    f"下移 {n} 行会把第 {r} 行挤出 1048576 上限、而该行有数据，已中止"
                )
        if over:
            logger.warning(f"插行挤掉 {len(over)} 个越界的纯格式空行（第 {over[0]} 行起）")
        start = row_pos[movable[0]][0]
        stop = row_pos[over[0]][0] if over else row_pos[movable[-1]][1]
        end = row_pos[movable[-1]][1]
        cut = sheet_xml.index("</sheetData>", end) + len("</sheetData>")
        # dimension 的下界跟着最大行号走（上限内），免得它比实际行范围还小
        head = re.sub(
            r'(<dimension ref="[A-Z]+\d+:[A-Z]+)(\d+)(")',
            lambda m: m.group(1) + str(min(int(m.group(2)) + n, 1048576)) + m.group(3),
            sheet_xml[:start],
            count=1,
        )
        return (
            head
            + cls._shift_data_region(sheet_xml[start:stop], at, n)
            + sheet_xml[end:cut]
            + cls._shift_tail_refs(sheet_xml[cut:], at, n)
        )

    @staticmethod
    def _shift_ref_cols(ref: str, at: int, n: int = 1) -> str:
        """把区域字符串里列号 >= at 的列字母整体 +n，用于插列后修正各类区域引用。

        与 _shift_ref_rows 对称（那个动行号、这个动列字母），语义同 Excel「在第 at 列
        插入 n 列」：at 左侧的端点不动，at 及右侧后移。上限卡在 16384（XFD）——超了整个
        ref 会被 WPS 判非法而丢掉整条规则（条件格式的 `C1:C1048576` 那种到底区域同理）。
        `$` 绝对标记原样保留，空格分隔的多段 sqref 逐段处理。
        """
        def one(mo: "re.Match") -> str:
            idx = _col_to_idx(mo.group(2))
            col = _idx_to_col(min(idx + n, 16384)) if idx >= at else mo.group(2)
            return mo.group(1) + col + mo.group(3) + mo.group(4)

        return " ".join(
            _CELL_REF_RE.sub(one, seg) for seg in ref.split()
        )

    @classmethod
    def _shift_data_cols(cls, region: str, at: int, n: int = 1) -> str:
        """把整段 sheetData 里 >= at 的列右移 n：单元格 r、行 spans、共享公式 ref。

        公式文本【不平移】，取而代之的是先校验：本表 5 张登记 Sheet 实测 6111 个公式全是
        `_xlfn.DISPIMG`（参数是图片 ID，与列号无关），一旦出现带单元格引用的其他公式就抛错
        中止——移了列不改公式等于静默算错，比写失败危险得多（同 _shift_data_region 的取向）。
        """
        for mo in re.finditer(r"<f(?![^>]*/>)[^>]*>(.*?)</f>", region, re.S):
            expr = re.sub(r"DISPIMG\([^)]*\)", "", mo.group(1))
            if re.search(r"\$?[A-Z]{1,3}\$?\d+", expr):
                raise ValueError(
                    f"插列会让公式引用错位（公式片段：{mo.group(1)[:60]}），已中止"
                )

        def cell(mo: "re.Match") -> str:
            idx = _col_to_idx(mo.group(2))
            col = _idx_to_col(min(idx + n, 16384)) if idx >= at else mo.group(2)
            return mo.group(1) + col + mo.group(3)

        region = re.sub(r'(<c r=")([A-Z]{1,3})(\d+")', cell, region)
        region = re.sub(
            r'(\sspans=")(\d+):(\d+)(")',
            lambda m: "%s%d:%d%s" % (
                m.group(1),
                int(m.group(2)) + n if int(m.group(2)) >= at else int(m.group(2)),
                int(m.group(3)) + n if int(m.group(3)) >= at else int(m.group(3)),
                m.group(4),
            ),
            region,
        )
        return re.sub(
            r'(\sref=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_cols(m.group(2), at, n) + m.group(3),
            region,
        )

    @classmethod
    def _shift_head_cols(cls, head: str, at: int, style: str, width: float) -> str:
        """平移 `<sheetData>` 之前的列引用：dimension / sheetView 视口与选区 / cols 定义。

        `<cols>` 的处理是这里唯一不平凡的部分。Excel 插列的语义是「新列继承左邻列的格式」，
        所以：
          - min/max 都 >= at 的整段右移；
          - 跨过 at 的段（min < at <= max）只把 max +n，等于把新列并进这一段、自然继承它的
            样式与列宽；
          - 没有任何段覆盖 at 时（新列左邻自成一段），补一条显式 <col> 给新列，样式取左邻
            那段的 style，列宽用调用方给的 width（数量列不需要跟「尺码」一样宽）。
        不这么做的后果是新列拿 defaultColWidth 且无边框，跟左右两列格式明显不一致。
        """
        head = re.sub(
            r'(<dimension ref=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_cols(m.group(2), at) + m.group(3),
            head,
            count=1,
        )
        head = re.sub(
            r'(\s(?:topLeftCell|activeCell|sqref)=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_cols(m.group(2), at) + m.group(3),
            head,
        )
        covered = False
        for mo in re.finditer(r'<col min="(\d+)" max="(\d+)"', head):
            if int(mo.group(1)) < at <= int(mo.group(2)):
                covered = True

        def one_col(mo: "re.Match") -> str:
            lo, hi = int(mo.group(2)), int(mo.group(3))
            if lo >= at:
                lo += 1
            if hi >= at:
                hi += 1
            return f'{mo.group(1)}{lo}" max="{hi}"'

        head = re.sub(r'(<col min=")(\d+)" max="(\d+)"', one_col, head)
        if not covered:
            sa = f' style="{style}"' if style else ""
            new = f'<col min="{at}" max="{at}" width="{width}"{sa} customWidth="1"/>'
            # 插到最后一个 max < at 的 <col> 之后：<col> 必须按列号升序排，而左邻列可能是
            # 某个多列段的末列（`min="1" max="4"`），按字面找 `min="4" max="4"` 会落空。
            end = None
            for mo in re.finditer(r'<col min="(\d+)" max="(\d+)"[^>]*/>', head):
                if int(mo.group(2)) < at:
                    end = mo.end()
            if end is not None:
                head = head[:end] + new + head[end:]
            elif "<cols>" in head:
                head = head.replace("<cols>", "<cols>" + new, 1)
            else:
                logger.warning("本表没有 <cols> 定义，新列用默认列宽（不影响数据）")
        return head

    @classmethod
    def _shift_tail_cols(cls, tail: str, at: int) -> str:
        """平移 `</sheetData>` 之后的列引用：mergeCell / autoFilter / 条件格式 sqref，
        外加 cfRule 公式里的【绝对】列引用。

        只动带 `$` 的公式引用，同 _shift_tail_refs 的理由：cfRule 里相对引用（`C1`）代表
        「当前被求值的单元格」、锚在 sqref 左上角，跟着 sqref 走就够了；绝对引用（`$C$1`）
        指向固定区域，必须自己平移。

        autoFilter 的 `<filterColumn colId="k">` 是【相对 ref 起点】的 0-based 序号：插列
        落在筛选区内会让它指错列。本表实测无 filterColumn（筛选条件没存盘），真出现就抛错
        中止而不是猜——把筛选条件挪错列，用户看到的是「表里数据凭空少了一半」。
        """
        af = re.search(r'<autoFilter ref="([^"]+)"', tail)
        if af and re.search(r"<filterColumn\b", tail):
            lo = _col_to_idx(re.match(r"\$?([A-Z]+)", af.group(1)).group(1))
            if at > lo:
                raise ValueError("本表 autoFilter 存了筛选条件（filterColumn），插列会让它指错列，已中止")

        def abs_col(mo: "re.Match") -> str:
            idx = _col_to_idx(mo.group(2))
            return mo.group(1) + (
                _idx_to_col(min(idx + 1, 16384)) if idx >= at else mo.group(2)
            ) + mo.group(3)

        tail = re.sub(
            r'(\s(?:ref|sqref)=")([^"]+)(")',
            lambda m: m.group(1) + cls._shift_ref_cols(m.group(2), at) + m.group(3),
            tail,
        )
        return re.sub(
            r"(<formula>)(.*?)(</formula>)",
            lambda m: m.group(1)
            + re.sub(r"(\$)([A-Z]{1,3})(\$\d+)", abs_col, m.group(2))
            + m.group(3),
            tail,
            flags=re.S,
        )

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
        """取单元格显示文本：t="s" 查 sharedStrings，否则取 <v> 或 <f>。

        统一做 XML 实体反转义（见 _xml_unescape）：sharedStrings / <v> 里存的是转义后的
        文本（尺码里的软换行是 `&#10;`），不还原就没法跟外部数据做等值判重。
        """
        t = re.search(r'\st="([^"]+)"', cell_xml)
        v = re.search(r"<v>(.*?)</v>", cell_xml, re.S)
        f = re.search(r"<f[^>]*>(.*?)</f>", cell_xml, re.S)
        if t and t.group(1) == "s" and v:
            try:
                return _xml_unescape(shared[int(v.group(1))])
            except (ValueError, IndexError):
                return ""
        if f:
            return "=" + _xml_unescape(f.group(1))
        if v:
            return _xml_unescape(v.group(1))
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
    def _find_last_data_row(
        cls, rows: List[tuple], key_cols: List[str], header_row: int = 1
    ) -> int:
        """最后一个真实数据行：key_cols 中任一列有 <v> 值的最大行号。

        header_row 既是【下界兜底】也是【跳过线】：表里一条数据都没有时返回 header_row，
        调用方 +1 才不会把新行写到表头上；订单登记表里 `牛仔裤`/`童装` 的表头在第 2 行，
        用默认 1 会把表头行本身当成数据行。默认 1 保持既有调用行为不变。
        """
        last = header_row
        for rid, body in rows:
            if int(rid) <= header_row:
                continue
            for col in key_cols:
                if re.search(r'<c r="%s%s"[^>]*>(?:<f[^>]*>.*?</f>)?<v>' % (col, rid), body, re.S):
                    last = max(last, int(rid))
                    break
        return last

    @classmethod
    def first_data_value(
        cls, file_path: str, sheet_name: str, col: str, header_row: int = 1
    ) -> str:
        """读某列【表头正下方第一个非空值】。读不到返回空串，绝不抛错。

        为什么取「第一个」而不是整列集合：订单登记表的写入是 insert_at_top（新行插到表头
        正下方，见 append_rows），所以数据区最顶端那一行就是上次登记的最新一条。它单条就是
        增量水位的完整语义——页面新→旧翻页时遇到它即「追上」。

        不严格要求就是 header_row+1 那一行：表里可能有零星空行（人工插的分隔行），
        从表头往下扫到首个非空为止。扫描上限 200 行，避免整表几万行全扫。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return ""
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                row_map = dict(cls._parse_rows(sheet_xml))
                for rid in range(header_row + 1, header_row + 201):
                    body = row_map.get(str(rid))
                    if not body:
                        continue
                    m = re.search(
                        r'<c r="%s%d"[^>]*>.*?</c>' % (col, rid), body, re.S
                    )
                    if m:
                        txt = cls._cell_text(m.group(0), shared).strip()
                        if txt:
                            return txt
                return ""
        except Exception:
            return ""

    @classmethod
    def existing_key_values(
        cls, file_path: str, sheet_name: str, col: str = "D", header_row: int = 1
    ) -> set:
        """返回某列（默认 D=SPU）在数据行(row>header_row)里所有非空值的集合（字符串）。

        供批量采集在开跑前判断某 SPU 是否已入库、可跳过（幂等 / 断点续跑）。
        文件或工作表缺失、解析异常时返回空集，绝不抛错。
        """
        return {
            t[0]
            for t in cls.existing_key_tuples(file_path, sheet_name, [col], header_row)
            if t[0]
        }

    @classmethod
    def existing_key_tuples(
        cls,
        file_path: str,
        sheet_name: str,
        cols: List[str],
        header_row: int = 1,
    ) -> set:
        """返回数据行(row>header_row)里 cols 各列文本组成的元组集合，用于【组合键】判重。

        为什么要组合键：订单登记表一个订单可含多个子订单（同一订单号、不同尺码各占一行），
        只按订单号判重会把后面的子订单全当重复丢掉；而表里又没有子订单号列，只能用
        (订单号, 尺码) 这种多列组合作等价键。整行各列都为空的行不计入。
        文件/表缺失或解析异常返回空集，绝不抛错（判重失效只会多写，不会写坏表）。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return set()
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                out = set()
                for rid, body in cls._parse_rows(sheet_xml):
                    if int(rid) <= header_row:
                        continue
                    vals = []
                    for col in cols:
                        m = re.search(
                            r'<c r="%s%s"[^>]*>.*?</c>' % (col, rid), body, re.S
                        )
                        vals.append(
                            cls._cell_text(m.group(0), shared).strip() if m else ""
                        )
                    if any(vals):
                        out.add(tuple(vals))
                return out
        except Exception:
            return set()

    @classmethod
    def read_header(
        cls, file_path: str, sheet_name: str, header_row: int = 1
    ) -> Dict[str, str]:
        """读某 Sheet 表头行：{列字母: 标题}。文件/表缺失或损坏返回 {}，绝不抛错。

        header_row 默认 1；订单登记表里 `牛仔裤`/`童装` 第 1 行是跨列大标题、第 2 行才是
        真表头，这类表要显式传 2（定位逻辑见 detect_header_row）。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return {}
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                row_map = dict(cls._parse_rows(sheet_xml))
                header: Dict[str, str] = {}
                body = row_map.get(str(header_row))
                if body:
                    for cm in re.finditer(
                        r'<c r="([A-Z]+)%d"[^>]*>.*?</c>' % header_row, body, re.S
                    ):
                        txt = cls._cell_text(cm.group(0), shared)
                        if txt:
                            header[cm.group(1)] = txt
                return header
        except Exception:
            return {}

    @classmethod
    def detect_header_row(
        cls, file_path: str, sheet_name: str, max_scan: int = 3
    ) -> int:
        """探测表头在第几行：前 max_scan 行里【非空单元格最多】的那一行，并列取最靠前。

        为什么不写死第 1 行：订单登记表 13 个 Sheet 里 `牛仔裤`/`童装` 第 1 行是只占一两格的
        跨列大标题，真表头在第 2 行。按「填得最满的那行是表头」判定，对两种布局都成立，
        比维护一张「哪个 Sheet 表头在第几行」的表更耐改名。探测失败返回 1。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return 1
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                row_map = dict(cls._parse_rows(sheet_xml))
                best_row, best_n = 1, -1
                for r in range(1, max_scan + 1):
                    body = row_map.get(str(r), "")
                    n = 0
                    for cm in re.finditer(
                        r'<c r="[A-Z]+%d"[^>]*>.*?</c>' % r, body, re.S
                    ):
                        if cls._cell_text(cm.group(0), shared).strip():
                            n += 1
                    if n > best_n:
                        best_row, best_n = r, n
                return best_row
        except Exception:
            return 1

    @classmethod
    def read_row_by_key(
        cls, file_path: str, sheet_name: str, key: str,
        cols: Dict[str, str], key_col: str = "D",
    ) -> Dict[str, str]:
        """按 key（默认 SPU，在 key_col 列）定位数据行(row>1)，读取该行 cols 指定的各列值。

        供活动管理管线在报活动前，按 SPU 回读已入库行的成本/日常价/售价等做红线校验与定价，
        避免二次解析整表。cols: {逻辑名: 列字母}，如 {"purchase":"J","daily":"G","sale":"I"}。
        返回 {逻辑名: 单元格文本}（只含成功读到且非空的项）；找不到 key 所在行返回 {}。
        公式单元格返回其缓存值文本（沿用 _cell_text 行为，与 existing_key_values 判重口径一致）。
        文件/表缺失或异常返回 {}，绝不抛错。
        """
        try:
            with zipfile.ZipFile(file_path) as zf:
                part = cls._resolve_sheet_part(zf, sheet_name)
                if not part:
                    return {}
                sheet_xml = zf.read(part).decode("utf-8")
                shared = cls._shared_strings(zf)
                target = str(key).strip()
                for rid, body in cls._parse_rows(sheet_xml):
                    if rid == "1":  # 跳过表头
                        continue
                    km = re.search(r'<c r="%s%s"[^>]*>.*?</c>' % (key_col, rid), body, re.S)
                    if not km:  # key_col 空/自闭合单元格，非目标行
                        continue
                    if cls._cell_text(km.group(0), shared).strip() != target:
                        continue
                    # 命中行：逐个逻辑列读文本，非空才收（自闭合空单元格匹配不到，自然跳过）
                    out: Dict[str, str] = {}
                    for name, col in cols.items():
                        cm = re.search(r'<c r="%s%s"[^>]*>.*?</c>' % (col, rid), body, re.S)
                        if not cm:
                            continue
                        txt = cls._cell_text(cm.group(0), shared).strip()
                        if txt:
                            out[name] = txt
                    return out
                return {}
        except Exception:
            return {}

    @classmethod
    def collect_mimic_columns(cls, header: Dict[str, str]) -> set:
        """商品采集允许照抄历史固定值的列，按真实表头动态识别。"""
        return _collect_mimic_columns(header)

    @classmethod
    def item_input_columns(cls, fields: Dict[str, str]) -> set:
        """逐商品写值的列（本地/云端两条写入路径共用，见 _ITEM_INPUT_FIELDS）。"""
        return item_input_columns(fields)

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
        它们。旧代码只硬编码 ros=7，换到 storeA美国(ros=6、另有操作费=5/尾程=8)就让这些格留空、
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

            def _row_cells(rid: int) -> List[str]:
                """该行有哪些列（列字母列表）。

                【返回的是列字母、不是 (列, 内容) 二元组】正则里只有一个捕获组，
                findall 给的就是字符串列表——按二元组解包会 ValueError，实测让整个
                inspect 在真实工作簿上直接崩（本地采集路径因此完全不可用）。
                """
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

        image_col = field_cols.get("image")
        mimic_cols = _collect_mimic_columns(header)
        # 逐商品输入列（采集逐条填的，不是常量）：这些列不当常量、不仿公式、也不该被常量覆盖。
        item_cols = item_input_columns(field_cols)
        # 公式列＝历史行里本来就是公式的列，【不再受成本列白名单限制】：公式是本表自己的
        # 计算逻辑，仿写它等于把这张表的算法延续到新行；用一张标题表去卡，换个 Sheet
        # 把「毛利率」写成「毛利率(%)」、多一列「含税成本」，新行这些列就整列空白，
        # 而空白列往往又是别的公式的输入，错会顺着公式链扩散。图片列与逐商品输入列除外。
        formula_cols = {
            c for c, v in sample.items()
            if isinstance(v, str) and v.startswith("=")
            and c != image_col and c not in item_cols
        }
        # 照抄历史固定值的列：只在成本语义白名单内，且本身不是公式列/逐商品输入列。
        # 这道闸不能放开——见 _COLLECT_MIMIC_TITLES 的注释（数据形态区分不了
        # 「尾程运费」和「加速器参考价格」，只有标题能）。
        template_cols = mimic_cols - formula_cols - item_cols

        def _template_row_score(rid: int) -> tuple:
            formula_count = 0
            input_count = 0
            populated = 0
            for col in _row_cells(rid):
                cx = _cell_xml(rid, col)
                formula = self._resolve_cell_formula(
                    cx, rid, _col_to_idx(col), masters
                )
                text = self._cell_text(cx, shared).strip()
                if formula is not None and col in formula_cols:
                    formula_count += 1
                if col in template_cols and text and formula is None:
                    input_count += 1
                if text or formula is not None:
                    populated += 1
            return formula_count, input_count, populated

        candidate_rows = [
            int(rid) for rid, _body in rows
            if 1 < int(rid) <= last_data
        ]
        template_rid = max(candidate_rows, key=_template_row_score, default=last_data)
        # 公式与输入必须来自同一健康模板行。否则会出现「参数抄了老行、公式却沿用坏尾行」的
        # 混搭，下一批仍可能继续传播错误。平移到 last_data 只是为了保持 inspect 输出契约，
        # resolve_sheet_schema 随后会把相对行号统一模板化成 {r}。
        for col in formula_cols:
            cx = _cell_xml(template_rid, col)
            formula = self._resolve_cell_formula(
                cx, template_rid, _col_to_idx(col), masters
            )
            if formula is not None:
                sample[col] = "=" + self._shift_formula(
                    formula, last_data - template_rid, 0
                )
        constant_columns = {}
        for col in sorted(template_cols, key=_col_to_idx):
            cx = _cell_xml(template_rid, col)
            if not cx or self._resolve_cell_formula(
                cx, template_rid, _col_to_idx(col), masters
            ) is not None:
                continue
            text = self._cell_text(cx, shared).strip()
            if not text:
                continue
            try:
                number = float(text)
                constant_columns[col] = int(number) if number == int(number) else number
            except ValueError:
                constant_columns[col] = text

        out = {
            "sheet": sheet_name,
            "part": part,
            "header_列标题": header,
            "字段列映射": field_cols,
            "模板输入列": constant_columns,
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
                style = cell_styles.get(col) or self._value_cell_style(rows, col)
                return f' s="{style}"' if style else ""

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

    # ---- 批量追加（订单登记管线用）--------------------------------------
    # 为什么不直接循环调 append_product_row：那条路每写一行都要 shutil.copyfile 整份备份
    # 再解压重压整个 zip。订单登记表 102MB、单批 200+ 行，逐行走要跑几个小时，还会甩出
    # 200 多份备份把输出目录塞爆。批量版把「备份 / 读 zip / 改 XML / 写 zip」各做一次，
    # N 行 N 图一次落盘；不改 append_product_row 的既有行为（采集管线仍走单行路径）。

    def append_rows(
        self,
        file_path: str,
        sheet_name: str,
        rows_data: List[Dict[str, Any]],
        header_row: int = 1,
        key_cols: Optional[List[str]] = None,
        insert_at_top: bool = False,
    ) -> Dict[str, Any]:
        """在最后一个数据行之后批量追加若干行，每行可带一张 DISPIMG 嵌入图。

        insert_at_top=True 改为**插到表头正下方**，表头以下所有行整体下移 len(rows_data) 行：
        订单登记表要「时间越新的越在上面」，追加到表尾正好相反。mergeCell / 条件格式 /
        autoFilter 以及 workbook.xml 里指向本表的 definedName 都随之平移，
        细节见 _shift_for_insert / _shift_ref_rows。

        rows_data 每项：
            {"values": {列字母: 值}, "formulas": {列字母: 公式模板(用 {r} 占位行号)},
             "image_column": "G", "image_path": r"...\\a.jpg"}
        values 里的空值（None / 空串）【不写单元格】——待发货订单的运单号等本就为空，
        写个空 <v> 只会把预留空行的原格式冲掉。

        写入是主流程，失败直接抛异常交上层重试兜底（不吞）；写前照例做一次时间戳备份。
        返回 {"written": N, "first_row": r1, "last_row": rN, "images": M, "backup": 路径}。
        """
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"文件不存在：{file_path}")
        if not rows_data:
            return {"written": 0, "first_row": None, "last_row": None,
                    "images": 0, "backup": None}

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = get_output_dir("backup") / f"{src.stem}_备份{ts}{src.suffix}"
        shutil.copyfile(src, backup)

        with zipfile.ZipFile(src) as zf:
            part = self._resolve_sheet_part(zf, sheet_name)
            if not part:
                raise ValueError(f"找不到工作表 '{sheet_name}'")
            sheet_xml = zf.read(part).decode("utf-8")
            rows = self._parse_rows(sheet_xml)
            row_pos = self._row_positions(sheet_xml)

            try:
                styles_xml = zf.read("xl/styles.xml").decode("utf-8")
            except KeyError:
                styles_xml = ""
            wrap_ctx = self._init_wrap_ctx(styles_xml)

            # 末行定位：默认按本批要写的所有列判定，避免只看某一列时被历史稀疏行带偏
            if key_cols is None:
                kc: set = set()
                for item in rows_data:
                    kc |= set(item.get("values") or {})
                    kc |= set(item.get("formulas") or {})
                key_cols = sorted(kc, key=_col_to_idx) or ["A"]
            last_data = self._find_last_data_row(rows, list(key_cols), header_row)
            tpl_body = dict(rows).get(str(last_data), "")
            cell_styles = dict(re.findall(r'<c r="([A-Z]+)\d+"\s+s="(\d+)"', tpl_body))

            # 预解析每列基样式。_wrap_style_for / _formula_cell_style 在 cell_styles 缺列时
            # 会扫全表找样式，逐行调用等于 N×列数 次全表扫描（3000 行表写 200 行实测要几分钟）。
            # 这里按列各扫一次填满，后续逐行取值全是字典命中。
            base_styles = dict(cell_styles)
            want_cols: set = set()
            for item in rows_data:
                want_cols |= set(item.get("values") or {})
            for col in want_cols - set(base_styles):
                st = self._text_cell_style(rows, col)
                if st:
                    base_styles[col] = st
            # 全表都找不到该列样式 → 取模板行左邻列的（Excel 插列本就是「继承左邻格式」）。
            # 撞上的场景：刚插出来的「数量」列，整表一个该列单元格都没有，学不到样式，
            # 写出来的格没有边框、跟左右邻明显不一致。
            for col in sorted(want_cols - set(base_styles), key=_col_to_idx):
                st = self._left_neighbor_style(cell_styles, col)
                if st:
                    base_styles[col] = st
            f_styles: Dict[str, str] = {}
            for item in rows_data:
                for col in (item.get("formulas") or {}):
                    if col not in f_styles:
                        f_styles[col] = cell_styles.get(col) or (
                            self._formula_cell_style(rows, col) or ""
                        )
            img_styles: Dict[str, str] = {}
            for item in rows_data:
                col = item.get("image_column")
                if col and col not in img_styles:
                    img_styles[col] = self._img_cell_style(rows, col) or cell_styles.get(col, "")

            changed: Dict[str, bytes] = {}
            new_media: List[tuple] = []

            # 图片：整批一次性追加 media + cellimages + rels，编号连续递增
            disp_ids: Dict[int, str] = {}
            if any(it.get("image_path") for it in rows_data):
                cellimages = zf.read("xl/cellimages.xml").decode("utf-8")
                ci_rels = zf.read("xl/_rels/cellimages.xml.rels").decode("utf-8")
                # 编号基数要同时看 rels 引用和 zip 里【实际存在】的 media 文件：普通浮动图
                # （drawing）也占用 image{N} 名字却不在 cellimages.rels 里，只看 rels 会撞名
                # 覆盖掉别人的图。单行版一次只加一张撞上的概率低，批量一次 200 张必须堵死。
                nums = [int(n) for n in re.findall(r"media/image(\d+)\.", ci_rels)]
                nums += [
                    int(m.group(1))
                    for m in (re.match(r"xl/media/image(\d+)\.", nm) for nm in zf.namelist())
                    if m
                ]
                img_n = max(nums) if nums else 0
                rid_nums = [int(n) for n in re.findall(r'Id="rId(\d+)"', ci_rels)]
                rel_n = max(rid_nums) if rid_nums else 0
                pic_ids = [int(n) for n in re.findall(r'<xdr:cNvPr id="(\d+)"', cellimages)]
                pic_n = max(pic_ids) if pic_ids else 1

                add_ci: List[str] = []
                add_rel: List[str] = []
                for idx, item in enumerate(rows_data):
                    ipath = item.get("image_path")
                    if not ipath:
                        continue
                    if not item.get("image_column"):
                        raise ValueError(f"第 {idx} 项给了 image_path 却没有 image_column")
                    img = Path(ipath)
                    if not img.exists():
                        raise FileNotFoundError(f"图片不存在：{ipath}")
                    ext = img.suffix.lower().lstrip(".")
                    if ext == "jpg":
                        ext = "jpeg"
                    img_n += 1
                    rel_n += 1
                    pic_n += 1
                    rel_id = f"rId{rel_n}"
                    disp_id = f"ID_{ts}{img_n:08d}".ljust(36, "0")[:36]
                    disp_ids[idx] = disp_id
                    add_ci.append(
                        '<etc:cellImage><xdr:pic><xdr:nvPicPr>'
                        f'<xdr:cNvPr id="{pic_n}" name="{disp_id}" descr="collected_image"/>'
                        '<xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
                        f'<a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
                        '</xdr:blipFill><xdr:spPr><a:xfrm><a:off x="0" y="0"/>'
                        '<a:ext cx="514350" cy="514350"/></a:xfrm>'
                        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr>'
                        "</xdr:pic></etc:cellImage>"
                    )
                    add_rel.append(
                        f'<Relationship Id="{rel_id}" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                        f'Target="media/image{img_n}.{ext}"/>'
                    )
                    new_media.append((f"xl/media/image{img_n}.{ext}", img.read_bytes()))

                changed["xl/cellimages.xml"] = cellimages.replace(
                    "</etc:cellImages>", "".join(add_ci) + "</etc:cellImages>"
                ).encode("utf-8")
                changed["xl/_rels/cellimages.xml.rels"] = ci_rels.replace(
                    "</Relationships>", "".join(add_rel) + "</Relationships>"
                ).encode("utf-8")

            # 逐行构造 XML
            tpl_open = row_pos.get(last_data, (0, 0, f'<row r="{last_data}">'))[2]
            n_new = len(rows_data)
            first_new = header_row + 1 if insert_at_top else last_data + 1
            patches: List[tuple] = []  # (起, 止, 文本)；插入表示为 (p, p, 文本)
            if insert_at_top:
                sheet_xml = self._shift_for_insert(sheet_xml, row_pos, header_row, n_new)
                # 下移后 [first_new, first_new+n) 是空档，不会遇到「复用预留空行」的情形，
                # 所以把 row_pos 清空让所有新行统一走模板行开标签（带 ht="41"，
                # 嵌入图要靠这个行高才显示得出来）；插入点取第一个下移后的行的起始偏移。
                shifted = self._row_positions(sheet_xml)
                after = sorted(r for r in shifted if r >= first_new)
                anchor = (
                    shifted[after[0]][0] if after else sheet_xml.index("</sheetData>")
                )
                row_pos = {}
            else:
                anchor = (
                    row_pos[last_data][1]
                    if last_data in row_pos
                    else sheet_xml.index("</sheetData>")
                )
            for i, item in enumerate(rows_data):
                new_rid = first_new + i
                pos = row_pos.get(new_rid)
                # 目标行已存在（多半是只带行高的预留空行）→ 复用它自己的开标签保住行高；
                # 不存在 → 拿模板行的开标签换行号。
                open_tag = (
                    pos[2]
                    if pos
                    else tpl_open.replace('r="%d"' % last_data, 'r="%d"' % new_rid)
                )
                body = self._build_cells(
                    new_rid, item, disp_ids.get(i),
                    base_styles, f_styles, img_styles, wrap_ctx,
                )
                text = open_tag + body + "</row>"
                if pos:
                    patches.append((pos[0], pos[1], text))
                    anchor = pos[1]
                else:
                    patches.append((anchor, anchor, text))

            # 一次性重建 sheet XML：逐行 re.sub 是 O(行数 × 文件长度)，
            # 468KB × 200 行要反复扫近 100MB，且插入后偏移全部失效。
            patches.sort(key=lambda p: p[0])  # 稳定排序：同一插入点保持原顺序
            out_parts: List[str] = []
            cur = 0
            for s, e, t in patches:
                out_parts.append(sheet_xml[cur:s])
                out_parts.append(t)
                cur = e
            out_parts.append(sheet_xml[cur:])
            sheet_new = "".join(out_parts)

            # dimension 只扩不缩：本表 ref 是 A1:P1048472（格式撑出来的），
            # 改小无意义还可能影响 WPS 的滚动区域判断。
            last_rid = last_data + len(rows_data)
            sheet_new = re.sub(
                r'<dimension ref="([A-Z]+\d+:[A-Z]+)(\d+)"/>',
                lambda m: '<dimension ref="%s%d"/>'
                % (m.group(1), max(int(m.group(2)), last_rid)),
                sheet_new,
                count=1,
            )
            changed[part] = sheet_new.encode("utf-8")

            # 插行还要同步 workbook.xml 里指向本表的 definedName（筛选区缓存
            # `_xlnm._FilterDatabase` 就是这个），不然筛选下拉的范围跟表内 autoFilter 打架
            if insert_at_top:
                wbx = zf.read("xl/workbook.xml").decode("utf-8")
                # 必须先切掉 `表名!` 再平移：表名本身可能以数字结尾（如 `StoreB2`），
                # 直接对整串套行号正则会把表名尾数当成行号改掉
                def shift_dn(mo: "re.Match") -> str:
                    hit = re.fullmatch(
                        r"(%s!)(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)" % re.escape(sheet_name),
                        mo.group(2),
                    )
                    if not hit:
                        return mo.group(0)
                    return (
                        mo.group(1)
                        + hit.group(1)
                        + self._shift_ref_rows(hit.group(2), header_row + 1, n_new)
                        + mo.group(3)
                    )

                new_wbx = re.sub(
                    r"(<definedName\b[^>]*>)([^<]*)(</definedName>)", shift_dn, wbx
                )
                if new_wbx != wbx:
                    changed["xl/workbook.xml"] = new_wbx.encode("utf-8")

            new_styles = self._rebuild_styles(wrap_ctx, styles_xml)
            if new_styles is not None:
                changed["xl/styles.xml"] = new_styles.encode("utf-8")

            tmp = src.with_name(src.stem + f"_tmp{ts}" + src.suffix)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zf.infolist():
                    data = changed.get(item.filename)
                    if data is None:
                        data = zf.read(item.filename)
                    zout.writestr(item, data)
                for arc, blob in new_media:
                    zout.writestr(arc, blob)

        shutil.move(str(tmp), str(src))

        return {
            "written": len(rows_data),
            "first_row": first_new,
            "last_row": first_new + n_new - 1,
            "images": len(new_media),
            "backup": str(backup),
        }

    @classmethod
    def insert_column_after(
        cls,
        file_path: str,
        sheet_name: str,
        after_title: str,
        new_title: str,
        header_row: int = 1,
        width: float = 9.0,
    ) -> Dict[str, Any]:
        """在标题为 after_title 的列右侧插入一列、表头写 new_title，返回操作结果。

        为什么不用 openpyxl 的 insert_cols：本工作簿是 WPS DISPIMG 表，openpyxl 一 save
        就毁嵌入图（见模块开头），只能走 zip/XML 直改。

        列号绑定的结构逐个平移（细节见各 _shift_*_cols）：单元格 r、行 spans、共享公式 ref、
        dimension、sheetView 视口/选区、cols 定义、mergeCell、autoFilter、条件格式 sqref 与
        cfRule 绝对引用、workbook.xml 里指向本表的 definedName（筛选区缓存）。
        既有数据行【不】补空单元格：新列的填充/边框由 <col> 的 style 提供，逐行插空 <c> 会给
        WINTAK 这种 5600 行的表凭空加几千个单元格。

        幂等：new_title 已存在则直接返回 inserted=False，不动文件、不做备份。
        改结构是不可逆操作，故照例先做时间戳备份；失败直接抛异常（不吞）。
        """
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"文件不存在：{file_path}")

        header = cls.read_header(file_path, sheet_name, header_row=header_row)
        titles = {t.strip(): c for c, t in header.items()}
        if new_title in titles:
            return {"inserted": False, "column": titles[new_title], "backup": None,
                    "reason": f"「{new_title}」列已存在"}
        anchor = titles.get(after_title)
        if not anchor:
            raise ValueError(
                f"Sheet「{sheet_name}」找不到「{after_title}」列，无法确定插列位置"
            )
        at = _col_to_idx(anchor) + 1

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = get_output_dir("backup") / f"{src.stem}_插列前备份{ts}{src.suffix}"
        shutil.copyfile(src, backup)

        with zipfile.ZipFile(src) as zf:
            part = cls._resolve_sheet_part(zf, sheet_name)
            if not part:
                raise ValueError(f"找不到工作表 '{sheet_name}'")
            xml = zf.read(part).decode("utf-8")
            i = xml.index("<sheetData")
            j = xml.index("</sheetData>") + len("</sheetData>")
            style = cls._col_style(xml[:i], at - 1)
            new_xml = (
                cls._shift_head_cols(xml[:i], at, style, width)
                + cls._insert_header_cell(
                    cls._shift_data_cols(xml[i:j], at), header_row, at, new_title, anchor
                )
                + cls._shift_tail_cols(xml[j:], at)
            )
            changed = {part: new_xml.encode("utf-8")}
            wbx = zf.read("xl/workbook.xml").decode("utf-8")
            new_wbx = cls._shift_defined_names(wbx, sheet_name, at)
            if new_wbx != wbx:
                changed["xl/workbook.xml"] = new_wbx.encode("utf-8")

            tmp = src.with_name(src.stem + f"_tmp{ts}" + src.suffix)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zf.infolist():
                    zout.writestr(item, changed.get(item.filename) or zf.read(item.filename))

        shutil.move(str(tmp), str(src))
        col = _idx_to_col(at)
        logger.info(
            f"Sheet「{sheet_name}」已在「{after_title}」({anchor}) 右侧插入「{new_title}」列({col})"
            f"，备份：{backup}"
        )
        return {"inserted": True, "column": col, "backup": str(backup)}

    @staticmethod
    def _col_style(head: str, idx: int) -> str:
        """取第 idx 列 <col> 的 style 索引（新列继承左邻格式用）；没有就返回空串。"""
        for mo in re.finditer(r'<col min="(\d+)" max="(\d+)"[^>]*/>', head):
            if int(mo.group(1)) <= idx <= int(mo.group(2)):
                st = re.search(r'style="(\d+)"', mo.group(0))
                return st.group(1) if st else ""
        return ""

    @classmethod
    def _insert_header_cell(
        cls, data: str, header_row: int, at: int, title: str, anchor: str
    ) -> str:
        """在表头行插入新列的标题单元格，样式抄左邻表头格（字体/填充/边框全跟着一致）。

        写成内联字符串 `t="str"`，不往 sharedStrings.xml 加条目：那份表 3500 多条共享串，
        动它要同步 count/uniqueCount，收益为零。read_header 走 _cell_text，两种都认。
        """
        col = _idx_to_col(at)
        mo = re.search(r'<row r="%d"[^>]*?>(.*?)</row>' % header_row, data, re.S)
        if not mo:
            raise ValueError(f"找不到表头行（第 {header_row} 行），无法写入新列标题")
        body = mo.group(1)
        st = re.search(r'<c r="%s%d"\s+s="(\d+)"' % (anchor, header_row), body)
        sa = f' s="{st.group(1)}"' if st else ""
        cell = f'<c r="{col}{header_row}"{sa} t="str"><v>{_xml_escape(title)}</v></c>'
        # 插到第一个列号 > at 的单元格之前；表头行末尾没有更右的列时直接追加
        nxt = None
        for c in re.finditer(r'<c r="([A-Z]{1,3})%d"' % header_row, body):
            if _col_to_idx(c.group(1)) > at:
                nxt = c.start()
                break
        new_body = body[:nxt] + cell + body[nxt:] if nxt is not None else body + cell
        return data[:mo.start(1)] + new_body + data[mo.end(1):]

    @classmethod
    def _shift_defined_names(cls, wbx: str, sheet_name: str, at: int) -> str:
        """平移 workbook.xml 里指向本表的 definedName（筛选区缓存 _FilterDatabase）。

        必须先切掉 `表名!` 再动列字母：表名本身可能含大写字母+数字（`Pawly全球1`、`StoreB2`），
        直接对整串套列引用正则会把表名的一部分当成列号改掉。
        """
        def one(mo: "re.Match") -> str:
            hit = re.fullmatch(
                r"(%s!)(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)" % re.escape(sheet_name),
                mo.group(2),
            )
            if not hit:
                return mo.group(0)
            return mo.group(1) + hit.group(1) + cls._shift_ref_cols(hit.group(2), at) + mo.group(3)

        return re.sub(r"(<definedName\b[^>]*>)([^<]*)(</definedName>)", one, wbx)

    def _build_cells(
        self,
        new_rid: int,
        item: Dict[str, Any],
        disp_id: Optional[str],
        base_styles: Dict[str, str],
        f_styles: Dict[str, str],
        img_styles: Dict[str, str],
        wrap_ctx: dict,
    ) -> str:
        """构造一行的 <c> 序列。样式索引全部来自预解析好的字典，不再扫表。"""
        values = item.get("values") or {}
        formulas = item.get("formulas") or {}
        image_column = item.get("image_column")

        cols_all = set(values) | set(formulas)
        if image_column and disp_id:
            cols_all.add(image_column)

        cells: List[str] = []
        for col in sorted(cols_all, key=_col_to_idx):
            ref = f"{col}{new_rid}"
            if col == image_column and disp_id:
                st = img_styles.get(col, "")
                sa = f' s="{st}"' if st else ""
                disp = f'_xlfn.DISPIMG(&quot;{disp_id}&quot;,1)'
                cached = f'=DISPIMG(&quot;{disp_id}&quot;,1)'
                cells.append(f'<c r="{ref}"{sa} t="str"><f>{disp}</f><v>{cached}</v></c>')
            elif col in formulas:
                f = _xml_escape(formulas[col].format(r=new_rid).lstrip("="))
                st = f_styles.get(col, "")
                sa = f' s="{st}"' if st else ""
                cells.append(f'<c r="{ref}"{sa}><f>{f}</f></c>')
            elif col in values:
                val = values[col]
                if val is None or val == "":
                    continue  # 空值不落单元格，保住预留行原格式
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    st = base_styles.get(col, "")
                    sa = f' s="{st}"' if st else ""
                    cells.append(f'<c r="{ref}"{sa}><v>{val}</v></c>')
                else:
                    wcol = self._wrap_style_for(wrap_ctx, col, base_styles, [])
                    sa = f' s="{wcol}"' if wcol else ""
                    cells.append(f'<c r="{ref}"{sa} t="str"><v>{_xml_escape(val)}</v></c>')
        return "".join(cells)

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
        for rid, body in reversed(rows):
            if rid == "1":
                continue
            m = re.search(r'<c r="%s\d+"\s+s="(\d+)"[^>]*?><f\b' % col, body)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _value_cell_style(rows: List[tuple], col: str) -> Optional[str]:
        """取最近一个同列非公式数据格的样式，供模板末行缺格时继承数字格式。"""
        for rid, body in reversed(rows):
            if rid == "1":
                continue
            for match in re.finditer(
                r'<c r="%s\d+"\s+s="(\d+)"[^>]*?(?:/>|>(.*?)</c>)' % col,
                body,
                re.S,
            ):
                if "<f" not in (match.group(2) or ""):
                    return match.group(1)
        return None

    @staticmethod
    def _left_neighbor_style(cell_styles: Dict[str, str], col: str) -> Optional[str]:
        """在模板行里往左找最近一个有样式的列，返回它的样式索引；一路到 A 都没有则 None。"""
        for idx in range(_col_to_idx(col) - 1, 0, -1):
            st = cell_styles.get(_idx_to_col(idx))
            if st:
                return st
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
