# -*- coding: utf-8 -*-
"""订单登记管线的确定性步骤：页面翻页勾选 → 触发官方导出 → 解析 xlsx → 抓主图。

为什么走「导出订单」而不是抓接口或逐字段读 DOM：
  - 订单列表请求跑在 Web Worker（`blob:https://agentseller.temu.com/...`）里，还叠了
    商家助手扩展的 `xhr-interceptor.js`。页面级 Playwright 和页面级 CDP `Network.*`
    都拿不到响应体（实测枚举了 98 条请求 / 71 种 URL，订单接口确实不在其中）；自造
    fetch 直连又过不了 anti-content 动态签名。
  - 逐字段读 DOM 要跟一堆构建期 hash 类名（`_3AHRHYjy` 这种）赛跑，改版就全崩。
  官方导出是唯一稳的路：一次点击拿全量、字段名固定、`--` 表示空值。

主图是唯一必须从 DOM 拿的字段（导出文件里没有图列），且图片是 IntersectionObserver
懒加载——表格在内部滚动容器里，`window.scrollTo` 完全无效，必须逐行 scrollIntoView。

全流程零大模型：确定性脚本 + 官方导出，没有任何 function-calling。别把它 agent 化。
"""
import asyncio
import inspect
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.collect.pipeline import _download_main_image
from app.logger import logger

# ---- 已实测确认的选择器（2026-07-27，agentseller.temu.com）------------------
# beast-core 是 Temu 后台的组件库，类名带 `_123` 版本后缀（如 PGT_totalText_123）。
# 一律用 [class*="前缀"] 前缀匹配，避免后缀跳版本就全崩。
_PAGINATION = 'ul[data-testid="beast-core-pagination"]'
_PAGE_NEXT = 'li[data-testid="beast-core-pagination-next"]'
_TOTAL_TEXT = '[class*="PGT_totalText"]'
# 表头全选框：真实 <input> 是 0×0 + opacity:0 的隐形元素（beast-core 的惯用做法，靠外层
# label 显示），点它必然 30s 超时报「element is not visible」。所以【状态读 input、点击点
# label】：label 带稳定的 data-testid，不像 CBX_*_123 那样跟版本号跳。
_HEAD_CHECKBOX = 'table thead input[type="checkbox"]'
_HEAD_CHECKBOX_CLICK = 'table thead label[data-testid="beast-core-checkbox"]'

# 商家助手插件（浏览器扩展注入，不是 Temu 官方 DOM）在页面右下角挂一个悬浮球容器
# `#temu-ass-core-ui-dashboder-root`，里面是一张 base64 图。它 position:fixed 压在分页条
# 和底部操作栏上方，Playwright 点击前的 hit-target 检查命中的是这张图而不是目标元素，
# 于是一路 retry 到 30s 超时，报
#   <img src="data:image/png;base64,..."> from <div id="temu-ass-core-ui-dashboder-root">
#   subtree intercepts pointer events
# 2026-07-29 实机在「下一页」上复现。注意此时选择器是好的（元素已解析、visible/enabled、
# 也滚进了视口），纯粹是被遮挡，别误判成页面改版去改选择器。
# 浮层按账号灰度下发，所以早先几次跑没遇到——同一份代码换个店铺或换一天就复现。
# 处理办法：点击前把这棵子树设成 pointer-events:none，只让它不吃指针事件，不隐藏、不删
# 节点，插件自身逻辑照跑。不做还原：本管线全程在同一个标签页上连续点击（勾选 → 翻页 →
# 导出），点完还原等于把坑重新埋回下一次点击；用户刷新页面即恢复原状。
_POINTER_OVERLAYS = '[id*="temu-ass"]'

# pointer-events 虽然是继承属性，但子节点可能自己声明了 auto 把继承盖掉（报错里真正拦住
# 点击的就是子节点 img），所以容器和后代一起设，别只设根节点。
_DISABLE_OVERLAY_JS = r"""
(sel) => {
  let n = 0;
  for (const root of document.querySelectorAll(sel)) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (el.style.getPropertyValue('pointer-events') === 'none') continue;
      el.style.setProperty('pointer-events', 'none', 'important');
      n += 1;
    }
  }
  return n;
}
"""

# 「共有 247 条」/「已选订单：40」
# 订单列表「创建时间新→旧」对应的 sortType 值（2026-07-27 实测，见 docs §2 的 URL）。
# 增量早停的全部正确性都压在这个排序上，故它是 check_list_sort_url 的唯一合格值。
_SORT_DESC = "1"

_RE_TOTAL = re.compile(r"共有\s*([\d,]+)\s*条")
_RE_SELECTED = re.compile(r"已选订单[:：]\s*([\d,]+)")

# 导出字段设置弹窗的分组名。序号固定为
# 0订单号(disabled,强制) 1订单信息 2子订单信息 3商品信息 4收货信息 5运单信息 6操作节点
# 7过滤已取消的商品，默认全勾。**收货信息必须取消**：登记表用不到，而它带的是买家 PII
# （姓名/电话/邮箱/身份证号/税号/地址），不该落到本地表。该设置会被后台记住，所以每次
# 都要先读当前状态再决定点不点（幂等）。
_EXPORT_GROUP_EXCLUDE = "收货信息"
# 弹窗里的复选框和表头全选框是同一套 beast-core 组件：真实 <input> 是 0×0 + opacity:0 的
# 隐形元素，**点它必然 30s 超时报「element is not visible」**（2026-08-06 实机踩到：增量
# 早停后走到导出，卡在这里整批中止）。上面 _HEAD_CHECKBOX 已记过同一个坑，这里漏了同一套
# 处理。所以【状态读 input、点击点 label】：label 才是可见可点的那层。
_EXPORT_CB_LABEL = 'label[data-testid="beast-core-checkbox"]'

# 导出文件的 19 个列名（表头第 1 行，sheet 名 `sheet1`）。
EXPORT_COLUMNS = [
    "订单号", "站点", "订单状态", "子订单号", "应履约件数", "商品名称",
    "SKUID", "SKCID", "SPUID", "SKU货号", "商品属性",
    "运单号", "物流商", "发货仓",
    "订单创建时间", "要求最晚发货时间", "实际发货时间", "预计送达时间", "实际签收时间",
]

# 导出文件里的空值是字符串 `--`，不是空单元格。
_EMPTY_TOKENS = {"--", "-", "—", "None", "null"}

# ---- 采购汇总 xlsx 的版式参数（2026-07-30 用户定的口径）----------------------
# 行高单位是磅，1 磅 = 96/72 px：60 磅 ≈ 80px，图缩到 76px 正好留一点边。
# 为什么带图的表不能也用 25 磅：25 磅只有 33px 高，主图缩到那么小认不出是哪一款，
# 而「看图确认要采的是这款」正是加图的目的。不带图的明细表就按 25 磅。
_ROW_H_IMAGE = 60.0
_ROW_H_PLAIN = 25.0
_IMG_BOX_PX = 76
_IMG_COL_PX = 84  # 图片列固定宽度：容得下 76px 图 + 边距，不跟着屏幕宽按比例放大

# 各表列宽权重（相对值，实际像素按屏幕宽度分配，见 _fit_columns）。
# 取值依据：标题字数 + 该列典型内容长度。长文本列（商品名称/采购清单/订单号）给大权重
# 让它们吃掉屏幕余量，短列（件数/订单数）压到最小，整表因此能塞进一屏。
_W_PRODUCT = [84, 62, 52, 300, 105, 48, 58, 290, 150, 48, 52, 64, 92, 190]
_W_SKU = [84, 62, 62, 105, 105, 150, 260, 150, 58, 48, 52, 64, 92, 170, 170]
# 明细表末列「订单创建时间」权重比汇总表大：25 磅行高只放得下一行，
# `2026-07-27 10:16:38` 折成两行第二行就被截掉，必须留够单行宽度（约 140px）。
_W_DETAIL = [150, 150, 105, 105, 150, 285, 150, 52, 64, 115]


@dataclass
class OrderRow:
    """一条【子订单】级记录——导出文件按子订单展开（20 个订单导出 22 行）。"""

    order_no: str = ""          # 订单号 PO-045-...
    site: str = ""              # 站点（已归一化，去掉尾字「站」）
    status: str = ""            # 订单状态
    sub_order_no: str = ""      # 子订单号 045-...
    qty: str = ""               # 应履约件数
    goods_name: str = ""
    sku_id: str = ""
    skc_id: str = ""
    spu_id: str = ""
    sku_code: str = ""          # SKU货号
    attrs: str = ""             # 商品属性 → 登记表的「尺码」
    tracking_no: str = ""       # 运单号（待发货态为空）
    carrier: str = ""
    warehouse: str = ""
    created_at: str = ""        # 订单创建时间
    latest_ship_at: str = ""    # 要求最晚发货时间
    shipped_at: str = ""
    eta: str = ""               # 预计送达时间
    received_at: str = ""
    # 以下两个来自 DOM，按子订单号 join 进来（导出文件没有）
    image_url: str = ""
    # 成交单价：商家助手插件注入 DOM，**不是 Temu 官方字段**（官方导出 19 列里没有价格
    # 列）。2026-07-28 实机确认回填有约一天延迟：当天下的单页面上连「成交单价」标签都没
    # 有，前一天的都有。2026-07-29 确认：这一格允许为空，不因抓不到价而不登记该订单，
    # 故 plan_writes 默认 require_price=False（要严格拦无价时显式传 True）。
    deal_price: str = ""
    image_path: str = ""        # 下载落地后的本地路径


# ---- 离线部分（可用样本文件单测，不需要浏览器）-----------------------------


def _clean(v: Any) -> str:
    """单元格值归一化：`--` 之类的占位空值统一成空串。"""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in _EMPTY_TOKENS else s


def should_stop_incremental(
    page_orders: List[dict], known: set, seen_new: bool
) -> tuple:
    """增量早停判据。返回 (要不要停, 原因)——纯集合运算，不碰 page，可离线单测。

    判据：本页【出现任意一条】已登记的订单号就停。known 通常只有一个元素——登记表里最新的
    那条订单号（见 read_known_order_nos：写入走 insert_at_top，数据区顶端行即最新）。页面
    新→旧，翻到它就说明它之后的都更旧、都是上次登记时已处理过的，不再是本批要采的新单。

    判据按集合成员判定而非单值相等：known 是 set 的既有契约不变，多元素时语义自然退化成
    「遇到其中任意一条即停」，与单条水位一致。

    为什么不是原先的「整页全部已登记才停」：那个判据的粒度是页、水位的粒度是单，两者不匹配。
    水位少于一页条数（新建 Sheet 只登记过几条）时，任何一页都不可能 20 条全命中，`all()`
    恒为假，早停出口结构性走不通，必然翻到底——2026-08-04 实机就是这样：`WINTAK8.4+` 表里
    只有 1 个订单号，它出现在第 4 页，本该停在第 4 页，实际翻了 63 页。表越空增量越失效，
    与设计意图正好相反。

    同时【去掉了原来的交错检测】（「首个已知单之后不该再有新单，否则退全量」）：它无法区分
    「排序被改」和「表本来就稀疏」。上面那次实机里，第 4 页那条已登记单前后都是未登记单——
    前面的更新、后面的更旧，都从没登记过，这是新建表的正常状态，却被判成「排序可能被改或表
    里有空洞」而退全量。排序异常改由 check_created_desc 用创建时间单调性判断，那个信号才真
    正指向排序，不会跟水位稀疏混淆。

    已知代价：比表内最新那条【更旧】的空洞（上批某几条因未映射/无价被跳过）不会再被增量补上，
    因为翻到那条就停了。要补洞请显式走全量（CLI `--no-incremental`）。这是刻意的取舍——
    原先为了补洞而在稀疏水位下无条件退全量，代价是增量完全不起作用。

    seen_new 只用于区分原因文案：它为假说明本页连一条新单都没有、且此前各页也没有，
    等价于「本批无新单」；为真则是正常追平。两种都停。
    """
    nos = [str(o.get("order_no") or "").strip() for o in page_orders]
    nos = [n for n in nos if n]
    if not nos:
        return False, "本页读不到订单号"

    hit = next((n for n in nos if n in known), "")
    if not hit:
        return False, ""
    if not seen_new:
        return True, f"本页出现已登记订单 {hit} 且本批未发现新单，判定为无新单"
    return True, f"本页出现已登记订单 {hit}，已追上上次登记位置"


def _orders_before_watermark(page_orders: List[dict], known: set) -> List[str]:
    """取本页里位于水位【之前】（更新）的订单号，按页面顺序。纯函数，可离线单测。

    列表是创建时间新→旧，所以第一个命中 known 的位置就是水位，它**之前**的都比它新＝本批
    真正要采的新单；它自身和之后的都更旧、上次已处理过。
    2026-08-06 之前早停页走整页全选，把水位之后更旧的单也导出了（实机 5 条新单带出 14 条
    旧子订单）；改成只勾这里返回的这些。

    水位恰好在第一行（本批无新单）时返回空表，调用方据此不勾任何行。
    """
    out: List[str] = []
    for o in page_orders:
        no = str(o.get("order_no") or "").strip()
        if no in known:
            break
        if no:
            out.append(no)
    return out


def check_list_sort_url(list_url: str) -> str:
    """校验 list_url 是「创建时间新→旧」排序。不合格返回给操作者看的中止文案，合格返回空串。

    为什么必须有这道【确定性】校验：增量早停的正确性完全建立在「列表是新→旧」之上——翻到
    水位那条就停，是因为它之后的都更旧。排序若反过来，第一页就是最老的单、里面必然含水位，
    于是【停在第 1 页】，导出最老的 20 条、全被判重挡掉，汇总显示「待写 0 行、早停」。
    这跟正常的「本批无新单」长得一模一样，是**静默漏采**：那次可能有上千条新单一条没进表。

    唯一的运行时护栏 check_created_desc 依赖列表页 DOM 里能读到创建时间，而这一点未经实机
    确认（实机日志里从未出现过它的告警，大概率是压根没匹到时间、静默降级成了无护栏）。
    所以补这道纯字符串校验：不依赖 DOM 结构、不会静默失效，坏了就中止而不是照跑。

    只认 URL 里的 `sortType`。页面上手点列头改排序【不改 URL】，这道校验拦不住那种，
    仍由 check_created_desc 兜（能读到时间的话）——两道护栏覆盖不同来源，不互相替代。
    """
    if not list_url:
        return ""  # 缺 list_url 由调用方的必填校验负责，这里不重复报
    m = re.search(r"[?&]sortType=([^&#]*)", list_url)
    if not m:
        return (
            "list_url 里没有 sortType 参数，无法确认订单列表是「创建时间新→旧」排序。\n"
            "增量采集要求新单在前（翻到已登记那条就停）；排序不对会静默漏采。\n"
            f"请在 config/config.toml 的 [orders].list_url 补上 sortType={_SORT_DESC}，"
            "或加 --no-incremental 走全量采集。"
        )
    got = m.group(1).strip()
    if got != _SORT_DESC:
        return (
            f"list_url 的 sortType={got or '(空)'}，不是「创建时间新→旧」"
            f"（应为 {_SORT_DESC}）。\n"
            "增量采集靠「翻到已登记那条就停」，排序反了会停在第 1 页、把上千条新单全漏掉，"
            "而汇总看起来只是「本批无新单」。\n"
            f"请把 config/config.toml 的 [orders].list_url 改成 sortType={_SORT_DESC}，"
            "或加 --no-incremental 走全量采集。"
        )
    return ""


def check_created_desc(prev_created: str, page_orders: List[dict]) -> str:
    """校验创建时间跨页单调不增；违反返回告警文案，正常/读不到返回空串。

    这是【唯一】的排序护栏（原先的交错检测已删，理由见 should_stop_incremental）：
    best-effort，列表页 DOM 里有没有创建时间未经实机确认，读不到就返回空串、静默降级到
    无护栏，不中断也不阻塞早停。
    """
    times = [str(o.get("created_at") or "").strip() for o in page_orders]
    times = [t for t in times if t]
    if not times:
        return ""
    if prev_created and max(times) > prev_created:
        return (
            f"本页出现比上一页更新的创建时间（{max(times)} > {prev_created}），"
            "列表可能不是新→旧排序"
        )
    for a, b in zip(times, times[1:]):
        if b > a:
            return f"页内创建时间不是倒序（{a} → {b}），列表可能不是新→旧排序"
    return ""


def normalize_site(raw: Any) -> str:
    """站点归一化：导出的「哥伦比亚站」→「哥伦比亚」。

    登记表里同一站点混着两种写法（实测「哥伦比亚」4379 行 vs「哥伦比亚站」30 行），
    主流是不带「站」。统一去掉尾字，既对得上表内主流写法，也让 sheet_map 只需配一种。
    """
    s = _clean(raw)
    return s[:-1] if len(s) > 1 and s.endswith("站") else s


def parse_export_xlsx(path: str) -> List[OrderRow]:
    """解析官方导出的订单 xlsx → [OrderRow]。

    按【表头标题】取列，不认列序：导出字段设置一改（比如以后又勾上某个分组），列序会变。
    表头缺列直接抛异常——字段对不上就别往登记表写。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if not rows:
        raise ValueError(f"导出文件没有任何行：{path}")

    header = [_clean(c) for c in rows[0]]
    idx = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in ("订单号", "站点", "子订单号", "商品属性", "订单创建时间") if c not in idx]
    if missing:
        raise ValueError(f"导出文件缺列 {missing}；实际表头={header}")

    def cell(row: tuple, name: str) -> str:
        i = idx.get(name)
        return _clean(row[i]) if i is not None and i < len(row) else ""

    out: List[OrderRow] = []
    for row in rows[1:]:
        if not any(_clean(c) for c in row):
            continue
        order_no = cell(row, "订单号")
        if not order_no:
            continue
        out.append(
            OrderRow(
                order_no=order_no,
                site=normalize_site(cell(row, "站点")),
                status=cell(row, "订单状态"),
                sub_order_no=cell(row, "子订单号"),
                qty=cell(row, "应履约件数"),
                goods_name=cell(row, "商品名称"),
                sku_id=cell(row, "SKUID"),
                skc_id=cell(row, "SKCID"),
                spu_id=cell(row, "SPUID"),
                sku_code=cell(row, "SKU货号"),
                attrs=cell(row, "商品属性"),
                tracking_no=cell(row, "运单号"),
                carrier=cell(row, "物流商"),
                warehouse=cell(row, "发货仓"),
                created_at=cell(row, "订单创建时间"),
                latest_ship_at=cell(row, "要求最晚发货时间"),
                shipped_at=cell(row, "实际发货时间"),
                eta=cell(row, "预计送达时间"),
                received_at=cell(row, "实际签收时间"),
            )
        )
    return out


def _purchase_quantity(raw: Any) -> Decimal:
    """把应履约件数转成可汇总数字；异常值按 0 处理并留在明细中供核对。"""
    try:
        quantity = Decimal(_clean(raw) or "0")
    except InvalidOperation:
        return Decimal(0)
    return quantity if quantity >= 0 else Decimal(0)


def _append_unique(values: List[str], value: str) -> None:
    cleaned = _clean(value)
    if cleaned and cleaned not in values:
        values.append(cleaned)


def summarize_purchases(orders: List[OrderRow]) -> List[dict]:
    """按 SPU ID + SKU ID 聚合本批待新增订单，返回采购汇总行。"""
    groups: Dict[tuple, dict] = {}
    for order in orders:
        fallback = order.sub_order_no or order.order_no
        group_key = (
            order.spu_id or f"缺SPU:{fallback}",
            order.sku_id or f"缺SKU:{fallback}",
        )
        group = groups.setdefault(group_key, {
            "spu_id": order.spu_id,
            "sku_id": order.sku_id,
            "sku_codes": [],
            "goods_names": [],
            "attrs": [],
            "sites": [],
            "total_qty": Decimal(0),
            "order_nos": [],
            "sub_order_nos": [],
            "created_times": [],
            "image_path": "",
        })
        if not group["image_path"] and order.image_path:
            group["image_path"] = order.image_path
        group["total_qty"] += _purchase_quantity(order.qty)
        _append_unique(group["sku_codes"], order.sku_code)
        _append_unique(group["goods_names"], order.goods_name)
        _append_unique(group["attrs"], order.attrs)
        _append_unique(group["sites"], order.site)
        _append_unique(group["order_nos"], order.order_no)
        _append_unique(group["sub_order_nos"], order.sub_order_no)
        _append_unique(group["created_times"], order.created_at)

    summary: List[dict] = []
    for group in groups.values():
        quantity = group["total_qty"]
        summary.append({
            "spu_id": group["spu_id"],
            "sku_id": group["sku_id"],
            # 同一 SKU 的所有子订单主图必然是同一张，取首个下到本地的即可（供 xlsx 嵌图）
            "image_path": group["image_path"],
            "sku_codes": group["sku_codes"],
            "goods_names": group["goods_names"],
            "attrs": group["attrs"],
            "sites": group["sites"],
            "total_qty": int(quantity) if quantity == quantity.to_integral() else float(quantity),
            "order_count": len(group["order_nos"]),
            "sub_order_count": len(group["sub_order_nos"]),
            "is_repeated": len(group["sub_order_nos"]) > 1,
            "first_created_at": min(group["created_times"], default=""),
            "last_created_at": max(group["created_times"], default=""),
            "order_nos": group["order_nos"],
            "sub_order_nos": group["sub_order_nos"],
        })
    return sorted(
        summary,
        key=lambda group: (-group["total_qty"], group["spu_id"], group["sku_id"]),
    )


def summarize_products(orders: List[OrderRow]) -> List[dict]:
    """按【商品】（SPU）聚合，同商品的不同属性（尺码/颜色）收成它下面的 variants。

    为什么要在 SKU 级之上再来一层：采购是【按商品链接】下单的——打开一次链接就该把这批
    要的所有码数/款式一次买齐。按 (SPU, SKU) 聚合会把同一条裙子的 5 个尺码拆成 5 组，
    看的人得自己在表里认哪几行是同一款，正好是这份统计要替他做的事。

    分组键用 SPU ID（等价于一个商品链接）。SPU 缺失时退回商品名——宁可按名字并，也不要
    每条各成一组；名字也空就用子订单号兜底，保证不同商品不会被并到一起。
    """
    groups: Dict[str, dict] = {}
    for order in orders:
        fallback = order.goods_name or order.sub_order_no or order.order_no
        key = order.spu_id or f"缺SPU:{fallback}"
        group = groups.setdefault(key, {
            "spu_id": order.spu_id,
            "goods_names": [],
            "sku_codes": [],
            "sites": [],
            "total_qty": Decimal(0),
            "order_nos": [],
            "sub_order_nos": [],
            "created_times": [],
            "image_path": "",
            "variants": {},
        })
        if not group["image_path"] and order.image_path:
            group["image_path"] = order.image_path
        qty = _purchase_quantity(order.qty)
        group["total_qty"] += qty
        _append_unique(group["goods_names"], order.goods_name)
        _append_unique(group["sku_codes"], order.sku_code)
        _append_unique(group["sites"], order.site)
        _append_unique(group["order_nos"], order.order_no)
        _append_unique(group["sub_order_nos"], order.sub_order_no)
        _append_unique(group["created_times"], order.created_at)

        # 变体键取【属性】而不是 SKU ID：属性（`杏色 / 3-4Y`）才是下单时要选的那一栏，
        # 也是登记表「尺码」列的值。属性为空时退回 SKU ID，免得多个无属性变体并成一条。
        vkey = order.attrs or order.sku_id or order.sub_order_no
        variant = group["variants"].setdefault(vkey, {
            "attrs": order.attrs,
            "sku_id": order.sku_id,
            "sku_codes": [],
            "qty": Decimal(0),
            "order_nos": [],
            "sub_order_nos": [],
        })
        variant["qty"] += qty
        _append_unique(variant["sku_codes"], order.sku_code)
        _append_unique(variant["order_nos"], order.order_no)
        _append_unique(variant["sub_order_nos"], order.sub_order_no)

    out: List[dict] = []
    for group in groups.values():
        variants = sorted(
            (
                {
                    "attrs": v["attrs"],
                    "sku_id": v["sku_id"],
                    "sku_codes": v["sku_codes"],
                    "qty": _fmt_qty(v["qty"]),
                    "order_count": len(v["order_nos"]),
                    "order_nos": v["order_nos"],
                    "sub_order_nos": v["sub_order_nos"],
                }
                for v in group["variants"].values()
            ),
            key=lambda v: str(v["attrs"]),
        )
        out.append({
            "spu_id": group["spu_id"],
            # 商品级取该 SPU 下首个下到本地的主图（同款不同码主图基本一致）
            "image_path": group["image_path"],
            "goods_names": group["goods_names"],
            "sku_codes": group["sku_codes"],
            "sites": group["sites"],
            "total_qty": _fmt_qty(group["total_qty"]),
            "variants": variants,
            "variant_count": len(variants),
            "order_count": len(group["order_nos"]),
            "sub_order_count": len(group["sub_order_nos"]),
            # 一次下单要买多个变体，或同一变体被多张单买到——两种都值得单独拎出来看
            "is_multi": len(variants) > 1 or len(group["sub_order_nos"]) > 1,
            "first_created_at": min(group["created_times"], default=""),
            "last_created_at": max(group["created_times"], default=""),
            "order_nos": group["order_nos"],
            "sub_order_nos": group["sub_order_nos"],
        })
    return sorted(
        out,
        key=lambda g: (-g["variant_count"], -g["total_qty"], g["spu_id"]),
    )


def screen_client_px(reserve: int = 130) -> int:
    """当前主屏能放下多少像素的表格列宽，用于把整表压进一屏（只上下滚、不左右滚）。

    reserve 是要让出去的那部分：行号列（约 40px）+ 竖滚动条（约 17px）+ 窗口边框与
    WPS 侧栏留白。取 130 是 1920 屏实测手调的值——宁可少算几十像素留一点余量，
    也不要算超导致最后一列被挤到屏幕外，那就白做了。

    best-effort：读不到分辨率（非 Windows、无桌面会话）按 1920 算，只是列宽不贴合，
    绝不该让整份采购统计因此导不出来。
    """
    width = 1920
    try:
        import ctypes

        got = int(ctypes.windll.user32.GetSystemMetrics(0))
        if got > 0:
            width = got
    except Exception as e:
        logger.warning(f"读屏幕分辨率失败，列宽按 {width}px 估算：{e}")
    return max(width - reserve, 800)


def _px_to_width(px: float) -> float:
    """像素 → openpyxl 列宽单位（Calibri 11 下 px ≈ 7 × 宽度 + 5）。"""
    return round(max((px - 5) / 7, 1.5), 2)


def _fit_columns(
    worksheet,
    weights: List[float],
    avail_px: int,
    min_px: int = 46,
    fixed: Optional[Dict[int, float]] = None,
) -> None:
    """按权重把 avail_px 分配给各列，保证每列不低于 min_px。

    权重＝该列「希望占多宽」的相对值（标题长度 + 典型内容长度估的），不是内容实测最大值：
    改动前用的是 `max(len(内容))+2` 逐列取最宽，一个长商品名就能把那列撑到 45 字符、
    把整表推出屏幕，正是这次要解决的问题。配合自动换行，压窄的列会折行而不是截断。

    fixed={列序号: 像素} 用于图片列：它要的宽度由图框尺寸决定、跟屏幕多宽无关，跟着一起
    按比例放大只是白占地方（挤掉真正需要宽度的商品名/采购清单）。这些列先按固定值扣掉，
    余下的宽度才参与权重分配。
    """
    if not weights:
        return
    fixed = fixed or {}
    avail_px = max(avail_px - sum(fixed.values()), 200)
    flex = [0.0 if i in fixed else w for i, w in enumerate(weights, 1)]
    total = sum(flex) or 1.0
    raw = [avail_px * w / total for w in flex]
    # 先垫到 min_px，再把垫出来的超额从「本就宽于 min_px」的列里按比例扣回去
    got = [max(p, min_px) for p in raw]
    over = sum(got) - avail_px
    if over > 0:
        slack = [max(p - min_px, 0) for p in got]
        pool = sum(slack) or 1.0
        got = [p - over * s / pool for p, s in zip(got, slack)]
    for i, px in enumerate(got, 1):
        worksheet.column_dimensions[worksheet.cell(row=1, column=i).column_letter].width = (
            _px_to_width(fixed.get(i, px))
        )


def _style_sheet(worksheet, row_height: float, rows: int, cols: int) -> None:
    """统一版式：全表自动换行 + 垂直居中，数据行行高 row_height，冻结表头并开筛选。

    为什么整片单元格逐个设而不是只设列样式：openpyxl 的列级 alignment 只作用于【新建】
    单元格，已 append 的行不受影响，实测表现就是「设了没生效」。
    """
    from openpyxl.styles import Alignment

    wrap = Alignment(wrap_text=True, vertical="center", horizontal="left")
    head = Alignment(wrap_text=True, vertical="center", horizontal="center")
    for row in worksheet.iter_rows(min_row=1, max_row=rows, max_col=cols):
        for cell in row:
            cell.alignment = head if cell.row == 1 else wrap
    worksheet.row_dimensions[1].height = 30
    for r in range(2, rows + 1):
        worksheet.row_dimensions[r].height = row_height
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = (
        f"A1:{worksheet.cell(row=1, column=cols).column_letter}{max(rows, 1)}"
    )


def _embed_image(worksheet, row: int, path: str, box_px: int) -> bool:
    """把本地主图等比缩放后嵌进该行 A 列，成功返回 True。

    这里用的是普通浮动图，不是登记表那种 WPS DISPIMG 嵌入图：采购统计是本管线【新建】的
    文件，没有 DISPIMG 图索引表要维护；而登记表是既有 WPS 文件，动它必须走 zip/XML 直改
    （见 wps_excel_tool 的说明）。

    锚定刻意用 twoCellAnchor + editAs="twoCell"（「随单元格移动并调整大小」），不用
    openpyxl 传字符串时默认的 oneCellAnchor：这两张表开着筛选，人一按「是否采购完成」筛，
    oneCellAnchor 的图不会跟着隐藏，会整片糊在剩下的行上。两个锚点都落在同一个单元格内，
    靠偏移量圈出 box_px 的方框，所以图不会跨到右边的列去。

    best-effort：单张图坏了/丢了只告警，这一格空着，绝不让整份统计导不出来。
    """
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor
    from openpyxl.utils.units import pixels_to_EMU

    try:
        if not path or not Path(path).exists():
            return False
        img = XlImage(path)
        scale = min(box_px / max(img.width, 1), box_px / max(img.height, 1), 1.0)
        w, h = int(img.width * scale), int(img.height * scale)
        pad = 3
        img.anchor = TwoCellAnchor(
            editAs="twoCell",
            _from=AnchorMarker(col=0, row=row - 1,
                               colOff=pixels_to_EMU(pad), rowOff=pixels_to_EMU(pad)),
            to=AnchorMarker(col=0, row=row - 1,
                            colOff=pixels_to_EMU(pad + w), rowOff=pixels_to_EMU(pad + h)),
        )
        worksheet.add_image(img)
        return True
    except Exception as e:
        logger.warning(f"采购统计嵌图失败（该格留空）：{path} → {e}")
        return False


def purchase_file_stem(kind: str, store: str, stamp: str, region: str = "") -> str:
    """拼采购产物的文件名主干：`店铺-区域_类型_时间戳`。

    店铺名前置而不是插在时间戳后面：多店铺时同一天会有好几份，文件管理器按名称排序时
    前置能把同店的自然聚到一起（2026-08-05 用户要求）。

    区域紧跟店铺名（`Pawly-美国_...`）：同一账号在不同区域（全球/美国/欧区）店名**完全
    相同**，只带店铺名的话同一天两个区域的汇总在目录里长得一模一样，只能靠时间戳猜是哪个
    区域的单，采购时拿错就是照着别的区域下单。用连字符而非下划线连接，是为了让「店铺-区域」
    在按 `_` 切分的文件名结构里仍是同一段。

    店铺名与区域名都可能带 Windows 文件名非法字符（页面上是自由文本），统一剔除；剔空或
    本来就没识别到时逐级退化（有店无区域 → `店铺_类型_时间戳`；都没有 → `类型_时间戳`），
    不留下「_」「-」这种空占位。
    """
    def _safe(v: str) -> str:
        return "".join(c for c in str(v) if c not in '\\/:*?"<>|').strip()

    parts = [p for p in (_safe(store), _safe(region)) if p]
    return f"{'-'.join(parts)}_{kind}_{stamp}" if parts else f"{kind}_{stamp}"


def export_purchase_summary(
    orders: List[OrderRow], out_dir: str, stamp: str, store: str = "",
    region: str = "",
) -> dict:
    """导出本批新增订单的 SPU/SKU 采购汇总和逐条明细。

    版式按「一屏放下、只上下滚」来做（2026-07-30 用户要求）：列宽按运行时屏幕分辨率按权重
    分配、全表自动换行 + 垂直居中、带图两张表行高 60 磅（图缩到 76px 居中），明细表 25 磅。
    汇总两张表每行嵌该 SKU/商品的主图，并留一列「是否采购完成」供人工勾。
    时间只保留一个「创建时间」＝该组最早的下单时间（采购紧急度看它）。
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    groups = summarize_purchases(orders)
    products = summarize_products(orders)
    path = output_dir / (
        f"{purchase_file_stem('新增订单采购汇总', store, stamp, region)}.xlsx"
    )

    workbook = openpyxl.Workbook()
    # 商品级放第一张：采购是按商品链接下单的，先看「这款要买哪些码各几件」。
    # 「采购清单」列把该商品所有规格拼成一格（`3-4Y×2；5-6Y×1`），一眼能照着下单。
    product_sheet = workbook.active
    product_sheet.title = "商品汇总"
    product_sheet.append([
        "主图", "是否采购完成", "需多规格", "商品名称", "SPU ID", "规格数",
        "采购总件数", "采购清单", "SKU货号", "订单数", "子订单数", "站点",
        "创建时间", "订单号",
    ])
    product_images = 0
    for i, p in enumerate(products, 2):
        product_sheet.append([
            "", "",
            "是" if p["is_multi"] else "否",
            "；".join(p["goods_names"]), p["spu_id"], p["variant_count"],
            p["total_qty"],
            "；".join(
                f"{v['attrs'] or '（无属性）'}×{v['qty']}" for v in p["variants"]
            ),
            "；".join(p["sku_codes"]), p["order_count"], p["sub_order_count"],
            "；".join(p["sites"]), p["first_created_at"],
            "；".join(p["order_nos"]),
        ])
        product_images += _embed_image(product_sheet, i, p["image_path"], _IMG_BOX_PX)

    summary_sheet = workbook.create_sheet("SPU_SKU汇总")
    summary_sheet.append([
        "主图", "是否采购完成", "是否重复采购", "SPU ID", "SKU ID", "SKU货号",
        "商品名称", "商品属性", "采购总件数", "订单数", "子订单数", "站点",
        "创建时间", "订单号", "子订单号",
    ])
    sku_images = 0
    for i, group in enumerate(groups, 2):
        summary_sheet.append([
            "", "",
            "是" if group["is_repeated"] else "否",
            group["spu_id"], group["sku_id"], "；".join(group["sku_codes"]),
            "；".join(group["goods_names"]), "；".join(group["attrs"]),
            group["total_qty"], group["order_count"], group["sub_order_count"],
            "；".join(group["sites"]), group["first_created_at"],
            "；".join(group["order_nos"]), "；".join(group["sub_order_nos"]),
        ])
        sku_images += _embed_image(summary_sheet, i, group["image_path"], _IMG_BOX_PX)

    detail_sheet = workbook.create_sheet("订单明细")
    detail_headers = [
        "订单号", "子订单号", "SPU ID", "SKU ID", "SKU货号", "商品名称", "商品属性",
        "采购件数", "站点", "订单创建时间",
    ]
    detail_sheet.append(detail_headers)
    for order in sorted(
        orders,
        key=lambda item: (item.spu_id, item.sku_id, item.created_at, item.sub_order_no),
    ):
        quantity = _purchase_quantity(order.qty)
        detail_sheet.append([
            order.order_no, order.sub_order_no, order.spu_id, order.sku_id, order.sku_code,
            order.goods_name, order.attrs,
            int(quantity) if quantity == quantity.to_integral() else float(quantity),
            order.site, order.created_at,
        ])

    header_fill = PatternFill("solid", fgColor="F4B183")
    avail = screen_client_px()
    for worksheet, weights, height, rows, img in (
        (product_sheet, _W_PRODUCT, _ROW_H_IMAGE, len(products) + 1, True),
        (summary_sheet, _W_SKU, _ROW_H_IMAGE, len(groups) + 1, True),
        (detail_sheet, _W_DETAIL, _ROW_H_PLAIN, len(orders) + 1, False),
    ):
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
            cell.fill = header_fill
        _fit_columns(worksheet, weights, avail, fixed={1: _IMG_COL_PX} if img else None)
        _style_sheet(worksheet, height, rows, len(weights))

    # 「是否采购完成」给个是/否下拉：这一列是人工勾的，下拉能防手输「已采」「ok」这类
    # 五花八门的写法，后续想按它筛未采购才筛得干净。allow_blank=True——留空就是还没处理。
    for worksheet, rows in ((product_sheet, len(products)), (summary_sheet, len(groups))):
        if rows:
            _add_done_dropdown(worksheet, rows)

    workbook.save(path)
    workbook.close()
    return {
        "file": str(path),
        "rows": len(orders),
        "groups": len(groups),
        "repeated_groups": sum(1 for group in groups if group["is_repeated"]),
        "products": len(products),
        "multi_products": sum(1 for p in products if p["is_multi"]),
        "total_qty": sum(group["total_qty"] for group in groups),
        "product_images": product_images,
        "sku_images": sku_images,
    }


def _add_done_dropdown(worksheet, rows: int) -> None:
    """给 B 列（是否采购完成）挂「是/否」下拉。best-effort，失败只告警。"""
    from openpyxl.worksheet.datavalidation import DataValidation

    try:
        dv = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
        dv.error = "请选择「是」或「否」"
        worksheet.add_data_validation(dv)
        dv.add(f"B2:B{rows + 1}")
    except Exception as e:
        logger.warning(f"「是否采购完成」下拉设置失败（该列仍可手输）：{e}")


def _md_cell(value: Any) -> str:
    """转义成表格单元格：竖线会截断列、换行会断行，两者都得处理。"""
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|")
    return " ".join(text.split()) or "-"


def _md_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    """拼一张 GFM 表格；无数据行时给一句占位，避免出现空表头。"""
    if not rows:
        return ["（无）", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    lines.append("")
    return lines


def _fmt_qty(quantity: Decimal) -> Any:
    """件数取整展示：Decimal('3') 打成 3 而不是 3.0，非整数才保留小数。"""
    return int(quantity) if quantity == quantity.to_integral() else float(quantity)


def export_purchase_markdown(
    orders: List[OrderRow], out_dir: str, stamp: str, store: str = "",
    region: str = "",
) -> dict:
    """把本批新增订单按 SPU+SKU 的合并采购情况写成 md，每批一个新文件。

    为什么在 xlsx 之外再出一份 md：xlsx 适合筛选核对，但采购前真正要看的是「这批里哪几
    张单买的是同一个 SPU+SKU、能合成一次下单、各要几件」——这类判断要的是一眼能读完的
    清单，而不是打开 WPS 拉筛选器。md 还能直接贴进聊天里跟供应商对量。

    统计口径与 xlsx 一致：只统计本批【待新增】的订单（判重跳过、未映射的都不在内），
    否则合并出来的件数会把已下过单的重复算进去。
    """
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    products = summarize_products(orders)
    multi = [p for p in products if p["is_multi"]]
    single = [p for p in products if not p["is_multi"]]
    total_qty = sum(p["total_qty"] for p in products)
    path = output_dir / (
        f"{purchase_file_stem('新增订单采购统计', store, stamp, region)}.md"
    )

    lines: List[str] = [
        "# 本批新增订单采购统计（按商品合并，打开一次链接买齐所有码数）", "",
    ]
    # 店铺+区域写进正文抬头：md 常被整段贴进聊天跟供应商对量，脱离文件名后也得看得出是
    # 哪家店哪个区域的单（同账号跨区域店名完全相同，只写店名分不清）
    if str(store).strip():
        rg = str(region).strip()
        lines.append(f"- 店铺：{store}" + (f"（{rg}）" if rg else ""))
    elif str(region).strip():
        lines.append(f"- 区域：{region}")
    lines += [
        f"- 批次标识：{stamp}",
        f"- 本批待登记子订单：{len(orders)} 条",
        f"- 涉及商品（SPU）：{len(products)} 个",
        f"- 需一次买多个码数/款式：{len(multi)} 个商品，"
        f"共 {sum(p['variant_count'] for p in multi)} 个规格",
        f"- 采购总件数：{total_qty}", "",
    ]
    lines += _section_multi(multi)
    lines += _section_single_product(single)
    lines += _section_detail(multi, orders)

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "md_file": str(path),
        "rows": len(orders),
        "products": len(products),
        "multi_products": len(multi),
        "variants": sum(p["variant_count"] for p in products),
        "total_qty": total_qty,
    }


def _section_multi(multi: List[dict]) -> List[str]:
    """需一次买多规格的商品：每个商品一个小节，下面一张「要买哪些码数各几件」的表。

    这是整份文件的主角——一个小节就对应【打开一次商品链接】要做的事，表里每行是那个链接
    页面上要选的一个规格。不做成一张大表：大表里同款的行会被别的商品隔开，等于把「这一款
    要买哪几个码」这个判断又推回给人。
    """
    lines = ["## 一、需一次买多个码数/款式（打开一次商品链接买齐）", ""]
    if not multi:
        lines += ["本批每个商品都只要一个规格、各一单，逐单采购即可。", ""]
        return lines
    for p in multi:
        title = "；".join(p["goods_names"]) or "（无商品名）"
        lines += [
            f"### {_md_cell(title)}",
            f"SPU `{p['spu_id'] or '缺失'}`｜{p['variant_count']} 个规格｜"
            f"合计 {p['total_qty']} 件｜{p['order_count']} 张订单｜"
            f"站点 {'；'.join(p['sites']) or '-'}", "",
        ]
        lines += _md_table(
            ["尺码/属性", "件数", "SKU货号", "SKU ID", "订单号"],
            [[
                v["attrs"] or "（无属性）", v["qty"], "；".join(v["sku_codes"]),
                v["sku_id"], "；".join(v["order_nos"]),
            ] for v in p["variants"]],
        )
    return lines


def _section_single_product(single: List[dict]) -> List[str]:
    """只要一个规格、且只有一条子订单的商品：列出来让本批总量自洽。"""
    lines = ["## 二、单规格单次采购", ""]
    if not single:
        lines += ["本批没有这类商品。", ""]
        return lines
    lines += _md_table(
        ["商品名称", "尺码/属性", "件数", "SKU货号", "SPU ID", "站点", "订单号"],
        [[
            "；".join(g["goods_names"]),
            "；".join(v["attrs"] for v in g["variants"]),
            g["total_qty"], "；".join(g["sku_codes"]), g["spu_id"],
            "；".join(g["sites"]), "；".join(g["order_nos"]),
        ] for g in single],
    )
    return lines


def _section_detail(multi: List[dict], orders: List[OrderRow]) -> List[str]:
    """把多规格商品展开到子订单：收货后按这份把货分回各订单，只有汇总件数不够用。

    分组键与 summarize_products 保持一致（SPU，缺失时退商品名），否则这一段会漏掉
    SPU 为空的那批订单。
    """
    if not multi:
        return []
    lines = ["## 三、子订单明细（收货后按此分单）", ""]
    by_key: Dict[str, List[OrderRow]] = {}
    for o in orders:
        fallback = o.goods_name or o.sub_order_no or o.order_no
        by_key.setdefault(o.spu_id or f"缺SPU:{fallback}", []).append(o)

    for p in multi:
        fallback = (p["goods_names"] or [""])[0] or (p["sub_order_nos"] or [""])[0]
        key = p["spu_id"] or f"缺SPU:{fallback}"
        members = sorted(
            by_key.get(key, []),
            key=lambda x: (str(x.attrs or ""), str(x.created_at or ""),
                           x.sub_order_no),
        )
        title = "；".join(p["goods_names"]) or "（无商品名）"
        lines += [
            f"### {_md_cell(title)}",
            f"SPU `{p['spu_id'] or '缺失'}`，合计 {p['total_qty']} 件"
            f"（{p['variant_count']} 个规格 / {p['sub_order_count']} 条子订单）", "",
        ]
        lines += _md_table(
            ["尺码/属性", "订单号", "子订单号", "件数", "站点", "下单时间",
             "要求最晚发货"],
            [[
                o.attrs, o.order_no, o.sub_order_no,
                _fmt_qty(_purchase_quantity(o.qty)),
                o.site, o.created_at, o.latest_ship_at,
            ] for o in members],
        )
    return lines


# 数量列的标题写法集合。管线自己插的那一列固定叫「数量」（见 service.ensure_qty_columns），
# 但用户可能早先手工加过别的写法，一并认下来，免得插出第二列同义列。
QTY_TITLES = {"数量", "件数", "商品数量", "采购件数", "应履约件数"}
QTY_TITLE = "数量"          # 管线插列时写入的标题
QTY_AFTER_TITLE = "尺码"    # 插在这一列右侧（2026-07-30 用户指定）


def _qty_number(raw: Any):
    """把「应履约件数」转成【数字】写进登记表；空值返回空串（不落单元格）。

    必须是数字而不是文本：这一列要能直接求和、筛选大于 1 的多件单。整数就写 int，
    免得 1 显示成 1.0；解析不出（导出偶发脏值）退回原始文本，宁可留痕给人看，
    也不要静默写 0 让人以为这单不用发货。
    """
    text = _clean(raw)
    if not text:
        return ""
    try:
        quantity = Decimal(text)
    except InvalidOperation:
        return text
    return int(quantity) if quantity == quantity.to_integral() else float(quantity)


# 登记表列标题 → 取值。一个字段可能有多种标题写法（` StoreD` 表用「站点」而非
# 「站点区分」），故用标题集合匹配。这里【只列管线能填的字段】：国内发出时间/采购日期/
# 采购费用/Y2头程费用/采购订单号/物流情况/产品图2 是人工后续填的，管线一律留空不碰。
_FIELD_SOURCES: List[tuple] = [
    ({"订单店铺", "店铺"}, lambda o, ctx: ctx.get("store_value", "")),
    ({"站点区分", "站点"}, lambda o, ctx: o.site),
    ({"订单号"}, lambda o, ctx: o.order_no),
    # 子订单号：表里本来没有这列，2026-08-06 起支持（有就写、没有就自然跳过，见本函数说明）。
    # 它是唯一能区分同一订单多行的字段，所以判重键可以升级成「订单号+子订单号」。
    # 为什么它比尺码可靠：尺码取自导出的「商品属性」，该值不保证唯一——实测某 SKU 的属性
    # 恒为字符串 `Variant`（Temu 那边就没有规格名），同一订单里两个该商品的子订单会生成
    # 完全相同的键，第二行被判成重复丢掉＝少买一件。子订单号一单一号，不存在这个问题。
    ({"子订单号"}, lambda o, ctx: o.sub_order_no),
    ({"尺码"}, lambda o, ctx: o.attrs),
    (QTY_TITLES, lambda o, ctx: _qty_number(o.qty)),
    ({"平台物流跟踪号", "平台跟踪号"}, lambda o, ctx: o.tracking_no),
    ({"平台创建时间"}, lambda o, ctx: o.created_at),
    ({"平台成交价"}, lambda o, ctx: o.deal_price),
]


def resolve_title_column(header: Dict[str, str], titles) -> Optional[str]:
    """在表头里找标题命中 titles（字符串或集合）的列字母；找不到返回 None。"""
    want = {titles} if isinstance(titles, str) else set(titles)
    for col, title in sorted(header.items(), key=lambda kv: _col_key(kv[0])):
        if title.strip() in want:
            return col
    return None


def image_column(header: Dict[str, str]) -> Optional[str]:
    """图片列：优先精确「产品图片」，否则取首个含「产品图」的列。

    为什么不能只按「含产品图」：`StoreA全球1` 同时有「产品图片」和「产品图2」，
    后者是人工补图位，管线只写前者。
    """
    exact = resolve_title_column(header, "产品图片")
    if exact:
        return exact
    for col, title in sorted(header.items(), key=lambda kv: _col_key(kv[0])):
        if "产品图" in title.strip():
            return col
    return None


def _col_key(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def build_row_values(
    order: OrderRow, header: Dict[str, str], store_value: str
) -> Dict[str, str]:
    """按目标 Sheet 的【真实表头】生成 {列字母: 值}。

    绝不硬编码列号：13 个登记 Sheet 的列序各不相同（`StoreA全球1` 订单号在 C，
    `牛仔裤` 在 B）。表头没有的字段自然不写，多出来的列留空给人工填。
    """
    ctx = {"store_value": store_value}
    values: Dict[str, str] = {}
    for col, title in header.items():
        t = title.strip()
        for titles, getter in _FIELD_SOURCES:
            if t in titles:
                values[col] = getter(order, ctx)
                break
    return values


def dedupe_key(
    order: OrderRow, header: Dict[str, str], titles: List[str], store_value: str
) -> Optional[tuple]:
    """按判重列标题生成本条订单的键，与 existing_key_tuples 的口径一致（strip 后比较）。

    任一判重列在该 Sheet 找不到 → 返回 None，调用方据此判定「这个 Sheet 没法判重」
    并跳过整个 Sheet，而不是无键硬写出一堆重复行。
    """
    values = build_row_values(order, header, store_value)
    key: List[str] = []
    for title in titles:
        col = resolve_title_column(header, title)
        if not col:
            return None
        key.append(str(values.get(col, "")).strip())
    return tuple(key)


def resolve_sheet(store: str, site: str, sheet_map: List[dict]) -> Optional[dict]:
    """按 (店铺, 站点) 查目标 Sheet 配置；未命中返回 None（调用方跳过并报告，不臆测）。

    店铺按【子串】匹配：页面右上角显示的店铺名可能带后缀（"StoreA Store"），
    配置里只写主名 "StoreA"。站点按归一化后精确匹配。
    """
    s_site = normalize_site(site)
    for item in sheet_map or []:
        cfg_store = str(item.get("store", "")).strip()
        if not cfg_store:
            continue
        if cfg_store not in store and store not in cfg_store:
            continue
        sites = [normalize_site(x) for x in (item.get("sites") or [])]
        if sites and s_site not in sites:
            continue
        return item
    return None


def to_jpeg_url(url: str) -> str:
    """把主图 URL 的 avif 输出参数改成 jpeg。

    kwcdn 默认发 `?imageView2/2/w/800/q/70/format/avif`，而 Excel/WPS 不认 avif，
    嵌进去就是一个空白格。顺手把宽度压到 800、质量提到 85（登记表里图是缩略展示）。
    """
    if not url:
        return url
    if "format/avif" in url:
        url = url.replace("format/avif", "format/jpeg")
    elif "imageView2" in url and "format/" not in url:
        url = url.rstrip("/") + "/format/jpeg"
    return url


# ---- 页面部分（需要真浏览器，实机验证）-------------------------------------

# 店铺名候选选择器：右上角店铺切换器。类名是构建期 hash，只能靠 data-testid / 语义定位，
# 全都可能失配——所以 detect_store 拿不到就返回空串，由调用方要求显式指定，绝不猜。
_STORE_SELECTORS = [
    '[data-testid="beast-core-dropdown"] [class*="mallName"]',
    '[class*="mallName"]',
    '[class*="shopName"]',
    'header [class*="mall"]',
]


# 页面自身注入的状态数据，优先于 DOM 选择器——店铺名挂在构建期 hash 类名（如 `_Wrz4-O9w`）
# 上，改版即失效；状态数据跟着接口字段走，稳得多。
#
# 【2026-08-07 实测修正】原先只读 `window.rawData.store.authUser.mallList`，但本版后台
# **压根没有 window.rawData**（实测该 JS 返回空串，detect_store 一直在靠 DOM 兜底）。
# 真正存在的是 `window.__USER_INFO__.shopList[].malInfoList[]`：
#   {mallId: 634418228070796（**数字**）, mallName: "Pawly", managedType: 1, ...}
# 注意平台把 mall 拼成了 `mal`（malInfoList）。rawData 那条保留，别的后台版本可能有。
#
# 仍坚持「只有一个店才认」：订单侧没有 mallid 可用来精确定位（采集侧是从列表请求头嗅到的），
# 多店账号无法从列表判断「当前是哪个」，那种情况交给 DOM，DOM 也读不到就返回空串让调用方
# 显式指定——店铺决定订单落哪张表，猜错要人工回滚。
_STORE_JS = """
() => {
  const one = (arr) => {
    if (!Array.isArray(arr) || arr.length !== 1) return '';
    return String(arr[0]?.mallName || '');
  };
  try {
    for (const shop of (window.__USER_INFO__?.shopList || [])) {
      const t = one(shop?.malInfoList);
      if (t) return t;
    }
  } catch (e) { /* 结构变了就往下降级 */ }
  try {
    return one(window.rawData?.store?.authUser?.mallList);
  } catch (e) { return ''; }
}
"""


async def detect_store(page) -> str:
    """读当前登录店铺名：优先页面状态数据，回退 DOM 选择器；拿不到返回空串。

    为什么允许失败：店铺名决定订单落到哪张表，猜错就是把订单写进别人家的 Sheet——
    比不写更糟且要人工回滚。所以这里只做尝试，service 层在拿不到时要求显式传 store，
    宁可停下问，不做默认值。

    `mallList` 只有一个店时才认：多店账号无法从这份列表判断「当前是哪个」，那种情况交给
    DOM（页头显示的是当前店），DOM 也读不到就返回空串让调用方显式指定。
    """
    try:
        name = (await page.evaluate(_STORE_JS) or "").strip()
        if name and len(name) <= 40:
            logger.info(f"识别到当前店铺：{name}（来自页面 rawData）")
            return name
    except Exception as e:
        logger.warning(f"读 rawData 店铺名失败，回退 DOM：{e}")

    for sel in _STORE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            txt = (await loc.inner_text() or "").strip()
            # 取首行：切换器里常把店铺名和「切换店铺」等提示排在一起
            txt = txt.splitlines()[0].strip() if txt else ""
            if txt and len(txt) <= 40:
                logger.info(f"识别到当前店铺：{txt}（选择器 {sel}）")
                return txt
        except Exception as e:
            logger.warning(f"读店铺名失败（选择器 {sel}）：{e}")
    logger.warning("没能从页面识别店铺名——需要调用方显式指定 store")
    return ""


async def read_pagination(page) -> Dict[str, Any]:
    """读分页状态：{total, page, page_size, has_next}。

    total 取自「共有 N 条」；当前页/每页取自分页容器的
    `data-status="beast-core-pagination-{pageSize}-{page}"`。
    """
    ul = page.locator(_PAGINATION).first
    if await ul.count() == 0:
        raise RuntimeError("找不到分页容器，页面可能没加载完或改版")

    status = await ul.get_attribute("data-status") or ""
    m = re.search(r"beast-core-pagination-(\d+)-(\d+)", status)
    page_size = int(m.group(1)) if m else 0
    cur = int(m.group(2)) if m else 0

    total = 0
    tt = ul.locator(_TOTAL_TEXT).first
    if await tt.count():
        mt = _RE_TOTAL.search(await tt.inner_text() or "")
        if mt:
            total = int(mt.group(1).replace(",", ""))

    nxt = ul.locator(_PAGE_NEXT).first
    has_next = False
    if await nxt.count():
        cls = await nxt.get_attribute("class") or ""
        has_next = "PGT_disabled" not in cls

    return {"total": total, "page": cur, "page_size": page_size, "has_next": has_next}


async def selected_count(page) -> Optional[int]:
    """读「已选订单：N」计数；页面没这个文案时返回 None（不当失败）。"""
    try:
        body = await page.inner_text("body")
    except Exception as e:
        logger.warning(f"读已选计数失败：{e}")
        return None
    m = _RE_SELECTED.search(body or "")
    return int(m.group(1).replace(",", "")) if m else None


async def disable_pointer_overlays(page) -> int:
    """让商家助手悬浮球不吃指针事件，返回改动的节点数。

    best-effort：这是辅助路径，失败不该中断采集——真被遮挡了后面的 click 自己会超时报错，
    错误信息比这里抛更有诊断价值（call log 会写明是哪个元素 intercepts pointer events）。
    详见 `_POINTER_OVERLAYS` 处的注释。
    """
    try:
        n = await page.evaluate(_DISABLE_OVERLAY_JS, _POINTER_OVERLAYS)
    except Exception as e:
        logger.warning(f"屏蔽悬浮层指针事件失败（不影响主流程）：{e}")
        return 0
    n = int(n or 0)
    if n:
        logger.info(f"已屏蔽商家助手悬浮层的指针事件（{n} 个节点）")
    return n


async def select_all_on_page(page) -> None:
    """勾选表头全选框 = 全选【当前页】（不是全部）。

    「跨页勾选」开关默认是开的，实测翻页后已勾选状态保留并累积（第1页勾20 → 翻到
    第2页再勾 → 累计40），所以逐页勾完再一次性导出可行。
    """
    box = page.locator(_HEAD_CHECKBOX).first
    if await box.count() == 0:
        raise RuntimeError("找不到表头全选框")
    if await box.is_checked():
        return

    await disable_pointer_overlays(page)

    # 点可见的 label（隐形 input 点不动，见 _HEAD_CHECKBOX_CLICK 注释）
    click_target = page.locator(_HEAD_CHECKBOX_CLICK).first
    if await click_target.count() == 0:
        click_target = box  # label 结构变了就退回直点 input，让报错停在原来的位置
    await click_target.click()
    await page.wait_for_timeout(400)

    # 校验勾上了：这一步失败就意味着后面导出的是空集或漏页，必须当场停下
    if not await box.is_checked():
        raise RuntimeError("点了全选框但状态没变成已勾选，页面可能改版")


# 按订单号勾选单行：定位到含该订单号的 tr，点它的行首 checkbox 的可见 label。
# 与表头全选同一套 beast-core 结构（隐形 input + 可点 label），所以同样【点 label】。
# 返回实际点中的行数，供调用方校验「想勾几行就勾中几行」。
_SELECT_ROWS_JS = r"""
(wanted) => {
  const want = new Set(wanted);
  let hit = 0, missed = [];
  const trs = Array.from(document.querySelectorAll('table tbody tr'));
  for (const tr of trs) {
    const m = (tr.innerText || '').match(/PO-[\d-]+/);
    if (!m || !want.has(m[0])) continue;
    const box = tr.querySelector('input[type="checkbox"]');
    if (!box) { missed.push(m[0]); continue; }
    if (box.checked) { hit++; continue; }
    // 点可见的 label：行首 input 同样是 0×0+opacity:0，直接 .click() 到 input 上
    // 在真实浏览器里不会触发 React 的 onChange，所以走 label
    const label = box.closest('label[data-testid="beast-core-checkbox"]')
                  || box.closest('label');
    (label || box).click();
    hit++;
  }
  return {hit, missed};
}
"""


async def select_rows_by_order_no(page, order_nos) -> int:
    """只勾选本页中订单号在 order_nos 里的行；返回勾中的行数。

    为什么需要它：增量早停那一页原先走整页全选，于是水位之后那些**更旧的**订单也被导出
    （2026-08-06 实机：只想要 5 条新单，导出却带上 14 条旧子订单）。旧行虽能被判重挡住，
    但前提是判重键足够细——而实测键不够细时（只配订单号、或属性恒为 `Variant`）就会写进去。
    所以正确做法是源头上只勾新单，别把「导出集合」的正确性外包给判重。

    一个订单可能占多行（多子订单），这里按订单号匹配，命中的行全勾——同一订单的兄弟行
    本来就该一起采。返回值由调用方与期望行数比对，不一致就说明页面结构变了。
    """
    wanted = [str(n).strip() for n in (order_nos or []) if str(n).strip()]
    if not wanted:
        return 0
    await disable_pointer_overlays(page)
    res = await page.evaluate(_SELECT_ROWS_JS, wanted)
    hit = int((res or {}).get("hit") or 0)
    missed = (res or {}).get("missed") or []
    if missed:
        logger.warning(f"有 {len(missed)} 行找不到行首复选框（未勾选）：{missed[:3]}")
    await page.wait_for_timeout(400)
    return hit


async def goto_next_page(page, timeout_ms: int = 20000) -> bool:
    """点「下一页」并等页码真的变了。已在尾页返回 False。"""
    ul = page.locator(_PAGINATION).first
    before = await ul.get_attribute("data-status")
    nxt = ul.locator(_PAGE_NEXT).first
    if await nxt.count() == 0:
        return False
    if "PGT_disabled" in (await nxt.get_attribute("class") or ""):
        return False

    # 悬浮球正压在分页条上，不先屏蔽会 30s 超时（见 _POINTER_OVERLAYS）。每次翻页都调：
    # 插件是 React 渲染，节点可能被重建，一次性设过不代表还生效。
    await disable_pointer_overlays(page)
    await nxt.click()
    waited = 0
    while waited < timeout_ms:
        await asyncio.sleep(0.25)
        waited += 250
        if await ul.get_attribute("data-status") != before:
            # 页码变了还要等表格行渲染出来，否则接着抓图会抓到上一页的残留
            try:
                await page.wait_for_selector("table tbody tr", timeout=10000)
            except Exception as e:
                logger.warning(f"翻页后等表格行超时：{e}")
            await page.wait_for_timeout(600)
            return True
    raise RuntimeError("点了下一页但分页状态没变，翻页失败")


# 逐行触发懒加载：表格在内部滚动容器里，window.scrollTo 对它无效（实测
# scrollHeight 只有 1059，滚不动），必须对每个 tr 调 scrollIntoView。
_SCROLL_ROWS_JS = r"""
async () => {
  const trs = Array.from(document.querySelectorAll('table tbody tr'));
  for (const tr of trs) {
    tr.scrollIntoView({block: 'center'});
    await new Promise(r => setTimeout(r, 260));
  }
  return trs.length;
}
"""

# 按【包含 img 且包含「子订单号：」的最小块】取 (子订单号 → 图 URL)。
# 为什么不按整行取：一行可含多个子订单，整行的 innerText 里第一个匹配到的往往是主订单号
# （去掉 PO- 前缀后长得跟子订单号一模一样），照抓必然错配。做法是从 img 往上找【第一个】
# 含「子订单号：」的祖先，并且要求该祖先只含【一个】子订单号——含多个说明已经爬到行级
# 容器、无法归属，宁可跳过也不错配。
_GRAB_IMAGES_JS = r"""
() => {
  const out = [];
  const skipped = [];
  const imgs = Array.from(document.querySelectorAll('table tbody img'));
  for (const img of imgs) {
    const src = img.getAttribute('src') || '';
    if (!src || src.startsWith('data:')) continue;
    let el = img, box = null, sub = null;
    for (let i = 0; i < 8; i++) {
      el = el.parentElement;
      if (!el) break;
      const t = el.innerText || '';
      const hits = t.match(/子订单号[:：]\s*[\d-]+/g);
      if (!hits || !hits.length) continue;
      if (hits.length === 1) {
        sub = hits[0].replace(/子订单号[:：]\s*/, '');
        box = el;
      }
      break;
    }
    if (!sub) { skipped.push(src.slice(0, 80)); continue; }
    const txt = box.innerText || '';
    let price = '';
    const pm = txt.match(/成交单价[:：]\s*([\d.]+)/);
    if (pm) {
      price = pm[1];
    } else {
      const infoEl = document.querySelector(`[id$="-${sub}-info"]`);
      if (infoEl) {
        const m2 = (infoEl.innerText || '').match(/([\d.]+)/);
        if (m2) price = m2[1];
      }
    }
    out.push({
      sub: sub,
      url: src,
      price: price,
      loaded: (img.className || '').includes('loaded') ||
              img.getAttribute('data-state') === 'succ',
    });
  }
  return {items: out, skipped: skipped};
}
"""


# 读本页各行的主订单号（+ 尽力取创建时间），供增量早停判断。
# 为什么能用正则取主订单号：`PO-` 前缀是【主订单号独有】的，子订单号是去掉该前缀的形式
# （`PO-045-xxx` vs `045-xxx`），所以 /PO-[\d-]+/ 不会误命中子订单号。这和抓图那边的
# 困境不同——那边要的是子订单号，才必须往上找最小块。
# 创建时间是 best-effort：列表页有没有这一列未经实机确认，取行内首个 `YYYY-MM-DD HH:MM:SS`
# 形状的文本，取不到就留空，由 check_created_desc 自行降级。
_PAGE_ORDERS_JS = r"""
() => {
  const out = [];
  const trs = Array.from(document.querySelectorAll('table tbody tr'));
  for (const tr of trs) {
    const txt = tr.innerText || '';
    const m = txt.match(/PO-[\d-]+/);
    if (!m) continue;
    const t = txt.match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/);
    out.push({order_no: m[0], created_at: t ? t[0].replace('T', ' ') : ''});
  }
  return out;
}
"""


async def read_page_orders(page) -> List[dict]:
    """读本页每行的 {order_no, created_at}，按页面显示顺序。失败返回空表。

    best-effort：读不到就让调用方按「本页读不到订单号」处理（不早停、继续翻），
    宁可多翻几页也不能因为读不到就停。
    """
    try:
        rows = await page.evaluate(_PAGE_ORDERS_JS)
    except Exception as e:
        logger.warning(f"读本页订单号失败（本页不参与增量判断）：{e}")
        return []
    out: List[dict] = []
    seen: set = set()
    for r in rows or []:
        no = str(r.get("order_no") or "").strip()
        # 一个订单可占多行（多子订单），同页去重后再判，避免重复计数
        if no and no not in seen:
            seen.add(no)
            out.append({"order_no": no, "created_at": str(r.get("created_at") or "")})
    return out


async def grab_page_images(page) -> Dict[str, Dict[str, str]]:
    """滚完当前页所有行触发懒加载，返回 {子订单号: {url, price, loaded}}。"""
    n_rows = await page.evaluate(_SCROLL_ROWS_JS)
    res = await page.evaluate(_GRAB_IMAGES_JS)
    items = res.get("items") or []
    skipped = res.get("skipped") or []
    if skipped:
        logger.warning(f"有 {len(skipped)} 张图归不到子订单（已跳过）：{skipped[:3]}")

    out: Dict[str, Dict[str, str]] = {}
    for it in items:
        sub = str(it.get("sub") or "").strip()
        if not sub:
            continue
        out[sub] = {
            "url": to_jpeg_url(str(it.get("url") or "")),
            "price": str(it.get("price") or ""),
            "loaded": bool(it.get("loaded")),
        }
    unloaded = [s for s, v in out.items() if not v["loaded"]]
    logger.info(
        f"本页 {n_rows} 行，抓到 {len(out)} 个子订单的图"
        + (f"，其中 {len(unloaded)} 张尚未加载完" if unloaded else "")
    )
    return out


async def sweep_pages(
    page,
    on_page: Optional[Callable[[dict], None]] = None,
    max_pages: int = 200,
    known_order_nos: Optional[set] = None,
) -> Dict[str, Any]:
    """逐页：触发懒加载抓图 → 全选本页 → 翻页。返回 {images, pages, total, selected, ...}。

    勾选累积依赖页面的「跨页勾选」开关（默认开）。收尾会校验「已选订单」计数是否等于
    总条数，不等就抛异常交上层重试——少勾了就导不全，宁可整批重来。

    **但被 max_pages 截断时不校验**：那是「只跑前 N 页」的冒烟模式，已选本就少于总数，
    导出的也正是这批选中的单。否则只要 max_pages 小于实际页数就必然中止，这个参数等于死的。
    truncated=True 会一路传到汇总，让人一眼看出「这批不是全量」。

    known_order_nos 非空即【增量模式】：翻到「本页出现已登记订单号」就停（判据见
    should_stop_incremental），不再翻完全部页。早停同样跳过「已选==总数」校验——本就只
    选了前几页。

    触发早停的那一页【只勾水位之前那些更新的行】（`_orders_before_watermark` +
    `select_rows_by_order_no`），不再整页全选。2026-08-06 改：原先整页勾的理由是「订单号
    已登记不代表每个尺码都登记了，重叠旧单交给判重挡」，但实机发现这个理由站不住——判重键
    不够细时（只配订单号、或商品属性恒为 `Variant`）旧行会直接写进表里，用户看到的就是
    「只想采 5 条新单，却多出 14 条旧子订单」。导出集合的正确性不该外包给判重，源头只勾
    新单才对；同订单新增尺码那种情况由「按订单号勾整行」覆盖（命中的订单其所有子订单行
    一起勾）。
    增量早停与 truncated 语义相反（那个是「可能漏」，这个是「故意只取新的、不漏」），
    故用独立的 stopped_early 字段上报，绝不复用 truncated。
    """
    images: Dict[str, Dict[str, str]] = {}
    info = await read_pagination(page)
    total = info["total"]
    logger.info(f"待发货共 {total} 条，每页 {info['page_size']}，当前第 {info['page']} 页")

    known = set(known_order_nos or ())
    if known:
        logger.info(f"增量模式：已登记 {len(known)} 个订单号作水位，追上即停")

    pages = 0
    cur = info  # max_pages<=0 时循环体不执行，收尾仍要有分页快照可读
    seen_new = False          # 本批是否见过新单（早停的附加条件）
    stopped_early = False
    stop_reason = ""
    prev_created = ""         # 上一页的最早创建时间，用于跨页单调校验
    desc_broken = False       # 排序校验失败 → 本批退全量，不早停
    while pages < max_pages:
        pages += 1
        cur = await read_pagination(page)
        page_imgs = await grab_page_images(page)
        images.update(page_imgs)

        # 增量模式下先读本页订单号再决定怎么勾：若本页含水位（即将早停），只勾水位【之前】
        # 那些更新的行，不再整页全选。原先整页勾的后果是把水位之后更旧的单一并导出
        # （2026-08-06 实机：想要 5 条新单，却带上 14 条旧子订单），旧行能否被拦全看判重键
        # 够不够细——不该把导出集合的正确性外包给判重，源头只勾新单才对。
        page_orders = await read_page_orders(page) if known else []
        page_new = page_known = 0
        if known:
            page_known = sum(1 for o in page_orders if o["order_no"] in known)
            page_new = len(page_orders) - page_known
            if page_new:
                seen_new = True
            # 排序护栏（best-effort）：读不到创建时间就返回空串，降级到无护栏
            if not desc_broken:
                broken = check_created_desc(prev_created, page_orders)
                if broken:
                    desc_broken = True
                    logger.warning(f"{broken}——本批退全量 sweep，不做增量早停")
            times = [o["created_at"] for o in page_orders if o["created_at"]]
            prev_created = min(times) if times else prev_created

            if not desc_broken:
                stop, reason = should_stop_incremental(page_orders, known, seen_new)
                if stop:
                    stopped_early, stop_reason = True, reason
                elif reason:
                    # 只剩「本页读不到订单号」一种：本页不参与判断、继续翻下一页即可。
                    # 绝不因此置 desc_broken——那会让一次瞬时 DOM 读失败毒化整批增量。
                    logger.warning(f"{reason}（本页不参与增量判断，继续翻页）")

        # 早停页只勾水位之前那些更新的行；其余页照旧整页全选（本就全是新单）
        if stopped_early:
            fresh = _orders_before_watermark(page_orders, known)
            if fresh:
                hit = await select_rows_by_order_no(page, fresh)
                logger.info(
                    f"早停页只勾水位之前的 {len(fresh)} 个订单（实际勾中 {hit} 行），"
                    "水位及更旧的行不导出"
                )
            else:
                logger.info("早停页没有比水位更新的订单，本页不勾选")
        else:
            await select_all_on_page(page)
        sel = await selected_count(page)

        if on_page:
            # 回调可能是协程函数（service 层的 _emit 是 async），返回值是 awaitable 就 await。
            # 不这么做的话进度事件会变成「创建了协程但从没 await」，SSE 里一条页进度都收不到。
            ret = on_page({
                "page": cur["page"],
                "pages_done": pages,
                "images": len(images),
                "selected": sel,
                "total": total,
                "new_on_page": page_new,
                "known_on_page": page_known,
            })
            if inspect.isawaitable(ret):
                await ret
        if stopped_early:
            logger.info(f"增量早停于第 {cur['page']} 页（共翻 {pages} 页）：{stop_reason}")
            break
        if not cur["has_next"]:
            break
        await goto_next_page(page)

    sel = await selected_count(page)
    # 还有下一页却已停下 = 被 max_pages 截断（正常翻完的出口是 has_next 为假）。
    # 早停也会「还有下一页就停」，但那是故意的，不算截断，故要排除掉。
    truncated = (
        not stopped_early and pages >= max_pages and bool(cur.get("has_next"))
    )
    if truncated:
        logger.warning(
            f"只跑了前 {pages} 页（max_pages={max_pages}），已选 {sel}/{total} 条，"
            f"本批不是全量——跳过「已选==总数」校验"
        )
    elif stopped_early:
        logger.info(
            f"增量早停：已选 {sel}/{total} 条（只覆盖前 {pages} 页的新单）"
            f"——跳过「已选==总数」校验"
        )
    elif total and sel is not None and sel != total:
        raise RuntimeError(f"已选 {sel} 条 != 总数 {total} 条，勾选不全，不做导出")

    return {"images": images, "pages": pages, "total": total, "selected": sel,
            "truncated": truncated, "stopped_early": stopped_early,
            "stop_reason": stop_reason,
            # 排序校验触发过 → 本批实际走了全量，汇总里要能看出增量没生效
            "fell_back": bool(known) and desc_broken}


def _export_checkbox(page, text: str):
    """定位导出弹窗里某个分组的复选框，返回 (可点的 label, 读状态的 input)。

    **必须用「自身带该文案的 checkbox label」，绝不能在外层节点的后代里取 .first**：
    `label:has-text("收货信息")` 会连包着整组的外层 label 一起匹配上，在它后代里取第一个
    checkbox 未必是想点的那个，可能点到弹窗里别的开关——那属于静默错误（日志一切正常、
    导出内容却不对），比超时报错难查得多。
    beast-core 的结构是 `<label data-testid=beast-core-checkbox><input 隐形><span>文案</span></label>`，
    所以文案和 data-testid 本就在同一个 label 上，精确定位不需要任何猜测。
    """
    box = page.locator(f'{_EXPORT_CB_LABEL}:has-text("{text}")').first
    return box, box.locator('input[type="checkbox"]').first


async def _warn_if_export_scope_is_all(page) -> bool:
    """导出弹窗里若有「全部/已选」范围开关且选中了「全部」，告警。返回是否检出异常。

    只告警不抛错：这个开关的真实 DOM 未实测确认（本项目规矩是选择器必须实测，没实测的
    不硬编码成判据），万一文案不同就会误拦本来正常的批次。所以这里做「能查到就查，查到
    明显不对就大声喊」，把决定权留给人——比静默导出整页好，也比误中止安全。
    真要收紧成硬判据，得先在实机把这个控件的结构记进 docs（照 1688 选择器那套来）。
    """
    try:
        checked = page.locator(
            f'{_EXPORT_CB_LABEL}:has-text("全部"), label:has-text("导出全部")'
        ).first
        if await checked.count() == 0:
            return False
        cb = checked.locator('input[type="checkbox"], input[type="radio"]').first
        if await cb.count() and await cb.is_checked():
            logger.warning(
                "导出弹窗的范围似乎选中了「全部」而不是「已选订单」——本批可能导出整页而非"
                "勾选的那些单。请到页面确认导出范围（增量批次尤其要留意）。"
            )
            return True
    except Exception as e:
        logger.warning(f"检查导出范围时出错（忽略，不影响导出）：{e}")
    return False


async def _click_checkbox_label(page, label, timeout_ms: int = 5000) -> None:
    """点 beast-core 复选框的可见 label（隐形 input 点不动，见 _EXPORT_CB_LABEL）。

    timeout 压到 5s（Playwright 默认 30s）：这一步是可跳过的隐私收敛，点不动就该赶紧
    放弃继续导出，而不是让整批在这儿干等半分钟。
    """
    await label.click(timeout=timeout_ms)


async def trigger_export(page, out_dir: str, timeout_ms: int = 180000) -> str:
    """点「导出订单」→ 弹窗取消勾「收货信息」→ 「确认导出」→ 接住下载，返回落地路径。

    两个坑：
      - 未勾选订单时「导出订单」按钮是 disabled（class 带 BTN_disabled），点了毫无反应，
        很容易误判成流程走通。这里显式检查 disabled 并抛错。
      - **绝不能用 CDP `Page.setDownloadBehavior`**：它和 Playwright 的 expect_download
        抢同一个落盘路径，互相截断，产出 0 字节的「不是 zip 文件」。纯用 expect_download。
    """
    os.makedirs(out_dir, exist_ok=True)
    # 「导出订单」在底部操作栏，和分页条一样会被悬浮球盖住（见 _POINTER_OVERLAYS）
    await disable_pointer_overlays(page)

    btn = page.locator('button:has-text("导出订单"), [class*="BTN_"]:has-text("导出订单")').first
    if await btn.count() == 0:
        raise RuntimeError("找不到「导出订单」按钮")
    cls = await btn.get_attribute("class") or ""
    if "BTN_disabled" in cls or await btn.is_disabled():
        raise RuntimeError("「导出订单」按钮是 disabled 状态——说明一条订单都没勾上")
    await btn.click()
    await page.wait_for_timeout(1200)

    # 弹窗里取消「收货信息」（买家 PII）。后台会记住上次设置，故先读状态再决定点不点。
    # 点击务必打在 label 上：input 是 0×0+opacity:0 的隐形元素（见 _EXPORT_CB_LABEL）。
    #
    # 整段 best-effort：取消勾选是隐私收敛，它失败不该让【已经翻完页、勾好单】的一批白跑。
    # 2026-08-06 实机就是这样挂的——点隐形 input 超时 30s，异常一路抛到 service，整批中止，
    # 前面的翻页与勾选全部作废。下面的 warning 本来就说明这一步是「确认不了就提醒人核对」，
    # 但异常会绕过它，所以必须显式吞掉。
    excluded = False
    try:
        label, cb = _export_checkbox(page, _EXPORT_GROUP_EXCLUDE)
        if await label.count() and await cb.count():
            if await cb.is_checked():
                await _click_checkbox_label(page, label)
                await page.wait_for_timeout(300)
            excluded = not await cb.is_checked()
    except Exception as e:
        logger.warning(f"取消「{_EXPORT_GROUP_EXCLUDE}」勾选时出错（继续导出）：{e}")
    if not excluded:
        logger.warning(
            f"没能确认「{_EXPORT_GROUP_EXCLUDE}」已取消勾选——导出文件可能含买家隐私字段，"
            "落表前请人工核对导出列"
        )

    # 导出范围护栏：确认弹窗仍停在「已选订单」而不是「全部」。
    # 为什么要专门守这一条：上面那个 checkbox 一旦点错对象（见 _export_checkbox 的说明），
    # 最坏情况是把导出范围从「已选」翻成「全部」，于是勾了几单却导出整页——日志全绿、
    # 导出内容全错。这种静默错误代价很大（白跑几分钟且掩盖真因），宁可在这儿多查一道。
    await _warn_if_export_scope_is_all(page)

    confirm = page.locator('button:has-text("确认导出"), [class*="BTN_"]:has-text("确认导出")').first
    if await confirm.count() == 0:
        raise RuntimeError("找不到「确认导出」按钮（导出字段设置弹窗没弹出？）")

    # 弹窗是新挂的节点，悬浮球可能盖在它的页脚按钮上，点确认前再屏蔽一次
    await disable_pointer_overlays(page)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(out_dir, f"订单导出_{ts}.xlsx")
    async with page.expect_download(timeout=timeout_ms) as dl_info:
        await confirm.click()
    download = await dl_info.value
    await download.save_as(dst)

    size = os.path.getsize(dst) if os.path.exists(dst) else 0
    if size < 1024:
        raise RuntimeError(f"导出文件异常小（{size} 字节）：{dst}")
    logger.info(f"导出文件已落地：{dst}（{size} 字节）")
    return dst


def join_images(orders: List[OrderRow], images: Dict[str, Dict[str, str]]) -> Dict[str, int]:
    """按子订单号把 DOM 抓到的图 URL / 成交单价 join 到订单行上。返回命中统计。"""
    hit_img = hit_price = 0
    for o in orders:
        got = images.get(o.sub_order_no)
        if not got:
            continue
        if got.get("url"):
            o.image_url = got["url"]
            hit_img += 1
        if got.get("price"):
            o.deal_price = got["price"]
            hit_price += 1
    miss = [o.sub_order_no for o in orders if not o.image_url]
    if miss:
        logger.warning(f"{len(miss)} 条子订单没匹配到图：{miss[:5]}")
    return {"image": hit_img, "price": hit_price, "miss": len(miss)}


def download_images(orders: List[OrderRow], out_dir: str) -> Dict[str, int]:
    """下载主图到本地。单张失败只记 warning——图是辅助字段，不该拖垮整批入库。

    下载必须带浏览器头 + 重试（复用采集管线的 _download_main_image）：裸 requests 会被
    kwcdn 当 bot 拦，表现为连接重置（WinError 10054）或 403。
    """
    os.makedirs(out_dir, exist_ok=True)
    ok = fail = 0
    for o in orders:
        if not o.image_url:
            continue
        name = f"{o.sub_order_no or o.order_no}.jpg"
        dst = os.path.join(out_dir, name)
        try:
            if not os.path.exists(dst):
                _download_main_image(o.image_url, dst)
            o.image_path = dst
            ok += 1
        except Exception as e:
            logger.warning(f"子订单 {o.sub_order_no} 主图下载失败（该行不带图入库）：{e}")
            fail += 1
    return {"ok": ok, "fail": fail}
