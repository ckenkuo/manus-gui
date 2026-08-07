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
from datetime import datetime
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
from app.cloud_docs import remember as remember_cloud_doc
from app.config import PROJECT_ROOT, config, get_output_dir
from app.logger import logger
from app.orders import pipeline
from app.orders.kdocs_sheet import KdocsSheet
from app.temu_region import (
    confirm_region_from_context,
    read_region,
    region_conflict,
)
from app.tool.wps_excel_tool import WpsExcelTool

# 整批护栏：翻完 N 页 + 导出 + 下 N 张图，247 条实测约 3~5 分钟，给足余量。
ORDERS_BATCH_TIMEOUT = 1800
# 判重键的默认列标题（见 docs/orders-pipeline-plan.md §5）。
# 保持「订单号+尺码」不动：各表是否已加「子订单号」列参差不齐（2026-08-06 实测云端 4 张表
# 都还没有），把它设成默认会让缺列的表 no_key 整表跳过、一行都写不进。
# 真正的升级路径是 _preferred_dedupe_by：**按目标表实际有的列自适应**——有子订单号列就用它
# （更精确），没有就退回尺码。
# **绝不要只配 ["订单号"]**：同订单的其余子订单会被当成重复丢掉（实测：单列键下同批内两行
# 撞键，第二行静默不写＝少买一件）。
_DEFAULT_DEDUPE_BY = ["订单号", "尺码"]
# 判重键里「区分同订单多行」那一位的候选，按精确度从高到低。子订单号一单一号最可靠；
# 尺码取自导出「商品属性」，实测有 SKU 恒为 `Variant`（Temu 无规格名），会撞键。
_ROW_DISCRIMINATORS = ["子订单号", "尺码"]
# 订单页「上次选择」（店铺/工作簿/Sheet）。与采集页的 collect_prefs.json 分开存。
ORDERS_PREFS = config.workspace_root / "orders_prefs.json"


def is_cloud_link(value: str) -> bool:
    """是协作文档链接（http(s)://...）而非本地路径/文件 ID。"""
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def cloud_backend(cfg: dict, cloud_url: str = "") -> Optional[KdocsSheet]:
    """确定本批的云端登记目标；返回 None 表示走本地 xlsx 路径（行为不变）。

    优先级：显式给的协作文档链接（UI 粘贴的）> config 的 cloud_file_id。
    云端模式下水位/判重/写入全部打在协作文档上，本地 workbook 不再参与读写
    （采购汇总不受影响，始终落本地）。
    """
    target = cloud_url.strip() or str(cfg.get("cloud_file_id") or "").strip()
    if not target:
        return None
    return KdocsSheet(target)


# 文档模式：UI 上的「本地文档 / 线上文档」开关值。auto 是历史行为（按链接形态和
# config 自动判定），local/cloud 是用户显式钉死。
DOC_MODE_AUTO = "auto"
DOC_MODE_LOCAL = "local"
DOC_MODE_CLOUD = "cloud"
_DOC_MODES = (DOC_MODE_AUTO, DOC_MODE_LOCAL, DOC_MODE_CLOUD)


def normalize_doc_mode(value: Optional[str]) -> str:
    """把外部传进来的模式值收敛到三个合法值之一；不认识的一律当 auto。

    不认识就退 auto 而不是报错：这个值来自 UI/CLI/prefs 三处，其中 prefs 是历史文件
    （老版本存的 JSON 里根本没有这个键），报错会让开过旧版的人一开页就红。
    """
    v = str(value or "").strip().lower()
    return v if v in _DOC_MODES else DOC_MODE_AUTO


def resolve_doc_target(
    cfg: dict, workbook: str = "", doc_mode: str = DOC_MODE_AUTO,
    prefs: Optional[dict] = None,
) -> tuple:
    """解析本批到底写本地表还是协作文档，返回 (cloud, workbook, mode)。

    cloud 为 None＝本地模式，此时 workbook 是本地 xlsx 路径；cloud 非空＝云端模式，
    workbook 返回空串（本地路径不参与读写）。mode 是实际生效的模式，回给 UI 回显。

    为什么要显式三态而不是继续「看链接形态自动判」：kdocs 有配额，用满了要能立刻切回
    本地表继续干活（2026-08-05 用户要求）。auto 模式下只要 config 配了 cloud_file_id
    就永远走云端，用户没有不改配置就切回本地的办法。

    - local：只认本地路径。即便 workbook 传进来是个 kdocs 链接也不用它（链接不是本地
      路径，拿它当文件名必然失败），改用 prefs/config 里的本地表，再兜底桌面自动挑。
    - cloud：只认协作文档。workbook 是链接就用它，否则退 prefs.cloud_url > config。
    - auto：保持改动前的行为（链接→云端；否则 config 配了云端就云端）。

    顺带修一个既有 bug：auto 分支里显式传本地路径时原先仍会去读 config.cloud_file_id，
    于是用户在 UI 选了本地表、实际却写进云端文档（同名 Sheet 存在时不报任何错）。
    collect 侧的 resolve_cloud 早就防了这个，orders 侧漏了，这里对齐。
    """
    mode = normalize_doc_mode(doc_mode)
    wb = (workbook or "").strip()
    prefs = prefs if prefs is not None else load_prefs()

    if mode == DOC_MODE_LOCAL:
        if wb and not is_cloud_link(wb):
            return None, wb, mode   # 用户这次显式选的，照用不做存在性判断
        # 挑不出就返回空串：由 UI 让用户从候选里选，不猜（见 _pick_local_workbook）
        return None, _pick_local_workbook(cfg, prefs), mode

    if mode == DOC_MODE_CLOUD:
        url = wb if is_cloud_link(wb) else str(prefs.get("cloud_url") or "").strip()
        return cloud_backend(cfg, cloud_url=url), "", mode

    if is_cloud_link(wb):
        return cloud_backend(cfg, cloud_url=wb), "", DOC_MODE_CLOUD
    if wb:
        # 显式本地路径压制 config 的云端目标（对齐 collect.resolve_cloud）
        return None, wb, DOC_MODE_LOCAL
    pref_url = str(prefs.get("cloud_url") or "").strip()
    if pref_url or str(cfg.get("cloud_file_id") or "").strip():
        return cloud_backend(cfg, cloud_url=pref_url), "", DOC_MODE_CLOUD
    local = str(prefs.get("workbook") or "").strip() \
        or str(cfg.get("workbook") or "").strip()
    return None, local, DOC_MODE_LOCAL


def _pick_local_workbook(cfg: dict, prefs: dict) -> str:
    """本地模式下没有本次显式选择时的登记表：prefs > config，两者都失效则返回空串。

    **刻意不猜**（2026-08-05 用户要求）：登记表选错就是把订单写进别人家的表，这种不可逆
    的落点不该由代码按文件名关键词猜。返回空串时 UI 的工作簿下拉里已经有桌面候选
    （list_workbooks 按修改时间倒序扫桌面与输出目录），让用户自己点一个。

    路径要先确认**文件还在**：登记表常被改名（如加日期后缀、加「（缓存）」），config 里那条
    就成了死路径。失效时返回空串让用户重选，而不是把死路径抛给 UI 报「登记表不存在」
    （2026-08-05 实测 config 指向 wintop订单登记表7.24.xlsx，实际已改名为「（缓存）」）。
    """
    for path in (str(prefs.get("workbook") or "").strip(),
                 str(cfg.get("workbook") or "").strip()):
        if path and Path(path).exists():
            return path
    return ""


@dataclass
class SheetPlan:
    """一个目标 Sheet 的写入计划（dry-run 展示与实写共用同一份数据）。"""

    sheet: str
    store_value: str
    header: Dict[str, str] = field(default_factory=dict)
    header_row: int = 1
    image_col: Optional[str] = None
    rows: List[dict] = field(default_factory=list)   # 待写：append_rows 的入参格式
    orders: List[pipeline.OrderRow] = field(default_factory=list)
    preview: List[dict] = field(default_factory=list)  # 人工核对用：{列标题: 值}
    dup: int = 0
    # 本批内撞键被丢掉的行（不是「表里已有」，是判重键不够细导致的少买），见 _stage_order
    collided: List[dict] = field(default_factory=list)
    # 这张表实际用的判重列（按表内有哪些列自适应，见 _preferred_dedupe_by）
    dedupe_by: List[str] = field(default_factory=list)
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


def save_prefs(store: str = "", workbook: str = "", sheet: str = "",
               doc_mode: str = "",
               region_label: Optional[str] = None) -> None:
    """记住本次选择，供下次 UI 缺省回填。写失败只告警、不阻断本批。

    workbook 是协作文档链接时存到 cloud_url（与本地路径分开）：下次首屏按
    「prefs.cloud_url > config.cloud_file_id」回填，而显式选过本地路径则清掉
    cloud_url，避免旧链接盖掉用户后来的选择。

    doc_mode 显式给 local/cloud 时，**两种路径互不覆盖**：本地模式只更新 workbook、保留
    原来的 cloud_url，云端模式反之。否则用户在两个模式间来回切时，切过去就把另一边记的
    目标清了，切回来又要重填一次（kdocs 配额用满时切本地、次日切回云端是常规操作）。
    能这么留是因为模式本身已经把歧义消掉了，不必再靠「哪边有值」来猜。

    doc_mode 为 auto（没传，即老版本 UI/CLI）时保持原来的互斥语义：显式选本地就清掉
    cloud_url。auto 的目标解析仍靠「cloud_url 有值就走云端」，留着旧链接会把用户后来选的
    本地表盖掉。
    """
    mode = normalize_doc_mode(doc_mode)
    old = load_prefs() if mode != DOC_MODE_AUTO else {}
    # 区域：None = 本次没传，沿用上次记的（老版本 UI/CLI 不带这个参数，别被空串重置掉）；
    # 空串 = 显式选「跟随浏览器当前区域」。故这里必须读完整旧偏好，不能借用上面按模式取的 old。
    region_val = (
        load_prefs().get("region_label") or "" if region_label is None
        else str(region_label).strip()
    )
    data = {"store": store, "sheet": sheet, "workbook": "", "cloud_url": "",
            "doc_mode": mode, "region_label": region_val}
    if is_cloud_link(workbook):
        data["cloud_url"] = workbook.strip()
        data["workbook"] = str(old.get("workbook") or "")
    else:
        data["workbook"] = workbook
        data["cloud_url"] = str(old.get("cloud_url") or "")
    # 协作文档链接同时进登记簿（app/cloud_docs.py），下次开页可直接从候选里选。
    # 只登记本次真正传进来的链接，沿用上面继承的旧值不必再登记一遍
    if is_cloud_link(workbook):
        remember_cloud_doc(data["cloud_url"])
    try:
        ORDERS_PREFS.parent.mkdir(parents=True, exist_ok=True)
        ORDERS_PREFS.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"保存订单页偏好失败（忽略）：{e}")


def inspect_sheet(workbook: str, sheet: str, dedupe_by: Optional[List[str]] = None,
                  cloud: Optional[KdocsSheet] = None) -> dict:
    """探测某 Sheet 能不能写：判重列是否齐、图片列在哪、表头行第几行。

    为什么 UI 必须先看这个：判重列（订单号+尺码）缺任一列时 service 会把整表标 no_key
    并跳过——一行都不写。让用户在点「开始」前就知道，而不是跑完几分钟导出才发现白跑。
    纯读，异常返回 writable=False，不抛错。cloud 非空时探测协作文档而非本地工作簿。
    """
    titles = dedupe_by or _DEFAULT_DEDUPE_BY
    try:
        header, header_row, image_col = _sheet_schema(workbook, sheet, cloud=cloud)
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


def _cloud_worklist(
    cfg: dict, prefs: dict, cloud_url: str, store: Optional[str],
    sheet: Optional[str], dedupe_by: List[str], store_options: List[str],
    cloud: Optional[KdocsSheet] = None,
) -> dict:
    """云端模式的 worklist：Sheet 列表与可写性都查协作文档，本地枚举不参与。

    cloud_url 为空＝用 config 的 cloud_file_id（展示用 cloud_link 回显）。
    读失败（未认证/网络/链接无效）不抛错：返回空列表 + cloud_error，由 UI 红条展示。
    cloud 已由调用方（resolve_doc_target）解析好时直接复用，避免重复构造。
    """
    if cloud is None:
        cloud = cloud_backend(cfg, cloud_url=cloud_url)
    display = cloud_url or str(cfg.get("cloud_link") or cfg.get("cloud_file_id") or "")
    if store is None:
        store = prefs.get("store") or ""
    if sheet is None:
        sheet = prefs.get("sheet") or ""
    cloud_err = ""
    sheets: List[str] = []
    if cloud is None:
        # 用户显式切到线上模式但压根没有可用目标（config 没配、也没粘过链接）。
        # 当成「读失败」走同一条 UI 红条通道，提示补链接，别在这里抛错
        cloud_err = "未指定协作文档：请在上方粘贴 kdocs 链接，或在 config.toml 配 cloud_file_id"
    else:
        try:
            sheets = cloud.sheet_names()
        except Exception as e:
            logger.warning(f"读协作文档工作表列表失败：{e}")
            sheets, cloud_err = [], str(e)
    sheet = sheet if sheet in sheets else ""
    # datalist 候选：当前目标 + 上次用过的 + config 配过的链接，去重保序
    suggestions: List[str] = []
    for v in (display, str(prefs.get("cloud_url") or "").strip(),
              str(cfg.get("cloud_link") or "").strip()):
        if v and v not in suggestions:
            suggestions.append(v)
    return {
        "store": store,
        "workbook": display,
        "workbook_exists": not cloud_err,
        "workbook_locked": False,
        "workbooks": suggestions,
        "sheet": sheet,
        "sheets": sheets,
        "dedupe_by": dedupe_by,
        "store_options": store_options,
        "cloud": True,
        "doc_mode": DOC_MODE_CLOUD,
        "cloud_error": cloud_err,
        "sheet_info": (
            inspect_sheet("", sheet, dedupe_by, cloud=cloud)
            if sheet and not cloud_err else {}
        ),
    }


async def peek_current_region() -> dict:
    """只读探当前浏览器里选定的区域，供订单页首屏展示。{label, host, labels, error}。

    为什么只展示、不做选择器：区域的真值在【浏览器里】——它是操作者在 Temu 顶栏点选的，
    切换会换域名。UI 上再放一个区域下拉就有了两处真值，选了却和浏览器不一致时反而更危险。
    所以这里把浏览器的实际区域显示出来，让操作者在点「开始」前对一眼，落表仍由店铺+Sheet 决定。

    best-effort：连不上 CDP、没开后台页、区域读不到都只回 error 字符串，绝不抛——首屏
    不该因为浏览器没开就打不开（对齐本项目辅助路径吞异常的取向）。
    """
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
            try:
                if not browser.contexts:
                    return {"error": "CDP 浏览器没有可用 context"}
                region = await confirm_region_from_context(browser.contexts[0])
                return {"label": region.label, "host": region.host,
                         "labels": list(region.labels), "error": ""}
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"读当前区域失败（首屏仅提示，忽略）：{e}")
        return {"error": str(e)}


def get_worklist_status(
    store: Optional[str] = None,
    workbook: Optional[str] = None,
    sheet: Optional[str] = None,
    doc_mode: Optional[str] = None,
) -> dict:
    """订单页首屏：可选工作簿/Sheet 列表 + 上次选择回显 + 选中 Sheet 的可写性。

    doc_mode 显式给 local/cloud 时钉死走哪条路（UI 上的「本地文档/线上文档」开关）；
    不给（None）则沿用上次选的模式，再退 auto——auto 的优先级是：显式传入（本地路径或
    粘贴的协作文档链接）> 上次使用的协作链接（prefs.cloud_url）> config 的
    cloud_file_id > 本地工作簿（prefs > config）。
    本地模式下已知路径都失效（表被改名等）时回传空 workbook，由用户从 workbooks 候选里
    自己选一个——登记表选错就是写进别人家的表，不由代码猜（见 _pick_local_workbook）。
    纯读、不触发任何采集或写入。
    """
    cfg = load_orders_config()
    prefs = load_prefs()
    dedupe_by = list(cfg.get("dedupe_by") or _DEFAULT_DEDUPE_BY)
    store_options = sorted(
        {str(m.get("store", "")).strip() for m in (cfg.get("sheet_map") or [])
         if str(m.get("store", "")).strip()}
    )

    wb = (workbook or "").strip()
    mode = normalize_doc_mode(
        doc_mode if doc_mode is not None else prefs.get("doc_mode")
    )
    cloud, local_wb, mode = resolve_doc_target(cfg, wb, mode, prefs=prefs)
    if mode == DOC_MODE_CLOUD:
        url = wb if is_cloud_link(wb) else str(prefs.get("cloud_url") or "").strip()
        return _cloud_worklist(cfg, prefs, url, store, sheet, dedupe_by,
                               store_options, cloud=cloud)

    workbook = local_wb
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
        "store_options": store_options,
        "cloud": False,
        "doc_mode": DOC_MODE_LOCAL,
        "cloud_error": "",
        "sheet_info": (
            inspect_sheet(workbook, sheet, dedupe_by) if exists and sheet else {}
        ),
    }


def read_known_order_nos(workbook: str, sheet: str,
                         cloud: Optional[KdocsSheet] = None) -> set:
    """读某 Sheet【最新登记的那一条】订单号，作增量采集的水位。纯读，失败返回空集。

    取的是表头正下方第一个非空订单号（`first_data_value`）。为什么单条就够：写入走的是
    insert_at_top（新行插到表头正下方）+ 写入前按创建时间倒序排（见 plan_writes），所以
    数据区顶端那行必然是上次登记的最新一条。页面也是新→旧，翻页遇到它就是「追上」。

    为什么不再读整列集合：整列在稀疏表上反而有害。旧实现配合「整页全已登记才停」的判据，
    在新建 Sheet（表里只有几条）上早停出口结构性走不通，必然翻到底（2026-08-04 实机
    `WINTAK8.4+` 翻了 63 页，详见 docs §16.1）。单条水位不受表内行数影响，语义也更直白。
    顺带省掉一次整列扫描（本地是 zip 解压全表、云端是分页拉几千行）。

    为什么只在【单表模式】用（调用方保证）：sheet_map 分流一次 sweep 同时喂 7 张表，各表
    新旧程度不同（某张上周登记过、另一张停了一个月）。拿其中一张的水位当停止信号会让落后的
    表永远补不回来。单表模式落点唯一、水位唯一，语义无歧义。

    返回值仍是 set（0 或 1 个元素），保持与 sweep_pages / should_stop_incremental 的既有
    契约不变。空集＝调用方退回全量 sweep（订单号列缺失、空表、文件损坏都走这条），不会误
    早停：水位为空时判据永远命不中，自然翻到底。cloud 非空时从协作文档读，口径相同。
    """
    try:
        header, header_row, _ = _sheet_schema(workbook, sheet, cloud=cloud)
    except Exception as e:
        logger.warning(f"读 Sheet「{sheet}」表头失败，本批退全量采集：{e}")
        return set()
    col = pipeline.resolve_title_column(header, "订单号")
    if not col:
        logger.warning(f"Sheet「{sheet}」没有「订单号」列，本批退全量采集")
        return set()
    if cloud is not None:
        latest = cloud.first_data_value(sheet, col=col, header_row=header_row)
    else:
        latest = WpsExcelTool.first_data_value(
            workbook, sheet, col=col, header_row=header_row
        )
    if not latest:
        logger.warning(f"Sheet「{sheet}」数据区顶端读不到订单号（空表？），本批退全量采集")
        return set()
    logger.info(f"Sheet「{sheet}」最新登记订单号 {latest}（增量水位，翻到它即停）")
    return {latest}


def _sheet_schema(workbook: str, sheet: str,
                  cloud: Optional[KdocsSheet] = None) -> tuple:
    """解析一次目标 Sheet 的表头结构，全批复用：(header, header_row, image_col)。

    13 个登记 Sheet 的表头行位置（第 1 或第 2 行）与列序都不一样，且工作簿 102MB——
    每行都去解析一次表头等于把 zip 解压几百遍。cloud 非空时改查协作文档（API 0-based
    索引在 kdocs_sheet 内部转换，返回口径与本地一致：列字母 + 1-based 行号）。
    """
    if cloud is not None:
        header, header_row = cloud.read_header(sheet)
    else:
        header_row = WpsExcelTool.detect_header_row(workbook, sheet)
        header = WpsExcelTool.read_header(workbook, sheet, header_row=header_row)
    return header, header_row, pipeline.image_column(header)


def ensure_qty_columns(
    orders: List[pipeline.OrderRow], workbook: str, store: str, sheet_map: List[dict],
    cloud: Optional[KdocsSheet] = None,
) -> List[dict]:
    """给本批真要写的每张 Sheet 补上「数量」列（在「尺码」右侧），返回逐表结果。

    为什么要插列：导出的「应履约件数」一直有值，但 5 张登记 Sheet 的表头里根本没有数量列，
    于是一单买 2 件跟买 1 件在表里长得一样（2026-07-30 用户反馈）。改结构是不可逆操作，
    所以只在【真写入】时做、且只碰本批实际有订单落进去的表——dry-run 全程只读不动结构。

    目标表用 resolve_sheet 纯配置查，不读工作簿：插列必须发生在 plan_writes 之前
    （它一进去就缓存表头），而那时还没有 plans。
    WpsExcelTool.insert_column_after 自身幂等（已有该列直接返回 inserted=False），
    所以重跑不会插出第二列。

    best-effort：某张表插失败只告警并继续——插不上的后果是那张表的数量列留空，
    而不是整批订单登记不了（沿用本项目「辅助路径坏了不影响主流程」的取向）。

    云端模式：协作文档是多人共享的，管线不擅自改它的列结构，只检查并报告缺列的表，
    由人手动在协作文档里补（缺列的后果同上：数量格留空，订单照常登记）。
    """
    wanted: List[str] = []
    for o in orders:
        cfg = pipeline.resolve_sheet(store, o.site, sheet_map)
        name = str((cfg or {}).get("sheet") or "")
        if name and name not in wanted:
            wanted.append(name)

    out: List[dict] = []
    for name in wanted:
        try:
            if cloud is not None:
                header, _ = cloud.read_header(name)
                if any(t.strip() in pipeline.QTY_TITLES for t in header.values()):
                    out.append({"sheet": name, "inserted": False, "reason": "已存在"})
                else:
                    logger.warning(
                        f"云端 Sheet「{name}」缺「数量」列：请手动在协作文档「尺码」右侧补一列，"
                        "本批该列留空、订单照常登记"
                    )
                    out.append({"sheet": name, "inserted": False,
                                "reason": "云端表缺「数量」列，需手动补"})
                continue
            header_row = WpsExcelTool.detect_header_row(workbook, name)
            res = WpsExcelTool.insert_column_after(
                workbook, name, pipeline.QTY_AFTER_TITLE, pipeline.QTY_TITLE,
                header_row=header_row,
            )
            out.append({"sheet": name, **res})
        except Exception as e:
            logger.warning(f"Sheet「{name}」补「数量」列失败（该列留空，不影响登记）：{e}")
            out.append({"sheet": name, "inserted": False, "reason": str(e)})
    return out


def plan_writes(
    orders: List[pipeline.OrderRow],
    workbook: str,
    store: str,
    sheet_map: List[dict],
    dedupe_by: Optional[List[str]] = None,
    require_price: bool = False,
    cloud: Optional[KdocsSheet] = None,
) -> tuple:
    """把订单按目标 Sheet 分组、判重，产出 {sheet: SheetPlan}、未映射清单、无价清单。

    返回 (plans, unmapped, unpriced)。两类跳过语义完全不同，刻意分开报：
      - unmapped：店铺+站点没配 sheet_map，是配置缺失，要人工补配置才能登记。
        绝不臆测落点（`StoreA牛仔裤` 是列结构完全不同的专用表，不是 StoreA 美国站）。
      - unpriced：仅在 require_price=True 时才产生，含义是「本批不登记、留到下批」。

    **require_price 默认 False（2026-07-29 确认）**：成交单价是商家助手插件注入 DOM 的、
    不是 Temu 官方字段，实测回填有约一天延迟（2026-07-28 实机：当天 12 单全部无「成交
    单价」标签，前一天的都有）。等一天再登记会让当天的单全部积压，而登记表的主用途是发货
    与采购跟单，成交价只是参考列——所以允许「平台成交价」这一格为空，订单照常入库。
    代价已知：判重键是订单号+尺码，带空价入库后重跑会判定已入库而不再补价，那一格需人工
    补填。要恢复「无价就留到下批」的旧口径，显式传 require_price=True。
    """
    titles = dedupe_by or _DEFAULT_DEDUPE_BY
    plans: Dict[str, SheetPlan] = {}
    unmapped: List[dict] = []
    unpriced: List[dict] = []
    seen: Dict[str, set] = {}  # sheet → 已排入本批的键，防同批内重复
    existing: Dict[str, set] = {}  # sheet → 进本批前表里已有的键（seen 的快照）

    # 本批按创建时间倒序：登记表要求「时间越新的越在上面」，而写入走的是插到表头下方，
    # 所以排在前面的落在更上面。Temu 页面本来就是新→旧，但那是页面顺序、不是契约，
    # 显式排一遍才是这条规则的实现。时间为空的排最后；stable sort 保证同一时间的
    # 多个尺码行维持原相对次序。
    orders = sorted(orders, key=lambda x: str(x.created_at or ""), reverse=True)

    for o in orders:
        # 严格模式（require_price=True）才拦无价：拦在分表/判重之前，免得白读表头。
        # 默认模式下这段整体跳过，无价订单带空「平台成交价」照常登记。
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
            header, header_row, image_col = _sheet_schema(workbook, sheet, cloud=cloud)
            plan = SheetPlan(
                sheet=sheet, store_value=str(cfg.get("store_value") or store),
                header=header, header_row=header_row, image_col=image_col,
            )
            plans[sheet] = plan
            # 判重键按【这张表】实际有的列定：各表加「子订单号」列的进度不同，
            # 所以逐表解析而不是全批共用一份（见 _preferred_dedupe_by）
            plan.dedupe_by = _preferred_dedupe_by(header, titles)
            seen[sheet] = _existing_keys(workbook, plan, plan.dedupe_by, cloud=cloud)
            # 存一份「进本批前表里已有的键」：seen 会被本批新排的行污染，靠它区分
            # 「表里已有」和「同批撞键」两种 dup（后者是少买，必须报出来）
            existing[sheet] = set(seen[sheet])
            if not header:
                logger.warning(f"Sheet「{sheet}」读不到表头，本批跳过它")

        _stage_order(o, plan, plan.dedupe_by, seen[sheet], existing[sheet])

    return plans, unmapped, unpriced


def _preferred_dedupe_by(header: Dict[str, str], titles: List[str]) -> List[str]:
    """按目标表实际有的列，把判重键升级到能区分同订单多行的那一位。

    为什么要自适应而不是让用户配死：各表加「子订单号」列的进度不一样（2026-08-06 云端 4 张
    表都还没加，本地表刚加了一张），配死任一种都会有表踩坑——配子订单号则缺列的表 no_key
    整表不写，配尺码则无规格商品（属性恒为 `Variant`）在同订单里撞键少买。

    规则：配置里已经指定了区分位（子订单号/尺码之一）就尊重配置，不动；
    只配了订单号这类单列键时，看表里有没有更精确的列，有就自动补上——单列键会把同订单的
    其余子订单全判成重复丢掉，这是静默少买，值得替用户兜一下。
    补的时候优先子订单号，其次尺码；两者都没有就只能维持原样（后续由 collided 计数暴露）。
    """
    if any(t.strip() in _ROW_DISCRIMINATORS for t in titles):
        return list(titles)
    for cand in _ROW_DISCRIMINATORS:
        if pipeline.resolve_title_column(header, cand):
            upgraded = list(titles) + [cand]
            logger.info(
                f"判重键 {titles} 只有订单号一位，按表内实际列自动补上「{cand}」→ {upgraded}"
                "（避免同订单多个子订单被判成重复而少买）"
            )
            return upgraded
    return list(titles)


def _existing_keys(workbook: str, plan: SheetPlan, titles: List[str],
                   cloud: Optional[KdocsSheet] = None) -> set:
    """读该 Sheet 已入库的判重键集合。任一判重列缺失 → 标记 no_key 并返回空集。

    no_key 的 Sheet 会被 _stage_order 整体跳过：没法判重就意味着重复跑会堆重复行，
    宁可不写。cloud 非空时从协作文档读，口径相同。
    """
    cols: List[str] = []
    for t in titles:
        col = pipeline.resolve_title_column(plan.header, t)
        if not col:
            plan.no_key = True
            logger.warning(f"Sheet「{plan.sheet}」找不到判重列「{t}」，本批跳过它")
            return set()
        cols.append(col)
    if cloud is not None:
        return cloud.existing_key_tuples(
            plan.sheet, cols, header_row=plan.header_row
        )
    return WpsExcelTool.existing_key_tuples(
        workbook, plan.sheet, cols, header_row=plan.header_row
    )


def _stage_order(
    o: pipeline.OrderRow, plan: SheetPlan, titles: List[str], seen: set,
    existing: Optional[set] = None,
) -> None:
    """把一条订单排进某 Sheet 的写入计划；已入库或本批内重复的只计数不排。

    existing 是「进本批之前表里已有的键」。传了它就能把两种 dup 分开：
      - 表里已有 → 正常判重，本来就不该再写
      - **本批内两行撞了同一个键** → 这行从没写过就被丢了，等于少买，属于判重键选得不够细
        （典型：dedupe_by 只配了订单号，或尺码取到恒为 `Variant` 的无规格商品）。
        这种必须报出来，不能跟上面那种混在一个数字里静默掉。
    """
    if plan.no_key or not plan.header:
        return
    key = pipeline.dedupe_key(o, plan.header, titles, plan.store_value)
    if key is None:
        plan.no_key = True
        return
    if key in seen:
        plan.dup += 1
        # 不在「原有键」里却已在 seen 里 → 是本批前面某行占的位，同批撞键
        if existing is not None and key not in existing:
            plan.collided.append({
                "order_no": o.order_no, "sub_order_no": o.sub_order_no,
                "attrs": o.attrs, "key": list(key),
            })
        return
    seen.add(key)

    values = pipeline.build_row_values(o, plan.header, plan.store_value)
    item: Dict[str, Any] = {"values": values}
    # 图片列只在真下到本地图时才带上；没图的行照常入库（图是辅助字段）
    if plan.image_col and o.image_path:
        item["image_column"] = plan.image_col
        item["image_path"] = o.image_path
    plan.rows.append(item)
    plan.orders.append(o)
    plan.preview.append(_preview_row(o, plan, values, item))


def _preview_row(
    o: pipeline.OrderRow, plan: SheetPlan, values: Dict[str, str], item: Dict[str, Any]
) -> Dict[str, Any]:
    """人工核对用的一行：{列标题: 值}，按目标表列序排。

    表里还没有「数量」列时（dry-run 不动表结构）补一个虚拟的 `数量*`，插在「尺码」右边——
    否则 dry-run 的待写计划 CSV 里看不到件数，而这批订单里恰恰有多件单要核对。
    带 `*` 是为了跟真实列区分：写入模式下这一列会变成表里真实存在的「数量」。
    """
    row: Dict[str, Any] = {}
    has_qty = any(t.strip() in pipeline.QTY_TITLES for t in plan.header.values())
    for col, value in sorted(values.items(), key=lambda kv: pipeline._col_key(kv[0])):
        title = plan.header.get(col, col)
        row[title] = value
        if not has_qty and title.strip() == pipeline.QTY_AFTER_TITLE:
            row[pipeline.QTY_TITLE + "*"] = pipeline._qty_number(o.qty)
    return row | {"_图片": "有" if item.get("image_path") else "无"}


def _retarget_url_host(url: str, host: str) -> str:
    """把 URL 的域名换成 host，**其余部分（路径/query/fragment）原样保留**。

    为什么不用 url_in_region 重建：list_url 是操作者自己配的，query 里带着订单列表的筛选
    与排序参数（sortType 等，check_list_sort_url 还要校验它）。按路径重建会把这些参数丢掉，
    等于悄悄改掉本批的采集范围。所以只动 netloc。

    host 为空或与原域相同则原样返回。url 解析不出域名（相对路径等）也原样返回，交由调用方
    的既有校验处理。
    """
    from urllib.parse import urlsplit, urlunsplit

    if not host:
        return url
    try:
        parts = urlsplit(str(url or ""))
        if not parts.netloc or parts.netloc.lower() == host.lower():
            return url
        switched = urlunsplit(parts._replace(netloc=host))
        logger.info(f"list_url 域名按区域改写：{parts.netloc} → {host}")
        return switched
    except Exception as e:
        logger.warning(f"改写 list_url 域名失败（沿用原链接）：{e}")
        return url


async def collect_orders(
    list_url: str,
    store: str = "",
    on_progress: ProgressCB = None,
    max_pages: int = 200,
    known_order_nos: Optional[set] = None,
    region_label: str = "",
) -> dict:
    """连 CDP、开专用页签、翻页勾选抓图、触发导出、解析。返回 {orders, store, stat...}。

    专用页签模式同活动管线：绝不复用用户正在操作的页签——它的筛选条件、弹窗和生命周期
    都不受管线控制，用户随手一关就把整批打断。用完即关，不碰用户原有页签。

    region_label：UI 上选定的区域，**以它为准**——浏览器停在别的区域会先切过去再采
    （区域切换换域名，决定这批能看到哪些订单）。空串＝跟随浏览器当前区域。
    """
    pw = await async_playwright().start()
    browser = None
    page = None
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("CDP 浏览器没有可用 context")
        ctx = browser.contexts[0]

        # 区域前置动作：顶栏「全球 / 美国 / 欧区」切换换的是【域名】（全球
        # agentseller.temu.com、美国 agentseller-us.temu.com，2026-08-07 实测），选中的
        # 区域决定页面能看到哪批订单。
        #
        # 以 UI 选定的区域为准：浏览器停在别的区域就切过去（confirm_region_from_context
        # 内部完成），随后把 list_url 的域名换成该区域的域名——**只换 host，路径与
        # query 原样保留**，因为那里带着操作者配的筛选与排序参数（sortType 等），
        # 重建 URL 会丢掉它们。域名不改则会拿全球域链接去导美国区的单，整批落错表。
        region = await confirm_region_from_context(ctx, region_label)
        list_url = _retarget_url_host(list_url, region.host)

        page = await ctx.new_page()
        await page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        try:
            from app.activity.pipeline import dismiss_all_page_popups

            await dismiss_all_page_popups(page)
        except Exception as e:
            logger.warning(f"关弹窗失败（继续）：{e}")
        await page.wait_for_selector("table tbody tr", timeout=60000)

        # 新开的页签也复核一次：goto 后页面可能被平台重定向到别的区域域名
        drift = region_conflict(region, await read_region(page))
        if drift:
            raise RuntimeError(
                f"订单列表页签{drift}。已中止，避免把别的区域的订单登记进本批。"
            )

        detected = store or await pipeline.detect_store(page)
        if not detected:
            raise RuntimeError(
                "识别不到当前登录店铺名，已中止：店铺决定订单写进哪张表，"
                "猜错会把订单写进别人家的 Sheet。请在界面/CLI 显式指定店铺。"
            )
        # 区域随 store 事件一并抛出：店铺名本身不含区域（同一账号在全球/美国区读到的是
        # 同一个名字），落表靠的是店铺+站点。把区域摆到进度里，操作者 dry-run 复核时能
        # 一眼确认「这批是哪个区域的单」，而不是等写完才发现区域选错。
        await _emit(on_progress, {
            "type": "store", "store": detected,
            "region": region.key, "region_label": region.label,
        })
        logger.info(f"本批区域={region.describe()} 店铺={detected}")

        async def _on_page(info: dict) -> None:
            await _emit(on_progress, {"type": "page", **info})

        swept = await pipeline.sweep_pages(
            page, on_page=_on_page, max_pages=max_pages,
            known_order_nos=known_order_nos,
        )
        await _emit(on_progress, {
            "type": "swept", "pages": swept["pages"], "total": swept["total"],
            "selected": swept["selected"], "images": len(swept["images"]),
            "truncated": swept.get("truncated", False),
            "stopped_early": swept.get("stopped_early", False),
            "stop_reason": swept.get("stop_reason", ""),
            "fell_back": swept.get("fell_back", False),
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
            # 实际生效的区域（可能是 UI 选定后切过去的）：进采购汇总文件名，
            # 同账号跨区域店名相同，只带店名分不清是哪个区域的单
            "region": region.label, "region_host": region.host,
            "total": swept["total"], "pages": swept["pages"], "join": join_stat,
            # 被 max_pages 截断＝本批不是全量，一路传到汇总，别让人误读成「全跑完了」
            "truncated": swept.get("truncated", False),
            # 增量早停＝故意只取新单（与 truncated 语义相反，见 sweep_pages）
            "stopped_early": swept.get("stopped_early", False),
            "stop_reason": swept.get("stop_reason", ""),
            "fell_back": swept.get("fell_back", False),
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
    require_price: bool = False,
    incremental: bool = True,
    doc_mode: str = "",
    region_label: str = "",
) -> dict:
    """整批入口：预检 →（读水位）→ 采集 → 计划 →（dry_run 则止步）→ 批量写入 → 汇总。

    dry_run 默认 True：写登记表是不可逆操作（虽有自动备份），方案文档要求实机 dry-run
    结果人工确认后才开写入。UI/CLI 必须显式传 dry_run=False 才会落盘。

    sheet 非空＝用户显式指定落点，绕过 sheet_map 的站点分流（见 _explicit_sheet_map）；
    此时 store 也必须显式给出，因为合成映射要拿它当匹配键。

    require_price 默认 False：允许「平台成交价」为空，页面还没注入成交单价的订单照常
    登记（见 plan_writes 的说明）；传 True 才恢复「无价留到下批」的旧口径。

    incremental 默认 True，但**只在单表模式（sheet 非空）真正生效**：采集前读该 Sheet
    已登记的订单号当水位，翻页追上就停，不必每次全量翻十几页。sheet_map 分流模式下水位
    有歧义（见 read_known_order_nos），一律退全量 sweep 靠判重兜底，行为与改动前一致。
    """
    cfg = load_orders_config()
    # 本批写本地表还是协作文档：doc_mode 显式给 local/cloud 就钉死，不给则按链接形态和
    # config 自动判（见 resolve_doc_target）。本地模式下挑不出登记表则 workbook 为空，
    # 走下面的「缺 workbook」中止，让用户回 UI 自己选一个——不猜。
    cloud, workbook, doc_mode = resolve_doc_target(cfg, workbook, doc_mode)
    list_url = list_url or str(cfg.get("list_url") or "")
    dedupe_by = list(cfg.get("dedupe_by") or _DEFAULT_DEDUPE_BY)
    # 只按订单号判重会把同订单的其余子订单当成重复丢掉（少买），且是静默的。
    # 不中止：这是用户的配置选择，但必须让他在日志里看见。
    if [t.strip() for t in dedupe_by] == ["订单号"]:
        logger.warning(
            "dedupe_by 只配了「订单号」：一个订单含多个子订单时，除第一行外都会被判成重复"
            "丢掉（＝少买）。建议改成 [\"订单号\", \"子订单号\"]（表里需有子订单号列）。"
        )
    sheet = (sheet or "").strip()
    if sheet:
        if not store:
            reason = "显式指定 Sheet 时必须同时指定店铺（要写进「订单店铺」列）"
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            return _summary(dry_run, aborted=reason)
        sheet_map = _explicit_sheet_map(store, sheet, cfg)
    else:
        sheet_map = list(cfg.get("sheet_map") or [])

    # 显式切到线上模式但没有可用目标：直接中止，别静默退回本地表（会写错文档）
    if cloud is None and doc_mode == DOC_MODE_CLOUD:
        reason = ("已选「线上文档」但未指定协作文档：请粘贴 kdocs 链接，"
                  "或在 config.toml 的 [orders] 配 cloud_file_id")
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    # 本地模式没选到登记表：给可操作的提示，别只报「配置不完整」让人去猜哪儿没配
    if cloud is None and not workbook:
        reason = ("未选择本地登记表：请在页面的「订单登记表」里从候选中选一个（或填路径），"
                  "也可在 config.toml 的 [orders].workbook 配默认值")
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    # 云端模式不需要本地登记表：workbook 缺失/被占用都不再是中止条件
    missing = [n for n, v in (("list_url", list_url),) if not v]
    if missing or not sheet_map:
        reason = f"[orders] 配置不完整：缺 {missing or ['sheet_map']}"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    if cloud is None and not Path(workbook).exists():
        reason = f"登记表不存在：{workbook}"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        return _summary(dry_run, aborted=reason)
    # 占用预检只在真要写时做：dry-run 全程只读，表开着也能跑。放在采集【之前】是为了
    # 别让人白等几分钟翻页导出，最后卡在「文件被 WPS 锁着」。云端表无文件锁，跳过。
    if cloud is None and not dry_run and excel_write_locked(workbook):
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
        "sheet": sheet, "store": store, "incremental": incremental,
        "cloud": bool(cloud), "doc_mode": doc_mode,
    })

    # 水位：采集之前读，纯读操作，dry-run 下同样生效（试跑就是要看有哪些新单）
    known: set = set()
    inc_reason = ""
    if not incremental:
        inc_reason = "已显式关闭增量，本批全量采集"
    elif not sheet:
        inc_reason = "未指定目标 Sheet（按 sheet_map 分站点分流），水位有歧义，本批全量采集"
    else:
        # 排序前置校验：增量早停靠「翻到已登记那条就停」，排序反了会停在第 1 页并把新单
        # 全漏掉，且汇总看起来只是「本批无新单」——静默漏采，所以这里【中止】而不是降级。
        # 只在增量真要生效时校验：全量模式不依赖排序，不该拦（见 check_list_sort_url）。
        sort_err = pipeline.check_list_sort_url(list_url)
        if sort_err:
            await _emit(on_progress, {"type": "aborted", "reason": sort_err})
            return _summary(dry_run, aborted=sort_err)
        known = read_known_order_nos(workbook, sheet, cloud=cloud)
        if not known:
            inc_reason = "该 Sheet 读不到已登记订单号（首次登记或缺订单号列），本批全量采集"
    await _emit(on_progress, {
        "type": "watermark", "enabled": bool(known), "known": len(known),
        "sheet": sheet, "reason": inc_reason,
    })
    if inc_reason:
        logger.info(f"增量未启用：{inc_reason}")

    try:
        got = await asyncio.wait_for(
            collect_orders(list_url, store=store, on_progress=on_progress,
                           max_pages=max_pages, known_order_nos=known,
                           region_label=region_label),
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

    # 插「数量」列必须在 plan_writes 之前：它一进去就把表头缓存进 SheetPlan 全批复用，
    # 插完再读才拿得到新列。dry-run 不动表结构，只在预览里虚拟一列（见 _stage_order）。
    if not dry_run:
        qty_cols = ensure_qty_columns(orders, workbook, got["store"], sheet_map,
                                      cloud=cloud)
        if any(item.get("inserted") for item in qty_cols):
            await _emit(on_progress, {"type": "qty_column", "sheets": qty_cols})

    plans, unmapped, unpriced = plan_writes(
        orders, workbook, got["store"], sheet_map, dedupe_by,
        require_price=require_price, cloud=cloud,
    )
    pending_orders = [order for plan in plans.values() for order in plan.orders]
    purchase = _export_purchase(
        pending_orders,
        Path(got.get("export_file") or "").stem.replace("订单导出_", "") or "batch",
        # 店铺名进文件名：多店铺时同一日期目录下要能一眼分辨。用 got["store"]（采集时
        # 实际识别/指定的那家）而不是入参 store——后者可能为空、由 detect_store 补上
        got.get("store", ""),
        # 区域同理用 got["region"]（实际生效的那个，可能是按 UI 选择切过去的），
        # 而不是入参 region_label——后者为空时表示「跟随浏览器当前」，文件名里要写实际值
        got.get("region", ""),
    )
    if purchase.get("file") or purchase.get("md_file"):
        await _emit(on_progress, {"type": "purchase_summary", **purchase})
    for plan in plans.values():
        await _emit(on_progress, {
            "type": "plan", "sheet": plan.sheet, "pending": len(plan.rows),
            "dup": plan.dup, "no_key": plan.no_key,
            # 这张表实际用的判重列，报出来便于核对（各表可能不同）
            "dedupe_by": plan.dedupe_by,
            # 同批撞键＝这些行从没写过就被丢了（少买），与 dup 语义不同，单独报
            "collided": len(plan.collided),
            "collided_samples": plan.collided[:5],
            "with_image": sum(1 for r in plan.rows if r.get("image_path")),
        })
        if plan.collided:
            logger.warning(
                f"Sheet「{plan.sheet}」有 {len(plan.collided)} 行在本批内撞了同一个判重键"
                f"被丢弃（＝少买）：判重键 {plan.dedupe_by} 区分不了这些行。"
                f"建议在该表加「子订单号」列（一单一号，不会撞）。样例：{plan.collided[:3]}"
            )
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

    written = await _write_plans(plans, workbook, dry_run, on_progress, cloud=cloud)
    summary = _summary(
        dry_run, orders=orders, plans=plans, unmapped=unmapped,
        written=written, got=got, img_stat=img_stat, plan_files=plan_files,
        unpriced=unpriced, purchase=purchase, doc_mode=doc_mode,
        incremental={
            "enabled": bool(known), "known": len(known), "reason": inc_reason,
            # 水位就是登记表最新那条订单号，报出来便于人工核对停在了哪儿
            "watermark": next(iter(known), ""),
            "stopped_early": bool(got.get("stopped_early")),
            "stop_reason": got.get("stop_reason", ""),
            "fell_back": bool(got.get("fell_back")),
            "pages_swept": got.get("pages", 0),
        },
    )
    await _emit(on_progress, {"type": "done", **summary})
    return summary


def _purchase_out_dir(stamp: str) -> Path:
    """采购汇总落到「订单采购汇总/<日期>/」下，按天分文件夹。

    为什么不按批次一批一个目录：一天里往往要跑好几批（补单、换店铺），一批一个目录会把
    父目录撑成几十个只装两个文件的空壳；而采购是按天推进的活儿，同一天的几批要一起看。
    日期取自 stamp 前缀（stamp 来自导出文件名 订单导出_YYYYMMDD_HHMMSS），拿不到就用
    当天日期——同一批的 xlsx 和 md 共用这一次结果，不会被跨零点跑批拆到两个目录里。
    """
    date = stamp[:8] if stamp[:8].isdigit() else datetime.now().strftime("%Y%m%d")
    root = get_output_dir("orders_purchase")
    target = root / date
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception as e:
        # 建子目录失败（如目录名被同名文件占了）不该让整份统计导不出来，退回分类根目录
        logger.warning(f"创建采购汇总日期目录失败，落回 {root}：{e}")
        return root


def _export_purchase(
    pending_orders: List[pipeline.OrderRow], stamp: str, store: str = "",
    region: str = "",
) -> dict:
    """出本批的采购统计：xlsx（筛选核对用）+ md（合并下单速览用），各批一份新文件。

    两份各自独立 try：它们是两个用途不同的产物，一个写失败不该连坐另一个。整段都是
    best-effort——采购统计是辅助产物，坏了不能影响登记表写入这条主流程。

    dry-run 也照样出：试跑时人最需要的就是「这批要采什么、哪些能合单」，等到正式写入才
    给统计就晚了。
    """
    purchase = {
        "file": "", "md_file": "", "store": store, "region": region,
        "rows": len(pending_orders),
        "groups": 0, "repeated_groups": 0, "total_qty": 0,
        # 商品级：products=涉及几个 SPU，multi_products=要一次买多规格的有几个
        "products": 0, "multi_products": 0, "variants": 0,
        # 汇总两张表各嵌进去多少张主图（下不到图的行留空格）
        "product_images": 0, "sku_images": 0,
    }
    if not pending_orders:
        return purchase

    out_dir = str(_purchase_out_dir(stamp))
    try:
        purchase.update(
            pipeline.export_purchase_summary(
                pending_orders, out_dir, stamp, store, region)
        )
    except Exception as e:
        logger.warning(f"导出新增订单采购汇总 xlsx 失败（不影响登记表写入）：{e}")
    try:
        purchase.update(
            pipeline.export_purchase_markdown(
                pending_orders, out_dir, stamp, store, region)
        )
    except Exception as e:
        logger.warning(f"导出新增订单采购统计 md 失败（不影响登记表写入）：{e}")
    return purchase


async def _write_plans(
    plans: Dict[str, SheetPlan], workbook: str, dry_run: bool, on_progress: ProgressCB,
    cloud: Optional[KdocsSheet] = None,
) -> Dict[str, dict]:
    """按 Sheet 逐个批量写入（插到表头正下方，不是追加到表尾）。dry_run 时只报告不落盘。

    insert_at_top=True 是订单登记表的既定要求：新订单要在最上面。plan.rows 已在
    plan_writes 里按创建时间倒序排过，插进去自然是新→旧。

    一个 Sheet 写失败不连坐其它 Sheet（各自独立一次 zip 重写 + 独立备份），失败信息进
    汇总。不做重试：失败多半是表被 WPS 独占锁定或磁盘满，重试同样失败还多一份备份。

    cloud 非空时改写协作文档：先插空行再一次批量写值+按 URL 嵌图（图片用订单的在线
    主图 image_url，不经本地文件），写后读回首行验证。kdocs_sheet 内部已对限频做
    一次重试，这里同样不做额外重试。
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
            if cloud is not None:
                # plan.rows 与 plan.orders 在 _stage_order 里成对追加，按下标对齐取在线图
                items = [
                    {**item, "image_url": order.image_url}
                    for item, order in zip(plan.rows, plan.orders)
                ]
                res = await asyncio.to_thread(
                    cloud.write_rows, sheet, items, plan.header_row,
                )
            else:
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
    purchase: Optional[dict] = None,
    incremental: Optional[dict] = None,
    doc_mode: str = "",
) -> dict:
    """汇总本批结果。字段对 UI/CLI 都是稳定契约，别随手改名。"""
    plans = plans or {}
    written = written or {}
    return {
        "dry_run": dry_run,
        "aborted": aborted,
        # 本批实际写的是本地表还是协作文档（local/cloud），供 UI/CLI 收尾时明示落点
        "doc_mode": doc_mode,
        # 增量采集：enabled/known/stopped_early/fell_back/pages_swept/reason。
        # 与 truncated 分开看：truncated=可能漏，stopped_early=故意只取新单、不漏。
        "incremental": incremental or {"enabled": False, "known": 0, "reason": ""},
        # dry-run 导出的「待写计划」CSV 路径，供人工逐行核对
        "plan_files": list(plan_files or []),
        "store": (got or {}).get("store", ""),
        "export_file": (got or {}).get("export_file", ""),
        "total_on_page": (got or {}).get("total", 0),
        # True = 被 max_pages 截断，本批只覆盖前 N 页，不是全量
        "truncated": bool((got or {}).get("truncated", False)),
        "parsed_rows": len(orders or []),
        "images": img_stat or {},
        "purchase": purchase or {},
        "pending": sum(len(p.rows) for p in plans.values()),
        "dup_skipped": sum(p.dup for p in plans.values()),
        # dup_skipped 的子集：本批内撞键被丢的行数（＝少买，判重键不够细），单独报出来
        "collided_skipped": sum(len(p.collided) for p in plans.values()),
        "collided_samples": [c for p in plans.values() for c in p.collided][:5],
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
            s: {"pending": len(p.rows), "dup": p.dup,
                "collided": len(p.collided), "preview": p.preview[:3]}
            for s, p in plans.items()
        },
    }
