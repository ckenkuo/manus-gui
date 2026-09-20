"""店小秘发布操作：media.carousel。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio

from app.logger import logger
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
# 【判据取「表单项标签恰好是『产品轮播图』」】同一页有 2 个 .img-list（轮播图区、
# 素材图区），要认对必须有个与两区都不共享的锚点。
#
# 2026-09-13 曾记「没有任何节点的 textContent 恰好等于『产品轮播图』，故按标签文字定位
# 恒空」——那条结论的范围搞错了：页面上含这四个字的元素确有 33 个（最深的那些全是素材图
# 区的说明文字，原文「素材图将自动获取产品轮播图/颜色图的第一张图片」），但把选择范围
# 限定在 .ant-form-item-label 上就唯一了。2026-09-20 实测三种形态下
# `txt(label) === '产品轮播图'` 都恰好命中那一栏的标签，素材图区的说明文字不带这个类。
#
# 【为什么不再按「格子里有没有 checkbox」认】那是原先的判据，它在已上架商品上失效：
#   184807703152448649（草稿，玩具）   15 格、15 个 checkbox、已选 5 张
#   184807703147300533（已上架，玩具） 5 格、【0 个 checkbox】、格子带 checked-in-box 类
#   682542618799 等（服装）            稳态没有这一区
# 已上架商品不允许改选用图，页面就不渲染 checkbox，于是认区判据落空、报 no-carousel-list。
# 2026-09-20 回归时正是被这单挡下来的（它本该判 supported=True）。
#
# 【原先那条回退分支为什么也不能留】它在主判据落空时改筛「有 .img-size 且有 img」的列表，
# 而素材图区恰好满足、且它就 1 格，于是 items 非空、supported 误判成 True：
# 2026-09-20 批（682542618799、1011826279690）两单就栽在这里——该格没有 checkbox 故
# checked 恒 false、picked 空、adds 空、target 空，最后报出一句方向完全错的
# 「轮播图区没有可选用的图片，需人工补图」，而真因是服装类压根没有这一区。
# 两单下载到的唯一候选 pool-00.jpg 与 main-01.jpg 的 md5 逐字节相同，可反证认错了区。
# 改按标签认区后，这两种情形各自落到正确的结论上，不需要任何回退。
_JS_PICK_CAROUSEL_LIST = r"""
  function pickCarouselList() {
    const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
    const label = Array.from(document.querySelectorAll('.ant-form-item-label'))
      .find(el => txt(el) === '产品轮播图');
    if (!label) return null;
    // 从标签向上找到这一栏的表单项容器，再在【本栏内】取 .img-list，
    // 免得越界拿到素材图区那一个（同 _JS_CAROUSEL_BTN_POS 的上溯做法）。
    let sec = label;
    for (let k = 0; k < 8 && sec; k++) {
      const p = sec.parentElement;
      if (!p) break;
      sec = p;
      const found = sec.querySelector('.img-list');
      if (found) return found;
    }
    return null;
  }
"""

# 勾选数量约束（页面原文，2026-09-12 实测）
CAROUSEL_MIN_PICKED = 3
CAROUSEL_MAX_PICKED = 10

# 单张体积上限（页面原文「大小在2M以内」）。此前全链路无人校验这条：upload_image 只查
# 尺寸，square_image/compress 只管像素。⑤c 的生图产物（1:1 高清出图）与合规化产物都有
# 超限的可能，故在备料收尾处按这条卡一道。
CAROUSEL_MAX_BYTES = 2 * 1024 * 1024


# 展开候选列表的「查看更多」。
#
# 【为什么必须点】池子超过约 21 格时，页面把后面的折叠起来、那些格子【压根不渲染】：
# 2026-09-17 真站实测同一个编辑页，折叠态读到 21 格、点开后 36 格（另一个商品
# 21 -> 33）。不展开就等于只看前 21 张候选，尺码表/产品介绍图若排在后面就永远补勾
# 不到——而补勾正是 ⑤c 的职责之一。
#
# 【JS click 就够，不需要 CDP 真实点击】与「选择图片」那个触发器不同（它吞 JS click，
# 见 _JS_CAROUSEL_BTN_POS 上方的实测记录），这个 span.link.view-more 是普通链接，
# 真站实测 el.click() 一次就展开（21 -> 33 且按钮自身消失）。故不做坐标点击那一套。
_JS_EXPAND_POOL = r"""(() => {
  const txt = el => ((el||{}).textContent || '').replace(/\s+/g, ' ').trim();
  const btn = Array.from(document.querySelectorAll('span.link.view-more'))
    .find(el => txt(el) === '查看更多' && el.offsetHeight > 0);
  if (!btn) return JSON.stringify({expanded: false, why: 'no-button'});
  btn.scrollIntoView({block: 'center'});
  btn.click();
  return JSON.stringify({expanded: true});
})()"""


async def expand_carousel_pool(session: BrowserSession) -> dict:
    """把轮播图候选列表展开（本来就没折叠时原样返回，不报错）。

    判据取「折叠按钮消失」而不是 sleep 固定时长：按钮消失说明列表已重渲染完，
    与 _JS_PICK_CAROUSEL_MENU 那段「等弹窗要轮询」同一取向。等不到也不抛——后面
    carousel_state 自己还有一道等图格渲染的轮询，这里只是尽量把候选读全。
    """
    first = await session.eval_json(_JS_EXPAND_POOL)
    if not first.get("expanded"):
        return first
    await common._poll_until(
        lambda: session.eval_json(r"""(() => JSON.stringify({n: document.querySelectorAll(
            'span.link.view-more').length}))()"""),
        lambda d: d.get("n", 1) == 0,
        timeout=8.0)
    return first


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
  // 【两个标签位与图格在同一次快照里返回】轮播项被移除的瞬间若夹在两次 eval 之间，
  // 「有没有这一区」与「区里几格」就会自相矛盾（见 carousel_state 里那段竞态取证）。
  // hasCarouselLabel 判本类目有没有这一区，hasMaterialLabel 判产品信息栏渲染到位没有。
  const labels = Array.from(document.querySelectorAll('.ant-form-item-label'))
    .map(el => txt(el));
  const flags = {hasCarouselLabel: labels.some(t => t === '产品轮播图'),
                 hasMaterialLabel: labels.some(t => t === '产品素材图')};
  const list = pickCarouselList();
  if (!list) return JSON.stringify({err: 'no-carousel-list', ...flags});
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
    const square = known && w === h;
    const cb = el.querySelector('input.ant-checkbox-input');
    const checked = /(^|\s)checked(\s|$)/.test(el.className) || !!(cb && cb.checked);
    items.push({i: i, url: src, w: w, h: h, sizeText: sizeText,
                checked: checked, known: known, hasCheckbox: !!cb,
                bad: known && checked && (!square || w < MIN || h < MIN)});
  });
  return JSON.stringify({items: items,
                         picked: items.filter(it => it.checked).length,
                         total: items.length,
                         badPicked: items.filter(it => it.bad).length,
                         ...flags});
})()"""


# 在页面上装一个 MutationObserver，记下「DOM 最后一次变动距今多久」。
#
# 【为什么监听整个 body 而不是只监听那一栏】要等的恰恰是「那一栏被整项移除」这件事，
# 移除发生在它的祖先上；只观察该栏自身，节点一被摘走就再也收不到通知。而 body 级监听
# 会不会被页面别处的动静（客服组件、余额刷新、图片懒加载）搅得永不静止，是实测过的：
# 2026-09-20 三个编辑页各连读十几秒，变动次数在开头几百毫秒内就跑完并冻住
# （草稿服装 225 次、草稿玩具 90 次、已上架玩具 0 次），此后 quiet 单调涨到 13s+，
# 没有任何后台组件在持续扰动。故 body 级监听是安全的，也是唯一能覆盖整项移除的范围。
_JS_INSTALL_DOM_WATCH = r"""(() => {
  // 重复装载要先断开旧的：同一页面会被多个阶段反复读，累积 observer 纯属浪费。
  if (window.__dxmCarouselWatch && window.__dxmCarouselWatch.obs) {
    window.__dxmCarouselWatch.obs.disconnect();
  }
  const st = {last: performance.now(), count: 0};
  const obs = new MutationObserver(list => {
    st.last = performance.now();
    st.count += list.length;
  });
  obs.observe(document.body, {childList: true, subtree: true});
  st.obs = obs;
  window.__dxmCarouselWatch = st;
  return JSON.stringify({ok: true});
})()"""

# 读「距最后一次 DOM 变动过了多少毫秒」。装载失败/被页面导航清掉时返回 -1，
# 由调用方当作「无从判断静止」处理。
_JS_READ_DOM_QUIET = r"""(() => {
  const st = window.__dxmCarouselWatch;
  return JSON.stringify({quietMs: st ? Math.round(performance.now() - st.last) : -1,
                         muts: st ? st.count : -1});
})()"""

# DOM 静止多久算「一拍渲染结束」。
#
# 【这个数不足以单独定论，页面会有假静止】2026-09-20 实测 184807703152448759：
#   0.00s  muts=0    carousel=True  cells=22
#   0.22~0.33s       quiet 涨到 275ms、变动停在 11 次   <= 假静止，此时轮播项还在
#   0.44s  muts=13   变动重启
#   0.70s  muts=237  carousel=False cells=1             <= 整项移除完成
# 275ms 的假静止与这里的 300ms 只差一点，管线里就真的误收过（读到 21 格判 True，
# 阶段展开候选后重读已消失，落回「没有可选用的图片」那句误报）。
# 把阈值往大调只是把赌注换个数字，故改为「静止 + 读数连续两轮一致」两条并用：
# 假静止期间读数恒 True、移除后恒 False，两者不会同时满足（见 _read_until_settled）。
_DOM_QUIET_MS = 300


async def _read_until_settled(session: BrowserSession, js: str,
                              timeout: float = 20.0) -> dict:
    """读轮播图区快照，等页面按类目重建完产品信息栏（DOM 不再变动）后再交出结果。

    【为什么不能收敛在某个标志出现上】页面挂载分两拍，而两拍都自称「渲染好了」：
    2026-09-20 实测服装类编辑页（184807703152448761），按 0.12s 一次连读——
      0.00~0.40s  hasCarouselLabel=True、hasMaterialLabel=True、18 格、已选 5 张
      0.59s 起    hasCarouselLabel=False、no-carousel-list，此后 20s 恒定不变
    即 Vue 先按通用模板把「产品轮播图」整项连图格一起渲染出来，再按类目把整项移除
    （触发源是 popTemuCategory/attributeList.json 返回，CDP 时间轴对齐所得：0.35s
    响应、0.56s 移除）。第一拍里【任何单点标志都已经是 True】：素材图标签在、轮播
    标签也在、图格也有，故先后试过的三种收敛条件（图格出现 / 素材图标签出现 / 轮播
    标签出现）全都在首次探测就通过，拿到的都是这个瞬态。据它跑下去的代价是实打实的：
    15:21 那次照着抢到的 18 格跑完 74s 备料上传，回头复核时该区已消失，卡在
    「选图前读不到轮播图区」。

    【为什么判据是「DOM 静止」而不是任何时长】这里先后试过两版都不成立：
      ① 「读数连续 0.6s 不变」——0.6s 是照本机翻转时刻取的余量，网慢时第二拍来得更晚，
         0.6s 会在翻转前先收敛，退化成抢瞬态；
      ② 「等 attributeList.json 响应 + 再稳 0.45s」——接口锚点本身可靠，但那 0.45s 仍是
         估值（实测响应到重建约 0.2s），极端卡顿下不够，且页面已加载过时压根等不到响应。
    改成等 MutationObserver 静止后，不再有任何预设时长跟网速挂钩：网慢只是让变动来得晚，
    而每次变动都把 quiet 计时归零，判定自动跟着推后。_DOM_QUIET_MS 衡量的是页面内两次
    渲染之间的间隔，与网速无关（取证见该常量上方）。

    【静止一条还不够，要叠加「读数连续两轮一致」】页面存在假静止：184807703152448759
    在 0.22~0.33s 有一段 275ms 的变动空档（轮播项还在），与 _DOM_QUIET_MS 只差一点，
    管线里就真误收过。而假静止期间读数恒 True、移除完成后恒 False，故要求「本轮静止
    且本轮读数与上一轮相同」——两条同时满足才是真稳态，不必再去猜一个更大的阈值。

    【读数与静止在同一轮里取】两者分开读会重新引入竞态：静止确认与快照之间若又发生一次
    重建，交出的就是重建前的读数。故每轮先读 quiet、再读快照，只在这一轮确认静止时返回
    当轮快照。

    【等不到静止不抛】按 best-effort 交出最后一次读数，由调用方按 supported 三态判断
    （辅助路径坏了不中断主流程，同本项目其它 best-effort 的取向）。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    try:
        await session.eval_json(_JS_INSTALL_DOM_WATCH)
    except Exception as e:
        # 装不上就退回单次读取：此时无从判断静止，硬等只是白耗 20s。
        logger.warning(f"DOM 变动监听装载失败，按单次读取判定轮播图区：{e}")
        return await session.eval_json(js)
    st = await session.eval_json(js)
    while loop.time() < deadline:
        quiet = await session.eval_json(_JS_READ_DOM_QUIET)
        prev, st = st, await session.eval_json(js)
        ms = quiet.get("quietMs")
        if (isinstance(ms, (int, float)) and ms >= _DOM_QUIET_MS
                and bool(st.get("hasCarouselLabel")) == bool(prev.get("hasCarouselLabel"))
                and (st.get("total") or 0) == (prev.get("total") or 0)):
            return st
        if ms == -1:
            # 监听被页面导航清掉了（本函数没导航，但上层可能刚跳过来）：重装一次再等。
            logger.info("DOM 变动监听已失效，重新装载")
            await session.eval_json(_JS_INSTALL_DOM_WATCH)
        await asyncio.sleep(0.12)
    logger.warning(f"DOM {timeout:.0f}s 内未静止，按最后一次读数判定轮播图区")
    return st


async def carousel_state(session: BrowserSession) -> dict:
    """读产品轮播图区现状。返回 {"items": [...], "picked": N, "supported": bool|None}。

    supported=False：产品信息栏已渲染完（「产品素材图」标签已出现）却没有轮播图区，
    即本类目没有这一区（服装类如此），由阶段判 skipped。
    supported=None：连产品信息栏都没渲染出来，证据不足，别当「不支持」，让真失败暴露
    （同 sku_preview_state 的取向）。

    【必须先等图格渲染，不能 open_edit 一回来就读】2026-09-13 实测：open_edit 的
    加载判据是「skuDataInfo 区块出现」，那是页面靠后的变种信息区；而轮播图在靠前的
    产品信息区，图格由 Vue 另行渲染，此刻往往一个都还没挂上。表现为阶段⑤c 在 0.3s
    内返回 no-carousel-list，而人在页面上看得清清楚楚（同一个 carousel_state 手动
    sleep 3 秒后读就完全正常）。

    【但「等某个标志出现」这类收敛条件一律不成立，会抢到瞬态】页面挂载分两拍，第一拍
    里每个标志都已经是 True。取证与做法见 _read_until_settled 的 docstring，判据换成
    「DOM 不再变动」。

    【上限 20s：2026-09-12~13 夜间批实测抬上来的】那批 3 个不同商品
    （rowid-184807703147300533、1078663432052、908737332112）都在原先的 8s 内没等到
    图格，如实报了「读不到产品轮播图区」——8s 原是照着 inspect 那两处 2.0s 放宽来的
    估值，不是量出来的。轮播图这一格与那些表单项不同：十几张缩略图要走 CDN，页面又是
    open_edit 刚回来最忙的时刻，慢起来远超 8s。
    抬上限只推迟【失败】的判定时刻，不推迟成功：DOM 一静止就返回，页面正常时这里是
    零点几秒，20s 只有真读不到时才付满——而那种情形下本阶段的结论是「发布必被拒尺寸、
    要人工换图」，多等十几秒远比误报划算。
    """
    # 【「有没有这一区」与「区里几格」必须在同一次 eval 里读】分两次读就有竞态：轮播项
    # 被移除的瞬间夹在两次 eval 之间，结论就自相矛盾。15:25 那次收敛读到 cells 0、详读
    # 却拿到 18 格（照着瞬态又跑了一轮 74s 备料）；15:28 那次收敛时轮播标签还在、详读时
    # 已消失，于是报 no-carousel-list（该判「本类目没有这一区」）。两次都是同一个竞态的
    # 两个方向。故把标签判定塞进 _JS_CAROUSEL_STATE 一起返回，一次快照定全部结论。
    js = (_JS_CAROUSEL_STATE.replace("__MIN__", J(CAROUSEL_MIN_SIDE))
                            .replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
    st = await _read_until_settled(session, js)
    # 【服装类压根没有产品轮播图区，要判「不支持」而不是「读不到」】2026-09-20 批
    # （682542618799 男童牛仔夹克、1011826279690 男童牛仔衬衫）取证：按 0.15s 一次连读
    # 20s，0.0~0.2s 读到 [{label:产品轮播图, cells:18, boxes:18}, {label:产品素材图,
    # cells:1}]，0.4s 起【只剩产品素材图那一项】——轮播图连 .ant-form-item-label 都不在了
    # （URL 未变、skuDataInfo 仍在、body 反而变长，故不是重载），此后 20s 无恢复。
    # 玩具类（184807703147300533）那种 18 格稳定在的商品与此并存，故不是版本改版，
    # 而是两种类目形态：服装类的图位只有素材图 + SKC 颜色图 + SKU 预览图。
    #
    # 判据取「产品轮播图标签在不在」——比数图格可靠：该标签在稳态下要么在、要么整项没有，
    # 不像图格那样会被填充后清空（那几毫秒的残留列表正是上面那个竞态的来源）。
    if not st.get("hasCarouselLabel"):
        logger.info(f"本类目没有产品轮播图区（只有产品素材图），"
                    f"图格 {st.get('total') or 0} 个")
        return {"supported": False, **st, "items": [], "picked": 0, "total": 0,
                "err": "no-carousel-section"}
    # 标签在、却读不到图格：那是真的证据不足（渲染未完/页面改版），如实报失败交人工。
    # 与上面「没有这一区」分开，取向同 sku_preview_state 的 no-preview-column
    # 与 skc_image_support。
    if st.get("err") or not (st.get("items") or []):
        return {"supported": None, **st}
    logger.info(f"轮播图区现状：候选 {st.get('total')} 格、已选用 {st.get('picked')} 张"
                f"（其中尺寸不合规 {st.get('badPicked')} 张）")
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
  // 【等弹窗要轮询，不能点完 sleep 一次就判死】2026-09-12~13 夜间批报过一条
  // 「打开选图弹窗失败[open]：」——err 为空、stage 已经是 open，说明菜单确实展开了、
  // 「空间图片」也点中了，纯粹是原先那句 sleep(1500) 到点时弹窗还没渲染完，被一次性
  // 判成 opened=false。判据（弹窗标题含「从图片空间选择」）一个字都没放宽，只把
  // 「等法」从固定 sleep 改成条件等待，原来的 1.5s 变成下限、上限抬到 6s。
  // 上限与 media/preview.py 里 ⑦b 点同一个「空间图片」项的两处取同一个值：那是同一个
  // 弹窗组件实例，没有理由在这里另定一套。
  let modal = null;
  for (let k = 0; k < 60; k++) {
    await sleep(100);
    modal = Array.from(document.querySelectorAll('.ant-modal'))
      .find(m => m.offsetHeight > 0 &&
        ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
    if (modal) break;
  }
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

    【no-dropdown 要重瞄一次，不能一次点不开就判死】坐标是「JS 读一次 → Python 另发
    一次 CDP 点击」得来的，两次往返之间只要 Vue 重渲染或页面滚一下，坐标就打在别的
    元素上——菜单实例压根不会创建，回来就是 no-dropdown（同 _JS_PICK_WAREHOUSE 上方
    记的那个「先读坐标再点」的坑）。2026-09-18 商品 1005064778878 实测栽在这里：⑤c
    刚把 9 张产物直传完、页面正好重渲染过，第一次点必落空。
    重瞄比放宽判据对：判据（菜单里同时有「空间图片」与「引用采集图片」）一个字都没动，
    只是把「读坐标→点」这对动作整体再做一遍。
    """
    last: dict = {}
    for attempt in range(3):
        pos = await session.eval_json(
            _JS_CAROUSEL_BTN_POS.replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
        if pos.get("err"):
            last = {"stage": "locate", **pos}
            await asyncio.sleep(0.8)
            continue

        # CDP 真实鼠标事件：先移过去再按下抬起。三个事件缺一不可——只发 pressed/released
        # 而不先 mouseMoved 时，实测有概率不展开（hover 态没建立）。
        failed = None
        for params in ({"type": "mouseMoved", "x": pos["x"], "y": pos["y"]},
                       {"type": "mousePressed", "x": pos["x"], "y": pos["y"],
                        "button": "left", "clickCount": 1},
                       {"type": "mouseReleased", "x": pos["x"], "y": pos["y"],
                        "button": "left", "clickCount": 1}):
            r = await session.cdp("Input.dispatchMouseEvent", params)
            if not r.get("ok"):
                failed = {"stage": "click", "err": f"CDP 点击失败: {r.get('err')}"}
                break
            await asyncio.sleep(0.3)
        if failed:
            return failed
        await asyncio.sleep(0.9)

        r = await session.eval_json(
            _JS_PICK_CAROUSEL_MENU.replace("__WANT__", J(CAROUSEL_MENU_WANT))
                                  .replace("__FEAT__", J(CAROUSEL_MENU_FEATURE))
                                  .replace("__TITLE__", J(media_space.SPACE_MODAL_TITLE)))
        if r.get("opened"):
            if attempt:
                logger.info(f"轮播图选图弹窗第 {attempt + 1} 次重瞄后打开")
            return r
        if r.get("stage") == "open" and not r.get("err"):
            r = {**r, "err": "图片空间弹窗在 6 秒内未出现"}
        last = r
        if r.get("err") != "no-dropdown" and r.get("stage") != "open":
            return r
        await asyncio.sleep(1.0)
    return last


async def toggle_carousel(session: BrowserSession, idx: int, want: bool) -> dict:
    """把第 idx 格的勾选态切成 want（已是该状态则不动，changed=False）。"""
    return await session.eval_json(
        _JS_TOGGLE_CAROUSEL.replace("__IDX__", J(idx)).replace("__WANT__", J(want))
                           .replace("__PICK__", _JS_PICK_CAROUSEL_LIST))
