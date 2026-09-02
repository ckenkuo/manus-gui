# -*- coding: utf-8 -*-
"""类目判断的年龄段/尺码线索单测（不碰 CDP、不碰 LLM）。

钉住 2026-08-29 那次误发的两端：
  - size_tier 能把月龄码/岁码/身高码与成人字母码判成互斥的两档；
  - 线索确实进了两条路径（遍历 + 缓存快路径）的提示词，且无线索时提示词退化成原样；
  - fix_sizes 在两侧档位相反时把报错指向「类目选错」而不是「尺码别名没覆盖」。
"""
import json

import pytest

from app.publish import pipeline, service


# ---- size_tier -------------------------------------------------------------

@pytest.mark.parametrize("sizes, expect", [
    (["18-24m", "12-18m", "6-9m", "9-12m", "2-3y"], "baby"),   # 真实误发商品
    (["3T", "4T"], "baby"),
    (["90", "100cm", "110cm建议身高100-110cm"], "baby"),
    (["Asian L", "Asian M", "Asian One-size", "Asian Tall XL"], "adult"),
    (["S", "M", "L", "XL"], "adult"),
    (["均码"], ""),          # 判不出：不能据此否定 LLM
    ([], ""),
    (["2-3y", "M"], ""),     # 两档混杂：不猜
])
def test_档位判定(sizes, expect):
    assert pipeline.size_tier(sizes) == expect


def test_成人码与童装码互斥():
    """这条互斥关系是本次修复的判据本身，单独钉一遍。"""
    src = pipeline.size_tier(["6-9m", "2-3y"])
    page = pipeline.size_tier(["Asian S", "Asian One-size"])
    assert src == "baby" and page == "adult" and src != page


# ---- cat_clues -------------------------------------------------------------

_INFO = {
    "title": "棕色女亚马逊牙雅跨境现货坑条短袖上衣夏季宝宝花朵印花牛仔裤棉",
    "attributes": {"适合年龄段": "婴幼童(1~3岁，80~100cm)", "主面料成分": "棉"},
    "skus": {"18-24m": {}, "12-18m": {}, "6-9m": {}, "9-12m": {}, "2-3y": {}},
    "sizes": ["18-24m", "12-18m", "6-9m", "9-12m", "2-3y"],
}


def test_线索含年龄段与档位():
    c = pipeline.cat_clues(_INFO)
    assert "婴幼童" in c and "6-9m" in c and "不可能是成人商品" in c


def test_没有尺码也没有年龄属性时线索为空():
    """线索为空必须是空串：提示词据此整段省掉，不留空占位诱导模型编。"""
    assert pipeline.cat_clues({"title": "x", "attributes": {}}) == ""
    assert pipeline.cat_clues(None) == ""
    assert pipeline.cat_clues({}) == ""


def test_没有sizes键时回落到skus键():
    info = {"attributes": {}, "skus": {"6-9m": {}, "2-3y": {}}}
    assert "6-9m" in pipeline.cat_clues(info)


def test_适合身高的脏值被截断():
    """1688 常把下游平台/销售地区黏进「适合身高」，不截断会淹没线索。"""
    info = {"attributes": {"适合身高": "6-9m,9-12m" + "主要下游平台ebay,亚马逊" * 20}}
    c = pipeline.cat_clues(info)
    assert len(c) < 120 and "6-9m" in c


# ---- 线索进提示词 ----------------------------------------------------------

@pytest.mark.asyncio
async def test_遍历路径提示词带线索(monkeypatch):
    seen = {}

    async def _ask(prompt, what="", **kw):
        seen["p"] = prompt
        return {"index": 0, "reason": "r"}
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    await pipeline._pick_category("标题", ["女士时尚", "婴儿服饰及鞋靴"], [],
                                  clues="适合年龄段: 婴幼童; 源商品尺码: 6-9m")
    assert "商品线索" in seen["p"] and "婴幼童" in seen["p"]


@pytest.mark.asyncio
async def test_无线索时提示词不留空占位(monkeypatch):
    seen = {}

    async def _ask(prompt, what="", **kw):
        seen["p"] = prompt
        return {"index": 0, "reason": "r"}
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    await pipeline._pick_category("标题", ["A", "B"], [])
    # 规则 2 本身会提到「商品线索」（措辞是条件句），故断言的是【没有线索行】：
    # 空占位会诱导模型自己编年龄段。
    assert "商品线索：" not in seen["p"]


@pytest.mark.asyncio
async def test_缓存路径提示词带线索(monkeypatch):
    seen = {}

    async def _ask(prompt, what="", **kw):
        seen["p"] = prompt
        return {"index": 1, "reason": "不匹配"}
    monkeypatch.setattr("app.publish.llm.ask_json", _ask)
    known = [{"path": ["A", "女士牛仔两件套"], "leaf": "女士牛仔两件套", "titles": []}]
    await pipeline._pick_cached_category("标题", known, "源商品尺码: 6-9m")
    assert "商品线索" in seen["p"] and "6-9m" in seen["p"]


def test_两条提示词都写明尺码优先于标题措辞():
    """规则本身就是修复的载体，被改回去必须有测试拦住。"""
    for tpl in (pipeline._CAT_PROMPT, pipeline._CAT_FROM_CACHE_PROMPT):
        assert "{clues}" in tpl
        assert "月龄码" in tpl and "优先于标题措辞" in tpl


# ---- service 把 info 传下去 -------------------------------------------------

@pytest.mark.asyncio
async def test_st_auto_cat把info传给auto_cat(monkeypatch, tmp_path):
    ip = tmp_path / "product-info.json"
    ip.write_text(json.dumps(_INFO, ensure_ascii=False), encoding="utf-8")
    got = {}

    async def _fake(session, rowid, title, **kw):
        got.update(kw, title=title)
        return {"status": "ok", "path": "A > B", "pathList": ["A", "B"],
                "source": "walk"}
    monkeypatch.setattr(service, "auto_cat", _fake)
    r = await service._st_auto_cat({"rowid": "r1", "info_path": str(ip)}, None, None)
    assert r["status"] == "ok"
    assert got["info"]["attributes"]["适合年龄段"].startswith("婴幼童")
    assert got["title"] == _INFO["title"]


@pytest.mark.asyncio
async def test_读info失败不拖垮类目阶段(monkeypatch):
    """best-effort：读不到 info 就只用标题判类目，与加线索前行为一致。"""
    got = {}

    async def _fake(session, rowid, title, **kw):
        got.update(kw, title=title)
        return {"status": "ok", "path": "A", "pathList": ["A"], "source": "walk"}
    monkeypatch.setattr(service, "auto_cat", _fake)
    r = await service._st_auto_cat(
        {"rowid": "r1", "info_path": "不存在的路径.json", "title": "标题"}, None, None)
    assert r["status"] == "ok" and got["info"] is None


@pytest.mark.asyncio
async def test_没有info也没有title才失败(monkeypatch):
    r = await service._st_auto_cat({"rowid": "r1"}, None, None)
    assert r["status"] == "fail" and "标题" in r["note"]


# ---- fix_sizes 报错指认真因 -------------------------------------------------

class _SizeSession:
    """只实现 fix_sizes 前置闸走到的两次 eval_json（尺码组状态 + 当前类目回读）。"""

    def __init__(self, page_sizes):
        self.page_sizes = page_sizes

    async def eval_json(self, js, *a, **kw):
        if "skuAttrsInfo" in js and "d-checkbox" in js:
            return [{"t": s, "c": False} for s in self.page_sizes]
        if "productBasicInfo" in js:
            return {"snippet": "产品分类 女士时尚 > 女装 > 女士牛仔 > 女士牛仔两件套"}
        return {}


def _write_info(tmp_path, skus):
    ip = tmp_path / "product-info.json"
    ip.write_text(json.dumps({"skus": {k: {} for k in skus}}, ensure_ascii=False),
                  encoding="utf-8")
    return str(ip)


@pytest.mark.asyncio
async def test_档位相反时报错指向类目而非尺码别名(tmp_path):
    """2026-08-29 那单的复现：源月龄码 vs 页面成人码。"""
    info_path = _write_info(tmp_path, ["18-24m", "12-18m", "6-9m", "9-12m", "2-3y"])
    r = await pipeline.fix_sizes(
        _SizeSession(["Asian L", "Asian M", "Asian One-size", "Asian Tall XL"]),
        info_path)
    assert r["status"] == "error" and r["catMismatch"] is True
    assert r["srcTier"] == "baby" and r["pageTier"] == "adult"
    # 真因必须落在前 200 字符内（service 的 note 会截断）
    assert "类目选错" in r["reason"][:200]
    assert "女士牛仔两件套" in r["reason"]


@pytest.mark.asyncio
async def test_同档位对不上仍报原文案(tmp_path):
    """都是成人码却对不上 → 确实是写法没覆盖，不能误导人去改类目。"""
    info_path = _write_info(tmp_path, ["加大码"])
    r = await pipeline.fix_sizes(_SizeSession(["S", "M", "L"]), info_path)
    assert r["status"] == "error" and r["catMismatch"] is False
    assert "类目选错" not in r["reason"]


@pytest.mark.asyncio
async def test_前置闸不改动任何勾选(tmp_path):
    """这道闸的本职（2026-08-24 加的「先破坏再失败」防线）不能被本次改动破坏。"""
    clicked = []

    class _S(_SizeSession):
        async def eval_json(self, js, *a, **kw):
            if "cb.click()" in js:
                clicked.append(js)
                return {"clicked": True}
            return await super().eval_json(js, *a, **kw)

    info_path = _write_info(tmp_path, ["6-9m", "2-3y"])
    r = await pipeline.fix_sizes(_S(["Asian S", "Asian L"]), info_path)
    assert r["status"] == "error" and not clicked
