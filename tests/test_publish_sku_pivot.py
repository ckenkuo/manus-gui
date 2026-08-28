# -*- coding: utf-8 -*-
"""SKU 规格透视（extract.pivot_skus）与阶段⑧ 空 skus 报错的离线测试。

2026-08-28 offer 1014675972015（手工编织水果花束摆件，非服装）暴露的整条链路：
1688 的 specAttrs 在单规格商品上是【裸颜色名】（无 `>`），pivot_skus 的
「不含 > 就 continue」把 6 条 SKU 全静默丢掉 → product-info.json 落成
skus={} / colors=[] / sizes=[] → 阶段⑦ plan_skc 报「视觉未给出任何颜色行选图」、
阶段⑧ fix_sizes 报「product-info.json 无 skus 数据」→ 未落库。

修复在适配器侧（alibaba1688.norm_spec，见 test_publish_sources.py）。本文件锁的是
另外两件事，它们决定了同类问题下次还要不要花一小时排查：

  1. **pivot_skus 丢弃 spec 时必须出声**。丢弃行为本身保留（两维形状由适配器保证，
     两处各归一一遍必然改漏一边），但静默是那次排查的真正代价——两条阶段提示都
     指不回「源 SKU 形状不对」。

  2. **阶段⑧ 的报错要指认真因**。只说「无 skus 数据」会让人去翻页面，而该翻的是
     raw.json 的 skuMap：那里有 6 条、product-info.json 里 0 条，才是问题所在。

全程离线：只用临时文件和内存数据，不连 CDP、不发 LLM 请求。

【异步用 asyncio.run 而不是 get_event_loop().run_until_complete】后者取的是全局
事件循环，全量跑测试时前面的用例已把它关掉，本文件就报 RuntimeError（单独跑却
全绿——最难查的那种失败）。asyncio.run 每次自建自销，也是本项目测试的主流写法。
"""
import asyncio
import json
import os

import pytest

from app.publish import extract as E
from app.publish import pipeline as P


# ---- pivot_skus 的透视与丢弃 -------------------------------------------------

def test_两维spec正常透视():
    """外层键是尺码、内层键是颜色（阶段⑧ 拿外层键去页面勾复选框）。"""
    pivot, colors, sizes = E.pivot_skus([
        {"spec": "6633-灰色>100码", "price": 29.8},
        {"spec": "6633-灰色>110码", "price": 31.8},
        {"spec": "6633-蓝色>100码", "price": 29.8},
    ])
    assert pivot == {"100码": {"6633-灰色": 29.8, "6633-蓝色": 29.8},
                     "110码": {"6633-灰色": 31.8}}
    assert colors == ["6633-灰色", "6633-蓝色"]
    assert sizes == ["100码", "110码"]


def _pivot_with_logs(sku_map: list) -> tuple:
    """跑一次 pivot_skus 并收走 loguru 的 warning 文本。

    项目日志走 loguru（不经 stdlib logging），pytest 的 caplog 一条都抓不到，
    故按 test_publish_toast_watch.py 的惯例临时挂 sink。
    """
    msgs = []
    sink = E.logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    try:
        out = E.pivot_skus(sku_map)
    finally:
        E.logger.remove(sink)
    return out, "".join(m + "|" for m in msgs)


def test_单维spec被丢弃且打warning():
    """裸颜色名（1014675972015 的真实形态）进来时：丢弃照旧，但必须留下线索。

    warning 里要点出「>」这个判据和被丢的具体 spec——否则日志里只有一句「skus 为空」，
    与「源商品真没规格」长得一样。
    """
    (pivot, colors, sizes), logs = _pivot_with_logs([
        {"spec": "【心想事橙】橙子花筒（life盆）", "stock": 9977},
        {"spec": "【莓有烦恼】草莓花筒（life盆）", "stock": 9980},
    ])
    assert (pivot, colors, sizes) == ({}, [], [])
    assert "2 条" in logs and ">" in logs
    assert "橙子花筒" in logs


def test_混合时只丢无大于号那几条():
    """一批里既有两维又有单维时，两维的必须正常入表，不能被一条坏数据带走。"""
    (pivot, colors, sizes), logs = _pivot_with_logs([
        {"spec": "灰色>M", "price": 20},
        {"spec": "裸色名", "price": 21},
    ])
    assert pivot == {"M": {"灰色": 20.0}}
    assert colors == ["灰色"] and sizes == ["M"]
    assert "1 条" in logs and "裸色名" in logs


def test_全两维时不打warning():
    """没有丢弃就不该有噪音——warning 要保持「出现即有问题」的信噪比。"""
    _, logs = _pivot_with_logs([{"spec": "灰色>M", "price": 20}])
    assert "被丢弃" not in logs


def test_无价格的spec也要入表():
    """1688 单规格商品的 skuMapOriginal 常常只有 stock 没有 price（1014675972015 就是）。
    价格缺失不能让整条 SKU 消失——尺码维是阶段⑧ 的唯一输入，比价格重要。"""
    pivot, colors, sizes = E.pivot_skus([{"spec": "橙子花筒>均码", "stock": 9977}])
    assert pivot == {"均码": {"橙子花筒": None}}
    assert sizes == ["均码"]


# ---- 阶段⑧ 空 skus 的报错内容 ------------------------------------------------

def _write_case(tmp_path, info: dict, raw: dict = None) -> str:
    info_path = os.path.join(str(tmp_path), "product-info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    if raw is not None:
        with open(os.path.join(str(tmp_path), "raw.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
    return info_path


def test_空skus报错要指认raw里有货(tmp_path):
    """product-info.json 空、raw.json 有 6 条 → 结论是「spec 形状不对，重跑阶段①」。

    这是 1014675972015 的现场：报错必须把人引到 raw.json 的 skuMap，而不是页面。
    session 传 None 是安全的——这个分支在任何页面交互之前就返回了。
    """
    info_path = _write_case(
        tmp_path, {"skus": {}},
        {"skuMap": [{"spec": f"色{i}", "stock": 1} for i in range(6)]})
    r = asyncio.run(P.fix_sizes(None, info_path))
    assert r["status"] == "error"
    assert "6 条" in r["reason"]
    assert "阶段①" in r["reason"]


def test_空skus且raw也空时不误报(tmp_path):
    """raw.json 也没有 SKU 时不能说「spec 形状不对」——那属于源页面真没抓到规格，
    误导的报错比笼统的报错更费时间。"""
    info_path = _write_case(tmp_path, {"skus": {}}, {"skuMap": []})
    r = asyncio.run(P.fix_sizes(None, info_path))
    assert r["status"] == "error"
    assert "raw.json 也无 SKU" in r["reason"]


def test_没有raw文件也不能炸(tmp_path):
    """raw.json 缺失（老工作目录/人工造的 info）时读档失败是 best-effort：
    照样返回原本那句报错，不能把辅助信息的异常冒成阶段崩溃。"""
    info_path = _write_case(tmp_path, {"skus": {}})
    r = asyncio.run(P.fix_sizes(None, info_path))
    assert r["status"] == "error"
    assert "无 skus 数据" in r["reason"]


# ---- 阶段① 显式重跑（修复要能作用到已落坏产物的商品）------------------------

def test_显式从阶段一重跑要真的重提(tmp_path, monkeypatch):
    """from_stage=extract 时不能被状态文件回填的 info_path 挡成 skipped。

    这是修复的「最后一公里」：单维 spec 的 bug 修好后，1014675972015 磁盘上那份
    skus={} 的 product-info.json 仍会被 publish_one 的 ctx 构造回填进来，原先
    _st_extract 见 info_path 有值就 skipped，重跑几次都是同一个错，无路可走。
    """
    from app.publish import service as S

    called = {}

    async def fake_extract(url, session=None, enrich=True, on_manual=None):
        called["url"] = url
        return {"status": "ok", "infoPath": str(tmp_path / "new.json"),
                "outdir": str(tmp_path), "title": "新抓的", "attrCount": 3,
                "mainImgs": 11, "descImgs": 9}

    monkeypatch.setattr(S.extract, "extract_product", fake_extract)
    ctx = {"url": "https://detail.1688.com/offer/1014675972015.html",
           "info_path": str(tmp_path / "old.json"), "from_stage": "extract"}

    async def emit(ev):
        pass

    r = asyncio.run(S._st_extract(ctx, None, emit))
    assert r["status"] == "ok"
    assert called["url"].endswith("1014675972015.html")
    assert ctx["info_path"] == str(tmp_path / "new.json")


def test_普通续跑仍沿用旧产物(tmp_path, monkeypatch):
    """没点名 ① 时必须保持原行为：续跑不该白重抓一遍源站（几十张图要重下）。"""
    from app.publish import service as S

    async def boom(*a, **kw):
        raise AssertionError("普通续跑不该调 extract_product")

    monkeypatch.setattr(S.extract, "extract_product", boom)
    ctx = {"url": "https://detail.1688.com/offer/1014675972015.html",
           "info_path": str(tmp_path / "old.json"), "from_stage": "fix_sizes"}

    async def emit(ev):
        pass

    r = asyncio.run(S._st_extract(ctx, None, emit))
    assert r["status"] == "skipped"
    assert ctx["info_path"] == str(tmp_path / "old.json")


def test_rowid模式点名阶段一也无从重提(tmp_path, monkeypatch):
    """rowid 模式（无 url）没有源页面可抓，点名 ① 仍要跳过而不是拿 None 去导航。"""
    from app.publish import service as S

    async def boom(*a, **kw):
        raise AssertionError("无 url 时不该调 extract_product")

    monkeypatch.setattr(S.extract, "extract_product", boom)
    ctx = {"url": None, "info_path": str(tmp_path / "old.json"), "from_stage": "extract"}

    async def emit(ev):
        pass

    r = asyncio.run(S._st_extract(ctx, None, emit))
    assert r["status"] == "skipped"
