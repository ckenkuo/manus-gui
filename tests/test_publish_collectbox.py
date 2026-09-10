# -*- coding: utf-8 -*-
"""采集箱定时扫描：设置读写、扫描结果解析、Web 接线的离线测试。

为什么值得单测（每条都对应一个「跑起来才发现」的静默故障）：
  1. **默认必须是关闭**。用户明确要求默认关，而 enabled 是从 prefs 文件读的——
     写成 `cfg.get("enabled", True)` 或用 truthy 判断（"false" 字符串为真）都会让
     定时器在全新环境自己跑起来，在后台反复导航用户的 Chrome。
  2. **站点名切分**。接口只给 siteValue 数字，站点名靠「DOM 站点列 ⨯ 接口 siteValue」
     按 rowid 对齐求解，而 DOM 单元格是「站点名+类目路径」粘在一起的。切错了会把
     「美国玩具与游戏」整格当站点名显示，用户照着选站点必然在 ② 认领阶段报「未找到站点」。
  3. **扫描失败不能清空上次清单**。扫挂了（Chrome 没开）若覆盖落盘，用户会看到清单
     凭空清零，以为采集箱被清了。
  4. **URL 与文案的对应不能搞反**。/pageList/offline 在页面上叫「待发布」，
     「采集箱」是 draft——这两个名字最容易被后来者「纠正」成反的（见模块 docstring）。
  5. **Web 接线**：开关接口要能开/关定时器且拒掉越界间隔；作业跑着时立即扫描要 409
     （扫描会把编辑页导航走，毁掉正在填的表单）。

全程离线：不连 CDP、不碰真站，扫描相关的浏览器调用一律 monkeypatch。
app.py 与 app 包同名，普通 import 会被包遮蔽，故按文件路径加载（同 test_publish_web）。
"""

from publish_patching import patch_publish
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.publish import collectbox as cb

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
    monkeypatch.setattr(cb, "CACHE_DIR", str(tmp_path / "publish-cache"))
    return tmp_path


# ---- 设置 -------------------------------------------------------------------

def test_默认关闭(prefs):
    """全新环境（prefs 文件都不存在）必须是关的——用户明确要求默认关。

    默认扫 draft（采集箱）：offline（页面文案「待发布」）里躺的是已经编辑过的商品，
    不是本功能的目标（2026-08-27 用户澄清）。
    """
    assert cb.get_settings() == {"enabled": False,
                                 "intervalMinutes": cb.INTERVAL_DEFAULT,
                                 "state": "draft",
                                 "onlyUnedited": True}
    assert cb.DEFAULT_STATE == "draft"


@pytest.mark.parametrize("stored,want", [
    (None, False), ({}, False), ({"enabled": None}, False), ({"enabled": 0}, False),
    ({"enabled": "false"}, False), ({"enabled": "true"}, False),
    # 1 在 Python 里 == True，但不 is True：判据用 `is True` 才拦得住它
    ({"enabled": 1}, False),
    ({"enabled": True}, True),
])
def test_只有显式True才算开启(prefs, stored, want):
    """非 True 的任何存值都算关。

    "true"/1 这类 truthy 值也判为关：能写进来只可能是手改文件或旧版本残留，
    此时宁可让用户再拨一次开关，也不要「没人要求就自己开始动浏览器」。
    """
    from app.publish import service

    if stored is not None:
        service.save_prefs({"collectBoxScan": stored})
    assert cb.get_settings()["enabled"] is want


def test_设置落盘并回读(prefs):
    cb.set_settings(enabled=True, interval_minutes=15, state="offline")
    assert cb.get_settings() == {"enabled": True, "intervalMinutes": 15,
                                 "state": "offline", "onlyUnedited": True}


def test_只看未编辑默认开(prefs):
    """本功能的用途就是找出还没编辑的去发布，默认就该只列那批。

    与 enabled 的判据刻意相反（那个只认显式 True）：默认开的语义要求
    「只有显式存过 False 才算关」，否则读不到就会退成「列出全部」。
    """
    from app.publish import service

    assert cb.get_settings()["onlyUnedited"] is True
    service.save_prefs({"collectBoxScan": {"onlyUnedited": False}})
    assert cb.get_settings()["onlyUnedited"] is False
    cb.set_settings(only_unedited=True)
    assert cb.get_settings()["onlyUnedited"] is True


def test_只改传了的项(prefs):
    """None＝不动：拨开关不该顺手把间隔重置成默认值。"""
    cb.set_settings(enabled=True, interval_minutes=120, state="offline",
                    only_unedited=False)
    cb.set_settings(enabled=False)
    assert cb.get_settings() == {"enabled": False, "intervalMinutes": 120,
                                 "state": "offline", "onlyUnedited": False}


@pytest.mark.parametrize("bad", [0, 1, 4, 1441, 99999, -30])
def test_间隔越界拒掉(prefs, bad):
    """越界抛 ValueError（路由转 400）：静默改成别的值会让人以为设成功了。"""
    with pytest.raises(ValueError):
        cb.set_settings(interval_minutes=bad)


def test_间隔非整数拒掉(prefs):
    with pytest.raises(ValueError):
        cb.set_settings(interval_minutes="半小时")


def test_未知列表拒掉(prefs):
    with pytest.raises(ValueError):
        cb.set_settings(state="waitPublish")  # 平台有这个词，但不是 dxmState 的取值


def test_损坏的设置回落默认(prefs):
    """prefs 里那一项被写成字符串/数组时按默认跑，不抛——辅助设施坏了不该拦住页面。"""
    from app.publish import service

    for junk in ("开", ["enabled"], 42):
        service.save_prefs({"collectBoxScan": junk})
        assert cb.get_settings() == {"enabled": False,
                                     "intervalMinutes": cb.INTERVAL_DEFAULT,
                                     "state": cb.DEFAULT_STATE,
                                     "onlyUnedited": True}


# ---- URL 与文案的对应（最容易被搞反的一处）------------------------------------

def test_draft是采集箱_offline是待发布():
    """两个 URL 与页面文案的对应关系，反直觉，别搞反。

    2026-08-27 真站子导航取证；用户同日澄清：offline（「待发布」）里是**已经编辑过**的
    商品，本功能要的是 draft（「采集箱」）里已认领但还没编辑的那批，故默认扫 draft。
    这条断言存在的意义就是防止后来者把两者「纠正」成反的。
    """
    assert cb.list_url("draft").endswith("/web/popTemu/pageList/draft")
    assert cb.state_label("draft") == "采集箱"
    assert cb.list_url("offline").endswith("/web/popTemu/pageList/offline")
    assert cb.state_label("offline") == "待发布"
    assert cb.DEFAULT_STATE == "draft", "默认扫采集箱（已认领待编辑），不是待发布"


def test_未知状态回落默认而不抛():
    assert cb.list_url("nope") == cb.list_url(cb.DEFAULT_STATE)


# ---- 站点名切分与对齐 --------------------------------------------------------

@pytest.mark.parametrize("cell,cat,want", [
    # 真站样本（2026-08-27）：单元格是「站点名 + 类目路径」粘连
    ("美国玩具与游戏 > 木偶、手偶 > 毛绒木偶", "玩具与游戏/木偶、手偶/毛绒木偶", "美国"),
    ("哥伦比亚服装、鞋靴和珠宝饰品 > 女童时尚 > 女童服装",
     "服装、鞋靴和珠宝饰品/女童时尚/女童服装/女童牛仔裤", "哥伦比亚"),
    ("欧盟站家居、厨房用品 > 家居装饰", "家居、厨房用品/家居装饰", "欧盟站"),
    # 类目对不上时不硬猜：宁可显示数字，也不要给个错站点名（选错站点认领必失败）
    ("玩具与游戏 > 木偶", "玩具与游戏/木偶", ""),
    ("", "玩具与游戏", ""),
])
def test_站点名从单元格切出(cell, cat, want):
    assert cb._site_name_from_cell(cell, cat) == want


def test_对齐求出siteValue映射():
    dom = [{"rowid": "1", "cell": "美国玩具与游戏 > 木偶"},
           {"rowid": "2", "cell": "哥伦比亚玩具与游戏 > 木偶"}]
    api = [{"rowid": "1", "siteValue": "1", "cat": "玩具与游戏/木偶"},
           {"rowid": "2", "siteValue": "10", "cat": "玩具与游戏/木偶"}]
    assert cb._solve_site_names(dom, api) == {"1": "美国", "10": "哥伦比亚"}


def test_对齐冲突时不采信():
    """同一 siteValue 在两行里指向不同名字时放弃该映射。

    DOM 与接口是两次独立取数，中间列表可能翻页导致行错位；给个错站点名比不给更糟。
    """
    dom = [{"rowid": "1", "cell": "美国玩具与游戏 > 木偶"},
           {"rowid": "2", "cell": "日本站玩具与游戏 > 木偶"}]
    api = [{"rowid": "1", "siteValue": "1", "cat": "玩具与游戏/木偶"},
           {"rowid": "2", "siteValue": "1", "cat": "玩具与游戏/木偶"}]
    assert cb._solve_site_names(dom, api) == {}


# ---- 来源平台识别（决定这行能不能走全流程）------------------------------------
# 【2026-08-27 契约变更】原先只认 1688、非 1688 源返回空 offerId，前端据此只填 rowid，
# 而 rowid 模式必须自带 product-info.json ⇒ 那些行在 GUI 上跑不通。实测采集箱 200 条
# 里非 1688 源占 76 条（38%），全都这么废掉。现在四平台都给出商品 ID 走全流程。
# 这几条 URL 都是实测采集箱里的真实形态（含拼多多老域名 yangkeduo）。

@pytest.mark.parametrize("url,platform,pid", [
    ("https://detail.1688.com/offer/1072680315855.html", "1688", "1072680315855"),
    ("https://m.1688.com/offer/987654321098.html", "1688", "987654321098"),
    # Temu：路径末尾 -g-<数字>.html
    ("https://www.temu.com/s-foo-g-601103468386809.html", "temu", "601103468386809"),
    ("https://www.temu.com/co-en/girls-dresses-g-605936466903004.html",
     "temu", "605936466903004"),
    # 拼多多：goods_id 查询参数；老域名 yangkeduo 也要认（实测有 2 条走它）
    ("https://mobile.pinduoduo.com/goods.html?goods_id=123456789", "pdd", "123456789"),
    ("https://mobile.yangkeduo.com/goods.html?goods_id=994437651298&page_from=39",
     "pdd", "994437651298"),
    # 亚马逊：各国站点后缀不同，ASIN 是 10 位大写字母数字
    ("https://www.amazon.com/dp/B0B54QHP7H?language=en_US", "amazon", "B0B54QHP7H"),
    ("https://www.amazon.com.au/dp/B0B9BJL45T?language=en_AU", "amazon", "B0B9BJL45T"),
    ("https://www.amazon.co.jp/gp/product/B0DR3JT314", "amazon", "B0DR3JT314"),
    # 认不出的域名：归未知源，前端仍只填 rowid（与改动前对非 1688 源的处置一致）
    ("https://www.taobao.com/item.htm?id=123456", "", ""),
    ("", "", ""),
])
def test_识别来源平台(url, platform, pid):
    got = cb._source_info(url)
    assert got["platform"] == platform
    assert got["productId"] == pid
    # offerId 与 productId 同值：前端按 offerId 判「能否走全流程」，键名保留不动
    assert got["offerId"] == pid


def test_拼多多长链接的跟踪参数不影响抽ID():
    """实测链接带 60+ 字符的 _oak_rcto 与搜索词参数，goods_id 在中间。

    抽 ID 必须锚定 goods_id= 参数名——「随便找 6 位以上数字」会命中
    refer_page_id 里的时间戳，让两个不同商品撞到同一个工作目录。
    """
    url = ("https://mobile.pinduoduo.com/goods.html?goods_id=985357680144"
           "&_oak_rcto=YWJkoUxjBIobsrSI9GnrA7uvkTSsmvDeQaiYUDjBkmkvOHxCJBTk5hFtufD1Hm8Z"
           "&_oak_search_term=%E5%B1%B1%E7%AB%B9&refer_page_id=10015_1786254742569_vndt2lqm60"
           "&refer_page_sn=10015")
    assert cb._source_info(url)["productId"] == "985357680144"


# ---- 扫描结果持久化 ----------------------------------------------------------

def test_扫描结果落盘回读(scan_cache):
    r = {"items": [{"rowid": "1", "title": "T"}], "scanned_at": "2026-08-27 10:00",
         "state": "offline", "counts": {"waitPublish": 1}, "total": 1, "error": ""}
    cb.save_scan(r)
    assert cb.load_scan()["items"] == r["items"]
    assert cb.load_scan()["counts"] == {"waitPublish": 1}


def test_缓存损坏当未扫过(scan_cache):
    import os

    os.makedirs(cb.CACHE_DIR, exist_ok=True)
    with open(cb._scan_path(), "w", encoding="utf-8") as f:
        f.write("{不是 json")
    assert cb.load_scan()["items"] == []


def test_缓存缺失当未扫过(scan_cache):
    """空壳必须把前端要读的键都给齐（含 platforms），否则页面上出现 undefined。"""
    assert cb.load_scan() == {"items": [], "scanned_at": "", "state": cb.DEFAULT_STATE,
                              "counts": {}, "total": 0, "error": "", "platforms": {},
                              "progress": {"none": 0, "partial": 0, "full": 0,
                                           "unknown": 0}}


# ---- 编辑进度判据（本功能的核心：筛出「已认领但没编辑过」）----------------------
# 判据来自 edit.json 的 6 个标记，实测依据见 collectbox.py 模块 docstring。
# 这几条测试锁住的是「哪些组合算没编辑」——判错的代价很实在：
#   判成没编辑 → 用户勾选后重跑 15 阶段，把已填内容覆盖掉
#   判成已编辑 → 那行从清单消失，用户以为没采到

def _probe(**marks):
    """造一条探测结果：给了的标记为 True，其余 False。"""
    return {"rowid": "1", "ok": True, **{m: marks.get(m, False) for m in cb.EDIT_MARKS}}


def test_全空判为未编辑():
    r = cb._edit_progress(_probe())
    assert r["edited"] is False and r["stage"] == "none" and r["marks"] == []


def test_只有包装尺寸也算未编辑():
    """实测 4/60 行只命中 dims：包装长宽高是认领时按源数据带进来的，不是编辑产物。

    把它当「编辑过」会让这批行从清单里消失——而它们恰恰是要发布的。
    """
    r = cb._edit_progress(_probe(dims=True))
    assert r["edited"] is False and r["stage"] == "none"
    assert r["marks"] == ["dims"], "标记本身要留着，供 UI 说明判断依据"


@pytest.mark.parametrize("marks", [
    {"origin": True, "region2": True, "shipLimit": True},   # 实测最常见的半编辑形态
    {"sizeCharts": True},
    {"price": True},
    {"origin": True, "dims": True},
])
def test_部分标记判为编辑了一半(marks):
    """任一强标记非空就是人（或管线）在编辑页填过，但没填全 → partial。

    这批不该重新走全流程（会覆盖已填），UI 上提示用「从阶段续跑」。
    """
    r = cb._edit_progress(_probe(**marks))
    assert r["edited"] is True and r["stage"] == "partial"


def test_强标记全中判为已编辑():
    r = cb._edit_progress(_probe(**{m: True for m in cb.EDIT_MARKS}))
    assert r["edited"] is True and r["stage"] == "full"
    # dims 不算强标记，故少了它也仍是 full（包装尺寸认领就带，不该成为「编辑完」的必要条件）
    r2 = cb._edit_progress(_probe(**{m: True for m in cb.EDIT_MARKS if m != "dims"}))
    assert r2["stage"] == "full"


@pytest.mark.parametrize("probe", [
    None, {}, {"rowid": "1", "ok": False, "err": "HTTP 500"},
])
def test_探测失败判为未知而不是未编辑(probe):
    """详情查不到时必须是第三种状态。

    默认判「未编辑」会让已编辑完的行混进清单被重跑一遍；判「已编辑」会让它凭空消失。
    unknown 的行照样列出来但标明查不到，交用户决定。
    """
    r = cb._edit_progress(probe)
    assert r["edited"] is None and r["stage"] == "unknown"
    assert r["probeError"], "要带上原因，UI 才能在 tooltip 里说明为什么判不了"


# ---- 扫描主流程（浏览器调用全 mock）------------------------------------------

class _FakeSession:
    """够用的假会话：按 JS 片段特征返回预设数据，不连 CDP。

    edited 传 {rowid: [命中的标记名]}，用来模拟 edit.json 的探测结果；
    没列到的 rowid 一律返回全 False（＝未编辑）。
    """

    def __init__(self, rows, dom_rows=None, shops=None, counts=None, edited=None,
                 probe_fails=False):
        self.rows = rows
        self.dom_rows = dom_rows or []
        self.shops = shops or {}
        self.counts = counts or {}
        self.edited = edited or {}
        self.probe_fails = probe_fails
        self.navigated = []
        self.probed = []

    async def open(self, **kw):
        return None

    async def close(self):
        return None

    async def navigate(self, url, **kw):
        self.navigated.append(url)
        return {"ok": True, "url": url}

    async def eval_json(self, code, arg=None, **kw):
        if "pageList.json" in code:
            return {"ok": True, "pageNo": 1, "totalPage": 1,
                    "totalSize": len(self.rows), "rows": self.rows}
        if "getOfflineCounts" in code:
            return {"ok": True, "counts": self.counts}
        if "userIn.json" in code:
            return {"ok": True, "shops": self.shops}
        if "edit.json" in code:
            if self.probe_fails:
                raise RuntimeError("详情接口挂了")
            self.probed.append(list(arg or []))
            rows = []
            for rid in arg or []:
                marks = self.edited.get(rid) or []
                rows.append({"rowid": rid, "ok": True,
                             **{m: (m in marks) for m in cb.EDIT_MARKS}})
            return {"rows": rows}
        if "站点" in code:
            return {"ok": True, "rows": self.dom_rows}
        return {}


@pytest.fixture()
def no_site_cache(monkeypatch):
    """站点名映射是模块级内存缓存（只增不减），逐个测试之间要清干净。"""
    monkeypatch.setattr(cb, "_site_names", {})


@pytest.mark.asyncio
async def test_扫描整合各路数据(scan_cache, no_site_cache):
    ses = _FakeSession(
        rows=[{"rowid": "173539495456054619", "title": "女童卫衣", "shopId": "8758807",
               "siteValue": "1", "sourceUrl": "https://detail.1688.com/offer/1072680315855.html",
               "cat": "玩具与游戏/木偶", "createTime": 1787737381000, "updateTime": None,
               "offlineState": "waitPublish", "img": "https://x/a.jpg|https://x/b.jpg",
               "variations": 5, "errMsg": ""}],
        dom_rows=[{"rowid": "173539495456054619", "cell": "美国玩具与游戏 > 木偶"}],
        shops={"8758807": "WINTAK"},
        counts={"waitPublish": 0, "draftNum": 503},
    )
    r = await cb.scan_once("draft", session=ses)
    assert r["error"] == ""
    it = r["items"][0]
    assert it["shop"] == "WINTAK", "shopId 要翻成店铺名"
    assert it["site"] == "美国", "siteValue 要按 DOM 对齐翻成站点名"
    assert it["offerId"] == "1072680315855", "1688 源要抽出 offerId"
    assert it["createdAt"].startswith("2026-"), "毫秒时间戳要格式化"
    assert it["img"] == "https://x/a.jpg", "多图串只取第一张"
    assert it["stage"] == "none" and it["edited"] is False, "没命中任何标记＝未编辑"
    assert r["counts"] == {"waitPublish": 0, "draftNum": 503}
    assert r["progress"] == {"none": 1, "partial": 0, "full": 0, "unknown": 0}
    assert ses.navigated and ses.navigated[0].endswith("/pageList/draft")


def _row(rowid, **kw):
    """造一条列表行（字段齐全，省得每个测试重抄一遍）。"""
    base = {"rowid": rowid, "title": f"商品{rowid}", "shopId": "1", "siteValue": "1",
            "sourceUrl": "https://detail.1688.com/offer/1234567890.html", "cat": "",
            "createTime": 1787737381000, "updateTime": None,
            "offlineState": "waitPublish", "img": "", "variations": 1, "errMsg": ""}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_扫描区分三档编辑进度(scan_cache, no_site_cache):
    """本功能的核心：把「没编辑 / 编辑了一半 / 编辑完」分开，并给出各档条数。"""
    ses = _FakeSession(
        rows=[_row("a"), _row("b"), _row("c"), _row("d")],
        edited={
            # b：实测最常见的半编辑形态（⑤产地 + ⑫运输 跑过，⑨⑩ 没到）
            "b": ["origin", "region2", "shipLimit"],
            "c": list(cb.EDIT_MARKS),          # 编辑完整
            "d": ["dims"],                     # 只有认领带来的包装尺寸＝仍算未编辑
        },
    )
    r = await cb.scan_once("draft", session=ses)
    by = {it["rowid"]: it for it in r["items"]}
    assert by["a"]["stage"] == "none"
    assert by["b"]["stage"] == "partial"
    assert by["c"]["stage"] == "full"
    assert by["d"]["stage"] == "none", "只有 dims 不算编辑过"
    assert r["progress"] == {"none": 2, "partial": 1, "full": 1, "unknown": 0}


@pytest.mark.asyncio
async def test_探测整批失败时行标未知但清单照给(scan_cache, no_site_cache):
    """详情接口挂了不该让整次扫描失败：清单本身（标题/店铺/站点）已经拿到了。"""
    ses = _FakeSession(rows=[_row("a"), _row("b")], probe_fails=True)
    r = await cb.scan_once("draft", session=ses)
    assert r["error"] == "" and len(r["items"]) == 2
    assert all(it["stage"] == "unknown" and it["edited"] is None for it in r["items"])
    assert r["progress"]["unknown"] == 2


@pytest.mark.asyncio
async def test_探测分批不漏行(scan_cache, no_site_cache, monkeypatch):
    """行数超过一批时要分批发，且每行都要有结果（漏一行就会被标成未知）。"""
    monkeypatch.setattr(cb, "EDIT_PROBE_BATCH", 3)
    ses = _FakeSession(rows=[_row(str(i)) for i in range(7)])
    r = await cb.scan_once("draft", session=ses)
    assert [len(b) for b in ses.probed] == [3, 3, 1]
    assert r["progress"]["unknown"] == 0 and r["progress"]["none"] == 7


@pytest.mark.asyncio
async def test_probe关掉时不查详情(scan_cache, no_site_cache):
    """probe=False 只在单测里用（省一层 mock），此时全部行标未知。"""
    ses = _FakeSession(rows=[_row("a")])
    r = await cb.scan_once("draft", session=ses, probe=False)
    assert ses.probed == [] and r["items"][0]["stage"] == "unknown"


@pytest.mark.asyncio
async def test_扫到0条不算错(scan_cache, no_site_cache):
    """采集箱空了（或都编辑过了）是正常业务状态，不该报错。"""
    r = await cb.scan_once("draft", session=_FakeSession(rows=[]))
    assert r["error"] == "" and r["items"] == []
    assert r["progress"] == {"none": 0, "partial": 0, "full": 0, "unknown": 0}


@pytest.mark.asyncio
async def test_接口失败时报错不抛(scan_cache, no_site_cache):
    class _Bad(_FakeSession):
        async def eval_json(self, code, arg=None, **kw):
            if "pageList.json" in code:
                return {"ok": False, "status": 500}
            return {}

    r = await cb.scan_once("offline", session=_Bad(rows=[]))
    assert r["error"] and r["items"] == []


@pytest.mark.asyncio
async def test_附加信息坏了不影响清单(scan_cache, no_site_cache):
    """计数/店铺名/站点对齐都是 best-effort：它们炸了清单照样要给出来。"""

    class _PartlyBad(_FakeSession):
        async def eval_json(self, code, arg=None, **kw):
            if "pageList.json" in code:
                return {"ok": True, "totalPage": 1, "totalSize": 1, "rows": self.rows}
            raise RuntimeError("接口挂了")

    ses = _PartlyBad(rows=[{"rowid": "1", "title": "T", "shopId": "9", "siteValue": "1",
                            "sourceUrl": "", "cat": "", "createTime": None,
                            "offlineState": "waitPublish", "img": "", "variations": 0,
                            "errMsg": ""}])
    r = await cb.scan_once("offline", session=ses)
    assert r["error"] == "" and len(r["items"]) == 1
    assert r["items"][0]["shop"] == "" and r["items"][0]["site"] == ""
    assert r["items"][0]["siteValue"] == "1", "站点名求不到时至少留下数字"


@pytest.mark.asyncio
async def test_扫描失败不覆盖上次清单(scan_cache, prefs, monkeypatch):
    """_tick 在扫挂时必须保留旧清单：清零会让用户以为采集箱被清了。"""
    cb.save_scan({"items": [{"rowid": "old", "title": "上次的"}],
                  "scanned_at": "2026-08-27 09:00", "state": "offline",
                  "counts": {}, "total": 1, "error": ""})

    async def _fail(state, *a, **kw):
        return {"items": [], "error": "CDP 不可用（调试 Chrome 未启动）",
                "scanned_at": "", "state": state, "counts": {}, "total": 0}

    monkeypatch.setattr(cb, "scan_once", _fail)
    await cb._tick()
    assert cb.load_scan()["items"] == [{"rowid": "old", "title": "上次的"}]


@pytest.mark.asyncio
async def test_作业跑着时跳过这一轮(scan_cache, prefs, monkeypatch):
    """扫描会导航那个 CDP 页面，撞上发布作业等于毁掉正在填的表单。"""
    called = []

    async def _scan(state, *a, **kw):
        called.append(state)
        return {"items": [], "error": "", "scanned_at": "", "state": state,
                "counts": {}, "total": 0}

    monkeypatch.setattr(cb, "scan_once", _scan)
    monkeypatch.setattr(cb, "_is_busy", lambda: True)
    await cb._tick()
    assert called == [], "作业跑着时不该扫"

    monkeypatch.setattr(cb, "_is_busy", lambda: False)
    await cb._tick()
    assert called == ["draft"]


@pytest.mark.asyncio
async def test_忙判据自己坏了照样扫(scan_cache, prefs, monkeypatch):
    called = []

    async def _scan(state, *a, **kw):
        called.append(state)
        return {"items": [], "error": "", "scanned_at": "", "state": state,
                "counts": {}, "total": 0}

    def _boom():
        raise RuntimeError("判据炸了")

    monkeypatch.setattr(cb, "scan_once", _scan)
    monkeypatch.setattr(cb, "_is_busy", _boom)
    await cb._tick()
    assert called == ["draft"], "判据异常按不忙处理，不该让定时器停摆"


# ---- Web 接线 ---------------------------------------------------------------

@pytest.fixture(scope="module")
def webapp():
    spec = importlib.util.spec_from_file_location("webapp_collectbox", str(ROOT / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webapp_collectbox"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(webapp):
    return TestClient(webapp.app)


def test_状态接口给出开关与两个列表(client, prefs, scan_cache):
    d = client.get("/publish/collectbox").json()
    assert d["enabled"] is False, "默认关"
    assert d["onlyUnedited"] is True, "默认只列未编辑的"
    assert d["intervalMinutes"] == cb.INTERVAL_DEFAULT
    assert {s["id"] for s in d["states"]} == {"offline", "draft"}
    assert d["state"] == "draft" and d["stateLabel"] == "采集箱"
    assert d["items"] == []
    assert d["progress"] == {"none": 0, "partial": 0, "full": 0, "unknown": 0}


def test_设置接口能改只看未编辑(client, prefs, scan_cache, monkeypatch):
    monkeypatch.setattr(cb, "start", lambda: True)
    r = client.post("/publish/collectbox/settings", json={"onlyUnedited": False})
    assert r.status_code == 200 and r.json()["onlyUnedited"] is False


def test_开关接口能开能关(client, prefs, scan_cache, monkeypatch):
    """开→定时器起来、关→停掉。start/stop 被替掉，不真起后台任务。"""
    acts = []
    monkeypatch.setattr(cb, "start", lambda: acts.append("start") or True)

    async def _stop():
        acts.append("stop")

    monkeypatch.setattr(cb, "stop", _stop)

    r = client.post("/publish/collectbox/settings", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True and acts == ["start"]
    r = client.post("/publish/collectbox/settings", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and acts[-1] == "stop"


def test_开关接口拒掉越界间隔(client, prefs, scan_cache):
    r = client.post("/publish/collectbox/settings", json={"intervalMinutes": 1})
    assert r.status_code == 400 and "5" in r.json()["detail"]


def test_立即扫描在作业跑着时拒掉(client, webapp, prefs, scan_cache):
    """409 而不是排队：扫描会把编辑页导航走，毁掉正在填的表单。"""

    class _Job:
        done = False

    webapp.publish_jobs["fake"] = _Job()
    try:
        r = client.post("/publish/collectbox/scan", json={})
        assert r.status_code == 409 and "发布作业" in r.json()["detail"]
    finally:
        webapp.publish_jobs.pop("fake", None)


def test_跑完的作业不算忙(webapp):
    """publish_jobs 里的条目跑完不删（SSE 重连要取 summary），故不能拿字典非空当判据。"""

    class _Job:
        def __init__(self, done):
            self.done = done

    webapp.publish_jobs["a"] = _Job(True)
    try:
        assert webapp._publish_job_busy() is False
        webapp.publish_jobs["b"] = _Job(False)
        assert webapp._publish_job_busy() is True
    finally:
        webapp.publish_jobs.pop("a", None)
        webapp.publish_jobs.pop("b", None)


def test_立即扫描落盘并返回清单(client, webapp, prefs, scan_cache, monkeypatch):
    async def _scan(state, *a, **kw):
        return {"items": [{"rowid": "1", "title": "扫到的"}], "error": "",
                "scanned_at": "2026-08-27 10:00", "state": state,
                "counts": {"waitPublish": 1}, "total": 1}

    monkeypatch.setattr(webapp.publish_collectbox, "scan_once", _scan)
    d = client.post("/publish/collectbox/scan", json={"state": "draft"}).json()
    assert d["items"] == [{"rowid": "1", "title": "扫到的"}]
    assert cb.load_scan()["items"], "扫到的结果要落盘，重启后仍能看到"


def test_手动扫另一个列表时标签不说谎(client, webapp, prefs, scan_cache, monkeypatch):
    """清单的标签要报「实际扫了哪个」，而不是定时器设置里那个。

    真站复验时发现的：设置是 draft（采集箱），手动扫 offline（待发布），
    返回的 stateLabel 仍是设置里那个——清单标着采集箱却列着待发布的行。
    故分成 stateLabel（定时器将要扫的）与 scannedLabel（这份清单来自的）两个字段。
    """
    async def _scan(state, *a, **kw):
        return {"items": [{"rowid": "1"}], "error": "", "scanned_at": "2026-08-27 10:00",
                "state": state, "counts": {}, "total": 1}

    monkeypatch.setattr(webapp.publish_collectbox, "scan_once", _scan)
    d = client.post("/publish/collectbox/scan", json={"state": "offline"}).json()
    assert d["state"] == "draft", "定时器设置没被这次手动扫改掉"
    assert d["stateLabel"] == "采集箱", "定时器将要扫的仍是 draft"
    assert d["scannedState"] == "offline" and d["scannedLabel"] == "待发布"


def test_立即扫描失败转503(client, webapp, prefs, scan_cache, monkeypatch):
    async def _scan(state, *a, **kw):
        return {"items": [], "error": "CDP 不可用（调试 Chrome 未启动）",
                "scanned_at": "", "state": state, "counts": {}, "total": 0}

    monkeypatch.setattr(webapp.publish_collectbox, "scan_once", _scan)
    r = client.post("/publish/collectbox/scan", json={})
    assert r.status_code == 503 and "CDP" in r.json()["detail"]


# ---- 前端（模板里少一个 id 就是「按钮点了没反应」）----------------------------

def test_顶部开关与清单表格在页面上(client):
    t = client.get("/publish").text
    for need in ('id="chkScanTimer"', 'id="scanTimerHint"', 'id="scanBodyRows"',
                 'id="btnScanNow"', 'id="btnScanToTasks"', 'id="chkScanAll"',
                 'id="selScanState"', 'id="inputScanInterval"', 'id="btnScanInvert"',
                 'id="chkOnlyUnedited"'):
        assert need in t, f"模板缺 {need}"


def test_编辑状态列与四档标签在页面上(client):
    """编辑进度是本功能的核心呈现：列头 + 四档 badge 文案都要在。"""
    t = client.get("/publish").text
    assert "编辑状态" in t, "表格要有编辑状态列"
    assert "STAGE_BADGE" in t
    for label in ("未编辑", "编辑了一半", "已编辑", "查不到"):
        assert label in t, f"缺四档中的「{label}」"


def test_只看未编辑默认不写死在模板上(client):
    """默认值由后端 prefs 决定（renderScanStatus 回填），模板不该带 checked。

    两处各写一份默认会漂移——这与「自动发布」开关刻意相反：那个的默认在前端，
    因为它是「本次点开始用什么参数」；这个是服务端定时器的呈现配置。
    """
    import re

    t = client.get("/publish").text
    m = re.search(r'<input[^>]*id="chkOnlyUnedited"[^>]*>', t)
    assert m and "checked" not in m.group(0)
    assert "d.onlyUnedited" in t, "要从后端状态回填这个开关"


def test_开关不预先勾选(client):
    """默认关是产品决定，模板上不能带 checked——那会让页面一打开就显示成开着。"""
    import re

    t = client.get("/publish").text
    m = re.search(r'<input[^>]*id="chkScanTimer"[^>]*>', t)
    assert m and "checked" not in m.group(0)


def test_前端接线到三个接口(client):
    t = client.get("/publish").text
    assert '"/publish/collectbox"' in t
    assert '"/publish/collectbox/settings"' in t
    assert '"/publish/collectbox/scan"' in t
