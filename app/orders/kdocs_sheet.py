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
                    f"kdocs-cli {service}.{action} 业务错误 code={code}: {env.get('msg')}"
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

        retry_5xx 只能开给【幂等】操作：range_data_batch_update（同值写同格）和
        所有读操作。insert_rows_cols 非幂等（重试会重复插行），禁止开。
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

    def read_data_sample(self, sheet_name: str, header_row: int, max_rows: int = 10
                         ) -> List[Dict[str, dict]]:
        """读表头下前 max_rows 行数据区，返回 [{列字母: {"text": cellText, "formula": fmlaText或""}}]。

        供云端模式学公式模板/常量列用（采集管线 resolve_sheet_schema_cloud）：
        fmlaText 是公式原文（含公式才返回），cellText 是显示值。isCellPic 的 DISPIMG
        单元格 formula 原样返回，由调用方过滤（坏行会把嵌入图落到普通列）。
        列上限取 sheets_info 的 col_to，兜底/封顶 _HEADER_SCAN_COLS。
        """
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
        col_to = min(max(col_to, 0), _HEADER_SCAN_COLS - 1)
        row_from = header_row  # 0-based 的表头下一行
        cells = self._get_range(sheet_name, row_from, row_from + max_rows - 1, 0, col_to)
        by_row: Dict[int, Dict[str, dict]] = {}
        for c in cells:
            text = str(c.get("cellText") or "")
            formula = str(c.get("fmlaText") or "")
            if not text and not formula:
                continue
            by_row.setdefault(int(c["rowFrom"]), {})[index_to_col(int(c["colFrom"]))] = {
                "text": text, "formula": formula,
            }
        return [by_row[r] for r in sorted(by_row)]

    # ---- 写：插行 + 批量写值/图 ------------------------------------------

    def insert_rows_below_header(self, sheet_name: str, n: int, header_row: int) -> None:
        """在表头正下方插入 n 个空行（0-based 插入位置 = header_row）。"""
        if n <= 0:
            return
        self._run("sheet", "insert_rows_cols", {
            "worksheet_id": self._worksheet_id(sheet_name),
            "type": "row", "row_from": header_row, "row_to": header_row + n - 1,
        })

    def write_rows(self, sheet_name: str, rows: List[dict], header_row: int) -> dict:
        """把 plan.rows 插到表头正下方并写入值与图片，返回 {written, images, images_failed}。

        rows 的元素在 plan.rows 的 {values, image_column, image_path} 之上多带一个
        image_url（service 组装，见 service._write_plans）：云端嵌图走 URL 不走本地
        文件，本地 image_path 在这里用不上。

        顺序是刻意的【先文后图】（2026-08-04 实机 504 教训）：文本操作轻，一两个包
        就写完，文本落表（含判重键）这单就算登记成功；图片服务端要逐张按 URL 拉图，
        是网关超时的唯一来源，拆成 10 张一批的小包写在后面，单批失败只告警——
        宁可留几个空格，不让整批文本陪葬。
        """
        if not rows:
            return {"written": 0, "images": 0, "images_failed": 0}
        self.insert_rows_below_header(sheet_name, len(rows), header_row)

        text_ops: List[dict] = []
        pic_ops: List[dict] = []
        for i, item in enumerate(rows):
            r = header_row + i  # 0-based 目标行
            for col, value in sorted(item.get("values", {}).items(),
                                     key=lambda kv: pipeline._col_key(kv[0])):
                if value is None or str(value) == "":
                    continue
                ci = col_to_index(col)
                text_ops.append({
                    "op_type": "cell_operation_type_formula",
                    "row_from": r, "row_to": r, "col_from": ci, "col_to": ci,
                    "formula": str(value),
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

        self._verify_top_row(sheet_name, rows[0], header_row)
        # 数据区行数变了，缓存的 row_to 已过期
        self.sheets_info(refresh=True)
        return {"written": len(rows), "images": images, "images_failed": images_failed}

    def _verify_top_row(self, sheet_name: str, first: dict, header_row: int) -> None:
        """读回新写区域第一行，抽查一个非空值是否真的落上去了。"""
        expect = [(col_to_index(c), str(v)) for c, v in first.get("values", {}).items()
                  if str(v or "").strip()]
        if not expect:
            return
        ci, want = expect[0]
        cells = self._get_range(sheet_name, header_row, header_row, ci, ci)
        got = str(cells[0].get("cellText") or "").strip() if cells else ""
        if got != want.strip():
            raise KdocsSheetError(
                f"写入验证失败：「{sheet_name}」首行 {index_to_col(ci)} 列 "
                f"期望「{want}」实际「{got}」"
            )
