"""发布管线的浏览器原语层：用 Playwright over CDP 接管已登录的真实 Chrome。

为什么要这一层（迁移的支点）：
原 skill（dianxiaomi-temu-publish）全部浏览器操作走 Kimi WebBridge daemon
（http://127.0.0.1:10086/command + 浏览器扩展），5266 行脚本里真实用到的原语只有 6 个：
    navigate 4 处 / evaluate·eval_json 113 处 / mouse_click 2 处 / cdp 6 处
也就是说「WebBridge daemon + 浏览器扩展」这个硬前提可以整个去掉——Playwright over CDP
对这 6 个原语全部有等价物，而且本项目 app/collect/service.py 早就在用 connect_over_cdp
接管已登录 Chrome，登录态复用这条路本来就是通的。少一个常驻 daemon、少一个扩展，
换机器只要 Chrome 带 --remote-debugging-port=9222 就能跑。

【签名刻意照抄 skill 原函数】evaluate / eval_json / wait_for / navigate / mouse_click / cdp
的参数名和返回结构与原脚本一致，因此那 113 处 `eval_json(\"\"\"...JS...\"\"\")` 调用点
搬过来时几乎不用改，只是从同步变 async。这是为了让 3423 行的编辑页脚本可以逐个子命令
增量移植、每搬一个就能单独验证，而不是一次性重写。

JS 字符串占位符替换（原脚本的 .replace("__NAME__", json.dumps(x))）也保留支持：
Playwright 的 page.evaluate 支持传参，但原脚本 113 处 JS 全是自包含字符串、且大量用
IIFE 包裹，改成传参要逐处改写 JS 本身，收益不抵风险。故这里维持「JS 自带字面量」的
原样，只在新写代码里推荐用 evaluate_arg 传参。

会话模型：本模块持有一个进程级 _Session（browser + page + cdp_session），因为发布是
【单商品、长流程、12 阶段共用同一个页面】——阶段间靠页面上已选好的类目/属性状态传递，
中途换页面就得从头再来。这与 collect 的「每批 async with 一次」不同，故单独管理。
"""
import asyncio
import json
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from playwright.async_api import Page, async_playwright

from app.config import config
from app.logger import logger

# CDP 地址与 collect 侧同源（config.browser_config.cdp_url，缺省 9222）。
# 【localhost 必须归一成 127.0.0.1】Chrome 的 --remote-debugging-port 只监听 IPv4；
# 而本机 localhost 在 Windows 上优先解析成 IPv6 ::1，于是 connect_over_cdp 直接
# EACCES ::1:9222（2026-08-19 实测）。配置里现成写的就是 http://localhost:9222，
# 这里做规范化而不去改配置：collect 侧靠 browser_use 那条路没踩到，改配置会牵动它。
def _normalize_cdp(url: str) -> str:
    return re.sub(r"//(localhost)(?=[:/]|$)", "//127.0.0.1", url)


CDP_URL = _normalize_cdp(
    getattr(config.browser_config, "cdp_url", None) or "http://127.0.0.1:9222"
)

# 店小秘页面地址：只留【路径】不留完整常量的理由与 Temu 侧不同——店小秘没有多区域域名，
# 但保持同一取向便于日后加测试环境；这里 host 是唯一的，故直接给完整模板。
DIANXIAOMI_HOST = "https://www.dianxiaomi.com"
EDIT_URL = DIANXIAOMI_HOST + "/web/popTemu/edit?id={rowid}"
DRAFT_LIST_URL = DIANXIAOMI_HOST + "/web/popTemu/pageList/draft"
# 阶段⑮ 发布成功的取证列表：发布后该行从草稿箱消失、出现在在线产品
# （2026-08-24 实测，见 pipeline._publish_landed）
ONLINE_LIST_URL = DIANXIAOMI_HOST + "/web/popTemu/pageList/online"
# 发布失败列表：平台的材积重量等校验是【后端异步】做的，点完「立即发布」草稿行会先
# 离开草稿箱，几秒后带着失败原因落到这里。只看在线/草稿两个列表会把这种情况判成成功
# （2026-09-01 实测，见 pipeline._publish_landed）。
PUBLISH_FAIL_LIST_URL = (DIANXIAOMI_HOST
                         + "/web/popTemu/pageList/offline?dxmOfflineState=publishFail")
CRAWL_URL = DIANXIAOMI_HOST + "/web/productCrawl/dataAcquisition"

# WebBridge 时代的瞬时错误特征（"Promise was collected" / -32000），Playwright 下换成
# 执行上下文销毁类错误：页面导航中 evaluate 会撞上 "Execution context was destroyed"，
# 都是等一下重试就好的瞬时态，不该让整阶段失败。
_TRANSIENT_ERRORS = (
    "Execution context was destroyed",
    "Cannot find context",
    "Promise was collected",
    "navigating and changing",
)

# 目标关闭类错误：页签被手动关掉、Chrome 整体退出、或页面崩溃（renderer crash）。
# 与瞬时错误不同——目标真没了，重试 evaluate 无意义，要立刻抛 TargetClosedError
# 给出可操作的中文提示（2026-08-21 实测：attrs 阶段中途页签被关，报的是
# "Target page, context or browser has been closed"，不在瞬时错误清单里，
# 直接抛了英文原文，排查时看不出是页签被关还是崩溃）。
_CLOSED_ERRORS = (
    "Target page, context or browser has been closed",
    "Target closed",
    "Target crashed",
    "Browser has been closed",
)

# 店小秘列表接口的限流提示词。全量扫描（SCAN_LIMIT=None）要连翻几十页、页间连发 fetch，
# 中段开始接口会返回 code!=0 的「系统繁忙,请稍后重试」这类限流提示（2026-09-08 实测：
# 取消单次扫描条数上限后，数据搬家池 1796 条翻 36 页就触发）。这是业务层返回的 msg，
# 不是 evaluate 的瞬时错误，eval_json 的瞬时重试兜不住，得单独判「繁忙」再退避。
_BUSY_HINTS = ("系统繁忙", "稍后重试", "稍后再试", "繁忙", "请稍候")


class TargetClosedError(RuntimeError):
    """页面/浏览器已关闭（页签被关、Chrome 退出或页面崩溃）——不可重试，须重建会话。"""


# ---- 共享页面锁 -------------------------------------------------------------
# 【为什么锁在这一层，而不在各自的扫描模块里】本模块的会话模型是「挑一个店小秘页签复用」
# （见 BrowserSession.open），于是任何两个各自 new BrowserSession() 的只读扫描，
# 拿到的其实是【同一个页签】——各自在自己模块里加锁毫无用处，A 正在读列表 DOM 时
# B 一个 navigate 就把页面换走了（表现为 A 读到另一个列表的行，还完全静默）。
# 2026-08-30 新增未认领清单扫描（app/publish/crawlbox.py）后这条路径才真实存在：
# 在那之前只有采集箱一个扫描者，它自己的模块级锁够用。
#
# 与发布作业的互斥不走这把锁：发布是 15 阶段跨多次调用共用一个页面的长流程，
# 拿锁拿几十分钟不现实。那边的取向是「有作业在跑就跳过这一轮扫描」（见
# collectbox._tick 与 app.py 的 _publish_job_busy），扫描迟一轮毫无损失。
PAGE_LOCK = asyncio.Lock()


# evaluate/eval_json 的「没传参」哨兵：None 是合法的 JS 参数（null），不能拿它判断
_NO_ARG = object()


async def eval_list_page(session, code: str, arg: Any = _NO_ARG,
                         busy_retries: int = 3, busy_backoff: float = 2.0) -> dict:
    """页面内 fetch 取一页列表 JSON，对店小秘「系统繁忙」限流做退避重试。

    【为什么单独一个函数，而不是三个扫描模块各写一遍】session.eval_json 的重试只覆盖
    evaluate 的瞬时错误（上下文销毁、导航中），兜不住接口返回的**业务**限流——那种情况
    下 evaluate 本身成功，是接口 body 里 code!=0、msg=「系统繁忙,请稍后重试」。全量扫描
    翻页撞上它若直接抛，整轮扫描失败、上次清单还保留，用户看到的就是「扫描失败：系统繁忙」。
    这里把「判繁忙 → 退避 sleep → 重取」收成一处，banjia/crawlbox/collectbox 共用。

    非限流的业务错误（ok=false 但 msg 不含繁忙词）不重试、原样返回，交由调用方按原逻辑
    raise 出带具体信息的错误；重试耗尽仍繁忙也原样返回最后一个结果，同样由调用方 raise——
    这样调用方只需把 eval_json 换成 eval_list_page，判断逻辑一字不改。

    设计成接受 session 参数的模块级函数而不是 BrowserSession 方法：三个扫描模块的测试用
    假 session 只需实现 eval_json 即可，不必再补一个新方法。
    """
    d: dict = {}
    for attempt in range(1, busy_retries + 1):
        d = await session.eval_json(code, arg=arg)
        if d.get("ok") is not False:
            return d
        hint = str(d.get("msg") or d.get("status") or "")
        if not any(w in hint for w in _BUSY_HINTS):
            return d
        if attempt >= busy_retries:
            return d
        logger.warning(
            f"列表接口限流，第 {attempt}/{busy_retries} 次退避 "
            f"{busy_backoff * attempt:.1f}s：{hint}")
        await asyncio.sleep(busy_backoff * attempt)
    return d


# ---- 页面 toast 哨兵 --------------------------------------------------------
# 【为什么要常驻监听，而不是在需要时查一次】店小秘的错误提示是浮层 toast，2~3 秒自动
# 消失。2026-08-24 排查 890843533224 时，编辑页弹的「该分类已在平台删除！」就是这么
# 溜掉的：日志里只剩后续 ⑧⑨⑩⑪「无处可填」的一串结果，根因一个字都没留下，只能靠人
# 盯着屏幕才看见。事后 eval_json 去查也查不到——那时浮层早被移除了。
# 故改成在页面里挂 MutationObserver，toast 一出现就通过 Playwright binding 回调进
# Python 打日志；service 层的日志桥（_install_log_bridge）会把它一并推到 UI。
#
# 去重放在 JS 侧：同一条文案 3 秒内只报一次（ant 的浮层进出场动画会重复触发 childList），
# 少一次跨进程调用也少一行重复日志。
_TOAST_BINDING = "__dxmToast"

# 判级用的关键词：命中即 warning（用户需要知道），其余按 info 记流水。
_TOAST_BAD_WORDS = ("错误", "失败", "异常", "不能", "不可", "无权", "删除",
                    "超时", "请先", "必填", "无效", "已存在")

# 最近的 toast 环形缓冲（(时间戳, 文案)），供调用方回看「刚才那次点击弹了什么」。
#
# 【为什么 toast 必须留缓冲，不能只打日志】2026-09-01 取证（两单 1067271196776、
# 1051827161006）：save 后平台弹「错误：请上传预览图」，这条被本模块的 toast 哨兵
# 抓到并记了 warning，但阶段⑭ 的 _JS_SAVE_FEEDBACK 只读 .ant-message /
# .ant-notification —— 店小秘的 d-message 是自有浮层，两个选择器都抓不到，于是
# save 判据两边都空，只能报「无校验错误但草稿更新时间未变，保存可能未生效」这种
# 查不下去的结论，把排查方向从「预览图没上传」带偏到「保存点击没生效」。
# 真因当时就明明白白弹在页面上，只是没人把它接住。
# 故在打日志的同一处留一份带时间戳的缓冲，让 save 能按时间窗回捞。
_TOAST_LOG: list = []
_TOAST_LOG_MAX = 60


def recent_toasts(since: float = 0.0, bad_only: bool = False) -> list:
    """回看 since（time.time() 时间戳）之后出现的 toast 文案，最新在后。

    save 这类「点一下然后看页面怎么回应」的场景用它取证：按时间窗过滤，避免把
    上一个阶段的旧提示算进本次结论。bad_only=True 时只留命中 _TOAST_BAD_WORDS 的。
    """
    out = [t for ts, t in _TOAST_LOG if ts >= since]
    if bad_only:
        out = [t for t in out if any(w in t for w in _TOAST_BAD_WORDS)]
    # 同一条文案在一个窗口里可能重复出现（浮层动画），按首次出现去重保序
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

_JS_TOAST_WATCH = r"""(() => {
  if (window.__dxmToastInstalled) return JSON.stringify({installed: false, reason: 'already'});
  window.__dxmToastInstalled = true;
  // d-message 是店小秘自有浮层，ant-message/ant-notification 是组件库的，三种都收
  const SEL = '.d-message, .ant-message-notice, .ant-notification-notice';
  const seen = new Map();
  const clean = t => (t || '').replace(/\s+/g, ' ').replace(/×\s*$/, '').trim();
  const report = el => {
    const text = clean(el.textContent);
    if (!text) return;
    const now = Date.now();
    if (now - (seen.get(text) || 0) < 3000) return;
    seen.set(text, now);
    try { window.__DXM_BINDING__(text); } catch (e) {}
  };
  const scan = node => {
    if (!node || node.nodeType !== 1) return;
    if (node.matches && node.matches(SEL)) report(node);
    if (node.querySelectorAll) node.querySelectorAll(SEL).forEach(report);
  };
  new MutationObserver(muts => {
    for (const m of muts) for (const n of m.addedNodes) scan(n);
  }).observe(document.documentElement, {childList: true, subtree: true});
  // 装之前就已经挂在页面上的（比如编辑页一加载就弹的类目失效提示）
  document.querySelectorAll(SEL).forEach(report);
  return JSON.stringify({installed: true});
})()""".replace("__DXM_BINDING__", _TOAST_BINDING)



class BrowserSession:
    """持有 CDP 连接 + 当前工作页面 + CDP session 的会话。

    刻意不做 async context manager 的一次性用法：发布 12 阶段跨多次调用共用同一页面，
    生命周期由 service 层控制（open() 一次、finally close()）。
    """

    def __init__(self, cdp_url: str = CDP_URL):
        self.cdp_url = _normalize_cdp(cdp_url)
        self._pw = None
        self._browser = None
        self._page: Optional[Page] = None
        self._cdp = None
        # 已装过 toast binding 的页面：expose_binding 同名重复注册会抛，
        # 而一个会话里 navigate 会反复回到同一个 Page 对象，故按页面记一次。
        self._toast_pages: set = set()

    # ---- 连接与页面 ---------------------------------------------------------
    async def open(self, url_hint: str = DIANXIAOMI_HOST) -> Page:
        """连上已登录 Chrome，挑一个店小秘页签复用（没有则新开一个）。

        url_hint 用于挑页签：优先复用已经停在店小秘域下的页签，避免把用户正在看的
        其它页面抢走导航。挑不到才 new_page。
        """
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
        if not self._browser.contexts:
            raise RuntimeError(f"CDP 连上了但没有 context（{self.cdp_url}）")
        ctx = self._browser.contexts[0]
        host = url_hint.split("//", 1)[-1].split("/", 1)[0]
        # 【跳过编辑页签】失败保留的编辑页签里可能还有未落库的表单，被这里复用再导航
        # 就会把那份现场冲掉（见 park_edit_tab）。故只复用非编辑页的店小秘页签，
        # 编辑页签一律留给人工接手。
        cands = [p for p in ctx.pages
                 if host in (p.url or "") and "/popTemu/edit" not in (p.url or "")]
        self._page = cands[0] if cands else await ctx.new_page()
        if not cands:
            logger.info(f"未找到店小秘页签，新开一个（{url_hint}）")
            await self._page.goto(url_hint, wait_until="domcontentloaded")
        self._watch_page(self._page)
        self._cdp = await ctx.new_cdp_session(self._page)
        await self.fix_hidden_tab()
        await self.install_toast_watch()
        return self._page

    @staticmethod
    def _watch_page(page: Page) -> None:
        """页签崩溃/被关时留一条日志：否则只能在几分钟后某次 evaluate 失败时
        才知道页面没了，且看不出是崩溃还是人为关闭（2026-08-21 的 attrs 阶段
        报错就是这么来的）。"""
        page.on("crash", lambda p: logger.error(f"页签崩溃（renderer crash）：{p.url}"))
        page.on("close", lambda p: logger.warning(f"页签被关闭：{p.url}"))

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("会话未打开：先 await session.open()")
        return self._page

    def is_alive(self) -> bool:
        """会话是否还可用：页签没关且到浏览器的 CDP 连接还在。"""
        try:
            return bool(
                self._page is not None
                and not self._page.is_closed()
                and self._browser is not None
                and self._browser.is_connected()
            )
        except Exception:
            return False

    @property
    def edit_page_open(self) -> bool:
        """当前工作页是否停在店小秘编辑页（可能还带着未落库的表单修改）。

        给 service 层判断「商品失败后要不要保留这个页签」——编辑页上有已填好但没
        落库的表单，下一个商品再 navigate 就会冲掉；草稿列表/采集页/源站没有值得
        留的现场。用 URL 即时判而不存标志位：save/_publish_landed 那类「开临时页签
        再切回」的操作会换 self._page，存标志位容易不同步（见 park_edit_tab）。
        """
        try:
            return bool(self._page is not None
                        and not self._page.is_closed()
                        and "/popTemu/edit" in (self._page.url or ""))
        except Exception:
            return False

    async def park_edit_tab(self) -> dict:
        """把当前（编辑页）页签原样留在浏览器里不关，另开一个新页签作为工作页。

        【为什么需要】商品保存/发布失败时编辑页上还留着已填好但未落库的表单，直接
        让下一个商品 navigate 会把这份现场冲掉、白烧一轮 token；把页签留在 Chrome
        里，人工能接着处理。新页签先导航到店小秘首页（后续各阶段会自行导航），
        这一步失败也 best-effort——最坏停在 about:blank，后续 navigate 照样能走。
        """
        try:
            ctx = self._browser.contexts[0]
            page = await ctx.new_page()
        except Exception as e:
            logger.warning(f"保留编辑页签失败（忽略，继续用当前页签）：{e}")
            return {"ok": False, "err": str(e)}
        self._page = page
        self._watch_page(page)
        try:
            self._cdp = await ctx.new_cdp_session(page)
        except Exception as e:
            logger.warning(f"新页签重建 CDP 会话失败（忽略）：{e}")
        try:
            await page.goto(DIANXIAOMI_HOST, wait_until="domcontentloaded",
                            timeout=30 * 1000)
        except Exception as e:
            logger.warning(f"新页签导航店小秘首页失败（后续阶段会自行导航）：{e}")
        await self.fix_hidden_tab()
        await self.install_toast_watch()
        logger.info(f"已保留编辑页签，另开新页签作为工作页：{self._page.url}")
        return {"ok": True, "url": self._page.url}

    async def close(self) -> None:
        """只断开 CDP 连接，不关用户的浏览器和页签（best-effort，坏了不影响主流程）。"""
        for closer, what in (
            (lambda: self._browser.close() if self._browser else None, "browser"),
            (lambda: self._pw.stop() if self._pw else None, "playwright"),
        ):
            try:
                r = closer()
                if r is not None:
                    await r
            except Exception as e:
                logger.warning(f"断开 {what} 失败（忽略）：{e}")
        self._page = self._cdp = self._browser = self._pw = None
        self._toast_pages.clear()

    # ---- 6 个原语（签名照抄 skill）------------------------------------------
    async def evaluate(self, code: str, timeout: int = 90, arg: Any = _NO_ARG) -> dict:
        """执行 JS，返回 {"ok": bool, "data": {"value": ...}} —— 结构与 WebBridge 一致。

        保持这个包了一层的返回结构（而不是直接返回值），是为了让原脚本里
        `resp.get("ok")` / `resp["data"]["value"]` 的判断能原样搬过来。

        arg 给了就走 Playwright 的传参调用（此时 code 必须是箭头函数/函数表达式）。
        【为什么用哨兵而不是 arg=None 判断】None 是合法的 JS 参数（null），拿它当
        「没传参」会让 `evaluate(fn, arg=None)` 静默变成无参调用、JS 侧收到 undefined。
        """
        try:
            coro = (self.page.evaluate(code) if arg is _NO_ARG
                    else self.page.evaluate(code, arg))
            value = await asyncio.wait_for(coro, timeout=timeout)
            return {"ok": True, "data": {"value": value}}
        except asyncio.TimeoutError:
            return {"ok": False, "err": f"JS 执行超时（{timeout}s）"}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    async def eval_json(self, code: str, timeout: int = 90, retries: int = 3,
                        arg: Any = _NO_ARG) -> dict:
        """执行 JS 并解析 JSON 返回值，对瞬时错误自动重试。

        原脚本的 113 处 JS 全部以 `JSON.stringify(...)` 收尾，故返回值是字符串、这里再
        json.loads。但 Playwright 的 evaluate 会把 JS 值直接反序列化成 Python 对象，
        若某段 JS 返回的已是对象（新写的代码），这里直接放行，不强行再 loads。

        arg 给了就把它作为参数传给 code（code 须是箭头函数），语义见 evaluate。
        新写的 JS 推荐走这条路：比 fill_js 的字面量替换少一层转义、且参数是结构化值。
        """
        last_err: Any = None
        for attempt in range(1, retries + 1):
            resp = await self.evaluate(code, timeout, arg=arg)
            if resp.get("ok"):
                val = resp.get("data", {}).get("value")
                if val is None or val == "":
                    return {}
                if isinstance(val, (dict, list)):
                    return val
                return json.loads(val)
            last_err = resp
            err_str = str(resp)
            if any(t in err_str for t in _CLOSED_ERRORS):
                raise TargetClosedError(
                    f"页面/浏览器已关闭，JS 无法继续执行：{resp.get('err')}。"
                    "常见原因：手动关了店小秘页签、Chrome 整体退出、或页面崩溃。"
                    "重新打开 Chrome/页签后可从断点阶段续跑。"
                )
            if not any(t in err_str for t in _TRANSIENT_ERRORS) or attempt >= retries:
                raise RuntimeError(f"JS 执行失败: {resp}")
            await asyncio.sleep(1.5 * attempt)
        raise RuntimeError(f"JS 执行失败: {last_err}")

    async def wait_for(
        self,
        code: str,
        predicate: Callable[[Any], bool],
        timeout: float = 30,
        interval: float = 1.5,
    ) -> dict:
        """轮询执行 JS 直到 predicate 成立；超时返回最后一次结果（不抛，由调用方判）。

        沿用原脚本「超时不抛、返回最后结果」的语义：多处调用点靠回读字段自己判失败并
        给出更具体的错误信息，这里抢先抛异常会丢掉那些上下文。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        data: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            try:
                data = await self.eval_json(code)
            except TargetClosedError:
                raise  # 页面真没了，再轮询也是白费，立刻抛给调用方
            except RuntimeError as e:
                # 导航中途轮询撞上上下文销毁属正常，继续等下一轮
                logger.debug(f"wait_for 轮询失败，继续等：{e}")
                data = {}
            if predicate(data):
                return data
            await asyncio.sleep(interval)
        return data

    async def navigate(
        self, url: str, new_tab: bool = False, timeout: int = 60
    ) -> dict:
        """导航到 url。new_tab=True 时新开页签并把会话切到它。

        导航后必须重发 fix_hidden_tab：那两个 CDP 开关每次导航/刷新即失效
        （2026-08-19 实测，见 fix_hidden_tab）。原 pipeline 是在各阶段手动重发，
        这里收进 navigate，少一个漏发的机会。

        【2026-09-03 1688 数据注入时机问题】1688 详情页的 window.context 数据是通过
        服务端注入的 <script> 标签加载的，但该脚本可能在 domcontentloaded 之后才执行。
        手动登录后仍报「页面数据未就绪」，排查发现 domcontentloaded 时机太早，数据还没
        注入完成。故对 1688 域名改用 "load" 事件（等所有资源加载完），给数据注入脚本
        足够的执行时间。其它域名保持 domcontentloaded（更快）。
        """
        if new_tab:
            ctx = self._browser.contexts[0]
            self._page = await ctx.new_page()
            self._watch_page(self._page)
            self._cdp = await ctx.new_cdp_session(self._page)
            # 新页签是新的 Page 对象，binding 要重新注册（下面 goto 后统一装）

        # 1688 域名用 load，其它用 domcontentloaded（见上面注释）
        wait_event = "load" if "1688.com" in url else "domcontentloaded"
        try:
            await self.page.goto(url, wait_until=wait_event, timeout=timeout * 1000)
        except Exception as e:
            return {"ok": False, "err": str(e)}
        await self.fix_hidden_tab()
        # 导航把 window 换掉了，MutationObserver 随之消失，必须重装（binding 本身
        # 由 expose_binding 持久注册，只有页面内那段 JS 要重跑）
        await self.install_toast_watch()
        return {"ok": True, "url": self.page.url}

    async def adopt_open_page(self, must_include: str) -> dict:
        """把会话切到【已打开且 URL 含 must_include】的页签，不导航。

        【为什么需要它：有些来源站重新导航就丢数据】2026-08-27 实测 Temu 买家页：
        用户已打开的商品页 window.rawData.store 完整（goodsId/goods/sku 全有），但对
        同一个 URL 执行 page.goto 会被 302 到 login.html?login_scene=2，SSR 数据压根
        不注入。也就是说那个页面所处的会话上下文是【导航不可复现】的——它是用户从站内
        点进去的，带着一整套 referer/session 状态。

        对这类站唯一可靠的取数方式就是「用户开着，我们只读」。这与本项目 CDP 接管
        真实浏览器的取向一致（见模块头），也是采集侧一贯的做法：不模拟登录、不重放
        会话，直接用人已经登进去的那个窗口。

        返回 {"ok", "url"}；没有匹配页签时 ok=False，由调用方决定是否退回 navigate。
        【不改 self._page 以外的东西】cdp 会话要跟着换页签重建，否则后续 cdp() 还打在
        旧页签上（素材图悬停菜单那类操作会点错窗口）。
        """
        try:
            ctx = self._browser.contexts[0]
            def matches_page(page_url):
                parsed = urlsplit(page_url or "")
                if re.search(r"login|signin|captcha|verification|punish", parsed.path, re.I):
                    return False
                if must_include.startswith("-g-"):
                    product_id = must_include[3:]
                    return (parsed.hostname in ("temu.com", "www.temu.com")
                            and bool(re.search(r"(?:/|-)g-" + re.escape(product_id) + r"\.html$", parsed.path)))
                return must_include in (page_url or "")
            cands = [p for p in ctx.pages
                     if not p.is_closed() and matches_page(p.url)]
        except Exception as e:
            return {"ok": False, "err": f"枚举页签失败：{e}"}
        if not cands:
            return {"ok": False, "err": f"没有已打开的页签匹配 {must_include!r}"}
        page = cands[0]
        if page is self._page and self._cdp is not None:
            return {"ok": True, "url": page.url}
        if self._cdp is not None:
            try:
                await self._cdp.detach()
            except Exception as error:
                logger.debug(f"旧页签 CDP 会话已不可用，继续切换：{error}")
        self._page = page
        self._watch_page(page)
        try:
            self._cdp = await ctx.new_cdp_session(page)
        except Exception as e:
            logger.warning(f"切页签后重建 CDP 会话失败（忽略）：{e}")
        await self.fix_hidden_tab()
        await self.install_toast_watch()
        logger.info(f"复用已打开的页签（不导航）：{(page.url or '')[:100]}")
        return {"ok": True, "url": page.url}

    async def mouse_click(self, selector: str, timeout: int = 15) -> dict:
        """真实鼠标点击（Playwright locator.click 底层走 Input.dispatchMouseEvent）。

        【与 skill 实测结论的冲突，务必注意】原脚本记录：编辑页上 WebBridge mouse_click
        与 CDP Input.dispatchMouseEvent 都【被吞】（坐标命中、返回成功，但零网络请求），
        故编辑页一律用 JS el.click()；而认领弹窗的店铺复选框反过来——JS .click() 不触发
        Vue，必须真实点击。两条结论都要在 Playwright 下逐个场景重验，不能假定继承。
        真实点击不生效时的退路是项目现成的 gui_action 视觉定位（browser_use_tool）。
        """
        try:
            await self.page.locator(selector).first.click(timeout=timeout * 1000)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    async def cdp(self, method: str, params: Optional[dict] = None) -> dict:
        """直发 CDP 命令（素材图悬停菜单等必须真实鼠标移动的场景用）。"""
        try:
            r = await self._cdp.send(method, params or {})
            return {"ok": True, "data": r}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    # ---- 环境修复（原 pipeline 的 fix_hidden_tab / kill_stuck_modals）--------
    async def fix_hidden_tab(self) -> None:
        """修隐藏/被遮挡页签的 rAF 节流（2026-08-19 实测）。

        症状：下拉/弹窗 DOM 有内容但 getBoundingClientRect 恒 -9999、
        document.visibilityState === "hidden"——浏览器窗口被遮挡导致 requestAnimationFrame
        被节流，ant-design 的浮层定位依赖 rAF，于是永远算不出位置。
        两个开关每次导航/刷新后失效，需重发（已收进 navigate）。best-effort：
        修不上不该让阶段失败，真定位不到后续回读自会报错。
        """
        for method, params in (
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ("Page.setWebLifecycleState", {"state": "active"}),
        ):
            r = await self.cdp(method, params)
            if not r.get("ok"):
                logger.warning(f"fix_hidden_tab {method} 失败（忽略）：{r.get('err')}")
        await asyncio.sleep(0.5)

    async def install_toast_watch(self) -> dict:
        """在当前页面挂 toast 哨兵：浮层一出现就打进日志（见 _JS_TOAST_WATCH 上方注释）。

        两段合起来才生效：expose_binding 在【页面对象】上注册一次 Python 回调（跨导航
        有效，故按 _toast_pages 去重）；_JS_TOAST_WATCH 是【window 级】的 observer，
        每次导航后都要重跑，由 navigate 负责重装。

        best-effort：装不上只是少了根因线索，不该让阶段失败（对齐 fix_hidden_tab）。
        """
        page = self._page
        if page is None:
            return {"ok": False, "err": "会话未打开"}
        try:
            if page not in self._toast_pages:
                await page.expose_binding(
                    _TOAST_BINDING, lambda _src, text: self._on_toast(text))
                self._toast_pages.add(page)
            r = await self.evaluate(_JS_TOAST_WATCH, timeout=15)
            if not r.get("ok"):
                logger.warning(f"toast 哨兵安装失败（忽略）：{r.get('err')}")
                return {"ok": False, "err": r.get("err")}
            return {"ok": True}
        except Exception as e:
            logger.warning(f"toast 哨兵安装失败（忽略）：{e}")
            return {"ok": False, "err": str(e)}

    @staticmethod
    def _on_toast(text: str) -> None:
        """页面 toast 的落地点：按关键词判级后写 loguru（service 的日志桥会推到 UI）。

        文案里带关键词的记 warning（用户要知道「该分类已在平台删除！」这类），其余
        （「保存成功」之类）记 info 留流水。回调在事件循环里同步执行，只做打印，
        不做任何可能抛的事——binding 里抛异常会污染页面上那次 JS 调用。
        """
        try:
            msg = (text or "").strip()
            if not msg:
                return
            # 先入缓冲再打印：调用方（如阶段⑭ save）要能回捞本次点击后的提示
            _TOAST_LOG.append((time.time(), msg))
            if len(_TOAST_LOG) > _TOAST_LOG_MAX:
                del _TOAST_LOG[:-_TOAST_LOG_MAX]
            if any(w in msg for w in _TOAST_BAD_WORDS):
                logger.warning(f"页面提示：{msg}")
            else:
                logger.info(f"页面提示：{msg}")
        except Exception:
            pass

    async def kill_stuck_modals(self) -> dict:
        """清理卡在 ant-fade-leave-active 的 modal（遮罩挡全页导致后续全点不中）。"""
        return await self.eval_json(
            """(() => {
              let n = 0;
              document.querySelectorAll('.ant-modal-wrap').forEach(w => {
                if (w.offsetHeight > 0) { w.remove(); n++; } });
              document.querySelectorAll('.ant-modal-mask').forEach(m => {
                if (m.offsetHeight > 0) { m.remove(); n++; } });
              document.body.style.overflow = '';
              return JSON.stringify({removed: n});
            })()"""
        )


# ---- 模块级便捷入口 --------------------------------------------------------
def J(obj: Any) -> str:
    """把 Python 值转成可嵌进 JS 的字面量（原脚本同名工具，占位符替换用）。"""
    return json.dumps(obj, ensure_ascii=False)


def fill_js(template: str, **kwargs: Any) -> str:
    """把 JS 模板里的 __NAME__ 占位符替换成 JSON 字面量。

    原脚本写法是链式 .replace("__NAME__", json.dumps(x))，多个占位符时又长又容易错顺序。
    这里收敛成一次调用：fill_js(js, NAME=name, IDX=idx)。
    """
    out = template
    for key, value in kwargs.items():
        out = out.replace(f"__{key}__", J(value))
    left = re.findall(r"__[A-Z_]+__", out)
    if left:
        raise ValueError(f"JS 模板仍有未替换的占位符: {sorted(set(left))}")
    return out


async def ensure_cdp_alive(cdp_url: str = CDP_URL, retries: int = 3, wait: float = 5.0) -> bool:
    """CDP 健康检查（与 collect/service.ensure_cdp_alive 同语义，避免循环依赖故独立一份）。"""
    cdp_url = _normalize_cdp(cdp_url)
    for attempt in range(1, retries + 1):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                _ = browser.contexts
                await browser.close()
            return True
        except Exception as e:
            logger.warning(f"CDP ping 失败（{attempt}/{retries}）：{e}")
            if attempt < retries:
                await asyncio.sleep(wait)
    logger.error(
        f"CDP 连续 {retries} 次连不上（{cdp_url}）。"
        f"请确认已登录的 Chrome 仍以 --remote-debugging-port=9222 运行。"
    )
    return False
