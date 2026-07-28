# -*- coding: utf-8 -*-
"""订单登记管线的编排层：CDP 护栏 → 页面导出 → 解析 join → 分表判重 → 批量写入。

对标 app/collect/service.py 与 app/activity/service.py 的三层结构：pipeline 出确定性
动作，service 管顺序、护栏与结构化进度事件（UI 的 SSE 和 CLI 共用同一套 event dict）。

两条硬护栏，都是「宁可不写，不写错」：
  - 店铺名识别不到就中止：店铺决定订单落到哪张表，猜错等于把订单写进别人家的 Sheet，
    比不写更糟且要人工回滚。调用方可显式传 store 覆盖。
  - 未命中 sheet_map 的订单一律跳过并在汇总里报告，绝不臆测落点。

`dry_run=True` 走完全部只读步骤（含导出与解析），只把「将写入什么」算出来给人核对，
不碰登记表。方案文档要求实机 dry-run 人工确认后才开写入。
"""
import asyncio
import csv
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import async_playwright

from app.collect.service import (
    CDP_URL,
    ProgressCB,
    _emit,
    ensure_cdp_alive,
    excel_write_locked,
    list_workbooks,
)
from app.config import PROJECT_ROOT, config, get_output_dir
from app.logger import logger
from app.orders import pipeline
from app.tool.wps_excel_tool import WpsExcelTool

# 整批护栏：翻完 N 页 + 导出 + 下 N 张图，247 条实测约 3~5 分钟，给足余量。
ORDERS_BATCH_TIMEOUT = 1800
# 判重键的默认列标题（登记表没有子订单号列，见 docs/orders-pipeline-plan.md §5）。
_DEFAULT_DEDUPE_BY = ["订单号", "尺码"]
# 订单页「上次选择」（店铺/工作簿/Sheet）。与采集页的 collect_prefs.json 分开存。
ORDERS_PREFS = config.workspace_root / "orders_prefs.json"


@dataclass
class SheetPlan:
    """一个目标 Sheet 的写入计划（dry-run 展示与实写共用同一份数据）。"""

    sheet: str
    store_value: str
    header: Dict[str, str] = field(default_factory=dict)
    header_row: int = 1
    image_col: Optional[str] = None
    rows: List[dict] = field(default_factory=list)   # 待写：append_rows 的入参格式
    preview: List[dict] = field(default_factory=list)  # 人工核对用：{列标题: 值}
    dup: int = 0
    no_key: bool = False


def load_orders_config() -> dict:
    """读 [orders] 段：config.toml 优先，**该段缺失时退到 config.example.toml**。

    为什么这里要「按段」回退，而不是像 activity 那样只读第一个存在的文件：现网
    config.toml 里根本没有 [orders] 段（用户配置只写了 llm/browser 等），只读它会拿到
    空配置、整个功能直接中止。而 [orders] 的默认值（登记表路径、sheet_map）没法在代码里
    兜底——猜错就是写错表。所以让随仓库分发的 example 充当默认值，用户在 config.toml
    写了 [orders] 就完全覆盖它。解析失败只告警返回 {}，由调用方按缺字段中止。
    """
    for name in ("config.toml", "config.example.toml"):
        p = PROJECT_ROOT / "config" / name
        if not p.exists():
            continue
        try:
            with p.open("rb") as f:
                section = tomllib.load(f).get("orders") or {}
            if section:
                return section
        except Exception as e:
            logger.warning(f"读 {name} 的 [orders] 配置失败：{e}")
    return {}


def load_prefs() -> dict:
    """读上次选择 {store, workbook, sheet}；缺失/损坏返回 {}（best-effort，不抛错）。

    单独存一份而不是复用 collect_prefs.json：采集页选的是「成本核算表 + 采集 Sheet」，
    订单页选的是「订单登记表 + 登记 Sheet」，两者是不同的工作簿，混用会互相踩掉。
    """
    if not ORDERS_PREFS.exists():
        return {}
    try:
        data = json.loads(ORDERS_PREFS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_prefs(store: str = "", workbook: str = "", sheet: str = "") -> None:
    """记住本次选择，供下次 UI 缺省回填。写失败只告警、不阻断本批。"""
    try:
        ORDERS_PREFS.parent.mkdir(parents=True, exist_ok=True)
        ORDERS_PREFS.write_text(
            json.dumps(
                {"store": store, "workbook": workbook, "sheet": sheet},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"保存订单页偏好失败（忽略）：{e}")


def inspect_sheet(workbook: str, sheet: str, dedupe_by: Optional[List[str]] = None) -> dict:
    """探测某 Sheet 能不能写：判重列是否齐、图片列在哪、表头行第几行。

    为什么 UI 必须先看这个：判重列（订单号+尺码）缺任一列时 service 会把整表标 no_key
    并跳过——一行都不写。让用户在点「开始」前就知道，而不是跑完几分钟导出才发现白跑。
    纯读，异常返回 writable=False，不抛错。
    """
    titles = dedupe_by or _DEFAULT_DEDUPE_BY
    try:
        header, header_row, image_col = _sheet_schema(workbook, sheet)
    except Exception as e:
        logger.warning(f"探测 Sheet「{sheet}」失败：{e}")
        return {"sheet": sheet, "writable": False, "reason": f"读表头失败：{e}"}
    if not header:
        return {"sheet": sheet, "writable": False, "reason": "读不到表头"}

    cols = {t: pipeline.resolve_title_column(header, t) for t in titles}
    missing = [t for t, c in cols.items() if not c]
    return {
        "sheet": sheet,
        "writable": not missing,
        "reason": f"缺判重列：{'、'.join(missing)}" if missing else "",
        "header_row": header_row,
        "image_col": image_col,
        "dedupe_cols": cols,
        # 表头标题列表供 UI 展示「这张表有哪些列会被写」
        "titles": [header[c] for c in sorted(header, key=pipeline._col_key)],
    }


def get_worklist_status(
    store: Optional[str] = None,
    workbook: Optional[str] = None,
    sheet: Optional[str] = None,
) -> dict:
    """订单页首屏：可选工作簿/Sheet 列表 + 上次选择回显 + 选中 Sheet 的可写性。

    缺省值优先级：显式传入 > 上次选择 > config 的 [orders].workbook。
    工作簿枚举复用采集 service 的 list_workbooks（同一数据源，不重复造）。
    纯读、不触发任何采集或写入。
    """
    cfg = load_orders_config()
    prefs = load_prefs()
    dedupe_by = list(cfg.get("dedupe_by") or _DEFAULT_DEDUPE_BY)

    workbook = workbook or prefs.get("workbook") or str(cfg.get("workbook") or "")
    if store is None:
        store = prefs.get("store") or ""
    if sheet is None:
        sheet = prefs.get("sheet") or ""

    workbooks = list_workbooks()
    if workbook and workbook not in workbooks:
        workbooks = [workbook] + workbooks   # 手填过的路径也要能回显选中
    sheets = WpsExcelTool.list_sheets(workbook) if workbook else []
    # WPS 的嵌入图索引表不是数据表，别让用户选中它
    sheets = [s for s in sheets if not s.startswith("WpsReserved")]
    # 换了工作簿导致选中的 Sheet 不存在 → 视为未选
    sheet = sheet if sheet in sheets else ""

    exists = bool(workbook) and Path(workbook).exists()
    return {
        "store": store,
        "workbook": workbook,
        "workbook_exists": exists,
        "workbook_locked": exists and excel_write_locked(workbook),
        "workbooks": workbooks,
        "sheet": sheet,
        "sheets": sheets,
        "dedupe_by": dedupe_by,
        # 店铺候选：sheet_map 里配过的店铺名，供下拉，仍允许手填
        "store_options": sorted(
            {str(m.get("store", "")).strip() for m in (cfg.get("sheet_map") or [])
             if str(m.get("store", "")).strip()}
        ),
        "sheet_info": (
            inspect_sheet(workbook, sheet, dedupe_by) if exists and sheet else {}
        ),
    }


def _sheet_schema(workbook: str, sheet: str) -> tuple:
    """解析一次目标 Sheet 的表头结构，全批复用：(header, header_row, image_col)。

    13 个登记 Sheet 的表头行位置（第 1 或第 2 行）与列序都不一样，且工作簿 102MB——
    每行都去解析一次表头等于把 zip 解压几百遍。
    """
    header_row = WpsExcelTool.detect_header_row(workbook, sheet)
    header = WpsExcelTool.read_header(workbook, sheet, header_row=header_row)
    return header, header_row, pipeline.image_column(header)


def plan_writes(
    orders: List[pipeline.OrderRow],
    workbook: str,
    store: str,
    sheet_map: List[dict],
    dedupe_by: Optional[List[str]] = None,
    require_price: bool = True,
) -> tuple:
    """把订单按目标 Sheet 分组、判重，产出 {sheet: SheetPlan}、未映射清单、无价清单。

    返回 (plans, unmapped, unpriced)。两类跳过语义完全不同，刻意分开报：
      - unmapped：店铺+站点没配 sheet_map，是配置缺失，要人工补配置才能登记。
        绝不臆测落点（`StoreA牛仔裤` 是列结构完全不同的专用表，不是 StoreA 美国站）。
      - unpriced：成交单价是商家助手插件注入 DOM 的、不是 Temu 官方字段，实测回填有约
        一天延迟（2026-07-28 实机：当天 12 单全部无「成交单价」标签，前一天的都有）。
        这类订单**本批不登记、留到下批**：判重键是订单号+尺码，一旦带空价入库，下次重跑
        会判定已入库而永不补价，那一格就只能人工填。等一天比人工补一列划算。
        require_price=False 可关掉这条规则（明确要先占位时用）。
    """
    titles = dedupe_by or _DEFAULT_DEDUPE_BY
    plans: Dict[str, SheetPlan] = {}
    unmapped: List[dict] = []
    unpriced: List[dict] = []
    seen: Dict[str, set] = {}  # sheet → 已排入本批的键，防同批内重复

    # 本批按创建时间倒序：登记表要求「时间越新的越在上面」，而写入走的是插到表头下方，
    # 所以排在前面的落在更上面。Temu 页面本来就是新→旧，但那是页面顺序、不是契约，
    # 显式排一遍才是这条规则的实现。时间为空的排最后；stable sort 保证同一时间的
    # 多个尺码行维持原相对次序。
    orders = sorted(orders, key=lambda x: str(x.created_at or ""), reverse=True)

    for o in orders:
        # 无价先拦：早于分表/判重，免得白读表头。判重键含尺码，带空价入库就补不回来了
        if require_price and not str(o.deal_price or "").strip():
            unpriced.append({
                "order_no": o.order_no, "sub_order_no": o.sub_order_no,
                "site": o.site, "created": o.created_at,
                "reason": "页面暂无「成交单价」（插件回填有延迟），留到下批登记",
            })
            continue

        cfg = pipeline.resolve_sheet(store, o.site, sheet_map)
        if not cfg:
            unmapped.append({
                "order_no": o.order_no, "sub_order_no": o.sub_order_no,
                "site": o.site, "reason": f"店铺「{store}」+ 站点「{o.site}」未配置 sheet_map",
            })
            continue

        sheet = str(cfg.get("sheet") or "")
        plan = plans.get(sheet)
        if plan is None:
            header, header_row, image_col = _sheet_schema(workbook, sheet)
            plan = SheetPlan(
                sheet=sheet, store_value=str(cfg.get("store_value") or store),
                header=header, header_row=header_row, image_col=image_col,
            )
            plans[sheet] = plan
            seen[sheet] = _existing_keys(workbook, plan, titles)
            if not header:
                logger.warning(f"Sheet「{sheet}」读不到表头，本批跳过它")

        _stage_order(o, plan, titles, seen[sheet])

    return plans, unmapped, unpriced


def _existing_keys(workbook: str, plan: SheetPlan, titles: List[str]) -> set:
    """读该 Sheet 已入库的判重键集合。任一判重列缺失 → 标记 no_key 并返回空集。

    no_key 的 Sheet 会被 _stage_order 整体跳过：没法判重就意味着重复跑会堆重复行，
    宁可不写。
    """
    cols: List[str] = []
    for t in titles:
        col = pipeline.resolve_title_column(plan.header, t)
        if not col:
            plan.no_key = True
            logger.warning(f"Sheet「{plan.sheet}」找不到判重列「{t}」，本批跳过它")
            return set()
        cols.append(col)
    return WpsExcelTool.existing_key_tuples(
        workbook, plan.sheet, cols, header_row=plan.header_row
    )


def _stage_order(
    o: pipeline.OrderRow, plan: SheetPlan, titles: List[str], seen: set
) -> None:
    """把一条订单排进某 Sheet 的写入计划；已入库或本批内重复的只计数不排。"""
    if plan.no_key or not plan.header:
        return
    key = pipeline.dedupe_key(o, plan.header, titles, plan.store_value)
    if key is None:
        plan.no_key = True
        return
    if key in seen:
        plan.dup += 1
        return
    seen.add(key)

    values = pipeline.build_row_values(o, plan.header, plan.store_value)
    item: Dict[str, Any] = {"values": values}
    # 图片列只在真下到本地图时才带上；没图的行照常入库（图是辅助字段）
    if plan.image_col and o.image_path:
        item["image_column"] = plan.image_col
        item["image_path"] = o.image_path
    plan.rows.append(item)
    plan.preview.append({
        plan.header.get(c, c): v for c, v in sorted(values.items())
    } | {"_图片": "有" if item.get("image_path") else "无"})


async def collect_orders(
    list_url: str,
    store: str = "",
    on_progress: ProgressCB = None,
    max_pages: int = 200,
) -> dict:
    """连 CDP、开专用页签、翻页勾选抓图、触发导出、解析。返回 {orders, store, stat...}。

    专用页签模式同活动管线：绝不复用用户正在操作的页签——它的筛选条件、弹窗和生命周期
    都不受管线控制，用户随手一关就把整批打断。用完即关，不碰用户原有页签。
    """
    pw = await async_playwright().start()
    browser = None
    page = None
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("CDP 浏览器没有可用 context")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        await page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        try:
            from app.activity.pipeline import dismiss_all_page_popups

            await dismiss_all_page_popups(page)
        except Exception as e:
            logger.warning(f"关弹窗失败（继续）：{e}")
        await page.wait_for_selector("table tbody tr", timeout=60000)

        detected = store or await pipeline.detect_store(page)
        if not detected:
            raise RuntimeError(
                "识别不到当前登录店铺名，已中止：店铺决定订单写进哪张表，"
                "猜错会把订单写进别人家的 Sheet。请在界面/CLI 显式指定店铺。"
            )
        await _emit(on_progress, {"type": "store", "store": detected})

        async def _on_page(info: dict) -> None:
            await _emit(on_progress, {"type": "page", **info})

        swept = await pipeline.sweep_pages(page, on_page=_on_page, max_pages=max_pages)
        await _emit(on_progress, {
            "type": "swept", "pages": swept["pages"], "total": swept["total"],
            "selected": swept["selected"], "images": len(swept["images"]),
            "truncated": swept.get("truncated", False),
        })

        out_dir = str(get_output_dir("orders_export"))
        xlsx = await pipeline.trigger_export(page, out_dir)
        await _emit(on_progress, {"type": "exported", "file": xlsx})

        orders = pipeline.parse_export_xlsx(xlsx)
        join_stat = pipeline.join_images(orders, swept["images"])
        await _emit(on_progress, {
            "type": "parsed", "rows": len(orders),
            "orders": len({o.order_no for o in orders}), **join_stat,
        })
        return {
            "orders": orders, "store": detected, "export_file": xlsx,
            "total": swept["total"], "pages": swept["pages"], "join": join_stat,
            # 被 max_pages 截断＝本批不是全量，一路传到汇总，别让人误读成「全跑完了」
            "truncated": swept.get("truncated", False),
        }
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception as e:
                logger.warning(f"关闭订单页签失败（忽略）：{e}")
        if browser is not None:
            try:
                await browser.close()
            except Exception as e:
                logger.warning(f"关闭 CDP 连接失败（忽略）：{e}")
        await pw.stop()


def _explicit_sheet_map(store: str, sheet: str, cfg: dict) -> List[dict]:
    """用户在 UI 里显式选了目标 Sheet → 合成一条通配映射，覆盖配置里的 sheet_map。

    `sites = []` 在 resolve_sheet 里就是「不限站点」，所以本批所有订单都进这张表——这正是
    显式选择的语义：用户已经自己判断过落点，不再按站点分流。

    「订单店铺」列写什么：优先照抄 sheet_map 里同名 Sheet 的 store_value（表内既有写法，
    如 StoreA 的表里写的是「StoreA全球」），没有就用用户填的店铺名。
    """
    store_value = store
    for m in cfg.get("sheet_map") or []:
        if str(m.get("sheet", "")).strip() == sheet:
            store_value = str(m.get("store_value") or store)
            break
    return [{"store": store, "sites": [], "sheet": sheet, "store_value": store_value}]


async def run_orders_batch(
    store: str = "",
    dry_run: bool = True,
    workbook: str = "",
    list_url: str = "",
    on_progress: ProgressCB = None,
    max_pages: int = 200,
    sheet: str = "",
    require_price: bool = True,
) -> dict:
    """整批入口：预检 → 采集 → 计划 → （dry_run 则止步）→ 批量写入 → 汇总。

    dry_run 默认 True：写登记表是不可逆操作（虽有自动备份），方案文档要求实机 dry-run
    结果人工确认后才开写入。UI/CLI 必须显式传 dry_run=False 才会落盘。

    sheet 非空＝用户显式指定落点，绕过 sheet_map 的站点分流（见 _explicit_sheet_map）；
    此时 store 也必须显式给出，因为合成映射要拿它当匹配键。

    require_price 默认 True：页面还没注入成交单价的订单本批不登记（见 plan_writes）。
    """
    cfg = load_orders_config()
    workbook = workbook or str(cfg.get("workbook") or "")
    list_url = list_url or str(cfg.get("list_url") or "")
    dedupe_by = list(cfg.get("dedupe_by") or _DEFAULT_DEDUPE_BY)
    sheet = (sheet or "").strip()
    if sheet:
        if not store:
            reason = "显式指定 Sheet 时必须同时指定店铺（要写进「订单店铺」列）"
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            return _summary(dry_run, aborted=reason)
        sheet_map = _explicit_sheet_map(store, sheet, cfg)
    else:
        sheet_map = list(cfg.get("sheet_map") or [])

    missing = [n for n, v in (("workbook", workbook), ("list_url", list_url)) if not v]
    if missing or not sheet_map:
        reason = f"[orders] 配置不完整：缺 {missing or ['sheet_map']}"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    if not Path(workbook).exists():
        reason = f"登记表不存在：{workbook}"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    # 占用预检只在真要写时做：dry-run 全程只读，表开着也能跑。放在采集【之前】是为了
    # 别让人白等几分钟翻页导出，最后卡在「文件被 WPS 锁着」。
    if not dry_run and excel_write_locked(workbook):
        reason = (
            f"登记表正被占用（疑似 WPS/Excel 打开中），无法写入：{workbook}\n"
            "请先在 WPS/Excel 里关闭该文件再重跑。"
        )
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)

    if not await ensure_cdp_alive():
        reason = f"CDP 不可用（{CDP_URL}），请确认调试 Chrome 已启动"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)

    await _emit(on_progress, {
        "type": "started", "dry_run": dry_run, "workbook": workbook,
        # sheet 非空表示本批不按站点分流，全部进这张表——务必让用户在日志里看见
        "sheet": sheet, "store": store,
    })
    try:
        got = await asyncio.wait_for(
            collect_orders(list_url, store=store, on_progress=on_progress,
                           max_pages=max_pages),
            timeout=ORDERS_BATCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        reason = f"整批超时（>{ORDERS_BATCH_TIMEOUT}s），未写入任何数据"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    except Exception as e:
        logger.error(f"订单采集失败：{e}")
        await _emit(on_progress, {"type": "aborted", "reason": f"采集失败：{e}"})
        return _summary(dry_run, aborted=str(e))

    orders: List[pipeline.OrderRow] = got["orders"]
    # 图必须先落地才能嵌入：DISPIMG 是把图片字节写进 zip，不是存 URL
    img_stat = pipeline.download_images(orders, str(get_output_dir("orders_image")))
    await _emit(on_progress, {"type": "images", **img_stat})

    plans, unmapped, unpriced = plan_writes(
        orders, workbook, got["store"], sheet_map, dedupe_by,
        require_price=require_price,
    )
    for plan in plans.values():
        await _emit(on_progress, {
            "type": "plan", "sheet": plan.sheet, "pending": len(plan.rows),
            "dup": plan.dup, "no_key": plan.no_key,
            "with_image": sum(1 for r in plan.rows if r.get("image_path")),
        })
    if unmapped:
        await _emit(on_progress, {
            "type": "unmapped", "count": len(unmapped), "samples": unmapped[:5],
        })
    if unpriced:
        await _emit(on_progress, {
            "type": "unpriced", "count": len(unpriced), "samples": unpriced[:5],
        })

    # dry-run 才导 CSV：写入模式下数据已经进表了，核对直接看表
    plan_files = (
        dump_plan_csv(plans, str(get_output_dir("orders_plan")),
                      Path(got.get("export_file") or "").stem[-15:] or "plan")
        if dry_run else []
    )
    if plan_files:
        await _emit(on_progress, {"type": "plan_files", "files": plan_files})

    written = await _write_plans(plans, workbook, dry_run, on_progress)
    summary = _summary(
        dry_run, orders=orders, plans=plans, unmapped=unmapped,
        written=written, got=got, img_stat=img_stat, plan_files=plan_files,
        unpriced=unpriced,
    )
    await _emit(on_progress, {"type": "done", **summary})
    return summary


async def _write_plans(
    plans: Dict[str, SheetPlan], workbook: str, dry_run: bool, on_progress: ProgressCB
) -> Dict[str, dict]:
    """按 Sheet 逐个批量写入（插到表头正下方，不是追加到表尾）。dry_run 时只报告不落盘。

    insert_at_top=True 是订单登记表的既定要求：新订单要在最上面。plan.rows 已在
    plan_writes 里按创建时间倒序排过，插进去自然是新→旧。

    一个 Sheet 写失败不连坐其它 Sheet（各自独立一次 zip 重写 + 独立备份），失败信息进
    汇总。不做重试：失败多半是表被 WPS 独占锁定或磁盘满，重试同样失败还多一份备份。
    """
    written: Dict[str, dict] = {}
    for sheet, plan in plans.items():
        if not plan.rows:
            continue
        if dry_run:
            written[sheet] = {"dry_run": True, "would_write": len(plan.rows)}
            continue
        await _emit(on_progress, {"type": "writing", "sheet": sheet, "rows": len(plan.rows)})
        try:
            res = await asyncio.to_thread(
                WpsExcelTool().append_rows,
                workbook, sheet, plan.rows, plan.header_row,
                key_cols=None, insert_at_top=True,
            )
            written[sheet] = res
            await _emit(on_progress, {"type": "written", "sheet": sheet, **res})
        except Exception as e:
            logger.error(f"写入 Sheet「{sheet}」失败：{e}")
            written[sheet] = {"error": str(e), "pending": len(plan.rows)}
            await _emit(on_progress, {"type": "write_failed", "sheet": sheet, "error": str(e)})
    return written


def dump_plan_csv(plans: Dict[str, SheetPlan], out_dir: str, stamp: str) -> List[str]:
    """把 dry-run 的完整写入计划逐表落成 CSV，供人工逐行核对落点与字段。

    为什么要落文件：日志里只打前 3 行（几十上百行灌进控制台/SSE 没法看），但方案要求
    人工确认后才开写入，只看 3 行确认不了。CSV 用 utf-8-sig 是为了 WPS/Excel 双击
    打开不乱码（裸 UTF-8 会被按 ANSI 猜）。
    best-effort：这是辅助产物，写失败只告警，绝不影响主流程的汇总与返回。
    """
    files: List[str] = []
    for sheet, plan in plans.items():
        if not plan.rows:
            continue
        try:
            safe = "".join(c for c in sheet if c not in '\\/:*?"<>|').strip() or "sheet"
            path = Path(out_dir) / f"待写计划_{safe}_{stamp}.csv"
            # 列序照抄 preview 的键序（已按目标表列号排好），前面补一列行号方便对着看
            titles = list(plan.preview[0].keys())
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["序号"] + titles)
                for i, row in enumerate(plan.preview, 1):
                    w.writerow([i] + [row.get(t, "") for t in titles])
            files.append(str(path))
            logger.info(f"写入计划已导出（{len(plan.preview)} 行）：{path}")
        except Exception as e:
            logger.warning(f"导出「{sheet}」写入计划 CSV 失败（不影响主流程）：{e}")
    return files


def _summary(
    dry_run: bool,
    aborted: str = "",
    orders: Optional[List[pipeline.OrderRow]] = None,
    plans: Optional[Dict[str, SheetPlan]] = None,
    unmapped: Optional[List[dict]] = None,
    written: Optional[Dict[str, dict]] = None,
    got: Optional[dict] = None,
    img_stat: Optional[dict] = None,
    plan_files: Optional[List[str]] = None,
    unpriced: Optional[List[dict]] = None,
) -> dict:
    """汇总本批结果。字段对 UI/CLI 都是稳定契约，别随手改名。"""
    plans = plans or {}
    written = written or {}
    return {
        "dry_run": dry_run,
        "aborted": aborted,
        # dry-run 导出的「待写计划」CSV 路径，供人工逐行核对
        "plan_files": list(plan_files or []),
        "store": (got or {}).get("store", ""),
        "export_file": (got or {}).get("export_file", ""),
        "total_on_page": (got or {}).get("total", 0),
        # True = 被 max_pages 截断，本批只覆盖前 N 页，不是全量
        "truncated": bool((got or {}).get("truncated", False)),
        "parsed_rows": len(orders or []),
        "images": img_stat or {},
        "pending": sum(len(p.rows) for p in plans.values()),
        "dup_skipped": sum(p.dup for p in plans.values()),
        "unmapped_skipped": len(unmapped or []),
        # 页面暂无成交单价、留到下批的订单（与 unmapped 语义不同：这个明天自己就好了）
        "unpriced_skipped": len(unpriced or []),
        "unpriced_samples": list(unpriced or [])[:5],
        "no_key_sheets": [s for s, p in plans.items() if p.no_key],
        "written": written,
        "written_rows": sum(
            r.get("written", 0) for r in written.values() if isinstance(r, dict)
        ),
        "failed_sheets": [s for s, r in written.items() if r.get("error")],
        "sheets": {
            s: {"pending": len(p.rows), "dup": p.dup, "preview": p.preview[:3]}
            for s, p in plans.items()
        },
    }
