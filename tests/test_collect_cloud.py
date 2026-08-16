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
from app.tool.wps_excel_tool import WpsExcelTool as W


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
    {"rowFrom": 1, "colFrom": 9, "cellText": "5", "numFormat": "0.00_ "},
    {"rowFrom": 1, "colFrom": 10, "cellText": "33.2", "fmlaText": "=G2+J2+H2*80"},
    {"rowFrom": 1, "colFrom": 11, "cellText": "100%", "fmlaText": "=E2/F2",
     "numFormat": "0.00%", "alignment": {"horizontal": "haCenter",
                                         "vertical": "vaCenter"}},
    {"rowFrom": 2, "colFrom": 2, "cellText": "SPU-2"},
    {"rowFrom": 2, "colFrom": 9, "cellText": "5"},
    {"rowFrom": 2, "colFrom": 10, "cellText": "41.2", "fmlaText": "=G3+J3+H3*80"},
    {"rowFrom": 2, "colFrom": 11, "cellText": "", "numFormat": "0.00%",
     "alignment": {"horizontal": "haCenter", "vertical": "vaCenter"}},
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
    # 响应里的 numFormat/alignment 必须翻成写侧 xf（numfmt 小写 + alcH/alcV），
    # 直接取 cell["xf"] 是取不到的——响应里没这个键（这正是格式一直没生效的根因）。
    assert sample[1]["L"]["format"] == {
        "numfmt": "0.00%", "alcH": 2, "alcV": 1,
    }, "空白格的既有格式也要保留，且要翻成写侧字段名"
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
    # 成本链与折扣均保留模板公式；折扣格式继续按百分比回放。
    assert schema.formula_columns == {
        "K": "=G{r}+J{r}+H{r}*80",
        "L": "=E{r}/F{r}",
    }
    assert schema.constant_columns == {"I": 6, "J": 5}
    assert schema.format_columns == {
        "J": {"numfmt": "0.00_ "},
        "L": {"numfmt": "0.00%", "alcH": 2, "alcV": 1},
    }


def test_schema_cloud_inherits_percent_and_cost_inputs_from_complete_row():
    """截图回归：55%/84/25/6.72/10 等公式输入必须随 Sheet 模板继承。

    第二行模拟管线刚写坏的行（公式还在，但模板输入只剩 P=1）；解析必须选第一条完整
    历史行，不能被末尾坏行反向污染。
    """
    header = {
        "D": "SPU ID", "E": "产品图片", "F": "货号", "G": "日常价",
        "H": "折扣", "I": "折扣参数", "J": "加速器参考价格", "K": "销售价格",
        "L": "采购价格", "M": "重量", "N": "空运头程", "O": "尾程运费",
        "P": "操作费", "Q": "广告", "R": "ros", "T": "成本", "U": "利润", "V": "毛利",
    }
    complete = {
        "D": {"text": "OLD", "formula": ""},
        "G": {"text": "152", "formula": "", "format": {"numfmt": "0.00_ "}},
        "H": {"text": "55%", "formula": "=K2/G2", "format": {"numfmt": "0.00%"}},
        "I": {"text": "55%", "formula": "", "format": {"numfmt": "0.00%"}},
        "J": {"text": "84", "formula": ""},
        "K": {"text": "67.2", "formula": "=J2*I2+J2*(1-I2)*H2"},
        "L": {"text": "22", "formula": "", "format": {"numfmt": "0.00_ "}},
        "M": {"text": "0.3", "formula": ""},
        "N": {"text": "25", "formula": "=M2*80+1"},
        "O": {"text": "25", "formula": ""},
        "P": {"text": "3", "formula": ""},
        "Q": {"text": "6.72", "formula": ""},
        "R": {"text": "10", "formula": ""},
        "T": {"text": "53.72", "formula": "=L2+N2+O2+P2+Q2"},
        "U": {"text": "13.48", "formula": "=K2-T2"},
        "V": {"text": "16.05%", "formula": "=U2/J2"},
    }
    broken = {
        "D": {"text": "NEW", "formula": ""},
        "G": {"text": "41", "formula": ""},
        "H": {"text": "2.04", "formula": "=K3/G3"},
        "K": {"text": "0", "formula": "=J3*I3+J3*(1-I3)*H3",
              "format": {"numfmt": "0.00_ "}},
        "N": {"text": "1", "formula": "=M3*80+1"},
        "P": {"text": "1", "formula": ""},
        "T": {"text": "1", "formula": "=L3+N3+O3+P3+Q3"},
        "U": {"text": "-1", "formula": "=K3-T3"},
        "V": {"text": "-0.01", "formula": "=U3/J3"},
    }

    class Cloud:
        def read_header(self, _sheet, _max_scan=3):
            return header, 1

        def data_end_row(self, _sheet):
            return 2  # 0-based：数据到第 3 行（complete=2、broken=3）

        def read_rows(self, _sheet, row_from, row_to):
            return {r: c for r, c in ((2, complete), (3, broken))
                    if row_from <= r <= row_to}

    schema = asyncio.run(P.resolve_sheet_schema_cloud(Cloud(), "VibeMakers全球"))

    assert schema.constant_columns == {"O": 25, "P": 3, "Q": 6.72, "R": 10}
    assert schema.formula_columns == {
        "H": "=K{r}/G{r}",
        "N": "=M{r}*80+1",
        "T": "=L{r}+N{r}+O{r}+P{r}+Q{r}",
        "U": "=K{r}-T{r}",
        "V": "=U{r}/J{r}",
    }
    assert schema.format_columns["H"] == {"numfmt": "0.00%"}
    assert schema.format_columns["G"] == {"numfmt": "0.00_ "}
    assert schema.format_columns["K"] == {"numfmt": "0.00_ "}, (
        "模板行缺格式时按同列历史格式补齐"
    )
    assert schema.format_columns["L"] == {"numfmt": "0.00_ "}

    row = P._cloud_row(
        {"spu": "NEW-2", "sku_spec": "红色", "price": "46"},
        P.CollectResult(spu="NEW-2", ok=True), schema, 0, 9,
    )
    assert row["values"]["H"] == "=K9/G9", "折扣保留模板公式，结果显示为 100%"
    assert row["values"]["K"] == 46, "销售价格直接取清单，不仿历史公式"
    assert "I" not in row["values"] and "J" not in row["values"], (
        "加速器参考价、叠加折扣没有清单来源，必须留空"
    )
    assert row["values"]["O"] == 25 and row["values"]["R"] == 10
    assert row["values"]["T"] == "=L9+N9+O9+P9+Q9"
    assert row["formats"]["G"] == {"numfmt": "0.00_ "}
    assert row["formats"]["K"] == {"numfmt": "0.00_ "}


def test_schema_cloud_votes_numfmt_separately_from_alignment():
    """数字格式与对齐分开投票：少数行才有的数字格式不能被「只有对齐」的多数票压掉。

    实机现象：广告列 6 行里只有 2 行带 0.00_，其余是管线自己写的、只有对齐没有数字
    格式的行。整字典投票时后者赢，新行又退回通用格式——历史包袱反过来决定新行。
    """
    header = {"D": "SPU ID", "T": "成本", "U": "利润"}
    only_align = {"alcH": 2, "alcV": 1}
    with_numfmt = {"alcH": 2, "alcV": 1, "numfmt": "0.00_ "}
    sample = [
        {"D": {"text": "1", "formula": "", "format": only_align},
         "T": {"text": "1", "formula": "=A2+B2", "format": only_align},
         "U": {"text": "1", "formula": "", "format": only_align}},
        {"D": {"text": "2", "formula": "", "format": only_align},
         "T": {"text": "2", "formula": "=A3+B3", "format": only_align},
         "U": {"text": "2", "formula": "", "format": with_numfmt}},
        {"D": {"text": "3", "formula": "", "format": only_align},
         "T": {"text": "3", "formula": "=A4+B4", "format": with_numfmt},
         "U": {"text": "3", "formula": "", "format": only_align}},
    ]

    class Cloud:
        def read_header(self, _sheet, _max_scan=3):
            return header, 1

        def data_end_row(self, _sheet):
            return 3  # 0-based：数据到第 4 行

        def read_rows(self, _sheet, row_from, row_to):
            return {r: row for r, row in zip((2, 3, 4), sample)
                    if row_from <= r <= row_to}

    schema = asyncio.run(P.resolve_sheet_schema_cloud(Cloud(), "S"))

    assert schema.format_columns["T"] == {
        "alcH": 2, "alcV": 1, "numfmt": "0.00_ ",
    }, "1/3 的行有数字格式也要赢——没设格式的行不该参与数字格式投票"
    assert schema.format_columns["D"] == {"alcH": 2, "alcV": 1}, "全列都没数字格式就不设"


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


# ---- 公式模板化 / 渲染（纯函数） -------------------------------------------


def test_templatize_keeps_cross_row_offsets_and_function_names():
    """公式模板化必须按【相对偏移】记账，且不许把函数名里的数字当行号。

    旧实现是「所有 [A-Z]+\\d+ 的行号一律换成 {r}」，实测三类算错：
      - `=J9-J8`（与上一行环比）→ `=J{r}-J{r}` → 渲染成 `=J100-J100`，恒等于 0；
      - `=SUM(I2:I9)`（累计求和）→ `=SUM(I{r}:I{r})`，区间塌成单格；
      - `=LOG10(G9)` → `=LOG{r}(G{r})` → 渲染成 `=LOG100(G100)`，公式直接失效。
    """
    t = P._templatize_formula
    assert t("=I9/G9", 9) == "=I{r}/G{r}"
    assert t("=J9-J8", 9) == "=J{r}-J{r-1}", "跨行引用要记成偏移，不能压平成本行"
    assert t("=SUM(I2:I9)", 9) == "=SUM(I{r-7}:I{r})", "区间起点的偏移要留住"
    assert t("=LOG10(G9)", 9) == "=LOG10(G{r})", "函数名里的 10 不是行号"
    assert t("=ATAN2(A9,B9)", 9) == "=ATAN2(A{r},B{r})"
    # 绝对行不动：WINTAK欧洲 的空运头程 = 重量 × 顶部运费表单价
    assert t("=L9*$B$2", 9) == "=L{r}*$B$2"
    assert t("=$C$2", 9) == "=$C$2"
    # 同一套逻辑在不同行要归一成同一个模板，逐列多数表决才统计得到一起
    assert t("=J9-J8", 9) == t("=J10-J9", 10)


def test_render_formula_resolves_offsets_and_clamps():
    r = P.render_formula
    assert r("=I{r}/G{r}", 100) == "=I100/G100"
    assert r("=J{r}-J{r-1}", 100) == "=J100-J99"
    assert r("=SUM(I{r-7}:I{r})", 100) == "=SUM(I93:I100)"
    assert r("=L{r}*$B$2", 100) == "=L100*$B$2"
    assert r("=J{r}-J{r-1}", 1) == "=J1-J1", "落点靠顶时行号夹到 1，不出 0 行"


# ---- 按落点采样（本次修复的核心） ------------------------------------------


def _layered_cloud(top_rows: dict, bottom_rows: dict, header: dict,
                   end_row: int, header_row: int = 1):
    """造一个「顶部与底部公式不同」的云端替身，复刻真实工作簿的改版历史。"""

    class Cloud:
        calls: list = []

        def read_header(self, _sheet, _max_scan=3):
            return header, header_row

        def data_end_row(self, _sheet):
            return end_row - 1  # 0-based

        def read_rows(self, _sheet, row_from, row_to):
            self.calls.append((row_from, row_to))
            out = {}
            for src in (top_rows, bottom_rows):
                for rid, row in src.items():
                    if row_from <= rid <= row_to:
                        out[rid] = row
            return out

    return Cloud()


def test_schema_cloud_learns_formulas_near_landing_not_from_top():
    """公式要学【落点附近】那一段，不能固定学表头下前 10 行。

    这是本次修复的根因回归：实测同一张表顶部与底部根本不是一套公式——表格中途改过算法
    或插过列，历史行不会被回刷。例 wintak童装货盘记录 顶部毛利 `=T{r}/L{r}`、底部是
    `=T{r}/I{r}`；pawly全球 顶部销售价 `=J{r}*K{r}`（一层折扣）、底部 `=J{r}*K{r}*L{r}`
    （两层）。新行落在底部却按顶部公式写，成本/毛利算出来就是错的，而且数字照样显示。
    """
    header = {"D": "SPU ID", "G": "日常价", "I": "加速器价格", "L": "销售价格",
              "M": "采购价格", "S": "成本", "T": "利润", "U": "毛利"}
    top = {
        r: {"D": {"text": f"OLD-{r}", "formula": ""},
            "S": {"text": "45", "formula": f"=M{r}+O{r}"},
            "U": {"text": "18%", "formula": f"=T{r}/L{r}"}}
        for r in (2, 3, 4)
    }
    bottom = {
        r: {"D": {"text": f"NEW-{r}", "formula": ""},
            "S": {"text": "53", "formula": f"=M{r}+O{r}+P{r}"},
            "U": {"text": "15%", "formula": f"=T{r}/I{r}"}}
        for r in (1869, 1870, 1871, 1872, 1873)
    }
    cloud = _layered_cloud(top, bottom, header, end_row=1873)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "wintak童装货盘记录"))

    assert schema.ok
    assert schema.formula_columns["S"] == "=M{r}+O{r}+P{r}", "要学底部那版成本公式"
    assert schema.formula_columns["U"] == "=T{r}/I{r}", "毛利分母跟底部，不跟顶部"
    assert all(lo > 1000 for lo, _hi in cloud.calls), (
        "采样窗口必须落在数据区末尾附近，不该去读表头下前几行"
    )


def test_schema_cloud_samples_above_explicit_landing_row():
    """「从第 R 行插」时要学 R 上方的公式，不是表尾的。"""
    header = {"D": "SPU ID", "S": "成本"}
    top = {r: {"D": {"text": "x", "formula": ""},
               "S": {"text": "1", "formula": f"=A{r}+B{r}"}} for r in (2, 3, 4, 5, 6)}
    bottom = {r: {"D": {"text": "y", "formula": ""},
                  "S": {"text": "2", "formula": f"=A{r}*B{r}"}} for r in (500, 501)}
    cloud = _layered_cloud(top, bottom, header, end_row=501)

    schema = asyncio.run(
        P.resolve_sheet_schema_cloud(cloud, "S", landing_row=7)
    )
    assert schema.formula_columns["S"] == "=A{r}+B{r}", "插在第 7 行就学上方那段"


def test_schema_cloud_looks_further_up_when_tail_is_blank():
    """表尾有空白预留区时要继续往上翻，而不是判定「本表无公式」。

    实测 wintak美国 数据到第 223 行，但最后一个带公式的行是 215——中间那几行是留白。
    只看紧邻末行的窗口会一个公式都学不到，新行整行没有成本/利润公式。
    """
    header = {"D": "SPU ID", "S": "成本"}
    rows = {r: {"D": {"text": "x", "formula": ""},
                "S": {"text": "1", "formula": f"=M{r}+N{r}"}}
            for r in range(206, 216)}
    blank = {r: {"A": {"text": " ", "formula": ""}} for r in range(216, 224)}
    cloud = _layered_cloud(rows, blank, header, end_row=223)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "wintak美国"))
    assert schema.formula_columns["S"] == "=M{r}+N{r}"


def test_schema_cloud_votes_per_column_majority():
    """逐列多数表决：同列个别行被人工改过，不该让新行跟着那一行走。

    实测 pawly全球 末尾 6 行里毛利列 4 行是 `=U{r}/M{r}`、2 行是 `=U{r}/J{r}`；
    整行取「公式最全的模板行」会把那一行的异常一起继承。
    """
    header = {"D": "SPU ID", "V": "毛利"}
    rows = {}
    for r in (590, 591, 593, 595):
        rows[r] = {"D": {"text": "x", "formula": ""},
                   "V": {"text": "16%", "formula": f"=U{r}/M{r}"}}
    for r in (592, 594):
        rows[r] = {"D": {"text": "x", "formula": ""},
                   "V": {"text": "16%", "formula": f"=U{r}/J{r}"}}
    cloud = _layered_cloud(rows, {}, header, end_row=595)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "pawly全球"))
    assert schema.formula_columns["V"] == "=U{r}/M{r}", "取多数票那版"


def test_schema_cloud_skips_column_whose_formula_differs_every_row():
    """逐行因地制宜的列宁可留空，不能抄多数票。

    实测 WINTAK欧洲 的「空运头程」= 重量 × 该国运费单价，采样行分别引用 $B$2/$D$2/$F$2…
    （跟着货号列的站点走）。抄一个等于把最后一行那个国家的运费按到所有新行上——算得出
    数、看不出错，比留空危险得多。
    """
    header = {"C": "SPU ID", "M": "空运头程", "U": "利润"}
    rates = ["$B$2", "$D$2", "$F$2", "$J$2", "$L$2", "$N$2", "$O$2", "$Q$2"]
    rows = {}
    for i, r in enumerate(range(401, 409)):
        rows[r] = {
            "C": {"text": f"spu{r}", "formula": ""},
            "M": {"text": "23", "formula": f"=L{r}*{rates[i]}"},
            "U": {"text": "18", "formula": f"=J{r}-T{r}"},
        }
    cloud = _layered_cloud(rows, {}, header, end_row=408)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "WINTAK欧洲"))
    assert "M" not in schema.formula_columns, "众口不一的列留空待人工填"
    assert schema.formula_columns["U"] == "=J{r}-T{r}", "同表其它稳定列照常仿写"


def test_schema_cloud_deep_scans_header_when_top_rows_are_another_table():
    """前 3 行认不出 SPU 时要往下深扫：真表头可能在第 4 行。

    实测 WINTAK欧洲 前 3 行是一张各国运费/操作费小表（22 列，被「非空最多」规则选中
    当表头），真表头在第 4 行（35 列）。深扫前这张表整个 ok=False、写不进去。
    """
    shallow = {"A": "相关项目", "B": "德国运费", "C": "税费"}
    real = {"C": "SPU ID", "D": "产品图片", "L": "重量", "T": "Y2成本"}

    class Cloud:
        def read_header(self, _sheet, max_scan=3):
            return (shallow, 1) if max_scan <= 3 else (real, 4)

        def data_end_row(self, _sheet):
            return 407

        def read_rows(self, _sheet, row_from, row_to):
            return {r: {"C": {"text": f"s{r}", "formula": ""},
                        "T": {"text": "1", "formula": f"=K{r}+M{r}"}}
                    for r in range(max(row_from, 400), min(row_to, 408) + 1)}

    schema = asyncio.run(P.resolve_sheet_schema_cloud(Cloud(), "WINTAK欧洲"))
    assert schema.ok and schema.header_row == 4
    assert schema.fields["spu"] == "C"
    assert schema.formula_columns["T"] == "=K{r}+M{r}"


def test_schema_cloud_skips_column_that_is_formula_in_only_few_rows():
    """某列只有个别数据行是公式 → 那是异常行，不是本表算法，不许仿。

    实测 wintak童装货盘记录 第 1872 行有人把「折扣」和「加速器价格」写反了（H 填成值
    70%、I 变成 =H*G），其余 8 行都是 H==I/G、I 是纯值。旧口径按「有公式的行」当分母，
    I 列以 1/1 满票当选，新行于是 H==I/G 且 I==H*G——直接【循环引用】。
    """
    header = {"D": "SPU ID", "G": "日常价", "H": "折扣", "I": "加速器价格",
              "L": "最终销售价格"}
    rows = {}
    for r in range(1862, 1872):  # 正常行：H 是公式，I 是纯值
        rows[r] = {"D": {"text": f"spu{r}", "formula": ""},
                   "G": {"text": "100", "formula": ""},
                   "H": {"text": "85%", "formula": f"=I{r}/G{r}"},
                   "I": {"text": "85", "formula": ""}}
    rows[1872] = {"D": {"text": "spu1872", "formula": ""},   # 异常行：H/I 写反
                  "G": {"text": "129", "formula": ""},
                  "H": {"text": "70%", "formula": ""},
                  "I": {"text": "90", "formula": "=H1872*G1872"}}
    cloud = _layered_cloud(rows, {}, header, end_row=1872)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "wintak童装货盘记录"))
    assert schema.formula_columns.get("H") == "=I{r}/G{r}", "多数行的算法照常仿"
    assert "I" not in schema.formula_columns, (
        "只有 1/11 行是公式的列按纯值列处理，否则新行 H 与 I 互相引用"
    )


def test_schema_cloud_counts_data_rows_when_spu_spans_rows():
    """一个 SPU 占多行（每行一个站点）时，数据行的分母不能只数 SPU 非空的行。

    实测 WINTAK欧洲 一个 SPU 铺 13 行欧洲站点，只有首行填 SPU。只认 SPU 会把分母压成 1，
    「逐行不同就留空」那道闸随之失效，各国运费公式又会被抄成同一个国家的。
    """
    header = {"C": "SPU ID", "E": "货号", "L": "重量", "M": "空运头程", "U": "利润"}
    rates = ["$B$2", "$D$2", "$F$2", "$J$2", "$L$2", "$N$2", "$O$2", "$Q$2", "$R$2"]
    rows = {}
    for i, r in enumerate(range(400, 409)):
        rows[r] = {
            # SPU 只有首行有值，其余行靠货号列区分站点
            "C": {"text": "39463986367" if i == 0 else "", "formula": ""},
            "E": {"text": f"站点{i}", "formula": ""},
            "M": {"text": "23", "formula": f"=L{r}*{rates[i]}"},
            "U": {"text": "18", "formula": f"=J{r}-T{r}"},
        }
    cloud = _layered_cloud(rows, {}, header, end_row=408)

    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "WINTAK欧洲"))
    assert "M" not in schema.formula_columns, (
        "各国运费单价逐行不同，必须留空——分母只数 SPU 会让这道闸失效"
    )
    assert schema.formula_columns["U"] == "=J{r}-T{r}"


# ---- 表头被改写 --------------------------------------------------------------


def test_field_rules_tolerate_real_header_variants():
    """表头标题的常见改写要认得出来：认不出的代价是把钱算错。

    「最终销售价格」是【线上真实存在】的写法（wintak童装货盘记录），旧规则精确等值匹配
    认不出 sale，于是该列失去「不许仿公式」的保护、被历史公式 =叠加折扣*加速器价格 顶掉，
    平台申报价根本没落进表里。
    """
    R = W._resolve_fields_from_header
    base = {"E": "SPU ID", "F": "产品图片", "M": "销售价格",
            "N": "采购价格", "O": "重量"}
    assert R(base)["sale"] == "M"
    # 前置限定语
    assert R({**base, "M": "最终销售价格"})["sale"] == "M"
    assert R({**base, "M": "折后销售价格"})["sale"] == "M"
    # 括号注释与计量/币种尾巴
    assert R({**base, "M": "销售价格(USD)"})["sale"] == "M"
    assert R({**base, "N": "采购价格（含税）"})["purchase"] == "N"
    assert R({**base, "O": "重量kg"})["weight"] == "O"
    assert R({**base, "O": "重量（kg）"})["weight"] == "O"
    assert R({**base, "N": "进货价"})["purchase"] == "N"


def test_field_rules_do_not_overreach_on_lookalike_titles():
    """容忍改写不能变成乱认：这些列各有各的用途，认错比认不出更糟。"""
    R = W._resolve_fields_from_header
    # 「叠加折扣1」「折扣参数」是公式输入/参数，不是折扣列
    f = R({"E": "SPU ID", "I": "折扣", "K": "叠加折扣1", "L": "叠加折扣2",
           "J": "折扣参数"})
    assert f["discount"] == "I"
    # 「调整前ROS」不能把真正的 ros 列顶掉（pawly美国 两列并存，前者列序更靠前）
    f2 = R({"C": "SPU ID", "Q": "调整前ROS", "R": "ros"})
    assert f2["ros"] == "R", "ros 必须精确匹配，否则被前置限定语的列抢走"
    # 「加速器参考价格」不是销售价，「日常价」也不该被「非日常价」之类抢走
    f3 = R({"E": "SPU ID", "J": "加速器参考价格", "M": "销售价格"})
    assert f3["sale"] == "M" and "J" not in f3.values()


def test_schema_cloud_warns_and_protects_when_value_field_unrecognized(caplog):
    """表头改得认不出来时：告警 + 该列既不写值也不被历史公式覆盖。

    这是「宁可留空也不要写错」的兜底——猜标题猜错等于把申报价写进别的列，更糟。
    """
    # 售价列标题改成完全认不出来的写法，且历史行该列是公式
    header = {"E": "SPU ID", "F": "产品图片", "M": "成交单价X",
              "N": "采购价格", "O": "重量", "T": "成本"}
    rows = {
        r: {"E": {"text": f"spu{r}", "formula": ""},
            "M": {"text": "67", "formula": f"=J{r}*K{r}"},
            "T": {"text": "53", "formula": f"=N{r}+P{r}"}}
        for r in range(590, 596)
    }
    cloud = _layered_cloud(rows, {}, header, end_row=595)

    import logging
    with caplog.at_level(logging.WARNING):
        schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "S"))

    assert schema.ok, "认不出可选字段不该整批拒写——SPU 在就还能写"
    assert "sale" not in schema.fields
    row = P._cloud_row({"spu": "NEW", "price": "46.10¥"},
                       P.CollectResult(spu="NEW", ok=True), schema, 0, 596)
    assert "M" not in row["values"] or not str(row["values"].get("M")).startswith("="), (
        "认不出的值列不能被历史公式顶掉（会把申报价算成依赖空白列的结果）"
    )
    assert schema.formula_columns.get("T") == "=N{r}+P{r}", "其它列照常"


def test_schema_cloud_still_refuses_when_spu_unrecognized():
    """SPU 列认不出来仍必须整批拒写：没有判重键，写进去就是重复+错位。"""
    header = {"E": "商品编号X", "F": "产品图片", "M": "销售价格"}
    cloud = _layered_cloud(
        {590: {"E": {"text": "x", "formula": ""}}}, {}, header, end_row=590)
    schema = asyncio.run(P.resolve_sheet_schema_cloud(cloud, "S"))
    assert not schema.ok and "SPU" in schema.error


def test_cloud_row_renders_cross_row_formula_per_offset():
    """整批写时每行公式按自己的行号渲染，跨行偏移也要跟着走。"""
    schema = P.SheetSchema(
        sheet="S", fields={"spu": "C"},
        formula_columns={"T": "=K{r}+M{r}", "L": "=J{r}-J{r-1}"},
        ok=True, header_row=1,
    )
    rows = [
        P._cloud_row({"spu": f"S{i}"}, P.CollectResult(spu=f"S{i}", ok=True),
                     schema, i, 100)
        for i in range(3)
    ]
    assert [r["values"]["T"] for r in rows] == [
        "=K100+M100", "=K101+M101", "=K102+M102",
    ]
    assert [r["values"]["L"] for r in rows] == [
        "=J100-J99", "=J101-J100", "=J102-J101",
    ]


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
    assert "I" not in by_col                 # 没有模板值就留空，不再固定写默认 ros
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


def test_write_row_cloud_replays_template_cell_formats(monkeypatch):
    """云端追加到空白行时要回放模板格式，保住百分比/金额等显示。

    并钉住「只回放到本行真写了值/公式的格」：给空白格设格式白占 payload，而 kdocs
    有配额（429001 限频要等 20s），一批 20 行 × 二十来列全设一遍是纯浪费。
    """
    verify = {"rangeData": [{"rowFrom": 6, "colFrom": 0, "cellText": "美国"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "update_range_data"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    schema = _schema()
    schema.format_columns = {
        "E": {"numfmt": "0.00_ "},                    # 销售价：本行会写值
        "K": {"numfmt": "0.00%", "alcH": 2},          # 成本：本行会写公式
        "L": {"numfmt": "0.00%"},                     # 本行不写这列 → 不该发格式
    }

    ok, msg = asyncio.run(P.write_product_row_cloud(
        cli, "pawly全球", {"spu": "SPU-9", "site": "美国", "price": "12.5"},
        P.CollectResult(spu="SPU-9", ok=True), schema,
    ))

    assert ok, msg
    format_call = next(
        payload for _service, action, payload in cli._fake.calls
        if action == "update_range_data"
    )
    by_col = {
        K.index_to_col(op["colFrom"]): op["xf"]
        for op in format_call["rangeData"]
    }
    assert by_col == {
        "E": {"numfmt": "0.00_ "},
        "K": {"numfmt": "0.00%", "alcH": 2},
    }, "只回放到写了值/公式的列，空白列跳过"
    assert all(op["opType"] == "format" for op in format_call["rangeData"])


def test_write_row_cloud_keeps_numfmt_off_text_cells(monkeypatch):
    """文本格不回放数字格式，只留对齐。

    实测：站点/类目这些文本列的历史单元格上常年挂着 `0_ ` 这种数字格式（表格默认
    样式，文本显示不受影响），照抄过去一旦写进形似数字的文本就会被格式化。
    """
    verify = {"rangeData": [{"rowFrom": 6, "colFrom": 0, "cellText": "美国"}]}
    routes = {
        ("sheet", "get_sheets_info"): SHEETS_INFO,
        ("sheet", "range_data_batch_update"): {"code": 0},
        ("sheet", "update_range_data"): {"code": 0},
        ("sheet", "get_range_data"): verify,
    }
    cli = _make(monkeypatch, routes)
    schema = _schema()
    schema.format_columns = {
        "A": {"numfmt": "0_ ", "alcH": 2, "alcV": 1},   # 站点：写的是文本
        "E": {"numfmt": "0.00_ ", "alcV": 1},           # 销售价：写的是数字
    }

    ok, msg = asyncio.run(P.write_product_row_cloud(
        cli, "pawly全球", {"spu": "SPU-9", "site": "美国", "price": "12.5"},
        P.CollectResult(spu="SPU-9", ok=True), schema,
    ))

    assert ok, msg
    format_call = next(
        payload for _service, action, payload in cli._fake.calls
        if action == "update_range_data"
    )
    by_col = {
        K.index_to_col(op["colFrom"]): op["xf"] for op in format_call["rangeData"]
    }
    assert by_col["A"] == {"alcH": 2, "alcV": 1}, "文本格摘掉 numfmt，对齐仍要回放"
    assert by_col["E"] == {"numfmt": "0.00_ ", "alcV": 1}, "数字格保留数字格式"


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

    async def fake_schema_cloud(c, sheet, landing_row=None):
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
    # header_row 是 existing_keys 统一传下来的（本地 1-based），故打桩要收这个参数
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c, header_row=1: set()),
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
    # header_row 是 existing_keys 统一传下来的（本地 1-based），故打桩要收这个参数
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c, header_row=1: set()),
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

    async def fake_schema_cloud(c, sheet, landing_row=None):
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

    async def boom_schema(c, sheet, landing_row=None):
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
    # header_row 是 existing_keys 统一传下来的（本地 1-based），故打桩要收这个参数
    monkeypatch.setattr(
        S.WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c, header_row=1: set()),
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

    async def fake_schema_cloud(c, sheet, landing_row=None):
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
