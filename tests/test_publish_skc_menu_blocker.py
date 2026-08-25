# -*- coding: utf-8 -*-
"""⑦ SKC 颜色图：行按钮被残留浮层遮挡的单测（不连 CDP、不开浏览器）。

2026-08-24 实测 890843533224「黑色」行：整行换图在第 1 张就断在 open-space，报
「瞄点未命中行按钮（落在 '引用采集图片' class='ant-dropdown-menu-title-content'）」，
而同一条错误里的 blockers 是【空数组】——因为它只查 .ant-modal-wrap / .ant-modal-mask，
根本没查 .ant-dropdown。于是错误信息看着像滚动时序问题，真实原因是上一张图留下的
图片菜单没收起、浮在行按钮上方（菜单 position:fixed，z-index 高于表格）。

两条修正各自锁一个不变量：
  1. blockers 必须同时覆盖弹窗与残留菜单，否则「谁挡的」这条线索永远是空的；
  2. 瞄点未命中时先【收】浮层再重试一次，不该让一行图只换了一张就中断
     （那会把行留在新旧混杂的中间态）。

2026-08-24 补充（846106032776「紫罗兰」行）：那个残留菜单 Escape 与合成 click
【都收不掉】（parked 恒 0），而原实现只在 parked>0 时重读坐标，于是一次都没重试
就失败。故再加一条不变量：
  3. 收浮层无论成没成都要重读坐标，且瞄点在按钮矩形内多点退让——菜单是
     position:fixed 浮在按钮上方，通常只盖住一部分，换个点就能命中；每个候选点
     仍各自过 elementFromPoint 校验，绝不会点到隔壁行。
"""
import asyncio
import inspect

import pytest

from app.publish import pipeline as P


# ---- JS 侧契约（DOM 交互无法离线跑，按不变量断言）----------------------------

def test_瞄点JS把dropdown也算遮挡物():
    js = P._JS_SKC_BTN_POS
    assert ".ant-dropdown" in js                 # 原先只有 .ant-modal-*，那才是漏报的根
    assert ".ant-modal-wrap" in js               # 弹窗那条不能因此丢掉
    # 菜单项文案要一并报出来，否则只知道「有个 dropdown」，不知道是哪个菜单
    assert ".ant-dropdown-menu-item" in js


def test_收浮层JS只认图片菜单不误伤属性下拉():
    """属性行那些是 .ant-select-dropdown，另有 _park_ghost_dropdowns 负责，两套别混。"""
    js = P._JS_PARK_IMAGE_MENUS
    assert "空间图片" in js                       # 判据：含这一项才是图片菜单
    assert ".ant-select-dropdown" not in js       # 不碰属性下拉
    # 收法：Escape 优先，收不掉再点空白（ant 的 dropdown 没有关闭按钮）
    assert "Escape" in js
    assert "click" in js
    # 不能用 remove()：那会让下次点击复用不到实例
    assert "remove()" not in js


def test_可见性判据只看inline_display():
    """与项目既有约定一致：判可见只看 inline display，不用 getComputedStyle。"""
    assert "display:\\s*none" in P._JS_PARK_IMAGE_MENUS


def test_空白点JS排掉一切交互元素():
    """真实点击空白处收浮层时，点错地方会触发别的表单交互，故候选点必须排交互元素。"""
    js = P._JS_BLANK_POINT
    for sel in ("button", "input", ".ant-modal", ".ant-select", "[role=button]"):
        assert sel in js
    assert "closest(bad)" in js          # 判据是 closest，不是只看命中元素本身
    assert "elementFromPoint" in js      # 取到的点必须回校验落在谁身上


def test_滚动JS的block位置是参数():
    """固定 center 会让按钮恒落在残留 fixed 菜单那条带上，整块瞄点一起被盖住。"""
    assert "__BLOCK__" in P._JS_SKC_BTN_SCROLL
    assert "block: 'center'" not in P._JS_SKC_BTN_SCROLL


# ---- Python 侧：收浮层 + 换滚动位置，都不行才失败 -----------------------------

def _nosleep(monkeypatch):
    """把 pipeline 里的等待去掉（本文件测的是分支走向，不测时序）。

    必须先存下真的 sleep 再包：直接 lambda: asyncio.sleep(0) 会调到「已被替换的
    自己」，无限递归。
    """
    real = asyncio.sleep
    monkeypatch.setattr(P.asyncio, "sleep", lambda *a, **k: real(0))


class _FakeSession:
    """按「问的是哪一步」分队列返回 eval_json 结果；cdp 记下点击坐标。

    分队列而不是一条扁平脚本：换滚动位置的重试让调用次数随分支变化，扁平脚本一改
    实现就要重排，且失败时只报 pop from empty list，看不出是哪一步缺了。
    """

    def __init__(self, **queues):
        self.q = {k: list(v) for k, v in queues.items()}
        self.calls = []
        self.clicks = []

    async def eval_json(self, code, **kw):
        if "Escape" in code:
            kind = "park"
        elif "可安全点击" in code:
            kind = "blank"
        elif "elementFromPoint" in code:
            kind = "pos"
        elif "scrollIntoView" in code:
            kind = "scroll"
        else:
            kind = "menu"
        self.calls.append(kind)
        q = self.q.get(kind) or []
        if not q:
            raise AssertionError(f"脚本没给 {kind} 的第 {self.calls.count(kind)} 次返回值")
        return q.pop(0) if len(q) > 1 else q[0]   # 单元素队列视为「每次都这样」

    async def cdp(self, method, params=None):
        p = params or {}
        if p.get("type") == "mousePressed":
            self.clicks.append((p.get("x"), p.get("y")))
        return {"ok": True}


_MISS_MENU = {"x": 10, "y": 20, "hit": False, "atText": "引用采集图片",
              "atClass": "ant-dropdown-menu-title-content",
              "blockers": ["ant-dropdown|本地图片/空间图片/引用采集图片"]}


def test_收掉浮层后重试命中就继续(monkeypatch):
    """第一次瞄点被菜单挡住、收掉后再读命中 → 继续走 CDP 点击，而不是报错。"""
    ses = _FakeSession(
        scroll=[{"ok": True}],
        pos=[_MISS_MENU, {"x": 10, "y": 20, "hit": True, "aimAt": "center"}],
        park=[{"parked": 1, "before": 1, "after": 0}],
        menu=[{"opened": True}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r == {"opened": True}
    assert ses.clicks == [(10, 20)]        # 真实点击发在校验过的瞄点上
    assert ses.calls.count("pos") == 2     # 收浮层后确实重读了一次坐标
    assert ses.calls.count("blank") == 0   # 合成事件就收掉了，不必再真实点空白


def test_合成事件收不掉时真实点击空白处(monkeypatch):
    """rc-trigger 只认真实 mousedown：合成 Escape/click 后 after>0 就必须补真实点击。

    2026-08-24 起反复出现的「落在 '引用采集图片' 上」正是这一类：合成收法对那个实例
    恒无效（parked 一直是 0），原实现到此就只能靠瞄点退让。
    """
    ses = _FakeSession(
        scroll=[{"ok": True}],
        pos=[_MISS_MENU, {"x": 10, "y": 20, "hit": True, "aimAt": "center"}],
        park=[{"parked": 0, "before": 1, "after": 1},   # 合成收法无效
              {"parked": 0, "before": 1, "after": 0}],  # 真实点击后收掉了
        blank=[{"x": 6, "y": 400, "tag": "DIV", "at": "page-content"}],
        menu=[{"opened": True}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r == {"opened": True}
    assert (6, 400) in ses.clicks          # 真实点了空白点
    assert ses.calls.count("park") == 2    # 真实点击后回读了一次残留数


def test_找不到安全空白点就不点(monkeypatch):
    """宁可不点也不乱点：没有安全空白点时退回换滚动位置那条路。"""
    ses = _FakeSession(
        scroll=[{"ok": True}],
        pos=[_MISS_MENU, _MISS_MENU, {"x": 90, "y": 500, "hit": True, "aimAt": "center"}],
        park=[{"parked": 0, "before": 1, "after": 1}],
        blank=[{"err": "找不到可安全点击的空白点"}],
        menu=[{"opened": True}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r == {"opened": True}
    assert ses.clicks == [(90, 500)]       # 只点了行按钮，没点任何空白坐标


def test_浮层收不掉时换滚动位置重瞄(monkeypatch):
    """本次修复的核心不变量：center 全被盖住时必须换 block 再瞄，而不是直接失败。

    根因（2026-08-25 复盘 663641923103）：残留菜单是 position:fixed，上一张图挂完时
    停在视口中段；每行都 block:'center' 滚动，下一行按钮被滚到的正是同一片区域，
    于是矩形内 9 个候选瞄点【整块】一起被盖住——瞄点退让在这种几何关系下必然无效，
    日志里因此一直是同一条「落在 '引用采集图片' 上」。换 block 把按钮挪出那条带即可。
    """
    ses = _FakeSession(
        scroll=[{"ok": True}],
        # 前两轮（center、收浮层后再 center）全灭，第三轮 nearest 才命中
        pos=[_MISS_MENU, _MISS_MENU, {"x": 33, "y": 700, "hit": True, "aimAt": "center"}],
        park=[{"parked": 0, "before": 1, "after": 1},
              {"parked": 0, "before": 1, "after": 1}],
        blank=[{"x": 6, "y": 400, "tag": "DIV", "at": ""}],
        menu=[{"opened": True}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r == {"opened": True}
    assert ses.calls.count("scroll") == 3    # 换过两次滚动位置
    assert (33, 700) in ses.clicks


def test_换位重瞄按不同block各滚一次(monkeypatch):
    """逐一核对重试真的换了 block：同一个 center 滚三次等于白试。"""
    blocks = []

    class _S(_FakeSession):
        async def eval_json(self, code, **kw):
            if "scrollIntoView" in code:
                blocks.append(code.split("block: ")[1].split("}")[0])
            return await super().eval_json(code, **kw)

    ses = _S(
        scroll=[{"ok": True}],
        pos=[_MISS_MENU],                       # 恒不命中，把所有轮次跑完
        park=[{"parked": 0, "before": 1, "after": 1}],
        blank=[{"err": "找不到可安全点击的空白点"}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r["stage"] == "aim"
    assert blocks == ['"center"', '"center"', '"nearest"', '"start"', '"end"']
    assert ses.clicks == []                  # 没瞄准就绝不点（会挂到错误的颜色行）


def test_全部退让都失败才报错且带遮挡物(monkeypatch):
    """收不掉、换位也全被盖住 → 才判失败，错误必须点名遮挡物。"""
    ses = _FakeSession(
        scroll=[{"ok": True}],
        pos=[{"x": 10, "y": 20, "hit": False, "atText": "遮罩",
              "atClass": "ant-modal-mask", "blockers": ["ant-modal-mask|图片空间"]}],
        park=[{"parked": 0, "before": 0, "after": 0}],
        blank=[{"err": "找不到可安全点击的空白点"}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r["stage"] == "aim"
    assert "ant-modal-mask" in r["err"]
    assert ses.clicks == []


def test_行不在页面上立刻失败不做无谓重试(monkeypatch):
    """找不到颜色行是结构性错误，换滚动位置救不了，不该白跑五轮。"""
    ses = _FakeSession(scroll=[{"err": "找不到颜色行"}])
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "不存在的颜色"))
    assert r == {"err": "找不到颜色行", "stage": "scroll"}
    assert ses.calls.count("scroll") == 1


def test_收浮层异常不影响主判定(monkeypatch):
    """_park_image_menus 是 best-effort：它自己炸了也只该走到「瞄点失败」，不外抛。"""

    class _Boom(_FakeSession):
        async def eval_json(self, code, **kw):
            if "Escape" in code:
                raise RuntimeError("evaluate 炸了")
            return await super().eval_json(code, **kw)

    ses = _Boom(
        scroll=[{"ok": True}],
        pos=[{"x": 1, "y": 2, "hit": False, "atText": "x", "atClass": "y",
              "blockers": []}],
    )
    _nosleep(monkeypatch)
    r = asyncio.run(P._skc_open_space(ses, "黑色"))
    assert r["stage"] == "aim"               # 给出正常的失败，而不是抛异常
    assert ses.calls.count("scroll") == 5    # 收浮层炸掉不该打断换位重试


def test_每挂完一张就主动收菜单(monkeypatch):
    """预防优于事后救：菜单停在视口中段，下一行按钮也滚到中段，几何上必然重叠。

    2026-08-25 复盘 663641923103：整行反复断在 open-space，且换行后仍复现——说明
    上一张/上一行留下的菜单一直没收。故 skc_replace_row 每挂成功一张就收一次。
    """
    src = inspect.getsource(P.skc_replace_row)
    body = src.split('attached.append(')[1]
    assert "_park_image_menus" in body, "挂载成功后没有主动收菜单"
