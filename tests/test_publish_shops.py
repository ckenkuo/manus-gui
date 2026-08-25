# -*- coding: utf-8 -*-
"""店铺/站点枚举层的离线单测（纯磁盘 + 假 session，不碰真浏览器）。

这一层的两条不变式最要紧：
  1. 站点枚举【只读】——绝不能点到认领弹窗的「确定」，否则用户只是打开发布页
     就凭空认领了一个商品。故下面钉住「JS 里不出现确定按钮」与「结束一定关弹窗」。
  2. 店铺按 platform 筛，但筛空要退回不筛——店小秘改了 platform 代号时下拉不能空掉。
"""
import json
import os

import pytest

from app.publish import shops


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """缓存目录重定向到临时目录，不污染真实 workspace/publish-cache/。"""
    monkeypatch.setattr(shops, "CACHE_DIR", str(tmp_path / "publish-cache"))


class FakeSession:
    """按「JS 片段特征 → 返回值」应答 eval_json 的假会话。

    不用 MagicMock 是为了能断言【调用顺序】与【真实点击的选择器】：站点枚举的正确性
    正在于「先勾店铺、再读站点、最后一定关弹窗」这个次序。
    """

    def __init__(self, replies: dict, url: str = "https://www.dianxiaomi.com/x"):
        self.replies = replies
        self.calls: list = []
        self.clicks: list = []
        self.navigations: list = []
        self._url = url

    class _Page:
        def __init__(self, url):
            self.url = url

    @property
    def page(self):
        return self._Page(self._url)

    def _match(self, js: str):
        for key, val in self.replies.items():
            if key in js:
                return val
        raise AssertionError(f"假会话没有为这段 JS 准备返回值：{js[:120]}")

    async def eval_json(self, js: str) -> dict:
        self.calls.append(js)
        val = self._match(js)
        return val(js) if callable(val) else val

    async def wait_for(self, js, pred, timeout=0, interval=0) -> dict:
        return await self.eval_json(js)

    async def navigate(self, url: str) -> dict:
        self.navigations.append(url)
        self._url = url
        return {"ok": True}

    async def mouse_click(self, selector: str) -> dict:
        self.clicks.append(selector)
        return {"ok": True}


def _userin(shops_list: list) -> dict:
    return {"ok": True, "shops": shops_list, "account": "wintop_design"}


# ---- 店铺枚举 ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_店铺只留Temu店并去掉已删除的():
    s = FakeSession({"userIn.json": _userin([
        {"id": "1", "name": "Pawly", "platform": "pddkj", "isDel": 0, "isExpire": 0},
        {"id": "2", "name": "手工订单", "platform": "our", "isDel": 0, "isExpire": None},
        {"id": "3", "name": "已删店", "platform": "pddkj", "isDel": 1, "isExpire": 0},
    ])})
    got = await shops.list_stores(s)
    assert [x["name"] for x in got] == ["Pawly"]


@pytest.mark.asyncio
async def test_店铺过期只打标记不隐藏():
    """过期店在认领弹窗里仍然在，藏掉会让用户以为店铺丢了。"""
    s = FakeSession({"userIn.json": _userin([
        {"id": "1", "name": "LureYu", "platform": "pddkj", "isDel": 0, "isExpire": 1},
    ])})
    got = await shops.list_stores(s)
    assert got == [{"name": "LureYu", "id": "1", "expired": True}]


@pytest.mark.asyncio
async def test_平台代号筛空时退回不筛():
    """店小秘改了 platform 代号时，宁可多列几个让用户认，也不要给空下拉。"""
    s = FakeSession({"userIn.json": _userin([
        {"id": "1", "name": "Pawly", "platform": "temu_new", "isDel": 0, "isExpire": 0},
    ])})
    got = await shops.list_stores(s)
    assert [x["name"] for x in got] == ["Pawly"]


@pytest.mark.asyncio
async def test_未登录时报错而不是返回空列表():
    s = FakeSession({"userIn.json": {"ok": False, "status": 302}})
    with pytest.raises(RuntimeError, match="未登录"):
        await shops.list_stores(s)


# ---- 站点枚举 ---------------------------------------------------------------

def _site_replies(sites: list, stores=("Pawly",)) -> dict:
    """按 JS 里的特征串分派返回值。

    键挑的是各段 JS 独有的、与排版无关的片段（返回字段名 / 打标属性名），不用
    选择器字符串——那些经 fill_js 替换后换行位置会变，按行匹配一改格式就全崩。
    dict 有序，故 stores 那条要排在 sites 之前（两段都含 shop-label-box）。
    """
    return {
        "ready: n > 0": {"ready": True, "count": 50},        # 等「认领」入口出现
        "no-claim-link": {"clicked": True, "count": 50},     # 点开认领弹窗
        "stores: names": {"ready": True, "stores": list(stores)},
        "wb-probe": {"found": True, "checked": False},
        "sites: sites": {"ready": True, "sites": sites},
        "closed: n": {"closed": 1},
    }


@pytest.mark.asyncio
async def test_站点剔掉全选并带回默认勾选():
    s = FakeSession(_site_replies([
        {"text": "全选", "checked": False},
        {"text": "美国", "checked": True},
        {"text": "加拿大", "checked": False},
    ]))
    got = await shops.list_sites(s, "Pawly")
    assert got["sites"] == ["美国", "加拿大"]
    assert got["default"] == "美国"


@pytest.mark.asyncio
async def test_站点枚举全程不点确定():
    """最高优先级：枚举只读。点到「确定」就等于在用户没发起批次时认领了商品。"""
    s = FakeSession(_site_replies([{"text": "美国", "checked": True}]))
    await shops.list_sites(s, "Pawly")
    assert not any("确定" in js for js in s.calls), "枚举侧出现了指向「确定」的 JS"


@pytest.mark.asyncio
async def test_站点枚举必须真实点击勾店铺():
    """ant-design Checkbox + Vue v-model 不认 JS .click()，且站点区只在勾中后才渲染。"""
    s = FakeSession(_site_replies([{"text": "美国", "checked": True}]))
    await shops.list_sites(s, "Pawly")
    assert s.clicks == ['label[data-wb-target="wb-probe"]']


@pytest.mark.asyncio
async def test_读站点失败也要关弹窗():
    """弹窗留着，它的遮罩会挡住后续发布作业的一切点击。"""
    replies = _site_replies([])
    replies["sites: sites"] = {"ready": False}
    s = FakeSession(replies)
    with pytest.raises(RuntimeError, match="站点列表加载超时"):
        await shops.list_sites(s, "Pawly")
    assert any("closed: n" in js for js in s.calls), "异常路径没有关弹窗"


@pytest.mark.asyncio
async def test_店铺不在弹窗里时报错带可选清单():
    replies = _site_replies([{"text": "美国", "checked": True}], stores=("Pawly", "WINTAK"))
    replies["wb-probe"] = {"found": False, "available": ["Pawly", "WINTAK"]}
    s = FakeSession(replies)
    with pytest.raises(RuntimeError, match="WINTAK"):
        await shops.list_sites(s, "不存在的店")


# ---- 站点缓存 ---------------------------------------------------------------

def test_缓存按店铺分别存取():
    shops.save_sites_cache("Pawly", ["美国", "加拿大"], default="美国")
    shops.save_sites_cache("WINTAK", ["日本"])
    data = shops.load_sites_cache()
    assert data["Pawly"]["sites"] == ["美国", "加拿大"]
    assert data["Pawly"]["default"] == "美国"
    assert data["WINTAK"]["sites"] == ["日本"]


def test_写一个店不清掉别的店():
    """换店铺探测时把别家已探好的结果清零，等于每次切店都要重探。"""
    shops.save_sites_cache("Pawly", ["美国"])
    shops.save_sites_cache("WINTAK", ["日本"])
    assert set(shops.load_sites_cache()) == {"Pawly", "WINTAK"}


def test_缓存文件损坏当未命中():
    path = shops._sites_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{截断的 json")
    assert shops.load_sites_cache() == {}


def test_空站点列表不写缓存():
    """探测失败返回空列表时写进去，会把「未命中」伪装成「该店没有站点」。"""
    shops.save_sites_cache("Pawly", [])
    assert shops.load_sites_cache() == {}


def test_清单个店只清那一个():
    shops.save_sites_cache("Pawly", ["美国"])
    shops.save_sites_cache("WINTAK", ["日本"])
    assert shops.clear_sites_cache("Pawly") == ["Pawly"]
    assert set(shops.load_sites_cache()) == {"WINTAK"}


def test_清全部():
    shops.save_sites_cache("Pawly", ["美国"])
    assert shops.clear_sites_cache() == ["Pawly"]
    assert shops.load_sites_cache() == {}


# ---- fetch_sites 的缓存优先 --------------------------------------------------

@pytest.mark.asyncio
async def test_命中缓存不连浏览器(monkeypatch):
    """探测要开弹窗、占 CDP 页面，命中缓存还去探就会与发布作业抢页面。"""
    shops.save_sites_cache("Pawly", ["美国", "日本"], default="美国")

    async def _boom():
        raise AssertionError("命中缓存却仍去连 CDP")

    monkeypatch.setattr(shops, "ensure_cdp_alive", _boom)
    got = await shops.fetch_sites("Pawly")
    assert got["cached"] is True
    assert got["sites"] == ["美国", "日本"]
    assert got["default"] == "美国"


@pytest.mark.asyncio
async def test_refresh_忽略缓存重探(monkeypatch):
    shops.save_sites_cache("Pawly", ["旧站点"])
    probed = {}

    async def _alive(*a, **kw):
        return True

    class _Sess:
        async def open(self):
            return None

        async def close(self):
            return None

    async def _list_sites(session, store, timeout=180):
        probed["store"] = store
        return {"store": store, "sites": ["美国"], "default": "美国"}

    monkeypatch.setattr(shops, "ensure_cdp_alive", _alive)
    monkeypatch.setattr(shops, "BrowserSession", _Sess)
    monkeypatch.setattr(shops, "list_sites", _list_sites)
    got = await shops.fetch_sites("Pawly", refresh=True)
    assert probed["store"] == "Pawly"
    assert got["sites"] == ["美国"] and got["cached"] is False
    # 重探结果要回写缓存，否则下次又要再探一遍
    assert shops.load_sites_cache()["Pawly"]["sites"] == ["美国"]


@pytest.mark.asyncio
async def test_店铺为空直接拒掉():
    with pytest.raises(ValueError):
        await shops.fetch_sites("")
