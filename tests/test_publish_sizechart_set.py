"""套装尺码表：两张表的存在与分类分派（平台强校验）。

【为什么单独一个文件】2026-08-27 商品 1051793179451（两件套裙套装）在包装清单修好后
再次被接口打回：
    套装尺码模板数量不合法 / 您发布的产品是套装，尺码表2也需要设置
套装的两件各有自己的尺码维度（上衣量衣长胸围、半身裙量裙长腰围），平台因此要求两张
模板。这个校验和包装清单件数和同性质——只在服务端做，前端一点提示都没有：同日实测把
SKU分类下拉在 单品/同款多件/混合套装 三档间切换，尺码表2 的 label 始终没有
ant-form-item-required 类、控件文案也不变。故必须靠我们自己的套装判断。

这里测两件事：
1. 定位改造：原实现全靠 .skuAttrSizeChart，而那个类【只挂在第一张表上】，第二张的
   form-item 没有任何具名类——只看第一张会让套装商品被判成「尺码表已加」而永不补第二张。
2. 分类分派：弹窗的尺码分类预选值两张都是「女童装-半身裙」，跟随预选会让两张表都填成
   裙长/腰围全围。按包装清单的件别分派才对（便服上衣→上装、半身裙→半身裙）。
"""
import inspect
import re

from app.publish.pipeline import (
    _ACCESSORY_SIZE_CATEGORY,
    _JS_OPEN_SIZECHART_MODAL,
    _JS_SIZECHART_LOCATE,
    _JS_SIZECHART_STATE,
    _PACKING_ACCESSORY_WORDS,
    _sc_js,
    _size_category_for,
    add_sizechart,
)

# 2026-08-27 真站实测：该类目下尺码分类共 6 个选项（前缀随类目变，故只存关键词）
真站分类关键词 = ["半身裙", "下装", "连衣裙", "马甲", "连体衣", "上装"]


def test_按label定位而不是靠skuAttrSizeChart类():
    """.skuAttrSizeChart 只挂在第一张表上，第二张没有具名类，只能按 label 文字取。"""
    assert "尺码表2" in _JS_SIZECHART_LOCATE
    assert "label" in _JS_SIZECHART_LOCATE
    for js in (_JS_SIZECHART_STATE, _JS_OPEN_SIZECHART_MODAL):
        assert "__LOCATE__" in js and "__IDX__" in js
        # 不许再退回按类定位
        assert "querySelector('.skuAttrSizeChart')" not in js


def test_占位符替换后无残留且IDX正确():
    for js in (_JS_SIZECHART_STATE, _JS_OPEN_SIZECHART_MODAL):
        for w in (0, 1):
            out = _sc_js(js, w)
            assert not re.findall(r"__[A-Z_]+__", out), f"which={w} 仍有占位符"
            assert "_scItem" in out, "定位工具没注入"
            assert f"const IDX = {w}" in out


def test_live_state不再按类读尺码表():
    """续跑判据要能区分两张表，只看第一张会让套装商品跳过 ⑨ 再被打回同一个错。"""
    from app.publish.pipeline import _JS_LIVE_STATE

    # 查实际的选择器调用而不是字符串出现（注释里提到类名是允许的）
    assert "querySelector('.skuAttrSizeChart')" not in _JS_LIVE_STATE
    assert "sizechart2Added" in _JS_LIVE_STATE
    assert "sizechartCount" in _JS_LIVE_STATE
    # scArea 是改造前的变量名，残留会导致 ReferenceError（2026-08-27 真站踩过）
    assert "scArea" not in _JS_LIVE_STATE


def test_add_sizechart带which参数():
    sig = inspect.signature(add_sizechart)
    assert "which" in sig.parameters
    assert sig.parameters["which"].default == 0
    src = inspect.getsource(add_sizechart)
    # 两张表模板名必须不同：同名平台会当同一个模板，第二张覆盖第一张
    assert "tpl_name}2" in src or 'f"{tpl_name}2"' in src


def test_件别到尺码分类的映射都落在真站选项里():
    for acc, cat in _ACCESSORY_SIZE_CATEGORY.items():
        assert cat in 真站分类关键词, f"{acc} -> {cat} 不在真站的 6 个选项里"


def test_映射的键都是平台配件词表里的词():
    """键要与包装清单归一后的名字对得上，否则永远查不到。"""
    for acc in _ACCESSORY_SIZE_CATEGORY:
        assert acc in _PACKING_ACCESSORY_WORDS, f"{acc} 不在平台配件词表里"


def test_上衣类归上装_裙类归各自分类():
    assert _size_category_for("便服上衣") == "上装"
    assert _size_category_for("西装上衣") == "上装"
    assert _size_category_for("T恤") == "上装"
    assert _size_category_for("夹克") == "上装"
    assert _size_category_for("半身裙") == "半身裙"
    assert _size_category_for("连衣裙") == "连衣裙"
    assert _size_category_for("长裤") == "下装"
    assert _size_category_for("短裤") == "下装"
    assert _size_category_for("马甲") == "马甲"
    assert _size_category_for("连体睡衣") == "连体衣"


def test_映射不到时返回None而不是猜一个():
    """分类决定强制测量参数，猜错会填出维度对不上实物的表——宁可跟随平台预选。"""
    assert _size_category_for("帽子") is None
    assert _size_category_for("腰带") is None
    assert _size_category_for("手套") is None
    assert _size_category_for("") is None
    assert _size_category_for(None) is None


def test_两件套的两张表分类不同():
    """本次踩的坑：两张都跟随预选会同为「半身裙」，上衣那件维度就错了。"""
    packing = ["便服上衣", "半身裙"]
    cats = [_size_category_for(n) for n in packing]
    assert cats == ["上装", "半身裙"]
    assert cats[0] != cats[1], "两张表分类相同等于没分工"


def test_service按SKU分类决定要不要第二张():
    from app.publish.service import _st_sizechart

    src = inspect.getsource(_st_sizechart)
    # 2=同款多件 3=混合套装 都算套装
    assert '"2", "3"' in src or "'2', '3'" in src
    assert "which=1" in src
    # 分类按包装清单件别分派，不能两张都跟随预选
    assert "_size_category_for" in src
    # 页面没有第二张栏时不算失败（非套装类目只有一张）
    assert "no-sizechart-item" in src


def test_service续跑把第二张空着也算stale():
    from app.publish.service import _stale_form_stages

    src = inspect.getsource(_stale_form_stages)
    assert "sizechart2Added" in src
    # None（该类目无此栏）不能被判成 stale，只有 False（有栏但空）才重跑
    assert "is False" in src


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
