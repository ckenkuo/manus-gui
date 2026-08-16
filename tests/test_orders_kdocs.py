# -*- coding: utf-8 -*-
"""云端登记表后端（app/orders/kdocs_sheet.py）离线单测：不碰真 kdocs-cli。

subprocess.run 被 patch 成按 (service, action) 派发的 fake，fake 会读出 --file 里的
请求体供断言。钉死四件事：
  1. 列字母 ↔ 0-based 索引换算（云端 API 是 0-based，本地口径是列字母）。
  2. 表头探测口径与本地 detect_header_row 一致（前 3 行非空最多的一行）。
  3. 判重键按行对齐（两列分别读回，按行号配对）。
  4. write_rows 的调用序列：先插行 → 再批量写值/图（图片走 jpeg URL）→ 读回验证。
"""
import json
from pathlib import Path

import pytest

from app.orders import kdocs_sheet as K


def _resp(env: dict):
    """伪造 subprocess.run 的返回对象。"""
    class P:
        returncode = 0
        stdout = json.dumps(env, ensure_ascii=False)
        stderr = ""
    return P()


class FakeCli:
    """按 (service, action) 返回预置响应；记录每次调用的请求体。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []  # [(service, action, payload)]

    def __call__(self, cmd, **kwargs):
        service, action = cmd[1], cmd[2]
        payload = {}
        if "--file" in cmd:
            payload = json.loads(Path(cmd[cmd.index("--file") + 1]).read_text("utf-8"))
        self.calls.append((service, action, payload))
        env = self.routes[(service, action)]
        if callable(env):
            env = env(payload)
        return _resp(env)


def _make(monkeypatch, routes) -> K.KdocsSheet:
    fake = FakeCli(routes)
    monkeypatch.setattr(K.subprocess, "run", fake)
    cli = K.KdocsSheet("FILE1")
    cli._fake = fake
    return cli


SHEETS_INFO = {"sheetsInfo": [
    {"sheetName": "StoreA全球1", "sheetId": 7, "rowTo": 3, "colTo": 8, "isEmpty": False},
]}


def test_url_identifier_uses_url_param(monkeypatch):
    """粘贴的协作文档链接（http 开头）必须按 url 参数传给 CLI，不是 file_id。"""
    fake = FakeCli({("sheet", "get_sheets_info"): SHEETS_INFO})
    monkeypatch.setattr(K.subprocess, "run", fake)
    cli = K.KdocsSheet("https://www.kdocs.cn/l/abc123")
    assert cli._id_param == "url"
    cli.sheet_names()
    payload = fake.calls[0][2]
    assert payload.get("url") == "https://www.kdocs.cn/l/abc123"
    assert "file_id" not in payload


def test_col_index_roundtrip():
    assert K.col_to_index("A") == 0
    assert K.col_to_index("Z") == 25
    assert K.col_to_index("AA") == 26
    assert K.index_to_col(0) == "A"
    assert K.index_to_col(26) == "AA"
    for col in ("A", "C", "G", "AA", "AB"):
        assert K.index_to_col(K.col_to_index(col)) == col


def test_force_text_input():
    """22 位物流跟踪号等长数字串加单引号前缀强制按文本录入，短串/带字母/金额原样。"""
    # 长数字串（订单号/子订单号/物流跟踪号/SKUID 等）加 `'` 前缀
    assert K.force_text_input("9200190419690851234567") == "'9200190419690851234567"  # 22位
    assert K.force_text_input("211031661904287936") == "'211031661904287936"          # 18位
    assert K.force_text_input("920019041969") == "'920019041969"                     # 12位（阈值）
    # 前导零：哪怕很短也必须按文本（否则前导零会被抹掉）
    assert K.force_text_input("007") == "'007"
    assert K.force_text_input("01234") == "'01234"
    # 短数字串留成数值，让表格可以求和/筛选大于1（数量、金额）
    assert K.force_text_input("12.34") == "12.34"       # 金额，含小数点不是纯数字
    assert K.force_text_input("12") == "12"             # 价格整数部分，未达阈值
    assert K.force_text_input("3") == "3"               # 件数
    assert K.force_text_input(3) == "3"                 # int 原样转字符串，让表格按数值解析
    assert K.force_text_input(12.34) == "12.34"         # float
    # 带字母/连字符的本就不会被强转成数值，无需干预
    assert K.force_text_input("PO-211-03166038385273625") == "PO-211-03166038385273625"
    assert K.force_text_input("GFUS01029661553153") == "GFUS01029661553153"
    # 已有前缀的不重复加（防调用方二次处理）
    assert K.force_text_input("'920019041969") == "'920019041969"
    assert K.force_text_input("'12") == "'12"


def test_read_cell_xf_translates_read_fields_to_write_fields():
    """读侧与写侧的格式字段根本不是一套，必须翻译。

    2026-08-12 实测：get_range_data 返回的是 numFormat / alignment{horizontal,vertical}，
    响应里【没有】xf 这个键；而 update_range_data 要的是 numfmt（全小写）+ alcH/alcV。
    此前代码直接取 cell["xf"] 原样回写，于是格式字典恒空，采集写的新行永远拿不到
    历史行的两位小数/百分比（历史行显示 25.00 / 16.05%，新行显示 1 / 0）。
    """
    assert K.read_cell_xf({
        "numFormat": "0.00_ ",
        "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"},
    }) == {"numfmt": "0.00_ ", "alcH": 2, "alcV": 1}
    # 百分比列
    assert K.read_cell_xf({"numFormat": "0.00%"}) == {"numfmt": "0.00%"}
    # 尾随空格是格式串的一部分（`_ ` 是对齐占位），不能被 strip 掉
    assert K.read_cell_xf({"numFormat": "0.00_ "})["numfmt"] == "0.00_ "
    # 通用格式 = 没设过，回放它没有意义
    assert K.read_cell_xf({"numFormat": "G/通用格式"}) is None
    assert K.read_cell_xf({"numFormat": "General"}) is None
    assert K.read_cell_xf({}) is None
    # haGeneral 没有对应的 alcH 数字（就是"没设过"），只留数字格式
    assert K.read_cell_xf({
        "numFormat": "0.00", "alignment": {"horizontal": "haGeneral"},
    }) == {"numfmt": "0.00"}
    # 只有对齐、没有数字格式的格也要回放对齐
    assert K.read_cell_xf({"alignment": {"vertical": "vaTop"}}) == {"alcV": 0}


def test_read_header_picks_fullest_row(monkeypatch):
    # 第 1 行只有一个跨列大标题，第 2 行才是真表头（与本地 detect_header_row 同口径）
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "get_range_data"): {"rangeData": [
            {"rowFrom": 0, "colFrom": 0, "cellText": "订单登记表"},
            {"rowFrom": 1, "colFrom": 0, "cellText": "订单店铺"},
            {"rowFrom": 1, "colFrom": 2, "cellText": "订单号"},
            {"rowFrom": 1, "colFrom": 3, "cellText": "尺码"},
            {"rowFrom": 1, "colFrom": 6, "cellText": "产品图片"},
        ]},
    }
    cli = _make(monkeypatch, routes)
    header, header_row = cli.read_header("StoreA全球1")
    assert header_row == 2
    assert header == {"A": "订单店铺", "C": "订单号", "D": "尺码", "G": "产品图片"}


def test_existing_key_tuples_aligns_by_row(monkeypatch):
    def range_data(payload):
        rng = payload["range"]
        col = rng["colFrom"]
        # 订单号列(C=2)：行2、行3有值；尺码列(D=3)：只有行2有值
        cells = {
            2: [{"rowFrom": 2, "colFrom": 2, "cellText": "PO-1"},
                {"rowFrom": 3, "colFrom": 2, "cellText": "PO-2"}],
            3: [{"rowFrom": 2, "colFrom": 3, "cellText": "杏色 / 3-4Y"}],
        }.get(col, [])
        return {"rangeData": cells}

    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "get_range_data"): range_data,
    }
    cli = _make(monkeypatch, routes)
    keys = cli.existing_key_tuples("StoreA全球1", ["C", "D"], header_row=2)
    assert keys == {("PO-1", "杏色 / 3-4Y"), ("PO-2", "")}
    values = cli.existing_key_values("StoreA全球1", col="C", header_row=2)
    assert values == {"PO-1", "PO-2"}


def test_write_rows_sequence_and_payload(monkeypatch):
    verify = {"rangeData": [{"rowFrom": 2, "colFrom": 2, "cellText": "PO-9"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    rows = [{
        "values": {"C": "PO-9", "D": "黑色 / 5Y", "E": 2},
        "image_column": "G",
        "image_url": "https://img.kwcdn.com/a.jpg?imageView2/2/w/800/q/70/format/avif",
    }]
    res = cli.write_rows("StoreA全球1", rows, header_row=2)
    # first_row 是新增的返回字段（0-based 落点）：订单默认 insert_at_top，落在表头
    # （第 2 行）正下方 = 0-based 2，行为与采集加落点开关之前一致。
    assert res == {"written": 1, "images": 1, "images_failed": 0, "first_row": 2}

    kinds = [a for _, a, _ in cli._fake.calls if a != "get_sheets_info"]
    # 插行 → 写文本 → 写图片（先文后图）→ 读回验证
    assert kinds == ["insert_rows_cols", "range_data_batch_update",
                     "range_data_batch_update", "get_range_data"]

    calls = [c for c in cli._fake.calls if c[1] != "get_sheets_info"]
    insert = calls[0][2]
    assert insert["type"] == "row"
    assert (insert["row_from"], insert["row_to"]) == (2, 2)  # 表头(行2)正下方, 0-based

    ops = calls[1][2]["range_data"]
    formulas = [o for o in ops if o["op_type"] == "cell_operation_type_formula"]
    pics = [o for o in ops if o["op_type"] == "cell_operation_type_picture"]
    assert pics == []  # 文本包不带图
    # 数量字段（int/short str）不加前缀，让表格保持数值语义可求和
    assert {(o["col_from"], o["formula"]) for o in formulas} == {
        (2, "PO-9"), (3, "黑色 / 5Y"), (4, "2")}
    assert all(o["row_from"] == 2 for o in ops)

    pic_ops = calls[2][2]["range_data"]
    assert [o["op_type"] for o in pic_ops] == ["cell_operation_type_picture"]
    # avif 必须转 jpeg，否则云端渲染不出
    assert pic_ops[0]["cell_pic_info"]["tag"] == "sheet_pic_type_url"
    assert "format/jpeg" in pic_ops[0]["cell_pic_info"]["pic_content"]
    assert pic_ops[0]["col_from"] == 6  # G 列


def test_write_rows_forces_text_for_long_digits(monkeypatch):
    """22 位物流跟踪号等长数字串加 `'` 前缀，防科学计数法丢精度（实机验证 2026-08-07）。"""
    verify = {"rangeData": [{"rowFrom": 2, "colFrom": 2, "cellText": "211031661904287936"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    rows = [{
        "values": {
            "C": "211031661904287936",  # 18 位子订单号 → 加前缀
            "D": "9200190419690851234567",  # 22 位跟踪号 → 加前缀
            "E": "12.34",  # 金额 → 原样（含小数点，不是纯数字）
            "F": 3,  # 件数 int → 原样转字符串
        },
    }]
    cli.write_rows("StoreA全球1", rows, header_row=2)
    calls = [c for c in cli._fake.calls if c[1] == "range_data_batch_update"]
    text_ops = calls[0][2]["range_data"]
    formulas = {o["col_from"]: o["formula"] for o in text_ops}
    # 长数字串带前缀 `'`（kdocs 读回的 cellText 不含前缀，验证用原始值 → 能过）
    assert formulas[2] == "'211031661904287936"
    assert formulas[3] == "'9200190419690851234567"
    # 金额/数量不加（保持数值语义，能求和）
    assert formulas[4] == "12.34"
    assert formulas[5] == "3"


def test_write_rows_verify_mismatch_raises(monkeypatch):
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): {"rangeData": []},  # 读回是空的
    }
    cli = _make(monkeypatch, routes)
    with pytest.raises(K.KdocsSheetError, match="验证失败"):
        cli.write_rows("StoreA全球1", [{"values": {"C": "PO-9"}}], header_row=2)


def test_business_error_raises(monkeypatch):
    routes = {("sheet", "get_sheets_info"): {"code": 400006, "msg": "鉴权失败"}}
    cli = _make(monkeypatch, routes)
    with pytest.raises(K.KdocsSheetError, match="400006"):
        cli.sheet_names()


def test_missing_sheet_raises(monkeypatch):
    cli = _make(monkeypatch, {("sheet", "get_sheets_info"): SHEETS_INFO})
    with pytest.raises(K.KdocsSheetError, match="找不到工作表"):
        cli.read_header("不存在的表")


def test_rate_limit_retried_once(monkeypatch):
    calls = {"n": 0}

    class FlakyCli(FakeCli):
        def __call__(self, cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp({"code": 429001, "msg": "限频"})
            return super().__call__(cmd, **kwargs)

    monkeypatch.setattr(K, "_RATE_LIMIT_WAIT", 0)
    fake = FlakyCli({("sheet", "get_sheets_info"): SHEETS_INFO})
    monkeypatch.setattr(K.subprocess, "run", fake)
    cli = K.KdocsSheet("FILE1")
    assert cli.sheet_names() == ["StoreA全球1"]
    assert calls["n"] == 2


def test_5xx_retried_only_for_idempotent(monkeypatch, tmp_path):
    """504 网关超时：幂等操作（批量写）重试一次；非幂等（插行）不重试。"""
    calls = []

    class Gateway504:
        def __call__(self, cmd, **kwargs):
            service, action = cmd[1], cmd[2]
            calls.append(action)
            payload = {}
            if "--file" in cmd:
                payload = json.loads(Path(cmd[cmd.index("--file") + 1]).read_text("utf-8"))
            if action == "range_data_batch_update" and calls.count(action) == 1:
                class P:
                    returncode = 1
                    stdout = ""
                    stderr = "Error: HTTP 504: Gateway Time-out"
                return P()
            if action == "insert_rows_cols":
                class P:
                    returncode = 1
                    stdout = ""
                    stderr = "Error: HTTP 504: Gateway Time-out"
                return P()
            return _resp({"code": 0})

    monkeypatch.setattr(K.subprocess, "run", Gateway504())
    monkeypatch.setattr(K.time, "sleep", lambda s: None)
    cli = K.KdocsSheet("FILE1")
    cli._sheets = {"StoreA全球1": {"id": 7, "row_to": 100, "col_to": 10}}

    # 批量写：504 → 重试成功
    cli._run("sheet", "range_data_batch_update", {"worksheet_id": 7, "range_data": []},
             retry_5xx=True)
    assert calls.count("range_data_batch_update") == 2

    # 插行：非幂等，504 直接抛、不重试
    with pytest.raises(K.KdocsSheetError, match="504"):
        cli.insert_rows_below_header("StoreA全球1", 3, header_row=1)
    assert calls.count("insert_rows_cols") == 1
