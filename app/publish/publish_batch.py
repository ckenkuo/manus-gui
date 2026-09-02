# -*- coding: utf-8 -*-
"""采集箱「批量操作 → 批量发布」：把已填好属性的草稿真实发布上架。

【这是整条数据搬家管线的最后一棒】完整链条是：
    banjia.claim_batch   数据搬家批量认领 → 新草稿落到采集箱
    bulkattr.apply_bulk_attrs  批量填仓库/发货时效/运费模板（平台的发布前硬性必填）
    publish.publish_batch      批量发布 ← 本模块
前两步的产物正是这一步的前提：2026-09-01 实测，属性没填就点批量发布，
「发布检测」直接判 0 个通过、原因写着「半托管仓库不能为空」。

【不可逆，务必看清 confirm 参数】发布会让商品在 Temu 真实上架，只能到在线产品列表
手动下架。故 publish_batch 的 confirm 默认 False：不显式传 True 就只跑到「发布检测」
看结果，绝不点最终发布。这与 pipeline.publish_now(confirm=True) 的取向一致。

【「发布检测」弹窗是必经的一步，不是异常】点「批量发布」后平台先弹它，实测三个按钮：
    下载报告                    导出问题清单（本模块不用）
    返回修改                    放弃发布，回列表
    跳过，发布检测通过的产品      只发通过的那批 ← 本模块走这个
用户 2026-09-01 明确选定「跳过」这条：能发的先发走，不因个别失败拖住整批；
未通过的留在采集箱，把原因原样报回去（那是可操作的信息，如「半托管仓库不能为空」
就是回去补填属性）。
【检测全不通过时不点「跳过」】那个按钮在 0 通过时点了也没有意义（没有可发的产品），
故本模块先读计数，passed == 0 就直接收工返回，连按钮都不碰——少一次无意义的点击，
也避免在「0 通过」这种边界上依赖平台按钮的行为。

【与 bulkattr 共用行勾选，不重写】勾选 rowid、翻页找行、真实点击复选框这套逻辑
（含「vxe 行复选框必须真实鼠标点击」这条实测结论）已经在 bulkattr 里，
这里直接 import 复用：两个模块操作的是同一张列表页的同一批复选框，
各写一份只会让其中一份先腐坏。
"""
import asyncio
import re

from app.logger import logger
from app.publish.browser import DRAFT_LIST_URL, BrowserSession, ensure_cdp_alive
# 复用采集箱列表页的行勾选与菜单原语（同一张页面、同一批 DOM，见模块 docstring）
from app.publish.bulkattr import (BULK_DROPDOWN, _check_rows, _JS_CLOSE,
                                  _JS_TAG_MENU_ITEM)

# 菜单项文案。与 bulkattr 的 MENU_ITEM（"全属性修改"）是同一个菜单里的两项。
MENU_ITEM = "批量发布"

# 发布检测弹窗的识别文案与三个按钮
CHECK_MODAL = "发布检测"
BTN_SKIP = "跳过，发布检测通过的产品"
BTN_BACK = "返回修改"


# 发布检测弹窗：读「N 个产品通过 / M 个产品未通过」与问题清单。
# 【计数从文案里正则抽】弹窗没有可读的结构化字段，实测文案形如
# 「检测完成!0个产品通过的产品检测1个产品未通过产品检测，请先完成修改再进行发布」。
# 抽不到就返回 None 交调用方按「读不到」处理，不冒充 0——冒充 0 会让「跳过」永不执行。
_JS_CHECK_MODAL = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return JSON.stringify({open: false});
  // 问题清单表格：每行「序号/产品问题/产品标题/店铺名称/操作时间」
  const rows = Array.from(m.querySelectorAll('tbody tr'))
    .map(tr => Array.from(tr.querySelectorAll('td')).map(td => t(td).slice(0, 120)))
    .filter(cells => cells.length && cells.join('').trim()
                     && !/暂无数据/.test(cells.join('')));
  return JSON.stringify({open: true, text: t(m).slice(0, 1500),
    issues: rows.slice(0, 50),
    buttons: Array.from(m.querySelectorAll('button'))
      .filter(b => b.offsetHeight > 0).map(b => t(b).slice(0, 30))});
})()""".replace("__TITLE__", CHECK_MODAL)

# 点发布检测弹窗里的按钮（按文案精确匹配）。这些是普通 <button>，合成 click 有效
# （与列表行复选框相反——那个必须真实鼠标点击，见 bulkattr._trusted_toggle）。
_JS_CLICK_CHECK_BTN = r"""((label) => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return {ok: false, reason: 'no-modal'};
  const btn = Array.from(m.querySelectorAll('button'))
    .filter(b => b.offsetHeight > 0).find(b => t(b) === label);
  if (!btn) return {ok: false, reason: 'no-button',
                    avail: Array.from(m.querySelectorAll('button')).map(t)};
  btn.click();
  return {ok: true};
})""".replace("__TITLE__", CHECK_MODAL)

# 发布提交后的结果：可能是另一个结果弹窗，也可能只有 toast。两者都收。
_JS_RESULT = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const modals = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null).map(x => t(x).slice(0, 800));
  const toasts = Array.from(document.querySelectorAll('.ant-message, .d-message'))
    .map(e => t(e)).filter(Boolean);
  return JSON.stringify({modals: modals, toasts: toasts,
    // 发布是异步任务，成功的判据之一是行从采集箱消失（草稿→在线）
    rowsLeft: document.querySelectorAll('.vxe-body--row').length});
})()"""


def _parse_check_counts(text: str) -> dict:
    """从发布检测弹窗文案抽「N 个通过 / M 个未通过」。

    实测文案：「检测完成!0个产品通过的产品检测1个产品未通过产品检测，请先完成修改…」
    两个数字都紧跟在「N个产品」后面，靠后面的「通过」/「未通过」区分。
    【必须先匹配「未通过」】「通过」是「未通过」的子串，先匹配「通过」会把
    「1个产品未通过」也算成通过数。故用两个各自锚定的正则，且未通过那个先跑。
    抽不到返回 None（不冒充 0，见 _JS_CHECK_MODAL 上方注释）。
    """
    out = {"text": text}
    m_fail = re.search(r"(\d+)\s*个产品未通过", text)
    out["failed"] = int(m_fail.group(1)) if m_fail else None
    # 通过数：排掉「未通过」那一处后再找。用负向后行断言挡住「未」
    m_pass = re.search(r"(?<!未)(\d+)\s*个产品通过", text)
    if not m_pass:
        # 文案变体：「0个产品通过的产品检测」——上面已能覆盖；这里兜「通过N个」语序
        m_pass = re.search(r"通过\D{0,6}(\d+)\s*个", text)
    out["passed"] = int(m_pass.group(1)) if m_pass else None
    return out


# 核实某批 rowid 是否已离开草稿列表并进了在线产品（发布成功的事实判据）。
# 【为什么需要它：没有「发布检测」弹窗也可能是成功】2026-09-01 真站实测，
# 必填项齐全的草稿点「批量发布」后**平台不弹检测弹窗、直接发布**（那条草稿最终
# dxmState=online、offlineState=publishSuccess、拿到平台商品 ID 6938205443）。
# 原实现把「没等到检测弹窗」当错误报 503，于是**发布成功却报失败**——这类误报比漏报
# 更坏：它会诱使人重跑，而发布不可逆。
# 故改成「等不到弹窗就去核实事实」：查这批 rowid 在 online 状态里在不在。
_JS_VERIFY = r"""(async (args) => {
  const [rowids, states] = args;
  const want = new Set(rowids);
  const out = {};
  for (const st of states) {
    const hits = [];
    for (let p = 1; p <= 4; p++) {
      const body = 'sortName=2&pageNo=' + p + '&pageSize=50'
        + '&total=0&searchType=0&searchValue=&productSearchType=1&shopId=-1'
        + '&dxmState=' + st + '&site=0&fullCid=&sortValue=2&productType=';
      const r = await fetch('/api/popTemuProduct/pageList.json', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
        body: body});
      if (!r.ok) break;
      const d = await r.json();
      if (d.code !== 0) break;
      const page = (d.data || {}).page || {};
      const list = page.list || [];
      list.forEach(it => {
        const id = it.idStr || String(it.id || '');
        if (want.has(id)) hits.push({rowid: id, state: it.dxmState || '',
          offlineState: it.dxmOfflineState || '',
          platformProductId: String(it.platformProductId || ''),
          errMsg: (it.errMsg || '').slice(0, 200)});
      });
      if (p >= (page.totalPage || 1) || !list.length) break;
    }
    out[st] = hits;
  }
  return JSON.stringify(out);
})"""


async def _verify_published(session: BrowserSession, rowids: list,
                            attempts: int = 3, wait: float = 5.0) -> dict:
    """核实这批 rowid 是否真的发布了：查它们现在在哪个列表。

    判据取自列表接口的事实，而不是弹窗文案：
      在 online 里且 offlineState=publishSuccess → 已上架（拿到 platformProductId 更确凿）
      还在 draft 里                              → 没发出去
    发布是异步的，故轮询几轮。

    返回 {"online": [...], "draft": [...], "publishedRowids": [...], "stillDraft": [...]}。
    """
    ids = [str(r) for r in rowids]
    res: dict = {}
    for attempt in range(1, attempts + 1):
        res = await session.eval_json(_JS_VERIFY, arg=[ids, ["online", "draft"]])
        online = res.get("online") or []
        if len(online) >= len(ids):
            break
        if attempt < attempts:
            logger.info(f"发布是异步的，已上架 {len(online)}/{len(ids)}，"
                        f"{wait}s 后再核实（{attempt}/{attempts}）")
            await asyncio.sleep(wait)
    online = res.get("online") or []
    draft = res.get("draft") or []
    ok_ids = [h["rowid"] for h in online
              if h.get("offlineState") == "publishSuccess" or h.get("platformProductId")]
    return {"online": online, "draft": draft, "publishedRowids": ok_ids,
            "stillDraft": [h["rowid"] for h in draft]}


async def publish_batch(rowids: list, confirm: bool = False,
                        session: BrowserSession = None,
                        on_log=None) -> dict:
    """批量发布采集箱里的草稿（管线最后一棒）。

    confirm=False（默认）只跑到「发布检测」读结果就关掉弹窗，**不发布任何东西**；
    confirm=True 才点「跳过，发布检测通过的产品」真实上架。

    on_log 是可选的进度回调 on_log(text)，用于把过程推到 UI 的「本批进度」日志；
    best-effort 调用，回调抛异常不影响发布（辅助路径不许拖垮主流程）。

    返回 {"status", "requested", "checked", "missing", "confirm",
          "passed", "failed", "issues": [...], "published", "result", "note"}。
    - passed/failed 来自发布检测弹窗的计数；**平台可能不弹该弹窗直接发布**，
      此时 passed/failed 为 None，成功与否由 _verify_published 的事实核实决定
    - issues 是未通过清单（每条含问题原因与标题），原样交给 UI 显示
    - published 为 True 表示商品真的上架了（经核实，不只是「点过按钮」）

    【不可逆】published=True 之后商品在 Temu 真实上架，只能手动下架。
    """
    def _log(text: str) -> None:
        """把一行过程日志推给调用方（best-effort）。"""
        logger.info(text)
        if on_log:
            try:
                on_log(text)
            except Exception as e:
                logger.warning(f"进度回调失败（忽略）：{e}")

    rowids = [str(r) for r in (rowids or []) if str(r).strip()]
    if not rowids:
        raise ValueError("没有传入要发布的草稿 rowid")

    own = session is None
    if own:
        if not await ensure_cdp_alive():
            raise RuntimeError("CDP 不可用（调试 Chrome 未启动或未开 9222 端口）")
        session = BrowserSession()
    try:
        if own:
            await session.open()
        # 与两个定时扫描抢同一个页签，故全程持锁（理由同 bulkattr：
        # 中途被某轮扫描 navigate 走会让勾选状态全丢，还可能发布错的行）
        from app.publish.browser import PAGE_LOCK

        async with PAGE_LOCK:
            _log(f"打开采集箱列表页，准备勾选 {len(rowids)} 条")
            r = await session.navigate(DRAFT_LIST_URL)
            if not r.get("ok"):
                raise RuntimeError(f"导航到采集箱列表页失败：{r}")
            await asyncio.sleep(3)
            await session.eval_json(_JS_CLOSE)

            picked = await _check_rows(session, rowids)
            if not picked["checked"]:
                # 【可能是已经发布过了】草稿发布后就离开采集箱，故「找不到」既可能是
                # 没这条、也可能是它已经上架。先核实再报错，别让人以为白跑了。
                _log("这批草稿不在采集箱里，核实是否已经上架…")
                v = await _verify_published(session, rowids, attempts=1)
                if v["publishedRowids"]:
                    _log(f"核实到 {len(v['publishedRowids'])} 条已在「在线产品」，"
                         f"无需重复发布")
                    return {"status": "ok", "requested": len(rowids),
                            "checked": 0, "missing": picked["missing"],
                            "confirm": bool(confirm), "passed": None, "failed": None,
                            "issues": [], "published": True,
                            "verified": v, "alreadyPublished": True,
                            "note": f"这 {len(v['publishedRowids'])} 条已经上架了"
                                    f"（不在采集箱是因为发布后就离开草稿列表），未重复操作"}
                raise RuntimeError(
                    f"要发布的 {len(rowids)} 条草稿在采集箱列表里一条都没找到，"
                    f"且不在「在线产品」里（可能已被删除）")
            if picked["missing"]:
                logger.warning(
                    f"有 {len(picked['missing'])} 条草稿没在列表里找到，"
                    f"本次只发布勾中的 {len(picked['checked'])} 条")
            _log(f"已勾选 {len(picked['checked'])} 条")

            # 点「批量操作 → 批量发布」
            try:
                await session.page.locator(
                    'button:has-text("批量操作")').first.click(timeout=15000)
            except Exception as e:
                raise RuntimeError(f"点击「批量操作」失败：{e}")
            await asyncio.sleep(2)
            tag = await session.eval_json(_JS_TAG_MENU_ITEM, arg=MENU_ITEM)
            if not tag.get("found"):
                raise RuntimeError(
                    f"批量操作菜单里没有「{MENU_ITEM}」，"
                    f"可选：{(tag.get('avail') or [])[:20]}")
            if not tag.get("visible"):
                raise RuntimeError("批量操作菜单未展开（菜单项不可见）")
            rr = await session.mouse_click('[data-ba-item="1"]')
            if not rr.get("ok"):
                raise RuntimeError(f"点击「{MENU_ITEM}」失败：{rr.get('err')}")
            _log(f"已点「{MENU_ITEM}」，等平台的发布检测")

            # 平台先跑发布检测（要逐条校验必填项，慢的时候要几十秒）。
            # 【等不到不算错误】必填项齐全时平台直接发布、不弹这个弹窗（2026-09-01 实测），
            # 那也是成功路径。故超时后走事实核实，而不是抛异常。
            chk = await session.wait_for(_JS_CHECK_MODAL, lambda d: d.get("open"),
                                        timeout=60, interval=2)
            base = {"status": "ok", "requested": len(rowids),
                    "checked": len(picked["checked"]), "missing": picked["missing"],
                    "confirm": bool(confirm)}

            if not chk.get("open"):
                _log("平台没弹发布检测（必填项齐全时会直接发布），核实实际结果…")
                v = await _verify_published(session, picked["checked"])
                done = v["publishedRowids"]
                if done:
                    ids = "、".join(h["platformProductId"] for h in v["online"]
                                   if h.get("platformProductId"))
                    _log(f"核实到 {len(done)} 条已上架"
                         + (f"（平台商品ID {ids}）" if ids else ""))
                    return {**base, "passed": len(done), "failed": None, "issues": [],
                            "published": True, "verified": v,
                            "noCheckModal": True,
                            "note": f"已上架 {len(done)} 条"
                                    + (f"（平台商品ID {ids}）" if ids else "")
                                    + "。平台未弹发布检测，说明必填项齐全、直接发布"}
                # 既没弹窗也没上架：这才是真的不对
                await session.eval_json(_JS_CLOSE)
                raise RuntimeError(
                    "点了「批量发布」但既没出现「发布检测」弹窗、"
                    f"这 {len(picked['checked'])} 条也没进「在线产品」"
                    f"（仍在采集箱 {len(v['stillDraft'])} 条）。"
                    "请到页面上确认当前状态")

            counts = _parse_check_counts(chk.get("text") or "")
            issues = chk.get("issues") or []
            passed, failed = counts.get("passed"), counts.get("failed")
            base.update({"passed": passed, "failed": failed, "issues": issues,
                         "checkText": (chk.get("text") or "")[:500]})
            _log(f"发布检测 通过 {passed} / 未通过 {failed}"
                 + (f"；首条未通过原因：{issues[0][1] if len(issues[0]) > 1 else issues[0]}"
                    if issues else ""))

            # 一条都没通过：不点「跳过」（没有可发的产品），关掉弹窗直接回报
            if passed == 0:
                await session.eval_json(_JS_CLOSE)
                _log("0 条通过检测，未发布任何商品")
                return {**base, "published": False,
                        "note": "发布检测一条都没通过，未发布任何商品；"
                                "按未通过原因补齐后重试（多为属性没填全）"}
            if passed is None:
                # 读不到计数：宁可不发也不瞎发（不可逆动作，不容许在判据不明时进行）
                await session.eval_json(_JS_CLOSE)
                raise RuntimeError(
                    f"读不出发布检测的通过条数（弹窗文案可能改版），未执行发布。"
                    f"原文：{(chk.get('text') or '')[:200]}")

            if not confirm:
                # 只看检测结果，不发布（默认路径）
                await session.eval_json(_JS_CLOSE)
                _log(f"仅检测（未发布）—— {passed} 条可发布")
                return {**base, "published": False,
                        "note": f"仅检测：{passed} 条可发布"
                                + (f"，{failed} 条未通过" if failed else "")
                                + "。确认后才会真实上架"}

            # 真实发布：点「跳过，发布检测通过的产品」
            btn = await session.eval_json(_JS_CLICK_CHECK_BTN, arg=BTN_SKIP)
            if not btn.get("ok"):
                raise RuntimeError(
                    f"点击「{BTN_SKIP}」失败：{btn.get('reason')}，"
                    f"弹窗按钮：{(btn.get('avail') or [])[:6]}")
            _log(f"已点「{BTN_SKIP}」，正在提交 {passed} 条")
            await asyncio.sleep(5)
            res = await session.wait_for(
                _JS_RESULT,
                lambda d: (d.get("toasts") or d.get("modals")),
                timeout=90, interval=3)
            # 关掉可能残留的结果弹窗（best-effort，遮罩留着会挡住后续操作）
            try:
                await session.eval_json(_JS_CLOSE)
            except Exception as e:
                logger.warning(f"关闭发布结果弹窗失败（忽略）：{e}")

            # 【按事实核实，不只信「点过按钮」】发布是异步的，点完不等于成了。
            # 判据取列表接口：进了 online 且 publishSuccess（或拿到平台商品 ID）才算。
            _log("核实上架结果…")
            v = await _verify_published(session, picked["checked"])
            done = v["publishedRowids"]
            ids = "、".join(h["platformProductId"] for h in v["online"]
                           if h.get("platformProductId"))
            _log(f"核实到 {len(done)} 条已上架"
                 + (f"（平台商品ID {ids}）" if ids else "")
                 + (f"；{len(v['stillDraft'])} 条仍在采集箱" if v["stillDraft"] else ""))
            return {**base, "published": bool(done), "verified": v,
                    "result": {"toasts": res.get("toasts") or [],
                               "modals": res.get("modals") or [],
                               "rowsLeft": res.get("rowsLeft")},
                    "note": (f"已上架 {len(done)} 条"
                             + (f"（平台商品ID {ids}）" if ids else "")
                             if done else "点了发布但核实时还没上架（发布是异步的，"
                                          "稍后到「在线产品」列表再看）")
                            + (f"；{failed} 条未通过检测仍留在采集箱" if failed else "")}
    finally:
        if own:
            await session.close()
