# -*- coding: utf-8 -*-
"""订单登记表的云端后端：通过 kdocs-cli 读写金山协作表格（在线 .xlsx）。

与本地后端（app/tool/wps_excel_tool.py 的 WpsExcelTool）保持同一套口径：
  - header 是 {列字母: 标题}，header_row 是 1-based 行号（detect_header_row 语义：
    前 3 行里非空单元格最多的一行）；
  - 判重读的是「订单号/尺码」等列的现有值集合；
  - 写入是「插到表头正下方」（insert_at_top），新订单在最上面。

云端特有约束与取舍：
  - 行列索引在 API 侧是 0-based，本模块内部统一转换，对外仍用列字母 + 1-based 行号；
  - 图片走 URL 嵌入（sheet.range_data_batch_update 的 cell_operation_type_picture +
    sheet_pic_type_url），用 OrderRow.image_url（kwcdn 在线图），不经本地上传；
    avif 参数先经 pipeline.to_jpeg_url 转 jpeg，否则云端渲染不出；
  - 判重/水位列整列读取有上限（_MAX_SCAN_ROWS）：登记表单 Sheet 数千行，整列读一次
    也就一次调用，但设个上限防表失控膨胀后把响应撑爆；
  - kdocs-cli 限频（429001/429002）时按响应提示等待后重试一次，仍失败则抛出由上层
    按「单表失败不连坐」处理。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.logger import logger
from app.orders import pipeline

# 判重/水位整列扫描的行数上限（数据区超过它只扫最近的部分——新单插在表头下，
# 最近的 N 行就是最新登记的那批，判重覆盖不到更老的历史单是已知代价）。
_MAX_SCAN_ROWS = 20000
# 读表头时扫的列数上限（登记表最宽的表也就十几列，给足余量）。
_HEADER_SCAN_COLS = 40
# CLI 单次调用超时（秒）：读大列 + 写批量行都可能偏慢。
_CLI_TIMEOUT = 180
# 限频重试：429001 响应里带恢复秒数时按它等，否则按这个默认值。
_RATE_LIMIT_WAIT = 20
# 嵌图尺寸：-1 = 自适应单元格（2026-08-04 实机对照实验，用户确认自适应观感最好；
# 固定 76px 在云文档里渲染得过小）。
_IMAGE_PX = -1
# 纯数字串达到这个长度就强制按文本录入（见 force_text_input）。取 12 是因为：
# 表格数值只有 15 位有效数字，12 位已经进了「显示成科学计数法」的区间；而管线会写的
# 数值字段（成交价、件数）不可能有 12 位，故这个阈值不会误伤需要求和的列。
_TEXT_DIGITS_MIN = 12

# 读侧（get_range_data）与写侧（range_data_batch_update 的 xf）的对齐枚举不是同一套：
# 读回来是 haCenter/vaCenter 这样的字符串，写进去要的是 alcH/alcV 数字。
# 枚举取自 kdocs 参数文档：alcH 1=左 2=居中 3=右 4=填充 5=两端 6=跨列 7=分散；
# alcV 0=上 1=中 2=下 3=两端 4=分散。haGeneral 没有对应数字（就是"没设过"），不回放。
_ALIGN_H = {
    "haleft": 1, "hacenter": 2, "haright": 3, "hafill": 4,
    "hajustify": 5, "hadistributed": 7,
}
_ALIGN_V = {
    "vatop": 0, "vacenter": 1, "vabottom": 2, "vajustify": 3, "vadistributed": 4,
}
# 通用格式＝没设过数字格式，回放它没有意义（还白占一次格式操作）。
_GENERAL_NUMFMT = {"g/通用格式", "general", ""}


def _writes_number(value: Any) -> bool:
    """这一格写进去的东西最终会不会以数值形态显示（公式的计算结果也算）。

    用来决定要不要把历史行的数字格式回放到这一格：站点/类目/货号这些文本列，
    历史单元格上常年挂着 `0_ ` 之类的数字格式（表格默认样式，文本显示不受影响），
    照抄到新行虽然当下看不出问题，但一旦写进去的是「20cm」「01 号仓」这类
    形似数字的文本就会被格式化。数字格式只回放给真数字。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    s = str(value).strip()
    if s.startswith("="):  # 公式：结果按数字格式显示
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def read_cell_xf(cell: dict) -> Optional[dict]:
    """把 get_range_data 读回的单元格格式，翻成批量写接口能吃的 xf。

    【为什么必须有这层翻译】两侧字段名根本不是一套：读回来是
    `numFormat`/`alignment:{horizontal,vertical}`/`fonts`/`cell_background_color`，
    写进去要的是 `numfmt`（**全小写**）/`alcH`/`alcV`/`font`/`fill`。
    此前代码直接取 `cell["xf"]` 再原样写回——响应里压根没有 xf 这个键，于是
    格式字典恒为空，新写的行永远拿不到历史行的两位小数/百分比（实测：历史行
    显示 `25.00`/`16.05%`，管线写的新行显示 `1`/`0`）。

    只翻【数字格式与对齐】：这两项决定数值怎么显示（小数位、百分比、居中），是
    「按其它数据的格式写入」的实质。字体/底色/边框不翻——颜色要 ARGB 转色对象，
    kdocs 文档明确要求「不确定颜色值时不传」，臆造一个反而会把整行刷成别的样子；
    况且新行插进来本就继承表格既有外观，缺的只是数字格式。
    无格式可回放（通用格式 + 未设对齐）返回 None，调用方据此跳过。
    """
    xf: Dict[str, Any] = {}
    # 【不能 strip】`0.00_ ` 尾部那个空格是格式串的一部分（`_ ` = 预留一个字符宽度用于
    # 与负数右对齐），去掉它写回去，新行的小数位就跟历史行差半个字符。
    numfmt = str(cell.get("numFormat") or "")
    if numfmt.strip() and numfmt.strip().lower() not in _GENERAL_NUMFMT:
        xf["numfmt"] = numfmt
    align = cell.get("alignment")
    if isinstance(align, dict):
        h = _ALIGN_H.get(str(align.get("horizontal") or "").strip().lower())
        v = _ALIGN_V.get(str(align.get("vertical") or "").strip().lower())
        if h is not None:
            xf["alcH"] = h
        if v is not None:
            xf["alcV"] = v
    return xf or None


def _resolve_cli(cli: str) -> str:
    """定位 kdocs-cli 可执行文件。

    不能只看 PATH：Web 服务由 启动.bat/launcher.ps1 拉起，继承的是开机环境，
    没有交互 shell 里 ~./bashrc 加的 ~/.local/bin。找不到时依次回退到安装脚本
    的默认目录，仍找不到则原样返回（让 subprocess 的 FileNotFoundError 给出
    可读的报错）。
    """
    if shutil.which(cli):
        return cli
    for candidate in (
        Path.home() / ".local" / "bin" / cli,
        Path.home() / ".local" / "bin" / f"{cli}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return cli


class KdocsSheetError(Exception):
    """kdocs-cli 调用失败（进程错误 / 业务错误码 / 响应不可解析）。"""


def _business_error_detail(env: dict) -> str:
    """从不同版本的 kdocs 错误信封里提取可读原因，避免只报一个 ``None``。

    2.6.1 的部分接口把原因放在 error/message/detail，而不是 msg；直接读 msg 会把
    400001 这类参数错误最关键的诊断信息丢掉。这里仅展开响应信封，不带请求体，避免
    文档链接或业务数据被写进日志。
    """
    for key in ("msg", "message", "error", "reason"):
        value = env.get(key)
        if value not in (None, "", {}):
            return str(value)[:300]
    detail = env.get("detail")
    if detail not in (None, "", {}):
        if isinstance(detail, str):
            return detail[:300]
        return json.dumps(detail, ensure_ascii=False, default=str)[:300]
    return "响应未提供错误说明"


def col_to_index(col: str) -> int:
    """列字母 → 0-based 列索引（A→0, B→1, AA→26）。"""
    n = 0
    for ch in col.strip().upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def index_to_col(idx: int) -> str:
    """0-based 列索引 → 列字母（0→A）。"""
    s = ""
    idx += 1
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def force_text_input(value: Any) -> str:
    """把「本该按文本落表」的纯数字串加上 `'` 前缀强制文本录入，其余原样返回。

    为什么需要：range_data_batch_update 的 formula 参数是【常规录入】语义，等同于人在
    单元格里敲字符——长数字串会被表格当数值解析。22 位的平台物流跟踪号
    `9200190419690851234567` 落表后显示成 `9.20019041969085E+21`，且表格数值只有 15 位
    有效数字，尾数被永久抹平、原值再也读不回来（2026-08-07 实机 get_typed_value 实测：
    裸写得到 type=double，加 `'` 前缀得到 type=string 且值完整）。

    为什么按【值】判定而不是按列标题：本函数同时服务订单登记与商品采集两条管线，
    write_rows 拿到的是 {列字母: 值}、不带标题语义；而纯数字长串这个特征本身就足以
    区分标识符和金额。

    命中条件是「纯数字」且「长度 >= _TEXT_DIGITS_MIN 或有前导零」，刻意不含这两类：
      - 金额（平台成交价）和数量必须留成数值才能求和、筛选大于 1 的多件单，而它们
        不可能有 12 位；含小数点的 `12.34` 本就不是纯数字串，天然不命中。
      - 带字母/连字符的订单号（`PO-211-…`）表格本来就存成文本，无需干预。
    前导零单独判：`0012` 这种无论多短，落成数值都会被抹成 12。

    `'` 前缀只作用于录入阶段，读回的 cellText 不含它，所以判重、水位、写入校验的
    口径完全不变（本模块所有读路径都走 cellText）。
    """
    s = str(value)
    # 已带前缀的原样返回，避免调用方重复处理时叠成 `''123`
    if s.startswith("'"):
        return s
    # isdigit 对全角数字/上标也为真，叠加 isascii 收窄到半角 0-9
    if not (s.isascii() and s.isdigit()):
        return s
    if len(s) >= _TEXT_DIGITS_MIN or (len(s) > 1 and s.startswith("0")):
        return "'" + s
    return s


class KdocsSheet:
    """一个云端表格文件（file_id）的读写句柄。无状态，方法级幂等性同底层 API。"""

    def __init__(self, file_id: str, cli: str = "kdocs-cli", timeout: int = _CLI_TIMEOUT):
        if not file_id:
            raise KdocsSheetError("cloud file_id 为空")
        self.file_id = file_id
        # kdocs-cli 的文档定位三选一：url / link_id / file_id。UI 允许直接粘贴
        # 协作文档链接，http(s) 开头的一律按 url 传，其余按 file_id。
        # scheme 大小写不敏感（与 is_cloud_link 的口径一致，用户可能贴大写链接）。
        self._id_param = ("url" if file_id.strip().lower().startswith(("http://", "https://"))
                          else "file_id")
        self.cli = _resolve_cli(cli)
        self.timeout = timeout
        self._sheets: Optional[Dict[str, dict]] = None

    # ---- 底层调用 --------------------------------------------------------

    def _run_once(self, service: str, action: str, payload: dict) -> dict:
        """同步调一次 kdocs-cli，返回解包后的数据对象；失败抛 KdocsSheetError。

        参数一律走 --file 临时 JSON（中文/大 payload 不能走 key=value，Windows 会毁
        UTF-8），用完即删。stdout 必须是 JSON；两种信封都接：
        {"code":0,"data":{...}} 和 {"result":"ok","detail":{...}}。
        """
        body = {self._id_param: self.file_id, **payload}
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="kdocs_", delete=False, encoding="utf-8"
        )
        try:
            with tmp:
                json.dump(body, tmp, ensure_ascii=False)
            cmd = [self.cli, service, action, "--file", tmp.name, "--timeout",
                   str(self.timeout * 1000)]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout + 30,
            )
        except subprocess.TimeoutExpired as e:
            raise KdocsSheetError(f"kdocs-cli {service}.{action} 超时：{e}") from e
        except FileNotFoundError as e:
            raise KdocsSheetError(f"找不到 kdocs-cli（{self.cli}），请先安装并认证") from e
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            raise KdocsSheetError(
                f"kdocs-cli {service}.{action} 退出码 {proc.returncode}："
                f"{(proc.stderr or out)[:500]}"
            )
        try:
            env = json.loads(out)
        except ValueError as e:
            raise KdocsSheetError(
                f"kdocs-cli {service}.{action} 输出不是 JSON：{out[:300]}"
            ) from e
        # 信封可能嵌套多层（{"code":0,"data":{"code":0,"data":{...}}}），逐层解到
        # 真正承载业务字段的那一层；任一层 code 非 0 都是业务错误。
        while isinstance(env, dict):
            code = env.get("code")
            if code not in (None, 0):
                raise KdocsSheetError(
                    f"kdocs-cli {service}.{action} 业务错误 code={code}: "
                    f"{_business_error_detail(env)}"
                )
            if env.get("result") not in (None, "ok") and "code" not in env:
                raise KdocsSheetError(f"kdocs-cli {service}.{action} 失败：{out[:300]}")
            inner = None
            for key in ("data", "detail"):
                if isinstance(env.get(key), (dict, list)):
                    inner = env[key]
                    break
            if inner is None:
                break
            env = inner
        return env

    def _run(self, service: str, action: str, payload: dict,
             retry_5xx: bool = False) -> dict:
        """带重试的调用：429001 等 _RATE_LIMIT_WAIT 秒后重试一次；retry_5xx=True 时
        HTTP 5xx（网关超时等）等 3s 重试一次。429002（熔断）与未知错误直接抛。

        retry_5xx 只能开给【幂等】操作：range_data_batch_update（同值或同格式写同格）
        和所有读操作。insert_rows_cols 非幂等（重试会重复插行），禁止开。
        """
        try:
            return self._run_once(service, action, payload)
        except KdocsSheetError as e:
            msg = str(e)
            if "429002" in msg:
                raise
            if "429001" in msg:
                logger.warning(f"kdocs-cli 限频，{_RATE_LIMIT_WAIT}s 后重试一次：{msg[:200]}")
                time.sleep(_RATE_LIMIT_WAIT)
                return self._run_once(service, action, payload)
            if retry_5xx and "HTTP 5" in msg:
                logger.warning(f"kdocs-cli 5xx，3s 后重试一次：{msg[:200]}")
                time.sleep(3)
                return self._run_once(service, action, payload)
            raise

    # ---- 工作表信息 ------------------------------------------------------

    def sheets_info(self, refresh: bool = False) -> Dict[str, dict]:
        """{工作表名: {"id": sheetId, "row_to": 数据区末行(0-based), "col_to": ...}}。"""
        if self._sheets is not None and not refresh:
            return self._sheets
        data = self._run("sheet", "get_sheets_info", {}, retry_5xx=True)
        infos = data.get("sheetsInfo") if isinstance(data, dict) else None
        if not isinstance(infos, list):
            raise KdocsSheetError(f"get_sheets_info 响应缺少 sheetsInfo：{str(data)[:200]}")
        self._sheets = {
            str(s.get("sheetName")): {
                "id": s.get("sheetId"),
                "row_to": s.get("rowTo", 0),
                "col_to": s.get("colTo", 0),
            }
            for s in infos if s.get("sheetName")
        }
        return self._sheets

    def sheet_names(self) -> List[str]:
        return list(self.sheets_info().keys())

    def _worksheet_id(self, sheet_name: str) -> int:
        info = self.sheets_info().get(sheet_name)
        if not info:
            raise KdocsSheetError(
                f"云端表格里找不到工作表「{sheet_name}」；现有：{self.sheet_names()}"
            )
        return int(info["id"])

    # ---- 读：表头 / 判重键 -----------------------------------------------

    def _get_range(self, sheet_name: str, row_from: int, row_to: int,
                   col_from: int, col_to: int) -> List[dict]:
        data = self._run("sheet", "get_range_data", {
            "worksheet_id": self._worksheet_id(sheet_name),
            "range": {"rowFrom": row_from, "rowTo": row_to,
                      "colFrom": col_from, "colTo": col_to},
        }, retry_5xx=True)
        cells = data.get("rangeData") if isinstance(data, dict) else None
        return cells if isinstance(cells, list) else []

    def read_header(self, sheet_name: str, max_scan: int = 3
                    ) -> Tuple[Dict[str, str], int]:
        """读表头 → ({列字母: 标题}, header_row 1-based)。口径同 detect_header_row。

        前 max_scan 行里【非空单元格最多】的那行算表头（登记表有的 Sheet 第 1 行是
        只占一格的跨列大标题，真表头在第 2 行）。
        """
        cells = self._get_range(sheet_name, 0, max_scan - 1, 0, _HEADER_SCAN_COLS - 1)
        by_row: Dict[int, Dict[int, str]] = {}
        for c in cells:
            text = str(c.get("cellText") or "").strip()
            if not text:
                continue
            by_row.setdefault(int(c["rowFrom"]), {})[int(c["colFrom"])] = text
        if not by_row:
            return {}, 1
        best_row = max(by_row, key=lambda r: (len(by_row[r]), -r))
        header = {index_to_col(ci): t for ci, t in sorted(by_row[best_row].items())}
        return header, best_row + 1

    def first_data_value(self, sheet_name: str, col: str, header_row: int) -> str:
        """读某列表头正下方第一个非空值，作增量水位。口径同本地同名方法。

        写入是 insert_at_top，所以数据区顶端那行就是上次登记的最新一条。
        """
        ci = col_to_index(col)
        row_from = header_row  # 0-based 的表头下一行
        row_to = min(
            int(self.sheets_info()[sheet_name].get("row_to", 0)), row_from + 199
        )
        if row_to < row_from:
            return ""
        cells = self._get_range(sheet_name, row_from, row_to, ci, ci)
        for c in sorted(cells, key=lambda x: int(x["rowFrom"])):
            text = str(c.get("cellText") or "").strip()
            if text:
                return text
        return ""

    def read_new_rows_column(self, sheet_name: str, col: str,
                             first_row: int, n: int) -> List[str]:
        """读【刚写入的 n 行】某列的值（自上而下），供写后确认用。

        first_row 是本批第一行的 1-based 行号（插顶端时 = header_row+1，追加时是末行之后），
        由调用方在写入前算好并复用——写入会让数据区增长，事后重算会读到本批之后的空白区。

        为什么单独开一个方法而不复用 existing_key_values：新行占据连续的 n 行、位置完全
        已知，只需读这 n 格。而 existing_key_values 会拉整列——采集管线逐商品写、每商品
        确认一次，500 行的表就是每商品传 500 格，一批 20 个白传一万格。次数一样、payload
        差两个数量级，而且表越大越亏。返回的 list 与写入顺序同序，缺失格补空串。
        """
        if n <= 0:
            return []
        ci = col_to_index(col)
        row_from = first_row - 1  # 1-based → 0-based
        cells = self._get_range(sheet_name, row_from, row_from + n - 1, ci, ci)
        by_row = {int(c["rowFrom"]): str(c.get("cellText") or "").strip()
                  for c in cells}
        return [by_row.get(row_from + i, "") for i in range(n)]

    def existing_key_values(self, sheet_name: str, col: str,
                            header_row: int) -> set:
        """读某列表头以下的所有值（strip 后非空），作增量水位。口径同本地同名方法。"""
        return {v for (v,) in self._read_columns(sheet_name, [col], header_row)}

    def existing_key_tuples(self, sheet_name: str, cols: List[str],
                            header_row: int) -> set:
        """读若干列的组合键集合（逐列 strip），口径同本地 existing_key_tuples。"""
        return set(self._read_columns(sheet_name, cols, header_row))

    def _read_columns(self, sheet_name: str, cols: List[str],
                      header_row: int) -> List[tuple]:
        """逐列读数据区（表头之下），按行对齐成 tuple 列表。

        行上限取 sheets_info 的 rowTo（实际数据区）与 _MAX_SCAN_ROWS 的较小者。
        只返回【至少有一列非空】的行——整列全空的尾部区域不产生垃圾键。
        """
        row_from = header_row  # 0-based 的表头下一行
        row_to = min(int(self.sheets_info()[sheet_name].get("row_to", 0)),
                     row_from + _MAX_SCAN_ROWS - 1)
        if row_to < row_from:
            return []
        per_col: Dict[str, Dict[int, str]] = {}
        for col in cols:
            ci = col_to_index(col)
            values: Dict[int, str] = {}
            for c in self._get_range(sheet_name, row_from, row_to, ci, ci):
                text = str(c.get("cellText") or "").strip()
                if text:
                    values[int(c["rowFrom"])] = text
            per_col[col] = values
        rows = sorted({r for v in per_col.values() for r in v})
        return [tuple(per_col[col].get(r, "") for col in cols) for r in rows]

    def _sample_col_to(self, sheet_name: str) -> int:
        """采样列上限（0-based）：取数据区 col_to，兜底/封顶 _HEADER_SCAN_COLS。"""
        try:
            col_to = int(self.sheets_info()[sheet_name].get("col_to", 0))
        except KeyError:
            col_to = 0
        if col_to <= 0:
            # 拿不到数据区列上限就按表头扫描宽度兜底，但留个痕迹——静默只采 A 列
            # 会让 schema 缺公式/常量列，新行成本利润公式缺失却很难排查。
            logger.warning(
                f"「{sheet_name}」读不到数据区列上限，公式/常量采样按 "
                f"{_HEADER_SCAN_COLS} 列兜底"
            )
            col_to = _HEADER_SCAN_COLS - 1
        return min(max(col_to, 0), _HEADER_SCAN_COLS - 1)

    def read_rows(self, sheet_name: str, row_from: int, row_to: int
                  ) -> Dict[int, Dict[str, dict]]:
        """读【任意行区间】→ {1-based 行号: {列字母: {"text","formula","format"}}}。

        row_from/row_to 都是 1-based 闭区间，会被夹到 >= 1。全空行不出现在结果里
        （调用方据此判断「这个窗口有没有真数据」）。

        为什么要按行号返回而不是像 read_data_sample 那样返回无名列表：采集管线要按
        【落点附近】学公式，既要知道公式原文、也要知道它原本在第几行，才能把本行引用
        换成占位符而不误伤跨行/绝对引用（见 pipeline._templatize_formula）。
        """
        row_from = max(int(row_from), 1)
        row_to = max(int(row_to), row_from)
        col_to = self._sample_col_to(sheet_name)
        cells = self._get_range(sheet_name, row_from - 1, row_to - 1, 0, col_to)
        by_row: Dict[int, Dict[str, dict]] = {}
        for c in cells:
            text = str(c.get("cellText") or "")
            formula = str(c.get("fmlaText") or "")
            cell_format = read_cell_xf(c)
            if not text and not formula and cell_format is None:
                continue
            by_row.setdefault(int(c["rowFrom"]) + 1, {})[
                index_to_col(int(c["colFrom"]))
            ] = {
                "text": text,
                "formula": formula,
                "format": cell_format,
            }
        return by_row

    def read_data_sample(self, sheet_name: str, header_row: int, max_rows: int = 10
                         ) -> List[Dict[str, dict]]:
        """读表头下前 max_rows 行数据区，返回 [{列字母: {"text": cellText, "formula": fmlaText或""}}]。

        fmlaText 是公式原文（含公式才返回），cellText 是显示值，format 是【已翻成写侧 xf】
        的数字格式与对齐（见 read_cell_xf——响应里没有 xf 这个键，必须翻译）。
        isCellPic 的 DISPIMG 单元格 formula 原样返回，由调用方过滤（坏行会把嵌入图落到普通列）。

        采集管线已改成按【落点附近】采样（见 pipeline.resolve_sheet_schema_cloud），
        不再走这个「固定取顶部」的入口；这里保留原语义供其它调用方与单测使用。
        """
        rows = self.read_rows(sheet_name, header_row + 1, header_row + max_rows)
        return [rows[r] for r in sorted(rows)]

    # ---- 写：插行 + 批量写值/图 ------------------------------------------

    def insert_rows_below_header(self, sheet_name: str, n: int, header_row: int) -> None:
        """在表头正下方插入 n 个空行（0-based 插入位置 = header_row）。"""
        self.insert_rows_at(sheet_name, n, header_row)

    def insert_rows_at(self, sheet_name: str, n: int, row_from: int) -> None:
        """在 0-based row_from 处插入 n 个空行（该行及以下整体下移）。

        采集页支持「从第 R 行开始插」，故插入点不再固定为表头正下方。
        """
        if n <= 0:
            return
        self._run("sheet", "insert_rows_cols", {
            "worksheet_id": self._worksheet_id(sheet_name),
            "type": "row", "row_from": row_from, "row_to": row_from + n - 1,
        })

    def data_end_row(self, sheet_name: str) -> int:
        """数据区末行（0-based），取自 sheets_info 的 rowTo。

        表尾追加要用它算落点。缓存里的 row_to 由 write_rows 写后本地增量维护
        （见那里的注释），故连续多批追加不必反复拉 get_sheets_info。
        """
        return int(self.sheets_info().get(sheet_name, {}).get("row_to", 0))

    def write_rows(self, sheet_name: str, rows: List[dict], header_row: int,
                   insert_at_top: bool = True,
                   first_row: Optional[int] = None) -> dict:
        """写入若干行（值 + 图片），返回 {written, images, images_failed, first_row}。

        insert_at_top=True（默认）插到表头正下方，新行在最上面——订单登记表的既定要求。
        False 则**追加到数据区末尾**：商品采集表习惯新品接在旧品后面，且它的判重是整列
        比对、不依赖行序（与订单靠「表头下第一条」当水位不同），所以两种都安全。
        追加分支不必插行——直接往末行之后的空白区写即可，还省掉一次 insert_rows_cols。

        first_row（1-based）显式指定落点，用于采集页的「从第 R 行向下/向上插」：给了它就
        按它插行，insert_at_top 只决定「要不要插行腾位」（给了 first_row 且要插行时，
        插入点就是 first_row，而不是表头正下方）。调用方须自行把 row_up 的减法算完
        （见 pipeline._cloud_first_row），这里只认最终落点。

        rows 的元素在 plan.rows 的 {values, image_column, image_path} 之上多带一个
        image_url（service 组装，见 service._write_plans）：云端嵌图走 URL 不走本地
        文件，本地 image_path 在这里用不上。

        顺序是刻意的【先文后图】（2026-08-04 实机 504 教训）：文本操作轻，一两个包
        就写完，文本落表（含判重键）这单就算登记成功；图片服务端要逐张按 URL 拉图，
        是网关超时的唯一来源，拆成 10 张一批的小包写在后面，单批失败只告警——
        宁可留几个空格，不让整批文本陪葬。
        """
        if not rows:
            return {"written": 0, "images": 0, "images_failed": 0, "first_row": None}
        if first_row is not None:
            # 显式落点（采集页「从第 R 行插」）：1-based → 0-based
            at = first_row - 1
            if insert_at_top:
                self.insert_rows_at(sheet_name, len(rows), at)
            first_row = at
        elif insert_at_top:
            self.insert_rows_below_header(sheet_name, len(rows), header_row)
            first_row = header_row  # 0-based：表头正下方
        else:
            # 追加：落在数据区末行之后。末行读不到（空表）时退回表头正下方，
            # 避免把首行写到表头上。
            end = self.data_end_row(sheet_name)
            first_row = max(end + 1, header_row)

        text_ops: List[dict] = []
        format_ops: List[dict] = []
        pic_ops: List[dict] = []
        for i, item in enumerate(rows):
            r = first_row + i  # 0-based 目标行
            written: Dict[str, Any] = {}
            for col, value in sorted(item.get("values", {}).items(),
                                     key=lambda kv: pipeline._col_key(kv[0])):
                if value is None or str(value) == "":
                    continue
                ci = col_to_index(col)
                written[col] = value
                text_ops.append({
                    "op_type": "cell_operation_type_formula",
                    "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                    "formula": force_text_input(value),
                })
            # 格式只回放到【本行真写了值/公式的格】：给空白格设格式除了多占 payload
            # 没有意义，而 kdocs 有配额（429001 限频要等 20s），一批 20 行 × 二十来列
            # 全设一遍是白花的调用量。文本格再摘掉数字格式（见 _writes_number）。
            for col, cell_format in sorted(item.get("formats", {}).items(),
                                           key=lambda kv: pipeline._col_key(kv[0])):
                if not isinstance(cell_format, dict) or not cell_format:
                    continue
                if col not in written:
                    continue
                xf = dict(cell_format)
                if "numfmt" in xf and not _writes_number(written[col]):
                    xf.pop("numfmt")
                if not xf:
                    continue
                ci = col_to_index(col)
                format_ops.append({
                    "op_type": "cell_operation_type_format",
                    "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                    "xf": xf,
                })
            url = pipeline.to_jpeg_url(str(item.get("image_url") or ""))
            if url and item.get("image_column"):
                ci = col_to_index(item["image_column"])
                pic_ops.append({
                    "op_type": "cell_operation_type_picture",
                    "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                    "cell_pic_info": {
                        "tag": "sheet_pic_type_url", "pic_content": url,
                        "width": _IMAGE_PX, "height": _IMAGE_PX,
                    },
                })

        wsid = self._worksheet_id(sheet_name)
        # 文本分批防单包过大：每批 500 个单元格操作
        for start in range(0, len(text_ops), 500):
            self._run("sheet", "range_data_batch_update", {
                "worksheet_id": wsid,
                "range_data": text_ops[start:start + 500],
            }, retry_5xx=True)

        # 云端追加落在空白行，天然没有模板行的百分比/货币/小数位。格式也统一走已经
        # 承担文本和图片写入的 range_data_batch_update，避免同一批数据混用两套参数风格。
        #
        # 格式写在文本之后，底层接口又没有事务：此处若把格式错误继续向上抛，上层会把
        # 【已经落表的值】误报成“未入库”，用户直接重跑就会产生重复行。故格式回放按
        # best-effort 处理并返回失败数；业务值仍会在后面的读回校验中独立确认。
        formats_failed = 0
        for start in range(0, len(format_ops), 500):
            batch = format_ops[start:start + 500]
            try:
                self._run("sheet", "range_data_batch_update", {
                    "worksheet_id": wsid,
                    "range_data": batch,
                }, retry_5xx=True)
            except KdocsSheetError as e:
                formats_failed += len(batch)
                logger.warning(
                    f"「{sheet_name}」第 {first_row + 1}~{first_row + len(rows)} 行"
                    f"格式回放失败（值已写入，继续做读回确认）：{e}"
                )

        images = 0
        images_failed = 0
        # 图片小包慢写：服务端逐张拉图，10 张一批不容易撞网关超时
        for start in range(0, len(pic_ops), 10):
            batch = pic_ops[start:start + 10]
            try:
                self._run("sheet", "range_data_batch_update", {
                    "worksheet_id": wsid, "range_data": batch,
                }, retry_5xx=True)
                images += len(batch)
            except KdocsSheetError as e:
                images_failed += len(batch)
                logger.warning(
                    f"「{sheet_name}」第 {start + 1}~{start + len(batch)} 张图写入失败"
                    f"（文本已登记，图片格留空）：{e}"
                )

        self._verify_top_row(sheet_name, rows[0], first_row)
        # 数据区行数变了，缓存的 row_to 已过期。但不必再跑一次 get_sheets_info——
        # 插行/追加都是精确 +n 行（追加分支落点本就由 row_to 算出），本地算即可。
        # 这一次省下的调用在【逐行写】场景下是每行一次（采集管线按商品逐行写协作文档，
        # 一批 20 个就是 20 次纯开销），而 kdocs 有配额、429001 限频要等 20s，省得值。
        # sheetId/sheetName 映射插行不会变，故整份缓存仍然有效，只需修 row_to。
        # 追加分支要按【实际落点】算末行：空表时 first_row 可能跳过了旧 row_to。
        info = (self._sheets or {}).get(sheet_name)
        if info is not None:
            grown = int(info.get("row_to", 0)) + len(rows)
            info["row_to"] = max(grown, first_row + len(rows) - 1)
        return {"written": len(rows), "images": images,
                "images_failed": images_failed, "formats_failed": formats_failed,
                "first_row": first_row}

    def _verify_top_row(self, sheet_name: str, first: dict, first_row: int) -> None:
        """读回新写区域第一行（0-based first_row），抽查一个非空值是否真的落上去了。"""
        expect = [(col_to_index(c), str(v)) for c, v in first.get("values", {}).items()
                  if str(v or "").strip()]
        if not expect:
            return
        ci, want = expect[0]
        cells = self._get_range(sheet_name, first_row, first_row, ci, ci)
        got = str(cells[0].get("cellText") or "").strip() if cells else ""
        if got != want.strip():
            raise KdocsSheetError(
                f"写入验证失败：「{sheet_name}」首行 {index_to_col(ci)} 列 "
                f"期望「{want}」实际「{got}」"
            )
