"""店小秘发布操作：stock_scripts。模块导航见 docs/publish-pipeline-refactor.md。"""


# ---- 阶段⑪ 库存与SKU分类（set_stock）---------------------------------------

# 仓库是多选（ant-select-multiple），选中项回读 .ant-select-selection-item。
# listId 来自 select 内 input 的 aria-owns，用来在多个常驻浮层里精确认出自己那一个。
_JS_WH_STATE = r"""(() => {
  const lab = Array.from(document.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && el.closest('#skuDataInfo') &&
      (el.textContent||'').trim().startsWith('选择仓库'))[0];
  if (!lab) return JSON.stringify({err: 'no-选择仓库-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 5 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const selected = Array.from(sel.querySelectorAll('.ant-select-selection-item'))
    .map(x => (x.title || x.textContent || '').trim());
  const inp = sel.querySelector('input[aria-owns]');
  const stockHeaders = Array.from(document.querySelectorAll('#skuDataInfo table thead th'))
    .map(header => (header.textContent || '').replace(/\s+/g, '')).filter(text => text.includes('库存'));
  return JSON.stringify({selected, stockHeaders, open: sel.classList.contains('ant-select-open'),
    listId: inp ? inp.getAttribute('aria-owns') : null});
})()"""


# 展开仓库下拉并勾选目标仓库。滚动、展开、点选、回读全塞在一次 eval 里。
# 【2026-08-23 修此处的 no-dropdown】原实现踩了三个坑，逐条对应下面的写法：
# 1. 用 elementFromPoint(x, y) 合成点击——先在 python 侧读坐标、再另发一次 eval 点，
#    两次之间 Vue 只要重渲染或页面滚一下，坐标就打在别的元素上。改成直接点
#    .ant-select-selector（antd 把点击处理绑在 selector 那层，点外层 .ant-select
#    无效，同 [[dianxiaomi-antselect-open-and-ghost]]）。
# 2. 按 getBoundingClientRect().height > 0 找浮层——隐藏的浮层高度恒为 0，但可见那个
#    在页面已滚动时 top 是文档坐标（实测 top: 4618px），仍可能算出 0 高度而漏掉。
#    改成按 listId 直接 getElementById 定位，再用 inline display 判可见。
# 3. 点开下拉后固定 sleep 1.5s 就去找——浮层首次挂载慢于此就报 no-dropdown。改成
#    轮询 6s。
_JS_PICK_WAREHOUSE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const WH = __WH__;
  const lab = Array.from(document.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && el.closest('#skuDataInfo') &&
      (el.textContent||'').trim().startsWith('选择仓库'))[0];
  if (!lab) return JSON.stringify({err: 'no-选择仓库-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 5 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const inp = sel.querySelector('input[aria-owns]');
  const listId = inp ? inp.getAttribute('aria-owns') : null;
  if (!listId) return JSON.stringify({err: 'no-aria-owns'});
  sel.scrollIntoView({block: 'center', behavior: 'instant'});
  await sleep(300);
  const visible = () => {
    const lb = document.getElementById(listId);
    const c = lb ? lb.closest('.ant-select-dropdown') : null;
    return (c && !/display:\s*none/.test(c.getAttribute('style') || '')) ? c : null;
  };
  let dd = visible();
  if (!dd) {
    const inner = sel.querySelector('.ant-select-selector') || sel;
    ['mousedown', 'mouseup', 'click'].forEach(t =>
      inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
    for (let i = 0; i < 30 && !dd; i++) { await sleep(200); dd = visible(); }
  }
  if (!dd) return JSON.stringify({err: 'no-dropdown', listId,
    open: sel.classList.contains('ant-select-open')});
  const opts = Array.from(dd.querySelectorAll('.ant-select-item-option'));
  const normalize = text => (text || '').replace(/\s+/g, '').replace(/仓库$/, '仓');
  const matches = opts.filter(option => normalize(option.textContent) === normalize(WH));
  const o = matches.length === 1 ? matches[0] : null;
  if (!o) return JSON.stringify({err: 'no-option',
    available: opts.map(x => (x.textContent || '').trim()).slice(0, 8)});
  if (o.classList.contains('ant-select-item-option-selected')) {
    o.click();
    await sleep(300);
    const refreshed = document.getElementById(listId)?.closest('.ant-select-dropdown');
    const option = refreshed && Array.from(refreshed.querySelectorAll('.ant-select-item-option'))
      .find(candidate => normalize(candidate.textContent) === normalize(WH));
    if (!option) return JSON.stringify({err: 'warehouse-option-disappeared'});
    if (!option.classList.contains('ant-select-item-option-selected')) option.click();
  } else {
    o.click();
  }
  await sleep(1000);
  document.body.click();
  await sleep(1200);
  return JSON.stringify({
    selected: Array.from(sel.querySelectorAll('.ant-select-selection-item'))
      .map(x => (x.title || x.textContent || '').trim()),
    open: sel.classList.contains('ant-select-open')});
})()"""


_JS_FILL_STOCK_ONLY = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const STOCK = __STOCK__;
  let inps = [];
  for (let i = 0; i < 10; i++) {
    inps = Array.from(document.querySelectorAll('input[name=stock]'));
    if (inps.length) break;
    await sleep(500);
  }
  if (!inps.length) return JSON.stringify({err: 'no-stock-inputs'});
  for (const inp of inps) {
    if (inp.value !== STOCK) {
      setter.call(inp, STOCK);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
  await sleep(500);
  const bad = Array.from(document.querySelectorAll('input[name=stock]')).filter(i => i.value !== STOCK).length;
  return JSON.stringify({filled: inps.length, bad});
})()"""


_JS_FILL_STOCK_CAT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setInp = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const setSel = (sel, v) => { sel.value = String(v); sel.dispatchEvent(new Event('change', {bubbles:true})); };
  const CAT = __CAT__, QTY = __QTY__, UNIT = __UNIT__;
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[1];
  if (!tb) return JSON.stringify({err: 'no-second-tbody'});
  // 行区间（左闭右开）由 python 侧分批传入，见 _fill_rows_batched
  const START = __START__, END = __END__;
  const all = Array.from(tb.querySelectorAll('tr'));
  const rows = all.slice(START, END);
  const findCatTd = tds => tds.find(td =>
    Array.from(td.querySelectorAll('select option')).some(o => o.text === '混合套装'));
  let processed = 0;
  for (const r of rows) {
    const tds = Array.from(r.querySelectorAll('td'));
    if (tds.length < 4) continue;
    const catTd = findCatTd(tds);
    if (!catTd) continue;
    processed++;
    let sels = Array.from(catTd.querySelectorAll('select'));
    if (sels[0] && sels[0].value !== CAT) { setSel(sels[0], CAT); await sleep(400); }
    sels = Array.from(catTd.querySelectorAll('select'));
    if (sels[1] && sels[1].value !== UNIT) setSel(sels[1], UNIT);
    const qtyInp = catTd.querySelector('input[name=skuCategoryNum]');
    if (qtyInp && qtyInp.value !== QTY) setInp(qtyInp, QTY);
  }
  await sleep(600);
  const bad = [], sample = [];
  // 回读只校验本批那几行；total 回传整表行数，供 python 侧算下一批
  let total = 0;
  Array.from(tb.querySelectorAll('tr')).forEach((r, i) => {
    const tds = Array.from(r.querySelectorAll('td'));
    const catTd = findCatTd(tds);
    if (!catTd) return;
    total++;
    if (i < START || i >= END) return;
    const sels = Array.from(catTd.querySelectorAll('select'));
    const qtyInp = catTd.querySelector('input[name=skuCategoryNum]');
    const row = [sels[0]?sels[0].value:'', sels[1]?sels[1].value:'', qtyInp?qtyInp.value:''];
    sample.push(row);
    if (row[0] !== CAT || row[1] !== UNIT || row[2] !== QTY) bad.push(row);
  });
  return JSON.stringify({processed, bad, sample: sample.slice(0, 4),
                         total, rowCount: all.length});
})()"""


# 包装清单：每行一个「配件 ant-select（w120） + 数量 input + 加号」的子行，末行另带
# i.icon_cancel 删除。同一 td 里子行数不定，故按 .ant-select 逐个取、用它的
# parentElement 当子行容器。
#
# 【为什么靠搜索过滤定位选项、不滚虚拟列表】2026-08-27 实测：配件词表 171 项、
# rc-virtual-list 每屏约 8 行，逐屏滚动收集两次分别只收到 60 / 61 项且集合不同——
# 平台这个下拉滚动时会整段替换渲染项，本项目在类目/尺码分类那边验证过的「逐屏滚
# 180ms」在这里收不全。而它带 ant-select-show-search，输入关键词后结果一次渲染完
# （「上衣」→3 项、「裙」→10 项），故改成 setter 写 search input + 等选项出现。
#
# 【配件名不能保证与词表字面相同】LLM 给的是「上衣/半身裙」这类通用词，词表里是
# 「便服上衣/西装上衣/露腰上衣」。故先精确匹配、再退到 includes，两者都不中才报
# option-not-found 交上层重试（判断层已按真实词表提示过，见 judge_packing_list）。
_JS_FILL_PACKING = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setInp = (i, v) => { setter.call(i, String(v));
    i.dispatchEvent(new Event('input', {bubbles:true})); i.dispatchEvent(new Event('change', {bubbles:true})); };
  const ITEMS = __ITEMS__;   // [{name, qty}, ...]，件数和须等于 SKU分类 的数量

  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[1];
  if (!tb) return JSON.stringify({err: 'no-second-tbody'});

  // 按「有 ant-select 且有加号图标」认包装清单列，不硬编码列号（同项目 Sheet 判重约定）
  const packTdOf = tr => Array.from(tr.querySelectorAll('td')).find(td =>
    !!td.querySelector('.ant-select') && !!td.querySelector('i.icon_add_circle_outline'));

  const lineOf = td => Array.from(td.querySelectorAll('.ant-select')).map(s => {
    const box = s.parentElement;
    return {sel: s,
      qtyInp: box.querySelector('input:not(.ant-select-selection-search-input)'),
      plus: box.querySelector('i.icon_add_circle_outline'),
      minus: box.querySelector('i.icon_cancel')};
  });

  const pickAccessory = async (sel, name) => {
    const search = sel.querySelector('.ant-select-selection-search-input');
    if (!search) return {ok: false, reason: 'no-search-input'};
    const listId = search.getAttribute('aria-controls');
    const inner = sel.querySelector('.ant-select-selector') || sel;
    const curOf = () => { const x = sel.querySelector('.ant-select-selection-item');
      return x ? (x.title || x.textContent || '').trim() : ''; };
    if (curOf() === name) return {ok: true, source: 'already'};
    sel.scrollIntoView({block: 'center', behavior: 'instant'});
    await sleep(250);
    // 同 _JS_PICK_WAREHOUSE：按 aria-controls 的 listId 认自己那个浮层，判可见只看
    // inline display（隐藏浮层高度恒 0，按高度找会误判）
    const dropOf = () => {
      let ds = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''));
      const mine = ds.filter(d => d.querySelector('#' + listId));
      return (mine.length ? mine : ds).pop();
    };
    let drop = dropOf();
    if (!drop) {
      ['mousedown','mouseup','click'].forEach(t =>
        inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      for (let i = 0; i < 30 && !drop; i++) { await sleep(200); drop = dropOf(); }
    }
    if (!drop) return {ok: false, reason: 'dropdown-not-open'};
    setter.call(search, name);
    search.dispatchEvent(new Event('input', {bubbles: true}));
    let opt = null;
    for (let i = 0; i < 20; i++) {
      await sleep(200);
      drop = dropOf() || drop;
      const opts = Array.from(drop.querySelectorAll('.ant-select-item-option'))
        .filter(o => (o.textContent || '').trim() !== '请选择配件');
      // 精确同名优先；退到包含匹配时取【最短】的那个候选，不要搜索结果首项——
      // 2026-08-27 实测「上衣」搜出 西装上衣/便服上衣/露腰上衣，按首项会挑到「西装
      // 上衣」，而牛仔花苞上衣该归「便服上衣」。最短即限定词最少、最接近通用词。
      opt = opts.find(o => (o.textContent || '').trim() === name);
      if (!opt) {
        const hits = opts.filter(o => (o.textContent || '').trim().includes(name));
        hits.sort((a, b) => (a.textContent||'').trim().length - (b.textContent||'').trim().length);
        opt = hits[0];
      }
      if (opt) break;
    }
    if (!opt) return {ok: false, reason: 'option-not-found', name,
      seen: Array.from(drop.querySelectorAll('.ant-select-item-option'))
        .map(o => (o.textContent||'').trim()).slice(0, 10)};
    opt.click();
    await sleep(400);
    const got = curOf();
    return {ok: got === name || got.includes(name), got};
  };

  // 行区间（左闭右开）由 python 侧分批传入，见 _fill_rows_batched
  const START = __START__, END = __END__;
  const all = Array.from(tb.querySelectorAll('tr'));
  const rows = all.slice(START, END);
  let processed = 0;
  const failed = [];
  for (const tr of rows) {
    const td = packTdOf(tr);
    if (!td) continue;
    processed++;
    // 子行数对齐 ITEMS：不足点加号补，多余点末行取消删（首行没有取消图标，删不动就停）
    for (let g = 0; lineOf(td).length < ITEMS.length && g < 12; g++) {
      const ls = lineOf(td);
      ls[ls.length - 1].plus.click();
      await sleep(450);
    }
    for (let g = 0; lineOf(td).length > ITEMS.length && g < 12; g++) {
      const ls = lineOf(td);
      const last = ls[ls.length - 1];
      if (!last.minus) break;
      last.minus.click();
      await sleep(450);
    }
    const ls = lineOf(td);
    for (let i = 0; i < ITEMS.length && i < ls.length; i++) {
      const r = await pickAccessory(ls[i].sel, ITEMS[i].name);
      if (!r.ok) failed.push({row: processed, i, ...r});
      const q = lineOf(td)[i].qtyInp;
      if (q && q.value !== String(ITEMS[i].qty)) setInp(q, ITEMS[i].qty);
    }
  }
  await sleep(500);

  // 回读校验：只看本批那几行，要求子行数对上、配件名非占位、数量和等于要求的件数
  // total 回传整表包装清单行数，供 python 侧算下一批（末批据此收敛）
  const want = ITEMS.reduce((a, x) => a + Number(x.qty), 0);
  const bad = [], sample = [];
  let total = 0;
  Array.from(tb.querySelectorAll('tr')).forEach((tr, i) => {
    const td = packTdOf(tr);
    if (!td) return;
    total++;
    if (i < START || i >= END) return;
    const got = lineOf(td).map(l => {
      const x = l.sel.querySelector('.ant-select-selection-item');
      return {name: x ? (x.title || x.textContent || '').trim() : '',
              qty: l.qtyInp ? l.qtyInp.value : ''};
    });
    if (sample.length < 3) sample.push(got);
    const sum = got.reduce((a, x) => a + (Number(x.qty) || 0), 0);
    const blank = got.some(x => !x.name || x.name === '请选择配件' || !x.qty);
    if (got.length !== ITEMS.length || blank || sum !== want) bad.push({got, sum, want});
  });
  return JSON.stringify({processed, failed, bad, sample, want,
                         total, rowCount: all.length});
})()"""
