"""店小秘发布操作：media.space。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish.browser import BrowserSession, J


# 空间图片弹窗的标题（2026-08-20 实测原文）
SPACE_MODAL_TITLE = "从图片空间选择"


# 在空间图片弹窗里按 fileId 片段选中目标图并点确定。
# 为什么整段放在一个 evaluate 里：弹窗和菜单都会因失焦自动收起，分成多次往返时
# 中间那一步可能落在已消失的 DOM 上（原脚本记录的坑，这条在 Playwright 下仍然成立）。
# 选中判据用 fileId 的【文件名部分】而不是完整 URL：弹窗里的 src 是缩略图地址，
# 与直传返回的 URL 前缀不同，但文件名一致。
_JS_PICK_FROM_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  if (!modal) return JSON.stringify({stage: 'modal', err: '空间图片弹窗没打开'});

  // 每张图在弹窗里有缩略图和预览两个 img 元素，故用 .img-item 单元去重
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const hit = items.find(it => Array.from(it.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  if (!hit) {
    // 【报清弹窗现状，别只说「找不到」】2026-09-01 排查 890185900190（4 行同一张
    // 200x200 源图、逐行各传一次）时，本分支只回 3 个 src 尾段，判断不了到底是
    // 「图床按内容去重导致新 fileId 根本不存在」还是「翻页了没找到」。
    // 故把当前页全部文件名与已选中项一并回报：前者能直接看出目标名在不在，
    // 后者能看出是不是上一行选中的那张还留着选中态（累积多选语义下会）。
    const names = items.map(it => {
      const i = it.querySelector('img');
      return i ? ((i.src || '').split('?')[0].split('/').pop() || '') : '';});
    const chosen = items.map((it, k) => {
      const c = it.querySelector('.img-check');
      return (c && /取消选[择中]/.test(c.textContent || '')) ? k : -1;})
      .filter(k => k >= 0);
    return JSON.stringify({stage: 'pick', err: '弹窗里找不到刚上传的图',
      wantName: __FID__, itemCount: items.length,
      pageNames: names.slice(0, 24), alreadySelected: chosen,
      firstSrcs: items.slice(0, 3).map(it => {
        const i = it.querySelector('img'); return i ? (i.src || '').slice(-40) : null;})});
  }
  hit.click();
  // 【选中态有明确信号，不必靠固定等待】2026-08-26 真站探查：item 的 class 完全不变，
  // 变的是 .img-check 的文本（「点击选择」↔「取消选中」）与弹窗顶部的「已选择N张图片」
  // 计数。故轮询等这个文本翻转，比硬等 900ms 既快又真的验证了「点中了」——原实现
  // 等完根本没校验选中态，点空了也照样去点确定（表现为确定后行内图数不变）。
  let selected = false;
  for (let i = 0; i < 30; i++) {
    const chk = hit.querySelector('.img-check');
    if (chk && /取消选[择中]/.test(chk.textContent || '')) { selected = true; break; }
    await sleep(100);
  }
  const cls = String(hit.className || '');
  if (!selected) {
    return JSON.stringify({stage: 'pick', err: '点了图但选中态没生效（3s 内未翻转）',
                           itemClass: cls});
  }
  const ok = Array.from(modal.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === '确定');
  if (!ok) return JSON.stringify({stage: 'confirm', err: '找不到确定按钮', itemClass: cls});
  ok.click();
  // 弹窗关闭即完成，轮询等它消失（原硬等 1800ms）
  let stillOpen = true;
  for (let i = 0; i < 100; i++) {
    stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
      .some(m => m.offsetHeight > 0 &&
        ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
    if (!stillOpen) break;
    await sleep(100);
  }
  return JSON.stringify({stage: 'ok', picked: true, itemClass: cls, stillOpen});
})()"""


# 在【一次】空间弹窗里按 fileId 勾选多张图并点确定。
#
# 【弹窗支持累积多选——2026-08-26 真站探查证实】_probe_skc_space_modal.py 连点前 3 张，
# 弹窗顶部计数逐次变成「已选择1/2/3张图片」，且每个 .img-item 的角标从「点击选择」变成
# 「取消选中」后【保持不变】（点第 2 张时第 1 张仍是「取消选中」）。故这是累积语义，
# 一次弹窗可以选完一整批，不必每张开关一次弹窗。
#
# 【为什么值得】逐张挂图时每张都要走「瞄点 → CDP 点行按钮 → 开菜单 → 开弹窗 → 选 1 张
# → 确定 → 关弹窗」整轮，2026-08-26 实测 29 张图花 402s。改成批量后一批只走一轮。
#
# 【选中态判据是 .img-check 的文本，不是 class】探查确认 item 的 className 完全不变
# （恒为 "img-item"），变的是内层 .img-check 的文本。别改回按 class 判——那会恒判未选中。
#
# 【空间里图片多时要翻页/搜索】弹窗带 vxe-pager 分页与搜索框（探查实测每页 20 张）。
# 本函数只在【当前页】找：调用方刚上传的图必然在第一页（按上传时间倒序），
# 找不到就如实报错并回传当前页的 src 尾巴，不静默跳过。
# 在空间弹窗里点【一张】图（按 fileId 定位），并回报点击前后的选中态。
#
# 【为什么点一张就返回，不在 JS 里连点多张】见 _pick_many_from_space 的 docstring：
# 连点版在真站上四次都出现「诊断说已选中、轮询判据恒 false」的自相矛盾。每张一次独立
# 往返后，每次 eval 都在全新上下文里重新 querySelector，与探查脚本里可用的路径一致。
#
# 选中态判据是 .img-check 的文本（「点击选择」↔【「取消选中」】）。两处要点：
# 1. 【文案是「取消选中」不是「取消选择」】2026-08-26 差这一个字，正则永不匹配，
#    表现为每张都报「点了但没翻转」、整行换不了图，而页面上其实已经选上了。
#    排查绕了五次真站验证，因为终端 GBK 把两词的乱码显示得一模一样——读诊断输出
#    必须 PYTHONIOENCODING=utf-8，否则中文对比毫无意义。故正则写成兼容两种写法。
# 2. 别改成按 class 判：
# 2026-08-26 真站探查确认 .img-item 的 className 恒定不变，按 class 会恒判未选中。
_JS_SPACE_CLICK_ONE = r"""(() => {
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  if (!modal) return JSON.stringify({err: '空间图片弹窗没打开'});
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const chk = it => {
    const c = it.querySelector('.img-check');
    return c ? (c.textContent || '').trim() : '';
  };
  // 【取全部匹配项】同一内容的图在空间里可能有多份记录（重传、历史跑批留下的），
  // 只看第一个会出现「点了 A 去读 B」。选中判据是其中任一为选中。
  const hits = items.filter(it => Array.from(it.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  if (!hits.length) {
    return JSON.stringify({err: '弹窗里找不到这张图', itemCount: items.length,
      firstSrcs: items.slice(0, 3).map(it => {
        const i = it.querySelector('img'); return i ? (i.src || '').slice(-40) : null;})});
  }
  const before = hits.map(chk);
  if (before.some(t => /取消选[择中]/.test(t))) {
    return JSON.stringify({already: true, before: before});
  }
  hits[0].click();          // 点第一个未选中的
  return JSON.stringify({clicked: true, before: before});
})()"""


# 只读回报某张图当前的选中态（点击后另起一次 eval 来确认，不与点击共用上下文）。
_JS_SPACE_CHECK_ONE = r"""(() => {
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  if (!modal) return JSON.stringify({err: '空间图片弹窗没打开'});
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const hits = items.filter(it => Array.from(it.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  const checks = hits.map(it => {
    const c = it.querySelector('.img-check');
    return c ? (c.textContent || '').trim() : '';
  });
  // 计数元素不一定存在（不同弹窗实例不同），拿不到就回 null，由调用方跳过该校验
  const t = Array.from(modal.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0)
    .map(el => (el.textContent || '').trim())
    .find(x => /已选择\s*\d+\s*张图片/.test(x));
  return JSON.stringify({matched: hits.length, checks: checks,
    selected: checks.some(c => /取消选[择中]/.test(c)),
    counted: t ? parseInt(t.match(/\d+/)[0], 10) : null});
})()"""


# 点弹窗的「确定」，并轮询等它关闭。
_JS_SPACE_CONFIRM = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const getModal = () => Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  const m = getModal();
  if (!m) return JSON.stringify({err: '空间图片弹窗没打开'});
  const btn = Array.from(m.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === '确定');
  if (!btn) return JSON.stringify({err: '找不到确定按钮'});
  btn.click();
  let stillOpen = true;
  for (let i = 0; i < 100; i++) {
    await sleep(100);
    stillOpen = !!getModal();
    if (!stillOpen) break;
  }
  return JSON.stringify({confirmed: true, stillOpen: stillOpen});
})()"""


async def _pick_many_from_space(session: BrowserSession, file_ids: list) -> dict:
    """在已打开的空间弹窗里勾选多张图并确定（每张一次独立往返，见上方 JS 的注释）。

    file_ids 传直传返回的 fileId 列表，内部只取文件名部分匹配（弹窗里是缩略图地址，
    前缀与直传返回的 URL 不同，同 _pick_from_space）。

    返回 {"stage": "ok", "picked": [...], "counted": N} 或带 err 的失败结构。
    失败时【不点确定】，弹窗留在打开状态交调用方决定（关掉重试还是报人工）——
    半选状态点确定会挂上数量不对的图，比直接失败糟。

    【每张分成「点一次 + 另起一次 eval 确认」两个往返】而不是在一段 JS 里连点：
    连点版在真站上四次都出现「诊断说已选中、判据恒 false」的自相矛盾（详见
    _JS_SPACE_CLICK_ONE 上方那段）。分开后每次 eval 都是全新上下文，与探查脚本里
    实测可用的路径一致。
    """
    fids = [f.rsplit("/", 1)[-1] for f in file_ids]
    picked, missing, diag = [], [], []

    for fid in fids:
        r = await session.eval_json(
            _JS_SPACE_CLICK_ONE.replace("__TITLE__", J(SPACE_MODAL_TITLE))
            .replace("__FID__", J(fid)))
        if r.get("err"):
            # 弹窗不在是致命的（后面每张都会一样），立刻收工而不是逐张重试
            return {"stage": "pick", "err": r["err"], "picked": picked,
                    "itemCount": r.get("itemCount"), "firstSrcs": r.get("firstSrcs")}
        if r.get("already"):
            picked.append(fid)
            continue

        # 点完另起一次 eval 确认选中态，最多等 3s
        ok = False
        last: dict = {}
        for _ in range(15):
            await asyncio.sleep(0.2)
            last = await session.eval_json(
                _JS_SPACE_CHECK_ONE.replace("__TITLE__", J(SPACE_MODAL_TITLE))
                .replace("__FID__", J(fid)))
            if last.get("selected"):
                ok = True
                break
        if not ok:
            diag.append({"fid": fid, "matched": last.get("matched"),
                         "checks": last.get("checks")})
            missing.append(fid)
            continue
        picked.append(fid)

    if missing:
        return {"stage": "pick", "err": "有图没能选中", "missing": missing,
                "picked": picked, "diag": diag}

    # 计数回读：「已选择N张图片」是平台自己维护的，比逐项判更权威。数字对不上就别点
    # 确定——那意味着有点击没被组件收到，确定后行内图数会与预期不符。
    # 计数元素不一定存在（不同弹窗实例不同），拿不到就跳过这道校验而不是报错。
    st = await session.eval_json(
        _JS_SPACE_CHECK_ONE.replace("__TITLE__", J(SPACE_MODAL_TITLE))
        .replace("__FID__", J(fids[0])))
    cnt = st.get("counted")
    if cnt is not None and cnt != len(fids):
        return {"stage": "count", "err": "已选计数与预期不符",
                "expected": len(fids), "counted": cnt}

    cf = await session.eval_json(
        _JS_SPACE_CONFIRM.replace("__TITLE__", J(SPACE_MODAL_TITLE)))
    if cf.get("err"):
        return {"stage": "confirm", "err": cf["err"], "counted": cnt}
    return {"stage": "ok", "picked": picked, "counted": cnt,
            "stillOpen": cf.get("stillOpen")}


async def _pick_from_space(session: BrowserSession, file_id: str) -> dict:
    """在已打开的空间图片弹窗里选中指定图片并点确定。

    file_id 传直传返回的 fileId（形如 /wxalbum/2525332/2026.../abc.jpg），
    内部只取文件名部分做匹配——弹窗里是缩略图地址，前缀与直传返回的 URL 不同。
    """
    fid = file_id.rsplit("/", 1)[-1]
    js = (_JS_PICK_FROM_SPACE
          .replace("__TITLE__", J(SPACE_MODAL_TITLE))
          .replace("__FID__", J(fid)))
    return await session.eval_json(js)


async def _close_space_modal(session: BrowserSession) -> dict:
    """关掉空间图片弹窗（点「取消」而非「确定」）。

    best-effort：关不掉只记警告不抛。但必须尽力关——弹窗遮罩会挡住后续一切点击，
    留着它会让下一个阶段莫名其妙地全部失败。
    """
    try:
        r = await session.eval_json(r"""(async () => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const modal = Array.from(document.querySelectorAll('.ant-modal'))
            .find(m => m.offsetHeight > 0 &&
              ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
          if (!modal) return JSON.stringify({wasOpen: false});
          const btn = Array.from(modal.querySelectorAll('button'))
            .find(b => (b.textContent || '').trim() === '取消')
            || modal.querySelector('.ant-modal-close');
          if (btn) { btn.click(); await sleep(1000); }
          return JSON.stringify({wasOpen: true, clicked: !!btn,
            stillOpen: Array.from(document.querySelectorAll('.ant-modal'))
              .some(m => m.offsetHeight > 0 &&
                ((m.querySelector('.ant-modal-title')||{}).textContent||'').includes(__TITLE__))});
        })()""".replace("__TITLE__", J(SPACE_MODAL_TITLE)))
        if r.get("stillOpen"):
            killed = await session.kill_stuck_modals()
            r["killedStuck"] = killed.get("removed")
        return r
    except Exception as e:
        logger.warning(f"关空间图片弹窗失败（不影响主流程判定）：{e}")
        return {"err": str(e)}
