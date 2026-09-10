"""店小秘发布操作：variant_dom。模块导航见 docs/publish-pipeline-refactor.md。"""


# 反选一个颜色复选框（按【页面】颜色文本精确匹配）。返回是否点到、点后的勾选态。
# 【只反选，绝不勾选】本函数用于剔配件色，任何情况下都不该把没勾的勾上。
_JS_UNCHECK_COLOR = r"""(() => {
  const WANT = __WANT__;
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (!(lab === '颜色' || (lab.includes('颜色') && !lab.includes('颜色表')))) continue;
    const cbs = Array.from(it.querySelectorAll('label.d-checkbox'));
    const hit = cbs.find(l => (l.textContent || '').trim() === WANT);
    if (!hit) return JSON.stringify({found: false,
      checkedOptions: cbs.filter(l => {
        const i = l.querySelector('input'); return i && i.checked;
      }).map(l => (l.textContent || '').trim())});
    const input = hit.querySelector('input');
    if (!input.checked) return JSON.stringify({found: true, wasChecked: false,
                                               clicked: false, checked: false});
    input.click();
    return JSON.stringify({found: true, wasChecked: true, clicked: true,
                           checked: !!input.checked});
  }
  return JSON.stringify({found: false, err: 'no-color-group'});
})()"""


_JS_SIZE_GROUP_STATES = r"""(() => {
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'))) {
      const cbs = Array.from(it.querySelectorAll('label.d-checkbox'));
      if (cbs.length) return JSON.stringify(cbs.map(l => ({t: (l.textContent||'').trim(), c: l.querySelector('input').checked})));
    }
  }
  return JSON.stringify([]);
})()"""


# 颜色维的全部选项 + 勾选态。与 _JS_SIZE_GROUP_STATES 同构，只是换找「颜色」组
# （排除「颜色表」，同 _JS_UNCHECK_COLOR 的判据）。剔伪变种要同时看颜色/尺码两维——
# 伪 SKU 可能是尺码（「2XL:尺寸参考选项图」），也可能是颜色（「短袖款式随机」）。
_JS_COLOR_GROUP_STATES = r"""(() => {
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (lab === '颜色' || (lab.includes('颜色') && !lab.includes('颜色表'))) {
      const cbs = Array.from(it.querySelectorAll('label.d-checkbox'));
      if (cbs.length) return JSON.stringify(cbs.map(l => ({t: (l.textContent||'').trim(), c: !!l.querySelector('input').checked})));
    }
  }
  return JSON.stringify([]);
})()"""


# 【为什么要单独探「区块在不在」】上面那段返回空数组有两种完全不同的成因，而阶段⑧
# 对它们的正确处置相反：
#   1. 变种属性区压根没渲染（类目失效/还在加载）→ 真异常，必须报错
#   2. 区块在、但这个类目【没有尺码维】→ 无事可做，应当 skipped
# 2026-08-28 真站取证（草稿 173539495451708963，类目「家居、厨房用品 > 家居装饰 >
# 仿真植物、仿真花、花艺 > 仿真花」）：等到 20s 稳定，skuAttrsInfo 高 412px、6 个
# d-checkbox 全是【颜色】，整页 32 个 label 里一个带「尺」的都没有，也没有尺码表栏。
# 区块文本是「变种属性 …重新对应变种 颜色【大吉大梨】梨花筒（life盆）… 添加尺码添加」
# ——「添加尺码」只是个按钮，不是已渲染的尺码组。
# 家居/玩具/饰品这类无尺码商品在 1688 上很常见，把「本类目不需要尺码」判成失败会让
# 整单卡在⑧ 永远发不出去（该商品的 6 个颜色复选框本来就已勾好、变种表也已生成，
# ⑧ 对它本就无事可做）。
#
# hasSizeBtn 单独报出来是为了把判断建立在【结构信号】上而不是「没找到就当没有」：
# 无尺码类目的页面上有「添加尺码」按钮，这是平台自己表达「此处可加尺码但当前没有」。
_JS_SIZE_GROUP_PRESENCE = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({section: false});
  const items = Array.from(sec.querySelectorAll('.ant-form-item'));
  const labels = items.map(it => {
    const l = it.querySelector('.ant-form-item-label');
    return (l ? l.textContent : '').trim();
  }).filter(Boolean);
  // 尺码组＝label 含「尺码」且不是「尺码表」的那一行（与 _JS_SIZE_GROUP_STATES 同判据）
  const sizeItems = items.filter(it => {
    const l = it.querySelector('.ant-form-item-label');
    const lab = (l ? l.textContent : '').trim();
    return lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'));
  });
  const txt = (sec.textContent || '').replace(/\s+/g, ' ').trim();
  return JSON.stringify({
    section: true,
    height: sec.offsetHeight,
    // 区块里的复选框总数：无尺码类目下这些全是颜色，非 0 说明区块确实渲染完了
    checkboxes: sec.querySelectorAll('label.d-checkbox').length,
    sizeItemCount: sizeItems.length,
    labels: labels.slice(0, 20),
    hasSizeBtn: txt.includes('添加尺码'),
    text: txt.slice(0, 200),
  });
})()"""


_JS_CLICK_SIZE_CB = r"""(() => {
  const target = __T__;
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'))) {
      const cb = Array.from(it.querySelectorAll('label.d-checkbox'))
        .find(l => (l.textContent||'').trim() === target);
      if (cb) { cb.click(); return JSON.stringify({clicked: true}); }
    }
  }
  return JSON.stringify({clicked: false});
})()"""


_JS_SKU_ROW_COUNT = """(() => {
  const tb = document.querySelector('#skuDataInfo tbody');
  return JSON.stringify({n: tb ? tb.querySelectorAll('tr').length : 0});
})()"""
