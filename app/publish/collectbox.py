# -*- coding: utf-8 -*-
"""采集箱定时扫描：定时找出【已认领但还没编辑过】的商品，供发布页勾选后批量发布。

【扫的是采集箱 draft，不是待发布 offline】2026-08-27 用户澄清：offline（页面文案
「待发布」）里躺的是**已经编辑过**的商品，本功能要的是采集箱
（/web/popTemu/pageList/draft）里【认领了但还没编辑】的那批——它们才是待发布管线的
输入。两个 URL 与文案的对应关系反直觉，务必别搞反：
    /web/popTemu/pageList/draft    → 页面上叫【采集箱】，dxmState=draft   ← 本功能扫这个
    /web/popTemu/pageList/offline  → 页面上叫【待发布】，dxmState=offline （已编辑过的）
两个状态都支持（DXM_STATES）以便对照，但默认且真正有用的是 draft。

【「编辑过没有」只能逐个查详情，列表接口判不了】2026-08-27 实测走了一圈弯路，
这几个看似可用的判据全部证伪，别再试：
  - `updateId != 0`：**批量认领动作本身就会写它**。455 条 waitPublish 里 191 条
    updateId=2525332，但那批 updateTime 只比 createTime 大 107 秒、同一时刻整批出现、
    标题还是中文源标题——是认领而非人工编辑。
  - 主图转存到店小秘图库（wxalbum）：全量 506 条里只 8 条命中，且 51 条
    publishSuccess 里 0 条命中。
  - 标题已英化：51 条 publishSuccess 里 48 条标题仍是中文（Temu 源采来的本身就是
    中文译文标题），漏判率 94%。
  - `sizeTemplateId` / `productOrigin` / `productExtCode`：**在 pageList 响应里恒为空**，
    编辑阶段的产物压根不在列表接口里。列表页也没有任何批量完成度接口（抓包只有
    pageList + getOfflineCounts）。
故判据只能取自详情接口 `/api/popTemuProduct/edit.json?id=<rowid>`，逐个 rowid 查。
好消息是它很便宜：页面内 Promise.all 并发，60 个 rowid 实测 0.2 秒（4ms/个）。

【编辑进度判据与三档语义】edit.json 里这 6 项都是发布管线编辑阶段的产物（见
EDIT_MARKS），实测在对照组上干净分离——本地 publish-state 里 save=ok 的行 6/6 全中，
认领未动的行 0/6 全不中。全量一页 60 条的分布呈现出三档，正对应编辑进度：
    0/6            认领后完全没编辑          ← 本功能要的
    1/6 仅 dims    只有认领带来的包装尺寸，实质未编辑（也算「没编辑」，见 _edit_progress）
    3/6 产地+运输  编辑了一半（⑤⑫ 跑过，⑨⑩ 没到）——续跑的对象
    6/6            编辑完整
故清单默认只列「未编辑」，但把「编辑了一半」也标出来交用户决定（那批用「从阶段续跑」
最省事），而不是直接藏掉——藏掉会让人以为它们不存在。

【取数走接口，不抓 DOM】列表数据源是 POST /api/popTemuProduct/pageList.json
（**表单编码 body，换成 JSON 会 400**），响应 data.page.list 就是行数据，带 idStr
（即 rowid）/ productName / shopId / siteValue / sourceUrl / categoryPathName /
createTime / dxmOfflineState。抓 DOM 要等 Vue 渲染、要处理分页按钮、列序还会随平台
改版变（记忆 dianxiaomi-store-site-enumeration 记过筛选区与弹窗命名不一致的坑）。

【为什么单独一个模块，不塞进 shops.py 或 service.py】
  - shops.py 是「打开发布页时跑一次」的枚举，本模块是【常驻定时器】，生命周期完全不同：
    它要能开、能关、能报「上次扫到几条、下次几点扫」，还要在扫描期间与发布作业抢同一个
    CDP 页面时让路（见 _scan_lock 与 _tick 的说明）。
  - service.py 是 15 阶段编排，本模块一行发布逻辑都没有：只读列表、只给清单。扫到的行
    交回前端由用户勾选，真正发布仍走 /publish/batch，故这里没有任何写动作。

【站点名靠运行时对齐求解，不写死站点表】接口给的是 siteValue 数字（实测 1=美国、
10=哥伦比亚）。平台的 42 个站点在前端 48 个 chunk 里都搜不到枚举表（2026-08-27 实测），
Vue 实例也被生产构建剥了。故用「同一页 DOM 的站点列文本 ⨯ 接口的 siteValue」按 rowid
对齐来求映射，结果缓存在内存里越扫越全；求不到就原样显示数字。
【DOM 站点单元格是「站点名 + 类目路径」粘在一起的】（如「美国玩具与游戏 > 木偶…」），
故取站点名要按 categoryPathName 的首段去掉后缀，不能整格当站点名。

【来源平台四家都能走全流程】2026-08-27 实测这一页 200 条草稿的 sourceUrl 分布：
1688 124 条、拼多多 42（含 2 条老域名 mobile.yangkeduo.com）、Temu 30、亚马逊 4。
原先只认 1688，其余 76 条（38%）被标成「非 1688 源」、前端只填 rowid，而 rowid 模式
必须自带 product-info.json，于是它们在 GUI 上跑不通。现在按域名判平台并抽商品 ID
（见 _source_info 与 app/publish/sources/base），四个平台一律给出 offerId，前端据此
让它们走完整的 ①→⑮。认不出的域名仍归「未知源」、只填 rowid。

【best-effort 取向】扫描失败、映射求不到、缓存写不进，一律 logger.warning 吞掉并保留
上一次的结果：定时器是辅助设施，坏了不该影响用户手工填链接发布（本项目既定模式，
见 service.py 的 _emit 与 config.py 的 get_output_dir）。
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
                                  ensure_cdp_alive, eval_list_page)
# 只 import base（纯字符串判断），不 import sources 包本身——那会经 get_adapter 的
# 延迟导入链拉起 Playwright，而本模块判平台只是个正则
from app.publish.sources.base import platform_name, platform_of, source_id

# 两个列表状态：键是 dxmState 的取值，label 用【店小秘页面上的真实文案】。
# path 只留路径不留完整 URL，与 browser.py 的取向一致。
DXM_STATES = {
    "offline": {"label": "待发布", "path": "/web/popTemu/pageList/offline"},
    "draft": {"label": "采集箱", "path": "/web/popTemu/pageList/draft"},
}
DEFAULT_STATE = "draft"

LIST_API = "/api/popTemuProduct/pageList.json"
COUNTS_API = "/api/popTemuProduct/getOfflineCounts.json"
EDIT_API = "/api/popTemuProduct/edit.json"

# 扫描间隔：分钟。默认 30 分钟——采集箱是人工采集攒出来的，攒够一批的节奏是小时级，
# 扫太勤只是白占那个 CDP 页面（扫描期间发布作业要等锁）。下限 5 分钟挡住手改配置
# 时的荒谬值（每分钟扫一次会让发布作业频繁抢不到页面）。
INTERVAL_DEFAULT = 30
INTERVAL_MIN = 5
INTERVAL_MAX = 1440

# 单次扫描取多少条。None = 全量（翻页到 totalPage 为止），不再设上限。
# 历史上一度限 200 以避免把上千行塞进前端表格卡住浏览器；现已改为全量扫描。
SCAN_LIMIT = None
PAGE_SIZE = 50
# 翻页间隔（秒）：全量扫描连翻几十页，页间留间隔避免撞店小秘「系统繁忙」限流
# （见 browser.eval_list_page 的退避重试，那边兜偶发，这边压频率）。
PAGE_GAP = 0.5

# 编辑进度探测的并发批大小。实测 60 个并发 0.2 秒（4ms/个），瓶颈完全不在这里；
# 取 40 是给平台留余量——这是别人的生产接口，没必要为省 0.1 秒去压它。
EDIT_PROBE_BATCH = 40

# edit.json 里代表「编辑过」的 6 个标记，每个都对应发布管线的一个阶段。
# 【为什么要 6 个而不是挑一个最准的】它们呈阶段性出现（⑤⑫ 先、⑨⑩ 后），
# 数出命中几个就能区分「完全没编辑 / 编辑了一半 / 编辑完整」——这正是用户要筛的维度。
# 单挑一个只能得到是非，没法把「编辑了一半」这档单独标出来。
EDIT_MARKS = ("sizeCharts", "origin", "region2", "shipLimit", "dims", "price")

# 【dims 单独一档的理由】实测有 4/60 行只命中 dims（包装长宽高非 0）而其余全空。
# 包装尺寸是认领时按源数据带进来的，不是编辑产物，故只有它中不算编辑过。
# 其余 5 项任一非空都必须是人（或管线）在编辑页填过才会有。
_EDIT_MARKS_STRONG = tuple(m for m in EDIT_MARKS if m != "dims")

# 扫描结果与设置的落盘位置：与 shops.py 的站点缓存同目录（都是「店小秘那边的客观事实」）
CACHE_DIR = str(config.workspace_root / "publish-cache")
_SCAN_NAME = "collectbox-scan.json"

# 扫描与发布作业抢同一个 CDP 页面（发布是 15 阶段共用一个 page，见 browser.py 的会话
# 模型）。与发布作业的互斥由 service 侧的「作业跑着就跳过这一轮」实现（见 _tick 里的
# busy 判断），因为让扫描去抢正在填表单的页面会把用户的表单导航掉。
#
# 【2026-08-30 从模块私有锁改成 browser.PAGE_LOCK】新增了未认领清单扫描
# （app/publish/crawlbox.py），它也 new BrowserSession() 而 open() 是「挑同一个店小秘
# 页签复用」——两个扫描器各锁自己模块等于没锁，对方一个 navigate 就把页面换走，
# 本模块会静默读到另一个列表的 DOM。锁必须与「被争用的资源」同层，故提到 browser.py。
_scan_lock = PAGE_LOCK

# siteValue → 站点名。运行时按 DOM 对齐求解后填进来（见 _solve_site_names），
# 只增不减：某一轮 DOM 没渲染出来时保留上一次求到的名字。
_site_names: dict = {}


def _scan_path() -> str:
    return os.path.join(CACHE_DIR, _SCAN_NAME)


def list_url(state: str = DEFAULT_STATE) -> str:
    """某状态的列表页 URL。未知状态回落默认，不抛——它只用于导航取 cookie 上下文。"""
    meta = DXM_STATES.get(state) or DXM_STATES[DEFAULT_STATE]
    return DIANXIAOMI_HOST + meta["path"]


def state_label(state: str) -> str:
    meta = DXM_STATES.get(state) or {}
    return meta.get("label") or state


# ---- 扫描结果持久化 ----------------------------------------------------------
# 【为什么要落盘】用户重启项目后打开发布页，应该立刻看到上次扫到的清单（哪怕是旧的、
# 标着扫描时间），而不是空表格等 30 分钟。定时器默认关闭，不落盘就等于「关着就永远没
# 数据看」。

def load_scan() -> dict:
    """读上次扫描结果；缺失/损坏返回空壳（best-effort，绝不让发布页打不开）。"""
    empty = {"items": [], "scanned_at": "", "state": DEFAULT_STATE, "counts": {},
             "total": 0, "error": "", "platforms": {},
             "progress": {"none": 0, "partial": 0, "full": 0, "unknown": 0}}
    try:
        with open(_scan_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return empty
        empty.update(data)
        return empty
    except FileNotFoundError:
        return empty
    except Exception as e:
        logger.warning(f"采集箱扫描缓存损坏，当未扫过：{e}")
        return empty


def save_scan(result: dict) -> None:
    """落盘扫描结果（临时文件 + os.replace 原子替换，照 shops.py 的做法）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _scan_path()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"采集箱扫描结果写入失败（忽略）：{e}")


# ---- 列表取数 ----------------------------------------------------------------
# body 照抄页面自己发的表单串（2026-08-27 抓包所得），只改 dxmState / pageNo / pageSize。
# 【不要改成 JSON body】该接口是 application/x-www-form-urlencoded，换成 JSON 会
# 400；shopId=-1 表示不筛店铺（列出账号下全部店），site=0 表示不筛站点。
_JS_LIST = r"""(async (args) => {
  const [state, pageNo, pageSize] = args;
  const body = 'sortName=2&pageNo=' + pageNo + '&pageSize=' + pageSize
    + '&total=0&searchType=0&searchValue=&productSearchType=1&shopId=-1'
    + '&dxmState=' + state + '&site=0&fullCid=&sortValue=2&productType=';
  const r = await fetch('__LIST_API__', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: body,
  });
  if (!r.ok) return JSON.stringify({ok: false, status: r.status});
  const d = await r.json();
  if (d.code !== 0) return JSON.stringify({ok: false, code: d.code, msg: d.msg || ''});
  const page = (d.data || {}).page || {};
  const list = page.list || [];
  return JSON.stringify({ok: true, pageNo: page.pageNo, totalPage: page.totalPage,
    totalSize: page.totalSize,
    rows: list.map(it => ({
      rowid: it.idStr || String(it.id || ''),
      title: it.productName || '',
      shopId: String(it.shopId == null ? '' : it.shopId),
      siteValue: it.siteValue == null ? null : String(it.siteValue),
      sourceUrl: it.sourceUrl || '',
      cat: it.categoryPathName || '',
      createTime: it.createTime || null,
      updateTime: it.updateTime || null,
      offlineState: it.dxmOfflineState || '',
      img: String(it.materialImgUrl || it.mainImage || '').split('|')[0],
      variations: (it.variations || []).length,
      errMsg: it.errMsg ? String(it.errMsg).slice(0, 200) : '',
    }))});
})""".replace("__LIST_API__", LIST_API)

# 各状态的条数（页面顶部那排计数）。用来在 UI 上标「待发布 0 / 采集箱 503」，
# 让用户一眼看出东西到底在哪个列表里——这正是「采集箱」这个名字容易搞反的地方。
_JS_COUNTS = r"""(async () => {
  const body = 'sortName=2&searchType=0&searchValue=&productSearchType=1&shopId=-1'
    + '&dxmState=offline&site=0&fullCid=&sortValue=2&productType=';
  const r = await fetch('__COUNTS_API__', {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: body,
  });
  if (!r.ok) return JSON.stringify({ok: false, status: r.status});
  const d = await r.json();
  return JSON.stringify({ok: d.code === 0, counts: d.data || {}});
})()""".replace("__COUNTS_API__", COUNTS_API)

# 店铺名：shopMap 与 shops.py 同源。这里只要 id → name，故比那边精简。
_JS_SHOPMAP = r"""(async () => {
  try {
    const r = await fetch('/api/userIn.json', {credentials: 'include'});
    if (!r.ok) return JSON.stringify({ok: false, status: r.status});
    const j = await r.json();
    const m = (j.data && j.data.shopMap) || {};
    const out = {};
    Object.keys(m).forEach(k => {
      const s = m[k] || {};
      const id = String(s.id != null ? s.id : k);
      if (s.name) out[id] = String(s.name).trim();
    });
    return JSON.stringify({ok: true, shops: out});
  } catch (e) { return JSON.stringify({ok: false, err: String(e).slice(0, 200)}); }
})()"""

# 站点名对齐：读当前列表页 DOM 里每行的「站点」列文本，交 Python 与接口的 siteValue 配对。
# 按表头文本找列号而不是写死第 4 列——列序随平台改版变过（记忆里 Sheet 列序同理，
# 「绝不硬编码列号」是本项目既定约定）。
_JS_DOM_SITES = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 25; i++) {
    if (document.querySelector('tr[rowid]')) break;
    await sleep(400);
  }
  const heads = Array.from(document.querySelectorAll('thead th'))
    .map(th => (th.textContent || '').replace(/\s+/g, ' ').trim());
  const col = heads.indexOf('站点');
  if (col < 0) return JSON.stringify({ok: false, reason: 'no-site-column', heads: heads});
  const rows = Array.from(document.querySelectorAll('tr[rowid]')).map(tr => {
    const tds = Array.from(tr.querySelectorAll('td'));
    const td = tds[col];
    return {rowid: tr.getAttribute('rowid'),
            cell: td ? (td.textContent || '').replace(/\s+/g, ' ').trim() : ''};
  });
  return JSON.stringify({ok: true, rows: rows});
})()"""


def _site_name_from_cell(cell: str, cat: str) -> str:
    """从「站点名+类目路径」粘连的单元格里切出站点名。

    实测单元格形如「美国玩具与游戏 > 木偶、手偶 > 毛绒木偶」，而接口的
    categoryPathName 是「玩具与游戏/木偶、手偶/毛绒木偶」——两者类目部分同源、
    只是分隔符不同。故拿类目首段（「玩具与游戏」）在单元格里找位置，前面那截就是站点名。
    切不出来就返回空串交调用方放弃这一行（宁可显示数字，不要瞎猜出个站点名——
    站点选错会让认领阶段直接失败）。
    """
    cell = (cell or "").strip()
    if not cell:
        return ""
    first = (cat or "").split("/")[0].strip()
    if first:
        i = cell.find(first)
        if i > 0:
            return cell[:i].strip()
        if i == 0:
            return ""  # 单元格就是类目、没带站点名
    # 没有类目可对齐时退一步：站点名都是纯中文短词，取开头连续的中文里前 6 字以内那段。
    # 这里不做更激进的猜测，理由同上。
    m = re.match(r"^[一-龥]{2,6}(?=[一-龥]|$)", cell)
    return m.group(0) if m and len(cell) <= 8 else ""


def _solve_site_names(dom_rows: list, api_rows: list) -> dict:
    """按 rowid 对齐 DOM 站点列与接口 siteValue，求 {siteValue: 站点名}。

    只在【同一 siteValue 的所有样本都指向同一个名字】时才采信：DOM 与接口是两次独立
    取数，中间列表可能翻页/刷新导致行不一致，冲突时宁可不给映射。
    """
    by_row = {r.get("rowid"): r for r in api_rows if r.get("rowid")}
    votes: dict = {}
    for d in dom_rows or []:
        api = by_row.get(d.get("rowid"))
        if not api or api.get("siteValue") is None:
            continue
        name = _site_name_from_cell(d.get("cell") or "", api.get("cat") or "")
        if name:
            votes.setdefault(str(api["siteValue"]), set()).add(name)
    return {k: next(iter(v)) for k, v in votes.items() if len(v) == 1}


def _fmt_time(ms) -> str:
    """毫秒时间戳 → 本地时间字符串；空/非法值给空串（列表里显示为空即可）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return ""


def _source_info(source_url: str) -> dict:
    """从源链接判平台并抽商品 ID，返回 {platform, platformName, productId, offerId}。

    【2026-08-27 从只认 1688 改成四平台】原先这里叫 _offer_id，非 1688 源一律返回空串，
    前端据此把那些行标成「非 1688 源」并只填 rowid——而 rowid 模式必须自带
    product-info.json，于是这些行在 GUI 上压根跑不通（阶段④ 报「缺 product-info.json」）。
    实测采集箱 200 条里非 1688 源有 76 条（拼多多 42 / Temu 30 / 亚马逊 4），占 38%，
    全都是这么废掉的。

    现在四个平台都能走全流程（阶段① 按域名分派适配器，见 app/publish/sources/），
    故这里给出完整的平台信息交前端渲染与填链接。

    【offerId 键保留】前端与既有代码都按它判「能不能走全流程」。现在它对四个平台
    都有值（就是该平台的商品 ID），语义从「1688 offerId」放宽成「源商品 ID」；
    保留键名而不是改名，是为了不动前端已经写好的那套三态渲染逻辑的其余部分。

    平台认不出（域名不在白名单）时 platform 为空串、productId 为空串——前端照旧
    显示「未知源」且只填 rowid，与改动前对非 1688 源的处置一致，不会更差。
    """
    url = source_url or ""
    platform = platform_of(url)
    if not platform:
        return {"platform": "", "platformName": "", "productId": "", "offerId": ""}
    pid = source_id(url, platform)
    return {"platform": platform, "platformName": platform_name(platform),
            "productId": pid, "offerId": pid}


# ---- 编辑进度探测（逐个 rowid 查详情）----------------------------------------
# 【为什么在页面内 Promise.all 而不是 Python 侧 asyncio.gather】每次 evaluate 都是一次
# 跨进程往返（Python → CDP → 页面），逐个查 200 条就是 200 次往返；打包成一段 JS 只有
# 一次往返，页面内并发 fetch 全程在浏览器里跑。实测 60 个 0.2 秒。
#
# 【失败的行按「未知」处理，不按「未编辑」】详情接口偶发失败时若默认判成未编辑，
# 那行就会出现在待发布清单里被用户勾选、跑一遍 15 阶段，最后发现它早编辑完了。
# 反过来默认判成已编辑会让它从清单里消失，用户以为没采到。故第三种状态 unknown：
# 照样列出来但标明「查不到」，交用户自己决定。
_JS_EDIT_PROBE = r"""(async (ids) => {
  const num = v => (v == null ? 0 : Number(v)) || 0;
  const one = async (rowid) => {
    try {
      const r = await fetch('__EDIT_API__?id=' + encodeURIComponent(rowid),
                            {credentials: 'include'});
      if (!r.ok) return {rowid: rowid, ok: false, err: 'HTTP ' + r.status};
      const d = await r.json();
      if (d.code !== 0) return {rowid: rowid, ok: false, err: 'code ' + d.code};
      const p = (d.data || {}).product || d.data || {};
      const v0 = (p.variations || [])[0] || {};
      return {
        rowid: rowid, ok: true,
        // ⑨ 尺码表：JSON 串，空值可能是 null/""/"[]"，故按长度判
        sizeCharts: !!(p.sizeCharts && String(p.sizeCharts).length > 2),
        origin: !!(p.productOrigin && String(p.productOrigin).trim()),   // ⑤ 产地
        region2: num(p.region2Id) !== 0,                                 // ⑤ 产地省份
        shipLimit: num(p.shipmentLimitSecond) !== 0,                     // ⑫ 运输信息
        // ⑩ 变种信息：包装三维要同时非 0（缺一个就是没填全）
        dims: num(v0.length) !== 0 && num(v0.width) !== 0 && num(v0.height) !== 0,
        price: num(v0.suggestedPrice) !== 0,                             // ⑩ 建议售价
      };
    } catch (e) { return {rowid: rowid, ok: false, err: String(e).slice(0, 80)}; }
  };
  return JSON.stringify({rows: await Promise.all(ids.map(one))});
})""".replace("__EDIT_API__", EDIT_API)


def _edit_progress(probe: dict) -> dict:
    """把一行的 6 个标记折成 {"edited", "stage", "marks"}。

    stage 取值与语义（判据与分档依据见模块 docstring 那段）：
        none    完全没编辑（0 项，或只有 dims）——本功能要筛的就是这批
        partial 编辑了一半（⑤⑫ 类标记有、⑨⑩ 没到）——适合「从阶段续跑」
        full    编辑完整（强标记全中）
        unknown 详情接口没查到——照样列出但标明，绝不猜（理由见 _JS_EDIT_PROBE）
    """
    if not probe or not probe.get("ok"):
        return {"edited": None, "stage": "unknown",
                "marks": [], "probeError": (probe or {}).get("err") or "详情未取到"}
    marks = [m for m in EDIT_MARKS if probe.get(m)]
    strong = [m for m in _EDIT_MARKS_STRONG if probe.get(m)]
    if not strong:
        # 一个强标记都没有：dims 单独命中也算没编辑（包装尺寸是认领带来的）
        return {"edited": False, "stage": "none", "marks": marks}
    if len(strong) == len(_EDIT_MARKS_STRONG):
        return {"edited": True, "stage": "full", "marks": marks}
    return {"edited": True, "stage": "partial", "marks": marks}


async def probe_edited(session: BrowserSession, rowids: list) -> dict:
    """批量查 {rowid: {"edited", "stage", "marks"}}。best-effort：整批失败返回空字典。

    空字典让调用方把所有行标成 unknown（照样列出），而不是让整次扫描失败——
    清单本身（标题/店铺/站点）已经拿到了，因为查不到编辑状态就一条不给太亏。
    """
    out: dict = {}
    ids = [r for r in (rowids or []) if r]
    for i in range(0, len(ids), EDIT_PROBE_BATCH):
        batch = ids[i:i + EDIT_PROBE_BATCH]
        try:
            d = await session.eval_json(_JS_EDIT_PROBE, arg=batch, timeout=120)
        except Exception as e:
            logger.warning(f"编辑进度探测失败（这批 {len(batch)} 条标为未知）：{e}")
            continue
        for row in d.get("rows") or []:
            rid = row.get("rowid")
            if rid:
                out[rid] = _edit_progress(row)
    return out


async def scan_once(state: str = DEFAULT_STATE, limit: Optional[int] = SCAN_LIMIT,
                    session: BrowserSession = None, probe: bool = True) -> dict:
    """扫一次列表，返回 {"items", "scanned_at", "state", "counts", "total", "error"}。

    只读：只导航列表页 + 页面内 fetch 读接口（列表/计数/店铺/详情），不点任何按钮、
    不改筛选。session 给了就复用（发布作业内部想顺手扫一次时用），否则自建并在结束时关掉。

    probe=True 时逐个 rowid 查详情判编辑进度（每行多出 edited/stage/marks 三个字段）。
    这是本功能的核心——「已认领但没编辑过」判不出来的话，清单就只是列表页的复制品。
    关掉它只在单测里用（省掉一层 mock）。

    【为什么导航到列表页而不是任意店小秘页】接口要 cookie 上下文，任何同域页面都行；
    但停在列表页还有个附带好处——DOM 上就有站点列，可以顺手做 siteValue 名字对齐
    （见 _solve_site_names），省一次导航。
    """
    if state not in DXM_STATES:
        state = DEFAULT_STATE
    own = session is None
    result = {"items": [], "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "state": state, "counts": {}, "total": 0, "error": "", "platforms": {}}
    if own:
        if not await ensure_cdp_alive():
            result["error"] = "CDP 不可用（调试 Chrome 未启动）"
            return result
        session = BrowserSession()
    try:
        if own:
            await session.open()
        async with _scan_lock:
            await session.navigate(list_url(state))
            rows: list = []
            page_no = 1
            while limit is None or len(rows) < limit:
                page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
                d = await eval_list_page(
                    session, _JS_LIST, arg=[state, page_no, page_size])
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

            # 计数与店铺名：都是 best-effort 的补充信息，坏了不影响清单本身
            counts = {}
            try:
                c = await session.eval_json(_JS_COUNTS)
                if c.get("ok"):
                    counts = c.get("counts") or {}
            except Exception as e:
                logger.warning(f"采集箱计数读取失败（忽略）：{e}")
            shops = {}
            try:
                s = await session.eval_json(_JS_SHOPMAP)
                if s.get("ok"):
                    shops = s.get("shops") or {}
            except Exception as e:
                logger.warning(f"店铺名读取失败（列表将显示 shopId）：{e}")

            # 站点名对齐：DOM 只覆盖当前页（默认 50 条），求到的映射进内存越扫越全
            try:
                dom = await session.eval_json(_JS_DOM_SITES)
                if dom.get("ok"):
                    solved = _solve_site_names(dom.get("rows") or [], rows)
                    if solved:
                        _site_names.update(solved)
                        logger.info(f"站点名映射已更新：{solved}")
                else:
                    logger.warning(f"列表页没有「站点」列，站点名将显示数字：{dom.get('reason')}")
            except Exception as e:
                logger.warning(f"站点名对齐失败（将显示数字）：{e}")

            # 全量（limit=None）翻到 totalPage 为止；显式传 limit 时按 limit 截断。
            if limit is not None:
                rows = rows[:limit]

            # 编辑进度：本功能的核心筛选维度，逐个 rowid 查详情（见 probe_edited）。
            # 【必须在锁内】它也用同一个 CDP 页面，放到锁外会与下一轮扫描/发布作业抢页面。
            edited_map: dict = {}
            if probe:
                edited_map = await probe_edited(
                    session, [r.get("rowid") for r in rows])

        for r in rows:
            sv = r.get("siteValue")
            src = _source_info(r.get("sourceUrl") or "")
            prog = edited_map.get(r.get("rowid") or "") or {
                "edited": None, "stage": "unknown", "marks": []}
            r2 = {
                "rowid": r.get("rowid") or "",
                "title": r.get("title") or "",
                "shopId": r.get("shopId") or "",
                "shop": shops.get(r.get("shopId") or "", ""),
                "siteValue": sv or "",
                "site": _site_names.get(str(sv), "") if sv else "",
                "sourceUrl": r.get("sourceUrl") or "",
                # 来源四件套：platform 是英文键（前端按它分色/判能否全流程），
                # platformName 是中文显示名，productId/offerId 同值（见 _source_info）
                "platform": src["platform"],
                "platformName": src["platformName"],
                "productId": src["productId"],
                "offerId": src["offerId"],
                "cat": r.get("cat") or "",
                "createdAt": _fmt_time(r.get("createTime")),
                "updatedAt": _fmt_time(r.get("updateTime")),
                "offlineState": r.get("offlineState") or "",
                # 图片：接口的 materialImgUrl/mainImage 是 | 分隔的多图串，列表只要首图。
                # JS 侧已切过一次，这里再切是因为「取首图」是本模块对数据的要求，
                # 不该指望取数那段 JS 永远替我们做（换个取数路径就静默变成整串）。
                "img": (r.get("img") or "").split("|")[0],
                "variations": r.get("variations") or 0,
                "errMsg": r.get("errMsg") or "",
                # 编辑进度三件套：edited 是三态（False/True/None=未知），
                # stage ∈ none/partial/full/unknown，marks 是命中的标记名（供 tooltip 说明依据）
                "edited": prog.get("edited"),
                "stage": prog.get("stage"),
                "marks": prog.get("marks") or [],
                "probeError": prog.get("probeError") or "",
            }
            result["items"].append(r2)
        result["counts"] = counts
        # 编辑进度分档统计：UI 上要显示「未编辑 N / 编辑了一半 M / 已编辑 K」，
        # 因为用户真正关心的数字是「还有多少个没编辑的可发」，而不是采集箱总条数。
        result["progress"] = {
            k: sum(1 for it in result["items"] if it["stage"] == k)
            for k in ("none", "partial", "full", "unknown")
        }
        # 来源平台分布：UI 上按平台筛选要用，也让「这批草稿都是哪来的」一眼可见。
        # 认不出平台的行归到 "" 键（前端显示「未知源」）。
        platforms: dict = {}
        for it in result["items"]:
            platforms[it["platform"]] = platforms.get(it["platform"], 0) + 1
        result["platforms"] = platforms
        n = len(result["items"])
        pg = result["progress"]
        # 扫到 0 条是正常业务状态（都编辑过了 / 采集箱空了），故只记 info 不告警
        by_src = "，".join(f"{platform_name(k) if k else '未知源'} {v}"
                          for k, v in sorted(platforms.items(),
                                             key=lambda kv: -kv[1]))
        logger.info(f"采集箱扫描完成：{state_label(state)} {n} 条"
                    + (f"（共 {result['total']} 条）" if result["total"] > n else "")
                    + f" — 未编辑 {pg['none']}，编辑了一半 {pg['partial']}，"
                      f"已编辑 {pg['full']}，未知 {pg['unknown']}"
                    + (f" | 来源：{by_src}" if by_src else ""))
    except Exception as e:
        result["error"] = str(e)[:300]
        logger.warning(f"采集箱扫描失败（保留上次结果）：{e}")
    finally:
        if own:
            await session.close()
    return result


# ---- 设置（开关 / 间隔 / 扫哪个列表）------------------------------------------
# 【为什么存在后端 prefs 而不是 localStorage】定时器跑在服务端：开关状态必须是服务端的
# 事实，不能由某个浏览器标签页的本地存储决定。与之相对，发布页的「自动发布」开关记在
# localStorage 是对的——那是「本次点开始时用什么参数」，属于前端选择（见
# templates/publish.html 的 LS_PUBLISH 注释）。这两种取向的分界就是「谁在执行」。
#
# 【默认关闭】用户明确要求默认关。故 get_settings 里 enabled 只有显式存过 True 才为
# True——读不到、文件损坏、键缺失全都算关。定时器会真实占用 CDP 页面并在后台反复导航
# 用户的 Chrome，默认开着属于「用户没要求就替他动浏览器」。

def get_settings() -> dict:
    """定时扫描设置：{"enabled", "intervalMinutes", "state", "onlyUnedited"}。

    读 service 的 prefs 文件（与生图并发数同一份，见 service.save_prefs 的合并语义）。
    非法值一律回落默认并不报错：这是辅助设施的配置，坏了就按默认跑。

    【onlyUnedited 默认开】本功能的用途就是「找出还没编辑的去发布」，默认就该只列那批；
    关掉它则连「编辑了一半」「已编辑完」的行一起列出来（对照用）。它只影响 UI 呈现，
    扫描本身照样把整页都探一遍——探测成本是 4ms/条，为省这点去做条件探测只会让
    「切一下开关就要重扫」。
    """
    from app.publish import service  # 延迟导入：service 顶部已 import 本模块的兄弟模块

    prefs = service.load_prefs()
    raw = prefs.get("collectBoxScan")
    cfg = raw if isinstance(raw, dict) else {}
    try:
        interval = int(cfg.get("intervalMinutes"))
    except (TypeError, ValueError):
        interval = INTERVAL_DEFAULT
    interval = max(INTERVAL_MIN, min(INTERVAL_MAX, interval))
    state = cfg.get("state")
    if state not in DXM_STATES:
        state = DEFAULT_STATE
    return {"enabled": cfg.get("enabled") is True,
            "intervalMinutes": interval, "state": state,
            "onlyUnedited": cfg.get("onlyUnedited") is not False}


def set_settings(enabled=None, interval_minutes=None, state=None, only_unedited=None) -> dict:
    """改设置并落盘，返回生效后的完整设置。只改传了的项（None＝不动）。

    越界/未知值抛 ValueError 交路由转 400——这些是用户在页面上填错，
    不该静默改成别的值让人以为设成功了。
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
        if state not in DXM_STATES:
            raise ValueError(f"未知列表：{state}（可选 {'/'.join(DXM_STATES)}）")
        cur["state"] = state
    if only_unedited is not None:
        cur["onlyUnedited"] = bool(only_unedited)
    service.save_prefs({"collectBoxScan": cur})
    return cur


# ---- 定时器 ------------------------------------------------------------------
# 【为什么是 asyncio 任务而不是 CronCreate/APScheduler】扫描本身是 async（要用同一套
# CDP 会话原语），而 app.py 已经是 FastAPI 事件循环——起一个后台协程是最少的新东西，
# 不用引依赖、不用跨进程传状态。间隔改了也不必重启：循环每轮都重读设置（见 _loop）。
#
# 【与发布作业的互斥：跳过而不是排队】发布是 15 阶段共用一个页面的长流程，中途被扫描
# 导航走就等于把用户填了一半的表单丢掉（browser.py 会话模型那段讲的就是这件事）。
# 故作业跑着时这一轮直接跳过、下一轮再说——扫描迟 30 分钟毫无损失，导航掉一张表单要
# 重跑十几个阶段。判据由 app.py 注入（_is_busy），因为「有没有作业在跑」是 Web 层的
# 事实（publish_jobs 字典），本模块不该反向依赖 app.py。

_task = None            # 当前定时器任务
_state: dict = {"running": False, "last_error": "", "next_at": "", "skipped": 0}
_is_busy = None         # 由 app.py 注入的「发布作业是否在跑」判据


def set_busy_checker(fn) -> None:
    """注入「当前是否有发布作业在跑」的判据（app.py 启动时调）。"""
    global _is_busy
    _is_busy = fn


def status() -> dict:
    """定时器现状 + 上次扫描结果摘要，供发布页顶部那一行显示。

    【state 与 scannedState 是两件事，别合并】state 是定时器【将要】扫哪个列表（设置），
    scannedState 是手上这份清单【实际来自】哪个列表。手动点「立即扫描」可以扫另一个
    列表而不改设置，此时两者不同——合成一个字段会让清单标着「待发布」却列着采集箱的
    行（本模块头那段讲的「两个名字最容易搞反」，在这里会以另一种形式重现）。
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
        "progress": scan.get("progress") or {"none": 0, "partial": 0, "full": 0,
                                             "unknown": 0},
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
            logger.info("有发布作业在跑，本轮采集箱扫描跳过（避免抢占编辑页）")
            return
    cfg = get_settings()
    r = await scan_once(cfg["state"])
    _state["last_error"] = r.get("error") or ""
    # 【失败不覆盖上次结果】扫挂了（Chrome 没开、未登录）就保留旧清单，只把错误挂到
    # status 上提示。否则用户会看到清单凭空清空，还以为采集箱被清了。
    if r.get("error") and not r.get("items"):
        return
    save_scan(r)


async def _loop() -> None:
    """定时循环：每轮重读设置（间隔/列表改了立即生效），开关关掉就退出。"""
    logger.info("采集箱定时扫描已启动")
    try:
        while True:
            cfg = get_settings()
            if not cfg["enabled"]:
                logger.info("采集箱定时扫描开关已关闭，定时器退出")
                return
            try:
                await _tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 一轮炸了不该让定时器整体停摆
                _state["last_error"] = str(e)[:300]
                logger.warning(f"采集箱扫描本轮异常（下一轮继续）：{e}")
            wait_s = get_settings()["intervalMinutes"] * 60
            _state["next_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + wait_s))
            await asyncio.sleep(wait_s)
    except asyncio.CancelledError:
        logger.info("采集箱定时扫描已停止")
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
                         only_unedited=None) -> dict:
    """改设置并让定时器立刻跟上（开→起、关→停）。返回 status()。

    间隔、列表与「只列未编辑」的变更不需要重启定时器：_loop 每轮都重读设置，
    且 onlyUnedited 只影响前端呈现（扫描照样把整页都探一遍）。但开关必须当场生效——
    用户拨了开关却要等 30 分钟才开始扫，会以为开关坏了。
    """
    cfg = set_settings(enabled=enabled, interval_minutes=interval_minutes, state=state,
                       only_unedited=only_unedited)
    if cfg["enabled"]:
        start()
    else:
        await stop()
    return status()


def start_if_enabled() -> bool:
    """进程启动时调：设置里开着才起定时器。

    默认关闭（get_settings 的 enabled 只认显式 True），故全新环境什么都不会发生——
    这正是用户要的「默认关，进页面自己开」。
    """
    if get_settings()["enabled"]:
        return start()
    return False
