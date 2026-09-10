"""店小秘发布操作：saving。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import time
from app.logger import logger
from app.publish import browser, navigation
from app.publish.browser import BrowserSession, DRAFT_LIST_URL, J
from typing import Optional


# ---- 阶段⑫ 保存（save）------------------------------------------------------
# 【本阶段是全管线的落库点】前面 9 个阶段改的都只是页面上的 Vue 状态，不点保存一律不入库。
#
# 【本函数只点「保存」，绝不碰「发布」】发布是独立的阶段⑮ publish_now，必须显式
# confirm=True 才执行（见该节注释）。save 与它严格分开：落库是可重复的，上架不可逆。
# 顶部操作栏上「保存」（btn-orange）与「发布」（btn-green）相邻，故按钮定位一律用
# 文本【精确等于】'保存' —— 用 includes 会同时命中「保存并移入待发布」（那个会把草稿
# 移出采集箱，行为不同且不可逆）。
#
# 编辑页按钮上 JS el.click() 有效（2026-08-14 实测，触发 POST /api/popTemuProduct/*.json），
# 而 mouse_click / CDP dispatchMouseEvent 在本页被吞（坐标命中、零网络请求），所以这里
# 刻意不用 session.mouse_click。

_JS_CLICK_SAVE = r"""(() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => (b.textContent || '').trim() === '保存');
  if (!btns.length) return JSON.stringify({clicked: false, reason: 'save-button-not-found'});
  const btn = btns[0];
  btn.click();
  return JSON.stringify({clicked: true, orange: btn.className.includes('btn-orange'),
    candidates: btns.length});
})()"""


# 成功判据里「校验错误」这一半：失败时页面【没有 toast】，只有各区块里的
# .ant-form-item-explain-error 会出现文案。.ant-message 一并读是因为个别提示走的是
# 通用组件，但保存成功的提示用 .ant-message 捕获不到（店小秘自定义实现），
# 所以「读到 messages」不能当成功判据，只作诊断信息。
_JS_SAVE_FEEDBACK = r"""(() => {
  const msgs = Array.from(document.querySelectorAll('.ant-message span, .ant-notification div'))
    .map(e => (e.textContent || '').trim()).filter(t => t && t.length < 200);
  const errs = Array.from(document.querySelectorAll('.ant-form-item-explain-error, [class*="explain-error"]'))
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  return JSON.stringify({messages: msgs.slice(0, 10), errors: Array.from(new Set(errs)).slice(0, 10)});
})()"""


# 校验失败时右侧锚点导航里对应区块的链接变红（class 含 f-red），页面静默滚到该区块、
# 不弹任何提示。逐节读锚点是唯一能说清「哪块红了」的办法——只报 explain-error 文案
# 常常是「请选择」这种无区块归属的字样，人看不出该去哪一节改。
_JS_RED_ANCHORS = r"""(() => {
  const ids = __IDS__;
  const out = [];
  for (const a of Array.from(document.querySelectorAll('a, li, span, div'))) {
    const cls = a.className;
    if (typeof cls !== 'string' || !cls.includes('f-red')) continue;
    const txt = (a.textContent || '').trim();
    if (!txt || txt.length > 20) continue;
    const href = a.getAttribute('href') || '';
    const hit = ids.find(x => href.includes(x.id) || txt === x.name);
    if (hit && !out.some(o => o.id === hit.id)) out.push({id: hit.id, name: hit.name});
  }
  // 读取具体的错误提示（ant-form-item-explain-error 类）
  const errors = [];
  for (const err of Array.from(document.querySelectorAll('.ant-form-item-explain-error'))) {
    const txt = (err.textContent || '').trim();
    if (txt && txt.length <= 100) errors.push(txt);
  }
  return JSON.stringify({redSections: out, fieldErrors: errors});
})()"""


# 保存成功后弹「继续编辑 / 返回列表」确认框。【必须关掉】：它是 .ant-modal 级弹窗，
# 遮罩盖住整个页面，不关会挡住顶部操作栏（包括发布按钮），后续任何点击都落在遮罩上。
# 点「继续编辑」而不是「返回列表」——留在编辑页，后续阶段/人工复核还要用这个页面。
_JS_CLOSE_SAVE_CONFIRM = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let closed = null, seen = [];
  for (let k = 0; k < 3; k++) {
    const modal = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
      .find(m => m.offsetHeight > 0 && (m.textContent || '').includes('继续编辑'));
    if (!modal) break;
    const btns = Array.from(modal.querySelectorAll('button'));
    seen = btns.map(b => (b.textContent || '').trim());
    const go = btns.find(b => (b.textContent || '').trim() === '继续编辑')
      || btns.find(b => (b.textContent || '').trim() === '确定');
    if (!go) break;
    go.click();
    closed = (go.textContent || '').trim();
    await sleep(1200);
  }
  const still = !!Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .find(m => m.offsetHeight > 0 && (m.textContent || '').includes('继续编辑'));
  // 「继续编辑」这个文案取自 SKILL.md、原脚本没处理过这个确认框，故文案未经实测。
  // 一旦不符，上面的检测会整体落空（closed 与 stillOpen 双 false，静默漏报），
  // 所以把当前【所有可见弹窗】的标题和按钮一并报出来，供真站验证时对照改文案。
  const visible = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .filter(m => m.offsetHeight > 0)
    .map(m => ({
      title: ((m.querySelector('.ant-modal-title, .ant-modal-confirm-title') || {})
        .textContent || '').trim().slice(0, 40),
      buttons: Array.from(m.querySelectorAll('button'))
        .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 6)
    }));
  return JSON.stringify({closed, buttons: seen, stillOpen: still, visibleModals: visible});
})()"""


async def _draft_update_time(session: BrowserSession, rowid: str) -> Optional[str]:
    """在【新页签】读草稿列表里该 rowid 行的更新时间。

    为什么必须新页签：保存前后各读一次做对比，若在当前页签导航去列表就把编辑页丢了，
    未保存内容全没。best-effort——读不到返回 None，只是少一条成功证据，不该让保存失败。
    """
    page_backup = session._page
    cdp_backup = session._cdp
    try:
        await session.navigate(DRAFT_LIST_URL, new_tab=True)
        js = r"""(() => {
          const tr = document.querySelector('tr[rowid=' + __RID__ + ']');
          if (!tr) return JSON.stringify({found: false});
          const m = (tr.textContent || '').match(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?/g);
          return JSON.stringify({found: true, times: m || []});
        })()""".replace("__RID__", J('"' + rowid + '"'))
        data = await session.wait_for(js, lambda d: d.get("found"), timeout=25)
        times = data.get("times") or []
        return times[-1] if times else None
    except Exception as e:
        logger.warning(f"读草稿更新时间失败（忽略）：{e}")
        return None
    finally:
        # 关掉临时页签、把会话切回编辑页
        try:
            if session._page is not page_backup:
                await session._page.close()
        except Exception as e:
            logger.warning(f"关闭临时列表页签失败（忽略）：{e}")
        session._page = page_backup
        session._cdp = cdp_backup
        # 开临时页签期间编辑页转入后台，rAF 节流那两个开关要重发一次，
        # 否则后续阶段的浮层定位又会读到 -9999（见 browser.fix_hidden_tab）
        await session.fix_hidden_tab()


_JS_CLOSE_FOREIGN_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 关掉「不属于保存确认框」的可见弹窗。典型是阶段⑬ 没关掉的描述编辑器
  // （标题含「产品描述」，按钮是「保存/关闭」）——它的遮罩会吃掉保存点击。
  // 【点「关闭」而不是「保存」】此刻描述改动该不该落库已由 ⑬ 的 desc_save 决定过，
  // 这里再点保存等于替它做决定；而且那个弹窗的「保存」是描述编辑器的保存，
  // 与主表单保存无关，点了只会又弹一层确认。
  const listed = () => Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .filter(m => m.offsetHeight > 0)
    .filter(m => !(m.textContent || '').includes('继续编辑'));
  const acted = [];
  for (let k = 0; k < 3; k++) {
    const ms = listed();
    if (!ms.length) break;
    const m = ms[ms.length - 1];
    const title = ((m.querySelector('.ant-modal-title, .ant-modal-confirm-title') || {})
      .textContent || '').trim().slice(0, 40);
    const btns = Array.from(m.querySelectorAll('button'));
    const close = btns.find(b => (b.textContent || '').trim() === '关闭')
      || m.querySelector('.ant-modal-close');
    if (!close) break;
    close.click();
    acted.push({title: title, clicked: '关闭'});
    await sleep(1200);
    // 「关闭」常带二次确认（怕丢弃改动），确认掉
    const c = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
      .find(x => x.offsetHeight > 0 && /确定|确认|放弃|不保存/.test(x.textContent || ''));
    if (c) {
      const ok = Array.from(c.querySelectorAll('button'))
        .find(b => /^(确定|确认|放弃)$/.test((b.textContent || '').trim()));
      if (ok) { ok.click(); await sleep(1000); }
    }
  }
  return JSON.stringify({acted: acted, remaining: listed().length});
})()"""


async def save(session: BrowserSession, rowid: str = "") -> dict:
    """阶段⑫：点顶部「保存」把前面各阶段的修改落库。不导航（当前页直接点）。

    【只点「保存」】发布是独立的阶段⑮ publish_now，本函数绝不碰「发布」按钮
    （顶栏两个按钮相邻，故定位用文本精确等于'保存'，见本节顶部注释）。

    成功判据（原脚本实测结论，别换）：
      无 .ant-form-item-explain-error + 草稿列表更新时间变化。
      不能用 toast：保存成功的提示店小秘是自定义实现，.ant-message 捕获不到。
    校验失败时页面【完全静默】——不弹提示，只静默滚到出错区块、右侧锚点变红 f-red，
    故失败分支要把红了的区块名逐节读出来报给人（否则只有一句「请选择」没法定位）。

    rowid 给了才做更新时间对比（要新开页签读草稿列表，见 _draft_update_time）；
    不给就只靠「无校验错误 + 确认框出现」判断，够用但证据弱一档。
    """
    before = await _draft_update_time(session, rowid) if rowid else None

    # 【点保存之前先清掉挡路的弹窗】2026-08-28 实测（1014675972015）：阶段⑬ 一张都没
    # 换成时会把描述编辑器留在页面上，它是全屏 modal，遮罩把保存点击整个吃掉——
    # _JS_CLICK_SAVE 仍报 clicked=true（按钮在 DOM 里、click() 也调用了），但请求没发出，
    # 最后只能靠「更新时间未变」这种含糊结论收场。
    # ⑬ 那边的早退路径已补上关闭动作，这里再兜一道：保存是整单成败的关口，
    # 让它对「上游漏关弹窗」这类状态自愈，比再失败一整轮划算。best-effort，不拦主流程。
    pre = await session.eval_json(_JS_CLOSE_FOREIGN_MODAL)
    if pre.get("acted"):
        logger.warning(f"保存前清掉了 {len(pre['acted'])} 个挡路弹窗（上游阶段未关闭）："
                       f"{pre['acted']}；剩余 {pre.get('remaining')}")
        await asyncio.sleep(1.0)

    # toast 时间窗起点：只回捞【本次点击之后】弹出的提示，免得把上游阶段的旧提示
    # 当成本次保存的结论（见 browser.recent_toasts 上方的取证记录）。
    t_click = time.time()
    clicked = await session.eval_json(_JS_CLICK_SAVE)
    if not clicked.get("clicked"):
        return {"status": "error", "stage": "click", **clicked}

    # 轮询 8s 收集反馈：保存请求往返 + 校验错误渲染都要时间，读太早两边都是空的。
    # 原脚本是 5s，这里放宽——Playwright 直连没有 WebBridge 的 HTTP 往返开销垫时间。
    feedback = {"messages": [], "errors": []}
    for _ in range(11):
        feedback = await session.eval_json(_JS_SAVE_FEEDBACK)
        if feedback.get("messages") or feedback.get("errors"):
            break
        await asyncio.sleep(0.7)

    # 无论成败都先把确认框关掉：它的遮罩会挡住后续一切点击（含发布按钮）
    confirm = await session.eval_json(_JS_CLOSE_SAVE_CONFIRM)
    if confirm.get("stillOpen"):
        # 关不掉就按卡住的 modal 处理（ant-fade-leave-active 残留的老毛病）
        killed = await session.kill_stuck_modals()
        confirm["killedStuck"] = killed.get("removed")
    elif not confirm.get("closed") and confirm.get("visibleModals"):
        # 没匹配到「继续编辑」但页面上确实有可见弹窗：多半是文案与 SKILL.md 不一致。
        # 这属于「没关掉但也没报错」的静默态，必须显式告警，否则遮罩会一路挡住后续点击。
        logger.warning(
            f"保存确认框文案可能不是「继续编辑」，未关闭；实际可见弹窗："
            f"{confirm['visibleModals']}"
        )

    errors = feedback.get("errors") or []
    if errors:
        ids = [{"id": k, "name": v} for k, v in navigation.SECTION_IDS.items()]
        red = await session.eval_json(_JS_RED_ANCHORS.replace("__IDS__", J(ids)))
        sections = red.get("redSections") or []
        field_errors = red.get("fieldErrors") or []
        detail = "、".join(s["name"] for s in sections) or "未识别出红色区块"
        if field_errors:
            detail += f"；具体错误：{' | '.join(field_errors[:5])}"
        logger.error(f"保存校验失败（页面无 toast，靠红锚点定位）：{detail}")
        return {"status": "validation-error", "errors": errors,
                "redSections": sections, "fieldErrors": field_errors,
                "messages": feedback.get("messages"),
                "confirmDialog": confirm}

    after = await _draft_update_time(session, rowid) if rowid else None
    # 【保存成功前先查错误 toast】「服装类图片尺寸不能小于1340px*1785px」这类是平台
    # 弹的 toast（.ant-message），不是 .ant-form-item-explain-error 锚点，只靠 errors
    # 会漏判、save 误报成功（2026-09-05 1071736188944：save 落库、发布才报尺寸）。
    # toast 哨兵按 t_click 时间窗回捞，命中「错误/不能」等关键词即判保存失败——
    # 与 publish_now 的拒绝判据同一套词，两个关口口径一致。
    bad_toasts = [t for t in browser.recent_toasts(since=t_click)
                  if any(w in t for w in ("失败", "错误", "不能", "请先", "请选择"))]
    if bad_toasts:
        reason = "保存被平台拒绝：" + "；".join(bad_toasts[:3])
        logger.error(f"保存失败，平台提示：{bad_toasts[:3]}")
        return {"status": "validation-error",
                "reason": reason[:400],
                "platformToasts": bad_toasts,
                "updateTime": {"before": before, "after": after},
                "messages": feedback.get("messages"), "confirmDialog": confirm}

    # 更新时间只在两次都读到、且相等时才判定「没落库」：读不到（None）属证据缺失，
    # 不能当失败——列表分页/筛选变化都可能读不到那一行。
    if before and after and before == after:
        # 【把「有别的弹窗挡着」这条成因单独指认出来】2026-08-28 实测
        # （1014675972015 仿真花）：阶段⑬ 9 张全替换失败后把描述编辑器留在页面上，
        # ⑭ 点保存时真正拦下它的是编辑器自己的弹窗（标题「Temu产品描述批量操作」、
        # 按钮「保存/关闭」），那条「错误：产品信息中有错误，请检查」toast 也是它弹的。
        # 而本函数的 errors 只看 .ant-form-item-explain-error，两者都抓不到，于是
        # 只报出「更新时间未变，保存可能未生效」——查不下去。
        # 现在把当时可见的非本函数弹窗与 toast 一并写进结论：成因在页面上是明确的，
        # 不该让人再去翻日志才发现是被遮罩挡住了。
        blockers = [m for m in (confirm.get("visibleModals") or [])
                    if "继续编辑" not in "".join(m.get("buttons") or [])]
        msgs = [m for m in (feedback.get("messages") or []) if m]
        # 【平台 toast 才是真因所在，优先于「更新时间未变」这个症状】2026-09-01 两单
        # （1067271196776、1051827161006）：页面弹的是「错误：请上传预览图」，而
        # _JS_SAVE_FEEDBACK 读不到店小秘自有的 d-message 浮层，只好报「保存可能未生效」，
        # 把人往「点击没生效」的方向带。toast 哨兵当时已经抓到了这条，接过来放在最前面。
        toasts = browser.recent_toasts(since=t_click, bad_only=True)
        if toasts:
            reason = "保存被平台拒绝：" + "；".join(toasts[:3])
            logger.error(f"保存失败，平台提示：{toasts[:3]}")
        else:
            reason = "无校验错误但草稿更新时间未变，保存可能未生效"
        if blockers:
            reason += (f"；页面上还有 {len(blockers)} 个弹窗挡着（很可能是上一阶段没关掉的"
                       f"编辑器，遮罩会吃掉保存点击）：{blockers[:2]}")
            logger.error(f"保存被遗留弹窗阻挡：{blockers[:2]}")
        if msgs:
            reason += f"；页面提示：{msgs[:3]}"
        return {"status": "validation-error",
                "reason": reason[:400],
                "platformToasts": toasts,
                "blockingModals": blockers,
                "updateTime": {"before": before, "after": after},
                "messages": feedback.get("messages"), "confirmDialog": confirm}

    return {"status": "ok", "rowid": rowid or None,
            "updateTime": {"before": before, "after": after},
            "messages": feedback.get("messages"),
            "confirmDialog": confirm, "published": False}
