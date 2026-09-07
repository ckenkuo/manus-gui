# -*- coding: utf-8 -*-
"""阶段⑨ service 层的套装护栏单测（不连浏览器，直接 mock add_sizechart）。

锁三条口径（都源自 2026-08-29 商品 1058585588864 那轮的收尾讨论）：
  1. 两张表填出同一组数值时要提醒人工——但【只对混合套装】。skuCat=2 是「多件相同
     商品」，两件同款、尺码维度本就一样，同值是正确结果，在那里告警就是误报。
  2. 包装清单三件以上时要明确报出来：平台只有「尺码表」「尺码表2」两栏
     （label 正则 /^尺码表2?$/），第 3 件起没有位置，静默丢弃会让人工复核看不出
     「有 3 件、只进了 2 件」。
  3. note 要点出每张表用了源图哪个部件（partUsed），那是复核数值分没分开的第一眼。
"""
import asyncio

from app.publish import service as S


def _run(monkeypatch, judge, r0, r1):
    """跑一次 _st_sizechart，返回（结果, 事件列表）。r0/r1 是两张表的假返回。"""
    events = []

    async def emit(ev):
        events.append(ev)

    async def fake_add(session, info_path, category=None, name=None, which=0, cat_path=None):
        return dict(r1 if which else r0, which=which)

    async def fake_prewarm(ctx, key):
        return judge

    monkeypatch.setattr(S, "add_sizechart", fake_add)
    monkeypatch.setattr(S, "_await_prewarm", fake_prewarm)
    r = asyncio.run(S._st_sizechart({"info_path": "x"}, None, emit))
    return r, events


def _ok(tpl, data, part=""):
    return {"status": "ok", "tplName": tpl, "category": "女童装-上装",
            "params": ["前衣长", "胸围"], "estimated": [], "data": data,
            "partUsed": part}


_D1 = {"6-9M": {"前衣长": 32, "胸围": 52}}
_D2 = {"6-9M": {"前衣长": 42, "腰围": 54}}


def test_混合套装两张同值时提醒人工(monkeypatch):
    judge = {"skuCat": "3", "packing": [{"name": "便服上衣", "qty": 1},
                                        {"name": "半身裙", "qty": 1}]}
    r, events = _run(monkeypatch, judge, _ok("A", _D1), _ok("A2", _D1))
    assert r["status"] == "ok"
    assert "两张数值相同(待复核)" in r["note"]
    msgs = [e.get("message", "") for e in events if e.get("type") == "manual_check"]
    assert any("完全相同的测量值" in m for m in msgs)


def test_同款多件两张同值不告警(monkeypatch):
    """skuCat=2 两件是同一款，尺码维度本就一样——同值是正确结果，不是可疑。"""
    judge = {"skuCat": "2", "packing": [{"name": "T恤", "qty": 2}]}
    r, events = _run(monkeypatch, judge, _ok("A", _D1), _ok("A2", _D1))
    assert r["status"] == "ok"
    assert "待复核" not in r["note"]
    assert not [e for e in events if e.get("type") == "manual_check"]


def test_混合套装数值已分开时不告警(monkeypatch):
    judge = {"skuCat": "3", "packing": [{"name": "便服上衣", "qty": 1},
                                        {"name": "半身裙", "qty": 1}]}
    r, events = _run(monkeypatch, judge,
                     _ok("A", _D1, "上衣"), _ok("A2", _D2, "连衣裙"))
    assert "待复核" not in r["note"]
    assert not [e for e in events if e.get("type") == "manual_check"]
    # 两张表各自用了哪个源部件要写进 note：复核数值分没分开就看这个
    assert "源部件 上衣" in r["note"] and "源部件 连衣裙" in r["note"]


def test_三件以上明确报平台装不下(monkeypatch):
    """平台只有两栏，第 3 件起填不进去；丢弃是事实，但不能静默。"""
    judge = {"skuCat": "3", "packing": [{"name": "便服上衣", "qty": 1},
                                        {"name": "半身裙", "qty": 1},
                                        {"name": "帽子", "qty": 1}]}
    r, events = _run(monkeypatch, judge,
                     _ok("A", _D1, "上衣"), _ok("A2", _D2, "连衣裙"))
    assert r["status"] == "ok"
    msgs = [e.get("message", "") for e in events if e.get("type") == "manual_check"]
    assert any("3 件" in m and "两栏" in m for m in msgs)
    # 件别要列出来，否则人得自己回去翻包装清单
    assert any("帽子" in m for m in msgs)


def test_两件套不报装不下(monkeypatch):
    judge = {"skuCat": "3", "packing": [{"name": "便服上衣", "qty": 1},
                                        {"name": "半身裙", "qty": 1}]}
    r, events = _run(monkeypatch, judge,
                     _ok("A", _D1, "上衣"), _ok("A2", _D2, "连衣裙"))
    msgs = [e.get("message", "") for e in events if e.get("type") == "manual_check"]
    assert not any("两栏" in m for m in msgs)


def test_单件不填第二张也不报任何护栏(monkeypatch):
    judge = {"skuCat": "1", "packing": [{"name": "连衣裙", "qty": 1}]}
    r, events = _run(monkeypatch, judge, _ok("A", _D1, "连衣裙"), _ok("A2", _D1))
    assert r["status"] == "ok"
    assert "尺码表2" not in r["note"]
    assert not [e for e in events if e.get("type") == "manual_check"]
