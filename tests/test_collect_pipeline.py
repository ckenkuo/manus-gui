# -*- coding: utf-8 -*-
"""采集管线「一个 SKU 一行」改造的离线单测。

为什么专门开一个文件：改造的主战场是 pipeline._build_column_values（造行值）与
service 的判重键，而这两处此前【零覆盖】——tests/ 下原本只有 test_collect_cloud.py
覆盖云端写入路径，造值与判重全靠打桩绕过去了。

全部离线：不连浏览器、不读真实工作簿、不发网络请求。底层读表（existing_key_tuples 的
XML 解析与反转义）已由 tests/test_wps_excel_batch.py 覆盖，这里只打桩它，专测
existing_keys 自己的分支选择与键归一。
"""
import app.collect.pipeline as P
import app.collect.service as S
from pathlib import Path
from app.tool.wps_excel_tool import WpsExcelTool


# --- 判重键：清单侧与读表侧必须同源 ---------------------------------------

def test_dedupe_key_uses_full_spec_text():
    """键是 `SPU|规格全文`。货号列存的就是规格文本，两侧同源。

    【不能按空格截断】实测有「1-2 Pack Black/Large-X-Large」这种带空格的规格，
    截断会把它和同 SPU 的其它规格并成一个键、漏掉一整行。
    """
    assert S.dedupe_key("7948115685", "奶白+黑色/10双") == "7948115685|奶白+黑色/10双"
    assert S.dedupe_key("6715341608", "1-2 Pack Black/Large-X-Large") == (
        "6715341608|1-2 Pack Black/Large-X-Large"
    )


def test_worklist_key_reads_sku_spec():
    """清单侧的键取自 sku_spec，与写进货号列的值同源（见 _build_column_values）。"""
    assert S.worklist_key({"spu": "123", "sku_spec": "白色/5双"}) == "123|白色/5双"
    # 老清单没有 sku_spec → 退化成 `SPU|`，等价纯 SPU 判重
    assert S.worklist_key({"spu": "123"}) == "123|"


# --- 同价 SKU 折叠 -----------------------------------------------------------

def test_collapse_keeps_one_row_per_distinct_price():
    """同 SPU 里价格一样的规格只留第一条，价格不同的各留一行。

    这是本次改造的核心口径：成本核算表按价核算，同价规格的整行数值完全相同，
    逐行写只会把 Sheet 撑长；价格不同（2 双 / 10 双装）的规格必须各占一行。
    """
    items = [
        {"spu": "1", "sku_id": "11", "sku_spec": "红色/5双", "price": "46.10¥"},
        {"spu": "1", "sku_id": "12", "sku_spec": "蓝色/5双", "price": "46.10¥"},
        {"spu": "1", "sku_id": "13", "sku_spec": "黑色/10双", "price": "299.68¥"},
    ]
    got = S.collapse_same_price_skus(items)
    assert [it["sku_spec"] for it in got] == ["红色/5双", "黑色/10双"]


def test_collapse_compares_price_numerically():
    """同价的不同文本形态（46.1 / 46.10¥ / 1,299.00¥）要算同一价。

    平台返回的价格串形态不统一，按字符串比会把同价规格判成不同价、白白多写一行。
    """
    items = [
        {"spu": "1", "sku_id": "11", "sku_spec": "红", "price": "46.10¥"},
        {"spu": "1", "sku_id": "12", "sku_spec": "蓝", "price": "46.1"},
        {"spu": "1", "sku_id": "13", "sku_spec": "绿", "price": 46.1},
    ]
    assert len(S.collapse_same_price_skus(items)) == 1


def test_collapse_never_merges_across_stores_or_regions():
    """同一 SPU 在不同店/不同区域各留一行：它们落进不同 Sheet，合并会抹掉一整个店的行。"""
    items = [
        {"spu": "1", "mallid": "m1", "region": "global", "sku_spec": "红", "price": "9¥"},
        {"spu": "1", "mallid": "m1", "region": "us", "sku_spec": "红", "price": "9¥"},
        {"spu": "1", "mallid": "m2", "region": "global", "sku_spec": "红", "price": "9¥"},
    ]
    assert len(S.collapse_same_price_skus(items)) == 3


def test_collapse_keeps_unpriced_rows_separate():
    """价读不出的行不与任何有价行合并，也不互相合并——它们要留给人工逐行补价。"""
    items = [
        {"spu": "1", "sku_id": "11", "sku_spec": "红", "price": ""},
        {"spu": "1", "sku_id": "12", "sku_spec": "蓝", "price": "46¥"},
    ]
    got = S.collapse_same_price_skus(items)
    assert [it["sku_spec"] for it in got] == ["红", "蓝"]


def test_collapse_leaves_legacy_worklist_untouched():
    """老清单（一个 SPU 一条、无 sku_id）逐条价格各异 → 一条都不该被合并掉。"""
    items = [
        {"spu": "1", "price": "10¥"},
        {"spu": "2", "price": "20¥"},
    ]
    assert S.collapse_same_price_skus(items) == items


def test_load_worklist_collapses_same_price(monkeypatch, tmp_path):
    """折叠发生在读取侧：worklist.json 存平台全量快照，读出来才是可采行。"""
    import json

    wl = tmp_path / "worklist.json"
    wl.write_text(json.dumps([
        {"spu": "1", "sku_id": "11", "sku_spec": "红", "price": "9¥"},
        {"spu": "1", "sku_id": "12", "sku_spec": "蓝", "price": "9¥"},
    ]), encoding="utf-8")
    monkeypatch.setattr(S, "WORKLIST", wl)

    assert len(S.load_worklist()) == 1
    # 落盘文件保持原样（全量快照），便于回看原始价、日后改折叠判据不必重新枚举
    assert len(json.loads(wl.read_text(encoding="utf-8"))) == 2


def test_worklist_status_reports_todo_spu_and_sku_counts(monkeypatch, tmp_path):
    """待采 SPU 按商品去重，SKU 数按实际待采清单行计数。"""
    monkeypatch.setattr(S, "COLLECT_PREFS", tmp_path / "collect_prefs.json")
    monkeypatch.setattr(S, "load_collect_config", lambda: {})
    monkeypatch.setattr(S, "list_workbooks", lambda: [])
    monkeypatch.setattr(S.WpsExcelTool, "list_sheets", classmethod(lambda cls, _p: []))
    monkeypatch.setattr(S, "load_worklist", lambda: [
        {"spu": "1", "sku_id": "11", "sku_spec": "白色"},
        {"spu": "1", "sku_id": "12", "sku_spec": "黑色"},
        {"spu": "2", "sku_id": "21", "sku_spec": "大号"},
    ])

    status = S.get_worklist_status(excel="", sheet="", doc_mode="local")

    assert status["todo_spu"] == 2
    assert status["todo_sku"] == 3
    assert status["todo"] == 3, "旧 todo 字段继续表示可采 SKU 行数"


def test_collect_page_shows_dual_todo_metrics_and_syncs_limit_after_enumerate():
    """采集页要明示两种待采口径，且只在重新枚举后把数量回填为最大可采 SKU 数。"""
    html = (Path(__file__).parents[1] / "templates" / "collect.html").read_text(
        encoding="utf-8"
    )

    assert 'id="statTodoSpu"' in html and "SPU 总数" in html
    assert 'id="statTodoSku"' in html and "SKU 数量" in html
    assert 'await loadWorklist({ syncCollectLimit: true });' in html
    assert '$("inputLimit").value = String(maxCollectible);' in html


def test_dedupe_key_strips_whitespace():
    """两侧都 strip：表格里手工录入常带前后空格，不归一会永远比不上。"""
    assert S.dedupe_key("  123  ", "  白色/5双  ") == "123|白色/5双"


# --- done_flags：组合键命中 + 历史 SPU 整体跳过 -----------------------------

def test_done_flags_exact_key_hit():
    """规格精确命中 → 已入库；同 SPU 的其它规格照采。"""
    items = [
        {"spu": "1", "sku_spec": "白色/5双"},
        {"spu": "1", "sku_spec": "黑色/5双"},
    ]
    assert S.done_flags(items, {"1|白色/5双"}) == [True, False]


def test_done_flags_skips_legacy_spu_entirely():
    """表里是改造前的人工行（货号「5双」对不上本管线的「白色/5双」）→ 整个 SPU 跳过。

    这是操作者选定的口径：不把历史采过的商品再采一遍。若按纯组合键判重，
    实测 388 行会全部判成待采、把整张表重写一遍。
    """
    items = [
        {"spu": "1", "sku_spec": "白色/5双"},
        {"spu": "1", "sku_spec": "黑色/5双"},
    ]
    assert S.done_flags(items, {"1|5双"}) == [True, True]


def test_done_flags_keeps_collecting_partially_written_spu():
    """本管线已写过该 SPU 的一个规格 → 只跳过命中的，其余规格继续采。

    分批采集（limit=20）必然把同一 SPU 的规格拆到多批。若「表里有这个 SPU 就跳过」，
    第二批起剩余规格就永远补不上了——这条用例把那个陷阱钉死。
    """
    items = [
        {"spu": "1", "sku_spec": "白色/5双"},
        {"spu": "1", "sku_spec": "黑色/5双"},
        {"spu": "1", "sku_spec": "红色/5双"},
    ]
    assert S.done_flags(items, {"1|白色/5双"}) == [True, False, False]


def test_done_flags_new_spu_all_todo():
    """表里完全没有该 SPU → 全部待采。"""
    items = [{"spu": "9", "sku_spec": "白色/5双"}, {"spu": "9", "sku_spec": "黑色/5双"}]
    assert S.done_flags(items, {"1|白色/5双"}) == [False, False]


# --- existing_keys：本地/云端分支与降级 -------------------------------------

def test_existing_keys_uses_combo_key_when_sku_column_present(monkeypatch):
    """有货号列 → 读两列组合键，并把「skuId 规格」归一成 skuId。"""
    seen = {}

    def fake_tuples(cls, path, sheet, cols, header_row=1):
        seen["cols"] = cols
        return {
            ("7948115685", "奶白+黑色/10双"),
            ("7948115685", "奶白+黑色/2双"),
        }

    monkeypatch.setattr(
        WpsExcelTool, "resolve_field_columns",
        classmethod(lambda cls, e, s: {"spu": "E", "sku": "G"}),
    )
    monkeypatch.setattr(WpsExcelTool, "existing_key_tuples", classmethod(fake_tuples))

    got = S.existing_keys("book.xlsx", "pawly全球")
    assert seen["cols"] == ["E", "G"], "要按真实表头读 SPU 列 + 货号列"
    assert got == {"7948115685|奶白+黑色/10双", "7948115685|奶白+黑色/2双"}, (
        "同一 SPU 的两个规格必须是两个键，否则第二个规格会被判成已入库而漏采"
    )


def test_existing_keys_falls_back_to_spu_when_no_sku_column(monkeypatch):
    """老表没有货号列 → 退回纯 SPU 判重，行为与改造前一致。"""
    monkeypatch.setattr(
        WpsExcelTool, "resolve_field_columns",
        classmethod(lambda cls, e, s: {"spu": "E"}),
    )
    monkeypatch.setattr(
        WpsExcelTool, "existing_key_values",
        classmethod(lambda cls, e, s, c, header_row=1: {"111", "222"}),
    )
    assert S.existing_keys("book.xlsx", "老表") == {"111|", "222|"}


def test_existing_keys_cloud_passes_header_row(monkeypatch):
    """云端走 KdocsSheet 且必须把 header_row 透传下去（云端是 0-based，别丢）。"""
    calls = {}

    class FakeCloud:
        def existing_key_tuples(self, sheet, cols, header_row):
            calls["args"] = (sheet, cols, header_row)
            return {("7948115685", "奶白+黑色/10双")}

    got = S.existing_keys(
        "", "pawly全球", cloud=FakeCloud(),
        fields={"spu": "E", "sku": "G"}, header_row=3,
    )
    assert calls["args"] == ("pawly全球", ["E", "G"], 3)
    assert got == {"7948115685|奶白+黑色/10双"}


# --- 表头解析：货号列 --------------------------------------------------------

def test_field_rules_resolve_sku_column():
    """「货号」归 sku；「SPU ID」归 spu，两者不能互抢。"""
    header = {"A": "站点", "E": "SPU ID", "F": "产品图片", "G": "货号", "L": "销售价格"}
    fields = WpsExcelTool._resolve_fields_from_header(header)
    assert fields["spu"] == "E"
    assert fields["sku"] == "G"


def test_field_rules_resolve_discount_column():
    fields = WpsExcelTool._resolve_fields_from_header(
        {"G": "日常价", "H": "折扣", "I": "加速器参考价格", "K": "销售价格"}
    )
    assert fields["discount"] == "H"


def test_field_rules_spu_wins_over_sku_for_spu_prefixed_title():
    """「SPU货号」这种标题该归 spu（先到先得），不能被 sku 规则截走。"""
    fields = WpsExcelTool._resolve_fields_from_header({"B": "SPU货号", "C": "货号"})
    assert fields["spu"] == "B"
    assert fields["sku"] == "C"


# --- 造行值 -----------------------------------------------------------------

def _schema():
    return P.SheetSchema(
        sheet="pawly全球",
        fields={"spu": "E", "sku": "G", "site": "A", "sale": "L", "daily": "H"},
        ok=True,
    )


def test_build_column_values_writes_plain_spec_into_sku_column():
    """货号列写纯规格值，与历史人工写法（「5双」「直径32CM」）保持一致。"""
    item = {
        "spu": "7948115685", "sku_id": "98286921781",
        "sku_spec": "奶白+黑色/10双", "price": "299.68¥",
    }
    res = P.CollectResult(spu="7948115685", ok=True)
    values = P._build_column_values(item, res, _schema())
    assert values["G"] == "奶白+黑色/10双"
    # 写进去的值必须与判重键同源，否则写完立刻判不出「已入库」
    assert S.dedupe_key(item["spu"], values["G"]) == S.worklist_key(item)


def test_build_column_values_writes_per_sku_price_not_range():
    """销售价写该 SKU 自己的申报价。

    改造前这里拿的是 SPU 级 supplierPrice，对多 SKU 商品是区间串 "60.00~299.68¥"，
    被 _to_number 截成下限 60.0——实测 128 个商品里 89 个销售价因此写错。
    """
    item = {"spu": "7948115685", "sku_id": "98286921781",
            "sku_spec": "数量=10双", "price": "299.68¥"}
    res = P.CollectResult(spu="7948115685", ok=True)
    values = P._build_column_values(item, res, _schema())
    assert values["L"] == 299.68
    assert values["H"] == 299.68


def test_build_column_values_skips_sku_column_without_sku_id():
    """老清单没有 sku_id → 不写货号列（别往人家的货号列里塞空串）。"""
    item = {"spu": "123", "price": "10¥"}
    res = P.CollectResult(spu="123", ok=True)
    values = P._build_column_values(item, res, _schema())
    assert "G" not in values


def test_resolve_local_schema_only_mimics_allowed_cost_columns():
    """本地 Excel 与云端同口径：只仿成本链，参考价/叠加折扣保持空白。"""
    class Result:
        error = ""
        output = __import__("json").dumps({
            "字段列映射": {"spu": "D", "daily": "G", "discount": "H", "sale": "K"},
            "模板输入列": {"O": 25, "Q": 6.72, "R": 10},
            "常量输入列": {"P": 3},
            "sample_最后行公式与值": {
                "H": "=K2/G2",
                "T": "=L2+N2+O2+P2+Q2",
            },
        }, ensure_ascii=False)

    class Tool:
        async def execute(self, **_kwargs):
            return Result()

    schema = __import__("asyncio").run(P.resolve_sheet_schema(Tool(), "x.xlsx", "S"))

    assert schema.constant_columns == {"O": 25, "Q": 6.72, "R": 10}
    assert schema.formula_columns == {
        "H": "=K{r}/G{r}",
        "T": "=L{r}+N{r}+O{r}+P{r}+Q{r}",
    }


def test_build_column_values_leaves_formula_and_unavailable_columns_for_later():
    schema = P.SheetSchema(
        sheet="S",
        fields={"spu": "D", "daily": "G", "discount": "H", "sale": "K"},
        constant_columns={"O": 25, "Q": 6.72, "R": 10},
        ok=True,
    )

    values = P._build_column_values(
        {"spu": "1", "price": "46"}, P.CollectResult(spu="1", ok=True), schema
    )

    assert values == {"D": "1", "G": 46, "K": 46, "O": 25, "Q": 6.72, "R": 10}
    assert "H" not in values, "折扣由公式写入阶段生成，不能硬编码成数值 1"


def test_to_number_truncates_range_string():
    """钉住区间串的危险行为：_to_number 只取第一个数字。

    它本身没错（价格串确实形如 "46.10¥"），错的是喂给它区间串。这条用例是防回归的
    警戒线——若哪天又把 SPU 级 supplierPrice 直接塞进 price，这里的语义就会再次咬人。
    """
    assert P._to_number("60.00~299.68¥") == 60.0
    assert P._to_number("299.68¥") == 299.68
    assert P._to_number("1,299.00¥") == 1299.0


# --- 云端行：图片按 SKU 走 ---------------------------------------------------

def test_cloud_row_prefers_sku_image():
    """同 SPU 的不同颜色行要嵌各自的预览图，否则表里几行长得一模一样。"""
    item = {"spu": "1", "sku_id": "2", "sku_spec": "颜色=红",
            "price": "9¥", "image": "https://x/spu.jpg",
            "sku_image": "https://x/sku.jpg"}
    row = P._cloud_row(item, P.CollectResult(spu="1", ok=True), _schema(), 0, 2)
    assert row["image_url"] == "https://x/sku.jpg"


def test_cloud_row_falls_back_to_spu_image():
    """老清单没有 sku_image → 退回 SPU 主图。"""
    item = {"spu": "1", "price": "9¥", "image": "https://x/spu.jpg"}
    row = P._cloud_row(item, P.CollectResult(spu="1", ok=True), _schema(), 0, 2)
    assert row["image_url"] == "https://x/spu.jpg"


# --- 表头语义：允许照抄固定值的列要认得写法变体 ------------------------------

def test_mimic_title_matches_common_variants():
    """各 Sheet 表头是人手打的，成本列的写法五花八门。

    精确等值匹配下「操作费用」「ROS(%)」「空运头程费」会被整体漏掉——这些列是
    成本/利润公式的输入，漏掉就让新行公式算在空白上，错还会顺着公式链扩散。
    """
    from app.tool.wps_excel_tool import _is_mimic_title as m

    assert m("操作费") and m("操作费用")
    assert m("ros") and m("ROS") and m("ROS(%)") and m("ROS（%）")
    assert m("空运头程") and m("空运头程费") and m("空运头程 ")
    assert m("尾程运费") and m("尾程运费（美元）")
    assert m("毛利") and m("毛利率")
    # 反面：平台清单没有来源的列绝不能进来，否则会照抄上一行的值
    assert not m("加速器参考价格")
    assert not m("叠加折扣1")
    assert not m("筛选编号")
    assert not m("备注")


def test_mimic_columns_resolve_from_header_variants():
    header = {"A": "站点", "N": "空运头程费", "P": "操作费用", "R": "ROS(%)",
              "J": "加速器参考价格"}
    assert WpsExcelTool.collect_mimic_columns(header) == {"N", "P", "R"}


# --- 公式列：不再受成本列白名单限制，但不能盖掉逐商品写值的列 ----------------

def _inspect_stub(payload: dict):
    """伪造 WpsExcelTool.execute(action="inspect") 的返回。"""
    import json as _json

    class Result:
        error = ""
        output = _json.dumps(payload, ensure_ascii=False)

    class Tool:
        async def execute(self, **_kwargs):
            return Result()

    return Tool()


def test_local_schema_learns_formula_columns_outside_whitelist():
    """标题不在成本白名单里的计算列（含税成本/利润率）也要仿公式。

    旧口径只认 9 个固定标题，换个 Sheet 多一列「含税成本」或把「毛利」写成
    「毛利率」，新行这些列就整列空白。公式是本表自己的算法，抄逻辑不是抄数据。
    """
    tool = _inspect_stub({
        "字段列映射": {"spu": "D", "sale": "K"},
        "模板输入列": {},
        "sample_最后行公式与值": {
            "T": "=L2+N2",          # 成本，白名单内
            "W": "=T2*1.06",        # 含税成本，白名单外
            "X": "=U2/K2",          # 利润率，白名单外
        },
    })

    schema = __import__("asyncio").run(P.resolve_sheet_schema(tool, "x.xlsx", "S"))

    assert schema.formula_columns == {
        "T": "=L{r}+N{r}", "W": "=T{r}*1.06", "X": "=U{r}/K{r}",
    }


def _collect_book(tmp_path):
    """合成一份最小的「商品成本核算」结构工作簿，用于端到端跑 inspect。

    结构照抄真实表的关键特征：图片列是 DISPIMG、折扣/成本/利润是公式、操作费与 ros
    是逐行相同的常量、加速器参考价格逐行不同（不该被照抄）、销售价格列历史带公式
    （不该被仿走）。
    """
    import zipfile

    def row(rid, cells):
        return f'<row r="{rid}">' + "".join(cells) + "</row>"

    def c(ref, value=None, formula=None, style="2"):
        if formula is not None:
            return f'<c r="{ref}" s="{style}"><f>{formula}</f><v>0</v></c>'
        return f'<c r="{ref}" s="{style}" t="str"><v>{value}</v></c>'

    header = row(1, [
        c("A1", "站点", style="1"), c("B1", "SPU ID", style="1"),
        c("C1", "产品图片", style="1"), c("D1", "日常价", style="1"),
        c("E1", "折扣", style="1"), c("F1", "加速器参考价格", style="1"),
        c("G1", "销售价格", style="1"), c("H1", "采购价格", style="1"),
        c("I1", "操作费用", style="1"), c("J1", "ROS(%)", style="1"),
        c("K1", "成本", style="1"), c("L1", "利润率", style="1"),
    ])
    data = []
    for i, rid in enumerate((2, 3, 4), start=1):
        data.append(row(rid, [
            c(f"A{rid}", "哥伦比亚"), c(f"B{rid}", f"SPU-{i}"),
            c(f"C{rid}", formula=f'_xlfn.DISPIMG(&quot;ID_{i}&quot;,1)'),
            c(f"D{rid}", "100"), c(f"E{rid}", formula=f"G{rid}/D{rid}"),
            c(f"F{rid}", str(80 + i)),  # 逐行不同 → 不是常量、不该照抄
            c(f"G{rid}", formula=f"F{rid}*0.8"),  # 销售价历史带公式 → 不该仿
            c(f"H{rid}", "22"), c(f"I{rid}", "5"), c(f"J{rid}", "10"),
            c(f"K{rid}", formula=f"H{rid}+I{rid}"),
            c(f"L{rid}", formula=f"K{rid}/G{rid}"),
        ]))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<dimension ref="A1:L4"/><sheetData>' + header + "".join(data) +
        "</sheetData></worksheet>"
    )
    path = tmp_path / "采集表.xlsx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="xl/workbook.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="StoreA全球" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            "</Relationships>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cellXfs count="3">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0"/>'
            '<xf numFmtId="2" fontId="0" fillId="0" borderId="1" xfId="0"/>'
            "</cellXfs></styleSheet>",
        )
    return path


def test_local_inspect_runs_on_real_workbook_structure(tmp_path):
    """本地 inspect 必须能在真实 xlsx 结构上跑通，并按新口径解析。

    这条用例是防 `_row_cells` 那类回归的：它的正则只有一个捕获组，findall 返回的是
    字符串列表，调用处却按二元组解包——实测让 inspect 在任何真实工作簿上直接抛
    ValueError，本地采集路径整条不可用，而当时的单测全绿（没人端到端跑过 inspect）。
    """
    import asyncio as _asyncio
    import json as _json

    book = _collect_book(tmp_path)
    result = _asyncio.run(
        WpsExcelTool().execute(action="inspect", file_path=str(book),
                               sheet_name="StoreA全球")
    )
    assert not result.error, result.error
    info = _json.loads(result.output)

    assert info["字段列映射"]["spu"] == "B"
    assert info["字段列映射"]["image"] == "C"
    assert info["字段列映射"]["sale"] == "G"
    # 「操作费用」「ROS(%)」这类变体写法要认得出来，否则公式输入变空白
    assert info["模板输入列"] == {"I": 5, "J": 10}, "只照抄成本语义列的固定值"

    formulas = {k: v for k, v in info["sample_最后行公式与值"].items()
                if str(v).startswith("=")}
    assert "L" in formulas, "「利润率」不在旧白名单里，但它是公式列，必须仿"
    assert "K" in formulas and "E" in formulas
    assert "DISPIMG" in formulas["C"], "图片列原样返回，由 pipeline 侧过滤"

    schema = _asyncio.run(P.resolve_sheet_schema(
        WpsExcelTool(), str(book), "StoreA全球"))
    assert schema.ok
    assert schema.formula_columns == {
        "E": "=G{r}/D{r}", "K": "=H{r}+I{r}", "L": "=K{r}/G{r}",
    }, "图片列与销售价列都不能进公式表"
    assert schema.constant_columns == {"I": 5, "J": 10}

    values = P._build_column_values(
        {"spu": "SPU-9", "price": "46", "site": "哥伦比亚"},
        P.CollectResult(spu="SPU-9", ok=True), schema,
    )
    assert values["G"] == 46, "销售价按清单写，不被历史公式顶掉"
    assert "F" not in values, "加速器参考价格没有清单来源，必须留空"
    assert values["I"] == 5 and values["J"] == 10


def test_local_schema_never_mimics_formula_on_item_input_columns():
    """逐商品写值的列即便历史是公式也不能仿。

    实测教训：Leoaqr 表的「销售价格」列历史是 =参考价*叠加折扣，仿走就会用一个
    依赖空白列的公式顶掉平台申报价，新行销售价显示成 0。
    """
    tool = _inspect_stub({
        "字段列映射": {"spu": "D", "sale": "K", "daily": "G", "image": "E"},
        "模板输入列": {},
        "sample_最后行公式与值": {
            "K": "=J2*I2",                          # 销售价：逐商品写值，不许仿
            "G": "=K2*0.8",                         # 日常价：同上
            "E": '=_xlfn.DISPIMG("ID_x",1)',        # 图片列：永远排除
            "T": "=L2+N2",                          # 成本：仿
        },
    })

    schema = __import__("asyncio").run(P.resolve_sheet_schema(tool, "x.xlsx", "S"))

    assert schema.formula_columns == {"T": "=L{r}+N{r}"}
