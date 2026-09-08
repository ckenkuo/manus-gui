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
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Union

from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from app.collect.pipeline import SheetSchema
    from app.orders.kdocs_sheet import KdocsSheet
    from app.temu_region import Region

from app.agent.manus import Manus
from app.cloud_docs import remember as remember_cloud_doc
from app.config import PROJECT_ROOT, config, config_search_dirs
from app.error_report import attach
from app.logger import logger

# 区域未确认异常与活动/订单侧共用同一个类型，三条管线的 UI/CLI 可用同一套 except 转成
# 「去浏览器里选好区域」的提示。沿用采集侧原有名字，调用方无需改名。
# host_of 是采集侧的区域主力：区域就是域名，从页签 URL 直接取，不必探测顶栏（见
# _enumerate_one_store）。read_region 只用来读那个供 UI 显示的中文名，best-effort。
from app.temu_region import RegionUnconfirmed as RegionNotConfirmed
from app.temu_region import host_of, read_region
from app.tool.wps_excel_tool import WpsExcelTool

# 采集目标出厂默认（偏好文件缺失时兜底；工作簿/Sheet 现已可在 UI/CLI 选择）
# 按当前用户的桌面拼，不写死用户名：原先硬编码 C:\Users\Administrator\... 是打包机的
# 路径，换机器/换账号必然指向不存在的文件，分发出去第一次采集就报错。
DEFAULT_EXCEL = str(Path.home() / "Desktop" / "商品成本核算_原始备份.xlsx")
DEFAULT_SHEET = "pawly全球"
# 商品列表页【路径】。刻意不留完整 URL：域名随区域变（全球 agentseller.temu.com、
# 美国 agentseller-us.temu.com），留个全球域常量，早晚有代码拿它 goto，把操作者选定的
# 区域悄悄换回全球。采集侧现在只复用用户已打开的列表页签（区域由他自己选定），不新开。
PRODUCT_SELECT_PATH = "/newon/product-select"
LIST_API = "searchForSemiSupplier"
# 采集范围「跟随当前页签」：不再写死 secondarySelectStatusList=[12]（已发布到站点），
# 而是让代码去点选中的页签、抓页面此刻真正发出的列表请求 body 原样复用（见 _enumerate_one_store）。
# 各页签的 status 码除 12 外均未实测确认，硬编码会踩「码必须实测」的规矩，故走抓包复用。
# 采集范围下拉的「可选覆盖」页签名字（对应 Temu 界面底部 tab 文字）。默认是空串=跟随页面
# 当前（不切页签、只点「查询」），这里仅列出想让代码替你切页签时的可选项。
STATUS_TABS = ["全部", "价格申报中", "未发布到站点", "已发布到站点", "已下架/终止"]
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


# ---- 云端协作文档目标（对齐 app/orders/service.py 的模式）--------------------
def is_cloud_link(value: str) -> bool:
    """是协作文档链接（http(s)://...）而非本地路径/文件 ID。"""
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def load_collect_config() -> dict:
    """读 [collect] 段：config.toml 优先，**该段缺失时退到 config.example.toml**。

    与 load_orders_config 同一回退策略：现网 config.toml 未必有 [collect] 段，
    随仓库分发的 example 充当默认值（占位空值），用户在 config.toml 写了就完全覆盖。
    解析失败只告警返回 {}，由调用方按「无云端目标」走本地路径。
    """
    # 目录维度也要遍历：冻结后 example 只存在于随包只读侧（_internal/config）。
    for cfg_dir in config_search_dirs():
        for name in ("config.toml", "config.example.toml"):
            p = cfg_dir / name
            if not p.exists():
                continue
            try:
                with p.open("rb") as f:
                    section = tomllib.load(f).get("collect") or {}
                if section:
                    return section
            except Exception as e:
                logger.warning(f"读 {p} 的 [collect] 配置失败：{e}")
    return {}


def cloud_backend(cfg: dict, cloud_url: str = "") -> Optional["KdocsSheet"]:
    """确定本批的云端写入目标；返回 None 表示走本地 xlsx 路径（行为不变）。

    优先级：显式给的协作文档链接（UI/CLI 粘贴的）> config 的 cloud_file_id。
    云端模式下判重/写入全部打在协作文档上，本地工作簿不再参与读写。
    """
    from app.orders.kdocs_sheet import KdocsSheet

    target = cloud_url.strip() or str(cfg.get("cloud_file_id") or "").strip()
    if not target:
        return None
    return KdocsSheet(target)


# 文档模式：UI 上的「本地文档 / 线上文档」开关值，与 orders 侧同一套取值。
# auto 是历史行为（按链接形态和 config 自动判），local/cloud 是用户显式钉死。
DOC_MODE_AUTO = "auto"
DOC_MODE_LOCAL = "local"
DOC_MODE_CLOUD = "cloud"
_DOC_MODES = (DOC_MODE_AUTO, DOC_MODE_LOCAL, DOC_MODE_CLOUD)

# 新行落点，四种：
#   bottom     追加到数据区末尾（默认，采集表习惯新品接在旧品之后）
#   top        插到表头正下方（订单登记表那套「新的在最上面」）
#   row_down   从指定行开始【向下插入】：该行及其以下整体下移，新行占据它原来的位置
#   row_up     从指定行开始【向上插入】：新行插在该行【之前】，即落在 row-n .. row-1
# 采集默认 bottom——2026-08-06 用户明确要求；云端此前一直沿用订单的 insert_at_top，
# 与本地 xlsx 的追加行为相反，两条路径就此对齐。
# row_down / row_up 都要配 append_row（1-based）；它们的差别只在「指定行本身要不要被推走」：
# row_down 从该行开始占位（该行下移），row_up 填到该行上面（该行仍是原内容、只是行号变大）。
APPEND_BOTTOM = "bottom"
APPEND_TOP = "top"
APPEND_ROW_DOWN = "row_down"
APPEND_ROW_UP = "row_up"
_APPEND_MODES = (APPEND_BOTTOM, APPEND_TOP, APPEND_ROW_DOWN, APPEND_ROW_UP)
_APPEND_ROW_MODES = (APPEND_ROW_DOWN, APPEND_ROW_UP)  # 需要 append_row 的两种
DEFAULT_APPEND_MODE = APPEND_BOTTOM

APPEND_MODE_LABELS = {
    APPEND_BOTTOM: "追加到末尾",
    APPEND_TOP: "插到表头下",
    APPEND_ROW_DOWN: "从指定行向下插",
    APPEND_ROW_UP: "从指定行向上插",
}


def normalize_append_mode(value: Optional[str]) -> str:
    """把外部传进来的落点值收敛到四个合法值之一；不认识的退默认（同 normalize_doc_mode 的
    理由：这个值来自 UI/CLI/prefs/config 四处，prefs 是历史文件、老版本没有这个键）。"""
    v = str(value or "").strip().lower()
    return v if v in _APPEND_MODES else DEFAULT_APPEND_MODE


def resolve_append_target(mode: str, append_row: Optional[int],
                         header_row: int) -> tuple[bool, Optional[int], str]:
    """把（落点模式, 指定行）解析成写入层要的 (insert_at_top, 起始行 1-based, 人读标签)。

    返回的起始行为 None 表示「由写入层自己算」（bottom 要读数据区末行、top 就是表头下一行）。
    row_down/row_up 返回具体行号，两者的差别在这里就地消化掉，写入层只认「插到第几行」：
      - row_down=R  → 新行占 R .. R+n-1（原 R 行及以下整体下移）
      - row_up=R    → 新行占 R-n .. R-1（插在 R 之前）
    行号非法（缺失/非数字/落到表头及以上）时【退回默认 bottom 并告警】而不是抛错：落点是
    辅助选项，写错了应当照常入库到安全位置，不该让整批停摆。row_up 的 n 由调用方在拿到
    起始行后自行减（见 _append_first_row）。
    """
    mode = normalize_append_mode(mode)
    if mode == APPEND_TOP:
        return True, header_row + 1, APPEND_MODE_LABELS[APPEND_TOP]
    if mode == APPEND_BOTTOM:
        return False, None, APPEND_MODE_LABELS[APPEND_BOTTOM]

    try:
        r = int(str(append_row).strip())
    except (TypeError, ValueError):
        r = 0
    if r <= header_row:
        logger.warning(
            f"落点「{APPEND_MODE_LABELS[mode]}」的行号（{append_row}）无效或落在表头"
            f"（第 {header_row} 行）及以上，本批退回「追加到末尾」。"
        )
        return False, None, APPEND_MODE_LABELS[APPEND_BOTTOM]
    return True, r, f"{APPEND_MODE_LABELS[mode]}（第 {r} 行）"


def normalize_doc_mode(value: Optional[str]) -> str:
    """把外部传进来的模式值收敛到三个合法值之一；不认识的一律当 auto。

    不认识就退 auto 而不是报错：这个值来自 UI/CLI/prefs 三处，其中 prefs 是历史文件
    （老版本存的 JSON 里没有这个键），报错会让开过旧版的人一开页就红。
    """
    v = str(value or "").strip().lower()
    return v if v in _DOC_MODES else DOC_MODE_AUTO


def _pick_local_excel(prefs: dict) -> str:
    """本地模式下没有本次显式选择时的工作簿：prefs 里那条（文件还在才算），否则空串。

    **刻意不猜**（2026-08-05 用户要求）：写错表要人工回滚，这种落点不该由代码按文件名
    关键词猜。返回空串时 UI 的工作簿下拉里已经有桌面候选（list_workbooks 按修改时间倒序
    扫桌面与输出目录），让用户自己点一个。

    路径要先确认文件还在：核算表常被改名（加日期后缀之类），失效时返回空串让用户重选，
    而不是把死路径抛给 UI 报「表不存在」。
    """
    path = str(prefs.get("excel") or "").strip()
    return path if path and os.path.exists(path) else ""


def resolve_cloud(excel: Optional[str] = None,
                  cloud_url: Optional[str] = None,
                  doc_mode: str = DOC_MODE_AUTO) -> Optional["KdocsSheet"]:
    """按调用方参数解析本批云端目标；返回 None 表示走本地 xlsx。

    doc_mode 显式给 local/cloud 时钉死走哪条路：kdocs 有配额，用满了要能立刻切回本地表
    继续干活（2026-08-05 用户要求）。auto 模式下只要 config 配了 cloud_file_id 就永远
    走云端，用户没有不改配置就切回本地的办法。
      - local：一律返回 None（本地），即便传进来的 excel 是个 kdocs 链接。
      - cloud：excel 是链接就用它，否则退 cloud_url 参数 > prefs.cloud_url > config。
      - auto（默认，行为不变）：显式给的协作文档链接 → 云端；【显式给的本地路径 →
        本地】——必须压制 prefs/config 里的云端目标，否则用户改选本地后，这一批仍会被
        写进旧云端文档（同名 Sheet 存在时不报任何错，直接落错文档）；未显式指定
        （空/None）→ cloud_url 参数 > prefs.cloud_url > [collect].cloud_file_id。
    """
    mode = normalize_doc_mode(doc_mode)
    explicit = (excel or "").strip()
    cfg = load_collect_config()
    if mode == DOC_MODE_LOCAL:
        return None
    if mode == DOC_MODE_CLOUD:
        url = explicit if is_cloud_link(explicit) else (
            (cloud_url or "").strip()
            or str(load_prefs().get("cloud_url") or "").strip()
        )
        return cloud_backend(cfg, cloud_url=url)
    if is_cloud_link(explicit):
        return cloud_backend(cfg, cloud_url=explicit)
    if explicit:
        return None
    url = (cloud_url or "").strip() or str(
        load_prefs().get("cloud_url") or "").strip()
    return cloud_backend(cfg, cloud_url=url)


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
    """读上次选择 {excel, cloud_url, sheet, store, status}；缺失/损坏返回 {}（best-effort，不抛错）。

    excel 是本地路径、cloud_url 是协作文档链接，两者互斥（见 save_prefs）。
    """
    if not COLLECT_PREFS.exists():
        return {}
    try:
        data = json.loads(COLLECT_PREFS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _looks_like_local_path(value: str) -> bool:
    """粗略判定本地路径形态（盘符/路径分隔符/.xlsx 后缀），供 prefs 拆分用。"""
    v = value.strip().lower()
    return (":" in v) or ("\\" in v) or ("/" in v) or v.endswith(".xlsx")


def save_prefs(
    excel: str = "", sheet: str = "", store: str = "", status: str = "",
    doc_mode: str = "", append_mode: str = "", append_row: Optional[int] = None,
    region_label: Optional[str] = None,
) -> None:
    """记住本次选择（含采集页签 status），供下次 UI/CLI 缺省回填。写失败只告警、不阻断采集。

    excel 是协作文档链接或云端 file_id（非本地路径形态）时存到 cloud_url、清空 excel
    （对齐 orders 的 prefs 拆分）：下次首屏按「显式链接 > prefs.cloud_url >
    config.cloud_file_id」解析云端目标；显式选过本地路径则清掉 cloud_url，
    避免旧链接盖掉用户后来的选择。

    doc_mode 显式给 local/cloud 时两侧目标互不覆盖（本地模式保留原 cloud_url，反之亦然），
    这样在两个模式间来回切不用重填对面那个路径——kdocs 配额用满时切本地、次日切回云端是
    常规操作。auto（没传，即老版本 UI/CLI）时保持原来的互斥语义：auto 的目标解析仍靠
    「cloud_url 有值就走云端」，留着旧链接会把用户后来选的本地表盖掉。
    """
    mode = normalize_doc_mode(doc_mode)
    # 落点偏好与目标解析无关，故【总是】读旧值兜底（上面的 old 在 auto 模式下是空的，
    # 不能借用）：不传就沿用上次选的，避免旧版 UI/CLI 不带这个参数时被悄悄重置成默认。
    _prev = load_prefs()
    prev_append, prev_row = _prev.get("append_mode"), _prev.get("append_row")
    old = load_prefs() if mode != DOC_MODE_AUTO else {}
    row_val = append_row if append_row is not None else prev_row
    try:
        row_val = int(str(row_val).strip()) if row_val not in (None, "") else None
    except (TypeError, ValueError):
        row_val = None
    # 区域选择：None = 本次没传，沿用上次记的（别被空串重置掉，老版本 UI/CLI 不带这个参数）；
    # 空串 = 显式选「跟随浏览器当前区域」。
    region_val = (
        _prev.get("region_label") or "" if region_label is None
        else str(region_label).strip()
    )
    data = {"excel": "", "cloud_url": "", "sheet": sheet, "store": store,
            "status": status, "doc_mode": mode,
            "append_mode": normalize_append_mode(append_mode or prev_append),
            "append_row": row_val, "region_label": region_val}
    v = (excel or "").strip()
    if is_cloud_link(v) or (v and not _looks_like_local_path(v)):
        data["cloud_url"] = v
        data["excel"] = str(old.get("excel") or "")
    else:
        data["excel"] = excel
        data["cloud_url"] = str(old.get("cloud_url") or "")
    # 协作文档链接同时进登记簿（app/cloud_docs.py），下次开页可直接从候选里选；
    # 只登记真正的 http(s) 链接，file_id 形态不进登记簿（没法在 UI 里直接选）。
    # 只登记本次真正传进来的，上面继承的旧值不必再登记一遍
    if is_cloud_link(v):
        remember_cloud_doc(v)
    try:
        COLLECT_PREFS.parent.mkdir(parents=True, exist_ok=True)
        COLLECT_PREFS.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
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


def dedupe_key(spu, sku="") -> str:
    """把（SPU, 规格文本）归一成判重键 `SPU|规格`。清单侧与读表侧共用同一个口径。

    货号列存的就是规格文本（如 `奶白+黑色/10双`，见 pipeline._build_column_values），
    两侧都用本函数造键，键与写入值同源、不会各写一套悄悄漂移（对齐 orders 的 dedupe_key）。

    【只 strip、不做别的归一】规格串里空格是有意义的（实测有「1-2 Pack Black/Large-X-Large」
    这种），不能按空格截断；大小写与全半角也照原样比——平台给什么就是什么，自作主张归一
    反而会把两个真不同的规格并成一个。
    代价是平台改文案（"10双"→"10 双"）会让同一个 SKU 算成新键、多写一行；这是选「纯规格值
    货号」换来的，取舍见本次改动的决策记录。
    """
    return f"{str(spu or '').strip()}|{str(sku or '').strip()}"


def worklist_key(it: dict) -> str:
    """清单条目 → 判重键。老清单没有 sku_spec 时退化成 `SPU|`，与纯 SPU 判重等价。"""
    return dedupe_key(it.get("spu"), it.get("sku_spec"))


def done_flags(items: list, done: set) -> list:
    """逐行判「是否已入库」，返回与 items 等长的布尔列表。两条规则叠加：

    ① **组合键精确命中**（`SPU|规格` 已在表里）→ 已入库。这是常规判重。

    ② **历史 SPU 整体跳过**：表里已有该 SPU 的行，但那些行的货号没有一个能对上本清单里
       该 SPU 的任何规格 → 判定它们是改造前的人工行，整个 SPU 跳过。
       为什么需要这条：历史行是「一个 SPU 一行」，货号由人手填（实测 wintak美国 是「5双」、
       pawly全球 是「直径32CM」「单人」），与本管线生成的规格串对不上，纯按组合键判重会把
       整表 388 行全判成待采、重写一遍。操作者选定的口径是「跳过已有 SPU」。

    【为什么用「有没有交集」而不是「SPU 在不在表里」】后者会误伤分批采集：本管线第一批
    写了某 SPU 的 2 个规格，第二批时该 SPU 已在表里，剩下的规格就永远补不上了。
    改用交集判据后，只要表里有一行是本管线写的（货号能对上清单里的某个规格），就说明这个
    SPU 正在被本管线采，此时只跳过精确命中的那几行，其余规格照采。
    """
    sheet_specs: dict = {}
    for k in done:
        spu, _, spec = str(k).partition("|")
        sheet_specs.setdefault(spu, set()).add(spec)

    list_specs: dict = {}
    for it in items:
        list_specs.setdefault(str(it.get("spu") or "").strip(), set()).add(
            str(it.get("sku_spec") or "").strip()
        )

    flags = []
    for it in items:
        spu = str(it.get("spu") or "").strip()
        in_sheet = sheet_specs.get(spu)
        if worklist_key(it) in done:
            flags.append(True)
        elif in_sheet and not (in_sheet & list_specs.get(spu, set())):
            flags.append(True)  # 表里是历史人工行 → 整个 SPU 跳过
        else:
            flags.append(False)
    return flags


def existing_keys(
    excel: str,
    sheet: str,
    cloud=None,
    fields: Optional[dict] = None,
    header_row: int = 1,
) -> set:
    """读目标表已入库的判重键集合 `{"SPU|skuId"}`；本地/云端只在最后一步分叉。

    【为什么必须是组合键】2026-08-11 起清单是一个 SKU 一行，同一 SPU 占多行、SPU 列必然
    重复。仍按纯 SPU 判重的话，该商品第一行写进去之后，其余规格全被判成"已入库"跳过——
    一个商品永远只落一行，正是要修的症状。

    货号列不存在（老表没这列）→ 退回纯 SPU 判重（键的 SKU 位为空），与改造前行为一致。
    读表失败由底层各自吞成空集（判重失效只会多写、不会写坏表），此处不另加 try。

    header_row：云端是 0-based、本地是 1-based（两边底层约定不同，别传串）。
    """
    if fields is None:
        fields = (
            WpsExcelTool._resolve_fields_from_header(cloud.read_header(sheet)[0])
            if cloud is not None
            else WpsExcelTool.resolve_field_columns(excel, sheet)
        )
    spu_col = fields.get("spu") or (
        None if cloud is not None else spu_col_of(excel, sheet)
    )
    if not spu_col:
        return set()
    sku_col = fields.get("sku")

    if cloud is not None:
        rows = (
            cloud.existing_key_tuples(sheet, [spu_col, sku_col], header_row)
            if sku_col
            else [(v,) for v in cloud.existing_key_values(sheet, spu_col, header_row)]
        )
    else:
        rows = (
            WpsExcelTool.existing_key_tuples(
                excel, sheet, [spu_col, sku_col], header_row)
            if sku_col
            else [
                (v,) for v in WpsExcelTool.existing_key_values(
                    excel, sheet, spu_col, header_row)
            ]
        )
    return {dedupe_key(*r) for r in rows}


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


# 页内翻页取全部商品并抽字段。关键：url + body 都用【页面此刻真正发出的那条列表请求】原样复用
# （由 _enumerate_one_store 抓包传入），只把 pageNum/pageSize 覆盖掉逐页翻。这样筛选条件
# （secondarySelectStatusList 等，即你选的页签）完全跟随当前页面，不再写死「已发布到站点」。
# url/body 任一没抓到就返回空（best-effort，交上层「0 条不覆盖」护栏兜底），绝不猜接口/造 body。
#
# 【2026-08-11 改·一个 SKU 一条】原先每个商品只出一条、价取 SPU 级 `it.supplierPrice`。
# 实测（128 商品 / 388 SKU）那个字段对多 SKU 商品是【价格区间串】如 "60.00~299.68¥"，
# 写入时被 pipeline._to_number 的正则截成下限 60.0——128 个商品里 89 个（70%）销售价是错的。
# 样本 SPU 7948115685（蕾丝袜 2/4/6/8/10 双装）表里写 60，而 10 双装实际 299.68。
#
# 逐 SKU 真价挂在 `skcList[].skuList[].siteSupplierPriceList[]`（按站点分列），实测：
#   - 388/388 的 SKU 都有这个数组，且每个 SKU 恒为 1 条（不存在一 SKU 多站点价）
#   - 388/388 的 siteName 与 SPU 级 siteName 一致
#   - sku.supplierPrice / sku.weight / extCode 基本全空，别指望它们（extCode 仅 4/388 非空）
# 故按 SPU 站点名匹配取那条，匹配不上退第一条；不硬编码 siteId（站点会增减）。
#
# skcList/skuList 缺失或为空 → 退回原来的「一个商品一条」（sku_id 留空），保住不比现在差；
# 这是既有 best-effort 取向，不是另造备用分支。
_FETCH_ALL_JS = r"""
async ({mallid, url, body}) => {
  const out = [];
  if (!url || !body) return out;  // 没抓到真实请求 → 不臆造，直接空
  const hdr = {'content-type': 'application/json'};
  if (mallid) hdr['mallid'] = mallid;
  let tpl = {};
  try { tpl = JSON.parse(body); } catch (e) { return out; }
  const size = tpl.pageSize || 50;
  // 取该 SKU 在本商品站点下的申报价：先按站点名匹配，匹配不上退第一条有价的。
  const skuPrice = (sku, site) => {
    const list = sku.siteSupplierPriceList || [];
    const hit = list.find(x => x && String(x.siteName || '') === String(site || ''));
    const pick = hit || list.find(x => x && x.supplierPrice);
    return (pick && pick.supplierPrice) || '';
  };
  // 规格文本：只取属性【值】拼成「奶白+黑色/10双」，写进货号列。
  // 【为什么不带属性名】这列本来就是人工在记规格，历史值形如「5双」「直径32CM」「单人」，
  // 带上「颜色=…/数量=…」会让新旧行风格割裂。判重也认这串文本（见 service.dedupe_key）。
  const skuSpec = (sku) => (sku.productPropertyList || [])
      .map(p => String(p.value == null ? '' : p.value).trim()).filter(Boolean).join('/');
  for (let pageNum = 1; pageNum <= 40; pageNum++) {
    // 以捕获 body 为模板，仅覆盖翻页字段，保留其余筛选条件原样
    const payload = Object.assign({}, tpl, {pageNum, pageSize: size});
    const resp = await fetch(url, {
      method: 'POST', headers: hdr, credentials: 'include',
      body: JSON.stringify(payload)
    });
    const j = await resp.json();
    const dl = (j.result && j.result.dataList) || [];
    for (const it of dl) {
      const site = it.siteName || (it.siteInfoList && it.siteInfoList[0] && it.siteInfoList[0].siteName) || '';
      const base = {
        spu: String(it.productId),
        name: (it.productName || '').slice(0, 120),
        site,
        category: it.leafCategoryName || (Array.isArray(it.fullCategoryName) ? it.fullCategoryName[it.fullCategoryName.length - 1] : '') || '',
        image: (it.carouselImageUrlList && it.carouselImageUrlList[0]) || ''
      };
      let expanded = 0;
      for (const skc of (it.skcList || [])) {
        for (const sku of (skc.skuList || [])) {
          if (!sku || sku.skuId === undefined || sku.skuId === null) continue;
          out.push(Object.assign({}, base, {
            sku_id: String(sku.skuId),
            sku_spec: skuSpec(sku),
            // 该 SKU 自己的申报价；读不到就留空（宁可空着待人工，也不写别的 SKU 的价）
            price: skuPrice(sku, site),
            // 不同颜色的 SKU 有各自预览图，缺则退回 SPU 主图
            sku_image: sku.skuPreviewImage || (skc.previewImgUrlList && skc.previewImgUrlList[0]) || base.image
          }));
          expanded++;
        }
      }
      if (!expanded) {
        // 没有可用的 SKU 结构 → 退回一个商品一条（与改造前一致）
        out.push(Object.assign({}, base, {
          sku_id: '', sku_spec: '', price: it.supplierPrice || '', sku_image: base.image
        }));
      }
    }
    if (dl.length < size) break;  // 最后一页
  }
  return out;
}
"""


# best-effort 读店铺名，四级降级：__USER_INFO__ → rawData → 语义类名 → 顶栏最右启发式。
#
# 【为什么不能靠顶栏启发式】它反复误命中：先是「查看使用帮助」「打开商家助手」，2026-08-06
# 又是「查看使用教程」——这些帮助入口渲染得比店名还靠右，文案随版本改名，靠排除词表永远
# 追不上（每修一个又冒一个）。所以必须优先读页面自己注入的状态数据。
#
# 【2026-08-07 实测修正：rawData 在本版后台压根不存在】product-select 页
# `window.rawData` 为 undefined（`has_rawData: false`），于是原来的「rawData 优先」形同
# 虚设，实际一路掉到最末的启发式——那次恰好挑对（Pawly）纯粹因为新文案「查看使用教程」
# 已被 badKw 拦住，再冒一个词就会误命中。订单侧 _STORE_JS 同样实测返回空串。
#
# 真正稳的挂载点是 `window.__USER_INFO__.shopList[].malInfoList[]`，实测结构：
#   {mallId: 634418228070796（**数字**）, mallName: "Pawly", managedType: 1, mallMode: 1, ...}
# 注意平台把 mall 拼成了 `mal`（malInfoList），别照 mallList 写。mallId 是数字类型，
# 与 mallid cookie/请求头的字符串比对前必须 String() 归一，否则 === 永远不等。
# rawData 那级予以保留：别的后台版本/页面可能有它，删掉等于自断一条路（见项目「保留原有
# 正确逻辑」的增量修改约定）。
#
# 【用 mallid 精确定位】采集侧此时已从网络请求头嗅到该标签的 mallid，多店账号也能精确挑出
# 【当前这个店】；匹配不上再退「列表只有一个」的单店情形。全失败返回 ''（调用方退回 mallid）。
_READ_STORE_NAME_JS = r"""
(mallid) => {
  const ok = (t) => t && t.length >= 1 && t.length <= 40 ? t : '';
  // 从「mall 对象数组」里挑店名：先按 mallid 精确匹配，其次单店直接取。
  // mallId 实测是数字，故两边都 String() 归一再比。
  const pickFrom = (arr) => {
    if (!Array.isArray(arr) || !arr.length) return '';
    if (mallid) {
      const hit = arr.find(m => String(m?.mallId ?? m?.mallid ?? '') === String(mallid));
      if (hit) { const t = ok(String(hit.mallName || '').trim()); if (t) return t; }
    }
    if (arr.length === 1) return ok(String(arr[0]?.mallName || '').trim());
    return '';
  };

  // ① __USER_INFO__.shopList[].malInfoList[]（2026-08-07 实测存在且带 mallId+mallName）
  try {
    for (const shop of (window.__USER_INFO__?.shopList || [])) {
      const t = pickFrom(shop?.malInfoList);
      if (t) return t;
    }
  } catch (e) { /* 结构变了就往下降级 */ }

  // ② rawData.store.authUser.mallList（本版后台不存在，保留兼容别的版本）
  try {
    const t = pickFrom(window.rawData?.store?.authUser?.mallList);
    if (t) return t;
  } catch (e) { /* 同上 */ }

  const clean = (el) => el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  // ③ 语义类名（类名全 hash 化时命不中，但别的卖家中心版本可能有）
  for (const sel of ['[class*="storeName"]','[class*="store-name"]',
                     '[class*="mallName"]','[class*="mall-name"]',
                     '[class*="shopName"]','[class*="shop-name"]']) {
    const t = ok(clean(document.querySelector(sel)));
    if (t) return t;
  }
  // ④ 顶栏最右侧短文本启发式（最不可靠，仅兜底）
  // bad 分两类：① 精确匹配已知功能按钮词；② 含帮助/教程/助手类字样的一律排除——
  // 这些入口常比店名更靠右，精确词表跟不上改名，故按关键字通配。店名基本不含这些字。
  const bad = /^(Beta|\d+|99\+|学习|自营对接|经理助手|消息|客服|设置|市场|履约管理|首页|通知)$/;
  const badKw = /(教程|帮助|指引|指南|使用说明|新手|助手|客服|反馈|下载|登出|退出|切换)/;
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
  const pick = items.find(o => !bad.test(o.t) && !badKw.test(o.t));
  return pick ? pick.t : '';
}
"""


# 点页面自带的「查询」按钮触发一次列表请求：用当前表单里所有筛选（类目/站点/商品名/时间…）
# + 当前选中页签原样重发，故采集范围完全跟随你在 Temu 页面的设置。精确匹配「查询」两字、
# 显式排除「重置」，绝不误点清空筛选。找到并点中返回 true。
_CLICK_QUERY_BTN_JS = r"""
() => {
  const nodes = [...document.querySelectorAll('button, [role="button"], a, span, div')];
  const hit = nodes.find(e => {
    const t = (e.textContent || '').replace(/\s+/g, '').trim();
    if (t !== '查询') return false;                       // 精确「查询」，排除「重置」
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.width < 160 && r.height > 0;   // 是个真按钮、非大容器
  });
  if (hit) { hit.click(); return true; }
  return false;
}
"""

# 按页签文字点击（可选覆盖用）：叶子/浅层节点 + 窄宽度，避免误点到含同样文字的大容器。
# 注意切页签可能重置详细筛选，故仅在用户显式指定了具体页签时才调用（默认「跟随页面」不切）。
_CLICK_TAB_JS = r"""
(tab) => {
  const t = [...document.querySelectorAll('*')].find(e =>
    (e.textContent || '').includes(tab)
    && e.children.length <= 2
    && e.getBoundingClientRect().width > 0
    && e.getBoundingClientRect().width < 200);
  if (t) { t.click(); return true; }
  return false;
}
"""


async def _enumerate_one_store(
    ctx, page, status_tab: str = "", allow_cookie_fallback: bool = False,
) -> tuple:
    """在单个 product-select 店铺标签内嗅 mallid + 抓该店当前筛选下的全部商品 + 读店名。

    返回 (mallid, store_label, items)。items 每条打上该店 mallid/store/region 标签。

    **区域直接取自本页签的域名**（`host_of(page.url)`）：区域就是域名（见 app/temu_region.py），
    而页签 URL 天然带着它。这条路零探测、永不失败，也不需要「所有页签必须在同一区域」——
    每个页签的商品各自打自己的 host，同时开着全球和美国两个页签也能一次采完、各归各的店铺键。
    顶栏那个中文显示名（全球/美国）只用于 UI 显示，故 best-effort 读、读不到就留空。
    单店内任一步失败只告警、返回已拿到的部分（best-effort），不阻断其它店。

    触发方式：点页面自带的「查询」按钮，用表单里当前所有筛选（页签 + 类目/站点/商品名/时间…）
    原样重发一次列表请求，抓其 url + body 翻页复用（只改 pageNum/pageSize，见 _FETCH_ALL_JS）。
    故采集范围完全跟随你在 Temu 页面的筛选，无需在采集端重建表单、也不硬编码任何接口字段。

    status_tab：可选覆盖。空串（默认）= 跟随页面当前页签，只点「查询」不切页签，保住已设的
    详细筛选；非空 = 先替你点该页签再查询（注意：切页签可能被 Temu 重置掉详细筛选）。

    allow_cookie_fallback：仅【单标签】场景为 True——网络嗅探失败时退回读 mallid cookie
    （等价旧单店行为）。多标签禁用：mallid cookie 是 context 级、跨标签共享，会把当前
    激活店的 mallid 错安到别的店上（串店）。
    """
    # 抓包结果：mallid（请求头）+ list_url/list_body（该页签真实列表请求，供翻页复用）。
    # req_id：CDP 对较大 POST body 会省略事件内联的 postData、只给 hasPostData 标记，此时
    # 记下 requestId，事后用 Network.getRequestPostData 补拉真实 body（见下方等待循环后）。
    cap = {"mallid": None, "url": None, "body": None, "req_id": None}
    client = await ctx.new_cdp_session(page)
    await client.send("Network.enable")

    def on_req(params):
        req = params.get("request", {})
        if LIST_API not in req.get("url", ""):
            return
        if not cap["mallid"]:
            cap["mallid"] = req.get("headers", {}).get("mallid")
        if cap["body"] or cap["req_id"]:
            return  # 已锁定该店的列表请求，忽略后续同接口请求，避免被覆盖
        # 列表查询是 POST：优先用事件内联的 postData；CDP 对较大 body 只给 hasPostData，
        # 此时锁定 requestId 事后补拉，绝不臆造 body。
        pd = req.get("postData")
        if pd:
            cap["url"] = req.get("url")
            cap["body"] = pd
        elif req.get("hasPostData"):
            cap["url"] = req.get("url")
            cap["req_id"] = params.get("requestId")

    client.on("Network.requestWillBeSent", on_req)

    # 触发一次真实列表请求以抓 mallid + body：把该标签置前
    try:
        await page.bring_to_front()
    except Exception:
        pass
    await asyncio.sleep(3)

    # 若显式指定了页签，先替用户切一次（仅一次，避免反复切放大重置副作用）
    if status_tab:
        try:
            switched = await page.evaluate(_CLICK_TAB_JS, status_tab)
            if not switched:
                logger.warning(f"未找到「{status_tab}」页签，改为跟随页面当前筛选")
            await asyncio.sleep(1)
        except Exception:
            pass

    for _ in range(20):  # 最多等 ~20s，直到抓到列表请求 body（或至少锁定 requestId 待补拉）
        if cap["body"] or cap["req_id"]:
            break
        try:
            # 点页面自带「查询」按钮，用当前表单所有筛选原样重发（跟随页面设置）
            await page.evaluate(_CLICK_QUERY_BTN_JS)
        except Exception:
            pass
        await asyncio.sleep(1)

    # body 未内联在事件里（CDP 对较大 POST 常如此）→ 用 requestId 补拉真实 body。
    # 补拉失败只告警、body 仍为 None，交上层「0 条不覆盖」护栏兜底，绝不臆造。
    if not cap["body"] and cap["req_id"]:
        try:
            r = await client.send(
                "Network.getRequestPostData", {"requestId": cap["req_id"]}
            )
            cap["body"] = r.get("postData")
        except Exception as e:
            logger.warning(f"补拉列表请求 body 失败：{e}")
    mallid = {"v": cap["mallid"]}

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
    if not cap["body"]:
        logger.warning(
            "未抓到列表请求（可能没找到「查询」按钮或页面未加载），该店返回 0 条。"
            "请确认该店列表页已打开、可见「查询」按钮后重试。"
        )

    # 店铺标签：best-effort 读店名，读不到退回 mallid。
    # 传 mallid 进去让它在 rawData.mallList 里精确挑出当前店（多店账号也不会串），
    # 故这段必须排在上面的 mallid 嗅探之后。
    store_label = ""
    try:
        store_label = (
            await page.evaluate(_READ_STORE_NAME_JS, mallid["v"] or "") or ""
        ).strip()
    except Exception as e:
        logger.warning(f"读店名失败（退回 mallid 当标签）：{e}")
        store_label = ""
    if not store_label:
        store_label = mallid["v"] or ""

    try:
        # 用抓到的真实 url + body 翻页复用（跟随该页签筛选）；未抓到则内部返回空
        items = await page.evaluate(
            _FETCH_ALL_JS,
            {"mallid": mallid["v"], "url": cap["url"], "body": cap["body"]},
        )
    except Exception as e:
        logger.warning(f"抓取该店商品失败：{e}")
        items = []

    try:  # 释放该标签的 CDP 会话，避免跨店监听泄漏/串扰
        await client.detach()
    except Exception:
        pass

    # 区域随每条商品落库：同一 mallid 在不同区域（全球/美国…）是不同的数据范围、通常也落
    # 不同 Sheet，缺了这个维度就会被判成同一个店（见 app/temu_region.py）。
    # host 从本页签 URL 直接取——区域就是域名，不必探测顶栏，也不会失败。
    region_host = host_of(getattr(page, "url", "") or "")
    # 显示名仅供 UI 展示（店铺下拉里的「WINTAK · 全球」），读不到就空着，绝不阻断采集
    region_label = ""
    try:
        region_label = (await read_region(page)).label
    except Exception as e:
        logger.warning(f"读顶栏区域显示名失败（仅影响 UI 显示，继续采集）：{e}")

    for it in items:
        it["mallid"] = mallid["v"] or ""
        it["store"] = store_label
        it["region"] = region_host
        it["region_label"] = region_label
    return mallid["v"], store_label, items


async def peek_regions() -> dict:
    """只读探当前浏览器的区域状态，供 UI 渲染区域下拉。

    返回 {current, host, options, error}：current 是浏览器此刻所在区域，options 是该账号
    顶栏可见的全部区域（下拉候选）。区域候选只能从页面读——账号能看到哪些区域随权限变，
    代码里写死一份映射就是在猜（见 app/temu_region.py 的设计取向）。

    best-effort：连不上 CDP / 没开后台页 / 读不到都只回 error 字符串，绝不抛——首屏不该
    因为浏览器没开就打不开。
    """
    from app.temu_region import is_seller_page, read_region

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
            try:
                if not browser.contexts:
                    return {"error": "CDP 浏览器没有可用 context", "options": []}
                pages = [p for p in browser.contexts[0].pages
                         if is_seller_page(p.url or "")]
                if not pages:
                    return {"error": "没有打开 Temu 后台页面", "options": []}
                r = await read_region(pages[0])
                return {"current": r.label, "host": r.host,
                        "options": list(r.labels), "error": ""}
            finally:
                await browser.close()
    except Exception as e:
        logger.warning(f"读区域候选失败（首屏仅提示，忽略）：{e}")
        return {"error": str(e), "options": []}


# 【已删除 confirm_active_region / _check_region（2026-08-11）】
# 它们是采集前的「确认基准区域 + 逐页签复核同区域」关卡，读不到顶栏就抛 RegionNotConfirmed
# 中止整批。删掉的理由：采集本来就在操作者已打开的页签里干活，页签停在哪个区域、采到的就是
# 那个区域的数据；而区域就是域名，页签 URL 天然带着它（见 _enumerate_one_store 的 host 打标）。
# 那道关卡想保护的「同 mallid 跨区域数据别混成一个店」，host 打标已经免费做到了，且不会因为
# 顶栏没渲染完/页面往下滚（列表在内部容器里滚，顶栏会被平移出视口）而误报中止。
# 顺带解除了「所有页签必须在同一区域」的限制：现在同时开着全球和美国的页签也能一次采完。
# 显式切区域的能力保留在 temu_region.switch_region，仅当 UI/CLI 明确指定区域时才调。


async def enumerate_worklist(status_tab: str = "", region_label: str = "") -> int:
    """确定性枚举：遍历调试 Chrome 里所有已打开的 product-select 店铺标签，逐店点「查询」
    抓其当前筛选下的真实列表请求、翻页取全部商品，合并写 worklist.json，返回条数。

    status_tab：可选覆盖。空串（默认）= 跟随各店页面当前的页签 + 所有筛选（类目/站点/商品名
    等你在 Temu 表单里设好的条件）；非空 = 先替你点该页签再查询（切页签可能重置详细筛选）。
    逐店透传给 _enumerate_one_store，采集端不重建表单、不硬编码任何接口字段。

    多店关键：mallid cookie 是 context 级、跨标签共享、只反映当前激活店，故【不能】靠
    cookie 区分店铺——必须逐标签从各自的网络请求头嗅 mallid（见 _enumerate_one_store）。

    **区域不再是前置关卡**（2026-08-11 改）：采集就在你已打开的那些页签里干活，页签停在哪个
    区域、采到的就是那个区域的数据，而区域就是域名（见 app/temu_region.py），页签 URL 天然
    带着它——所以每个页签各自按自己的 host 打标即可，不必先探测顶栏、更不必要求所有页签同区域。
    原先那道「确认区域，读不到就中止」的关卡只会白白挡住采集：顶栏没渲染完、页面往下滚了
    （列表在内部容器里滚，顶栏会被平移出视口）都能让它误报，而它拦下来保护的东西，host 打标
    已经免费提供了。

    region_label：仅当**显式指定**时才动浏览器——把各页签切到该区域再采（换域名的整页跳转）。
    留空（默认，也是 UI 的默认）＝完全不碰页签，跟随它们当前所在的区域。
    """
    cdp = CDP_URL
    all_items: list = []
    store_count = 0
    want_region = str(region_label or "").strip()
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0]
        try:
            # 所有已打开的列表标签（每个 = 一个已登录店铺在某区域下的视图）
            store_pages = [
                p for p in ctx.pages if PRODUCT_SELECT_PATH in (p.url or "")
            ]
            if not store_pages:
                raise RegionNotConfirmed(
                    "没有打开任何 Temu 商品列表页（product-select）。请先在调试 Chrome 里"
                    "打开商品列表页，再开始采集。"
                )
            # 只有显式选了区域才切页签：这是操作者主动要求换区域作业，切不动就该如实报错
            if want_region:
                from app.temu_region import switch_region

                for page in store_pages:
                    await switch_region(page, want_region)

            for idx, page in enumerate(store_pages, 1):
                mid, label, items = await _enumerate_one_store(
                    ctx, page, status_tab=status_tab,
                    allow_cookie_fallback=len(store_pages) == 1,
                )
                store_count += 1
                region_desc = (items[0].get("region_label") or "") if items else ""
                logger.info(
                    f"[店 {idx}/{len(store_pages)}] "
                    f"区域={region_desc or '(顶栏未读到)'}（{host_of(page.url or '')}） "
                    f"mallid={mid or '无'} 店名={label or '(未读到)'} 商品={len(items)}"
                )
                all_items.extend(items)
        finally:
            await browser.close()

    # 合并去重：同一 spu 可能出现在不同店/不同区域 → 按 (店铺+区域, spu, sku) 去重，各留一份
    # （可落不同 Sheet）。键必须含区域：同一 SPU 在全球区和美国区各有一条时，只按 mallid
    # 去重会误删掉其中一条（mallid 跨区域不变，见 _store_key）。
    # 键还必须含 SKU：清单已是一个 SKU 一条（见 _FETCH_ALL_JS），只按 spu 去重会把同一
    # 商品的其余规格全当重复项删掉，只剩第一个 SKU。老结构（无 sku_id）该位是空串，行为不变。
    # 这里用 sku_id（平台主键）而非规格文本：同一商品下两个 SKU 规格文案偶有雷同，
    # 用文本会在枚举阶段就把其中一个丢掉；判重落表用的才是规格文本（见 dedupe_key）。
    seen, uniq = set(), []
    for it in all_items:
        spu = it.get("spu")
        key = (_store_key(it), spu, str(it.get("sku_id") or ""))
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
    # 落盘存平台全量快照（一个 SKU 一条），折叠只在读取侧做（见 load_worklist）：这样以后
    # 想改折叠判据或回看某个规格的原始价，不必重新连浏览器枚举一遍。
    # 返回值报【折叠后】的行数，与 UI 待采统计、实际写表行数同口径——报全量会让操作者按
    # 388 去设本批数量，而实际可采只有折叠后那些行。
    collapsed = collapse_same_price_skus(uniq)
    merged = len(uniq) - len(collapsed)
    logger.info(
        f"枚举完成（页签「{status_tab}」）：{len(uniq)} 行原始 SKU，"
        f"同 SPU 同价折叠后 {len(collapsed)} 行（合并 {merged} 行），"
        f"来自 {store_count} 个店铺标签 → {WORKLIST}"
    )
    return len(collapsed)


def _price_bucket(v) -> str:
    """把 SKU 申报价归一成「用于比较是否同价」的桶键。

    平台同一价格的文本形态并不统一（实测有 `46.10¥` / `46.1` / `1,299.00¥`），直接比
    字符串会把同价的两个 SKU 判成不同价、白白多写一行；故复用 pipeline._to_number 剥成
    数字再比——那也正是最终写进销售价列的值，「同价」的判据与落表的值同源。
    保留两位小数：申报价就是两位精度，浮点直接比会因 46.1 与 46.10 的表示差异出岔。
    读不出数字（空价/脏值）→ 返回原始文本做桶键，不与任何有价 SKU 合并：价读不到的行
    本就要留给人工，不能被同 SPU 的别的行「代表」掉。
    """
    from app.collect.pipeline import _to_number

    n = _to_number(v)
    return f"n:{round(n, 2)}" if n is not None else f"s:{str(v or '').strip()}"


def collapse_same_price_skus(items: list) -> list:
    """同一 SPU 下申报价相同的多个 SKU 只保留一条，返回折叠后的清单。

    【为什么要折叠】清单一个 SKU 一条后，一个 SPU 常展开出十几行（实测 128 商品 → 388
    行），但成本核算表关心的是【采购价与销售价】：同 SPU 里价格一样的规格（比如只是颜色
    不同的 5 双装），每行的销售价、采购价、重量、折扣全都一模一样，逐行写只会把 Sheet
    撑得又长又难看，人工核对时还得逐行确认「这几行是不是重复的」。价格不同的规格（2 双
    /10 双装那种）才是真正需要各占一行、各自核价的对象，一条都不能少。

    折叠键是 (店铺+区域, SPU, 价格桶)：
    - 必须带店铺+区域：同一 SPU 在全球区与美国区各有一条、落进不同 Sheet，跨区域合并会
      直接抹掉其中一个店的行（与 enumerate_worklist 里合并去重的理由相同）。
    - 价格桶按数值比，见 _price_bucket。

    保留组内【第一条】（即平台返回顺序里的首个 SKU），它的 sku_spec 就成了这一行的货号。
    这样判重键 `SPU|规格`（见 dedupe_key）仍与写进表里的值同源，不会因为折叠而漂移。
    代价是平台调整 SKU 顺序后，同一价格组的「代表规格」可能换成另一个规格名，届时该组会
    被判成新键、多写一行；这是选「货号写纯规格文本」换来的，与平台改文案的既有代价同源。

    老清单（无 sku_id / 无 price）不受影响：价读不出时各自独立成桶（见 _price_bucket），
    一条也不会被合并掉。
    """
    seen: set = set()
    out: list = []
    for it in items:
        if not isinstance(it, dict):
            continue
        spu = str(it.get("spu") or "").strip()
        if not spu:
            out.append(it)  # 无 SPU 的行不参与折叠，原样透传给下游护栏处理
            continue
        key = (_store_key(it), spu, _price_bucket(it.get("price")))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    dropped = len(items) - len(out)
    if dropped:
        # 走 debug：本函数在每次 UI 刷新状态时都会被调（load_worklist），打 info 会刷屏。
        # 给操作者看的那条汇总由 enumerate_worklist 在枚举结束时打一次。
        logger.debug(
            f"同价 SKU 折叠：{len(items)} 行 → {len(out)} 行（合并掉 {dropped} 行同 SPU 同价规格）"
        )
    return out


def load_worklist() -> list:
    """读工作清单，并在返回前做【同 SPU 同价 SKU 折叠】（见 collapse_same_price_skus）。

    落盘的 worklist.json 始终是平台全量快照（一个 SKU 一条），折叠只发生在读取侧——
    判重水位、UI 待采统计、写表全都走这个入口，口径天然一致。
    """
    if not WORKLIST.exists():
        return []
    try:
        data = json.loads(WORKLIST.read_text(encoding="utf-8"))
        return collapse_same_price_skus(data) if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"读取工作清单失败：{e}")
        return []


def _store_key(it: dict) -> str:
    """某条商品的「店铺+区域」标识：mallid@region。用于筛选/分组的稳定键。

    【为什么带区域】同一账号在不同区域（全球/美国/欧区）看到的是不同的数据范围，而
    mallid 在切区域后【完全不变】（2026-08-07 实测，见 app/temu_region.py）。只用 mallid
    当键，全球和美国的商品会被归成同一个店、落进同一张 Sheet。

    老清单没有 region 字段 → 退回纯 mallid，与旧行为一致（不带 @ 后缀），故历史偏好里
    存的 store 值仍能匹配上，不会因为升级就整批失配。
    """
    base = str(it.get("mallid") or it.get("store") or "").strip()
    region = str(it.get("region") or "").strip()
    return f"{base}@{region}" if base and region else base


def summarize_stores(worklist: list) -> list:
    """从 worklist 汇总去重店铺列表：[{key, label, mallid, region, count}]（按商品数倒序）。

    label 带区域后缀（如 `Pawly · 美国`）：同一账号跨区域时店名完全相同，不带区域的话
    UI 下拉会并排两个一模一样的选项，用户没法选对。
    """
    agg: dict = {}
    for it in worklist:
        key = _store_key(it)
        if not key:
            continue
        name = str(it.get("store") or it.get("mallid") or key)
        region_label = str(it.get("region_label") or "").strip()
        e = agg.setdefault(
            key,
            {
                "key": key,
                "label": f"{name} · {region_label}" if region_label else name,
                "mallid": it.get("mallid") or "",
                "region": it.get("region") or "",
                "region_label": region_label,
                "count": 0,
            },
        )
        e["count"] += 1
    return sorted(agg.values(), key=lambda e: e["count"], reverse=True)


def get_worklist_status(
    excel: Optional[str] = None,
    sheet: Optional[str] = None,
    store: Optional[str] = None,
    doc_mode: Optional[str] = None,
) -> dict:
    """UI 展示用：返回清单总量 / 已入库 / 待采 SPU 与 SKU 数等状态。

    不触发任何采集，纯读 worklist.json + 已入库 SPU 集合，供 UI 渲染。
    - excel/sheet/store 均缺省回填「上次选择」偏好，再兜底出厂默认。
    - doc_mode 显式给 local/cloud 时钉死走哪条路；不给（None）则沿用上次选的模式，
      再退 auto。本地模式下上次的路径已失效时回传空 excel，由用户从 workbooks 候选里
      自己选——写错表要人工回滚，不由代码猜（见 _pick_local_excel）。
    - 传了 store → items 过滤到该店；有有效 sheet → done 按该工作簿/Sheet 判重，
      否则 done=None（跨店对单 sheet 判重无意义）。
    - 回传当前生效的 excel/sheet/store/doc_mode 供 UI 回显选中态。
    - 云端分支：excel 是协作文档链接、或无显式本地目标但 prefs/config 配了云端时，
      Sheet 列表与判重都查协作文档（KdocsSheet）；返回带 cloud=True，读失败不抛错、
      带 cloud_error 由 UI 红条展示（对齐订单页的展示契约）。云端模式不返回
      excel_locked（本地锁预检不适用）。
    Excel 不可读（缺失/被占用/无此 Sheet）时 done 视为空集，不抛错。
    """
    prefs = load_prefs()
    cfg = load_collect_config()
    # 目标解析（与 run_batch 同一优先级）：doc_mode 显式钉死 > 显式链接 → 云端；
    # 无显式目标时 prefs.cloud_url > config.cloud_file_id → 云端；否则本地。
    explicit = (excel or "").strip()
    mode = normalize_doc_mode(
        doc_mode if doc_mode is not None else prefs.get("doc_mode")
    )
    cloud = None
    cloud_missing = ""
    if mode == DOC_MODE_LOCAL:
        # 输入框里残留的链接不能当本地路径用（拿它当文件名必然失败）
        excel = (explicit if explicit and not is_cloud_link(explicit) else "") \
            or _pick_local_excel(prefs)
    elif mode == DOC_MODE_CLOUD:
        url = explicit if is_cloud_link(explicit) else (
            str(prefs.get("cloud_url") or "").strip()
            or str(cfg.get("cloud_file_id") or "").strip()
        )
        if url:
            cloud = cloud_backend(cfg, cloud_url=url)
            excel = url
        else:
            # 显式选线上但没有可用目标：走同一条 cloud_error 红条通道提示补链接
            excel = ""
            cloud_missing = ("未指定协作文档：请在上方粘贴 kdocs 链接，"
                             "或在 config.toml 配 cloud_file_id")
    elif is_cloud_link(explicit):
        cloud = cloud_backend(cfg, cloud_url=explicit)
        excel = explicit
    elif not explicit:
        url = str(prefs.get("cloud_url") or "").strip() or str(
            cfg.get("cloud_file_id") or "").strip()
        if url:
            cloud = cloud_backend(cfg, cloud_url=url)
            excel = url  # 回显目标（链接或 file_id）
        else:
            excel = prefs.get("excel") or DEFAULT_EXCEL
    else:
        excel = explicit
    if sheet is None:
        sheet = prefs.get("sheet") or ""
    if store is None:
        store = prefs.get("store") or ""

    worklist = load_worklist()
    stores = summarize_stores(worklist)
    workbooks = list_workbooks()
    cloud_err = cloud_missing
    if cloud_missing:
        # 线上模式但无目标：没有可读的 Sheet 列表，判重一律空集，等用户补链接
        sheets, sheet_valid, done = [], False, set()
    elif cloud is not None:
        try:
            sheets = cloud.sheet_names()
        except Exception as e:
            logger.warning(f"读协作文档工作表列表失败：{e}")
            sheets, cloud_err = [], str(e)
        sheet_valid = bool(sheet) and sheet in sheets
        done: set = set()
        if sheet_valid and not cloud_err:
            try:  # 云端判重：按真实表头定位 SPU 列（勿硬编码 D）
                header, header_row = cloud.read_header(sheet)
                fields = WpsExcelTool._resolve_fields_from_header(header)
                if fields.get("spu"):
                    done = existing_keys(
                        excel, sheet, cloud=cloud, fields=fields, header_row=header_row
                    )
                else:
                    # 选到没有 SPU 列的表（最常见是误选了订单登记表那份文档）→ 现在
                    # 就把原因摆到红条上。原先这里静默跳过判重，页面显示「已入库 –」
                    # 照样能点开始，跑到 run_batch 才中止，用户白等一轮枚举。
                    # 深扫一次（口径同 resolve_sheet_schema_cloud）：WINTAK欧洲 真表头
                    # 在第 4 行，只看前 3 行会把它误判成「没有 SPU 列」。
                    from app.collect.pipeline import (
                        _HEADER_DEEP_SCAN, explain_missing_spu,
                    )

                    deep, deep_row = cloud.read_header(sheet, _HEADER_DEEP_SCAN)
                    deep_fields = WpsExcelTool._resolve_fields_from_header(deep)
                    if deep_fields.get("spu"):
                        done = existing_keys(
                            excel, sheet, cloud=cloud, fields=deep_fields,
                            header_row=deep_row,
                        )
                    else:
                        cloud_err = explain_missing_spu(sheet, deep or header)
            except Exception as e:
                logger.warning(f"读协作文档判重水位失败：{e}")
                cloud_err = str(e)
    else:
        sheets = WpsExcelTool.list_sheets(excel)
        # 选中的 sheet 若不在该工作簿里（换了工作簿导致失配）→ 视为未选
        sheet_valid = bool(sheet) and sheet in sheets
        done = existing_keys(excel, sheet) if sheet_valid else set()
    # 选中的 store 若不在清单里（换清单）→ 视为未选（全部）
    store_valid = bool(store) and any(s["key"] == store for s in stores)

    # 判重按 SPU|规格 组合键 + 历史 SPU 整体跳过（见 done_flags）：同一 SPU 的多个规格
    # 各占一行，纯 SPU 比对会把后面的规格全算成已入库。
    scoped = [
        it for it in worklist
        if not (store_valid and _store_key(it) != store)
    ]
    flags = done_flags(scoped, done) if sheet_valid else [False] * len(scoped)

    items = []
    todo_spus: set[str] = set()
    todo_sku = 0
    for it, is_done in zip(scoped, flags):
        spu = str(it.get("spu", "")).strip()
        is_done = bool(spu) and is_done
        if spu and not is_done:
            todo_spus.add(spu)
            todo_sku += 1
        items.append(
            {
                "spu": spu,
                "name": it.get("name", ""),
                "site": it.get("site", ""),
                "store": it.get("store", ""),
                "region": it.get("region", ""),
                "region_label": it.get("region_label", ""),
                "mallid": it.get("mallid", ""),
                "category": it.get("category", ""),
                "price": it.get("price", ""),
                "image": it.get("image", ""),
                # 规格：一个 SKU 一行后，同一 SPU 在列表里会出现多次，不给规格前端没法区分
                "sku_id": it.get("sku_id", ""),
                "sku_spec": it.get("sku_spec", ""),
                "done": is_done,
            }
        )
    result = {
        "total": len(items),
        "done": len([i for i in items if i["done"]]) if sheet_valid else None,
        # 清单是一行一个 SKU；待采展示同时给出商品（SPU 去重）与实际可采行（SKU）口径。
        # todo 保持旧接口语义，避免 CLI/旧前端断裂；新 UI 明确读取 todo_spu / todo_sku。
        "todo": todo_sku,
        "todo_spu": len(todo_spus),
        "todo_sku": todo_sku,
        "excel": excel,
        "sheet": sheet if sheet_valid else "",
        "store": store if store_valid else "",
        "workbooks": workbooks,
        "sheets": sheets,
        "stores": stores,
        # 采集范围：可选覆盖页签清单 + 上次选择（空串=跟随页面当前，供前端下拉渲染与回显）
        "status_tabs": STATUS_TABS,
        "status": prefs.get("status") or "",
        "items": items,
        # 新行落点：模式 + 指定行 + 可选项清单，供 UI 渲染下拉与回显上次选择
        "append_mode": normalize_append_mode(
            prefs.get("append_mode") or cfg.get("append_mode")
        ),
        "append_row": prefs.get("append_row") or cfg.get("append_row") or "",
        "append_modes": [{"key": k, "label": v} for k, v in APPEND_MODE_LABELS.items()],
        # 实际生效的文档模式，供 UI 回显开关（本地模式恒为 local，即便 config 配了云端）
        "doc_mode": (DOC_MODE_CLOUD if (cloud is not None or cloud_missing)
                     else DOC_MODE_LOCAL),
    }
    if cloud is not None or cloud_missing:
        result["cloud"] = True
        result["cloud_error"] = cloud_err
    else:
        result["excel_locked"] = excel_write_locked(excel)
    return result


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

        # 【这条路径仍按纯 SPU 确认，是刻意的】agent 是照提示词自己写表的，提示词没教它写
        # 货号列（也没法教稳——它连列号都按老表硬编码在提示词里），写出来的行货号是空的。
        # 若这里改用 SPU|skuId 组合键，就会永远确认不到、重试到耗尽。
        # 代价是多 SKU 商品在这条路径上可能把「本行没写进去」误判成已入库（同 SPU 的前一行
        # 已在列里）。属已知局限：agent 路径只是兜底，主路径是基础/管道模式，两者都走
        # write_product_row(_cloud)、会正确写货号列。
        if spu in {k.split("|", 1)[0] for k in existing_keys(excel, sheet)}:
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
    cloud=None,
    append_mode: str = DEFAULT_APPEND_MODE,
    append_row: Optional[int] = None,
) -> "CollectOutcome":
    """确定性管道采集单商品（阶段二），失败退回 agent 兜底。

    schema：批次开始时解析好的目标 Sheet 写入结构，全批复用（见 pipeline.SheetSchema）；
    未传则 write_product_row 内部就地解析一次。cloud 非空时写协作文档
    （write_product_row_cloud，主图走在线 URL），判重确认也改读云端。
    返回 CollectOutcome（携带落库结果与来源），供上层生成结构化进度事件。
    """
    from app.collect.pipeline import (
        _cloud_first_row,
        archive_unmatched_image,
        collect_one_product,
        judge_price,
        read_detail_price,
        write_product_row,
        write_product_row_cloud,
    )

    spu = str(item.get("spu", ""))
    browser_tool = agent.available_tools.get_tool("browser_use")
    excel_tool = WpsExcelTool()
    img_path = os.path.join(str(config.output_dir("image")), f"{spu}.jpeg")

    # 云端落点与写后确认共用同一个行号：写入会让数据区增长，确认时重算会读到空白区。
    # 逐商品写，故每商品各算一次（写前）。
    cloud_first_row = {"v": None}

    async def _write_row(res) -> tuple[bool, str]:
        """按当前后端（云端/本地）写一行，口径与本地一致。"""
        if cloud is not None:
            # 逐商品写：row_up 与 row_down 等价（每次插一行），故 up_count=0，见 collect_one_base
            at_top, at_row, _label = resolve_append_target(
                append_mode, append_row, schema.header_row
            )
            cloud_first_row["v"] = await asyncio.to_thread(
                _cloud_first_row, cloud, sheet, schema, at_top, at_row, 0
            )
            return await write_product_row_cloud(
                cloud, sheet, item, res, schema, insert_at_top=at_top, at_row=at_row
            )
        return await write_product_row(
            excel_tool, excel, sheet, item, res, img_path, schema=schema
        )

    def _written_confirmed() -> bool:
        """写后判重确认：云端读协作文档的 SPU 列，本地读 xlsx。

        云端优化：只读新行那一格（落点由 _write_row 写前算好），不读整列。
        """
        if cloud is not None:
            return spu in cloud.read_new_rows_column(
                sheet, schema.fields["spu"], cloud_first_row["v"], 1
            )
        # 本地按 SPU|skuId 组合键确认：一个 SKU 一行后，同 SPU 的前一行早就在 SPU 列里，
        # 只比 SPU 会把「这一行没写进去」误判成成功、静默漏掉该规格。
        return worklist_key(item) in existing_keys(excel, sheet)

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
        wrote, msg = await _write_row(res)
        try:
            await browser_tool.execute(action="close_tabs", text="1688")
        except Exception:
            pass
        if wrote and _written_confirmed():
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
            wrote, msg = await _write_row(res)
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
    cloud=None,
    append_mode: str = DEFAULT_APPEND_MODE,
    append_row: Optional[int] = None,
) -> "CollectOutcome":
    """基础采集单商品：只写 Temu 基础行（站点/类目/SPU/销售价/主图），采购价(J)/重量(K)
    留空待人工填。【不跑 1688 图搜/判同款/读价】，故不用浏览器/CDP/LLM，纯下图 + 写 Excel。

    复用 pipeline.write_product_row 的留空写入逻辑（res.purchase_price/weight_g 为 None →
    对应列写空串），与「无同款留空行」同一套机制，只是这里【无条件】留空。

    cloud 非空时写协作文档（write_product_row_cloud）：主图走 item["image"] 的在线
    URL 嵌入，跳过本地下载；写后判重确认也改读云端。
    """
    from app.collect.pipeline import (
        CollectResult,
        _cloud_first_row,
        _download_main_image,
        resolve_sheet_schema_cloud,
        write_product_row,
        write_product_row_cloud,
    )

    spu = str(item.get("spu", ""))
    img_path = os.path.join(str(config.output_dir("image")), f"{spu}.jpeg")

    if cloud is not None and schema is None:
        # 公式要学落点附近那一段，故先按表头行号把落点算出来再解析（口径同 run_batch）。
        _hdr, _hdr_row = await asyncio.to_thread(cloud.read_header, sheet)
        _at_top, _at_row, _ = resolve_append_target(append_mode, append_row, _hdr_row)
        schema = await resolve_sheet_schema_cloud(
            cloud, sheet, landing_row=_at_row
        )

    # 主图：能下就下（供 Excel 产品图片列），下不到只告警、仍写基础行。
    # 云端模式走在线 URL 嵌入，不需要本地图，跳过下载。
    if cloud is None and not os.path.exists(img_path) and item.get("image"):
        try:
            await asyncio.to_thread(_download_main_image, item["image"], img_path)
        except Exception as e:
            logger.warning(f"SPU={spu} 主图下载失败（忽略，仍写基础行）：{e}")

    res = CollectResult(spu=spu, ok=True, note="采购价/重量待人工填")
    if cloud is not None:
        # 逐商品写时 row_up 与 row_down 等价：每次只插一行，插在 R 就把原 R 行推到 R+1，
        # 下一个商品又插在 R……最终顺序是「后采的在上面」。要「先采的在上面」请用整批写
        # （基础模式云端默认走 write_product_rows_cloud，见 run_batch）。故这里 up_count=0。
        at_top, at_row, _label = resolve_append_target(
            append_mode, append_row, schema.header_row
        )
        # 落点要在写入前算好并复用给确认：写入会让数据区增长，事后重算会读到本批之后的空白区
        first_row = await asyncio.to_thread(
            _cloud_first_row, cloud, sheet, schema, at_top, at_row, 0
        )
        wrote, msg = await write_product_row_cloud(
            cloud, sheet, item, res, schema, insert_at_top=at_top, at_row=at_row
        )
        # 写后确认优化：只读新行那一格（落点已知），不读整列（500 行的表就是
        # 500 格冗余传输，payload 浪费两个数量级）。次数一样、只省 payload。
        confirmed = wrote and (
            spu in await asyncio.to_thread(
                cloud.read_new_rows_column, sheet, schema.fields["spu"],
                first_row, 1
            )
        )
    else:
        wrote, msg = await write_product_row(
            WpsExcelTool(), excel, sheet, item, res, img_path, schema=schema
        )
        # 组合键确认，理由同 collect_one_pipeline._written_confirmed
        confirmed = wrote and worklist_key(item) in existing_keys(excel, sheet)
    if confirmed:
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
    cloud_url: Optional[str] = None,
    doc_mode: str = "",
    append_mode: str = "",
    append_row: Optional[int] = None,
) -> dict:
    """跑一批未入库商品的采集，进度经 on_progress 抛出。返回汇总 {ok, fail, batch}。

    - excel/sheet 缺省回填「上次选择」偏好、再兜底出厂默认；store 非空则只采该店商品。
      本次组合成功启动后 save_prefs 记住，供下次缺省回填。
    - append_mode 决定新行落点，四选一：bottom（默认，追加到数据区末尾）、top（插到表头
      正下方）、row_down / row_up（配 append_row，从指定行向下 / 向上插）。优先级同其它
      偏好：显式传入 > prefs > [collect] 配置 > bottom。指定行非法（缺失/落在表头及以上）
      时退回 bottom 并告警——落点是辅助选项，不该让整批停摆（见 resolve_append_target）。
      云端此前只能插顶端、与本地 xlsx 的追加行为相反，现已统一由这个开关决定。
    - doc_mode 显式给 local/cloud 时钉死走本地表还是协作文档（kdocs 有配额，用满要能
      立刻切回本地）；不给则按 auto 的历史优先级判（见 resolve_cloud）。本地模式下
      挑不出工作簿则中止并提示回 UI 选，不兜底到 DEFAULT_EXCEL、也不猜。
    - 云端目标（resolve_cloud）：excel 是协作文档链接 → 云端；显式给的本地路径 →
      本地（压制 prefs/config 的云端目标）；未显式指定 → cloud_url 参数 >
      prefs.cloud_url > [collect].cloud_file_id 有值 → 云端；再否则本地 xlsx。
      云端模式下判重/写入打在协作文档上，跳过本地锁预检与主图本地下载。
    - base_only=True（默认）：仅采 Temu 基础信息、采购价/重量留空待人工填，【不跑 1688】——
      不开 agent、不连 CDP、不用 LLM，最省。此时 use_pipeline 被忽略。
    - base_only=False：走 1688 自动采价。agent 为空则内部创建并在结束时清理；调用方传入
      则复用、由调用方负责清理（UI 场景可复用长驻 agent，避免每批重开浏览器/MCP）。
      use_pipeline=True 走确定性管道（推荐），失败自动退回 agent 兜底。
    - Excel 被占用 / 清单为空 / CDP 不可用（仅 1688 模式）→ 抛结构化事件并提前返回，不空跑。
    """
    # 失败事件切面：aborted / product_done fail 自动上报公网 MySQL（best-effort，坏了不影响采集）。
    on_progress = attach(on_progress, "collect")
    prefs = load_prefs()
    doc_mode = normalize_doc_mode(doc_mode)
    # 落点：显式 > prefs > config > 默认（bottom）。与 doc_mode 同一套「四处来源」取向。
    _cfg_for_append = load_collect_config()
    append_mode = normalize_append_mode(
        append_mode
        or prefs.get("append_mode")
        or _cfg_for_append.get("append_mode")
    )
    if append_row is None:
        append_row = prefs.get("append_row") or _cfg_for_append.get("append_row")
    # 云端目标解析必须在 excel 兜底成默认本地路径【之前】做：显式给的本地路径要能
    # 压制 prefs/config 里的云端目标（resolve_cloud），否则用户改选本地后这一批仍
    # 会被写进旧云端文档。
    cloud = resolve_cloud(excel, cloud_url, doc_mode)
    # 显式选线上却没有可用文档：中止而不是静默退回本地表（会写错文档）
    if cloud is None and doc_mode == DOC_MODE_CLOUD:
        reason = ("已选「线上文档」但未指定协作文档：请粘贴 kdocs 链接，"
                  "或在 config.toml 的 [collect] 配 cloud_file_id")
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        logger.error(reason)
        return {"ok": 0, "fail": 0, "batch": 0}
    # 本地模式下输入框里残留的链接不能当本地路径用（拿它当文件名必然失败）
    if doc_mode == DOC_MODE_LOCAL and excel and is_cloud_link(excel):
        excel = ""
    if doc_mode == DOC_MODE_LOCAL:
        # 不兜底到 DEFAULT_EXCEL：那是写死的备份文件路径，多半不是用户此刻要写的表，
        # 静默落进去比报错更糟。挑不出就让用户回 UI 从候选里选一个——不猜。
        excel = excel or _pick_local_excel(prefs)
        if not excel:
            reason = ("未选择本地工作簿：请在页面的「目标工作簿」里从候选中选一个"
                      "（或填路径）")
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            logger.error(reason)
            return {"ok": 0, "fail": 0, "batch": 0}
    excel = excel or prefs.get("excel") or DEFAULT_EXCEL
    sheet = sheet or prefs.get("sheet") or DEFAULT_SHEET
    # store 的两种来源语义不同，护栏也不同（见下方 store 失配处理）：
    # None = 调用方没指定、由 prefs 回填；空串 = 调用方显式要「全部店铺」，不许回填。
    store_from_prefs = store is None
    if store is None:
        store = prefs.get("store") or ""

    # 纯 agent 兜底路径（collect_one，非管道）自己 inspect 本地表，云端模式下不适用——
    # 它本就只是调试兜底，直接中止并提示，别跑到写本地默认表。
    if cloud is not None and not base_only and not use_pipeline:
        reason = "纯 agent 模式（--no-pipeline）不支持写入协作文档，请改用确定性管道或基础采集。"
        await _emit(on_progress, {"type": "aborted", "reason": reason})
        logger.error(reason)
        return {"ok": 0, "fail": 0, "batch": 0}

    worklist = load_worklist()
    # store 失配护栏（口径对齐 get_worklist_status 的 store_valid）：prefs 里记的店可能
    # 已不在当前清单里（重新枚举时只开了另一家店的标签），此时【回填来的】store 降级成
    # 「全部」并告警，不是报错——UI 那边失配就回显空 store、下拉显示「全部店铺」，若这里
    # 仍按失效 mallid 硬过滤，就会出现「页面写着全部、后端却过滤到 0 条」的静默不一致
    # （2026-08-06 实机：prefs 存着上一家的 mallid，本次枚举换了店，直接报「清单里没有商品」）。
    # 显式传入的 store 不降级：那是调用方明确的意图，失配要如实报错，别悄悄改成全量采。
    if store and not any(s["key"] == store for s in summarize_stores(worklist)):
        if store_from_prefs:
            logger.warning(
                f"上次选的店铺（{store}）不在当前清单里（清单可能已重新枚举），"
                f"本批按「全部店铺」采集。"
            )
            store = ""
        else:
            reason = (f"指定的店铺（{store}）不在当前清单里。"
                      f"请重新枚举（--refresh）或改选清单里已有的店铺。")
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            logger.error(reason)
            return {"ok": 0, "fail": 0, "batch": 0}
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

    # 锁预检只针对本地 xlsx；协作文档是多人实时协作的，不存在本地占用锁
    if cloud is None and excel_write_locked(excel):
        msg = (
            f"Excel 正被占用（疑似 WPS/Excel 打开中），无法写入：{excel}。"
            "请先在 WPS/Excel 里关闭该文件，再重跑。"
        )
        await _emit(on_progress, {"type": "aborted", "reason": msg})
        logger.error("❌ " + msg)
        return {"ok": 0, "fail": 0, "batch": 0}

    # 记住本次选择（工作簿/Sheet/店铺），供下次 UI/CLI 缺省回填；链接会被拆到 cloud_url。
    # 云端目标生效时记的是云端目标本身，不能记兜底出的本地 excel——那会把 prefs 里的
    # cloud_url 冲掉，下一批不带参数就静默退回本地默认表。
    # status 是枚举页签、非本批参数，沿用已存值，避免被空串覆盖丢掉。
    save_prefs(cloud.file_id if cloud is not None else excel,
               sheet, store, prefs.get("status") or "", doc_mode,
               append_mode, append_row)

    # 批次开始就解析一次目标 Sheet 的写入结构（列映射/公式/常量），全批复用——各 Sheet
    # 列序不同，必须按真实表头写；这份结构一批恒定，不必逐商品重 inspect 大工作簿。
    # 结构认不出（如缺 SPU 列）→ 直接中止本批，避免逐个商品去撞同一个错误。
    # 基础模式与管道模式都要按真实表头写，故都先解析（纯 agent 兜底路径自己 inspect，跳过）。
    # 云端模式提前到这里解析：判重也要用它的表头行号与 SPU 列。
    pipe_schema = None
    if cloud is not None:
        from app.collect.pipeline import resolve_sheet_schema_cloud
        from app.orders.kdocs_sheet import KdocsSheetError

        try:
            # 落点要先算出来再解析 schema：公式必须学【落点附近】那一段，而不是表头下前
            # 10 行——同一张表顶部与底部常常不是一套公式（见 pipeline._sample_near_landing）。
            # resolve_append_target 只需要表头行号，故先单独读一次表头（3 行小请求）。
            _hdr, _hdr_row = await asyncio.to_thread(cloud.read_header, sheet)
            _at_top, _at_row, _ = resolve_append_target(
                append_mode, append_row, _hdr_row
            )
            pipe_schema = await resolve_sheet_schema_cloud(
                cloud, sheet, landing_row=_at_row
            )
            if pipe_schema.ok:
                done = existing_keys(
                    excel, sheet, cloud=cloud, fields=pipe_schema.fields,
                    header_row=pipe_schema.header_row,
                )
        except KdocsSheetError as e:
            reason = f"读取协作文档失败（{e}），已中止本批。"
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            logger.error("❌ " + reason)
            return {"ok": 0, "fail": 0, "batch": 0}
        if not pipe_schema.ok:
            await _emit(on_progress, {"type": "aborted", "reason": pipe_schema.error})
            logger.error(f"❌ 目标表结构不可写，中止本批：{pipe_schema.error}")
            return {"ok": 0, "fail": 0, "batch": 0}
        logger.info(
            f"目标=协作文档；目标表结构已解析：字段列={pipe_schema.fields} "
            f"公式列={list(pipe_schema.formula_columns)} 模板输入={pipe_schema.constant_columns}"
        )
    else:
        done = existing_keys(excel, sheet)
    todo = [
        it for it, is_done in zip(worklist, done_flags(worklist, done))
        if str(it.get("spu", "")).strip() and not is_done
    ]
    # 同批撞键护栏：本批里若有多行判重键相同（SKU 展开异常/清单被手工改过/接口给了重复
    # skuId），它们会各写一行进表、却只留下一个判重键——下次重跑时其余份全被判成已入库，
    # 人工很难看出表里那几行是重复的。只告警不拦：数据仍写得进去，但必须让操作者看见
    # （对齐 orders 侧 _stage_order 区分「本批内撞键」与「表里已有」的取向）。
    seen_keys: set = set()
    collided: set = set()
    for it in todo:
        k = worklist_key(it)
        if k in seen_keys:
            collided.add(k)
        seen_keys.add(k)
    if collided:
        logger.warning(
            f"本批有 {len(collided)} 个判重键重复（如 {list(collided)[:3]}），"
            f"这些行会重复写入且下批会被判为已入库，请检查清单"
        )
    batch = min(limit, len(todo))
    mode_label = "基础(价/重人工填)" if base_only else ("管道" if use_pipeline else "agent")
    target_label = (f"协作文档（{cloud.file_id}）" if cloud is not None
                    else f"工作簿={excel}")
    # 落点解析放在 schema 解析之后：row_down/row_up 的合法性要对着真实表头行号判。
    # 返回的 at_row 为 None 表示「由写入层自己算」（bottom 读末行、top 取表头下一行）。
    at_top, at_row, append_label = resolve_append_target(
        append_mode, append_row,
        pipe_schema.header_row if pipe_schema is not None else 1,
    )
    up_count = batch if append_mode == APPEND_ROW_UP and at_row is not None else 0
    logger.info(
        f"=== 采集批次：模式={mode_label} {target_label} Sheet={sheet} 店铺={store or '全部'} "
        f"落点={append_label}；"
        # 计量单位是【行】不是商品：清单一个 SKU 一行，一个多规格商品占多行，
        # 说"个"会让人以为采少了（128 个商品会显示成 388 行）。
        f"清单 {len(worklist)} 行，已入库 {len(done)}，待采 {len(todo)}，本批 {batch} 行 ==="
    )
    await _emit(on_progress, {
        "type": "batch_start", "total": len(worklist),
        "done_existing": len(done), "todo": len(todo), "batch": batch,
        # 本批落点：本地表还是协作文档，供 UI/CLI 明示（切错模式要能一眼看出来）
        "cloud": cloud is not None,
        "doc_mode": DOC_MODE_CLOUD if cloud is not None else DOC_MODE_LOCAL,
        "target": target_label,
        "append_mode": append_mode,
    })

    if cloud is None and (base_only or use_pipeline):
        from app.collect.pipeline import resolve_sheet_schema

        pipe_schema = await resolve_sheet_schema(WpsExcelTool(), excel, sheet)
        if not pipe_schema.ok:
            await _emit(on_progress, {"type": "aborted", "reason": pipe_schema.error})
            logger.error(f"❌ 目标表结构不可写，中止本批：{pipe_schema.error}")
            return {"ok": 0, "fail": 0, "batch": 0}
        logger.info(
            f"目标表结构已解析：字段列={pipe_schema.fields} "
            f"公式列={list(pipe_schema.formula_columns)} 模板输入={pipe_schema.constant_columns}"
        )

    ok = fail = 0

    # 基础模式：只写 Temu 基础行、价/重留空待人工填。不开 agent、不连 CDP、不用 LLM。
    # 云端目标额外走【整批一次写】：基础模式的数据全来自 worklist（不需要浏览器/LLM 逐个采），
    # 故可攒批。逐商品写一行要 5~6 次 kdocs 调用，整批 20 行只要 6 次（实测 124 → 10）。
    # kdocs 有配额（429001 限频等 20s、429002 熔断），这个差距决定整批能否一次跑完。
    # 本地 xlsx 不走这条：没有配额压力，且逐行写的失败隔离更细，保持原样不动。
    if base_only and cloud is not None:
        from app.collect.pipeline import write_product_rows_cloud

        items = todo[:limit]
        for i, item in enumerate(items, 1):
            await _emit(on_progress, {
                "type": "product_start", "index": i, "total": batch,
                "spu": item.get("spu"), "name": item.get("name", ""),
            })
        logger.info(f"--- 整批写入协作文档：{len(items)} 行（基础模式） ---")
        wrote, msg, written_spus = await write_product_rows_cloud(
            cloud, sheet, items, pipe_schema,
            insert_at_top=at_top, at_row=at_row, up_count=up_count,
        )
        if wrote:
            ok = len(written_spus)
            logger.info(f"⬜ {msg}（采购价/重量留空待人工填）")
            for i, item in enumerate(items, 1):
                await _emit(on_progress, CollectOutcome(
                    spu=str(item.get("spu", "")), ok=True, status="base",
                    via="base", note="采购价/重量待人工填",
                ).to_event(i, batch))
        else:
            # 文本批写失败时逐个报失败；云接口没有事务，提示会要求先查 SPU 再重跑。
            fail = len(items)
            logger.error(f"❌ 整批未入库：{msg}")
            for i, item in enumerate(items, 1):
                await _emit(on_progress, CollectOutcome(
                    spu=str(item.get("spu", "")), ok=False, status="fail",
                    via="base", note=msg,
                ).to_event(i, batch))
        logger.info(f"=== 本批完成：成功 {ok}，失败 {fail} ===")
        await _emit(on_progress, {"type": "batch_done", "ok": ok, "fail": fail})
        return {"ok": ok, "fail": fail, "batch": batch}

    if base_only:
        for i, item in enumerate(todo[:limit], 1):
            spu = item.get("spu")
            name = item.get("name", "")
            logger.info(f"--- [{i}/{batch}] 基础采集 SPU={spu}（{name[:20]}）---")
            await _emit(on_progress, {
                "type": "product_start", "index": i, "total": batch,
                "spu": spu, "name": name,
            })
            outcome = await collect_one_base(
                item, excel, sheet, schema=pipe_schema, cloud=cloud,
                append_mode=append_mode, append_row=append_row,
            )
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
                    agent, item, excel, sheet, schema=pipe_schema, cloud=cloud,
                    append_mode=append_mode, append_row=append_row,
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
