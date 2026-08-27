"""描述专属菜单未展开的处置：离线单测。

2026-08-26 线上表现（925861971282 第 3 张描述图）：
    {'status': 'error', 'stage': 'menu',
     'detail': {'err': '描述专属菜单未展开', 'visibleMenus': []}}
整页连一个可见 .ant-dropdown 都没有，而紧接着第 4 张同一条代码路径就成功了。
「上一张成功、这一张挂、下一张又成功」不是结构问题，是时序 + rc-trigger 的
「页面已有打开浮层时，第一次真实 mousedown 先用来关旧浮层」。原实现点完硬等 1.8s
就去找菜单，等短了直接判失败，那张图于是退回原图、把 1688 外链留在描述区。

这里钉三件事：菜单晚出来要等到、第一次点击被吃掉要补点一次、瞄点没落在链接上
要先收浮层再重读坐标。不覆盖真实页面（要 Chrome 和登录态）。
"""
import pytest

from app.publish import pipeline


class _FakeSession:
    """按 JS 片段的特征分发假返回值，并记录 CDP 点击次数。

    分发靠 JS 源码里的特征串而不是调用次序：desc_replace 里 eval 的顺序改一下，
    按次序写的桩就会静默对错，而特征串跟着被测代码一起改。
    """

    def __init__(self, menu_ready_after_clicks=1, on_link=True,
                 stale_menu_open=False):
        self.menu_ready_after = menu_ready_after_clicks
        self.on_link = on_link
        # stale_menu_open：一进来就有上一张图留下的描述菜单开着
        self.stale_menu_open = stale_menu_open
        self.clicks = 0
        self.menu_polls = 0
        self.parked = 0
        self.link_reads = 0
        self.blank_clicks = 0

    async def eval_json(self, code, timeout=90, retries=3):
        if "ant-dropdown-menu-item" in code and "found:" in code:
            self.menu_polls += 1
            # 残留菜单：还没点空白处收掉之前，一直报 found=true
            if self.stale_menu_open and not self.blank_clicks:
                return {"found": True, "visibleMenus": [["空间上传", "引用skc轮播图"]]}
            ready = self.clicks >= self.menu_ready_after
            return {"found": ready, "visibleMenus": [] if not ready else [["空间上传"]]}
        if "更换图片" in code:
            self.link_reads += 1
            # 收过浮层后瞄点就正常了（模拟 _park_image_menus 起作用）
            ok = self.on_link or self.parked > 0
            return {"x": 100, "y": 200, "onLink": ok,
                    "hitTag": "A" if ok else "DIV", "hitAt": "ant-dropdown"}
        if "空间上传" in code:            # _JS_DESC_PICK_SPACE：直接算选图成功
            return {"picked": True}
        if "parked" in code:              # _JS_PARK_IMAGE_MENUS
            self.parked += 1
            return {"parked": 1, "before": 1, "after": 0}
        if "elementFromPoint" in code and "bad" in code:   # _JS_BLANK_POINT
            self.blank_point_reads = getattr(self, "blank_point_reads", 0) + 1
            return {"x": 6, "y": 300, "tag": "DIV"}
        if "srcBefore" in code:           # _JS_DESC_BOX_POS
            return {"x": 500, "y": 400, "srcBefore": "https://cdn/a.jpg"}
        if "scrollIntoView" in code:      # _JS_DESC_BOX_SCROLL
            return {"scrolled": True}
        if "usingCount" in code:          # _JS_DESC_STATE
            return {"open": True, "hasButton": True, "modalCount": 1, "count": 3,
                    "srcs": ["https://cdn/a.jpg"] * 3, "sizes": [[800, 1000]] * 3}
        return {}


@pytest.fixture
def _patched(monkeypatch):
    """把 CDP 点击、上传、以及等待都换成假的（等待归零，测的是逻辑不是墙上时间）。"""
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _no_sleep)

    async def _upload(session, path, full_cid=None):
        return {"status": "ok", "fileId": "abc/deadbeef"}
    monkeypatch.setattr(pipeline, "upload_image", _upload)


async def _click_counter(session, x, y):
    session.clicks += 1


@pytest.mark.asyncio
async def test_菜单晚一拍出来要等到而不是直接判失败(monkeypatch, _patched):
    """硬等 1.8s 不够就判失败，是这条 bug 的直接成因。"""
    monkeypatch.setattr(pipeline, "_cdp_click_xy", _click_counter)
    # 第一次点击就会开菜单，但要轮询几轮才可见
    s = _FakeSession(menu_ready_after_clicks=1)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")
    assert r["status"] != "error" or r.get("stage") != "menu"
    assert s.menu_polls >= 1, "必须轮询菜单状态，不能只硬等一次"


@pytest.mark.asyncio
async def test_第一次点击被吃掉时补点一次(monkeypatch, _patched):
    """rc-trigger 会把页面已有浮层时的第一次真实 mousedown 用来关旧浮层。

    模块图点击也算一次，故这里让菜单要到第 3 次点击才出现：
    模块图(1) + 更换图片首点(2) 都不出，补点(3) 才出。
    """
    monkeypatch.setattr(pipeline, "_cdp_click_xy", _click_counter)
    s = _FakeSession(menu_ready_after_clicks=3)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")
    assert not (r["status"] == "error" and r.get("stage") == "menu"), \
        f"补点一次后该成功，实际 {r}"
    assert s.clicks >= 3, "首点没出菜单时必须补点一次"


@pytest.mark.asyncio
async def test_始终不展开时报错带上瞄点诊断(monkeypatch, _patched):
    """真的打不开也要报清楚：坐标、是否命中链接、命中了谁，否则只能靠猜。"""
    monkeypatch.setattr(pipeline, "_cdp_click_xy", _click_counter)
    s = _FakeSession(menu_ready_after_clicks=99)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")
    assert r["status"] == "error" and r["stage"] == "menu"
    assert "轮询" in r["err"]
    assert r["linkAim"]["x"] == 100 and r["linkAim"]["onLink"] is True
    assert s.clicks >= 3, "两轮点击都要打（模块图 1 次 + 更换图片 2 次）"


@pytest.mark.asyncio
async def test_瞄点被浮层盖住时先收浮层再重读坐标(monkeypatch, _patched):
    """残留图片菜单是 fixed，正好停在右侧面板这条带上——同 SKC 行按钮那类遮挡。"""
    monkeypatch.setattr(pipeline, "_cdp_click_xy", _click_counter)
    s = _FakeSession(menu_ready_after_clicks=2, on_link=False)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")
    assert s.parked >= 1, "瞄点没命中链接时必须先收残留浮层"
    assert s.link_reads >= 2, "收完浮层要重读一次坐标（浮层挪走后位置可能变）"
    assert not (r["status"] == "error" and r.get("stage") == "menu")


@pytest.mark.asyncio
async def test_点击前先收掉上一张残留的描述菜单(monkeypatch, _patched):
    """残留菜单会让轮询立刻看到 found=true，而这次点击可能恰好把它 toggle 关掉，
    于是下一步选图又找不到菜单——回到原来那条「菜单未展开」。

    【也钉住 _park_image_menus 对描述菜单无效这件事】那个函数按「空间图片」认菜单，
    描述菜单的对应项叫「空间上传」，文案不同（DESC_MENU_ITEMS 上方 2026-08-20 实测），
    所以这里必须自己点空白处收，不能指望它。
    """
    clicked = []

    async def _track(session, x, y):
        session.clicks += 1
        clicked.append((x, y))
        # 点的是空白点（_JS_BLANK_POINT 返回 6,300）就记一次
        if (x, y) == (6, 300):
            session.blank_clicks += 1

    monkeypatch.setattr(pipeline, "_cdp_click_xy", _track)
    s = _FakeSession(menu_ready_after_clicks=2, stale_menu_open=True)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")

    assert s.blank_clicks >= 1, "点「更换图片」之前必须先把残留菜单收掉"
    assert not (r["status"] == "error" and r.get("stage") == "menu"),         f"收掉残留菜单后该正常走下去，实际 {r}"
