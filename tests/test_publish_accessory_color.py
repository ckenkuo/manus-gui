"""⑦a 剔配件色：变种表里只有单行有源数据的颜色要反选（2026-09-01 真站取证）。

取证（草稿 184807703138719835，源 offer 890185900190「魔法精灵蓬蓬裙」）：
1688 商家把「主商品 + 配件」放在同一 offer 的颜色维里卖——主色 5 个码，配件
「紫精灵头纱」只有 M 一个码。平台按 颜色×尺码 展开完整笛卡尔积（2×5=10 行），
配件色多出来的 4 行没有任何源数据（货号/申报价/重量全空），也正是 ⑦b 那 4 行
预览图替换失败的真因——它们压根不对应任何源 SKU。

判据刻意落在【页面变种表】而不是源 skus：源颜色名（紫精灵头纱）与页面颜色维
（平台色板的「红色」）永远对不上，按源名找复选框必然找不到目标。
"""
import pytest

from app.publish.pipeline import accessory_colors_from_rows


def test_取证单_主色5行配件色1行():
    by = {"白色": {"total": 5, "filled": 5, "filledSizes": ["XS", "S", "M", "L", "XL"]},
          "红色": {"total": 5, "filled": 1, "filledSizes": ["M"]}}
    assert accessory_colors_from_rows(by) == ["红色"]


def test_多个配件色都要剔():
    by = {"主": {"filled": 6}, "配A": {"filled": 1}, "配B": {"filled": 1}}
    assert sorted(accessory_colors_from_rows(by)) == ["配A", "配B"]


@pytest.mark.parametrize("by, why", [
    ({"白": {"filled": 5}, "红": {"filled": 5}}, "各色齐平＝正常笛卡尔积商品"),
    ({"A": {"filled": 1}, "B": {"filled": 1}, "C": {"filled": 1}},
     "各色都只有1行＝无尺码维商品，不是配件色"),
    ({"白": {"filled": 5}, "红": {"filled": 0}},
     "整列空是上游还没填（价格/重量阶段未跑），据此反选会误伤正常颜色"),
    ({"白": {"filled": 5}}, "单颜色无从比较"),
    ({}, "空统计"),
])
def test_不该剔的形态一个都不剔(by, why):
    assert accessory_colors_from_rows(by) == [], why


def test_非字典输入不炸():
    assert accessory_colors_from_rows(None) == []
    assert accessory_colors_from_rows([]) == []


def test_三色里只有一个是单行():
    """主色多行、另一正常色多行、配件色单行 —— 只剔配件色。"""
    by = {"白色": {"filled": 5}, "黑色": {"filled": 5}, "红色": {"filled": 1}}
    assert accessory_colors_from_rows(by) == ["红色"]
