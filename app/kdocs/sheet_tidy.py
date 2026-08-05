# -*- coding: utf-8 -*-
"""协作表格整理管线：拆开跨行合并 + 数据区行序反转（最新在最上）。

适用场景：人工维护的登记表长期按时间往下追加，最老的在最上面；且遇到「一单多
尺码/多规格」时用合并单元格把订单号、运单号等公共字段竖着合起来。要看最新记录
得滚到底部，且合并行让筛选、排序、按行读取全部失效。本管线把这两件事一次做掉。

关键设计取舍：

1. 拆合并用 merge_range(across=true)。kdocs API 没有独立的 unmerge，但
   merge_range 的 across=true 语义是「区域内每行各自合并」——对【单列】区域来说，
   每行各自合并就等于拆开（2026-08-05 在空白远端行实测确认）。所以跨行跨列的块
   要【逐列】调用，不能整块调：整块传 across=true 会把每行横向合并，反而更糟。

2. 拆开后要把原值补进每一行（用户要求「复制相同的内容填充」）。顺序是先读值 →
   再拆 → 再回填：拆完只有左上角保留值，此时回填最简单。值优先取 fmlaText，
   这样 =DISPIMG(...) 这类嵌入图公式能原样复制到新行。

3. 行序反转【不】自己读出来重写，而是加一列临时序号 + 调原生 range_sort。
   自己重写等于把每个单元格的值搬家，DISPIMG 图片、单元格格式、行高全都会丢；
   range_sort 是表格引擎的原地排序，声明「保留格式和公式」，图片跟着行走。
   序号列写 1..N 后按它 desc 排，就是精确反转——不解析任何日期，因此不受
   「6.1 / 8.4-1 / 空值」这种混乱时间格式影响，也不会把空值行甩到一起。

4. 默认 dry-run。真正落笔前先 copy_worksheet 备份一份，出问题能对照。
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.kdocs.cli import KdocsCliError, call
from app.logger import logger

# 单次 get_range_data 的分块行数：整表一次读容易把响应撑爆，也容易撞网关超时。
_READ_CHUNK_ROWS = 400
# 表头探测扫描行数（口径同 orders 的 detect_header_row：前 N 行里非空最多那行）。
_HEADER_SCAN_ROWS = 3
# 批量写单元格操作的分包大小。
_WRITE_BATCH = 500
# 辅助序号列相对数据区最右列的偏移；留 1 列空隙，避免紧贴数据被误认成数据列。
_HELPER_COL_GAP = 2
# 表头里含这些词的列按「时间列」看待，用于校验现有行序是否真的时间升序。
# 顺序即优先级：越靠前越可信（平台创建时间是系统写的，国内发出时间是人工填的）。
# 「平台*」优先于人工列：平台创建时间/平台时间是系统写的，格式统一且不缺值；
# 采购日期/国内发出时间是人工填的，常年缺值且写成「6.1」「8.4-1」这种。
# 「客人下单日期」也归到前面——它反映真实下单先后，比采购日期更贴近业务时序。
_DATE_HINTS = ("平台创建时间", "平台时间", "创建时间", "客人下单日期", "下单日期",
               "下单时间", "订单时间", "采购日期", "国内发出时间", "发出时间",
               "发货时间", "日期", "时间")
# 升序判定阈值：可比较的相邻对里升序占比低于它就认为「现有行序不是时间升序」，
# 此时直接反转排不出「时间倒序」，管线拒绝写入（除非 --force）。
_ASC_RATIO_MIN = 0.9


def parse_date_key(text: str) -> Optional[Tuple[bool, int, int, int, int, int]]:
    """把登记表里的时间文本解析成可比较的元组；解析不出返回 None。

    这些表的时间列是人工维护的，格式五花八门，实测见过：
      2026-05-26 23:50:28 / 2026/6/20 14:12 / 6.25 / 8.4-1 / 7.14（无年份的月.日）
    返回元组第一位是「是否带年份」——带年份的和不带年份的【不能互相比较】
    （(0,8,4) 会小于 (2026,7,10)，混着比会得出荒谬结论），调用方据此分组。
    """
    s = str(text or "").strip()
    if not s:
        return None
    # 带年份：2026-05-26 [23:50:28] / 2026/6/20 [14:12]
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
                 r"(?:[\sT]+(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (True, y, mo, d, hh, mi)
        return None
    # 带年份的中文写法：2026年8月2日
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (True, y, mo, d, 0, 0)
        return None
    # 无年份的中文写法：1月27日 / 3月12飞特 / 3月14（「日」可缺，后面常跟物流商名）
    m = re.match(r"^(\d{1,2})\s*月\s*(\d{1,2})", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (False, 0, mo, d, 0, 0)
        return None
    # 无年份：6.25 / 8.4-1 / 4/4嘉运（尾巴 -1 是同日第二批，当作次序后缀；
    # 后面可以跟物流商等中文备注，只取开头的月日）
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:\s*-\s*(\d+))?", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        seq = int(m.group(3) or 0)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return (False, 0, mo, d, seq, 0)
    return None


def col_letter(idx: int) -> str:
    """0-based 列索引 → 列字母（0→A, 26→AA）。"""
    s = ""
    idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


@dataclass
class MergeRegion:
    """一处合并区域（全部 0-based，闭区间）。"""
    row_from: int
    row_to: int
    col_from: int
    col_to: int
    value: str = ""

    @property
    def spans_rows(self) -> bool:
        return self.row_to > self.row_from

    @property
    def spans_cols(self) -> bool:
        return self.col_to > self.col_from

    def label(self) -> str:
        return (f"{col_letter(self.col_from)}{self.row_from + 1}:"
                f"{col_letter(self.col_to)}{self.row_to + 1}")


@dataclass
class SheetPlan:
    """一个 Sheet 的整理计划 / 执行结果。"""
    sheet_name: str
    worksheet_id: int
    header_row: int          # 1-based
    data_row_from: int       # 0-based，数据区首行
    data_row_to: int         # 0-based，数据区末行
    col_to: int              # 0-based，数据区最右列
    row_merges: List[MergeRegion] = field(default_factory=list)
    col_merges: List[MergeRegion] = field(default_factory=list)
    pic_cells: int = 0
    # 行序校验：反转等于「时间倒序」的前提是现有行序为时间升序，这里记校验依据
    date_col: str = ""          # 用来校验的时间列（列字母）
    date_col_title: str = ""
    asc_pairs: int = 0          # 可比较的相邻对中，升序的对数
    cmp_pairs: int = 0          # 可比较的相邻对总数
    date_samples: List[str] = field(default_factory=list)
    # 执行结果
    unmerged: int = 0
    filled_cells: int = 0
    reversed_rows: int = 0
    reversed_by_rewrite: bool = False   # 走了「读出来倒序重写」的降级路径
    backup_sheet: str = ""
    skipped_reason: str = ""

    @property
    def asc_ratio(self) -> Optional[float]:
        """相邻可比较对里升序占比；没有可比较对返回 None（无法判断）。"""
        return (self.asc_pairs / self.cmp_pairs) if self.cmp_pairs else None

    @property
    def order_ok(self) -> bool:
        """现有行序是否可判定为时间升序（反转才等于时间倒序）。"""
        r = self.asc_ratio
        return r is not None and r >= _ASC_RATIO_MIN

    @property
    def data_rows(self) -> int:
        return max(0, self.data_row_to - self.data_row_from + 1)


class SheetTidy:
    """一个协作表格文件的整理句柄。url 可以是协作链接，也可以是 file_id。"""

    def __init__(self, doc: str, timeout: Optional[int] = None):
        doc = str(doc or "").strip()
        if not doc:
            raise KdocsCliError("协作文档标识（链接或 file_id）为空")
        # kdocs-cli 三选一定位文档：http(s) 开头按 url 传，其余按 file_id
        self._id_param = ("url" if doc.lower().startswith(("http://", "https://"))
                          else "file_id")
        self.doc = doc
        self._timeout_kw = {"timeout": timeout} if timeout else {}
        self._sheets: Optional[Dict[str, dict]] = None

    def _call(self, action: str, payload: dict, retry_5xx: bool = False):
        return call("sheet", action, {self._id_param: self.doc, **payload},
                    retry_5xx=retry_5xx, **self._timeout_kw)

    # ---- 读 --------------------------------------------------------------

    def sheets_info(self, refresh: bool = False) -> Dict[str, dict]:
        """{Sheet 名: {id, row_to, col_to, visible}}，行列均 0-based。"""
        if self._sheets is not None and not refresh:
            return self._sheets
        data = self._call("get-sheets-info", {}, retry_5xx=True)
        infos = data.get("sheetsInfo") if isinstance(data, dict) else None
        if not isinstance(infos, list):
            raise KdocsCliError(f"get_sheets_info 响应缺少 sheetsInfo：{str(data)[:200]}")
        self._sheets = {
            str(s.get("sheetName")): {
                "id": int(s.get("sheetId")),
                "row_to": int(s.get("rowTo") or 0),
                "col_to": int(s.get("colTo") or 0),
                "visible": bool(s.get("isVisible")),
                "empty": bool(s.get("isEmpty")),
            }
            for s in infos if s.get("sheetName")
        }
        return self._sheets

    def _info(self, sheet: str) -> dict:
        info = self.sheets_info().get(sheet)
        if not info:
            raise KdocsCliError(
                f"文档里找不到工作表「{sheet}」；现有：{list(self.sheets_info())}"
            )
        return info

    def _read(self, sheet: str, row_from: int, row_to: int,
              col_from: int, col_to: int) -> List[dict]:
        data = self._call("get-range-data", {
            "worksheet_id": self._info(sheet)["id"],
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to},
        }, retry_5xx=True)
        cells = data.get("rangeData") if isinstance(data, dict) else None
        return cells if isinstance(cells, list) else []

    def detect_header_row(self, sheet: str) -> int:
        """前 _HEADER_SCAN_ROWS 行里非空单元格最多的那行算表头，返回 1-based 行号。

        口径与订单管线的 detect_header_row 一致：有的登记表第 1 行是只占一格的
        跨列大标题，真表头在第 2 行；平票取更靠上那行。
        """
        info = self._info(sheet)
        col_to = min(int(info["col_to"]), 40)
        cells = self._read(sheet, 0, _HEADER_SCAN_ROWS - 1, 0, max(col_to, 0))
        counts: Dict[int, int] = {}
        for c in cells:
            if str(c.get("cellText") or "").strip():
                counts[int(c["rowFrom"])] = counts.get(int(c["rowFrom"]), 0) + 1
        if not counts:
            return 1
        return max(counts, key=lambda r: (counts[r], -r)) + 1

    def read_header(self, sheet: str, header_row: int) -> Dict[int, str]:
        """读表头行 → {0-based 列索引: 标题}。"""
        info = self._info(sheet)
        cells = self._read(sheet, header_row - 1, header_row - 1, 0,
                           max(int(info["col_to"]), 0))
        out: Dict[int, str] = {}
        for c in cells:
            title = str(c.get("cellText") or "").strip()
            if title:
                out[int(c["colFrom"])] = title
        return out

    def pick_date_col(self, header: Dict[int, str]) -> Tuple[int, str]:
        """按 _DATE_HINTS 优先级挑一个时间列，返回 (0-based 列索引, 标题)。

        挑不到返回 (-1, "")。优先级刻意让「平台创建时间」这类系统写入的列胜过
        「国内发出时间」这类人工填的——后者常年缺值且格式随意。
        """
        for hint in _DATE_HINTS:
            for ci, title in sorted(header.items()):
                if hint in title:
                    return ci, title
        return -1, ""

    def check_order(self, p: SheetPlan, date_col: Optional[int] = None) -> None:
        """校验现有行序是否时间升序，结果写回 plan（不改表）。

        为什么必须校验：本管线「反转行序」只有在原表是【时间升序】时才等于用户要的
        「时间倒序」。人工维护的表通常是往下追加所以天然升序，但不能假定——万一某表
        已经是倒序，反转会把它排成正序，正好排反。

        比法：只比【相邻】两行，且只比同为「带年份」或同为「不带年份」的对
        （带年份与不带年份不可比，见 parse_date_key）。相等不计入 cmp_pairs，
        因为相等既不支持也不反对升序假设。
        """
        header = self.read_header(p.sheet_name, p.header_row)
        ci, title = ((date_col, header.get(date_col, "")) if date_col is not None
                     else self.pick_date_col(header))
        if ci < 0:
            return
        p.date_col, p.date_col_title = col_letter(ci), title

        values: Dict[int, str] = {}
        for start in range(p.data_row_from, p.data_row_to + 1, _READ_CHUNK_ROWS):
            end = min(start + _READ_CHUNK_ROWS - 1, p.data_row_to)
            for c in self._read(p.sheet_name, start, end, ci, ci):
                text = str(c.get("cellText") or "").strip()
                if text:
                    values[int(c["rowFrom"])] = text

        keys = [(r, parse_date_key(v)) for r, v in sorted(values.items())]
        keys = [(r, k) for r, k in keys if k is not None]
        for (_, a), (_, b) in zip(keys, keys[1:]):
            if a[0] != b[0]:      # 一个带年份一个不带，不可比
                continue
            if a == b:            # 相等不表态
                continue
            p.cmp_pairs += 1
            if a < b:
                p.asc_pairs += 1

        ordered = [values[r] for r, _ in keys]
        p.date_samples = ordered[:3] + (["..."] if len(ordered) > 6 else []) + ordered[-3:]

    # ---- 计划 ------------------------------------------------------------

    def plan(self, sheet: str, header_row: Optional[int] = None) -> SheetPlan:
        """扫一遍数据区，产出整理计划（不写任何东西）。"""
        info = self._info(sheet)
        hrow = header_row or self.detect_header_row(sheet)
        p = SheetPlan(
            sheet_name=sheet, worksheet_id=info["id"], header_row=hrow,
            data_row_from=hrow, data_row_to=int(info["row_to"]),
            col_to=int(info["col_to"]),
        )
        if p.data_rows <= 0:
            p.skipped_reason = "表头之下没有数据行"
            return p

        seen: set = set()
        last_content_row = -1
        for start in range(p.data_row_from, p.data_row_to + 1, _READ_CHUNK_ROWS):
            end = min(start + _READ_CHUNK_ROWS - 1, p.data_row_to)
            for c in self._read(sheet, start, end, 0, p.col_to):
                r0, r1 = int(c["rowFrom"]), int(c["rowTo"])
                c0, c1 = int(c["colFrom"]), int(c["colTo"])
                if str(c.get("cellText") or "").strip() or str(c.get("fmlaText") or "").strip():
                    last_content_row = max(last_content_row, r1)
                if c.get("isCellPic"):
                    p.pic_cells += 1
                if r1 == r0 and c1 == c0:
                    continue
                # 分块读会让跨块的合并区域重复出现，按坐标去重
                key = (r0, r1, c0, c1)
                if key in seen:
                    continue
                seen.add(key)
                # 值优先取公式原文，DISPIMG 这类嵌入图才能原样复制到拆出来的新行
                value = str(c.get("fmlaText") or c.get("cellText") or "")
                region = MergeRegion(r0, r1, c0, c1, value)
                (p.row_merges if region.spans_rows else p.col_merges).append(region)

        # 数据区末行以「最后一个真有内容的行」为准，而不是 sheets_info 的 rowTo。
        # rowTo 是水位线：某个单元格被写过再清空，水位不会缩回去（脚本探测、
        # 用户误输入都会留下这种虚高尾巴）。按 rowTo 反转会把成千个空行搬到数据
        # 上方，看起来就像整表被清空了。
        if last_content_row >= p.data_row_from and last_content_row < p.data_row_to:
            logger.info(
                f"「{sheet}」数据区末行按实际内容收窄：{p.data_row_to + 1} → "
                f"{last_content_row + 1} 行（rowTo 水位虚高）"
            )
            p.data_row_to = last_content_row
        elif last_content_row < p.data_row_from:
            p.skipped_reason = "表头之下没有非空数据行"
            return p

        # 收窄末行后，落在数据区之外的合并（空白区里的残留合并）不属于本次处理范围
        p.row_merges = [m for m in p.row_merges if m.row_from <= p.data_row_to]
        p.col_merges = [m for m in p.col_merges if m.row_from <= p.data_row_to]
        p.row_merges.sort(key=lambda m: (m.row_from, m.col_from))
        p.col_merges.sort(key=lambda m: (m.row_from, m.col_from))
        return p

    # ---- 写：拆合并 -------------------------------------------------------

    def unmerge_rows(self, p: SheetPlan) -> None:
        """拆开数据区内所有合并（跨行的和仅跨列的），把原值复制填充到每个单元格。

        逐列调 merge_range(across=true)：单列区域「每行各自合并」即拆开。跨行跨列
        的块必须按列拆，整块传 across=true 会横向合并每一行。

        仅跨列的合并也必须拆。原以为跨列合并不影响行序、可以留着，结果 2026-08-05
        实测发现：range_sort 只要区域内含任何合并单元格就会静默失效——返回
        status=finished、无报错、日志干净，但一行都没动（与 Excel 桌面版拒绝对含
        合并区域排序的行为一致）。只拆跨行合并的表（如 Pawly牛仔裤 留着跨列合并）
        排序会白跑，管线还误报成功。
        """
        targets = p.row_merges + p.col_merges
        if not targets:
            return
        wsid = p.worksheet_id
        for m in targets:
            for ci in range(m.col_from, m.col_to + 1):
                rng = f"{col_letter(ci)}{m.row_from + 1}:{col_letter(ci)}{m.row_to + 1}"
                self._call("merge-range", {"worksheet_id": wsid, "range": rng,
                                           "across": True})
            p.unmerged += 1

        # 回填：拆完只有左上角留着值，把它写进区域内每个单元格
        ops: List[dict] = []
        for m in targets:
            if not m.value:
                continue
            for r in range(m.row_from, m.row_to + 1):
                for ci in range(m.col_from, m.col_to + 1):
                    if r == m.row_from and ci == m.col_from:
                        continue  # 左上角原值还在，不必重写
                    ops.append({
                        "op_type": "cell_operation_type_formula",
                        "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                        "formula": m.value,
                    })
        self._batch_write(wsid, ops)
        p.filled_cells = len(ops)

    def _batch_write(self, wsid: int, ops: List[dict]) -> None:
        """分包批量写。range_data_batch_update 是幂等的（同值写同格），可开 5xx 重试。"""
        for start in range(0, len(ops), _WRITE_BATCH):
            self._call("range-data-batch-update", {
                "worksheet_id": wsid,
                "range_data": ops[start:start + _WRITE_BATCH],
            }, retry_5xx=True)

    # ---- 写：行序反转 -----------------------------------------------------

    def reverse_rows(self, p: SheetPlan) -> None:
        """把数据区行序整体反转（末行变首行），靠临时序号列 + 原生 range_sort 实现。

        为什么不直接读出来倒着写回：那会把每格的值搬家，DISPIMG 嵌入图、单元格
        格式和行高全丢。range_sort 是引擎原地排序，保留格式与公式。
        """
        n = p.data_rows
        if n <= 1:
            return
        wsid = p.worksheet_id
        helper = p.col_to + _HELPER_COL_GAP  # 0-based 辅助列
        helper_letter = col_letter(helper)

        # 序号列写 1..N，再按它 desc 排 → 精确反转，不依赖任何业务列的可排序性
        ops = [{
            "op_type": "cell_operation_type_formula",
            "row_from": p.data_row_from + i, "row_to": p.data_row_from + i,
            "col_from": helper, "col_to": helper,
            "formula": str(i + 1),
        } for i in range(n)]
        self._batch_write(wsid, ops)

        try:
            # 排序区域必须覆盖【全部数据列 + 辅助列】，漏列会让那些列不跟着走、行内错位。
            # header=false：这里的 range 从数据首行起，本身不含表头。
            rng = (f"A{p.data_row_from + 1}:{helper_letter}{p.data_row_to + 1}")
            self._call("range-sort", {
                "worksheet_id": wsid, "range": rng,
                "key": helper_letter, "order": "desc", "header": False,
            })
            # 必须读回验证：range_sort 遇到区域内残留合并会【静默失效】——返回
            # status=finished、无报错、日志干净，却一行都没动。不验证就会误报成功
            # （2026-08-05 在 Pawly牛仔裤 等 3 个表上正是这样白跑了一遍）。
            # 序号列排序后首行应当是 N（降序），仍是 1 就说明没生效。
            top = self._read(p.sheet_name, p.data_row_from, p.data_row_from,
                             helper, helper)
            got = str(top[0].get("cellText") or "").strip() if top else ""
            if got != str(n):
                raise KdocsCliError(
                    f"排序未生效：「{p.sheet_name}」序号列首行期望 {n} 实际「{got}」。"
                    f"通常是数据区内仍有合并单元格（range_sort 遇合并会静默放弃）"
                )
            p.reversed_rows = n
        finally:
            # 无论排序成败都清掉辅助列，别在用户表上留垃圾
            self._batch_write(wsid, [{
                "op_type": "cell_operation_type_formula",
                "row_from": p.data_row_from, "row_to": p.data_row_to,
                "col_from": helper, "col_to": helper, "formula": "",
            }])

    def reverse_rows_by_rewrite(self, p: SheetPlan) -> None:
        """反转行序的替代路径：整块读出来，倒序写回。

        什么时候用：range_sort 在个别工作表上就是不工作（2026-08-05 实测
        WINTAK欧区 与「 Vibe Link」两表，换 4 种调用写法、甚至复制成新副本都失效，
        后者还报 "Cannot read properties of undefined (reading 'Activate')"，
        而同文档其余 5 个表均正常）。原因在云端，客户端无法绕过。

        代价与取舍：这条路搬的是【单元格值/公式】，不搬单元格格式（背景色、边框、
        行高）。所以格式会留在原来的行位置上，与内容错位。DISPIMG 是公式引用，
        搬公式即等于搬图，图能跟着行走——这是它还能用的前提。
        因此只在 range_sort 确实失效时才降级到这里，不作为默认路径。
        """
        n = p.data_rows
        if n <= 1:
            return
        # 整块读出：值优先取公式原文，DISPIMG 才能原样搬到新行
        grid: Dict[int, Dict[int, str]] = {}
        for start in range(p.data_row_from, p.data_row_to + 1, _READ_CHUNK_ROWS):
            end = min(start + _READ_CHUNK_ROWS - 1, p.data_row_to)
            for c in self._read(p.sheet_name, start, end, 0, p.col_to):
                value = str(c.get("fmlaText") or c.get("cellText") or "")
                if value:
                    grid.setdefault(int(c["rowFrom"]), {})[int(c["colFrom"])] = value

        rows = [grid.get(p.data_row_from + i, {}) for i in range(n)]
        rows.reverse()

        # 倒序写回。空单元格也要显式写空串，否则原行残留值会留在新行上
        ops: List[dict] = []
        for i, row in enumerate(rows):
            r = p.data_row_from + i
            for ci in range(p.col_to + 1):
                ops.append({
                    "op_type": "cell_operation_type_formula",
                    "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                    "formula": row.get(ci, ""),
                })
        self._batch_write(p.worksheet_id, ops)
        p.reversed_rows = n
        p.reversed_by_rewrite = True

    # ---- 备份 / 校验 ------------------------------------------------------

    def backup(self, p: SheetPlan) -> str:
        """copy_worksheet 复制一份当前 Sheet 作为备份，返回副本名（拿不到名字返回空）。"""
        before = set(self.sheets_info(refresh=True))
        self._call("copy-worksheet", {"worksheet_id": p.worksheet_id})
        added = set(self.sheets_info(refresh=True)) - before
        name = next(iter(added), "")
        p.backup_sheet = name
        return name

    def snapshot(self, p: SheetPlan, cols: List[int]) -> List[Tuple[str, ...]]:
        """按行取若干列的文本，用于比对反转前后是否只是顺序变了、内容没丢。"""
        rows: Dict[int, Dict[int, str]] = {}
        lo, hi = min(cols), max(cols)
        for start in range(p.data_row_from, p.data_row_to + 1, _READ_CHUNK_ROWS):
            end = min(start + _READ_CHUNK_ROWS - 1, p.data_row_to)
            for c in self._read(p.sheet_name, start, end, lo, hi):
                ci = int(c["colFrom"])
                if ci in cols:
                    text = str(c.get("cellText") or "").strip()
                    if text:
                        rows.setdefault(int(c["rowFrom"]), {})[ci] = text
        return [tuple(rows.get(r, {}).get(ci, "") for ci in cols)
                for r in range(p.data_row_from, p.data_row_to + 1)]

    # ---- 编排 ------------------------------------------------------------

    def tidy(self, sheet: str, header_row: Optional[int] = None,
             write: bool = False, do_backup: bool = True,
             verify_cols: Optional[List[int]] = None,
             date_col: Optional[int] = None, force: bool = False) -> SheetPlan:
        """整理一个 Sheet：先拆跨行合并，再反转行序。write=False 只出计划。

        写入前先 check_order 校验「现有行序是时间升序」这个前提——不成立时反转会把
        顺序排反，所以默认拒绝写入，需要 force=True 才强行执行。

        校验用 verify_cols（0-based 列索引）在动手前后各取一次快照，比对「多重集
        相同、顺序恰好相反」。列没指定就取前 3 列。
        """
        p = self.plan(sheet, header_row)
        if p.skipped_reason:
            return p

        self.check_order(p, date_col)
        if not write:
            return p

        if not p.order_ok and not force:
            r = p.asc_ratio
            p.skipped_reason = (
                f"现有行序无法判定为时间升序"
                + (f"（{p.date_col_title} 列升序占比 {r:.0%}，"
                   f"{p.asc_pairs}/{p.cmp_pairs} 对）" if r is not None
                   else f"（找不到可解析的时间列，识别到的列：{p.date_col_title or '无'}）")
                + "；直接反转可能把顺序排反，已跳过。确认要反转请加 --force"
            )
            logger.warning(f"「{sheet}」{p.skipped_reason}")
            return p

        cols = verify_cols if verify_cols else list(range(0, min(3, p.col_to + 1)))
        before = self.snapshot(p, cols)

        if do_backup:
            name = self.backup(p)
            logger.info(f"「{sheet}」已备份为副本：{name or '（未取到副本名）'}")

        self.unmerge_rows(p)
        try:
            self.reverse_rows(p)
        except KdocsCliError as e:
            # range_sort 在个别工作表上就是不工作（云端问题，客户端绕不过），
            # 降级到「读出来倒序重写」。代价是单元格格式不跟着搬，见该方法说明。
            if "排序未生效" not in str(e) and "Activate" not in str(e):
                raise
            logger.warning(
                f"「{sheet}」range_sort 失效，降级为读出重写（格式不随行搬动）：{e}"
            )
            self.reverse_rows_by_rewrite(p)
        self.sheets_info(refresh=True)

        after = self.snapshot(p, cols)
        self._verify(p, before, after)
        return p

    def _verify(self, p: SheetPlan, before: List[tuple], after: List[tuple]) -> None:
        """反转后自检：行数不变、内容集合不变、顺序确实反了。

        自检只告警不抛错——动作已经落表，抛错也回不去；备份副本在，用户可对照。
        但拆过合并的表内容集合本就会变（空格被填上值），这时只查行数与顺序倾向。
        """
        if len(before) != len(after):
            logger.warning(
                f"「{p.sheet_name}」反转后行数变了：{len(before)} → {len(after)}，"
                f"请对照备份副本 {p.backup_sheet or '（无）'} 检查"
            )
            return
        if not p.unmerged and sorted(before) != sorted(after):
            logger.warning(
                f"「{p.sheet_name}」反转后内容集合与之前不一致，"
                f"请对照备份副本 {p.backup_sheet or '（无）'} 检查"
            )
        if before and after and before[::-1] != after and not p.unmerged:
            logger.warning(f"「{p.sheet_name}」反转结果与「顺序恰好相反」不完全一致，请抽查")
