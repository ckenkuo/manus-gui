# -*- coding: utf-8 -*-
"""数据采集页定时扫描：定时找出【还没认领】的采集记录，供发布页勾选后走完整发布流程。

【与 collectbox.py 是两个不同的列表，务必别混】2026-08-30 用户澄清：采集箱里躺的是
**已经认领过**的商品（认领的产物就是店铺+站点+草稿 rowid），而本模块扫的是数据采集页
（/web/productCrawl/dataAcquisition）「未认领」标签下的采集记录——它们**还没有店铺、
没有站点、没有 rowid**，正因如此才要走「认领 → 编辑 → 发布」的完整流程。

    本模块        /api/crawl/list.json  state=no        未认领采集记录  主键 idStr
    collectbox    /api/popTemuProduct/pageList.json     已认领草稿      主键 rowid
                  dxmState=draft  → 页面文案「采集箱」
                  dxmState=offline → 页面文案「待发布」（已编辑过的）

两者的数据源、主键、可用字段、以及「填进任务框该填什么」全都不同（见下），故刻意分成
两个模块、两份缓存、两个定时器，而不是给 collectbox 加个 state 分支——那样会让
「这一行有没有店铺站点」变成运行时才知道的事，而前端渲染与填任务框的逻辑正是按这个分岔的。

【填任务框填纯源链接，不带 rowid】这是与采集箱清单最要紧的差别：
  - 采集箱的行已经是草稿，填「源链接|rowid」＝跑① 提炼、跳② 认领（再认领会凭空多一条草稿）
  - 本模块的行【还没认领】，必须填纯源链接，让阶段② 真实执行「采集 → 认领到店铺站点 →
    取 rowid」（见 app/publish/claim.py 的 collect_and_claim）。店铺/站点由用户在下方
    表单里选定——本模块给不出这两个值，它们本来就是认领动作的产物。
故前端那一列显示的是「待认领」，不是编辑进度：认领都还没发生，谈编辑进度没有意义。

【主键必须用 idStr，绝不能用 id】2026-08-30 实测 50/50 行的 `id` 与 `idStr` 全不相等
（如 id=173539496022768700 而 idStr=...701）——`id` 是 18 位雪花号，超出 JS 双精度整数
安全范围（2^53 ≈ 9.0e15，而这些 id 已到 1.7e17），响应一进 JSON.parse 尾数就被抹平。
故取数 JS 里只读 idStr。
好在本模块把它当「去重键 + 前端行标识」用，不参与任何接口调用（认领是按标题搜索点行，
见 claim.py），故精度损坏在这里不会造成误操作——但把它写死成 id 会让去重静默失效。

【取数走接口，不抓 DOM】数据源是 POST /api/crawl/list.json（**表单编码 body，
换成 JSON 会 400**，与 collectbox 那个接口同样的坑），响应 data.page.list 是行数据、
data.stat 直接给三个标签的计数（all/no/claimed），故计数不必另发 count.json。
body 里的筛选字段照抄页面自己发的（2026-08-30 抓包），只改 state/pageNo/pageSize：
    state=no|claimed（省略 state 即「全部」标签）、collectStatus=CLAIM_TAB、site=all
【「全部」标签不传 state】实测点「全部」时页面发的 body 里连 state 与 collectStatus
两个键都不出现，不是传 state=all；故本模块对 all 也走「整键省略」，不自造取值。

【为什么不做「编辑进度」那样的逐行探测】采集箱那边要逐个查 edit.json 才能判「编辑过
没有」（列表接口判不了，那一堆被证伪的判据记在 collectbox.py 的 docstring 里）。
本模块不需要：未认领是列表接口自己就分好的标签（state=no），`operate.canClaim` 也在
行数据里直接给出。少一次逐行探测，扫 200 条就是一次往返。

【采集失败的行会混在里面吗】2026-08-30 实测「未认领」标签这一页 60 行 collectStatus
全为 null、canClaim 全为 true；而「全部」标签同样取 60 行里有 3 行 canClaim=false
（对应 count.json 的 collectStatus.FAILED_TAB=3）。故不可认领的行确实存在、只是不在
未认领标签里——仍按 canClaim 标出来而不是藏掉，理由与 collectbox 对 unknown 的处置
一致：藏掉会让人以为它们不存在。

【「已认领」标签的行 canClaim 也是 true，不是 bug】实测已认领的行照样可认领——店小秘
允许把同一个采集记录再认领到别的店铺/站点（claim.py 的 _JS_TAB_CLAIMED 就是靠这条路
处理「商品已被认领过」的场合）。故 canClaim 判的是「能不能发起认领」，**不是**
「认领过没有」；「认领过没有」由标签本身（state=no/claimed）决定。

【与采集箱扫描共用同一个 CDP 页签，锁在 browser.PAGE_LOCK】两个扫描器都
new BrowserSession()，而 open() 是「挑一个店小秘页签复用」——各自锁自己模块等于没锁，
对方一个 navigate 就把页面换走（表现为读到另一个列表的数据，且完全静默）。

【best-effort 取向】扫描失败、计数读不到、缓存写不进，一律 logger.warning 吞掉并保留
上一次的结果：定时器是辅助设施，坏了不该影响用户手工填链接发布（本项目既定模式，
见 service.py 的 _emit 与 config.py 的 get_output_dir）。
"""
import asyncio
import json
import os
import time
from typing import Optional

from app.config import config
from app.logger import logger
from app.publish.browser import CRAWL_URL, PAGE_LOCK, BrowserSession, ensure_cdp_alive
# 只 import base（纯字符串判断），不 import sources 包本身——那会经 get_adapter 的
# 延迟导入链拉起 Playwright，而本模块判平台只是个正则（与 collectbox 同一取向）
from app.publish.sources.base import platform_name, platform_of, source_id

# 三个标签：键是接口 state 的取值，label 用【店小秘页面上的真实文案】。
# 【all 的 state 是 None 而不是 "all"】页面点「全部」时 body 里整个键都不出现
# （2026-08-30 抓包），故这里用 None 表达「省略该键」，取数 JS 据此拼 body。
CRAWL_STATES = {
    "no": {"label": "未认领", "param": "no"},
    "claimed": {"label": "已认领", "param": "claimed"},
    "all": {"label": "全部", "param": None},
}
DEFAULT_STATE = "no"

LIST_API = "/api/crawl/list.json"

# 扫描间隔：分钟。默认与采集箱一致（30），理由同样是「采集是人工攒出来的，攒够一批是
# 小时级节奏」；下限 5 分钟挡住手改配置的荒谬值（扫描要占用那个 CDP 页签）。
INTERVAL_DEFAULT = 30
INTERVAL_MIN = 5
INTERVAL_MAX = 1440

# 单次扫描取多少条 / 每页多少条。None = 全量（翻页到 totalPage 为止），不再设上限。
# 历史上一度限 200 以避免把上千行塞进前端表格卡住浏览器（与 collectbox 同取值同理由）；
# 现已改为全量扫描。
SCAN_LIMIT = None
PAGE_SIZE = 50

# 扫描结果落盘位置：与 collectbox 的扫描缓存同目录（都是「店小秘那边的客观事实」），
# 但**必须是另一个文件名**——两个列表的行结构不同，共用一份会互相覆盖。
CACHE_DIR = str(config.workspace_root / "publish-cache")
_SCAN_NAME = "crawlbox-scan.json"

# 与采集箱扫描共用页面锁（理由见模块 docstring 最后一段）
_scan_lock = PAGE_LOCK


def state_label(state: str) -> str:
    meta = CRAWL_STATES.get(state) or {}
    return meta.get("label") or state


def _scan_path() -> str:
    return os.path.join(CACHE_DIR, _SCAN_NAME)


# ---- 扫描结果持久化 ----------------------------------------------------------
# 【为什么要落盘】与采集箱同理：用户重启项目后打开发布页，应立刻看到上次扫到的清单
# （哪怕是旧的、标着扫描时间），而不是空表格等 30 分钟。定时器默认关闭，不落盘就等于
# 「关着就永远没数据看」。

def load_scan() -> dict:
    """读上次扫描结果；缺失/损坏返回空壳（best-effort，绝不让发布页打不开）。"""
    empty = {"items": [], "scanned_at": "", "state": DEFAULT_STATE, "counts": {},
             "total": 0, "error": "", "platforms": {},
             "claimable": {"yes": 0, "no": 0}}
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
        logger.warning(f"未认领清单扫描缓存损坏，当未扫过：{e}")
        return empty


def save_scan(result: dict) -> None:
    """落盘扫描结果（临时文件 + os.replace 原子替换，照 collectbox/shops.py 的做法）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _scan_path()
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"未认领清单扫描结果写入失败（忽略）：{e}")


# ---- 列表取数 ----------------------------------------------------------------
# body 照抄页面自己发的表单串（2026-08-30 抓包所得），只改 state / pageNo / pageSize。
# 【不要改成 JSON body】该接口是 application/x-www-form-urlencoded，换成 JSON 会 400
# （与 collectbox 那个 pageList.json 同样的坑）。
# 【state 为 null 时连 state 与 collectStatus 两个键一起省略】页面点「全部」标签时
# 发的 body 里就是这样，不是 state=all；照抄它而不自造取值。
# 【只读 idStr，不读 id】19 位雪花号超出 JS 安全整数范围，id 的尾数已被 JSON.parse
# 抹平（实测 50/50 全不等，见模块 docstring）。
# 【canClaim 从 operate 里解出来】它是个 JSON **字符串**（不是对象），要 parse 一次；
# parse 失败按 true 处理——那只是行内的一个提示标记，解不出不该让整行消失。
_JS_LIST = r"""(async (args) => {
  const [stateParam, pageNo, pageSize] = args;
  const st = stateParam === null || stateParam === undefined
    ? '' : ('&state=' + stateParam + '&collectStatus=CLAIM_TAB');
  const body = 'pageNo=' + pageNo + '&pageSize=' + pageSize + st
    + '&site=all&searchValue=&productSearchType=name&sortTime=&accountName='
    + '&commentType=&commentValue=&sourcePriceUsdMin=&sourcePriceUsdMax='
    + '&orderName=&orderValue=';
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
  const op = it => {
    try {
      const o = typeof it.operate === 'string' ? JSON.parse(it.operate) : (it.operate || {});
      return {claim: o.canClaim !== false, hint: o.operateHint || ''};
    } catch (e) { return {claim: true, hint: ''}; }
  };
  return JSON.stringify({ok: true, pageNo: page.pageNo, totalPage: page.totalPage,
    totalSize: page.totalSize, stat: data.stat || {},
    rows: list.map(it => {
      const o = op(it);
      return {
        // 主键只用 idStr：id 的尾数已被 JS 数字精度抹平（见模块 docstring）
        cid: it.idStr || '',
        title: it.name || '',
        sourceUrl: it.sourceUrl || '',
        sourceName: it.sourceName || '',
        // 源价：sourcePrice 是原币种价、sourcePriceUsd 是折算后的美元价。
        // 两个都带上——1688 是人民币、Temu 源本身就是美元，只看一个会误读一位数量级。
        price: it.sourcePrice == null ? '' : String(it.sourcePrice),
        priceUsd: it.sourcePriceUsd == null ? '' : String(it.sourcePriceUsd),
        currency: it.sourceCurrency || '',
        createTime: it.createTime || null,
        createName: it.createName || '',
        collectSource: it.collectSource || '',
        collectStatus: it.collectStatus || '',
        collectFailReason: it.collectFailReason
          ? String(it.collectFailReason).slice(0, 200) : '',
        canClaim: o.claim,
        operateHint: o.hint ? String(o.hint).slice(0, 120) : '',
        // 图：imgUrl/previewImgUrl 都是 | 分隔的多图串，列表只要首图
        img: String(it.previewImgUrl || it.imgUrl || '').split('|')[0],
        variations: (() => {
          try {
            const v = typeof it.variant === 'string' ? JSON.parse(it.variant) : it.variant;
            return Array.isArray(v) ? v.length : 0;
          } catch (e) { return 0; }
        })(),
      };
    })});
})""".replace("__LIST_API__", LIST_API)


def _fmt_time(ms) -> str:
    """毫秒时间戳 → 本地时间字符串；空/非法值给空串（列表里显示为空即可）。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return ""


def _source_info(source_url: str) -> dict:
    """从源链接判平台并抽商品 ID，返回 {platform, platformName, productId, offerId}。

    与 collectbox._source_info 同实现同理由（四平台都能走完整 ①→⑮，见
    app/publish/sources/）。这里刻意各写一份而不 import 那边：两个模块除了「都要判
    平台」之外没有任何共同语义，跨模块借一个私有函数会把两份清单的字段口径绑在一起，
    而它们本来就在朝不同方向长（那边有编辑进度、这里有可否认领）。

    【本模块对 offerId 的依赖比采集箱更强】采集箱的行认不出平台还能退回纯 rowid 续跑，
    本模块的行连 rowid 都还没有——认不出平台就意味着阶段① 提炼没有适配器可用，
    整行在 GUI 上跑不通。故前端要把这类行明确标成「不支持」而不只是「未知源」。
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
    """扫一次未认领列表，返回 {"items", "scanned_at", "state", "counts", "total", ...}。

    只读：只导航数据采集页 + 页面内 fetch 读列表接口，不点任何按钮、不改筛选、
    **不认领任何东西**。session 给了就复用，否则自建并在结束时关掉。

    【没有 probe 参数】采集箱那边要逐个 rowid 查 edit.json 判编辑进度，本模块不需要：
    「未认领」是列表接口自己分好的标签，canClaim 也在行数据里（见模块 docstring）。

    【计数直接取响应里的 data.stat】它就是页面三个标签上那组数字（all/no/claimed），
    故不必像 collectbox 那样另发一次 count.json——少一次请求少一个失败点。
    """
    if state not in CRAWL_STATES:
        state = DEFAULT_STATE
    own = session is None
    result = {"items": [], "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "state": state, "counts": {}, "total": 0, "error": "", "platforms": {},
              "claimable": {"yes": 0, "no": 0}}
    if own:
        if not await ensure_cdp_alive():
            result["error"] = "CDP 不可用（调试 Chrome 未启动）"
            return result
        session = BrowserSession()
    try:
        if own:
            await session.open()
        async with _scan_lock:
            await session.navigate(CRAWL_URL)
            param = (CRAWL_STATES.get(state) or {}).get("param")
            rows: list = []
            stat: dict = {}
            page_no = 1
            while limit is None or len(rows) < limit:
                page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
                d = await session.eval_json(
                    _JS_LIST, arg=[param, page_no, page_size])
                if not d.get("ok"):
                    raise RuntimeError(
                        f"列表接口失败：{d.get('status') or d.get('msg') or d}")
                rows.extend(d.get("rows") or [])
                stat = d.get("stat") or stat
                total_page = int(d.get("totalPage") or 1)
                result["total"] = int(d.get("totalSize") or len(rows))
                if page_no >= total_page or not d.get("rows"):
                    break
                page_no += 1

            # 全量（limit=None）翻到 totalPage 为止；显式传 limit 时按 limit 截断。
            if limit is not None:
                rows = rows[:limit]

        for r in rows:
            src = _source_info(r.get("sourceUrl") or "")
            result["items"].append({
                # 主键：采集记录 id（idStr）。**不是 rowid**——未认领的行还没有草稿，
                # rowid 是认领的产物。前端拿它做行标识与勾选键。
                "cid": r.get("cid") or "",
                "title": r.get("title") or "",
                "sourceUrl": r.get("sourceUrl") or "",
                # 来源四件套：与采集箱清单同口径（platform 英文键 / platformName 中文名 /
                # productId 与 offerId 同值），前端两张表可共用同一套徽章渲染。
                "platform": src["platform"],
                "platformName": src["platformName"],
                "productId": src["productId"],
                "offerId": src["offerId"],
                # sourceName 是店小秘自己标的来源（"1688"/"Temu"），与我们按域名判出的
                # platform 各存一份：两者不一致时说明域名白名单或平台标注有一方过时了，
                # 留着好排查（前端只显示 platformName，这个字段仅作取证）。
                "sourceName": r.get("sourceName") or "",
                "price": r.get("price") or "",
                "priceUsd": r.get("priceUsd") or "",
                "currency": r.get("currency") or "",
                "createdAt": _fmt_time(r.get("createTime")),
                "createName": r.get("createName") or "",
                "collectSource": r.get("collectSource") or "",
                "collectStatus": r.get("collectStatus") or "",
                "collectFailReason": r.get("collectFailReason") or "",
                # 可否认领：来自行数据的 operate.canClaim。false 的行照样列出但标灰
                # （藏掉会让人以为它们不存在，与 collectbox 对 unknown 的处置一致）。
                "canClaim": r.get("canClaim") is not False,
                "operateHint": r.get("operateHint") or "",
                "img": (r.get("img") or "").split("|")[0],
                "variations": r.get("variations") or 0,
            })
        # 三个标签的计数直接用接口给的 stat（页面标签上那组数字）
        result["counts"] = {k: stat.get(k) for k in ("all", "no", "claimed")
                            if stat.get(k) is not None}
        result["claimable"] = {
            "yes": sum(1 for it in result["items"] if it["canClaim"]),
            "no": sum(1 for it in result["items"] if not it["canClaim"]),
        }
        # 来源平台分布：认不出平台的行归到 "" 键（前端显示「不支持」——本模块的行
        # 连 rowid 都没有，认不出平台就跑不通阶段①，见 _source_info）
        platforms: dict = {}
        for it in result["items"]:
            platforms[it["platform"]] = platforms.get(it["platform"], 0) + 1
        result["platforms"] = platforms
        n = len(result["items"])
        by_src = "，".join(f"{platform_name(k) if k else '不支持的源'} {v}"
                          for k, v in sorted(platforms.items(), key=lambda kv: -kv[1]))
        # 扫到 0 条是正常业务状态（都认领完了），故只记 info 不告警
        logger.info(f"未认领清单扫描完成：{state_label(state)} {n} 条"
                    + (f"（共 {result['total']} 条）" if result["total"] > n else "")
                    + f" — 可认领 {result['claimable']['yes']}"
                    + (f"，不可认领 {result['claimable']['no']}"
                       if result["claimable"]["no"] else "")
                    + (f" | 来源：{by_src}" if by_src else ""))
    except Exception as e:
        result["error"] = str(e)[:300]
        logger.warning(f"未认领清单扫描失败（保留上次结果）：{e}")
    finally:
        if own:
            await session.close()
    return result


# ---- 设置（开关 / 间隔 / 扫哪个标签）------------------------------------------
# 【与采集箱的设置各存一份，键名不同】prefs 里 collectBoxScan 是采集箱扫描的，本模块用
# crawlBoxScan。两个定时器是独立设施：用户可能只想扫未认领（走完整流程）而不扫采集箱，
# 共用一份设置会让「开一个就开两个」。
#
# 【默认关闭】理由与采集箱同：定时器会真实占用 CDP 页签并在后台反复导航用户的 Chrome，
# 默认开着属于「用户没要求就替他动浏览器」。故 enabled 只有显式存过 True 才为 True。
_PREFS_KEY = "crawlBoxScan"


def get_settings() -> dict:
    """定时扫描设置：{"enabled", "intervalMinutes", "state", "onlyClaimable"}。

    读 service 的 prefs 文件（与采集箱扫描、生图并发数同一份，合并语义见
    service.save_prefs）。非法值一律回落默认并不报错：辅助设施的配置，坏了按默认跑。

    【onlyClaimable 默认开】本功能的用途是「找出还没认领的去走完整流程」，
    canClaim=false 的行点了也认领不了，默认就该只列可认领的那批。关掉则一起列出来对照
    （它只影响 UI 呈现，不用重扫——扫描本来就把整页都取回来了）。
    """
    from app.publish import service  # 延迟导入，与 collectbox 同一取向

    prefs = service.load_prefs()
    raw = prefs.get(_PREFS_KEY)
    cfg = raw if isinstance(raw, dict) else {}
    try:
        interval = int(cfg.get("intervalMinutes"))
    except (TypeError, ValueError):
        interval = INTERVAL_DEFAULT
    interval = max(INTERVAL_MIN, min(INTERVAL_MAX, interval))
    state = cfg.get("state")
    if state not in CRAWL_STATES:
        state = DEFAULT_STATE
    return {"enabled": cfg.get("enabled") is True,
            "intervalMinutes": interval, "state": state,
            "onlyClaimable": cfg.get("onlyClaimable") is not False}


def set_settings(enabled=None, interval_minutes=None, state=None,
                 only_claimable=None) -> dict:
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
        if state not in CRAWL_STATES:
            raise ValueError(f"未知标签：{state}（可选 {'/'.join(CRAWL_STATES)}）")
        cur["state"] = state
    if only_claimable is not None:
        cur["onlyClaimable"] = bool(only_claimable)
    service.save_prefs({_PREFS_KEY: cur})
    return cur


# ---- 定时器 ------------------------------------------------------------------
# 结构与 collectbox 的定时器一致（asyncio 任务 + 每轮重读设置 + 作业忙则跳过），
# 但**必须是各自独立的任务与状态**：两个列表的扫描间隔可以不同，且用户可能只开一个。
# 【两个定时器会不会互相抢页面】会，且正是靠 browser.PAGE_LOCK 串起来：谁先拿到锁谁先
# 扫，另一个在 async with 处等着，等到了再导航。扫描是秒级的，等一下无损失。

_task = None
_state: dict = {"running": False, "last_error": "", "next_at": "", "skipped": 0}
_is_busy = None


def set_busy_checker(fn) -> None:
    """注入「当前是否有发布作业在跑」的判据（app.py 启动时调）。"""
    global _is_busy
    _is_busy = fn


def status() -> dict:
    """定时器现状 + 上次扫描结果摘要，供发布页那一行显示。

    【state 与 scannedState 是两件事，别合并】理由同 collectbox.status：手动「立即扫描」
    可以扫另一个标签而不改设置，合成一个字段会让清单标着「未认领」却列着已认领的行。
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
        "claimable": scan.get("claimable") or {"yes": 0, "no": 0},
        "platforms": scan.get("platforms") or {},
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
            logger.info("有发布作业在跑，本轮未认领清单扫描跳过（避免抢占编辑页）")
            return
    cfg = get_settings()
    r = await scan_once(cfg["state"])
    _state["last_error"] = r.get("error") or ""
    # 【失败不覆盖上次结果】扫挂了（Chrome 没开、未登录）就保留旧清单，只把错误挂到
    # status 上提示。否则用户会看到清单凭空清空，还以为采集记录被清了。
    if r.get("error") and not r.get("items"):
        return
    save_scan(r)


async def _loop() -> None:
    """定时循环：每轮重读设置（间隔/标签改了立即生效），开关关掉就退出。"""
    logger.info("未认领清单定时扫描已启动")
    try:
        while True:
            cfg = get_settings()
            if not cfg["enabled"]:
                logger.info("未认领清单定时扫描开关已关闭，定时器退出")
                return
            try:
                await _tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 一轮炸了不该让定时器整体停摆
                _state["last_error"] = str(e)[:300]
                logger.warning(f"未认领清单扫描本轮异常（下一轮继续）：{e}")
            wait_s = get_settings()["intervalMinutes"] * 60
            _state["next_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + wait_s))
            await asyncio.sleep(wait_s)
    except asyncio.CancelledError:
        logger.info("未认领清单定时扫描已停止")
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
                         only_claimable=None) -> dict:
    """改设置并让定时器立刻跟上（开→起、关→停）。返回 status()。

    间隔、标签与「只列可认领」的变更不需要重启定时器：_loop 每轮都重读设置，
    且 onlyClaimable 只影响前端呈现。但开关必须当场生效——用户拨了开关却要等 30 分钟
    才开始扫，会以为开关坏了。
    """
    cfg = set_settings(enabled=enabled, interval_minutes=interval_minutes, state=state,
                       only_claimable=only_claimable)
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
