"""店小秘发布操作：persistence。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish import navigation, saving
from app.publish.browser import (
    BrowserSession,
    DRAFT_LIST_URL,
    J,
    ONLINE_LIST_URL,
    PUBLISH_FAIL_LIST_URL,
)


# ---- 阶段⑮ 立即发布 --------------------------------------------------------
# 【这里是全管线唯一不可逆的一步】原 skill 与本模块此前刻意不实现发布入口（真实商家
# 账号、发布后要下架才能改）。2026-08-24 按用户明确要求补上：⑭ save 落库之后点顶部
# 「发布」下拉里的「立即发布」，把草稿真正推到平台。调用方必须显式传 confirm=True，
# 防止别处 import 后误触发。
#
# 顶栏三个按钮相邻：「保存」btn-orange、「保存并移入待发布」、「发布 ∨」btn-green
# （带下拉箭头）。故按钮定位一律用文本【精确等于】'发布'——用 includes 会同时命中
# 「保存并移入待发布」（那个只挪列表、不上架，行为完全不同）。
#
# 【这个下拉是 hover 出来的，且 hover 一停就收起】2026-08-24 真站取证：
# `btn.click()` 之后页面上一个含「立即发布」的节点都没有；合成
# mouseover/mouseenter/mousemove 之后才渲染出 .ant-dropdown（inline style 带 left/top）
# + ul.ant-dropdown-menu，项是「立即发布 / 定时发布」。
#
# 【必须等入场动画放完再点，offsetHeight 不能当判据】同日逐帧取证（hover 后 0 /
# 900 / 2400ms 三次采样）：
#   t+900ms   实例已建，但 inline style 是 opacity:0、transform matrix(0,0,0,0,0,0)
#             → 容器与菜单项的 getBoundingClientRect 都是 0x0，
#             而 offsetHeight 此时【已经是最终值】72 / 32
#   t+2400ms  opacity:1、transform none → 容器 rect 84x72、「立即发布」项 rect 76x32
# 所以收敛判据只能用【菜单项的 rect.height > 0】：用 offsetHeight 会在动画中途就通过，
# 拿到 0x0 的坐标（表现为 publish-now-item-zero-height，或更坏——点在页面外）。
#
# 顺带纠正一个我一度写下的错误结论：菜单【不会】因为 evaluate 结束而收起。
# 早先 dry-run 拿到「定位不到」不是菜单消失，而是第二段 JS 赶在动画中途跑。
# 展开与点击仍合并在一个 evaluate 里——少一次往返，也不必依赖「菜单会一直留着」。
#
# 顶栏与页脚各有一个同 class 的「发布」按钮（rect.top 84 / 7286），取靠上的可见实例：
# 页脚那个 hover 也能展开，但菜单渲染在页面底部，后续判定与取证都更难对齐。
#
# 【菜单容器只认 .ant-dropdown 且必须非零高度】页面上另有两个无 class、height=0、
# width=2552 的包裹层同样含「立即发布」文本，宽度与 top 能过「幽灵浮层」那套过滤，
# 从里面挑节点去点等于点在页面外、什么都不会发生。
#
# 【必须排除「定时发布」】它与「立即发布」同菜单相邻，行为完全不同（定时上架）。
# 故菜单项文本用【精确等于】，不用 includes。

# 只读探测：发布按钮在不在、hover 能否展开菜单。给 publish_now 前置校验与排查用，
# 它【不点】任何菜单项（真正的展开+点击在 _JS_CLICK_PUBLISH_NOW 里一气做完）。
_JS_OPEN_PUBLISH_DROPDOWN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => b.offsetHeight > 0 && (b.textContent || '').trim() === '发布')
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  if (!btns.length) {
    const all = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetHeight > 0)
      .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 20);
    return JSON.stringify({opened: false, reason: 'publish-button-not-found', buttons: all});
  }
  const btn = btns[0];
  btn.scrollIntoView({block: 'center'});
  await sleep(600);
  // 【判据用项的 rect 高度，不用 offsetHeight】见本节顶部逐帧取证：动画中途
  // offsetHeight 已是最终值而 rect 仍 0x0，用它收敛会点在 0x0 坐标上。
  const liveMenu = () => Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (String(d.className).includes('ant-dropdown-hidden')) return false;
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const it = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .find(i => (i.textContent || '').trim() === '立即发布');
    if (!it) return false;
    return it.getBoundingClientRect().height > 0;   // 动画放完才算可点
  });
  const r = btn.getBoundingClientRect();
  const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  // 首轮 hover 后按 300ms 步长轮询等动画放完（实测约 1.5s 到位）；
  // 菜单一直没建则每 6 轮补发一次 hover（Vue 的监听未必挂在同一个事件上）。
  for (let k = 0; k < 20 && !liveMenu(); k++) {
    if (k % 6 === 0) {
      for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
        btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
          view: window, clientX: cx, clientY: cy}));
      }
    }
    await sleep(300);
  }
  const menu = liveMenu();
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('div, ul'))
      .filter(e => (e.textContent || '').includes('立即发布'))
      .map(e => ({cls: String(e.className).slice(0, 60), h: e.offsetHeight}))
      .slice(0, 6);
    return JSON.stringify({opened: false, reason: 'dropdown-did-not-render',
      btnRect: {top: Math.round(r.top), left: Math.round(r.left)}, seen});
  }
  return JSON.stringify({opened: true,
    green: String(btn.className).includes('btn-green'),
    candidates: btns.length,
    items: Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim())});
})()"""


# hover 展开 + 点「立即发布」，一个 evaluate 里做完（见本节顶部：菜单跨 evaluate 会收起）。
_JS_CLICK_PUBLISH_NOW = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => b.offsetHeight > 0 && (b.textContent || '').trim() === '发布')
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  if (!btns.length) return JSON.stringify({clicked: false, reason: 'publish-button-not-found'});
  const btn = btns[0];
  btn.scrollIntoView({block: 'center'});
  await sleep(600);

  // 【判据用项的 rect 高度，不用 offsetHeight】见本节顶部逐帧取证：动画中途
  // offsetHeight 已是最终值而 rect 仍 0x0，用它收敛会点在 0x0 坐标上。
  const liveMenu = () => Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (String(d.className).includes('ant-dropdown-hidden')) return false;
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const it = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .find(i => (i.textContent || '').trim() === '立即发布');
    if (!it) return false;
    return it.getBoundingClientRect().height > 0;   // 动画放完才算可点
  });

  // hover 三连（Vue 的监听未必挂在同一个事件上），最多试 3 轮
  const r = btn.getBoundingClientRect();
  const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  // 首轮 hover 后按 300ms 步长轮询等动画放完（实测约 1.5s 到位）；
  // 菜单一直没建则每 6 轮补发一次 hover（Vue 的监听未必挂在同一个事件上）。
  for (let k = 0; k < 20 && !liveMenu(); k++) {
    if (k % 6 === 0) {
      for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
        btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
          view: window, clientX: cx, clientY: cy}));
      }
    }
    await sleep(300);
  }

  const menu = liveMenu();
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => d.offsetHeight > 0)
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()).filter(Boolean).slice(0, 8))
      .filter(a => a.length);
    return JSON.stringify({clicked: false, reason: 'dropdown-did-not-render', seen});
  }

  const items = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'));
  const item = items.find(i => (i.textContent || '').trim() === '立即发布');
  if (!item) {
    return JSON.stringify({clicked: false, reason: 'publish-now-item-not-found',
      seen: [items.map(i => (i.textContent || '').trim())]});
  }
  const rect = item.getBoundingClientRect();
  if (rect.height <= 0) {
    return JSON.stringify({clicked: false, reason: 'publish-now-item-zero-height'});
  }
  // 菜单还开着的这一刻直接点，中间不回 Python
  item.click();
  await sleep(1500);
  return JSON.stringify({clicked: true,
    menuItems: items.map(i => (i.textContent || '').trim()),
    rect: {top: Math.round(rect.top), left: Math.round(rect.left),
           w: Math.round(rect.width), h: Math.round(rect.height)},
    // 菜单收起是「点中了」的旁证（ant 的菜单项点击后会关闭浮层）
    menuGone: !liveMenu()});
})()"""


# 「立即发布」后平台可能再弹一次二次确认（.ant-modal）。按钮文案未经实测，故兜
# 「确定/确认/立即发布/发布/是」几种常见文案，并把实际弹窗结构一并报出来备查。
_JS_CONFIRM_PUBLISH_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let confirmed = null;
  const dump = () => Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .filter(m => m.offsetHeight > 0)
    .map(m => ({
      title: ((m.querySelector('.ant-modal-title, .ant-modal-confirm-title') || {})
        .textContent || '').trim().slice(0, 60),
      body: ((m.querySelector('.ant-modal-body') || {}).textContent || '').trim().slice(0, 150),
      buttons: Array.from(m.querySelectorAll('button'))
        .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 6)
    }));
  const before = dump();
  for (let k = 0; k < 3 && !confirmed; k++) {
    const modal = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
      .find(m => m.offsetHeight > 0);
    if (!modal) { await sleep(800); continue; }
    const btns = Array.from(modal.querySelectorAll('button'))
      .filter(b => b.offsetHeight > 0);
    // 【不点取消类】只认肯定语义的文案
    const go = btns.find(b => ['确定', '确认', '立即发布', '发布', '是']
      .includes((b.textContent || '').trim()));
    if (!go) break;
    go.click();
    confirmed = (go.textContent || '').trim();
    await sleep(1500);
  }
  return JSON.stringify({confirmed, modalsBefore: before, modalsAfter: dump()});
})()"""


# 发布结果判据：页面 toast（店小秘自定义 d-message 与 ant-message 并存，两边都读）
# + 校验错误块。发布走与保存同一套前端校验，失败同样静默滚到红区块、不弹提示。
_JS_PUBLISH_FEEDBACK = r"""(() => {
  const msgs = Array.from(document.querySelectorAll(
    '.ant-message span, .ant-notification div, [class*="d-message"], [class*="message-content"]'))
    .map(e => (e.textContent || '').trim())
    .filter(t => t && t.length < 200);
  const errs = Array.from(document.querySelectorAll(
    '.ant-form-item-explain-error, [class*="explain-error"]'))
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  return JSON.stringify({messages: Array.from(new Set(msgs)).slice(0, 10),
    errors: Array.from(new Set(errs)).slice(0, 10), url: location.href});
})()"""


# 【发布成功只能去列表取证，前端两个信号都不可用】2026-08-24 真站实测
# （rowid 173539495454560681，确认已上架：在线产品列表有它，平台 ID 2319138008）：
#   - 成功 toast 抓不到：店小秘的提示是自定义实现且转瞬即逝，轮询 12s 一条没有
#     （与 save 那边「保存成功提示 .ant-message 捕获不到」是同一条既有结论）；
#   - 页面不跳转：发布后【留在编辑页】，故「离开编辑页」这个判据恒为 False。
# 靠这两个判的话，明明成功了却报 unknown，人工还得自己去列表确认一遍。
#
# 服务端事实是清楚的：该行从草稿箱消失、出现在在线产品列表。两个列表各读一次，
# 任一确认即算发布成功（草稿箱没了但在线还没刷出来，属于平台侧的短暂延迟）。
#
# 与 _draft_update_time 同样的做法：在【新页签】里读，读完关掉、把会话切回编辑页——
# 当前页签一导航就把编辑页丢了。best-effort：读不到只是少一条证据，不抛异常。
_JS_LIST_HAS_ROW = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 等表格渲染：读太早会把「还没加载」误判成「不在这个列表里」
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('tr[rowid]')) break;
    await sleep(400);
  }
  const tr = document.querySelector('tr[rowid="' + __RID__ + '"]');
  const total = document.querySelectorAll('tr[rowid]').length;
  if (!tr) return JSON.stringify({found: false, rowsOnPage: total});
  return JSON.stringify({found: true, rowsOnPage: total,
    text: (tr.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200)});
})()"""


async def _publish_landed(session: BrowserSession, rowid: str) -> dict:
    """发布取证：草稿箱里没了 + 在线产品列表里有 → 判定已上架（见上方注释）。

    返回 {"published": bool, "draft": {...}, "online": {...}}。
    读列表全程 best-effort：任何一步失败只是少一条证据，绝不抛（对齐 _draft_update_time）。
    """
    page_backup = session._page
    cdp_backup = session._cdp
    js = _JS_LIST_HAS_ROW.replace("__RID__", rowid)
    out: dict = {}
    try:
        for name, url in (("fail", PUBLISH_FAIL_LIST_URL),
                          ("draft", DRAFT_LIST_URL),
                          ("online", ONLINE_LIST_URL)):
            try:
                await session.navigate(url, new_tab=(name == "fail"))
                out[name] = await session.eval_json(js)
            except Exception as e:
                logger.warning(f"读{name}列表失败（忽略）：{e}")
                out[name] = {"err": str(e)}
        # 【失败列表优先，且它有一票否决权】2026-09-01 实测：材积重量这类校验是平台
        # 后端异步做的，点完「立即发布」草稿行确实离开了草稿箱，几秒后才带着失败原因
        # 落到发布失败列表。原判据 `online_found or draft_gone` 在这种情况下命中
        # draft_gone、判成 ok，于是「发了但被拒」被报成成功——本轮宠物衣服 11 个商品
        # 状态文件全记着 publish: ok，实际全在失败列表里，就是这么来的。
        fail_found = bool((out.get("fail") or {}).get("found"))
        online_found = bool((out.get("online") or {}).get("found"))
        draft_gone = (out.get("draft") or {}).get("found") is False
        if fail_found:
            out["published"] = False
            out["reason"] = "在发布失败列表里（平台后端校验拒绝）"
            out["failText"] = (out.get("fail") or {}).get("text")
            return out
        # 【draft_gone 单独不足以判成功】草稿列表分页，_JS_LIST_HAS_ROW 只看当前页
        # （rowsOnPage 恒 50）；商品在第 2 页也会读成 found: False。故要么在线列表
        # 确认有它，要么至少排除了「还在失败列表」这一种——后者已在上面 return。
        out["published"] = online_found or draft_gone
        out["reason"] = ("在线产品列表已有该行" if online_found
                         else "草稿箱当前页已无该行（注意草稿列表分页，非强证据）"
                         if draft_gone else "三个列表都未确认")
        return out
    finally:
        try:
            if session._page is not page_backup:
                await session._page.close()
        except Exception as e:
            logger.warning(f"关闭临时列表页签失败（忽略）：{e}")
        session._page = page_backup
        session._cdp = cdp_backup
        # 开临时页签期间编辑页转后台，rAF 节流开关要重发（同 _draft_update_time）
        await session.fix_hidden_tab()


async def publish_now(session: BrowserSession, rowid: str = "",
                      confirm: bool = False) -> dict:
    """阶段⑮：点顶部「发布」→「立即发布」，把已落库的草稿真正上架。

    【不可逆】必须显式 confirm=True 才执行，否则直接返回 refused——这个闸门是故意的，
    防止其它调用方（或续跑逻辑）在没人盯着时把真实商家草稿推上平台。

    前提：⑭ save 必须已成功。发布走与保存同一套前端校验，草稿没落库就点发布只会
    重复卡在同一批校验错误上。

    判据：
      成功 = 无 .ant-form-item-explain-error，且【列表取证】通过——草稿箱里没了
             或在线产品列表里有它（见 _publish_landed；成功 toast 抓不到、页面也
             不跳转，两个前端信号都不可用）。
      失败 = 有 explain-error → 逐节读红锚点定位区块（页面静默，不弹提示）。
    """
    if not confirm:
        return {"status": "refused", "reason": "发布不可逆，必须显式传 confirm=True"}

    # 遗留遮罩会把点击全部吞掉（保存确认框的老坑），发布前先清一次
    await session.kill_stuck_modals()

    # 展开下拉与点「立即发布」在同一段 JS 里完成（菜单跨 evaluate 就收起了，
    # 见 _JS_CLICK_PUBLISH_NOW 上方注释）。整段重试 3 轮：hover 偶发不触发。
    clicked = {}
    for attempt in (1, 2, 3):
        clicked = await session.eval_json(_JS_CLICK_PUBLISH_NOW)
        if clicked.get("clicked"):
            break
        logger.warning(f"第 {attempt} 次展开/点击「立即发布」未成功：{clicked}")
        await asyncio.sleep(1.5)
    if not clicked.get("clicked"):
        return {"status": "error", "stage": "click-publish-now", **clicked}
    logger.info(f"已点「立即发布」：{clicked}")

    # 二次确认弹窗（若有）
    modal = await session.eval_json(_JS_CONFIRM_PUBLISH_MODAL)
    if modal.get("confirmed"):
        logger.info(f"已确认发布弹窗：{modal.get('confirmed')}")

    # 轮询 12s 收反馈：发布请求往返比保存慢（服务端要过一遍平台校验）
    feedback = {"messages": [], "errors": []}
    for _ in range(16):
        feedback = await session.eval_json(_JS_PUBLISH_FEEDBACK)
        if feedback.get("messages") or feedback.get("errors"):
            break
        await asyncio.sleep(0.75)

    errors = feedback.get("errors") or []
    if errors:
        ids = [{"id": k, "name": v} for k, v in navigation.SECTION_IDS.items()]
        red = await session.eval_json(saving._JS_RED_ANCHORS.replace("__IDS__", J(ids)))
        sections = red.get("redSections") or []
        field_errors = red.get("fieldErrors") or []
        error_detail = ("、".join(s["name"] for s in sections) or "未识别出红色区块")
        if field_errors:
            error_detail += f"；具体错误：{' | '.join(field_errors[:5])}"
        logger.error(f"发布校验失败（页面无 toast，靠红锚点定位）：{error_detail}")
        return {"status": "validation-error", "errors": errors,
                "redSections": sections, "fieldErrors": field_errors,
                "messages": feedback.get("messages"),
                "publishModal": modal}

    msgs = feedback.get("messages") or []
    bad = [m for m in msgs if any(w in m for w in ("失败", "错误", "不能", "请先", "请选择"))]
    if bad:
        logger.error(f"发布被平台拒绝：{bad}")
        return {"status": "rejected", "messages": msgs, "publishModal": modal}

    # 【成功判据去列表取服务端事实，不看前端提示】理由见 _publish_landed 上方注释：
    # 成功 toast 抓不到、页面也不跳转，两个前端信号都恒为「没有」。
    landed = await _publish_landed(session, rowid) if rowid else {}
    if landed.get("published"):
        logger.info(f"发布完成（列表取证）：{landed}")
        return {"status": "ok", "rowid": rowid, "published": True,
                "messages": msgs, "evidence": landed, "publishModal": modal}

    ok_words = [m for m in msgs if any(w in m for w in ("成功", "已发布", "提交"))]
    if ok_words:
        # 列表没读到但抓到了成功提示：也算成功，但把证据缺口说清楚
        logger.info(f"发布完成（据页面提示，列表未取到证）：{ok_words}")
        return {"status": "ok", "rowid": rowid or None, "published": True,
                "messages": msgs, "evidence": landed, "publishModal": modal}

    logger.warning(f"发布后列表未取到证且无成功提示，判据不足：{landed} / {msgs}")
    return {"status": "unknown", "reason": "列表未取到证且未捕获成功提示",
            "messages": msgs, "evidence": landed,
            "publishModal": modal, "publishClick": clicked}
