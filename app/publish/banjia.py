# -*- coding: utf-8 -*-
"""数据搬家清单扫描 + 批量认领到指定店铺站点。

【这是发布页的第三张来源清单，与前两张都不同】2026-09-01 新增。三张清单的关系：

    banjia.py（本模块）  /api/popTemuProduct/moveData/pageList.json  moveDataState=no
                         数据搬家池：**别人已在 Temu 上架过的成品**，可整条搬到自己店铺
                         主键 rowid（= idStr）  有 productId(SPU)  有源站点
    crawlbox.py          /api/crawl/list.json  state=no
                         自己采集但还没认领的采集记录  主键 idStr  没有店铺站点
    collectbox.py        /api/popTemuProduct/pageList.json  dxmState=draft
                         已认领的草稿  主键 rowid  有店铺站点

【为什么单独一个模块，不给 crawlbox 加个 state】三者的**列表接口完全不同**（不是同一个
接口的不同参数），行字段不同，且「认领」这个动作的 UI 路径也不同：crawlbox 是逐行点
「认领」开弹窗（见 claim.py），本模块是**勾多行 + 一次「批量认领」**——一次弹窗认领 N 条，
这才是本管线存在的理由（数据搬家池动辄上千条，逐条走 claim.py 那条路是不可行的）。

【页面是 vxe-table，不是 ant-table】2026-09-01 实测：行是 `.vxe-body--row`，行主键在
DOM 的 `rowid` 属性上（与接口 idStr 一致），单元格是 `.vxe-body--column`。
故取不到 `thead th`（那套选择器在这页上全是空手）。但行复选框本身是 **ant-checkbox
嵌在 vxe-cell 里**，即 `.col--checkbox label.ant-checkbox-wrapper`。

【行复选框与店铺复选框都必须真实鼠标点击】2026-09-01 实测，这是本模块最要紧的坑：
JS `label.click()` 对它们**完全无效**——不是「class 变了但 v-model 没变」那种半生效，
而是连 DOM 的 `ant-checkbox-wrapper-checked` 都不加。表现是点「批量认领」弹出 toast
「请至少选择一个产品」，或者弹窗里勾了店铺却认领 0 条。
必须走「打临时标记 → session.mouse_click → 回读 class 校验 → 不生效重试」，
与 claim.py 的 `_trusted_toggle` 同一套路（那边的结论是 v-model 不更新，这边更彻底）。
反过来，「批量认领」按钮、弹窗「确定」「关闭」用 JS click 有效——与 claim.py 一致。

【勾店铺后站点区才出现，且「美国」默认勾上】弹窗是**一层**不是两层：左侧店铺列表，
勾中某个店铺后，同一弹窗里才异步渲染出站点复选框区（42 个站点 + 欧盟站）。
「美国」会被平台自动勾上，要认领到别的站点**必须先取消它**，否则会一次认领到两个站点。
这与 claim.py 的认领弹窗行为一致，故取消逻辑照抄那边（_JS_TAG_OTHER_SITE 的思路）。

【弹窗里的两个底部开关刻意不动】「已认领至相同店铺的产品不再认领」与「eBay产品默认认领
到原站点」都保持平台默认（不勾）。前者勾上会静默跳过重复行——但本模块的判重口径是
banjiaShops（见 _claimed_shops），在扫描阶段就该把已认领的行标出来，而不是让平台在
提交时悄悄少认领几条、让「认领成功 N」与勾选数对不上还查不出原因。

【认领是不可逆写动作】认领会在目标店铺真实创建草稿商品（只能到采集箱手动删）。故：
  - 扫描（scan_once）是纯只读：只导航 + 页面内 fetch，不勾任何复选框、不开弹窗。
  - 认领（claim_batch）是显式调用，前端必须由用户勾行 + 选店铺站点后才发起。
  - 本模块**没有**定时自动认领：定时器只扫清单（与另两个模块一致）。
"""
import asyncio
import json
import os
import re
import time
from typing import Optional

from app.config import config
from app.logger import logger
from app.publish.browser import (DIANXIAOMI_HOST, PAGE_LOCK, BrowserSession,
                                 ensure_cdp_alive, eval_list_page, fill_js)
# 只 import base（纯字符串判断），不 import sources 包本身——那会经 get_adapter 的
# 延迟导入链拉起 Playwright，而本模块判平台只是个正则（与 crawlbox 同一取向）
from app.publish.sources.base import platform_name, platform_of, source_id

# 数据搬家页地址：Temu 半托管那一支。页面上左侧还有 Wish/速卖通/eBay 等平行入口，
# 它们各是一条 URL，本模块只管 popTemu（本项目的发布管线只发 Temu 半托管）。
BANJIA_URL = DIANXIAOMI_HOST + "/web/productCrawl/productBanjia/popTemu"

LIST_API = "/api/popTemuProduct/moveData/pageList.json"
# 三个标签的计数接口。【与 crawlbox 不同，这里必须单独发一次】crawlbox 的列表响应里
# 带 data.stat 直接给三个计数，本接口的 stateCountMap 实测**只给当前标签**那一个数，
# 故要三个数就得发这个 count 接口（页面自己也是这么发的，2026-09-01 抓包）。
#
# 【curMoveDataStateNum 必须传对，服务端拿它做减法】接口名里的 "OtherState" 就是字面
# 意思：它只真算 yes（已认领），另外两个数是用你传进来的「当前标签数量」推出来的。
# 2026-09-01 实测传 0 的后果：state=all&cur=0 得到 {all:0, no:-729, yes:729}——
# no 是个负数，all 是 0，全错且不报错。传对了（all&cur=1796）才得到 {1796,1067,729}。
# 故本模块的顺序是「先取列表拿 totalSize → 再把它当 cur 传给计数接口」，
# 不能像原先那样先发计数。
COUNTS_API = "/api/popTemuProduct/moveData/getMoveDataOtherStateCount.json"

# 三个标签：键是接口的 moveDataState 取值，label 用页面上的真实文案。
# 【与 crawlbox 的「全部」不同，这里 all 是真的传 moveDataState=all】实测页面点「全部」
# 时 body 里就是 all（不是整键省略），故不要照搬 crawlbox 那条「省略键」的结论。
BANJIA_STATES = {
    "no": {"label": "未认领", "param": "no"},
    "yes": {"label": "已认领", "param": "yes"},
    "all": {"label": "全部", "param": "all"},
}
DEFAULT_STATE = "no"

INTERVAL_DEFAULT = 30
INTERVAL_MIN = 5
INTERVAL_MAX = 1440

# 单次扫描取多少条。None = 全量（翻页到 totalPage 为止），不再设上限。
# 历史上一度限 300（数据搬家池实测 1796 条，比另两个清单大得多）；现已改为全量扫描。
SCAN_LIMIT = None
PAGE_SIZE = 50
# 翻页间隔（秒）：全量扫描要连翻几十页，页间留点间隔避免撞店小秘「系统繁忙」限流
# （见 browser.eval_list_page 的退避重试，那边兜偶发，这边压频率）。
PAGE_GAP = 0.5

CACHE_DIR = str(config.workspace_root / "publish-cache")
_SCAN_NAME = "banjia-scan.json"

# 与另两个扫描器共用同一把页面锁：三者都 new BrowserSession() 而 open() 是「挑同一个
# 店小秘页签复用」，各锁自己模块等于没锁（详见 browser.PAGE_LOCK 的注释）。
_scan_lock = PAGE_LOCK

# 认领弹窗的识别文本：店小秘不给弹窗加稳定 class/id，只能靠标题文本认。
# 实测标题是「选择店铺-认领到采集箱」（与 claim.py 那个「选择店铺」弹窗**不是同一个**：
# 那边是单条认领、这边是批量，但 DOM 结构与交互结论一致）。
MODAL_CLAIM = "选择店铺"
# 批量认领的结果弹窗。实测批量提交后不一定弹（成功时可能只有 toast），故读不到不算失败，
# 最终判据是重扫后 banjiaShops 里是否出现目标店铺（见 claim_batch 的说明）。
MODAL_RESULT = "认领到采集箱状态"


def state_label(state: str) -> str:
    return (BANJIA_STATES.get(state) or {}).get("label") or state


def _scan_path() -> str:
    return os.path.join(CACHE_DIR, _SCAN_NAME)


def load_scan() -> dict:
    """读上次扫描结果（进程重启后清单还在，页面一打开就有东西看）。

    best-effort：读不出、解析失败一律当空结果，不抛——这是缓存，坏了重扫一次就好。
    """
    try:
        with open(_scan_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"读数据搬家清单缓存失败（当空处理）：{e}")
    return {}


def save_scan(result: dict) -> None:
    """落盘扫描结果（best-effort，写不进只记 warning，不影响本次返回给前端的数据）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_scan_path(), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写数据搬家清单缓存失败（忽略）：{e}")


# ---- 取数：页面内 fetch 列表接口 ---------------------------------------------
# body 里的筛选字段照抄页面自己发的（2026-09-01 抓包），只改 moveDataState/pageNo/pageSize：
#   searchType=0 productSearchType=1 productStatus=-1 sortName=2 sortValue=2
#   productStateValue=%20（是个空格，别改成空串）productType= shopId=-1
#   platform=popTemu site=0
# 【表单编码，换成 JSON 会 400】与另两个列表接口同样的坑。
#
# 【判重看 banjiaShops，不是 moveDataState】行数据里 banjiaShops / banjiaShopMap 形如
# {"popTemu":[8746250]}，直接给出「已被搬到哪些 shopId」。moveDataState=no 的标签筛的是
# 「一个店都没搬过」，而实际需要的判据是「有没有搬到**我要的那个**店铺」——同一条商品
# 可以搬到多个店铺，已搬到 A 店的行在 yes 标签里，但对 B 店来说仍是可搬的。
# 故扫描一律带回 claimedShops，由调用方/前端按目标店铺判。
_JS_LIST = r"""(async (args) => {
  const [state, pageNo, pageSize] = args;
  const body = 'pageNo=' + pageNo + '&pageSize=' + pageSize
    + '&searchType=0&searchValue=&productSearchType=1&productStatus=-1'
    + '&sortName=2&sortValue=2&productStateValue=%20&productType='
    + '&shopId=-1&platform=popTemu&site=0&moveDataState=' + state;
  const r = await fetch('__LIST_API__', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: body,
  });
  if (!r.ok) return JSON.stringify({ok: false, status: r.status});
  const d = await r.json();
  if (d.code !== 0) return JSON.stringify({ok: false, code: d.code, msg: d.msg || ''});
  const data = d.data || {};
  const page = data.page || {};
  const list = page.list || [];
  // banjiaShops 实测是**对象**（{"popTemu":[shopId,...]}），但保险起见兼容字符串
  const shops = it => {
    const raw = it.banjiaShopMap || it.banjiaShops;
    try {
      const o = typeof raw === 'string' ? JSON.parse(raw) : (raw || {});
      const arr = o.popTemu || o.popTemuProduct || [];
      return Array.isArray(arr) ? arr.map(String) : [];
    } catch (e) { return []; }
  };
  return JSON.stringify({ok: true, pageNo: page.pageNo, totalPage: page.totalPage,
    totalSize: page.totalSize,
    rows: list.map(it => ({
      // 主键：idStr。【与 crawlbox 同一个坑】id 是 18 位雪花号，超出 JS 双精度安全整数
      // 范围（2^53），一进 JSON.parse 尾数就被抹平（实测 id=...750 而 idStr=...739）。
      // vxe-table 的行 rowid 属性用的也是 idStr，故两者能对上。
      rowid: it.idStr || '',
      // SPU ID：数据搬家池的行是**别人已上架的成品**，故有平台 productId。
      // 这是它与 crawlbox（自采记录，只有源链接）最本质的区别。
      spuId: it.productIdStr || (it.productId == null ? '' : String(it.productId)),
      title: it.productName || '',
      sourceUrl: it.sourceUrl || '',
      // 源站点数字码（1=美国、10=哥伦比亚，实测）。站点名靠 DOM 对齐求解，见 _solve_site_names
      siteValue: it.siteValue == null ? '' : String(it.siteValue),
      categoryPath: it.categoryPathName || '',
      // 已搬到哪些 shopId（判重的真正依据，见上方注释）
      claimedShops: shops(it),
      shopId: it.shopId == null ? '' : String(it.shopId),
      // 【价在 SPU 级只有美元区间，币种价要下钻到 SKU】2026-09-01 实测 SPU 级的
      // sourcePrice / sourcePriceUsd / sourceCurrency **恒为空**，有值的是
      // sourcePriceUsdMin / sourcePriceUsdMax（美元区间，同款时两者相等）。
      // 原币种价与币种在 variations[0].sourcePrice / .sourceCurrency 上。
      // 【区间取下限会写错价】记忆 collect-sku-level-rows 记过同类的坑（SPU 级价是
      // 区间串被截成下限），故这里 min/max 都带回去、由前端显示区间而不是自作主张取一头。
      priceUsdMin: it.sourcePriceUsdMin == null ? '' : String(it.sourcePriceUsdMin),
      priceUsdMax: it.sourcePriceUsdMax == null ? '' : String(it.sourcePriceUsdMax),
      price: (() => {
        const v = Array.isArray(it.variations) ? (it.variations[0] || {}) : {};
        return v.sourcePrice == null ? '' : String(v.sourcePrice);
      })(),
      currency: (() => {
        const v = Array.isArray(it.variations) ? (it.variations[0] || {}) : {};
        return v.sourceCurrency || '';
      })(),
      createTime: it.createTime || null,
      updateTime: it.updateTime || null,
      createName: it.createName || '',
      // 变种数：variations 是数组（不是字符串），长度即 SKU 数
      variations: Array.isArray(it.variations) ? it.variations.length : 0,
      // 【图在 materialImgUrl】2026-09-01 实测 mainImage / draftImgUrl / extraImages
      // 在本接口里**恒为空**（它们是编辑阶段的产物），有值的是 materialImgUrl。
      // 照搬 collectbox 的 mainImage 会让整列图全空且不报错。
      img: String(it.materialImgUrl || '').split('|')[0],
    }))});
})""".replace("__LIST_API__", LIST_API)

# 三个标签的计数。body 与列表接口同构，多两个必须传对的字段：
#   moveDataState        当前标签
#   curMoveDataStateNum  **当前标签的行数**（即列表接口刚返回的 totalSize）
# 【为什么必须传对】服务端只真算 yes，另外两个数是拿 cur 做减法推出来的（见 COUNTS_API
# 上方的实测记录：传 0 会得到 no=-729 这种负数且不报错）。故调用方必须先取列表、
# 拿到 totalSize 再调这段，不能先发计数。
_JS_COUNTS = r"""(async (args) => {
  const [state, cur] = args;
  const body = 'pageNo=1&pageSize=50&searchType=0&searchValue=&productSearchType=1'
    + '&productStatus=-1&sortName=2&sortValue=0&productState=-1&shopId=-1'
    + '&moveDataState=' + state + '&curMoveDataStateNum=' + cur;
  const r = await fetch('__COUNTS_API__', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: body,
  });
  if (!r.ok) return JSON.stringify({ok: false, status: r.status});
  const d = await r.json();
  if (d.code !== 0) return JSON.stringify({ok: false, code: d.code});
  const x = d.data || {};
  // 接口给的键是 all/no/yes（与页面三个标签一一对应）
  return JSON.stringify({ok: true, all: x.all, no: x.no, yes: x.yes});
})""".replace("__COUNTS_API__", COUNTS_API)

# 站点名求解：接口只给 siteValue 数字，页面 DOM 的站点列给中文名。
# 手法与 collectbox 同（平台的 42 个站点在前端 chunk 里搜不到枚举表，只能用「同一页
# DOM 的站点列文本 ⨯ 接口的 siteValue」按 rowid 对齐求映射，越扫越全），但
# **切法与那边相反，别照搬**：
#   collectbox 的站点单元格是「站点名 + 类目路径」粘在一起的（要按 categoryPathName 切）
#   本页的站点列是**独立单元格**，整格就是站点名（实测 cells[3] == '哥伦比亚'）
# 而且本接口的 categoryPathName 用 `/` 分隔（服装、鞋靴和珠宝饰品/女童时尚/…），
# DOM 那边显示成 ` > `——2026-09-01 第一版照搬了 collectbox 的后缀匹配，于是
# siteNames 恒为空、整列站点显示成数字 10，且完全不报错。
_JS_DOM_SITES = r"""(() => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const rows = Array.from(document.querySelectorAll('.vxe-body--row'));
  return JSON.stringify({rows: rows.map(r => {
    const tds = Array.from(r.querySelectorAll('td'));
    // 列序会随平台改版变，故整行单元格都带回去，由 Python 侧挑出站点格
    return {rowid: r.getAttribute('rowid') || '', cells: tds.map(td => t(td).slice(0, 120))};
  })});
})()"""

# siteValue → 站点名。运行时求解后填进来，只增不减（某轮 DOM 没渲染出来时保留上次的）。
_site_names: dict = {}


def _site_name_from_cells(cells: list) -> str:
    """从行的单元格里挑出站点名那一格。

    实测行的单元格是：['', '1688', '标题…SPU「店铺」', '哥伦比亚', 'SKC/价格串',
                      '创建：…更新：…', '认领 搬家记录']
    站点格的特征：短（站点名最长如「吉尔吉斯坦站」6 字）、纯中文、不含数字/分隔符。
    故按「长度 ≤ 8 且只含中文（可带尾字「站」）」筛，而不是按下标取——列序会随平台
    改版变（本项目一贯不硬编码列号，见记忆 per-sheet-header-driven-write）。

    筛不出返回空串，由调用方保留 siteValue 数字显示（不猜）。
    """
    for cell in cells:
        s = (cell or "").strip()
        if not s or len(s) > 8:
            continue
        # 纯中文（含「站」尾），排掉 '1688'、'认领 搬家记录'、价格串这类
        if re.fullmatch(r"[一-龥]+", s):
            # 排掉操作列的动作词：它们也是纯中文短词
            if s in ("认领", "搬家记录", "编辑", "发布", "更多", "删除"):
                continue
            return s
    return ""


def _solve_site_names(dom_rows: list, api_rows: list) -> dict:
    """按 rowid 对齐 DOM 行与接口行，求出 siteValue → 站点名。

    只增不减地并进模块级 _site_names：DOM 每次只渲染当前页 50 行，多扫几轮才凑齐全表。
    """
    by_id = {r.get("rowid"): r for r in dom_rows if r.get("rowid")}
    got = {}
    for row in api_rows:
        rid = row.get("rowid")
        sv = row.get("siteValue")
        if not rid or not sv or sv in _site_names:
            continue
        dom = by_id.get(rid)
        if not dom:
            continue
        name = _site_name_from_cells(dom.get("cells") or [])
        if name:
            got[sv] = name
    if got:
        _site_names.update(got)
        logger.info(f"数据搬家站点名求解到 {len(got)} 个：{got}")
    return dict(_site_names)


def _fmt_time(ms) -> str:
    """毫秒时间戳 → 本地时间字符串；空/非法值给空串。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return ""


def _source_info(source_url: str) -> dict:
    """从源链接判平台并抽商品 ID，返回 {platform, platformName, productId, offerId}。

    与 crawlbox / collectbox 各存一份的理由同那两处：三张清单除了「都要判平台」之外没有
    共同语义，跨模块借私有函数会把三份字段口径绑在一起，而它们在朝不同方向长。

    【本模块对 offerId 的依赖最弱】数据搬家的行自带平台 SPU ID（spuId），认领后直接
    就是能编辑的草稿；源链接只是「这东西原本从哪采的」这类取证信息。故认不出平台
    不影响本管线跑通，前端不必标「不支持」。
    """
    url = source_url or ""
    platform = platform_of(url)
    if not platform:
        return {"platform": "", "platformName": "", "productId": "", "offerId": ""}
    pid = source_id(url, platform)
    return {"platform": platform, "platformName": platform_name(platform),
            "productId": pid, "offerId": pid}


async def scan_once(state: str = DEFAULT_STATE, limit: Optional[int] = SCAN_LIMIT,
                    session: BrowserSession = None) -> dict:
    """扫一次数据搬家清单，返回 {"items", "scanned_at", "state", "counts", "total", ...}。

    只读：导航数据搬家页 + 页面内 fetch 读列表/计数接口，**不勾任何复选框、不开认领弹窗、
    不认领任何东西**。session 给了就复用，否则自建并在结束时关掉。

    【为什么要导航到页面而不是直接 fetch】接口要带登录 cookie 与同源 referer；本项目一贯
    的做法是「在目标页面里 fetch」（见另两个扫描模块）。顺带这一趟还能读 DOM 求站点名。
    """
    if state not in BANJIA_STATES:
        state = DEFAULT_STATE
    own = session is None
    result = {"items": [], "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "state": state, "counts": {}, "total": 0, "error": "",
              "platforms": {}, "siteNames": {}}
    if own:
        if not await ensure_cdp_alive():
            result["error"] = "CDP 不可用（调试 Chrome 未启动）"
            return result
        session = BrowserSession()
    try:
        if own:
            await session.open()
        async with _scan_lock:
            await session.navigate(BANJIA_URL)
            # 页面首屏是 Vue 异步渲染，导航完成（domcontentloaded）时表格常还没挂上。
            # 等表格出现再取数：接口 fetch 本身不依赖 DOM，但站点名求解要读 DOM。
            await session.wait_for(
                "JSON.stringify({ready: !!document.querySelector('.vxe-body--row')})",
                lambda d: d.get("ready"), timeout=40, interval=2)
            param = (BANJIA_STATES.get(state) or {}).get("param")
            rows: list = []
            page_no = 1
            while limit is None or len(rows) < limit:
                page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
                d = await eval_list_page(
                    session, _JS_LIST, arg=[param, page_no, page_size])
                if not d.get("ok"):
                    raise RuntimeError(
                        f"列表接口失败：{d.get('status') or d.get('msg') or d}")
                rows.extend(d.get("rows") or [])
                total_page = int(d.get("totalPage") or 1)
                result["total"] = int(d.get("totalSize") or len(rows))
                if page_no >= total_page or not d.get("rows"):
                    break
                page_no += 1
                await asyncio.sleep(PAGE_GAP)
            # 三个标签的计数单独发。**必须放在取列表之后**：cur 要传当前标签的真实行数
            # （= 刚拿到的 totalSize），服务端拿它做减法推另外两个数（见 COUNTS_API）。
            # 负数/0 这类明显不对的结果直接丢掉不写进 counts——宁可前端不显示计数，
            # 也不让「未认领 -729」这种数字糊到界面上。
            try:
                c = await session.eval_json(
                    _JS_COUNTS, arg=[param, int(result["total"] or 0)])
                if c.get("ok"):
                    got = {k: c.get(k) for k in ("all", "no", "yes")
                           if isinstance(c.get(k), int) and c.get(k) >= 0}
                    if got.get("all"):  # all=0 说明 cur 没传对，整组丢掉
                        result["counts"] = got
                    else:
                        logger.warning(f"数据搬家标签计数看着不对（丢弃）：{c}")
            except Exception as e:
                # best-effort：计数只是标签上那三个数字，取不到不该让整轮扫描失败
                logger.warning(f"数据搬家标签计数取不到（忽略）：{e}")
            # 站点名求解（读当前页 DOM，与接口行按 rowid 对齐）
            try:
                dom = await session.eval_json(_JS_DOM_SITES)
                result["siteNames"] = _solve_site_names(dom.get("rows") or [], rows)
            except Exception as e:
                logger.warning(f"数据搬家站点名求解失败（保留已知映射）：{e}")
                result["siteNames"] = dict(_site_names)

            # 全量（limit=None）翻到 totalPage 为止；显式传 limit 时按 limit 截断。
            if limit is not None:
                rows = rows[:limit]

        for r in rows:
            src = _source_info(r.get("sourceUrl") or "")
            sv = r.get("siteValue") or ""
            result["items"].append({
                # 主键：数据搬家记录的 rowid（= idStr）。前端拿它做行标识与勾选键，
                # 认领时也用它在页面上定位要勾的那一行（见 claim_batch）。
                "rowid": r.get("rowid") or "",
                # 平台 SPU ID：本清单独有（另两张清单的行还没有平台商品）
                "spuId": r.get("spuId") or "",
                "title": r.get("title") or "",
                "sourceUrl": r.get("sourceUrl") or "",
                "platform": src["platform"],
                "platformName": src["platformName"],
                "productId": src["productId"],
                "offerId": src["offerId"],
                "siteValue": sv,
                # 站点名求不到就原样显示数字（与 collectbox 同一取向：不写死站点表）
                "site": _site_names.get(sv, sv),
                "categoryPath": r.get("categoryPath") or "",
                # 已搬到哪些店铺（shopId 字符串数组）。前端按目标店铺判「这行要不要跳过」
                "claimedShops": r.get("claimedShops") or [],
                # 原币种价（SKU 级取来的，见 _JS_LIST）+ 美元价区间（SPU 级）。
                # 两个都给：1688 源是人民币、Temu 源本身就是美元，只看一个会误读数量级
                "price": r.get("price") or "",
                "currency": r.get("currency") or "",
                "priceUsdMin": r.get("priceUsdMin") or "",
                "priceUsdMax": r.get("priceUsdMax") or "",
                "createdAt": _fmt_time(r.get("createTime")),
                "updatedAt": _fmt_time(r.get("updateTime")),
                "createName": r.get("createName") or "",
                "variations": r.get("variations") or 0,
                "img": (r.get("img") or "").split("|")[0],
            })
        platforms: dict = {}
        for it in result["items"]:
            platforms[it["platform"]] = platforms.get(it["platform"], 0) + 1
        result["platforms"] = platforms
        n = len(result["items"])
        by_src = "，".join(f"{platform_name(k) if k else '未知源'} {v}"
                          for k, v in sorted(platforms.items(), key=lambda kv: -kv[1]))
        # 扫到 0 条是正常业务状态（都搬完了），故只记 info 不告警
        logger.info(f"数据搬家清单扫描完成：{state_label(state)} {n} 条"
                    + (f"（共 {result['total']} 条）" if result["total"] > n else "")
                    + (f" | 来源：{by_src}" if by_src else ""))
    except Exception as e:
        result["error"] = str(e)[:300]
        logger.warning(f"数据搬家清单扫描失败（保留上次结果）：{e}")
    finally:
        if own:
            await session.close()
    return result


# ---- 批量认领 ----------------------------------------------------------------
# 【本段全是写动作，会在目标店铺真实创建草稿】不可逆（草稿只能到采集箱手动删）。
#
# 交互序列（2026-09-01 实测）：
#   1. 导航数据搬家页 → 切到目标标签（未认领）
#   2. 逐个目标 rowid：在当前页找到行 → **真实鼠标点击**行复选框 → 回读校验
#      找不到就翻页（列表可能有上千条，目标不一定在第一页）
#   3. JS click「批量认领」按钮 → 弹窗出现
#   4. **真实鼠标点击**店铺复选框 → 等站点区渲染
#   5. 取消默认勾上的非目标站点（「美国」）→ 真实点击勾目标站点
#   6. JS click「确定」→ 读结果 → 关弹窗
#
# 步骤 2 与 4 的复选框必须真实点击（见模块 docstring）；3 与 6 的按钮 JS click 有效。

# 当前页所有行的 rowid + 勾选状态。用于「目标在不在这一页」与勾选后的回读校验。
_JS_ROWS = r"""(() => {
  const rows = Array.from(document.querySelectorAll('.vxe-body--row'));
  return JSON.stringify({rows: rows.map(r => ({
    rowid: r.getAttribute('rowid') || '',
    checked: !!r.querySelector('.col--checkbox label.ant-checkbox-wrapper-checked'),
  })), total: rows.length});
})()"""

# 给目标 rowid 那一行的复选框打临时标记，供 mouse_click 定位。
# 【打标前先清旧标记】与 claim.py 同一条实测教训：不清的话 DOM 上会同时存在多个标记，
# 选择器 .first 按 DOM 顺序取到的是**上一次**那个，于是「勾选未生效」且报错指不到根因。
_JS_TAG_ROW = r"""((rowid) => {
  document.querySelectorAll('[data-bj-row]').forEach(e => e.removeAttribute('data-bj-row'));
  const r = Array.from(document.querySelectorAll('.vxe-body--row'))
    .find(x => x.getAttribute('rowid') === rowid);
  if (!r) return {found: false};
  const lb = r.querySelector('.col--checkbox label.ant-checkbox-wrapper');
  if (!lb) return {found: false, reason: 'no-checkbox'};
  lb.setAttribute('data-bj-row', '1');
  lb.scrollIntoView({block: 'center'});
  return {found: true, checked: lb.className.includes('ant-checkbox-wrapper-checked')};
})"""

_JS_ROW_CHECKED = r"""((rowid) => {
  const r = Array.from(document.querySelectorAll('.vxe-body--row'))
    .find(x => x.getAttribute('rowid') === rowid);
  if (!r) return {checked: false, reason: 'row-gone'};
  return {checked: !!r.querySelector('.col--checkbox label.ant-checkbox-wrapper-checked')};
})"""

# 切标签：页面顶部的 ant-tabs（「全部(1796)」「未认领(1067)」「已认领(729)」）。
# 文案带计数，故用前缀匹配而不是全等。
_JS_TAB = r"""((label) => {
  const t = e => (e.textContent||'').replace(/\s+/g,' ').trim();
  const tabs = Array.from(document.querySelectorAll('.ant-tabs-tab'))
    .filter(e => e.offsetParent !== null);
  const tab = tabs.find(e => t(e).startsWith(label));
  if (!tab) return {clicked: false, avail: tabs.map(t)};
  const active = tab.className.includes('ant-tabs-tab-active');
  if (!active) tab.click();
  return {clicked: true, wasActive: active};
})"""

# 翻页：vxe-table 的分页器。用「下一页」按钮而不是填页码——填页码要触发 vxe-input 的
# 事件链（与本项目 Vue 表单填值同一类麻烦），而顺序翻页天然够用（认领是按批处理的）。
_JS_NEXT_PAGE = r"""(() => {
  const btn = document.querySelector('.vxe-pager--next-btn');
  if (!btn) return JSON.stringify({clicked: false, reason: 'no-pager'});
  if (btn.className.includes('is--disabled')) return JSON.stringify({clicked: false, reason: 'last-page'});
  btn.click();
  return JSON.stringify({clicked: true});
})()"""

_JS_PAGE_NO = r"""(() => {
  const el = document.querySelector('.vxe-pager--jump-count, .vxe-pager--jump');
  const cur = document.querySelector('.vxe-pager--jump-input, .vxe-input--inner');
  return JSON.stringify({page: cur ? (cur.value || '') : '', text: el ? (el.textContent||'').trim() : ''});
})()"""

# 「批量认领」按钮：JS click 有效（实测）。offsetHeight > 0 排掉隐藏的同名按钮。
_JS_BATCH_CLAIM = r"""(() => {
  const b = Array.from(document.querySelectorAll('button'))
    .find(b => b.offsetHeight > 0 && (b.textContent||'').trim() === '批量认领');
  if (!b) return JSON.stringify({clicked: false, reason: 'no-button'});
  b.click();
  return JSON.stringify({clicked: true});
})()"""

# ---- 认领弹窗内的操作 --------------------------------------------------------
# 【弹窗查找必须限定「可见」】offsetParent !== null 漏一处就会命中已关闭但未销毁的
# 幽灵弹窗（claim.py 踩过：点在隐藏元素上事件照样生效，于是勾到别的店铺）。
_JS_HEAD = r"""(() => {
  const _pick = (kw) => Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => m.offsetParent !== null && (m.textContent||'').includes(kw))[0];
"""

_JS_MODAL_OPEN = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({open: false});
  const boxes = Array.from(m.querySelectorAll('.shop-label-box'));
  return JSON.stringify({open: true, stores: boxes.map(b => (b.textContent||'').trim())});
})()"""

_JS_TAG_STORE = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-bj-store]').forEach(e => e.removeAttribute('data-bj-store'));
  const boxes = Array.from(m.querySelectorAll('.shop-label-box'));
  const target = boxes.find(b => (b.textContent||'').trim() === __STORE__);
  if (!target) return JSON.stringify({found: false,
                                      available: boxes.map(b => (b.textContent||'').trim())});
  const label = target.querySelector('label');
  if (!label) return JSON.stringify({found: false, reason: 'no-label'});
  label.setAttribute('data-bj-store', '1');
  label.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true,
                         checked: label.className.includes('ant-checkbox-wrapper-checked')});
})()"""

_JS_STORE_CHECKED = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({checked: false, reason: 'no-modal'});
  const target = Array.from(m.querySelectorAll('.shop-label-box'))
    .find(b => (b.textContent||'').trim() === __STORE__);
  if (!target) return JSON.stringify({checked: false, reason: 'store-not-found'});
  const label = target.querySelector('label');
  return JSON.stringify({checked: !!label
    && label.className.includes('ant-checkbox-wrapper-checked')});
})()"""

# 站点区：勾店铺后才异步渲染。排除三处才是站点复选框——
#   .shop-label-box（店铺项）、.ant-modal-footer（底部两个开关）、文案「全选」（站点区表头）
_JS_WAIT_SITES = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({ready: false, reason: 'no-modal'});
  const labels = Array.from(m.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'))
    .filter(l => (l.textContent||'').trim() && (l.textContent||'').trim() !== '全选');
  return JSON.stringify({ready: labels.length > 0,
    sites: labels.map(l => ({text: (l.textContent||'').trim(),
      checked: l.className.includes('ant-checkbox-wrapper-checked')}))});
})()"""

# 站点名归一：页面上「美国」与「美国站」两种写法都出现（实测同一弹窗里
# 前 10 个不带「站」、其后带），比对前统一去掉尾部的「站」。与 claim.py 同一处置。
_JS_TAG_SITE = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-bj-site]').forEach(e => e.removeAttribute('data-bj-site'));
  const norm = t => t.replace(/站$/, '');
  const want = norm(__SITE__);
  const labels = Array.from(m.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'))
    .filter(l => (l.textContent||'').trim() !== '全选');
  const target = labels.find(l => norm((l.textContent||'').trim()) === want);
  if (!target) return JSON.stringify({found: false, reason: 'site-not-found',
    available: labels.map(l => (l.textContent||'').trim())});
  target.setAttribute('data-bj-site', '1');
  target.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true,
    checked: target.className.includes('ant-checkbox-wrapper-checked')});
})()"""

_JS_SITE_CHECKED = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({checked: false, reason: 'no-modal'});
  const norm = t => t.replace(/站$/, '');
  const want = norm(__SITE__);
  const labels = Array.from(m.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'))
    .filter(l => (l.textContent||'').trim() !== '全选');
  const target = labels.find(l => norm((l.textContent||'').trim()) === want);
  if (!target) return JSON.stringify({checked: false, reason: 'site-not-found'});
  return JSON.stringify({checked: target.className.includes('ant-checkbox-wrapper-checked')});
})()"""

# 找第一个已勾选的【非目标】站点并打标，供真实点击取消。
# 【为什么必须取消】平台把「美国」自动勾上，不取消就会一次认领到两个站点，
# 于是采集箱里凭空多出一条美国站草稿（要手动删）。claim.py 同一处置。
_JS_TAG_OTHER_SITE = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({found: false, reason: 'no-modal'});
  document.querySelectorAll('[data-bj-uncheck]').forEach(e => e.removeAttribute('data-bj-uncheck'));
  const norm = t => t.replace(/站$/, '');
  const want = norm(__SITE__);
  const labels = Array.from(m.querySelectorAll('label.ant-checkbox-wrapper'))
    .filter(l => !l.closest('.shop-label-box') && !l.closest('.ant-modal-footer'))
    .filter(l => (l.textContent||'').trim() !== '全选');
  const target = labels.find(l => l.className.includes('ant-checkbox-wrapper-checked')
    && norm((l.textContent||'').trim()) !== want);
  if (!target) return JSON.stringify({found: false});
  target.setAttribute('data-bj-uncheck', '1');
  target.scrollIntoView({block: 'center'});
  return JSON.stringify({found: true, text: (target.textContent||'').trim()});
})()"""

# 「确定」是普通 <button>，JS click 有效（与弹窗内复选框相反）。
_JS_CONFIRM = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({found: false, reason: 'no-modal'});
  const btn = Array.from(m.querySelectorAll('button'))
    .find(b => (b.textContent||'').trim() === '确定');
  if (!btn) return JSON.stringify({found: false, reason: 'no-confirm-btn'});
  btn.click();
  return JSON.stringify({found: true});
})()"""

_JS_RESULT = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (!m) return JSON.stringify({finished: false});
  return JSON.stringify({finished: true,
    text: (m.textContent||'').trim().replace(/\s+/g, ' ').slice(0, 300)});
})()"""

_JS_CLOSE = _JS_HEAD + r"""
  const m = _pick(__MODAL__);
  if (m) {
    const btn = Array.from(m.querySelectorAll('button'))
      .find(b => /^(关闭|取消)$/.test((b.textContent||'').trim()));
    if (btn) { btn.click(); return JSON.stringify({clicked: true, via: 'btn'}); }
    const x = m.querySelector('.ant-modal-close, [aria-label="close"]');
    if (x) { x.click(); return JSON.stringify({clicked: true, via: 'x'}); }
  }
  return JSON.stringify({clicked: false});
})()"""


def _norm_site(site: str) -> str:
    """站点名归一：去掉尾部的「站」（页面上两种写法都出现，见 _JS_TAG_SITE）。"""
    return (site or "").strip().rstrip("站")


def _parse_counts(text: str) -> dict:
    """从结果弹窗文本抽出「认领成功 N / 失败 N / 跳过 N」的计数。

    【为什么必须解析成数字】「认领成功 0，认领失败 5」也含「成功」二字，靠子串判断会
    静默判成通过（claim.py 记过这个坑）。抽不到返回 None 交调用方按「读不到」处理，
    不冒充 0——批量认领的结果弹窗不一定弹，读不到 ≠ 失败。
    """
    out: dict = {"text": text}
    for key, pat in (("success", r"成功\D{0,4}(\d+)"),
                     ("failed", r"失败\D{0,4}(\d+)"),
                     ("skipped", r"跳过\D{0,4}(\d+)")):
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    return out


async def _trusted_toggle(session: BrowserSession, tag_js: str, verify_js: str,
                          selector: str, want: bool = True, retries: int = 3,
                          arg=None) -> bool:
    """打标 → 真实鼠标点击 → 回读 class 校验，未生效时重试。

    【为什么必须真实点击】数据搬家页的行复选框与认领弹窗的店铺/站点复选框都是
    ant-checkbox + Vue。JS `label.click()` 对它们**完全无效**（2026-09-01 实测：连
    DOM 的 checked class 都不加，不是「class 变了但 v-model 没变」那种半生效）。
    真实鼠标事件（Playwright locator.click 走 CDP Input.dispatchMouseEvent）才有效。
    这与编辑页按钮的结论相反（那里真实点击被吞、必须 JS click），不要统一。

    want=False 用于「取消勾选」（取消默认站点），此时校验条件反过来。
    """
    for attempt in range(1, retries + 1):
        tag = (await session.eval_json(tag_js, arg=arg) if arg is not None
               else await session.eval_json(tag_js))
        if not tag.get("found"):
            return False
        if bool(tag.get("checked")) == want:  # 已是目标态，不用再点
            return True
        r = await session.mouse_click(selector)
        if not r.get("ok"):
            logger.warning(f"真实点击失败（{attempt}/{retries}）：{r.get('err')}")
            await asyncio.sleep(1.5 * attempt)
            continue
        await asyncio.sleep(1)
        chk = (await session.eval_json(verify_js, arg=arg) if arg is not None
               else await session.eval_json(verify_js))
        if bool(chk.get("checked")) == want:
            return True
        logger.warning(f"勾选未生效（想要 checked={want}），重试 {attempt}/{retries}")
    return False


async def _check_rows(session: BrowserSession, rowids: list,
                      max_pages: int = 20) -> dict:
    """在列表里逐个勾选目标 rowid（真实鼠标点击），跨页查找。

    返回 {"checked": [勾上的 rowid], "missing": [翻完所有页都没找到的]}。

    【为什么要跨页】数据搬家池上千条，一页 50 行，用户勾的这一批不一定都在第一页。
    做法是「当前页能勾的先勾完 → 翻下一页 → 再勾剩下的」，直到勾完或翻到末页。
    【勾选状态跨页保留】vxe-table 的选中态由组件维护，翻页回来仍在（实测），
    故不必担心翻页把前面勾的冲掉。
    """
    want = [r for r in rowids if r]
    checked: list = []
    for page_i in range(1, max_pages + 1):
        cur = await session.eval_json(_JS_ROWS)
        have = {r.get("rowid") for r in (cur.get("rows") or [])}
        todo = [r for r in want if r not in checked and r in have]
        for rid in todo:
            ok = await _trusted_toggle(
                session, _JS_TAG_ROW, _JS_ROW_CHECKED,
                'label[data-bj-row="1"]', want=True, arg=rid)
            if ok:
                checked.append(rid)
            else:
                logger.warning(f"行 {rid} 勾选未生效，跳过（本批将少认领这一条）")
        if len(checked) >= len(want):
            break
        nxt = await session.eval_json(_JS_NEXT_PAGE)
        if not nxt.get("clicked"):
            # 末页或没有分页器：剩下的确实找不到
            break
        # 翻页后表格重渲染，等新行挂上（列表接口慢时要等几秒）
        await asyncio.sleep(3)
        logger.info(f"翻到第 {page_i + 1} 页继续找剩余 {len(want) - len(checked)} 条")
    missing = [r for r in want if r not in checked]
    if missing:
        logger.warning(f"有 {len(missing)} 条在列表里没找到（可能已被认领而离开「未认领」标签）："
                       f"{missing[:5]}")
    return {"checked": checked, "missing": missing}


async def _select_store_and_site(session: BrowserSession, store: str,
                                 site: str) -> dict:
    """在批量认领弹窗内勾店铺 + 站点（真实鼠标点击）。

    返回 {"storeOk", "siteOk", "unchecked": [被取消的其它站点]}。
    弹窗必须已打开（由调用方确保），这里不再检测——检测与操作分离，便于续跑。
    """
    # 店铺列表异步加载（店小秘这个接口偶发要 2~3 分钟，claim.py 记过）
    logger.info("等待店铺列表加载（店小秘接口慢时可能需 2~3 分钟）")
    data = await session.wait_for(
        fill_js(_JS_MODAL_OPEN, MODAL=MODAL_CLAIM),
        lambda d: d.get("stores"), timeout=180, interval=2)
    if not data.get("stores"):
        raise RuntimeError("认领弹窗店铺列表加载超时：店小秘接口可能异常，建议稍后重跑")

    tag = await session.eval_json(fill_js(_JS_TAG_STORE, MODAL=MODAL_CLAIM, STORE=store))
    if not tag.get("found"):
        raise RuntimeError(
            f"弹窗中未找到店铺「{store}」，可选店铺：{tag.get('available') or []}")
    ok = await _trusted_toggle(
        session,
        fill_js(_JS_TAG_STORE, MODAL=MODAL_CLAIM, STORE=store),
        fill_js(_JS_STORE_CHECKED, MODAL=MODAL_CLAIM, STORE=store),
        'label[data-bj-store="1"]')
    if not ok:
        raise RuntimeError(f"勾选店铺「{store}」未生效（重试 3 次后仍失败）")
    logger.info(f"已勾选店铺：{store}")
    await asyncio.sleep(2)

    # 站点区在勾店铺后才渲染
    data = await session.wait_for(
        fill_js(_JS_WAIT_SITES, MODAL=MODAL_CLAIM),
        lambda d: d.get("ready"), timeout=90, interval=2)
    if not data.get("ready"):
        raise RuntimeError("站点列表加载超时：店小秘接口可能异常，建议稍后重跑")

    # 取消平台默认勾上的非目标站点（实测「美国」会被自动勾上）
    unchecked: list = []
    for _ in range(10):  # 最多取消 10 个，防死循环
        tag = await session.eval_json(
            fill_js(_JS_TAG_OTHER_SITE, MODAL=MODAL_CLAIM, SITE=site))
        if not tag.get("found"):
            break
        r = await session.mouse_click('label[data-bj-uncheck="1"]')
        if not r.get("ok"):
            logger.warning(f"取消默认站点失败：{r.get('err')}")
            break
        other = tag.get("text") or "未知站点"
        unchecked.append(other)
        logger.info(f"已取消默认站点：{other}")
        await asyncio.sleep(1)

    tag = await session.eval_json(fill_js(_JS_TAG_SITE, MODAL=MODAL_CLAIM, SITE=site))
    if tag.get("reason") == "site-not-found":
        raise RuntimeError(
            f"弹窗中未找到站点「{site}」，可选站点：{(tag.get('available') or [])[:12]}")
    ok = await _trusted_toggle(
        session,
        fill_js(_JS_TAG_SITE, MODAL=MODAL_CLAIM, SITE=site),
        fill_js(_JS_SITE_CHECKED, MODAL=MODAL_CLAIM, SITE=site),
        'label[data-bj-site="1"]')
    if not ok:
        raise RuntimeError(f"勾选站点「{site}」未生效（重试 3 次后仍失败）")
    logger.info(f"已勾选站点：{site}")
    return {"storeOk": True, "siteOk": True, "unchecked": unchecked}


# 采集箱列表接口（回查新草稿用）。**与本模块的搬家列表是两个不同接口**，
# 这里刻意不 import collectbox 的 _JS_LIST：那个函数带着编辑进度探测等一整套语义，
# 而这里只要「按源链接找 rowid」这一件事，借过来会把两个模块的字段口径绑在一起。
_DRAFT_LIST_API = "/api/popTemuProduct/pageList.json"

# 按源链接 + 店铺筛采集箱草稿。只取前两页（100 条）——刚认领的草稿按创建时间倒序
# （sortName=2&sortValue=2）必然在最前面，翻更多页只是白等。
_JS_FIND_DRAFTS = r"""(async (args) => {
  const [urls, shopName, pages] = args;
  const want = new Set(urls);
  const hits = [];
  let scanned = 0;
  for (let p = 1; p <= pages; p++) {
    const body = 'sortName=2&pageNo=' + p + '&pageSize=50'
      + '&total=0&searchType=0&searchValue=&productSearchType=1&shopId=-1'
      + '&dxmState=draft&site=0&fullCid=&sortValue=2&productType=';
    const r = await fetch('__API__', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
      body: body,
    });
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const d = await r.json();
    if (d.code !== 0) return JSON.stringify({ok: false, code: d.code, msg: d.msg || ''});
    const page = (d.data || {}).page || {};
    const list = page.list || [];
    scanned += list.length;
    list.forEach(it => {
      const u = it.sourceUrl || '';
      if (!want.has(u)) return;
      hits.push({rowid: it.idStr || String(it.id || ''),
                 sourceUrl: u,
                 shopId: String(it.shopId == null ? '' : it.shopId),
                 siteValue: it.siteValue == null ? '' : String(it.siteValue),
                 title: it.productName || '',
                 createTime: it.createTime || null});
    });
    if (p >= (page.totalPage || 1) || !list.length) break;
  }
  return JSON.stringify({ok: true, scanned: scanned, hits: hits});
})""".replace("__API__", _DRAFT_LIST_API)


async def _find_new_drafts(session: BrowserSession, source_urls: list,
                           store: str, site: str, pages: int = 2,
                           attempts: int = 4, wait: float = 4.0) -> dict:
    """认领后按源链接回查采集箱里新产生的草稿 rowid。

    【为什么必须回查，不能沿用搬家记录的 rowid】两个 rowid 不是一回事：
    搬家列表的 rowid 是**公共池记录**的 id，认领后在我们店里新建的**草稿**是另一个 id。
    下一步 bulkattr 要操作的是草稿（它在采集箱列表页上），故必须换成草稿 rowid。

    【匹配键取 sourceUrl，不取标题】标题会被平台改写（认领时可能带上店铺后缀「「Pawly」」，
    实测采集箱标题与搬家标题不完全一致），而 sourceUrl 是原样带过来的同一个字段
    （两张清单的行都有它，实测同一条商品两边完全相等）。
    再叠加店铺过滤：同一个源链接可能已被认领到多个店铺，只要目标店那条。

    【要重试等待】认领提交后草稿落到列表有秒级延迟（claim.py 也记过这条：
    认领后直接查常查不到）。故轮询几轮，凑齐就提前返回。

    返回 {"rowids": [...], "matched": N, "wanted": N, "error": ""}。
    查不全不抛：认领本身已经成功了，这里只是便利功能（best-effort）。
    """
    want = [u for u in (source_urls or []) if u]
    if not want:
        return {"rowids": [], "matched": 0, "wanted": 0,
                "error": "没有可用的源链接，无法回查草稿"}
    shop_id = ""
    # 店铺名 → shopId：草稿行只给 shopId，故先拿一次店铺表来对。
    # 取不到就退化成「只按源链接匹配」（下面会说明风险并如实报出来）。
    try:
        from app.publish import shops as _shops

        for s in (await _shops.fetch_stores()).get("stores") or []:
            if (s.get("name") or "") == store:
                shop_id = str(s.get("id") or "")
                break
    except Exception as e:
        logger.warning(f"取店铺 id 失败，回查草稿将不按店铺过滤（忽略）：{e}")

    ns = _norm_site(site)
    best: dict = {}
    scanned = 0
    for attempt in range(1, attempts + 1):
        d = await session.eval_json(_JS_FIND_DRAFTS, arg=[want, store, pages])
        if not d.get("ok"):
            raise RuntimeError(f"采集箱列表接口失败：{d.get('status') or d.get('msg') or d}")
        scanned = d.get("scanned") or 0
        for h in d.get("hits") or []:
            # 按店铺过滤：同一源链接可能已认领到多个店铺，只要目标店那条。
            # shop_id 取不到时这一步跳过（宁可多给几条让用户核对，也不要漏）。
            if shop_id and h.get("shopId") != shop_id:
                continue
            # 站点也对一下（同店多站点时避免拿错行）。站点名映射不全时不强求——
            # siteValue 是数字，_site_names 可能还没求到这个站点。
            if ns and _site_names:
                got = _site_names.get(h.get("siteValue") or "")
                if got and _norm_site(got) != ns:
                    continue
            u = h.get("sourceUrl")
            # 同一源链接在目标店可能有多条（历史遗留），取 createTime 最大的那条
            # ——刚认领的必然是最新的。
            cur = best.get(u)
            if not cur or (h.get("createTime") or 0) > (cur.get("createTime") or 0):
                best[u] = h
        if len(best) >= len(want):
            break
        if attempt < attempts:
            logger.info(f"新草稿还没全部同步到采集箱（{len(best)}/{len(want)}），"
                        f"{wait}s 后重查（{attempt}/{attempts}）")
            await asyncio.sleep(wait)

    rowids = [best[u]["rowid"] for u in want if u in best and best[u].get("rowid")]
    err = ""
    if len(rowids) < len(want):
        err = (f"只回查到 {len(rowids)}/{len(want)} 条新草稿"
               f"（采集箱前 {pages} 页共 {scanned} 条里没找到其余的）；"
               f"剩下的可稍后在采集箱清单里手动勾选补填")
        logger.warning(err)
    else:
        logger.info(f"回查到 {len(rowids)} 条新草稿 rowid，可直接接着批量填三项")
    return {"rowids": rowids, "matched": len(rowids), "wanted": len(want), "error": err}


async def claim_batch(rowids: list, store: str, site: str,
                      state: str = DEFAULT_STATE,
                      session: BrowserSession = None,
                      on_log=None) -> dict:
    """把数据搬家清单里勾中的行批量认领到指定店铺站点。

    【不可逆写动作】认领会在目标店铺真实创建草稿商品（只能到采集箱手动删）。
    调用前请确认 rowids 是用户在 UI 上明确勾选的那批。

    返回 {"status": "ok", "requested": N, "checked": N, "missing": [...],
          "unchecked": [被取消的默认站点], "result": {"success": N, ...}}。

    【成功判据】优先看结果弹窗的「认领成功 N」计数；批量提交后弹窗不一定弹
    （成功时可能只有 toast），故读不到不算失败，而是提示用户重扫清单核对——
    重扫后目标行的 claimedShops 里会出现目标店铺的 shopId，那才是最终事实。
    明确读到「成功 0」时抛异常。
    """
    def _log(text: str) -> None:
        """把一行过程日志推给调用方（best-effort，坏了不影响认领）。"""
        logger.info(text)
        if on_log:
            try:
                on_log(text)
            except Exception as e:
                logger.warning(f"进度回调失败（忽略）：{e}")

    rowids = [str(r) for r in (rowids or []) if str(r).strip()]
    if not rowids:
        raise ValueError("没有要认领的行（rowids 为空）")
    if not (store or "").strip():
        raise ValueError("必须指定目标店铺")
    if not (site or "").strip():
        raise ValueError("必须指定目标站点（不指定会认领到平台默认的美国站）")
    if state not in BANJIA_STATES:
        state = DEFAULT_STATE

    own = session is None
    if own:
        if not await ensure_cdp_alive():
            raise RuntimeError("CDP 不可用（调试 Chrome 未启动或未登录店小秘）")
        session = BrowserSession()
    try:
        if own:
            await session.open()
        # 【认领全程持页面锁】与扫描共用 PAGE_LOCK：认领要在页面上勾行、开弹窗，
        # 中途被某轮定时扫描 navigate 走就全废了（还会静默认领错的行）。
        async with _scan_lock:
            r = await session.navigate(BANJIA_URL)
            if not r.get("ok"):
                raise RuntimeError(f"导航到数据搬家页失败：{r}")
            await session.wait_for(
                "JSON.stringify({ready: !!document.querySelector('.vxe-body--row')})",
                lambda d: d.get("ready"), timeout=40, interval=2)
            # 切到目标标签：默认「未认领」。切错标签会在列表里找不到目标行
            tab = await session.eval_json(_JS_TAB, arg=state_label(state))
            if not tab.get("clicked"):
                logger.warning(f"未找到「{state_label(state)}」标签（可选 {tab.get('avail')}），"
                               f"按当前标签继续")
            elif not tab.get("wasActive"):
                await asyncio.sleep(4)  # 切标签会重新请求列表

            _log(f"在数据搬家清单里勾选 {len(rowids)} 条")
            picked = await _check_rows(session, rowids)
            if not picked["checked"]:
                raise RuntimeError(
                    f"目标行一条都没勾上（共 {len(rowids)} 条）：它们可能已被认领而离开"
                    f"「{state_label(state)}」标签，请重扫清单后再试")

            b = await session.eval_json(_JS_BATCH_CLAIM)
            if not b.get("clicked"):
                raise RuntimeError(f"点击「批量认领」失败：{b}")
            await asyncio.sleep(2)

            sel = await _select_store_and_site(session, store, site)

            confirm = await session.eval_json(fill_js(_JS_CONFIRM, MODAL=MODAL_CLAIM))
            if not confirm.get("found"):
                raise RuntimeError(f"点击「确定」失败：{confirm}")
            await asyncio.sleep(3)

            res = await session.wait_for(
                fill_js(_JS_RESULT, MODAL=MODAL_RESULT),
                lambda d: d.get("finished"), timeout=40, interval=2)
            counts = _parse_counts(res.get("text") or "")
            if res.get("finished"):
                logger.info(f"批量认领结果：{counts.get('text', '')[:150]}")
                _log(f"成功 {counts.get('success')} 条"
                     + (f"，失败 {counts.get('failed')}" if counts.get('failed') else ""))
            else:
                # 读不到结果弹窗不当失败：批量认领成功时可能只弹 toast。
                # 最终判据是重扫后 claimedShops 里有没有目标店铺。
                logger.warning("未检测到认领结果弹窗，请重扫清单核对（以列表实际结果为准）")

            # 关弹窗（best-effort）。关不掉就清遮罩——遮罩留着会挡住后续所有点击
            try:
                c = await session.eval_json(fill_js(_JS_CLOSE, MODAL=MODAL_RESULT))
                if not c.get("clicked"):
                    await session.eval_json(fill_js(_JS_CLOSE, MODAL=MODAL_CLAIM))
                    await session.kill_stuck_modals()
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"关闭认领弹窗失败（忽略）：{e}")

            if counts.get("success") == 0:
                raise RuntimeError(
                    f"认领成功 0 条（失败 {counts.get('failed')} / "
                    f"跳过 {counts.get('skipped')}）：{counts.get('text', '')[:200]}")

            # 【回查新产生的草稿 rowid】认领的产物是采集箱里的新草稿，它的 rowid 与
            # 这里的搬家记录 rowid **不是一个东西**（后者是公共池记录，前者是我们店的草稿）。
            # 而下一步「批量填仓库/发货时效/运费模板」要的正是草稿 rowid，故在这里就把
            # 它查出来一并返回——让调用方不必再自己去猜「刚认领的是哪几条」。
            # 匹配键是 sourceUrl + 目标店铺，理由见 _find_new_drafts。
            # 源链接从落盘的扫描结果里取（那也正是 UI 渲染所用的那份），避免为了几个
            # sourceUrl 再发一轮列表请求。
            try:
                by_id = {it.get("rowid"): it
                         for it in (load_scan().get("items") or [])}
                src_urls = [(by_id.get(r) or {}).get("sourceUrl")
                            for r in picked["checked"]]
                src_urls = [u for u in src_urls if u]
                if not src_urls:
                    raise RuntimeError(
                        "扫描缓存里找不到这批行的源链接（缓存可能已被新一轮扫描覆盖）")
                _log("按源链接回查新草稿 rowid…")
                drafts = await _find_new_drafts(session, src_urls, store, site)
                if drafts.get("rowids"):
                    _log(f"回查到 {len(drafts['rowids'])} 条新草稿，"
                         f"接下来填属性")
            except Exception as e:
                # best-effort：查不到只是少了「接着批量填」的便利，认领本身已经成了。
                # 绝不能因为回查失败就让调用方以为认领失败（那会诱使人重跑、多出一批草稿）。
                logger.warning(f"回查新草稿 rowid 失败（认领已成功，忽略）：{e}")
                drafts = {"rowids": [], "matched": 0, "error": str(e)[:200]}

            return {"status": "ok", "store": store, "site": site,
                    "requested": len(rowids), "checked": len(picked["checked"]),
                    "missing": picked["missing"],
                    "unchecked": sel.get("unchecked") or [],
                    # 新草稿的 rowid：前端据此直接接着跑「批量填三项」，不必人工再扫一次
                    "draftRowids": drafts.get("rowids") or [],
                    "draftMatched": drafts.get("matched") or 0,
                    "draftError": drafts.get("error") or "",
                    "result": {"finished": bool(res.get("finished")), **counts}}
    finally:
        if own:
            await session.close()


# ---- 设置（开关 / 间隔 / 扫哪个标签）------------------------------------------
# 【第三份独立设置】prefs 里 collectBoxScan / crawlBoxScan 之外再加 banjiaScan。
# 三个定时器是独立设施：用户可能只想扫数据搬家而不扫另两个，共用设置会「开一个开三个」。
# 【默认关闭】理由同另两处：定时器会真实占用 CDP 页签并在后台反复导航用户的 Chrome。
_PREFS_KEY = "banjiaScan"


def get_settings() -> dict:
    """定时扫描设置：{"enabled", "intervalMinutes", "state", "hideClaimed"}。

    读 service 的 prefs 文件（与另两个扫描、生图并发数同一份）。非法值一律回落默认
    并不报错：辅助设施的配置，坏了按默认跑。

    【hideClaimed 默认关】2026-09-03 改的：原先默认开，理由是「本管线的用途是找还没
    搬到我店的商品去搬」。但那个判据是错的——`claimedShops` 只给 shopId，**不带站点**，
    而「同一个店铺、搬到不同站点」是完全合法的常规操作（数据搬家的核心用途就是跨站，
    见 claim_batch 的说明）。于是默认开会把「已搬到本店美国站、现在要搬到哥伦比亚站」
    的行一并藏掉，且看不出原因——用户发布完商品后在清单里找不到它们就是这么来的。
    现在默认关：宁可多显示几行让人自己判断，也不要用一个分不清站点的判据静默藏行。
    开关保留（想过滤同店同站的重复行时仍可手动打开），只影响 UI 呈现、不用重扫
    （扫描本来就把 claimedShops 一并带回来了）。
    """
    from app.publish import service  # 延迟导入，与另两个扫描模块同一取向

    prefs = service.load_prefs()
    raw = prefs.get(_PREFS_KEY)
    cfg = raw if isinstance(raw, dict) else {}
    try:
        interval = int(cfg.get("intervalMinutes"))
    except (TypeError, ValueError):
        interval = INTERVAL_DEFAULT
    interval = max(INTERVAL_MIN, min(INTERVAL_MAX, interval))
    state = cfg.get("state")
    if state not in BANJIA_STATES:
        state = DEFAULT_STATE
    return {"enabled": cfg.get("enabled") is True,
            "intervalMinutes": interval, "state": state,
            "hideClaimed": cfg.get("hideClaimed") is True}


def set_settings(enabled=None, interval_minutes=None, state=None,
                 hide_claimed=None) -> dict:
    """改设置并落盘，返回生效后的完整设置。只改传了的项（None＝不动）。

    越界/未知值抛 ValueError 交路由转 400——那是页面上填错了，不该静默改成别的值。
    """
    from app.publish import service

    cur = get_settings()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if interval_minutes is not None:
        try:
            n = int(interval_minutes)
        except (TypeError, ValueError):
            raise ValueError(f"扫描间隔必须是整数分钟，收到 {interval_minutes!r}")
        if n < INTERVAL_MIN or n > INTERVAL_MAX:
            raise ValueError(f"扫描间隔需在 {INTERVAL_MIN}~{INTERVAL_MAX} 分钟之间，收到 {n}")
        cur["intervalMinutes"] = n
    if state is not None:
        if state not in BANJIA_STATES:
            raise ValueError(f"未知标签：{state}（可选 {'/'.join(BANJIA_STATES)}）")
        cur["state"] = state
    if hide_claimed is not None:
        cur["hideClaimed"] = bool(hide_claimed)
    service.save_prefs({_PREFS_KEY: cur})
    return cur


# ---- 定时器 ------------------------------------------------------------------
# 结构与另两个定时器一致（asyncio 任务 + 每轮重读设置 + 作业忙则跳过），但必须是各自
# 独立的任务与状态：三个列表的扫描间隔可以不同，且用户可能只开一个。
# 三者会互相抢页面，正是靠 browser.PAGE_LOCK 串起来：谁先拿到锁谁先扫。
#
# 【定时器只扫，永不认领】认领是不可逆写动作，必须由用户在 UI 上勾选后显式发起。

_task = None
_state: dict = {"running": False, "last_error": "", "next_at": "", "skipped": 0}
_is_busy = None


def set_busy_checker(fn) -> None:
    """注入「当前是否有发布作业在跑」的判据（app.py 启动时调）。"""
    global _is_busy
    _is_busy = fn


def status() -> dict:
    """定时器现状 + 上次扫描结果摘要，供发布页那一行显示。

    【state 与 scannedState 是两件事，别合并】手动「立即扫描」可以扫另一个标签而不改
    设置，合成一个字段会让清单标着「未认领」却列着已认领的行（另两个模块同一处置）。
    """
    scan = load_scan()
    cfg = get_settings()
    scanned_state = scan.get("state") or cfg["state"]
    return {
        **cfg,
        "running": bool(_task and not _task.done()),
        "nextAt": _state.get("next_at") or "",
        "lastError": _state.get("last_error") or scan.get("error") or "",
        "skipped": _state.get("skipped") or 0,
        "scannedAt": scan.get("scanned_at") or "",
        "count": len(scan.get("items") or []),
        "total": scan.get("total") or 0,
        "counts": scan.get("counts") or {},
        "stateLabel": state_label(cfg["state"]),
        "scannedState": scanned_state,
        "scannedLabel": state_label(scanned_state),
        "platforms": scan.get("platforms") or {},
        "siteNames": scan.get("siteNames") or {},
    }


async def _tick() -> None:
    """跑一轮扫描：作业在跑就跳过；扫到结果就落盘覆盖上一次。"""
    if _is_busy is not None:
        try:
            busy = bool(_is_busy())
        except Exception as e:  # 判据自己坏了不该让定时器停摆
            logger.warning(f"发布作业忙判据异常，按不忙处理：{e}")
            busy = False
        if busy:
            _state["skipped"] = (_state.get("skipped") or 0) + 1
            logger.info("有发布作业在跑，本轮数据搬家清单扫描跳过（避免抢占编辑页）")
            return
    cfg = get_settings()
    r = await scan_once(cfg["state"])
    _state["last_error"] = r.get("error") or ""
    # 【失败不覆盖上次结果】扫挂了（Chrome 没开、未登录）就保留旧清单，只把错误挂到
    # status 上提示。否则用户会看到清单凭空清空，还以为池子空了。
    if r.get("error") and not r.get("items"):
        return
    save_scan(r)


async def _loop() -> None:
    """定时循环：每轮重读设置（间隔/标签改了立即生效），开关关掉就退出。"""
    logger.info("数据搬家清单定时扫描已启动")
    try:
        while True:
            cfg = get_settings()
            if not cfg["enabled"]:
                logger.info("数据搬家清单定时扫描开关已关闭，定时器退出")
                return
            try:
                await _tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 一轮炸了不该让定时器整体停摆
                _state["last_error"] = str(e)[:300]
                logger.warning(f"数据搬家清单扫描本轮异常（下一轮继续）：{e}")
            wait_s = get_settings()["intervalMinutes"] * 60
            _state["next_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + wait_s))
            await asyncio.sleep(wait_s)
    except asyncio.CancelledError:
        logger.info("数据搬家清单定时扫描已停止")
        raise
    finally:
        _state["next_at"] = ""


def start() -> bool:
    """启动定时器（幂等：已在跑就什么都不做）。返回是否新启动了一个。"""
    global _task
    if _task and not _task.done():
        return False
    _task = asyncio.ensure_future(_loop())
    return True


async def stop() -> None:
    """停掉定时器并等它真正结束（best-effort，取消异常吞掉）。"""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None


async def apply_settings(enabled=None, interval_minutes=None, state=None,
                         hide_claimed=None) -> dict:
    """改设置并让定时器立刻跟上（开→起、关→停）。返回 status()。

    间隔、标签与 hideClaimed 的变更不需要重启定时器：_loop 每轮都重读设置。
    但开关必须当场生效——用户拨了开关却要等 30 分钟才开始扫，会以为开关坏了。
    """
    cfg = set_settings(enabled=enabled, interval_minutes=interval_minutes,
                       state=state, hide_claimed=hide_claimed)
    if cfg["enabled"]:
        start()
    else:
        await stop()
    return status()


def start_if_enabled() -> bool:
    """进程启动时调：设置里开着才起定时器（默认关，故全新环境什么都不发生）。"""
    if get_settings()["enabled"]:
        return start()
    return False
