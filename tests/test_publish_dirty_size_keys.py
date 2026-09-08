"""源尺码键被商家拼进规格尾巴时的归一与档位判定（2026-09-01 实测三单）。

取证：一批 17 单里 3 单卡在阶段⑧，报「源尺码在页面选项里全部不存在」——
1065904018146（`M 胸围33背长29Г`）、668802700848（`L:60克`）、
1051830242405（`L【`）。页面选项就是干净的 S/M/L/XL，字母码全都在，
纯粹是 norm_size 只剥了 cm/码 后缀、剥不掉冒号/方括号/空格后的测量描述。
"""
import pytest

from app.publish.pipeline import norm_size, size_tier

# 三单实际的页面选项（成人字母码 + 数字码混排）
PAGE = ["3", "4", "5", "6", "7", "8", "L", "M", "S", "XL", "XS", "XXL"]


@pytest.mark.parametrize("raw, expect", [
    ("M 胸围33背长29Г", "M"),   # 空格后跟测量值，Г 是商家把 cm 打成西里尔字母
    ("S 胸围29背长27Г", "S"),
    ("L:60克", "L"),            # 冒号后跟克重
    ("M:50克", "M"),
    ("XXL:88克", "XXL"),
    ("L【", "L"),               # 商家描述被截断只留下半个方括号
    ("XS【", "XS"),
    # 2026-09-08 追加：宠物服装把背长/胸围/体重【无分隔粘连】在字母码后（offer 1063010884595）
    ("L背30", "L"),             # 字母码后紧跟「背」+背长，无空格冒号
    ("M背27", "M"),
    ("XXL背38", "XXL"),
    ("M胸40背30约5-6斤左右", "M"),   # 字母码后粘连整段测量描述
])
def test_脏尾缀被剥掉(raw, expect):
    assert norm_size(raw) == expect


@pytest.mark.parametrize("src", [
    ["M 胸围33背长29Г", "S 胸围29背长27Г"],
    ["L:60克", "M:50克", "S:40克", "XL:70克", "XXL:88克"],
    ["L【", "M【", "S【", "XL【", "XS【"],
])
def test_三单源尺码在页面上全部命中且不多勾(src):
    wanted = {norm_size(k) for k in src}
    page_norms = {norm_size(p) for p in PAGE}
    # 每个源尺码都要在页面上找到
    assert all(w in page_norms for w in wanted)
    # 勾选的页面项数量必须与源尺码数一致：多勾就是 6M/6-9M 那类碰撞复发
    checked = [p for p in PAGE if norm_size(p) in wanted]
    assert len(checked) == len(wanted)


@pytest.mark.parametrize("raw", [
    "Asian One-size", "Petite One-size", "Asian Tall XL", "Asian L",
])
def test_版型变体不被空格切分吃掉(raw):
    """不能无条件按空格取首段：那会把这些平台版型变体全归一成 Asian/Petite，
    多个不同选项撞成同一个值，复现「一个源尺码勾中多个框却仍判收敛」的假 ok。"""
    assert norm_size(raw) == raw


def test_版型变体两两不相等():
    variants = ["Asian One-size", "Petite One-size", "Asian Tall XL", "Asian L"]
    assert len({norm_size(v) for v in variants}) == len(variants)


@pytest.mark.parametrize("sizes, expect", [
    (["L:60克", "M:50克"], "adult"),          # 脏尾缀下档位判定也要能出结论
    (["M 胸围33背长29Г"], "adult"),
    (["L【", "XS【"], "adult"),
    (["均码"], ""),                            # 均码童装成人都用，刻意判不出
    (["one-size"], "adult"),                  # 英文 onesize 仍算成人码
])
def test_档位判定不被脏尾缀致盲也不误判均码(sizes, expect):
    assert size_tier(sizes) == expect
