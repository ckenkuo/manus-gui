"""活动管理管线：读取活动、确定性算价、提交报名和切换流量。

为什么是确定性管道而非 agent 自由循环：本质是「for 每个 SPU：几步确定性 DOM/接口只读 +
纯本地算价筛选」，不需要 function-calling/LangGraph。浏览器操作使用 Playwright over CDP。
判定完全确定性，不再用 LLM 选活动（judge_activity 保留但主流程未接入）。

申报价(日常价×活动折扣率)的入围判定按 SPU 级门槛：活动折扣率 ≥ 毛利率最低货号的
「底价÷日常价」（见 service.plan_spu_activities）。
商品实时采购，库存不参与初筛。详情页仍由平台判定报名资格。

最高优先级安全约束（真实商家账号、操作不可逆）：
- service 的 dry-run 分支只读和规划，变更函数各自通过 allow 参数控制最终动作。
- 报名记录仅已标定的有效状态可判成功，场次失败只作提示；完整对账由 service 编排。

页面事实（2026-07 实测 agentseller.temu.com，已写进选择器常量/解析逻辑）：
- 流量页 main/flux-analysis：每商品一行，行文本内含 `SPU ID： {数字}`（全角冒号 + 空格）；
  行内操作是 <a>：`立即开启`(加速器待开启入口)/`查看详情`/`去补货`/`去投放推广`。
- 活动页 activity/marketing-activity：每行右侧一个 <a>报名；实测有近百个活动（远超早期假设的
  4 个固定活动），含大量带日期的限时/满减/秒杀活动、且上下半区有重复。折扣率解析优先取
  「≤ X折」条件、并把「85折」类两位整数折扣二次归一（详见 _parse_activity）；同名活动按名去重。
- 库存不在 DOM，在商品列表接口 skc/pageQuery（成本表 SPU ID == 接口 productId），见 read_stock。
"""
import asyncio
import re
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any, Optional

# 复用采集管道已实测的 JSON 解析（剥 ```json 围栏 + 兜底抓首个 {...}），避免重复实现。
from app.collect.pipeline import _parse_json
from app.llm import LLM
from app.logger import logger
from app.schema import Message

# ---- 页面路径（域名在运行时按当前区域拼，见下）--------------------------------
# 【为什么只留路径、不留完整 URL】区域切换（顶栏「全球 / 美国 / 欧区」）换的是【域名】：
# 全球 agentseller.temu.com、美国 agentseller-us.temu.com（2026-08-07 实测）。本管线自己
# 新开页面干活，若拿写死的全球域 URL 去 goto，就会把操作者选定的美国区悄悄换回全球区，
# 采到/改到的都不是他要的那批数据。故域名必须运行时取（app/temu_region.url_in_region）。
FLUX_PATH = "/main/flux-analysis"
ACTIVITY_PATH = "/activity/marketing-activity"
ACTIVITY_LOG_PATH = "/activity/marketing-activity/log"
GOODS_LIST_PATH = "/goods/list"
# 库存查询接口关键字（库存不在 DOM，在此接口响应里，见 read_stock）。
_STOCK_API_KEY = "skc/pageQuery"

# 登录态失效标志：Temu SPA 会把所有页签陆续 302 到 /auth/authentication（或安全验证页
# bgn_verification）。2026-09-30 实测：批次执行中登录被踢，各等待循环认不出登录页，在
# 登录页上空等超时甚至卡死，批次表现为「点了提交后直接跳转了然后没下文」。
_LOGIN_REDIRECT_KEYS = ("/auth/authentication", "bgn_verification")


def kicked_to_login(url) -> bool:
    """URL 是否已被平台踢回登录/安全验证页（登录态失效）。

    等待循环每轮先查它：一旦被踢，后续任何等待都是空等，必须立刻带明确原因退出，
    让批次 fail-fast 而不是挂到超时。尤其 enroll_activity 的结果行等待——登录页上
    查不到行会被误判成「详情页 0 行 = 无资格」（info 语义不重试），那是错上加错。
    """
    url = str(url or "")
    return any(key in url for key in _LOGIN_REDIRECT_KEYS)

# ---- 已探测确认的活动主题名（用于从行文本里认出是哪个活动）--------------------
_KNOWN_ACTIVITIES = ["清仓甩卖", "限时秒杀", "官方大促", "额外降价超级万人团"]

# 流量页行内操作 <a> 文案：既用于把「含 SPU ID 的小节点」向上归并到真正的商品行容器，
# 也用于判断加速态（见 _classify_accel）。
_FLUX_ACTION_WORDS = ["立即开启", "查看详情", "去补货", "去投放推广"]
_FLUX_LIST_API_KEY = "/api/flow/analysis/list"


_CLOSE_SITE_NOTIFICATION_JS = r"""
() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const norm = value => (value || '').replace(/\s+/g, '');
  const heading = [...document.querySelectorAll('div,span,h1,h2,h3')].find(element =>
    visible(element) && norm(element.innerText || element.textContent).startsWith('全部消息')
  );
  if (!heading) return false;
  let panel = heading;
  for (let depth = 0; depth < 10 && panel.parentElement; depth += 1) {
    const rect = panel.getBoundingClientRect();
    const text = norm(panel.innerText || panel.textContent);
    if (rect.width >= 260 && rect.height >= 100 && text.includes('全部消息')) break;
    panel = panel.parentElement;
  }
  if (!panel || !visible(panel)) return false;
  const panelRect = panel.getBoundingClientRect();
  const trigger = element => {
    if (typeof element.click === 'function') element.click();
    else element.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  };
  const exactClose = panel.querySelector('[data-testid="beast-core-icon-close"]');
  if (exactClose && visible(exactClose)) {
    trigger(exactClose);
    return true;
  }
  const controls = [...panel.querySelectorAll('button,a,[role="button"],[role="img"],svg')]
    .filter(visible);
  let close = controls.find(element => {
    const label = `${element.getAttribute('aria-label') || ''} ${element.title || ''}`;
    const icon = element.querySelector(
      '[aria-label="close"],[aria-label="关闭"],svg[data-icon="close"],svg[data-icon="close-circle"]'
    );
    return /关闭|close/i.test(label) || Boolean(icon);
  });
  if (!close) {
    close = controls.find(element => {
      const rect = element.getBoundingClientRect();
      const text = norm(element.innerText || element.textContent);
      return !text && rect.width <= 50 && rect.height <= 50
        && rect.right >= panelRect.right - 60 && rect.top <= panelRect.top + 75;
    });
  }
  if (!close) return false;
  trigger(close);
  return true;
}
"""


async def dismiss_site_notification_panel(page) -> bool:
    """关闭站点右上角“全部消息”通知面板，不触碰业务确认弹窗。"""
    if page is None:
        return False
    try:
        closed = await page.evaluate(_CLOSE_SITE_NOTIFICATION_JS)
        if closed:
            await asyncio.sleep(0.3)
        return bool(closed)
    except Exception as exc:
        logger.warning(f"关闭站点「全部消息」面板失败（忽略）：{exc}")
        return False


_CLOSE_ALL_POPUPS_JS = r"""() => {
  const norm = value => (value || '').replace(/\s+/g, '');
  const candidates = [...document.querySelectorAll('button,a,[role=button]')];
  const button = candidates.find(element => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      norm(element.innerText || element.textContent).startsWith('关闭所有弹窗');
  });
  if (!button) return false;
  button.click();
  return true;
}"""


async def click_assistant_close_all_popups(page) -> bool:
    """只点商家助手插件注入的「关闭所有弹窗」按钮，返回是否点到。

    插件未装 / 按钮未注入 / 页面未就绪都返回 False，不影响原流程（best-effort）。

    单独抽出来是给【逐页循环】用的（订单采集 sweep_pages）：dismiss_all_page_popups 里
    的站点通知面板重试在面板没开时是纯等待（最多 5×0.5s），每页都做会累出分钟级开销；
    而这个按钮是插件注入的、一次 evaluate 就有结论，适合高频探测。
    """
    if page is None:
        return False
    try:
        clicked = await page.evaluate(_CLOSE_ALL_POPUPS_JS)
    except Exception as exc:
        logger.warning(f"点击商家助手「关闭所有弹窗」失败（忽略）：{exc}")
        return False
    if clicked:
        await asyncio.sleep(0.5)
    return bool(clicked)


async def dismiss_all_page_popups(page) -> bool:
    """在业务动作开始前关闭站点通知面板和商家助手弹窗。

    只在阶段入口调用；业务确认框已经打开或正在等待结果 toast 时不得调用，以免清掉权威结果。
    插件未安装、按钮未注入或页面尚未就绪时返回 False，不影响原流程。
    """
    if page is None:
        return False
    try:
        site_closed = False
        for attempt in range(5):
            site_closed = await dismiss_site_notification_panel(page) or site_closed
            if site_closed:
                break
            if attempt < 4:
                await asyncio.sleep(0.5)
        clicked = await click_assistant_close_all_popups(page)
        return bool(site_closed or clicked)
    except Exception as exc:
        logger.warning(f"点击商家助手「关闭所有弹窗」失败（忽略）：{exc}")
        return False

# 定位流量页某 SPU 所在行：行文本内含 `SPU ID： {spu}`（全角冒号+空格，但兼容半角/无空格）。
# 策略：先找【最小】的、其文本里「SPU ID」后紧跟的数字恰等于目标 spu 的元素（避免包含关系
# 误判，如 90728 命中 907288），再向上归并到含行内操作 <a> 的行容器，取其可读行文本。
_LOCATE_ROW_JS = r"""
(spu) => {
  const target = String(spu);
  const actionWords = %s;
  const all = [...document.querySelectorAll('div,tr,li,section,article,td')];
  let bestEl = null, bestLen = Infinity;
  for (const el of all) {
    const t = el.innerText || '';
    if (t.indexOf('SPU ID') < 0) continue;
    const m = t.match(/SPU\s*ID[：:\s]*([0-9]+)/);
    if (!m || m[1] !== target) continue;      // SPU ID 后的数字须精确相等
    if (t.length < bestLen) { bestLen = t.length; bestEl = el; }
  }
  if (!bestEl) return null;
  // 权威状态只读 SPU 所在“商品信息”单元格，不能从更大的父容器找“流量加速中”：
  // 父容器可能同时包含相邻商品行，曾把下一行的开启标签串给当前 SPU。
  const productCell = bestEl.closest('td') || bestEl;
  const row = productCell.closest('tr') || productCell.parentElement || productCell;
  const productInfoText = (productCell.innerText || '').replace(/\s+/g, ' ').trim();
  const rowText = (row.innerText || '').replace(/\s+/g, ' ').trim();
  return {
    found: true,
    product_info_text: productInfoText.slice(0, 600),
    row_text: rowText.slice(0, 1200),
  };
}
""" % ("[" + ",".join('"%s"' % w for w in _FLUX_ACTION_WORDS) + "]")


_MARK_FLUX_SPU_SEARCH_JS = r"""
() => {
  document.querySelectorAll('[data-kiro-flux-spu-search]').forEach(
    element => element.removeAttribute('data-kiro-flux-spu-search')
  );
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const field = [...document.querySelectorAll('input')].find(input =>
    visible(input) && /多个查询|空格|逗号/.test(input.placeholder || '')
  );
  if (!field) return false;
  field.setAttribute('data-kiro-flux-spu-search', '1');
  return true;
}
"""

# 活动页读 4 个活动行：以文案恰为「报名」的 <a>/button 为锚，向上归并到含「折/库存/个」等
# 条件描述的行容器，取其行文本；Python 侧再解析折扣率/库存门槛/活动名（见 _parse_activity）。
# 切到「直降促销活动」tab（用户规则 2026-07-17：只报无折上折的直降活动，排除「专属补贴活动」
# tab 里的叠加活动）。tab 是 own-text 恰为「直降促销活动」的节点，祖先带 tabItem/TAB_reunit 类。
_CLICK_ZHIJIANG_TAB_JS = r"""
() => {
  for (const el of document.querySelectorAll('div,span')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').replace(/\s+/g, '');
    if (own === '直降促销活动') {
      let n = el;
      for (let i = 0; i < 6 && n; i++) {
        const c = (n.className || '').toString();
        if (c.includes('tabItem') || c.includes('TAB_reunit')) { n.click(); return true; }
        n = n.parentElement;
      }
      el.click();
      return true;
    }
  }
  return false;
}
"""

# 只读「直降促销活动」tab 的 <table> 行（实测该 tab 用 tr 表格，专属补贴 tab 用卡片，故读 tr
# 天然只拿直降活动、不混叠加类）。每行须含「报名」按钮 + 折扣/库存/提报字样。
_ACTIVITY_ROWS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  for (const tr of document.querySelectorAll('tr')) {
    const hasSignup = [...tr.querySelectorAll('a, button, [role="button"]')]
      .some(e => (e.textContent || '').replace(/\s+/g, '').includes('报名'));
    if (!hasSignup) continue;
    const text = (tr.innerText || '').replace(/\s+/g, ' ').trim();
    if (!text || seen.has(text)) continue;
    if (text.indexOf('折') < 0 && text.indexOf('库存') < 0 && text.indexOf('个') < 0
        && text.indexOf('提报') < 0) continue;
    seen.add(text);
    out.push(text.slice(0, 400));
  }
  return out;
}
"""


def _to_number(v: Any) -> Optional[float]:
    """从可能带货币符（¥）/千分位的价格串解析出数字；失败返回 None。

    与 collect/pipeline._to_number 同口径：Excel/页面读来的价常是「46.10¥」「1,299.00」
    这类字符串，需剥成纯数字再参与红线运算。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group(0)) if m else None


def _classify_accel(product_info_text: str) -> str:
    """按商品信息单元格判流量状态：有「流量加速中」为 on，否则为 off。"""
    return "on" if "流量加速中" in (product_info_text or "") else "off"


async def locate_product_row(page, spu: str) -> Optional[dict]:
    """只读：在流量页定位某 SPU 所在行，返回 {found, accel_state, row_text}；找不到返回 None。

    页面事实：每商品一行、行文本含 `SPU ID： {spu}`（全角冒号+空格）。纯 evaluate 只读，
    不点任何按钮。异常只告警、返回 None（best-effort，不阻断整批）。
    """
    try:
        info = await page.evaluate(_LOCATE_ROW_JS, str(spu))
    except Exception as e:
        logger.warning(f"locate_product_row：evaluate 异常 SPU={spu}：{e}")
        return None
    if not info:
        return None
    product_info_text = info.get("product_info_text", "")
    return {
        "found": True,
        "accel_state": _classify_accel(product_info_text),
        "product_info_text": product_info_text,
        "row_text": info.get("row_text", ""),
    }


async def _search_flux_product(page, spu: str) -> dict:
    """在流量页按 SPU 精确查询，等待列表接口返回后读取唯一目标商品行。"""
    await page.bring_to_front()
    if not await page.evaluate(_MARK_FLUX_SPU_SEARCH_JS):
        return {"found": False, "state": "unknown", "note": "未找到流量页 SPU 搜索框"}
    field = page.locator('[data-kiro-flux-spu-search="1"]')
    await field.fill(str(spu))
    entered = (await field.input_value()).strip()
    if entered != str(spu):
        return {
            "found": False, "state": "unknown",
            "note": f"流量页 SPU 输入校验失败（实际={entered or '空'}）",
        }
    logger.info(f"[流量] 已输入 SPU={spu}，准备查询当前加速状态")
    await asyncio.sleep(0.5)
    query_button = page.get_by_role("button", name="查询", exact=True).first
    try:
        async with page.expect_response(
            lambda response: _FLUX_LIST_API_KEY in response.url,
            timeout=15000,
        ) as response_info:
            await query_button.click(timeout=5000)
        response = await response_info.value
        payload = await response.json()
        result = payload.get("result") or {}
        total = int(result.get("total") or 0)
        page_items = result.get("pageItems") or []
        exact_items = [
            item for item in page_items if str(item.get("productId") or "") == str(spu)
        ]
    except Exception as exc:
        return {
            "found": False, "state": "unknown",
            "note": f"流量页按 SPU 查询异常：{str(exc)[:120]}",
        }
    info = None
    for _ in range(20):
        info = await locate_product_row(page, str(spu))
        if info:
            break
        await asyncio.sleep(0.25)
    if total != 1 or len(exact_items) != 1 or not info:
        return {
            "found": False, "state": "unknown", "total": total,
            "note": (
                f"流量页 SPU 查询结果异常（total={total}，接口精确项={len(exact_items)}，"
                f"目标行={'有' if info else '无'}）"
            ),
        }
    state = info["accel_state"]
    api_flow_status = exact_items[0].get("flowGrowStatus")
    logger.info(
        f"[流量] SPU={spu} 查询完成：total=1，接口 productId 已核对，"
        f"flowGrowStatus={api_flow_status}，商品信息列状态={state}，"
        f"是否含流量加速中={'是' if state == 'on' else '否'}"
    )
    return {
        "found": True, "state": state, "total": total,
        "api_flow_status": api_flow_status, "note": "按 SPU 查询成功", **info,
    }


async def read_accel_state(page, spu: str, search: bool = False) -> str:
    """只读：返回某 SPU 的流量加速器状态 "on"|"off"|"unknown"。

    search=True 时先使用页面搜索框精确查询并等待列表接口；否则只读当前 DOM。
    """
    if search:
        try:
            result = await _search_flux_product(page, str(spu))
            return result.get("state", "unknown")
        except Exception as exc:
            logger.warning(f"流量页按 SPU 查询异常 SPU={spu}：{exc}")
            return "unknown"
    info = await locate_product_row(page, spu)
    if not info:
        return "unknown"
    return info.get("accel_state", "unknown")


def _parse_activity(raw: str) -> dict:
    """把活动页一行原文解析成 {name, discount_rate, min_stock, registered_count, raw}。

    - name：优先匹配已知活动名，否则取行首短文本兜底。
    - discount_rate：`7折→0.7`、`8.5折→0.85`、`9折→0.9`；含「详见商品提报列表」或无「折」→ None。
    - min_stock：`≥ 10个` / `10个` / `库存≥30` 里取数字，取不到 → None。
    - registered_count：「已报名」列的 `-`/数字；`-` 归零，取不到 → None。
    """
    text = raw or ""
    name = next((n for n in _KNOWN_ACTIVITIES if n in text), "")
    if not name:
        # 取完整活动标题：截到 New/长期有效/日期/查看站点时间/折扣条件/详见提报 之前。
        # 旧实现 text[:12] 会把长标题（如「【满减优惠-盛夏大促专题C】-抢占全站爆发」）截断，
        # 影响阶段3 报名时按名定位活动，故改为按这些锚点切出完整名。
        head = re.split(
            r"\s*(?:New\b|长期有效|查看站点时间|详见商品提报列表|\d{4}-\d{2}-\d{2}|[≤<]=?\s*\d)",
            text,
            maxsplit=1,
        )[0].strip()
        name = head or text[:12].strip()

    discount_rate = None
    if "详见商品提报列表" not in text:
        # 实机发现（2026-07-16 Leoaqr全球）：活动名里常自带描述性「85折/75折」等数字，
        # 若直接抓文本第一个「数字折」会误命中活动名而非真正的折扣条件上限。故优先取
        # 「≤/< X折」这类条件里的折扣，取不到再兜底抓首个「数字折」（下半区无≤条件的
        # 「日常8折」类活动靠兜底）。
        m = re.search(r"[≤<]=?\s*(\d+(?:\.\d+)?)\s*折", text) or re.search(
            r"(\d+(?:\.\d+)?)\s*折", text
        )
        if m:
            try:
                rate = float(m.group(1)) / 10.0
                # 中文折扣两种写法都归一到 0~1：「8.5折」=0.85（/10 即得），
                # 「85折」=八五折=0.85（/10 得 8.5 >1，需再 /10）。
                if rate > 1:
                    rate = rate / 10.0
                discount_rate = round(rate, 4)
            except ValueError:
                discount_rate = None

    min_stock = None
    m = re.search(r"[≥>=]{1,2}\s*(\d+)", text) or re.search(r"(\d+)\s*个", text)
    if m:
        try:
            min_stock = int(m.group(1))
        except ValueError:
            min_stock = None

    registered_count = None
    registered_display = None
    registered_match = re.search(r"(?:^|\s)(-|\d+)\s*报名(?:\s|\d|$)", text)
    if registered_match:
        registered_display = registered_match.group(1)
        registered_count = 0 if registered_display == "-" else int(registered_display)

    return {
        "name": name, "discount_rate": discount_rate, "min_stock": min_stock,
        "registered_count": registered_count, "registered_display": registered_display,
        "raw": text,
    }


async def read_activities(page) -> list[dict]:
    """只读：读活动页全部活动的 {name, discount_rate, min_stock, raw} 列表。

    实机（2026-07-16）发现活动页有近百个活动（远超 plan 早期假设的 4 个），且抓到的行里
    有重复项和 raw=='报名' 的空归并行。故这里在解析后做两件清理：
    - 过滤垃圾行（name 为空或恰为「报名」）；
    - 按 (name, discount_rate, min_stock) 去重（同活动在页面上/下半区重复出现）。
    纯 evaluate 抓每个「报名」<a> 所在行原文再本地解析（见 _parse_activity）。异常只告警、
    返回已拿到的部分（best-effort）；一条都没抓到返回空列表。
    """
    await dismiss_all_page_popups(page)

    # 先切到「直降促销活动」tab（只报无折上折活动的用户规则）。切换失败不阻断，但告警——
    # 因为若停在「专属补贴活动」tab，读到的会是叠加类活动，违反规则。
    # 实测教训（2026-07-20）：单发「点一次 tab + 固定 sleep」不稳——页面未就绪/tab 未切成时
    # 抓到 0 行，误判 skip_nomatch。故改为重试循环：点 tab → 轮询等活动列表渲染
    # （body 出现「符合条件的活动数量」）→ 抓行；抓到非空即停，最多 3 轮。
    rows = []
    for attempt in range(3):
        try:
            switched = await page.evaluate(_CLICK_ZHIJIANG_TAB_JS)
            if not switched:
                logger.warning(f"read_activities：未找到「直降促销活动」tab（第{attempt+1}次）")
        except Exception as e:
            logger.warning(f"read_activities：切直降 tab 异常（第{attempt+1}次）：{e}")
        # 轮询等活动列表渲染（最多 ~8s）
        for _ in range(16):
            await asyncio.sleep(0.5)
            try:
                if await page.evaluate("() => (document.body.innerText||'').includes('符合条件的活动数量')"):
                    break
            except Exception:
                pass
        try:
            rows = await page.evaluate(_ACTIVITY_ROWS_JS)
        except Exception as e:
            logger.warning(f"read_activities：evaluate 异常（第{attempt+1}次）：{e}")
            rows = []
        if rows:
            break
        logger.warning(f"read_activities：第{attempt+1}次读到 0 行，重试切 tab")
    if not rows:
        logger.warning("read_activities：重试 3 轮仍读到 0 行，返回空（疑页面未就绪/结构变化）")
        return []
    # 按活动名去重：同一活动常在页面上半区（带「≥N个」库存门槛）和下半区（无门槛、只「报名」）
    # 各出现一次，折扣率一致。若按 (name,rate,min_stock) 去重，两条 min_stock 不同不会合并，
    # 导致同一活动被报两次。故按 name 去重，并保留【带库存门槛(min_stock 非空)】的那条，
    # 使卡库存门槛可用。
    by_name: dict = {}
    order: list = []
    for r in (rows or []):
        parsed = _parse_activity(r)
        if not parsed["name"] or parsed["name"] == "报名":
            continue
        name = parsed["name"]
        prev = by_name.get(name)
        if prev is None:
            by_name[name] = parsed
            order.append(name)
        elif prev.get("min_stock") is None and parsed.get("min_stock") is not None:
            by_name[name] = parsed  # 用带库存门槛的版本替换
    return [by_name[n] for n in order]


async def verify_activity_registration(
    page, activity_name: str, baseline_count: int, expected_add: int, tries=6,
) -> dict:
    """刷新活动列表，以“已报名”列数字增量作为报名成功的权威判据。"""
    target_count = baseline_count + expected_add
    last_count = None
    last_note = ""
    for attempt in range(tries):
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            activities = await read_activities(page)
            current = next(
                (activity for activity in activities if activity.get("name") == activity_name), None
            )
            last_count = current.get("registered_count") if current else None
            if last_count is not None and last_count >= target_count:
                return {
                    "verified": True, "before": baseline_count, "after": last_count,
                    "note": f"活动页已报名 {baseline_count}→{last_count}",
                }
            last_note = (f"活动页当前已报名={last_count}，目标≥{target_count}"
                         if current else "刷新后未找到目标活动")
        except Exception as exc:
            last_note = f"刷新活动页核验异常：{str(exc)[:100]}"
        if attempt + 1 < tries:
            await asyncio.sleep(2)
    return {
        "verified": False, "before": baseline_count, "after": last_count,
        "note": last_note or "活动页已报名列未出现预期增量",
    }


_MARK_ACTIVITY_LOG_SPU_JS = r"""
() => {
  document.querySelectorAll('[data-kiro-log-spu]').forEach(
    element => element.removeAttribute('data-kiro-log-spu')
  );
  const visible = input => {
    const rect = input.getBoundingClientRect();
    const style = getComputedStyle(input);
    return rect.width > 0 && rect.height > 0
      && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const inputs = [...document.querySelectorAll('input')].filter(visible);
  // 记录页的 SPU 选择器可能重绘成“SPU ID”或“ID查询”；真正输入框稳定特征是
  // 非 readonly 且 placeholder 含“多个/空格/逗号”。不再把 selector value 当唯一条件。
  const labels = inputs.filter(input => input.readOnly &&
    /SPU\s*ID|ID查询|SPU/i.test((input.value || '').trim()));
  const label = labels[labels.length - 1];
  const labelRect = label ? label.getBoundingClientRect() : null;
  const candidates = inputs.filter(input => {
    if (input.readOnly || !/多个|空格|逗号/.test(input.placeholder || '')) return false;
    const rect = input.getBoundingClientRect();
    return !labelRect || (rect.x > labelRect.x && Math.abs(rect.y - labelRect.y) < 18);
  });
  const field = candidates[candidates.length - 1];
  if (!field) return false;
  field.setAttribute('data-kiro-log-spu', '1');
  return true;
}
"""


_EXITED_ENROLL_STATUSES = frozenset({6})
_EXITED_STATUS_WORDS = ("已退出",)
_KNOWN_ENROLL_STATUSES = frozenset({1, 3, 4, 6})


def _parse_activity_log_item(item: dict) -> dict:
    session_failures = [
        session.get("sessionFailReason") for session in (item.get("assignSessionList") or [])
        if session.get("sessionFailReason")
    ]
    enroll_status = item.get("enrollStatus")
    status_value = str(enroll_status).strip()
    enroll_status = int(status_value) if re.fullmatch(r"\d+", status_value) else None
    status_text = " ".join(str(value) for value in item.values() if isinstance(value, str))
    exited = enroll_status in _EXITED_ENROLL_STATUSES or any(
        word in status_text for word in _EXITED_STATUS_WORDS
    )
    if enroll_status not in _KNOWN_ENROLL_STATUSES and not exited:
        logger.info(
            f"[活动记录] 未标定的 enrollStatus={item.get('enrollStatus')}（不确认成功），"
            f"enrollId={item.get('enrollId')}，请后台核对状态"
        )
    return {
        "spu": str(item.get("productId") or ""),
        "activity": item.get("activityThematicName") or item.get("activityTypeName") or "",
        "enroll_status": enroll_status,
        "success": enroll_status in {1, 3, 4} and not exited,
        "exited": exited,
        "enroll_time": item.get("enrollTime"),
        "enroll_id": item.get("enrollId"),
        "session_failures": session_failures,
        "raw": item,
    }


_MARK_ACTIVITY_LOG_NEXT_JS = r"""
() => {
  document.querySelectorAll('[data-kiro-log-next]').forEach(
    element => element.removeAttribute('data-kiro-log-next')
  );
  const pagination = document.querySelector('ul[data-testid="beast-core-pagination"]');
  if (!pagination) return {found: false, disabled: false, page: null};
  const candidates = [...pagination.querySelectorAll('li,button,[role="button"]')];
  // 当前页码（beast 分页的激活项）：「翻页后组件是否回稳」的主判据——翻到末页后下一页
  // 按钮合法禁用，只有页码能证明上一次翻页已经渲染完。读不到（结构变化）时回 null，
  // 调用方退回「下一页按钮可用」判据。
  const active = candidates.find(element =>
    /active/i.test(String(element.className || ''))
    || ['true', 'page'].includes(String(element.getAttribute('aria-current') || '').toLowerCase())
  );
  const activeText = active ? (active.textContent || '').trim() : '';
  const page = /^\d+$/.test(activeText) ? parseInt(activeText, 10) : null;
  const next = candidates.find(element => {
    const cls = String(element.className || '');
    const label = `${element.getAttribute('aria-label') || ''} ${element.title || ''}`;
    // 排除 jumpNext 跳页块快进钮：它的 class（PGT_jumpNext）也含 "next" 且 DOM 顺序在
    // 真「下一页」（PGT_next）前面，不误排会点到快进钮——2026-10-03 实机实锤：从第 1 页
    // 点一下直接跳第 6 页，中间 2-5 页数据整段漏拉。
    if (/jump/i.test(cls)) return false;
    return /next/i.test(cls) || /下一页|next/i.test(label)
      || Boolean(element.querySelector(
        'svg[data-icon="right"],svg[data-icon="right-circle"],[class*="rightArrow"]'
      ));
  });
  if (!next) return {found: false, disabled: false, page};
  const disabled = next.matches('[disabled],[aria-disabled="true"]')
    || /disabled/i.test(String(next.className || ''));
  if (!disabled) next.setAttribute('data-kiro-log-next', '1');
  return {found: true, disabled, page};
}
"""


# 空安全点击：beast 组件重绘会丢 data-kiro-log-next 标记，querySelector 拿到 null 直接
# .click() 会抛 JS 异常；返回 false 让调用方重新标记后再点。
_CLICK_ACTIVITY_LOG_NEXT_JS = r"""
() => {
  const next = document.querySelector('[data-kiro-log-next="1"]');
  if (!next) return false;
  next.click();
  return true;
}
"""


async def _maximize_activity_log_page_size(page):
    """把记录页「每页条数」切到最大档，返回切换触发的新首屏 result（没切换回 None）。

    2026-10-03 探针实测：记录页 sizeChanger 档位 10/20/30/40，切到 40 后 78 条记录只剩
    2 页——页数不超页码窗口时跳页块快进钮（jumpNext）不渲染，翻页恒为逐页 +1；且翻页
    请求从 8 次降到 2 次，enroll/list 限流暴露面小 4 倍。best-effort：没有切换器（记录
    太少不渲染）、已是最大档或切换失败都回 None，调用方按原 pageSize 继续。
    """
    current = await page.evaluate(r"""() => {
      const header = document.querySelector(
        'ul[data-testid="beast-core-pagination"] [class*="PGT_sizeChanger"] [data-testid="beast-core-select-header"]'
      ) || document.querySelector(
        'ul[data-testid="beast-core-pagination"] [class*="PGT_sizeChanger"] [class*="ST_head"]'
      );
      if (!header) return -1;
      const match = (header.textContent || '').match(/\d+/);
      header.click();  // 点开下拉
      return match ? parseInt(match[0], 10) : 0;
    }""")
    if current < 0:
        return None
    # 等下拉选项渲染，取最大数字档
    target = 0
    for _ in range(10):
        target = await page.evaluate(r"""() => {
          const opts = [...document.querySelectorAll(
            '[data-testid="beast-core-select-option"],[class*="cIL_item"],[role="option"]'
          )].filter(o => /^\s*\d+\s*$/.test(o.textContent || ''));
          if (!opts.length) return 0;
          return Math.max(...opts.map(o => parseInt(o.textContent, 10)));
        }""")
        if target:
            break
        await asyncio.sleep(0.5)
    if not target or target <= current:
        return None  # 已是最大档（或读不到选项）：点了也不发请求，不必等响应
    async with page.expect_response(
        lambda response: "/marketing/enroll/list" in response.url, timeout=15000
    ) as response_info:
        await page.evaluate(r"""(size) => {
          const opt = [...document.querySelectorAll(
            '[data-testid="beast-core-select-option"],[class*="cIL_item"],[role="option"]'
          )].find(o => parseInt((o.textContent || '').trim(), 10) === size);
          if (opt) opt.click();
        }""", target)
    response = await response_info.value
    payload = await response.json()
    result = payload.get("result")
    return result if isinstance(result, dict) else None


async def _wait_activity_log_next_ready(page, timeout_s=10):
    """轮询等「下一页」按钮从加载态恢复（found 且未禁用），返回最后一次采样状态。

    beast 分页组件拿到响应后要处理/重绘，期间下一页按钮处于 disabled 加载态（2026-10-03
    批次实锤：上一页响应刚 return、下一次循环入口几毫秒内一次性采样必命中加载态，
    total=78 只拉到 4/8 页就被误判「按钮已禁用」中断全量拉取，19 个实际成功的报名全被
    误判 not_verified）。加载态是暂时的：每秒重采一次、最多约 10s，仍不可用才是真故障。
    """
    state = {}
    for _ in range(max(1, int(timeout_s))):
        state = await page.evaluate(_MARK_ACTIVITY_LOG_NEXT_JS)
        if state.get("found") and not state.get("disabled"):
            return state
        await asyncio.sleep(1.0)
    if not state.get("found"):
        raise RuntimeError("报名记录页未找到下一页按钮（等分页组件渲染约 10s 超时）")
    raise RuntimeError("报名记录下一页按钮持续禁用约 10s 未恢复")


async def _wait_activity_log_page_settled(page, before_page, timeout_s=10):
    """翻页响应到手后等分页组件回稳，返回稳定后读到的当前页码（读不到回 None）。

    主判据是「页码变成与翻页前不同的值」而不是「等于目标页码」——beast 分页在页数
    超过页码窗口时「下一页」是跳页块（2026-10-03 实机插桩：第 1 页点一次直接到第 6 页，
    1→6→7→8），目标页码可能根本不存在。翻到末页时下一页按钮合法禁用，故按钮可用只是
    页码读不出时的旁证。best-effort：等不到稳定态按超时返回最后读到的页码，不为「等稳」
    本身报错——下一次入口还有 _wait_activity_log_next_ready 兜底。
    """
    last_page = None
    for _ in range(max(1, int(timeout_s))):
        try:
            state = await page.evaluate(_MARK_ACTIVITY_LOG_NEXT_JS)
        except Exception:
            return last_page  # 页面跳转/上下文销毁：等不到稳定态，交下一次入口重采兜底
        if state.get("page") is not None:
            last_page = state["page"]
            if last_page != before_page:
                return last_page
        elif state.get("found") and not state.get("disabled"):
            return None  # 页码读不出但按钮可用：按已回稳处理，页码缺失交调用方估算
        await asyncio.sleep(1.0)
    return last_page


async def _collect_activity_log_pages(first_result: dict, fetch_next) -> dict:
    """按首个响应的 total/pageSize 拉完当前 SPU 的报名记录分页。

    "0 条记录" 的平台响应是 {"total": 0, "list": null}（实测 2026-09-25：无报名记录的商品，
    页面同时显示「暂无数据 / 共有 0 条」）。旧实现要求 list 必须是数组，于是把「确实没有记录」
    误判成「响应缺少 list/total → 查询不完整」，而调用方据此判「报名未确认」，让无记录的商品
    全部记成失败。这里把 null 归一成空列表：total 是权威计数，list 只提供内容。
    """
    raw_list = first_result.get("list")
    total = first_result.get("total")
    if not isinstance(raw_list, (list, type(None))) or total is None:
        raise ValueError("报名记录响应缺少 list/total，无法确认查询完整性")
    first_items = raw_list or []
    total = int(total)
    if total < 0:
        raise ValueError("报名记录 total 无效")
    if not first_items and total > 0:
        raise ValueError("报名记录响应 total>0 但 list 为空，无法确认查询完整性")
    reported_size = int(first_result.get("pageSize") or first_result.get("page_size") or 0)
    page_size = reported_size or len(first_items) or 10
    expected_pages = max(1, (total + page_size - 1) // page_size)
    items = list(first_items)
    current_page = 1
    error = None

    # 终止按【实际到达页码 / 已拉条数】而非翻页点击次数：beast 分页页数超过页码窗口时
    # 「下一页」是跳页块（2026-10-03 实测 1→6→7→8，4 次点击就拉完 8 页），点击次数与到达
    # 页码不一致，到末页后按钮合法禁用、再翻必报错——按次数翻页会把「数据已拉全」误判成
    # 「查询不完整」。数据拉全（items==total）或到达末页即正常完成。
    while current_page < expected_pages and len(items) < total:
        try:
            result, actual_page = await fetch_next(current_page)
            page_list = result.get("list")
            if not isinstance(page_list, (list, type(None))) or int(result.get("total", -1)) != total:
                raise ValueError("报名记录分页响应不完整或 total 发生变化，请重新查询")
        except Exception as exc:
            error = str(exc)[:120]
            break
        items.extend(page_list or [])
        # 页码读不出时按 +1 保守估算（跳块场景会低估，但 len(items)==total 判据兜住完整性）。
        current_page = actual_page if isinstance(actual_page, int) else current_page + 1

    identities = [str(item["enrollId"]) for item in items if item.get("enrollId") not in (None, "")]
    unique_records = len(identities) == len(items) and len(set(identities)) == len(items)
    if not unique_records and error is None:
        error = "报名记录缺少 enrollId 或分页重复返回相同记录"

    return {
        "items": items,
        "total": total,
        "page_size": page_size,
        "expected_pages": expected_pages,
        # 实际到达页码（跳页块下不等于翻页点击次数）；完整性权威判据是 len(items)==total
        "pages_read": current_page,
        "complete": error is None and len(items) == total and unique_records,
        "error": error,
    }


def _group_log_items_by_spu(items, targets, collected) -> tuple:
    """把全量报名记录按 SPU（接口 productId）分组，产出 (records, queries)。

    全量不完整（有页没拉到）时所有 SPU 一律 complete=False——缺失页里可能正有该 SPU
    的记录，按残缺列表说「无记录」是假阴性（fail-closed）。查询完整但确实没记录时
    total=0 且 complete=True（2026-09-25 起「无记录」与「查询不完整」必须分开）。
    """
    complete = bool(collected.get("complete"))
    by_spu = {spu: [] for spu in targets}
    for item in items:
        pid = str(item.get("productId") or "")
        if pid in by_spu:
            by_spu[pid].append(item)
    records = []
    queries = []
    for spu in targets:
        matched = by_spu[spu]
        queries.append({
            "spu": spu, "returned": len(matched), "total": len(matched),
            "page_size": collected.get("page_size"),
            "expected_pages": 1, "pages_read": 1,
            "complete": complete,
            "error": None if complete else collected.get("error"),
        })
        records.extend(_parse_activity_log_item(item) for item in matched)
    return records, queries


async def read_activity_log_records(context, spus, region=None) -> dict:
    """只读打开报名记录页，拉【全量】报名记录分页，本地按 SPU（接口 productId）分组返回。

    【为什么不逐 SPU 搜索】2026-09-30 实测：记录页「SPU ID」搜索框整体失效——搜 SPU、
    goodsId、商品名一律返回 total=0（有历史记录的老商品同样搜不到），清空条件才返回
    全量。而批次提交明明已被平台收录（结果页 successCount=1、全量列表里能翻到），按 SPU
    搜却全 0，对账把成功提交全误判成「未收录」。记录行自带 productId（即 SPU），故改为
    拉全量分页本地分组，不再依赖平台搜索；店铺记录量级（年几十条）下翻页成本可接受，
    且平台哪天修好搜索本实现依然正确。

    region：调用方已确认的作业区域（app/temu_region.Region）。报名记录页开在该区域的
    域名下——区域切换换域名，写死全球域会读到另一个区域的报名记录。未传则就地从 context
    里的用户页签确认一次（读不到就抛，不默认全球域）。
    """
    from app.temu_region import confirm_region_from_context, url_in_region

    if region is None:
        region = await confirm_region_from_context(context)
    targets = list(dict.fromkeys(str(spu).strip() for spu in spus if str(spu).strip()))
    page = await context.new_page()
    records = []
    queries = []
    try:
        # 开页即自动请求列表第一页（无需点查询），先挂 expect_response 再导航。
        async with page.expect_response(
            lambda response: "/marketing/enroll/list" in response.url,
            timeout=30000,
        ) as response_info:
            await page.goto(url_in_region(ACTIVITY_LOG_PATH, region),
                            wait_until="domcontentloaded", timeout=30000)
        await page.bring_to_front()
        await dismiss_all_page_popups(page)
        # 【曾在这里硬点顶栏「全球」，已移除】那排标签是区域切换器，点它会【跨域跳转】
        # （全球 agentseller.temu.com ↔ 美国 agentseller-us.temu.com，2026-08-07 实测），
        # 等于把操作者选定的区域悄悄换掉、读到别的区域的报名记录。区域现由上面的 goto
        # 用已确认区域的域名钉住，页面自己会把数据限定在该区域，不需要也不该再点标签。
        response = await response_info.value
        payload = await response.json()
        first_result = payload.get("result") or {}

        # 每页条数切到最大档（40）：页数从 8 降到 2，翻页请求少了、限流暴露面小；且页数
        # 不超页码窗口时跳页块快进钮（jumpNext）不渲染，翻页恒逐页。切换触发的新首屏
        # 响应替换原 first_result。best-effort：切换失败按原 pageSize 继续。
        try:
            switched_result = await _maximize_activity_log_page_size(page)
            if switched_result:
                first_result = switched_result
        except Exception as exc:
            logger.warning(f"记录页切换每页条数失败（按原分页继续）：{str(exc)[:120]}")

        async def fetch_next(before_page):
            # 入口先等分页组件从「上一页响应处理中」的加载态回稳，而不是一次性采样看到
            # disabled/not found 就报错——加载态是暂时的（2026-10-03 批次竞态根因）。
            await _wait_activity_log_next_ready(page)
            # 平台对 enroll/list 有限流（2026-09-30 实测：快速连点翻页会返回
            # {"success": false, "errorMsg": "请求太频繁了"}，result=null），点击前先等 2s；
            # 仍撞上就把那次响应丢弃、再等 3s 重点（限流响应里页面没翻成，按钮还在当前页）。
            await asyncio.sleep(2.0)
            next_payload = None
            ever_clicked = False
            for _ in range(3):
                # 每次点击前重新标记：组件重绘会丢 data-kiro-log-next 标记（此时 querySelector
                # 拿到 null），且在 disabled 按钮上 JS click 不发请求、只会等满 expect_response
                # 的 15s 超时，把「没点上」误导成「接口慢」。点击间隙又进加载态就再等回稳。
                state = await page.evaluate(_MARK_ACTIVITY_LOG_NEXT_JS)
                if not state.get("found") or state.get("disabled"):
                    await _wait_activity_log_next_ready(page)
                async with page.expect_response(
                    lambda next_response: "/marketing/enroll/list" in next_response.url,
                    timeout=15000,
                ) as next_response_info:
                    clicked = await page.evaluate(_CLICK_ACTIVITY_LOG_NEXT_JS)
                if not clicked:
                    continue  # 点击瞬间标记丢了（组件刚重绘），下一轮重标再点
                ever_clicked = True
                next_response = await next_response_info.value
                next_payload = await next_response.json()
                if isinstance(next_payload.get("result"), dict):
                    # 拿到响应≠页面渲染完：等组件回稳再 return，保证下一次循环入口采到
                    # 的是稳定 DOM（否则下次入口采样又撞加载态）。跳页块场景下实际到达
                    # 页码不等于 before_page+1，须带回真实页码给上层循环。
                    actual_page = await _wait_activity_log_page_settled(page, before_page)
                    return next_payload["result"], actual_page
                await asyncio.sleep(3.0)
            if not ever_clicked:
                raise RuntimeError("报名记录下一页按钮标记反复丢失，翻页点击未发出")
            raise RuntimeError(f"报名记录翻页连续被限流：{str(next_payload)[:80]}")

        collected = await _collect_activity_log_pages(first_result, fetch_next)
        items = collected.pop("items")
        records, queries = _group_log_items_by_spu(items, targets, collected)
        complete = bool(collected["complete"])
        matched_desc = ", ".join(f"{q['spu']}={q['total']}" for q in queries) or "无目标"
        logger.info(
            f"[活动记录] 全量拉取 total={collected['total']}，"
            f"页数={collected['pages_read']}/{collected['expected_pages']}，本地匹配：{matched_desc}"
        )
        note = "报名记录查询完成（全量拉取本地匹配）" if complete else "报名记录查询不完整"
        return {"records": records, "complete": complete, "queries": queries, "note": note}
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ---- 商品库存读取（卡活动库存门槛用）----------------------------------------
# 库存不在页面 DOM，而在商品列表接口 skc/pageQuery。实测：自造 fetch 直连该接口会 403
# （缺动态 anti-content 反爬签名），故不主动构造请求；改用【被动监听】——reload 商品列表页，
# 让页面自己的 JS 带签名发请求，我们只捕获响应。库存 = 每个 SPU 下各 SKU 的 virtualStock
# 之和（总可售）。成本表「SPU ID」== 接口 productId。best-effort：异常只告警、返回已拿到的。
async def read_stock(page, product_ids=None) -> dict:
    """只读：捕获商品列表接口响应，返回 {productId(str): 库存合计}。

    page 须为商品列表页（goods/list）。若不在该页先导航过去、否则 reload 触发接口。
    product_ids 可选：给了则只保留这些 SPU（其余忽略），便于按本批过滤。
    注意：接口默认返回列表第一页；商品数超过一页时本函数只覆盖第一页（当前店铺场景够用，
    多页/精确过滤依赖反爬签名，暂不主动构造）。
    """
    want = {str(x).strip() for x in (product_ids or []) if str(x).strip().isdigit()}
    result: dict = {}

    async def on_resp(resp):
        if _STOCK_API_KEY not in (resp.url or ""):
            return
        try:
            body = await resp.json()
        except Exception:
            return
        items = (body.get("result") or {}).get("pageItems") or [] if isinstance(body, dict) else []
        for it in items:
            pid = str(it.get("productId"))
            skus = it.get("productSkuSummaries") or []
            result[pid] = sum(int(s.get("virtualStock") or 0) for s in skus)

    page.on("response", on_resp)
    try:
        if "goods/list" not in (page.url or ""):
            # 用【本页当前所在域】拼商品列表 URL：这个 page 是本批在已确认区域下新开的，
            # 它的 host 就是那个区域。写死全球域会把页面跳到另一个区域、读到别家库存。
            from app.temu_region import Region, host_of, url_in_region

            await page.goto(
                url_in_region(GOODS_LIST_PATH, Region(host=host_of(page.url or ""))),
                wait_until="domcontentloaded", timeout=60000,
            )
        else:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        await dismiss_site_notification_panel(page)
        # 等接口自然返回（页面 JS 带 anti-content 发请求）。
        for _ in range(24):
            await asyncio.sleep(0.5)
            if result:
                break
    except Exception as e:
        logger.warning(f"read_stock：加载商品列表/捕获库存异常：{e}")
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass

    if want:
        return {k: v for k, v in result.items() if k in want}
    return result


# ---- LLM 判断点A：选活动 ----------------------------------------------------
_JUDGE_ACTIVITY_SYSTEM = (
    "你是电商活动报名助手。给你一个商品的【日常价、成本、毛利率红线价】，以及若干个可报名的"
    "营销活动（含各自的折扣率上限与库存门槛）。请判断该商品最适合报名【哪一个】活动。"
    "规则：申报价 = 日常价 × 活动折扣率，须保证 (申报价−成本)/申报价 ≥ 红线毛利率（即申报价 ≥ 红线价）。"
    "在满足红线的前提下，优先选【折扣率越高（越接近原价、让利越少）】的活动。"
    "只能从我给出的活动名里选一个；若没有任何活动能在守住红线的同时可行，返回 activity 为 null。"
    "严格返回 JSON：{\"activity\": \"<活动名，须与给定名字完全一致>\" 或 null, \"reason\": \"<15字内理由>\"}。"
    "只输出 JSON，勿加围栏或解释。"
)


async def judge_activity(
    daily_price, cost, red_line, activities: list, config_name: str = "default"
) -> dict:
    """LLM 判断点A：从 activities 里选出该商品最适合报名的活动。

    注意：2026-07-16 重构后，主流程改为「确定性筛选：申报价≥销售底价 且 库存够门槛 的活动
    全报」，不再调用本函数选单个活动。此函数暂保留，以备未来「候选活动过多需 LLM 推荐排序」
    等场景复用；当前 service 流程未接入。

    返回 {"activity": str|None, "reason": str}。judge 只在【折扣率已知】的活动里选——
    「详见商品提报列表」类无固定折扣（discount_rate=None）阶段1 无法确定性算价，故先剔除
    （阶段3 落地弹窗字段后再处理）。可选活动为空 / LLM 判无合适 / 解析失败 → activity=None。
    """
    usable = [a for a in (activities or []) if a.get("discount_rate") is not None]
    if not usable:
        return {"activity": None, "reason": "无折扣率可判定的活动"}

    lines = [
        f"日常价：{daily_price}",
        f"成本：{cost}",
        f"红线价（申报价不得低于此）：{red_line}",
        "可选活动（活动名 → 折扣率上限 / 库存门槛）：",
    ]
    for a in usable:
        stock = a.get("min_stock")
        lines.append(
            f"- {a['name']}：折扣率{a['discount_rate']}"
            + (f"，库存≥{stock}" if stock is not None else "")
        )
    user_text = "\n".join(lines) + "\n\n请选出最适合报名的活动名（或 null）。"

    llm = LLM(config_name=config_name)
    try:
        raw = await llm.ask(
            messages=[Message.user_message(user_text)],
            system_msgs=[Message.system_message(_JUDGE_ACTIVITY_SYSTEM)],
            stream=False,
            temperature=0.0,
        )
    except Exception as e:
        logger.warning(f"judge_activity：LLM 调用异常：{e}")
        return {"activity": None, "reason": f"LLM 异常：{e}"}

    data = _parse_json(raw)
    if not data:
        logger.warning(f"judge_activity：解析失败，原始：{(raw or '')[:120]}")
        return {"activity": None, "reason": "判断解析失败"}
    chosen = data.get("activity")
    reason = str(data.get("reason", ""))
    # 校验 LLM 只能选给定活动名，防幻觉出不存在的活动。
    if chosen is not None and chosen not in {a["name"] for a in usable}:
        logger.warning(f"judge_activity：LLM 返回了不在候选里的活动「{chosen}」，视为无匹配")
        return {"activity": None, "reason": f"返回了未知活动：{chosen}"}
    return {"activity": chosen, "reason": reason}


def build_over_ref_note(submit_price, ref_price, daily_price, discount_rate) -> dict:
    """申报价超参考价时生成失败提示：反推平台认可的日常价基准，供操作者核对 Excel。

    正算是 submit_price = 日常价 × 折扣率（见 compute_submit_price）；平台参考价同样是
    「前端实际售价 × 折扣率」，故 参考价 / 折扣率 = 平台认可的前端售价基准（≈应填的日常价）。
    折扣率无效（缺省/≤0）时不反推，退回原文案。返回 {note, suggested_daily_price, current_daily_price}。
    """
    dr_num = _to_number(discount_rate)
    dp_num = _to_number(daily_price)
    suggest = round(ref_price / dr_num, 2) if (dr_num is not None and dr_num > 0) else None
    if suggest is not None and dp_num is not None:
        hint = (
            f"（按活动折扣 {dr_num} 反推：平台认可日常价约 {suggest}，"
            f"当前 Excel 日常价 {dp_num}，请把该商品日常价核对为约 {suggest} 后重报）"
        )
    elif suggest is not None:
        hint = (
            f"（按活动折扣 {dr_num} 反推：平台认可日常价约 {suggest}，"
            f"请核对该商品 Excel 日常价后重报）"
        )
    else:
        hint = "（疑 Excel 日常价与商品前端实际售价不一致）——请核对该商品 Excel 日常价"
    return {
        "note": f"申报价 {submit_price} 高于提报页参考价 {ref_price}{hint}——已跳过未报名",
        "suggested_daily_price": suggest,
        "current_daily_price": dp_num,
    }


def compute_submit_price(daily_price, discount_rate, sale) -> dict:
    """按销售底价算申报价并判是否达底价（纯本地，无副作用）。

    用户方案（2026-07-16）：红线从「毛利率」改为 Excel「销售价格」列——即用户手填的可接受
    最低售价（底价），完全替代旧的 成本÷(1−毛利率)。判定只看申报价是否够底价，与成本/毛利率
    无关（等价式：submit≥sale ⟺ 活动折扣率 ≥ sale/日常价）。
    - 申报价策略 discount_ceiling：submit_price = 日常价 × 活动折扣率（贴折扣上限、少让利），
      结果按【向下取整】到分——与平台参考价同口径，理由见下面的实现注释。
    - within_floor：submit_price ≥ sale（销售底价）。判定用的是取整后的价：平台上限够不到
      底价的活动在初筛就被淘汰，不会等到填价时才失败。
    - 防御：任一关键值缺失（日常价/折扣率/底价读不到）→ within_floor=False，note 标注原因。
    返回 {submit_price, floor_price(=sale), within_floor, note}。
    """
    dp = _to_number(daily_price)
    dr = _to_number(discount_rate)
    fl = _to_number(sale)

    notes = []
    if dp is None:
        notes.append("日常价缺失")
    if dr is None:
        notes.append("折扣率缺失")
    if fl is None:
        notes.append("销售底价缺失")

    submit_price = None
    if dp is not None and dr is not None:
        # 舍入口径必须与平台一致：申报价一旦高于平台的「参考价」就填不进提报页。
        # 实测（2026-09-24）平台的参考价 = 平台日常价 × 折扣率【向下取整到分】：
        #   188.88 × 0.85 = 160.548 → 平台给 160.54（四舍五入会给 160.55）
        #   163.23 × 0.85 = 138.7455 → 平台给 138.74（四舍五入会给 138.75）
        # 用四舍五入时，第三位小数 ≥5（约占一半）的商品申报价就比上限高 1 分、必被平台拒——
        # 这正是历史上「Excel 日常价与参考价对不上」的一大来源。故这里同样向下取整到分。
        # 代价：极少数情况下比四舍五入少报 1 分；换来的是「不会再因 1 分差报不上」，
        # 且底价高于平台上限的活动会在初筛阶段就被判不达底价（矩阵里直接可见），
        # 而不是等填价时才失败。
        submit_price = float(
            Decimal(str(dp * dr)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        )
    within = bool(submit_price is not None and fl is not None and submit_price >= fl)

    return {
        "submit_price": submit_price,
        "floor_price": fl,
        "within_floor": within,
        "note": "；".join(notes),
    }


def rate_reaches_floor(daily_price, discount_rate, sale) -> bool:
    """活动折扣率是否达到该货号底价（精确判定，不截断到分）。

    与 compute_submit_price 的 ROUND_DOWN 分工：截断只用于【填价】（申报价超过平台参考价
    必被拒）；门槛判定用精确乘积——188.88×0.85=160.548，若拿截断后的 160.54 比底价
    160.548 会差 8 厘误判破底（2026-09-29 2879383652×限时秒杀 就是这样被误淘汰的：
    底价本身就是按 85 折填的，SPU 明明能报）。任一值缺失返回 False（fail-closed）。
    """
    dp = _to_number(daily_price)
    dr = _to_number(discount_rate)
    fl = _to_number(sale)
    if dp is None or dr is None or fl is None:
        return False
    return Decimal(str(dp)) * Decimal(str(dr)) >= Decimal(str(fl))


def ceil_cent(value) -> Optional[float]:
    """向上取整到分（None/非法值原样返回）。

    保活动所需加速价用（用户规则 2026-09-28）：平台要求活动价 ≤ 加速价×0.9，故加速价
    须 ≥ 申报价÷0.9，向上取整宁可高 1 分——保证「申报价 ≤ 加速价×0.9」在整分边界上恒成立；
    向下取整会在边界上少 1 分、活动价恰好顶破 0.9 倍线。与 compute_submit_price 的
    ROUND_DOWN 同理：都是把边界误差推到「约束更严」的一侧，只是两侧利益方向相反。
    """
    v = _to_number(value)
    if v is None:
        return None
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


# ---- 流量加速器档位（用户规则 2026-09-29）--------------------------------------
# 档位按成本表「折扣」列定：9折/8折 → 高级流量加权；85折 → 普通流量加权；75折及更低 →
# 超级流量加权（自定义价 = 最低折扣价÷0.9）。未点名的折扣率按区间就近归档（用户同日复核）：
# >0.85 归高级、0.75~0.85 归高级（8折在其中）、≤0.75 归超级——即只有 85折 是普通档。
ACCEL_TIER_NAMES = {"normal": "普通", "advanced": "高级", "super": "超级"}
# 档位卡在抽屉里从左到右固定为 普通→高级→超级（2026-07-20 起卡片名画进背景图，DOM
# 认不出文字时按这个位置序选）。
ACCEL_TIER_POS = {"normal": 0, "advanced": 1, "super": 2}


def accel_tier_for_discount(discount) -> Optional[str]:
    """成本表折扣列值 → 加速档位（"normal"|"advanced"|"super"）；非法值返回 None。"""
    d = _to_number(discount)
    if d is None or d <= 0 or d > 1:
        return None
    if d <= 0.75:
        return "super"
    if abs(d - 0.85) < 1e-9:
        return "normal"
    return "advanced"


# 限流品加速器列的标志词（2026-09-29 用户定）：行内是这些文案 = 平台限流，此时停止
# 流量加速动作、只做活动报名。正常品的加速器列是「该商品已获得开启流量加速器机会」+「立即开启」。
ACCEL_THROTTLE_WORDS = ("流量待关注", "报名流量加速器", "调价提效")


def norm_sku_label(value) -> str:
    """归一化货号文本用于比对：小写、去掉除字母数字与汉字外的一切（点、斜杠、空格、连字符）。

    实测（2026-09-24）成本表写「70cm0.2kg」而平台货号字段是「70cm02kg」——只差点号，
    归一化后相等。中英混写的（成本表「30厘米/0.2kg」对平台「30cm02kg」）归一化也救不了，
    那种情况交给日常价匹配兜底。
    """
    return re.sub(r"[^0-9a-z一-鿿]+", "", str(value or "").lower())


def _match_by_label(sku_prices, page_rows) -> Optional[dict]:
    """按【平台货号】把页面行配对到成本表货号（身份键，最可靠）。

    日常价以表为准（用户 2026-09-24 定）：平台显示的日常价可能过期或与表不一致，不能拿它
    当配对键，否则一有差异就整单报不上。页面行里的货号字段才是身份。要求每个页面行都能唯一
    命中一个成本表货号、且每个货号至多被一行……允许多行（同货号多款式），但不能有行落空。
    行里没有货号字段、或归一化后对不上任何货号 → None（交给日常价匹配兜底）。
    """
    if not sku_prices or not page_rows:
        return None
    pool = {}
    for item in sku_prices:
        key = norm_sku_label(item["label"])
        if not key or key in pool:  # 货号重复/为空：身份不可判，放弃这条键
            return None
        pool[key] = item
    covered = set()
    assigned = {}
    for row in page_rows:
        key = norm_sku_label(row.get("label"))
        item = pool.get(key) if key else None
        if item is None:
            return None
        covered.add(key)
        assigned[row["idx"]] = {
            "label": item["label"], "labels": [item["label"]],
            "daily": item["daily"], "ref": row.get("ref"),
            "submit_price": item["submit_price"], "matched_by": "label",
        }
    if covered != set(pool):
        return None
    return assigned


def _match_by_daily(sku_prices, page_rows) -> Optional[dict]:
    """按【日常价】把页面行配对到成本表货号（平台价与表一致时可用）。

    申报价 = 日常价 × 折扣率，同日常价的货号填的价必然相同，故日常价相同的不必区分身份；
    但 label 要把它们都列出来——只写其中一个，超参考价时的失败文案就会张冠李戴
    （真机实测过：40cm 行被写成 50cm）。所以只有在平台价与表一致时这条键才成立。
    """
    if not sku_prices or not page_rows:
        return None
    pool: dict = {}
    for item in sku_prices:
        pool.setdefault(round(float(item["daily"]), 2), []).append(item)
    covered = set()
    assigned = {}
    for row in page_rows:
        daily = row.get("daily")
        if daily is None:
            return None
        key = round(float(daily), 2)
        items = pool.get(key)
        if not items:
            return None
        covered.add(key)
        assigned[row["idx"]] = {
            "label": "、".join(item["label"] for item in items),
            "labels": [item["label"] for item in items],
            "daily": items[0]["daily"], "ref": row.get("ref"),
            "submit_price": items[0]["submit_price"], "matched_by": "daily",
        }
    if covered != set(pool):  # 有货号的日常价没有任何页面行覆盖 → 那个货号会漏填
        return None
    return assigned


def match_enroll_rows(sku_prices, page_rows) -> Optional[dict]:
    """把提报页价格行匹配到成本表货号，返回 {行idx: {label,daily,ref,submit_price}} 或 None。

    匹配键是【平台货号】优先、【日常价】兜底（顺序有讲究：货号是身份，日常价只是巧合相同）：
    ① 用户规则「日常价以表为准」——平台显示的日常价可能过期，不能因为与表不一致就整单不报；
       真机实测（2026-09-24）一个 2 货号商品就是这样被整单拦下的（表 163.23 / 平台 188.88）。
    ② 货号字段缺失或中英写法差太大（表「30厘米/0.2kg」对平台「30cm02kg」）时，退回按日常价
       配对——那种情况下平台价通常与表一致，配对依然成立。
    两条键都配不齐（行数少于货号数、有行落空、日常价与货号都对不上）→ None，调用方 fail-closed
    不填价：报错价不可逆，报不上可重试。
    """
    by_label = _match_by_label(sku_prices, page_rows)
    if by_label is not None:
        return by_label
    return _match_by_daily(sku_prices, page_rows)


def accel_price_groups(accel_prices) -> dict:
    """把逐货号加速价合并成「日常价档 → 该档要填的价」。

    档的键用【平台日常价】（platform_daily，从提报页读到的那个），没有才退回表里的日常价：
    对话框按平台的日常价档列行（实测 2026-09-25：SPU 8791757215 表里两个货号都写 163.23，
    平台却是 163.23 / 188.88 两档，对话框就给 2 行——用表里的价分组只有 1 档，档数对不上必中止）。
    同档平台只让填一个价，故取该档【最高传入价】：绝不会低于档内任一货号的底价，也不会
    低于任一货号「保活动所需加速价」（=最低折扣价÷0.9，见 service 层 accel_prices 构造）——
    价由调用方按货号算好传入（底价+1 与最低折扣价÷0.9 取高），这里只管同档取最高。
    floor 额外保留「档内最高底价+1」：传入价超对话框上限时退填这个旧逻辑价（用户 2026-09-28
    定「接收活动失效」），见 _set_accel_prices。
    """
    groups: dict = {}
    for item in accel_prices or []:
        key = round(float(item.get("platform_daily") or item["daily"]), 2)
        floor = (round(float(item["sale"]) + 1, 2)
                 if item.get("sale") is not None else None)
        group = groups.get(key)
        if group is None:
            groups[key] = {"daily": key, "labels": [item.get("label")],
                           "price": float(item["price"]), "floor": floor}
            continue
        group["labels"].append(item.get("label"))
        group["price"] = max(group["price"], float(item["price"]))
        if floor is not None:
            group["floor"] = (floor if group["floor"] is None
                              else max(group["floor"], floor))
    return groups


def match_accel_rows(accel_prices, rows) -> Optional[dict]:
    """把「调整申报价」对话框的行匹配到【日常价档】，返回 {行idx: 档} 或 None。

    身份键是行文本里的「参考申报价格 + 让价」：两者之和正好是该档的日常价（实测
    30.27+44.45=74.72、168.67+20.21=188.88）。行数不等于档数、某行的日常价算不出、
    或与档对不上 → None，调用方中止不开——给错价比不开严重得多（不可逆）。
    """
    groups = accel_price_groups(accel_prices)
    if not groups or not rows or len(rows) != len(groups):
        return None
    if len(groups) == 1:
        # 单档单行：只有一个价可填，读不出日常价也没有错配对象，直接对应。
        return {rows[0]["idx"]: next(iter(groups.values()))}
    pairs = {}
    used = set()
    for row in rows:
        daily = row.get("daily")
        if daily is None:
            return None
        key = round(float(daily), 2)
        group = groups.get(key)
        if group is None or key in used:
            return None
        used.add(key)
        pairs[row["idx"]] = group
    if len(pairs) != len(groups):
        return None
    return pairs


def accel_price_hint(suggested_daily, sku_prices) -> str:
    """平台「认可日常价」若与某货号的加速价（底价+1）吻合 → 前端售价是被加速器压住的。

    实测 2026-09-25：某商品加速价设为 64.0 / 98.94 后，报名页的参考价变成
    57.6 / 89.04（= 加速价 × 0.9 活动折扣），按表里日常价算的申报价必然超——因为
    「加速器一开，前端显示价以加速价为准」，而活动申报上限是按前端售价算的。
    这种失败光说「请核对 Excel 日常价」会把人带偏，得直说是加速器压的。
    """
    if suggested_daily is None or not sku_prices:
        return ""
    for item in sku_prices:
        try:
            accel = round(float(item["sale"]) + 1, 2)
        except (KeyError, TypeError, ValueError):
            continue
        if abs(float(suggested_daily) - accel) <= 0.02:
            return (f"；该数值与货号{item['label']}的加速价 {accel} 吻合——该商品前端售价很可能"
                    f"已被加速价压低（加速器一开，前端显示价以加速价为准），"
                    f"请先在流量页关闭加速器再报名")
    return ""


def summarize_over_ref(over_rows, discount_rate, sku_prices=None) -> dict:
    """把「逐行超参考价」整理成人能读的文案：同一货号 + 同一参考价只写一条并带行数。

    为什么必须去重：提报页按平台 SKC 列行（实测一个货号两行、3 货号 6 行），不去重的话
    同一个原因会在失败文案里重复三四遍，操作者反而看不出到底几行有问题、是哪个货号。
    over_rows: [(assigned_item, ref)]，assigned_item 见 match_enroll_rows 的返回。
    sku_prices 给定时，若平台的「认可日常价」与某货号加速价吻合，会在文案里点明是加速器压的。
    返回 {"notes":[str], "ref_price", "daily_price", "suggested_daily_price"}。
    """
    groups: dict = {}
    for item, ref in over_rows:
        key = (item["label"], item["daily"], item["submit_price"], ref)
        groups[key] = groups.get(key, 0) + 1
    notes = []
    first = {}
    for (label, daily, submit_price, ref), count in groups.items():
        note = build_over_ref_note(submit_price, ref, daily, discount_rate)
        # 行数标在货号后面：build_over_ref_note 的文案自带「——已跳过未报名」结尾，别再追加尾巴。
        head = f"货号{label}" + (f"（{count} 行）" if count > 1 else "")
        hint = accel_price_hint(note.get("suggested_daily_price"), sku_prices)
        notes.append(f"{head}：{note['note']}{hint}")
        if not first:
            first = {"ref_price": ref, "daily_price": daily,
                     "suggested_daily_price": note.get("suggested_daily_price")}
    return {"notes": notes, **first}


def match_keys_desc(rows) -> str:
    """把页面行汇成「货号(日常价)」一句，供匹配失败时人工核对（货号可能读不到）。"""
    return "、".join(
        f"{row.get('label') or '行' + str(row['idx'])}({row.get('daily')})" for row in rows)


def rows_text_desc(rows, limit: int = 220) -> str:
    """把行文本摘要成一句（截断到 limit）写进失败 note。

    平台行结构只能实测得知：读不到日常价/归属不明时，把页面实际文本带出来，人一眼就能
    看出是哪一层结构变了，不用再跑一轮抓页面。
    """
    desc = "；".join(f"[{row.get('idx')}] {(row.get('text') or '')[:60]}" for row in rows)
    return desc[:limit]


# ---- 变更动作：阶段1 全部留桩（绝不在 dry-run 里被调用）------------------------
# 安全约束：真实商家账号、操作不可逆。阶段3 才实现，实现时每步须先 verify_state 确认幂等、
# 失败即停不硬闯。阶段1/dry-run 分支绝不调用以下任一函数。
# 在流量页某 SPU 行内标记指定文案的操作 a（查看效果/立即开启），供点击。
# 开启加速器只认「立即开启」一个入口（用户 2026-09-29 定）：限流品的加速器列是
# 「商品流量待关注 / 您可加速提效」+「报名流量加速器 / 调价提效」，这种品按规则停止
# 流量加速动作、只做活动报名——绝不能拿「报名流量加速器」当开启入口点进去。
_MARK_ROW_ACTION_JS = r"""
(args) => {
  const [spu, word] = args;
  const norm = s => (s || '').replace(/\s+/g, '');
  const hit = el => (el.textContent || '').replace(/\s+/g, '').includes(norm(word));
  // ① 同一容器内找（容器文本同时含 SPU ID 与目标文案）
  const all=[...document.querySelectorAll('div,tr,td,section')];
  let best=null,min=1e9;
  for(const el of all){
    const t=el.innerText||'';
    if(!new RegExp('SPU\\s*ID[：:\\s]*'+spu+'(\\D|$)').test(t)) continue;
    if(t.indexOf(word)<0) continue;
    if(t.length<min){min=t.length;best=el;}
  }
  if(best){
    const a=[...best.querySelectorAll('a,button,[role="button"]')].find(hit);
    if(a){a.setAttribute('data-kiro-act','1'); return (best.innerText||'').replace(/\s+/g,' ').trim().slice(0,60);}
  }
  // ② 同高查找（这平台的大表左右分栏渲染：SPU 信息列与操作列不在同一 DOM 子树里，
  //    按容器找必然漏；改成按纵向坐标对齐操作列）。
  let label=null,lmin=1e9;
  for(const el of all){
    const t=el.innerText||'';
    if(!new RegExp('SPU\\s*ID[：:\\s]*'+spu+'(\\D|$)').test(t)) continue;
    if(t.length<lmin){lmin=t.length;label=el;}
  }
  if(!label) return null;
  const lr=label.getBoundingClientRect();
  const cy=(lr.top+lr.bottom)/2;
  let pick=null,pd=1e9,ptext='';
  for(const el of document.querySelectorAll('a,button,[role="button"],[class*=btn],[class*=Btn]')){
    if(!hit(el)) continue;
    const r=el.getBoundingClientRect();
    if(r.width===0||r.height===0) continue;
    const d=Math.abs((r.top+r.bottom)/2-cy);
    if(d>40) continue;                      // 只认同一条视觉带
    if(d<pd){pd=d;pick=el;ptext=(el.textContent||'').replace(/\s+/g,'').slice(0,40);}
  }
  if(!pick) return null;
  pick.setAttribute('data-kiro-act','1');
  return '同高命中：'+ptext;
}
"""

# 找不到操作入口时把该 SPU 那行的【加速器列】文本带回来：用来区分「页面结构变了」和「平台本来
# 就没给这个入口」（实机 2026-09-24：该行加速器列写「商品流量待关注 / 您可加速提效 / 报名流量
# 加速器」，而没有「立即开启」）。只读。
# 为什么按坐标取：这平台的大表左右分栏渲染，加速器列与 SPU 信息列不在同一 DOM 子树里，
# 按「含 SPU ID 的最小容器」取只会拿到「SPU ID：xxx」这几个字，什么也说明不了。
_ROW_TEXT_JS = r"""
(spu) => {
  const re = new RegExp('SPU\\s*ID[：:\\s]*' + spu + '(\\D|$)');
  const all = [...document.querySelectorAll('div,tr,td,section')];
  let label = null, min = 1e9;
  for (const el of all) {
    const t = el.innerText || '';
    if (!re.test(t) || t.length > 600) continue;
    if (t.length < min) { min = t.length; label = el; }
  }
  if (!label) return '';
  const r = label.getBoundingClientRect();
  const cy = (r.top + r.bottom) / 2;
  const parts = [];
  for (const el of document.querySelectorAll('*')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (!own || own.length > 30) continue;
    if (!/加速|开启|关注|提效/.test(own)) continue;
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) continue;
    if (Math.abs((b.top + b.bottom) / 2 - cy) > 40) continue;
    if (parts.indexOf(own) < 0) parts.push(own);
  }
  return parts.slice(0, 4).join(' / ').slice(0, 120);
}
"""


async def _click_marked(page):
    """点击被 _MARK_ROW_ACTION_JS 标记的元素（JS click 绕过遮挡），并清标记。返回是否点到。"""
    ok = await page.evaluate(
        "() => { const a=document.querySelector('[data-kiro-act=\"1\"]'); "
        "if(a){a.removeAttribute('data-kiro-act'); a.click(); return true;} return false; }"
    )
    return bool(ok)


# 24h 冷却提示关键片段（平台文案：「加速器开启后需满24小时才可手动关闭，请耐心等待」）。
# 用「24小时」+（「关闭」或「开启后」）组合判定，容忍全/半角与措辞微调。
_COOLDOWN_KEYS = ("24小时", "24 小时", "满24", "需满")


async def _detect_cooldown_toast(page, tries=6) -> str:
    """检测「加速器开启后需满24小时才可手动关闭」类冷却 toast；命中返回其文案，否则返回 ""。

    toast 一闪即逝，故短轮询几次。返回非空即表示【关闭被 24h 冷却拦截、未真正关闭】。
    """
    for _ in range(tries):
        try:
            hit = await page.evaluate(r"""(keys) => {
              const visible = element => {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };
              const selectors = [
                '[class*=Toast]', '[class*=toast]', '[class*=Message]', '[class*=message]',
                '[role=alert]', '[class*=Notice]', '[class*=notice]'
              ];
              const texts = [...document.querySelectorAll(selectors.join(','))]
                .filter(visible)
                .map(element => (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim())
                .filter(Boolean);
              for (const text of texts) {
                if (keys.some(k => text.includes(k)) &&
                    (text.includes('关闭') || text.includes('停止') || text.includes('加速'))) {
                  return text.slice(0, 160);
                }
              }
              return '';
            }""", list(_COOLDOWN_KEYS))
        except Exception:
            hit = ""
        if hit:
            return hit
        await asyncio.sleep(0.5)
    return ""


_READ_CLOSE_REQUEST_UI_JS = r"""
() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const norm = text => (text || '').replace(/\s+/g, '').trim();
  const dialogs = [...document.querySelectorAll(
    '[class*=MDL_outerWrapper],[role=dialog],[class*=Modal],[class*=modal]'
  )]
    .filter(visible);
  const dialog = dialogs.find(element => norm(element.innerText).includes('确定停止流量加速吗')) || null;
  const stopButton = dialog
    ? [...dialog.querySelectorAll('button,[role=button]')].find(element => norm(element.textContent) === '停止')
    : null;
  const loading = Boolean(stopButton && (
    stopButton.disabled
    || stopButton.getAttribute('aria-busy') === 'true'
    || /loading|spin/i.test(String(stopButton.className || ''))
    || stopButton.querySelector('[class*=loading],[class*=Loading],[class*=spin],[class*=Spin],svg')
  ));

  const toastSelectors = [
    '[class*=Toast]', '[class*=toast]', '[class*=Message]', '[class*=message]',
    '[role=alert]', '[class*=Notice]', '[class*=notice]'
  ];
  const toastTexts = [...document.querySelectorAll(toastSelectors.join(','))]
    .filter(visible)
    .map(element => (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  for (const text of toastTexts) {
    if ((text.includes('24小时') || text.includes('24 小时') || text.includes('需满24') || text.includes('满24'))
        && (text.includes('关闭') || text.includes('停止'))) {
      return {dialog_open: Boolean(dialog), loading, status: 'cooldown', message: text.slice(0, 160)};
    }
  }
  for (const text of toastTexts) {
    if (text.includes('成功')) {
      return {dialog_open: Boolean(dialog), loading, status: 'success', message: text.slice(0, 160)};
    }
  }
  for (const text of toastTexts) {
    if ((text.includes('失败') || text.includes('稍后再试'))
        && (text.includes('关闭') || text.includes('停止') || text.includes('加速'))) {
      return {dialog_open: Boolean(dialog), loading, status: 'failure', message: text.slice(0, 160)};
    }
  }
  return {dialog_open: Boolean(dialog), loading, status: 'pending', message: ''};
}
"""


async def _wait_close_request_completion(page, tries=60) -> dict:
    """等待“停止”按钮转圈请求结算；请求未结束时绝不刷新页面。

    结算信号按优先级为：关闭结果 toast；确认弹窗消失；观察到 loading 后 loading 消失。
    最多等待约 30 秒，超时返回 settled=False，让调用方保守停下。
    """
    saw_dialog = True
    saw_loading = False
    settled_since = None
    last = {"dialog_open": True, "loading": False, "status": "pending", "message": ""}
    for attempt in range(tries):
        last = await page.evaluate(_READ_CLOSE_REQUEST_UI_JS)
        saw_dialog = saw_dialog or bool(last.get("dialog_open"))
        saw_loading = saw_loading or bool(last.get("loading"))
        status = last.get("status")
        if status in {"success", "failure", "cooldown"}:
            return {**last, "settled": True, "saw_loading": saw_loading, "polls": attempt + 1}
        ui_settled = ((saw_dialog and not last.get("dialog_open"))
                      or (saw_loading and not last.get("loading")))
        if ui_settled:
            if settled_since is None:
                settled_since = attempt
            elif attempt - settled_since >= 6:
                return {**last, "settled": True, "saw_loading": saw_loading, "polls": attempt + 1}
        else:
            settled_since = None
        await asyncio.sleep(0.5)
    return {**last, "settled": False, "saw_loading": saw_loading, "polls": tries,
            "message": last.get("message") or "等待关闭请求完成超时"}


async def _show_accel_on_products(page, tries=60) -> str:
    """切到“流量加速中”快速筛选并等待列表渲染，返回是否点到筛选入口。"""
    clicked = await page.evaluate(r"""() => {
      const norm = text => (text || '').replace(/\s+/g, '').trim();
      for (const element of document.querySelectorAll('button,a,div,span,[role=button]')) {
        const own = [...element.childNodes]
          .filter(node => node.nodeType === 3)
          .map(node => node.textContent)
          .join('');
        if (norm(own) !== '流量加速中') continue;
        let node = element;
        for (let depth = 0; depth < 5 && node; depth += 1, node = node.parentElement) {
          const rect = node.getBoundingClientRect();
          const className = String(node.className || '');
          if (rect.width > 100 && rect.height > 25
              && (node.matches('button,a,[role=button]') || /filter|quick|item/i.test(className))) {
            node.click();
            return true;
          }
        }
        element.click();
        return true;
      }
      return false;
    }""")
    if not clicked:
        return "not_found"
    for _ in range(tries):
        await asyncio.sleep(0.5)
        ready = await page.evaluate(
            "() => (document.body.innerText || '').includes('SPU ID')"
        )
        if ready:
            return "ready"
    return "timeout"


async def _verify_accel_off_in_list(page, spu, tries=120) -> dict:
    """刷新后切到“流量加速器待开启”，精确找到目标 SPU 且状态为 off 才算关闭成功。"""
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception as error:
        reload_warning = str(error)[:160]
    else:
        reload_warning = ""
    await dismiss_site_notification_panel(page)
    clicked = await page.evaluate(r"""() => {
      const norm = text => (text || '').replace(/\s+/g, '').trim();
      for (const element of document.querySelectorAll('button,a,div,span,[role=button]')) {
        const own = [...element.childNodes]
          .filter(node => node.nodeType === 3)
          .map(node => node.textContent)
          .join('');
        if (norm(own) !== '流量加速器待开启') continue;
        let node = element;
        for (let depth = 0; depth < 5 && node; depth += 1, node = node.parentElement) {
          const rect = node.getBoundingClientRect();
          const className = String(node.className || '');
          if (rect.width > 100 && rect.height > 25
              && (node.matches('button,a,[role=button]') || /filter|quick|item/i.test(className))) {
            node.click();
            return true;
          }
        }
        element.click();
        return true;
      }
      return false;
    }""")
    if not clicked:
        return {"verified": False, "filter_state": "not_found", "state": "unknown",
                "reload_warning": reload_warning}
    state = "unknown"
    for attempt in range(tries):
        await asyncio.sleep(0.5)
        state = await read_accel_state(page, spu)
        if state == "off":
            return {"verified": True, "filter_state": "ready", "state": state,
                    "polls": attempt + 1, "reload_warning": reload_warning}
    return {"verified": False, "filter_state": "timeout", "state": state,
            "polls": tries, "reload_warning": reload_warning}


async def _restore_flux_page_after_panel(page, tries=30) -> None:
    """close_accel 打开「查看效果」面板后的页面恢复：reload 回流量页列表并等搜索框就绪。

    面板（含遮罩）不关会挡住下一个 SPU 查询的「查询」按钮——Locator.click 5s 超时，
    状态判 unknown（2026-10-02 批次：1619974426 关闭走 success 早退没关面板，后续连续
    6 个 SPU 连环 unknown 的实测根因，复现确认死法是查询按钮被遮挡）。reload 是最确定
    的恢复方式（顺带清掉 toast/确认弹窗/筛选残留），verify 路径本就 reload；等搜索框可
    标记再返回，避免把「面板遮挡」换成「页面未就绪」。best-effort：失败只告警，不改变
    close_accel 已确定的结果。
    """
    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        logger.warning(f"关闭流量面板后 reload 失败（忽略）：{str(exc)[:120]}")
        return
    for _ in range(tries):
        try:
            if await page.evaluate(_MARK_FLUX_SPU_SEARCH_JS):
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    logger.warning("关闭流量面板后等待搜索框就绪超时（忽略）")


async def close_accel(page, spu, allow=False) -> dict:
    """关闭某 SPU 的流量加速：点行内「查看效果」→ 面板「近期流量加速效果」tab →「停止流量加速」
    →确认弹窗。allow=False（默认）只走到确认弹窗前不点最终确认（半程、可逆）。

    实测入口（2026-07-17）：加速中行无直接关闭按钮，须经「查看效果」面板。真关闭不可逆，
    故 allow=False 时打开面板、定位到「停止流量加速」即返回，不真正停止。
    返回 {state, closed, note}。若该 SPU 本就未加速（off）→ 直接 closed=True(no-op)。
    """
    await dismiss_all_page_popups(page)
    result = {"state": "unknown", "closed": False, "note": ""}
    state = await read_accel_state(page, spu, search=True)
    if state == "unknown":
        filter_state = await _show_accel_on_products(page)
        result["filter_state"] = filter_state
        for _ in range(60):
            state = await read_accel_state(page, spu)
            if state in {"on", "off"}:
                break
            await asyncio.sleep(0.5)
    result["state"] = state
    if state == "off":
        result["closed"] = True
        result["note"] = "本就未加速，无需关闭（no-op）"
        return result
    if state != "on":
        result["note"] = f"加速态={state}，保守不操作"
        return result

    # 点「查看效果」打开数据面板
    # 若「商品数据分析」效果面板尚未打开，点该行「查看效果」打开它（已开则跳过，避免重复点异常）。
    panel_open = await page.evaluate(
        "() => (document.body.innerText||'').includes('近期流量加速效果')"
    )
    if not panel_open:
        marked = await page.evaluate(_MARK_ROW_ACTION_JS, [str(spu), "查看效果"])
        if not marked or not await _click_marked(page):
            result["note"] = "未定位到该行「查看效果」入口"
            return result
        await asyncio.sleep(2.5)

    # 切到「近期流量加速效果」tab（JS click，tab 是普通文本节点，get_by_text 可能不可点）。
    await page.evaluate(r"""() => {
      const t=[...document.querySelectorAll('*')]
        .find(e=>{const own=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').replace(/\s+/g,'');return own==='近期流量加速效果';});
      if(t) t.click();
    }""")
    await asyncio.sleep(1.5)

    # 定位「停止流量加速」链接（裸 span）——存在性用 JS 判定（避免 span 不可 actionable）。
    has_stop = await page.evaluate(r"""() => {
      return [...document.querySelectorAll('*')]
        .some(e=>{const own=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').replace(/\s+/g,'');return own==='停止流量加速';});
    }""")
    if not has_stop:
        result["note"] = "面板内未找到「停止流量加速」"
        await _restore_flux_page_after_panel(page)
        return result

    if not allow:
        result["note"] = "半程：已打开效果面板并定位「停止流量加速」，未点击（allow=False）"
        return result

    # 正式关闭（授权后）：JS click「停止流量加速」（裸 span 非 actionable，用 JS 点其自身）→ 确认弹窗
    await page.evaluate(r"""() => {
      const el=[...document.querySelectorAll('*')]
        .find(e=>{const own=[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').replace(/\s+/g,'');return own==='停止流量加速';});
      if(el) el.click();
    }""")
    await asyncio.sleep(1.5)
    confirmed = False
    for word in ("确定", "确认", "停止"):
        btn = page.get_by_role("button", name=word).first
        if await btn.count():
            try:
                await btn.click()
                confirmed = True
                break
            except Exception:
                pass
    if not confirmed:
        result["note"] = "关闭确认弹窗内未点到「停止」按钮"
        await _restore_flux_page_after_panel(page)
        return result
    result["clicked_stop"] = True

    # 确认后平台会在「停止」按钮内转圈发请求。请求尚未结束就 reload 会中断现场、错过 toast，
    # 并把仍显示 on 的旧行状态误判为关闭失败。先完整等待请求结算，再做冷却/状态判定。
    completion = await _wait_close_request_completion(page)
    result["request_completion"] = completion
    if not completion.get("settled"):
        result["request_timeout"] = True
        result["note"] = f"关闭请求仍在处理中或未确认结束：{completion.get('message', '')}"
        await _restore_flux_page_after_panel(page)
        return result
    if completion.get("status") == "cooldown":
        result["cooldown"] = True
        result["note"] = f"未关闭：{completion.get('message', '命中24小时冷却提示')}"
        await _restore_flux_page_after_panel(page)
        return result
    if completion.get("status") == "failure":
        result["note"] = f"关闭失败：{completion.get('message', '平台返回失败提示')}"
        await _restore_flux_page_after_panel(page)
        return result
    if completion.get("status") == "success":
        result["closed"] = True
        result["success_message"] = completion.get("message", "")
        result["note"] = f"已停止流量加速（成功提示：{completion.get('message', '')}）"
        await _restore_flux_page_after_panel(page)
        return result

    # ★ 24h 冷却检测（最可靠的判定信号）：加速器开启不满 24h 手动关闭时，平台会弹 toast
    #   「加速器开启后需满24小时才可手动关闭，请耐心等待」。检出即判定【未关闭·被冷却拦截】。
    cooldown = await _detect_cooldown_toast(page)
    if cooldown:
        result["closed"] = False
        result["cooldown"] = True
        result["note"] = f"未关闭：{cooldown}"
        await _restore_flux_page_after_panel(page)
        return result

    off_verification = await _verify_accel_off_in_list(page, spu)
    result["off_verification"] = off_verification
    result["closed"] = bool(off_verification.get("verified"))
    result["note"] = ("已停止流量加速（待开启列表精确校验为 OFF）" if result["closed"]
                      else "关闭请求已结算，但未在待开启列表精确校验到目标 SPU 为 OFF")
    return result


# 打开提报页 JS：文本含活动名的最小元素 → 向上找最近含「报名」的行容器 → 标记其中报名 a。
_MARK_ENROLL_JS = r"""
(name) => {
  // 上一次活动留下的标记必须先清掉；否则 querySelector 会一直点击首个旧标记，反复进入同一活动。
  document.querySelectorAll('[data-kiro-enroll]').forEach(
    element => element.removeAttribute('data-kiro-enroll')
  );
  // 精确匹配（修撞名 bug）：从「报名」按钮反查其所在行，行文本须以目标活动名开头
  // （活动名是行首文本）。旧逻辑「找含名最小元素→向上爬 8 层抓第一个报名按钮」会在
  // 虚拟表格里抓到相邻行的报名按钮，导致点进错活动（如 85折撞进 6折同名场次）。
  const norm = s => (s || '').replace(/\s+/g, '').trim();
  const target = norm(name);
  if (!target) return null;
  const btns = [...document.querySelectorAll('a,button,[role="button"]')]
    .filter(e => norm(e.textContent) === '报名');
  for (const btn of btns) {
    let node = btn;
    for (let i = 0; i < 8 && node; i++) {
      const nt = norm(node.innerText);
      // 该祖先文本以目标名开头，且其内只有一个「报名」按钮 → 确是目标行
      if (nt.startsWith(target)) {
        const inner = [...node.querySelectorAll('a,button,[role="button"]')]
          .filter(e => norm(e.textContent) === '报名');
        if (inner.length === 1) {
          btn.setAttribute('data-kiro-enroll', '1');
          return (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80);
        }
      }
      node = node.parentElement;
    }
  }
  return null;
}
"""


async def verify_enroll_page_activity(page, expected_name: str, tries=20) -> dict:
    """确认 detail-new 页头显示的活动名与目标完全一致；无法确认时保守拒绝填价。"""
    last = {"matched": False, "actual": "", "ready": False}
    for _ in range(tries):
        last = await page.evaluate(r"""expected => {
          const norm = value => (value || '').replace(/\s+/g, '').trim();
          const target = norm(expected);
          const visibleOwnTexts = [];
          for (const element of document.querySelectorAll('h1,h2,h3,h4,div,span')) {
            const rect = element.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const own = [...element.childNodes]
              .filter(node => node.nodeType === Node.TEXT_NODE)
              .map(node => node.textContent).join('').trim();
            const text = norm(own);
            if (!text || text.length > 120) continue;
            if (text === target) return {matched: true, actual: own.trim(), ready: true};
            if (rect.top >= 70 && rect.top <= 220 && rect.left <= 1000) {
              visibleOwnTexts.push({text: own.trim(), top: rect.top, left: rect.left});
            }
          }
          visibleOwnTexts.sort((a, b) => a.top - b.top || a.left - b.left);
          const body = document.body.innerText || '';
          const ready = body.includes('SPU ID') && body.includes('活动申报价格');
          const ignored = new Set(['Seller Central', '服务市场', '履约中心', '学习', '运营对接',
            '规则中心', '消息', '客服', '反馈', '活动详情', '活动报名课程', '问题反馈']);
          const candidate = visibleOwnTexts.find(item => !ignored.has(item.text));
          return {matched: false, actual: candidate ? candidate.text : '', ready};
        }""", expected_name)
        if last.get("matched"):
            return last
        await asyncio.sleep(0.5)
    return last


_CLICK_ACTIVITY_RULE_JS = r"""
(activityName) => {
  const norm = value => (value || '').replace(/\s+/g, '').trim();
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
      && style.visibility !== 'hidden';
  };
  const target = norm(activityName);
  const buttons = [...document.querySelectorAll('button,a,[role="button"]')]
    .filter(button => visible(button) && norm(button.textContent).includes('同意活动规则'));
  let fallbackText = '';
  let fallbackLoading = false;
  for (const button of buttons) {
    let node = button;
    for (let depth = 0; depth < 16 && node && node !== document.body; depth += 1) {
      const rect = node.getBoundingClientRect();
      const text = norm(node.innerText);
      const isLargeLayer = rect.width >= 400 && rect.height >= 250;
      if (isLargeLayer && text.includes('活动详情')) {
        fallbackText = (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
        // 弹窗正文异步加载：「加载中」期间活动名还没渲染（实测 2026-07-23，标题区是
        // 占位符「-」）。此刻绑定必然 mismatch，必须让调用方继续轮询等正文加载完，
        // 而不是当失败整轮重试。外层文本含内层全部内容，最外层大层命中即可。
        fallbackLoading = text.includes('加载中');
      }
      if (isLargeLayer && text.includes('活动详情') && text.includes(target)) {
        if (button.disabled || String(button.className || '').includes('disabled')) {
          return {status: 'blocked', actual: fallbackText};
        }
        button.click();
        return {status: 'clicked', actual: activityName};
      }
      node = node.parentElement;
    }
  }
  if (buttons.length) {
    if (fallbackLoading) {
      return {status: 'loading', actual: fallbackText};
    }
    return {status: 'mismatch', actual: fallbackText || '规则按钮所在弹层未包含目标活动名'};
  }
  return {status: 'none', actual: ''};
}
"""


async def sweep_late_detail_tabs(ctx, initial_before, tries: int = 12, interval: float = 0.5) -> list:
    """关掉「本次调用之后才出现」的迟到提报页（detail-new），返回被关页的 URL 列表。

    平台有时在 open_enroll_page 放弃【之后】才把提报页开出来（实测 2026-09-25：三次都
    「未捕获到新 tab」，事后浏览器里却多出 3 个 detail-new 页签）。这些页会一直堆着，还会让
    下一次运行的区域确认失败（提报页没有顶栏区域切换器，被当成「读不到区域」）。
    只关本次调用后新出现的页签（initial_before 快照之外的），绝不碰操作者原本打开的页面。
    """
    for _ in range(max(1, tries)):
        late = [p for p in ctx.pages
                if "detail-new" in (getattr(p, "url", "") or "")
                and all(p is not old for old in initial_before)]
        if late:
            closed = []
            for page in late:
                try:
                    await page.close()
                    closed.append(getattr(page, "url", ""))
                except Exception:
                    pass
            return closed
        await asyncio.sleep(interval)
    return []


async def open_enroll_page(activity_page, activity_name, timeout_s=25):
    """从活动页打开指定活动的提报页(detail-new) tab 并返回该 Page（未填未提交、可逆）。

    实测链路（2026-07-17 验证）：活动页是 beast 表格，报名 a 无 href（React onClick）。
    ① 保留启动前已存在的 detail-new tab（可能是操作者手动截图/测试页）；
    ② 用 _MARK_ENROLL_JS 按活动名精确标记报名 a；
    ③ JS click 报名（绕过虚拟列表/遮挡的可见性超时）→ 等活动详情弹窗；
    ④ 只在【包含目标活动名】的规则弹窗内点「同意活动规则」（正文「加载中」时等其加载完再绑定）；
    ⑤ 用点击前/后 Page 对象集合 diff 认本次新 tab。失败只关闭本次新建页，绝不碰既有页。
    """
    await dismiss_all_page_popups(activity_page)
    ctx = activity_page.context
    await asyncio.sleep(0.5)

    # 实测教训（2026-07-20）：批量里第一个活动常「点报名后未捕获到提报页 tab」——标记按钮
    # 本身没问题（_MARK_ENROLL_JS 能命中），是「点击→弹窗→开新 tab」这段偶发 flake（首个尤甚）。
    # 故把「标记→点报名→同意规则→diff 认新 tab」整段包 3 次重试，每次用较短认 tab 超时，
    # 失败即重新标记再点，而非一次等满超时。
    per_try_s = max(6, int(timeout_s / 3))
    initial_before = list(ctx.pages)  # 收尾清场用：只关本次尝试后新出现的页
    for attempt in range(3):
        # 所有活动提报页 URL 都相同，必须按 Page 对象身份识别新标签，不能按 URL diff。
        before = list(ctx.pages)
        # ② 标记报名按钮
        matched = await activity_page.evaluate(_MARK_ENROLL_JS, activity_name)
        if not matched:
            logger.warning(f"[活动] 未定位到活动「{activity_name}」的报名按钮（第{attempt+1}次）")
            await asyncio.sleep(1)
            continue

        # ③ 点报名 → 弹活动详情弹窗
        await activity_page.evaluate(
            "() => { const b=document.querySelector('[data-kiro-enroll=\"1\"]'); if(b) b.click(); }"
        )

        # ④ 同时等直接打开的新页或目标活动规则弹窗。不能全页找「同意活动规则」直接点：
        #    上一活动残留弹窗会导致目标「官方大促」实际再次打开「限时秒杀」。
        #    实测教训（2026-07-23，15/17 个活动首次打开失败）：弹窗正文「加载中...」期间
        #    活动名尚未渲染，此刻绑定必然 mismatch——loading 只是没加载完，继续轮询即可；
        #    只有加载完仍不含目标活动名才判 mismatch 整轮重试。此前每个活动白等 ~8s。
        agreed = False
        modal_mismatch = None
        modal_loading = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            if kicked_to_login(getattr(activity_page, "url", "")):
                logger.error("[活动] 登录态失效：活动页已被踢回登录页，中止开提报页")
                return None
            direct_pages = [
                page for page in ctx.pages
                if "detail-new" in (page.url or "") and all(page is not old for old in before)
            ]
            if direct_pages:
                break
            rule = await activity_page.evaluate(_CLICK_ACTIVITY_RULE_JS, activity_name)
            rule_status = rule.get("status")
            if rule_status == "clicked":
                agreed = True
                break
            if rule_status in {"mismatch", "blocked"}:
                modal_mismatch = rule
                break
            if rule_status == "loading":
                modal_loading = True
        if modal_mismatch:
            logger.error(
                f"[活动] 规则弹窗未绑定目标「{activity_name}」："
                f"{modal_mismatch.get('actual') or modal_mismatch.get('status')}；未点击并重试"
            )
        elif not agreed and modal_loading:
            logger.error(
                f"[活动] 规则弹窗正文持续「加载中」超 10s 未渲染完：{activity_name}；未点击并重试"
            )
        elif not agreed:
            logger.info(f"[活动] 未见「同意活动规则」按钮（可能无需同意或弹窗未出）：{activity_name}")

        # ⑤ 轮询 diff 认新 tab（本次尝试的短超时）
        for _ in range(int(per_try_s * 2)):
            await asyncio.sleep(0.5)
            if kicked_to_login(getattr(activity_page, "url", "")):
                logger.error("[活动] 登录态失效：活动页已被踢回登录页，中止开提报页")
                return None
            news = [
                p for p in ctx.pages
                if "detail-new" in (p.url or "") and all(p is not old for old in before)
            ]
            if news:
                np = news[-1]
                try:
                    await np.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await dismiss_all_page_popups(np)
                verification = await verify_enroll_page_activity(np, activity_name)
                if not verification.get("matched"):
                    actual = verification.get("actual") or "未识别"
                    logger.error(
                        f"[活动] 提报页活动错位：目标「{activity_name}」，实际页头「{actual}」；"
                        "已关页且不填价"
                    )
                    try:
                        await np.close()
                    except Exception:
                        pass
                    break
                logger.info(f"[活动] 已打开提报页：{activity_name} → {np.url}（第{attempt+1}次）")
                return np
        logger.warning(f"[活动] 点报名后未捕获到提报页 tab（第{attempt+1}次）：{activity_name}")
        # 重试前只关闭【本次尝试新建】的 detail-new，保留操作者原先打开的手动测试页。
        for pg in list(ctx.pages):
            if ("detail-new" in (pg.url or "")
                    and all(pg is not old for old in before)):
                try:
                    await pg.close()
                except Exception:
                    pass
        await asyncio.sleep(1)
    logger.warning(f"[活动] 重试 3 次仍未捕获提报页 tab：{activity_name}")
    # 收尾清场：平台有时在本函数放弃【之后】才把提报页开出来，那些迟到页会一直堆着并污染
    # 下一次运行的区域确认。只关本次调用后新出现的页签。
    for url in await sweep_late_detail_tabs(ctx, initial_before):
        logger.info(f"[活动] 关掉迟到的提报页：{url[:80]}")
    return None


# 在提报页顶部搜索区按 SPU ID 过滤（实测 2026-07-17：详情页默认虚拟列表不一定含目标 SPU，
# 必须先搜。搜索框 = 值为「SPU ID」的标签框同一行、x 更大、placeholder 含「输入」的那个 input）。
_SEARCH_SPU_JS = r"""
(spu) => {
  const bs = [...document.querySelectorAll('input')];
  const lab = bs.find(i => (i.value || '').trim() === 'SPU ID');
  if (!lab) return 'no-label';
  const lr = lab.getBoundingClientRect();
  const box = bs.find(i => {
    const r = i.getBoundingClientRect();
    return Math.abs(r.y - lr.y) < 10 && r.x > lr.x && (i.placeholder || '').includes('输入');
  });
  if (!box) return 'no-search';
  box.setAttribute('data-kiro-spu', '1');
  return 'ok';
}
"""

# 勾选目标 SPU 行的商品 checkbox。beast 把行拆成左固定列(报名场次/勾选)+右滚动列(SPU ID)，
# DOM 不同 tr。按视觉 y 对齐：找「可报名场次」文本 y，勾同 y band(±30) 的 CBX 外层 wrapper。
_CHECK_ROW_JS = r"""
() => {
  let sy = null;
  for (const el of document.querySelectorAll('td,div,span')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    if (/可报名场次[：:]\s*\d/.test(own)) { sy = el.getBoundingClientRect().y; break; }
  }
  if (sy === null) return 'no-sess';
  const ws = [...document.querySelectorAll('[class*=CBX_squareInputWrapper],label[class*=CBX_outerWrapper]')]
    .filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && Math.abs(b.y - sy) < 30; });
  if (!ws.length) return 'no-cbx';
  ws[0].click();
  return 'ok';
}
"""


async def _set_sessions_all(page) -> dict:
    """勾选商品后点「批量设置场次」→ 弹窗「全选」→「确认」，把该商品所有可报场次全选。

    实测 2026-07-17：弹窗根是 beast `MDL_outerWrapper`（非 role=dialog），含「设置场次」标题、
    每场次一个 CBX、左下「全选」、右下「确认/取消」。不设场次会导致提交无效（历史全失败根因）。
    用「批量设置场次」（对已勾选商品统一设，用户确认「反正全报」用批量即可）。
    返回 {ok, note}。
    """
    # 点「批量设置场次」
    clicked = await page.evaluate(r"""() => {
      const el = [...document.querySelectorAll('button,a,[role="button"],span,div')].find(e => {
        const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, '');
        return own === '批量设置场次';
      });
      if (!el) return 'no-btn'; el.click(); return 'ok';
    }""")
    if clicked != "ok":
        return {"ok": False, "note": f"未找到「批量设置场次」按钮（{clicked}）"}
    await asyncio.sleep(2.0)

    # 弹窗「全选」（全选是带「全选」文本的 label/CBX，非 button；点其内的 CBX wrapper）
    qx = await page.evaluate(r"""() => {
      const dlgs = [...document.querySelectorAll('[class*=MDL_outerWrapper]')]
        .filter(e => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes('设置场次'));
      if (!dlgs.length) return 'no-dialog';
      const d = dlgs[dlgs.length - 1];
      const el = [...d.querySelectorAll('label,[class*=CBX_outerWrapper],[class*=CBX_squareInputWrapper],span,div')].find(e => {
        const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, '');
        return own === '全选';
      });
      if (!el) return 'no-quanxuan';
      const w = el.querySelector('[class*=CBX_squareInputWrapper]')
        || (el.closest('label') || el).querySelector('[class*=CBX_squareInputWrapper]') || el;
      w.click();
      return 'ok';
    }""")
    if qx != "ok":
        return {"ok": False, "note": f"弹窗全选失败（{qx}）"}
    await asyncio.sleep(1.0)

    # 校验有场次被勾中，再点「确认」。注意排除「全选」框自身——它也是 CBX_squareInputWrapper，
    # 不剥掉会把 1 个真实场次报成「已全选 2 个场次」（2026-09-29 排查破冰提交失败时被这
    # 句文案误导过一轮：弹窗实际只有「秘鲁」1 个场次 + 全选框）。
    checked_n = await page.evaluate(r"""() => {
      const dlgs = [...document.querySelectorAll('[class*=MDL_outerWrapper]')]
        .filter(e => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes('设置场次'));
      if (!dlgs.length) return -1;
      const d = dlgs[dlgs.length - 1];
      return [...d.querySelectorAll('[class*=CBX_squareInputWrapper] input')].filter(i => {
        if (!i.checked) return false;
        const label = i.closest('label') || i.parentElement;
        const t = ((label && label.textContent) || '').replace(/\s+/g, '');
        return t !== '全选';
      }).length;
    }""")
    if not isinstance(checked_n, int) or checked_n < 1:
        return {"ok": False, "note": f"全选后无场次勾中（checked={checked_n}）"}

    confirm = await page.evaluate(r"""() => {
      const dlgs = [...document.querySelectorAll('[class*=MDL_outerWrapper]')]
        .filter(e => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes('设置场次'));
      if (!dlgs.length) return 'no-dialog';
      const d = dlgs[dlgs.length - 1];
      const c = [...d.querySelectorAll('button')].find(e => (e.textContent || '').replace(/\s+/g, '') === '确认');
      if (!c) return 'no-confirm';
      if (c.disabled || (c.className || '').includes('disabled')) return 'confirm-disabled';
      c.click(); return 'ok';
    }""")
    if confirm != "ok":
        return {"ok": False, "note": f"点确认失败（{confirm}）"}
    await asyncio.sleep(1.5)
    return {"ok": True, "note": f"已全选 {checked_n} 个场次并确认"}


# 枚举提报页本 SPU 的全部 SKU 价格行：从含「SPU ID: {spu}」的锚点 tr 起向后续兄弟 tr
# 收集，遇到写着【其它 SPU】的行即停（防越界到别的商品）；锚点行本身就是第一个货号的价格行。
# 每个行容器取【第一个空的】可填 text input（勾选后「活动申报价格」框才可用），打
# data-enroll-sku-idx 标记供 Playwright 逐行填价；行文本解析日常价/参考价供 Python 侧匹配。
# 为什么必须挑「空的」而不是「第一个」（2026-09-24 真机 dump 教训）：一行里有多个 text input
# ——申报价（空）+ 参考库存/销售库存（预填 30 那种）。取第一个纯属侥幸命中；万一列序变了，
# 就会把价格填进库存框并被提交。取空框既避开预填的库存，也容得下列序变化；一个空框都没有
# 时标 fillable=False，由 Python 侧 fail-closed 报「该行没有可填的申报价框」。
# 实测（2026-09-24）：行是平台 SKC 粒度，一个货号多款式时会有多行（3 货号的商品出现 6 个
# 价格行），故行数不与货号数比对，改按日常价配对（见 match_enroll_rows）。
_MARK_ENROLL_SKU_ROWS_JS = r"""
(spu) => {
  const target = String(spu);
  const spuRe = /SPU\s*ID[：:\s]*([0-9]+)/;
  const norm = s => (s || '').replace(/\s+/g, ' ');
  const priceOf = (text, labels) => {
    for (const lab of labels) {
      const m = text.match(new RegExp(lab + '[：:]\\s*¥?\\s*([\\d.]+)'));
      if (m) return Number(m[1]);
    }
    return null;
  };
  const anchor = [...document.querySelectorAll('tr')].find(tr => {
    const m = (tr.innerText || '').match(spuRe);
    return m && m[1] === target;
  });
  if (!anchor) return {rows: []};
  const group = [];
  for (let tr = anchor; tr; tr = tr.nextElementSibling) {
    if (tr.tagName !== 'TR') continue;
    const m = (tr.innerText || '').match(spuRe);
    if (tr !== anchor && m && m[1] !== target) break;
    group.push(tr);
  }
  const rows = [];
  for (const tr of group) {
    const inputs = [...tr.querySelectorAll('input[type=text],input:not([type])')]
      .filter(x => !x.disabled);
    if (!inputs.length) continue;
    const text = norm(tr.innerText);
    const idx = rows.length;
    const blank = inputs.find(x => !(x.value || '').trim());
    if (blank) blank.setAttribute('data-enroll-sku-idx', String(idx));
    // 平台货号：行里有「货号: xxx」（SKU 属性列）与「货号:xxx」（真正的货号字段）两处，
    // 取最后一处；它是对上成本表货号的身份键，比日常价可靠（表与平台价可能不一致）。
    const labels = [...text.matchAll(/货号[:：]\s*([^\s]+)/g)];
    rows.push({
      idx,
      daily: priceOf(text, ['日常价', '供货价', '售价', '现价']),
      ref: priceOf(text, ['参考价']),
      label: labels.length ? labels[labels.length - 1][1] : '',
      text: text.slice(0, 120),
      fillable: !!blank,
    });
  }
  return {rows};
}
"""


# 提报页「场次共用活动库存」输入框标记（2026-09-29 补）：秒杀/破冰类活动库存必填——空着提交
# 平台拦下报「采集失败」、记录页无记录（2879383652 全程实跑 3 个活动实锤）；大促进阶类没有
# 此栏、平台兜底，故找不到「参考库存」锚不算失败。
# 为什么用「参考库存」文本当锚而不是 SPU 行：平台大表左右分栏渲染，库存列与商品信息列不在
# 同一 DOM 子树（TR 里可能根本不含库存格）；而搜索后页面只剩目标商品，全文档认锚即可。
_MARK_STOCK_INPUT_JS = r"""
() => {
  const anchors = [];
  for (const el of document.querySelectorAll('*')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent).join('').replace(/\s+/g, '');
    if (/^参考库存[：:]\d+$/.test(own)) anchors.push(el);
  }
  if (!anchors.length) return {found: false, reason: 'no-ref-stock'};
  const refStock = Number(anchors[0].textContent.match(/(\d+)/)[1]);
  // 从锚向上找含可填输入框的容器（库存值与输入框同栏）
  let node = anchors[0];
  for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
    const input = [...node.querySelectorAll('input[type=text],input:not([type])')]
      .find(x => !x.disabled && x.offsetWidth > 0);
    if (input) {
      const current = (input.value || '').trim();
      // 已有值（平台默认/上轮填过）：不覆盖不拦，如实带回去
      if (current) return {found: false, reason: 'already-filled', refStock, current};
      input.setAttribute('data-enroll-stock', '1');
      return {found: true, refStock};
    }
  }
  return {found: false, reason: 'no-input-near-ref', refStock};
}
"""


async def probe_detail_eligibility(page, spu, on_step=None) -> dict:
    """提报页只读资格探测：定位 SPU 搜索框 → 填 SPU → 点「查询」→ 等结果行数。

    从 `enroll_activity` 的搜索段原样抽出，供两处共用同一份判定：
    - 报名（enroll_activity）：探测通过后继续勾选/场次/填价；
    - 识别扫描（service.scan_activity_matrix）：只想拿资格、不勾选不填价。
    判据只能有一份实现——资格要落盘当天复用，两边判定不一致就是脏数据。

    返回 {"queried": bool, "detail_eligible": bool|None, "failed_step": str|None, "note": str}：
    True  = 查询后目标行恰为 1（详情页可报名）；
    False = 行数为 0（平台正常业务结果：列表页价格初筛通过不等于详情资格通过）；
    None  = fail-closed（未定位搜索框 / 未点到查询 / 行数非唯一 / 异常）。
    不勾选、不设场次、不填价、不提交，任何分支都不点按钮。
    """
    async def report(step, ok, note=""):
        if on_step is not None:
            await on_step({"step": step, "ok": bool(ok), "note": note})

    out = {"queried": False, "detail_eligible": None, "failed_step": None, "note": ""}

    # 1. 搜索 SPU（过滤出目标行）。详情页 domcontentloaded 后搜索区(「SPU ID」标签框)仍异步
    #    渲染，须轮询等其出现再搜（否则立即 evaluate 得 no-label）。
    loc = "no-label"
    for _ in range(20):  # 最多等 ~10s
        if kicked_to_login(getattr(page, "url", "")):
            out["failed_step"] = "input_spu"
            out["note"] = "登录态失效：提报页已被踢回登录页"
            await report("input_spu", False, out["note"])
            return out
        loc = await page.evaluate(_SEARCH_SPU_JS, str(spu))
        if loc == "ok":
            break
        await asyncio.sleep(0.5)
    if loc != "ok":
        out["failed_step"] = "input_spu"
        out["note"] = f"未定位到 SPU 搜索框（{loc}）"
        await report("input_spu", False, out["note"])
        return out
    try:
        await page.locator('[data-kiro-spu="1"]').fill(str(spu))
        await report("input_spu", True, f"已输入 {spu}")
        await asyncio.sleep(0.4)
        query_clicked = await page.evaluate(
            r"""() => { for (const b of document.querySelectorAll('button')) {"""
            r"""if ((b.textContent||'').replace(/\s+/g,'') === '查询') { b.click(); return true; } } return false; }"""
        )
        if not query_clicked:
            out["failed_step"] = "query"
            out["note"] = "未点到「查询」按钮"
            await report("query", False, out["note"])
            return out
    except Exception as e:
        out["failed_step"] = "query"
        out["note"] = f"搜索 SPU 异常：{e}"
        await report("query", False, out["note"])
        return out

    # 2. 目标行须唯一（防呆：搜索后结果非唯一/为 0 一律跳过，绝不误报）
    row = page.locator("tr").filter(has_text=f"SPU ID: {spu}")
    n = 0
    for _ in range(20):
        if kicked_to_login(getattr(page, "url", "")):
            out["failed_step"] = "query"
            out["note"] = "登录态失效：提报页已被踢回登录页"
            await report("query", False, out["note"])
            return out
        n = await row.count()
        if n == 1:
            break
        await asyncio.sleep(0.5)
    if n == 0:
        out["queried"] = True
        out["detail_eligible"] = False
        out["note"] = (
            "详情页查询结果为 0：该 SPU 不在本活动可报名商品列表中"
            "（列表页价格初筛通过不等于详情资格通过）"
        )
        await report("query", True, out["note"])
        return out
    if n != 1:
        out["failed_step"] = "query"
        out["note"] = f"搜索后定位行数={n}（非唯一），保守跳过"
        await report("query", False, out["note"])
        return out
    out["queried"] = True
    out["detail_eligible"] = True
    await report("query", True, "查询结果唯一")
    return out


async def enroll_activity(
    page, spu, activity, sku_prices, allow_submit=False, on_step=None,
    discount_rate=None,
) -> dict:
    """在提报页(detail-new)给某 SPU 搜索→勾选→设置场次(全选)→逐货号填申报价；allow_submit=False 停在提交前。

    sku_prices：成本表逐货号价格 [{label, daily, sale, submit_price}]（submit_price 由
    规划层按 各自日常价×折扣率 算好）。提报页价格行是平台 SKC 粒度、可能一行货号对应多行
    （实测 3 货号的商品 6 个价格行），故不按行数比对，而是逐行按【该行自身日常价】配到成本表
    货号（match_enroll_rows）后填各自申报价；页面日常价与成本表对不上、或某个货号无行覆盖，
    一律 fail-closed 不填价（报错价不可逆，报不上可重试）。单货号单行是 n=1 特例；该行读不到
    日常价时无错配对象，只校验参考价直接填。
    discount_rate 仅在超参考价时反推「建议核对的日常价」写进失败 note，可缺省。

    page 须为该活动已打开的 detail-new 提报页。实测操作序列（2026-07-17 半程试点验证）：
    实测操作序列（2026-07-17 用户逐步纠正后定稿）：
    1. 搜索：顶部 SPU ID 搜索框填 spu → 点「查询」→ 等结果行渲染（详情页默认列表不含目标 SPU）。
    2. 勾选：按视觉 y 对齐勾选商品行 checkbox（beast 左右分表，见 _CHECK_ROW_JS）。
    3. 设置场次：点「批量设置场次」→ 弹窗(MDL_outerWrapper「设置场次」) → 点「全选」→「确认」。
       场次不设 → 提交无效（这是之前全失败的根因之一）。
    4. 匹配：枚举本 SPU 的 SKU 价格行（_MARK_ENROLL_SKU_ROWS_JS）并与 sku_prices 匹配。
    5. 填价：逐行参考价校验（申报价≤参考价，全过才填）→ 逐行填申报价。
    5.5 填库存：秒杀/破冰类必填「场次共用活动库存」，按参考库存填；大促进阶类无此栏跳过。
    6. 提交：底部「提交」，仅 allow_submit=True（授权后）才点。

    安全：默认 allow_submit=False → 只走到填价、绝不提交（未提交不生效、可逆）。返回
    {located, checked, sessions_set, filled, submitted, note}。任一步失败 → 记 note 保守返回。
    """
    await dismiss_all_page_popups(page)
    result = {"located": False, "queried": False, "detail_eligible": None,
              "checked": False, "sessions_set": False,
              "filled": False, "submitted": False, "over_ref": False,
              "ref_price": None, "stock_filled": None, "failed_step": None, "note": ""}

    async def report(step, ok, note=""):
        if on_step is not None:
            await on_step({"step": step, "ok": bool(ok), "note": note})

    # 1-2. 搜索 SPU 并判资格（判定实现在 probe_detail_eligibility，识别扫描共用同一份）。
    async def probe_step(event):
        await report(event["step"], event["ok"], event["note"])

    probe = await probe_detail_eligibility(page, spu, on_step=probe_step)
    result["queried"] = probe["queried"]
    result["detail_eligible"] = probe["detail_eligible"]
    if probe["failed_step"]:
        result["failed_step"] = probe["failed_step"]
        result["note"] = probe["note"]
        return result
    if probe["detail_eligible"] is False:
        # 平台正常业务结果（列表页初筛通过 ≠ 详情有资格）：不填价、如实回报，由调用方按 info 处理。
        result["note"] = probe["note"]
        return result
    result["located"] = True

    # 勾选商品行（beast 左右分表，按 y 对齐点可见 wrapper）
    chk = await page.evaluate(_CHECK_ROW_JS)
    await asyncio.sleep(1.5)
    if chk != "ok":
        result["failed_step"] = "select_product"
        result["note"] = f"勾选商品失败（{chk}）"
        await report("select_product", False, result["note"])
        return result
    result["checked"] = True
    await report("select_product", True, "已勾选商品")

    # 3. 设置场次（全选）：不设场次提交无效
    sess = await _set_sessions_all(page)
    result["sessions_set"] = sess.get("ok", False)
    if not result["sessions_set"]:
        result["failed_step"] = "set_sessions"
        result["note"] = f"设置场次失败：{sess.get('note','')}"
        await report("set_sessions", False, result["note"])
        return result
    await report("set_sessions", True, sess.get("note", "已设置场次"))

    # 4. 枚举本 SPU 的 SKU 价格行并与成本表货号匹配（fail-closed：配不上绝不填价）。
    #    实测教训（2026-07-20）：Excel 日常价可能与商品前端实际售价不一致——参考价按前端
    #    实际售价×折扣给出，若 Excel 日常价偏高，按 Excel 算的申报价会超过参考价，提交必被
    #    平台拒。此处【不擅自改价重报】，而是【记失败、清晰上报】交人核对。
    #    多货号/多款式同理：页面日常价与成本表对不上，说明「成本表价格 ≠ 平台价格」，填价必错，保守不填。
    info = None
    for _ in range(6):
        info = await page.evaluate(_MARK_ENROLL_SKU_ROWS_JS, str(spu))
        if info and info.get("rows"):
            break
        await asyncio.sleep(0.5)
    rows = (info or {}).get("rows") or []
    n = len(sku_prices)
    if not rows:
        result["failed_step"] = "match_sku"
        result["note"] = "提报页未识别到 SKU 价格输入行"
        await report("match_sku", False, result["note"])
        return result
    unfillable = [row["idx"] for row in rows if not row.get("fillable", True)]
    if unfillable:
        # 行里所有文本输入框都已有值：可能是页面结构变了、也可能是上一轮已填过。
        # 不猜、不覆盖，如实说不填（报不上比填错字段好——库存框也在这一行里）。
        result["failed_step"] = "match_sku"
        result["note"] = (f"提报页价格行 {unfillable} 没有空的申报价输入框"
                          f"（可能已填过或页面结构变了），保守不填价"
                          f"（行文本：{rows_text_desc(rows)}）")
        await report("match_sku", False, result["note"])
        return result
    # 行数不要求等于货号数：提报页价格行按平台 SKC 列出（款式×尺码），多款式时是货号数的
    # 整数倍（实测 2026-09-24：3 货号的商品 6 个价格行）。归口判定交给 match_enroll_rows——
    # 每个页面行的日常价都必须能在成本表找到、且每个货号都至少被一行覆盖；逐行按该行自身
    # 日常价填价，配不上就一行不填（fail-closed）。
    # n==1 且页面只有一行、且读不到日常价：只有一个框一个价、无错配对象，退回只校验参考价的旧路径。
    if n == 1 and len(rows) == 1 and rows[0].get("daily") is None:
        assigned = {rows[0]["idx"]: {**sku_prices[0], "ref": rows[0].get("ref")}}
    else:
        unreadable = [row["idx"] for row in rows if row.get("daily") is None]
        if unreadable:
            result["failed_step"] = "match_sku"
            result["note"] = (f"提报页价格行 {unreadable} 读不到日常价，"
                              f"无法按日常价逐行配对，保守不填价"
                              f"（行文本：{rows_text_desc(rows)}）")
            await report("match_sku", False, result["note"])
            return result
        assigned = match_enroll_rows(sku_prices, rows)
        if assigned is None:
            result["failed_step"] = "match_sku"
            cost_desc = "、".join(f"{it['label']}({it['daily']})" for it in sku_prices)
            result["note"] = (
                f"提报页 {len(rows)} 个价格行（{match_keys_desc(rows)}）与成本表 {n} 个货号"
                f"（{cost_desc}）的货号与日常价都对不上——请核对成本表，保守不填价"
                f"（行文本：{rows_text_desc(rows)}）")
            await report("match_sku", False, result["note"])
            return result
    result["ref_price"] = rows[0].get("ref") if n == 1 else None

    # 5. 逐行参考价校验（平台硬约束：申报价不可大于参考价）。任一行超价都 all-or-nothing
    #    一行不填——部分货号填上、部分没填就提交，等于按残缺价格报名。
    #    同一格会出现在多行里（提报页按 SKC 列出，实测一个货号两行），失败原因必须按
    #    「货号 + 参考价」去重后带上行数，否则同一句话在文案里重复三四遍。
    over = []
    for row in rows:
        a = assigned[row["idx"]]
        ref = a.get("ref")
        if ref is not None and float(a["submit_price"]) > float(ref):
            over.append((a, ref))
    if over:
        result["over_ref"] = True
        result["failed_step"] = "fill_price"
        summary = summarize_over_ref(over, discount_rate, sku_prices=sku_prices)
        result["ref_price"] = summary.get("ref_price")
        result["suggested_daily_price"] = summary.get("suggested_daily_price")
        result["current_daily_price"] = summary.get("daily_price")
        result["note"] = "；".join(summary["notes"])
        await report("fill_price", False, result["note"])
        return result

    # 6. 逐行填申报价（匹配与校验全过后才动笔）
    for k, row in enumerate(rows, 1):
        a = assigned[row["idx"]]
        inp = page.locator(f'[data-enroll-sku-idx="{row["idx"]}"]').first
        try:
            await inp.click()
            await inp.fill(str(a["submit_price"]))
            # 回读校验：React 受控输入可能把我们的写入吞掉（不报错但值没进去），
            # 只捕获异常是不够的——「写了但没生效」必须当成填价失败，绝不当作已填。
            back = (await inp.input_value() or "").strip()
            if back != str(a["submit_price"]):
                result["failed_step"] = "fill_price"
                result["note"] = (f"填写第 {k} 行（货号{a['label']}）申报价未生效："
                                  f"写入 {a['submit_price']} 但读回 {back!r}")
                await report("fill_price", False, result["note"])
                return result
        except Exception as exc:
            result["failed_step"] = "fill_price"
            result["note"] = f"填写第 {k} 行（货号{a['label']}）申报价失败：{exc}"
            await report("fill_price", False, result["note"])
            return result
    result["filled"] = True
    result["sku_rows"] = [{
        "idx": row["idx"], "label": assigned[row["idx"]]["label"],
        "daily": assigned[row["idx"]]["daily"],
        "platform_daily": row.get("daily"),
        "submit_price": assigned[row["idx"]]["submit_price"],
        "ref": assigned[row["idx"]].get("ref"),
    } for row in rows]
    # 平台侧每个货号的真实日常价：后续加速器对话框按【平台日常价档】列行，要用它配对上
    # （表里的日常价可能被人工填错/过期，实测 8791757215 就差了 25 元）。
    result["platform_daily"] = {}
    for row in rows:
        value = row.get("daily")
        if value is None:
            continue
        for label in (assigned[row["idx"]].get("labels") or [assigned[row["idx"]]["label"]]):
            result["platform_daily"][label] = value
    if n == 1:
        prices_desc = str(sku_prices[0]["submit_price"])
    else:
        prices_desc = "、".join(f"{it['label']} {it['submit_price']}" for it in sku_prices)
    if len(rows) > n:
        # 提报页行多于货号数（同一货号的多个款式）：把实际填了几行一并报出来供核对。
        prices_desc += f"（提报页 {len(rows)} 个价格行）"
    await report("fill_price", True, f"已填写 {prices_desc}")

    # 5.5 填「场次共用活动库存」（2026-09-29 实测补）：秒杀/破冰类活动此栏必填——空着提交
    #    平台拦下报「采集失败」、报名记录页无记录（2879383652 全程实跑 3 个活动实锤）。
    #    库存值按平台给的参考库存填（用户定案）；大促进阶类没有此栏（平台兜底），找不到
    #    「参考库存」锚不算失败；页面上已有值时不覆盖（上轮填过/平台默认）。
    stock = await page.evaluate(_MARK_STOCK_INPUT_JS)
    if stock.get("found"):
        stock_value = str(stock["refStock"])
        inp = page.locator('[data-enroll-stock="1"]').first
        try:
            await inp.click()
            await inp.fill(stock_value)
            # 与填价同判据：React 受控输入可能吞掉写入，必须回读校验
            back = (await inp.input_value() or "").strip()
            if back != stock_value:
                result["failed_step"] = "fill_stock"
                result["note"] = f"填写活动库存未生效：写入 {stock_value} 但读回 {back!r}"
                await report("fill_stock", False, result["note"])
                return result
        except Exception as exc:
            result["failed_step"] = "fill_stock"
            result["note"] = f"填写活动库存失败：{exc}"
            await report("fill_stock", False, result["note"])
            return result
        result["stock_filled"] = True
        await report("fill_stock", True, f"已按参考库存填写活动库存 {stock_value}")
    elif stock.get("reason") == "already-filled":
        result["stock_filled"] = True
        await report("fill_stock", True, f"活动库存已有值 {stock.get('current')}，不覆盖")
    elif stock.get("reason") == "no-ref-stock":
        # 大促进阶类无「场次共用活动库存」栏，平台兜底，不算失败
        await report("fill_stock", True, "本活动无「场次共用活动库存」栏（平台兜底），跳过")
    else:
        # 有参考库存栏但找不到可填输入框：秒杀/破冰类空库存提交必败，fail-closed 不提交
        result["failed_step"] = "fill_stock"
        result["note"] = (f"找到「参考库存：{stock.get('refStock')}」但附近没有可填的"
                          f"活动库存输入框，保守不提交")
        await report("fill_stock", False, result["note"])
        return result

    if not allow_submit:
        result["note"] = (
            f"已勾选+全选场次+填申报价 {prices_desc}，待提交"
        )
        return result

    # 单条即时提交（授权后）。多 SPU 批量报名同样走「逐 SPU 填完立即提交」（调用方逐个
    # 调本函数+提交，每 SPU 一张干净提报页）——2026-10-03 起废弃「活动末统一提交」：
    # 提报页每次搜索都重渲结果表格，上一 SPU 的勾选随旧行卸载，统一提交必然丢单。
    sub = await submit_enroll_page(page, allow=True, expected_spus=[str(spu)])
    result["submitted"] = sub.get("submitted", False)
    result["note"] = f"已提交申报价 {prices_desc}" if result["submitted"] else sub.get("note", "")
    return result


_SUBMIT_FEEDBACK_JS = r"""
(canConfirm) => {
  // 词表里的「我已阅读并同意」是分层申报（破冰/阶梯价类）活动的前置协议弹窗按钮——
  // 2026-09-29 实锤：该类活动点提交后前端先弹《商品活动申报价分层申报功能说明》协议
  // （加载 PDF），不同意就根本不发报名请求；页面无 toast、无行内报错，此前三次「点了
  // 没反应」全是它。弹窗文本不含「提交/报名」，只能靠 actionWords 命中按钮来识别。
  const norm = value => (value || '').replace(/\s+/g, '').trim();
  const visible = element => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
      && style.visibility !== 'hidden';
  };
  const messageSelectors = [
    '[role=alert]', '[role=status]', '[class*=Toast]', '[class*=toast]', '[class*=Message]', '[class*=message]',
    '[class*=Notice]', '[class*=notice]', '[class*=Snackbar]', '[class*=snackbar]'
  ].join(',');
  const dialogs = [...document.querySelectorAll(
    '[role=dialog],[aria-modal=true],[class*=MDL_outerWrapper],[class*=Modal],[class*=modal],'
      + '[class*=Dialog],[class*=dialog]'
  )].filter(visible)
    // 店小秘插件注入的弹窗（class 全带 dxm- 前缀）与报名无关——它的「采集失败：」常驻
    // 文案 2026-09-29 曾把破冰提交失败的排查带偏三轮，反馈识别一律排除。
    .filter(element => !element.closest('[class*="dxm-"]'));
  const messages = [...document.querySelectorAll(messageSelectors), ...dialogs]
    .filter(visible)
    .filter(element => !element.closest('[class*="dxm-"]'))
    .map(element => (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(text => text && text.length <= 300);
  const failureWords = ['活动太火爆', '稍后再试', '提交失败', '报名失败', '操作失败',
    '系统繁忙', '网络异常', '请求失败'];
  const successWords = ['提交成功', '报名成功', '操作成功', '报名已提交'];
  const failure = messages.find(text => failureWords.some(word => norm(text).includes(norm(word))));
  if (failure) return {status: 'failure', message: failure.slice(0, 160)};
  const success = messages.find(text => successWords.some(word => norm(text).includes(norm(word))));
  if (success) return {status: 'success', message: success.slice(0, 160)};

  // 联报推荐弹窗（2026-10-02 官方大促实锤）：点「提交」后平台弹「推荐将当前活动所选商品
  // 同时报入以下活动」（联报限时包邮活动推广）。弹窗文本不含「确认/确定」、按钮文案
  // 「仅提交当前活动」也不匹配下方 actionWords 的 endsWith 规则——识别不到时提交请求
  // 根本发不出去，表现为「点了提交零请求、无任何提示」的静默失败。用户拍板的正确动作
  // 只有「仅提交当前活动」：联报 toggle 保持关闭、绝不碰「一键报名活动 (N)」——所以
  // 按钮文案必须【精确等值】匹配，endsWith/包含一旦放宽就可能误点一键报名。
  for (const dialog of dialogs) {
    const dialogText = norm(dialog.innerText);
    if (!dialogText.includes('联报') || !dialogText.includes('仅提交当前活动')) continue;
    const buttons = [...dialog.querySelectorAll('button,[role=button]')].filter(visible);
    const only = buttons.find(button => norm(button.textContent) === '仅提交当前活动');
    if (!only || only.disabled || String(only.className || '').includes('disabled')) {
      return {status: 'confirmation_blocked', message: (dialog.innerText || '').slice(0, 160)};
    }
    if (!canConfirm) return {status: 'pending', message: '联报推荐弹窗等待处理'};
    only.click();
    return {status: 'confirmation_clicked', message: '联报推荐弹窗：仅提交当前活动'};
  }

  const actionWords = ['确认提交', '确定提交', '继续提交', '仍要提交', '确认报名', '确定报名',
    '继续报名', '确认', '确定', '提交', '我已阅读并同意'];
  for (const dialog of dialogs.reverse()) {
    const text = norm(dialog.innerText);
    const buttons = [...dialog.querySelectorAll('button,[role=button]')].filter(visible);
    const action = buttons.find(button => {
      const text = norm(button.textContent);
      return actionWords.some(word => text === word || text.endsWith(word));
    });
    const relevant = Boolean(action) || ((text.includes('提交') || text.includes('报名'))
      && (text.includes('确认') || text.includes('确定')));
    if (!relevant) continue;
    if (!action || action.disabled || String(action.className || '').includes('disabled')) {
      return {status: 'confirmation_blocked', message: (dialog.innerText || '').slice(0, 160)};
    }
    if (!canConfirm) return {status: 'pending', message: '二次确认请求处理中'};
    action.click();
    return {status: 'confirmation_clicked', message: norm(action.textContent)};
  }
  return {status: 'pending', message: ''};
}
"""


# 提交后等页面跳到结果页的上限（× 0.25s）。URL 一变即返回，实测跳转在 1s 内完成；给 3s
# 兜住慢响应，等不到就按原回执返回，不为失败/不跳转的情形额外拖时间。
SUBMIT_RESULT_WAIT_TRIES = 12


async def _wait_submit_result_page(page, tries=12, expected_count=None) -> dict:
    """等待提交后的 detail-new-result 页面，读取 successCount/“已提交N个商品”。

    expected_count：本次提交【应该】提交的商品数（提报页已填价勾选的商品数）。给了它才做
    数量对账——「>0 即成功」会把部分丢失吞掉（2026-10-03 实测：8791757215 填价成功却被
    后一个 SPU 的搜索重渲冲掉勾选，结果页「已提交 1 个商品」实际是别人，旧判据照判成功）。
    count < expected 时 success 仍为 True（确实有商品提交成功）但 verified=False，note
    点名差额，交报名记录页逐 SPU 对账兜底。
    """
    for attempt in range(tries):
        url = str(getattr(page, "url", "") or "")
        if kicked_to_login(url):
            # 被踢回登录页就不可能再跳结果页了，立即退出，别白等满整个窗口。
            return {"detected": False, "success": False, "success_count": None,
                    "url": url, "note": "登录态失效：页面被踢回登录页"}
        if "detail-new-result" in url:
            url_match = re.search(r"[?&]successCount=(\d+)", url)
            url_count = int(url_match.group(1)) if url_match else None
            body_count = None
            try:
                body = await page.evaluate("() => document.body.innerText || ''")
                body_match = re.search(r"已提交\s*(\d+)\s*个商品", body or "")
                body_count = int(body_match.group(1)) if body_match else None
            except Exception:
                pass
            success_count = body_count if body_count is not None else url_count
            if success_count is not None:
                success = success_count > 0
                verified = success
                note = (f"结果页确认已提交 {success_count} 个商品"
                        if success else "结果页显示提交成功商品数为 0")
                if success and expected_count is not None and success_count < expected_count:
                    verified = False
                    note = (f"结果页显示已提交 {success_count} 个商品，低于本次填价的 "
                            f"{expected_count} 个——差 {expected_count - success_count} 个商品的"
                            f"勾选/填价在提交前丢失，须到报名记录页逐个核验")
                return {
                    "detected": True, "success": success, "verified": verified,
                    "success_count": success_count,
                    "url": url, "note": note,
                }
        if attempt + 1 < tries:
            await asyncio.sleep(0.25)
    return {"detected": False, "success": False, "success_count": None, "url": "", "note": ""}


def _submit_result_payload(result_page: dict, confirmed: bool) -> dict:
    # verified 不再等于 success（2026-10-03 起）：success 只表示「有商品提交成功」，
    # verified 还要求结果页数量不少于本次填价数（_wait_submit_result_page 已比对；
    # 没做比对的旧路径不带 verified 键，维持原口径）。
    success = bool(result_page.get("success"))
    return {
        "submitted": success,
        "verified": success and bool(result_page.get("verified", True)),
        "clicked_submit": True,
        "confirmed": confirmed,
        "result_page": True,
        "success_count": result_page.get("success_count"),
        "note": result_page.get("note", ""),
    }


# 提交前的勾选盘点（2026-10-03 起）：提报页每做一次 SPU 搜索就重渲结果表格，之前勾选的
# 商品随旧行卸载丢失（多 SPU 静默漏报的根因），故点提交前必须核对当前勾选是不是本次要报
# 的商品。beast 左右分表，勾选框与商品文字列不在同一 DOM 行，按视觉 y 对齐把勾中的勾选框
# 归到「SPU ID: n」文本行（同 _CHECK_ROW_JS 的套路）；同时带出搜索框现值与未勾的协议框，
# 供「提交按钮持续禁用」时的现场归因。
_CHECKED_ENROLL_STATE_JS = r"""
() => {
  const visible = element => {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const wrappers = [...document.querySelectorAll('[class*=CBX_squareInputWrapper]')].filter(visible);
  const isChecked = w => {
    const input = w.querySelector('input');
    return Boolean(input && input.checked) || /checked/i.test(String(w.className || ''));
  };
  const checked = wrappers.filter(isChecked);
  const spuRows = [];
  for (const el of document.querySelectorAll('td,div,span')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    const m = own.match(/SPU\s*ID[：:\s]*([0-9]+)/);
    if (m) spuRows.push({spu: m[1], y: el.getBoundingClientRect().y});
  }
  const spus = new Set();
  let unattributed = 0;
  for (const w of checked) {
    const y = w.getBoundingClientRect().y;
    let best = null;
    for (const row of spuRows) {
      const d = Math.abs(row.y - y);
      if (d < 30 && (best === null || d < best.d)) best = {spu: row.spu, d};
    }
    if (best) spus.add(best.spu); else unattributed += 1;
  }
  const searchValues = [...document.querySelectorAll('input')]
    .map(i => (i.value || '').trim()).filter(v => /^\d{6,}$/.test(v));
  const uncheckedAgreements = wrappers.filter(w => {
    if (isChecked(w)) return false;
    const label = ((w.closest('label') || w.parentElement || w).textContent || '');
    return /同意|协议|须知|承诺/.test(label);
  }).length;
  return {checked: checked.length, spus: [...spus], unattributed,
          search_values: searchValues.slice(0, 3), unchecked_agreements: uncheckedAgreements};
}
"""


async def _submit_disabled_scene(page) -> str:
    """「提交」按钮持续禁用时的页面现场摘要（best-effort，读不到的部分自动省略）。

    旧文案「本页无已填 SPU？」只是猜测；多 SPU 场景的真凶是「上一 SPU 的勾选被搜索重渲
    冲掉」，必须把当前勾选数/勾选归属 SPU/搜索框现值/未勾协议框/行内报错摆出来。
    """
    parts = []
    try:
        inv = await page.evaluate(_CHECKED_ENROLL_STATE_JS)
    except Exception:
        inv = None
    if isinstance(inv, dict):
        desc = f"当前勾选 {inv.get('checked', 0)} 行"
        spus = inv.get("spus") or []
        if spus:
            desc += f"（勾选归属 SPU：{'、'.join(map(str, spus))}）"
        if inv.get("unattributed"):
            desc += f"，{inv['unattributed']} 个勾选框未能归属到商品行"
        parts.append(desc)
        if inv.get("search_values"):
            parts.append(f"搜索框现值：{'、'.join(map(str, inv['search_values']))}")
        if inv.get("unchecked_agreements"):
            parts.append(f"有 {inv['unchecked_agreements']} 个协议/须知勾选框未勾")
    try:
        scene = await page.evaluate(_SUBMIT_SCENE_JS)
    except Exception:
        scene = ""
    if scene:
        parts.append(f"行内提示：{scene}")
    return "；".join(parts) or "页面现场读取失败"


async def _submit_enroll_page_once(page, allow=False, feedback_tries=20, expected_spus=None) -> dict:
    """点一次「提交」并按页面反馈判定结果。

    expected_spus：本次提交【应该】提交的 SPU 列表（提报页已填价勾选的商品）。给定时点
    提交前先核对页面勾选：勾选数=0 或能确认期望 SPU 的勾选已丢失 → 不照点、报错点名
    （提报页搜索重渲会清掉之前的勾选，照点等于把残缺提交发出去；提交不可逆）。

    不含提交后的结果页收尾等待——那一步在 submit_enroll_page 里统一做（本函数的所有返回
    路径都可能带着「跳转还没走完」的页面出去）。
    """
    btn = page.get_by_role("button", name="提交").first
    if not await btn.count():
        return {"submitted": False, "note": "未找到「提交」按钮"}
    if not allow:
        return {"submitted": False, "note": "半程：已定位「提交」按钮，未点击（allow=False）"}
    # 提交前勾选核对（2026-10-03 起）：勾选丢失是静默的（页面无提示），不核对就会把
    # 「只剩最后一个 SPU」的残缺提交发出去。
    if expected_spus:
        try:
            inv = await page.evaluate(_CHECKED_ENROLL_STATE_JS)
        except Exception:
            inv = None  # 读不到页面不擅自阻断：后面的 disabled 判定与点击异常自带护栏
        if isinstance(inv, dict):
            checked_spus = {str(s) for s in (inv.get("spus") or [])}
            missing = [str(s) for s in expected_spus if str(s) not in checked_spus]
            if not inv.get("checked"):
                return {
                    "submitted": False, "clicked_submit": False,
                    "note": (f"提交前勾选核对失败：页面当前勾选数=0，本次要报的 "
                             f"{'、'.join(map(str, expected_spus))} 的勾选已丢失"
                             f"（提报页搜索重渲会清掉勾选），不照点提交"),
                }
            # 勾选框与 SPU 文字行对不齐时（unattributed>0）退化为「有勾选」判据——
            # 归不了属就无法证明丢失，不拦；只有全部可归因且确实缺期望 SPU 才拦。
            if missing and not inv.get("unattributed"):
                return {
                    "submitted": False, "clicked_submit": False,
                    "note": (f"提交前勾选核对失败：页面勾选的是 "
                             f"{'、'.join(sorted(checked_spus)) or '未识别'}，缺 "
                             f"{'、'.join(missing)} 的勾选（疑似被后续搜索重渲冲掉），不照点提交"),
                }
    # 「提交」按钮 disabled 时（本页无已勾选/已填 SPU）不可点——旧代码硬点会等满 30s 超时
    # 抛异常，冒泡中断整个执行遍（含阶段三重开流量）。故先判 disabled，禁用即优雅返回。
    # 2026-10-03 加固：命中后有界等待约 8s 排除「页面重渲中」的瞬时禁用；仍禁用则抓页面
    # 现场（勾选数/勾选归属/搜索框现值/未勾协议框/行内报错）写进 note，不再只写猜测。
    try:
        disabled = await btn.is_disabled()
        for _ in range(16):
            if not disabled:
                break
            await asyncio.sleep(0.5)
            disabled = await btn.is_disabled()
        if disabled:
            scene = await _submit_disabled_scene(page)
            return {
                "submitted": False, "clicked_submit": False, "scene": scene,
                "note": f"「提交」按钮持续禁用（等约 8s 未恢复），跳过：{scene}",
            }
    except Exception:
        pass
    # 点提交 + 反馈轮询最多两轮：分层申报（破冰/阶梯价类）首次提交会先弹《功能说明》
    # 协议，点「我已阅读并同意」只是接受协议、平台【不会自动续提交】（2026-09-30 实测：
    # confirmed=True 但零报名请求，再点一次才发出 /marketing/enroll/semi/submit 并收录）。
    # 故 confirmed 后轮询仍无结论时，等弹窗散尽重新定位按钮再点一次。
    confirmed = False
    last_message = ""
    expected_count = len(expected_spus) if expected_spus else None
    for submit_attempt in range(2):
        if submit_attempt > 0:
            await asyncio.sleep(1.0)
            btn = page.get_by_role("button", name="提交").first
            if not await btn.count():
                break
        try:
            await btn.click(timeout=8000)
        except Exception as e:
            if submit_attempt > 0:
                return {
                    "submitted": True, "verified": False, "clicked_submit": True,
                    "confirmed": confirmed,
                    "note": f"已确认弹窗但重点「提交」失败：{str(e)[:60]}，待报名记录页最终核验",
                }
            return {"submitted": False, "note": f"点「提交」失败：{str(e)[:80]}"}
        blocked_polls = 0
        for _ in range(feedback_tries):
            await asyncio.sleep(0.5)
            if kicked_to_login(getattr(page, "url", "")):
                # 点已点下但登录态被踢：提交请求是否被平台收录不确定，口径与 navigated
                # 分支一致（submitted=True / verified=False），附明确原因，别再空等。
                return {
                    "submitted": True, "verified": False, "clicked_submit": True,
                    "confirmed": confirmed, "login_lost": True,
                    "note": "提交后页面被踢回登录页：登录态已失效，本次提交结果须到报名记录页核验",
                }
            if "detail-new-result" in str(getattr(page, "url", "") or ""):
                result_page = await _wait_submit_result_page(page, tries=4,
                                                             expected_count=expected_count)
                if result_page.get("detected"):
                    return _submit_result_payload(result_page, confirmed)
            try:
                raw_feedback = await page.evaluate(_SUBMIT_FEEDBACK_JS, not confirmed)
            except Exception as exc:
                message = str(exc)
                if "Execution context was destroyed" in message or "navigat" in message.lower():
                    result_page = await _wait_submit_result_page(page, expected_count=expected_count)
                    if result_page.get("detected"):
                        return _submit_result_payload(result_page, confirmed)
                    return {
                        "submitted": True, "verified": False, "clicked_submit": True,
                        "confirmed": confirmed, "navigated": True,
                        "note": "点击提交后页面发生跳转，待报名记录页最终核验",
                    }
                return {
                    "submitted": True, "verified": False, "clicked_submit": True,
                    "confirmed": confirmed, "feedback_error": message[:120],
                    "note": f"点击提交后读取结果异常，待报名记录页最终核验：{message[:80]}",
                }
            feedback = raw_feedback if isinstance(raw_feedback, dict) else {}
            status = feedback.get("status", "pending")
            last_message = feedback.get("message") or last_message
            if status == "confirmation_clicked":
                confirmed = True
                blocked_polls = 0
                continue
            if status == "success":
                return {
                    "submitted": True, "verified": True, "clicked_submit": True,
                    "confirmed": confirmed, "note": last_message or "报名成功",
                }
            if status == "failure":
                return {
                    "submitted": False, "verified": False, "clicked_submit": True,
                    "confirmed": confirmed, "note": last_message or "提交失败",
                }
            if status == "confirmation_blocked":
                blocked_polls += 1
                if blocked_polls >= 4:
                    return {
                        "submitted": False, "verified": False, "clicked_submit": True,
                        "confirmed": False,
                        "note": f"出现二次确认弹窗但无法点击确认：{last_message or '未识别确认按钮'}",
                    }
            else:
                blocked_polls = 0
        # 本轮轮询无结论：点过二次确认（协议类弹窗）就再点一轮提交——平台不自动续提交；
        # 没点过确认说明弹窗/提示压根没出现，重点无意义，落尾部抓现场。
        if not confirmed:
            break
    # 点了提交却既没弹二次确认、也没抓到成功/失败提示（实测 2026-09-25：平台侧最终 0 条记录）。
    # 这时页面往往有行内报错（资格/站点/库存/价格），但不在 toast 里 → 抓一次现场写进 note，
    # 否则只剩「已点击提交」这句没法定位原因。
    scene = ""
    try:
        scene = await page.evaluate(_SUBMIT_SCENE_JS)
    except Exception:
        scene = ""
    tail = f"（提交后页面现场：{scene}）" if scene else ""
    return {
        "submitted": True, "verified": False, "clicked_submit": True,
        "confirmed": confirmed, "scene": scene,
        "note": ("已点击提交并确认，未捕获明确结果提示" if confirmed
                 else "已点击提交，等待期间未出现二次确认或明确结果提示") + tail,
    }


async def submit_enroll_page(page, allow=False, feedback_tries=20, expected_spus=None) -> dict:
    """点提报页底部「提交」，提交本页已勾选+填价的商品（逐 SPU 报名时 expected_spus=[该SPU]）。

    allow=False（默认）只定位提交按钮、不点击（半程、可逆）。allow=True 才真提交（不可逆，
    授权后）。提交后短轮询等待可能延迟出现的二次确认弹窗，只在弹窗内部点击；同时捕获
    成功/失败提示。返回 {submitted, verified, note}，其中 submitted 仅表示提交动作已被页面接受，
    verified=True 才表示本函数捕获到明确成功提示。

    expected_spus（2026-10-03 起）：本次应提交的 SPU 列表。给定时两道额外闸门生效——
    ① 点提交前核对页面勾选（提报页搜索重渲会清掉之前的勾选，不一致就不照点）；
    ② 结果页 successCount 与应提交数对账（少了判 verified=False 并点名差额），替换旧的
    「successCount>0 即成功」（它会把多 SPU 场景的部分丢失吞掉）。

    返回前多一道收尾：等页面跳到 detail-new-result 结果页（「已提交 N 个商品」）。用户
    2026-09-30 要求——提交报名后不能直接关页签，必须等跳转到结果页，再开新页签报下一个
    活动。SPA 的提交是异步收尾的：成功提示先到、跳转后到，拿到提示就返回会让调用方立刻关
    页签、把这次跳转打断。等到了就用结果页当回执（successCount 比 toast 硬——0 就是平台
    当刻没收录），等不到（明确失败/压根不跳转）原样返回，不额外拖时间。
    """
    expected_count = len(expected_spus) if expected_spus else None
    result = await _submit_enroll_page_once(page, allow=allow, feedback_tries=feedback_tries,
                                            expected_spus=expected_spus)
    # submitted=False 的这些情形没有跳转可等：未找到按钮/按钮禁用/半程没点/明确失败提示/
    # 确认弹窗点不动。result_page=True 则已经是结果页回执。
    if result.get("result_page") or not result.get("submitted"):
        return result
    detected = await _wait_submit_result_page(page, tries=SUBMIT_RESULT_WAIT_TRIES,
                                              expected_count=expected_count)
    if not detected.get("detected"):
        return result
    return _submit_result_payload(detected, bool(result.get("confirmed")))


# 提交后没回执时抓页面现场：URL + 含「报错关键词」的短句（行内校验/资格提示多半长这样）。
# 店小秘弹窗（dxm-* class）整棵子树跳过：它的「采集失败：」常驻文案会被关键词命中，
# 与报名成败无关（2026-09-29 实测被它误导，把协议弹窗根因盖了三轮）。
_SUBMIT_SCENE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const keys = /失败|错误|异常|不可|不能|已结束|已过期|库存不足|不符合|无资格|不支持|超出|请选择|请填写|请设置/;
  const hits = [];
  for (const el of document.querySelectorAll('*')) {
    if (el.closest('[class*="dxm-"]')) continue;
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    const t = norm(own);
    if (!t || t.length > 60 || !keys.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (hits.indexOf(t) < 0) hits.push(t);
    if (hits.length >= 4) break;
  }
  return (hits.join(' / ') + ' @ ' + location.pathname).slice(0, 200);
}
"""


# 在开启弹窗里选指定档卡片（普通/高级/超级；2026-09-29 起档位按成本表「折扣」列定，
# 不再是写死超级档）。参数 args=[word, pos]：word=档名文字（"普通"/"高级"/"超级"），
# pos=卡片从左到右的位置序号（0/1/2，对应 ACCEL_TIER_POS），用于文字认不出时的位置兜底。
# 注意必须是单参数解构：Playwright page.evaluate 只把第二个实参整体传给第一个形参，
# 写成 (word, pos) => 双形参会让 pos=undefined、位置兜底取到 cards[NaN] 直接抛错
# （2026-09-29 半程实测踩到，单测桩签名宽松没拦住）。
_SELECT_TIER_JS = r"""
(args) => {
  const [word, pos] = args;
  for (const el of document.querySelectorAll('*')) {
    const t = (el.innerText || '').replace(/\s+/g, '');
    if (t.includes(word) && t.includes('流量加权') && t.length < 40) {
      let n = el;
      for (let i = 0; i < 4 && n; i++) {
        if (n.offsetWidth > 80) { n.click(); return true; }
        n = n.parentElement;
      }
      el.click();
      return true;
    }
  }
  // 2026-07-20 页面把「普通/高级/超级」画进卡片背景图，DOM innerText 只剩
  // 「让价/对应申报价格」，上面的文字匹配会必然失败。三张可选档位按普通→高级→超级
  // 从左到右排列；只收集同时含两列价格、可见且 cursor:pointer 的独立卡片，按 x 排序取第 pos 张。
  const cards = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('*')) {
    const own = [...el.childNodes]
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent)
      .join('')
      .replace(/\s+/g, '');
    if (!own.includes('对应申报价格')) continue;
    let node = el;
    for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
      const rect = node.getBoundingClientRect();
      const text = (node.innerText || '').replace(/\s+/g, '');
      if (getComputedStyle(node).cursor === 'pointer'
          && rect.width > 100 && rect.width < 400 && rect.height > 150
          && text.includes('让价') && text.includes('对应申报价格')) {
        if (!seen.has(node)) {
          seen.add(node);
          cards.push({node, x: rect.x});
        }
        break;
      }
    }
  }
  if (cards.length >= 3) {
    cards.sort((a, b) => a.x - b.x);
    cards[Math.min(pos, cards.length - 1)].node.click();
    return true;
  }
  return false;
}
"""


async def _select_tier(page, tier: str, tries=20) -> bool:
    """轮询等待档位卡异步渲染后选指定档（normal/advanced/super），避免抽屉已开但卡片尚未挂载的偶发空读。"""
    args = [ACCEL_TIER_NAMES[tier], ACCEL_TIER_POS[tier]]
    for _ in range(tries):
        if await page.evaluate(_SELECT_TIER_JS, args):
            return True
        await asyncio.sleep(0.5)
    return False

# 在「调整申报价」对话框里，给每个含「参考申报价格」的行的可填输入框打 data-accel-idx 标记，
# 返回 [{idx, ref, daily, text, current}]。ref=该行参考申报价格上限；daily=该行所属【日常价档】
# （= 参考申报价格 + 让价，实测 30.27+44.45=74.72、168.67+20.21=188.88，这是行的身份键，
# 因为对话框按日常价档列行、不是按货号）；text=行容器文本、current=输入框当前值。
_MARK_PRICE_INPUTS_JS = r"""
() => {
  const rows = [];
  const seen = new Set();
  let idx = 0;
  for (const el of document.querySelectorAll('*')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    if (!own.includes('参考申报价格')) continue;
    let r = el;
    for (let i = 0; i < 8 && r; i++) {
      const ins = [...r.querySelectorAll('input')]
        .filter(x => (x.type === 'text' || !x.type) && !x.disabled);
      if (ins.length) {
        const key = (r.innerText || '').slice(0, 80);
        if (seen.has(key)) break;
        seen.add(key);
        const text = r.innerText || '';
        const mRef = text.match(/参考申报价格[：:]\s*¥?\s*([\d.]+)/);
        const mCut = text.match(/让价\s*¥?\s*([\d.]+)/);
        const ref = mRef ? Number(mRef[1]) : null;
        const cut = mCut ? Number(mCut[1]) : null;
        ins[0].setAttribute('data-accel-idx', String(idx));
        rows.push({ idx, ref: ref,
                    daily: (ref !== null && cut !== null) ? Math.round((ref + cut) * 100) / 100 : null,
                    text: (r.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
                    current: ins[0].value || '' });
        idx++;
        break;
      }
      r = r.parentElement;
    }
  }
  return rows;
}
"""


async def _set_accel_prices(page, accel_prices) -> dict:
    """在「调整申报价」对话框逐行填加速价（传入价须 ≤ 每行参考价，超上限的档退填底价+1）。

    accel_prices: [{label, daily, sale, price}]（逐货号传入，price 由调用方算好=底价+1 与
    最低折扣价÷0.9 取高；内部按日常价档合并，见 accel_price_groups）。对话框按日常价档列行，
    行身份键是「参考申报价格+让价」= 日常价；行数不等于档数 → count_mismatch；某行对不上档
    → match_failed；两种情况调用方都中止不开（给错价比不开严重得多）。

    逐行超参考价分两种：传入价（保活动价）超上限但「档内最高底价+1」能填进 → 退填底价+1
    并计入 degraded（用户 2026-09-28 定「接收活动失效」，如实注明，不中止）；连底价+1 都
    超上限 → 计入 over，调用方整单中止不开（不冒险填低价）。
    """
    rows = await page.evaluate(_MARK_PRICE_INPUTS_JS)
    base = {"rows_total": len(rows or []), "rows_filled": 0, "rows_over": 0,
            "over_detail": [], "rows_degraded": 0, "degraded_detail": [],
            "match": "ok",
            # 行文本摘要随结果返回：匹配失败时写进 note，人工能直接看到对话框里是什么行。
            "rows_desc": rows_text_desc(rows or [], limit=400)}
    if not rows:
        return base
    if len(rows) != len(accel_price_groups(accel_prices)):
        return {**base, "match": "count_mismatch"}
    pairs = match_accel_rows(accel_prices, rows)
    if pairs is None:
        return {**base, "match": "match_failed"}
    filled = 0
    over = []
    degraded = []
    for row in rows:
        group = pairs[row["idx"]]
        price = group["price"]
        ref = row.get("ref")
        label = "、".join(str(x) for x in group["labels"])
        if ref is not None and float(price) > float(ref):
            floor_price = group.get("floor")
            if floor_price is not None and float(floor_price) <= float(ref):
                # 保活动所需价超该行上限：退填「档内最高底价+1」（用户 2026-09-28 定
                # 「接收活动失效」）——加速价没达到申报价÷0.9，如实注明活动将失效，照开。
                degraded.append({"idx": row["idx"], "label": label,
                                 "daily": group["daily"], "price": price,
                                 "filled": floor_price, "ref": ref})
                price = floor_price
            else:
                # 该档的平台上限低于「最高底价+1」：这一档填不进合底价的价 → 整单中止（用户
                # 2026-09-25 定：不冒险填低价，如实报「上限 < 底价」）。
                over.append({"idx": row["idx"], "label": label,
                             "daily": group["daily"], "price": price, "ref": ref,
                             "reason": "档上限低于该档最高底价+1"})
                continue
        inp = page.locator(f'[data-accel-idx="{row["idx"]}"]').first
        try:
            await inp.click()
            await inp.fill(str(price))
            back = (await inp.input_value() or "").strip()
            if back != str(price):
                over.append({"idx": row["idx"], "label": label, "daily": group["daily"],
                             "ref": ref, "err": f"回读 {back!r} 与写入不一致"})
                continue
            filled += 1
        except Exception as e:
            over.append({"idx": row["idx"], "label": label, "daily": group["daily"],
                         "ref": ref, "err": str(e)[:60]})
    return {**base, "rows_filled": filled, "rows_over": len(over),
            "over_detail": over, "rows_degraded": len(degraded),
            "degraded_detail": degraded}


async def _open_accel_once(page, spu, allow=False, accel_prices=None, tier="super") -> dict:
    """单次尝试（重新）开启某 SPU 的流量加速。

    首要前提——限流校验（用户 2026-09-29 定）：正常品的加速器列是「该商品已获得开启流量
    加速器机会」+「立即开启」；限流品显示「商品流量待关注 / 您可加速提效」，入口只剩
    「报名流量加速器 / 调价提效」。限流品按规则停止流量加速动作、只做活动报名（返回
    throttled=True）。所以入口只认「立即开启」，找不到时按行文本判限流，绝不点
    「报名流量加速器」。

    tier（档位）由调用方按成本表「折扣」列定（accel_tier_for_discount，用户 2026-09-29）：
    - "normal"/"advanced"（85折→普通、其余高位折扣→高级）：平台给默认申报价、卡片上
      没有自定义价入口（实测 2026-09-29 只有超级档有「去获取」），选卡后直接收尾，
      accel_prices 用不上。
    - "super"（75折及以下）：完整路径——立即开启→选超级流量加权→「去获取」→「调整申报价」
      对话框【逐货号】填加速价(=各自底价+1 与最低折扣价÷0.9 取高)→确认→(授权后)立即加速。
      accel_prices: [{label, daily, sale, price}]；=None 退回旧简单路径（只点开启确认，
      兼容老调用/半程探测）。

    accel_prices 多货号时对话框行必须先按货号 label 归属到成本表货号（match_accel_rows，
    一行货号可对应多行）；行数少于货号数、或有行归属不到任何货号 → 中止不开（fail-closed：
    同日常价的货号底价可以不同，瞎填就是把高底价货号按低价卖，不可逆）。

    allow=False（默认）只走到最终「立即加速」前不点（半程、可逆）。返回 {state, opened,
    note, price_set, tier}。若该 SPU 已加速（on）→ opened=True(no-op)。

    用户规则 2026-07-17：加速器一开，前端显示价以加速价为准，故加速价须设为 Excel 底价+1；
    加速价不得高于该品「参考申报价格」（超参考价则该行填不进、须告警）。
    真开启不可逆（实测锁 24h 才能手动停），故 allow=False 时不点「立即加速」。
    """
    await dismiss_all_page_popups(page)
    result = {
        "state": "unknown", "precheck_state": "unknown",
        "opened": False, "note": "", "price_set": None, "tier": tier,
    }
    state = await read_accel_state(page, spu, search=True)
    result["state"] = state
    result["precheck_state"] = state
    if state == "on":
        result["opened"] = True
        result["note"] = "本就在加速中，无需开启（no-op）"
        return result
    if state != "off":
        result["note"] = f"加速态={state}，保守不操作"
        return result

    # 限流校验：先找「立即开启」；找不到再看行文本是不是限流现场。
    # 带上加速器列的现场文本：平台限流和页面结构变了是两回事，只报「未定位到」操作者分不出来。
    marked = await page.evaluate(_MARK_ROW_ACTION_JS, [str(spu), "立即开启"])
    if not marked:
        row_text = await page.evaluate(_ROW_TEXT_JS, str(spu))
        if any(w in row_text for w in ACCEL_THROTTLE_WORDS):
            result["throttled"] = True
            result["note"] = (f"商品限流（加速器列：{row_text}），按规则停止流量加速动作、"
                              f"只做活动报名")
            return result
        result["note"] = ("未定位到该行的加速器入口「立即开启」"
                          + (f"（加速器列：{row_text}）" if row_text else "（也没读到该 SPU 的行）"))
        return result
    result["entry_word"] = "立即开启"

    # 普通/高级档：平台默认申报价、无自定义价入口（实测 2026-09-29 只有超级档卡片有
    # 「去获取」），选卡后直接收尾。半程也走完选卡——档位选择器是否在真实页面生效，
    # 正是半程要验证的东西。
    if tier in ("normal", "advanced"):
        tier_name = ACCEL_TIER_NAMES[tier]
        if not await _click_marked(page):
            result["note"] = "「立即开启」点击失败"
            return result
        await asyncio.sleep(2.5)
        if not await _select_tier(page, tier):
            result["note"] = f"未找到「{tier_name}流量加权」档卡片"
            return result
        await asyncio.sleep(1.2)
        if not allow:
            result["note"] = (f"半程：已选{tier_name}流量加权档（平台默认申报价，无自定义价入口），"
                              f"未点「立即加速」（allow=False）")
            return result
        return await _finalize_accel_open(page, spu, result, f"{tier_name}档平台默认申报价")

    # 旧路径：未给 accel_prices → 只点开启确认（兼容老调用/半程探测）。
    if accel_prices is None:
        if not allow:
            result["note"] = "半程：已定位「立即开启」，未点击（allow=False）"
            return result
        if not await _click_marked(page):
            result["note"] = "「立即开启」点击失败"
            return result
        await asyncio.sleep(2)
        for word in ("确定", "确认", "立即开启", "开启"):
            btn = page.get_by_role("button", name=word).first
            if await btn.count():
                try:
                    await btn.click()
                    break
                except Exception:
                    pass
        await asyncio.sleep(1.5)
        result["clicked_open"] = True
        result["opened"] = await verify_state(page, spu, "on")
        result["note"] = ("已开启流量加速（已校验）" if result["opened"]
                          else "已点立即开启；状态未即时校验到（页面慢/平台回显延迟）")
        return result

    # 完整路径：立即开启 → 选超级档 → 去获取 → 调价对话框填价 → 确认 → (授权后)立即加速。
    if not await _click_marked(page):
        result["note"] = "「立即开启」点击失败"
        return result
    await asyncio.sleep(2.5)
    if not await _select_tier(page, "super"):
        result["note"] = "未找到「超级流量加权」档卡片"
        return result
    await asyncio.sleep(1.2)
    # 点「去获取」（继续让价获取更多流量）打开「调整申报价」对话框
    got = await page.evaluate(
        r"""() => {for (const el of document.querySelectorAll('a,button,span,[role=button]')){"""
        r"""if ((el.textContent||'').replace(/\s+/g,'')==='去获取'){el.click();return true;}}return false;}"""
    )
    if not got:
        result["note"] = "未找到「去获取」入口（继续让价）"
        return result
    await asyncio.sleep(2.5)

    price = await _set_accel_prices(page, accel_prices)
    result["price_set"] = price
    if price["rows_total"] == 0:
        result["note"] = "「调整申报价」对话框未识别到可填行"
        return result
    if price.get("match") == "count_mismatch":
        # 对话框按【日常价档】列行，档数对不上说明成本表与平台的价格结构不一致 → 中止不开。
        await _abort_accel_dialog(page)
        result["match_failed"] = True
        result["note"] = (f"「调整申报价」有 {price['rows_total']} 行，与成本表的 "
                          f"{len(accel_price_groups(accel_prices))} 个日常价档对不上，"
                          f"已中止不开（请核对成本表价格）")
        return result
    if price.get("match") == "match_failed":
        # 有行的日常价（参考申报价格+让价）对不上任何一档 → 无法确定填哪个价，中止不开。
        await _abort_accel_dialog(page)
        result["match_failed"] = True
        result["note"] = (f"「调整申报价」{price['rows_total']} 行无法按日常价对上成本表的价格档"
                          f"（对话框行：{price.get('rows_desc') or '无文本'}），"
                          f"已中止不开——请人工核对")
        return result
    if price["rows_over"] > 0:
        # 有档的上限低于「该档最高底价+1」→ 填不进合底价的价，保守中止，不开（避免开成
        # 默认价或低于底价的价）。取消退出。
        await _abort_accel_dialog(page)
        detail = "；".join(
            f"日常价 {o.get('daily')} 档（{o.get('label')}）上限 {o.get('ref')} "
            f"低于该档最高底价+1={o.get('price')}" if o.get("reason") else
            f"日常价 {o.get('daily')} 档（{o.get('label')}）填价失败：{o.get('err')}"
            for o in price["over_detail"])
        result["note"] = f"加速价按日常价档校验后中止不开：{detail}"
        return result
    # 确认调价对话框
    await _click_first_button(page, ("确认", "确定"))
    await asyncio.sleep(1.5)

    # 加速价按【日常价档】算（同档多货号取最高传入价），对话框也是一档一行，故这里报档数。
    groups = accel_price_groups(accel_prices)
    price_desc = (f"{next(iter(groups.values()))['price']}" if len(groups) == 1
                  else f"{len(groups)} 个价格档同档取最高价")
    if price.get("rows_degraded"):
        # 有档保活动价超上限、退填了底价+1：活动将失效（用户 2026-09-28 定「接收活动
        # 失效」），note 里必须点名哪一档、保活动需多少、退填成多少，操作者才知道哪个活动
        # 保不住——笼统一句「活动可能失效」排查时要翻对话框。
        price_desc += "；" + "；".join(
            f"档{o.get('daily')}（{o.get('label')}）按活动价需 {o.get('price')}、超上限 "
            f"{o.get('ref')}，已退填底价+1={o.get('filled')}（活动将失效）"
            for o in price["degraded_detail"])
    if not allow:
        result["note"] = (f"半程：已选超级档、填加速价 {price_desc}（{price['rows_filled']} 个 SKC）"
                          f"并确认，未点「立即加速」（allow=False）")
        return result
    return await _finalize_accel_open(
        page, spu, result, f"{price_desc}（{price['rows_filled']} 个 SKC）")


async def _finalize_accel_open(page, spu, result, price_desc) -> dict:
    """授权（allow=True）后点「立即加速」并按「平台受理 + 回查状态」收尾，三档共用。

    price_desc 仅用于写 note：普通/高级档=「平台默认申报价」，超级档=实际填的加速价。

    最终判据：平台受理（成功提示）+ 回查状态。【实测 2026-09-25 的时序】
      18:29 点「立即加速」→ 成功提示「流量加速成功…」；18:30~18:50 回查该 SPU 仍是
      「开启流量加速器 / 立即开启」（状态 off）；19:0x 再看已是「流量加速中」（状态 on，
      「查看效果」入口也出现了）——**平台生效有延迟（约半小时）**。
    所以：状态读不到「加速中」不能立刻判未开（那会触发外层整轮重试、反复点「立即加速」），
    但要如实把「平台已受理、状态尚未生效」写进 note，让人知道这一格还没落地。
    只有「平台没给成功提示 + 回查也非加速中」才是真的没开成。
    """
    feedback = await _click_open_with_busy_retries(page)
    result["final_clicks"] = feedback["clicks"]
    result["submit_feedback"] = feedback
    result["clicked_open"] = feedback["clicks"] > 0
    if not result["clicked_open"]:
        result["note"] = "未点到最终「立即加速」按钮"
        return result
    state_after = "unknown"
    for attempt in range(3):
        state_after = await read_accel_state(page, spu, search=True)
        if state_after == "on":
            break
        if attempt < 2:
            await asyncio.sleep(5)
    result["row_state_snapshot"] = state_after
    accepted = feedback["status"] == "success"
    if state_after == "on":
        result["opened"] = True
        tail = f"成功提示：{feedback['message']}" if feedback.get("message") else "回查确认"
        result["note"] = (f"已开启加速、加速价设为 {price_desc}"
                          f"（回查流量页确认状态=加速中；{tail}）")
    elif accepted:
        result["opened"] = True
        result["note"] = (f"已点「立即加速」并收到平台成功提示（{feedback['message']}），"
                          f"但回查该 SPU 尚未显示加速中——平台生效有延迟（实测约半小时），"
                          f"已按受理记录；加速价已设为 {price_desc}，"
                          f"在生效前该商品的活动申报上限仍按加速价算")
    else:
        result["opened"] = False
        result["note"] = (f"点「立即加速」{feedback['clicks']} 次"
                          f"（{feedback.get('message') or feedback.get('status')}），"
                          f"且回查该 SPU 非加速中，已如实记为【未开启】；"
                          f"加速价已设为 {price_desc}")
    return result


async def open_accel(page, spu, allow=False, accel_prices=None, tier="super", tries=3) -> dict:
    """开启流量加速，外层「开启→回查状态确认」重试，最多 tries 次（用户要求 2026-07-24）。

    单次执行（`_open_accel_once`）内部已有两级判定：点「立即加速」后先等成功 toast，toast 抓不到
    （unknown）时回查一次流量页状态兜底。本外层再包一层：一轮结束仍未确认开启成功（opened=False）
    时，等状态回显后【重来一轮】，直到成功或用尽 tries 次。例外两类确定性失败直接返回：
    match_failed（价格行数/货号匹配不上）与 throttled（商品限流）——前者页面结构与成本表对不上，
    后者是平台状态，重试都只是反复开关页面。

    安全前提（关键）：每轮 `_open_accel_once` 开头都 `read_accel_state(search=True)` 按 SPU 回查
    状态，读到 on 即 no-op 直接判成功——所以「上一轮其实已开成、只是没抓到 toast」时，下一轮
    precheck 会读到 on 并成功返回，绝不会重复点「立即开启」（开流量不可逆、锁 24h）。故这里的
    重试对已开成的品是幂等的。busy（平台火爆）的按钮级重试仍在 `_click_open_with_busy_retries`
    里，与本外层的整轮重试是两个层级：前者只重点按钮，后者从 precheck 重走整套开启流程。

    半程（allow=False）不真开、opened 恒为 False，整轮重试无意义且徒增页面查询，故只跑一次。
    """
    if not allow:
        return await _open_accel_once(page, spu, allow=allow, accel_prices=accel_prices, tier=tier)
    last = None
    for attempt in range(1, tries + 1):
        result = await _open_accel_once(page, spu, allow=allow, accel_prices=accel_prices, tier=tier)
        result["open_attempts"] = attempt
        if result.get("opened"):
            if attempt > 1:
                # 前几轮未确认、这轮成功：把尝试次数并进 note，便于事后判读是靠重试救回来的。
                result["note"] = f"第 {attempt} 次尝试确认开启成功；{result.get('note', '')}"
            return result
        last = result
        if result.get("match_failed") or result.get("throttled"):
            return last
        if attempt < tries:
            logger.warning(
                f"[流量] SPU={spu} 第 {attempt} 次开启未确认成功，等待状态回显后重试："
                f"{result.get('note', '')}"
            )
            await asyncio.sleep(2)
    if last is not None:
        last["note"] = f"重试 {tries} 次仍未确认开启成功；{last.get('note', '')}"
    return last if last is not None else {
        "state": "unknown", "precheck_state": "unknown", "opened": False,
        "open_attempts": 0, "note": "开启未执行",
    }


async def _wait_accel_submit_feedback(page, tries=32) -> dict:
    """轮询可见 toast：成功提示是开启成功的权威判据，火爆提示是重试的唯一依据。"""
    for _ in range(tries):
        feedback = await page.evaluate(r"""() => {
          const selectors = [
            '[class*=Toast]', '[class*=toast]', '[class*=Message]', '[class*=message]',
            '[role=alert]', '[class*=Notice]', '[class*=notice]'
          ];
          const texts = [];
          for (const element of document.querySelectorAll(selectors.join(','))) {
            const rect = element.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
            if (text) texts.push(text);
          }
          for (const text of texts) {
            if (text.includes('活动太火爆') && text.includes('稍后再试')) {
              return {status: 'busy', message: text.slice(0, 160)};
            }
          }
          for (const text of texts) {
            if (text.includes('成功') && (text.includes('流量') || text.includes('加速') || text.includes('开启'))) {
              return {status: 'success', message: text.slice(0, 160)};
            }
          }
          return {status: 'pending', message: ''};
        }""")
        if feedback.get("status") in {"success", "busy"}:
            return feedback
        await asyncio.sleep(0.25)
    return {"status": "unknown", "message": "等待流量加速结果提示超时"}


async def _click_open_with_busy_retries(page, tries=3) -> dict:
    """同一面板内点最终按钮；成功提示立即停，只有“活动太火爆”才继续重试。"""
    attempts = []
    for attempt in range(1, tries + 1):
        clicked = await _click_first_button(page, ("立即加速",))
        if not clicked:
            break
        feedback = await _wait_accel_submit_feedback(page)
        if feedback["status"] == "unknown":
            await _click_first_button(page, ("确定", "确认"))
            feedback = await _wait_accel_submit_feedback(page, tries=8)
        attempts.append({"attempt": attempt, **feedback})
        if feedback["status"] == "success":
            return {"clicks": attempt, **feedback, "attempts": attempts}
        if feedback["status"] != "busy":
            return {"clicks": attempt, **feedback, "attempts": attempts}
        if attempt < tries:
            logger.warning(
                f"[活动] 捕获平台火爆提示，继续第{attempt + 1}次点击：{feedback['message']}"
            )
    last = attempts[-1] if attempts else {"status": "no_button", "message": "未点到立即加速"}
    return {"clicks": len(attempts), "status": last["status"],
            "message": last["message"], "attempts": attempts}


async def _click_first_button(page, names) -> bool:
    """按文案顺序点第一个存在的按钮（对话框确认/提交用）。点到即返回 True。"""
    for w in names:
        btn = page.get_by_role("button", name=w).first
        if await btn.count():
            try:
                await btn.click()
                return True
            except Exception:
                pass
    return False


async def _abort_accel_dialog(page) -> None:
    """安全退出加速设置：取消调价对话框 → 放弃加速机会 →（二次）确认。绝不点「立即加速」。"""
    await _click_first_button(page, ("取消",))
    await asyncio.sleep(1)
    await page.evaluate(
        r"""() => {for (const el of document.querySelectorAll('button,a,[role=button]')){"""
        r"""if ((el.textContent||'').replace(/\s+/g,'')==='放弃加速机会'){el.click();return;}}}"""
    )
    await asyncio.sleep(1)
    await _click_first_button(page, ("确定", "确认"))
    await asyncio.sleep(1)


async def verify_state(page, spu, expect: str) -> bool:
    """校验某 SPU 加速态是否达到期望（"on"/"off"）——reload 后【轮询等行渲染完】再读，比对。

    ⚠️ 重要教训（2026-07-17）：流量页近百商品加载慢，reload 后急读会读到「加载中」空页→
    误判为未达期望（假阴性），据此把已成功的关闭/开启报成失败，误导上层。故此处必须轮询
    等目标行真正渲染出来（_LOCATE_ROW_JS 返回非空）再判定；等不到行 → 返回 True 且告警
    「无法校验」而非 False——因为真实变更可能已生效，不能因页面慢就判失败。

    另注：平台侧关闭流量加速有 ~24h 冷却，状态回显也可能延迟；调用方不应把本函数的 False
    当作「操作失败」硬处理，而应结合人工/后续读取确认。expect 非 on/off → 不阻断返回 True。
    """
    if expect not in ("on", "off"):
        logger.warning(f"verify_state：期望 {expect} 无稳定只读校验依据，SPU={spu} 暂放行")
        return True
    try:
        await page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    await dismiss_site_notification_panel(page)
    # 轮询等目标行渲染（最多 ~25s）：读到行才判定，读不到就当「无法校验」放行。
    for _ in range(25):
        await asyncio.sleep(1)
        try:
            info = await page.evaluate(_LOCATE_ROW_JS, str(spu))
        except Exception:
            info = None
        if info:
            actual = _classify_accel(info.get("product_info_text", ""))
            ok = (actual == expect)
            if not ok:
                logger.warning(f"verify_state：SPU={spu} 期望 {expect} 实际 {actual}（可能平台回显延迟）")
            return ok
    logger.warning(f"verify_state：SPU={spu} reload 后行未渲染，无法校验 {expect}，放行（不判失败）")
    return True
