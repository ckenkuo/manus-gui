# -*- coding: utf-8 -*-
"""页面 toast 哨兵的离线单测（不连 CDP、不开浏览器）。

测的是「浮层文案能不能可靠落进日志」这件事本身：
店小秘的错误提示 2~3 秒自动消失，2026-08-24 排查 890843533224 时那条
「该分类已在平台删除！」就是这么溜掉的——日志里只剩后续阶段一串「无处可填」，
根因一个字都没留下。故哨兵改成常驻 MutationObserver + Playwright binding。
"""
import asyncio

import pytest

from app.publish import browser as B


# ---- JS 侧的契约（DOM 交互无法离线跑，故按不变量断言）------------------------

def test_哨兵JS覆盖三种浮层():
    """d-message 是店小秘自有浮层，ant-message/ant-notification 是组件库的。"""
    js = B._JS_TOAST_WATCH
    assert ".d-message" in js
    assert ".ant-message-notice" in js
    assert ".ant-notification-notice" in js


def test_哨兵JS装一次且自带去重():
    js = B._JS_TOAST_WATCH
    assert "__dxmToastInstalled" in js          # 重复注入不叠加 observer
    assert "MutationObserver" in js
    assert "3000" in js                         # 同文案 3 秒内只报一次
    # 装之前已挂在页面上的也要补报（编辑页一加载就弹的类目失效提示）
    assert "document.querySelectorAll(SEL).forEach(report)" in js


def test_哨兵JS里的binding名与常量一致():
    """占位符必须替换掉，否则页面调的是不存在的函数、静默什么都不报。"""
    assert "__DXM_BINDING__" not in B._JS_TOAST_WATCH
    assert B._TOAST_BINDING in B._JS_TOAST_WATCH


# ---- Python 侧的判级与落地 ----------------------------------------------------

def test_坏词命中记warning(caplog):
    """用户需要知道的（含「删除」「失败」等）走 warning。"""
    msgs = []
    sink = B.logger.add(lambda m: msgs.append((m.record["level"].name,
                                               m.record["message"])), level="INFO")
    try:
        B.BrowserSession._on_toast("错误：该分类已在平台删除！")
        B.BrowserSession._on_toast("保存成功")
    finally:
        B.logger.remove(sink)

    levels = {msg: lvl for lvl, msg in msgs}
    assert levels["页面提示：错误：该分类已在平台删除！"] == "WARNING"
    assert levels["页面提示：保存成功"] == "INFO"


def test_空文案不打日志():
    msgs = []
    sink = B.logger.add(lambda m: msgs.append(m.record["message"]), level="INFO")
    try:
        B.BrowserSession._on_toast("")
        B.BrowserSession._on_toast(None)
        B.BrowserSession._on_toast("   ")
    finally:
        B.logger.remove(sink)
    assert not [m for m in msgs if m.startswith("页面提示")]


def test_回调不抛异常():
    """binding 里抛异常会污染页面上那次 JS 调用，故必须自己吞掉。"""
    class Bad:
        def __str__(self): raise ValueError("炸")
    B.BrowserSession._on_toast(Bad())     # 不应抛


def test_关键词表覆盖实测文案():
    """「该分类已在平台删除！」必须被判成 warning（本次排查的那条）。"""
    assert any(w in "错误：该分类已在平台删除！" for w in B._TOAST_BAD_WORDS)


# ---- 会话接线 ----------------------------------------------------------------

class _FakePage:
    def __init__(self):
        self.bindings = []
        self.evaluated = []

    async def expose_binding(self, name, fn):
        if name in self.bindings:
            raise Exception(f"Function \"{name}\" has been already registered")
        self.bindings.append(name)

    async def evaluate(self, code):
        self.evaluated.append(code)
        return '{"installed": true}'


def _session_with(page):
    s = B.BrowserSession()
    s._page = page
    return s


@pytest.mark.asyncio
async def test_binding按页面只注册一次():
    """navigate 会反复回到同一个 Page 对象，同名 expose_binding 第二次会抛。"""
    page = _FakePage()
    s = _session_with(page)
    assert (await s.install_toast_watch()).get("ok")
    assert (await s.install_toast_watch()).get("ok")
    assert page.bindings == [B._TOAST_BINDING]     # 只注册一次
    assert len(page.evaluated) == 2                # 但 observer 每次都重装


@pytest.mark.asyncio
async def test_装不上只告警不抛():
    """best-effort：少了根因线索不该让阶段失败（对齐 fix_hidden_tab）。"""
    class Broken(_FakePage):
        async def expose_binding(self, name, fn):
            raise RuntimeError("CDP 断了")

    s = _session_with(Broken())
    r = await s.install_toast_watch()
    assert r.get("ok") is False and "CDP 断了" in r.get("err", "")


@pytest.mark.asyncio
async def test_会话未打开时不炸():
    assert (await B.BrowserSession().install_toast_watch()).get("ok") is False


def test_close清理页面集合():
    """会话复用时不该残留已关页面的引用。"""
    s = _session_with(_FakePage())
    s._toast_pages.add(s._page)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(s.close())
    assert not s._toast_pages


def test_open与navigate都装哨兵():
    """导航把 window 换掉，observer 随之消失，必须重装——否则只有首屏有监听。"""
    import inspect as _i

    assert "install_toast_watch" in _i.getsource(B.BrowserSession.open)
    assert "install_toast_watch" in _i.getsource(B.BrowserSession.navigate)
