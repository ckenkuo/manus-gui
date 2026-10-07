"""plan_spu_activities 与申报价口径的纯函数用例。

plan_spu_activities 是「活动能不能报」这条业务规则的唯一实现，规划遍与识别矩阵共用，
所以这里锁死三件事：判定口径（SPU 级门槛——活动折扣率 ≥ 毛利率最低货号的 底价÷日常价，
2026-09-29 用户改定）、note 文案（skip_nomatch 与矩阵格子共用）、cells 覆盖被淘汰的
活动（矩阵要能显示「为什么报不了」）。
另外锁死申报价的舍入口径：【填价】向下取整（与平台参考价同口径，见 compute_submit_price），
【门槛判定】精确比较不打折到分（见 pipeline.rate_reaches_floor）——两者分工不同，混用
就在 2879383652×限时秒杀 上出过 8 厘误杀。
"""
from app.activity import pipeline, service


def _items(*rows):
    """[(货号, 日常价, 底价, 毛利率)] → 逐货号价格条目（形状同 read_costs 的 items）。"""
    return [
        {"label": label, "daily": daily, "sale": sale, "margin": margin,
         "purchase": "12", "row_number": i}
        for i, (label, daily, sale, margin) in enumerate(rows, start=7)
    ]


def test_submit_price_truncates_to_cents_like_the_platform():
    """申报价必须【向下取整】到分，与平台参考价同口径（填价口径，与门槛判定无关）。

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


def test_floor_equal_to_exact_product_is_reportable():
    """底价恰好等于「日常价 × 折扣率」的精确值时可报：门槛判定不打折到分。

    真机案例（2026-09-29 2879383652×限时秒杀）：50cm 底价 160.548 = 188.88×0.85 精确值，
    旧口径拿截断后的申报价 160.54 比底价（差 8 厘）把整活动误杀；新口径精确比较 → 85折达标。
    填价仍是截断口径（平台参考价只给到分，160.54 是平台上限）。
    """
    items = _items(("30cm", 74.72, 45.0, 25.56), ("50cm", 188.88, 160.548, 15.88))
    plan = service.plan_spu_activities(items, [{"name": "85折档", "discount_rate": 0.85}])
    assert [s["activity"] for s in plan["selected"]] == ["85折档"]
    cell = plan["cells"]["85折档"]
    assert cell["verdict"] == "pass" and cell["within_floor"] is True
    assert cell["skus"][1]["submit_price"] == 160.54
    # 再深一档（0.84 × 188.88 = 158.66 < 160.548）就低于门槛、整活动淘汰
    deeper = service.plan_spu_activities(items, [{"name": "84折档", "discount_rate": 0.84}])
    assert deeper["selected"] == []
    assert deeper["cells"]["84折档"]["verdict"] == "under_floor"


def test_gate_is_the_lowest_margin_skus_floor_ratio():
    """门槛 = 毛利率最低货号的「底价÷日常价」（SPU 级判定，不再逐货号票决）。

    30cm 毛利 25.56 / 50cm 毛利 15.88 → 门槛货号是 50cm（可打 160/188.88 ≈ 84.7折），
    85折活动达标入选；30cm 的底价（45/74.72 ≈ 60折）不参与判定。
    """
    items = _items(("30cm", 74.72, 45.0, 25.56), ("50cm", 188.88, 160.0, 15.88))
    activities = [{"name": "85折档", "discount_rate": 0.85, "min_stock": 5}]
    plan = service.plan_spu_activities(items, activities)
    assert [s["activity"] for s in plan["selected"]] == ["85折档"]
    assert [s["submit_price"] for s in plan["selected"]] == [None]  # 多货号不下发单值
    cell = plan["cells"]["85折档"]
    assert cell["verdict"] == "pass" and cell["within_floor"] is True
    assert [(s["label"], s["submit_price"]) for s in cell["skus"]] == [
        ("30cm", 63.51), ("50cm", 160.54)]
    assert plan["rejected"] == []


def test_non_binding_sku_below_floor_still_selected():
    """非门槛货号破底不再一票否决：报名是 SPU 级，门槛只认毛利率最低的货号。

    30cm 毛利更厚（25.56 > 15.88），其底价 70 高于 85折价 63.51——旧规则整活动淘汰，
    新口径照报（毛利厚 = 折扣空间大，门槛货号扛得住它必然扛得住）。
    """
    items = _items(("30cm", 74.72, 70.0, 25.56), ("50cm", 188.88, 160.548, 15.88))
    plan = service.plan_spu_activities(items, [{"name": "85折档", "discount_rate": 0.85}])
    assert [s["activity"] for s in plan["selected"]] == ["85折档"]


def test_binding_sku_below_threshold_rejects_activity_with_reason():
    """门槛货号扛不住 → 整活动淘汰，原因（点明门槛货号与最低折扣）进 rejected 与格子 note。"""
    items = _items(("30cm", 74.72, 45.0, 25.56), ("50cm", 188.88, 160.548, 15.88))
    activities = [{"name": "65折档", "discount_rate": 0.65, "min_stock": None}]
    plan = service.plan_spu_activities(items, activities)
    assert plan["selected"] == []
    assert plan["rejected"] == [
        "活动「65折档」6.5折 低于该 SPU 可打的最低折扣 8.5折"
        "（毛利率最低的货号50cm：底价160.548÷日常价188.88）"]
    cell = plan["cells"]["65折档"]
    assert cell["verdict"] == "under_floor" and cell["within_floor"] is False
    assert cell["note"] == plan["rejected"][0]
    # 格子仍带逐货号算价，人工能看出各货号填价会是多少（48.568 向下取整 → 48.56）
    assert cell["skus"][0]["submit_price"] == 48.56


def test_missing_margin_fails_closed():
    """毛利率读不到就没法定门槛货号：fail-closed 全部格子 no_cost，绝不猜。"""
    items = _items(("默认", 60.0, 40.0, None))
    plan = service.plan_spu_activities(items, [{"name": "活动A", "discount_rate": 0.9}])
    assert plan["selected"] == [] and plan["rejected"] == []
    assert plan["cells"]["活动A"]["verdict"] == "no_cost"
    assert "毛利率" in plan["cells"]["活动A"]["note"]


def test_activity_without_discount_rate_is_a_no_rate_cell():
    """无固定折扣率的活动（万人团/详见提报列表）无法确定性算价：既不入入选也不进淘汰原因。"""
    items = _items(("默认", 60.0, 40.0, 20.0))
    two = [{"name": "万人团", "discount_rate": None}, {"name": "官方大促", "discount_rate": 0.9}]
    plan = service.plan_spu_activities(items, two)
    assert [s["activity"] for s in plan["selected"]] == ["官方大促"]
    assert plan["rejected"] == []
    assert plan["cells"]["万人团"]["verdict"] == "no_rate"
    assert "无固定折扣率" in plan["cells"]["万人团"]["note"]


def test_single_sku_keeps_legacy_single_value_fields():
    """单货号时保留旧单值字段（模板与日志在用），多货号置 None。"""
    single = service.plan_spu_activities(
        _items(("默认", 60.0, 40.0, 20.0)), [{"name": "活动A", "discount_rate": 0.9}])
    selected = single["selected"][0]
    assert selected["daily_price"] == 60.0 and selected["submit_price"] == 54.0
    assert single["cells"]["活动A"]["submit_price"] == 54.0
    assert single["cells"]["活动A"]["floor_price"] == 40.0
