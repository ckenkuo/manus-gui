"""区域识别与一致性校验的离线单测（app/temu_region.py + 采集侧店铺键）。

全部用假 page（只需要 .url 和 .evaluate），不连浏览器、不发网络请求。
"""
import asyncio

import pytest

from app.collect import service as S
from app.temu_region import (
    Region,
    RegionUnconfirmed,
    confirm_region_from_context,
    host_of,
    is_seller_page,
    read_region,
    region_conflict,
    switch_region,
    url_in_region,
)


class FakePage:
    """最小 page 替身：只提供 url 与 evaluate（返回预设的区域读取结果或抛异常）。"""

    def __init__(self, url, result=None, raise_exc=None):
        self.url = url
        self._result = result
        self._raise = raise_exc

    async def evaluate(self, _js, *args):
        if self._raise:
            raise self._raise
        return self._result


def _tabs(active, labels=("全球", "美国", "欧区")):
    return {"labels": list(labels), "active": active, "ambiguous": []}


GLOBAL_URL = "https://agentseller.temu.com/newon/product-select"
US_URL = "https://agentseller-us.temu.com/newon/product-select"


async def _no_sleep(_secs):
    """_enumerate_one_store 里有等抓包的 3s + 20×1s 轮询，测试里必须掐掉否则要跑 20 秒。"""


class TestHostOf:
    def test_extracts_lowercase_hostname(self):
        assert host_of(GLOBAL_URL) == "agentseller.temu.com"
        assert host_of("HTTPS://AgentSeller-US.temu.com/x") == "agentseller-us.temu.com"

    def test_garbage_returns_empty(self):
        assert host_of("") == ""
        assert host_of(None) == ""
        assert host_of("not a url") == ""


class TestReadRegion:
    def test_region_host_differs_between_global_and_us(self):
        """核心实测事实：切区域换域名，故 host 是区域主键。"""
        g = asyncio.run(read_region(FakePage(GLOBAL_URL, _tabs("全球"))))
        u = asyncio.run(read_region(FakePage(US_URL, _tabs("美国"))))
        assert g.ok and u.ok
        assert g.label == "全球" and u.label == "美国"
        assert g.key != u.key  # 同一账号、同一 mallid，但区域不同

    def test_no_active_tab_is_not_ok(self):
        """顶栏没有任何激活标签（未渲染完/改版）→ 不认，交调用方中止。"""
        r = asyncio.run(read_region(FakePage(GLOBAL_URL, _tabs(""))))
        assert not r.ok
        assert r.host == "agentseller.temu.com"
        assert r.labels == ("全球", "美国", "欧区")

    def test_multiple_active_tabs_refuses_to_guess(self):
        """多个标签同时是选中态说明 active 类语义变了，绝不取其一。"""
        r = asyncio.run(read_region(FakePage(
            GLOBAL_URL,
            {"labels": ["全球", "美国"], "active": "", "ambiguous": ["全球", "美国"]},
        )))
        assert not r.ok
        assert r.label == ""

    def test_evaluate_failure_keeps_host_but_not_ok(self):
        """读 DOM 失败只降级：host 仍可用于日志，但不算已确认区域。"""
        r = asyncio.run(read_region(
            FakePage(US_URL, raise_exc=RuntimeError("boom"))))
        assert r.host == "agentseller-us.temu.com"
        assert not r.ok

    def test_labels_are_not_hardcoded(self):
        """区域名从页面读，代码不带映射表：给个没见过的区域名也能正常识别。"""
        r = asyncio.run(read_region(FakePage(
            "https://agentseller-xx.temu.com/x", _tabs("某新区", ("全球", "某新区")))))
        assert r.ok and r.label == "某新区"


class TestRegionConflict:
    def test_same_host_no_conflict(self):
        base = Region(host="agentseller.temu.com", label="全球")
        cur = Region(host="agentseller.temu.com", label="全球")
        assert region_conflict(base, cur) == ""

    def test_different_host_is_conflict(self):
        base = Region(host="agentseller.temu.com", label="全球")
        cur = Region(host="agentseller-us.temu.com", label="美国")
        msg = region_conflict(base, cur)
        assert "区域不一致" in msg and "美国" in msg

    def test_unconfirmed_current_is_conflict(self):
        base = Region(host="agentseller.temu.com", label="全球")
        assert "未能确认区域" in region_conflict(
            base, Region(host="agentseller.temu.com"))

    def test_same_host_different_label_is_tolerated(self):
        """同域两种叫法（美国/美区）是文案差异，不该当冲突拦下来。"""
        base = Region(host="agentseller-us.temu.com", label="美国")
        cur = Region(host="agentseller-us.temu.com", label="美区")
        assert region_conflict(base, cur) == ""


class _FakeCdpClient:
    """够 _enumerate_one_store 跑通的 CDP 替身：不产生任何网络事件。

    于是 cap["body"] 始终为 None，走的正是「没抓到列表请求」那条 best-effort 分支——
    这恰好是我们要验证的：即使抓包一无所获，区域打标也必须照常完成。
    """

    async def send(self, _method, _params=None):
        return {}

    def on(self, _event, _cb):
        pass

    async def detach(self):
        pass


class _FakeCtx:
    def __init__(self, pages):
        self.pages = pages

    async def new_cdp_session(self, _page):
        return _FakeCdpClient()

    async def cookies(self):
        return []


class _EnumPage:
    """最小 product-select 页替身：按脚本特征分派 evaluate 的返回值。"""

    def __init__(self, url, active_label="全球", items=None, region_raises=None):
        self.url = url
        self._label = active_label
        self._items = items if items is not None else [{"spu": "1", "sku_id": "11"}]
        self._region_raises = region_raises

    async def bring_to_front(self):
        pass

    async def evaluate(self, script, _arg=None):
        if "_regionGroup" in script:                      # 读顶栏区域
            if self._region_raises:
                raise self._region_raises
            return {"labels": ["全球", "美国"], "active": self._label, "ambiguous": []}
        if "__USER_INFO__" in script:                     # 读店名
            return "WINTAK"
        if "dataList" in script:                          # 翻页取商品
            return self._items
        return None                                       # 点「查询」等


class TestCollectRegionComesFromTabHost:
    """采集侧不再有「确认区域」关卡：区域＝域名，直接取自页签 URL（2026-08-11 改）。

    删掉的 confirm_active_region / _check_region 原本会在读不到顶栏时中止整批，
    而顶栏读不到的常见原因（没渲染完、列表在内部容器里滚导致顶栏被平移出视口）
    与「该采哪个区域」毫无关系——host 已经把区域定死了。
    """

    def test_blocking_confirm_helpers_are_gone(self):
        assert not hasattr(S, "confirm_active_region")
        assert not hasattr(S, "_check_region")

    def test_tags_region_with_tab_host(self, monkeypatch):
        """每条商品的 region ＝ 本页签的 host；顶栏中文名进 region_label 供 UI 显示。"""
        monkeypatch.setattr(S.asyncio, "sleep", _no_sleep)
        page = _EnumPage(US_URL, "美国")
        _mid, label, items = asyncio.run(
            S._enumerate_one_store(_FakeCtx([page]), page))
        assert label == "WINTAK"
        assert items[0]["region"] == "agentseller-us.temu.com"
        assert items[0]["region_label"] == "美国"

    def test_unreadable_top_bar_still_tags_host(self, monkeypatch):
        """顶栏读不到（改版/没渲染/滚出视口）→ 照采，region 仍是 host，只是显示名为空。

        这是本次改动的要点：原先这种情况会抛 RegionNotConfirmed、整批中止。
        """
        monkeypatch.setattr(S.asyncio, "sleep", _no_sleep)
        page = _EnumPage(GLOBAL_URL, region_raises=RuntimeError("顶栏没读到"))
        _mid, _label, items = asyncio.run(
            S._enumerate_one_store(_FakeCtx([page]), page))
        assert items[0]["region"] == "agentseller.temu.com"
        assert items[0]["region_label"] == ""

    def test_tabs_in_different_regions_each_keep_own_host(self, monkeypatch):
        """同时开着全球和美国的页签不再是错误：各自打自己的 host、归各自的店铺键。"""
        monkeypatch.setattr(S.asyncio, "sleep", _no_sleep)
        got = []
        for page in (_EnumPage(GLOBAL_URL, "全球"), _EnumPage(US_URL, "美国")):
            _m, _l, items = asyncio.run(
                S._enumerate_one_store(_FakeCtx([page]), page))
            got.append(items[0])
        assert got[0]["region"] != got[1]["region"]
        # 同 mallid 跨区域仍分得开——这正是当初引入 region 维度要保住的性质
        for it in got:
            it["mallid"] = "SAME-MALL"
        assert S._store_key(got[0]) != S._store_key(got[1])


class TestStoreKeyWithRegion:
    def test_same_mallid_different_region_are_distinct_stores(self):
        """本次改动的要点：mallid 跨区域不变，故键必须带区域才分得开。"""
        a = {"mallid": "634418228070796", "region": "agentseller.temu.com"}
        b = {"mallid": "634418228070796", "region": "agentseller-us.temu.com"}
        assert S._store_key(a) != S._store_key(b)

    def test_legacy_item_without_region_falls_back_to_mallid(self):
        """老清单没有 region 字段 → 退回纯 mallid，历史偏好仍能匹配上。"""
        assert S._store_key({"mallid": "6344"}) == "6344"

    def test_falls_back_to_store_name_when_no_mallid(self):
        assert S._store_key({"store": "Pawly"}) == "Pawly"

    def test_summarize_stores_labels_include_region(self):
        """同名店必须靠区域后缀区分，否则 UI 下拉出现两个一样的选项。"""
        worklist = [
            {"spu": "1", "mallid": "M1", "store": "Pawly",
             "region": "agentseller.temu.com", "region_label": "全球"},
            {"spu": "2", "mallid": "M1", "store": "Pawly",
             "region": "agentseller-us.temu.com", "region_label": "美国"},
            {"spu": "3", "mallid": "M1", "store": "Pawly",
             "region": "agentseller-us.temu.com", "region_label": "美国"},
        ]
        got = S.summarize_stores(worklist)
        assert len(got) == 2
        assert got[0]["count"] == 2 and got[0]["label"] == "Pawly · 美国"
        assert {s["label"] for s in got} == {"Pawly · 全球", "Pawly · 美国"}

    def test_summarize_stores_without_region_keeps_plain_label(self):
        got = S.summarize_stores([{"spu": "1", "mallid": "M1", "store": "Pawly"}])
        assert got[0]["label"] == "Pawly" and got[0]["key"] == "M1"


class TestRegionGroupLocatorIsClassNameFree:
    """区域切换器必须纯按结构定位——类名靠不住，实测两类页面体系完全不同：
      product-select：<a class="index-module__drItem___2UzKL ...active___3Jovd">
      订单页 mmsos  ：<div class="_2JBlx01R _1Q9JwBPE">（纯 hash，无语义子串）
    早先按 drItem/active 子串匹配，在订单页上一个都读不到（UI 区域下拉为空就是这个原因）。
    """

    def test_no_hardcoded_class_name_substrings(self):
        from app.temu_region import _ACTIVE_REGION_JS, _READ_REGION_LINKS_JS

        for js in (_ACTIVE_REGION_JS, _READ_REGION_LINKS_JS):
            assert "drItem" not in js, "不能再依赖 drItem 类名（订单页没有）"

    def test_locates_by_same_row_layout(self):
        """同一行横排是区域切换器与「商家助手」竖排浮层菜单的决定性区别。"""
        from app.temu_region import _REGION_GROUP_JS

        assert "Math.max(...ys) - Math.min(...ys)" in _REGION_GROUP_JS
        assert "Math.max(...xs) - Math.min(...xs)" in _REGION_GROUP_JS

    def test_anchors_on_bold_top_bar_label(self):
        """靠「当前区域名在顶栏以粗体重复出现」锚定分组：这是区分区域切换器与
        「学习/运营对接/规则中心…」功能按钮组的决定性判据（后者更靠右、项数更多，
        纯靠几何和类名打分会被它盖过）。没有锚定就返回空，绝不把功能按钮当区域交出去。
        """
        from app.temu_region import _REGION_GROUP_JS

        assert "boldLeft" in _REGION_GROUP_JS
        assert "fontWeight" in _REGION_GROUP_JS
        assert "best.anchored" in _REGION_GROUP_JS

    def test_does_not_filter_by_pointer_cursor(self):
        """区域标签在自身/无权限区域是 disabled、cursor:auto——按 pointer 过滤会把
        整组滤掉，反而误中助手菜单（实测踩过）。注释里可以提 cursor，代码里不能拿它过滤。
        """
        from app.temu_region import _REGION_GROUP_JS

        code = "\n".join(
            line.split("//")[0]
            for line in _REGION_GROUP_JS.splitlines()
        )
        assert "cursor" not in code


class TestIsSellerPage:
    def test_matches_any_region_host(self):
        """按 agentseller 子串判，才能同时认出全球域与美国域。"""
        assert is_seller_page(GLOBAL_URL)
        assert is_seller_page(US_URL)
        assert is_seller_page("https://agentseller-xx.temu.com/goods/list")

    def test_rejects_non_seller_pages(self):
        assert not is_seller_page("https://www.temu.com/")
        assert not is_seller_page("https://www.1688.com/")
        assert not is_seller_page("about:blank")
        assert not is_seller_page("")


class TestUrlInRegion:
    def test_builds_url_on_current_region_host(self):
        """活动管线的核心修复：页面 URL 必须拼在当前区域域名下。"""
        us = Region(host="agentseller-us.temu.com", label="美国")
        assert url_in_region("/activity/marketing-activity", us) == (
            "https://agentseller-us.temu.com/activity/marketing-activity")

    def test_tolerates_path_without_leading_slash(self):
        g = Region(host="agentseller.temu.com", label="全球")
        assert url_in_region("goods/list", g) == "https://agentseller.temu.com/goods/list"

    def test_refuses_when_region_has_no_host(self):
        """区域未确认时抛错，绝不默认回落到全球域。"""
        with pytest.raises(RegionUnconfirmed):
            url_in_region("/goods/list", Region())


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)


class TestConfirmRegionFromContext:
    def test_reads_region_from_user_tabs(self):
        ctx = FakeContext([FakePage(US_URL, _tabs("美国"))])
        assert asyncio.run(confirm_region_from_context(ctx)).label == "美国"

    def test_ignores_non_seller_tabs(self):
        """用户开着的 1688/其它页签不该干扰区域判定。"""
        ctx = FakeContext([
            FakePage("https://www.1688.com/", _tabs("全球")),
            FakePage(US_URL, _tabs("美国")),
        ])
        assert asyncio.run(confirm_region_from_context(ctx)).host == \
            "agentseller-us.temu.com"

    def test_no_seller_tab_aborts(self):
        with pytest.raises(RegionUnconfirmed):
            asyncio.run(confirm_region_from_context(
                FakeContext([FakePage("https://www.1688.com/", _tabs("全球"))])))

    def test_mixed_regions_abort(self):
        ctx = FakeContext([FakePage(GLOBAL_URL, _tabs("全球")),
                           FakePage(US_URL, _tabs("美国"))])
        with pytest.raises(RegionUnconfirmed) as e:
            asyncio.run(confirm_region_from_context(ctx))
        assert "区域不一致" in str(e.value)


class TestActivityPathsHaveNoHardcodedHost:
    def test_paths_are_relative(self):
        """活动管线不能再留写死域名的 URL 常量——那会把用户选的区域顶掉。"""
        from app.activity import pipeline as AP

        for name in ("FLUX_PATH", "ACTIVITY_PATH", "ACTIVITY_LOG_PATH",
                     "GOODS_LIST_PATH"):
            val = getattr(AP, name)
            assert val.startswith("/"), f"{name} 应是路径而非完整 URL：{val}"
            assert "temu.com" not in val

    def test_old_url_constants_are_gone(self):
        from app.activity import pipeline as AP

        for name in ("FLUX_URL", "ACTIVITY_URL", "ACTIVITY_LOG_URL",
                     "GOODS_LIST_URL"):
            assert not hasattr(AP, name), f"{name} 仍在，可能有代码还在用写死的全球域"

    def test_activity_log_no_longer_clicks_global_tab(self):
        """报名记录页不能再点顶栏「全球」——那是区域切换器、会跨域跳转。"""
        import inspect

        from app.activity import pipeline as AP

        src = inspect.getsource(AP.read_activity_log_records)
        assert 'get_by_text("全球"' not in src


class TestStoreNameReadsUserInfo:
    """店名必须从 __USER_INFO__ 读，不能再依赖不存在的 rawData 或掉到顶栏启发式。

    2026-08-07 实测：product-select 页 window.rawData 压根不存在，原先「rawData 优先」
    形同虚设，实际一路掉到顶栏最右启发式——那里店名与「查看使用教程」「打开商家助手」
    并排，靠排除词表挡，文案一改就误命中。
    """

    def test_collect_side_reads_malinfolist_first(self):
        from app.collect.service import _READ_STORE_NAME_JS as JS

        # 平台把 mall 拼成 mal，写错就取不到
        assert "__USER_INFO__" in JS and "malInfoList" in JS
        # mallId 实测是数字，比对前必须 String() 归一
        assert "String(m?.mallId" in JS
        # rawData 那级保留（别的后台版本可能有），不能删
        assert "rawData" in JS
        # __USER_INFO__ 必须排在 rawData 之前
        assert JS.index("__USER_INFO__") < JS.index("window.rawData")

    def test_orders_side_reads_malinfolist_first(self):
        from app.orders.pipeline import _STORE_JS as JS

        assert "__USER_INFO__" in JS and "malInfoList" in JS
        assert "rawData" in JS
        assert JS.index("__USER_INFO__") < JS.index("window.rawData")

    def test_orders_side_still_requires_single_mall(self):
        """订单侧没有 mallid 可精确定位，只有一个店时才敢认（多店交给 DOM/显式指定）。"""
        from app.orders.pipeline import _STORE_JS as JS

        assert "length !== 1" in JS


class TestPeekCurrentRegion:
    """订单页首屏的区域探测必须 best-effort：浏览器没开也不能让首屏打不开。"""

    def test_returns_error_when_cdp_unavailable(self, monkeypatch):
        from app.orders import service as OS

        class Boom:
            async def __aenter__(self):
                raise RuntimeError("connect refused")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(OS, "async_playwright", lambda: Boom())
        got = asyncio.run(OS.peek_current_region())
        assert got["error"] and "connect refused" in got["error"]

    def test_returns_region_labels(self, monkeypatch):
        from app.orders import service as OS

        class FakeBrowser:
            contexts = [FakeContext([FakePage(US_URL, _tabs("美国"))])]

            async def close(self):
                pass

        class FakePW:
            class chromium:
                @staticmethod
                async def connect_over_cdp(_u):
                    return FakeBrowser()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(OS, "async_playwright", lambda: FakePW())
        got = asyncio.run(OS.peek_current_region())
        assert got["label"] == "美国" and got["error"] == ""
        assert "欧区" in got["labels"]


class SwitchablePage:
    """会切区域的假 page：goto 到某区域域名后，激活标签随之变化（模拟真实平台行为）。

    实测口径（2026-08-07）：切区域不能靠点标签（业务页上非当前区域全是 disabled），
    只能换域名导航，落地后顶栏 active 就是该区域。

    redirect_to_global=True 模拟「无权限区域被平台重定向回默认区」，用来验证导航后必须复核。
    """

    HOSTS = {"全球": "agentseller.temu.com", "美国": "agentseller-us.temu.com"}

    def __init__(self, url, active, labels=("全球", "美国", "欧区"),
                 redirect_to_global=False, goto_raises=False):
        self.url = url
        self.active = active
        self.labels = list(labels)
        self.redirect_to_global = redirect_to_global
        self.goto_raises = goto_raises
        self.gotos = []

    async def evaluate(self, js, *args):
        # 两段脚本共用同一个 _regionGroup，靠 getAttribute('href') 区分（只有 links 版有）
        if "getAttribute" in js:  # _READ_REGION_LINKS_JS
            return [
                {"text": t, "href": "", "active": t == self.active,
                 "disabled": t != self.active}
                for t in self.labels
            ]
        return {"labels": self.labels, "active": self.active, "ambiguous": []}

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        if self.goto_raises:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")
        host = host_of(url)
        if self.redirect_to_global:  # 无权限 → 被踢回默认区
            self.url = f"https://{self.HOSTS['全球']}/newon/product-select"
            self.active = "全球"
            return
        self.url = url
        hit = next((k for k, v in self.HOSTS.items() if v == host), "")
        self.active = hit or self.active


class TestSwitchRegion:
    def test_switches_to_target_and_lands_on_new_host(self):
        """以 UI 为准的核心：浏览器在全球区、UI 选美国 → 切过去，host 也变。"""
        from app.temu_region import switch_region

        page = SwitchablePage(GLOBAL_URL, "全球")
        got = asyncio.run(switch_region(page, "美国"))
        assert got.label == "美国"
        assert got.host == "agentseller-us.temu.com"
        assert len(page.gotos) == 1

    def test_switch_keeps_path_and_query(self):
        """切区域只换域名：路径与筛选参数必须保留，否则等于悄悄改掉作业范围。"""
        from app.temu_region import switch_region

        page = SwitchablePage(
            "https://agentseller.temu.com/order/list?sortType=1&page=2", "全球")
        asyncio.run(switch_region(page, "美国"))
        assert page.gotos[0] == (
            "https://agentseller-us.temu.com/order/list?sortType=1&page=2")

    def test_no_navigation_when_already_in_target(self):
        """已在目标区域不该导航——白跳一次整页还可能打断页面状态。"""
        from app.temu_region import switch_region

        page = SwitchablePage(US_URL, "美国")
        got = asyncio.run(switch_region(page, "美国"))
        assert got.label == "美国" and page.gotos == []

    def test_unknown_region_reports_available_ones(self):
        from app.temu_region import switch_region

        page = SwitchablePage(GLOBAL_URL, "全球")
        with pytest.raises(RegionUnconfirmed) as e:
            asyncio.run(switch_region(page, "火星区"))
        assert "火星区" in str(e.value) and "全球" in str(e.value)

    def test_region_with_unknown_host_refuses_to_guess(self):
        """欧区域名未实测 → 如实报错，绝不臆造 agentseller-eu 这类域名。"""
        from app.temu_region import switch_region

        page = SwitchablePage(GLOBAL_URL, "全球")
        with pytest.raises(RegionUnconfirmed) as e:
            asyncio.run(switch_region(page, "欧区"))
        assert "欧区" in str(e.value)
        assert page.gotos == []  # 没有瞎试

    def test_verifies_after_navigation_and_raises_when_redirected(self):
        """导航后被平台踢回默认区（无权限）必须报错，不能假定成功继续跑。"""
        from app.temu_region import switch_region

        page = SwitchablePage(GLOBAL_URL, "全球", redirect_to_global=True)
        with pytest.raises(RegionUnconfirmed) as e:
            asyncio.run(switch_region(page, "美国"))
        assert "失败" in str(e.value)

    def test_navigation_error_is_reported(self):
        from app.temu_region import switch_region

        page = SwitchablePage(GLOBAL_URL, "全球", goto_raises=True)
        with pytest.raises(RegionUnconfirmed):
            asyncio.run(switch_region(page, "美国"))

    def test_empty_target_is_rejected(self):
        from app.temu_region import switch_region

        with pytest.raises(RegionUnconfirmed):
            asyncio.run(switch_region(SwitchablePage(GLOBAL_URL, "全球"), ""))

    def test_does_not_switch_by_clicking_disabled_tabs(self):
        """实测：业务页上非当前区域的标签全带 disabled，点击（合成或真实）都不跳。
        所以切换必须走导航，代码里不该再有点区域标签的逻辑。
        """
        import inspect

        from app.temu_region import switch_region

        src = inspect.getsource(switch_region)
        assert "page.goto" in src
        assert ".click(" not in src


class TestUiSelectionWins:
    """显式选定区域仍然以 UI 为准（采集侧改动后，这是唯一会主动动浏览器的路径）。

    采集侧「不选区域」的默认路径已不再探测/校验区域，见 TestCollectRegionComesFromTabHost；
    这里只保留「显式指定 → 切过去」的语义，它由 switch_region 承担。
    """

    def test_switches_mismatched_tab_to_ui_choice(self):
        """UI 选美国、页签停在全球 → 换域名导航切过去。"""
        page = SwitchablePage(GLOBAL_URL, "全球")
        got = asyncio.run(switch_region(page, "美国"))
        assert got.label == "美国"
        assert len(page.gotos) == 1

    def test_tab_already_in_target_region_is_untouched(self):
        """已经在目标区域的页签不做无谓跳转（换域名是整页重载，代价不小）。"""
        page = SwitchablePage(US_URL, "美国")
        got = asyncio.run(switch_region(page, "美国"))
        assert got.label == "美国"
        assert page.gotos == []

    def test_context_level_confirm_switches_too(self):
        ctx = FakeContext([SwitchablePage(GLOBAL_URL, "全球")])
        got = asyncio.run(confirm_region_from_context(ctx, "美国"))
        assert got.label == "美国"


class TestRetargetUrlHost:
    """订单侧 list_url 改域名：只换 host，query 必须原样保留。"""

    def test_replaces_host_and_keeps_query(self):
        from app.orders.service import _retarget_url_host

        url = ("https://agentseller.temu.com/order/list"
               "?sortType=1&status=待发货&page=2#tab")
        got = _retarget_url_host(url, "agentseller-us.temu.com")
        assert got.startswith("https://agentseller-us.temu.com/order/list")
        # 筛选与排序参数是操作者配的，丢了等于悄悄改掉采集范围
        assert "sortType=1" in got and "status=" in got and "page=2" in got
        assert got.endswith("#tab")

    def test_same_host_returns_unchanged(self):
        from app.orders.service import _retarget_url_host

        url = "https://agentseller.temu.com/order/list?sortType=1"
        assert _retarget_url_host(url, "agentseller.temu.com") == url

    def test_empty_host_returns_unchanged(self):
        from app.orders.service import _retarget_url_host

        url = "https://agentseller.temu.com/order/list?sortType=1"
        assert _retarget_url_host(url, "") == url
