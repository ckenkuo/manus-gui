"""店小秘发布操作：variant_dom。模块导航见 docs/publish-pipeline-refactor.md。"""


# ---- 变种表「维度列」的共用定位判据 ------------------------------------------
# 【为什么不能只认「颜色」和「尺码」两个词】2026-09-12 真站取证（Temu 商品
# 601101104447803 骷髅车贴，类目车贴）：变种表表头是
#   ['预览图( 批量)', '型号', 'SKU货号 ( 一键生成 · 高级 )', 'EANUPCISBN (批量编辑)',
#    '申报价格 (CNY) (批量)', '尺寸(cm)(批量)', '重量(g)(批量)', '建议售价(批量)',
#    '型号', 'SKU分类 (批量)', '包装清单 (批量)']
# 唯一的变种维叫【型号】——既不是「颜色」也不是「尺码」。原先各处判据硬性要求
# /^颜色/ 或含「尺码」，两条全落空：⑩a 直接 no-color-column 判失败（整单卡死、交
# Manus 兜底也无解，页面上确实没有这两列），⑦b 的 colorIdx=-1 让行序核对失效，
# ⑦a 读不到 byColor 而跳过。日志里另见「存储容量」也当过第二维（3C 类目）。
#
# 故维度列改按【结构位置】认，而不是按名字猜：变种表的列布局是
#   预览图 | 变种维1 [| 变种维2 …] | SKU货号 | EAN | 申报价格 | …
# 即「预览图之后、SKU货号之前」的所有列就是这个类目的变种维，名字是平台按类目定的
# （颜色/尺码/型号/存储容量/…），穷举不完。两侧的锚点列反而是全类目稳定的：预览图
# 与 SKU货号在上面 5 份实测表头里逐字出现。
#
# 【第一维当颜色位、第二维当尺码位】既有的「维度取舍」「行序核对」「配件色统计」
# 全是按两个维度写的，位置语义（第一维/第二维）与原先的颜色/尺码完全同构，故沿用
# 那两个变量名，只把取列的方式换掉。真·颜色+尺码类目下第一维就是颜色、第二维就是
# 尺码，行为与改动前逐字一致。
_JS_DIM_COLS = r"""
  // 变种维列 = 预览图之后、SKU货号之前的那些列（名字随类目变，位置不变）。
  // 表头文案带「( 批量)」这类后缀，故一律 includes 而非全等。
  const dimCols = (heads) => {
    const iPrev = heads.findIndex(h => h.includes('预览图'));
    const iCode = heads.findIndex(h => h.includes('SKU货号'));
    const cols = [];
    if (iCode > 0) {
      // 预览图列缺失（未实测到，但别据此整段失效）时从 0 起扫
      for (let k = (iPrev >= 0 ? iPrev + 1 : 0); k < iCode; k++) cols.push(k);
    }
    if (cols.length) return cols;
    // 锚点列都没读到：退回按名字找「颜色」「尺码」，两者都读不到才算真没有维度列。
    // 这一支只为兜住表头文案改版，正常类目走不到。
    const byName = [];
    const ic = heads.findIndex(h => /^颜色/.test(h));
    const is = heads.findIndex(h => h.includes('尺码') && !h.includes('尺码表'));
    if (ic >= 0) byName.push(ic);
    if (is >= 0 && is !== ic) byName.push(is);
    return byName.sort((a, b) => a - b);
  };
  // 第一维放颜色位、第二维放尺码位（位置语义与原先的颜色/尺码列同构）。
  // 三维及以上（未实测到）只取前两维：货号/行序核对用两维已足够区分行。
  const dimIdx = (heads) => {
    const cols = dimCols(heads);
    return {colorIdx: cols.length ? cols[0] : -1,
            sizeIdx: cols.length > 1 ? cols[1] : -1,
            dimCols: cols};
  };
"""


# 反选一个变种维选项（按【页面】选项文本精确匹配）。返回是否点到、点后的勾选态。
# 【只反选，绝不勾选】本函数用于剔配件色/伪选项/补不上图的规格，任何情况下都不该把
# 没勾的勾上。
#
# 【为什么不能只在「颜色」组里找】2026-09-12 取证（Temu 商品 601101104447803 车贴）：
# 该类目的变种维叫【型号】，变种属性区压根没有「颜色」这一行 → 原实现直接返回
# no-color-group，⑦a 剔配件色与「补不上预览图就反选该规格」都无从落地。维度名是平台
# 按类目定的（颜色/尺码/型号/存储容量/…），与变种表列名同源、穷举不完（同
# _JS_DIM_COLS 的取证）。
# 故改成：颜色组优先（真·颜色类目行为与改动前逐字一致），没有颜色组时在【其余变种维
# 组】里按选项文本找。选项文本跨维度不会重名（颜色名与型号名不会撞），故按文本认是
# 安全的；「尺码表」这类不是变种维的行排除掉。
_JS_UNCHECK_COLOR = r"""(() => {
  const WANT = __WANT__;
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  const labelOf = it => {
    const l = it.querySelector('.ant-form-item-label');
    return (l ? l.textContent : '').trim();
  };
  const isColorGroup = it => {
    const lab = labelOf(it);
    return lab === '颜色' || (lab.includes('颜色') && !lab.includes('颜色表'));
  };
  // 变种维组 = 带复选框、且不是「尺码表/颜色表」这类附属行
  const isDimGroup = it => {
    const lab = labelOf(it);
    if (!lab || lab.includes('尺码表') || lab.includes('颜色表')) return false;
    return it.querySelectorAll('label.d-checkbox').length > 0;
  };
  // 颜色组排在前面：真·颜色类目下先在它里面找，与本函数原先的行为一致
  const groups = items.filter(isColorGroup).concat(
    items.filter(it => !isColorGroup(it) && isDimGroup(it)));
  if (!groups.length) return JSON.stringify({found: false, err: 'no-dim-group'});
  const checkedAll = [];
  for (const it of groups) {
    const cbs = Array.from(it.querySelectorAll('label.d-checkbox'));
    cbs.forEach(l => {
      const i = l.querySelector('input');
      if (i && i.checked) checkedAll.push((l.textContent || '').trim());
    });
    const hit = cbs.find(l => (l.textContent || '').trim() === WANT);
    if (!hit) continue;   // 这一维里没有，再看下一维
    const input = hit.querySelector('input');
    // 【绝不反选到「这一维一个都不剩」】变种维不能为空：把最后一个已勾选项也取消掉，
    // 平台会把整张变种表清掉，比留着一个不合格规格坏得多（那只是这一个 SKU 有问题）。
    // 判据放在这里而不是各调用方：⑦a 剔配件色、剔伪选项、⑦b 反选补不上图的规格三处
    // 都会撞上「整维只剩一个」，在唯一出口上拦一次比三处各判一次可靠。
    const checkedInGroup = cbs.filter(l => {
      const i = l.querySelector('input'); return i && i.checked;
    });
    if (input.checked && checkedInGroup.length <= 1) {
      return JSON.stringify({found: true, wasChecked: true, clicked: false,
                             checked: true, err: 'last-checked-in-group',
                             group: labelOf(it),
                             checkedOptions: checkedInGroup.map(
                               l => (l.textContent || '').trim())});
    }
    if (!input.checked) return JSON.stringify({found: true, wasChecked: false,
                                               clicked: false, checked: false,
                                               group: labelOf(it)});
    input.click();
    return JSON.stringify({found: true, wasChecked: true, clicked: true,
                           checked: !!input.checked, group: labelOf(it)});
  }
  // 各维度组里都没有这个选项：把已勾选项报出来供排查（同原先的返回形状）
  return JSON.stringify({found: false, checkedOptions: checkedAll,
                         groups: groups.map(labelOf)});
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
