"""店小秘发布操作：shipping。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import re
from app.publish.browser import BrowserSession, J
from typing import Optional


# ---- 阶段⑩ 运输信息（set_shipping）------------------------------------------
# 承诺发货时效（radio 组）+ 运费模板（ant-select），都在 #shipmentInfo 区块内。
#
# 【搬的是不导航的那个版本】原脚本有两个入口：cmd_set_shipping 会先 open_edit 刷新页面，
# cmd_set_shipping_page 不导航。刷新会把前面 9 个阶段填的、尚未保存的内容全丢掉，
# 故这里只搬不导航版——与本模块其它写入阶段一致，由调用方（CLI / service）负责先
# open_edit 一次，之后各阶段共用同一页面。

_JS_SHIPPING_RADIOS = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const opts = Array.from(sec.querySelectorAll('.ant-radio-button-wrapper, .ant-radio-wrapper'))
    .map(o => (o.textContent || '').trim()).filter(Boolean);
  const c = sec.querySelector('.ant-radio-button-wrapper-checked, .ant-radio-wrapper-checked');
  return JSON.stringify({options: opts, checked: c ? (c.textContent || '').trim() : null});
})()"""


# 【必须点选项内部的 input】点外层 .ant-radio-wrapper 不触发 Vue 的 v-model
# （2026-08-14 实测）：wrapper 上的 click 被 antd 自己的事件代理吃掉，只有内层
# 原生 input 的 click 会走到 change 回调。
_JS_CLICK_SHIPPING_RADIO = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({clicked: false, reason: 'section-not-found'});
  const opt = Array.from(sec.querySelectorAll('.ant-radio-button-wrapper, .ant-radio-wrapper'))
    .find(o => (o.textContent || '').trim() === __T__);
  if (!opt) return JSON.stringify({clicked: false, reason: 'option-not-found'});
  opt.scrollIntoView({block: 'center', behavior: 'instant'});
  const inp = opt.querySelector('input');
  if (!inp) return JSON.stringify({clicked: false, reason: 'no-inner-input'});
  inp.click();
  return JSON.stringify({clicked: true});
})()"""


# 运费模板没有 ant-form-item 包裹、也没有稳定的 name/id，只能从「运费模板」这个纯文本
# 叶子节点出发往上找最近的 .ant-select（最多 6 层）。同时回读坐标，供合成点击展开下拉。
_JS_FREIGHT_TPL_STATE = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const lab = Array.from(sec.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent || '').trim() === '运费模板')[0];
  if (!lab) return JSON.stringify({err: 'no-运费模板-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 6 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const selected = Array.from(sel.querySelectorAll('.ant-select-selection-item'))
    .map(x => (x.title || x.textContent || '').trim()).filter(Boolean);
  const r = sel.getBoundingClientRect();
  return JSON.stringify({selected, open: sel.classList.contains('ant-select-open'),
    x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
})()"""


_JS_SCROLL_FREIGHT_TPL = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const lab = Array.from(sec.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent || '').trim() === '运费模板')[0];
  if (!lab) return JSON.stringify({err: 'no-运费模板-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 6 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  sel.scrollIntoView({block: 'center', behavior: 'instant'});
  return JSON.stringify({ok: true});
})()"""


# 展开运费模板下拉。点内部 .ant-select-selector 而不是外层 .ant-select（实测后者无效）。
_JS_OPEN_FREIGHT_TPL = r"""(() => {
  // 【定位走 #shipmentInfo 区块内的 .ant-select，不按「运费模板」文本反查】
  // 2026-08-20 实测：按叶子元素文本恰等于「运费模板」去找会 no-select——该文案在
  // DOM 里不是独立叶子节点。而运输信息区块里 .ant-select 只有这一个（12 个发货时效
  // 是 radio，不是 select），故按区块取更可靠。
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'no-shipmentInfo'});
  const sel = sec.querySelector('.ant-select');
  if (!sel) return JSON.stringify({err: 'no-select'});
  const inner = sel.querySelector('.ant-select-selector') || sel;
  ['mousedown', 'mouseup', 'click'].forEach(t =>
    inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  return JSON.stringify({clicked: true, open: sel.classList.contains('ant-select-open')});
})()"""


# 运费模板一般只有一个（唯一模板，2026-08-18 用户确认），直接取下拉第一项。
_JS_PICK_FIRST_TPL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 【判可见必须看 inline display，不能用 getBoundingClientRect().height】
  // 2026-08-20 实测：页面上常驻 2 个 .ant-select-dropdown（仓库那个 + 运费模板那个），
  // 隐藏时高度恒为 0 但 style 里留着上次的 left/top，且 ant-select-dropdown-hidden
  // 这个类也不一定加上。按 height>0 找会一个都找不到（no-dropdown）。
  const dd = Array.from(document.querySelectorAll('.ant-select-dropdown'))
    .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
    .pop();
  if (!dd) return JSON.stringify({err: 'no-dropdown'});
  const o = dd.querySelector('.ant-select-item-option');
  if (!o) return JSON.stringify({err: 'no-option'});
  const text = (o.textContent || '').trim();
  o.click();
  await sleep(800);
  document.body.click();
  return JSON.stringify({picked: text});
})()"""


def _longest_deadline(options: list) -> Optional[str]:
    """从时效选项里挑「最长」的那个（SKILL.md 阶段⑩规则）。

    选项文本形如「1个工作日内发货」「15个工作日内发货」，按前导整数比大小——不能按
    字符串排序（"9" > "15"），也不能按选项在 DOM 里的顺序取最后一个（页面上是
    1/2/7/8/…/16 递增，但顺序是平台给的、不保证）。取不出数字的选项直接排除。
    """
    scored = []
    for text in options:
        m = re.match(r"\s*(\d+)", text)
        if m:
            scored.append((int(m.group(1)), text))
    return max(scored)[1] if scored else None


async def set_shipping(session: BrowserSession, deadline: str = "") -> dict:
    """阶段⑩：承诺发货时效（radio）+ 运费模板（ant-select）。不导航。

    deadline 留空时按 SKILL.md 规则选【最长】时效——时效越长越不容易因超时被平台罚，
    这是业务侧定的默认。给了具体值就按文本精确匹配（匹配不上会把可选项列出来）。

    两处踩过的坑，对应下面两段处理：
    1. radio 必须点选项内部的 input（见 _JS_CLICK_SHIPPING_RADIO 注释）
    2. 点完 Vue 会重渲染整个区块，回读必须【重新查询】元素。故这里回读是另发一次
       eval_json（_JS_SHIPPING_RADIOS）从 document 重新找 checked 的那一项，
       不复用点击时拿到的任何引用。
    """
    state = await session.eval_json(_JS_SHIPPING_RADIOS)
    if state.get("err"):
        return {"status": "error", "stage": "deadline-locate", **state}
    options = state.get("options") or []
    if not options:
        return {"status": "error", "stage": "deadline-locate",
                "reason": "运输信息区未找到发货时效选项"}

    target = deadline or _longest_deadline(options)
    if not target:
        return {"status": "error", "stage": "deadline-pick",
                "reason": "选项里解析不出工作日天数", "options": options}
    if target not in options:
        return {"status": "error", "stage": "deadline-pick",
                "reason": f"指定的时效不在选项里: {target}", "options": options}

    if state.get("checked") != target:
        clicked = await session.eval_json(
            _JS_CLICK_SHIPPING_RADIO.replace("__T__", J(target)))
        if not clicked.get("clicked"):
            return {"status": "error", "stage": "deadline-click",
                    "options": options, **clicked}
        await asyncio.sleep(1.5)  # 等重渲染，太早回读会读到旧的 checked
        state = await session.eval_json(_JS_SHIPPING_RADIOS)
        if state.get("checked") != target:
            return {"status": "error", "stage": "deadline-verify",
                    "reason": "点选后回读不符", "want": target,
                    "checked": state.get("checked")}

    # 运费模板：已选中就不动（幂等），未选中才展开下拉取第一项
    tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
    if tpl.get("err"):
        return {"status": "error", "stage": "tpl-locate",
                "checked": state.get("checked"), **tpl}
    picked = None
    if not tpl.get("selected"):
        # 先滚到视野中央再另起一次 eval 读坐标：滚动未停就读到的 rect 会让点击落空
        await session.eval_json(_JS_SCROLL_FREIGHT_TPL)
        await asyncio.sleep(0.8)
        tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
        if not tpl.get("open"):
            # 【必须点内部的 .ant-select-selector，点外层 .ant-select 容器无效】
            # 2026-08-20 实测：sel.click() 与 elementFromPoint 取到的外层容器都打不开
            # 下拉（open 仍为 false），antd 把点击处理绑在 selector 那一层。
            await session.eval_json(_JS_OPEN_FREIGHT_TPL)
            await asyncio.sleep(1.5)
        opt = await session.eval_json(_JS_PICK_FIRST_TPL)
        if opt.get("err"):
            return {"status": "error", "stage": "tpl-option",
                    "checked": state.get("checked"), **opt}
        picked = opt.get("picked")
        await asyncio.sleep(1.0)
        tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
        if not tpl.get("selected"):
            return {"status": "error", "stage": "tpl-verify",
                    "reason": "选中后回读为空", "checked": state.get("checked"),
                    "picked": picked, **tpl}

    return {"status": "ok", "deadline": state.get("checked"),
            "autoPicked": not deadline, "options": options,
            "freightTemplate": (tpl.get("selected") or [None])[0],
            "templatePicked": picked}
