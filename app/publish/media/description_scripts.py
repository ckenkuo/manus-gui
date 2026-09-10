"""店小秘发布操作：media.description_scripts。模块导航见 docs/publish-pipeline-refactor.md。"""


# ==================== 阶段⑪ 产品描述长图 ====================
# 业务规则（SKILL.md 阶段⑪）：Temu 只关心商品图。工厂/公司介绍图、促销海报、
# 中文尺码表、与商品无关的图一律删；重复图删；含中文的商品图英化后替换。
#
# 【编辑器是全屏 .ant-modal，不是新页面/新标签】打开后盖住整个编辑页，此时页面上
# 任何 elementFromPoint 都会命中编辑器内的元素，【别误判成「遮挡」】。
#
# 【每点一次「编辑描述」都新叠一个 modal，旧的不销毁】屏幕上看到的是最后打开的那个。
# 用 querySelector 取第一个会把删除/替换写进被盖住的旧弹窗、用户看不到效果。
# 故一律用 _JS_DESC_MODAL 取「最顶层」实例，并在写操作前先关掉多余的。
#
# 【确认框按钮文字是带空格的「确 定」】所有按钮文本匹配前先去掉全部空白再比。
#
# 2026-08-20 实测确认（基准 rowid 173539495450551101，5 个模块）：
#   - 「编辑描述」按钮 offsetHeight 为 0（靠 CSS 悬停显示），但 JS click 直接有效
#   - .using-item 的 data-idx 从 0 起，每项都有 .icon_delete
#   - 编辑器底部按钮只有「保存」「关闭」
#   - 本商品 5 张描述图全是 cbu01.alicdn.com 外链（未落店小秘图床）

# 取「最顶层」描述编辑器弹窗的 JS 表达式（内联进其它 JS 用，故不带外层括号调用）。
# 判据不用弹窗中心点：中心可能落在图片间隙里；用 y+300 并夹到视口内。
# 兜底取 DOM 最后一个——同 z-index 时 DOM 序最后者盖在最上面。
_JS_DESC_MODAL = r"""(() => {
  const list = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content'));
  if (!list.length) return null;
  for (const m of list) {
    const r = m.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2,
      Math.min(r.y + 300, innerHeight - 10));
    if (hit && m.contains(hit)) return m;
  }
  return list[list.length - 1];
})()"""


# 探测编辑器状态：是否已开、有无「编辑描述」按钮、模块图 URL（DOM 顺序即展示顺序）。
# imgs 刻意【不过滤】非 http 的 src：过滤会让这里的下标与删除/替换用的
# querySelectorAll 下标错位（原实现有这个隐患，懒加载未填 src 时会错位）。
_JS_DESC_STATE = r"""(() => {
  const m = __MODAL__;
  const sec = document.getElementById('describeInfo');
  const btn = sec && sec.querySelector('.wireless-description-shadow button');
  if (!m) return JSON.stringify({open: false, hasButton: !!btn,
    editPageImgs: sec ? sec.querySelectorAll('img').length : 0});
  const imgs = Array.from(m.querySelectorAll('.smt-desc-content .desc-img-box img'));
  const modalCount = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content')).length;
  return JSON.stringify({open: true, hasButton: !!btn, modalCount,
    count: imgs.length,
    srcs: imgs.map(i => i.currentSrc || i.src || ''),
    // 真实像素尺寸：服装类下限 1340x1785 要靠它判，naturalWidth 是现成的，
    // 不必为了量尺寸把每张图再下载一遍。未加载完时为 0，调用方按「读不到」处理。
    sizes: imgs.map(i => [i.naturalWidth || 0, i.naturalHeight || 0]),
    usingCount: m.querySelectorAll('.using-item').length});
})()"""


# 打开描述编辑器：JS click 有效（按钮 offsetHeight 为 0 也照样能点）
_JS_DESC_OPEN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sec = document.getElementById('describeInfo');
  if (!sec) return JSON.stringify({err: '找不到产品描述区块'});
  const btn = sec.querySelector('.wireless-description-shadow button');
  if (!btn) return JSON.stringify({err: '找不到「编辑描述」按钮'});
  btn.scrollIntoView({block: 'center'});
  await sleep(600);
  btn.click();
  let list = [];
  for (let attempt = 0; attempt < 20 && !list.length; attempt++) {
    await sleep(500);
    list = Array.from(document.querySelectorAll('.ant-modal'))
      .filter(element => element.querySelector('.smt-desc-content') && element.getClientRects().length);
  }
  return JSON.stringify({opened: list.length > 0, modalCount: list.length, url: location.href});
})()"""


# 关掉多余的描述编辑器实例，只留最顶层那一个。
# 为什么必须做：每次点「编辑描述」都新叠一层且旧层不销毁，多层并存时写操作可能
# 落在被盖住的旧层上——页面看不出变化，排查时会以为是选择器不对。
_JS_DESC_CLOSE_EXTRA = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const list = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content'));
  if (list.length <= 1) return JSON.stringify({closed: 0, remain: list.length});
  let closed = 0;
  // 留最后一个（最顶层），其余点「关闭」
  for (const m of list.slice(0, -1)) {
    const btn = Array.from(m.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
    if (btn) { btn.click(); closed++; await sleep(1000); }
    // 关闭可能弹二次确认，按钮文字是带空格的「确 定」
    for (let k = 0; k < 3; k++) {
      const cf = Array.from(document.querySelectorAll('.ant-modal-confirm'))
        .find(c => c.offsetHeight > 0);
      if (!cf) break;
      const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
      if (!ok) break;
      ok.click();
      await sleep(1200);
    }
  }
  const remain = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content')).length;
  return JSON.stringify({closed, remain});
})()"""


# 枚举描述区的【文字模块】：data-idx + 文本内容。
#
# 【为什么单独一个 JS 而不并进 _JS_DESC_STATE】那个函数按 .desc-img-box img 枚举，
# 下标即 desc_map 的 pos，混进非图片模块会让 pos 与删除/替换的下标错位（见下方
# _JS_DESC_IDX_MAP 的注释，那次错位真删错了两张图）。文字模块用 data-idx 直接寻址，
# 与图片的 pos 体系互不干扰。
#
# 2026-08-24 真站探查（rowid 173539495454339053，19 个模块）：data-idx=0 是文字模块，
# 内容是 1688 的关联商品 JSON 残留 `{"styleType":"offer-type-1","items":"8886...}`，
# 纯垃圾；另有商品用文字模块放尺码对照（`80【身高65-75cm】`）。2026-09-01 起两类
# 一律删除（见 desc_text_delete_all），本函数只负责枚举、不做内容判断。
_JS_DESC_TEXT_MAP = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const mods = Array.from(m.querySelectorAll('.smt-content-center .smt-desc-content'));
  const items = [];
  mods.forEach(c => {
    if (c.querySelector('.desc-img-box img')) return;   // 图片模块不管
    const box = c.querySelector('.desc-content');
    const txt = ((box ? box.innerText : c.innerText) || '').trim();
    items.push({idx: c.getAttribute('data-idx'), text: txt, len: txt.length});
  });
  return JSON.stringify({items: items, modCount: mods.length});
})()"""


# 改写 data-idx 为 __IDX__ 的文字模块内容。
#
# 【必须走右侧面板的 textarea】模块自身的 div.desc-content 不是 contenteditable、
# 也不是 input，直接改 innerText 保存时不生效（组件状态没变）。流程是：
# 点左侧 .using-item → 右侧 .smt-content-right 渲染「文字模块」面板 → 写它的
# textarea.ant-input。写完必须派发 input 事件，否则 Vue 不同步、保存后回到原文
# （本项目其它表单字段同样的坑，见 _js_fill_by_label）。
#
# 500 字符是面板自己标的上限（「总字符数:160 / 500」），超了平台会截断，
# 故调用方传进来前就该截好；这里再兜一刀，避免静默截断。
_JS_DESC_TEXT_SET = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({status: 'error', reason: '编辑器不在'});
  const item = m.querySelector('.using-item[data-idx="__IDX__"]');
  if (!item) return JSON.stringify({status: 'error', reason: 'using-item-not-found'});
  item.scrollIntoView({block: 'center'});
  await sleep(300);
  item.click();
  await sleep(1200);            // 面板渲染有入场过程，读太早拿不到 textarea

  const panel = m.querySelector('.smt-content-right');
  if (!panel) return JSON.stringify({status: 'error', reason: 'panel-not-found'});
  const ta = panel.querySelector('textarea.ant-input, textarea');
  if (!ta) return JSON.stringify({status: 'error', reason: 'textarea-not-found'});

  const before = ta.value;
  const text = __TEXT__;
  if (text.length > 500) {
    return JSON.stringify({status: 'error', reason: 'text-too-long',
                           len: text.length});
  }
  // 原生 setter + input 事件：Vue 只认事件，直接赋 value 不同步
  const desc = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value');
  desc.set.call(ta, text);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.dispatchEvent(new Event('change', {bubbles: true}));
  await sleep(600);

  // 回读：面板 textarea 与模块本体都要变（后者证明组件真的同步了）
  const mod = m.querySelector(
    '.smt-content-center .smt-desc-content[data-idx="__IDX__"] .desc-content');
  return JSON.stringify({
    status: 'ok', before: before.slice(0, 80),
    readback: ta.value.slice(0, 80),
    modText: mod ? (mod.innerText || '').trim().slice(0, 80) : null,
    filled: ta.value.trim() === text.trim(),
  });
})()"""


# 描述图序号（pos，从 1 起）到左侧「使用中模块」列表 data-idx 的映射表。
#
# 【为什么不能拿 pos-1 当 data-idx】描述区是【图文混排】的：每个内容模块都是一个
# .smt-desc-content，自带与左侧列表一致的 data-idx，但「文字」模块不含图片盒子。
# 而 desc_map 按 .desc-img-box img 枚举 pos，只数图片。于是只要描述区里混有非图片
# 模块，两套序号就整体错位——2026-08-24 实测（rowid 173539495454339053）：19 个模块
# 里 data-idx=0 是「文字」模块（存 offer JSON），18 张图对应 data-idx 1..18，
# pos-1 全部偏 1；删 pos 3/2 实际删掉的是 pos 2/1，删 pos 1 命中文字模块——
# 图片数不变，被判定为「删除失败」。真实后果比报错更糟：前两张删错了对象。
#
# 映射按【模块自身的 data-idx】建立而不是按出现次序计数：data-idx 是平台自己维护的
# 关联键（与左侧 .using-item 一一对应），比「第几个含图模块」更贴近页面真相。
_JS_DESC_IDX_MAP = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const mods = Array.from(m.querySelectorAll('.smt-content-center .smt-desc-content'));
  const map = [];
  mods.forEach(c => {
    // 只收含图片盒子的模块，顺序即 desc_map 的 pos 顺序（实测 src 逐张吻合）
    if (c.querySelector('.desc-img-box img')) map.push(c.getAttribute('data-idx'));
  });
  return JSON.stringify({map, modCount: mods.length});
})()"""


# 删左侧列表里 data-idx 为 __IDX__ 的模块。
# 走左侧「使用中模块」列表的垃圾桶图标，JS click 有效、无二次确认框。
#
# 【判据用 data-id 而不是图片计数】计数只能看出「少了一个」，看不出少的是不是目标：
# 删到非图片模块时图片数不变，会被误判成失败（见 _JS_DESC_IDX_MAP 的实测记录）。
# data-id 是模块的稳定标识，点击前先记下，删后确认它真的从列表里消失了。
_JS_DESC_DELETE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const ids = () => Array.from(m.querySelectorAll('.using-item'))
    .map(i => i.getAttribute('data-id'));
  const before = m.querySelectorAll('.smt-desc-content .desc-img-box img').length;
  const item = m.querySelector('.using-item[data-idx="' + __IDX__ + '"]');
  if (!item) return JSON.stringify({err: '找不到 data-idx=' + __IDX__ + ' 的模块'});
  const targetId = item.getAttribute('data-id');
  const del = item.querySelector('.icon_delete');
  if (!del) return JSON.stringify({err: '模块上没有删除图标'});
  del.click();
  await sleep(1200);
  const after = m.querySelectorAll('.smt-desc-content .desc-img-box img').length;
  return JSON.stringify({deleted: !ids().includes(targetId), targetId,
    before, after, imgDropped: after < before});
})()"""


# 保存描述。保存成功弹窗会自动关闭，故「弹窗还在」本身就是异常信号。
# 判据不看 toast：编辑页的消息提示是自定义实现，.ant-message 捕获不到。
_JS_DESC_SAVE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const save = Array.from(m.querySelectorAll('button')).find(b => norm(b.textContent) === '保存');
  if (!save) return JSON.stringify({err: '找不到保存按钮'});
  save.click();
  await sleep(3000);

  // 保存成功编辑器会自己关闭；没关就主动点「关闭」并处理二次确认（「确 定」带空格）
  let stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
    .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0);
  if (stillOpen) {
    const top = __MODAL__;
    const cl = top && Array.from(top.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
    if (cl) { cl.click(); await sleep(1500); }
    for (let k = 0; k < 3; k++) {
      const cf = Array.from(document.querySelectorAll('.ant-modal-confirm')).find(c => c.offsetHeight > 0);
      if (!cf) break;
      const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
      if (!ok) break;
      ok.click();
      await sleep(1200);
    }
    stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
      .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0);
  }

  // 回读【编辑页】描述区（不是弹窗）。两条业务校验：
  //   1. 所有描述图都已落店小秘图床（外链没被平台转存，发布可能被拦）
  //   2. 尺寸符合【描述图自己的规则】：宽高比 0.5~2 且两边 >= 480
  //      （2026-08-28 用户截图取证的模块弹窗说明，见 images.check_desc_size）。
  //      【不再套服装的 1340x1785】那是 SKC/素材图的服装类校验，与描述图无关；
  //      套错的后果是 1000x1000 这种本来合格的图被判不达标、每跑一次白烧一轮生图。
  const sec = document.getElementById('describeInfo');
  const imgs = sec ? Array.from(sec.querySelectorAll('img'))
    .filter(i => (i.currentSrc || i.src || '').startsWith('http')) : [];
  const urls = imgs.map(i => i.currentSrc || i.src || '');
  const small = [];
  imgs.forEach((i, k) => {
    const w = i.naturalWidth || 0, h = i.naturalHeight || 0;
    // 0 表示还没加载完；1 表示源图失效/懒加载占位（浏览器只拿到 1 像素的占位图），
    // 两者都是「没读到真实图片」，按「读不到」跳过而不是当成不达标。
    // 2026-09-04 实测（836739130561）：1688 源图 404 后编辑页渲染成 1x1 占位，
    // naturalWidth=1 被误判成「两边 < 480」整单未落库。
    if (w < 2 || h < 2) return;
    const ratio = w / h;
    if (w < __MINW__ || h < __MINH__ || ratio < __RMIN__ || ratio > __RMAX__)
      small.push({pos: k + 1, size: w + 'x' + h, ratio: Math.round(ratio * 1000) / 1000});
  });
  return JSON.stringify({stillOpen, descImgs: urls.length,
    dxmHosted: urls.filter(u => u.includes('dianxiaomi.com')).length,
    tooSmall: small,
    foreignHosts: [...new Set(urls.filter(u => !u.includes('dianxiaomi.com'))
      .map(u => { try { return new URL(u).host; } catch (e) { return '?'; } }))]});
})()"""


# 关掉描述编辑器（若开着）。点「关闭」而非「保存」——只负责让出屏幕，
# 不替调用方决定改动是否落库。二次确认按钮文案带空格（「确 定」），故 norm 掉空白再比。
_JS_DESC_CLOSE_IF_OPEN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const top = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0).pop();
  if (!top) return JSON.stringify({already: true});
  const cl = Array.from(top.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
  if (!cl) return JSON.stringify({err: '描述编辑器里找不到「关闭」按钮'});
  cl.click();
  await sleep(1500);
  for (let k = 0; k < 3; k++) {
    const cf = Array.from(document.querySelectorAll('.ant-modal-confirm')).find(c => c.offsetHeight > 0);
    if (!cf) break;
    const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
    if (!ok) break;
    ok.click();
    await sleep(1200);
  }
  return JSON.stringify({
    stillOpen: Array.from(document.querySelectorAll('.ant-modal'))
      .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0),
    visibleMasks: Array.from(document.querySelectorAll('.ant-modal-mask'))
      .filter(m => m.offsetHeight > 0).length});
})()"""
