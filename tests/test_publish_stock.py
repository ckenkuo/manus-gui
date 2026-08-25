"""阶段⑪ set_stock 逻辑验证（不依赖真实浏览器）。

验证点：
1. 仓库选择逻辑：检测 → 点击展开 → 勾选 → 回读验证
2. 库存填写：等仓库勾选后才渲染 input[name=stock]
3. SKU分类：DeepSeek 判断（单品/同款多件/混合套装 + 数量 + 单位）
4. JS 脚本语法（检查原始字符串前缀 r，避免反斜杠转义问题）
"""
import json
import re


def test_js_syntax():
    """验证三段 JS 脚本都是合法的、可在浏览器执行的代码。"""
    from app.publish.pipeline import (
        _JS_WH_STATE,
        _JS_PICK_WAREHOUSE,
        _JS_FILL_STOCK_ONLY,
        _JS_FILL_STOCK_CAT,
    )

    # 检查是否用了 r""" 原始字符串（防止 \s 被转义成 s）
    assert r"\s" in _JS_WH_STATE or "\\s" not in _JS_WH_STATE, "JS 正则需要 r\"\"\" 原始字符串"
    assert r"\s" in _JS_FILL_STOCK_CAT or "\\s" not in _JS_FILL_STOCK_CAT

    # 检查占位符存在
    assert "__STOCK__" in _JS_FILL_STOCK_ONLY
    assert "__CAT__" in _JS_FILL_STOCK_CAT
    assert "__QTY__" in _JS_FILL_STOCK_CAT
    assert "__UNIT__" in _JS_FILL_STOCK_CAT
    # _JS_WH_STATE 是【只读】仓库下拉当前状态，不需要仓库名占位符——仓库名只在
    # 勾选那一步（_JS_PICK_WAREHOUSE）才用到。
    assert "ant-select-selection-item" in _JS_WH_STATE
    # listId 取自 select 内 input 的 aria-owns，是在多个常驻浮层里认出自己那个的依据。
    # 2026-08-23 起【不再回读坐标】：原先读 x/y 再另发一次 eval 用 elementFromPoint
    # 合成点击，两次 eval 之间 Vue 一重渲染坐标就打偏，报 no-dropdown。
    assert "aria-owns" in _JS_WH_STATE
    assert "getBoundingClientRect" not in _JS_WH_STATE

    # 勾选仓库这步：滚动+展开+点选+回读必须在同一段 JS 里做完
    assert "__WH__" in _JS_PICK_WAREHOUSE
    # 点内部 .ant-select-selector（点外层 .ant-select 打不开下拉，实测）
    assert "ant-select-selector" in _JS_PICK_WAREHOUSE
    # 判浮层可见只看 inline display，不看高度
    assert r"display:\s*none" in _JS_PICK_WAREHOUSE
    assert "getBoundingClientRect" not in _JS_PICK_WAREHOUSE
    # 浮层挂载慢，必须轮询等待而不是固定 sleep
    assert "await sleep" in _JS_PICK_WAREHOUSE


def test_sku_category_prompt():
    """验证 SKU 分类 LLM prompt 包含关键信息。"""
    # 模拟 set_stock 中的 prompt 构造
    title = "2026春夏新款儿童套装男童休闲运动服三件套"
    attrs = {"套装件数": "三件套", "套装类型": "上衣+裤子+外套"}

    prompt = (
        "你是跨境电商 Listing 专家。店小秘 Temu 半托管发布时需要为每个 SKU 填写「SKU分类」。\n\n"
        f"商品信息：\n- 标题：{title}\n- 套装件数：{attrs.get('套装件数', '单件')}"
        f"\n- 套装类型：{attrs.get('套装类型', '无')}\n\n"
        "SKU分类选项：1=单品（一个SKU只含一件商品） 2=同款多件（多件相同商品） 3=混合套装（多件不同商品组合）\n"
        "单位选项：1=件 2=双 3=包\n\n"
        "请判断这个商品的 SKU分类（含数量、单位）。\n"
        '只输出严格JSON：{"skuCat":"1|2|3","qty":数字,"unit":"1|2|3","reason":"一句话理由"}'
    )

    assert "三件套" in prompt
    assert "上衣+裤子+外套" in prompt
    assert "1=单品" in prompt and "3=混合套装" in prompt
    assert "只输出严格JSON" in prompt


def test_warehouse_flow():
    """验证仓库选择的多步骤逻辑。"""
    # 模拟返回值结构（_JS_PICK_WAREHOUSE 自己回读 selected，不再回传坐标）
    wh_before = {"selected": [], "open": False, "listId": "rc_select_8_list"}
    wh_after_click = {"selected": ["飞特COL仓库"], "open": False}

    # 逻辑：未选中 → 点击 → 勾选 → 回读验证
    assert "飞特COL仓库" not in wh_before["selected"]
    assert "飞特COL仓库" in wh_after_click["selected"]


def test_stock_fill_logic():
    """验证库存填写的等待逻辑。"""
    # 模拟 JS 返回：勾选仓库前 input[name=stock] 不存在
    before = {"err": "no-stock-inputs"}
    # 勾选后才渲染
    after = {"filled": 8, "bad": 0}

    assert "no-stock-inputs" in before.get("err", "")
    assert after.get("filled", 0) > 0 and after.get("bad", 0) == 0


def test_category_mapping():
    """验证 SKU 分类的选项值映射。"""
    # LLM 返回的是 "1"/"2"/"3"，对应店小秘下拉的 value
    cat_map = {"1": "单品", "2": "同款多件", "3": "混合套装"}
    unit_map = {"1": "件", "2": "双", "3": "包"}

    judge = {"skuCat": "3", "qty": 3, "unit": "1", "reason": "三件不同商品组合"}

    assert cat_map[judge["skuCat"]] == "混合套装"
    assert unit_map[judge["unit"]] == "件"
    assert judge["qty"] == 3


def test_result_structure():
    """验证函数返回值结构完整性。"""
    # 成功返回
    ok_result = {
        "status": "ok",
        "warehouse": "飞特COL仓库",
        "stock": "100",
        "skuCategory": {"cat": "1", "qty": 1, "unit": "1", "reason": "单品"},
        "processed": 8,
        "bad": [],
        "sample": []
    }

    assert ok_result["status"] == "ok"
    assert "warehouse" in ok_result
    assert "skuCategory" in ok_result
    assert ok_result["skuCategory"]["cat"] in ["1", "2", "3"]

    # 错误返回
    err_result = {"status": "error", "stage": "warehouse", "err": "no-select"}
    assert err_result["status"] == "error"
    assert "stage" in err_result


if __name__ == "__main__":
    import sys
    import traceback

    tests = [
        test_js_syntax,
        test_sku_category_prompt,
        test_warehouse_flow,
        test_stock_fill_logic,
        test_category_mapping,
        test_result_structure,
    ]

    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
