# -*- coding: utf-8 -*-
"""非服装类目（无尺码维）下阶段⑧⑨⑩a 与续跑判定的离线单测。

2026-08-28 offer 1014675972015（手工编织水果花束摆件）真站取证。补齐单维 spec 的
「均码」之后该商品并没有发出去，只是把报错换了个说法——真因是**这个类目压根没有
尺码维**。草稿 173539495451708963（类目「家居、厨房用品 > 家居装饰 > 仿真植物、
仿真花、花艺 > 仿真花」）等到 20s 稳定时的页面事实：

  skuAttrsInfo   高 412px，6 个 d-checkbox 全是【颜色】，ant-form-item 一个都没有
  整页 32 个 label 里没有任何带「尺」的项，也没有「尺码表」栏
  变种信息表表头 = ["预览图( 批量)", "颜色", "SKU货号…", "EAN…", "申报价格…", …]
                   ——tds[0] 是预览图、tds[1] 才是颜色，没有尺码列

由此四处要改，本文件逐一钉住：

  1. **⑧ fix_sizes**：页面无尺码组时返回 skipped，不是 error。但要与「区块没渲染」
     分开——后者是类目失效的征兆，必须照旧报错。
  2. **⑨ sizechart**：第一张尺码表栏就不存在时 skipped（原先只对第二张宽容）。
  3. **live_state 的 rendered**：不能只看尺码表栏，否则无尺码类目恒 false，
     续跑判定永久返回「全部重跑」且每次白等 12s。
  4. **⑩a 货号**：颜色/尺码列按【表头】定位。写死 tds[0]/tds[1] 会在无尺码类目下
     读成 color 空、size 是颜色名，整体错位一列。

全程离线：假会话按 JS 片段特征分派，不连 CDP、不发 LLM 请求。
"""

from publish_patching import patch_publish
import asyncio
import json
import os

import pytest

from app.publish import pipeline as P
from app.publish import service as S


def _info(tmp_path, skus) -> str:
    p = os.path.join(str(tmp_path), "product-info.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"skus": skus}, f, ensure_ascii=False)
    return p


# ---- 1) 阶段⑧：无尺码维跳过，未渲染仍报错 ------------------------------------

class _SizeSession:
    """只覆盖 fix_sizes 开头两次 eval：尺码组状态 + 区块存在性探查。"""

    def __init__(self, states, presence):
        self.states = states
        self.presence = presence

    async def eval_json(self, js):
        if "sizeItemCount" in js:
            return self.presence
        if "d-checkbox" in js:
            return self.states
        return {}


def test_无尺码维时阶段八跳过而不报错(tmp_path):
    """仿真花类目的现场：区块在、6 个复选框全是颜色、没有尺码行 → skipped。

    这个商品的颜色复选框本来就已勾好、变种表也已生成，⑧ 对它本就无事可做。
    判成失败会让整单永远卡在⑧（先报「无 skus 数据」，补了均码后改报「源尺码在页面
    选项里全部不存在」，换个说法而已）。
    """
    s = _SizeSession([], {"section": True, "checkboxes": 6, "sizeItemCount": 0,
                          "labels": [], "hasSizeBtn": True})
    r = asyncio.run(P.fix_sizes(s, _info(tmp_path, {"均码": {"橙子花筒": None}})))
    assert r["status"] == "skipped"
    assert "没有尺码属性行" in r["reason"]


def test_区块未渲染时阶段八仍报错(tmp_path):
    """区块压根不存在 = 类目失效/还在加载，这是整单卡死的上游根因，绝不能放过。"""
    s = _SizeSession([], {"section": False})
    r = asyncio.run(P.fix_sizes(s, _info(tmp_path, {"均码": {"橙子花筒": None}})))
    assert r["status"] == "error"
    assert "未渲染" in r["reason"]


def test_区块在但复选框为零时也报错(tmp_path):
    """区块在、却连颜色复选框都没有：说明还没渲染完，不能当「本类目无尺码」放过。

    这道判据是刻意的——只看 sizeItemCount 为 0 会把渲染中途也判成 skipped，
    那样⑧⑨⑩⑪ 会一路跳过，最后带着空变种表走到 save。
    """
    s = _SizeSession([], {"section": True, "checkboxes": 0, "sizeItemCount": 0})
    r = asyncio.run(P.fix_sizes(s, _info(tmp_path, {"均码": {"橙子花筒": None}})))
    assert r["status"] == "error"


def test_有尺码组时照旧走原逻辑(tmp_path):
    """服装类目不受影响：源尺码与页面全对不上仍要报「全部不存在」且不动任何勾选。

    这是 2026-08-24 offer 846106032776 那道闸，不能被本次改动削弱。
    """
    s = _SizeSession([{"t": "XXS", "c": True}, {"t": "XS", "c": False}], {})
    r = asyncio.run(P.fix_sizes(s, _info(tmp_path, {"110cm": {"灰色": 1}})))
    assert r["status"] == "error"
    assert "全部不存在" in r["reason"]


def test_阶段八包装把skipped透传(monkeypatch):
    """service 侧不能把 skipped 二次判成 fail（原实现「不等于 ok」就 fail）。"""
    events = []

    async def emit(ev):
        events.append(ev)

    async def fake_fix_sizes(session, info_path):
        return {"status": "skipped", "reason": "本类目没有尺码属性行（非服装类目）"}

    patch_publish(monkeypatch, "service", "fix_sizes", fake_fix_sizes)
    r = asyncio.run(S._st_fix_sizes({"info_path": "x"}, None, emit))
    assert r["status"] == "skipped"
    # 要在进度里留一条说明，否则用户只看到⑧ 没做事、不知道为什么
    assert any("尺码" in (e.get("message") or "") for e in events)


# ---- 2) 阶段⑨：第一张尺码表栏就不存在 ----------------------------------------

def test_无尺码表栏时阶段九跳过(monkeypatch):
    """整页没有「尺码表」label → add_sizechart 报 no-sizechart-item → skipped。

    原先只有第二张表缺栏才宽容，第一张缺栏算 fail，于是⑨ 紧接⑧ 再挂一次。
    """
    events = []

    async def emit(ev):
        events.append(ev)

    async def fake_add(session, info_path, category=None, name=None, which=0, cat_path=None):
        return {"status": "error", "reason": "no-sizechart-item", "which": which,
                "charts": 0}

    async def fake_prewarm(ctx, key):
        return {"skuCat": "1", "packing": []}

    patch_publish(monkeypatch, "service", "add_sizechart", fake_add)
    patch_publish(monkeypatch, "service", "_await_prewarm", fake_prewarm)
    r = asyncio.run(S._st_sizechart({"info_path": "x"}, None, emit))
    assert r["status"] == "skipped"
    assert "无尺码表栏" in r["note"]


def test_尺码表其它失败仍算失败(monkeypatch):
    """缺栏之外的原因（弹窗没打开、分类选不上）不能被顺带放过。"""
    async def emit(ev):
        pass

    async def fake_add(session, info_path, category=None, name=None, which=0, cat_path=None):
        return {"status": "error", "reason": "添加尺码表弹窗未打开"}

    async def fake_prewarm(ctx, key):
        return {"skuCat": "1", "packing": []}

    patch_publish(monkeypatch, "service", "add_sizechart", fake_add)
    patch_publish(monkeypatch, "service", "_await_prewarm", fake_prewarm)
    r = asyncio.run(S._st_sizechart({"info_path": "x"}, None, emit))
    assert r["status"] == "fail"


# ---- 3) 续跑判定：rendered 与不安排⑨ -----------------------------------------

def test_无尺码类目的rendered判据():
    """rendered 要认「变种属性区有复选框」这条路，否则无尺码类目恒 false。

    恒 false 的后果：_stale_form_stages 每次返回全量（含③类目，走一遍类目树 110s），
    且 _JS_LIVE_STATE 里那个等待循环每次空转满 12s。
    """
    js = P._JS_LIVE_STATE
    assert "attrCbCount > 0" in js
    assert "hasSizeGroup" in js
    # 等待循环也要认两条路，不能只等尺码表栏
    assert "_attrReady()" in js


def test_无尺码维时续跑不安排尺码表():
    """hasSizeGroup 假且 sizechartCount 为 0 → ⑨ 不进重跑集。

    否则每次续跑都安排⑨、每次又 skipped，白跑一轮还把日志搅浑。
    """
    live = {"rendered": True, "titleFilled": True, "attrImgCount": 6,
            "skuRowCount": 6, "skuCodeCount": 6, "skuCodeBad": 0,
            "skuFilledRows": 6, "shippingSet": True, "descImgCount": 0,
            "hasSizeGroup": False, "sizechartCount": 0, "sizechartAdded": False}
    assert "sizechart" not in S._stale_form_stages(live)


def test_有尺码维且尺码表未加时仍安排():
    """服装类目不受影响：尺码表没加就要重跑⑨。"""
    live = {"rendered": True, "titleFilled": True, "attrImgCount": 6,
            "skuRowCount": 6, "skuCodeCount": 6, "skuCodeBad": 0,
            "skuFilledRows": 6, "shippingSet": True, "descImgCount": 0,
            "hasSizeGroup": True, "sizechartCount": 1, "sizechartAdded": False}
    assert "sizechart" in S._stale_form_stages(live)


# ---- 4) 阶段⑩a：颜色/尺码列按表头定位 ---------------------------------------

class _CodeSession:
    """按真实表头顺序造变种表，验证列定位而不是下标硬编码。"""

    def __init__(self, heads, cells):
        self.heads = heads
        self.cells = cells      # [[各列文本], ...]
        self.plan = None

    async def eval_json(self, js):
        if "const PLAN = " in js:
            head = "const PLAN = "
            i = js.index(head) + len(head)
            j = js.index(";", i)
            self.plan = json.loads(js[i:j])
            return {"filled": len(self.plan), "mismatch": [], "bad": [],
                    "sample": []}
        # 读列：按被测 JS 的同一套表头判据算出下标，模拟浏览器行为
        color_idx = next((k for k, h in enumerate(self.heads)
                          if h.startswith("颜色")), -1)
        size_idx = next((k for k, h in enumerate(self.heads)
                         if "尺码" in h and "尺码表" not in h), -1)
        rows = [{"i": i,
                 "color": c[color_idx] if color_idx >= 0 else "",
                 "size": c[size_idx] if size_idx >= 0 else "",
                 "cur": ""}
                for i, c in enumerate(self.cells)]
        return {"rows": rows, "colorIdx": color_idx, "sizeIdx": size_idx,
                "heads": self.heads}


def test_读货号列的JS按表头定位而不是写死下标():
    """真站表头 tds[0] 是【预览图】，写死 tds[0] 会把颜色读成空。"""
    js = P._JS_READ_SKU_CODES
    assert "findIndex" in js and "thead th" in js
    assert "txt(tds[0]), size: txt(tds[1])" not in js
    # 填写那段的逐行核对必须用同一套下标，否则会全行 row-moved
    assert "colorIdx" in P._JS_FILL_SKU_CODES


def test_无尺码列时货号退化成纯颜色():
    """仿真花的真实表头：没有尺码列，货号只用颜色，且不能报「读不到列」。"""
    heads = ["预览图( 批量)", "颜色", "SKU货号 ( 一键生成 · 高级 )",
             "EANUPCISBN (批量编辑)", "申报价格 (CNY) (批量)"]
    s = _CodeSession(heads, [["", "Orange", "", "", ""],
                             ["", "Strawberry", "", "", ""]])
    r = asyncio.run(P.fix_sku_codes(s))
    assert r["status"] == "ok"
    assert [p["code"] for p in s.plan] == ["Orange", "Strawberry"]
    # size 一律空串，不能把颜色名塞进尺码位
    assert all(p["size"] == "" for p in s.plan)


def test_有尺码列时仍按两维拼():
    """服装类目表头带尺码列，货号照旧「颜色-尺码」。"""
    heads = ["预览图( 批量)", "颜色", "尺码", "SKU货号 ( 一键生成 )"]
    s = _CodeSession(heads, [["", "Gray", "100", ""], ["", "Gray", "110", ""]])
    r = asyncio.run(P.fix_sku_codes(s))
    assert r["status"] == "ok"
    assert [p["code"] for p in s.plan] == ["Gray-100", "Gray-110"]


# ---- 5) ⑦ SKC：本类目没有颜色图位时跳过 ---------------------------------------
#
# 2026-08-29 真站取证（草稿 173539495451708963，类目仿真花）+ 用户截图：
# 该类目变种属性区【没有任何图片位】——行内 http 图 0、「选择图片」按钮 0、
# .single-image 图格 0、连 <tr> 都没有（颜色是复选框列表，界面上只有勾选框和铅笔）。
# 于是 _skc_row_state 按「tr + textContent 含颜色名」找行必然报
# 「找不到颜色行: 【心想事橙】橙子花筒（life盆）」，⑦ 六行全失败（0/6 行完成）。
# 那不是定位写错，是平台按类目决定不支持按颜色配图——与⑧⑨ 同一性质。

class _SkcSupportSession:
    def __init__(self, payload):
        self.payload = payload

    async def eval_json(self, js, **kw):
        if "pickBtns" in js:
            return self.payload
        return {}


def test_无图片位时判不支持():
    """仿真花的真实现场：6 个颜色复选框、三个图片位信号全 0。"""
    s = _SkcSupportSession({"section": True, "rows": 0, "imgs": 0, "pickBtns": 0,
                            "imageCells": 0, "checkboxes": 6})
    r = asyncio.run(P.skc_image_support(s))
    assert r["supported"] is False


def test_有图片位时判支持():
    """服装类目：行内有 .single-image 图格与「选择图片」按钮，必须照旧走原路。"""
    s = _SkcSupportSession({"section": True, "rows": 4, "imgs": 12, "pickBtns": 4,
                            "imageCells": 12, "checkboxes": 8})
    r = asyncio.run(P.skc_image_support(s))
    assert r["supported"] is True


def test_区块未渲染时不判不支持():
    """连复选框都没有 = 还没渲染完，证据不足。

    判成「不支持」会让服装商品静默跳过 SKC 换图、带着 1688 原图发出去——
    这个方向的误判代价远大于多跑一轮。
    """
    for bad in ({"section": False},
                {"section": True, "checkboxes": 0, "imgs": 0, "pickBtns": 0,
                 "imageCells": 0}):
        r = asyncio.run(P.skc_image_support(_SkcSupportSession(bad)))
        assert r["supported"] is None, bad


def test_只要有一个图片位信号就算支持():
    """三个信号任一非 0 即支持：换过图的行有 imgs、没换过的至少有按钮或图格。"""
    for one in ("imgs", "pickBtns", "imageCells"):
        p = {"section": True, "rows": 4, "imgs": 0, "pickBtns": 0,
             "imageCells": 0, "checkboxes": 8}
        p[one] = 3
        r = asyncio.run(P.skc_image_support(_SkcSupportSession(p)))
        assert r["supported"] is True, one


def test_阶段七不支持时跳过且不发视觉请求(monkeypatch):
    """跳过要发生在 plan_skc 之前——那是一次视觉调用，白烧没有意义。"""
    events = []

    async def emit(ev):
        events.append(ev)

    async def boom(*a, **kw):
        raise AssertionError("不支持颜色图时不该调 plan_skc（白烧一次视觉请求）")

    async def fake_support(session):
        return {"supported": False, "checkboxes": 6}

    patch_publish(monkeypatch, "service", "skc_image_support", fake_support)
    monkeypatch.setattr(S.vision, "plan_skc", boom)
    patch_publish(monkeypatch, "service", "_load_info", lambda p: {"colors": ["a", "b"]})
    r = asyncio.run(S._st_skc({"info_path": "x", "workdir": "y"}, None, emit))
    assert r["status"] == "skipped"
    assert "颜色图位" in r["note"]
    assert any("不支持按颜色配图" in (e.get("message") or "") for e in events)
