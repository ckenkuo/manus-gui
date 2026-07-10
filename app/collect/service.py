"""采集服务层：把 batch_collect 的两段式编排（枚举 + 逐商品确定性管道）抽成
UI / CLI 共用的纯函数入口，并把进度通过【结构化回调】抛出，而不是只写 logger。

为什么单独一层：
- 采集管道是【确定性批处理作业】（见 app/collect/pipeline.py 开头），不是给大模型
  function-calling 用的 tool，也用不上 LangGraph 那种有状态图/持久化状态机——它本质
  就是「for 每个未入库商品：确定性步骤 + 2 次单发 LLM」。故不封装成 BaseTool，
  而是抽成 service：CLI（batch_collect.py）和 UI（app.py 的 FastAPI 接口）都调这里。
- 进度以结构化事件（dict）经 on_progress 回调抛出，UI 可直接走 SSE 渲染进度条/表格，
  彻底摆脱旧 app.py 里「靠正则+emoji 猜日志属于哪类事件」的脆弱做法。

事件契约（on_progress 收到的 dict，均含 "type"）：
    {"type": "batch_start",   "total": int, "done_existing": int, "todo": int, "batch": int}
    {"type": "product_start", "index": int, "total": int, "spu": str, "name": str}
    {"type": "product_done",  "index": int, "total": int, "spu": str,
                              "status": "ok"|"base"|"empty"|"fail",
                              "offer_id": str|None, "purchase_price": float|None, "shipping": float|None,
                              "weight_g": float|None, "detail_url": str|None, "note": str,
                              "via": "pipeline"|"agent"|"base"}
    # status="base"：仅采 Temu 基础信息、采购价/重量留空待人工填（不跑 1688 图搜/判价）
    {"type": "batch_done",    "ok": int, "fail": int}
    {"type": "log",           "level": "info"|"warning"|"error", "message": str}   # 兜底文本进度
    {"type": "aborted",       "reason": str}                                       # CDP 不可用等中止

on_progress 可为普通函数或 async 函数；两者都支持（内部统一 await）。回调抛异常只告警、
不影响采集主流程。
"""
import asyncio
import inspect
import json
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Union

from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from app.collect.pipeline import SheetSchema

from app.agent.manus import Manus
from app.config import config
from app.logger import logger
from app.tool.wps_excel_tool import WpsExcelTool

# 采集目标出厂默认（偏好文件缺失时兜底；工作簿/Sheet 现已可在 UI/CLI 选择）
DEFAULT_EXCEL = r"C:\Users\Administrator\Desktop\商品成本核算_原始备份.xlsx"
DEFAULT_SHEET = "pawly全球"
TEMU_URL = "https://agentseller.temu.com/newon/product-select"
LIST_API = "searchForSemiSupplier"
PUBLISHED_STATUS = 12  # secondarySelectStatusList=[12] → 已发布到站点
WORKLIST = config.workspace_root / "worklist.json"
# 上次选择的工作簿/Sheet/店铺，供 UI/CLI 缺省回填（best-effort，坏了不影响采集）
COLLECT_PREFS = config.workspace_root / "collect_prefs.json"

# 稳定性护栏参数
CDP_URL = getattr(config.browser_config, "cdp_url", None) or "http://localhost:9222"
PRODUCT_TIMEOUT = 300  # 单商品超时（秒，agent 兜底路径）
PRODUCT_RETRIES = 2  # 单商品尝试次数（首次 + 重试 1 次）
CHECKOUT_COMPARE_TIMEOUT = 600  # 比价模式整商品超时：top_k 个候选各跑一次 DOM 读价
CDP_PING_RETRIES = 3  # CDP 连续 ping 失败次数上限，超过才放弃整批
CDP_PING_WAIT = 5.0  # 每次 CDP ping 失败后的等待秒数

# 进度回调类型：接收一个事件 dict，返回 None 或可 await 对象。
ProgressCB = Optional[Callable[[dict], Union[None, Awaitable[None]]]]


async def _emit(on_progress: ProgressCB, event: dict) -> None:
    """调用进度回调（兼容同步/异步）；回调异常只告警、不阻断采集。"""
    if on_progress is None:
        return
    try:
        r = on_progress(event)
        if inspect.isawaitable(r):
            await r
    except Exception as e:
        logger.warning(f"进度回调异常（忽略）：{e}")


# ---- 偏好持久化（记住上次选的工作簿/Sheet/店铺）-----------------------------
def load_prefs() -> dict:
    """读上次选择 {excel, sheet, store}；缺失/损坏返回 {}（best-effort，不抛错）。"""
    if not COLLECT_PREFS.exists():
        return {}
    try:
        data = json.loads(COLLECT_PREFS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(excel: str = "", sheet: str = "", store: str = "") -> None:
    """记住本次选择，供下次 UI/CLI 缺省回填。写失败只告警、不阻断采集。"""
    try:
        COLLECT_PREFS.parent.mkdir(parents=True, exist_ok=True)
        COLLECT_PREFS.write_text(
            json.dumps(
                {"excel": excel, "sheet": sheet, "store": store},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"保存采集偏好失败（忽略）：{e}")


def resolve_excel(excel: Optional[str] = None) -> str:
    """解析目标工作簿：显式传入 > 上次选择 > 出厂默认。"""
    return excel or load_prefs().get("excel") or DEFAULT_EXCEL


def spu_col_of(excel: str, sheet: str) -> str:
    """某工作簿/Sheet 里 SPU 所在列字母（判重用）。

    【必须按真实表头解析，勿硬编码 D】不同 Sheet 的 SPU 列不同（pawly全球在 D、pawly美国在
    C、wintak美国在 B…）。若沿用旧的固定 "D"，在 SPU 不在 D 的 Sheet 上会去读错列（如 pawly
    美国的 D 是产品图片列），导致判重全错、把"待采"算成满数。解析不出退回 D。
    """
    return WpsExcelTool.spu_column(excel, sheet, default="D")


def list_workbooks() -> list:
    """列出可选工作簿（桌面 + 桌面输出根目录下的 .xlsx），按修改时间倒序。

    排除 WPS/Excel 打开时的 ~$ 临时锁文件与「Excel备份」目录，避免把备份/锁当目标。
    纯读、异常返回空列表，绝不抛错。始终把当前生效工作簿并入结果（即便不在扫描目录）。
    """
    from pathlib import Path

    roots = [Path.home() / "Desktop"]
    try:
        roots.append(config.output_dir())  # 桌面输出根（可能与桌面不同）
    except Exception:
        pass
    backup_dir = None
    try:
        backup_dir = config.output_dir("backup").resolve()
    except Exception:
        pass

    found: dict = {}  # 绝对路径 → mtime，天然去重
    for root in roots:
        try:
            for p in root.glob("*.xlsx"):
                if p.name.startswith("~$"):
                    continue
                rp = p.resolve()
                if backup_dir and backup_dir in rp.parents:
                    continue
                try:
                    found[str(rp)] = rp.stat().st_mtime
                except Exception:
                    found[str(rp)] = 0.0
        except Exception:
            continue

    # 当前生效工作簿务必在列（即便不在扫描目录，用户手填过的也能回显选中）
    cur = resolve_excel()
    if cur and cur not in found and os.path.exists(cur):
        try:
            found[cur] = Path(cur).stat().st_mtime
        except Exception:
            found[cur] = 0.0

    return [p for p, _ in sorted(found.items(), key=lambda kv: kv[1], reverse=True)]


# 页内翻页取全部商品并抽字段（命中列表接口，带 mallid + cookie）。
_FETCH_ALL_JS = r"""
async (mallid) => {
  const out = [];
  const hdr = {'content-type': 'application/json'};
  if (mallid) hdr['mallid'] = mallid;
  for (let pageNum = 1; pageNum <= 40; pageNum++) {
    const resp = await fetch('/api/kiana/mms/robin/searchForSemiSupplier', {
      method: 'POST', headers: hdr, credentials: 'include',
      body: JSON.stringify({pageSize: 50, pageNum,
        secondarySelectStatusList: [__STATUS__], supplierTodoTypeList: []})
    });
    const j = await resp.json();
    const dl = (j.result && j.result.dataList) || [];
    for (const it of dl) {
      out.push({
        spu: String(it.productId),
        name: (it.productName || '').slice(0, 120),
        site: it.siteName || (it.siteInfoList && it.siteInfoList[0] && it.siteInfoList[0].siteName) || '',
        category: it.leafCategoryName || (Array.isArray(it.fullCategoryName) ? it.fullCategoryName[it.fullCategoryName.length - 1] : '') || '',
        price: it.supplierPrice || '',
        image: (it.carouselImageUrlList && it.carouselImageUrlList[0]) || ''
      });
    }
    if (dl.length < 50) break;  // 最后一页
  }
  return out;
}
""".replace("__STATUS__", str(PUBLISHED_STATUS))


# best-effort 读店铺名。Temu 卖家中心（agentseller.temu.com）类名全哈希化，语义类名
# （storeName/mallName…）一个都命不中；实测店名是【顶栏最右侧】的短文本（如 x≈1965 的
# 「VibeMakers」）。故策略：取顶栏（top<60）所有叶子短文本，按 x 从右往左，跳过已知功能
# 按钮词（消息/客服/设置/Beta/纯数字/99+ 等），取最右侧的第一个即店名。
# 先试语义类名（别的卖家中心可能有），命中就用；否则退顶栏最右启发式；全失败返回 ''
# （调用方退回 mallid）。启发式易随改版失效，需要时对真实页重验微调，勿过拟合。
_READ_STORE_NAME_JS = r"""
() => {
  const clean = (el) => el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  for (const sel of ['[class*="storeName"]','[class*="store-name"]',
                     '[class*="mallName"]','[class*="mall-name"]',
                     '[class*="shopName"]','[class*="shop-name"]']) {
    const t = clean(document.querySelector(sel));
    if (t && t.length <= 40) return t;
  }
  // 顶栏最右侧短文本启发式
  const bad = /^(Beta|\d+|99\+|学习|自营对接|经理助手|消息|客服|设置|市场|履约管理|首页|通知)$/;
  const items = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.top < 0 || r.top > 60 || r.width < 10 || r.height < 8) continue;
    if (el.children.length > 0) continue;  // 叶子节点
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length < 2 || t.length > 30) continue;
    items.push({ x: Math.round(r.left), t });
  }
  items.sort((a, b) => b.x - a.x);
  const pick = items.find(o => !bad.test(o.t));
  return pick ? pick.t : '';
}
"""


async def _enumerate_one_store(ctx, page, allow_cookie_fallback: bool = False) -> tuple:
    """在单个 product-select 店铺标签内嗅 mallid + 抓该店全部商品 + 读店名。

    返回 (mallid, store_label, items)。items 每条打上该店 mallid/store 标签。
    单店内任一步失败只告警、返回已拿到的部分（best-effort），不阻断其它店。

    allow_cookie_fallback：仅【单标签】场景为 True——网络嗅探失败时退回读 mallid cookie
    （等价旧单店行为）。多标签禁用：mallid cookie 是 context 级、跨标签共享，会把当前
    激活店的 mallid 错安到别的店上（串店）。
    """
    mallid = {"v": None}
    client = await ctx.new_cdp_session(page)
    await client.send("Network.enable")

    def on_req(params):
        req = params.get("request", {})
        if LIST_API in req.get("url", "") and not mallid["v"]:
            mallid["v"] = req.get("headers", {}).get("mallid")

    client.on("Network.requestWillBeSent", on_req)

    # 触发一次真实列表请求以抓 mallid：把该标签置前 + 点「已发布到站点」tab
    try:
        await page.bring_to_front()
    except Exception:
        pass
    await asyncio.sleep(3)
    for _ in range(20):  # 最多等 ~20s
        if mallid["v"]:
            break
        try:
            await page.evaluate(
                "() => { const t=[...document.querySelectorAll('*')].find("
                "e=>/已发布到站点/.test(e.textContent||'')&&e.children.length<=2"
                "&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().width<200);"
                "if(t)t.click(); }"
            )
        except Exception:
            pass
        await asyncio.sleep(1)

    if not mallid["v"] and allow_cookie_fallback:
        try:
            for c in await ctx.cookies():
                if c.get("name") == "mallid" and c.get("value"):
                    mallid["v"] = c["value"]
                    logger.info(f"从 cookie 兜底取到 mallid={mallid['v']}")
                    break
        except Exception as e:
            logger.warning(f"cookie 读 mallid 失败：{e}")
    if not mallid["v"]:
        logger.warning("未抓到该店 mallid（网络监听失败），尝试直接重放（可能失败/串店）")

    # 店铺标签：best-effort 读店名，读不到退回 mallid
    store_label = ""
    try:
        store_label = (await page.evaluate(_READ_STORE_NAME_JS) or "").strip()
    except Exception:
        store_label = ""
    if not store_label:
        store_label = mallid["v"] or ""

    try:
        items = await page.evaluate(_FETCH_ALL_JS, mallid["v"])
    except Exception as e:
        logger.warning(f"抓取该店商品失败：{e}")
        items = []

    try:  # 释放该标签的 CDP 会话，避免跨店监听泄漏/串扰
        await client.detach()
    except Exception:
        pass

    for it in items:
        it["mallid"] = mallid["v"] or ""
        it["store"] = store_label
    return mallid["v"], store_label, items


async def enumerate_worklist() -> int:
    """确定性枚举：遍历调试 Chrome 里所有已打开的 product-select 店铺标签，逐店命中
    列表接口取全部「已发布到站点」商品，合并写 worklist.json，返回条数。

    多店关键：mallid cookie 是 context 级、跨标签共享、只反映当前激活店，故【不能】靠
    cookie 区分店铺——必须逐标签从各自的网络请求头嗅 mallid（见 _enumerate_one_store）。
    一个店标签都没打开时，退回新开一个（等价旧单店行为）。
    """
    cdp = CDP_URL
    all_items: list = []
    store_count = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0]

        # 所有已打开的列表标签（每个 = 一个已登录店铺）；一个都没有再新开一个兜底。
        store_pages = [p for p in ctx.pages if "product-select" in (p.url or "")]
        created_page = None
        if not store_pages:
            created_page = await ctx.new_page()
            try:  # commit 只等导航提交、不等整页加载，够跑 fetch 了
                await created_page.goto(TEMU_URL, wait_until="commit", timeout=60000)
            except Exception as e:
                logger.warning(f"打开列表页超时（忽略，继续）：{e}")
            store_pages = [created_page]

        single_tab = len(store_pages) == 1
        for idx, page in enumerate(store_pages, 1):
            mid, label, items = await _enumerate_one_store(
                ctx, page, allow_cookie_fallback=single_tab
            )
            store_count += 1
            logger.info(
                f"[店 {idx}/{len(store_pages)}] mallid={mid or '无'} "
                f"店名={label or '(未读到)'} 商品={len(items)}"
            )
            all_items.extend(items)

        if created_page is not None:
            await created_page.close()  # 只关自己开的
        await browser.close()

    # 合并去重：同一 spu 可能出现在不同店 → 按 (mallid, spu) 去重，各留一份（可落不同 Sheet）
    seen, uniq = set(), []
    for it in all_items:
        spu = it.get("spu")
        key = (it.get("mallid") or "", spu)
        if spu and key not in seen:
            seen.add(key)
            uniq.append(it)
    # 防误删护栏：合并后 0 条（几乎总是 mallid 抓取失败/接口 403），绝不用空列表覆盖已有
    # 非空清单——否则一次失败的 --refresh 就把好数据冲掉。保留旧清单、告警。
    if not uniq and WORKLIST.exists():
        try:
            old = json.loads(WORKLIST.read_text(encoding="utf-8"))
            if isinstance(old, list) and old:
                logger.warning(
                    f"枚举得 0 条（疑似 mallid/登录问题），保留原有 {len(old)} 条清单不覆盖。"
                    f"请确认已登录 Temu 半托管、列表页可访问后重试。"
                )
                return 0
        except Exception:
            pass
    WORKLIST.parent.mkdir(parents=True, exist_ok=True)
    WORKLIST.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"枚举完成：{len(uniq)} 个商品，来自 {store_count} 个店铺标签 → {WORKLIST}")
    return len(uniq)


def load_worklist() -> list:
    if not WORKLIST.exists():
        return []
    try:
        data = json.loads(WORKLIST.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"读取工作清单失败：{e}")
        return []


def _store_key(it: dict) -> str:
    """某条商品的店铺标识：优先 mallid，缺失退回 store 标签。用于筛选/分组的稳定键。"""
    return str(it.get("mallid") or it.get("store") or "").strip()


def summarize_stores(worklist: list) -> list:
    """从 worklist 汇总去重店铺列表：[{key, label, mallid, count}]（按商品数倒序）。"""
    agg: dict = {}
    for it in worklist:
        key = _store_key(it)
        if not key:
            continue
        e = agg.setdefault(
            key,
            {"key": key, "label": str(it.get("store") or key), "mallid": it.get("mallid") or "", "count": 0},
        )
        e["count"] += 1
    return sorted(agg.values(), key=lambda e: e["count"], reverse=True)


def get_worklist_status(
    excel: Optional[str] = None,
    sheet: Optional[str] = None,
    store: Optional[str] = None,
) -> dict:
    """UI 展示用：返回清单总量 / 已入库 / 待采、可选工作簿/Sheet/店铺列表及每条状态。

    不触发任何采集，纯读 worklist.json + Excel 已入库 SPU 集合，供 UI 渲染。
    - excel/sheet/store 均缺省回填「上次选择」偏好，再兜底出厂默认。
    - 传了 store → items 过滤到该店；有有效 sheet → done 按该工作簿/Sheet 判重，
      否则 done=None（跨店对单 sheet 判重无意义）。
    - 回传当前生效的 excel/sheet/store 供 UI 回显选中态。
    Excel 不可读（缺失/被占用/无此 Sheet）时 done 视为空集，不抛错。
    """
    prefs = load_prefs()
    excel = excel or prefs.get("excel") or DEFAULT_EXCEL
    if sheet is None:
        sheet = prefs.get("sheet") or ""
    if store is None:
        store = prefs.get("store") or ""

    worklist = load_worklist()
    stores = summarize_stores(worklist)
    workbooks = list_workbooks()
    sheets = WpsExcelTool.list_sheets(excel)
    # 选中的 sheet 若不在该工作簿里（换了工作簿导致失配）→ 视为未选
    sheet_valid = bool(sheet) and sheet in sheets
    # 选中的 store 若不在清单里（换清单）→ 视为未选（全部）
    store_valid = bool(store) and any(s["key"] == store for s in stores)

    done = (
        WpsExcelTool.existing_key_values(excel, sheet, spu_col_of(excel, sheet))
        if sheet_valid else set()
    )

    items = []
    todo = 0
    for it in worklist:
        if store_valid and _store_key(it) != store:
            continue
        spu = str(it.get("spu", "")).strip()
        is_done = bool(spu) and spu in done if sheet_valid else False
        if spu and not is_done:
            todo += 1
        items.append(
            {
                "spu": spu,
                "name": it.get("name", ""),
                "site": it.get("site", ""),
                "store": it.get("store", ""),
                "mallid": it.get("mallid", ""),
                "category": it.get("category", ""),
                "price": it.get("price", ""),
                "image": it.get("image", ""),
                "done": is_done,
            }
        )
    return {
        "total": len(items),
        "done": len([i for i in items if i["done"]]) if sheet_valid else None,
        "todo": todo,
        "excel": excel,
        "sheet": sheet if sheet_valid else "",
        "store": store if store_valid else "",
        "workbooks": workbooks,
        "sheets": sheets,
        "stores": stores,
        "excel_locked": excel_write_locked(excel),
        "items": items,
    }


def excel_write_locked(path: str) -> bool:
    """Excel 是否被占用（WPS/Excel 打开中）导致写不进。

    两个信号：① Office 打开时会在同目录生成 ~$ 锁文件；② 尝试以读写方式打开，
    被占用时 Windows 抛 PermissionError。任一命中即视为锁定。
    """
    from pathlib import Path

    p = Path(path)
    if (p.parent / ("~$" + p.name)).exists():
        return True
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


async def ensure_cdp_alive(cdp_url: str = CDP_URL) -> bool:
    """CDP 健康检查 + 确定性重连：能连上返回 True，连续失败达上限返回 False。"""
    for attempt in range(1, CDP_PING_RETRIES + 1):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(cdp_url)
                _ = browser.contexts  # 拿到 context 即视为存活，不做页面操作避免副作用
                await browser.close()
            return True
        except Exception as e:
            logger.warning(
                f"CDP ping 失败（{attempt}/{CDP_PING_RETRIES}）：{e}；{CDP_PING_WAIT}s 后重试"
            )
            if attempt < CDP_PING_RETRIES:
                await asyncio.sleep(CDP_PING_WAIT)
    logger.error(
        f"❌ CDP 连续 {CDP_PING_RETRIES} 次连不上（{cdp_url}）。"
        f"请确认已登录的 Chrome 仍以 --remote-debugging-port=9222 运行。"
    )
    return False


def reset_agent(agent: Manus) -> None:
    """把 agent 恢复到「全新对话」状态：清空记忆/步数/状态/token 计数，避免跨商品累积。"""
    from app.prompt.manus import NEXT_STEP_PROMPT
    from app.schema import AgentState

    agent.memory.messages = []
    agent.current_step = 0
    agent.state = AgentState.IDLE
    agent.next_step_prompt = NEXT_STEP_PROMPT  # handle_stuck_state 可能改过它
    agent._last_terminate_status = None
    if agent.browser_context_helper is not None:
        agent.browser_context_helper._current_base64_image = None
    # LLM 是按 config_name 的进程级单例，token 计数会跨商品累加；check_token_limit 用
    # 累计 total_input_tokens。这里清零，让 max_input_tokens 成为【单商品】护栏。
    if getattr(agent, "llm", None) is not None:
        agent.llm.total_input_tokens = 0
        agent.llm.total_completion_tokens = 0


def reset_pipeline_llms() -> None:
    """清零管道用的 LLM 单例 token 计数（判断点A samematch + 判断点B default）。

    与 reset_agent 同理：LLM 是按 config_name 的进程级单例，check_token_limit 用累计
    total_input_tokens。不清零则约 12 个商品后累计撞上限、后半段静默退化。
    """
    from app.llm import LLM

    for name in ("samematch", "default"):
        inst = LLM._instances.get(name)
        if inst is not None:
            inst.total_input_tokens = 0
            inst.total_completion_tokens = 0


def per_product_prompt(item: dict, excel: str = DEFAULT_EXCEL, sheet: str = DEFAULT_SHEET) -> str:
    img_dir = config.output_dir("image")
    spu = item.get("spu", "")
    return (
        "这是一个已从 Temu 采集好基础信息的商品，请完成它在 1688 的采购价采集并写入 Excel："
        f"\n- SPU: {spu}\n- 商品名: {item.get('name')}\n- 站点: {item.get('site')}"
        f"\n- 类目: {item.get('category')}\n- 销售价: {item.get('price')}"
        f"\n- 主图URL: {item.get('image')}\n\n"
        "步骤：\n"
        f"1. 把主图下载到 {img_dir}\\{spu}.jpeg：直接用 python_execute + requests.get 下载"
        "（服务端请求，不受浏览器同源策略限制；Temu 的 img.kwcdn.com 主图实测可直连 200）。"
        "不要用 execute_js 会话内 fetch——从 Temu 页跨域抓 CDN 必然 CORS 报错（Failed to fetch），白费一步。"
        "仅当直连被 CDN 以 403 拦截时，才退回 execute_js 会话内 fetch 转 dataURL。\n"
        "2. 用 browser_use 的 open_tab 打开 https://www.1688.com/【新标签】（勿用 go_to_url 原地导航，"
        "会把当前的 Temu 店铺标签覆盖掉、采完被 close_tabs 关掉致店铺页签丢失），再 paste_image 以图搜图；"
        "落地结果页后先确认「框选主体」选对了"
        "（Temu 主图多为营销拼图，默认主体常选错→召回不相干品类），品类不对就点顶部【目标商品主体的裁剪缩略图】"
        "切换主体、或用「框选主体」重框；主体选对后在结果里滤掉不相干品类、只看真正同款，挑常规批发价最便宜的一家进详情页。"
        "关键词兜底只在图搜实在无果时用，且要在页面搜索框 input_text 输入再回车（勿手拼 offer_search.htm?keywords= 的 URL，会 GBK 乱码）。\n"
        "3. 选与本商品一致的规格，读【常规批发价+运费】作采购价（务必剔除新人价/首单价/优惠券等一次性优惠）；"
        "重量不照抄平台，据尺寸/材质/填充推测。红线：绝不下单/支付。\n"
        f"4. 用 wps_excel_tool 先 inspect 再 append_product_row，把该商品追加到 {excel} 的「{sheet}」表："
        "站点(A)/类目(B)/SPU(D)/销售价(I 纯数字, 剥掉¥)/日常价(G 同)/采购价(J)/重量(K 单位公斤, 如 0.05)/"
        "ros(O=7) 走 column_values，公式列 H/L/N/P/Q/R 照 inspect 的同列公式（把行号写成 {r} 占位符随行自适应）；"
        "主图 image_path 传步骤1的路径、image_column=E（产品图片列；F 是货号列，勿写图）。\n"
        "5. 用 browser_use 的 close_tabs(text=\"1688\") 关掉本商品开的 1688 标签。\n"
        "6. 完成后用 terminate 结束。只处理这一个商品，不要去碰列表里的其它商品。"
    )


async def collect_one(
    agent: Manus, item: dict, excel: str = DEFAULT_EXCEL, sheet: str = DEFAULT_SHEET
) -> bool:
    """采集单个商品（agent 兜底路径），带超时 + 重试护栏。返回是否已入库。"""
    spu = str(item.get("spu", ""))
    for attempt in range(1, PRODUCT_RETRIES + 1):
        reset_agent(agent)
        try:
            await asyncio.wait_for(
                agent.run(per_product_prompt(item, excel, sheet)), timeout=PRODUCT_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"⏱️ SPU={spu} 第 {attempt}/{PRODUCT_RETRIES} 次超时"
                f"（>{PRODUCT_TIMEOUT}s），放弃本次"
            )
        except Exception as e:
            logger.error(f"SPU={spu} 第 {attempt}/{PRODUCT_RETRIES} 次异常：{e}")
        finally:
            try:  # 兜底清理本商品可能残留的 1688 标签
                await agent.available_tools.get_tool("browser_use").execute(
                    action="close_tabs", text="1688"
                )
            except Exception:
                pass

        if spu in WpsExcelTool.existing_key_values(excel, sheet, spu_col_of(excel, sheet)):
            return True
        if attempt < PRODUCT_RETRIES:
            logger.info(f"↻ SPU={spu} 未入库，重试（{attempt + 1}/{PRODUCT_RETRIES}）")
    return False


async def collect_one_pipeline(
    agent: Manus,
    item: dict,
    excel: str = DEFAULT_EXCEL,
    sheet: str = DEFAULT_SHEET,
    schema: Optional["SheetSchema"] = None,
) -> "CollectOutcome":
    """确定性管道采集单商品（阶段二），失败退回 agent 兜底。

    schema：批次开始时解析好的目标 Sheet 写入结构，全批复用（见 pipeline.SheetSchema）；
    未传则 write_product_row 内部就地解析一次。
    返回 CollectOutcome（携带落库结果与来源），供上层生成结构化进度事件。
    """
    from app.collect.pipeline import (
        archive_unmatched_image,
        collect_one_product,
        judge_price,
        read_detail_price,
        write_product_row,
    )

    spu = str(item.get("spu", ""))
    browser_tool = agent.available_tools.get_tool("browser_use")
    excel_tool = WpsExcelTool()
    img_path = os.path.join(str(config.output_dir("image")), f"{spu}.jpeg")

    reset_pipeline_llms()  # 单商品护栏：清零 samematch/default 单例 token 计数

    # 比价回调：纯 DOM 读单个候选的货价/运费/重量（不开 agent、不进结算）。
    async def _checkout_probe(offer: dict):
        oid = str(offer.get("offerId", ""))
        detail_url = offer.get("detailUrl", "")
        if not oid or not detail_url:
            return None
        try:
            price_block = await read_detail_price(browser_tool, detail_url)
        except Exception as e:
            logger.warning(f"读价探测异常 offer={oid}：{e}")
            price_block = None
        finally:
            try:  # 兜底关标签，避免候选间标签堆积
                await browser_tool.execute(action="close_tabs", text="1688")
            except Exception:
                pass
        if not price_block or not price_block.get("mainPrice"):
            logger.warning(f"读价探测无价格 offer={oid}（详情页未读到主价区）")
            return None
        price = await judge_price(price_block)
        if not price or price.get("purchase_price") is None:
            logger.warning(f"读价探测判价失败 offer={oid}")
            return None
        goods = price["purchase_price"]
        shipping = price.get("shipping", 0) or 0
        info = {
            "offer_id": oid,
            "goods_price": goods,
            "shipping": shipping,
            "total": goods + shipping,
            "sku_desc": offer.get("title", ""),
            "weight_g": price.get("weight_g"),
            "weight_basis": price.get("note", ""),
        }
        logger.info(
            f"读价探测 offer={oid}：货价={info['goods_price']} 运费={info['shipping']} "
            f"总价={info['total']} 重量={info['weight_g']}g（{info['weight_basis']}）"
            f" | {detail_url}"
        )
        return info

    try:
        res = await asyncio.wait_for(
            collect_one_product(browser_tool, item, checkout_probe=_checkout_probe),
            timeout=CHECKOUT_COMPARE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ SPU={spu} 管道超时（>{CHECKOUT_COMPARE_TIMEOUT}s），退回 agent 兜底")
        res = None
    except Exception as e:
        logger.error(f"SPU={spu} 管道异常：{e}，退回 agent 兜底")
        res = None

    if res is not None and res.ok:
        wrote, msg = await write_product_row(
            excel_tool, excel, sheet, item, res, img_path, schema=schema
        )
        try:
            await browser_tool.execute(action="close_tabs", text="1688")
        except Exception:
            pass
        if wrote and spu in WpsExcelTool.existing_key_values(
            excel, sheet, spu_col_of(excel, sheet)
        ):
            logger.info(
                f"管道成功：SPU={spu} offer={res.offer_id} 采购价={res.purchase_price} "
                f"运费={res.shipping} 重量={res.weight_g}g {('存疑:' + res.note) if res.note else ''}"
                f" | 选中货源：{res.detail_url}"
            )
            return CollectOutcome.from_result(spu, True, "ok", res, via="pipeline")
        logger.warning(f"SPU={spu} 管道判断成功但写入未确认（{msg}），退回 agent 兜底")
    else:
        reason = res.fail_reason if res is not None else "超时/异常"
        # 「无同款」跳过 agent 兜底：写入留空行、采购价/重量待人工补，记为已处理。
        if res is not None and res.no_same_match:
            wrote, msg = await write_product_row(
                excel_tool, excel, sheet, item, res, img_path, schema=schema
            )
            archived = archive_unmatched_image(spu)
            try:
                await browser_tool.execute(action="close_tabs", text="1688")
            except Exception:
                pass
            if wrote:
                extra = f"，主图已归档 → {archived}" if archived else ""
                logger.info(
                    f"⬜ SPU={spu} 无同款 → 已入库留空行（采购价/重量待人工补）{extra}"
                )
                return CollectOutcome(
                    spu=spu, ok=True, status="empty", note="无同款，留空待人工补",
                    via="pipeline",
                )
            logger.warning(f"SPU={spu} 无同款且留空行写入失败（{msg}），跳过")
            return CollectOutcome(spu=spu, ok=False, status="fail", note=msg, via="pipeline")
        logger.warning(f"SPU={spu} 管道未成（{reason}），退回 agent 兜底")

    # 兜底：确定性管道失败（非无同款）→ 走旧 agent 自由循环路径
    try:
        await browser_tool.execute(action="close_tabs", text="1688")
    except Exception:
        pass
    ok = await collect_one(agent, item, excel, sheet)
    return CollectOutcome(
        spu=spu, ok=ok, status="ok" if ok else "fail", via="agent",
        note="" if ok else "agent 兜底仍未入库",
    )


class CollectOutcome:
    """单商品采集结果的轻量载体：供 service 层生成结构化进度事件。

    与 pipeline.CollectResult 的区别：CollectResult 是管道内部细粒度结果；
    CollectOutcome 是 service 对上（UI/CLI）暴露的统一结果，含来源 via 与状态 status。
    """

    __slots__ = (
        "spu", "ok", "status", "offer_id", "purchase_price", "shipping",
        "weight_g", "detail_url", "note", "via",
    )

    def __init__(
        self, spu: str, ok: bool, status: str, via: str = "pipeline",
        offer_id: Optional[str] = None, purchase_price: Optional[float] = None,
        shipping: Optional[float] = None, weight_g: Optional[float] = None,
        detail_url: Optional[str] = None, note: str = "",
    ):
        self.spu = spu
        self.ok = ok
        self.status = status  # "ok" | "base"（仅基础信息、价/重留空）| "empty"（无同款留空行）| "fail"
        self.via = via  # "pipeline" | "agent" | "base"
        self.offer_id = offer_id
        self.purchase_price = purchase_price
        self.shipping = shipping
        self.weight_g = weight_g
        self.detail_url = detail_url
        self.note = note

    @classmethod
    def from_result(cls, spu, ok, status, res, via) -> "CollectOutcome":
        return cls(
            spu=spu, ok=ok, status=status, via=via,
            offer_id=res.offer_id, purchase_price=res.purchase_price,
            shipping=res.shipping, weight_g=res.weight_g,
            detail_url=res.detail_url, note=res.note or "",
        )

    def to_event(self, index: int, total: int) -> dict:
        return {
            "type": "product_done", "index": index, "total": total,
            "spu": self.spu, "status": self.status, "via": self.via,
            "offer_id": self.offer_id, "purchase_price": self.purchase_price,
            "shipping": self.shipping, "weight_g": self.weight_g,
            "detail_url": self.detail_url, "note": self.note,
        }


async def collect_one_base(
    item: dict,
    excel: str = DEFAULT_EXCEL,
    sheet: str = DEFAULT_SHEET,
    schema: Optional["SheetSchema"] = None,
) -> "CollectOutcome":
    """基础采集单商品：只写 Temu 基础行（站点/类目/SPU/销售价/主图），采购价(J)/重量(K)
    留空待人工填。【不跑 1688 图搜/判同款/读价】，故不用浏览器/CDP/LLM，纯下图 + 写 Excel。

    复用 pipeline.write_product_row 的留空写入逻辑（res.purchase_price/weight_g 为 None →
    对应列写空串），与「无同款留空行」同一套机制，只是这里【无条件】留空。
    """
    from app.collect.pipeline import (
        CollectResult,
        _download_main_image,
        write_product_row,
    )

    spu = str(item.get("spu", ""))
    excel_tool = WpsExcelTool()
    img_path = os.path.join(str(config.output_dir("image")), f"{spu}.jpeg")

    # 主图：能下就下（供 Excel 产品图片列），下不到只告警、仍写基础行。
    if not os.path.exists(img_path) and item.get("image"):
        try:
            await asyncio.to_thread(_download_main_image, item["image"], img_path)
        except Exception as e:
            logger.warning(f"SPU={spu} 主图下载失败（忽略，仍写基础行）：{e}")

    res = CollectResult(spu=spu, ok=True, note="采购价/重量待人工填")
    wrote, msg = await write_product_row(
        excel_tool, excel, sheet, item, res, img_path, schema=schema
    )
    if wrote and spu in WpsExcelTool.existing_key_values(
        excel, sheet, spu_col_of(excel, sheet)
    ):
        logger.info(f"⬜ SPU={spu} 基础行已写入（采购价/重量留空待人工填）")
        return CollectOutcome(
            spu=spu, ok=True, status="base", via="base", note="采购价/重量待人工填",
        )
    logger.warning(f"SPU={spu} 基础行写入失败（{msg}）")
    return CollectOutcome(spu=spu, ok=False, status="fail", via="base", note=msg)


async def run_batch(
    limit: int = 20,
    use_pipeline: bool = True,
    on_progress: ProgressCB = None,
    agent: Optional[Manus] = None,
    excel: Optional[str] = None,
    sheet: Optional[str] = None,
    store: Optional[str] = None,
    base_only: bool = True,
) -> dict:
    """跑一批未入库商品的采集，进度经 on_progress 抛出。返回汇总 {ok, fail, batch}。

    - excel/sheet 缺省回填「上次选择」偏好、再兜底出厂默认；store 非空则只采该店商品。
      本次组合成功启动后 save_prefs 记住，供下次缺省回填。
    - base_only=True（默认）：仅采 Temu 基础信息、采购价/重量留空待人工填，【不跑 1688】——
      不开 agent、不连 CDP、不用 LLM，最省。此时 use_pipeline 被忽略。
    - base_only=False：走 1688 自动采价。agent 为空则内部创建并在结束时清理；调用方传入
      则复用、由调用方负责清理（UI 场景可复用长驻 agent，避免每批重开浏览器/MCP）。
      use_pipeline=True 走确定性管道（推荐），失败自动退回 agent 兜底。
    - Excel 被占用 / 清单为空 / CDP 不可用（仅 1688 模式）→ 抛结构化事件并提前返回，不空跑。
    """
    prefs = load_prefs()
    excel = excel or prefs.get("excel") or DEFAULT_EXCEL
    sheet = sheet or prefs.get("sheet") or DEFAULT_SHEET
    if store is None:
        store = prefs.get("store") or ""

    worklist = load_worklist()
    if store:  # 只采选中店铺的商品
        worklist = [it for it in worklist if _store_key(it) == store]
    if not worklist:
        reason = (
            f"选中店铺（{store}）在工作清单里没有商品。"
            if store
            else f"工作清单为空（{WORKLIST}）。请先枚举（--refresh / enumerate）。"
        )
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        logger.error(reason)
        return {"ok": 0, "fail": 0, "batch": 0}

    if excel_write_locked(excel):
        msg = (
            f"Excel 正被占用（疑似 WPS/Excel 打开中），无法写入：{excel}。"
            "请先在 WPS/Excel 里关闭该文件，再重跑。"
        )
        await _emit(on_progress, {"type": "aborted", "reason": msg})
        logger.error("❌ " + msg)
        return {"ok": 0, "fail": 0, "batch": 0}

    # 记住本次选择（工作簿/Sheet/店铺），供下次 UI/CLI 缺省回填
    save_prefs(excel, sheet, store)

    done = WpsExcelTool.existing_key_values(excel, sheet, spu_col_of(excel, sheet))
    todo = [
        it for it in worklist
        if str(it.get("spu", "")).strip() and str(it["spu"]) not in done
    ]
    batch = min(limit, len(todo))
    mode_label = "基础(价/重人工填)" if base_only else ("管道" if use_pipeline else "agent")
    logger.info(
        f"=== 采集批次：模式={mode_label} 工作簿={excel} Sheet={sheet} 店铺={store or '全部'}；"
        f"清单 {len(worklist)} 个，已入库 {len(done)}，待采 {len(todo)}，本批 {batch} 个 ==="
    )
    await _emit(on_progress, {
        "type": "batch_start", "total": len(worklist),
        "done_existing": len(done), "todo": len(todo), "batch": batch,
    })

    # 批次开始就解析一次目标 Sheet 的写入结构（列映射/公式/常量），全批复用——各 Sheet
    # 列序不同，必须按真实表头写；这份结构一批恒定，不必逐商品重 inspect 大工作簿。
    # 结构认不出（如缺 SPU 列）→ 直接中止本批，避免逐个商品去撞同一个错误。
    # 基础模式与管道模式都要按真实表头写，故都先解析（纯 agent 兜底路径自己 inspect，跳过）。
    pipe_schema = None
    if base_only or use_pipeline:
        from app.collect.pipeline import resolve_sheet_schema

        pipe_schema = await resolve_sheet_schema(WpsExcelTool(), excel, sheet)
        if not pipe_schema.ok:
            await _emit(on_progress, {"type": "aborted", "reason": pipe_schema.error})
            logger.error(f"❌ 目标表结构不可写，中止本批：{pipe_schema.error}")
            return {"ok": 0, "fail": 0, "batch": 0}
        logger.info(
            f"目标表结构已解析：字段列={pipe_schema.fields} "
            f"公式列={list(pipe_schema.formula_columns)} 常量列={pipe_schema.constant_columns}"
        )

    ok = fail = 0

    # 基础模式：只写 Temu 基础行、价/重留空待人工填。不开 agent、不连 CDP、不用 LLM。
    if base_only:
        for i, item in enumerate(todo[:limit], 1):
            spu = item.get("spu")
            name = item.get("name", "")
            logger.info(f"--- [{i}/{batch}] 基础采集 SPU={spu}（{name[:20]}）---")
            await _emit(on_progress, {
                "type": "product_start", "index": i, "total": batch,
                "spu": spu, "name": name,
            })
            outcome = await collect_one_base(item, excel, sheet, schema=pipe_schema)
            await _emit(on_progress, outcome.to_event(i, batch))
            if outcome.ok:
                ok += 1
            else:
                fail += 1
                logger.warning(f"⚠️ SPU={spu} 未入库（继续下一个）")
        logger.info(f"=== 本批完成：成功 {ok}，失败 {fail} ===")
        await _emit(on_progress, {"type": "batch_done", "ok": ok, "fail": fail})
        return {"ok": ok, "fail": fail, "batch": batch}

    # 1688 自动采价模式：需要 agent（浏览器/MCP）+ CDP。
    own_agent = agent is None
    if own_agent:
        agent = await Manus.create()
    try:
        agent.max_steps = 20  # 单商品收紧步数控成本

        for i, item in enumerate(todo[:limit], 1):
            spu = item.get("spu")
            name = item.get("name", "")
            logger.info(f"--- [{i}/{batch}] 采集 SPU={spu}（{name[:20]}）---")
            await _emit(on_progress, {
                "type": "product_start", "index": i, "total": batch,
                "spu": spu, "name": name,
            })

            # CDP 健康检查：断了就确定性重连；连续失败达上限则放弃整批。
            if not await ensure_cdp_alive():
                await _emit(on_progress, {
                    "type": "aborted",
                    "reason": "CDP 不可用（Chrome 未以 9222 调试端口运行？），已中止本批。",
                })
                logger.error("CDP 不可用，中止本批（已入库的不受影响，可稍后续跑）。")
                break

            if use_pipeline:
                outcome = await collect_one_pipeline(
                    agent, item, excel, sheet, schema=pipe_schema
                )
            else:
                got = await collect_one(agent, item, excel, sheet)
                outcome = CollectOutcome(
                    spu=str(spu), ok=got, status="ok" if got else "fail", via="agent",
                )

            await _emit(on_progress, outcome.to_event(i, batch))
            if outcome.ok:
                ok += 1
                logger.info(f"✅ SPU={spu} 已写入")
            else:
                fail += 1
                logger.warning(f"⚠️ SPU={spu} 未入库（继续下一个）")
    finally:
        if own_agent:
            await agent.cleanup()

    logger.info(f"=== 本批完成：成功 {ok}，失败/存疑 {fail} ===")
    await _emit(on_progress, {"type": "batch_done", "ok": ok, "fail": fail})
    return {"ok": ok, "fail": fail, "batch": batch}
