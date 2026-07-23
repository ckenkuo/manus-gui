"""活动管理管线（阶段1：只读 + dry-run）——对标 app/collect/pipeline.py 的确定性单商品例程。

为什么是确定性管道而非 agent 自由循环：本质是「for 每个 SPU：几步确定性 DOM/接口只读 +
纯本地算价筛选」，不需要 function-calling/LangGraph。浏览器操作全部裸 Playwright over CDP、
纯只读。判定完全确定性（见下），不再用 LLM 选活动（judge_activity 保留但主流程未接入）。

判定逻辑（2026-07-16 用户重定义）：不再「LLM 选一个 + 毛利率红线」，而是【确定性筛选：
申报价(=日常价×活动折扣率) ≥ 销售底价(Excel 销售价列) 且 商品库存 ≥ 活动库存门槛 的活动
全部报名】（满足的都报，一个 SPU 可报多个活动）。销售底价完全替代旧毛利率红线。

最高优先级安全约束（真实商家账号、操作不可逆）：
- 本模块阶段1 只提供【只读】函数（定位行/读加速态/读活动列表/读库存）+ 算价（纯本地）。
- 变更动作（close_accel / enroll_activity / open_accel）一律留桩：函数存在、正式分支先
  `raise NotImplementedError("阶段3")`，被 service 的 dry-run 分支跳过、绝不调用。
- 关闭入口须在「流量加速中」筛选下才出现，尚未探测确认——read_accel_state 只读现有 DOM
  尽力判断，探不到就返回 "unknown"，绝不臆造关闭选择器。

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
from typing import Any, Optional

# 复用采集管道已实测的 JSON 解析（剥 ```json 围栏 + 兜底抓首个 {...}），避免重复实现。
from app.collect.pipeline import _parse_json
from app.llm import LLM
from app.logger import logger
from app.schema import Message

# ---- 页面 URL 常量（Wave3 web 层会依赖）------------------------------------
FLUX_URL = "https://agentseller.temu.com/main/flux-analysis"
ACTIVITY_URL = "https://agentseller.temu.com/activity/marketing-activity"
ACTIVITY_LOG_URL = "https://agentseller.temu.com/activity/marketing-activity/log"
# 商品列表页 + 库存查询接口关键字（库存不在 DOM，在此接口响应里，见 read_stock）。
GOODS_LIST_URL = "https://agentseller.temu.com/goods/list"
_STOCK_API_KEY = "skc/pageQuery"

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
        clicked = await page.evaluate(r"""() => {
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
        }""")
        if clicked:
            await asyncio.sleep(0.5)
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


def _parse_activity_log_item(item: dict) -> dict:
    session_failures = [
        session.get("sessionFailReason") for session in (item.get("assignSessionList") or [])
        if session.get("sessionFailReason")
    ]
    enroll_status = item.get("enrollStatus")
    return {
        "spu": str(item.get("productId") or ""),
        "activity": item.get("activityThematicName") or item.get("activityTypeName") or "",
        "enroll_status": enroll_status,
        "success": enroll_status == 4 and not session_failures,
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
  if (!pagination) return {found: false, disabled: false};
  const candidates = [...pagination.querySelectorAll('li,button,[role="button"]')];
  const next = candidates.find(element => {
    const cls = String(element.className || '');
    const label = `${element.getAttribute('aria-label') || ''} ${element.title || ''}`;
    return /next/i.test(cls) || /下一页|next/i.test(label)
      || Boolean(element.querySelector(
        'svg[data-icon="right"],svg[data-icon="right-circle"],[class*="rightArrow"]'
      ));
  });
  if (!next) return {found: false, disabled: false};
  const disabled = next.matches('[disabled],[aria-disabled="true"]')
    || /disabled/i.test(String(next.className || ''));
  if (!disabled) next.setAttribute('data-kiro-log-next', '1');
  return {found: true, disabled};
}
"""


async def _collect_activity_log_pages(first_result: dict, fetch_next) -> dict:
    """按首个响应的 total/pageSize 拉完当前 SPU 的报名记录分页。"""
    first_items = first_result.get("list") or []
    total = int(first_result.get("total") or 0)
    reported_size = int(first_result.get("pageSize") or first_result.get("page_size") or 0)
    page_size = reported_size or len(first_items) or 10
    expected_pages = max(1, (total + page_size - 1) // page_size)
    items = list(first_items)
    pages_read = 1
    error = None

    for page_number in range(2, expected_pages + 1):
        try:
            result = await fetch_next(page_number)
        except Exception as exc:
            error = str(exc)[:120]
            break
        page_items = result.get("list") or []
        pages_read += 1
        items.extend(page_items)

    return {
        "items": items,
        "total": total,
        "page_size": page_size,
        "expected_pages": expected_pages,
        "pages_read": pages_read,
        "complete": pages_read == expected_pages and len(items) >= total,
        "error": error,
    }


async def read_activity_log_records(context, spus) -> dict:
    """只读打开报名记录页，逐 SPU 查询并返回平台报名记录接口结果。"""
    targets = list(dict.fromkeys(str(spu).strip() for spu in spus if str(spu).strip()))
    page = await context.new_page()
    records = []
    queries = []
    complete = True
    try:
        await page.goto(ACTIVITY_LOG_URL, wait_until="domcontentloaded", timeout=30000)
        await page.bring_to_front()
        await dismiss_all_page_popups(page)
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                body = await page.evaluate("() => document.body.innerText || ''")
            except Exception:
                continue
            if "报名记录" in body and "SPU ID" in body:
                break
        global_tab = page.get_by_text("全球", exact=True).last
        if await global_tab.count():
            try:
                await global_tab.click(timeout=5000)
                await asyncio.sleep(2)
            except Exception:
                pass

        query_button = page.get_by_role("button", name="查询", exact=True).last
        for spu in targets:
            marked = False
            for _ in range(12):
                if await page.evaluate(_MARK_ACTIVITY_LOG_SPU_JS):
                    marked = True
                    break
                await asyncio.sleep(0.5)
            if not marked:
                complete = False
                queries.append({"spu": spu, "error": "报名记录页重绘后仍未找到可编辑 SPU 查询框"})
                logger.warning(f"[活动记录] SPU={spu} 未找到可编辑查询框，跳过本次查询")
                continue
            field = page.locator('[data-kiro-log-spu="1"]')
            await field.fill(spu)
            entered_spu = (await field.input_value()).strip()
            if entered_spu != spu:
                complete = False
                queries.append({
                    "spu": spu,
                    "error": f"SPU 查询框输入校验失败（实际={entered_spu or '空'}）",
                })
                continue
            logger.info(f"[活动记录] 已在报名记录页输入 SPU={spu}，准备点击查询")
            # 让受控输入框完成状态同步，也让操作者能在前台页肉眼确认本次查询条件。
            await asyncio.sleep(0.8)
            try:
                async with page.expect_response(
                    lambda response: "/marketing/enroll/list" in response.url,
                    timeout=15000,
                ) as response_info:
                    await query_button.click(timeout=5000)
                response = await response_info.value
                payload = await response.json()
                first_result = payload.get("result") or {}

                async def fetch_next(_page_number):
                    state = await page.evaluate(_MARK_ACTIVITY_LOG_NEXT_JS)
                    if not state.get("found"):
                        raise RuntimeError("报名记录页未找到下一页按钮")
                    if state.get("disabled"):
                        raise RuntimeError("报名记录下一页按钮已禁用")
                    async with page.expect_response(
                        lambda next_response: "/marketing/enroll/list" in next_response.url,
                        timeout=15000,
                    ) as next_response_info:
                        await page.locator('[data-kiro-log-next="1"]').click(timeout=5000)
                    next_response = await next_response_info.value
                    next_payload = await next_response.json()
                    return next_payload.get("result") or {}

                collected = await _collect_activity_log_pages(first_result, fetch_next)
                items = collected.pop("items")
                query = {"spu": spu, "returned": len(items), **collected}
                queries.append(query)
                logger.info(
                    f"[活动记录] SPU={spu} 查询完成：total={query['total']}，"
                    f"已读取={query['returned']}，页数={query['pages_read']}/{query['expected_pages']}"
                )
                if not query["complete"]:
                    complete = False
                records.extend(_parse_activity_log_item(item) for item in items)
            except Exception as exc:
                complete = False
                queries.append({"spu": spu, "error": str(exc)[:120]})
        note = "报名记录查询完成" if complete else "报名记录查询不完整"
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
            await page.goto(GOODS_LIST_URL, wait_until="domcontentloaded", timeout=60000)
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


def compute_submit_price(daily_price, discount_rate, sale) -> dict:
    """按销售底价算申报价并判是否达底价（纯本地，无副作用）。

    用户方案（2026-07-16）：红线从「毛利率」改为 Excel「销售价格」列——即用户手填的可接受
    最低售价（底价），完全替代旧的 成本÷(1−毛利率)。判定只看申报价是否够底价，与成本/毛利率
    无关（等价式：submit≥sale ⟺ 活动折扣率 ≥ sale/日常价）。
    - 申报价策略 discount_ceiling：submit_price = 日常价 × 活动折扣率（贴折扣上限、少让利）。
    - within_floor：submit_price ≥ sale（销售底价）。
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

    submit_price = round(dp * dr, 2) if (dp is not None and dr is not None) else None
    within = bool(submit_price is not None and fl is not None and submit_price >= fl)

    return {
        "submit_price": submit_price,
        "floor_price": fl,
        "within_floor": within,
        "note": "；".join(notes),
    }


# ---- 变更动作：阶段1 全部留桩（绝不在 dry-run 里被调用）------------------------
# 安全约束：真实商家账号、操作不可逆。阶段3 才实现，实现时每步须先 verify_state 确认幂等、
# 失败即停不硬闯。阶段1/dry-run 分支绝不调用以下任一函数。
# 在流量页某 SPU 行内标记指定文案的操作 a（查看效果/立即开启），供点击。
_MARK_ROW_ACTION_JS = r"""
(args) => {
  const [spu, word] = args;
  const all=[...document.querySelectorAll('div,tr,td,section')];
  let best=null,min=1e9;
  for(const el of all){
    const t=el.innerText||'';
    if(!new RegExp('SPU\\s*ID[：:\\s]*'+spu+'(\\D|$)').test(t)) continue;
    if(t.indexOf(word)<0) continue;
    if(t.length<min){min=t.length;best=el;}
  }
  if(!best) return null;
  const a=[...best.querySelectorAll('a,button,[role="button"]')]
    .find(e=>(e.textContent||'').replace(/\s+/g,'').includes(word.replace(/\s+/g,'')));
  if(!a) return null;
  a.setAttribute('data-kiro-act','1');
  return (best.innerText||'').replace(/\s+/g,' ').trim().slice(0,60);
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
        return result
    result["clicked_stop"] = True

    # 确认后平台会在「停止」按钮内转圈发请求。请求尚未结束就 reload 会中断现场、错过 toast，
    # 并把仍显示 on 的旧行状态误判为关闭失败。先完整等待请求结算，再做冷却/状态判定。
    completion = await _wait_close_request_completion(page)
    result["request_completion"] = completion
    if not completion.get("settled"):
        result["request_timeout"] = True
        result["note"] = f"关闭请求仍在处理中或未确认结束：{completion.get('message', '')}"
        return result
    if completion.get("status") == "cooldown":
        result["cooldown"] = True
        result["note"] = f"未关闭：{completion.get('message', '命中24小时冷却提示')}"
        return result
    if completion.get("status") == "failure":
        result["note"] = f"关闭失败：{completion.get('message', '平台返回失败提示')}"
        return result
    if completion.get("status") == "success":
        result["closed"] = True
        result["success_message"] = completion.get("message", "")
        result["note"] = f"已停止流量加速（成功提示：{completion.get('message', '')}）"
        return result

    # ★ 24h 冷却检测（最可靠的判定信号）：加速器开启不满 24h 手动关闭时，平台会弹 toast
    #   「加速器开启后需满24小时才可手动关闭，请耐心等待」。检出即判定【未关闭·被冷却拦截】。
    cooldown = await _detect_cooldown_toast(page)
    if cooldown:
        result["closed"] = False
        result["cooldown"] = True
        result["note"] = f"未关闭：{cooldown}"
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
  for (const button of buttons) {
    let node = button;
    for (let depth = 0; depth < 16 && node && node !== document.body; depth += 1) {
      const rect = node.getBoundingClientRect();
      const text = norm(node.innerText);
      const isLargeLayer = rect.width >= 400 && rect.height >= 250;
      if (isLargeLayer && text.includes('活动详情')) {
        fallbackText = (node.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
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
    return {status: 'mismatch', actual: fallbackText || '规则按钮所在弹层未包含目标活动名'};
  }
  return {status: 'none', actual: ''};
}
"""


async def open_enroll_page(activity_page, activity_name, timeout_s=25):
    """从活动页打开指定活动的提报页(detail-new) tab 并返回该 Page（未填未提交、可逆）。

    实测链路（2026-07-17 验证）：活动页是 beast 表格，报名 a 无 href（React onClick）。
    ① 保留启动前已存在的 detail-new tab（可能是操作者手动截图/测试页）；
    ② 用 _MARK_ENROLL_JS 按活动名精确标记报名 a；
    ③ JS click 报名（绕过虚拟列表/遮挡的可见性超时）→ 等活动详情弹窗；
    ④ 只在【包含目标活动名】的规则弹窗内点「同意活动规则」；
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
        agreed = False
        modal_mismatch = None
        for _ in range(16):
            await asyncio.sleep(0.5)
            direct_pages = [
                page for page in ctx.pages
                if "detail-new" in (page.url or "") and all(page is not old for old in before)
            ]
            if direct_pages:
                break
            rule = await activity_page.evaluate(_CLICK_ACTIVITY_RULE_JS, activity_name)
            if rule.get("status") == "clicked":
                agreed = True
                break
            if rule.get("status") in {"mismatch", "blocked"}:
                modal_mismatch = rule
                break
        if modal_mismatch:
            logger.error(
                f"[活动] 规则弹窗未绑定目标「{activity_name}」："
                f"{modal_mismatch.get('actual') or modal_mismatch.get('status')}；未点击并重试"
            )
        elif not agreed:
            logger.info(f"[活动] 未见「同意活动规则」按钮（可能无需同意或弹窗未出）：{activity_name}")

        # ⑤ 轮询 diff 认新 tab（本次尝试的短超时）
        for _ in range(int(per_try_s * 2)):
            await asyncio.sleep(0.5)
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

    # 校验有场次被勾中，再点「确认」
    checked_n = await page.evaluate(r"""() => {
      const dlgs = [...document.querySelectorAll('[class*=MDL_outerWrapper]')]
        .filter(e => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes('设置场次'));
      if (!dlgs.length) return -1;
      const d = dlgs[dlgs.length - 1];
      return [...d.querySelectorAll('[class*=CBX_squareInputWrapper] input')].filter(i => i.checked).length;
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


async def enroll_activity(
    page, spu, activity, submit_price, allow_submit=False, on_step=None,
) -> dict:
    """在提报页(detail-new)给某 SPU 搜索→勾选→设置场次(全选)→填申报价；allow_submit=False 停在提交前。

    page 须为该活动已打开的 detail-new 提报页。实测操作序列（2026-07-17 半程试点验证）：
    实测操作序列（2026-07-17 用户逐步纠正后定稿）：
    1. 搜索：顶部 SPU ID 搜索框填 spu → 点「查询」→ 等结果行渲染（详情页默认列表不含目标 SPU）。
    2. 勾选：按视觉 y 对齐勾选商品行 checkbox（beast 左右分表，见 _CHECK_ROW_JS）。
    3. 设置场次：点「批量设置场次」→ 弹窗(MDL_outerWrapper「设置场次」) → 点「全选」→「确认」。
       场次不设 → 提交无效（这是之前全失败的根因之一）。
    4. 填价：勾选后行内第一个非 disabled 的 text input 即「活动申报价格」框。
    5. 提交：底部「提交」，仅 allow_submit=True（授权后）才点。

    安全：默认 allow_submit=False → 只走到填价、绝不提交（未提交不生效、可逆）。返回
    {located, checked, sessions_set, filled, submitted, note}。任一步失败 → 记 note 保守返回。
    """
    await dismiss_all_page_popups(page)
    result = {"located": False, "queried": False, "detail_eligible": None,
              "checked": False, "sessions_set": False,
              "filled": False, "submitted": False, "over_ref": False,
              "ref_price": None, "failed_step": None, "note": ""}

    async def report(step, ok, note=""):
        if on_step is not None:
            await on_step({"step": step, "ok": bool(ok), "note": note})

    # 1. 搜索 SPU（过滤出目标行）。详情页 domcontentloaded 后搜索区(「SPU ID」标签框)仍异步
    #    渲染，须轮询等其出现再搜（否则立即 evaluate 得 no-label）。
    loc = "no-label"
    for _ in range(20):  # 最多等 ~10s
        loc = await page.evaluate(_SEARCH_SPU_JS, str(spu))
        if loc == "ok":
            break
        await asyncio.sleep(0.5)
    if loc != "ok":
        result["failed_step"] = "input_spu"
        result["note"] = f"未定位到 SPU 搜索框（{loc}）"
        await report("input_spu", False, result["note"])
        return result
    try:
        await page.locator('[data-kiro-spu="1"]').fill(str(spu))
        await report("input_spu", True, f"已输入 {spu}")
        await asyncio.sleep(0.4)
        query_clicked = await page.evaluate(
            r"""() => { for (const b of document.querySelectorAll('button')) {"""
            r"""if ((b.textContent||'').replace(/\s+/g,'') === '查询') { b.click(); return true; } } return false; }"""
        )
        if not query_clicked:
            result["failed_step"] = "query"
            result["note"] = "未点到「查询」按钮"
            await report("query", False, result["note"])
            return result
    except Exception as e:
        result["failed_step"] = "query"
        result["note"] = f"搜索 SPU 异常：{e}"
        await report("query", False, result["note"])
        return result

    # 2. 目标行须唯一（防呆：搜索后结果非唯一/为 0 一律跳过，绝不误报）
    row = page.locator("tr").filter(has_text=f"SPU ID: {spu}")
    n = 0
    for _ in range(20):
        n = await row.count()
        if n == 1:
            break
        await asyncio.sleep(0.5)
    if n == 0:
        result["queried"] = True
        result["detail_eligible"] = False
        result["note"] = (
            "详情页查询结果为 0：该 SPU 不在本活动可报名商品列表中"
            "（列表页折扣/库存初筛通过不等于详情资格通过）"
        )
        await report("query", True, result["note"])
        return result
    if n != 1:
        result["failed_step"] = "query"
        result["note"] = f"搜索后定位行数={n}（非唯一），保守跳过"
        await report("query", False, result["note"])
        return result
    result["queried"] = True
    result["detail_eligible"] = True
    await report("query", True, "查询结果唯一")
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

    # 4. 读该行「参考价」上限并校验（平台硬约束：申报价不可大于参考价）。
    #    实测教训（2026-07-20）：Excel 日常价可能与商品前端实际售价不一致——参考价按前端
    #    实际售价×折扣给出，若 Excel 日常价偏高，按 Excel 算的申报价会超过参考价，提交必被
    #    平台拒（且旧代码乐观判 submitted=True 会误报成功）。此处【不擅自改价重报】（Excel
    #    数据本身可能过期，擅自压到参考价不合操作者本意），而是【记失败、清晰上报】交人核对。
    #    读不到参考价（无此约束的活动）则跳过校验、按原逻辑填价。
    row = page.locator("tr").filter(has_text=f"SPU ID: {spu}").first
    try:
        row_txt = ((await row.inner_text()) or "").replace("\n", " ")
    except Exception:
        row_txt = ""
    m = re.search(r"参考价[：:]\s*¥?\s*([\d.]+)", row_txt)
    ref_price = float(m.group(1)) if m else None
    result["ref_price"] = ref_price
    if ref_price is not None and float(submit_price) > ref_price:
        result["over_ref"] = True
        result["failed_step"] = "fill_price"
        result["note"] = (
            f"申报价 {submit_price} 高于提报页参考价 {ref_price}"
            f"（疑 Excel 日常价与商品前端实际售价不一致）——已跳过未报名，请核对该商品 Excel 日常价"
        )
        await report("fill_price", False, result["note"])
        return result

    # 5. 填申报价：勾选后行内第一个非 disabled 的 text input（活动申报价格列）
    tis = row.locator("input[type=text]")
    price_input = None
    for i in range(await tis.count()):
        inp = tis.nth(i)
        if not await inp.is_disabled():
            price_input = inp
            break
    if price_input is None:
        result["failed_step"] = "fill_price"
        result["note"] = "未找到可填的申报价输入框（勾选后仍 disabled？）"
        await report("fill_price", False, result["note"])
        return result
    try:
        await price_input.click()
        await price_input.fill(str(submit_price))
    except Exception as exc:
        result["failed_step"] = "fill_price"
        result["note"] = f"填写活动申报价失败：{exc}"
        await report("fill_price", False, result["note"])
        return result
    result["filled"] = True
    await report("fill_price", True, f"已填写 {submit_price}")

    if not allow_submit:
        result["note"] = (
            f"已勾选+全选场次+填申报价 {submit_price}，等待当前活动统一点击提交"
        )
        return result

    # 单条即时提交（授权后）。批量报名请改用 allow_submit=False 逐个填 + submit_enroll_page 一次提交。
    sub = await submit_enroll_page(page, allow=True)
    result["submitted"] = sub.get("submitted", False)
    result["note"] = f"已提交申报价 {submit_price}" if result["submitted"] else sub.get("note", "")
    return result


_SUBMIT_FEEDBACK_JS = r"""
(canConfirm) => {
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
  )].filter(visible);
  const messages = [...document.querySelectorAll(messageSelectors), ...dialogs]
    .filter(visible)
    .map(element => (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(text => text && text.length <= 300);
  const failureWords = ['活动太火爆', '稍后再试', '提交失败', '报名失败', '操作失败',
    '系统繁忙', '网络异常', '请求失败'];
  const successWords = ['提交成功', '报名成功', '操作成功', '报名已提交'];
  const failure = messages.find(text => failureWords.some(word => norm(text).includes(norm(word))));
  if (failure) return {status: 'failure', message: failure.slice(0, 160)};
  const success = messages.find(text => successWords.some(word => norm(text).includes(norm(word))));
  if (success) return {status: 'success', message: success.slice(0, 160)};

  const actionWords = ['确认提交', '确定提交', '继续提交', '仍要提交', '确认报名', '确定报名',
    '继续报名', '确认', '确定', '提交'];
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


async def _wait_submit_result_page(page, tries=12) -> dict:
    """等待提交后的 detail-new-result 页面，读取 successCount/“已提交N个商品”。"""
    for attempt in range(tries):
        url = str(getattr(page, "url", "") or "")
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
                return {
                    "detected": True, "success": success, "success_count": success_count,
                    "url": url,
                    "note": (f"结果页确认已提交 {success_count} 个商品"
                             if success else "结果页显示提交成功商品数为 0"),
                }
        if attempt + 1 < tries:
            await asyncio.sleep(0.25)
    return {"detected": False, "success": False, "success_count": None, "url": "", "note": ""}


def _submit_result_payload(result_page: dict, confirmed: bool) -> dict:
    return {
        "submitted": bool(result_page.get("success")),
        "verified": bool(result_page.get("success")),
        "clicked_submit": True,
        "confirmed": confirmed,
        "result_page": True,
        "success_count": result_page.get("success_count"),
        "note": result_page.get("note", ""),
    }


async def submit_enroll_page(page, allow=False, feedback_tries=20) -> dict:
    """点提报页底部「提交」，一次提交本页已勾选+填价的所有 SPU（活动维度批量报名用）。

    allow=False（默认）只定位提交按钮、不点击（半程、可逆）。allow=True 才真提交（不可逆，
    授权后）。提交后短轮询等待可能延迟出现的二次确认弹窗，只在弹窗内部点击；同时捕获
    成功/失败提示。返回 {submitted, verified, note}，其中 submitted 仅表示提交动作已被页面接受，
    verified=True 才表示本函数捕获到明确成功提示。
    """
    btn = page.get_by_role("button", name="提交").first
    if not await btn.count():
        return {"submitted": False, "note": "未找到「提交」按钮"}
    if not allow:
        return {"submitted": False, "note": "半程：已定位「提交」按钮，未点击（allow=False）"}
    # 「提交」按钮 disabled 时（本页无已勾选/已填 SPU）不可点——旧代码硬点会等满 30s 超时
    # 抛异常，冒泡中断整个执行遍（含阶段三重开流量）。故先判 disabled，禁用即优雅返回。
    try:
        if await btn.is_disabled():
            return {"submitted": False, "note": "「提交」按钮禁用（本页无已填 SPU？），跳过"}
    except Exception:
        pass
    try:
        await btn.click(timeout=8000)
    except Exception as e:
        return {"submitted": False, "note": f"点「提交」失败：{str(e)[:80]}"}
    confirmed = False
    blocked_polls = 0
    last_message = ""
    for _ in range(feedback_tries):
        await asyncio.sleep(0.5)
        if "detail-new-result" in str(getattr(page, "url", "") or ""):
            result_page = await _wait_submit_result_page(page, tries=4)
            if result_page.get("detected"):
                return _submit_result_payload(result_page, confirmed)
        try:
            raw_feedback = await page.evaluate(_SUBMIT_FEEDBACK_JS, not confirmed)
        except Exception as exc:
            message = str(exc)
            if "Execution context was destroyed" in message or "navigat" in message.lower():
                result_page = await _wait_submit_result_page(page)
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
    return {
        "submitted": True, "verified": False, "clicked_submit": True,
        "confirmed": confirmed,
        "note": ("已点击提交并确认，未捕获明确结果提示" if confirmed
                 else "已点击提交，等待期间未出现二次确认或明确结果提示"),
    }


# 在开启弹窗里选「超级流量加权」档卡片（用户规则 2026-07-17：加速统一选超级档）。
_SELECT_SUPER_TIER_JS = r"""
() => {
  for (const el of document.querySelectorAll('*')) {
    const t = (el.innerText || '').replace(/\s+/g, '');
    if (t.includes('超级') && t.includes('流量加权') && t.length < 40) {
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
  // 从左到右排列；只收集同时含两列价格、可见且 cursor:pointer 的独立卡片，取最右侧。
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
    cards[cards.length - 1].node.click();
    return true;
  }
  return false;
}
"""


async def _select_super_tier(page, tries=20) -> bool:
    """轮询等待档位卡异步渲染后选择超级档，避免抽屉已开但卡片尚未挂载的偶发空读。"""
    for _ in range(tries):
        if await page.evaluate(_SELECT_SUPER_TIER_JS):
            return True
        await asyncio.sleep(0.5)
    return False

# 在「调整申报价」对话框里，给每个含「参考申报价格」的行的可填输入框打 data-accel-idx 标记，
# 返回 [{idx, ref}]（ref=该行参考申报价格上限）。Python 侧据此用 Playwright 逐个填价（React
# 友好）并校验 目标价 ≤ ref。多 SKC 会有多行，逐行标记。
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
        const m = (r.innerText || '').match(/参考申报价格[：:]\s*¥?\s*([\d.]+)/);
        ins[0].setAttribute('data-accel-idx', String(idx));
        rows.push({ idx, ref: m ? Number(m[1]) : null });
        idx++;
        break;
      }
      r = r.parentElement;
    }
  }
  return rows;
}
"""


async def _set_accel_prices(page, accel_price: float) -> dict:
    """在「调整申报价」对话框逐行填加速价=accel_price（须 ≤ 每行参考价）。
    返回 {rows_total, rows_filled, rows_over, over_detail}；任一行超参考价则该行不填、计入 over。
    """
    rows = await page.evaluate(_MARK_PRICE_INPUTS_JS)
    filled = 0
    over = []
    for row in (rows or []):
        ref = row.get("ref")
        if ref is not None and float(accel_price) > float(ref):
            over.append({"idx": row["idx"], "ref": ref})
            continue
        inp = page.locator(f'[data-accel-idx="{row["idx"]}"]').first
        try:
            await inp.click()
            await inp.fill(str(accel_price))
            filled += 1
        except Exception as e:
            over.append({"idx": row["idx"], "ref": ref, "err": str(e)[:60]})
    return {
        "rows_total": len(rows or []), "rows_filled": filled,
        "rows_over": len(over), "over_detail": over,
    }


async def _open_accel_once(page, spu, allow=False, accel_price=None) -> dict:
    """单次尝试（重新）开启某 SPU 的流量加速。accel_price 给定时走完整路径：立即开启→选超级流量加权
    →继续让价「去获取」→「调整申报价」对话框逐行填加速价(=底价+1，多 SKC 统一)→确认→
    (授权后)立即加速。accel_price=None 时退回旧简单路径（只点开启确认）。

    allow=False（默认）只走到最终「立即加速」前不点（半程、可逆）。返回 {state, opened, note,
    price_set}。若该 SPU 已加速（on）→ opened=True(no-op)。

    用户规则 2026-07-17：加速器一开，前端显示价以加速价为准，故加速价须设为 Excel 底价+1；
    档位固定选超级；加速价不得高于该品「参考申报价格」（超参考价则该行填不进、须告警）。
    真开启不可逆（实测锁 24h 才能手动停），故 allow=False 时不点「立即加速」。
    """
    await dismiss_all_page_popups(page)
    result = {
        "state": "unknown", "precheck_state": "unknown",
        "opened": False, "note": "", "price_set": None,
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

    marked = await page.evaluate(_MARK_ROW_ACTION_JS, [str(spu), "立即开启"])
    if not marked:
        result["note"] = "未定位到该行「立即开启」入口"
        return result

    # 旧路径：未给 accel_price → 只点开启确认（兼容老调用/半程探测）。
    if accel_price is None:
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
    if not await _select_super_tier(page):
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

    price = await _set_accel_prices(page, accel_price)
    result["price_set"] = price
    if price["rows_total"] == 0:
        result["note"] = "「调整申报价」对话框未识别到可填行"
        return result
    if price["rows_over"] > 0:
        # 有行超参考价填不进 → 保守中止，不开（避免开成默认价）。取消退出。
        await _abort_accel_dialog(page)
        result["note"] = (f"加速价 {accel_price} 高于 {price['rows_over']} 个 SKC 的参考价"
                          f"（{price['over_detail']}），已中止不开")
        return result
    # 确认调价对话框
    await _click_first_button(page, ("确认", "确定"))
    await asyncio.sleep(1.5)

    if not allow:
        result["note"] = (f"半程：已选超级档、填加速价 {accel_price}（{price['rows_filled']} 个 SKC）"
                          f"并确认，未点「立即加速」（allow=False）")
        return result

    feedback = await _click_open_with_busy_retries(page)
    result["final_clicks"] = feedback["clicks"]
    result["submit_feedback"] = feedback
    result["clicked_open"] = feedback["clicks"] > 0
    if not result["clicked_open"]:
        result["note"] = "未点到最终「立即加速」按钮"
        return result
    result["opened"] = feedback["status"] == "success"
    # 成功 toast 是平台提交结果的权威判据；商品行可能仍保留旧状态，字段只作非权威快照。
    result["row_state_snapshot"] = await read_accel_state(page, spu) if result["opened"] else "off"
    result["note"] = (f"已开启加速、加速价设为 {accel_price}（{price['rows_filled']} 个 SKC，"
                      f"最终按钮点击 {feedback['clicks']} 次，成功提示：{feedback['message']}）"
                      if result["opened"] else
                      f"最终按钮点击 {feedback['clicks']} 次、填价 {accel_price}；"
                      f"未捕获成功提示（{feedback['message'] or feedback['status']}）")
    return result


async def open_accel(page, spu, allow=False, accel_price=None) -> dict:
    """开启流量加速；具体防护与平台火爆重试见 `_open_accel_once`。"""
    return await _open_accel_once(page, spu, allow=allow, accel_price=accel_price)


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
