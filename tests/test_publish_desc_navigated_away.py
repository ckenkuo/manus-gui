# -*- coding: utf-8 -*-
"""⑬ 描述长图：页签被导航走时的失败语义单测（不连 CDP、不开浏览器）。

2026-08-24 实测 890843533224：⑬ 逐张替换到第 11 张时，本进程的页签被【另一个进程】
导航去了草稿列表，此后每一张都报「当前页面没有「编辑描述」按钮，可能不在编辑页」。
报错本身没错，但两处失灵：
  1. 它把「页面被换走了」说成「可能不在编辑页」——不带 URL，五条一模一样的信息看不出
     是同一个外部原因，只会以为是选择器失效；
  2. 剩下 7 张各重试一次、各报一条同样的错，噪音掩盖唯一的根因，还白等好几轮。
故这里锁两条不变量：错误信息带上当前 URL 并给出 navigatedAway 标志，
且该标志一出现就中断整段替换循环。
"""
import asyncio

import pytest

from app.publish import pipeline as P
from app.publish import service as S


class _FakePage:
    def __init__(self, url):
        self.url = url


class _FakeSession:
    """只实现 desc 阶段用到的两个面：eval_json 与 page.url。"""

    def __init__(self, url, has_button=False):
        self.page = _FakePage(url)
        self._has_button = has_button
        self.evals = 0

    async def eval_json(self, code, **kw):
        self.evals += 1
        # _JS_DESC_STATE：编辑器没开、按钮在不在由构造参数决定
        return {"open": False, "hasButton": self._has_button, "editPageImgs": 0}


def test_被导航走时带URL并标记navigatedAway():
    """页签跑到草稿列表：错误信息要含 URL，且 navigatedAway 为真。"""
    ses = _FakeSession("https://www.dianxiaomi.com/web/popTemu/pageList/draft")
    st = asyncio.run(P._desc_ensure_open(ses))
    assert st.get("navigatedAway") is True
    assert "pageList/draft" in st["err"]          # 具体跑到哪儿了，一眼可见
    assert "另一个" in st["err"]                   # 指出最常见的原因


def test_还在编辑页但没按钮不算被导航走():
    """URL 仍是编辑页却读不到按钮：那是页面结构/渲染问题，不该误报成被导航走。"""
    ses = _FakeSession("https://www.dianxiaomi.com/web/popTemu/edit?id=17353949545")
    st = asyncio.run(P._desc_ensure_open(ses))
    assert not st.get("navigatedAway")
    assert "编辑描述" in st["err"]
    assert "edit?id=" in st["err"]                # URL 照样带上，便于确认页签没跑


def test_取不到URL也不炸():
    """page.url 读不到只是少一条线索，不该让判定本身失败（best-effort）。"""

    class _Broken(_FakeSession):
        @property
        def page(self):
            raise RuntimeError("页签没了")

        @page.setter
        def page(self, v):
            pass

    st = asyncio.run(P._desc_ensure_open(_Broken("x")))
    assert st.get("err")                          # 仍然给出错误，而不是抛异常


def test_resolve_desc_pos返回三元且透传fatal(monkeypatch):
    """_resolve_desc_pos 的 fatal 位取自 desc_map 的 navigatedAway，不靠文案匹配。"""

    async def _map_navigated(session, info_path=""):
        return {"status": "error", "err": "页签已不在编辑页（当前 .../draft）",
                "navigatedAway": True}

    monkeypatch.setattr(S, "desc_map", _map_navigated)
    pos, err, fatal = asyncio.run(S._resolve_desc_pos(None, "http://x/a.jpg"))
    assert (pos, fatal) == (0, True)
    assert "不在编辑页" in err

    async def _map_missing(session, info_path=""):
        return {"status": "ok", "modules": [{"pos": 1, "url": "http://x/b.jpg"}]}

    monkeypatch.setattr(S, "desc_map", _map_missing)
    pos, err, fatal = asyncio.run(S._resolve_desc_pos(None, "http://x/a.jpg"))
    # 源图找不到是【单张】的问题（可能已被删或已替换），后续几张仍该继续试
    assert (pos, fatal) == (0, False)
    assert err


def test_fatal时中断整段而不是逐张重试(monkeypatch, tmp_path):
    """替换到中途被导航走：只报一条、且不再对剩余的图调定位。"""
    calls = {"resolve": 0, "replace": 0}
    msgs = []

    plan = {"delete": [], "replace": [{"pos": i, "url": f"http://x/{i}.jpg"}
                                      for i in range(1, 6)]}

    async def _map(session, info_path=""):
        return {"status": "ok",
                "modules": [{"pos": 1, "url": "http://x/1.jpg"}]}

    async def _plan_desc(mods, info):
        return plan

    async def _resolve(session, url):
        calls["resolve"] += 1
        # 第 1 张正常，第 2 张起页签被导航走
        if calls["resolve"] == 1:
            return 1, "", False
        return 0, "页签已不在编辑页（当前 .../draft）", True

    async def _replace(session, pos, path, expect_url=None, **kw):
        calls["replace"] += 1
        return {"status": "ok"}

    async def _save(session):
        return {"status": "ok", "descImgs": 1, "dxmHosted": 1}

    async def _closed(session):
        return {"status": "ok"}

    async def _emit(ev):
        msgs.append(ev)

    # 第 1 张走「缓存命中」这条最短的路：本测只关心循环的中断时机，不测生图
    en = tmp_path / "desc-edit"
    en.mkdir()

    def _paths(workdir, url):
        p = str(en / "cached.jpg")
        with open(p, "wb") as f:
            f.write(b"x")
        return p, p

    monkeypatch.setattr(S, "desc_map", _map)
    monkeypatch.setattr(S.vision, "plan_desc", _plan_desc)
    monkeypatch.setattr(S, "_resolve_desc_pos", _resolve)
    monkeypatch.setattr(S, "_desc_cache_paths", _paths)
    monkeypatch.setattr(S, "desc_replace", _replace)
    monkeypatch.setattr(S, "desc_save", _save)
    monkeypatch.setattr(S, "ensure_desc_closed", _closed)
    monkeypatch.setattr(S, "_load_info", lambda p: {})

    ctx = {"info_path": "", "workdir": str(tmp_path)}
    asyncio.run(S._st_desc(ctx, None, _emit))

    # 5 张里第 2 张就 fatal：定位只该被调 2 次（不是 5 次）
    assert calls["resolve"] == 2
    assert calls["replace"] == 1
    fatal_msgs = [m for m in msgs
                  if m.get("type") == "manual_check" and "不在编辑页" in m.get("message", "")]
    assert len(fatal_msgs) == 1                       # 只报一条，不是 4 条
    assert "剩余 4 张" in fatal_msgs[0]["message"]     # 说清还有多少张没动
    assert "续跑" in fatal_msgs[0]["message"]          # 给出下一步动作
