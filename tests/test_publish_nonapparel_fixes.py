# -*- coding: utf-8 -*-
"""非服装商品发布的三处修复：包装清单词表、⑬ 漏关编辑器堵死 save、描述链接双向推。

2026-08-28 offer 1014675972015（手工编织水果花束摆件，类目仿真花）端到端实跑暴露。
前一轮已让 ⑧⑨ 在无尺码类目下跳过（见 test_publish_no_size_category.py），这一跑
走到了更后面，于是暴露三处新问题：

  1. **包装清单被判成「便服上衣x1」**——仿真花填成上衣。真站取证（草稿
     173539495451708963 的配件下拉，只读开出来读选项）：该下拉【不按类目过滤】，
     是全局可搜列表，搜「仿真」返回 仿真花/仿真植物/仿真水果…，搜「摆件」返回
     「摆件」。是我们代码里那份 74 词的服装词表 + 提示词「实在没有对应项时给一个
     最接近的服装通用词」把选择面锁死在服装里了。

  2. **⑬ 一张都没换成时 return skipped 前没关描述编辑器**，它是全屏 modal，
     ⑭ save 点下去被它的遮罩吃掉。页面弹的是编辑器自己的「Temu产品描述批量操作/
     保存/关闭」弹窗（文案不是 save 认的「继续编辑」），那条「错误：产品信息中有
     错误，请检查」toast 也是它弹的，而 save 只看 .ant-form-item-explain-error，
     于是报出「无校验错误但草稿更新时间未变」这种查不下去的结论。

  3. **描述图「更换图片」瞄点只会往一个方向推**。日志里 9 张全挂，每张都是
     `链接 top=1322 遮挡带下沿=1313 bodyScrollTop=422…2949`、命中 None：
     视口高 1257，链接 top=1322 已在视口【下方】，probe 的候选点全被
     `y > innerHeight` 跳过（elementFromPoint 只对视口内坐标有效），
     而 need = 1313-1322+8 = -1 <= 0 → 循环 break，以为「无需再推」。
     被上方 fixed 压住要往下推，掉到视口下方要往【上】推，原实现把两者混成一个量。

全程离线：假会话/纯函数，不连 CDP、不发 LLM 请求。
"""
import asyncio
import json

import pytest

from app.publish import pipeline as P
from app.publish import service as S


# ---- 1) 包装清单词表与提示词 --------------------------------------------------

def test_词表含非服装真实词():
    """这些词都是 2026-08-28 在真站配件下拉里搜索确认存在的。

    只收实测存在的词——塞进不存在的词会让页面侧 option-not-found、白跑一轮重试。
    """
    for w in ("仿真花", "仿真植物", "仿真水果", "摆件", "装饰品", "花瓶",
              "说明书", "电池"):
        assert w in P._PACKING_ACCESSORY_WORDS, w


def test_服装词仍在词表里():
    """补非服装不能把原有服装词挤掉——服装是主要品类，不能为了这一单回归。"""
    for w in ("便服上衣", "连衣裙", "半身裙", "夹克", "防寒夹克", "中筒袜"):
        assert w in P._PACKING_ACCESSORY_WORDS, w


def test_canon保持服装归一不变():
    """_canon_accessory 的服装纠偏（表外词 → 表内确切词）行为一个都不能变。"""
    assert P._canon_accessory("上衣") == "便服上衣"
    assert P._canon_accessory("外套") == "夹克"
    assert P._canon_accessory("羽绒服") == "防寒夹克"
    assert P._canon_accessory("裙子") == "半身裙"


def test_canon认得非服装词():
    """新词进表后要能原样通过（在表内就直接返回，不再被包含匹配改写）。"""
    assert P._canon_accessory("仿真花") == "仿真花"
    assert P._canon_accessory("摆件") == "摆件"
    assert P._canon_accessory("装饰品") == "装饰品"


def test_标题猜品类命中仿真花():
    """1014675972015 的真实标题 → 仿真花（兜底路径要能给出同品类词）。"""
    t = "创意向日葵水果花束手工编织盆栽成品桌面摆件春节新年小礼物批发"
    assert P._guess_accessory_by_title(t) == "仿真花"


def test_标题猜品类顺序按具体优先():
    """「仿真花束」不能被「花瓶」抢走——命中顺序要先具体后笼统。"""
    assert P._guess_accessory_by_title("仿真花束摆件") == "仿真花"
    assert P._guess_accessory_by_title("玻璃花瓶装饰") == "花瓶"
    assert P._guess_accessory_by_title("桌面摆件小夜灯") == "摆件"


def test_标题猜不出时返回空():
    """猜不出必须返回空串，让调用方明确失败——不许跨品类硬凑。"""
    assert P._guess_accessory_by_title("纯棉婴儿连体衣") == ""
    assert P._guess_accessory_by_title("") == ""


def test_提示词不再强制服装通用词():
    """提示词里那句「才给一个最接近的服装通用词」是把仿真花逼成上衣的直接原因。

    改成「同品类通用词」并显式点出「不是服装时不要选服装词」。2026-09-06 起这段
    引导抽成按品类分支的 cat_guidance 变量（赋值语句，不再是 prompt 字符串行），
    故改查整个函数源码，仍只断「有没有这些有害/有益措辞」这一件事。
    """
    import inspect
    src = inspect.getsource(P.judge_sku_category)
    assert "服装通用词" not in src
    assert "同品类通用词" in src
    assert "不是服装时不要选服装词" in src


def test_模型未给清单时按标题补而不是补上衣():
    """兜底路径的回归闸：非服装商品不能再被补成「便服上衣」。"""
    info = {"title": "仿真花束桌面摆件", "attributes": {}}
    out = P._normalize_sku_judge({"skuCat": "1", "qty": 1, "packing": []}, info)
    assert out["packing"] == [{"name": "仿真花", "qty": 1}]


def test_模型未给清单且猜不出时取中性附件():
    """猜不出品类时不能再补「便服上衣」，但也【不能留空】。

    平台强校验「清单件数和 == SKU分类数量」，空清单的和是 0、qty=1 也不相等，
    一样被打回（见 test_publish_packing 的「件数和永远等于 qty 是不变量」）。
    故取词表内的中性附件「说明书」：填错的语义代价比拿上衣去套仿真花小得多。
    """
    info = {"title": "某种说不清的东西", "attributes": {}}
    out = P._normalize_sku_judge({"skuCat": "1", "qty": 1, "packing": []}, info)
    assert out["packing"] == [{"name": P.PACKING_FALLBACK_ACCESSORY, "qty": 1}]
    assert P.PACKING_FALLBACK_ACCESSORY in P._PACKING_ACCESSORY_WORDS
    # 不变量不能破
    assert sum(x["qty"] for x in out["packing"]) == out["qty"]


def test_兜底词不再是服装():
    """回归闸：兜底词一旦又变回某个具体服装品类，非服装商品就会重蹈覆辙。"""
    assert P.PACKING_FALLBACK_ACCESSORY not in ("便服上衣", "西装上衣", "连衣裙",
                                                "半身裙", "长裤")


def test_套装类型仍优先于标题猜():
    """源属性「套装类型」给得出确切词时照用（比标题关键词更接近事实）。"""
    info = {"title": "两件套", "attributes": {"套装类型": "裙套装"}}
    out = P._normalize_sku_judge({"skuCat": "1", "qty": 1, "packing": []}, info)
    assert out["packing"] == [{"name": "半身裙", "qty": 1}]


def test_件数和仍被硬对齐():
    """平台强校验「清单件数和 == SKU分类数量」，这条既有行为不能被本次改动碰坏。"""
    out = P._normalize_sku_judge(
        {"skuCat": "3", "qty": 2,
         "packing": [{"name": "便服上衣", "qty": 1}, {"name": "半身裙", "qty": 2}]},
        {"title": "两件套", "attributes": {}})
    assert out["qty"] == 3          # 以清单实际和为准
    assert sum(x["qty"] for x in out["packing"]) == 3


# ---- 2) ⑬ 早退路径必须关掉编辑器 ---------------------------------------------

def test_一张都没换成时也要关编辑器(monkeypatch):
    """`not deleted and not replaced` 那条早退原先直接 return，把全屏 modal 留在页面上。

    一张都没换成时恰恰最需要关——此时编辑器一定是开着的。

    2026-08-30 起该条件多了 `and not rehosted`（keep 图转存也算干了活，见
    _rehost_desc_keeps），故按前缀定位而不是整句字面量——判的是「早退路径关不关
    编辑器」，不该被条件表达式的增补写死。
    """
    import inspect
    src = inspect.getsource(S._st_desc)
    i = src.index("if not deleted and not replaced")
    j = src.index("张全部保留", i)
    # 早退分支内、return 之前必须有关闭动作
    assert "ensure_desc_closed" in src[i:j], "早退路径没有关闭描述编辑器"


def test_save前会清掉挡路弹窗():
    """save 要对「上游漏关弹窗」自愈：点保存之前先关掉非保存确认框的可见弹窗。"""
    js = P._JS_CLOSE_FOREIGN_MODAL
    # 只关别人的弹窗，不能把保存确认框（含「继续编辑」）一起关掉
    assert "继续编辑" in js
    # 点「关闭」而不是「保存」：描述改动该不该落库已由 ⑬ 的 desc_save 决定过
    assert "'关闭'" in js
    # 清弹窗要在【真正点保存】之前。按 eval_json 调用顺序判，不按源码里第一次
    # 出现的位置——注释里会提前提到 _JS_CLICK_SAVE，按裸字符串判会误伤。
    import inspect
    calls = [l for l in inspect.getsource(P.save).split("\n")
             if "eval_json(" in l and ("_JS_CLOSE_FOREIGN_MODAL" in l
                                       or "_JS_CLICK_SAVE" in l)]
    assert len(calls) >= 2, calls
    assert "_JS_CLOSE_FOREIGN_MODAL" in calls[0]
    assert "_JS_CLICK_SAVE" in calls[1]


def test_save失败结论要指认挡路弹窗():
    """「更新时间未变」这种含糊结论要带上真因：页面上还有谁挡着、页面提示是什么。"""
    import inspect
    src = inspect.getsource(P.save)
    assert "blockingModals" in src
    assert "遗留弹窗" in src or "弹窗挡着" in src


# ---- 3) 描述链接双向推 -------------------------------------------------------

def test_瞄点修正要双向():
    """被上方 fixed 压住往下推，掉到视口下方往上推——两个方向都要有。"""
    js = P._JS_DESC_REPLACE_LINK
    assert "needUp" in js and "needDown" in js
    # 往上推要防超出可滚范围（加 scrollTop 没有 <=0 那道天然限制）
    assert "scrollHeight" in js and "clientHeight" in js


def test_瞄点返回视口高度():
    """innerH 要报出来，调用方靠它把两种失效方向分开写日志。"""
    assert "innerH: innerHeight" in P._JS_DESC_REPLACE_LINK


def test_视口下方的推算方向():
    """按真站实测数值验算：链接 top=1322 / bottom≈1342、视口 1257 → 该往上推。

    这是原实现算出 need=-1 就 break 的那组数（1313-1322+8）。纯算术复核，
    确认新公式给出的是「往上推」且推量足以把链接拉回视口。
    """
    link_top, link_bottom, inner_h, blocker_bottom = 1322, 1342, 1257, 1313
    need_down = max(blocker_bottom - link_top + 8, 0)
    need_up = max(link_bottom - inner_h + 12, 0)
    assert need_down == 0           # 原实现据此 break，什么都不做
    assert need_up == 97            # 新公式：往上推 97px
    assert link_bottom - need_up < inner_h   # 推完链接整条落进视口


# ---- 3b) 「更换图片」链接是 hover 才浮出的（三跑排查的真因）-------------------

def test_遮挡带排除弹窗遮罩():
    """.ant-modal-mask 铺满视口，算进遮挡带会让 blockerBottom 恒等于视口高度。

    2026-08-28 真站取证：描述编辑器的遮罩是 fixed、2560×1313（= innerHeight）。
    原实现把它当「压在链接上方的遮挡带」→ needDown = 1313-227+8 = 1094 →
    把本来 top=227 完全正常的链接一路推出视口，然后在「视口外」与「被预览图盖住」
    两个状态间振荡 4 轮耗尽。即那个「被顶栏压住」的判断是【自造的问题】。
    """
    js = P._JS_DESC_REPLACE_LINK
    assert "ant-modal-mask" in js
    # 铺满视口高度的元素是背景板/容器，不是「带」
    assert "innerHeight - 1" in js


def test_求瞄点前先hover():
    """链接外层 div 未 hover 时 z-index:-1（在预览图下面），hover 后变 2。

    真站取证：只发一个 mouseMoved 到链接中心、不点任何东西，onLink 就从 False
    变 True。所以缺的只是一次 hover——原先「滚容器调高度 + 12 点采样」治错了方向。
    _cdp_click_xy 里的 mouseMoved 在瞄点求出来【之后】，救不了求瞄点这一步。
    """
    import inspect
    src = inspect.getsource(P._desc_aim_replace_link)
    assert "_hover_link" in src
    assert "mouseMoved" in src
    # 四轮退让与收浮层后的重读都要走 hover，不能只在第一轮
    assert src.count("_hover_link()") >= 2


# ---- 3c) 菜单判据被平台改名击穿（第三跑的真因）--------------------------------

def test_菜单判据不含会变的轮播图文案():
    """平台把「引用skc轮播图」改名成「引用产品轮播图」，精确全等判据一项改名就整条失配。

    2026-08-28 实测（logs/20260828183839.log）：hover 修好后瞄点命中了 16 次、
    菜单其实【已经展开】，可见项是
      ['本地上传','空间上传','网络上传','引用产品轮播图','引用采集图片','小秘美图']
    却被判成「未展开」32 次，重点两次 + 派发事件兜底全部空转、9 张无一替换成功。
    判据与现实只差「skc」→「产品」两个字，而那份正确清单每次都被原样打进了日志。

    故判据只留平台没动过、且能与素材图菜单相区分的项；已经变过一次文案的那一项
    不再进判据（该假定它还会再变）。
    """
    assert "引用skc轮播图" not in P.DESC_MENU_ITEMS
    assert "引用产品轮播图" not in P.DESC_MENU_ITEMS
    # 「空间上传」区分两个菜单实例（素材图那套叫「空间图片」）
    assert "空间上传" in P.DESC_MENU_ITEMS


def test_菜单判据能认出真站那份菜单():
    """用真站实测的那份可见项清单验算：判据必须全部命中。"""
    real = ["本地上传", "空间上传", "网络上传", "引用产品轮播图",
            "引用采集图片", "小秘美图"]
    assert all(w in real for w in P.DESC_MENU_ITEMS)


def test_菜单判据仍能与素材图菜单区分():
    """素材图/SKC 那套菜单不能被误判成描述菜单——否则会点错实例、改错图。"""
    material = ["本地图片", "空间图片", "网络图片", "引用采集图片", "应用到所有颜色"]
    assert not all(w in material for w in P.DESC_MENU_ITEMS)


# ---- 4) 变种表列位置：一套表头判据同时覆盖服装与非服装 ------------------------
#
# 2026-08-28 用户截图 + 真站取证：无尺码类目的变种表【没有「尺码」列】，表头是
#   [预览图, 颜色, SKU货号, EAN/UPC/ISBN, 申报价格(CNY), 尺寸(cm), 重量(g), 建议售价]
# 而原实现写死 tds[3]=申报价 / tds[4]=尺寸 / tds[5]=重量 / tds[6]=建议售价——那套
# 下标只在【服装表头多一列「尺码」】时才对得上。非服装类整体错位一列：188.88 写进
# EAN 列、建议售价 13.59 落到申报价列、尺寸三个框全空 → save 被平台拒。
# 实跑回读证据：price=13.59、skuLength/skuWidth/skuHeight 全空。
#
# 用户明确要求：不要为非服装单开分支、不要影响服装类。按表头定位天然满足——
# 服装有尺码列时申报价在 tds[3]、非服装没有时在 tds[4]，找「申报价」两种都命中。

_HEADS_APPAREL = ["预览图( 批量)", "颜色", "尺码", "SKU货号 ( 一键生成 )",
                  "EANUPCISBN (批量编辑)", "申报价格 (CNY) (批量)",
                  "尺寸(cm)(批量)", "重量(g)(批量)", "建议售价(批量)"]
_HEADS_NONAPPAREL = ["预览图( 批量)", "颜色", "SKU货号 ( 一键生成 · 高级 )",
                     "EANUPCISBN (批量编辑)", "申报价格 (CNY) (批量)",
                     "尺寸(cm)(批量)", "重量(g)(批量)", "建议售价(批量)"]


def _locate(heads):
    """按被测 JS 的同一套判据算列下标（findCol = 表头 includes 关键词）。"""
    def find(*keys):
        for i, h in enumerate(heads):
            if any(k in h for k in keys):
                return i
        return -1
    return {"price": find("申报价"), "dims": find("尺寸"),
            "weight": find("重量"), "msrp": find("建议售价"),
            "size": next((i for i, h in enumerate(heads)
                          if "尺码" in h and "尺码表" not in h), -1)}


def test_变种表不再写死列下标():
    js = P._JS_FILL_VARIANT
    assert "thead th" in js and "findIndex" in js
    # 这几个写死下标是病根，必须消失
    for dead in ("tds[3].querySelector", "tds[4].querySelectorAll",
                 "tds[5].querySelector", "tds[6].querySelector"):
        assert dead not in js, dead
    # 行宽门槛也不能再按服装列数写死（只看真正的代码行，注释里会提到那个旧写法）
    code = [l for l in js.split("\n") if not l.strip().startswith("//")]
    assert not any("tds.length < 7" in l for l in code)
    assert any("Math.max(iPrice" in l for l in code)


def test_服装表头列定位不变():
    """服装类目：申报价仍在 tds[3]、尺寸 tds[4]、重量 tds[5]、建议售价 tds[6]。

    这正是原实现写死的那套值——按表头算出来必须与它完全一致，才能保证
    「修非服装不影响服装」。
    """
    c = _locate(_HEADS_APPAREL)
    assert (c["price"], c["dims"], c["weight"], c["msrp"]) == (5, 6, 7, 8)


def test_非服装表头列定位左移一列():
    """无尺码列时各列整体左移一位——原写死下标正是在这里错位的。"""
    c = _locate(_HEADS_NONAPPAREL)
    assert (c["price"], c["dims"], c["weight"], c["msrp"]) == (4, 5, 6, 7)
    assert c["size"] == -1          # 没有尺码列


def test_两种表头都能定位到全部必需列():
    """不管有没有尺码列，申报价/尺寸/重量三个必填列都要找得到。"""
    for heads in (_HEADS_APPAREL, _HEADS_NONAPPAREL):
        c = _locate(heads)
        assert c["price"] >= 0 and c["dims"] >= 0 and c["weight"] >= 0, heads


def test_缺列时报错而不是猜下标():
    """缺列必须硬失败：静默按下标猜正是这次的病根。"""
    js = P._JS_FILL_VARIANT
    assert "no-column:" in js
    import inspect
    src = inspect.getsource(P.set_variant)
    assert "变种表列定位失败" in src


def test_建议售价格子排除币种下拉():
    """建议售价那格里还有个币种下拉（USD/CNY…），它也是 input，不能被当成售价框。"""
    assert "ant-select-selection-search-input" in P._JS_FILL_VARIANT


# ---- 5) 平台硬校验：尺寸必须 长 >= 宽 >= 高 -----------------------------------
#
# 2026-08-28 真站取证（用户截图）：模型估出 25x20x30（高 30 > 宽 20）后，尺寸列
# 每一行都挂红字「尺寸长宽高需要满足长≥宽≥高」，save 被拒。
# 这三个数描述同一个盒子，降序重排即满足，不改变申报体积、不影响体积重运费。

def test_乱序尺寸被重排():
    """本次实跑那组真实值：25x20x30 → 30x25x20。"""
    assert P._order_dims(["25", "20", "30"]) == ["30", "25", "20"]


def test_服装固定尺寸不受影响():
    """30x25x3 本来就是降序，排序必须是恒等操作——不能因为这次改动动了服装。"""
    assert P._order_dims(list(P._APPAREL_DIMS)) == list(P._APPAREL_DIMS)


def test_兜底尺寸本来就合规():
    """_PACK_FALLBACK 的 30x24x5 同样应保持原样。"""
    f = P._PACK_FALLBACK
    src = [str(f["长"]), str(f["宽"]), str(f["高"])]
    assert P._order_dims(src) == src
    assert float(f["长"]) >= float(f["宽"]) >= float(f["高"])


def test_已降序的不动():
    assert P._order_dims(["40", "30", "20"]) == ["40", "30", "20"]


def test_整数不带小数点():
    """页面尺寸框是整数 cm，排完不能变成 30.0 这种（会与回读比对失配）。"""
    assert P._order_dims([25.0, 20.0, 30.0]) == ["30", "25", "20"]


def test_非数值原样返回():
    """异常输入不在这里吞掉，交下游 len/量级闸报错。"""
    assert P._order_dims(["a", "b", "c"]) == ["a", "b", "c"]


def test_set_variant_会调用重排():
    """显式 --dims 传进来的乱序值也要被纠正，不能只管模型那条路。"""
    import inspect
    src = inspect.getsource(P.set_variant)
    i = src.index("_order_dims")
    j = src.index("if len(d_list) != 3")
    assert i < j, "重排必须在长度校验之前、且在三条取值路径汇合之后"


def test_平台约束是长宽高降序而不是升序():
    """方向写反会让每一行照样报错——用真站文案的语义钉住方向。"""
    out = P._order_dims(["2", "50", "10"])
    assert out == ["50", "10", "2"]
    assert float(out[0]) >= float(out[1]) >= float(out[2])


# ---- 6) 描述长图有自己的尺寸规则，不套服装 SKC 的 1340x1785 -------------------
#
# 2026-08-28 用户截图取证：描述图模块弹窗自带说明
#   「0.5 <= [图片宽 + 图片高] <= 2，宽度 >= 480，高度 >= 480，
#     上传图片大小限制在 10M 以内，发布到 temu 后台时请压缩至 3M 以内」
# 原先描述图也套服装 SKC 的 1340x1785，于是 1000x1000（比例 1.0、两边 >= 480，
# 本来完全合格）被判「低于 1340x1785」→ 触发放大甚至重新生图，每跑白烧一轮。

from app.publish import images as I


def test_描述图规则与服装分开():
    """两套常量必须各自独立，不能一动就互相牵连。"""
    assert (I.DESC_MIN_W, I.DESC_MIN_H) == (480, 480)
    assert (I.CLOTH_MIN_W, I.CLOTH_MIN_H) == (1340, 1785)
    assert I.DESC_RATIO_MIN == 0.5 and I.DESC_RATIO_MAX == 2.0


def test_一千方图是合格的():
    """这就是本次白烧生图的那张：1000x1000 必须判合格，一张都不该动。"""
    assert I.check_desc_size(1000, 1000)["ok"] is True


def test_比例超界判不合格():
    """0.5~2 之外要拦：极端狭长图平台自己也不收。"""
    assert I.check_desc_size(1200, 500)["ok"] is False    # 2.4
    assert I.check_desc_size(500, 1200)["ok"] is False    # 0.417
    # 边界内侧合格
    assert I.check_desc_size(960, 480)["ok"] is True      # 恰好 2.0
    assert I.check_desc_size(480, 960)["ok"] is True      # 恰好 0.5


def test_短边不足判不合格():
    assert I.check_desc_size(479, 600)["ok"] is False
    assert I.check_desc_size(480, 480)["ok"] is True


def test_读不到宽高时不判不合格():
    """未知不能当不合格——否则又会触发无谓的放大/重烧，正是这次要消除的行为。"""
    assert I.check_desc_size(None, None)["ok"] is None
    assert I.check_desc_size(0, 0)["ok"] is None


def test_体积按temu后台三M口径():
    assert I.DESC_MAX_BYTES == 3 * 1024 * 1024
    assert I.check_desc_size(1000, 1000, 4 * 1024 * 1024)["ok"] is False
    assert I.check_desc_size(1000, 1000, 1 * 1024 * 1024)["ok"] is True


def test_desc_save回读用描述图口径():
    """desc_save 的 JS 判据要收比例上下限，且不再注入服装的 1340x1785。"""
    import inspect
    src = inspect.getsource(P.desc_save)
    assert "DESC_MIN_W" in src and "DESC_RATIO_MIN" in src
    assert "CLOTH_MIN_W" not in src
    assert "__RMIN__" in P._JS_DESC_SAVE and "__RMAX__" in P._JS_DESC_SAVE


def test_描述图放大不再拉到服装红线():
    """needsUpscale 走 compress 时必须传描述图下限，否则 1000x1000 会被插值放大。"""
    import inspect
    src = inspect.getsource(S._prepare_desc_image)
    assert "DESC_MIN_W" in src and "DESC_MIN_H" in src


def test_英化出图走描述图模式():
    """desc_mode=True：不按服装闸门挑出图尺寸、收尾 compress 也不放大到 1340x1785。"""
    import inspect
    assert "desc_mode=True" in inspect.getsource(S._prepare_desc_image)
    esrc = inspect.getsource(I.edit_image)
    assert "gate_aware=not desc_mode" in esrc
    assert "min_w=DESC_MIN_W" in esrc


def test_SKC与素材图仍守服装红线():
    """描述图放宽绝不能连带放宽 SKC/素材图——那两条是平台的服装类校验。"""
    import inspect
    # SKC 行尺寸兜底判据仍用 CLOTH_MIN
    assert "CLOTH_MIN_W" in inspect.getsource(P._skc_row_state)
    assert I.MATERIAL_TARGET >= I.CLOTH_MIN_H
