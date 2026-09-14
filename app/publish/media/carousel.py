"""店小秘发布操作：media.carousel。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio

from app.publish import common
from app.publish.browser import BrowserSession, J
from app.publish.media import space as media_space


# ==================== 阶段⑤c 产品轮播图 ====================
# 【轮播图是第四处图片位，而且是另外三处的上游】
# 2026-09-12 取证（编辑页 id=184807703147300533，弹珠机玩具类）：发布被平台拒
# 「错误：产品轮播图尺寸不能小于800*800」，而管线此前【没有任何代码碰过轮播图】。
# 四处图位各自独立：
#   ⑤c 产品轮播图  产品信息区 .img-list        多选，勾 3~10 张
#   ⑥  素材图      .material-img-module        整个商品一张
#   ⑦  SKC 颜色图  #skuAttrsInfo（变种属性区）  每颜色 3~10 张
#   ⑦b SKU 预览图  #skuDataInfo（变种信息表）   每 SKU 行一张
#
# 【为什么必须排在 ⑥ 之前】页面说明原文：「素材图将自动获取产品轮播图/颜色图的
# 第一张图片」。轮播图是素材图的图源，先修素材图等于拿破线的原图去做方图——上游
# 没修，下游每一处都要各自补救。这也是预览图空格菜单里那项叫「引用产品轮播图」
# 的原因（见 media/preview.py 的 SKU_PREVIEW_EMPTY_MENU_ITEMS）。
#
# 【为什么不能靠改勾选绕过】轮播图列表里通常有十几张候选，直觉上「取消破线那张、
# 改勾一张大图」最省事。实测那单 18 张候选：已勾 5 张（899²/905²/905²/676²/800²），
# 未勾的 13 张【没有一张是严格 1:1】——905x707、676x531、1268x1920 差得远，连
# 1918x1920 也差 2px。平台两条要求（1:1 且 >=800）是并列的，换勾选换不出合格图。
# 故与 ⑦b 同一取向：下载现有图、square_image 合规化后传回去顶替，画面一张不换。
#
# 【为什么连恰好 800x800 的也一起重做】同 media/preview.py 那段的理由：拒绝文案说
# 「不能小于 800*800」，字面上 800 应当通过；但逐张分别判「差比例还是差像素」会让
# 这里长出两条分支，而两条分支的产物都是同一个 square_image 调用。统一重做，代价
# 只是多传几张图（纯本地变换 + 直传，无 LLM），换来边界情况一次性消除。
#
# 【数量约束是硬红线】页面原文「产品轮播图最多选用10张，最少选用3张」。本阶段只做
# 「等量替换」：换掉几张就勾回几张，勾选总数一动不动，绝不因为替换失败就让勾选数
# 掉到 3 以下——那会把「图太小」换成「图太少」，同样发不出去。

# 轮播图要求：1:1 且不小于 800x800（页面原文「比例1:1，不小于800*800，大小在2M以内」）
CAROUSEL_MIN_SIDE = 800

# 在页面上认出「产品轮播图」那个 .img-list（三段 JS 共用，仿 variant_dom._JS_DIM_COLS
# 的注入做法）。
#
# 【为什么不按「产品轮播图」标签文字定位】2026-09-13 真站实测（编辑页
# id=184807703147300533）：页面上含「产品轮播图」四个字的元素有 33 个，而其中最深的
# 那些【全是素材图区的说明文字】——素材图区原文就写着「素材图将自动获取产品轮播图/
# 颜色图的第一张图片」。没有任何一个节点的 textContent 恰好等于「产品轮播图」，
# 故按标签文字找叶子节点恒空（阶段⑤c 首跑就是这么报「读不到产品轮播图区」的）；
# 而放宽成 includes 又会命中素材图那一区，等于把两区搞混。
#
# 【判据取「格子里有没有 checkbox」】同一页有 2 个 .img-list：轮播图区 18 格、
# 每格都有 input.ant-checkbox-input（多选语义）；素材图区 1 格、没有 checkbox
# （整个商品一张，不需要选）。这是两区唯一稳定的结构差异，且与 hash 类名无关。
# 多个候选时取格子最多的那个：轮播图候选池通常十几张，不会比别处少。
_JS_PICK_CAROUSEL_LIST = r"""
  function pickCarouselList() {
    const lists = Array.from(document.querySelectorAll('.img-list'));
    let cands = lists.filter(L => {
      const items = Array.from(L.querySelectorAll('.single-image'));
      if (!items.length) return false;
      return items.some(el => !!el.querySelector('input.ant-checkbox-input'));
    });
    if (!cands.length) {
      cands = lists.filter(L => Array.from(L.querySelectorAll('.single-image')).some(el =>
        !!el.querySelector('.img-size') && !!el.querySelector('img')));
    }
    if (!cands.length) return null;
    cands.sort((a, b) => b.querySelectorAll('.single-image').length
                       - a.querySelectorAll('.single-image').length);
    return cands[0];
  }
"""

# 勾选数量约束（页面原文，2026-09-12 实测）
CAROUSEL_MIN_PICKED = 3
CAROUSEL_MAX_PICKED = 10


# 读产品轮播图区的现状：每格的图 URL、尺寸、勾选态，并标出不合规的格子。
#
# 【区块按「产品轮播图」标签文字向上找，不写死 class】该区外层是
# .ant-row.ant-form-item-row（css-l8jjhh 这种 hash 类名会随构建变），而页面上
# .img-list 不止一处（素材图区、颜色图区都有类似结构）。从标签文字出发向上找到
# 含 .img-list 的祖先，是唯一能把这一区与其它图区区分开的锚点。
#
# 【尺寸取 .img-size 文本而不是 naturalWidth】格子里的 img 是缩略图（w-120 类，
# 显示宽 120px），naturalWidth 读到的是缩略图尺寸，不是源图尺寸。页面自己在每格
# 下方渲染了「899 X 899」这样的原图尺寸文本，那才是平台校验用的值。
# 文本读不到（渲染慢）时按【读不到】处理，不判不合格：未知不等于不合格
# （同 media/preview.py 与 upload_image 尺寸闸的取向）。
_JS_CAROUSEL_STATE = r"""(() => {
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  __PICK__
  const list = pickCarouselList();
  if (!list) return JSON.stringify({err: 'no-carousel-list'});
  const MIN = __MIN__;
  const items = [];
  Array.from(list.querySelectorAll('.single-image')).forEach((el, i) => {
    const im = Array.from(el.querySelectorAll('img'))
      .find(x => (x.currentSrc || x.src || '').startsWith('http'));
    const src = im ? (im.currentSrc || im.src || '') : '';
    const sizeText = txt(el.querySelector('.img-size'));
    const m = /(\d+)\s*[xX×]\s*(\d+)/.exec(sizeText);
    const w = m ? parseInt(m[1], 10) : 0;
    const h = m ? parseInt(m[2], 10) : 0;
    const known = w > 0 && h > 0;
    const square = known && Math.abs(w / h - 1) < 0.01;
    const cb = el.querySelector('input.ant-checkbox-input');
    const checked = /(^|\s)checked(\s|$)/.test(el.className) || !!(cb && cb.checked);
    items.push({i: i, url: src, w: w, h: h, sizeText: sizeText,
                checked: checked, known: known, hasCheckbox: !!cb,
                bad: known && checked && (!square || w < MIN || h < MIN)});
  });
  return JSON.stringify({items: items,
                         picked: items.filter(it => it.checked).length,
                         total: items.length,
                         badPicked: items.filter(it => it.bad).length});
})()"""


async def carousel_state(session: BrowserSession) -> dict:
    """读产品轮播图区现状。返回 {"items": [...], "picked": N, "supported": bool|None}。

    supported=None：区块没找到或零格时证据不足，别当「不支持」，让真失败暴露
    （同 sku_preview_state 的取向）。

    【必须先等图格渲染，不能 open_edit 一回来就读】2026-09-13 实测：open_edit 的
    加载判据是「skuDataInfo 区块出现」，那是页面靠后的变种信息区；而轮播图在靠前的
    产品信息区，图格由 Vue 另行渲染，此刻往往一个都还没挂上。表现为阶段⑤c 在 0.3s
    内返回 no-carousel-list，而人在页面上看得清清楚楚（同一个 carousel_state 手动
    sleep 3 秒后读就完全正常）。判据取「带 checkbox 的图格出现」，与 pickCarouselList
    的定位判据同一口径；超时给到 8s——18 张缩略图比表单项慢，比 inspect 那两处的
    2.0s 宽一些。
    """
    await common._poll_until(
        lambda: session.eval_json(r"""(() => {
            const n = Array.from(document.querySelectorAll('.img-list .single-image'))
              .filter(el => !!el.querySelector('input.ant-checkbox-input')).length;
            return JSON.stringify({n: n});
        })()"""),
        lambda d: d.get("n", 0) > 0,
        timeout=8.0)
    st = await session.eval_json(
        _JS_CAROUSEL_STATE.replace("__MIN__", J(CAROUSEL_MIN_SIDE))
                          .replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
    if st.get("err"):
        return {"supported": None, **st}
    if not (st.get("items") or []):
        return {"supported": None, **st}
    return {"supported": True, **st}


# 轮播图区「选择图片」是【下拉菜单触发器】，不是直接开弹窗的按钮。
#
# 2026-09-13 真站实测（编辑页 id=184807703147300533）：按钮的 DOM 是
#   <button><span>选择图片</span><span class="iconfont icon_down"></span></button>
# 那个 icon_down 就是线索——点它展开一个 4 项菜单：
#   本地图片 / 空间图片 / 网络图片 / 引用采集图片
# 要的是「空间图片」（与 ⑥⑦⑦b 同一个图片空间弹窗）。原先直接等 .ant-modal 出现，
# 于是恒判 opened=false（菜单确实开了，但弹窗要再点一层才有）。
#
# 【必须 CDP 真实鼠标点击，JS click 与 Playwright locator.click 都被吞】同日实测三种
# 点法：JS el.click() 与 session.mouse_click 都返回成功、但页面上【连 dropdown 实例都
# 没创建】（.ant-dropdown 数量恒 0）；只有 CDP Input.dispatchMouseEvent（mouseMoved +
# mousePressed + mouseReleased）能真正展开菜单。这与 banjia.py 那条「编辑页按钮 JS
# click 有效、搬家页复选框必须真实点击」的结论【方向相反】，印证了那段注释的告诫：
# 每个场景都要单独实测，不能跨场景假定继承。
# 故本函数拆成「JS 定坐标 → Python 走 CDP 点击 → JS 点菜单项」三步，由
# open_carousel_space 编排。
_JS_CAROUSEL_BTN_POS = r"""(() => {
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  __PICK__
  const list = pickCarouselList();
  if (!list) return JSON.stringify({err: 'no-carousel-list'});
  // 「选择图片」按钮要在【本区块内】找：从轮播图列表向上找到那一栏的表单容器。
  // 【为什么不用 .ant-form-item.mainImage 直接选】那个 class 是实测所见，但同为
  // hash 之外的业务类名，未必每个类目/版本都在；从 list 上溯是与 pickCarouselList
  // 同源的定位，不额外引入一个可能失效的锚点。
  let sec = list;
  for (let k = 0; k < 8 && sec; k++) {
    const p = sec.parentElement;
    if (!p) break;
    sec = p;
    if (Array.from(sec.querySelectorAll('button')).some(e => txt(e) === '选择图片')) break;
  }
  const btn = Array.from(sec.querySelectorAll('button')).find(e => txt(e) === '选择图片');
  if (!btn) return JSON.stringify({err: 'no-select-button'});
  btn.scrollIntoView({block: 'center'});
  const r = btn.getBoundingClientRect();
  if (!(r.width > 0 && r.height > 0)) return JSON.stringify({err: 'button-not-visible'});
  return JSON.stringify({x: Math.round(r.x + r.width / 2),
                         y: Math.round(r.y + r.height / 2)});
})()"""


# 点展开后的菜单里那一项（默认「空间图片」），再回读图片空间弹窗有没有打开。
#
# 【菜单项用 JS click 即可】被吞的只有那个 dropdown 触发器按钮；菜单项本身是
# 菜单实例自己的 li，JS 点击有效（同 ⑦b 展开后点「空间图片」的做法）。
# 【菜单要按项集合认，不能全局取第一个 .ant-dropdown】页面上 ⑥⑦⑦b 各自也有菜单
# 实例，认错会点到别处的同名项。「引用采集图片」是本菜单的特征项（⑦b 空格菜单那
# 5 项里叫「引用产品轮播图」，两者刚好互斥），故用它 + 「空间图片」唯一认出本菜单。
_JS_PICK_CAROUSEL_MENU = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const WANT = __WANT__, FEAT = __FEAT__;
  const dds = Array.from(document.querySelectorAll('.ant-dropdown'))
    .filter(d => !d.classList.contains('ant-dropdown-hidden') && d.offsetHeight > 0);
  if (!dds.length) return JSON.stringify({stage: 'menu', err: 'no-dropdown'});
  let hit = null, seen = [];
  for (const d of dds) {
    const items = Array.from(d.querySelectorAll('li, .ant-dropdown-menu-item'));
    const texts = items.map(li => txt(li)).filter(Boolean);
    seen.push(texts);
    if (texts.some(t => t === FEAT) && texts.some(t => t === WANT)) {
      hit = items.find(li => txt(li) === WANT);
      if (hit) break;
    }
  }
  if (!hit) return JSON.stringify({stage: 'menu', err: 'no-menu-item', seen: seen});
  hit.click();
  await sleep(1500);
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  return JSON.stringify({stage: 'open', opened: !!modal,
                         title: modal ? txt(modal.querySelector('.ant-modal-title')) : ''});
})()"""

# 轮播图「选择图片」菜单的项（2026-09-13 实测共 4 项）。
# 「引用采集图片」是本菜单独有的特征项，用于在页面众多 .ant-dropdown 里认出它。
CAROUSEL_MENU_WANT = "空间图片"
CAROUSEL_MENU_FEATURE = "引用采集图片"


# 按下标切换轮播图格子的勾选态。
#
# 【点 input.ant-checkbox-input 而不是整格】整格绑的是「预览大图」（can-image-viewer
# 类），点它会弹出图片查看器盖住页面；勾选只认 checkbox 那个 input。
# 【一次只切一格 + 回读】与 _pick_many_from_space 同一理由：批量连点在真站上出现过
# 「诊断说已改、判据恒 false」的自相矛盾，分成独立往返每次都是全新上下文。
_JS_TOGGLE_CAROUSEL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  __PICK__
  const IDX = __IDX__, WANT = __WANT__;
  const list = pickCarouselList();
  if (!list) return JSON.stringify({stage: 'locate', err: 'no-carousel-list'});
  const items = Array.from(list.querySelectorAll('.single-image'));
  const el = items[IDX];
  if (!el) return JSON.stringify({stage: 'locate', err: 'slot-missing: ' + IDX,
                                  count: items.length});
  const cb = el.querySelector('input.ant-checkbox-input');
  if (!cb) return JSON.stringify({stage: 'locate', err: 'no-checkbox'});
  const was = /(^|\s)checked(\s|$)/.test(el.className) || !!cb.checked;
  if (was === WANT) return JSON.stringify({stage: 'ok', changed: false,
                                           was: was, now: was});
  el.scrollIntoView({block: 'center'});
  await sleep(300);
  cb.click();
  await sleep(600);
  const now = /(^|\s)checked(\s|$)/.test(el.className) || !!cb.checked;
  return JSON.stringify({stage: now === WANT ? 'ok' : 'readback',
                         changed: now !== was, was: was, now: now,
                         err: now === WANT ? '' : 'toggle-no-effect'});
})()"""


async def open_carousel_space(session: BrowserSession) -> dict:
    """点轮播图区「选择图片」→「空间图片」，打开图片空间弹窗。

    三步：JS 定坐标 → CDP 真实鼠标点击展开菜单 → JS 点「空间图片」。
    为什么必须这么拆（那个按钮吞 JS click 与 Playwright click），见
    _JS_CAROUSEL_BTN_POS 上方的实测记录。
    """
    pos = await session.eval_json(
        _JS_CAROUSEL_BTN_POS.replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
    if pos.get("err"):
        return {"stage": "locate", **pos}

    # CDP 真实鼠标事件：先移过去再按下抬起。三个事件缺一不可——只发 pressed/released
    # 而不先 mouseMoved 时，实测有概率不展开（hover 态没建立）。
    for params in ({"type": "mouseMoved", "x": pos["x"], "y": pos["y"]},
                   {"type": "mousePressed", "x": pos["x"], "y": pos["y"],
                    "button": "left", "clickCount": 1},
                   {"type": "mouseReleased", "x": pos["x"], "y": pos["y"],
                    "button": "left", "clickCount": 1}):
        r = await session.cdp("Input.dispatchMouseEvent", params)
        if not r.get("ok"):
            return {"stage": "click", "err": f"CDP 点击失败: {r.get('err')}"}
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.9)

    return await session.eval_json(
        _JS_PICK_CAROUSEL_MENU.replace("__WANT__", J(CAROUSEL_MENU_WANT))
                              .replace("__FEAT__", J(CAROUSEL_MENU_FEATURE))
                              .replace("__TITLE__", J(media_space.SPACE_MODAL_TITLE)))


async def toggle_carousel(session: BrowserSession, idx: int, want: bool) -> dict:
    """把第 idx 格的勾选态切成 want（已是该状态则不动，changed=False）。"""
    return await session.eval_json(
        _JS_TOGGLE_CAROUSEL.replace("__IDX__", J(idx)).replace("__WANT__", J(want))
                           .replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
