"""尺码名归一（pipeline.norm_size）单测。

2026-08-21 真站踩坑：1688 的 skus 键会把身高建议塞进同一个键
（`110cm建议身高100-110cm`），页面复选框只有 `110`。原先只剥尾部 `cm码`
的实现归一出 `110cm建议身高100-110`，与页面永远匹配不上，导致 fix_sizes
先把已勾选尺码全部取消、再报错（先破坏再失败）。故两侧必须共用同一归一函数。
"""
from app.publish.pipeline import norm_size


def test_带建议身高描述的数字码():
    # 本次真站商品 1069791567592 的实际 skus 键
    assert norm_size("110cm建议身高100-110cm") == "110"
    assert norm_size("80cm建议身高70-80cm") == "80"
    assert norm_size("150cm建议身高140-145cm") == "150"


def test_原有格式不回退():
    assert norm_size("90cm码") == "90"
    assert norm_size("90cm") == "90"
    assert norm_size("140") == "140"


def test_字母码保留():
    assert norm_size("M码") == "M"
    assert norm_size("XL") == "XL"
    assert norm_size("XXL码") == "XXL"


def test_其它描述性尾巴():
    assert norm_size("120（参考身高110-120）") == "120"
    assert norm_size("130cm适合身高120-130") == "130"
    assert norm_size("100cm推荐身高90-100cm") == "100"


def test_页面纯数字文本与源键归一后一致():
    src = ["110cm建议身高100-110cm", "80cm建议身高70-80cm"]
    page = ["80", "90", "110", "120"]
    wanted = {norm_size(k) for k in src}
    checked = [p for p in page if norm_size(p) in wanted]
    assert checked == ["80", "110"]


def test_空与空白():
    assert norm_size("") == ""
    assert norm_size("  ") == ""


# 2026-08-24 真站踩坑（offer 846106032776，成人女装针织开衫）：源 skus 键只有中文
# 「均码」，而成人女装类目的页面尺码选项全是英文（one-size / XXS / Asian One-size…）。
# 归一后「均」对不上 `one-size`，fix_sizes 把页面原本勾着的 XXS 取消完还返回 ok，
# 阶段⑨ 才报「错误：请先选择尺码」。故单一尺码的同义写法必须映射到同一个键。
def test_均码与one_size归一到同一键():
    assert norm_size("均码") == norm_size("one-size")
    assert norm_size("均码") == norm_size("One Size")
    assert norm_size("单码") == norm_size("onesize")
    assert norm_size("F") == norm_size("free size")
    assert norm_size("通用码") == norm_size("均码")


def test_版型变体不并入通用均码():
    # Asian / Petite / Tall One-size 是平台的版型变体，不是通用均码：
    # 并进来会让程序在多个选项间乱勾
    onesize = norm_size("均码")
    for v in ("Asian One-size", "Petite One-size", "Tall XXS"):
        assert norm_size(v) != onesize


def test_字母码不被别名表误伤():
    # 别名表只收确定同义的写法，S/M/L 这类不能被牵连
    for v in ("S", "M", "L", "XL", "XXL", "XXS"):
        assert norm_size(v) == v

# 2026-08-29 真站踩坑（草稿 173539495458370139，类目「女童牛仔两件套」，54 个尺码
# 选项）：月龄/岁码只取第一段数字时，6M / 6-9M / 6-12M / 6Y / 6-7Y 全归一成 "6"，
# 源尺码 6-9m 于是把这 5 个框全勾上（5 个源尺码勾出 13 个框、SKU 表多 8 行），而
# fix_sizes 的收敛判据恰好全部成立，返回 status=ok——错误一路带到人工核对。
# 故月龄与岁分开、区间两端都保留。
def test_月龄区间码不与单值码碰撞():
    keys = [norm_size(x) for x in ("6M", "6-9M", "6-12M", "9M", "9-12M", "12M")]
    assert len(set(keys)) == 6, keys


def test_月龄与岁码不互相碰撞():
    assert norm_size("6-9M") != norm_size("6-9Y")
    assert norm_size("2Y") != norm_size("2M")
    assert norm_size("18-24M") != norm_size("18-24Y")


def test_月龄岁码大小写与中文单位同归一():
    assert norm_size("6-9m") == norm_size("6-9M")
    assert norm_size("2-3y") == norm_size("2-3Y")
    assert norm_size("6个月") == norm_size("6M")
    assert norm_size("3岁") == norm_size("3Y")


def test_T码按岁归一():
    # 美式学步童装 3T 与 3Y 是同一档
    assert norm_size("3T") == norm_size("3Y")


def test_真实商品源尺码只勾中五个框():
    """1055568943470 的源尺码 × 该类目 54 个选项：必须精确命中 5 个。"""
    src = ["18-24m", "12-18m", "6-9m", "9-12m", "2-3y"]
    page = ["56", "80", "Newborn", "0-1M", "0-3M", "1-3M", "3M", "3-6M", "6M",
            "6-9M", "9M", "9-12M", "6-12M", "12M", "12-18M", "18M", "24M", "1Y",
            "1-2Y", "18-24M", "2Y", "2-3Y", "3Y", "3-4Y", "6Y", "6-7Y", "8Y",
            "1M", "90", "110", "130"]
    wanted = {norm_size(k) for k in src}
    hit = [p for p in page if norm_size(p) in wanted]
    assert hit == ["6-9M", "9-12M", "12-18M", "18-24M", "2-3Y"], hit


def test_身高码行为不回退():
    """月龄码分支不能吃掉身高码：页面身高码只有单值 90/100/110/120/130。"""
    assert norm_size("110cm建议身高100-110cm") == norm_size("110")
    assert norm_size("90cm码") == "90"
