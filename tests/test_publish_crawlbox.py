# -*- coding: utf-8 -*-
"""数据采集页「未认领」清单扫描：设置读写、取数解析、Web 接线的离线测试。

为什么值得单测（每条都对应一个「跑起来才发现」的静默故障）：
  1. **不能与采集箱清单搞混**。两者是完全不同的数据源与主键：本模块是
     /api/crawl/list.json 的采集记录（主键 idStr，**没有店铺/站点/rowid**），
     采集箱是 /api/popTemuProduct/pageList.json 的草稿（主键 rowid，有店铺站点）。
     混了的后果：拿采集记录 id 当 rowid 去开编辑页，页面会打开一条别人的草稿。
     故本文件专门测「缓存文件名、prefs 键、路由前缀」三处都与 collectbox 分开。
  2. **主键必须取 idStr 不能取 id**。18 位雪花号（1.7e17）远超 JS 双精度安全整数
     范围（2^53 ≈ 9.0e15），响应一进 JSON.parse 尾数就被抹平——2026-08-30 实测
     50/50 行两者全不相等。读错字段会让去重键静默失真（同一批里重复行清不掉）。
  3. **「全部」标签不传 state**。实测页面点「全部」时 body 里连 state 与
     collectStatus 两个键都不出现，不是传 state=all。自造 state=all 会让「全部」
     标签返回未筛选的结果或直接报错，而这种错在 UI 上表现为「计数对不上」，很难查。
  4. **默认必须是关闭**。理由同采集箱：定时器会在后台反复导航用户的 Chrome。
  5. **不可认领的行要列出来但标出来**，不能藏掉（藏掉会让人以为它们不存在），
     也不能默认算可认领（勾了会在阶段②失败）。
  6. **扫描失败不能清空上次清单**（清零会让用户以为采集记录被清了）。
  7. **两个扫描器共用 browser.PAGE_LOCK**。它们各自 new BrowserSession()，而
     open() 是「挑同一个店小秘页签复用」——各锁自己模块等于没锁，对方一个 navigate
     就把页面换走，且完全静默。

全程离线：不连 CDP、不碰真站，浏览器调用一律 monkeypatch。
app.py 与 app 包同名，普通 import 会被包遮蔽，故按文件路径加载（同 test_publish_web）。
"""

from publish_patching import patch_publish
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.publish import crawlbox as xb

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def prefs(tmp_path, monkeypatch):
    """把 prefs 指到临时文件：设置读写都落在这里，不污染用户真实配置。"""
    from app.publish import service

    p = tmp_path / "publish_prefs.json"
    patch_publish(monkeypatch, "service", "PREFS_PATH", str(p))
    return p


@pytest.fixture()
def scan_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(xb, "CACHE_DIR", str(tmp_path / "publish-cache"))
    return tmp_path


# ---- 与采集箱清单的隔离 -------------------------------------------------------

def test_缓存文件名与采集箱不同(scan_cache):
    """两个列表的行结构不同（那边 rowid+店铺站点，这里 cid+可否认领），
    共用一份缓存会互相覆盖，且表现为「表格里的列全空」。"""
    from app.publish import collectbox as cb

    assert xb._SCAN_NAME != cb._SCAN_NAME


def test_prefs键与采集箱不同(prefs):
    """两个定时器是独立设施：用户可能只想扫未认领。共用一份设置会让「开一个就开两个」。"""
    from app.publish import collectbox as cb

    xb.set_settings(enabled=True, interval_minutes=15)
    assert cb.get_settings()["enabled"] is False, "开未认领扫描不该把采集箱扫描也打开"
    assert cb.get_settings()["intervalMinutes"] == cb.INTERVAL_DEFAULT


def test_共用同一把页面锁():
    """两个扫描器复用同一个店小秘页签，锁必须与被争用的资源同层（browser.PAGE_LOCK）。"""
    from app.publish import browser
    from app.publish import collectbox as cb

    assert xb._scan_lock is browser.PAGE_LOCK
    assert cb._scan_lock is browser.PAGE_LOCK


# ---- 设置 -------------------------------------------------------------------

def test_默认关闭(prefs):
    """全新环境（prefs 文件都不存在）必须是关的，且默认扫「未认领」标签。"""
    assert xb.get_settings() == {"enabled": False,
                                 "intervalMinutes": xb.INTERVAL_DEFAULT,
                                 "state": "no",
                                 "onlyClaimable": True}


def test_设置只改传了的项(prefs):
    xb.set_settings(enabled=True)
    xb.set_settings(interval_minutes=60)
    cfg = xb.get_settings()
    assert cfg["enabled"] is True and cfg["intervalMinutes"] == 60
    assert cfg["state"] == "no", "没传 state 不该被动过"


@pytest.mark.parametrize("bad", [1, 4, 1441, 9999, "abc"])
def test_越界间隔被拒(prefs, bad):
    """越界值抛 ValueError 交路由转 400：这是页面上填错了，不该静默改成别的值。"""
    with pytest.raises(ValueError):
        xb.set_settings(interval_minutes=bad)


def test_未知标签被拒(prefs):
    with pytest.raises(ValueError):
        xb.set_settings(state="draft")  # 那是采集箱的取值，本模块没有


def test_三个标签齐全():
    """页面上就是这三个标签（2026-08-30 抓包），少一个会让下拉里选不到。"""
    assert set(xb.CRAWL_STATES) == {"no", "claimed", "all"}
    assert xb.state_label("no") == "未认领"
    assert xb.state_label("claimed") == "已认领"


def test_全部标签的state参数是None():
    """【关键】实测点「全部」时 body 里连 state 与 collectStatus 两个键都不出现，
    不是传 state=all。写成 "all" 会让接口拿到一个它不认的取值。"""
    assert xb.CRAWL_STATES["all"]["param"] is None
    assert xb.CRAWL_STATES["no"]["param"] == "no"


# ---- 取数解析 ----------------------------------------------------------------

class _FakeSession:
    """够用的 BrowserSession 替身：只回答本模块真正发出的那一个接口调用。

    比采集箱那个替身简单得多——本模块不查详情（未认领是列表接口自己分好的标签）、
    不读店铺名（还没认领哪来的店铺）、不做站点名对齐（同理），故只有一个分支。
    """

    def __init__(self, rows, stat=None, total=None, total_page=1):
        self.rows = rows
        self.stat = stat or {}
        self.total = total if total is not None else len(rows)
        self.total_page = total_page
        self.navigated = []
        self.args = []

    async def open(self, **kw):
        return None

    async def close(self):
        return None

    async def navigate(self, url, **kw):
        self.navigated.append(url)
        return {"ok": True, "url": url}

    async def eval_json(self, code, arg=None, **kw):
        if "crawl/list.json" in code:
            self.args.append(arg)
            return {"ok": True, "pageNo": 1, "totalPage": self.total_page,
                    "totalSize": self.total, "stat": self.stat, "rows": self.rows}
        return {}


def _row(cid, **kw):
    """造一条未认领行（字段齐全，省得每个测试重抄一遍）。"""
    base = {"cid": cid, "title": f"商品{cid}",
            "sourceUrl": "https://detail.1688.com/offer/1234567890.html",
            "sourceName": "1688", "price": "4.4", "priceUsd": "0.66",
            "currency": "CNY", "createTime": 1787737381000, "createName": "u",
            "collectSource": "PLUGIN_LINK", "collectStatus": "",
            "collectFailReason": "", "canClaim": True, "operateHint": "",
            "img": "", "variations": 1}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_扫描解析一整行(scan_cache):
    ses = _FakeSession(
        rows=[_row("173539496022762883",
                   title="新款猫爪起泡胶高颜值可爱水晶泥",
                   img="https://x/a.jpg|https://x/b.jpg")],
        stat={"all": 1748, "no": 395, "claimed": 1353}, total=395)
    r = await xb.scan_once("no", session=ses)
    assert r["error"] == ""
    it = r["items"][0]
    assert it["cid"] == "173539496022762883", "主键是采集记录 idStr"
    assert "rowid" not in it, "未认领的行还没有草稿 rowid（认领才产生），不该凭空造一个"
    assert "shop" not in it and "site" not in it, "店铺站点是认领的产物，本表给不出"
    assert it["offerId"] == "1234567890", "1688 源要抽出 offerId（阶段①按它建工作目录）"
    assert it["platformName"] == "1688"
    assert it["createdAt"].startswith("2026-"), "毫秒时间戳要格式化"
    assert it["img"] == "https://x/a.jpg", "多图串只取第一张"
    assert it["canClaim"] is True
    assert r["counts"] == {"all": 1748, "no": 395, "claimed": 1353}, \
        "计数直接取响应的 data.stat，不必另发 count.json"
    assert r["claimable"] == {"yes": 1, "no": 0}
    assert r["platforms"] == {"1688": 1}
    assert ses.navigated and ses.navigated[0].endswith("/productCrawl/dataAcquisition")


@pytest.mark.asyncio
async def test_扫描传给取数JS的state参数(scan_cache):
    """three 个标签各自传对参数；「全部」必须传 None（＝省略整个键）。"""
    for state, want in (("no", "no"), ("claimed", "claimed"), ("all", None)):
        ses = _FakeSession(rows=[])
        await xb.scan_once(state, session=ses)
        assert ses.args[0][0] == want, f"{state} 标签的 state 参数应为 {want!r}"


@pytest.mark.asyncio
async def test_不可认领的行列出但标出(scan_cache):
    """藏掉会让人以为它们不存在；默认算可认领会让用户勾了在阶段②失败。"""
    ses = _FakeSession(rows=[_row("a"), _row("b", canClaim=False,
                                             operateHint="该商品已被认领")])
    r = await xb.scan_once("no", session=ses)
    assert len(r["items"]) == 2, "不可认领的行也要列出来"
    bad = [i for i in r["items"] if not i["canClaim"]][0]
    assert bad["operateHint"] == "该商品已被认领", "提示原文要带上，否则用户不知为何不可认领"
    assert r["claimable"] == {"yes": 1, "no": 1}


@pytest.mark.asyncio
async def test_canClaim不是认领过没有的判据(scan_cache):
    """2026-08-30 实测「已认领」标签的行 canClaim 也是 true——店小秘允许把同一条采集
    记录再认领到别的店铺/站点。故「认领过没有」只能由标签（state）决定，不能拿 canClaim
    去判：拿它判会把 1353 条已认领的行当成待认领，勾选后在阶段②凭空多建一堆草稿。
    本模块的做法是只按 state 分标签、canClaim 仅用来标「这行能不能发起认领」。"""
    ses = _FakeSession(rows=[_row("a", canClaim=True)])
    r = await xb.scan_once("claimed", session=ses)
    assert r["state"] == "claimed", "扫的是哪个标签要原样记住（前端据此提示别混）"
    assert r["items"][0]["canClaim"] is True, "已认领的行 canClaim 照样为 true"


@pytest.mark.asyncio
async def test_认不出平台的行不给offerId(scan_cache):
    """认不出域名＝阶段①没有提炼适配器。本模块的行连 rowid 都没有、没有续跑路径，
    故前端要把它标成「不支持」并在填入任务框时剔掉——判据就是 offerId 为空。"""
    ses = _FakeSession(rows=[_row("a", sourceUrl="https://example.com/x")])
    r = await xb.scan_once("no", session=ses)
    it = r["items"][0]
    assert it["offerId"] == "" and it["platform"] == ""
    assert r["platforms"] == {"": 1}


@pytest.mark.asyncio
async def test_源价两种币种都留(scan_cache):
    """1688 是人民币、Temu 源本身是美元。只留一个会让人误读一位数量级
    （0.59 元 vs 0.09 美元）。"""
    ses = _FakeSession(rows=[_row("a", price="0.59", priceUsd="0.09", currency="CNY")])
    it = (await xb.scan_once("no", session=ses))["items"][0]
    assert it["price"] == "0.59" and it["priceUsd"] == "0.09" and it["currency"] == "CNY"


@pytest.mark.asyncio
async def test_翻页到取满limit为止(scan_cache):
    """totalPage>1 时要接着翻；显式传 limit 时取满就停、最后一页只请求剩余条数。
    （默认 SCAN_LIMIT=None 是全量扫描，此测试专测显式 limit 仍能截断的分页语义。）"""
    class _Paged(_FakeSession):
        async def eval_json(self, code, arg=None, **kw):
            if "crawl/list.json" in code:
                self.args.append(arg)
                n = arg[2]
                return {"ok": True, "pageNo": arg[1], "totalPage": 9,
                        "totalSize": 400, "stat": {},
                        "rows": [_row(f"p{arg[1]}-{i}") for i in range(n)]}
            return {}

    ses = _Paged(rows=[])
    r = await xb.scan_once("no", limit=120, session=ses)
    assert len(r["items"]) == 120, "取满 limit 就停"
    assert ses.args[-1][2] == 20, "最后一页只该请求剩下的条数，不多取"


@pytest.mark.asyncio
async def test_扫到0条不算错(scan_cache):
    """未认领清空了（都认领完了）是正常业务状态，不该报错。"""
    r = await xb.scan_once("no", session=_FakeSession(rows=[]))
    assert r["error"] == "" and r["items"] == []
    assert r["claimable"] == {"yes": 0, "no": 0}


@pytest.mark.asyncio
async def test_接口失败时报错不抛(scan_cache):
    class _Bad(_FakeSession):
        async def eval_json(self, code, arg=None, **kw):
            if "crawl/list.json" in code:
                return {"ok": False, "status": 500}
            return {}

    r = await xb.scan_once("no", session=_Bad(rows=[]))
    assert r["error"] and r["items"] == []


@pytest.mark.asyncio
async def test_扫描失败不覆盖上次清单(scan_cache, prefs, monkeypatch):
    """_tick 在扫挂时必须保留旧清单：清零会让用户以为采集记录被清了。"""
    xb.save_scan({"items": [{"cid": "old", "title": "上次的"}],
                  "scanned_at": "2026-08-30 09:00", "state": "no",
                  "counts": {}, "total": 1, "error": ""})

    async def _fail(state, *a, **kw):
        return {"items": [], "error": "CDP 不可用（调试 Chrome 未启动）",
                "scanned_at": "", "state": state, "counts": {}, "total": 0}

    monkeypatch.setattr(xb, "scan_once", _fail)
    await xb._tick()
    assert xb.load_scan()["items"] == [{"cid": "old", "title": "上次的"}]


@pytest.mark.asyncio
async def test_作业跑着时跳过这一轮(scan_cache, prefs, monkeypatch):
    """扫描会导航那个 CDP 页面，撞上发布作业等于毁掉正在填的表单。"""
    called = []

    async def _scan(state, *a, **kw):
        called.append(state)
        return {"items": [_row("a")], "error": "", "scanned_at": "x", "state": state,
                "counts": {}, "total": 1}

    monkeypatch.setattr(xb, "scan_once", _scan)
    xb.set_busy_checker(lambda: True)
    try:
        await xb._tick()
        assert called == [], "作业跑着时这一轮该跳过"
        assert xb._state["skipped"] >= 1
        xb.set_busy_checker(lambda: False)
        await xb._tick()
        assert called == ["no"], "作业结束后该照常扫"
    finally:
        xb.set_busy_checker(None)


def test_缓存损坏当未扫过(scan_cache):
    import os

    os.makedirs(xb.CACHE_DIR, exist_ok=True)
    with open(xb._scan_path(), "w", encoding="utf-8") as f:
        f.write("{不是 json")
    d = xb.load_scan()
    assert d["items"] == [] and d["scanned_at"] == ""


# ---- 取数 JS 的字段口径（静默故障，只能靠读 JS 源文本守住）--------------------

def test_取数JS只读idStr():
    """id 是 19 位雪花号，超出 JS 双精度安全整数范围，尾数已被 JSON.parse 抹平
    （2026-08-30 实测 50/50 行 id != idStr）。读 it.id 会让去重键静默失真。"""
    js = xb._JS_LIST
    assert "it.idStr" in js
    assert "it.id ||" not in js and "String(it.id)" not in js, "绝不能回落到 it.id"


def test_取数JS用表单编码():
    """该接口是 application/x-www-form-urlencoded，换成 JSON body 会 400
    （与 collectbox 那个 pageList.json 同样的坑）。"""
    assert "application/x-www-form-urlencoded" in xb._JS_LIST
    assert "JSON.stringify(body)" not in xb._JS_LIST


def test_取数JS在state为空时省略两个键():
    """页面点「全部」时 body 里 state 与 collectStatus 都不出现，不是 state=all。"""
    js = xb._JS_LIST
    assert "collectStatus=CLAIM_TAB" in js
    # 拼 body 的那段必须是「三元判断后整段拼接」，而不是无条件带上 state=
    assert "'&state=' + stateParam" in js


# ---- Web 接线 ----------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """按文件路径加载 app.py（与 app 包同名，普通 import 会被包遮蔽）。"""
    spec = importlib.util.spec_from_file_location("_app_main", ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_app_main"] = mod
    spec.loader.exec_module(mod)
    return TestClient(mod.app), mod


def test_接口给出清单与标签(client, scan_cache, prefs):
    c, _ = client
    r = c.get("/publish/crawlbox")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is False, "默认关"
    assert [s["id"] for s in d["states"]] == ["no", "claimed", "all"]
    assert d["intervalMin"] == xb.INTERVAL_MIN and d["intervalMax"] == xb.INTERVAL_MAX
    assert "items" in d and "claimable" in d


def test_路由与采集箱是两套(client, scan_cache, prefs):
    """两张表的行结构不同，前端也是两张各自渲染的表。合成一个接口会让返回体变成
    「看 state 才知道有哪些字段」。"""
    c, _ = client
    a = c.get("/publish/crawlbox").json()
    b = c.get("/publish/collectbox").json()
    assert [s["id"] for s in a["states"]] != [s["id"] for s in b["states"]]
    assert "claimable" in a and "progress" in b


def test_设置接口开关与越界(client, scan_cache, prefs):
    c, _ = client
    d = c.post("/publish/crawlbox/settings", json={"onlyClaimable": False}).json()
    assert d["onlyClaimable"] is False
    r = c.post("/publish/crawlbox/settings", json={"intervalMinutes": 2})
    assert r.status_code == 400, "越界间隔要 400（页面填错了，不能静默改值）"
    r = c.post("/publish/crawlbox/settings", json={"state": "draft"})
    assert r.status_code == 400, "draft 是采集箱的取值，本模块该拒"


def test_作业跑着时立即扫描409(client, scan_cache, prefs, monkeypatch):
    """扫描会把编辑页导航走，毁掉正在填的表单。"""
    c, mod = client

    class _Job:
        done = False

    monkeypatch.setitem(mod.publish_jobs, "j1", _Job())
    r = c.post("/publish/crawlbox/scan", json={})
    assert r.status_code == 409
