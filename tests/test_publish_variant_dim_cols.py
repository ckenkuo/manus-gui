# -*- coding: utf-8 -*-
"""变种维列/维度组的定位判据：名字随类目变，位置不变（2026-09-12 车贴那单）。

取证（Temu 商品 601101104447803 骷髅车贴，类目车贴）：变种表表头是
  ['预览图( 批量)', '型号', 'SKU货号 ( 一键生成 · 高级 )', 'EANUPCISBN (批量编辑)',
   '申报价格 (CNY) (批量)', '尺寸(cm)(批量)', '重量(g)(批量)', '建议售价(批量)',
   '型号', 'SKU分类 (批量)', '包装清单 (批量)']
唯一的变种维叫【型号】——既不是「颜色」也不是「尺码」，于是所有按这两个名字找列/找
复选框组的判据全部落空：⑩a 直接报 no-color-column 判失败（整单卡死，交 Manus 兜底
也无解，页面上确实没有这两列），⑦b 的 colorIdx=-1 让行序核对失效，⑦a 读不到 byColor。

按名字认列的路子此前已补过两次（2026-09-11 派对桌布唯一维装在「尺码」列、日志里另见
3C 类目的「存储容量」当第二维），每换一个类目就再补一次。故改按【结构位置】认：
变种表列布局恒为「预览图 | 变种维1 [| 变种维2 …] | SKU货号 | EAN | 申报价格 | …」，
两侧锚点列（预览图、SKU货号）在 5 份实测表头里逐字出现。判据收敛进
variant_dom._JS_DIM_COLS 一处，⑦a/⑦b/⑩a/⑩/续跑判定共用。

本文件不连浏览器：JS 片段用同一套规则的 Python 复刻做对照，另外钉住「各处都注入了
这段共用判据、没有谁私留一份按名字猜的旧代码」。
"""

import re

import pytest

from app.publish import pipeline as P
from app.publish import variant_dom


# 5 份真实表头（前 4 份来自实跑日志，最后一份是服装类的常态形态）
HEADS_REAL = {
    "车贴-型号": (
        ['预览图( 批量)', '型号', 'SKU货号 ( 一键生成 · 高级 )', 'EANUPCISBN (批量编辑)',
         '申报价格 (CNY) (批量)', '尺寸(cm)(批量)', '重量(g)(批量)', '建议售价(批量)',
         '型号', 'SKU分类 (批量)', '包装清单 (批量)'],
        ('型号', None)),
    "派对桌布-唯一维在尺码列": (
        ['预览图( 批量)', '尺码', 'SKU货号 ( 一键生成 · 高级 )', 'EANUPCISBN (批量编辑)',
         '申报价格 (CNY) (批量)', '尺寸(cm)(批量)', '重量(g)(批量)', '建议售价(批量)',
         '尺码', 'SKU分类 (批量)', '包装清单 (批量)'],
        ('尺码', None)),
    "仿真花-单颜色维": (
        ['预览图( 批量)', '颜色', 'SKU货号 ( 一键生成 · 高级 )', 'EANUPCISBN (批量编辑)',
         '申报价格 (CNY) (批量)'],
        ('颜色', None)),
    "3C-颜色加存储容量": (
        ['预览图( 批量)', '颜色', '存储容量', 'SKU货号 ( 一键生成 · 高级 )',
         'EANUPCISBN (批量编辑)', '申报价格 (CNY) (批量)', '尺寸(cm)(批量)',
         '重量(g)(批量)', '建议售价(批量)', '颜色', '存储容量', 'SKU分类 (批量)',
         '包装清单 (批量)'],
        ('颜色', '存储容量')),
    "服装-颜色加尺码": (
        ['预览图( 批量)', '颜色', '尺码', 'SKU货号 ( 一键生成 )', 'EANUPCISBN (批量编辑)',
         '申报价格 (CNY) (批量)'],
        ('颜色', '尺码')),
}


def _dim_idx(heads):
    """_JS_DIM_COLS 里 dimIdx 的 Python 等价实现（同一套规则，供离线对照）。"""
    i_prev = next((k for k, h in enumerate(heads) if '预览图' in h), -1)
    i_code = next((k for k, h in enumerate(heads) if 'SKU货号' in h), -1)
    cols = list(range(i_prev + 1 if i_prev >= 0 else 0, i_code)) if i_code > 0 else []
    if not cols:
        # 锚点列都没读到才退回按名字找（只为兜住表头文案改版）
        ic = next((k for k, h in enumerate(heads) if re.match('^颜色', h)), -1)
        isz = next((k for k, h in enumerate(heads)
                    if '尺码' in h and '尺码表' not in h), -1)
        cols = sorted({x for x in (ic, isz) if x >= 0})
    return (cols[0] if cols else -1, cols[1] if len(cols) > 1 else -1)


@pytest.mark.parametrize("name", list(HEADS_REAL))
def test_五份真实表头都能认出变种维列(name):
    """按结构位置认列：五种类目（含车贴的「型号」）一份都不能落空。"""
    heads, (want1, want2) = HEADS_REAL[name]
    c, s = _dim_idx(heads)
    assert c >= 0, f"{name} 认不出第一个变种维列"
    assert heads[c] == want1
    if want2 is None:
        assert s < 0, f"{name} 只有一个变种维，第二维应为 -1"
    else:
        assert s >= 0 and heads[s] == want2


def test_旧的按名字判据在车贴表头上会落空():
    """钉住这次的病根：/^颜色/ 与「尺码」两条在车贴表头上都匹配不到。

    这条断言不是重复上面的用例——它说明为什么必须改判据，而不是再给「型号」加一个词。
    """
    heads = HEADS_REAL["车贴-型号"][0]
    assert next((k for k, h in enumerate(heads) if re.match('^颜色', h)), -1) == -1
    assert next((k for k, h in enumerate(heads)
                 if '尺码' in h and '尺码表' not in h), -1) == -1
    # 而按结构位置能认出来
    assert _dim_idx(heads)[0] == 1


def test_第二张表的重复维度列不会被当成第二维():
    """表头尾部还有一组同名维度列（SKU分类区那张表），它们在 SKU货号【之后】。

    按结构位置取「预览图之后、SKU货号之前」天然只圈住前面那组；若改成「找出所有叫
    型号的列」就会把尾部那列也算进来、把单维商品误判成双维。
    """
    heads = HEADS_REAL["车贴-型号"][0]
    c, s = _dim_idx(heads)
    assert (c, s) == (1, -1)
    assert heads.index('SKU货号 ( 一键生成 · 高级 )') == 2


@pytest.mark.parametrize("js_name", [
    "_JS_READ_SKU_CODES", "_JS_FILL_SKU_CODES", "_JS_VARIANT_ROW_FILL",
    "_JS_SKU_PREVIEW_STATE", "_JS_FILL_VARIANT",
])
def test_各处变种表JS都注入共用判据而不是各写一套(js_name):
    """五段 JS 必须共用 _JS_DIM_COLS：只改一处会让读与写取到不同的列。

    读写判据不一致的后果实测过——⑩a 的读与填曾各写一套，无尺码类目下逐行核对整表
    row-moved、货号一个都填不进去。
    """
    js = getattr(P, js_name)
    assert "__DIM_COLS__" in js, f"{js_name} 没有注入共用判据"
    assert "dimIdx(" in js, f"{js_name} 没有走 dimIdx 取列"
    # 不许再私留按名字猜的旧判据
    assert "/^颜色/.test" not in js, f"{js_name} 仍在按「颜色」这个名字找列"


def test_共用判据本身按锚点列取而不是穷举维度名():
    js = variant_dom._JS_DIM_COLS
    assert "预览图" in js and "SKU货号" in js, "锚点列判据丢了"
    for word in ("型号", "存储容量"):
        assert word not in js, f"不该把「{word}」写进词表——维度名穷举不完"


def test_反选能落到非颜色命名的维度组():
    """反选 JS 不能只在「颜色」组里找：车贴那单的维度组叫「型号」。

    原实现找不到颜色组就直接返回 no-color-group，⑦a 剔配件色与 ⑦b「补不上预览图就
    反选该规格」在这类类目上都无从落地。
    """
    js = variant_dom._JS_UNCHECK_COLOR
    assert "isDimGroup" in js, "没有「其余变种维组」兜底"
    assert "no-color-group" not in js, "仍会因为没有颜色组而直接失败"
    # 颜色组仍要优先（真·颜色类目行为不变）
    assert "isColorGroup" in js
    # 【只反选不勾选】这条约束不许丢
    assert "input.checked" in js


def test_反选绝不清空整个维度():
    """整维只剩一个已勾选项时必须拒绝反选：清空变种维会让平台毁掉整张变种表。"""
    js = variant_dom._JS_UNCHECK_COLOR
    assert "last-checked-in-group" in js
    assert "checkedInGroup.length <= 1" in js
