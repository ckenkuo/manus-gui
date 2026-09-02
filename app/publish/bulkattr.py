# -*- coding: utf-8 -*-
"""采集箱「全属性修改」批量填仓库 / 发货时效 / 运费模板。

【它补的是哪一段】数据搬家认领过来的草稿，仓库、发货时效、运费模板三项都是空的，而这
三项是发布前的硬性必填。逐条进编辑页填（pipeline.set_stock / set_shipping 那条路）
在批量场景下不划算：一条草稿开一次编辑页要十几秒，几十条就是十几分钟；而采集箱列表页
的「批量操作 → 全属性修改」能一次弹窗改完整批。故这里走列表页批量入口，
**不复用 pipeline 的编辑页函数**——两者的 DOM 与交互体系完全不同。

【与 pipeline.set_stock / set_shipping 的分工，别混】
    pipeline 那两个：单条草稿、在编辑页表单里填、能填全部细项（库存数量、SKU 分类等）
    本模块：      整批草稿、在列表页弹窗里填、只填三项且每项只取一个固定值
本模块刻意不做「填任意值」：批量入口的下拉是按店铺+站点分别渲染的（见下），
支持任意值意味着要让调用方给出「店铺×站点 → 值」的完整矩阵，那已经不叫批量了。
故取值口径是固定规则（2026-09-01 用户确认）：
    仓库     = 下拉第一项（DOM 顺序）
    发货时效 = **工作日数最大**的那项
    运费模板 = 下拉第一项（DOM 顺序）

【发货时效为什么按天数取最大而不是取末项】实测选项顺序是
「1、2、7、8、9、10、11、12、13、14 个工作日内发货」——**不连续**（缺 3~6），
说明这个列表是平台按店铺资质拼出来的，顺序不保证。用户要的是「时效最宽松」，
故解析文案里的数字取最大值，而不是依赖 DOM 顺序（顺序一变就静默选错）。
解析不出数字的选项（文案改版）不参与比较，全都解析不出时报错而不是瞎选一个。

【三个下拉都是按「店铺 / 站点」分行渲染的】弹窗右侧不是三个下拉，而是三张小表：
    仓库     表头「站点 | 仓库选择」，每个店铺一个分组、每个站点一行（多选下拉）
    发货时效 表头「站点 | 承诺发货时效」，每个站点一行（单选下拉）
    运费模板 表头「店铺 | 运费模板」，每个店铺一行（单选下拉）
故选中的草稿跨越 N 个店铺 M 个站点时，会有多个下拉要逐个填。本模块的做法是
**遍历弹窗里所有可见下拉**，逐个展开、按所属区块套用对应规则——而不是按下标写死。

【选项集合因站点而异，不许写死仓库名】实测哥伦比亚站是「飞特COL仓库 / 哥伦比亚-新势力」，
秘鲁站是「飞特PE仓库」。故只能运行时读选项，取「第一项」这个位置规则。

【勾选属性用 input.value，不用文案】弹窗左侧 31 个属性复选框每个都带稳定的
`input.value` 键名，本模块要的三项是：
    warehouse         仓库
    sendTimeLimit     发货时效
    shippingTemplate  运费模板
按文案匹配（「仓库」「发货时效」「运费模板」）在文案改版时会静默失配，且「仓库」二字
还与右侧面板标题重名。键名是平台自己的字段名，稳得多。

【行复选框与属性复选框都必须真实鼠标点击】2026-09-01 实测：JS `label.click()` 对采集箱
列表的行复选框与弹窗里的属性复选框**完全无效**（连 DOM 的 checked class 都不加），
表现是点「批量操作」后菜单项点了没反应、或弹窗右侧面板始终不出现。
与 banjia.py 同一条结论、同一套「打标 → mouse_click → 回读校验」处置。
反过来，下拉选项（.ant-select-item-option）JS click **有效**（实测），因为它不是
ant-checkbox 而是普通 option div。

【「批量操作」菜单是常驻隐藏 DOM，容器 class 是 .batchOperationDropdown】菜单项是
`div.menu-item`（**不是 `li`**，也不在 .ant-dropdown-menu 里）。2026-09-01 排查时先按
ant 的常规结构找了两轮全是空手，记在这里免得后来者再走一遍。
菜单要真实点击「批量操作」按钮才展开（JS click 展不开）。

【这是不可逆写动作】点「确定」会真实改掉整批草稿的这三项（覆盖已有值）。故：
  - 提供 dry_run（默认 True）：走完全部探测与选项读取，**不点确定**，返回打算填什么。
  - 只处理调用方明确给出的 rowids，不做「全选当前页」这种便捷操作。
"""
import asyncio
import re

from app.logger import logger
from app.publish.browser import DRAFT_LIST_URL, BrowserSession, ensure_cdp_alive

# 弹窗左侧属性复选框的 input.value 键名（平台自己的字段名，比文案稳）
ATTR_WAREHOUSE = "warehouse"
ATTR_SEND_TIME = "sendTimeLimit"
ATTR_FREIGHT = "shippingTemplate"

# 本模块处理的三项，顺序即勾选顺序。勾选顺序影响右侧面板的区块排列顺序，
# 但因为区块归属是按标题文本判的（见 _PANEL_KINDS），顺序变了也不会填错。
TARGET_ATTRS = (ATTR_WAREHOUSE, ATTR_SEND_TIME, ATTR_FREIGHT)

# 属性键名 → 中文名（只用于日志与返回给前端，不参与匹配）
ATTR_LABELS = {
    ATTR_WAREHOUSE: "仓库",
    ATTR_SEND_TIME: "发货时效",
    ATTR_FREIGHT: "运费模板",
}

# 右侧面板区块的识别关键词 → 取值规则。
# 【为什么按文本判区块而不按下标】三项是否都勾、勾的顺序、每项有几行下拉，都随选中的
# 草稿而变（跨 N 个店铺 M 个站点就有 N+M+… 个下拉）；按下标就要在 Python 侧重建一套
# 「第几个下拉属于哪一项」的推算，而弹窗里现成就有区块标题。
#
# 【判据用「所在表的表头」，不用祖先文本】2026-09-01 第一版用「往上找含关键词的祖先」，
# 结果仓库那两个下拉恒判不出（owner 空串、被跳过）：三个区块的 DOM 深度差很多——
# 运费模板的关键词在第 6 层祖先、发货时效在第 7 层、而**仓库要到第 8 层**才出现
# （它的表是 vxe-grid，中间多套了 body-wrapper / main-wrapper / render-wrapper 三层）。
# 层数上限调大又会撞上另一头：层数够深时祖先文本已经把三个区块全包进去了
# （k9 的 modal-content-container 含全部 31 个属性名 + 三个区块），关键词全部命中、
# 谁先匹配算谁，等于抽签。
# 稳定的判据是**表头**：三张表的表头是平台写死的三组文案，且各表互不重叠：
#     仓库     ['站点', '仓库选择']
#     发货时效 ['站点', '承诺发货时效']
#     运费模板 ['店铺', '运费模板']
# 表头拿不到时（vxe 偶发只渲染 body）才退回祖先文本，取**最近的**那一个匹配。
_PANEL_KINDS = (
    # (关键词正则, 规则键, 中文名) —— 顺序即优先级：更具体的排前面
    (r"仓库选择", "first", "仓库"),
    (r"承诺发货时效", "max_workdays", "发货时效"),
    (r"运费模板", "first", "运费模板"),
    # 兜底：只出现「仓库」二字（表头没渲染出来、只有祖先文本时）
    (r"仓库", "first", "仓库"),
)

# 弹窗标题识别文本：实测是「修改属性」（不是菜单项文案「全属性修改」）
MODAL_TITLE = "修改属性"

# 「批量操作」下拉菜单的容器与目标菜单项文案
BULK_DROPDOWN = ".batchOperationDropdown"
MENU_ITEM = "全属性修改"


# ---- 列表页：勾行 + 开菜单 ----------------------------------------------------
# 采集箱列表页也是 vxe-table（与数据搬家页同）：行是 .vxe-body--row、主键在 rowid 属性。
_JS_ROWS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('.vxe-body--row'));
  return JSON.stringify({rows: rows.map(r => ({
    rowid: r.getAttribute('rowid') || '',
    checked: !!r.querySelector('label.ant-checkbox-wrapper-checked'),
  })), total: rows.length});
})()"""

# 【打标前先清旧标记】不清的话 DOM 上会同时存在多个标记，选择器 .first 按 DOM 顺序取到
# 的是上一次那个，于是「勾选未生效」且报错完全指不到根因（claim.py 记过这条教训）。
_JS_TAG_ROW = r"""((rowid) => {
  document.querySelectorAll('[data-ba-row]').forEach(e => e.removeAttribute('data-ba-row'));
  const r = Array.from(document.querySelectorAll('.vxe-body--row'))
    .find(x => x.getAttribute('rowid') === rowid);
  if (!r) return {found: false};
  const lb = r.querySelector('.col--checkbox label.ant-checkbox-wrapper')
          || r.querySelector('label.ant-checkbox-wrapper');
  if (!lb) return {found: false, reason: 'no-checkbox'};
  lb.setAttribute('data-ba-row', '1');
  lb.scrollIntoView({block: 'center'});
  return {found: true, checked: lb.className.includes('ant-checkbox-wrapper-checked')};
})"""

_JS_ROW_CHECKED = r"""((rowid) => {
  const r = Array.from(document.querySelectorAll('.vxe-body--row'))
    .find(x => x.getAttribute('rowid') === rowid);
  if (!r) return {checked: false, reason: 'row-gone'};
  return {checked: !!r.querySelector('label.ant-checkbox-wrapper-checked')};
})"""

_JS_NEXT_PAGE = r"""(() => {
  const btn = document.querySelector('.vxe-pager--next-btn');
  if (!btn) return JSON.stringify({clicked: false, reason: 'no-pager'});
  if (btn.className.includes('is--disabled'))
    return JSON.stringify({clicked: false, reason: 'last-page'});
  btn.click();
  return JSON.stringify({clicked: true});
})()"""

# 菜单项打标。菜单是常驻隐藏 DOM，故点「批量操作」之前也能打上标，
# 但点击必须在菜单展开之后（隐藏元素上 mouse_click 会因不可见而超时）。
_JS_TAG_MENU_ITEM = r"""((label) => {
  document.querySelectorAll('[data-ba-item]').forEach(e => e.removeAttribute('data-ba-item'));
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const dd = document.querySelector('__DROPDOWN__');
  if (!dd) return {found: false, reason: 'no-dropdown'};
  const it = Array.from(dd.querySelectorAll('.menu-item')).find(e => t(e) === label);
  if (!it) return {found: false,
                   avail: Array.from(dd.querySelectorAll('.menu-item')).map(t)};
  it.setAttribute('data-ba-item', '1');
  return {found: true, visible: it.offsetParent !== null};
})""".replace("__DROPDOWN__", BULK_DROPDOWN)


# ---- 弹窗：勾属性 + 读面板 ----------------------------------------------------
_JS_MODAL_OPEN = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return JSON.stringify({open: false});
  const cbs = Array.from(m.querySelectorAll('input.ant-checkbox-input')).map(i => i.value);
  return JSON.stringify({open: true, attrs: cbs});
})()""".replace("__TITLE__", MODAL_TITLE)

_JS_TAG_ATTR = r"""((val) => {
  document.querySelectorAll('[data-ba-attr]').forEach(e => e.removeAttribute('data-ba-attr'));
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return {found: false, reason: 'no-modal'};
  const inp = Array.from(m.querySelectorAll('input.ant-checkbox-input'))
    .find(i => i.value === val);
  if (!inp) return {found: false,
                    avail: Array.from(m.querySelectorAll('input.ant-checkbox-input')).map(i => i.value)};
  const lb = inp.closest('label.ant-checkbox-wrapper');
  if (!lb) return {found: false, reason: 'no-label'};
  lb.setAttribute('data-ba-attr', '1');
  lb.scrollIntoView({block: 'center'});
  return {found: true, txt: t(lb).slice(0, 20),
          checked: lb.className.includes('ant-checkbox-wrapper-checked')};
})""".replace("__TITLE__", MODAL_TITLE)

_JS_ATTR_CHECKED = r"""((val) => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return {checked: false, reason: 'no-modal'};
  const inp = Array.from(m.querySelectorAll('input.ant-checkbox-input'))
    .find(i => i.value === val);
  if (!inp) return {checked: false, reason: 'attr-not-found'};
  const lb = inp.closest('label.ant-checkbox-wrapper');
  return {checked: !!lb && lb.className.includes('ant-checkbox-wrapper-checked')};
})""".replace("__TITLE__", MODAL_TITLE)

# 枚举右侧面板里的下拉，并给每个打上 data-ba-sel 序号供后续定位。
# owner 是往上找到的最近「含区块关键词的祖先文本」，用来判这个下拉属于哪一项。
# 【为什么顺带打序号】ant-select 没有稳定 id，而后续要逐个展开它们；用序号属性定位
# 比「第 N 个 .ant-select」稳——中途某个下拉被填上值后 DOM 可能重排。
_JS_SCAN_SELECTS = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return JSON.stringify({open: false});
  document.querySelectorAll('[data-ba-sel]').forEach(e => e.removeAttribute('data-ba-sel'));
  const sels = Array.from(m.querySelectorAll('.ant-select'))
    .filter(s => s.offsetParent !== null);
  return JSON.stringify({open: true, selects: sels.map((s, i) => {
    s.setAttribute('data-ba-sel', String(i));
    // 【首选判据：所在表的表头】三张表的表头互不重叠，是最稳的区块归属依据
    // （祖先文本在深度上不可比，见 _PANEL_KINDS 上方的实测记录）。
    // 仓库/运费模板的表是 vxe-grid（表头在 .vxe-header--column），发货时效的是
    // 原生 table（表头在 th）——两种都收。
    const tb = s.closest('.vxe-grid, table');
    let heads = [];
    if (tb) {
      heads = Array.from(tb.querySelectorAll('.vxe-header--column, th'))
        .map(t).filter(x => x);
    }
    // 兜底判据：往上找**最近的**含关键词的祖先，但层数放宽到 10 且限制文本长度——
    // 太深的祖先会把三个区块全包进去（k9 起就含全部 31 个属性名），那时关键词全命中、
    // 谁先匹配算谁，等于抽签，故 len 上限压到 260。
    let owner = '';
    let p = s.parentElement;
    for (let k = 0; k < 10 && p; k++) {
      const txt = t(p);
      if (/仓库选择|承诺发货时效|运费模板|仓库/.test(txt) && txt.length < 260) {
        owner = txt.slice(0, 240); break;
      }
      p = p.parentElement;
    }
    // 同一行里的「站点/店铺」名：取所在 tr 的首格文本，供日志说清填的是哪一行
    const tr = s.closest('tr');
    const rowName = tr ? t(tr.querySelector('td') || {textContent: ''}).slice(0, 40) : '';
    return {idx: i, owner: owner, heads: heads, rowName: rowName,
            cur: t(s).slice(0, 60),
            multiple: s.className.includes('ant-select-multiple'),
            disabled: s.className.includes('ant-select-disabled'),
            // 已有值判据：ant 在有值时会挂 selection-item；「请选择」是 placeholder
            hasValue: !!s.querySelector('.ant-select-selection-item')};
  })});
})()""".replace("__TITLE__", MODAL_TITLE)

# 读某个已展开下拉的选项。
# 【必须用 aria-owns 关联，不能取「唯一可见的 dropdown」】弹窗里有多个下拉，前一个的
# 浮层可能还在退场动画中未销毁，取「第一个可见的」会读到上一个下拉的选项（于是把
# 仓库名填进运费模板）。ant 在 input 上挂 aria-owns 指向浮层列表的 id，这是唯一可靠的关联。
# 读某个下拉的全部选项。
# 【必须滚动虚拟列表，不能静态扫 DOM】2026-09-01 用户发现「发货时效没滚到最迟的时效」，
# 查证正是这个坑：ant 的选项用 rc-virtual-list，**只渲染可视窗口约 10 条**。
# 首轮真站读到的恰好是 10 条（1、2、7、8、9、10、11、12、13、14 个工作日），
# 于是 max_workdays 在这 10 条里取最大得到「14」——看着像最大值，其实只是**首屏末条**，
# 后面滚下去还可能有更长的时效。这类静默截断是本项目记过多次的一类错
# （见 pipeline._read_attr_options 的坑3、以及属性缓存「截断的 options 拒收」）。
#
# 滚动参数照抄 pipeline._read_attr_options 的实测值（2026-08-19 定的 180ms/屏）：
# 那边验证过 Playwright 直连下 120ms 太短会只采到首屏。不另起一套参数，
# 免得两处各自漂移。
# 终止条件用「滚到底」而非「scrollTop 不再变」：后者在惯性滚动未结束时会提前判定。
#
# 返回带 virtual / scrolledToEnd：**没滚到底的清单是截断的**，调用方据此拒绝按它取值
# （宁可跳过也不要拿半份清单去挑「最大值」——那正是这个 bug 的成因）。
_JS_OPTIONS = r"""(async (idx) => {
  const sleep = ms => new Promise(res => setTimeout(res, ms));
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const s = document.querySelector('[data-ba-sel="' + idx + '"]');
  if (!s) return JSON.stringify({ok: false, reason: 'no-select'});
  const inp = s.querySelector('input');
  const owns = inp ? (inp.getAttribute('aria-owns') || inp.getAttribute('aria-controls') || '') : '';
  let dd = owns ? document.getElementById(owns.split(' ')[0]) : null;
  if (dd) dd = dd.closest('.ant-select-dropdown') || dd;
  if (!dd) return JSON.stringify({ok: false, reason: 'no-dropdown'});
  if (dd.className.includes('ant-select-dropdown-hidden'))
    return JSON.stringify({ok: false, reason: 'dropdown-hidden'});

  // 按 DOM 顺序累积，去重靠文案（同一条目滚动中会被反复渲染）
  const seen = [];
  const collect = () => {
    Array.from(dd.querySelectorAll('.ant-select-item-option')).forEach(o => {
      const txt = t(o).slice(0, 80);
      if (!txt) return;
      if (seen.some(x => x.txt === txt)) return;
      seen.push({txt: txt, val: o.getAttribute('title') || '',
                 selected: o.className.includes('ant-select-item-option-selected'),
                 disabled: o.className.includes('ant-select-item-option-disabled')});
    });
  };

  const holder = dd.querySelector('.rc-virtual-list-holder');
  if (!holder) {
    // 短列表没有虚拟滚动容器，一次收全（天然完整）
    collect();
    return JSON.stringify({ok: true, empty: /暂无数据|No Data/.test(t(dd)),
      n: seen.length, opts: seen.map((o, i) => ({i: i, ...o})),
      virtual: false, scrolledToEnd: true});
  }
  holder.scrollTop = 0;
  await sleep(200);
  collect();
  for (let k = 0; k < 40; k++) {
    holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
    await sleep(180);
    collect();
    if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
  }
  holder.scrollTop = holder.scrollHeight;   // 末屏可能不足一屏高，补一次
  await sleep(250);
  collect();
  const atEnd = holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2;
  // 【回到顶部】点选项那一步要按 index 在当前渲染窗口里找目标；停在底部会让
  // 前几项不在 DOM 里（虚拟列表只渲染窗口内的），于是「第一项」点不中。
  holder.scrollTop = 0;
  await sleep(200);
  return JSON.stringify({ok: true, empty: /暂无数据|No Data/.test(t(dd)),
    n: seen.length, opts: seen.map((o, i) => ({i: i, ...o})),
    virtual: true, scrolledToEnd: atEnd,
    scrollHeight: holder.scrollHeight, clientHeight: holder.clientHeight});
})"""

# 点某个下拉的第 i 个选项。
# 【选项 JS click 有效】它是普通 div（.ant-select-item-option），不是 ant-checkbox，
# 故不必真实点击（实测有效）。这与本模块里复选框的结论相反，别顺手统一。
# 点某个选项。
# 【按文案找，不按下标找】虚拟列表只渲染窗口内的条目，而 _JS_OPTIONS 交回的清单是
# **滚完整个列表**累积出来的——两者的下标不是一回事：清单里第 20 项在 DOM 里可能压根
# 没渲染，`opts[20]` 会取到别的条目甚至 undefined（静默点错值，比报错坏得多）。
# 故这里改成按文案精确匹配，且**找不到就滚动去找**（照 pipeline._scroll_click_option
# 的同一取向）。
_JS_PICK_OPTION = r"""(async (args) => {
  const [idx, wantTxt] = args;
  const sleep = ms => new Promise(res => setTimeout(res, ms));
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const s = document.querySelector('[data-ba-sel="' + idx + '"]');
  if (!s) return JSON.stringify({ok: false, reason: 'no-select'});
  const inp = s.querySelector('input');
  const owns = inp ? (inp.getAttribute('aria-owns') || inp.getAttribute('aria-controls') || '') : '';
  let dd = owns ? document.getElementById(owns.split(' ')[0]) : null;
  if (dd) dd = dd.closest('.ant-select-dropdown') || dd;
  if (!dd) return JSON.stringify({ok: false, reason: 'no-dropdown'});

  const find = () => Array.from(dd.querySelectorAll('.ant-select-item-option'))
    .find(o => t(o).slice(0, 80) === wantTxt);
  let o = find();
  if (o) { o.click(); return JSON.stringify({ok: true, txt: t(o).slice(0, 80),
                                             scrolled: false}); }

  // 不在当前渲染窗口里：从头滚一遍找它
  const holder = dd.querySelector('.rc-virtual-list-holder');
  if (!holder) return JSON.stringify({ok: false, reason: 'option-not-found',
    avail: Array.from(dd.querySelectorAll('.ant-select-item-option'))
      .map(x => t(x).slice(0, 40)).slice(0, 20)});
  holder.scrollTop = 0;
  await sleep(200);
  for (let k = 0; k < 40; k++) {
    o = find();
    if (o) {
      o.scrollIntoView({block: 'center'});
      await sleep(150);
      // scrollIntoView 后节点可能被虚拟列表回收重建，重新取一次再点
      o = find();
      if (o) { o.click();
        return JSON.stringify({ok: true, txt: t(o).slice(0, 80), scrolled: true}); }
    }
    if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
    holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
    await sleep(180);
  }
  return JSON.stringify({ok: false, reason: 'option-not-rendered', want: wantTxt});
})"""

# 回读某个下拉当前显示的值（校验「填上了没有」）
_JS_SEL_VALUE = r"""((idx) => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const s = document.querySelector('[data-ba-sel="' + idx + '"]');
  if (!s) return {ok: false, reason: 'no-select'};
  const items = Array.from(s.querySelectorAll('.ant-select-selection-item'))
    .map(e => t(e).slice(0, 60));
  return {ok: true, hasValue: items.length > 0, items: items, txt: t(s).slice(0, 80)};
})"""

_JS_CONFIRM = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const m = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.offsetParent !== null && /__TITLE__/.test(t(x)))[0];
  if (!m) return JSON.stringify({found: false, reason: 'no-modal'});
  const btn = Array.from(m.querySelectorAll('button'))
    .find(b => (b.textContent||'').trim() === '确定');
  if (!btn) return JSON.stringify({found: false, reason: 'no-confirm-btn'});
  btn.click();
  return JSON.stringify({found: true});
})()""".replace("__TITLE__", MODAL_TITLE)

_JS_CLOSE = r"""(() => {
  let n = 0;
  Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => m.offsetParent !== null).forEach(m => {
      const c = Array.from(m.querySelectorAll('button'))
        .find(b => /^(取消|关闭)$/.test((b.textContent||'').trim()));
      if (c) { c.click(); n++; return; }
      const x = m.querySelector('.ant-modal-close');
      if (x) { x.click(); n++; }
    });
  return JSON.stringify({closed: n});
})()"""


def _kind_of(heads: list, owner: str = "") -> tuple:
    """判下拉属于哪一项，返回 (规则键, 中文名)。判不出返回 ("", "")。

    【先看表头，再退回祖先文本】表头是三组互不重叠的固定文案，是稳定判据；祖先文本
    只在表头没渲染出来时兜底（详见 _PANEL_KINDS 上方的实测记录：三个区块的 DOM 深度
    差 2 层，靠祖先文本判会让仓库恒判不出，放宽层数又会三个全命中）。

    判不出时调用方跳过该下拉并记 warning，不瞎填——填错一项（比如把仓库名填进运费模板）
    比漏填一项难查得多。
    """
    head_txt = " ".join(str(h) for h in (heads or []))
    for src in (head_txt, owner or ""):
        if not src:
            continue
        for pat, rule, name in _PANEL_KINDS:
            if re.search(pat, src):
                return rule, name
    return "", ""


def _workdays(text: str):
    """从「N个工作日内发货」抽出 N；抽不到返回 None（不参与取最大值的比较）。"""
    m = re.search(r"(\d+)\s*个?\s*工作日", text or "")
    if m:
        return int(m.group(1))
    # 兜底：文案改版但仍带数字时，取第一个数字（比整条丢掉好）
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def _pick_option(rule: str, opts: list) -> dict:
    """按规则从选项里挑一个，返回 {"i", "txt"}；挑不出抛 ValueError。

    可选项先排掉 disabled 的——平台会把不适用的选项置灰，点了不生效还静默。
    """
    usable = [o for o in opts if not o.get("disabled")]
    if not usable:
        raise ValueError("下拉没有可选项（全部置灰或为空）")
    if rule == "first":
        # 「第一项」＝ DOM 顺序的首个可选项（2026-09-01 用户确认的口径）
        o = usable[0]
        return {"i": o["i"], "txt": o.get("txt") or ""}
    if rule == "max_workdays":
        # 「时效最宽松」＝ 工作日数最大。**不取 DOM 末项**：实测选项顺序不连续
        # （1、2、7、8…14），顺序不保证，靠位置取会静默选错（见模块 docstring）。
        scored = [(o, _workdays(o.get("txt") or "")) for o in usable]
        scored = [(o, n) for o, n in scored if n is not None]
        if not scored:
            raise ValueError(
                f"发货时效选项里一个都解析不出工作日数（文案可能改版）："
                f"{[o.get('txt') for o in usable][:6]}")
        o, n = max(scored, key=lambda x: x[1])
        return {"i": o["i"], "txt": o.get("txt") or "", "workdays": n}
    raise ValueError(f"未知取值规则：{rule!r}")


async def _trusted_toggle(session: BrowserSession, tag_js: str, verify_js: str,
                          selector: str, arg, want: bool = True,
                          retries: int = 3) -> bool:
    """打标 → 真实鼠标点击 → 回读 class 校验，未生效时重试。

    【为什么必须真实点击】列表行复选框与弹窗里的属性复选框都是 ant-checkbox + Vue，
    JS `label.click()` 对它们完全无效（2026-09-01 实测，连 DOM 的 checked class 都不加）。
    与 banjia.py 的同名函数同一结论；两处各存一份是因为两个模块的 JS 片段与标记名不同，
    共用一个会把两边的标记属性绑在一起（一边改标记名另一边静默失配）。
    """
    for attempt in range(1, retries + 1):
        tag = await session.eval_json(tag_js, arg=arg)
        if not tag.get("found"):
            return False
        if bool(tag.get("checked")) == want:
            return True
        r = await session.mouse_click(selector)
        if not r.get("ok"):
            logger.warning(f"真实点击失败（{attempt}/{retries}）：{r.get('err')}")
            await asyncio.sleep(1.5 * attempt)
            continue
        await asyncio.sleep(1)
        chk = await session.eval_json(verify_js, arg=arg)
        if bool(chk.get("checked")) == want:
            return True
        logger.warning(f"勾选未生效（想要 checked={want}），重试 {attempt}/{retries}")
    return False


async def _check_rows(session: BrowserSession, rowids: list,
                      max_pages: int = 20) -> dict:
    """在采集箱列表里逐个勾选目标 rowid（真实鼠标点击），跨页查找。

    返回 {"checked": [...], "missing": [...]}。逻辑与 banjia._check_rows 同构
    （两个列表页都是 vxe-table），但标记属性与选择器各自一份，理由同 _trusted_toggle。
    """
    want = [r for r in rowids if r]
    checked: list = []
    for page_i in range(1, max_pages + 1):
        cur = await session.eval_json(_JS_ROWS)
        have = {r.get("rowid") for r in (cur.get("rows") or [])}
        for rid in [r for r in want if r not in checked and r in have]:
            ok = await _trusted_toggle(
                session, _JS_TAG_ROW, _JS_ROW_CHECKED,
                'label[data-ba-row="1"]', arg=rid, want=True)
            if ok:
                checked.append(rid)
            else:
                logger.warning(f"行 {rid} 勾选未生效，跳过（本批将少改这一条）")
        if len(checked) >= len(want):
            break
        nxt = await session.eval_json(_JS_NEXT_PAGE)
        if not nxt.get("clicked"):
            break
        await asyncio.sleep(3)  # 翻页后表格重渲染
        logger.info(f"翻到第 {page_i + 1} 页继续找剩余 {len(want) - len(checked)} 条")
    missing = [r for r in want if r not in checked]
    if missing:
        logger.warning(f"有 {len(missing)} 条在采集箱列表里没找到"
                       f"（可能已移入待发布）：{missing[:5]}")
    return {"checked": checked, "missing": missing}


async def _open_modal(session: BrowserSession) -> dict:
    """点开「批量操作 → 全属性修改」，返回弹窗现状 {"open", "attrs"}。

    【「批量操作」按钮要真实点击才展开菜单】JS click 展不开（实测）。菜单本身是常驻
    隐藏 DOM（.batchOperationDropdown），展开后菜单项才可见、才能被 mouse_click 命中。
    """
    try:
        await session.page.locator('button:has-text("批量操作")').first.click(timeout=15000)
    except Exception as e:
        raise RuntimeError(f"点击「批量操作」失败：{e}")
    await asyncio.sleep(2)

    tag = await session.eval_json(_JS_TAG_MENU_ITEM, arg=MENU_ITEM)
    if not tag.get("found"):
        raise RuntimeError(
            f"批量操作菜单里没有「{MENU_ITEM}」，可选：{(tag.get('avail') or [])[:20]}")
    if not tag.get("visible"):
        raise RuntimeError("批量操作菜单未展开（菜单项不可见），无法点击菜单项")
    r = await session.mouse_click('[data-ba-item="1"]')
    if not r.get("ok"):
        raise RuntimeError(f"点击「{MENU_ITEM}」失败：{r.get('err')}")

    # 弹窗要拉店铺/仓库/模板等接口，慢的时候要十几秒
    data = await session.wait_for(_JS_MODAL_OPEN, lambda d: d.get("open"),
                                 timeout=90, interval=2)
    if not data.get("open"):
        raise RuntimeError("「全属性修改」弹窗未打开（店小秘接口可能异常，稍后重试）")
    return data


# 某个下拉「点得中吗」：拿它的中心点做 elementFromPoint，命中的是不是它自己。
# 【为什么用命中测试而不是数「有几个浮层开着」】2026-09-01 实测，ant 收起浮层后
# **不会**加 ant-select-dropdown-hidden、也不从 DOM 移除（它靠内联 style 与动画隐藏），
# 故「未 hidden 的 .ant-select-dropdown 个数」在收起后仍是 1，拿它当判据会：
#   - 收起成功也判成失败，白跑一轮 Escape；
#   - 一开始就误判「有浮层」，多点一次反而把本来关着的下拉点开了。
# 命中测试直接回答我们真正关心的问题：这个下拉现在点得中吗。
_JS_HITTABLE = r"""((idx) => {
  const s = document.querySelector('[data-ba-sel="' + idx + '"]');
  if (!s) return {ok: false, reason: 'no-select'};
  s.scrollIntoView({block: 'center'});
  const r = s.getBoundingClientRect();
  const e = document.elementFromPoint(Math.round(r.left + r.width / 2),
                                     Math.round(r.top + r.height / 2));
  return {ok: true, hittable: !!(e && s.contains(e)),
          blocker: e ? (e.tagName + '.' + String(e.className).slice(0, 50)) : ''};
})"""


# 强制隐藏「挡住目标下拉」的残留浮层（_clear_overlay 的最后手段）。
# 【只处理真的与目标相交的浮层】不分青红皂白把所有 .ant-select-dropdown 隐藏掉会连
# 目标自己将要展开的那个浮层一起关（它们复用同一批容器），于是下一步读不到选项。
# 故先算目标的矩形，只对与它相交的浮层加 hidden 类 + pointer-events:none。
# 【不碰表单值】被隐藏的浮层其选项已经选完，这只是让没退场的空壳不再拦截点击。
_JS_FORCE_HIDE_DROPDOWNS = r"""((idx) => {
  const s = document.querySelector('[data-ba-sel="' + idx + '"]');
  if (!s) return {ok: false, reason: 'no-select'};
  const t = s.getBoundingClientRect();
  const overlap = (r) => !(r.bottom < t.top || r.top > t.bottom
                        || r.right < t.left || r.left > t.right);
  let hidden = 0;
  const kept = [];
  document.querySelectorAll('.ant-select-dropdown').forEach(d => {
    // 目标自己的浮层不能碰：aria-owns/ariaControls 关联的那个是它将要展开的
    const inp = s.querySelector('input');
    const owns = inp ? (inp.getAttribute('aria-owns')
                        || inp.getAttribute('aria-controls') || '') : '';
    if (owns && d.id && owns.split(' ').includes(d.id)) { kept.push(d.id); return; }
    if (d.contains(s)) { kept.push('contains-target'); return; }
    const r = d.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;   // 已经不占位的不用管
    if (!overlap(r)) return;                        // 没挡住目标的不动
    d.classList.add('ant-select-dropdown-hidden');
    d.style.pointerEvents = 'none';
    hidden++;
  });
  return {ok: true, hidden: hidden, kept: kept};
})"""


async def _clear_overlay(session: BrowserSession, target_idx: int,
                         last_open=None, attempts: int = 3) -> bool:
    """清掉挡住 target_idx 的残留浮层，直到它点得中（或试完放弃）。

    【判据是「target 点不点得中」这个事实，不是「我记得开着哪个浮层」】2026-09-01 真站
    实测：单选下拉（发货时效）点完选项后，ant 的浮层**仍留在原位挡住下一个下拉**，
    而调用方那时已经把 last_open 置空了——按记账去收就什么都不收，运费模板照旧被
    判「被遮挡」跳过、发布检测随后报「半托管仓库不能为空」。

    手段按实测有效性依次升级：
      1. 点回 last_open 自己（ant 的 toggle 语义，对多选下拉是唯一有效的，见下）
      2. Escape（对单选下拉有效）
      3. 点弹窗标题栏空白处（把焦点移出浮层）
      4. 直接给残留浮层加 ant-select-dropdown-hidden（最后手段，见下）
    每一步之后都用命中测试确认，通了就返回。

    【第 4 步为什么允许直接改 DOM】前三步都是「模拟用户操作让 ant 自己收」，属于正道；
    但实测存在收不掉的情形，而这时的选择只有两个：改 DOM，或者漏填一个必填项
    （漏填的后果是发布检测直接判不通过，整条管线白跑）。加 hidden 类只影响这个**残留**
    浮层的可见性，不碰任何表单值——它的选项已经选完了，浮层本身只是没退场的空壳。
    """
    async def _hittable() -> bool:
        try:
            hit = await session.eval_json(_JS_HITTABLE, arg=target_idx)
            return bool(hit.get("hittable")) or not hit.get("ok")
        except Exception:
            return False

    async def _click_prev():
        if last_open is None:
            raise RuntimeError("没有记录上一个展开的下拉")
        await session.page.locator('[data-ba-sel="%d"]' % last_open).first.click(timeout=6000)

    async def _escape():
        await session.page.keyboard.press("Escape")

    async def _click_title():
        await session.page.locator(".ant-modal-title").first.click(timeout=5000)

    async def _force_hide():
        r = await session.eval_json(_JS_FORCE_HIDE_DROPDOWNS, arg=target_idx)
        logger.warning(f"用强制隐藏清掉挡住 #{target_idx} 的残留浮层："
                       f"处理了 {r.get('hidden')} 个")

    ways = [("点回上一个下拉", _click_prev), ("Escape", _escape),
            ("点弹窗标题", _click_title), ("强制隐藏残留浮层", _force_hide)]
    for attempt in range(1, attempts + 1):
        for how, action in ways:
            try:
                await action()
            except Exception as e:
                logger.debug(f"清浮层用「{how}」没能执行：{e}")
                continue
            await asyncio.sleep(0.8)
            if await _hittable():
                logger.info(f"清掉挡住 #{target_idx} 的浮层：用「{how}」"
                            f"（第 {attempt} 轮）")
                return True
        if attempt < attempts:
            await asyncio.sleep(1.0)
    logger.warning(f"#{target_idx} 始终被浮层挡住，清不掉")
    return False


async def _close_dropdown(session: BrowserSession, idx=None,
                          verify_idx=None, attempts: int = 3) -> bool:
    """收起 idx 那个下拉的浮层。idx 为 None 时什么都不做。

    verify_idx 给了就用它做命中测试来**确认真收掉了**，并在没收掉时换手段重试；
    不给就只点一次（老语义，best-effort）。返回是否确认收起（无 verify_idx 时返回 True）。

    【为什么必须靠「再点一次那个下拉」，Escape 与 blur 都不行】2026-09-01 实测，
    仓库那个**多选**下拉的浮层里带一个「搜索仓库」输入框，ant 把焦点锁在浮层内：
        Escape                                    浮层不动
        activeElement.blur() + 点标题栏 mousedown  浮层不动
        点弹窗标题栏（真实点击）                    浮层不动
        点回它自己                                 **有效**（ant 的 toggle 语义）
    而它的位置（实测 y 373~516）正好盖住仓库表**下一行**的下拉（y 385~417），于是
    下一行的 click 撞到「<input class=ant-input> intercepts pointer events」超时被跳过
    ——表现是「仓库有两个站点却只填了第一个」，报错信息完全指不到根因。

    【为什么要加「确认 + 换手段重试」】2026-09-01 真站跑完整管线时，**发货时效**
    （单选）的浮层在点完选项后没收干净，盖住了下一个「运费模板」下拉，于是运费模板
    被判「被遮挡」跳过——一次 click 收不掉就放弃，等于把一个可恢复的时序问题
    变成漏填。故这里改成：点一次 → 用下一个下拉的命中测试确认 → 没通就换手段
    （Escape / 点弹窗标题 / 再点一次）继续试，试完还不行才交给调用方跳过。

    收不掉不抛：那一行会走「被遮挡」分支被跳过并如实报出来，不会静默填错。
    """
    if idx is None:
        return True

    async def _hittable() -> bool:
        """verify_idx 那个下拉现在点得中吗（没给就不判，直接算通过）。"""
        if verify_idx is None:
            return True
        try:
            hit = await session.eval_json(_JS_HITTABLE, arg=verify_idx)
            # ok=False（找不到那个下拉）不算「被挡」，交给调用方自己处理
            return bool(hit.get("hittable")) or not hit.get("ok")
        except Exception:
            return False

    # 手段按实测有效性排序：点回自己最可靠（ant 的 toggle 语义），
    # 其余是给「点不回去（浮层把它自己也盖住了）」留的退路。
    async def _click_self():
        await session.page.locator('[data-ba-sel="%d"]' % idx).first.click(timeout=6000)

    async def _escape():
        await session.page.keyboard.press("Escape")

    async def _click_title():
        await session.page.locator(".ant-modal-title").first.click(timeout=5000)

    ways = [("点回自己", _click_self), ("Escape", _escape), ("点弹窗标题", _click_title)]
    for attempt in range(1, attempts + 1):
        for how, action in ways:
            try:
                await action()
            except Exception as e:
                logger.debug(f"收起下拉 #{idx} 用「{how}」失败：{e}")
                continue
            await asyncio.sleep(0.8)
            if await _hittable():
                if attempt > 1 or how != "点回自己":
                    logger.info(f"收起下拉 #{idx} 用「{how}」成功"
                                f"（第 {attempt} 轮）")
                return True
        if attempt < attempts:
            logger.warning(f"下拉 #{idx} 的浮层没收掉，换一轮重试"
                           f"（{attempt}/{attempts}）")
            await asyncio.sleep(1.0)
    if verify_idx is not None:
        logger.warning(f"下拉 #{idx} 的浮层始终收不掉，#{verify_idx} 仍被遮挡")
    return False


async def _fill_selects(session: BrowserSession, dry_run: bool,
                        on_log=None) -> dict:
    """遍历弹窗右侧所有下拉，按所属区块的规则各选一个值。

    返回 {"filled": [{kind, rowName, picked, ...}], "skipped": [...]}。
    dry_run=True 时只读选项、算出该选谁，**不点选项也不点确定**。

    【为什么逐个展开而不批量】ant-select 的选项是展开时才渲染的（懒加载浮层），
    不展开就读不到选项集合。而选项集合因店铺/站点而异（见模块 docstring），
    没有「读一次套用全部」的可能。
    """
    scan = await session.eval_json(_JS_SCAN_SELECTS)
    if not scan.get("open"):
        raise RuntimeError("读弹窗下拉失败：弹窗已不在（可能被别的操作关掉了）")
    selects = scan.get("selects") or []
    if not selects:
        raise RuntimeError("弹窗右侧没有任何下拉：三项属性可能都没勾上")

    filled: list = []
    skipped: list = []
    # 上一个展开过的下拉序号：收起浮层只能靠「再点回它自己」（Escape/blur 对多选浮层
    # 无效，见 _close_dropdown），故必须记住是谁开的。
    last_open = None
    for s in selects:
        idx = s.get("idx")
        rule, name = _kind_of(s.get("heads") or [], s.get("owner") or "")
        if not rule:
            skipped.append({"idx": idx, "reason": "认不出所属区块",
                            "heads": s.get("heads") or [],
                            "owner": (s.get("owner") or "")[:80]})
            logger.warning(f"下拉 #{idx} 认不出属于哪一项，跳过（不瞎填）："
                           f"表头 {s.get('heads')} / {(s.get('owner') or '')[:60]}")
            continue
        if s.get("disabled"):
            skipped.append({"idx": idx, "kind": name, "reason": "下拉被置灰"})
            logger.warning(f"{name}（{s.get('rowName')}）下拉被置灰，跳过")
            continue

        # 【展开前必须确认这个下拉真点得中】否则上一个浮层会把它整个盖住，click 撞到
        # 「intercepts pointer events」超时（实测记录见 _close_dropdown）。
        #
        # 【为什么不能只在 last_open 非空时才收】2026-09-01 第一版就是那样写的，结果
        # **修复完全没生效**：点完选项那一步（下面）已经收过一次浮层并把 last_open 置空，
        # 于是轮到下一个下拉时 last_open 是 None、_close_dropdown 直接早退返回 True，
        # 「确认 + 换手段重试」整段是死代码——而那一次收其实没收干净（单选下拉点完选项后
        # ant 的浮层仍占位），运费模板照旧被判「被遮挡」跳过。
        # 故判据改成「本下拉点不点得中」这个**事实**，而不是「我记得有没有开着的浮层」：
        # 挡住它的可能是任何残留浮层，我们的记账不该被当成真相。
        hit = await session.eval_json(_JS_HITTABLE, arg=idx)
        if hit.get("ok") and not hit.get("hittable"):
            # 真被挡住了：不管 last_open 记的是谁，用命中测试驱动重试把浮层清掉
            await _clear_overlay(session, idx, last_open)
            hit = await session.eval_json(_JS_HITTABLE, arg=idx)
        last_open = None
        if hit.get("ok") and not hit.get("hittable"):
            skipped.append({"idx": idx, "kind": name, "rowName": s.get("rowName"),
                            "reason": f"被遮挡（{hit.get('blocker')}）"})
            if on_log:
                try:
                    on_log(f"跳过 {name}（{s.get('rowName')}）"
                           f"—— 被浮层遮挡")
                except Exception:
                    pass
            logger.warning(f"{name}（{s.get('rowName')}）下拉被遮挡，跳过："
                           f"{hit.get('blocker')}")
            continue
        # 展开下拉（真实点击：ant-select 的展开也依赖可信事件，且这里没有 checkbox 的坑）
        try:
            await session.page.locator('[data-ba-sel="%d"]' % idx).first.click(timeout=10000)
        except Exception as e:
            skipped.append({"idx": idx, "kind": name, "reason": f"展开失败：{e}"[:120]})
            logger.warning(f"{name}（{s.get('rowName')}）下拉展开失败，跳过：{e}")
            continue
        last_open = idx
        await asyncio.sleep(1.5)

        opts = await session.eval_json(_JS_OPTIONS, arg=idx)
        if not opts.get("ok") or not opts.get("opts"):
            reason = opts.get("reason") or ("选项为空" if opts.get("empty") else "读不到选项")
            skipped.append({"idx": idx, "kind": name, "reason": reason,
                            "rowName": s.get("rowName")})
            logger.warning(f"{name}（{s.get('rowName')}）读不到可选项（{reason}），跳过")
            await _close_dropdown(session, idx)
            last_open = None
            continue
        # 【截断的清单一律拒收，不拿它挑值】虚拟列表没滚到底时读到的是前几屏，
        # 用它算「工作日数最大」会挑出**首屏末条**当最大值——2026-09-01 用户发现的
        # 「发货时效没滚到最迟的时效」就是这么来的（读到 10 条、以为 14 天最大）。
        # 宁可跳过让人工填，也不要填一个看似合理的错值：错值会一路通过发布检测，
        # 没有任何下游环节能发现它。
        if opts.get("virtual") and not opts.get("scrolledToEnd"):
            skipped.append({
                "idx": idx, "kind": name, "rowName": s.get("rowName"),
                "reason": f"选项清单被截断（虚拟列表没滚到底，只读到 {opts.get('n')} 条），"
                          f"不按半份清单取值"})
            logger.warning(f"{name}（{s.get('rowName')}）选项清单被截断"
                           f"（读到 {opts.get('n')} 条、scrollHeight="
                           f"{opts.get('scrollHeight')}），跳过不瞎填")
            await _close_dropdown(session, idx)
            last_open = None
            continue
        if opts.get("virtual"):
            logger.info(f"{name}（{s.get('rowName')}）滚动读到 {opts.get('n')} 个选项"
                        f"（虚拟列表，已滚到底）")
        try:
            pick = _pick_option(rule, opts["opts"])
        except ValueError as e:
            skipped.append({"idx": idx, "kind": name, "reason": str(e)[:150],
                            "rowName": s.get("rowName")})
            logger.warning(f"{name}（{s.get('rowName')}）选不出值，跳过：{e}")
            await _close_dropdown(session, idx)
            last_open = None
            continue

        item = {"idx": idx, "kind": name, "rule": rule,
                "rowName": s.get("rowName") or "", "multiple": bool(s.get("multiple")),
                "picked": pick.get("txt"), "optionCount": opts.get("n"),
                "options": [o.get("txt") for o in opts["opts"]][:12]}
        if pick.get("workdays") is not None:
            item["workdays"] = pick["workdays"]

        if dry_run:
            # dry-run：算出该选谁就收工，不点选项（弹窗仍是干净的，取消即可）
            item["applied"] = False
            filled.append(item)
            logger.info(f"[dry-run] {name}（{item['rowName']}）将选：{item['picked']}"
                        f"（共 {opts.get('n')} 个可选）")
            await _close_dropdown(session, idx)
            last_open = None
            continue

        # 传文案而不是下标：清单是滚完整个虚拟列表累积的，与 DOM 当前渲染的下标
        # 不是一回事（详见 _JS_PICK_OPTION 上方注释）
        r = await session.eval_json(_JS_PICK_OPTION, arg=[idx, pick["txt"]])
        if not r.get("ok"):
            skipped.append({"idx": idx, "kind": name,
                            "reason": f"点选项失败：{r.get('reason')}",
                            "rowName": s.get("rowName")})
            logger.warning(f"{name}（{s.get('rowName')}）点选项失败：{r}")
            await _close_dropdown(session, idx)
            last_open = None
            continue
        await asyncio.sleep(1)
        # 多选下拉点完不会自动收起（Escape 也关不掉），点回它自己收起
        await _close_dropdown(session, idx)
        last_open = None
        # 回读校验：ant 在有值时挂 .ant-select-selection-item
        val = await session.eval_json(_JS_SEL_VALUE, arg=idx)
        item["applied"] = bool(val.get("hasValue"))
        item["value"] = (val.get("items") or [None])[0]
        if not item["applied"]:
            skipped.append({"idx": idx, "kind": name, "reason": "选完回读仍为空",
                            "rowName": s.get("rowName")})
            logger.warning(f"{name}（{s.get('rowName')}）选了「{pick.get('txt')}」"
                           f"但回读仍为空")
        else:
            logger.info(f"{name}（{item['rowName']}）已选：{item['value']}")
            if on_log:
                try:
                    on_log(f"{name}（{item['rowName']}）→ {item['value']}")
                except Exception as e:
                    logger.warning(f"进度回调失败（忽略）：{e}")
        filled.append(item)

    return {"filled": filled, "skipped": skipped}


async def apply_bulk_attrs(rowids: list, dry_run: bool = True,
                           session: BrowserSession = None,
                           on_log=None) -> dict:
    """给采集箱里指定的草稿批量填仓库 / 发货时效 / 运费模板。

    取值规则固定（见模块 docstring）：仓库与运费模板取下拉第一项，发货时效取工作日数最大。

    dry_run=True（默认）走完全部流程但**不点选项、不点确定**，返回打算填什么，
    供先核对再执行。dry_run=False 才真实修改（覆盖这三项的已有值，不可逆）。

    on_log 是可选的进度回调 on_log(text)，把过程推到 UI 的「本批进度」日志。
    best-effort：回调抛异常不影响主流程。

    返回 {"status", "requested", "checked", "missing", "dryRun",
          "filled": [...], "skipped": [...], "confirmed": bool}。
    """
    rowids = [str(r) for r in (rowids or []) if str(r).strip()]
    if not rowids:
        raise ValueError("没有要修改的行（rowids 为空）")

    own = session is None
    if own:
        if not await ensure_cdp_alive():
            raise RuntimeError("CDP 不可用（调试 Chrome 未启动或未登录店小秘）")
        session = BrowserSession()
    try:
        if own:
            await session.open()
        # 【全程持页面锁】要在列表页勾行、开弹窗、逐个展开下拉，中途被某轮定时扫描
        # navigate 走就全废了（还可能把已勾的行状态丢掉却继续往下走）。
        # 锁与三个扫描器共用（browser.PAGE_LOCK）。
        from app.publish.browser import PAGE_LOCK

        async with PAGE_LOCK:
            r = await session.navigate(DRAFT_LIST_URL)
            if not r.get("ok"):
                raise RuntimeError(f"导航到采集箱失败：{r}")
            await session.wait_for(
                "JSON.stringify({ready: !!document.querySelector('.vxe-body--row')})",
                lambda d: d.get("ready"), timeout=40, interval=2)
            # 进来先清掉可能残留的弹窗（上次异常中断留下的遮罩会挡住所有点击）
            await session.eval_json(_JS_CLOSE)
            await asyncio.sleep(1)

            picked = await _check_rows(session, rowids)
            if not picked["checked"]:
                raise RuntimeError(
                    f"目标行一条都没勾上（共 {len(rowids)} 条）：它们可能已移入待发布，"
                    f"请重扫采集箱清单后再试")

            modal = await _open_modal(session)
            avail = set(modal.get("attrs") or [])
            missing_attrs = [a for a in TARGET_ATTRS if a not in avail]
            if missing_attrs:
                # 属性键名对不上说明平台改了字段名，此时继续跑只会填错东西
                await session.eval_json(_JS_CLOSE)
                raise RuntimeError(
                    f"弹窗里没有这些属性键：{missing_attrs}"
                    f"（平台可能改了字段名）。已知键：{sorted(avail)[:12]}")

            # 逐个勾三项属性（真实点击），每勾一项右侧会异步渲染出对应区块
            for attr in TARGET_ATTRS:
                ok = await _trusted_toggle(
                    session, _JS_TAG_ATTR, _JS_ATTR_CHECKED,
                    'label[data-ba-attr="1"]', arg=attr, want=True)
                if not ok:
                    await session.eval_json(_JS_CLOSE)
                    raise RuntimeError(
                        f"勾选属性「{ATTR_LABELS.get(attr, attr)}」未生效（重试 3 次）")
                logger.info(f"已勾选属性：{ATTR_LABELS.get(attr, attr)}")
                await asyncio.sleep(2.5)  # 等右侧区块渲染

            res = await _fill_selects(session, dry_run, on_log=on_log)

            confirmed = False
            if dry_run:
                # dry-run 一律取消，不留半填状态的弹窗
                await session.eval_json(_JS_CLOSE)
                logger.info(f"[dry-run] 未提交：将改 {len(picked['checked'])} 条草稿，"
                            f"填 {len(res['filled'])} 个下拉"
                            + (f"，跳过 {len(res['skipped'])} 个" if res["skipped"] else ""))
            else:
                applied = [f for f in res["filled"] if f.get("applied")]
                if not applied:
                    await session.eval_json(_JS_CLOSE)
                    raise RuntimeError(
                        "一个下拉都没填上，已取消（不提交空修改）："
                        f"跳过原因 {[s.get('reason') for s in res['skipped']][:4]}")
                c = await session.eval_json(_JS_CONFIRM)
                if not c.get("found"):
                    await session.eval_json(_JS_CLOSE)
                    raise RuntimeError(f"点击「确定」失败：{c}")
                confirmed = True
                await asyncio.sleep(3)
                # 关掉可能弹出的结果提示（best-effort）；成功与否看页面 toast
                # （browser 的 toast 哨兵会把它打进日志）
                try:
                    await session.eval_json(_JS_CLOSE)
                except Exception as e:
                    logger.warning(f"关闭结果弹窗失败（忽略）：{e}")
                logger.info(f"已提交：{len(picked['checked'])} 条草稿，"
                            f"填了 {len(applied)} 个下拉")

            return {"status": "ok", "dryRun": bool(dry_run),
                    "requested": len(rowids), "checked": len(picked["checked"]),
                    "missing": picked["missing"], "confirmed": confirmed,
                    "filled": res["filled"], "skipped": res["skipped"]}
    finally:
        if own:
            await session.close()
