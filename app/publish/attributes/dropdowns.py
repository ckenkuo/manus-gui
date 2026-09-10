"""店小秘发布操作：attributes.dropdowns。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.publish import common
from app.publish.browser import BrowserSession, J


async def _expand_attr_section(session: BrowserSession) -> dict:
    """展开产品属性的「+展开」折叠开关。

    收起状态下大量属性行 display:none，下拉根本点不开。已展开时不动（找不到开关就
    当已展开，不报错）。
    """
    return await session.eval_json(r"""(() => {
      const sec = document.getElementById('productBasicInfo');
      if (!sec) return JSON.stringify({expanded: false, reason: 'no-section'});
      const t = Array.from(sec.querySelectorAll('span'))
        .find(el => el.offsetHeight > 0 && /^\+?\s*展开/.test((el.textContent||'').trim())
             && (el.textContent||'').trim().length < 8);
      if (!t) return JSON.stringify({expanded: false, reason: 'already-or-not-found'});
      t.click();
      return JSON.stringify({expanded: true});
    })()""")


async def _park_ghost_dropdowns(session: BrowserSession) -> dict:
    """把视觉残留的幽灵浮层恢复成停靠态（清 left/top、宽高归零）。见坑1。"""
    return await session.eval_json(r"""(() => {
      let parked = 0;
      Array.from(document.querySelectorAll('.ant-select-dropdown')).forEach(d => {
        const r = d.getBoundingClientRect();
        if (r.top > -1000 && r.width > 50) {
          d.style.left = ''; d.style.top = '';
          d.style.width = '0px'; d.style.minWidth = '0px';
          parked++;
        }
      });
      return JSON.stringify({parked: parked});
    })()""")


async def _press_escape(session: BrowserSession) -> None:
    """派发 Escape 关下拉（合成事件，比 keyboard.press 更贴近原脚本行为）。"""
    await session.eval_json(
        "(() => { document.dispatchEvent(new KeyboardEvent('keydown', "
        "{key: 'Escape', bubbles: true})); return JSON.stringify({ok: true}); })()"
    )


def _js_dropdown_options_rendered(label: str) -> str:
    """探「该行最近的可见浮层里已渲染出几个选项」。

    给「开下拉之后等什么」用：_open_attr_dropdown 的收敛条件是浮层可见，而浮层可见
    ≠ 里面的 rc-virtual-list 已挂上 .ant-select-item-option（虚拟列表要一帧才渲染）。
    原先靠固定 sleep(0.6) 兜这段，现在等这个真实信号（上限仍 0.6s）。
    「取距本行最近的可见浮层」与坑1/坑2 那套幽灵浮层过滤同一判据
    （top > -1000 && width > 50），不另立标准。
    """
    return r"""(() => {
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      const rowY = row ? row.getBoundingClientRect().top : 0;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({n: 0});
      drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      return JSON.stringify({
        n: drops[0].d.querySelectorAll('.ant-select-item-option').length});
    })()""".replace("__LABEL__", J(label))


async def _visible_dropdown_near(session: BrowserSession, label: str,
                                 max_dist: int = 600) -> dict:
    """检测指定属性行附近是否有可见（未停靠）的下拉浮层。见坑1、坑2。"""
    js = r"""(() => {
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!row) return JSON.stringify({found: false, reason: 'row-not-found'});
      row.scrollIntoView({block: 'center'});
      const rowY = row.getBoundingClientRect().top;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({found: false});
      drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const dist = Math.abs(drops[0].r.top - rowY);
      if (dist > __MAXD__) return JSON.stringify({found: false, nearestDist: Math.round(dist)});
      return JSON.stringify({found: true, top: Math.round(drops[0].r.top)});
    })()""".replace("__LABEL__", J(label)).replace("__MAXD__", str(max_dist))
    return await session.eval_json(js)


async def _is_dropdown_open(session: BrowserSession, label: str,
                            sel_idx: int = 0) -> bool:
    """检测目标字段第 sel_idx 个下拉自身是否处于打开态（.ant-select-open）。

    为什么不用「行附近是否有可见浮层」（_visible_dropdown_near）判打开：
    .ant-select-dropdown 是懒渲染的——点击后 .ant-select-open 立即加上，但浮层要
    一帧后才挂上；且重复点击 toggle 关闭后浮层还会残留几帧（幽灵浮层）。用浮层位置
    判打开会把「别的字段残留的幽灵浮层」误当成「本字段已打开」，正是 2026-09-04
    「上装成分/下装成分」错位写入、保存报「上装成分不能重复选择」的根因。
    .ant-select-open 是 Vue 内部打开态的直接标记，与浮层渲染时机无关。
    """
    js = r"""(() => {
      const it = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!it) return JSON.stringify({open: false, reason: 'row-not-found'});
      const sels = Array.from(it.querySelectorAll('.ant-select'));
      const s = sels[__IDX__] || sels[0];
      return JSON.stringify({open: !!s && s.classList.contains('ant-select-open')});
    })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(sel_idx))
    d = await session.eval_json(js)
    return bool(d.get("open"))


async def _open_attr_dropdown(session: BrowserSession, label: str,
                              sel_idx: int = 0) -> dict:
    """点开指定属性行的下拉，以「目标下拉自身的 .ant-select-open」为准做幂等打开。

    已打开则不重复点（重复点会 toggle 关掉）；点了没开就再点一次——Vue 内部状态与
    视觉停靠态错位时第一次点击会反向 toggle（原脚本实测）。
    sel_idx：成分类复合字段行内第 N 个下拉（0 起）。

    【2026-09-04 修】打开态判断从「行附近是否有可见浮层」改成「目标下拉自身的
    .ant-select-open」，并在点开前先清一轮残留浮层。理由：浮层懒渲染 + 关闭后残留，
    用浮层位置判打开会把别的字段残留的幽灵浮层误当成本字段已打开，随后
    _click_dropdown_option 的「距行最近」浮层定位就点到了错字段——上装成分/下装成分/
    材质三个字段相邻且共用同一份纤维列表，「棉」这类在多个字段浮层都首屏可见的选项
    必现错位（保存报「XX成分不能重复选择」）。清残留保证点开后页面上只有本字段这
    一个浮层，下游的浮层定位就不会再选错。
    """
    if await _is_dropdown_open(session, label, sel_idx):
        return {"opened": True, "already": True}
    # 没打开：先清残留浮层（park 停靠 + Escape 关 Vue 内部态），再点开。
    await _park_ghost_dropdowns(session)
    await _press_escape(session)
    js_click = r"""(() => {
      const it = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!it) return JSON.stringify({dispatched: false, reason: 'row-not-found'});
      it.scrollIntoView({block: 'center'});
      const sels = Array.from(it.querySelectorAll('.ant-select'));
      const box = sels[__IDX__] || sels[0];
      const sel = box ? (box.querySelector('.ant-select-selector') || box) : null;
      if (!sel) return JSON.stringify({dispatched: false, reason: 'no-select'});
      ['mousedown','mouseup','click'].forEach(t =>
        sel.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      return JSON.stringify({dispatched: true});
    })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(sel_idx))
    for attempt in (1, 2):
        r = await session.eval_json(js_click)
        if not r.get("dispatched"):
            return {"opened": False, **r}
        # 等目标下拉自身的 .ant-select-open 出现（浮层渲染有时滞，但打开态 class
        # 点击即生效，比等浮层更及时）
        seen = await common._poll_until(
            lambda: _is_dropdown_open(session, label, sel_idx),
            lambda d: d, timeout=0.5)
        if seen:
            return {"opened": True, "attempts": attempt}
    return {"opened": False, "reason": "open-verify-failed"}


async def _read_active_options(session: BrowserSession, label: str,
                               with_meta: bool = False):
    """主动点开该行下拉、滚动虚拟列表读出全部选项，读完关闭并清幽灵。

    不点任何选项，不改表单值。为什么必须点开读而不静态扫 DOM：选项懒渲染 +
    虚拟列表只渲染可视窗口约 10 条（坑3）。

    with_meta=True 时返回 (options, meta) 而不是裸列表，meta 带 virtual/scrollHeight/
    scrolledToEnd 等完整性判据——【落盘缓存前必须看它】：被截断的首屏 10 条写进缓存
    会让 _rebuild_main_comp 的纤维匹配静默失效（成分是 67 项的长列表，最容易截断），
    而静默截断本来就是这一带最难发现的一类错。默认 False 保持老调用方的裸列表契约。
    """
    await _park_ghost_dropdowns(session)
    await _press_escape(session)
    r = await _open_attr_dropdown(session, label)
    if not r.get("opened"):
        # 页面渲染慢时第一次点不开（下拉组件未挂载 / 浮层未渲染），等一会再试一次
        # （2026-09-04 商品 998669429087：必填行下拉点不开，重试能救回渲染慢的）
        await asyncio.sleep(1.0)
        r = await _open_attr_dropdown(session, label)
    if not r.get("opened"):
        if not with_meta:
            return []
        # 重试一次仍点不开才按二元组契约返回，让调用方走 open-failed / 重读为空 的
        # best-effort 兜底路径，而不是抛解包异常打断整个属性审核阶段。
        return [], {"virtual": False, "scrollHeight": None,
                    "scrolledToEnd": False, "complete": False}
    await asyncio.sleep(0.5)
    js = r"""(async () => {
      const sleep = ms => new Promise(res => setTimeout(res, ms));
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      const rowY = row ? row.getBoundingClientRect().top : null;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({open: false, options: []});
      if (drops.length > 1 && rowY !== null)
        drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const target = drops[0].d;
      const holder = target.querySelector('.rc-virtual-list-holder');
      const seen = [];
      const collect = () => Array.from(target.querySelectorAll('.ant-select-item-option-content'))
        .forEach(o => { const t = (o.textContent || '').trim();
          if (t && !seen.includes(t)) seen.push(t); });
      if (!holder) {           // 短列表没有虚拟滚动容器，直接收
        collect();
        return JSON.stringify({open: true, options: seen, virtual: false});
      }
      // 【滚动参数是实测值，别下调】2026-08-19 实测：上装成分共 67 项、
      // scrollHeight=1704 / clientHeight=256（可视约 10 条）。每屏等 180ms 才能
      // 稳定采到新渲染的条目；原脚本的 120ms 在 Playwright 直连下太短——
      // WebBridge 时代每次 evaluate 的往返开销间接补足了等待，换成直连就暴露了，
      // 表现为只读到首屏 10 条（静默截断，最难发现的一类错）。
      // 终止条件用「滚到底」而非「scrollTop 不再变」：后者在 smooth-scroll 或
      // 惯性未结束时会提前判定到底。
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
      return JSON.stringify({open: true, options: seen, virtual: true,
                             visibleDrops: drops.length,
                             scrollHeight: holder.scrollHeight,
                             clientHeight: holder.clientHeight,
                             // 滚到底了才算读全（没滚到底说明 40 次循环用完还没走完，
                             // 那份清单是截断的，不能进缓存）
                             scrolledToEnd: holder.scrollTop + holder.clientHeight
                                            >= holder.scrollHeight - 2});
    })()""".replace("__LABEL__", J(label))
    res = await session.eval_json(js)
    await _press_escape(session)
    await asyncio.sleep(0.2)
    await _park_ghost_dropdowns(session)
    opts = res.get("options", []) if res.get("open") else []
    if not with_meta:
        return opts
    # 非虚拟列表（短列表）一次收全，天然完整；虚拟列表要真滚到底才算完整
    complete = bool(opts) and (not res.get("virtual") or res.get("scrolledToEnd"))
    return opts, {"virtual": bool(res.get("virtual")),
                  "scrollHeight": res.get("scrollHeight"),
                  "scrolledToEnd": bool(res.get("scrolledToEnd")),
                  "complete": complete}


# ---- 阶段④ 属性写入 ---------------------------------------------------------

async def _click_dropdown_option(session: BrowserSession, label: str,
                                 value: str) -> dict:
    """在【目标行附近唯一可见浮层】里点选项。

    绝不扫全页浮层（坑1）：上装成分/下装成分/材质/辅料成分共用同一份 67 项纤维列表，
    按文本匹配会在多个浮层里全命中；而点在隐藏浮层上事件照样生效，结果改掉别的字段。
    找不到目标选项时返回 option-not-rendered，交 _scroll_click_option 滚动去找。
    """
    js = r"""(() => {
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!row) return JSON.stringify({clicked: false, reason: 'row-not-found'});
      row.scrollIntoView({block: 'center'});
      const rowY = row.getBoundingClientRect().top;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({clicked: false, reason: 'no-visible-dropdown'});
      drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      if (Math.abs(drops[0].r.top - rowY) > 600)
        return JSON.stringify({clicked: false, reason: 'dropdown-too-far'});
      const opt = Array.from(drops[0].d.querySelectorAll('.ant-select-item-option'))
        .find(o => {
          const c = o.querySelector('.ant-select-item-option-content');
          return ((c || o).textContent || '').trim() === __VALUE__;
        });
      if (!opt) return JSON.stringify({clicked: false, reason: 'option-not-rendered',
        value: __VALUE__});
      opt.click();
      return JSON.stringify({clicked: true, value: __VALUE__});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))
    return await session.eval_json(js)


async def _scroll_click_option(session: BrowserSession, label: str,
                               value: str) -> dict:
    """滚动虚拟列表找到目标选项渲染出来后点击（选项在可视窗口外时用）。

    滚动等待同 _read_active_options 的 180ms（见那里的注释）：原脚本 120ms 在
    Playwright 直连下不够，会滚过目标却没采到，误报 option-not-rendered。
    """
    js = r"""(async () => {
      const sleep = ms => new Promise(res => setTimeout(res, ms));
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (row) row.scrollIntoView({block: 'center'});
      const rowY = row ? row.getBoundingClientRect().top : null;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({clicked: false, reason: 'no-visible-dropdown'});
      if (drops.length > 1 && rowY !== null)
        drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const target = drops[0].d;
      const hitNow = () => Array.from(target.querySelectorAll('.ant-select-item-option'))
        .find(o => {
          const c = o.querySelector('.ant-select-item-option-content');
          return ((c || o).textContent || '').trim() === __VALUE__;
        });
      const holder = target.querySelector('.rc-virtual-list-holder');
      if (!holder) {
        const h = hitNow();
        if (h) { h.click(); return JSON.stringify({clicked: true, value: __VALUE__}); }
        return JSON.stringify({clicked: false, reason: 'option-not-rendered', value: __VALUE__});
      }
      holder.scrollTop = 0;
      await sleep(200);
      for (let k = 0; k < 60; k++) {
        const hit = hitNow();
        if (hit) { hit.click(); return JSON.stringify({clicked: true, value: __VALUE__, scrolled: k}); }
        if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
        holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
        await sleep(180);
      }
      const last = hitNow();
      if (last) { last.click(); return JSON.stringify({clicked: true, value: __VALUE__, atEnd: true}); }
      return JSON.stringify({clicked: false, reason: 'option-not-rendered', value: __VALUE__});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))
    return await session.eval_json(js)
