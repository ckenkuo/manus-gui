"""店小秘发布操作：sizechart.scripts。模块导航见 docs/publish-pipeline-refactor.md。"""


# ---- 阶段⑨ 尺码表（add_sizechart）-------------------------------------------

# 【要自己等尺码表区域渲染出来，别指望调用方 sleep】2026-08-22 实测：
# open_edit 的加载判据是 #skuDataInfo 出现，而尺码表入口在 SKU 属性区里、渲染更晚。
# CLI 路径每条命令后面都跟着 asyncio.sleep(2) 掩盖了这一点，service 续跑补开编辑页
# 那条路没有，于是导航完同一秒就跑阶段⑨，报 no-link（同商品 947662049255）。
# 更坏的情形是 _JS_SIZECHART_STATE 此时返回 found:false ——「尺码表已存在则跳过」
# 的判断跟着失效，会对已有尺码表的商品重复走一遍新增。故两处都改成轮询等待。
#
# 【为什么按 label 文字定位、不再用 .skuAttrSizeChart】2026-08-27 实测：套装商品的
# 编辑页有两个尺码表 form-item（label 分别是「尺码表」「尺码表2」），而 .skuAttrSizeChart
# 这个类【只挂在第一个上】，尺码表2 的 form-item 没有任何具名类。原实现全靠
# document.querySelector('.skuAttrSizeChart')，因此永远只碰第一张表，套装商品发布被
# 平台打回「套装尺码模板数量不合法：您发布的产品是套装，尺码表2也需要设置」。
# 两个 item 的祖先链完全相同（form-card.skuAttrModule），区分它们的唯一稳定信号就是
# label 文字，故统一改成按 label 取、用序号选第几张。
#
# 【尺码表2 必填与否前端看不出来】同日实测：把 SKU分类下拉在 单品/同款多件/混合套装
# 三档间来回切，尺码表2 的 label 始终【没有】ant-form-item-required 类，控件文案也不变。
# 这个校验只在平台服务端做（与包装清单件数和同性质），前端不给任何提示，故不能靠读
# required 判断要不要填第二张表，只能按 SKU分类自己判（见 add_sizechart 的 which 参数
# 与 service._st_sizechart）。
_JS_SIZECHART_LOCATE = r"""
  // 按 label 文字取第 IDX 个尺码表 form-item（0=尺码表，1=尺码表2）
  const _scItem = async (idx) => {
    for (let i = 0; i < 40; i++) {
      const labs = Array.from(document.querySelectorAll('label'))
        .filter(l => /^尺码表2?$/.test((l.textContent || '').trim()));
      const l = labs[idx];
      const item = l ? l.closest('.ant-form-item') : null;
      if (item && item.querySelector('.ant-form-item-control-input')) return item;
      await sleep(300);
    }
    return null;
  };
"""


_JS_SIZECHART_STATE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
__LOCATE__
  const IDX = __IDX__;
  const item = await _scItem(IDX);
  if (!item) return JSON.stringify({found: false});
  const ctrl = item.querySelector('.ant-form-item-control-input');
  const labs = Array.from(document.querySelectorAll('label'))
    .filter(l => /^尺码表2?$/.test((l.textContent || '').trim()));
  return JSON.stringify({found: true, text: (ctrl ? ctrl.textContent : '').trim(),
    label: (labs[IDX].textContent || '').trim(), charts: labs.length});
})()"""


_JS_OPEN_SIZECHART_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
__LOCATE__
  // 同 _JS_SIZECHART_STATE：轮询等区域与入口渲染完，别假定调用方已经 sleep 过
  const IDX = __IDX__;
  const item = await _scItem(IDX);
  const link = item ? item.querySelector('span.link') : null;
  if (!link) return JSON.stringify({opened: false, reason: 'no-link'});
  link.scrollIntoView({block: 'center'});
  await sleep(500);
  link.click();
  await sleep(1500);
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  const wrap = _scList[_scList.length - 1];
  return JSON.stringify({opened: !!wrap});
})()"""


# 尺码分类：默认【跟随平台按已选类目预选的值】，不再拿写死关键词去点选项。
#
# 【为什么废掉写死的「上装」】2026-08-22 实测 947662049255（女童网纱连衣裙）报
# option-not-found：弹窗里这个下拉的选项**由页面已选类目决定**，只有「女童装-连衣裙」
# 一项，且平台已经替你选好了。原实现假设它是「上装/下装/套装」这样的固定枚举，拿
# 「上装」去 includes 匹配必然一个都命中不了——不是时序问题，是假设错了。故改为：
# 已有预选值就照用（平台按类目给的比关键词猜的准），只在没预选时才去展开挑。
#
# 另修两处定位隐患：
# 1. 【按「尺码分类」form-item 锚定 select】弹窗里有 2 个 .ant-select（尺码分类、
#    引用模板），原来 wrap.querySelector('.ant-select-selector') 取第一个、靠 DOM
#    顺序侥幸命中，字段一调序就会去改「引用模板」。
# 2. 【判浮层可见只看 inline display】原来按 offsetHeight > 0 找，与本项目已验证的
#    结论相反（隐藏浮层高度恒为 0 但 ant-select-dropdown-hidden 类不一定加，见
#    _JS_PICK_FIRST_TPL 注释）。多个浮层同时可见时，再用 search input 的
#    aria-controls 指向的 list id 精确挑出属于本 select 的那个。
_JS_SET_SIZECHART_CAT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const keyword = __CAT__;   // null 表示跟随平台预选，不指定具体分类
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  const wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({ok: false, reason: 'no-modal'});

  const item = Array.from(wrap.querySelectorAll('.ant-form-item'))
    .find(it => (((it.querySelector('.ant-form-item-label')||{}).textContent)||'').includes('尺码分类'));
  const sel = (item || wrap).querySelector('.ant-select');
  if (!sel) return JSON.stringify({ok: false, reason: 'no-select'});
  const curOf = () => {
    const x = sel.querySelector('.ant-select-selection-item');
    return x ? (x.title || x.textContent || '').trim() : '';
  };

  // 预选值可用就直接收工：不展开下拉、不点任何东西（幂等，也少一次浮层残留风险）
  const cur = curOf();
  if (cur && (!keyword || cur.includes(keyword)))
    return JSON.stringify({ok: true, selected: cur, source: 'preset'});

  const inner = sel.querySelector('.ant-select-selector') || sel;
  ['mousedown','mouseup','click'].forEach(t =>
    inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  await sleep(900);

  const search = sel.querySelector('.ant-select-selection-search-input');
  const listId = search ? search.getAttribute('aria-controls') : null;
  let drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
    .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''));
  if (listId) {
    const mine = drops.filter(d => d.querySelector('#' + listId));
    if (mine.length) drops = mine;
  }
  const drop = drops[drops.length - 1];
  if (!drop) return JSON.stringify({ok: false, reason: 'dropdown-not-open', cur});

  // 分类可能是长列表（rc-virtual-list 只渲染可视约 10 条），滚动逐屏收集。
  // 每屏 180ms 是本项目实测值，别下调（见 _read_active_options 注释）。
  const holder = drop.querySelector('.rc-virtual-list-holder');
  const opts = [];
  const collect = () => Array.from(drop.querySelectorAll('.ant-select-item-option'))
    .forEach(o => { const t = (o.textContent || '').trim();
      if (t && !opts.includes(t)) opts.push(t); });
  collect();
  if (holder) {
    for (let k = 0; k < 40; k++) {
      if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
      holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
      await sleep(180);
      collect();
    }
  }

  // 没指定关键词时只认「唯一选项」——多选项又没预选值，说明平台没按类目定死，
  // 这时替用户瞎挑一个会填出错档尺码表，宁可报错让人工指定。
  let target = null;
  if (keyword) {
    target = Array.from(drop.querySelectorAll('.ant-select-item-option'))
      .find(o => (o.textContent || '').includes(keyword));
    if (!target && opts.some(t => t.includes(keyword))) {
      // 命中的那条被虚拟列表滚出了渲染窗口：滚回顶再逐屏找回来
      if (holder) holder.scrollTop = 0;
      await sleep(200);
      for (let k = 0; k < 40 && !target; k++) {
        target = Array.from(drop.querySelectorAll('.ant-select-item-option'))
          .find(o => (o.textContent || '').includes(keyword));
        if (target || !holder) break;
        if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
        holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
        await sleep(180);
      }
    }
    // 【关键词落空但下拉只有唯一选项 → 跟随它，不判失败】2026-09-01 取证：
    // 999389808041 / 1039758347697 / 827598413718 三单的阶段⑨ 全部以
    // option-not-found（keyword=连体衣，options=['女童装']）告败、整张尺码表没填。
    // 唯一选项意味着平台已按已选类目把分类定死，此时关键词对不上只说明我们按件别
    // 猜的那个词与平台措辞不同，而不是选错了档——没有别的可选，跟随它即是唯一正解。
    // 这与下面 keyword 为空时「只认唯一选项」是同一条取向（多选项才宁可报错交人工）。
    if (!target) {
      const all = Array.from(drop.querySelectorAll('.ant-select-item-option'));
      if (opts.length === 1 && all.length) {
        all[0].click();
        await sleep(800);
        const only = curOf();
        if (only) return JSON.stringify({ok: true, selected: only,
                                         source: 'only-option', keyword, options: opts});
      }
      return JSON.stringify({ok: false, reason: 'option-not-found',
                             keyword, cur, options: opts});
    }
  } else {
    const all = Array.from(drop.querySelectorAll('.ant-select-item-option'));
    if (opts.length !== 1 || !all.length)
      return JSON.stringify({ok: false, reason: 'no-preset-and-ambiguous',
                             cur, options: opts});
    target = all[0];
  }
  target.click();
  await sleep(800);
  const now = curOf();
  if (!now) return JSON.stringify({ok: false, reason: 'click-no-effect',
                                   options: opts});
  return JSON.stringify({ok: true, selected: now, source: 'picked', options: opts});
})()"""


_JS_SIZECHART_PARAMS = r"""(() => {
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({params: [], sizes: []});
  const table = Array.from(wrap.querySelectorAll('table')).find(candidate =>
    Array.from(candidate.querySelectorAll('tbody tr')).some(row =>
      !row.matches('.ant-table-measure-row, [aria-hidden="true"]') && row.querySelector('input')));
  if (!table) return JSON.stringify({params: [], sizes: []});
  const headerTable = Array.from(wrap.querySelectorAll('table')).find(candidate => candidate.querySelector('thead th'));
  const ths = Array.from((headerTable || table).querySelectorAll('thead th'));
  // 【参数名只取 th 的直接文本节点】2026-08-22 实测：表头是
  //   <th>裙长 <div class="flex..."><div class="link">(批量)</div></div></th>
  // 用 textContent 会得到「裙长 (批量)」——那是批量填充按钮的 UI 文案，不是参数名。
  // 带着后缀往下走，会污染 LLM 提示词（让模型去估一个叫「裙长 (批量)」的参数）、
  // 也让源实测值的模糊对齐失准，最终 data 的键全是脏的。
  const thName = th => Array.from(th.childNodes)
    .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
    .filter(Boolean).join('') || (th.textContent||'').trim();
  const params = ths.slice(1).map(thName)
    .filter(t => t && !t.includes('身高') && !t.includes('体重'));
  const trs = Array.from(table.querySelectorAll('tbody tr'))
    .filter(row => !row.matches('.ant-table-measure-row, [aria-hidden="true"]') && row.querySelector('input'));
  const sizes = trs.map(tr => {
    const td = tr.querySelector('td');
    const input = td && td.querySelector('input');
    return td ? ((input && input.value) || td.textContent || '').trim() : '';
  }).filter(t => t);
  return JSON.stringify({params, sizes});
})()"""


# 弹窗上方那排「尺码参数」是【可选复选框】，不是分类写死的必填列：勾选哪几项，下面的
# 表格就渲染出哪几列输入框。原先代码只读已渲染的 thead（_JS_SIZECHART_PARAMS），
# 等于只认「平台默认勾上的那几项」，源数据能覆盖的其它部位（裤长/胸围全围/臀围全围…）
# 一个都没勾、全被浪费——2026-09-07 offer 1074392045040（男婴背带裤）就是这么把源
# 8 列全丢掉、改交模型凭空估一个「领围」的。故先读出这排可选参数与各自勾中态。
#
# 定位拿「尺码参数」四个字做锚（form-item 行），不依赖具体类名：antd 的 Checkbox.Group
# 各版本类名有浮动（ant-checkbox-group / ant-checkbox-wrapper），但 label 文字是稳定的。
_JS_SIZECHART_AVAILABLE_PARAMS = r"""(() => {
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({found: false});
  // 「尺码参数」form-item 行：拿 label 文字锚定，再收这一行里所有 checkbox
  const item = Array.from(wrap.querySelectorAll('.ant-form-item'))
    .find(it => {
      const lab = it.querySelector('.ant-form-item-label');
      return lab && (lab.textContent || '').includes('尺码参数');
    });
  const scope = item || wrap;
  const out = [];
  scope.querySelectorAll('.ant-checkbox-wrapper').forEach(cw => {
    const name = (cw.textContent || '').trim();
    if (!name) return;
    out.push({name,
              checked: cw.classList.contains('ant-checkbox-wrapper-checked')
                       || !!cw.querySelector('.ant-checkbox-checked'),
              disabled: cw.classList.contains('ant-checkbox-wrapper-disabled')
                        || !!cw.querySelector('.ant-checkbox-disabled')});
  });
  return JSON.stringify({found: out.length > 0, params: out});
})()"""


# 勾选/取消勾选「尺码参数」复选框，让表格渲染出目标列。
# 目标 = 源数据能覆盖的部位（LLM 语义匹配勾上）；平台默认勾上但源没有的部位（领围）
# 取消勾选——那是平台默认集对背带裤的误判，留着只会填进一个对不上实物的凭空估算值。
# 取消勾选刻意保守：只动「源数据存在、但平台默认勾上的这个部位源没有」的那几项，
# 不碰任何非默认、或已被勾选且有值的项（避免误伤平台真正强制的项）。
_JS_SET_SIZECHART_PARAMS = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const toCheck = __CHECK__;       // 要勾上的参数名列表
  const toUncheck = __UNCHECK__;   // 要取消勾选的参数名列表
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({ok: false, reason: 'no-modal'});
  const item = Array.from(wrap.querySelectorAll('.ant-form-item'))
    .find(it => {
      const lab = it.querySelector('.ant-form-item-label');
      return lab && (lab.textContent || '').includes('尺码参数');
    });
  const scope = item || wrap;
  const wrappers = Array.from(scope.querySelectorAll('.ant-checkbox-wrapper'));
  const byName = name => wrappers.find(cw => (cw.textContent || '').trim() === name);
  const isChecked = cw => cw.classList.contains('ant-checkbox-wrapper-checked')
                          || !!cw.querySelector('.ant-checkbox-checked');
  const isDisabled = cw => cw.classList.contains('ant-checkbox-wrapper-disabled')
                           || !!cw.querySelector('.ant-checkbox-disabled');
  const changed = [], skipped = [], failed = [];
  const clickBox = async cw => {
    const inner = cw.querySelector('.ant-checkbox-input') || cw;
    ['mousedown','mouseup','click'].forEach(t =>
      cw.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
    await sleep(500);
  };
  // 先勾上要勾的，再取消勾选要取消的（一次操作完再统一等表格重渲）
  for (const name of toCheck) {
    const cw = byName(name);
    if (!cw) { failed.push(name + '(无此项)'); continue; }
    if (isDisabled(cw)) { skipped.push(name + '(禁用)'); continue; }
    if (isChecked(cw)) { continue; }   // 已勾上，幂等跳过
    await clickBox(cw);
    if (isChecked(cw)) changed.push(name); else failed.push(name + '(勾选未生效)');
  }
  for (const name of toUncheck) {
    const cw = byName(name);
    if (!cw) { continue; }                       // 本就无此项，不算失败
    if (isDisabled(cw)) { skipped.push(name + '(禁用,未取消)'); continue; }
    if (!isChecked(cw)) { continue; }            // 本就没勾，幂等跳过
    await clickBox(cw);
    if (!isChecked(cw)) changed.push('-' + name); else failed.push(name + '(取消未生效)');
  }
  await sleep(800);   // 等表格按新勾选集重渲出输入列
  return JSON.stringify({ok: failed.length === 0, changed, skipped, failed});
})()"""


# 【鞋类国家码列是平台算的，别自己换算】2026-09-14 真站取证（1076651064534，女士
# 居家拖鞋，尺码行 36-37 / 38-39 / 40-41）：欧码/英码/美码/日本码/韩国码/墨西哥码/
# 巴西码/哥伦比亚码/智利码 这 9 列在弹窗里是【下拉框】，其选项是平台按脚长给出的固定
# 全集（欧码是 30.5-31…50.5-51 这样的半码桶，巴西码/哥伦比亚码/智利码是单个数字），
# 与行无关、与卖家写的尺码标签也无关。脚长列表头的问号气泡原文就是「填写脚长，将自动
# 填充鞋码大小」——给脚长框打真实键盘事件（input/change/blur）后，实测 300ms 内 9 列
# 全部被平台自己填上。
#
# 原实现（把欧码当唯一基准、自己算各国码再逐格去点下拉）对不上任何选项：欧码列没有
# "36-37" 这个选项，巴西码列没有 "34-35" 这种区间值，只有碰巧是单个数字的巴西码命中
# 过一次，于是 8 列全空、整表报「表格填充不完整」，还因为 setSelect 每格 2 轮 × 60 次
# 轮询空转到 1118 秒。故改成：先填文本列 → 把脚长调准 → 等平台把国家码列填完 →
# 只对仍空着的下拉格兜底（正常情况一格都不会走到）。
#
# 【为什么脚长要来回调】平台的脚长→欧码表并不等于「欧码 = 脚长 + 12」，实测
# 20→30.5-31、23→35.5-36、24→36.5-37、25→38.5-39、26→40.5-41、28→43.5-44，斜率在
# 0.5cm/码 到 1cm/码 之间跳，且这张表按类目走、我们无从穷举。故不写死换算，改成拿
# 页面当唯一权威：设一个脚长、读回平台算出的欧码、按差值修正，收敛即停（每行 <=6 次，
# 每次约 350ms）。这样无论类目、无论尺码标签是区间还是单码都对得上。
_JS_FILL_SIZECHART = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const tplName = __NAME__;
  const data = __DATA__;
  const params = __PARAMS__;
  const derived = __DERIVED__;   // 由平台按脚长自动算出的国家码列（无脚长列时为空）
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({ok: false, reason: 'no-modal'});
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setVal = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const setSelect = async (cell, value) => {
    const select = cell && cell.querySelector('.ant-select');
    if (!select) return false;
    const want = String(value).trim();
    const candidates = [want, want.replace(/\.0$/, ''), want + 'cm', want + '厘米'];
    const input = select.querySelector('input');
    const popupId = input && (input.getAttribute('aria-controls') || input.getAttribute('aria-owns'));
    for (let attempt = 0; attempt < 2; attempt++) {
      document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
      await sleep(80);
      const selector = select.querySelector('.ant-select-selector');
      ['mousedown','mouseup','click'].forEach(type =>
        selector.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window})));
      await sleep(180);
      for (let k = 0; k < 60; k++) {
        const dropdowns = popupId && document.getElementById(popupId)
          ? [document.getElementById(popupId).closest('.ant-select-dropdown') || document.getElementById(popupId)]
          : Array.from(document.querySelectorAll('.ant-select-dropdown'));
        const option = dropdowns
          .filter(Boolean)
          .filter(d => getComputedStyle(d).display !== 'none'
                   && !d.classList.contains('ant-select-dropdown-hidden'))
          .flatMap(d => Array.from(d.querySelectorAll('.ant-select-item-option')))
          .find(o => candidates.includes(((o.querySelector('.ant-select-item-option-content') || o).textContent || '').trim()));
        if (option) {
          const box = option.getBoundingClientRect();
          ['mousedown','mouseup','click'].forEach(type =>
            option.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
              clientX: box.left + box.width / 2, clientY: box.top + box.height / 2, view: window})));
          await sleep(150);
          const got = (select.querySelector('.ant-select-selection-item') || {}).textContent || '';
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
          if (candidates.includes(got.trim()) || candidates.includes(((cell.innerText || '').split(/\n/).pop() || '').trim()))
            return true;
          break;
        }
        const holder = dropdowns.filter(Boolean).map(d => d.querySelector('.rc-virtual-list-holder')).find(Boolean);
        if (holder) holder.scrollTop += holder.clientHeight || 200;
        await sleep(100);
      }
    }
    return false;
  };
  const nameInp = wrap.querySelector('input[placeholder*="模板名称"]');
  if (nameInp && nameInp.value !== tplName) setVal(nameInp, tplName);
  const table = Array.from(wrap.querySelectorAll('table')).find(candidate =>
    Array.from(candidate.querySelectorAll('tbody tr')).some(row =>
      !row.matches('.ant-table-measure-row, [aria-hidden="true"]') && row.querySelector('input')));
  if (!table) return JSON.stringify({ok: false, reason: 'no-table'});
  const headerTable = Array.from(wrap.querySelectorAll('table')).find(candidate => candidate.querySelector('thead th'));
  const ths = Array.from((headerTable || table).querySelectorAll('thead th'));
  // 参数名提取须与 _JS_SIZECHART_PARAMS 的 thName 一致（剥掉「(批量)」子元素文案），
  // 否则这里按 textContent 比对，params 里的干净名一个都匹配不上、colIdx 全空。
  const thName = th => Array.from(th.childNodes)
    .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
    .filter(Boolean).join('') || (th.textContent||'').trim();
  const colIdx = {};
  ths.forEach((th, i) => {
    const t = thName(th);
    if (params.includes(t)) colIdx[t] = i;
  });
  const trs = Array.from(table.querySelectorAll('tbody tr'))
    .filter(row => !row.matches('.ant-table-measure-row, [aria-hidden="true"]') && row.querySelector('input'));
  if (!trs.length) return JSON.stringify({ok: false, reason: 'no-data-rows'});
  const rowSize = cell => ((cell.querySelector('input') || {}).value || cell.textContent || '').trim();
  const empty = [];
  const numOf = text => (String(text || '').match(/\d+(?:\.\d+)?/g) || []).map(Number);
  const cellText = cell => {
    if (!cell) return '';
    if (cell.querySelector('.ant-select'))
      return ((cell.querySelector('.ant-select-selection-item') || {}).textContent || '').trim();
    const inp = cell.querySelector('input');
    return inp ? inp.value.trim() : '';
  };
  const blurInput = inp => { inp.dispatchEvent(new Event('blur', {bubbles: true})); inp.blur(); };
  const rows = trs.map(tr => ({tr, tds: Array.from(tr.querySelectorAll('td'))}))
    .map(r => Object.assign(r, {size: rowSize(r.tds[0])}));
  const cellOf = (row, p) => colIdx[p] === undefined ? null : row.tds[colIdx[p]];

  // 【平量/拉量同一单元格两个输入框都要填】2026-09-08 商品 875335387236（猫狗服饰）：
  // 勾选「平量」+「拉量」两种测量方式后，同一参数单元格会渲染出上下两个输入框（平量/
  // 拉量），接口要求两格要么都空、要么都填。源实测尺寸只有一组值（提取阶段要求值只填
  // 单个数字），故把同一源值重复填进单元格每个输入框，拉量缺失就沿用平量值——只填第一
  // 个 input 会留下空着的拉量格，点确定即被接口以「请全部填写」打回。
  //
  // 文本格与下拉格分两遍走：脚长属文本格，填完它平台才会去算国家码列；原先按行交错，
  // 脚长落值的同一瞬间就去读国家码下拉，那时平台还没算，必然读到空。
  for (const row of rows) {
    if (!data[row.size]) { empty.push(row.size + ':缺少测量数据'); return; }
    for (const p of params) {
      const idx = colIdx[p];
      if (idx === undefined) { empty.push(`${row.size}-${p}:缺列`); continue; }
      const cell = row.tds[idx];
      if (cell && cell.querySelector('.ant-select')) continue;   // 下拉留到第二遍
      const inps = cell ? Array.from(cell.querySelectorAll('input')) : [];
      if (!inps.length) empty.push(`${row.size}-${p}:无输入框`);
      const v = String(data[row.size][p] || '');
      if (!v) continue;
      inps.forEach(inp => { if (inp.value !== v) setVal(inp, v); });
    }
  }

  // ---- 让平台按脚长自己算国家码列 ----
  // 目标：本行欧码桶的上端点 = 本行尺码标签的上端点（36-37 → 36.5-37）。标签不是数字
  // （S/M/L 之类）就不管，留给平台按现成的脚长自己填。
  const footParam = params.find(p => p.indexOf('脚长') === 0);
  if (derived.length && footParam && colIdx[footParam] !== undefined) {
    const euParam = derived.indexOf('欧码') >= 0 ? '欧码' : null;
    const upperOf = text => { const ns = numOf(text); return ns.length ? Math.max.apply(null, ns) : null; };
    for (const row of rows) {
      if (!data[row.size]) continue;
      const footInp = (cellOf(row, footParam) || {querySelector: null}).querySelector
        ? cellOf(row, footParam).querySelector('input') : null;
      if (!footInp) continue;
      const want = upperOf(row.size);
      if (want === null) continue;
      let cur = parseFloat(footInp.value);
      if (!isFinite(cur)) cur = want - 13;   // 无可用初值时的粗起点，靠下面几轮修正
      for (let k = 0; k < 6; k++) {
        setVal(footInp, String(cur));
        blurInput(footInp);
        await sleep(350);
        const got = euParam ? upperOf(cellText(cellOf(row, euParam))) : null;
        if (got === null) break;
        if (got === want) break;
        cur = Math.round((cur + (want - got) / 2) * 2) / 2;   // 实测斜率 0.5~1cm/欧码，半码步逼近
        if (cur <= 0) break;
      }
      blurInput(footInp);
    }
    // 等平台把国家码列填完：连续两轮取值不变且无空格才算收敛（实测 <300ms，留足余量）
    let prev = null;
    for (let k = 0; k < 20; k++) {
      await sleep(250);
      const snap = rows.map(row => derived.map(p => cellText(cellOf(row, p))).join('|')).join('#');
      const blank = rows.some(row => data[row.size] && derived.some(p => !cellText(cellOf(row, p))));
      if (snap === prev && !blank) break;
      prev = snap;
    }
  }

  // ---- 第二遍：只补平台没填上的下拉格（正常一格都不会走到）----
  for (const row of rows) {
    if (!data[row.size]) continue;
    for (const p of params) {
      const cell = cellOf(row, p);
      if (!cell || !cell.querySelector('.ant-select')) continue;
      if (cellText(cell)) continue;   // 平台已算好，别覆盖
      const v = String(data[row.size][p] || '').trim();
      if (!v || !(await setSelect(cell, v))) empty.push(`${row.size}-${p}`);
    }
  }
  await sleep(600);
  for (const row of rows) {
    if (!data[row.size]) continue;
    for (const p of params) {
      const cell = cellOf(row, p);
      if (!cell) continue;
      if (cell.querySelector('.ant-select')) {
        const got = cellText(cell);
        if (!got || got === '请选择') empty.push(`${row.size}-${p}`);
        continue;
      }
      Array.from(cell.querySelectorAll('input')).forEach(inp => {
        if (!inp.value) empty.push(`${row.size}-${p}`);
      });
    }
  }
  return JSON.stringify({ok: empty.length === 0, empty});
})()"""


_JS_CLICK_SIZECHART_OK = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({stillOpen: false, reason: 'no-modal'});
  const okBtn = Array.from(wrap.querySelectorAll('button')).find(b => (b.textContent||'').trim() === '确定');
  if (!okBtn) return JSON.stringify({stillOpen: false, reason: 'no-ok-btn'});
  okBtn.click();
  await sleep(2000);
  const stillOpen = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .some(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  const errors = stillOpen ? Array.from(document.querySelectorAll(
    '.ant-form-item-explain-error, .ant-form-item-explain, .ant-message-error'))
    .map(e => (e.textContent || '').trim()).filter(Boolean) : [];
  return JSON.stringify({stillOpen, errors});
})()"""
