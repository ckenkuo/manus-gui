"""店小秘发布操作：media.menus。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish.browser import BrowserSession


# 收起残留的图片菜单浮层（素材图/SKC 那套 4~5 项菜单）。
# 【为什么必须主动收】菜单是 position:fixed、z-index 高于表格，一张图挂完后若没收起，
# 下一行/下一张的「选择图片」按钮就被它盖住：瞄点校验会命中菜单项而不是按钮
# （2026-08-24 实测 890843533224 黑色行，落在 '引用采集图片' 上，整行换图就此中断）。
# 判据只按「含空间图片项」认，避免误伤属性行那些 ant-select 浮层（它们是
# .ant-select-dropdown，另有 _park_ghost_dropdowns 负责，两套别混）。
# 收法是派发 Escape + 点空白：ant 的 dropdown 没有关闭按钮，直接 remove 会让下次
# 点击复用不到实例。返回收了几个，供调用方记日志。
_JS_PARK_IMAGE_MENUS = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const live = () => Array.from(document.querySelectorAll('.ant-dropdown')).filter(d =>
    !/display:\s*none/.test(d.getAttribute('style') || '') && d.offsetHeight > 0
    && Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .some(i => (i.textContent || '').trim() === '空间图片'));
  const before = live().length;
  if (!before) return JSON.stringify({parked: 0});
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await sleep(400);
  if (live().length) {
    // Escape 收不掉时点页面空白处：ant 的 dropdown 靠 document 上的 click 收起
    document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    await sleep(500);
  }
  const after = live().length;
  return JSON.stringify({parked: before - after, before, after});
})()"""


# 找一个可以安全真实点击的空白点，用来收起 rc-trigger 系浮层。
# 【为什么要真实点击】ant 的 dropdown（rc-trigger）收起逻辑挂在 document 的
# 【真实】mousedown 上，合成 MouseEvent('click') 与合成 Escape 都进不了那条路
# （2026-08-24 实测 846106032776 紫罗兰行：parked 恒 0，display 属性根本没被设回 none）。
# 与「SKC 行按钮必须 CDP 真实点击」是同一类事实：这个页面上开与关都吃真实事件。
#
# 候选点要排掉一切交互元素（按钮/链接/输入/浮层本身），落在容器 div 或 body 上才用——
# 点错地方可能触发别的表单交互。取不到安全点就不点，宁可退回滚动挪位那条路。
_JS_BLANK_POINT = r"""(() => {
  const bad = 'button, a, input, textarea, select, label, .ant-dropdown, .ant-modal,'
    + ' .ant-select, .ant-checkbox, .ant-radio, [role=button], [class*=btn], [class*=icon]';
  const W = window.innerWidth, H = window.innerHeight;
  const pts = [];
  for (const fy of [0.5, 0.35, 0.65, 0.85, 0.2]) {
    pts.push([6, Math.round(H * fy)]);
    pts.push([W - 6, Math.round(H * fy)]);
  }
  for (const [x, y] of pts) {
    const el = document.elementFromPoint(x, y);
    if (!el || el.closest(bad)) continue;
    return JSON.stringify({x, y, tag: el.tagName,
      at: (el.className || '').toString().slice(0, 40)});
  }
  return JSON.stringify({err: '找不到可安全点击的空白点'});
})()"""


async def _park_image_menus(session: BrowserSession) -> dict:
    """收起残留的图片菜单浮层（best-effort，坏了不影响主流程）。

    先走合成事件那一版（便宜，某些实例吃这套），收不掉再用【CDP 真实点击空白点】——
    rc-trigger 的关闭只认真实 mousedown，见 _JS_BLANK_POINT 上方说明。
    """
    try:
        r = await session.eval_json(_JS_PARK_IMAGE_MENUS)
        if r.get("parked"):
            logger.info(f"收起残留图片菜单浮层 {r['parked']} 个（否则会盖住行按钮）")
        if not r.get("after"):
            return r
        # 合成事件收不掉：换真实鼠标点空白处
        bp = await session.eval_json(_JS_BLANK_POINT)
        if bp.get("err"):
            logger.warning(f"没找到安全空白点，跳过真实点击收浮层：{bp['err']}")
            return r
        await _cdp_click_xy(session, bp["x"], bp["y"])
        await asyncio.sleep(0.6)
        r2 = await session.eval_json(_JS_PARK_IMAGE_MENUS)
        left = r2.get("after") if "after" in r2 else r2.get("parked")
        logger.info(f"真实点击空白点({bp['x']},{bp['y']} on {bp.get('tag')})后"
                    f"残留图片菜单 {left} 个")
        return {"parked": r.get("before", 0) - (r2.get("after") or 0),
                "before": r.get("before"), "after": r2.get("after") or 0,
                "realClick": True}
    except Exception as e:
        logger.warning(f"收起图片菜单浮层失败（忽略）：{e}")
        return {"err": str(e)}


async def _cdp_click_xy(session: BrowserSession, x: int, y: int) -> None:
    """CDP 真实鼠标点击指定坐标（mouseMoved → mousePressed → mouseReleased）。

    每步间隔 120ms：连发太快时页面的事件处理来不及跟上，表现为点了没反应。
    描述编辑器与 SKC 行按钮都必须走这条路，JS click 建立不了绑定。
    """
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        params = {"type": kind, "x": x, "y": y}
        if kind != "mouseMoved":
            params.update(button="left", clickCount=1)
        await session.cdp("Input.dispatchMouseEvent", params)
        await asyncio.sleep(0.12)
