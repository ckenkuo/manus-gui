"""发布管线阶段②：数据采集（链接采集）+ 认领到店铺站点。

搬运来源（两处合一）：
    skill 的 scripts/dianxiaomi_claim.py（564 行，认领全流程）
    skill 的 scripts/publish_pipeline.py::stage2_claim 的 2a 段（链接采集）
原来采集在 pipeline 里内联、认领是独立脚本靠 subprocess 调起并 grep stdout 判成败
（`if "成功" not in r.stdout`）。这里合成一个模块、返回结构化 dict，判成败不再靠捞日志。

为什么单独成文件、不塞进 pipeline.py：
  1. pipeline.py 已 2157 行，且它整体是【编辑页表单】的操作集——所有函数的前提是
     open_edit 之后停在 /web/popTemu/edit。而阶段② 跑在采集页与草稿列表页上，
     连 rowid 都还不存在（rowid 是认领的产物），共享不了 pipeline 的任何一段。
  2. DOM 体系完全不同：编辑页是长表单 + 锚点区块，这里是列表行 + ant-modal 弹窗。
  3. 点击方式的结论在这里是【相反】的（见下），放在同一文件里极易被后来者「统一风格」
     顺手改坏。物理隔开一个文件，是给那条反直觉结论留个显眼的边界。

【点击方式在本模块内部就分两种，不要统一】
  - 认领弹窗的店铺/站点复选框：**必须真实鼠标点击**。JS `label.click()` 会让
    ant-checkbox 的 class 变成 checked，但 Vue 的 v-model 不更新，点「确定」时提交的
    仍是空选择（表现为「认领成功 0」）。故走「打临时标记 → mouse_click → 回读 class
    校验 → 不生效重试」。
  - 同一弹窗的「确定」「关闭」按钮、列表行里的「认领」链接：JS `.click()` 有效
    （与编辑页按钮同）。这两类混在一个弹窗里，改前务必看清改的是哪类。

与原实现的差异（都是有理由的，别改回去）：
  1. **打标前先清掉旧标记**。原实现每次 tag 只 setAttribute，不清历史标记；同一会话里
     换店铺/换站点重跑时，DOM 上会同时存在两个 `data-wb-target="wb-store"`，
     选择器命中多个、`.first` 按 DOM 顺序取到的是【上一次】那个，于是「勾选未生效」
     且报错完全指不到根因。这里每段 tag JS 开头统一 `removeAttribute`。
  2. **弹窗内查找全部限定在目标可见弹窗内**。原实现检测用 `modal.querySelectorAll`
     但打标改用了全局 `document.querySelectorAll`（应属笔误）。全局查找会撞上
     todo 第四节第 6 条的幽灵浮层——已关闭但未销毁的旧弹窗里也有 .shop-label-box，
     点在隐藏元素上事件照样生效，就勾到了别的店铺。
  3. **去掉原 step7「进草稿列表点编辑」**。那个「编辑」是无 href 的 button，点击后在
     新标签页打开、跑出会话拿不到（原脚本自己在注释里记了这点，所以只敢点一次）。
     本项目改用 pipeline.open_edit 拼 URL 进编辑页，所以阶段② 只需交出 rowid，
     由 pick_rowid 提供。
  4. **去掉搜索框填值的 WebBridge fill 兜底分支**。原生 setter + input 事件是店小秘
     Vue 表单唯一可靠的填值方式（本项目 pipeline 里 113 处 JS 同一写法），
     fill 兜底在原脚本里从未命中过；按项目约定不写 fallback。
  5. **认领结果解析成数字**。原实现只把结果弹窗文本打印出来给人看，pipeline 靠
     `"成功" in stdout` 判断——「认领成功 0，失败 1」也含「成功」，会误判为通过。
     这里正则抽出成功/失败/跳过的计数，成功数为 0 直接报错。

【店铺列表接口慢 → 要能续跑】店小秘的店铺列表接口偶发要 2~3 分钟，超时后弹窗通常
还开着、列表随后自己加载完。故 claim_to_store 入口先探测「选择店铺」弹窗是否已打开：
开着就跳过导航/搜索/点「认领」，直接从等店铺列表接着做。重跑一次命令即可续，
不需要额外的 --resume 参数（少一个用错的机会）。

【副作用，调用前想清楚】采集会在店小秘账号下真实创建采集记录，认领会真实创建
草稿商品。两者都不可逆（草稿只能到列表里手动删）。因此本模块不做「验证性重跑」，
真站验证要么复用已有草稿（跳过采集直接 pick_rowid），要么接受多出一条草稿。
"""
import asyncio
import re
from typing import Optional

from app.logger import logger
from app.publish.browser import CRAWL_URL, BrowserSession, fill_js
from app.publish.navigation import find_rowid

# 认领弹窗与结果弹窗的识别文本：店小秘不给弹窗加稳定 class/id，只能靠标题文本认。
# 两个弹窗会先后出现在同一个 .ant-modal-root 下，认错了就会在结果弹窗里找店铺列表。
MODAL_STORE = "选择店铺"
MODAL_RESULT = "认领到采集箱状态"

# 站点名归一：页面上「美国」与「美国站」两种写法都出现过（列表列头带「站」、
# 弹窗 checkbox 不带），比对前统一去掉尾部的「站」。
def _norm_site(site: str) -> str:
    return (site or "").strip().rstrip("站")


# 各段弹窗 JS 的公共开头：开 IIFE + 声明「取目标可见弹窗」的 _pick。
# 弹窗查找抽成公共片段而不是每段重抄，是因为 offsetParent !== null 这个条件漏一处就会
# 命中已关闭未销毁的幽灵弹窗（todo 第四节第 6 条）——点在隐藏元素上事件照样生效，
# 于是勾到别的店铺。
# 【_pick 必须声明在 IIFE 内部】放在 IIFE 外面变成两条顶层语句，取值就得靠
# eval 的 completion value 语义（Playwright 内部走 globalThis.eval），
# 能跑但依赖实现细节；声明在函数体内是普通闭包，与调用方式无关。
_JS_HEAD = r"""(() => {
  const _pick = (kw) => Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => m.offsetParent !== null && (m.textContent||'').includes(kw))[0];
"""


# ---- 2a 链接采集 -------------------------------------------------------------
# 采集页只有一个 textarea（「链接采集」的输入框），故直接取第一个即可；给它加更精确的
# 选择器反而脆——店小秘换过一次外层容器 class。
# 【「开始采集」用合成 click 有效】这是原 pipeline 实测结论，不要改成 mouse_click：
# 该按钮点击后页面会插入 loading 遮罩，真实点击的 Playwright 可见性检查会与遮罩抢时序。
# offsetHeight > 0 是为了排掉折叠面板里同名的隐藏按钮（页面有「链接采集」「店铺采集」
# 两个 tab，非当前 tab 的按钮 DOM 仍在）。
_JS_START_CRAWL = r"""(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return JSON.stringify({filled: false, reason: 'no-textarea'});
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, __URL__);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  const b = Array.from(document.querySelectorAll('button'))
    .find(b => b.offsetHeight > 0 && (b.textContent||'').trim() === '开始采集');
  if (!b) return JSON.stringify({filled: true, clicked: false, reason: 'no-button'});
  b.click();
  return JSON.stringify({filled: true, clicked: true});
})()"""

# 采集结果只以 toast（.ant-message）或结果弹窗正文的形式出现，没有可回读的状态字段。
# 【必须用 r"""】\s 走普通字符串会被格式化工具改成字面 s（todo 第四节第 4 条）。
_JS_CRAWL_RESULT = r"""(() => {
  const txt = Array.from(document.querySelectorAll('.ant-message, .ant-modal-body, .ant-notification'))
    .map(e => e.textContent || '').join(' ').replace(/\s+/g, ' ').slice(0, 300);
  const ok = /采集成功|采集完成/.test(txt);
  const fail = /采集失败/.test(txt);
  return JSON.stringify({done: ok || fail, ok: ok, fail: fail, text: txt});
})()"""


async def crawl_link(session: BrowserSession, url: str, timeout: float = 60) -> dict:
    """在数据采集页用「链接采集」采集一个 1688 链接。

    返回 {"status": "ok"/"exists"/"timeout", "text": <页面提示原文>}。
    **「采集失败」不当错误**：最常见的失败原因是该链接此前已采集过（店小秘不给区分
    的错误码，提示文案一律是「采集失败」），此时采集记录本来就在，认领照样能做。
    故这里返回 "exists" 让调用方继续走认领，而不是抛异常把整条管线卡死——原 pipeline
    也是这个取向（`log("采集接口报失败（商品可能已采集过），继续认领")`）。
    """
    r = await session.navigate(CRAWL_URL)
    if not r.get("ok"):
        raise RuntimeError(f"导航到数据采集页失败: {r}")
    # 采集页的 textarea 由 Vue 异步渲染，导航完成（domcontentloaded）时常还没挂上；
    # 原实现是死等 6 秒，这里改成轮询到出现为止，快的时候省 5 秒、慢的时候不会漏。
    ready = await session.wait_for(
        "JSON.stringify({ready: !!document.querySelector('textarea')})",
        lambda d: d.get("ready"), timeout=30)
    if not ready.get("ready"):
        raise RuntimeError("数据采集页未渲染出链接输入框（未登录或页面改版）")

    data = await session.eval_json(fill_js(_JS_START_CRAWL, URL=url))
    if not data.get("clicked"):
        raise RuntimeError(f"触发「开始采集」失败: {data}")

    res = await session.wait_for(_JS_CRAWL_RESULT, lambda d: d.get("done"),
                                 timeout=timeout, interval=3)
    text = res.get("text") or ""
    if res.get("ok"):
        logger.info(f"采集成功：{text[:80]}")
        return {"status": "ok", "text": text}
    if res.get("fail"):
        logger.warning(f"采集接口报失败（商品可能已采集过），继续认领：{text[:120]}")
        return {"status": "exists", "text": text}
    # 超时也继续：采集是异步任务，提示可能已经自己消失了（toast 3 秒后自动销毁），
    # 后续搜索认领时若真的没采到，会在「未找到商品」那步报错，信息更准。
    logger.warning(f"未在 {timeout}s 内读到采集结果提示，继续认领：{text[:120]}")
    return {"status": "timeout", "text": text}


# ---- 2a+ 按来源链接查采集记录（取记录自己的标题）-------------------------------
# 【为什么要有这一步】阶段② 原来拿【源商品标题】填搜索框、并按它匹配行，而源标题与
# 采集箱里记录的标题可能不是一回事：2026-09-12 实测 Temu 墙纸那条，① 复用的是用户
# 开着的中文站页签（goodsName 是中文「17.7 英寸 x 118 英寸…」），店小秘插件却是在
# 英文站采的（记录标题「17.7 Inches by 118 Inches…」）——中文搜不到英文记录，报
# 「采集列表未找到商品」，而人在页面上一搜就有，白等一轮还指不到根因。
# 记录自己的标题（list.json 的 name）就是列表里显示的那一个，拿它去搜必然命中；
# 按来源链接查（productSearchType=url）更是完全绕开标题——实测记录里的 sourceUrl
# 与任务传进来的 URL 逐字符相等，故再按 sourceUrl 精确比对一次认领那条。
_JS_CRAWL_RECORD = r"""async (q) => {
  // 【先自证在店小秘域】接口走的是同源相对路径，页签停在别的域（如 www.temu.com）
  // 时这一 fetch 打的是那个域的 /api/crawl/list.json：返回 404/HTML，解析后 rows 为空，
  // 表现与「采集箱里真没这条记录」一模一样。故跑错域要显式报出来，不要静默当没找到。
  if (!location.hostname.endsWith('dianxiaomi.com')) {
    return JSON.stringify({found: false, wrongHost: location.hostname});
  }
  const p = new URLSearchParams({
    pageNo: '1', pageSize: '50', state: q.state, collectStatus: 'CLAIM_TAB',
    site: 'all', searchValue: q.url, productSearchType: 'url',
    sortTime: '', accountName: '', commentType: '', commentValue: '',
    sourcePriceUsdMin: '', sourcePriceUsdMax: '', orderName: '', orderValue: ''
  });
  const r = await fetch('/api/crawl/list.json?' + p.toString(), {credentials: 'include'});
  const j = await r.json();
  const rows = (((j.data || {}).page || {}).list) || [];
  const hit = rows.find(x => (x.sourceUrl || '') === q.url);
  return JSON.stringify(hit ? {found: true, id: hit.id, name: hit.name,
                               productId: hit.productId}
                            : {found: false, total: rows.length});
}"""


async def _ensure_dianxiaomi(session: BrowserSession) -> None:
    """把会话页签带回店小秘域（已经在店小秘域上则什么都不做）。

    采集箱/草稿列表的取数一律走【同源相对路径 fetch】，页签停在别的域时请求会打到
    那个域上、静默返回空——见 find_crawl_record 里记的 2026-09-12 那次事故。
    故凡是「先读列表再操作」的入口都要先过这一关。

    导航目标取数据采集页而不是首页：后面 claim_to_store 找不到已开弹窗时也要导航到
    这一页，提前到这儿导航等于把那一次省掉，不是多跑一趟。
    """
    here = await session.eval_json(
        "JSON.stringify({host: location.hostname})")
    if str(here.get("host") or "").endswith("dianxiaomi.com"):
        return
    logger.info(f"当前页签在 {here.get('host') or '未知域'}，先导航回店小秘采集页再查采集记录")
    r = await session.navigate(CRAWL_URL)
    if not r.get("ok"):
        raise RuntimeError(f"导航到数据采集页失败（查采集记录需停在店小秘域）: {r}")
    await asyncio.sleep(3)


async def find_crawl_record(session: BrowserSession, url: str,
                            attempts: int = 3, interval: float = 2.0) -> dict:
    """按来源链接在采集箱里找这条记录，返回 {"id", "name", "productId"}；没有返回 {}。

    【两个列表都要查】state=no 是「未认领」、state=claimed 是「已认领」（2026-09-12
    实测确认：stat 字段里的 all/no/claimed 三个计数就是这三个列表的规模）。同一商品
    重新认领到另一个站点时记录已落在「已认领」列表里——这与 _open_claim_modal 找不到
    就切「已认领」标签重试是同样两个真实入口，不是兜底。

    采集是异步任务，记录落库有秒级延迟，故带重试；重试耗尽返回空，由调用方报错。

    【必须先把页签带回店小秘域】2026-09-12 实测：Temu 源跑到这里时会话页签停在
    www.temu.com 的买家商品页——① 抽取靠 adopt_open_page 复用用户开着的那个页签取数
    （Temu 导航不可复现，见 browser.adopt_open_page），而 Temu 分支 skip_crawl=True
    跳过了 crawl_link，恰好 crawl_link 是这一段里唯一会导航回店小秘的一步。于是本函数
    的同源 fetch 打在了 temu.com 上，查不到记录，报「采集箱里没有这条 Temu 商品」，
    而人在店小秘页面上一搜就有。1688 源不跳 crawl_link，所以从没暴露。
    """
    await _ensure_dianxiaomi(session)
    for i in range(1, attempts + 1):
        for state in ("no", "claimed"):
            r = await session.eval_json(_JS_CRAWL_RECORD, arg={"url": url, "state": state})
            if r.get("wrongHost"):
                raise RuntimeError(
                    f"查采集记录时页签跑到了 {r['wrongHost']}——采集箱接口是同源相对"
                    f"路径，必须停在店小秘域（导航回店小秘这一步没生效）")
            if r.get("found"):
                logger.info(f"采集记录已找到（{'已认领' if state == 'claimed' else '未认领'}）："
                            f"id={r.get('id')}，记录标题「{str(r.get('name'))[:40]}」")
                # 带上「在哪个列表里找到的」：调用方据此判断是不是已经认领过一次
                # （见 collect_and_claim 的防重）。
                return {**r, "claimed": state == "claimed"}
            if r.get("total"):
                # 按链接搜到了行、却没一条 sourceUrl 对得上：链接被改写过或命中的是
                # 别的商品。留一条日志，免得排查时只有一句「查不到记录」。
                logger.warning(f"按链接搜到 {r.get('total')} 行但 sourceUrl 均不匹配：{url}")
        if i < attempts:
            logger.info(f"采集箱里暂未出现该链接的记录，{interval}s 后重试（{i}/{attempts}）")
            await asyncio.sleep(interval)
    return {}


# ---- 2b 搜索 + 点「认领」-----------------------------------------------------
# 搜索框 id 是实测得来的 #c-searchValue（采集列表页专用）。原生 setter + input 事件
# 是店小秘 Vue 表单唯一可靠的填值方式：直接赋 .value 不触发 v-model，点搜索时查的是空值。
_JS_SEARCH = r"""(() => {
  const input = document.querySelector('#c-searchValue');
  if (!input) return JSON.stringify({filled: false, reason: 'no-input'});
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(input, __KW__);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  const btn = Array.from(document.querySelectorAll('button, a, [class*="btn"]'))
    .find(b => (b.textContent||'').trim() === '搜索' && b.offsetParent !== null);
  if (btn) { btn.click(); return JSON.stringify({filled: true, triggered: true, via: 'button'}); }
  input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
  input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', keyCode: 13, bubbles: true}));
  return JSON.stringify({filled: true, triggered: true, via: 'enter'});
})()"""

# 找到含标题的行、点行内的「认领」。优先点按钮而非回车触发搜索的理由同上：
# 店小秘的 Enter 事件在部分版本上不绑查询。
# 这里的 `.click()` 是合成点击且**有效**——「认领」是普通 <a>，只负责开弹窗，
# 不像弹窗内的复选框那样依赖可信事件更新 v-model。
_JS_CLICK_CLAIM = r"""(() => {
  const title = __TITLE__;
  const cells = document.querySelectorAll('td, [class*="cell"]');
  let row = null;
  for (const c of cells) {
    if ((c.textContent||'').includes(title)) { row = c.closest('tr') || c.parentElement; break; }
  }
  if (!row) return JSON.stringify({found: false});
  const links = Array.from(row.querySelectorAll('a, button'));
  const btn = links.find(a => (a.textContent||'').trim() === '认领');
  if (!btn) return JSON.stringify({found: true, clicked: false,
                                   links: links.map(a => (a.textContent||'').trim())});
  btn.click();
  return JSON.stringify({found: true, clicked: true});
})()"""

# 商品已被认领过时会从「未认领」列表消失，切到「已认领」标签仍能再认领到别的店铺/站点。
# 长度 < 15 的过滤是为了排掉「已认领的商品不会再次采集」这类说明文字（原实现结论）。
_JS_TAB_CLAIMED = r"""(() => {
  const tabs = Array.from(document.querySelectorAll('a, span, div, li'))
    .filter(el => el.offsetParent !== null
                  && /^已认领/.test((el.textContent||'').trim())
                  && (el.textContent||'').trim().length < 15);
  if (!tabs.length) return JSON.stringify({clicked: false});
  tabs[0].click();
  return JSON.stringify({clicked: true});
})()"""


async def _search_and_click_claim(session: BrowserSession, title: str,
                                  attempts: int = 3, interval: float = 2.0) -> dict:
    """在当前标签页下「重新搜索 + 找行点认领」，最多 attempts 轮。

    采集成功的 toast 只表示采集接口返回了，商品落到「未认领」列表还有秒级延迟；
    原实现搜完只等 3 秒查一次，赶上慢的时候就误判成「列表未找到」，进而白跑一次
    切「已认领」标签、最后整条管线报错要人重跑。故每轮都重新触发一次搜索
    （等价于刷新列表，比原地重读 DOM 靠谱），间隔 interval 秒。
    """
    data: dict = {}
    for i in range(1, attempts + 1):
        res = await session.eval_json(fill_js(_JS_SEARCH, KW=title))
        if not res.get("triggered"):
            raise RuntimeError(f"触发搜索失败: {res}")
        await asyncio.sleep(interval)
        data = await session.eval_json(fill_js(_JS_CLICK_CLAIM, TITLE=title))
        if data.get("found"):
            return data
        if i < attempts:
            logger.info(f"采集列表暂未出现该商品，{interval}s 后刷新重试（{i}/{attempts}）")
    return data


async def _open_claim_modal(session: BrowserSession, title: str) -> dict:
    """搜索商品并点开认领弹窗。返回 {"tab": "未认领"/"已认领"}。"""
    data = await _search_and_click_claim(session, title)
    tab = "未认领"
    if not data.get("found"):
        # 已认领过的商品不在默认列表里，切标签重试（不是 fallback，是另一个真实入口）
        logger.info("「未认领」列表未找到，切到「已认领」标签重试")
        await session.eval_json(_JS_TAB_CLAIMED)
        await asyncio.sleep(2)
        data = await _search_and_click_claim(session, title)
        tab = "已认领"
    if not data.get("found"):
        raise RuntimeError(f"采集列表未找到商品「{title}」（采集可能还没完成，稍后重跑）")
    if not data.get("clicked"):
        raise RuntimeError(f"找到商品行但行内没有「认领」入口: {data}")
    await asyncio.sleep(2)
    return {"tab": tab}


# ---- 2c 勾店铺 + 勾站点 -------------------------------------------------------
# 【打标前先清旧标记】原实现每次只 setAttribute、不清历史；同一会话换店铺重跑时
# DOM 上会同时存在两个 `data-wb-target="wb-store"`，选择器 `.first` 按 DOM 顺序取到的
# 是【上次】那个，于是「勾选未生效」且报错指不到根因。每段 tag 开头统一 removeAttribute。
# 【只在目标弹窗内查找】原实现的 tag JS 用了全局 `document.querySelectorAll`（应属笔误，
# 因检测用的是 `modal.querySelectorAll`），会撞上未销毁的幽灵弹窗（todo 第四节第 6 条）。

_JS_WAIT_STORES = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({ready: false, reason: 'no-modal'});
  const boxes = Array.from(modal.querySelectorAll('.shop-label-box'));
  const names = boxes.map(b => (b.textContent||'').trim()).filter(t => t);
  return JSON.stringify({ready: names.length > 0, stores: names});
})()"""

_JS_TAG_STORE = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-wb-target="wb-store"]').forEach(e => e.removeAttribute('data-wb-target'));
  const boxes = Array.from(modal.querySelectorAll('.shop-label-box'));
  const target = boxes.find(b => (b.textContent||'').trim() === __STORE__);
  if (!target) return JSON.stringify({found: false, available: boxes.map(b => (b.textContent||'').trim())});
  const label = target.querySelector('label');
  if (!label) return JSON.stringify({found: false, reason: 'no-label'});
  label.setAttribute('data-wb-target', 'wb-store');
  label.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true, checked: label.className.includes('ant-checkbox-wrapper-checked')});
})()"""

_JS_STORE_CHECKED = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({checked: false, reason: 'no-modal'});
  const boxes = Array.from(modal.querySelectorAll('.shop-label-box'));
  const target = boxes.find(b => (b.textContent||'').trim() === __STORE__);
  if (!target) return JSON.stringify({checked: false, reason: 'store-not-found'});
  const label = target.querySelector('label');
  return JSON.stringify({checked: !!label && label.className.includes('ant-checkbox-wrapper-checked')});
})()"""

# 站点区的 checkbox 排除两处：.shop-label-box（店铺项）和 .ant-modal-footer（弹窗底部的
# 全选/确定/取消区），剩下的才是站点区（全球/美国/欧区等）。站点名归一去掉尾部「站」。
_JS_WAIT_SITES = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({ready: false, reason: 'no-modal'});
  const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'));
  const sites = labels.map(l => {
    const t = (l.textContent||'').trim();
    return {text: t, checked: l.className.includes('ant-checkbox-wrapper-checked')};
  }).filter(x => x.text);
  return JSON.stringify({ready: sites.length > 0, sites: sites});
})()"""

_JS_TAG_SITE = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-wb-target="wb-site"]').forEach(e => e.removeAttribute('data-wb-target'));
  const want = __SITE__;
  const norm = t => t.replace(/站$/, '');
  const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'));
  const target = labels.find(l => norm((l.textContent||'').trim()) === norm(want));
  if (!target) return JSON.stringify({found: false, reason: 'site-not-found',
                                     available: labels.map(l => (l.textContent||'').trim())});
  target.setAttribute('data-wb-target', 'wb-site');
  target.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true, checked: target.className.includes('ant-checkbox-wrapper-checked')});
})()"""

_JS_SITE_CHECKED = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({checked: false, reason: 'no-modal'});
  const want = __SITE__;
  const norm = t => t.replace(/站$/, '');
  const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'));
  const target = labels.find(l => norm((l.textContent||'').trim()) === norm(want));
  if (!target) return JSON.stringify({checked: false, reason: 'site-not-found'});
  return JSON.stringify({checked: target.className.includes('ant-checkbox-wrapper-checked')});
})()"""

# 找第一个已勾选的【非目标】站点并打标，供真实点击取消。店小秘常把「美国」自动勾上，
# 不取消就会认领到多个站点（可能是预期行为，但 find_rowid 后要按站点筛、多一步麻烦）。
_JS_TAG_OTHER_SITE = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-wb-target="wb-uncheck"]').forEach(e => e.removeAttribute('data-wb-target'));
  const want = __SITE__;
  const norm = t => t.replace(/站$/, '');
  const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'));
  const target = labels.find(l => l.className.includes('ant-checkbox-wrapper-checked')
                                  && norm((l.textContent||'').trim()) !== norm(want));
  if (!target) return JSON.stringify({found: false});
  target.setAttribute('data-wb-target', 'wb-uncheck');
  target.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true, text: (target.textContent||'').trim()});
})()"""


async def _trusted_toggle(
    session: BrowserSession,
    tag_js: str,
    verify_js: str,
    selector: str,
    retries: int = 3
) -> bool:
    """打标 → 真实鼠标点击 → 回读 class 校验，未生效时重试。

    【为什么必须真实点击，不能 JS .click()】店小秘的店铺/站点 checkbox 是 ant-design
    的 Checkbox + Vue v-model。JS `label.click()` 会让 DOM class 切成 checked 状态，
    但 Vue 组件的内部 state 不更新，点「确定」时提交的仍是空选择（表现为「认领成功 0」）。
    真实鼠标事件（Playwright locator.click 走 CDP Input.dispatchMouseEvent）才会触发
    整个 Vue 事件链、更新 v-model。这与编辑页按钮的结论【相反】（那里真实点击被吞、
    必须 JS click），不要统一。
    """
    for attempt in range(1, retries + 1):
        tag = await session.eval_json(tag_js)
        if not tag.get("found"):
            return False
        if tag.get("checked"):  # 已勾选，不用再点
            return True
        r = await session.mouse_click(selector)
        if not r.get("ok"):
            logger.warning(f"真实点击失败（{attempt}/{retries}）: {r.get('err')}")
            await asyncio.sleep(1.5 * attempt)
            continue
        await asyncio.sleep(1)
        chk = await session.eval_json(verify_js)
        if chk.get("checked"):
            return True
        logger.warning(f"勾选未生效，重试 {attempt}/{retries}")
    return False


async def _select_store_and_site(
    session: BrowserSession,
    store: str,
    site: str = "",
    modal_kw: str = MODAL_STORE
) -> dict:
    """在认领弹窗内勾选店铺 + 站点（真实鼠标点击）。

    返回 {"storeOk": bool, "siteOk": bool, "unchecked": [已取消的其它站点]}。
    弹窗必须已打开（由调用方确保），这里不再检测——检测与操作分离，续跑时可跳过前几步。
    """
    # 4.1 等店铺列表异步加载完成（店小秘接口慢时可能要 2~3 分钟）
    logger.info("等待店铺列表加载（店小秘接口慢时可能需 2~3 分钟）")
    data = await session.wait_for(
        fill_js(_JS_WAIT_STORES, MODAL=modal_kw),
        lambda d: d.get("ready"), timeout=180, interval=2)
    if not data.get("ready"):
        raise RuntimeError(
            "店铺列表加载超时：店小秘接口可能异常（店铺区卡在骨架屏），建议稍后重跑"
        )

    # 4.2 勾店铺
    tag = await session.eval_json(fill_js(_JS_TAG_STORE, MODAL=modal_kw, STORE=store))
    if not tag.get("found"):
        avail = tag.get("available") or []
        raise RuntimeError(f"弹窗中未找到店铺「{store}」，可选店铺: {avail}")
    ok = await _trusted_toggle(
        session,
        fill_js(_JS_TAG_STORE, MODAL=modal_kw, STORE=store),
        fill_js(_JS_STORE_CHECKED, MODAL=modal_kw, STORE=store),
        'label[data-wb-target="wb-store"]'
    )
    if not ok:
        raise RuntimeError(f"勾选店铺「{store}」未生效（重试 3 次后仍失败）")
    logger.info(f"已勾选店铺：{store}")
    await asyncio.sleep(2)

    # 4.3 等站点区渲染（勾店铺后站点 checkbox 异步出现；接口异常时会卡骨架屏）
    data = await session.wait_for(
        fill_js(_JS_WAIT_SITES, MODAL=modal_kw),
        lambda d: d.get("ready"), timeout=90, interval=2)
    if not data.get("ready"):
        raise RuntimeError("站点列表加载超时：店小秘接口可能异常，建议稍后重跑")

    # 4.4 取消所有默认勾选的非目标站点（店小秘常自动勾上「美国」）
    unchecked = []
    for _ in range(10):  # 最多取消 10 个，防死循环
        tag = await session.eval_json(fill_js(_JS_TAG_OTHER_SITE, MODAL=modal_kw, SITE=site))
        if not tag.get("found"):
            break
        r = await session.mouse_click('label[data-wb-target="wb-uncheck"]')
        if not r.get("ok"):
            logger.warning(f"取消默认站点失败: {r.get('err')}")
            break
        other = tag.get("text") or "未知站点"
        unchecked.append(other)
        logger.info(f"已取消默认站点：{other}")
        await asyncio.sleep(1)

    # 4.5 勾目标站点
    tag = await session.eval_json(fill_js(_JS_TAG_SITE, MODAL=modal_kw, SITE=site))
    if tag.get("reason") == "site-not-found":
        avail = tag.get("available") or []
        raise RuntimeError(f"弹窗中未找到站点「{site}」，可选站点: {avail}")
    ok = await _trusted_toggle(
        session,
        fill_js(_JS_TAG_SITE, MODAL=modal_kw, SITE=site),
        fill_js(_JS_SITE_CHECKED, MODAL=modal_kw, SITE=site),
        'label[data-wb-target="wb-site"]'
    )
    if not ok:
        raise RuntimeError(f"勾选站点「{site}」未生效（重试 3 次后仍失败）")
    logger.info(f"已勾选站点：{site}")

    return {"storeOk": True, "siteOk": True, "unchecked": unchecked}


# ---- 2d 确定 + 读结果 + 关弹窗 ------------------------------------------------
# 「确定」是普通 <button>，合成 click 有效（与弹窗内复选框相反，见模块 docstring）。
_JS_CONFIRM = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({found: false, reason: 'no-modal'});
  const btn = Array.from(modal.querySelectorAll('button'))
    .find(b => (b.textContent||'').trim() === '确定');
  if (!btn) return JSON.stringify({found: false, reason: 'no-confirm-btn'});
  btn.click();
  return JSON.stringify({found: true});
})()"""

_JS_CLAIM_RESULT = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({finished: false});
  return JSON.stringify({finished: true,
    text: (modal.textContent||'').trim().replace(/\s+/g, ' ').slice(0, 300)});
})()"""

# 关结果弹窗：先点「关闭」按钮，没有就点右上角 X。两者都是合成 click 有效。
# 不清掉弹窗后面的步骤会被遮罩挡住（kill_stuck_modals 治的就是漏清这一步的后果）。
_JS_CLOSE_RESULT = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (modal) {
    const btn = Array.from(modal.querySelectorAll('button, a'))
      .find(b => (b.textContent||'').trim() === '关闭');
    if (btn) { btn.click(); return JSON.stringify({clicked: true, via: 'close-btn'}); }
    const x = modal.querySelector('.ant-modal-close, [aria-label="close"]');
    if (x) { x.click(); return JSON.stringify({clicked: true, via: 'close-x'}); }
  }
  return JSON.stringify({clicked: false});
})()"""


def _parse_claim_counts(text: str) -> dict:
    """从结果弹窗文本抽出「认领成功 N / 失败 N / 跳过 N」的计数。

    【为什么必须解析成数字】原 pipeline 判成败靠 `"成功" in stdout`——而
    「认领成功 0，认领失败 1」也含「成功」，会静默判成通过、后面找不到 rowid 才炸，
    错误信息完全指不到根因。这里抽出数字，成功数为 0 由调用方直接报错。
    弹窗文案变化（成功/失败/跳过的措辞）时抽不到就返回 None，交调用方按「读不到」处理，
    不冒充 0。
    """
    out: dict = {"text": text}
    for key, pat in (("success", r"成功\D{0,4}(\d+)"),
                     ("failed", r"失败\D{0,4}(\d+)"),
                     ("skipped", r"跳过\D{0,4}(\d+)")):
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    return out


async def _confirm_claim(session: BrowserSession) -> dict:
    """点「确定」提交认领，读结果弹窗计数，然后关掉它。"""
    data = await session.eval_json(fill_js(_JS_CONFIRM, MODAL=MODAL_STORE))
    if not data.get("found"):
        raise RuntimeError(f"点击「确定」失败: {data}")
    await asyncio.sleep(3)

    res = await session.wait_for(
        fill_js(_JS_CLAIM_RESULT, MODAL=MODAL_RESULT),
        lambda d: d.get("finished"), timeout=30, interval=2)
    counts = _parse_claim_counts(res.get("text") or "")
    if res.get("finished"):
        logger.info(f"认领结果：{counts.get('text', '')[:120]}")
    else:
        # 读不到结果弹窗不当失败：认领可能已提交、弹窗自己关了。
        # 最终判据是后面 pick_rowid 能否在草稿列表找到目标站点的行。
        logger.warning("未检测到认领结果弹窗，以草稿列表实际结果为准")

    # 关结果弹窗（best-effort）。关不掉就交给 kill_stuck_modals 兜底清遮罩——
    # 遮罩留着会挡住后续所有点击，属于坏了也不该中断主流程的辅助路径。
    try:
        r = await session.eval_json(fill_js(_JS_CLOSE_RESULT, MODAL=MODAL_RESULT))
        if not r.get("clicked"):
            await session.kill_stuck_modals()
        await asyncio.sleep(1)
    except Exception as e:
        logger.warning(f"关闭认领结果弹窗失败（忽略）：{e}")

    if counts.get("success") == 0:
        raise RuntimeError(
            f"认领成功 0 条（失败 {counts.get('failed')} / 跳过 {counts.get('skipped')}）："
            f"{counts.get('text', '')[:200]}"
        )
    return {"finished": bool(res.get("finished")), **counts}


# ---- 公开入口 ----------------------------------------------------------------
# 续跑探测：店铺列表接口慢导致超时后，弹窗通常还开着、列表随后自己加载完。
# 重跑命令时先探测这个状态，开着就跳过导航/搜索/点认领，直接从等店铺列表接着做。
# 刻意不做成 --resume 参数：自动探测少一个用错的机会，且「弹窗开着」这件事本身
# 就是唯一判据，不需要用户告诉我们。
_JS_MODAL_OPEN = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({open: false});
  const boxes = modal.querySelectorAll('.shop-label-box').length;
  return JSON.stringify({open: true, stores: boxes});
})()"""


async def claim_to_store(
    session: BrowserSession,
    title: str,
    store: str,
    site: str = "",
) -> dict:
    """把已采集的商品认领到指定店铺的指定站点（会真实创建草稿商品）。

    支持【续跑】：若「选择店铺」弹窗已经开着（上次因店铺列表接口慢超时中断），
    直接从等店铺列表接着做，不重新导航/搜索——重新导航会把弹窗关掉，
    而店铺列表接口慢的时候关掉再开一次要重新等 2~3 分钟。

    返回 {"status": "ok", "resumed": bool, "tab": ..., "store": ..., "site": ...,
          "unchecked": [被取消的其它站点], "result": {"success": N, "failed": N, ...}}。
    认领成功 0 条时抛异常（结果计数解析见 _parse_claim_counts）。
    """
    opened = await session.eval_json(fill_js(_JS_MODAL_OPEN, MODAL=MODAL_STORE))
    resumed = bool(opened.get("open"))
    tab = None
    if resumed:
        logger.info(f"「选择店铺」弹窗已开着（店铺 {opened.get('stores')} 个），续跑认领")
    else:
        r = await session.navigate(CRAWL_URL)
        if not r.get("ok"):
            raise RuntimeError(f"导航到数据采集页失败: {r}")
        await asyncio.sleep(3)
        tab = (await _open_claim_modal(session, title)).get("tab")

    picked = await _select_store_and_site(session, store, site)
    result = await _confirm_claim(session)
    return {"status": "ok", "resumed": resumed, "tab": tab,
            "store": store, "site": site,
            "unchecked": picked.get("unchecked") or [], "result": result}


def _pick_candidates(rows: list, store: str, site: str):
    """从草稿列表行里两级收窄出目标行，返回 (候选行, 匹配方式)。

    第一级要站点+店铺都出现在行文本里，第二级只要站点——理由见 pick_rowid 的说明。
    店铺名在行里是「店铺名」这种带书名号的写法（2026-09-12 实测草稿列表
    `…RW138.「Pawly」哥伦比亚家居装修 > …`），故用 `in` 匹配即可。
    """
    ns = _norm_site(site)
    strict = [r for r in rows if ns in (r.get("text") or "") and store in (r.get("text") or "")]
    loose = [r for r in rows if ns in (r.get("text") or "")]
    return (strict, "store+site") if strict else (loose, "site")


async def find_draft_rowid(
    session: BrowserSession,
    title: str,
    store: str,
    site: str = "",
    keyword: Optional[str] = None,
) -> dict:
    """只读查草稿列表里这个商品在目标店铺/站点的行；没有返回 {}（不抛异常）。

    与 pick_rowid 的唯一区别是「找不到」不当错误：认领【前】先用它探一次，那时
    「还没有」是正常结果。用途见 collect_and_claim 里的防重说明。
    """
    kw = keyword or title[:12]
    data = await find_rowid(session, kw)
    rows = data.get("matched") or []
    cands, how = _pick_candidates(rows, store, site)
    if not cands:
        return {}
    return {"rowid": cands[0].get("rowid"), "matchedBy": how, "keyword": kw,
            "candidates": cands, "total": data.get("total")}


async def pick_rowid(
    session: BrowserSession,
    title: str,
    store: str,
    site: str = "",
    keyword: Optional[str] = None,
) -> dict:
    """认领后在草稿列表按【站点】筛出目标 rowid。

    【为什么要按站点筛】店小秘的认领规则可能一次认领到多个站点（弹窗里「美国」常被
    默认勾上；即便这里取消了，账号级规则也可能追加），于是草稿列表出现同标题多行、
    每行一个站点。pipeline.find_rowid 刻意返回全部匹配不做筛选（职责单一：它只认
    rowid，不认业务语义），筛选放在这里——阶段② 才是知道「目标店铺+站点」的地方。

    筛选是【两级收窄】而不是一次到底：先按站点+店铺都匹配，没有则只按站点匹配。
    理由是店铺名在列表行文本里不一定完整出现（列宽截断时会带省略号），
    而站点名是短词、稳定出现；只按站点也能定位到唯一行的场合不该因店铺名截断而失败。

    返回 {"rowid": ..., "matchedBy": "store+site"/"site", "candidates": [...]}。
    """
    kw = keyword or title[:12]
    data = await find_rowid(session, kw)
    rows = data.get("matched") or []
    if not rows:
        raise RuntimeError(
            f"草稿列表未匹配到「{kw}」（列表共 {data.get('total')} 行，认领可能还在同步，稍后重试）"
        )
    cands, how = _pick_candidates(rows, store, site)
    if not cands:
        raise RuntimeError(
            f"草稿列表未找到店铺「{store}」站点「{site}」的行，"
            f"匹配到的行：{[r.get('text', '')[:160] for r in rows][:5]}"
        )
    if len(cands) > 1:
        logger.warning(
            f"站点「{site}」匹配到 {len(cands)} 行，取第一行 {cands[0].get('rowid')}；"
            f"若不对请用 publish_inspect.py find 手动确认"
        )
    rowid = cands[0].get("rowid")
    logger.info(f"rowid = {rowid}（按 {how} 筛出，候选 {len(cands)} 行）")
    return {"rowid": rowid, "matchedBy": how, "keyword": kw,
            "candidates": cands, "total": data.get("total")}


async def collect_and_claim(
    session: BrowserSession,
    url: str,
    title: str,
    store: str,
    site: str = "",
    skip_crawl: bool = False,
) -> dict:
    """阶段② 完整流程：链接采集 → 认领到店铺站点 → 按站点筛出 rowid。

    skip_crawl=True 用于「商品已在采集列表里、只想重新认领到另一个站点」的场合，
    也是真站验证时少造一条采集记录的省钱开关。

    返回 {"status": "ok", "rowid": ..., "crawl": {...}, "claim": {...}, "pick": {...}}，
    rowid 可直接喂给 pipeline.open_edit 进入阶段③。

    【title 参数只用于日志】搜索与匹配一律用【采集记录自己的标题】（见
    _JS_CRAWL_RECORD 上方：源标题与记录标题可能不同语言，用源标题搜必然落空）。

    【不可逆副作用】采集在账号下创建采集记录、认领创建草稿商品，都只能到列表里手动删。
    """
    crawl = {"status": "skipped"} if skip_crawl else await crawl_link(session, url)
    # 查不到记录说明商品压根不在采集箱（或链接与记录里的 sourceUrl 不一致），此时
    # 直接报错。错误信息里保留「采集列表未找到商品」这句话——stages/form.py 靠它给
    # Temu 源补一条「先用浏览器插件采集」的可照做提示，别改成别的措辞。
    record = await find_crawl_record(session, url)
    if not record:
        raise RuntimeError(
            f"采集列表未找到商品（按来源链接查不到采集记录，采集可能还没完成，"
            f"稍后重跑）：{url}")
    # 源头是中文、记录是英文这类语言错位在这里被抹平：后续搜索与草稿列表筛选都用
    # 记录自己的标题，它与两个列表里显示的文本必然一致。
    match_title = record["name"]
    logger.info(f"商品「{(title or '')[:30]}」改按采集记录标题匹配「{match_title[:40]}」")

    # 【记录已在「已认领」列表 + 草稿列表里也有该店铺/站点的行】= 上次认领其实成了，
    # 只是后面某一步失败。此时若再走一遍认领，会在账号下多建一条同标题同站点的草稿
    # ——2026-09-12 实测重跑两次就攒出两条，只能到列表里手动删。故直接复用已有行。
    # 只在【记录已认领】时才查这一次：首次跑记录还在「未认领」列表里，不为此多付
    # 一次草稿列表导航。想认领到【另一个站点】时，草稿列表里没有那个站点的行，
    # 这里查不到、照样往下走认领，不会把多站点铺货挡掉。
    if record.get("claimed"):
        existing = await find_draft_rowid(session, match_title, store, site)
        if existing.get("rowid"):
            logger.info(f"草稿列表已有该店铺/站点的行（rowid={existing['rowid']}），"
                        f"跳过认领，不重复建草稿")
            return {"status": "ok", "rowid": existing["rowid"], "crawl": crawl,
                    "claim": {"status": "skipped", "reason": "草稿已存在"},
                    "pick": existing}

    claim = await claim_to_store(session, match_title, store, site)
    # 认领后列表要等后台同步，直接查常查不到；等一下再查比在 find_rowid 里加长
    # 超时更省——find_rowid 是只读通用件，不该为阶段② 的时序特例加等待。
    await asyncio.sleep(5)
    pick = await pick_rowid(session, match_title, store, site)
    return {"status": "ok", "rowid": pick.get("rowid"),
            "crawl": crawl, "claim": claim, "pick": pick}
