"""店小秘发布操作：media.skc_scripts。模块导航见 docs/publish-pipeline-refactor.md。"""


# 读某颜色行的图片状态：数量 + 前几张 src（用于替换前后对比）
_JS_SKC_ROW_STATE = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => ((tr.querySelector('td') || {}).textContent || '').trim() === __KEY__.trim());
  if (!row) return JSON.stringify({err: '找不到颜色行: ' + __KEY__});
  const imgs = Array.from(row.querySelectorAll('img'))
    .filter(im => (im.currentSrc || im.src || '').startsWith('http'));
  // srcs 是给日志看的短尾；urls 是完整地址（尺寸兜底要按它把图下载回来重做合规化），
  // sizes 用来判服装类下限——0 表示还没加载完，调用方按「读不到」处理。
  return JSON.stringify({count: imgs.length,
    srcs: imgs.map(im => (im.currentSrc || im.src || '').slice(-46)),
    urls: imgs.map(im => im.currentSrc || im.src || ''),
    sizes: imgs.map(im => [im.naturalWidth || 0, im.naturalHeight || 0])});
})()"""


# 删某颜色行的第 1 张图。
# 为什么固定删第 1 张而不按索引删：先挂新图后，旧图仍在前 N 位，逐次删第 1 张删 N 次
# 即可清掉全部旧图，且每次删完 DOM 重排后「第 1 张」始终是下一张待删的旧图——
# 不需要处理索引位移（原脚本同样的取向）。
# 删除按钮是 .single-image 内的 a.icon_delete（2026-08-20 实测，6 张图对应 6 个）。
_JS_SKC_DEL_FIRST = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => ((tr.querySelector('td') || {}).textContent || '').trim() === __KEY__.trim());
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const cells = Array.from(row.querySelectorAll('.single-image'))
    .filter(c => Array.from(c.querySelectorAll('img'))
      .some(im => (im.currentSrc || im.src || '').startsWith('http')));
  if (!cells.length) return JSON.stringify({err: '行内已无图片'});
  const before = cells.length;
  const del = cells[0].querySelector('a.icon_delete, .icon_delete');
  if (!del) return JSON.stringify({err: '第 1 张图上找不到删除图标'});
  del.click();
  // 【轮询回读而不是硬等 1200ms】删除无二次确认框、直接生效，DOM 重排通常几十毫秒。
  // 判据就是原来的回读（行内图数减少），只是不再等满 1.2s——一行 6 张要删 6 次，
  // 硬等在这一处每行就白花约 7s。上限 6s 兜住重渲染慢的极端情况。
  const count = () => Array.from(row.querySelectorAll('.single-image'))
    .filter(c => Array.from(c.querySelectorAll('img'))
      .some(im => (im.currentSrc || im.src || '').startsWith('http'))).length;
  let after = before;
  for (let i = 0; i < 60; i++) {
    await sleep(100);
    after = count();
    if (after < before) break;
  }
  return JSON.stringify({deleted: after < before, before, after});
})()"""


# 第 1 步：滚到行按钮并回传坐标。
# 【必须分两次 evaluate】滚动是平滑动画，同一次 evaluate 里读到的坐标是动画中途的值，
# CDP 按那个坐标点会点偏（原脚本记录的坑，本项目同样成立）。故这里只滚动，
# 等待后另起一次 _JS_SKC_BTN_POS 读坐标。
# 【block 位置做成参数】固定 center 时按钮恒落在残留 fixed 菜单停靠的那条带上，
# 整块 9 个候选瞄点一起被盖住；换 nearest/start/end 能把按钮挪开，
# 见 _skc_aim_row_button。
_JS_SKC_BTN_SCROLL = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => ((tr.querySelector('td') || {}).textContent || '').trim() === __KEY__.trim());
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const btn = Array.from(row.querySelectorAll('button'))
    .find(b => (b.textContent || '').includes('选择图片'));
  if (!btn) return JSON.stringify({err: '行内找不到「选择图片」按钮'});
  btn.scrollIntoView({block: __BLOCK__});
  return JSON.stringify({ok: true});
})()"""


# 第 2 步：读坐标并用 elementFromPoint 校验瞄点确实落在按钮上。
# 不校验就点的话，页面稍有位移就会点到隔壁行的按钮——那会把图挂到错误的颜色行。
#
# 【多瞄点，不是只试中心点】2026-08-24 实测（846106032776 紫罗兰行、890843533224
# 黑色行）：上一张图留下的图片菜单是 position:fixed、z-index 高于表格，正好盖住按钮
# 中心点，瞄点落在 '引用采集图片' 上。那个菜单 Escape 和合成 click 都收不掉
# （见 _park_image_menus），但它通常只盖住按钮的一部分——换个点就能命中。
# 故按「中心 → 左右 → 上下 → 四角内缩」的顺序试，取第一个 elementFromPoint 确实
# 命中按钮的点。安全性不打折：每个候选点都过同一道 elementFromPoint 校验才会被采用，
# 绝不会点到隔壁行。
_JS_SKC_BTN_POS = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => ((tr.querySelector('td') || {}).textContent || '').trim() === __KEY__.trim());
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const btn = Array.from(row.querySelectorAll('button'))
    .find(b => (b.textContent || '').includes('选择图片'));
  if (!btn) return JSON.stringify({err: '行内找不到「选择图片」按钮'});
  const r = btn.getBoundingClientRect();
  // 候选瞄点：中心优先，其余都在按钮矩形内（4px 内缩，避开边框与圆角）
  const inset = 4;
  const cands = [
    ['center', r.x + r.width / 2,        r.y + r.height / 2],
    ['left',   r.x + inset,              r.y + r.height / 2],
    ['right',  r.right - inset,          r.y + r.height / 2],
    ['top',    r.x + r.width / 2,        r.y + inset],
    ['bottom', r.x + r.width / 2,        r.bottom - inset],
    ['tl',     r.x + inset,              r.y + inset],
    ['tr',     r.right - inset,          r.y + inset],
    ['bl',     r.x + inset,              r.bottom - inset],
    ['br',     r.right - inset,          r.bottom - inset],
  ];
  let x = Math.round(cands[0][1]), y = Math.round(cands[0][2]);
  let at = document.elementFromPoint(x, y);
  let hit = !!(at && (at === btn || btn.contains(at)));
  let aimAt = 'center';
  if (!hit) {
    for (const [name, cx, cy] of cands.slice(1)) {
      const px = Math.round(cx), py = Math.round(cy);
      const el = document.elementFromPoint(px, py);
      if (el && (el === btn || btn.contains(el))) {
        x = px; y = py; at = el; hit = true; aimAt = name;
        break;
      }
    }
  }
  // 未命中时要说清【被什么遮着】：只报「瞄点未命中」会让人以为是滚动时序问题，
  // 而最常见的原因是有全屏弹窗盖着（2026-08-24 实测：描述编辑器 modal 没关，
  // 2560x1257 盖满整页，瞄点落在它的 .page-content 上，连着两次误判成时序脆点）。
  const cls = el => (el && (el.className || '').toString().slice(0, 60)) || '';
  // 【遮挡物必须连 .ant-dropdown 一起查】2026-08-24 实测（890843533224 黑色行）：
  // 瞄点落在 '引用采集图片' 上——上一张图的 SKC/素材图菜单没收起，浮在行按钮上方。
  // 原先只查 .ant-modal-*，那次 blockers 报的是空数组，于是错误信息看着像滚动时序
  // 问题，实际根因是残留浮层。菜单是 position:fixed 且 z-index 高于表格，必查。
  const blockers = hit ? [] : [
    ...Array.from(document.querySelectorAll('.ant-modal-wrap, .ant-modal-mask'))
      .filter(d => d.offsetHeight > 0)
      .map(d => cls(d) + '|' + ((d.querySelector('.ant-modal-title') || {}).textContent || '').trim().slice(0, 20)),
    ...Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || '') && d.offsetHeight > 0)
      .map(d => 'ant-dropdown|' + Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()).join('/').slice(0, 40)),
  ];
  return JSON.stringify({x, y, hit, aimAt: hit ? aimAt : null,
    atText: at ? (at.textContent || at.tagName).trim().slice(0, 16) : null,
    atClass: hit ? '' : cls(at), blockers});
})()"""


# 第 3 步：在 CDP 点击后新建出的 SKC 菜单里点「空间图片」。
# 靠「含应用到所有颜色」认出 SKC 菜单，绝不能只按 4 项菜单文本找——那会命中
# 素材图的菜单实例，导致图挂到素材图上（已实测复现过一次）。
_JS_SKC_CLICK_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const menus = Array.from(document.querySelectorAll('.ant-dropdown')).filter(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return txt.includes('空间图片') && txt.includes(__EXTRA__);
  });
  if (!menus.length) {
    // 把当前所有可见菜单报出来，便于判断是不是又拿到了素材图那个实例
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()));
    return JSON.stringify({err: 'CDP 点击后没出现 SKC 菜单（含「' + __EXTRA__ + '」）',
      visibleMenus: seen});
  }
  // 多个 SKC 菜单实例并存时取最后一个：新建的实例追加在 DOM 末尾
  const menu = menus[menus.length - 1];
  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间图片');
  if (!item) return JSON.stringify({err: 'SKC 菜单里没有「空间图片」项'});
  item.click();
  // 【轮询而不是硬等 3s】2026-08-26 真站实测（_probe_skc_space_modal.py）：弹窗连同
  // 图片列表一起在 106ms 就绪，硬等 3s 等于每张图白等 2.9s——SKC 一行 6 张、一个商品
  // 6 行时白等 104s。上限给 15s（远宽于实测，网络抖动时才会用到），到点仍未出现才报错。
  // 判据要等到 .img-item 出现而不是只等弹窗容器：容器先挂载、列表异步渲染，
  // 只等容器时下一步 _pick_from_space 会因「弹窗里找不到刚上传的图」失败。
  let opened = false, items = 0, waitedMs = 0;
  const t0 = performance.now();
  for (let i = 0; i < 150; i++) {
    const m = Array.from(document.querySelectorAll('.ant-modal')).find(m =>
      m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
    if (m) {
      opened = true;
      items = m.querySelectorAll('.img-item').length;
      if (items) break;
    }
    await sleep(100);
  }
  waitedMs = Math.round(performance.now() - t0);
  return JSON.stringify({opened, itemCount: items, waitedMs});
})()"""


# 轮询等平滑滚动停稳：连续两帧读到同一个 top 就算停稳。
#
# 【为什么判据是「位置不再变」而不是固定时长】scrollIntoView({behavior:'smooth'})
# 的动画时长由浏览器定，硬等 1.2s 既可能不够（长页面）又通常过头（实测多数 200~300ms
# 就停了）。位置稳定是这件事的真实判据，也正是后续 CDP 点击所依赖的前提——原注释说的
# 「动画中途的坐标会点偏到隔壁行」，指的就是位置还在变。
_JS_SCROLL_SETTLED = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => ((tr.querySelector('td') || {}).textContent || '').trim() === __KEY__.trim());
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const top = () => Math.round(row.getBoundingClientRect().top);
  const t0 = performance.now();
  let last = top(), same = 0;
  // 上限 3s：比原来的硬等 1.2s 更宽容（长页面平滑滚动可能超过 1.2s，原实现那种情况
  // 下反而是坐标没稳就去点，正是「落在隔壁行」的成因之一）
  for (let i = 0; i < 30; i++) {
    await sleep(100);
    const now = top();
    if (now === last) {
      // 连续两次相同才认停稳：单次相同可能撞上动画的匀速平台期
      if (++same >= 2) {
        return JSON.stringify({settled: true, top: now,
                               waitedMs: Math.round(performance.now() - t0)});
      }
    } else {
      same = 0;
    }
    last = now;
  }
  return JSON.stringify({settled: false, top: last,
                         waitedMs: Math.round(performance.now() - t0)});
})()"""


# 轮询等 SKC 菜单出现（判据同 _JS_SKC_CLICK_SPACE：必须含「应用到所有颜色」，
# 否则会认成素材图那个菜单实例——那会把图挂到素材图上，见本节开头的踩坑记录）。
_JS_WAIT_SKC_MENU = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const t0 = performance.now();
  const hit = () => Array.from(document.querySelectorAll('.ant-dropdown')).some(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return txt.includes('空间图片') && txt.includes(__EXTRA__);
  });
  for (let i = 0; i < 80; i++) {
    if (hit()) return JSON.stringify({ready: true,
                                      waitedMs: Math.round(performance.now() - t0)});
    await sleep(100);
  }
  // 超时把当前可见菜单报出来，便于判断是不是拿到了素材图那个实例（同原错误信息的取向）
  const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
    .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
    .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim()));
  return JSON.stringify({ready: false, visibleMenus: seen});
})()"""


# 变种属性区有没有「按颜色配图」这个功能。
#
# 【为什么要单独探这个】2026-08-29 真站取证（草稿 173539495451708963，类目仿真花）：
# 该类目的变种属性区【没有任何图片位】——行内 http 图 0、「选择图片」按钮 0、
# .single-image 图格 0，连 <tr> 都没有（颜色是复选框列表，界面上只有勾选框和一个
# 铅笔改名图标，见用户截图）。于是 _skc_row_state 按「tr + textContent 含颜色名」
# 找行必然报「找不到颜色行: 【心想事橙】橙子花筒（life盆）」，⑦ 六行全失败。
# 那不是定位写错，是这个类目压根不支持按颜色配图——与⑧ 无尺码维、⑨ 无尺码表栏
# 同一性质：平台按类目决定有没有这个功能，没有就该 skipped 而不是 fail。
#
# 判据取【图片位的三个结构信号】而不是「找不到行就算没有」：后者会把
# 「颜色名对不上」（真的定位问题，比如平台改了名字）也判成「本类目不支持」，
# 那种情况必须暴露出来，否则服装商品的 SKC 换图会静默跳过、发出去全是 1688 原图。
_JS_SKC_IMAGE_SUPPORT = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({section: false});
  const httpImg = el => (el.currentSrc || el.src || '').startsWith('http');
  return JSON.stringify({
    section: true,
    rows: sec.querySelectorAll('tr').length,
    imgs: Array.from(sec.querySelectorAll('img')).filter(httpImg).length,
    pickBtns: Array.from(sec.querySelectorAll('button'))
      .filter(b => (b.textContent || '').includes('选择图片')).length,
    imageCells: sec.querySelectorAll('.single-image').length,
    // 复选框数非 0 说明区块确实渲染完了（无图片位的类目这些是颜色复选框）
    checkboxes: sec.querySelectorAll('label.d-checkbox').length,
  });
})()"""
