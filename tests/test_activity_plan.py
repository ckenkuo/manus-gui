"""plan_spu_activities 与申报价口径的纯函数用例。

plan_spu_activities 是「活动能不能报」这条业务规则的唯一实现，规划遍与识别矩阵共用，
所以这里锁死三件事：判定口径（全部货号达底价才入选）、note 文案（skip_nomatch 与矩阵
格子共用）、cells 覆盖被淘汰的活动（矩阵要能显示「为什么报不了」）。
另外锁死申报价的舍入口径（向下取整，与平台参考价同口径，见 compute_submit_price）。
"""
from app.activity import pipeline, service


def _items(*pairs):
    """[(货号, 日常价, 底价)] → 逐货号价格条目（形状同 read_costs 的 items）。"""
    return [
        {"label": label, "daily": daily, "sale": sale, "purchase": "12", "row_number": i}
        for i, (label, daily, sale) in enumerate(pairs, start=7)
    ]


def test_submit_price_truncates_to_cents_like_the_platform():
    """申报价必须【向下取整】到分，与平台参考价同口径。

    真机实测（2026-09-24）：平台的参考价 = 平台日常价 × 折扣率，向下取整：
      90cm 日常价 163.23 × 0.85 = 138.7455 → 平台给 138.74（四舍五入会给 138.75）
    用四舍五入时申报价就比平台上限高 1 分、必被拒（实测 2879383652 的 50cm 就是这样被拦下的）。
    """
    calc = pipeline.compute_submit_price(163.23, 0.85, 100.0)
    assert calc["submit_price"] == 138.74
    assert calc["within_floor"] is True
    # 精确中点也一并向下去，绝不冒出 1 分：188.5 × 0.25 = 47.125 → 47.12
    assert pipeline.compute_submit_price(188.5, 0.25, 47.12)["submit_price"] == 47.12
    # 整数结果不受影响
    assert pipeline.compute_submit_price(60.0, 0.9, 40.0)["submit_price"] == 54.0


def test_floor_equal_to_exact_product_is_unreportable():
    """底价恰好等于「日常价 × 折扣率」的精确值时，该活动报不了：

    188.88 × 0.85 = 160.548（精确），而平台参考价按向下取整只有 160.54 < 底价 160.548。
    真机实测（2026-09-24）填 160.55 被平台拒（高于参考价 160.54）。现在在初筛阶段就判
    不达底价——矩阵里直接看得见，不用等填价才失败。
    """
    items = _items(("30cm", 74.72, 45.0), ("50cm", 188.88, 160.548))
    plan = service.plan_spu_activities(items, [{"name": "85折档", "discount_rate": 0.85}])
    assert plan["selected"] == []
    assert plan["cells"]["85折档"]["verdict"] == "under_floor"
    assert plan["cells"]["85折档"]["skus"][1]["submit_price"] == 160.54
    # 换成 9 折（169.99 ≥ 160.548）就报得了
    nine = service.plan_spu_activities(items, [{"name": "9折档", "discount_rate": 0.9}])
    assert [s["activity"] for s in nine["selected"]] == ["9折档"]


def test_selected_requires_every_sku_to_pass_its_own_floor():
    """一个 SPU 两个货号：折后都达各自底价才入选；同日常价不同底价时按各自底价判。"""
    items = _items(("30cm", 74.72, 45.0), ("50cm", 188.88, 160.0))
    activities = [{"name": "85折档", "discount_rate": 0.85, "min_stock": 5}]
    plan = service.plan_spu_activities(items, activities)
    assert [s["activity"] for s in plan["selected"]] == ["85折档"]
    assert [s["submit_price"] for s in plan["selected"]] == [None]  # 多货号不下发单值
    cell = plan["cells"]["85折档"]
    assert cell["verdict"] == "pass" and cell["within_floor"] is True
    assert [(s["label"], s["submit_price"]) for s in cell["skus"]] == [
        ("30cm", 63.51), ("50cm", 160.54)]
    assert plan["rejected"] == []


def test_one_sku_under_floor_kills_whole_activity_with_reason():
    """任一货号穿底 → 整活动淘汰（提报是 SPU 级，不能只报部分货号），原因进 rejected 与格子 note。"""
    items = _items(("30cm", 74.72, 45.0), ("50cm", 188.88, 160.548))
    activities = [{"name": "65折档", "discount_rate": 0.65, "min_stock": None}]
    plan = service.plan_spu_activities(items, activities)
    assert plan["selected"] == []
    assert plan["rejected"] == ["活动「65折档」货号50cm 申报价122.77<底价160.548"]
    cell = plan["cells"]["65折档"]
    assert cell["verdict"] == "under_floor" and cell["within_floor"] is False
    assert cell["note"] == plan["rejected"][0]
    # 格子仍带逐货号算价，人工能看出是哪个货号穿底、差多少（48.568 向下取整 → 48.56）
    assert cell["skus"][0]["submit_price"] == 48.56


def test_activity_without_discount_rate_is_a_no_rate_cell():
    """无固定折扣率的活动（万人团/详见提报列表）无法确定性算价：既不入入选也不进淘汰原因。"""
    items = _items(("默认", 60.0, 40.0))
    two = [{"name": "万人团", "discount_rate": None}, {"name": "官方大促", "discount_rate": 0.9}]
    plan = service.plan_spu_activities(items, two)
    assert [s["activity"] for s in plan["selected"]] == ["官方大促"]
    assert plan["rejected"] == []
    assert plan["cells"]["万人团"]["verdict"] == "no_rate"
    assert "无固定折扣率" in plan["cells"]["万人团"]["note"]


def test_single_sku_keeps_legacy_single_value_fields():
    """单货号时保留旧单值字段（模板与日志在用），多货号置 None。"""
    single = service.plan_spu_activities(
        _items(("默认", 60.0, 40.0)), [{"name": "活动A", "discount_rate": 0.9}])
    selected = single["selected"][0]
    assert selected["daily_price"] == 60.0 and selected["submit_price"] == 54.0
    assert single["cells"]["活动A"]["submit_price"] == 54.0
    assert single["cells"]["活动A"]["floor_price"] == 40.0
