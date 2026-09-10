"""店小秘发布操作：media.description_replace_scripts。模块导航见 docs/publish-pipeline-refactor.md。"""


# 点「空间上传」→ 空间弹窗选图 → 确定。
# 空间弹窗的识别要【排除描述编辑器自身】：编辑器也是 .ant-modal 且文本里可能含
# 「图片空间」字样，不排除会命中自己然后在里面找不到 .img-item。
_JS_DESC_PICK_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(x => {
    if (/display:\s*none/.test(x.getAttribute('style') || '')) return false;
    const t = Array.from(x.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return __ITEMS__.every(w => t.includes(w));
  });
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()));
    return JSON.stringify({stage: 'menu', err: '描述专属菜单未展开', visibleMenus: seen});
  }
  const it = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间上传');
  if (!it) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间上传」项'});
  it.click();
  await sleep(3000);

  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 && !m.querySelector('.smt-desc-content')
      && (m.textContent || '').includes('图片空间'));
  if (!modal) {
    return JSON.stringify({stage: 'modal', err: '空间弹窗没打开',
      openTitles: Array.from(document.querySelectorAll('.ant-modal'))
        .filter(m => m.offsetHeight > 0)
        .map(m => ((m.querySelector('.ant-modal-title') || {}).textContent || '').trim())});
  }
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const hit = items.find(x => Array.from(x.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  if (!hit) return JSON.stringify({stage: 'pick', err: '弹窗里找不到刚上传的图',
    itemCount: items.length});
  hit.click();
  await sleep(900);
  const ok = Array.from(modal.querySelectorAll('button'))
    .find(b => (b.textContent || '').replace(/\s/g, '') === '确定');
  if (!ok) return JSON.stringify({stage: 'confirm', err: '找不到确定按钮'});
  ok.click();
  await sleep(2000);
  return JSON.stringify({stage: 'ok'});
})()"""


# 滚动到第 pos 个模块图（pos 从 1 起）。与读坐标分成两次 evaluate，理由同 SKC：
# 平滑滚动未停就读坐标会点偏。
# 【block 由调用方给】模块图滚到哪个位置，决定了右侧面板「更换图片」链接落在视口的
# 什么高度——而那条链接可能被 fixed 顶栏压住（见 _desc_aim_replace_link）。原先写死
# 'center'，链接被压住时无路可退。
_JS_DESC_BOX_SCROLL = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const boxes = m.querySelectorAll('.smt-desc-content .desc-img-box');
  const b = boxes[__IDX__];
  if (!b) return JSON.stringify({err: '没有第 ' + (__IDX__ + 1) + ' 个模块'});
  b.scrollIntoView({block: __BLOCK__});
  return JSON.stringify({ok: true, total: boxes.length});
})()"""


# 读第 pos 个模块图的中心坐标。
# 刻意【不做 elementFromPoint 校验】：编辑器是全屏 modal，任何坐标都会命中编辑器内的
# IMG，校验必然「失败」——那不是遮挡（见本节开头说明）。
_JS_DESC_BOX_POS = r"""(() => {
  const m = __MODAL__;
  const b = m.querySelectorAll('.smt-desc-content .desc-img-box')[__IDX__];
  if (!b) return JSON.stringify({err: '模块消失了'});
  const r = b.getBoundingClientRect();
  const img = b.querySelector('img');
  return JSON.stringify({x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
    srcBefore: img ? (img.currentSrc || img.src || '') : null});
})()"""


# 读右侧面板「更换图片」链接的坐标（点模块图后才出现）。
# 【这个坐标要校验 elementFromPoint，与模块图相反】模块图那边不校验是因为编辑器是
# 全屏 modal、任何坐标都会命中编辑器内的 IMG（见本节开头说明）；而这里要点的是一个
# 具体的小链接，点偏了就什么都不会发生——正是「菜单未展开」那条报错的一种成因。
# 校验不通过时把实际命中的元素报出来，好分清「被浮层盖住」还是「链接被滚出视口」。
#
# 【「在视口内」不等于「点得到」——2026-08-28 实测的整段失效】原判据只看
# `r.top < 0 || r.bottom > innerHeight`，链接停在 y≈61 时几何上完全在视口内、闸门放行,
# 但页面顶栏 .top-header 是 position:fixed、高 70，正压在它上面。于是瞄点命中的是
# 顶栏那个 DIV（hitAt=top-header title h70 ...），CDP 点击全打在顶栏上，
# rc-trigger 收不到 mousedown、菜单当然不展开。表现就是「轮询 3.2s + 补点一次」
# 两轮全空转、visibleMenus 恒为 []。日志证据：logs/20260828102412.log 里 6 张失败
# 前每一张都先打了 onLink 为假那条 info，两次点击点的是同一个错坐标。
#
# 【为什么必须在这段 JS 里自己修正，而不是靠调用方收浮层】调用方原本唯一的处置是
# _park_image_menus + 重读坐标，那治的是 hitAt=ant-dropdown 那类残留菜单遮挡
# （统计历史日志：ant-dropdown 遮挡 23 次几乎都被「补点一次」救回，而 top-header /
# image-box 遮挡 13+18 次一次都没救回）。顶栏是页面框架、预览大图是编辑器自身内容,
# 两者都不是「浮层」，收不掉；重读坐标拿到的还是同一个错值，所以补点必然同样落空。
#
# 【修正手段是滚 .ant-modal-body，不是 scrollIntoView(link)】右侧面板靠 transform
# 跟随 .ant-modal-body 的滚动（2026-08-27 probe_desc_panel_pos.py 实测：pos>=2 时
# 面板 top=-50，对链接调 scrollIntoView 滚完仍停在 y≈61）。而【不能对模块图再滚】：
# 模块必须保持选中态，右侧面板才在，滚走会让面板连带消失。故这里减容器 scrollTop
# 把链接往下推——推的量按「要越过的遮挡带下沿」算，一轮不够就再来一轮。
#
# 【求瞄点要多点采样，别只试中心】链接只有约 20px 高，被顶栏压住时中心不可点而下半
# 可能已经露出来了；同理左右两侧有时避得开预览图的圆角。采样顺序由密到疏，每个候选点
# 都过同一道 elementFromPoint 校验才会被采用——安全性不打折（同 _JS_SKC_BTN_POS 的取向）。
_JS_DESC_REPLACE_LINK = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const a = Array.from(m.querySelectorAll('.smt-content-right a'))
    .find(x => (x.textContent || '').trim() === '更换图片');
  if (!a) return JSON.stringify({err: '右侧面板没有「更换图片」链接（模块图可能没点中）'});

  const onLink = el => !!el && (el === a || a.contains(el) || el.contains(a));
  // 在链接矩形内按「由密到疏」取候选点，返回第一个 elementFromPoint 命中链接的
  const probe = () => {
    const r = a.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return {rect: r, hit: null};
    const cand = [];
    // fy 先中心再下半（顶栏压住上半时下半往往已露出），fx 先中心再左右退让
    for (const fy of [0.5, 0.72, 0.88, 0.28]) {
      for (const fx of [0.5, 0.25, 0.75]) {
        cand.push([Math.round(r.x + r.width * fx), Math.round(r.y + r.height * fy)]);
      }
    }
    for (const [x, y] of cand) {
      if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
      const el = document.elementFromPoint(x, y);
      if (onLink(el)) return {rect: r, hit: el, x, y};
    }
    const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
    return {rect: r, hit: document.elementFromPoint(cx, cy), x: cx, y: cy, missed: true};
  };

  // 压在链接上的 fixed/sticky 遮挡带下沿（顶栏就是这一类）。只看盖住链接横向范围、
  // 且位于链接上方的那些——它们的 bottom 就是链接必须让到的位置。
  //
  // 【必须排除 .ant-modal-mask ——2026-08-28 真站取证，这是描述图整段失效的真因】
  // 描述编辑器自己的遮罩 .ant-modal-mask 是 position:fixed、z-index:1000、
  // 尺寸 2560×1313 铺满整个视口（top=0 bottom=1313=innerHeight）。它被这个函数
  // 当成「压在链接上方的遮挡带」，于是 blockerBottom 恒等于视口高度：
  //   链接本来在 top=227 位置完全正常，needDown = 1313-227+8 = 1094，
  //   循环就把它一路推到 y≈1321（视口外）→ elementFromPoint 返回 null；
  //   推回来又落在 1280 被预览图盖住 → 两个状态间来回振荡 4 轮耗尽。
  // 也就是说：原先「链接被顶栏压住」的判断在这个页面上从头到尾是【自造的问题】,
  // 是这个错误的 need 把一个本来可点的链接推出了视口。offer 1014675972015 的
  // 9 张描述图两跑全挂在这里（logs/20260828170738.log、20260828180909.log）。
  //
  // 遮罩在链接【下方】（z 更低时点击穿透）还是上方无从用几何判断，但它是本编辑器
  // 自己的背景板、绝不该被当成需要躲开的东西——真正挡住链接的是 IMG.image-box
  // （预览大图），那是编辑器内容、不是 fixed，本函数统计不到它，滚动也躲不开
  // （它跟右侧面板一起动），只能靠调用方换模块图落点重瞄。
  const blockerBottom = () => {
    const r = a.getBoundingClientRect();
    const x = r.x + r.width / 2;
    let bottom = 0;
    for (const e of document.querySelectorAll('*')) {
      if (a === e || a.contains(e) || e.contains(a)) continue;
      const s = getComputedStyle(e);
      if (s.position !== 'fixed' && s.position !== 'sticky') continue;
      if (s.visibility === 'hidden' || s.display === 'none') continue;
      // 弹窗遮罩/容器不算遮挡带（见上方取证）：它们铺满视口，算进来会让
      // blockerBottom 恒为视口高度，把可点的链接推出视口
      const cls = (e.className || '').toString();
      if (/ant-modal-mask|ant-modal-wrap/.test(cls)) continue;
      const b = e.getBoundingClientRect();
      if (b.width < 100 || b.height < 10) continue;
      if (b.left > x || b.right < x) continue;
      if (b.top > r.top) continue;             // 只算压在链接上方的
      // 铺满视口高度的元素不是「带」，是背景板/容器，同样跳过
      if (b.height >= innerHeight - 1) continue;
      if (b.bottom > bottom) bottom = b.bottom;
    }
    return bottom;
  };

  const body = m.querySelector('.ant-modal-body');
  const tried = [];
  let p = probe();
  // 【最多 4 轮】每轮把链接推到可点位置。推不动（到边界了，或推完位置没变）就不再
  // 空转，如实报出诊断交调用方。
  //
  // 【必须双向推——2026-08-28 实测】原实现只算 `blockerBottom - top + 8`（把链接从
  // 上方 fixed 遮挡带底下往【下】推），于是链接掉到视口【下方】时彻底失效：
  // offer 1014675972015 的 9 张描述图全挂在这里，日志里每张都是
  // `链接 top=1322 遮挡带下沿=1313 bodyScrollTop=422…2949`、命中 None。
  // 视口高 1257，链接 top=1322 已在视口下沿之外 → probe 的候选点全被
  // `y > innerHeight` 跳过 → elementFromPoint 返回 null（它只对视口内坐标有效）；
  // 而 need = 1313 - 1322 + 8 = -1 <= 0 → break，循环以为「已越过遮挡带、无需再推」。
  // 两种失效方向被混成了一个量：被上方压住要往下推，掉到视口下方要往【上】推。
  // 故这里分别算 needDown / needUp，取当前真正需要的那个方向。
  for (let round = 0; round < 4 && p.missed; round++) {
    const r = p.rect;
    const bb = blockerBottom();
    // 被上方遮挡带压住 → 把链接往下挪（减 scrollTop）
    const needDown = Math.max(bb - r.top + 8, 0);
    // 掉到视口下方 → 把链接往上挪（加 scrollTop）。留 12px 余量，
    // 让整条链接（约 20px 高）连同下半的候选点都落进视口
    const needUp = Math.max(r.bottom - innerHeight + 12, 0);
    tried.push({round, y: Math.round(r.top), needDown: Math.round(needDown),
                needUp: Math.round(needUp), innerH: innerHeight,
                scrollTop: body ? Math.round(body.scrollTop) : null,
                hitAt: p.hit ? (p.hit.className || '').toString().slice(0, 40) : null});
    if (!body) break;
    const before = body.scrollTop;
    if (needUp > 0) {
      // 往上推没有 scrollTop<=0 那道限制（是加不是减），但要防超出可滚范围
      const max = Math.max(body.scrollHeight - body.clientHeight, 0);
      if (before >= max) break;                       // 已经到底，推不动了
      body.scrollTop = Math.min(before + needUp, max);
    } else if (needDown > 0) {
      if (before <= 0) break;                         // 已经到顶，推不动了
      body.scrollTop = Math.max(before - needDown, 0);
    } else {
      break;                                          // 两个方向都不需要推
    }
    await sleep(350);
    if (Math.round(body.scrollTop) === Math.round(before)) break;   // 推不动了
    p = probe();
  }

  const r = p.rect;
  return JSON.stringify({x: p.x, y: p.y,
    onLink: onLink(p.hit),
    linkTop: Math.round(r.top), linkBottom: Math.round(r.bottom),
    blockerBottom: Math.round(blockerBottom()),
    // innerH 要报出来：调用方靠「linkTop > innerH」把「掉到视口下方」与「被上方
    // fixed 压住」分开写日志（两者修正方向相反）
    innerH: innerHeight,
    bodyScrollTop: body ? Math.round(body.scrollTop) : null,
    fixTried: tried,
    hitTag: p.hit ? p.hit.tagName : null,
    hitAt: p.hit ? (p.hit.className || '').toString().slice(0, 60) : null});
})()"""


# 不用坐标，直接给「更换图片」链接派发鼠标事件（坐标被 fixed 顶栏压住时的兜底）。
#
# 【为什么这不是首选】rc-trigger 绑的是 mousedown，合成事件多数情况能触发，但素材图与
# SKC 那两处都实测过合成点击不稳（见 DESC_MENU_ITEMS 上方 2026-08-20 的记录、以及
# 记忆里「SKC 行按钮必须 CDP 真实点击」那条），所以这里只在坐标物理上走不通时用。
#
# 三个事件都派发（mousedown / mouseup / click）：rc-trigger 认 mousedown，
# 而 antd 的 Dropdown 在某些版本上还要 click 才切换 open 态，缺一种就可能只闪一下。
_JS_DESC_DISPATCH_LINK = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const a = Array.from(m.querySelectorAll('.smt-content-right a'))
    .find(x => (x.textContent || '').trim() === '更换图片');
  if (!a) return JSON.stringify({err: '右侧面板没有「更换图片」链接'});
  const r = a.getBoundingClientRect();
  const x = Math.round(r.x + r.width / 2), y = Math.round(r.y + r.height / 2);
  const opt = {bubbles: true, cancelable: true, view: window, button: 0,
               clientX: x, clientY: y};
  for (const t of ['mousedown', 'mouseup', 'click']) {
    a.dispatchEvent(new MouseEvent(t, opt));
    await sleep(120);
  }
  return JSON.stringify({dispatched: true, x, y});
})()"""


# 描述专属菜单是否已展开（判据与 _JS_DESC_PICK_SPACE 里那段完全一致，故共用
# __ITEMS__ 占位符）。单独抽出来【为了改成轮询而不是固定 sleep】：
# 2026-08-26 实测（925861971282 描述区第 3 张）报 `描述专属菜单未展开, visibleMenus: []`
# ——整页连一个可见 .ant-dropdown 都没有，而紧接着的第 4 张同一条代码路径就成功了。
# 这类「上一张成功、下一张挂、再下一张又成功」的失败不是结构问题，是时序：
# 固定等 1.8s 有时不够，且 ant 的 rc-trigger 在页面已有打开浮层时会把第一次真实
# mousedown 用来【关掉旧浮层】而不是打开新的（与 _park_image_menus 处理的是同一类
# 事实）。轮询 + 补一次点击才是对症的处置。
_JS_DESC_MENU_STATE = r"""(() => {
  const vis = Array.from(document.querySelectorAll('.ant-dropdown'))
    .filter(x => !/display:\s*none/.test(x.getAttribute('style') || ''));
  const texts = vis.map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
    .map(i => (i.textContent || '').trim()));
  return JSON.stringify({
    found: texts.some(t => __ITEMS__.every(w => t.includes(w))),
    visibleMenus: texts});
})()"""
