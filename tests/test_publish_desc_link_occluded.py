# -*- coding: utf-8 -*-
"""「更换图片」链接被 fixed 顶栏压住时的处置：离线单测。

2026-08-28 线上表现（pdd-994437651298，logs/20260828102412.log）：⑬ 计划替换 6 张描述图，
6 张【全部】失败，每一张的失败前都先打了同一条 info：

    「更换图片」瞄点落在 DIV（top-header title h70 flex-y-center flex-x-center）上
    「更换图片」瞄点落在 IMG（image-box）上

即 onLink 校验每张都为假——瞄点根本没落在链接上。原实现照样拿这个错坐标点两轮，
于是「轮询 3.2s + 补点一次」全打在页面顶栏上，报出的却是「菜单未展开」，把遮挡说成时序。
后果连锁：6 张保留 1688 原图 → desc_save 回读「仍有外链未转存 img.pddpic.com」
+ 6 张 1200x1200 低于服装硬红线 1340x1785。

根因是原 _JS_DESC_REPLACE_LINK 的视口闸门只看 top < 0 || bottom > innerHeight：
链接停在 y 约 61 时几何上在视口内、闸门放行，而 .top-header 是 fixed、高 70，正压在上面。
而唯一的补救 _park_image_menus 收的是「素材图那套菜单」，对页面顶栏与编辑器自身的预览图
都无效（历史日志统计：ant-dropdown 遮挡 23 次几乎都被补点救回，top-header/image-box
遮挡 13+18 次一次没救回）。

这里钉四件事：
  1. 遮挡诊断在 JS 里真的算得出来（拿 node 跑一遍那段 JS，用假 DOM 复现顶栏压住链接）
  2. 瞄点不可用时【不能】拿它去 CDP 点击
  3. 换模块图落点重瞄时要重滚 + 重点模块图（滚动会让模块失去选中态、右侧面板消失）
  4. 坐标始终不可用时走派发事件兜底，且仍能把图换上
不覆盖真实页面（要 Chrome 和登录态）。
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from app.publish import pipeline


# ---- 1. 那段 JS 到底算不算得出遮挡：拿 node + 假 DOM 验一遍 --------------------
# 【为什么值得单独验 JS】遮挡判定全在浏览器侧（getComputedStyle / elementFromPoint /
# getBoundingClientRect），Python 侧的桩再怎么写也只是假设它算对了。而这次的 bug 恰恰
# 就出在 JS 的判据上——闸门写了、也「通过」了，只是判的不是真正要判的东西。
# 故这里用一个最小假 DOM 复现现场：链接在 y=61、顶栏 fixed 高 70 压在它上面，
# 容器 scrollTop=400 还能往回滚。期望 JS 自己把链接推到顶栏下面并命中。
_DOM_HARNESS = r"""
// 最小假 DOM：只实现被测 JS 用到的那几个 API
const HEADER_H = 70, LINK_H = 20, INNER_H = 1313, INNER_W = 2560;
let bodyScrollTop = START_SCROLL;

function rect(x, y, w, h) {
  return {x, y, width: w, height: h, top: y, bottom: y + h, left: x, right: x + w};
}

// 链接的 y 由容器 scrollTop 决定（右侧面板跟着 .ant-modal-body 滚，见被测注释）。
// 【符号方向】减 scrollTop 是把内容往下带，故链接的视口 y 变【大】——这正是被测 JS
// 用来把链接推出顶栏的手段，写反就会测出「越推越进遮挡带」的假失败。
function linkTop() { return LINK_TOP + (START_SCROLL - bodyScrollTop); }

const link = {
  tagName: 'A', className: 'replace-link', textContent: '更换图片',
  contains: el => el === link,
  getBoundingClientRect: () => rect(1700, linkTop(), 62, LINK_H),
};
const header = {
  tagName: 'DIV', className: 'top-header title h70 flex-y-center flex-x-center',
  contains: () => false,
  getBoundingClientRect: () => rect(0, 0, INNER_W, HEADER_H),
  __pos: 'fixed',
};
const modalBody = {
  tagName: 'DIV', className: 'ant-modal-body', contains: () => false,
  getBoundingClientRect: () => rect(0, 0, INNER_W, INNER_H), __pos: 'static',
  get scrollTop() { return bodyScrollTop; },
  set scrollTop(v) { bodyScrollTop = Math.max(0, v); },
};
const modal = {
  tagName: 'DIV', className: 'ant-modal', contains: el => el !== null,
  getBoundingClientRect: () => rect(0, 0, INNER_W, INNER_H),
  querySelector: sel => sel.includes('ant-modal-body') ? modalBody
    : (sel.includes('smt-desc-content') ? {} : null),
  querySelectorAll: sel => sel.includes('smt-content-right') ? [link] : [],
};

const ALL = [header, modalBody, link];
global.innerHeight = INNER_H;
global.innerWidth = INNER_W;
global.getComputedStyle = el => ({position: el.__pos || 'static',
                                  visibility: 'visible', display: 'block'});
global.document = {
  querySelectorAll: sel => sel === '*' ? ALL : (sel.includes('ant-modal') ? [modal] : []),
  // 命中测试：顶栏盖在最上面，其覆盖范围内一律返回顶栏
  elementFromPoint: (x, y) => {
    if (y >= 0 && y <= HEADER_H) return header;
    const r = link.getBoundingClientRect();
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return link;
    return modalBody;
  },
};
global.MouseEvent = class { constructor(t) { this.type = t; } };

(async () => {
  const out = await (CODE);
  console.log(JSON.stringify({result: JSON.parse(out), finalScrollTop: bodyScrollTop,
                              finalLinkTop: linkTop()}));
})();
"""


def _run_link_js(start_scroll: int, link_top: int = 61) -> dict:
    """在假 DOM 里跑 _JS_DESC_REPLACE_LINK，返回它的输出与收尾时的滚动位置。"""
    code = pipeline._JS_DESC_REPLACE_LINK.replace("__MODAL__", pipeline._JS_DESC_MODAL)
    harness = (_DOM_HARNESS.replace("START_SCROLL", str(start_scroll))
               .replace("LINK_TOP", str(link_top))
               .replace("(CODE)", "(" + code + ")"))
    path = os.path.join(tempfile.mkdtemp(), "harness.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    r = subprocess.run(["node", path], capture_output=True, text=True,
                       encoding="utf-8")
    assert r.returncode == 0, f"node 跑挂了：{r.stderr[:800]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑 JS 断言")
def test_链接上半被顶栏压住时改点露出的下半():
    """这是 2026-08-28 那次 6 张全挂的真实现场：链接 top=61 高 20，顶栏 fixed 高 70。

    原实现只试链接【中心】一个点（y=71 紧贴顶栏边界）且视口闸门恒为假、直接放行，
    于是返回落在顶栏上的坐标。而链接下沿是 81——顶栏下面还露着 11px，本就点得到。
    多点采样先于滚容器解决这一类，代价是零。
    """
    out = _run_link_js(start_scroll=400)
    res = out["result"]

    assert res["onLink"] is True, f"下半露出时就该命中，实际 {res}"
    assert res["hitTag"] == "A", f"命中的应是链接本身，实际落在 {res['hitTag']}"
    assert res["y"] > res["blockerBottom"], \
        f"采到的点必须在遮挡带下沿以下：y={res['y']} blocker={res['blockerBottom']}"
    assert res["blockerBottom"] == 70, f"顶栏下沿该算成 70，实际 {res['blockerBottom']}"
    assert out["finalScrollTop"] == 400, "露出部分够点时不该白滚容器"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑 JS 断言")
def test_链接整条被埋住时滚容器把它推出来():
    """link_top=45 高 20 → 下沿 65，整条都埋在顶栏（高 70）底下，采样再密也无可点之处。

    此时唯一的出路是滚 .ant-modal-body：右侧面板靠 transform 跟随它，减 scrollTop
    就能把链接往下推（2026-08-27 probe_desc_panel_pos.py 实测的定位关系）。
    """
    out = _run_link_js(start_scroll=400, link_top=45)
    res = out["result"]

    assert res["onLink"] is True, f"滚完必须命中，实际 {res}"
    assert out["finalScrollTop"] < 400, "必须真的滚过容器"
    assert out["finalLinkTop"] >= res["blockerBottom"], \
        f"链接仍埋在遮挡带里：top={out['finalLinkTop']} blocker={res['blockerBottom']}"
    assert res["fixTried"], "修正过程要留痕"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 跑 JS 断言")
def test_容器滚不动时如实报未命中而不是硬返回坐标():
    """整条被埋 + scrollTop 已到 0 就无处可推（2026-08-27 probe_desc_moved.py 的怀疑）。

    此时必须如实报 onLink=false 并带上诊断，让上层去换模块图落点——绝不能返回一个
    落在顶栏上的坐标假装成功，那就退化成原来那条「点两轮全打空」。
    """
    out = _run_link_js(start_scroll=0, link_top=45)
    res = out["result"]

    assert res["onLink"] is False, "推不动时不许假装命中"
    assert "top-header" in (res["hitAt"] or ""), f"要报出真正压住它的是谁，实际 {res}"
    assert res["bodyScrollTop"] == 0, "要报出容器已经滚到顶（说明这条路走不通）"
    assert res["fixTried"], "尝试过程要留痕，否则排查只能回真站重探"


# ---- 2~4. Python 侧的处置：不拿废坐标点、换落点重瞄、派发事件兜底 --------------
class _FakeSession:
    """模拟「坐标永远被顶栏遮挡」的页面，记录各条处置路径是否被走到。

    分发靠 JS 源码里的特征串而不是调用次序（同 test_publish_desc_menu_retry 的取向）。
    """

    def __init__(self, on_link_after_blocks=99, dispatch_opens_menu=False):
        # on_link_after_blocks：换到第几轮落点后瞄点才命中（99 表示始终不命中）
        self.on_link_after_blocks = on_link_after_blocks
        self.dispatch_opens_menu = dispatch_opens_menu
        self.clicks = []
        self.scroll_blocks = []
        self.link_reads = 0
        self.dispatched = 0
        self.parked = 0
        self.menu_open = False
        # 瞄点命中过之后，对该坐标的 CDP 点击才算真的落在链接上、能开出菜单
        self.aim_ok = False

    async def eval_json(self, code, timeout=90, retries=3):
        # 派发事件那段也含「更换图片」，故必须先按更专属的特征分流
        if "dispatched" in code:
            self.dispatched += 1
            if self.dispatch_opens_menu:
                self.menu_open = True
            return {"dispatched": True, "x": 1731, "y": 61}
        if "ant-dropdown-menu-item" in code and "found:" in code:
            return {"found": self.menu_open,
                    "visibleMenus": [["空间上传"]] if self.menu_open else []}
        if "更换图片" in code:
            self.link_reads += 1
            ok = len(self.scroll_blocks) >= self.on_link_after_blocks
            if ok:
                self.aim_ok = True
            return {"x": 1731, "y": 61, "onLink": ok,
                    "hitTag": "A" if ok else "DIV",
                    "hitAt": "link" if ok else "top-header title h70",
                    "linkTop": 61, "blockerBottom": 70, "bodyScrollTop": 0,
                    "fixTried": [{"round": 0, "y": 61, "need": 17, "scrollTop": 0}]}
        if "空间上传" in code:
            return {"picked": True}
        if "parked" in code:
            self.parked += 1
            return {"parked": 0, "before": 0, "after": 0}
        if "elementFromPoint" in code and "bad" in code:
            return {"x": 6, "y": 300, "tag": "DIV"}
        if "srcBefore" in code and "scrollIntoView" not in code:
            return {"x": 500, "y": 400, "srcBefore": "https://cdn/a.jpg"}
        if "scrollIntoView" in code:
            # 记下这一轮用的是哪个 block（退让顺序是本次修复的关键之一）
            for b in ("center", "nearest", "start", "end"):
                if ("'" + b + "'") in code or ('"' + b + '"') in code:
                    self.scroll_blocks.append(b)
                    break
            return {"ok": True, "total": 3}
        if "usingCount" in code:
            return {"open": True, "hasButton": True, "modalCount": 1, "count": 3,
                    "srcs": ["https://cdn/a.jpg"] * 3, "sizes": [[800, 1000]] * 3}
        return {}


@pytest.fixture
def _patched(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(pipeline.asyncio, "sleep", _no_sleep)

    async def _upload(session, path, full_cid=None):
        return {"status": "ok", "fileId": "abc/deadbeef"}
    monkeypatch.setattr(pipeline, "upload_image", _upload)

    async def _click(session, x, y):
        session.clicks.append((x, y))
        # 只有瞄点已校验命中时，点链接坐标才真的开菜单——这正是本次修复的要点：
        # 坐标没命中就去点，等于点在顶栏上，菜单永远不会展开
        if (x, y) == (1731, 61) and session.aim_ok:
            session.menu_open = True
    monkeypatch.setattr(pipeline, "_cdp_click_xy", _click)


@pytest.mark.asyncio
async def test_瞄点被遮挡时绝不拿这个坐标去点(_patched):
    """2026-08-28 那次的直接错误：明知 onLink 为假，还照点两轮。

    坐标落在顶栏上，CDP 点击就是点顶栏——rc-trigger 永远收不到 mousedown。
    """
    s = _FakeSession(on_link_after_blocks=99)
    await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")

    assert (1731, 61) not in s.clicks, "被遮挡的坐标一次都不许点（点了就是打在顶栏上）"


@pytest.mark.asyncio
async def test_换模块图落点重瞄且每轮都重点模块图(_patched):
    """容器滚不动时唯一还能动的量是模块图的落点——右侧面板跟着模块走。

    而滚动会让模块失去选中态、右侧面板随之消失，故每轮都必须重新点一次模块图。
    """
    # 首轮沿用 desc_replace 已做过的 center 滚动，故退让到第 2 个 block（nearest）时命中
    s = _FakeSession(on_link_after_blocks=2)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")

    assert s.scroll_blocks == ["center", "nearest"], \
        f"退让顺序应由近及远且命中即止，实际 {s.scroll_blocks}"
    # 模块图瞄点是 (500,400)：首轮 1 次 + 换落点那轮 1 次（不重点则右侧面板不在）
    assert s.clicks.count((500, 400)) >= 2, \
        f"换落点后必须重点模块图，实际点击 {s.clicks}"
    assert not (r["status"] == "error" and r.get("stage") == "menu"), \
        f"退让到命中后该正常走下去，实际 {r}"


@pytest.mark.asyncio
async def test_坐标始终不可用时派发事件兜底(_patched):
    """所有落点都被压住时，坐标这条路物理上走不通，但事件能绕过命中测试直达元素。"""
    s = _FakeSession(on_link_after_blocks=99, dispatch_opens_menu=True)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")

    assert s.dispatched >= 1, "坐标无路时必须派发鼠标事件兜底"
    assert not (r["status"] == "error" and r.get("stage") == "menu"), \
        f"兜底成功后该继续替换，实际 {r}"


@pytest.mark.asyncio
async def test_全都走不通时报错带遮挡诊断(_patched):
    """真的换不了也要一眼看出是遮挡而不是时序，否则又要回真站探测一轮。"""
    s = _FakeSession(on_link_after_blocks=99, dispatch_opens_menu=False)
    r = await pipeline.desc_replace(s, 1, "x.jpg", expect_url="https://cdn/a.jpg")

    assert r["status"] == "error" and r["stage"] == "menu"
    aim = r["linkAim"]
    assert aim["onLink"] is False
    assert aim["linkTop"] == 61 and aim["blockerBottom"] == 70, \
        "必须报出链接位置与遮挡带下沿，这两个数才说明是被压住了"
    assert aim["bodyScrollTop"] == 0, "还要报出容器已滚到顶（说明推不动）"
    assert "派发事件" in r["err"], "错误文案要说清已经试到了哪一步"
