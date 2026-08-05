# -*- coding: utf-8 -*-
"""协作表格整理管线的离线单测：日期解析、时间列挑选、行序判定、拆合并回填。

不碰网络：凡是要调 kdocs-cli 的地方都用假的 _call 顶掉。
"""
import pytest

from app.kdocs.cli import KdocsCliError
from app.kdocs.sheet_tidy import (
    MergeRegion, SheetPlan, SheetTidy, col_letter, parse_date_key,
)


class TestParseDateKey:
    """时间文本解析。这些表的时间列是人工填的，格式实测五花八门。"""

    @pytest.mark.parametrize("text, expect", [
        ("2026-05-26 23:50:28", (True, 2026, 5, 26, 23, 50)),
        ("2026/6/20 14:12", (True, 2026, 6, 20, 14, 12)),
        ("2026.8.2", (True, 2026, 8, 2, 0, 0)),
        ("2026-07-10", (True, 2026, 7, 10, 0, 0)),
        ("2026年8月2日", (True, 2026, 8, 2, 0, 0)),
    ])
    def test_带年份(self, text, expect):
        assert parse_date_key(text) == expect

    @pytest.mark.parametrize("text, expect", [
        ("6.25", (False, 0, 6, 25, 0, 0)),
        ("8.4-1", (False, 0, 8, 4, 1, 0)),
        ("1月27日", (False, 0, 1, 27, 0, 0)),
        ("3月12飞特", (False, 0, 3, 12, 0, 0)),
        ("4/4嘉运", (False, 0, 4, 4, 0, 0)),
    ])
    def test_无年份(self, text, expect):
        assert parse_date_key(text) == expect

    @pytest.mark.parametrize("text", [
        "", "   ", "已打包", "已打包1、24", "已下架", "XM2D24L011545", "1595G",
        "13.5月", "2026-13-45",
    ])
    def test_解析不出的返回None(self, text):
        assert parse_date_key(text) is None

    def test_带年份与无年份不可混比(self):
        """(False,0,8,4) < (True,2026,7,10) 会得出「8月4日早于7月10日」的荒谬结论，
        所以元组第一位区分两类，由调用方分组后再比。"""
        a = parse_date_key("8.4")
        b = parse_date_key("2026-07-10")
        assert a[0] is False and b[0] is True

    def test_同类可正常比较(self):
        assert parse_date_key("6.1") < parse_date_key("6.25")
        assert parse_date_key("2026-05-26 10:00") < parse_date_key("2026-05-26 11:00")
        assert parse_date_key("8.4") < parse_date_key("8.4-1")


class TestColLetter:
    @pytest.mark.parametrize("idx, letter", [
        (0, "A"), (1, "B"), (25, "Z"), (26, "AA"), (27, "AB"), (32, "AG"),
    ])
    def test_列索引转字母(self, idx, letter):
        assert col_letter(idx) == letter


class _FakeTidy(SheetTidy):
    """把 _call 顶掉的 SheetTidy：读表头/读区域都从内存里的假数据取。"""

    def __init__(self, header, col_values, data_rows=10):
        self._id_param = "file_id"
        self.doc = "fake"
        self._timeout_kw = {}
        self._sheets = {"S": {"id": 1, "row_to": data_rows,
                              "col_to": max(header) if header else 0,
                              "visible": True, "empty": False}}
        self._header = header
        self._col_values = col_values

    def _read(self, sheet, row_from, row_to, col_from, col_to):
        cells = []
        if row_from == 0:  # 表头行
            for ci, title in self._header.items():
                if col_from <= ci <= col_to:
                    cells.append({"rowFrom": 0, "rowTo": 0, "colFrom": ci,
                                  "colTo": ci, "cellText": title})
            return cells
        for ci, values in self._col_values.items():
            if not (col_from <= ci <= col_to):
                continue
            for offset, v in enumerate(values):
                r = 1 + offset
                if row_from <= r <= row_to:
                    cells.append({"rowFrom": r, "rowTo": r, "colFrom": ci,
                                  "colTo": ci, "cellText": v})
        return cells


def _plan(rows=10):
    return SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                     data_row_from=1, data_row_to=rows, col_to=5)


class TestPickDateCol:
    def test_平台列优先于人工列(self):
        """平台时间是系统写的、格式统一；采购日期人工填、常年缺值，必须让前者胜出。"""
        t = _FakeTidy({0: "订单号", 1: "采购日期", 2: "平台创建时间"}, {})
        assert t.pick_date_col({0: "订单号", 1: "采购日期", 2: "平台创建时间"})[0] == 2

    def test_客人下单日期优先于采购日期(self):
        h = {0: "采购日期", 1: "客人下单日期"}
        assert t_pick(h) == 1

    def test_没有时间列返回负一(self):
        h = {0: "订单号", 1: "尺码", 2: "产品图片"}
        assert t_pick(h) == -1


def t_pick(header):
    return _FakeTidy(header, {}).pick_date_col(header)[0]


class TestCheckOrder:
    def test_升序表判定可反转(self):
        vals = ["2026-06-01", "2026-06-05", "2026-06-09", "2026-06-20"]
        t = _FakeTidy({0: "平台创建时间"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.cmp_pairs == 3 and p.asc_pairs == 3
        assert p.order_ok is True

    def test_降序表判定不可反转(self):
        """已经是倒序的表如果照样反转，会被排成正序——正好排反，必须拦住。"""
        vals = ["2026-06-20", "2026-06-09", "2026-06-05", "2026-06-01"]
        t = _FakeTidy({0: "平台创建时间"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.asc_pairs == 0 and p.cmp_pairs == 3
        assert p.order_ok is False

    def test_少量乱序仍可反转(self):
        """人工维护的表难免有个别插错位置的行，97% 升序应当放行。"""
        vals = [f"2026-06-{d:02d}" for d in range(1, 21)]
        vals[7], vals[8] = vals[8], vals[7]
        t = _FakeTidy({0: "平台创建时间"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.order_ok is True

    def test_相等值不计入判定(self):
        """相等既不支持也不反对升序假设，计入会稀释占比。"""
        vals = ["6.1", "6.1", "6.1", "6.2"]
        t = _FakeTidy({0: "采购日期"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.cmp_pairs == 1 and p.asc_pairs == 1

    def test_全是不可解析文本则无可比对(self):
        vals = ["已打包", "已打包1、24", "已下架"]
        t = _FakeTidy({0: "国内发出时间"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.cmp_pairs == 0
        assert p.asc_ratio is None
        assert p.order_ok is False  # 判断不了就不放行

    def test_找不到时间列不写列名(self):
        t = _FakeTidy({0: "订单号", 1: "尺码"}, {}, data_rows=3)
        p = _plan(3)
        t.check_order(p)
        assert p.date_col == "" and p.order_ok is False

    def test_跨年混排不误判为降序(self):
        """无年份的 12月→1月 是正常跨年递增，但元组比较会当成降序。
        这类表 cmp_pairs 里会出现降序对，占比降下来后 order_ok 为 False，
        管线拒绝写入并要求 --force —— 这正是期望行为（宁可拦住让人看一眼）。"""
        vals = ["12.20", "12.28", "1.5", "1.12"]
        t = _FakeTidy({0: "采购日期"}, {0: vals}, data_rows=len(vals))
        p = _plan(len(vals))
        t.check_order(p)
        assert p.asc_pairs == 2 and p.cmp_pairs == 3
        assert p.order_ok is False


class TestReverseVerify:
    """排序后必须读回验证：range_sort 静默失效时不能误报成功。"""

    def _tidy(self, top_value):
        """top_value 是排序后序号列首行的值：等于 N 表示排序生效。"""
        class T(_FakeTidy):
            def __init__(self):
                super().__init__({0: "订单号"}, {})
                self.sorted_called = False

            def _call(self, action, payload, retry_5xx=False):
                if action == "range-sort":
                    self.sorted_called = True
                return {}

            def _read(self, sheet, row_from, row_to, col_from, col_to):
                return [{"rowFrom": row_from, "rowTo": row_from,
                         "colFrom": col_from, "colTo": col_from,
                         "cellText": top_value}]
        return T()

    def test_排序生效则记录行数(self):
        t = self._tidy("5")           # 5 行数据，降序后首行应为 5
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=5, col_to=3)
        t.reverse_rows(p)
        assert t.sorted_called is True
        assert p.reversed_rows == 5

    def test_排序静默失效则抛错(self):
        """序号列首行仍是 1 说明一行都没动——必须抛错，不能当成功。"""
        t = self._tidy("1")
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=5, col_to=3)
        with pytest.raises(KdocsCliError, match="排序未生效"):
            t.reverse_rows(p)
        assert p.reversed_rows == 0

    def test_单行表不排序(self):
        t = self._tidy("1")
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=1, col_to=3)
        t.reverse_rows(p)
        assert t.sorted_called is False


class TestReverseByRewrite:
    """range_sort 在个别表上失效时的降级路径：读出来倒序写回。"""

    def _tidy(self, rows, col_to=2):
        """rows 是 [{列索引: 值}]，模拟数据区各行内容。"""
        class T(_FakeTidy):
            def __init__(self):
                super().__init__({}, {})
                self.written = []

            def _read(self, sheet, row_from, row_to, col_from, col_to_):
                cells = []
                for i, row in enumerate(rows):
                    r = 1 + i
                    if not (row_from <= r <= row_to):
                        continue
                    for ci, v in row.items():
                        cells.append({"rowFrom": r, "rowTo": r, "colFrom": ci,
                                      "colTo": ci, "fmlaText": v})
                return cells

            def _call(self, action, payload, retry_5xx=False):
                if action == "range-data-batch-update":
                    self.written.extend(payload["range_data"])
                return {}
        return T()

    def _result(self, t, n, col_to):
        """从写入操作还原出各行内容。"""
        out = [{} for _ in range(n)]
        for op in t.written:
            r = op["row_from"] - 1
            if 0 <= r < n and op["formula"]:
                out[r][op["col_from"]] = op["formula"]
        return out

    def test_行序被反转(self):
        rows = [{0: "a"}, {0: "b"}, {0: "c"}]
        t = self._tidy(rows)
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=3, col_to=0)
        t.reverse_rows_by_rewrite(p)
        got = self._result(t, 3, 0)
        assert [r.get(0) for r in got] == ["c", "b", "a"]
        assert p.reversed_rows == 3
        assert p.reversed_by_rewrite is True

    def test_DISPIMG公式随行搬动(self):
        """DISPIMG 是公式引用，搬公式即等于搬图——这条降级路径能用的前提。"""
        rows = [
            {0: "o1", 1: '=DISPIMG("ID_AAA",1)'},
            {0: "o2", 1: '=DISPIMG("ID_BBB",1)'},
        ]
        t = self._tidy(rows)
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=2, col_to=1)
        t.reverse_rows_by_rewrite(p)
        got = self._result(t, 2, 1)
        # 订单号与图片必须成对搬动，不能错位
        assert got[0] == {0: "o2", 1: '=DISPIMG("ID_BBB",1)'}
        assert got[1] == {0: "o1", 1: '=DISPIMG("ID_AAA",1)'}

    def test_空单元格显式写空串(self):
        """不写空串的话，原行残留值会留在新行上造成串行。"""
        rows = [{0: "a", 1: "x"}, {0: "b"}]
        t = self._tidy(rows)
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=2, col_to=1)
        t.reverse_rows_by_rewrite(p)
        # 第 1 行（原末行 b）的 B 列必须被显式写空，覆盖掉原来的 x
        first_b = [op for op in t.written
                   if op["row_from"] == 1 and op["col_from"] == 1]
        assert first_b and first_b[-1]["formula"] == ""

    def test_单行表不重写(self):
        t = self._tidy([{0: "a"}])
        p = SheetPlan(sheet_name="S", worksheet_id=1, header_row=1,
                      data_row_from=1, data_row_to=1, col_to=0)
        t.reverse_rows_by_rewrite(p)
        assert t.written == [] and p.reversed_rows == 0


class TestMergeRegion:
    def test_跨行与跨列区分(self):
        assert MergeRegion(8, 9, 2, 2).spans_rows is True
        assert MergeRegion(8, 9, 2, 2).spans_cols is False
        assert MergeRegion(0, 0, 0, 3).spans_rows is False
        assert MergeRegion(0, 0, 0, 3).spans_cols is True

    def test_区域标签是1based的A1记法(self):
        assert MergeRegion(8, 9, 2, 2).label() == "C9:C10"
        assert MergeRegion(17, 18, 4, 4).label() == "E18:E19"


class TestUnmergeFill:
    """拆合并后要把原值复制到区域内每一行（用户要求「复制相同的内容填充」）。"""

    def test_回填跳过左上角且覆盖其余单元格(self):
        calls = []

        class T(_FakeTidy):
            def _call(self, action, payload, retry_5xx=False):
                calls.append((action, payload))
                return {}

        t = T({0: "订单号"}, {})
        p = _plan(10)
        # C9:C10 跨两行一列：拆开后只需回填 C10 一格
        p.row_merges = [MergeRegion(8, 9, 2, 2, "PO-211-0411")]
        t.unmerge_rows(p)

        assert p.unmerged == 1
        assert p.filled_cells == 1
        merges = [c for c in calls if c[0] == "merge-range"]
        assert len(merges) == 1
        # across=true 对单列区域即「拆开」——kdocs 没有独立的 unmerge
        assert merges[0][1]["across"] is True
        assert merges[0][1]["range"] == "C9:C10"

        writes = [c for c in calls if c[0] == "range-data-batch-update"]
        ops = [op for _, pl in writes for op in pl["range_data"]]
        assert len(ops) == 1
        assert ops[0]["row_from"] == 9 and ops[0]["col_from"] == 2
        assert ops[0]["formula"] == "PO-211-0411"

    def test_跨行跨列的块逐列拆分(self):
        """整块传 across=true 会把每行横向合并，反而更糟，必须逐列调。"""
        calls = []

        class T(_FakeTidy):
            def _call(self, action, payload, retry_5xx=False):
                calls.append((action, payload))
                return {}

        t = T({0: "订单号"}, {})
        p = _plan(10)
        p.row_merges = [MergeRegion(6, 7, 1, 3, "X")]  # B7:D8
        t.unmerge_rows(p)

        ranges = [pl["range"] for a, pl in calls if a == "merge-range"]
        assert ranges == ["B7:B8", "C7:C8", "D7:D8"]
        # 3 列 x 2 行 = 6 格，减去左上角 = 5 格回填
        assert p.filled_cells == 5

    def test_仅跨列合并也要拆(self):
        """range_sort 遇区域内任何合并都静默失效，所以跨列合并不能留。
        2026-08-05 实测：Pawly牛仔裤 只拆了跨行合并，排序整个白跑还误报成功。"""
        calls = []

        class T(_FakeTidy):
            def _call(self, action, payload, retry_5xx=False):
                calls.append((action, payload))
                return {}

        t = T({0: "订单号"}, {})
        p = _plan(10)
        p.col_merges = [MergeRegion(0, 0, 1, 3, "标题")]  # B1:D1 仅跨列
        t.unmerge_rows(p)

        ranges = [pl["range"] for a, pl in calls if a == "merge-range"]
        assert ranges == ["B1:B1", "C1:C1", "D1:D1"]
        assert p.unmerged == 1

    def test_跨行与跨列合并一起拆(self):
        calls = []

        class T(_FakeTidy):
            def _call(self, action, payload, retry_5xx=False):
                calls.append((action, payload))
                return {}

        t = T({0: "订单号"}, {})
        p = _plan(10)
        p.row_merges = [MergeRegion(8, 9, 2, 2, "A")]
        p.col_merges = [MergeRegion(0, 0, 1, 2, "B")]
        t.unmerge_rows(p)
        assert p.unmerged == 2

    def test_空值合并不回填(self):
        calls = []

        class T(_FakeTidy):
            def _call(self, action, payload, retry_5xx=False):
                calls.append((action, payload))
                return {}

        t = T({0: "订单号"}, {})
        p = _plan(10)
        p.row_merges = [MergeRegion(8, 9, 2, 2, "")]
        t.unmerge_rows(p)
        assert p.unmerged == 1 and p.filled_cells == 0
