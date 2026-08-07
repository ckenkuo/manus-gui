# -*- coding: utf-8 -*-
"""商品采集管线的云端协作文档模式离线单测：不碰真 kdocs-cli。

复刻 tests/test_orders_kdocs.py 的 FakeCli 模式（patch app.orders.kdocs_sheet 的
subprocess.run，按 (service, action) 派发，读 --file payload 断言）。钉死五件事：
  1. KdocsSheet.read_data_sample：rangeData → 按行聚合、fmlaText 提取。
  2. resolve_sheet_schema_cloud：表头→字段映射（SPU 在非常规列）、缺 SPU → ok=False、
     公式 {r} 模板化、DISPIMG/图片列排除、常量列识别。
  3. write_product_row_cloud：插行 → 文本批（值+公式，行号=header_row+1）→ 图片批 → 读回校验。
  4. run_batch 云端分支：云端判重/写行、跳过本地锁预检；无云端目标时本地路径回归；
     显式本地路径压制 prefs 里的云端链接；prefs 云端链接跨批次保留。
  5. save_prefs/load_prefs：链接/file_id ↔ cloud_url 拆分与回填。
  6. resolve_cloud 优先级：显式链接 > 显式本地（→None）> cloud_url 参数 > prefs > config。
  7. KdocsSheet 链接判定大小写不敏感（HTTPS:// 也按 url 传参）。
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.collect import pipeline as P
from app.collect import service as S
from app.orders import kdocs_sheet as K


def _resp(env: dict):
    """伪造 subprocess.run 的返回对象。"""
    class Proc:
        returncode = 0
        stdout = json.dumps(env, ensure_ascii=False)
        stderr = ""
    return Proc()


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
    {"sheetName": "pawly全球", "sheetId": 3, "rowTo": 5, "colTo": 12, "isEmpty": False},
]}

# 表头（行 1）：SPU 刻意放在 C（非常规列），D 是图片列，J/K/L 分别是常量/公式/公式列
HEADER_CELLS = [
    {"rowFrom": 0, "colFrom": 0, "cellText": "站点"},
    {"rowFrom": 0, "colFrom": 1, "cellText": "类目"},
    {"rowFrom": 0, "colFrom": 2, "cellText": "SPU"},
    {"rowFrom": 0, "colFrom": 3, "cellText": "产品图片"},
    {"rowFrom": 0, "colFrom": 4, "cellText": "销售价"},
    {"rowFrom": 0, "colFrom": 5, "cellText": "日常价"},
    {"rowFrom": 0, "colFrom": 6, "cellText": "采购价"},
    {"rowFrom": 0, "colFrom": 7, "cellText": "重量"},
    {"rowFrom": 0, "colFrom": 8, "cellText": "ros"},
    {"rowFrom": 0, "colFrom": 9, "cellText": "操作费"},
    {"rowFrom": 0, "colFrom": 10, "cellText": "成本"},
    {"rowFrom": 0, "colFrom": 11, "cellText": "折扣"},
]

# 数据区采样（0-based 行 1、2）：K/L 是公式列，D 是 DISPIMG 嵌入图（坏行也要排除），
# J（操作费）每行都是 5 → 常量列
SAMPLE_CELLS = [
    {"rowFrom": 1, "colFrom": 2, "cellText": "SPU-1"},
    {"rowFrom": 1, "colFrom": 3, "cellText": "",
     "fmlaText": '=_xlfn.DISPIMG("ID_aaa",1)', "isCellPic": True},
    {"rowFrom": 1, "colFrom": 4, "cellText": "12.5"},
    {"rowFrom": 1, "colFrom": 6, "cellText": "3.2"},
    {"rowFrom": 1, "colFrom": 7, "cellText": "0.3"},
    {"rowFrom": 1, "colFrom": 8, "cellText": "6"},
    {"rowFrom": 1, "colFrom": 9, "cellText": "5"},
    {"rowFrom": 1, "colFrom": 10, "cellText": "33.2", "fmlaText": "=G2+J2+H2*80"},
    {"rowFrom": 1, "colFrom": 11, "cellText": "1", "fmlaText": "=E2/F2"},
    {"rowFrom": 2, "colFrom": 2, "cellText": "SPU-2"},
    {"rowFrom": 2, "colFrom": 9, "cellText": "5"},
    {"rowFrom": 2, "colFrom": 10, "cellText": "41.2", "fmlaText": "=G3+J3+H3*80"},
]


def _range_router(payload):
    """按请求的行范围分发：表头区（rowFrom=0）vs 数据采样区。"""
    rng = payload["range"]
    cells = HEADER_CELLS if rng["rowFrom"] == 0 else SAMPLE_CELLS
    return {"rangeData": cells}


# ---- read_data_sample -------------------------------------------------------


def test_read_data_sample_aggregates_rows_and_formulas(monkeypatch):
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "get_range_data"): _range_router,
    }
    cli = _make(monkeypatch, routes)
    sample = cli.read_data_sample("pawly全球", header_row=1)

    assert len(sample) == 2  # 两行采样，按行聚合
    row1 = sample[0]
    assert row1["C"]["text"] == "SPU-1"
    assert row1["K"]["formula"] == "=G2+J2+H2*80"
    assert row1["K"]["text"] == "33.2"
    assert row1["D"]["formula"].startswith("=_xlfn.DISPIMG")  # 原样返回，调用方过滤
    # 请求的列上限取 sheets_info 的 colTo（12）
    rng_payloads = [p for s, a, p in cli._fake.calls if a == "get_range_data"]
    assert rng_payloads[0]["range"]["colTo"] == 12


# ---- resolve_sheet_schema_cloud --------------------------------------------


def _resolve(monkeypatch, routes=None):
    cli = _make(monkeypatch, routes or {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "get_range_data"): _range_router,
    })
    return asyncio.run(P.resolve_sheet_schema_cloud(cli, "pawly全球"))


def test_schema_cloud_maps_fields_formulas_constants(monkeypatch):
    schema = _resolve(monkeypatch)

    assert schema.ok and schema.header_row == 1
    # 表头→字段映射与本地同一纯函数：SPU 在 C、图片在 D
    assert schema.fields["spu"] == "C"
    assert schema.fields["image"] == "D"
    assert schema.fields["purchase"] == "G"
    # 公式列行号模板化；图片列（DISPIMG）被排除
    assert schema.formula_columns == {"K": "=G{r}+J{r}+H{r}*80", "L": "=E{r}/F{r}"}
    # 常量列：公式引用到、非公式列、非逐商品字段列，且采样行里全是同一个纯数字
    assert schema.constant_columns == {"J": 5}


def test_schema_cloud_rejects_missing_spu(monkeypatch):
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "get_range_data"): {"rangeData": [
            {"rowFrom": 0, "colFrom": 0, "cellText": "站点"},
            {"rowFrom": 0, "colFrom": 1, "cellText": "类目"},
        ]},
    }
    schema = _resolve(monkeypatch, routes)

    assert not schema.ok
    assert "SPU" in schema.error


# ---- write_product_row_cloud -------------------------------------------------


def _schema() -> P.SheetSchema:
    return P.SheetSchema(
        sheet="pawly全球",
        fields={"site": "A", "category": "B", "spu": "C", "image": "D",
                "sale": "E", "daily": "F", "purchase": "G", "weight": "H",
                "ros": "I"},
        formula_columns={"K": "=G{r}+J{r}+H{r}*80"},
        constant_columns={"J": 5},
        ok=True,
        header_row=1,
    )


def test_write_row_cloud_sequence_and_payload(monkeypatch):
    """插顶端（insert_at_top=True）：插行 → 文本 → 图片 → 读回，公式行号 = 表头下一行。"""
    verify = {"rangeData": [{"rowFrom": 1, "colFrom": 0, "cellText": "美国"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    item = {"spu": "SPU-9", "site": "美国", "category": "玩具", "price": "¥12.50",
            "image": "https://img.kwcdn.com/a.jpg?imageView2/2/w/800/format/avif"}
    res = P.CollectResult(spu="SPU-9", ok=True, purchase_price=3.2, shipping=0.8,
                          weight_g=300)

    ok, msg = asyncio.run(P.write_product_row_cloud(
        cli, "pawly全球", item, res, _schema(), insert_at_top=True))
    assert ok, msg

    kinds = [a for _, a, _ in cli._fake.calls if a != "get_sheets_info"]
    # 插行 → 文本批（值+公式）→ 图片批（先文后图）→ 读回校验
    assert kinds == ["insert_rows_cols", "range_data_batch_update",
                     "range_data_batch_update", "get_range_data"]

    calls = [c for c in cli._fake.calls if c[1] != "get_sheets_info"]
    insert = calls[0][2]
    assert (insert["row_from"], insert["row_to"]) == (1, 1)  # 表头(行1)正下方, 0-based

    ops = calls[1][2]["range_data"]
    by_col = {K.index_to_col(o["col_from"]): o["formula"] for o in ops}
    assert all(o["op_type"] == "cell_operation_type_formula" for o in ops)
    assert all(o["row_from"] == 1 for o in ops)
    assert by_col["A"] == "美国" and by_col["C"] == "SPU-9"
    assert by_col["E"] == "12.5"           # 售价已剥 ¥
    assert by_col["G"] == "4.0"            # 采购价 = 货价 3.2 + 运费 0.8
    assert by_col["H"] == "0.3"            # 重量 300g → 0.3kg
    assert by_col["J"] == "5"              # 常量列回填
    assert by_col["I"] == "7"              # ros 非常量 → 默认值兜底
    assert by_col["K"] == "=G2+J2+H2*80"   # 公式行号 = header_row + 1 = 2

    pic_ops = calls[2][2]["range_data"]
    assert [o["op_type"] for o in pic_ops] == ["cell_operation_type_picture"]
    assert K.index_to_col(pic_ops[0]["col_from"]) == "D"  # 图片列
    assert "format/jpeg" in pic_ops[0]["cell_pic_info"]["pic_content"]  # avif→jpeg


def test_write_row_cloud_append_bottom_skips_insert(monkeypatch):
    """默认追加到末尾：不发 insert_rows_cols（省一次调用），落点 = 数据区末行之后。

    SHEETS_INFO 的 rowTo=5（0-based）→ 新行落 0-based 第 6 行、公式行号 1-based = 7。
    """
    verify = {"rangeData": [{"rowFrom": 6, "colFrom": 0, "cellText": "美国"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    item = {"spu": "SPU-9", "site": "美国", "category": "玩具", "price": "¥12.50",
            "image": "https://img.kwcdn.com/a.jpg"}
    res = P.CollectResult(spu="SPU-9", ok=True, purchase_price=3.2, shipping=0.8,
                          weight_g=300)

    ok, msg = asyncio.run(
        P.write_product_row_cloud(cli, "pawly全球", item, res, _schema())
    )
    assert ok, msg

    kinds = [a for _, a, _ in cli._fake.calls if a != "get_sheets_info"]
    assert "insert_rows_cols" not in kinds, "追加不该插行"
    assert kinds == ["range_data_batch_update", "range_data_batch_update",
                     "get_range_data"]

    calls = [c for c in cli._fake.calls if c[1] != "get_sheets_info"]
    ops = calls[0][2]["range_data"]
    assert all(o["row_from"] == 6 for o in ops), "0-based 落在末行(5)之后"
    by_col = {K.index_to_col(o["col_from"]): o["formula"] for o in ops}
    assert by_col["K"] == "=G7+J7+H7*80", "公式行号跟随实际落点(1-based 7)"


def test_write_row_cloud_error_is_per_product(monkeypatch):
    """云端写失败（KdocsSheetError）只让本商品失败、不连坐（与本地同口径）。"""
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 400001, "msg": "boom"},
        ("sheet", "get_range_data"): {"rangeData": []},
    }
    cli = _make(monkeypatch, routes)
    res = P.CollectResult(spu="SPU-9", ok=True)

    ok, msg = asyncio.run(
        P.write_product_row_cloud(cli, "pawly全球", {"spu": "SPU-9"}, res, _schema())
    )
    assert not ok
    assert "写入失败" in msg and "boom" in msg


def test_write_row_cloud_aborts_on_bad_schema(monkeypatch):
    cli = _make(monkeypatch, {})
    bad = P.SheetSchema(sheet="pawly全球", error="缺 SPU 列")
    ok, msg = asyncio.run(
        P.write_product_row_cloud(cli, "pawly全球", {"spu": "X"},
                                  P.CollectResult(spu="X", ok=True), bad)
    )
    assert not ok and "SPU" in msg
    assert cli._fake.calls == []  # 结构不可写 → 一次云端调用都不该发


# ---- run_batch 云端/本地路由 ---------------------------------------------------


class FakeCloud:
    """run_batch 云端分支用的假后端：记录判重读与批量写，sheet_names/read_header 供首屏用。"""

    def __init__(self, sheets=("pawly全球",), file_id="https://www.kdocs.cn/l/abc123"):
        self._sheets = list(sheets)
        self.file_id = file_id  # run_batch 记 prefs/打日志都取它（对齐 KdocsSheet）
        self.key_reads = []  # [(sheet, col, header_row)]
        self.writes = []  # [(sheet, rows, header_row, insert_at_top)]，基础模式走整批写
        self.new_row_reads = []  # [(sheet, col, first_row, n)]，写后确认只读新行区
        self.row_to = 5  # 0-based 数据区末行，追加落点靠它算；write_rows 后增长

    def sheet_names(self):
        return self._sheets

    def read_header(self, sheet):
        return {"C": "SPU"}, 1

    def existing_key_values(self, sheet, col, header_row):
        self.key_reads.append((sheet, col, header_row))
        return set()

    def data_end_row(self, sheet):
        """数据区末行（0-based）。默认 5 行历史数据，供追加落点计算。"""
        return self.row_to

    def write_rows(self, sheet, rows, header_row, insert_at_top=True,
                   first_row=None):
        self.writes.append((sheet, list(rows), header_row, insert_at_top, first_row))
        self.row_to += len(rows)  # 与真实实现一致：写后数据区增长
        return {"written": len(rows), "images": 0, "images_failed": 0,
                "first_row": header_row if insert_at_top else self.row_to}

    def read_new_rows_column(self, sheet, col, first_row, n):
        """按写入顺序回放 SPU，模拟服务端已落值（供写后确认比对）。"""
        self.new_row_reads.append((sheet, col, first_row, n))
        out = []
        for _s, rows, _hr, _top, _fr in self.writes:
            for r in rows:
                out.append(str(r["values"].get(col, "")))
        return out[:n]


def _patch_common(monkeypatch, tmp_path, cloud):
    """把 run_batch 的外部依赖全换成假的（清单/prefs/结构解析/单商品写入）。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_worklist",
                        lambda: [{"spu": "S1", "name": "n", "mallid": "m"}])
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "cloud_backend", lambda cfg, cloud_url="": cloud)
    seen = {}

    async def fake_base(item, excel, sheet, schema=None, cloud=None,
                        append_mode="bottom", append_row=None):
        seen["cloud"] = cloud
        seen["schema"] = schema
        seen["append_mode"] = append_mode
        return S.CollectOutcome(spu=str(item["spu"]), ok=True, status="base", via="base")

    monkeypatch.setattr(S, "collect_one_base", fake_base)
    return seen


def test_run_batch_cloud_branch(monkeypatch, tmp_path):
    """云端目标：走云端 schema/判重/写行，跳过本地锁预检。"""
    cloud = FakeCloud()
    seen = _patch_common(monkeypatch, tmp_path, cloud)

    async def fake_schema_cloud(c, sheet):
        assert c is cloud
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True, header_row=1)

    monkeypatch.setattr(P, "resolve_sheet_schema_cloud", fake_schema_cloud)

    def locked(_path):
        raise AssertionError("云端模式不该做本地锁预检")

    monkeypatch.setattr(S, "excel_write_locked", locked)

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True,
        excel="https://www.kdocs.cn/l/abc123", sheet="pawly全球",
    ))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert cloud.key_reads == [("pawly全球", "C", 1)], "判重要读云端的 SPU 列"
    # 基础模式云端走【整批一次写】，不再逐商品调 collect_one_base
    assert len(cloud.writes) == 1, "整批只该发一次 write_rows"
    assert cloud.writes[0][0] == "pawly全球"
    assert [r["values"]["C"] for r in cloud.writes[0][1]] == ["S1"]
    # 写后确认只读新行区那 n 格，不再拉整列（key_reads 只有批次开始那一次判重）。
    # 默认落点是 bottom：FakeCloud 的 row_to=5（0-based）→ 新行 1-based 第 7 行。
    assert cloud.new_row_reads == [("pawly全球", "C", 7, 1)]
    assert cloud.writes[0][3] is False, "默认 bottom → insert_at_top=False"
    # 链接被拆到 cloud_url 键（对齐 orders 的 prefs 拆分）
    assert S.load_prefs()["cloud_url"] == "https://www.kdocs.cn/l/abc123"


def test_run_batch_local_path_unchanged(monkeypatch, tmp_path):
    """无云端目标：本地路径回归——锁预检照常、本地判重、collect_one_base 不带 cloud。"""
    seen = _patch_common(monkeypatch, tmp_path, None)
    lock_checked = {"n": 0}

    def locked(_path):
        lock_checked["n"] += 1
        return False

    monkeypatch.setattr(S, "excel_write_locked", locked)
    monkeypatch.setattr(S, "spu_col_of", lambda e, s: "C")
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c: set()),
    )

    async def fake_schema(tool, excel, sheet):
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True)

    monkeypatch.setattr(P, "resolve_sheet_schema", fake_schema)

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="D:/wb.xlsx", sheet="pawly全球",
    ))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert lock_checked["n"] == 1, "本地模式必须保留锁预检"
    assert seen["cloud"] is None


def test_run_batch_cloud_rejects_pure_agent(monkeypatch, tmp_path):
    """云端 + 纯 agent 兜底路径（非管道）不适用：开头中止并提示。"""
    _patch_common(monkeypatch, tmp_path, FakeCloud())
    events = []

    res = asyncio.run(S.run_batch(
        limit=5, base_only=False, use_pipeline=False,
        excel="https://www.kdocs.cn/l/abc123", sheet="pawly全球",
        on_progress=lambda e: events.append(e),
    ))

    assert res == {"ok": 0, "fail": 0, "batch": 0}
    assert events and events[0]["type"] == "aborted"
    assert "agent" in events[0]["reason"]


def test_run_batch_explicit_local_overrides_prefs_cloud(monkeypatch, tmp_path):
    """prefs 里存着旧云端链接、本次显式给本地路径 → 必须走本地（压制 prefs/config），
    且 prefs 的 cloud_url 被清掉。否则这一批会静默写进旧云端文档（同名 Sheet 不报错）。"""
    seen = _patch_common(monkeypatch, tmp_path, FakeCloud())
    S.save_prefs(excel="https://www.kdocs.cn/l/old", sheet="pawly全球")
    lock_checked = {"n": 0}

    def locked(_path):
        lock_checked["n"] += 1
        return False

    monkeypatch.setattr(S, "excel_write_locked", locked)
    monkeypatch.setattr(S, "spu_col_of", lambda e, s: "C")
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c: set()),
    )

    async def fake_schema(tool, excel, sheet):
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True)

    monkeypatch.setattr(P, "resolve_sheet_schema", fake_schema)

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="D:/wb.xlsx", sheet="pawly全球",
    ))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert lock_checked["n"] == 1, "显式本地路径必须走本地锁预检"
    assert seen["cloud"] is None, "显式本地路径要压制 prefs 里的云端链接"
    prefs = S.load_prefs()
    assert prefs["excel"] == "D:/wb.xlsx" and prefs["cloud_url"] == ""


def test_run_batch_cloud_from_prefs_keeps_link(monkeypatch, tmp_path):
    """云端目标来自 prefs（不带参数跑批）：走云端，且 prefs 的 cloud_url 不被冲掉——
    否则本批写云端、下一批不带参数就静默退回本地默认表。"""
    cloud = FakeCloud()  # file_id 默认就是 prefs 里那条链接
    seen = _patch_common(monkeypatch, tmp_path, cloud)
    S.save_prefs(excel=cloud.file_id, sheet="pawly全球")

    async def fake_schema_cloud(c, sheet):
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True, header_row=1)

    monkeypatch.setattr(P, "resolve_sheet_schema_cloud", fake_schema_cloud)

    def locked(_path):
        raise AssertionError("云端模式不该做本地锁预检")

    monkeypatch.setattr(S, "excel_write_locked", locked)

    res = asyncio.run(S.run_batch(limit=5, base_only=True, sheet="pawly全球"))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert cloud.key_reads == [("pawly全球", "C", 1)]
    assert len(cloud.writes) == 1, "基础模式云端走整批写"
    assert S.load_prefs()["cloud_url"] == cloud.file_id, "云端批次不能冲掉 prefs 链接"


def test_run_batch_cloud_read_error_aborts_cleanly(monkeypatch, tmp_path):
    """云端读失败（未认证/限频/网络）：结构化 aborted 事件，不抛裸 traceback（CLI 场景）。"""
    _patch_common(monkeypatch, tmp_path, FakeCloud())

    async def boom_schema(c, sheet):
        raise K.KdocsSheetError("鉴权失败")

    monkeypatch.setattr(P, "resolve_sheet_schema_cloud", boom_schema)
    events = []

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True,
        excel="https://www.kdocs.cn/l/abc123", sheet="pawly全球",
        on_progress=lambda e: events.append(e),
    ))

    assert res == {"ok": 0, "fail": 0, "batch": 0}
    assert events and events[0]["type"] == "aborted"
    assert "鉴权失败" in events[0]["reason"]


def test_resolve_cloud_priority(monkeypatch, tmp_path):
    """显式链接 > 显式本地（→None，压制 prefs/config）> cloud_url 参数 > prefs > config。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_collect_config",
                        lambda: {"cloud_file_id": "CFG_FILE"})
    made = []

    def fake_backend(cfg, cloud_url=""):
        made.append(cloud_url or cfg.get("cloud_file_id"))
        return object()

    monkeypatch.setattr(S, "cloud_backend", fake_backend)

    assert S.resolve_cloud("https://www.kdocs.cn/l/abc123") is not None
    assert made[-1] == "https://www.kdocs.cn/l/abc123"

    S.save_prefs(excel="https://www.kdocs.cn/l/old", sheet="S")
    assert S.resolve_cloud("D:/wb.xlsx") is None, "显式本地路径必须压制 prefs/config"

    assert S.resolve_cloud() is not None
    assert made[-1] == "https://www.kdocs.cn/l/old", "未显式指定时落到 prefs 链接"

    assert S.resolve_cloud(cloud_url="https://www.kdocs.cn/l/new") is not None
    assert made[-1] == "https://www.kdocs.cn/l/new", "cloud_url 参数优先于 prefs"

    S.save_prefs(excel="D:/wb.xlsx", sheet="S")  # 清掉 cloud_url
    assert S.resolve_cloud() is not None
    assert made[-1] == "CFG_FILE", "无 prefs 链接时落到 config 的 cloud_file_id"


# ---- 本地/线上文档模式切换（kdocs 限额时要能立刻切回本地）----------------------


def test_resolve_cloud_local_mode_never_goes_cloud(monkeypatch, tmp_path):
    """选了 local：即便 config 配了云端、连输入框里是 kdocs 链接，也一律走本地。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_collect_config",
                        lambda: {"cloud_file_id": "CFG_FILE"})
    monkeypatch.setattr(S, "cloud_backend",
                        lambda cfg, cloud_url="": pytest.fail("本地模式不该构造云端后端"))

    assert S.resolve_cloud(None, None, "local") is None
    assert S.resolve_cloud("https://www.kdocs.cn/l/abc", None, "local") is None


def test_resolve_cloud_cloud_mode_uses_prefs_then_config(monkeypatch, tmp_path):
    """选了 cloud 但没粘链接：退 prefs.cloud_url，再退 config，不该回本地。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_collect_config",
                        lambda: {"cloud_file_id": "CFG_FILE"})
    made = []

    def fake_backend(cfg, cloud_url=""):
        made.append(cloud_url or cfg.get("cloud_file_id"))
        return object()

    monkeypatch.setattr(S, "cloud_backend", fake_backend)

    S.save_prefs(excel="https://www.kdocs.cn/l/old", sheet="S", doc_mode="cloud")
    assert S.resolve_cloud(None, None, "cloud") is not None
    assert made[-1] == "https://www.kdocs.cn/l/old"

    # 本地路径不该把 cloud 模式拽回本地（用户已经明确选了线上）
    assert S.resolve_cloud("D:/wb.xlsx", None, "cloud") is not None


def test_run_batch_local_mode_ignores_config_cloud(monkeypatch, tmp_path):
    """整批：选了 local 就写本地表，config 的 cloud_file_id 一概不理。"""
    _patch_common(monkeypatch, tmp_path, FakeCloud())
    monkeypatch.setattr(S, "load_collect_config",
                        lambda: {"cloud_file_id": "CFG_FILE"})
    monkeypatch.setattr(S, "cloud_backend",
                        lambda cfg, cloud_url="": pytest.fail("本地模式不该构造云端后端"))
    events: list = []

    asyncio.run(S.run_batch(
        limit=1, base_only=True, on_progress=lambda e: events.append(e),
        excel=str(tmp_path / "wb.xlsx"), sheet="pawly全球", doc_mode="local",
    ))

    start = next(e for e in events if e["type"] == "batch_start")
    assert start["cloud"] is False and start["doc_mode"] == "local"


def test_run_batch_cloud_mode_without_target_aborts(monkeypatch, tmp_path):
    """选了 cloud 却没有可用文档：中止，不能静默退回本地表（会写错文档）。"""
    _patch_common(monkeypatch, tmp_path, FakeCloud())
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "cloud_backend", lambda cfg, cloud_url="": None)
    events: list = []

    res = asyncio.run(S.run_batch(
        limit=1, base_only=True, on_progress=lambda e: events.append(e),
        excel="", sheet="pawly全球", doc_mode="cloud",
    ))

    assert res == {"ok": 0, "fail": 0, "batch": 0}
    aborted = next(e for e in events if e["type"] == "aborted")
    assert "线上文档" in aborted["reason"]


def test_pick_local_excel_skips_stale_pref():
    """prefs 记的表被改名了就回传空串让用户重选，别把死路径抛给 UI 报「表不存在」。"""
    assert S._pick_local_excel({"excel": "D:/已经改名了.xlsx"}) == ""


def test_pick_local_excel_keeps_existing_pref(tmp_path):
    """prefs 路径文件还在就用它，不必每次都要用户重选。"""
    wb = tmp_path / "核算.xlsx"
    wb.write_bytes(b"x")

    assert S._pick_local_excel({"excel": str(wb)}) == str(wb)


def test_run_batch_local_without_excel_aborts(monkeypatch, tmp_path):
    """本地模式没选表：中止并提示回 UI 选，不能静默写进出厂默认的 DEFAULT_EXCEL。"""
    _patch_common(monkeypatch, tmp_path, FakeCloud())
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "load_prefs", lambda: {})
    events: list = []

    res = asyncio.run(S.run_batch(
        limit=1, base_only=True, on_progress=lambda e: events.append(e),
        excel="", sheet="pawly全球", doc_mode="local",
    ))

    assert res == {"ok": 0, "fail": 0, "batch": 0}
    aborted = next(e for e in events if e["type"] == "aborted")
    assert "未选择本地工作簿" in aborted["reason"]


# ---- get_worklist_status 云端分支 ----------------------------------------------


def test_worklist_status_cloud_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_worklist", lambda: [])
    monkeypatch.setattr(S, "list_workbooks", lambda: [])
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    cloud = FakeCloud(sheets=("pawly全球", "wintak美国"))
    monkeypatch.setattr(S, "cloud_backend", lambda cfg, cloud_url="": cloud)

    st = S.get_worklist_status(excel="https://www.kdocs.cn/l/abc123", sheet="pawly全球")

    assert st["cloud"] is True
    assert st["sheets"] == ["pawly全球", "wintak美国"]
    assert st["sheet"] == "pawly全球"
    assert st["cloud_error"] == ""
    assert "excel_locked" not in st, "云端模式不返回本地锁字段"
    assert cloud.key_reads == [("pawly全球", "C", 1)], "已入库数走云端判重读"


def test_worklist_status_cloud_error_not_raised(monkeypatch, tmp_path):
    """云端读失败（未认证/网络）不抛错：空列表 + cloud_error 交 UI 红条展示。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_worklist", lambda: [])
    monkeypatch.setattr(S, "list_workbooks", lambda: [])
    monkeypatch.setattr(S, "load_collect_config", lambda: {})

    class BoomCloud(FakeCloud):
        def sheet_names(self):
            raise K.KdocsSheetError("鉴权失败")

    monkeypatch.setattr(S, "cloud_backend", lambda cfg, cloud_url="": BoomCloud())

    st = S.get_worklist_status(excel="https://www.kdocs.cn/l/abc123")

    assert st["cloud"] is True and st["sheets"] == []
    assert "鉴权失败" in st["cloud_error"]


# ---- prefs：链接 ↔ cloud_url 拆分 -----------------------------------------------


def test_prefs_cloud_link_split(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    S.save_prefs(excel="https://www.kdocs.cn/l/abc123", sheet="pawly全球",
                 store="m1", status="全部")

    prefs = S.load_prefs()
    assert prefs["cloud_url"] == "https://www.kdocs.cn/l/abc123"
    assert prefs["excel"] == ""
    assert prefs["sheet"] == "pawly全球" and prefs["status"] == "全部"


def test_prefs_local_path_clears_cloud_url(tmp_path, monkeypatch):
    """显式选过本地路径要清掉旧链接，避免旧链接盖掉后来的选择。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    S.save_prefs(excel="https://www.kdocs.cn/l/abc123", sheet="S")
    S.save_prefs(excel="D:/wb.xlsx", sheet="S")

    prefs = S.load_prefs()
    assert prefs["excel"] == "D:/wb.xlsx"
    assert prefs["cloud_url"] == ""


def test_is_cloud_link():
    assert S.is_cloud_link("https://www.kdocs.cn/l/abc")
    assert S.is_cloud_link(" http://x ")
    assert not S.is_cloud_link("D:/wb.xlsx")
    assert not S.is_cloud_link("")


def test_prefs_file_id_goes_to_cloud_url(tmp_path, monkeypatch):
    """云端目标是裸 file_id（来自 config）时同样存 cloud_url，不能当本地路径存 excel。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    S.save_prefs(excel="VsdfG0001234567", sheet="S")

    prefs = S.load_prefs()
    assert prefs["cloud_url"] == "VsdfG0001234567"
    assert prefs["excel"] == ""


def test_kdocs_sheet_link_scheme_case_insensitive():
    """大写 scheme 的链接也按 url 传参（与 is_cloud_link 的 .lower() 口径一致）。"""
    assert K.KdocsSheet("HTTPS://WWW.KDOCS.CN/L/ABC")._id_param == "url"
    assert K.KdocsSheet("VsdfG0001234567")._id_param == "file_id"
    assert not S.is_cloud_link(None)


def _patch_local_batch(monkeypatch, tmp_path, worklist):
    """本地基础模式跑 run_batch 的最小 patch 集，供 store 失配护栏用例复用。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_worklist", lambda: worklist)
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "excel_write_locked", lambda _p: False)
    monkeypatch.setattr(S, "spu_col_of", lambda e, s: "C")
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c: set()),
    )

    async def fake_schema(tool, excel, sheet):
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True)

    monkeypatch.setattr(P, "resolve_sheet_schema", fake_schema)
    done = []

    async def fake_base(item, excel, sheet, schema=None, cloud=None,
                        append_mode="bottom", append_row=None):
        done.append(str(item["spu"]))
        return S.CollectOutcome(spu=str(item["spu"]), ok=True, status="base", via="base")

    monkeypatch.setattr(S, "collect_one_base", fake_base)
    return done


def test_run_batch_stale_prefs_store_falls_back_to_all(monkeypatch, tmp_path):
    """prefs 里的店已不在清单里（清单被重新枚举成另一家店）→ 降级「全部店铺」并照常采。

    UI 侧 get_worklist_status 失配时回显空 store、下拉显示「全部店铺」，这里若仍按失效
    mallid 硬过滤，就会「页面写着全部、后端过滤到 0 条」，报「清单里没有商品」。
    """
    done = _patch_local_batch(
        monkeypatch, tmp_path,
        [{"spu": "S1", "name": "n", "mallid": "NEW", "store": "新店"}],
    )
    S.save_prefs(excel="D:/wb.xlsx", sheet="pawly全球", store="OLD")

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="D:/wb.xlsx", sheet="pawly全球",
    ))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert done == ["S1"], "降级后应采全部店铺的商品，而不是过滤到空"
    assert S.load_prefs()["store"] == "", "失效 store 要被清掉，别继续污染下一批"


def test_run_batch_explicit_unknown_store_aborts(monkeypatch, tmp_path):
    """显式指定的店不在清单里 → 如实报错，绝不悄悄降级成全量采（那是改写调用方意图）。"""
    done = _patch_local_batch(
        monkeypatch, tmp_path,
        [{"spu": "S1", "name": "n", "mallid": "NEW", "store": "新店"}],
    )
    events = []

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="D:/wb.xlsx", sheet="pawly全球",
        store="OLD", on_progress=lambda e: events.append(e),
    ))

    assert res == {"ok": 0, "fail": 0, "batch": 0}
    assert done == []
    assert events and events[0]["type"] == "aborted"
    assert "OLD" in events[0]["reason"]


def test_run_batch_explicit_store_still_filters(monkeypatch, tmp_path):
    """回归：显式指定清单里存在的店，仍只采该店（护栏不能把正常过滤放行掉）。"""
    done = _patch_local_batch(
        monkeypatch, tmp_path,
        [
            {"spu": "S1", "name": "n", "mallid": "A", "store": "店A"},
            {"spu": "S2", "name": "n", "mallid": "B", "store": "店B"},
        ],
    )

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="D:/wb.xlsx", sheet="pawly全球", store="B",
    ))

    assert res == {"ok": 1, "fail": 0, "batch": 1}
    assert done == ["S2"]


# ---- 云端批量写（基础模式）----------------------------------------------------


def test_write_rows_cloud_formula_row_numbers_increment():
    """一批 n 行的公式行号必须逐行递增：全用同一个行号会让 n 行公式指向同一行。

    两种落点都要验：追加时起点是数据区末行之后，插顶端时是表头正下方。
    """
    schema = P.SheetSchema(
        sheet="S", fields={"spu": "C"}, ok=True, header_row=1,
        formula_columns={"H": "=E{r}*2"},
    )
    items = [{"spu": f"S{i}"} for i in range(3)]

    # 默认 bottom：FakeCloud.row_to=5（0-based）→ 新行 1-based 从第 7 行起
    bottom = FakeCloud()
    ok, msg, wrote = asyncio.run(P.write_product_rows_cloud(bottom, "S", items, schema))
    assert ok, msg
    assert wrote == ["S0", "S1", "S2"]
    assert bottom.writes[0][3] is False
    assert [r["values"]["H"] for r in bottom.writes[0][1]] == [
        "=E7*2", "=E8*2", "=E9*2",
    ]

    # 插顶端：header_row=1 → 新行占 1-based 的 2/3/4 行
    top = FakeCloud()
    ok, msg, _ = asyncio.run(
        P.write_product_rows_cloud(top, "S", items, schema, insert_at_top=True)
    )
    assert ok, msg
    assert top.writes[0][3] is True
    assert [r["values"]["H"] for r in top.writes[0][1]] == [
        "=E2*2", "=E3*2", "=E4*2",
    ]


def test_write_rows_cloud_single_call_for_whole_batch():
    """整批只发一次 write_rows + 一次写后确认（配额敏感：逐行写是 5~6 次/行）。"""
    cloud = FakeCloud()
    schema = P.SheetSchema(sheet="S", fields={"spu": "C"}, ok=True, header_row=1)
    items = [{"spu": f"S{i}"} for i in range(20)]

    ok, _msg, wrote = asyncio.run(
        P.write_product_rows_cloud(cloud, "S", items, schema, insert_at_top=True)
    )

    assert ok and len(wrote) == 20
    assert len(cloud.writes) == 1, "20 行只该发一次 write_rows"
    # 插顶端：落点 = header_row+1 = 2
    assert cloud.new_row_reads == [("S", "C", 2, 20)], "确认只读新行区，不拉整列"
    assert cloud.key_reads == [], "批量写路径不该再逐行读整列判重"


def test_write_rows_cloud_all_or_nothing_on_write_error():
    """写失败 → 整批算失败、返回空已写列表（一坏全坏，重跑即可，不留半批）。"""
    from app.orders.kdocs_sheet import KdocsSheetError

    class BoomCloud(FakeCloud):
        def write_rows(self, sheet, rows, header_row, insert_at_top=True,
                       first_row=None):
            raise KdocsSheetError("429002 熔断")

    schema = P.SheetSchema(sheet="S", fields={"spu": "C"}, ok=True, header_row=1)
    items = [{"spu": "A"}, {"spu": "B"}]

    ok, msg, wrote = asyncio.run(
        P.write_product_rows_cloud(BoomCloud(), "S", items, schema)
    )

    assert not ok
    assert wrote == []
    assert "429002" in msg and "一行不落" in msg


def test_write_rows_cloud_detects_confirm_mismatch():
    """写后确认读回的 SPU 与预期不符 → 整批判失败，绝不谎报成功。"""
    class SkewCloud(FakeCloud):
        def read_new_rows_column(self, sheet, col, first_row, n):
            return ["WRONG"] * n

    schema = P.SheetSchema(sheet="S", fields={"spu": "C"}, ok=True, header_row=1)
    ok, msg, wrote = asyncio.run(
        P.write_product_rows_cloud(SkewCloud(), "S", [{"spu": "A"}], schema)
    )

    assert not ok and wrote == []
    assert "确认不一致" in msg


def test_run_batch_cloud_batch_write_failure_reports_all_failed(monkeypatch, tmp_path):
    """整批写失败时 run_batch 要把本批全部计为失败，并逐个发 aborted/fail 事件。"""
    from app.orders.kdocs_sheet import KdocsSheetError

    class BoomCloud(FakeCloud):
        def write_rows(self, sheet, rows, header_row, insert_at_top=True,
                       first_row=None):
            raise KdocsSheetError("429002 熔断")

    cloud = BoomCloud()
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_worklist", lambda: [
        {"spu": "S1", "name": "n", "mallid": "m"},
        {"spu": "S2", "name": "n", "mallid": "m"},
    ])
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "cloud_backend", lambda cfg, cloud_url="": cloud)

    async def fake_schema_cloud(c, sheet):
        return P.SheetSchema(sheet=sheet, fields={"spu": "C"}, ok=True, header_row=1)

    monkeypatch.setattr(P, "resolve_sheet_schema_cloud", fake_schema_cloud)
    events = []

    res = asyncio.run(S.run_batch(
        limit=5, base_only=True, excel="https://www.kdocs.cn/l/abc123",
        sheet="pawly全球", on_progress=lambda e: events.append(e),
    ))

    assert res == {"ok": 0, "fail": 2, "batch": 2}
    fails = [e for e in events if e.get("status") == "fail"]
    assert len(fails) == 2, "两个商品都要报失败，不能静默"
    assert any("429002" in str(e.get("note", "")) for e in fails)


# ---- 落点四模式 --------------------------------------------------------------


def test_resolve_append_target_four_modes():
    """四种落点解析成 (insert_at_top, 起始行, 标签)：起始行 None = 交给写入层自己算。"""
    assert S.resolve_append_target(S.APPEND_BOTTOM, None, 1)[:2] == (False, None)
    assert S.resolve_append_target(S.APPEND_TOP, None, 1)[:2] == (True, 2)
    # 表头在第 2 行时，top 落到第 3 行
    assert S.resolve_append_target(S.APPEND_TOP, None, 2)[:2] == (True, 3)
    # 指定行两种：解析阶段都返回该行本身，向上插的减法在 _cloud_first_row 里做
    assert S.resolve_append_target(S.APPEND_ROW_DOWN, 10, 1)[:2] == (True, 10)
    assert S.resolve_append_target(S.APPEND_ROW_UP, 10, 1)[:2] == (True, 10)
    # 标签带上行号，供日志/UI 明示落点
    assert "10" in S.resolve_append_target(S.APPEND_ROW_DOWN, 10, 1)[2]


def test_resolve_append_target_bad_row_falls_back_to_bottom():
    """行号缺失/非数字/落在表头及以上 → 退回 bottom，不抛错（落点是辅助选项，别让整批停摆）。"""
    for bad in (None, "", "abc", 0, -5, 1):  # header_row=1 时第 1 行就是表头
        at_top, at_row, label = S.resolve_append_target(S.APPEND_ROW_DOWN, bad, 1)
        assert (at_top, at_row) == (False, None), bad
        assert label == S.APPEND_MODE_LABELS[S.APPEND_BOTTOM]
    # 表头在第 2 行时，第 2 行也非法（就是表头本身）
    assert S.resolve_append_target(S.APPEND_ROW_UP, 2, 2)[:2] == (False, None)


def test_normalize_append_mode_unknown_falls_back():
    """不认识的值退默认 bottom：这个值来自 UI/CLI/prefs/config，老 prefs 里没有这个键。"""
    assert S.normalize_append_mode(None) == S.APPEND_BOTTOM
    assert S.normalize_append_mode("") == S.APPEND_BOTTOM
    assert S.normalize_append_mode("nonsense") == S.APPEND_BOTTOM
    assert S.normalize_append_mode(" TOP ") == S.APPEND_TOP  # 大小写/空格不敏感
    assert S.normalize_append_mode("row_up") == S.APPEND_ROW_UP


def test_cloud_first_row_row_up_subtracts_batch_size():
    """向上插：n 行要落在指定行【之前】，故起点 = R - n；越过表头则从表头下一行开始。"""
    cloud = FakeCloud()
    schema = P.SheetSchema(sheet="S", fields={"spu": "C"}, ok=True, header_row=1)

    # 从第 10 行向上插 3 行 → 占 7/8/9
    assert P._cloud_first_row(cloud, "S", schema, True, 10, 3) == 7
    # 向下插不减
    assert P._cloud_first_row(cloud, "S", schema, True, 10, 0) == 10
    # 挤到表头之上时钳到表头下一行（header_row=1 → 2）
    assert P._cloud_first_row(cloud, "S", schema, True, 3, 10) == 2


def test_write_rows_cloud_row_up_lands_before_target():
    """整批向上插：公式行号与落点都在指定行之前，且插行请求打在算出的起点上。"""
    cloud = FakeCloud()
    schema = P.SheetSchema(
        sheet="S", fields={"spu": "C"}, ok=True, header_row=1,
        formula_columns={"H": "=E{r}*2"},
    )
    items = [{"spu": f"S{i}"} for i in range(3)]

    ok, msg, wrote = asyncio.run(P.write_product_rows_cloud(
        cloud, "S", items, schema, insert_at_top=True, at_row=20, up_count=3))

    assert ok, msg
    assert wrote == ["S0", "S1", "S2"]
    # 起点 = 20 - 3 = 17 → 占 17/18/19，正好排在第 20 行之前
    assert [r["values"]["H"] for r in cloud.writes[0][1]] == [
        "=E17*2", "=E18*2", "=E19*2",
    ]
    assert cloud.writes[0][4] == 17, "显式落点要透传给 write_rows"
    assert cloud.new_row_reads == [("S", "C", 17, 3)]


def test_write_rows_cloud_row_down_starts_at_target():
    """整批向下插：从指定行开始占位（原该行及以下被推走）。"""
    cloud = FakeCloud()
    schema = P.SheetSchema(
        sheet="S", fields={"spu": "C"}, ok=True, header_row=1,
        formula_columns={"H": "=E{r}*2"},
    )
    items = [{"spu": f"S{i}"} for i in range(2)]

    ok, msg, _ = asyncio.run(P.write_product_rows_cloud(
        cloud, "S", items, schema, insert_at_top=True, at_row=8, up_count=0))

    assert ok, msg
    assert [r["values"]["H"] for r in cloud.writes[0][1]] == ["=E8*2", "=E9*2"]
    assert cloud.writes[0][4] == 8


def test_write_row_cloud_at_row_insert_payload(monkeypatch):
    """从指定行插入：insert_rows_cols 的 0-based 行号 = 指定行 - 1，文本也落在那一行。"""
    verify = {"rangeData": [{"rowFrom": 9, "colFrom": 0, "cellText": "美国"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "insert_rows_cols"): {"code": 0},
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    item = {"spu": "SPU-9", "site": "美国", "category": "玩具", "price": "¥12.50"}
    res = P.CollectResult(spu="SPU-9", ok=True, purchase_price=3.2, shipping=0.8)

    # 1-based 第 10 行 → 0-based 9
    ok, msg = asyncio.run(P.write_product_row_cloud(
        cli, "pawly全球", item, res, _schema(), insert_at_top=True, at_row=10))
    assert ok, msg

    calls = [c for c in cli._fake.calls if c[1] != "get_sheets_info"]
    insert = calls[0][2]
    assert (insert["row_from"], insert["row_to"]) == (9, 9)
    ops = calls[1][2]["range_data"]
    assert all(o["row_from"] == 9 for o in ops)
    by_col = {K.index_to_col(o["col_from"]): o["formula"] for o in ops}
    assert by_col["K"] == "=G10+J10+H10*80", "公式行号用 1-based 的 10"
