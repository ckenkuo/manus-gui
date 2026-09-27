"""识别矩阵落盘的用例：往返、损坏兜底、原子写不留 .tmp、并库时丢弃消失的活动。"""
from app.activity import matrix


def _price(*spus):
    return {
        spu: {
            "items": [{"label": "默认", "daily": 60.0, "sale": 40.0, "purchase": "12"}],
            "cells": {
                "官方大促": {"verdict": "pass", "submit_price": 54.0, "floor_price": 40.0,
                         "within_floor": True, "sku_count": 1,
                         "skus": [{"label": "默认", "daily": 60.0, "sale": 40.0,
                                   "submit_price": 54.0}], "note": ""},
                "万人团": {"verdict": "no_rate", "submit_price": None, "floor_price": None,
                        "within_floor": False, "sku_count": 0, "skus": [], "note": "无固定折扣率"},
            },
        }
        for spu in spus
    }


def _activities(*names):
    return [{"name": name, "discount_rate": 0.9, "min_stock": 5} for name in names]


def test_save_then_load_roundtrip_leaves_no_tmp_file(tmp_path):
    data = matrix.apply_scan(matrix.empty(), matrix.today(), "https://kdocs.cn/l/x", "店A", "全球",
                             _activities("官方大促", "万人团"), _price("111"), {})
    path = matrix.save(data)
    assert path.endswith(f"activity-matrix-{matrix.today()}.json")
    assert not (tmp_path / "activity-matrix" / f"activity-matrix-{matrix.today()}.json.tmp").exists()
    loaded = matrix.load()
    assert loaded["document"] == "https://kdocs.cn/l/x" and loaded["sheet"] == "店A"
    assert [a["name"] for a in loaded["activities"]] == ["官方大促", "万人团"]
    assert loaded["counts"] == {"cells": 1, "eligible": 0, "ineligible": 0, "unknown": 1}


def test_load_missing_or_broken_file_returns_empty(tmp_path):
    assert matrix.load()["activities"] == []
    target = tmp_path / "activity-matrix"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"activity-matrix-{matrix.today()}.json").write_text("{坏文件", encoding="utf-8")
    assert matrix.load()["activities"] == []  # 损坏当没有缓存，不抛错


def test_apply_scan_drops_vanished_activities_and_keeps_today_eligibility():
    """资格按「活动名」缓存：活动下架（期数换轮）时整列丢弃，新出现的活动回到未扫。"""
    prev = matrix.apply_scan(
        matrix.empty(), matrix.today(), "doc", "店A", "全球",
        _activities("官方大促", "限时秒杀"), _price("111", "222"),
        {"111": {"官方大促": {"eligible": False, "note": "详情页查询结果为 0",
                          "scanned_at": "2026-09-24 11:00:00"},
                 "限时秒杀": {"eligible": True, "note": "", "scanned_at": "2026-09-24 11:01:00"}},
         "222": {"官方大促": {"eligible": True, "note": "", "scanned_at": "2026-09-24 11:02:00"}}})
    # 计数只算价格初筛通过的格子（万人团/no_rate 不进），两个 SPU 各一格
    assert prev["counts"] == {"cells": 2, "eligible": 1, "ineligible": 1, "unknown": 0}

    # 下一轮：限时秒杀下架、新增「半托管85折」；111 这次只探了新的那个活动
    after = matrix.apply_scan(
        prev, matrix.today(), "doc", "店A", "全球",
        _activities("官方大促", "半托管85折"), _price("111"),
        {"111": {"半托管85折": {"eligible": True, "note": "", "scanned_at": "2026-09-24 12:00:00"}}})
    assert after["eligibility"] == {
        "111": {"官方大促": {"eligible": False, "note": "详情页查询结果为 0",
                          "scanned_at": "2026-09-24 11:00:00"},
                "半托管85折": {"eligible": True, "note": "", "scanned_at": "2026-09-24 12:00:00"}},
    }  # 222 不再扫 → 整条丢弃；限时秒杀 下架 → 整列丢弃


def test_flatten_cells_merges_price_and_eligibility():
    data = matrix.apply_scan(
        matrix.empty(), matrix.today(), "doc", "店A", "全球", _activities("官方大促", "万人团"),
        _price("111"), {"111": {"官方大促": {"eligible": True, "note": "", "scanned_at": "t"}}})
    cells = {cell["activity"]: cell for cell in matrix.flatten_cells(data)}
    assert cells["官方大促"]["eligible"] is True and cells["官方大促"]["from_cache"] is True
    assert cells["官方大促"]["submit_price"] == 54.0 and cells["官方大促"]["sku_count"] == 1
    # 格子是概览视图：不带逐货号明细（活动近百，一格一份明细会让 scan_start 涨到几 MB）
    assert "skus" not in cells["官方大促"]
    assert data["price"]["111"]["cells"]["官方大促"]["skus"][0]["label"] == "默认"  # 文件里仍有明细
    # 价格初筛就没过的活动（无固定折扣率）不参与资格统计，也没有缓存标记
    assert cells["万人团"]["eligible"] is None and cells["万人团"]["verdict"] == "no_rate"
    assert data["counts"] == {"cells": 1, "eligible": 1, "ineligible": 0, "unknown": 0}
