"""Temu 后台「区域」维度的运行时识别与一致性校验。

## 为什么需要这一层

同一个卖家账号在后台顶栏分「全球 / 美国 / 欧区 / 商家中心」等区域标签，选中哪个区域，
其下的商品、订单数据就自动限定在该区域——页面自己会过滤，采集端不必也不该重建筛选。
但代码此前把「店铺」当成了唯一维度（只按 mallid 分组），于是同一账号跨区域的数据会被
归成同一个店，落进同一张 Sheet。本机 worklist 实测就是这个症状：716 条商品同挂
mallid=6344...796，站点却混着秘鲁 277 / 哥伦比亚 228 / 美国站 211。

## 实测结论（2026-08-07，调试 Chrome 只读探测）

- 区域切换是顶栏一排 **无 href 的 `<a>`**（纯前端跳转），点下去**换域名**：
  全球 `agentseller.temu.com` → 美国 `agentseller-us.temu.com`。
- 这排 `<a>` 的类名形如 `index-module__drItem___2UzKL`，选中的那个**追加**
  `index-module__active___3Jovd`。`___xxxx` 是构建期 hash，改版即变，故按子串
  `drItem` / `active` 匹配，不写全名。
- **cookie 靠不住**：`region=211`、`mallid=6344...796` 在切区域前后完全不变
  （`211` 只是碰巧与美国站订单前缀 `PO-211-` 同码，与当前区域无关）。
- 该会话的 product-select 页读不到 `window.rawData`，故区域不能指望它。

## 设计取向

区域标识 = `(host, 区域显示名)`，**两者都在运行时从页面取**：host 取自 `page.url`，
显示名取自顶栏激活标签的文字。刻意**不做域名→区域名的映射表**——账号能看到哪些区域随
权限变（有的账号没有欧区），域名规则也没实测全，写死映射就是在猜。读不到就返回空区域，
让调用方中止并提示用户，而不是替用户选一个（对齐 orders 侧 detect_store 的取向：
落点猜错比不写更糟）。
"""
import asyncio
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlparse

from app.logger import logger

# 读顶栏区域切换器：**纯按结构定位，不依赖任何类名**。
#
# 【为什么不能按类名】2026-08-07 实测同一账号两类页面的类名体系完全不同：
#   product-select：`<a class="index-module__drItem___2UzKL ...active___3Jovd">`
#   订单页 mmsos ：`<div class="_2JBlx01R _1Q9JwBPE">`（纯 hash，无任何语义子串）
# 早先按 `drItem`/`active` 子串匹配，在订单页上一个都读不到（下拉候选为空就是这个原因）。
# 类名是构建产物、按页面 bundle 各自 hash，靠它必然漏页面。
#
# 【跨版本一致的结构特征】区域切换器总是：顶栏右侧、同一父容器下的一组【同类兄弟节点】，
# 每个节点是纯文本短标签、cursor:pointer。所以按「父容器下有 ≥2 个可见短文本子节点、
# 且整组位于页面顶部」来定位，任何类名体系都能读到，区域名也不必写死在代码里。
#
# 【选中态怎么判】选中态类名也是 hash（`_1Q9JwBPE` / `active___3Jovd`），跨页面不通用。
# 但两类页面都在顶栏左侧放了一个 **font-weight:600 的当前区域名**（订单页 x=396、
# product-select x=472，都在切换器左边），它与切换器里的某个标签同名——用它判当前区域，
# 比 hash 类名稳。读不到这个标签时退回「类名含 active 且只命中一个」的老判据。
_REGION_GROUP_JS = r"""
  // 定位顶栏区域切换器那一组，返回按 x 排序的 [{el, text, x, cls}]；找不到返回 []。
  //
  // 判据（全部来自 2026-08-07 实测，不含任何类名）：
  //  - 位于顶栏（top <= 140）、同一父容器下的 ≥2 个可见短文本叶子节点
  //  - **横排同一行**：这是区域切换器与「商家助手」竖排浮层菜单的决定性区别
  //    （实测浮层组 sameRow=false / xSpread=0，区域组 4 项同 y、x 依次展开）
  //  - 类名首 token 一致（同一渲染循环产出），用于打分排除杂牌组合
  //
  // 【刻意不按 cursor:pointer 过滤】区域标签在自身区域/无权限时是 disabled、cursor:auto，
  // 早先加了 pointer 过滤，导致 product-select 页的区域组被整组滤掉、反而误中助手菜单。
  const _regionGroup = () => {
    const norm = (el) => (el.textContent || '').replace(/\s+/g, '').trim();
    const groups = new Map();
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length > 1) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      if (r.top < 0 || r.top > 140) continue;
      const t = norm(el);
      if (!t || t.length < 2 || t.length > 10) continue;
      const p = el.parentElement;
      if (!p) continue;
      if (!groups.has(p)) groups.set(p, []);
      groups.get(p).push({
        el, text: t, x: Math.round(r.left), y: Math.round(r.top),
        cls: String(el.className || ''),
      });
    }
    // 顶栏左侧的粗体短文本集合：当前区域名一定在里面（平台在页面标题左侧重复显示它）。
    // 这是区分「区域切换器」与「学习/运营对接/规则中心…」这类功能按钮组的**决定性判据**：
    // 后者的任何一项都不会以粗体出现在顶栏左侧（实测全球域 product-select 上，功能按钮组
    // 6 项同行且更靠右，纯靠几何/类名打分会盖过区域组）。
    const boldLeft = new Set();
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length > 0) continue;
      const r = el.getBoundingClientRect();
      if (r.top < 0 || r.top > 140) continue;
      const w = getComputedStyle(el).fontWeight;
      if (!(String(w) === 'bold' || Number(w) >= 600)) continue;
      const t = norm(el);
      if (t && t.length >= 2 && t.length <= 10) boldLeft.add(t);
    }

    let best = null;
    for (const [, items] of groups) {
      if (items.length < 2) continue;
      const ys = items.map(i => i.y);
      const xs = items.map(i => i.x);
      if (Math.max(...ys) - Math.min(...ys) > 6) continue;   // 必须同一行（排除竖排浮层）
      if (Math.max(...xs) - Math.min(...xs) < 20) continue;  // 必须横向展开
      const base = items[0].cls.split(/\s+/)[0] || '';
      const same = items.filter(i => !base || i.cls.split(/\s+/)[0] === base).length;
      // 组内有项与顶栏粗体文本同名 → 几乎必然是区域切换器，权重给到压倒性
      const anchored = items.some(i => boldLeft.has(i.text)) ? 1 : 0;
      const score = anchored * 100000 + same * 100 + items.length * 10
        + Math.max(...xs) / 1000;
      if (!best || score > best.score) best = {items, score, anchored};
    }
    // 没有任何组被粗体锚定 → 说明这个页面读不到区域切换器（顶栏未渲染完/改版），
    // 返回空而不是把功能按钮组当区域交出去（宁可让调用方报「读不到」，也不给错值）。
    return best && best.anchored ? best.items.sort((a, b) => a.x - b.x) : [];
  };
"""

_ACTIVE_REGION_JS = r"""
() => {
  __REGION_GROUP__
  const norm = (el) => (el.textContent || '').replace(/\s+/g, '').trim();

  const tabs = _regionGroup();
  if (!tabs.length) return {labels: [], active: '', ambiguous: []};
  const labels = tabs.map(t => t.text);

  // ② 当前区域：顶栏左侧那个 font-weight:600 的短文本，且其文字必须是上面某个标签
  //    （不在标签集合里的粗体文字是别的东西，一律不认）。
  let active = '';
  const leftMost = tabs[0].x;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 0) continue;
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.top > 140) continue;
    if (r.left >= leftMost) continue;                 // 必须在切换器左边
    const t = norm(el);
    if (!labels.includes(t)) continue;
    const w = getComputedStyle(el).fontWeight;
    if (String(w) === '600' || String(w) === 'bold' || Number(w) >= 600) {
      active = t;
      break;
    }
  }

  // ③ 退路：没有那个粗体标签时，用「类名含 active 且恰好命中一个」的老判据
  let ambiguous = [];
  if (!active) {
    const on = tabs.filter(t => /active/i.test(t.cls));
    if (on.length === 1) active = on[0].text;
    else if (on.length > 1) ambiguous = on.map(t => t.text);
  }
  return {labels, active, ambiguous};
}
"""


def host_of(url: str) -> str:
    """取 URL 的 hostname（小写）。解析不出返回空串。

    host 是区域的硬事实：切区域换域名，这比任何 DOM 类名都稳，故它是区域标识的主键，
    顶栏文字只作人类可读的显示名。
    """
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""


@dataclass(frozen=True)
class Region:
    """一个已识别的区域：host 是主键，label 是顶栏显示名，labels 是该账号可见的整排区域。

    `ok` 要求 host 与 label 都拿到：只有 host 说明页面在某个域但顶栏没读出来（可能没
    渲染完/改版），此时不该当成「已确认区域」——用户自主选择这件事就没验证到。
    """

    host: str = ""
    label: str = ""
    labels: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.host and self.label)

    @property
    def key(self) -> str:
        """用于分组/判重的稳定键：host（区域主键）。label 会随文案改版变，不进键。"""
        return self.host

    def describe(self) -> str:
        return f"{self.label or '未识别'}（{self.host or '无 host'}）"


async def read_region(page) -> Region:
    """读某个页签当前所处的区域。读不到顶栏则只带 host 返回（ok=False）。

    纯只读：只 evaluate 读 DOM，不点任何标签。要切区域走 switch_region（那是显式动作，
    由「UI 上选定的区域」驱动，见其 docstring）。
    """
    host = host_of(getattr(page, "url", "") or "")
    try:
        got = await page.evaluate(_ACTIVE_REGION_JS) or {}
    except Exception as e:
        logger.warning(f"读区域标签失败（host={host or '未知'}）：{e}")
        return Region(host=host)

    labels = tuple(str(x) for x in (got.get("labels") or []))
    ambiguous = [str(x) for x in (got.get("ambiguous") or [])]
    if ambiguous:
        logger.warning(
            f"顶栏有多个区域同时是选中态（{'/'.join(ambiguous)}），无法确定当前区域，"
            f"按未识别处理（host={host or '未知'}）"
        )
        return Region(host=host, labels=labels)
    return Region(host=host, label=str(got.get("active") or ""), labels=labels)


SELLER_HOST_MARK = "agentseller"


class RegionUnconfirmed(RuntimeError):
    """无法确认用户自主选定的区域，或多个页签区域不一致。

    单独的类型让上层（UI/CLI）能把它转成可操作提示（去浏览器里选好区域），
    而不是混进一般的抓取失败里。
    """


def is_seller_page(url: str) -> bool:
    """是不是 Temu 卖家后台页面（任意区域）。

    按 `agentseller` 子串判而不是等于某个域名：区域切换换域名（全球 `agentseller.`、
    美国 `agentseller-us.`），写死任一个都会漏掉别的区域。
    """
    host = host_of(url)
    return SELLER_HOST_MARK in host and "temu.com" in host


# 读某个区域标签的 href（可能为空）+ 是否禁用，供切换时决定目标 URL。
#
# 【为什么切区域不靠点击】实测（2026-08-07）：在业务页（product-select）上，除当前区域外
# 其余区域标签全带 `index-module__disabled___*` 类——JS 合成 click 和 Playwright 真实
# 鼠标点击都不会跳转。而且禁用与否随页面上下文变（在美国域下「全球」反而可点），靠点击
# 天生不可靠。
#
# 但区域【本身就是域名】（全球 agentseller.temu.com、美国 agentseller-us.temu.com），
# 而 host→区域名的对应关系可以在运行时【从页面读出来】：切到目标域后顶栏的 active 标签
# 就是该区域名。所以切换 = 换域名导航 + 复核，既绕开 disabled，也不需要写死映射表。
#
# 域名从哪来：优先读标签自己的 href（平台改版给上 href 时自动跟随）；没有 href 就用
# 「已知区域 host 的构造规律」——这条必须靠实测样本推，见 _candidate_hosts。
_READ_REGION_LINKS_JS = r"""
() => {
  __REGION_GROUP__
  // 与 _ACTIVE_REGION_JS 同一套结构定位，额外带出 href 供切换用
  return _regionGroup().map(({el, text, cls}) => ({
    text,
    // 区域项可能是 <a>（product-select）也可能是 <div>（订单页），后者没有 href
    href: (el.getAttribute && el.getAttribute('href')) || '',
    active: /active/i.test(cls),
    disabled: /disabled/i.test(cls),
  }));
}
"""

# 把共用的分组函数注入两段脚本（Playwright 传的是独立字符串，没法共享作用域）
_ACTIVE_REGION_JS = _ACTIVE_REGION_JS.replace("__REGION_GROUP__", _REGION_GROUP_JS)
_READ_REGION_LINKS_JS = _READ_REGION_LINKS_JS.replace(
    "__REGION_GROUP__", _REGION_GROUP_JS)


# 已实测的区域 host 前缀规律（2026-08-07）：
#   全球 = agentseller.temu.com（无后缀）
#   美国 = agentseller-us.temu.com
# 只有这两个是实测确认的。其它区域（欧区等）后缀未知，故【不猜】——切不过去就报错让操作者
# 手动切一次，代码不臆造域名（对齐项目「码/域名必须实测」的规矩）。
#
# 注意这【不是】区域名→域名的映射表：区域名是从页面读的，这里只记「host 长什么样」的样本。
# 目标 host 的首选来源仍是标签自己的 href；这份样本只在 href 缺失时兜底。
_KNOWN_REGION_HOST_PREFIX = {"全球": "agentseller", "美国": "agentseller-us"}


def _candidate_hosts(want: str, cur_host: str) -> list:
    """推测目标区域的候选 host，按可信度排序。空列表＝推不出来（调用方应报错，不猜）。"""
    base_domain = ""
    if cur_host and "." in cur_host:
        base_domain = cur_host.split(".", 1)[1]  # temu.com
    if not base_domain:
        return []
    prefix = _KNOWN_REGION_HOST_PREFIX.get(want)
    return [f"{prefix}.{base_domain}"] if prefix else []


async def _await_region(page, tries: int = 30, interval: float = 0.5) -> Region:
    """轮询等顶栏区域标签渲染出来再读区域。

    为什么需要等：`domcontentloaded` 只保证 HTML 解析完，顶栏是 React 挂载后才出现的。
    实测切区域后立刻读会拿到「未识别」（labels 为空），把切换成功误判成失败。
    等满 tries 仍读不到就返回最后一次结果（带 host、ok=False），由调用方判失败。
    """
    got = Region()
    for _ in range(tries):
        got = await read_region(page)
        if got.ok:
            return got
        await asyncio.sleep(interval)
    return got


async def switch_region(page, want_label: str, timeout: int = 60000) -> Region:
    """把页签切到指定区域（UI 上选定的那个），返回切换后的区域。

    【为什么允许代码切区域】区域以 UI 上的选择为准：操作者在采集页/订单页选定要作业的区域，
    浏览器当前停在哪个区域只是环境状态，不一致就切过去再干活，省掉「去浏览器里手动点一下
    再回来点开始」这一趟。

    【为什么靠导航而不是点标签】实测 2026-08-07：在业务页上，除当前区域外其余区域标签都带
    `disabled` 类，JS 合成 click 与 Playwright 真实鼠标点击都不跳转；且禁用与否随页面上下文
    变（在美国域下「全球」反而可点）。而区域本身就是域名，直接导航到目标域【实测可达】
    （开 agentseller-us.temu.com/newon/product-select → 顶栏 active 就是「美国」）。

    目标 URL 的域名来源：① 该区域标签自己的 href（平台改版给上 href 时自动跟随）；
    ② 已实测的 host 规律（仅全球/美国）。都拿不到就报错让操作者手动切，绝不臆造域名。
    路径与 query 保留当前页的，这样切完还停在同一个功能页、筛选参数也不丢。

    切换后【必须复核】：平台可能把无权限的区域重定向回默认区。复核不通过抛 RegionUnconfirmed，
    绝不带着「以为切好了」继续跑。
    """
    from urllib.parse import urlsplit, urlunsplit

    want = str(want_label or "").strip()
    if not want:
        raise RegionUnconfirmed("没有指定要切换到的区域")

    cur = await read_region(page)
    if cur.ok and cur.label == want:
        return cur  # 已在目标区域，不做无谓跳转

    try:
        links = await page.evaluate(_READ_REGION_LINKS_JS) or []
    except Exception as e:
        raise RegionUnconfirmed(f"读区域标签失败：{e}") from e

    labels = [str(x.get("text") or "") for x in links]
    hit = next((x for x in links if str(x.get("text") or "") == want), None)
    if hit is None:
        known = "、".join(labels) or "未读到"
        raise RegionUnconfirmed(
            f"顶栏没有「{want}」这个区域（当前可选：{known}）。"
            f"请确认该账号有此区域权限，或改选其它区域。"
        )

    cur_parts = urlsplit(page.url or "")
    targets = []
    href = str(hit.get("href") or "").strip()
    if href:  # 标签自带链接：最可信，直接用
        targets.append(href if href.startswith("http") else
                       urlunsplit(cur_parts._replace(path=href, query="", fragment="")))
    for host in _candidate_hosts(want, cur_parts.hostname or ""):
        # 保留当前路径与 query：切完仍停在同一功能页，筛选参数不丢
        targets.append(urlunsplit(cur_parts._replace(netloc=host)))

    if not targets:
        raise RegionUnconfirmed(
            f"无法确定「{want}」区域的地址：该区域标签没有链接，且其域名规律尚未实测确认。"
            f"请在浏览器里手动切到「{want}」后重试（代码不臆造域名）。"
        )

    last = ""
    for url in targets:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            last = str(e)
            logger.warning(f"导航到 {url} 失败（继续尝试下一个候选）：{e}")
            continue
        after = await _await_region(page)
        if after.ok and after.label == want:
            logger.info(f"已切换区域：{cur.describe()} → {after.describe()}")
            return after
        last = f"落地后区域是 {after.describe()}"
        logger.warning(f"导航到 {url} 后区域不是「{want}」（{last}），尝试下一个候选")

    raise RegionUnconfirmed(
        f"切换到「{want}」失败（{last or '无可用候选地址'}）。"
        f"可能该账号无此区域权限，或平台把它重定向回了默认区域。"
        f"请在浏览器里手动切一次确认。"
    )


async def confirm_region_from_context(ctx, want_label: str = "") -> Region:
    """从浏览器 context 里【用户自己打开的】卖家后台页签确认当前区域。

    给「自己新开页面」的管线（活动/订单）用：它们不复用用户页签干活，但必须知道要在哪个
    区域作业，才能把新页面开在对应域名下——硬编码全球域会把选定的美国区顶掉。

    want_label 非空 = UI 上显式选定了区域：与浏览器当前区域不一致时【切过去】（以 UI 为准），
    切完复核。为空 = 沿用浏览器当前区域（读不到则抛，不猜）。
    多个页签处在不同区域时抛 RegionUnconfirmed，不擅自挑一个。
    """
    pages = [p for p in getattr(ctx, "pages", []) if is_seller_page(getattr(p, "url", ""))]
    if not pages:
        raise RegionUnconfirmed(
            "没有打开任何 Temu 卖家后台页面，无法确认要在哪个区域作业。"
            "请先在调试 Chrome 里选好区域（顶栏「全球 / 美国 / 欧区」）并打开后台页面。"
        )

    # UI 指定了区域 → 以它为准：把第一个后台页签切过去（已在目标区域则原地返回）。
    # 只切这一个页签：它就是下面用来确认区域的基准，其余页签由后面的一致性校验兜。
    if str(want_label or "").strip():
        base = await switch_region(pages[0], want_label)
    else:
        base = await read_region(pages[0])
        if not base.ok:
            known = "、".join(base.labels) if base.labels else "未读到"
            raise RegionUnconfirmed(
                f"读不到当前区域（{base.describe()}；顶栏区域标签：{known}）。"
                "请确认后台页面已加载完成、顶栏能看到区域标签后重试。"
            )

    for p in pages[1:]:
        conflict = region_conflict(base, await read_region(p))
        if conflict:
            # UI 指定了区域时，其它页签也一并切过去——以 UI 为准就该把环境对齐，
            # 而不是让操作者回去手动收拾页签。切不动才报错。
            if str(want_label or "").strip():
                logger.info(f"页签 {getattr(p, 'url', '')[:60]} 不在目标区域，切换中")
                await switch_region(p, want_label)
                continue
            raise RegionUnconfirmed(
                f"{conflict}。多个后台页签处在不同区域时无法判断本批该在哪个区域作业，"
                "请只保留你要作业的那个区域的页面，或把它们都切到同一区域后重试。"
            )
    logger.info(f"已确认作业区域：{base.describe()}")
    return base


def url_in_region(path: str, region: Region) -> str:
    """把后台页面路径拼到【当前区域】的域名下。

    为什么不留硬编码 URL 常量：`https://agentseller.temu.com/...` 是全球域，管线拿它
    新开页面，等于把用户选定的美国区悄悄换回全球区（换域名的整页跳转）。故 URL 必须在
    运行时用已确认区域的 host 拼出来。

    path 传 `/activity/marketing-activity` 这样的绝对路径。region 无 host 时抛错——
    宁可停下，不默认全球域。
    """
    if not region.host:
        raise RegionUnconfirmed("区域未确认，无法确定后台域名（不默认用全球域）")
    return f"https://{region.host}{path if path.startswith('/') else '/' + path}"


def region_conflict(base: Region, cur: Region) -> str:
    """比对两个区域，返回冲突描述；一致返回空串。

    以 host 为准：顶栏文案可能改版（「美国」也可能写成「美区」），但域名是接口层事实。
    host 相同而 label 不同只告警不算冲突——同一区域的两种叫法，拦下来反而是误报。
    """
    if not cur.ok:
        return f"未能确认区域（{cur.describe()}）"
    if base.key != cur.key:
        return f"区域不一致：基准是 {base.describe()}，该页签是 {cur.describe()}"
    if base.label and cur.label and base.label != cur.label:
        logger.warning(
            f"同一区域 host（{cur.host}）读到两种显示名：{base.label} / {cur.label}，"
            f"按同一区域处理"
        )
    return ""
