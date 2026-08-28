"""包装清单件数与 SKU分类数量的一致性（平台强校验）。

【为什么单独一个文件】2026-08-27 商品 1051793179451（两件套裙套装）发布被接口打回：
    Mixed-Set SKU Accessories Num Sum Not Equal to Number of Pieces
平台校验的是【包装清单件数之和 == SKU分类填的数量】。原先包装清单是占位一件不填、
和恒为 0，只有单件商品（qty=1，和 0≠1）也不符——历史商品能过是因为它们的 SKU分类
被判成 1 且清单为空时平台没拦，套装商品一填 qty=2 就必然被打回。

这里测的是归一函数 _normalize_sku_judge：它是「模型给的清单不可信」这一前提下的最后
一道算术闸门，必须证明它真的能把不一致的输入掰回一致，而不是只在提示词里写一句要求。
"""
import json

from app.publish.pipeline import _normalize_sku_judge

INFO = {"title": "外贸26年欧美夏季新款女童牛仔花苞上衣收身牛仔裙套装",
        "attributes": {"套装件数": "两件套", "套装类型": "裙套装"}}


def _sum(judge):
    return sum(int(x["qty"]) for x in judge["packing"])


def test_模型给的清单件数和正确时原样保留():
    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": 2, "unit": "1",
         "packing": [{"name": "上衣", "qty": 1}, {"name": "半身裙", "qty": 1}]}, INFO)
    assert out["qty"] == 2
    assert _sum(out) == 2
    # 「上衣」不是平台选项，归一到表内的「便服上衣」（件数结构不动）
    assert [x["name"] for x in out["packing"]] == ["便服上衣", "半身裙"]


def test_件数和大于qty时以清单为准修正qty():
    # 模型列了 3 件却把 qty 写成 2：这是实测最常见的错法
    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": 2, "unit": "1",
         "packing": [{"name": "上衣", "qty": 1}, {"name": "短裤", "qty": 1},
                     {"name": "帽子", "qty": 1}]}, INFO)
    assert out["qty"] == 3 == _sum(out)


def test_件数和小于qty时同样对齐():
    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": 5, "unit": "1",
         "packing": [{"name": "上衣", "qty": 1}, {"name": "半身裙", "qty": 1}]}, INFO)
    assert out["qty"] == 2 == _sum(out)


def test_清单缺失时按qty补一项且和相等():
    out = _normalize_sku_judge({"skuCat": "3", "qty": 2, "unit": "1"}, INFO)
    assert _sum(out) == out["qty"] == 2
    # 名字取源「套装类型」（裙套装）再归一到平台词表里的「半身裙」
    assert out["packing"][0]["name"] == "半身裙"


def test_清单为空列表等同缺失():
    out = _normalize_sku_judge({"skuCat": "1", "qty": 1, "unit": "1", "packing": []}, INFO)
    assert _sum(out) == out["qty"] == 1


def test_单件商品也必须列一项():
    # qty=1 而清单为空时和为 0，一样过不了校验，故必须补出一项
    out = _normalize_sku_judge({"skuCat": "1", "qty": 1, "unit": "1"},
                               {"title": "女童连衣裙", "attributes": {}})
    assert len(out["packing"]) == 1
    assert _sum(out) == 1


def test_配件名空白项被剔除后仍能对齐():
    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": 2, "unit": "1",
         "packing": [{"name": "上衣", "qty": 1}, {"name": "  ", "qty": 1}]}, INFO)
    assert [x["name"] for x in out["packing"]] == ["便服上衣"]
    assert _sum(out) == out["qty"] == 1


def test_qty是字符串或脏值也不崩():
    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": "2", "unit": "1",
         "packing": [{"name": "上衣", "qty": "1"}, {"name": "半身裙", "qty": "x"}]}, INFO)
    # "x" 兜成 1，和为 2
    assert _sum(out) == out["qty"] == 2


def test_qty缺失或为0时至少按1件走():
    out = _normalize_sku_judge({"skuCat": "1", "qty": 0, "unit": "1"},
                               {"title": "T恤", "attributes": {}})
    assert out["qty"] >= 1 and _sum(out) == out["qty"]


def test_空判断也能产出可填的清单():
    # 预热结果丢了/模型整体返空时不能把空清单带到页面
    out = _normalize_sku_judge({}, {"title": "上衣", "attributes": {}})
    assert out["packing"] and _sum(out) == out["qty"] >= 1


def test_件数和永远等于qty是不变量():
    """随便给什么形状，出来的和必须等于 qty——这是平台校验的唯一条件。"""
    cases = [
        {},
        {"qty": 3},
        {"qty": 1, "packing": [{"name": "上衣", "qty": 2}]},
        {"qty": 2, "packing": [{"name": "上衣", "qty": 1}, {"name": "裤子", "qty": 2}]},
        {"qty": "4", "packing": [{"name": "睡衣", "qty": 4}]},
        {"qty": 2, "packing": [{"name": "", "qty": 9}]},
    ]
    for c in cases:
        out = _normalize_sku_judge(c, INFO)
        assert _sum(out) == out["qty"], f"{c} -> {json.dumps(out, ensure_ascii=False)}"


def test_填写JS带必要占位符与定位约定():
    from app.publish.pipeline import _JS_FILL_PACKING

    assert "__ITEMS__" in _JS_FILL_PACKING
    # 按「有 ant-select + 有加号」认列，不硬编码列号
    assert "icon_add_circle_outline" in _JS_FILL_PACKING
    assert "icon_cancel" in _JS_FILL_PACKING
    # 靠搜索过滤定位选项：171 项虚拟列表滚动收集实测只收到 60/154，收不全
    assert "ant-select-selection-search-input" in _JS_FILL_PACKING
    assert "rc-virtual-list-holder" not in _JS_FILL_PACKING
    # 判浮层可见只看 inline display，且按 aria-controls 的 listId 认自己那个
    assert r"display:\s*none" in _JS_FILL_PACKING
    assert "aria-controls" in _JS_FILL_PACKING
    # 回读要算件数和，不能只看「填过了」
    assert "want" in _JS_FILL_PACKING


def test_判断提示词点明件数和硬约束():
    """提示词里必须写明「相加正好等于」——归一是兜底，别让模型一开始就乱给。"""
    import inspect

    from app.publish.pipeline import judge_sku_category

    src = inspect.getsource(judge_sku_category)
    assert "packing" in src
    assert "正好等于" in src
    assert "平台强校验" in src
    # 词表内嵌进提示词：靠页面侧包含匹配挑不出「便服上衣 vs 西装上衣」（都 4 字）
    assert "_PACKING_ACCESSORY_WORDS" in src


def test_配件词表覆盖服装常见件():
    from app.publish.pipeline import _PACKING_ACCESSORY_WORDS as W

    # 都是 2026-08-27 在真站逐词搜索确认存在的词，别往里塞没验证过的
    for w in ["便服上衣", "西装上衣", "半身裙", "连衣裙", "短裤", "长裤", "T恤",
              "衬衫", "背心", "马甲", "卫衣", "毛衣", "风衣", "睡衣", "帽子",
              "腰带", "围巾", "手套", "领结", "蝴蝶结", "夹克", "大衣", "中筒袜"]:
        assert w in W, f"词表缺 {w}"
    assert len(W) == len(set(W)), "词表有重复项"
    # 实测搜索返回空的词绝不能进词表：模型照着选、页面侧一个都匹配不到
    for w in ["上衣", "裙子", "裤子", "外套", "袜子", "套装", "棉服", "羽绒服", "校服"]:
        assert w not in W, f"{w} 不在平台词表里，不该出现在候选清单"


def test_表外常用词被归一到表内词():
    from app.publish.pipeline import _PACKING_ACCESSORY_WORDS as W
    from app.publish.pipeline import _canon_accessory

    # 这些是实测平台搜不到、模型却很爱给的词
    for bad in ["外套", "棉服", "羽绒服", "冲锋衣", "袜子", "裤子", "裙子",
                "开衫", "校服", "运动服", "打底裤", "泳衣", "背带裤"]:
        got = _canon_accessory(bad)
        assert got in W, f"{bad} 归一成 {got}，仍不在平台词表里"


def test_归一保留已在表内的词():
    from app.publish.pipeline import _canon_accessory

    for w in ["便服上衣", "西装上衣", "半身裙", "连衣裙", "中筒袜", "夹克"]:
        assert _canon_accessory(w) == w


def test_通用词按最短候选归一():
    from app.publish.pipeline import _canon_accessory

    # 「上衣」「裙套装」有别名表兜着
    assert _canon_accessory("上衣") == "便服上衣"
    assert _canon_accessory("裙套装") == "半身裙"   # 源属性里的「裙套装」
    # 没有别名时走「表内包含该名、取最短」
    assert _canon_accessory("内衣") == "内衣背心"
    # 反向包含：该名含表内词，取最长命中
    assert _canon_accessory("牛仔夹克") == "夹克"
    assert _canon_accessory("加厚防寒夹克") == "防寒夹克"
    # 完全无从归一的词原样返回（页面侧还有一层包含匹配），【不做逐字猜】：
    # 按单字命中会把「护腕」判成「防护服」，错得比报错更难查
    assert _canon_accessory("护腕") == "护腕"
    assert _canon_accessory("") == ""


def test_别名表不与词表重复():
    from app.publish.pipeline import _PACKING_ACCESSORY_WORDS as W
    from app.publish.pipeline import _PACKING_ALIAS as A

    # 键必须是表外词（表内词进来会白绕一层且永不生效）
    for k in A:
        assert k not in W, f"别名键 {k} 已在词表内，多余"
    # 值必须落在表内，否则归一等于没做
    for k, v in A.items():
        assert v in W, f"别名 {k} -> {v} 的目标不在词表内"


def test_清单里的表外词也会被归一():
    from app.publish.pipeline import _PACKING_ACCESSORY_WORDS as W

    out = _normalize_sku_judge(
        {"skuCat": "3", "qty": 2, "unit": "1",
         "packing": [{"name": "外套", "qty": 1}, {"name": "裤子", "qty": 1}]}, INFO)
    names = [x["name"] for x in out["packing"]]
    assert names == ["夹克", "长裤"], names
    assert all(n in W for n in names)
    assert _sum(out) == out["qty"] == 2


def test_补出的那一项也在词表内():
    from app.publish.pipeline import _PACKING_ACCESSORY_WORDS as W

    # 源「套装类型」是「裙套装」，归一后应落到表内
    out = _normalize_sku_judge({"skuCat": "3", "qty": 2, "unit": "1"}, INFO)
    assert out["packing"][0]["name"] in W
    # 什么线索都没有时给的默认项同样要在表内
    out2 = _normalize_sku_judge({"qty": 1}, {"title": "x", "attributes": {}})
    assert out2["packing"][0]["name"] in W


if __name__ == "__main__":
    import sys
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
