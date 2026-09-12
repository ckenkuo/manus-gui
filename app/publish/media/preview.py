"""店小秘发布操作：media.preview。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
from app.logger import logger
from app.publish import variant_dom
from app.publish.browser import BrowserSession, J
from app.publish.media import space as media_space
from app.publish.upload import upload_image
from typing import Optional


# ==================== 阶段⑦b SKU 预览图 ====================
# 【预览图是第三处图片位，与 ⑥ 素材图、⑦ SKC 颜色图都不是同一个地方】
# 2026-08-30 取证（rowid 173539495459009087，玩具类 8 颜色无尺码）：发布被平台拒
# 「错误：预览图尺寸不能小于800*800」，而管线此前【没有任何代码碰过这一列】。
# 三处的容器各不相同：
#   ⑥ 素材图      .material-img-module                  整个商品一张
#   ⑦ SKC 颜色图  #skuAttrsInfo（变种【属性】区）        每颜色 3~10 张
#   ⑦b SKU 预览图 #skuDataInfo（变种【信息】表）第一列   每 SKU 行一张
#
# 【为什么阶段⑦ 跳过了却仍被拒】skc_image_support 探的是 #skuAttrsInfo，玩具类那个
# 区块确实没有图位（8 个复选框全是颜色），判 supported=False → ⑦ 正确地 skipped。
# 但预览图在 #skuDataInfo，两者正交：⑦ 该跳过，⑦b 仍然必须跑。服装类此前没暴露是
# 因为 1688 服装主图普遍 >=800x800 恰好蒙过，玩具类源图小才撞线（实测那 8 行：
# 720x606 / 749x627 / 717x610 三张破线，另有 3 张恰好 800x800、1 张 1920x1920）。
#
# 【不重判颜色归属，只补几何——与 _skc_size_fallback 同一取向】认领时店小秘已按
# SKU 把每行的图带过来了，归属本来就对（实测 8 行 6 个不同 src）。这里下载现有图、
# 做 1:1 合规化后原位换回：画面一张不换、行序一动不动，只补像素与比例。故本阶段
# 【零 LLM 调用】，也不需要 vision.plan_skc 那套按颜色分图。
#
# 【规格取素材图那条页面原文】页面在素材图区写着「素材图尺寸要求比例为1：1，
# 不小于800*800」，预览图列自己没写提示，但拒绝文案与之逐字一致。故走
# images.square_image（中心裁 1:1 + 放大到 1785 方图）：一次同时满足 1:1 与
# >=800x800，且与阶段⑥ 同规格。
#
# 【为什么连恰好 800x800 的也一起重做】拒绝文案说的是「不能小于 800*800」，字面上
# 800 应当通过；但同一批里 720x606 那几张连【1:1 比例】都不满足，而比例是平台明写的
# 另一条要求。逐行分别判「差比例还是差像素」会让这里长出两条分支，而两条分支的产物
# 都是同一个 square_image 调用。故统一重做，代价只是多传几张图（纯本地变换 + 直传，
# 无 LLM），换来的是边界情况一次性消除。
#
# 【交互形态 2026-08-30 三轮真站探查确认，勿照搬 ⑥⑦ 的做法】
#   - 触发器：td > div.sku-image-box.ant-dropdown-trigger
#   - 【合成 hover 即可展开菜单】不必像 ⑦ SKC 行那样走 CDP 真实点击。⑦ 那条约束的
#     成因是「JS click 会命中素材图残留的菜单实例」，这里的菜单是行内 trigger 自己
#     的实例，hover 展开后菜单项集合就能唯一认出（见下方 SKU_PREVIEW_MENU_ITEMS）。
#   - 菜单 8 项，含「空间图片」（与素材图同名；注意描述图那处叫「空间【上传】」，
#     三处文案不统一，每处都必须实测，别互相照抄）。
#   - 弹窗与 ⑥⑦ 是【同一个组件实例】：标题「从图片空间选择」、20 个 .img-item、
#     .img-check 文本「点击选择」、计数器「已选择N张图片」全部一致，故 _pick_from_space
#     可以直接复用，不必另写选图逻辑。
#   - 菜单里有「应用到全部」，能一次给所有行赋同一张图。【刻意不用】：那会让 8 个
#     颜色共用一张预览图，等于把每个 SKU 的辨识度抹掉，人工发布也不会这么做。

# 预览图行内菜单的特征菜单项（2026-08-30 实测该菜单共 8 项）。
# 用于在页面众多 .ant-dropdown 实例里认出它——「应用到全部」是它独有的特征项，
# 素材图菜单只有 4 项且没有这一项，故两者可明确区分（同 ⑦ 靠「应用到所有颜色」区分）。
SKU_PREVIEW_MENU_ITEMS = ("空间图片", "应用到全部")


# 空预览格的菜单（2026-09-08 真站取证）：与有图格的两处不同——【点击】展开而非
# hover、共 5 项且【没有】「应用到全部」。独有特征是「引用产品轮播图」（素材图菜单
# 4 项没有它、有图格 8 项菜单也没有它），故用它 + 「空间图片」就能唯一认出空格菜单。
SKU_PREVIEW_EMPTY_MENU_ITEMS = ("本地图片", "空间图片", "网络图片",
                                "引用产品轮播图", "引用采集图片")


# 预览图要求：1:1 且不小于 800x800（页面素材图区原文，拒绝文案与之一致）
PREVIEW_MIN_SIDE = 800


# 读变种信息表每行的预览图状态：颜色名 + 图 URL + 尺寸，并标出不合规的行。
#
# 【表格与列都按结构定位，不写死下标】#skuDataInfo 里有两张 table（第一张是价格/
# 尺寸/重量，第二张是库存/SKU分类），预览图在【第一张】的第一列。
#
# 【行标识列取「第一个变种维」，不是硬找「颜色」】与 ⑩a 共用 variant_dom._JS_DIM_COLS
# （预览图之后、SKU货号之前即变种维列）。2026-09-12 取证（Temu 商品 601101104447803
# 车贴）：该类目唯一那维叫【型号】，原判据 /^颜色/ 落空 → colorIdx=-1，于是
# sku_preview_replace_row 的 expect_color 恒为空、行序核对整体失效（Vue 重排后会把图
# 挂到别的 SKU 上），空位补图也拿不到「同色行」去找源图。名字随类目变、位置不变。
#
# bad 的判据是「非 1:1 或短边 < 800」，与 PREVIEW_MIN_SIDE 一致。naturalWidth 为 0
# 表示图还没加载完，按【读不到】处理而不是判不合格：未知不等于不合格（同
# upload_image 尺寸闸的取向）。
#
# 【「空图位」必须与「尺寸未知」分开，empty 单独一个字段】2026-09-01 取证（两单
# 1067271196776、1051827161006）：save 被平台拒「错误：请上传预览图」，而 ⑦b 之前
# 判的是 skipped、note 还写「6 行预览图均已满足 1:1 且不小于 800x800」——因为原判据
# 只看得见 <img>，格子里压根没有 img 的行既进不了 bad、也进不了 unknown（unknown 要
# 求有 img 但 naturalWidth=0），于是被当成合规行放过，错误一路延后到 ⑭ 才以「保存
# 可能未生效」这种含糊结论暴露。
# 「没有图」是确定性的不合格，与「图在加载」相反：前者要去上传，后者要等或跳过。
# 判据取【格子里有没有 trigger 却没有任何 http 图源】——trigger 在说明这一列可换图。
_JS_SKU_PREVIEW_STATE = r"""(() => {
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const t0 = sku.querySelector('table');
  if (!t0) return JSON.stringify({err: 'no-table'});
  const heads = Array.from(t0.querySelectorAll('thead th')).map(th => txt(th));
  const iPrev = heads.findIndex(h => h.includes('预览图'));
  __DIM_COLS__
  const {colorIdx: iColor} = dimIdx(heads);
  if (iPrev < 0) return JSON.stringify({err: 'no-preview-column', heads: heads});
  const MIN = __MIN__;
  const rows = [];
  Array.from(t0.querySelectorAll('tbody tr')).forEach((tr, i) => {
    const tds = Array.from(tr.children);
    const cell = tds[iPrev];
    if (!cell) return;
    const im = Array.from(cell.querySelectorAll('img'))
      .find(x => (x.currentSrc || x.src || '').startsWith('http'));
    const src = im ? (im.currentSrc || im.src || '') : '';
    // 【空图位识别必须先于「图太小」判】店小秘空位占位符 addImg-*.jpg 也是 http 图、
    // 尺寸 200x200（.no-img-status 类），按真实图判会落进 bad（200<800），而它其实
    // 压根没图。2026-09-08 取证（offer 1011303528447 狗裙子，5 尺码里 XL 行空位）：
    // 阶段⑦b 因此判「5 行均满足」静默放过，直到 ⑭ 保存才报「请上传预览图」。
    const placeholder = /addImg[^/]*\.(jpg|jpeg|png)/i.test(src)
      || !!cell.querySelector('.no-img-status');
    const empty = placeholder || !im;
    const w = empty ? 0 : (im.naturalWidth || 0);
    const h = empty ? 0 : (im.naturalHeight || 0);
    // 未知尺寸(w/h=0)不判 bad：图没加载完不等于不合格
    const known = w > 0 && h > 0;
    const square = known && Math.abs(w / h - 1) < 0.01;
    rows.push({i: i, color: iColor >= 0 ? txt(tds[iColor]) : '',
               url: empty ? '' : src,
               w: w, h: h, empty: empty,
               // 行级换图入口：变种表只有部分行（颜色主行）有 trigger，其余行共享
               // 主图、无独立换图入口（2026-09-06 两单宠物窝全卡在这里，见 _st_sku_preview）
               hasTrigger: !!cell.querySelector('.sku-image-box.ant-dropdown-trigger'),
               // 空位补图入口：空格的 .single-image 图格【点击】即出「空间图片」菜单
               // （与有图格的 hover trigger 是两种交互），据此判断空位能否自动补图。
               hasFillSlot: !!cell.querySelector('.single-image'),
               bad: !empty && known && (!square || w < MIN || h < MIN)});
  });
  return JSON.stringify({rows: rows, heads: heads,
                         previewIdx: iPrev, colorIdx: iColor,
                         hasTrigger: !!t0.querySelector(
                           'tbody tr .sku-image-box.ant-dropdown-trigger')});
})()"""


async def sku_preview_state(session: BrowserSession) -> dict:
    """读变种信息表每行预览图的现状。返回 {"rows": [...], "supported": bool|None, ...}。

    有预览图列和数据行即 supported=True；是否能换图由每行入口单独判断。
    supported=None：表没渲染完（零行）时证据不足，别当「不支持」，让真失败暴露。
    """
    st = await session.eval_json(
        _JS_SKU_PREVIEW_STATE.replace("__MIN__", J(PREVIEW_MIN_SIDE))
                             .replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    if st.get("err"):
        return {"supported": None, **st}
    rows = st.get("rows") or []
    if not rows:
        return {"supported": None, **st}
    return {"supported": True, **st}


# 悬停某行预览图展开菜单并点「空间图片」，打开空间弹窗。
#
# 【整段放在一个 evaluate 里】与 ⑥ 素材图同一个理由：hover 菜单会因失焦自动收起，
# 拆成多次往返时中间那步可能落在已消失的 DOM 上。
#
# 【按行下标定位而不按颜色名匹配】读与写之间本阶段不点任何东西、行序不会变；
# 但仍回传该行【当前】颜色名，由调用方核对与读到时是否一致——Vue 若在两次 eval
# 之间重排过，宁可跳过那行报出来，也不能把图挂到别的 SKU 上（同 _JS_FILL_SKU_CODES
# 的取向：填错比不填更难查）。
_JS_OPEN_SKU_PREVIEW_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const IDX = __IDX__, PREV = __PREV__, ICOLOR = __ICOLOR__;
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({stage: 'locate', err: 'no-skuDataInfo'});
  const t0 = sku.querySelector('table');
  if (!t0) return JSON.stringify({stage: 'locate', err: 'no-table'});
  const trs = Array.from(t0.querySelectorAll('tbody tr'));
  const tr = trs[IDX];
  if (!tr) return JSON.stringify({stage: 'locate', err: '行不存在: ' + IDX,
                                  rowCount: trs.length});
  const tds = Array.from(tr.children);
  const cell = tds[PREV];
  if (!cell) return JSON.stringify({stage: 'locate', err: '该行没有预览图单元格'});
  const nowColor = ICOLOR >= 0 ? txt(tds[ICOLOR]) : '';
  const trig = cell.querySelector('.sku-image-box.ant-dropdown-trigger');
  if (!trig) return JSON.stringify({stage: 'locate', err: '该行没有预览图 trigger',
                                    nowColor: nowColor});
  const before = (() => {
    const im = Array.from(cell.querySelectorAll('img'))
      .find(x => (x.currentSrc || x.src || '').startsWith('http'));
    return im ? (im.currentSrc || im.src || '') : '';
  })();
  trig.scrollIntoView({block: 'center'});
  await sleep(700);
  // 【合成 hover 即可展开——2026-08-30 实测】trigger 与它内层的 .single-image
  // 都派发：实测 trigger 那层就绑了事件，但两个都发更稳（多余那次无副作用，
  // 同 _JS_OPEN_MATERIAL_SPACE 的做法）
  const inner = trig.querySelector('.single-image') || trig;
  [trig, inner].forEach(el => {
    if (!el) return;
    ['mouseenter', 'mouseover', 'mousemove'].forEach(t =>
      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  });
  // 【轮询等菜单，不硬等】菜单是同步渲染（实测 hover 后 1.2s 内必现），轮询命中即走；
  // 判「展开」只认 display:none 之外的实例——菜单 off-screen 停靠时 offsetHeight
  // 不可靠（见本模块阶段⑥⑦ 公共机制段落的第 3 条实测结论）
  const WANT = __ITEMS__;
  let menu = null;
  for (let k = 0; k < 30; k++) {
    menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
      if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
      const its = Array.from(d.querySelectorAll('.ant-dropdown-menu-item')).map(i => txt(i));
      return WANT.every(w => its.includes(w));
    });
    if (menu) break;
    await sleep(100);
  }
  if (!menu) return JSON.stringify({stage: 'menu', err: '悬停后预览图菜单未展开',
                                    nowColor: nowColor});
  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => txt(i) === '空间图片');
  if (!item) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间图片」项',
                                    items: Array.from(
                                      menu.querySelectorAll('.ant-dropdown-menu-item'))
                                      .map(i => txt(i))});
  item.click();
  // 轮询等弹窗（实测点后约 100ms 列表就绪，上限 6s 兜住慢的情况）
  let opened = false;
  for (let k = 0; k < 60; k++) {
    await sleep(100);
    opened = Array.from(document.querySelectorAll('.ant-modal'))
      .some(m => m.offsetHeight > 0 &&
        ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
    if (opened) break;
  }
  return JSON.stringify({stage: 'ok', opened: opened, nowColor: nowColor,
                         srcBefore: before});
})()"""


# 空预览格点开空间弹窗：点击图格（不是 hover trigger）展开 5 项菜单，点「空间图片」。
#
# 【与 _JS_OPEN_SKU_PREVIEW_SPACE 是两种交互，别混用】有图格靠 hover `.sku-image-box
# .ant-dropdown-trigger` 出 8 项菜单；空图位没有那个 trigger，格子里是 `.single-image`
# （占位符 addImg 图 + .no-img-status），【点击】它才出菜单，且菜单只有 5 项（没有
# 「应用到全部」，独有「引用产品轮播图」）。2026-09-08 真站取证（offer 1011303528447
# 狗裙子 XL 行空位）：点击 .img-out 出 ["本地图片","空间图片","网络图片",
# "引用产品轮播图","引用采集图片"]，点「空间图片」打开的是同一个「从图片空间选择」
# 弹窗（.img-item 20 个、_pick_from_space 可直接复用）。
#
# 除「点击 vs hover」外，其余与 _JS_OPEN_SKU_PREVIEW_SPACE 同构：按行下标定位、
# 回传当前颜色名供调用方核对行序、轮询等菜单/弹窗。
_JS_OPEN_SKU_PREVIEW_FILL_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const IDX = __IDX__, PREV = __PREV__, ICOLOR = __ICOLOR__;
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({stage: 'locate', err: 'no-skuDataInfo'});
  const t0 = sku.querySelector('table');
  if (!t0) return JSON.stringify({stage: 'locate', err: 'no-table'});
  const trs = Array.from(t0.querySelectorAll('tbody tr'));
  const tr = trs[IDX];
  if (!tr) return JSON.stringify({stage: 'locate', err: '行不存在: ' + IDX,
                                  rowCount: trs.length});
  const tds = Array.from(tr.children);
  const cell = tds[PREV];
  if (!cell) return JSON.stringify({stage: 'locate', err: '该行没有预览图单元格'});
  const nowColor = ICOLOR >= 0 ? txt(tds[ICOLOR]) : '';
  // 点击目标：空位的图格（.img-out 是 .single-image 内层、实测点它就能出菜单）
  const box = cell.querySelector('.img-out') || cell.querySelector('.single-image') || cell;
  const before = (() => {
    const im = Array.from(cell.querySelectorAll('img'))
      .find(x => (x.currentSrc || x.src || '').startsWith('http'));
    return im ? (im.currentSrc || im.src || '') : '';
  })();
  box.scrollIntoView({block: 'center'});
  await sleep(500);
  // 【点击展开，不是 hover】空位没有 trigger，hover 无菜单；点击才出。
  // 【必须用原生 box.click()，不能 dispatchEvent(new MouseEvent('click'))】2026-09-08
  // 实跑（offer 1044697103545 宠物裙 3色×4码，L/XL 码 6 行空位）发现 dispatchEvent 的
  // 合成 click 不触发店小秘空位菜单（轮询 3s 菜单仍 display:none），而 box.click() 原生
  // 方法能正常展开 5 项菜单——两者同为 isTrusted=false，但组件只认原生 click 的事件链。
  box.click();
  const WANT = __ITEMS__;
  let menu = null;
  for (let k = 0; k < 30; k++) {
    menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
      if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
      const its = Array.from(d.querySelectorAll('.ant-dropdown-menu-item')).map(i => txt(i));
      return WANT.every(w => its.includes(w));
    });
    if (menu) break;
    await sleep(100);
  }
  if (!menu) return JSON.stringify({stage: 'menu', err: '点击后空位菜单未展开',
                                    nowColor: nowColor});
  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => txt(i) === '空间图片');
  if (!item) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间图片」项',
                                    items: Array.from(
                                      menu.querySelectorAll('.ant-dropdown-menu-item'))
                                      .map(i => txt(i))});
  item.click();
  // 轮询等弹窗（与有图格同一组件实例，上限 6s 兜住慢的情况）
  let opened = false;
  for (let k = 0; k < 60; k++) {
    await sleep(100);
    opened = Array.from(document.querySelectorAll('.ant-modal'))
      .some(m => m.offsetHeight > 0 &&
        ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
    if (opened) break;
  }
  return JSON.stringify({stage: 'ok', opened: opened, nowColor: nowColor,
                         srcBefore: before});
})()"""


# 回读某行预览图的 src 与尺寸（替换后的成功判据）
_JS_SKU_PREVIEW_ROW_SRC = r"""(() => {
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const t0 = sku.querySelector('table');
  if (!t0) return JSON.stringify({err: 'no-table'});
  const tr = Array.from(t0.querySelectorAll('tbody tr'))[__IDX__];
  if (!tr) return JSON.stringify({err: 'no-row'});
  const cell = Array.from(tr.children)[__PREV__];
  if (!cell) return JSON.stringify({err: 'no-cell'});
  const im = Array.from(cell.querySelectorAll('img'))
    .find(x => (x.currentSrc || x.src || '').startsWith('http'));
  return JSON.stringify({src: im ? (im.currentSrc || im.src || '') : null,
                         w: im ? (im.naturalWidth || 0) : 0,
                         h: im ? (im.naturalHeight || 0) : 0});
})()"""


async def sku_preview_replace_row(session: BrowserSession, row_idx: int,
                                  image_path: str, preview_idx: int,
                                  color_idx: int = -1, expect_color: str = "",
                                  full_cid: Optional[str] = None,
                                  fill_empty: bool = False) -> dict:
    """阶段⑦b 换/补【一行】的 SKU 预览图：直传图床、菜单选「空间图片」、弹窗选图。

    image_path 必须是【已做过合规化】的图（1:1 且 >=800x800，走 images.square_image）：
    与 ⑥⑦ 一致，本函数不代做合规化——那是纯本地的确定性变换，由调用方先做好，
    免得这里既管页面交互又管图片处理、失败时分不清是哪一层的问题。

    preview_idx / color_idx 由 sku_preview_state 读出来传进来，不在这里重复解析表头：
    一批行共用一次表头解析，与「批次开始解析一次 SheetSchema 全批复用」同一取向。

    expect_color 非空时核对该行【当前】颜色名是否与读到时一致，不一致就拒绝替换——
    Vue 重排过的话，挂上去就是挂到别的 SKU 上了。

    fill_empty=True 时走【空位补图】交互：点击空格出菜单（不是 hover trigger），
    见 _JS_OPEN_SKU_PREVIEW_FILL_SPACE 的取证。其余流程（上传/选图/回读判据）一致。
    """
    up = await upload_image(session, image_path, full_cid=full_cid)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "row": row_idx, "upload": up}

    open_js = _JS_OPEN_SKU_PREVIEW_FILL_SPACE if fill_empty else _JS_OPEN_SKU_PREVIEW_SPACE
    menu_items = (("本地图片", "空间图片") if fill_empty
                  else SKU_PREVIEW_MENU_ITEMS)
    opened = await session.eval_json(
        open_js
        .replace("__IDX__", J(row_idx))
        .replace("__PREV__", J(preview_idx))
        .replace("__ICOLOR__", J(color_idx))
        .replace("__ITEMS__", J(list(menu_items)))
        .replace("__TITLE__", J(media_space.SPACE_MODAL_TITLE))
    )
    # 行序核对放在开弹窗【之后】：菜单已经绑定到这一行了，此时若发现颜色对不上，
    # 关掉弹窗即可，什么都没改动。放在之前则要多一次 eval 往返。
    now_color = opened.get("nowColor") or ""
    if expect_color and now_color and now_color != expect_color:
        # 多颜色商品（尤其各颜色尺码数不等）最容易撞这条：读状态与逐行替换之间
        # Vue 若重排过，按下标定位就会落到别的 SKU 上。拒绝替换是对的，但必须留证，
        # 否则事后只看到「N 行替换失败」，看不出是行序问题（2026-09-01 的盲区）。
        logger.warning(
            f"预览图第 {row_idx + 1} 行行序变了，拒绝替换："
            f"期望「{expect_color}」，现在是「{now_color}」")
        await media_space._close_space_modal(session)
        return {"status": "error", "stage": "row-moved", "row": row_idx,
                "err": f"行序变了：期望「{expect_color}」，现在是「{now_color}」",
                "upload": up}
    if opened.get("err") or not opened.get("opened"):
        # 与 pick/readback 一样落日志：本阶段失败会让整单发布被平台拦，
        # 而 service 那边只发 manual_check（不写日志文件），事后无从定位（2026-09-01
        # 排查 890185900190 时就卡在这个盲区）。
        logger.warning(
            f"预览图第 {row_idx + 1} 行打不开空间弹窗[{opened.get('stage') or '?'}]："
            f"{opened.get('err') or ''}"
            + (f"（页面共 {opened['rowCount']} 行）" if opened.get("rowCount") else "")
            + f" 当前颜色「{now_color}」")
        await media_space._close_space_modal(session)
        return {"status": "error", "stage": "open-space", "row": row_idx,
                "detail": opened, "upload": up}

    picked = await media_space._pick_from_space(session, up["fileId"])
    if picked.get("err"):
        # 【选图失败要把弹窗现状说全】同图逐行各传一次时，图床可能按内容去重、
        # 让本次的新 fileId 在弹窗里根本不存在（2026-09-01 890185900190 的疑点）。
        # 判断这一点只需要「想要的文件名 + 当前页有哪些名字 + 谁已被选中」，
        # 故这三样直接进日志，别只留一句「找不到刚上传的图」。
        logger.warning(
            f"预览图第 {row_idx + 1} 行选图失败[{picked.get('stage') or '?'}]："
            f"{picked.get('err') or ''}"
            + (f" | 想要 {picked['wantName']}" if picked.get("wantName") else "")
            + (f" | 弹窗 {picked['itemCount']} 项" if picked.get("itemCount") is not None else "")
            + (f" | 已选中项序 {picked['alreadySelected']}"
               if picked.get("alreadySelected") else "")
            + (f" | 当前页文件名 {picked['pageNames']}" if picked.get("pageNames") else ""))
        # 弹窗可能还开着挡住后续操作，尽力关掉（best-effort，失败不影响错误返回）
        await media_space._close_space_modal(session)
        return {"status": "error", "stage": "pick", "row": row_idx,
                "detail": picked, "upload": up}

    after = await session.eval_json(
        _JS_SKU_PREVIEW_ROW_SRC.replace("__IDX__", J(row_idx))
        .replace("__PREV__", J(preview_idx)))
    fid = up["fileId"].rsplit("/", 1)[-1]
    for attempt in range(10):
        if fid in (after.get("src") or "") and after.get("w") and after.get("h"):
            break
        await asyncio.sleep(0.3)
        after = await session.eval_json(
            _JS_SKU_PREVIEW_ROW_SRC.replace("__IDX__", J(row_idx))
            .replace("__PREV__", J(preview_idx)))
    # 【成功判据是回读到的 src 含新 fileId】只看「src 变了」不够：挂错行时本行 src
    # 同样可能变（原脚本的素材图误替换事故正是这么发生的，见 set_material 的注释）
    width, height = after.get("w") or 0, after.get("h") or 0
    ok = (fid in (after.get("src") or "") and min(width, height) >= PREVIEW_MIN_SIDE
          and abs(width / height - 1) < 0.01)
    if not ok:
        logger.error(f"预览图第 {row_idx + 1} 行替换后回读不含新 fileId："
                     f"before={(opened.get('srcBefore') or '')[-50:]} "
                     f"after={(after.get('src') or '')[-50:]}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "readback",
            "row": row_idx, "color": now_color, "upload": up,
            "fileId": up["fileId"],
            "srcBefore": opened.get("srcBefore"), "srcAfter": after.get("src"),
            "sizeAfter": {"w": after.get("w"), "h": after.get("h")}}
