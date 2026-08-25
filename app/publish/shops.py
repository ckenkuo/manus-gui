# -*- coding: utf-8 -*-
"""发布管线的店铺 / 站点枚举：给发布页的两个下拉供数据。

【为什么单独一个模块，不塞进 claim.py】claim.py 的职责是「认领」这个写动作，
本模块全是只读枚举。两者共用同一套弹窗选择器，但生命周期完全不同——枚举在
用户打开发布页时跑（要快、要幂等、绝不能落库），认领在批次里跑（会真实建草稿）。
混在一起最大的风险是：枚举侧一旦手滑点到「确定」，就在用户没发起批次的时候
凭空认领了一个商品。故本模块只做「打标 → 读 → 关弹窗」，代码里没有任何
指向「确定」按钮的分支。

【店铺与站点的取数路径不对称，这是平台决定的，不是设计选择】2026-08-24 实测：
  - 店铺：`/api/userIn.json` 的 `data.shopMap` 直接带全部店铺（含 platform /
    isDel / isExpire），一次 fetch 秒级返回、无副作用。Temu 店的 platform 是
    **pddkj**（拼多多跨境的历史代号，不是 "temu"）。
  - 站点：**没有任何接口能拿到**。翻遍 161 个前端 chunk 也没有站点表——它由
    接口下发到弹窗内。站点复选框只在【认领弹窗内】、且【必须先勾中某个店铺】
    才异步渲染（不勾店铺盯 20 秒始终是 0 项）。所以站点枚举只能开一次认领弹窗、
    勾一下目标店铺、读完立刻关掉。
故店铺是「进页面就能列」，站点是「选定店铺后按需探一次并缓存」。

【为什么不用列表页筛选区那 42 个站点】草稿列表页 searchForm_site 的标签组是
【平台全量站点】，不是该店已开通的集合，而且命名与认领弹窗不一致：
筛选区叫「澳洲站」「欧盟站」「日本站」，弹窗叫「澳大利亚」「新西兰」「欧盟站（27站）」。
拿筛选区的名字去认领弹窗里找，_select_store_and_site 会直接抛「未找到站点」。

【站点里没有「全球」】这是 Temu 卖家后台与店小秘的差异：Temu 后台顶栏那个
「全球/美国/欧区」是**域名级的区域**（见 app/temu_region.py），而店小秘认领弹窗
列的是**具体国家站点**，其中没有「全球」这一项，弹窗默认自动勾「美国」。
所以站点没有可用的默认值，UI 必须强制用户选。

缓存取向与 cache.py 一致：只缓存「平台给的客观事实」，坏了、缺了当未命中重探，
全程 best-effort 吞异常——枚举失败只该让下拉空着并提示，绝不能让页面打不开。
"""
import asyncio
import json
import os
import threading
import time

from app.config import config
from app.logger import logger
from app.publish.browser import (
    BrowserSession,
    CRAWL_URL,
    ensure_cdp_alive,
    fill_js,
)

# 缓存根照 cache.py 的取向：只定一个根常量，子路径由函数拼，便于单测 monkeypatch。
CACHE_DIR = str(config.workspace_root / "publish-cache")
_SITES_NAME = "shop-sites.json"

# Temu 店在 shopMap 里的 platform 值。平台没换过这个历史代号，写死并在筛不到时
# 退回「不筛平台」——否则店小秘哪天改了代号，下拉会直接空掉且看不出原因。
TEMU_PLATFORM = "pddkj"

_lock = threading.Lock()

# 枚举与发布作业抢的是同一个 CDP 页面（发布是 12 阶段共用一个 page）。这把锁保证
# 同一时刻只有一次站点探测在开弹窗，避免两次探测互相把对方的弹窗关掉。
_probe_lock = asyncio.Lock()


def _sites_path() -> str:
    return os.path.join(CACHE_DIR, _SITES_NAME)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---- 站点缓存（磁盘）---------------------------------------------------------

def load_sites_cache() -> dict:
    """读整份「店铺 → 站点列表」缓存；缺失/损坏一律返回空 dict（当未命中）。"""
    try:
        with open(_sites_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        shops = data.get("shops")
        return shops if isinstance(shops, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"站点缓存损坏，当未命中：{e}")
        return {}


def save_sites_cache(store: str, sites: list, default: str = "") -> None:
    """把某店铺的站点列表写进缓存（临时文件 + os.replace 原子替换）。

    只覆盖该店铺那一条、不重写整份：换店铺探测时不该把别的店已探好的结果清零。
    写失败只告警——缓存没了只是下次多花几秒重探。
    """
    if not store or not sites:
        return
    try:
        with _lock:
            path = _sites_path()
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            shops = data.get("shops")
            if not isinstance(shops, dict):
                shops = {}
            shops[store] = {"sites": list(sites), "default": default or "",
                            "updated_at": _now()}
            data["shops"] = shops
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"站点缓存写入失败（忽略）：{e}")


def clear_sites_cache(store: str = "") -> list:
    """清站点缓存：给了 store 只清那一个店，否则整份删掉。返回被清掉的店铺名。"""
    removed = []
    try:
        with _lock:
            path = _sites_path()
            if not store:
                if os.path.exists(path):
                    removed = list(load_sites_cache().keys())
                    os.remove(path)
                return removed
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                return removed
            shops = (data or {}).get("shops") or {}
            if store in shops:
                shops.pop(store)
                removed.append(store)
                data["shops"] = shops
                tmp = f"{path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"清站点缓存失败（忽略）：{e}")
    return removed


# ---- 店铺枚举（走 userIn.json，秒级、无副作用）--------------------------------
# 在页面内 fetch 而不是用 requests：登录态是 Chrome 的 cookie，进程外拿不到。
_JS_SHOPS = r"""(async () => {
  try {
    const r = await fetch('/api/userIn.json', {credentials: 'include'});
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const j = await r.json();
    const m = (j.data && j.data.shopMap) || {};
    const shops = Object.keys(m).map(k => {
      const s = m[k] || {};
      return {id: String(s.id != null ? s.id : k), name: (s.name || '').trim(),
              platform: s.platform || '', isDel: s.isDel, isExpire: s.isExpire,
              orderIndex: s.orderIndex};
    }).filter(s => s.name);
    return JSON.stringify({ok: true, shops: shops, account: (j.data || {}).account || ''});
  } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 200)}); }
})()"""


async def list_stores(session: BrowserSession,
                      platform: str = TEMU_PLATFORM) -> list:
    """枚举当前登录账号下的店铺。

    返回 [{"name", "id", "expired": bool}]，按平台筛（默认只留 Temu 店）并去掉
    已删除的。**平台筛不到任何店时退回不筛**——店小秘若改了 platform 代号，
    宁可多列几个非 Temu 店让用户自己认，也不要给一个空下拉。

    已过期的店（isExpire=1，授权到期）照样列出来但打标记：认领弹窗里它仍然在，
    只是认领会失败；把它藏掉反而让用户以为店铺丢了。
    """
    if "dianxiaomi.com" not in (session.page.url or ""):
        await session.navigate(CRAWL_URL)
    data = await session.eval_json(_JS_SHOPS)
    if not data.get("ok"):
        raise RuntimeError(
            f"读取店小秘账号信息失败（可能未登录）：{data.get('status') or data.get('err')}"
        )
    raw = [s for s in (data.get("shops") or []) if not s.get("isDel")]
    hit = [s for s in raw if (s.get("platform") or "") == platform] if platform else raw
    if platform and not hit:
        logger.warning(
            f"shopMap 里没有 platform={platform} 的店铺（平台可能改了代号），"
            f"退回列出全部 {len(raw)} 个店铺"
        )
        hit = raw
    hit.sort(key=lambda s: (s.get("orderIndex") or 0, s.get("name") or ""))
    return [{"name": s["name"], "id": s.get("id") or "",
             "expired": bool(s.get("isExpire"))} for s in hit]


# ---- 站点枚举（必须开认领弹窗）-----------------------------------------------
# 弹窗识别与店铺/站点选择器与 claim.py 同源（同一个弹窗）。这里刻意重抄一份精简版
# 而不 import claim 的私有 JS：claim 那几段带「取消其它站点」「点确定」的语义，
# 复用会把写动作的代码路径引进只读枚举里。
_JS_HEAD = r"""(() => {
  const _pick = (kw) => Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => m.offsetParent !== null && (m.textContent||'').includes(kw))[0];
"""

MODAL_STORE = "选择店铺"

# 采集列表任意一行的「认领」都能开出同一个弹窗（弹窗只列店铺和站点，与具体商品
# 无关），故取第一个可见的即可。offsetParent 判可见是为了排掉非当前 tab 里同名的
# 隐藏入口（采集页有「链接采集」「店铺采集」两个 tab，非当前 tab 的 DOM 仍在）。
_JS_COUNT_CLAIM = r"""(() => {
  const n = Array.from(document.querySelectorAll('a, button'))
    .filter(a => (a.textContent||'').trim() === '认领' && a.offsetParent !== null).length;
  return JSON.stringify({ready: n > 0, count: n});
})()"""

_JS_OPEN_MODAL = r"""(() => {
  const links = Array.from(document.querySelectorAll('a, button'))
    .filter(a => (a.textContent||'').trim() === '认领' && a.offsetParent !== null);
  if (!links.length) return JSON.stringify({clicked: false, reason: 'no-claim-link'});
  links[0].click();
  return JSON.stringify({clicked: true, count: links.length});
})()"""

_JS_WAIT_STORES = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({ready: false, reason: 'no-modal'});
  const names = Array.from(modal.querySelectorAll('.shop-label-box'))
    .map(b => (b.textContent||'').trim()).filter(t => t);
  return JSON.stringify({ready: names.length > 0, stores: names});
})()"""

# 打标前先清历史标记：同一会话连着探两个店铺时 DOM 上会留着上一轮的
# data-wb-target，选择器按 DOM 顺序取到的是上一个（claim.py 踩过同一个坑）。
_JS_TAG_STORE = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-wb-target="wb-probe"]').forEach(
    e => e.removeAttribute('data-wb-target'));
  const boxes = Array.from(modal.querySelectorAll('.shop-label-box'));
  const target = boxes.find(b => (b.textContent||'').trim() === __STORE__);
  if (!target) return JSON.stringify({found: false,
                                      available: boxes.map(b => (b.textContent||'').trim())});
  const label = target.querySelector('label');
  if (!label) return JSON.stringify({found: false, reason: 'no-label'});
  label.setAttribute('data-wb-target', 'wb-probe');
  label.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true,
                         checked: label.className.includes('ant-checkbox-wrapper-checked')});
})()"""

# 站点区 = 弹窗内除「店铺项」（.shop-label-box）和「底部按钮区」（.ant-modal-footer）
# 之外的复选框。这个筛法与 claim.py 的 _JS_WAIT_SITES 一致，改一处要改两处。
# 「全选」也落在这一区里，读出来后由 Python 侧剔掉——写进 JS 正则的话，这个文案
# 一变就静默漏一项，放 Python 侧集中处理更好查。
_JS_READ_SITES = _JS_HEAD + r"""
  const modal = _pick(__MODAL__);
  if (!modal) return JSON.stringify({ready: false, reason: 'no-modal'});
  const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'));
  const sites = labels.map(l => ({
    text: (l.textContent||'').trim(),
    checked: l.className.includes('ant-checkbox-wrapper-checked'),
  })).filter(x => x.text);
  return JSON.stringify({ready: sites.length > 0, sites: sites});
})()"""

# 关弹窗：只用「取消」/「关闭」/右上角 X，代码里绝不出现「确定」——枚举不落库。
# 遍历所有可见弹窗是为了连带清掉上一次异常留下的幽灵弹窗（否则它的遮罩会挡住
# 后续发布作业的一切点击，claim.py 的 kill_stuck_modals 治的就是这个后果）。
_JS_CLOSE_MODALS = r"""(() => {
  let n = 0;
  Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => m.offsetParent !== null)
    .forEach(m => {
      const root = m.closest('.ant-modal-root') || m;
      const btn = Array.from(m.querySelectorAll('button, a'))
        .find(b => /^(取消|关闭)$/.test((b.textContent||'').trim()));
      if (btn) { btn.click(); n++; return; }
      const x = root.querySelector('.ant-modal-close, [aria-label="close"]');
      if (x) { x.click(); n++; }
    });
  return JSON.stringify({closed: n});
})()"""

# 「全选」不是站点。单列在这里而不是写进 JS 正则，理由见 _JS_READ_SITES 的注释。
_NOT_A_SITE = {"全选", "全部"}

# 站点区渲染完的判据：连续几轮读到同一个数量。见 list_sites 里那段注释的实测取证。
_STABLE_ROUNDS = 2
_STABLE_INTERVAL = 1.0


async def list_sites(session: BrowserSession, store: str,
                     timeout: float = 180) -> dict:
    """探测某店铺在认领弹窗里可选的站点列表（只读，读完立刻关弹窗）。

    返回 {"store", "sites": [站点名], "default": 弹窗默认已勾的站点或 ""}。
    默认勾选一并带回来是给 UI 做「推荐项」用：店小秘通常自动勾「美国」，而
    _select_store_and_site 会把非目标的默认勾选取消掉，两边对齐才不会让用户
    以为自己选的被忽略了。

    【为什么给到 3 分钟超时】店铺列表接口偶发要 2~3 分钟（claim.py 同一结论）。
    枚举侧不像认领那样能靠重跑续，超时就只能报错让用户重试，故给足时间。
    """
    if not store:
        raise ValueError("store 不能为空")

    r = await session.navigate(CRAWL_URL)
    if not r.get("ok"):
        raise RuntimeError(f"导航到数据采集页失败：{r}")

    try:
        # 采集列表由 Vue 异步渲染，导航完成时「认领」入口常还没挂上，轮询到出现为止。
        ready = await session.wait_for(_JS_COUNT_CLAIM, lambda d: d.get("ready"),
                                       timeout=40, interval=2)
        if not ready.get("ready"):
            raise RuntimeError("采集列表里没有可用的「认领」入口（列表为空或未登录）")

        opened = await session.eval_json(_JS_OPEN_MODAL)
        if not opened.get("clicked"):
            raise RuntimeError(f"打开认领弹窗失败：{opened}")

        stores = await session.wait_for(
            fill_js(_JS_WAIT_STORES, MODAL=MODAL_STORE),
            lambda d: d.get("ready"), timeout=timeout, interval=2)
        if not stores.get("ready"):
            raise RuntimeError("店铺列表加载超时：店小秘接口可能异常，稍后重试")

        tag = await session.eval_json(
            fill_js(_JS_TAG_STORE, MODAL=MODAL_STORE, STORE=store))
        if not tag.get("found"):
            avail = tag.get("available") or stores.get("stores") or []
            raise RuntimeError(f"认领弹窗里没有店铺「{store}」，可选：{avail}")

        # 站点区只在勾中店铺后才渲染，这一下真实点击是拿到站点的唯一途径。
        # 必须真实鼠标点击：ant-design Checkbox + Vue v-model 不认 JS .click()
        # （claim.py 的 _trusted_toggle 同一结论）。这里只勾店铺、不勾站点，
        # 也不点确定，故不产生任何认领。
        if not tag.get("checked"):
            click = await session.mouse_click('label[data-wb-target="wb-probe"]')
            if not click.get("ok"):
                raise RuntimeError(f"勾选店铺「{store}」失败：{click.get('err')}")

        # 【必须等数量稳定，不能见到第一项就收】站点区是逐步渲染的：2026-08-24 实测
        # 「有任意一项就返回」只读到 1 个（欧盟站），而弹窗实际有 42 个。故连续
        # _STABLE_ROUNDS 轮读到同一个数量才认它渲染完。
        sites_js = fill_js(_JS_READ_SITES, MODAL=MODAL_STORE)
        data, last, stable = {}, -1, 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            data = await session.eval_json(sites_js)
            n = len(data.get("sites") or [])
            if n and n == last:
                stable += 1
                if stable >= _STABLE_ROUNDS:
                    break
            else:
                stable = 0
            last = n
            await asyncio.sleep(_STABLE_INTERVAL)
        if not data.get("ready"):
            raise RuntimeError(f"站点列表加载超时（店铺「{store}」），稍后重试")

        raw = [s for s in (data.get("sites") or [])
               if s.get("text") not in _NOT_A_SITE]
        sites = [s["text"] for s in raw]
        default = next((s["text"] for s in raw if s.get("checked")), "")
        logger.info(
            f"店铺「{store}」可选站点 {len(sites)} 个，弹窗默认勾选：{default or '无'}"
        )
        return {"store": store, "sites": sites, "default": default}
    finally:
        # 关弹窗是 best-effort：关不掉只告警，不能让「已读到站点」这个结果丢掉。
        # 但真关不掉会留下遮罩挡住后续发布作业，故日志级别给到 warning。
        try:
            closed = await session.eval_json(_JS_CLOSE_MODALS)
            if not closed.get("closed"):
                logger.warning("认领弹窗未能关闭（后续操作可能被遮罩挡住）")
        except Exception as e:
            logger.warning(f"关闭认领弹窗失败（忽略）：{e}")


# ---- 对外入口（给 app.py 的两个接口用）---------------------------------------

_CDP_HINT = ("连不上调试 Chrome（9222）。请确认已登录店小秘的 Chrome "
             "带 --remote-debugging-port=9222 运行。")


async def fetch_stores() -> dict:
    """给 /publish/stores 用：连一次 CDP 列店铺，用完即关会话。

    每次新建会话而不像 service 那样长持有：枚举是一次性的秒级操作，长持有反而
    会与发布作业争同一个页面。
    """
    if not await ensure_cdp_alive():
        raise RuntimeError(_CDP_HINT)
    session = BrowserSession()
    await session.open()
    try:
        stores = await list_stores(session)
    finally:
        await session.close()
    return {"stores": stores}


async def fetch_sites(store: str, refresh: bool = False) -> dict:
    """给 /publish/sites 用：优先读缓存，未命中（或 refresh）才开弹窗探测。

    返回 {"store", "sites", "default", "cached": bool, "updated_at": str}。
    """
    if not store:
        raise ValueError("store 不能为空")
    if not refresh:
        entry = load_sites_cache().get(store) or {}
        sites = entry.get("sites") or []
        if sites:
            return {"store": store, "sites": sites,
                    "default": entry.get("default") or "",
                    "cached": True, "updated_at": entry.get("updated_at") or ""}

    if not await ensure_cdp_alive():
        raise RuntimeError(_CDP_HINT)
    # 探测要独占那个 CDP 页面，故整段串行：并发探两个店铺会互相关掉对方的弹窗。
    async with _probe_lock:
        session = BrowserSession()
        await session.open()
        try:
            got = await list_sites(session, store)
        finally:
            await session.close()
    save_sites_cache(store, got["sites"], got.get("default") or "")
    return {"store": store, "sites": got["sites"],
            "default": got.get("default") or "",
            "cached": False, "updated_at": _now()}
