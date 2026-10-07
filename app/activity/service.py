"""活动管理服务层：把「关流量加速器 → 报活动 → 重开加速器」的逐 SPU 编排抽成
UI / CLI 共用入口，进度经【结构化回调】抛出——对标 app/collect/service.py。

为什么单独一层（与采集同理）：这是【确定性批处理作业】，不是给大模型 function-calling
用的 tool，也用不上 LangGraph。故不封 BaseTool，而抽成 service：CLI（activity_manage.py）
和 UI（app.py 的 /activity 接口）都调这里。进度以结构化事件（dict）经 on_progress 抛出，
UI 直接走 SSE 渲染。

判定逻辑：一个 SPU 可有多个货号行（合并单元格成本表，各自一套日常价/销售底价/毛利率）；
报名是 SPU 级（提报页勾选无法只报部分货号），门槛也按 SPU 级定——取【毛利率最低】的
货号，其 底价÷日常价 即该 SPU 能打的最低折扣，活动折扣率 ≥ 它才入选（精确比较不打折
到分，2026-09-29 用户改定；旧规则「全部货号申报价都达底价」被截断误差误杀过，见
plan_spu_activities）。一个 SPU 可报多个活动。商品实时采购，不读取库存、不按库存门槛
初筛；平台详情资格检查仍保留。无需 LLM 选活动。

最高优先级安全约束（真实商家账号、操作不可逆）：
- 默认【正式执行】：dry_run 默认 False，只有调用方显式要求才有 dry-run 只读计划
  （2026-09-30 用户改定，此前默认 dry-run）。不可逆动作仍靠前端二次确认 + live 档位把关。
- 云端价格无效时中止；无活动达到该 SPU 门槛（最低毛利货号可打折扣）时 skip_nomatch。
- 报名成功由本次提交、完整前后快照和最新有效报名记录共同确认，历史成功不补作本次成功。

事件契约（on_progress 收到的 dict，均含 "type"）：
    # live=True 批次的关键事件（exec_close/exec_log_verify/exec_reopen/exec_done）由
    # app/activity/history.py 的切面落 MySQL 的 activity_history 表（[activity_history]
    # 配置，best-effort）；历史摘要经 history.summarize 注入 product_plan/product_done/
    # scan_start 的 history 字段（无历史/未启用为 None）。
    {"type":"batch_start","total":int,"todo":int,"batch":int,"dry_run":bool}
    {"type":"product_start","index":int,"total":int,"spu":str,"name":str}
    # product_plan：一个 SPU 会发多条（每个折扣率达门槛的活动一条）；skus 是逐货号
    # 明细（权威价格），daily_price/sale/submit_price/cost 是单货号兼容字段（多货号置 None）。
    # history 是该 SPU×活动上次报名结论 {status,ok,submit_price,at}（无历史为 None）。
    {"type":"product_plan","spu":str,"activity":str,
                           "skus":[{"label":str,"daily":float,"sale":float,"submit_price":float}],
                           "sku_count":int,
                           "daily_price":float|None,"sale":float|None,
                           "cost":float|None,"submit_price":float|None,"within_floor":bool,
                           "stock":None,"min_stock":int|None,"stock_ok":None,"stock_policy":"on_demand",
                           "selected":bool,"reason":str,"history":dict|None}
    # product_done：last_accel_open_at/accel_open_within_24h 是历史上次成功开启加速器的
    # 时间（无历史为 None/False），供前端画「24h 内开启过，关闭可能撞锁定期」预警。
    {"type":"product_done","spu":str,"status":"done"|"skip_nofloor"|"skip_nomatch"|"fail",
                           "accel_closed":bool,"enrolled":bool,"submit_price":float|None,
                           "enrolled_activities":list,"accel_reopened":bool,"verified":bool,
                           "last_accel_open_at":str|None,"accel_open_within_24h":bool,"note":str}
    {"type":"batch_done","done":int,"skip":int,"fail":int,"dry_run":bool,"live":bool}
    {"type":"aborted","reason":str}
    {"type":"log","level":"info"|"warning"|"error","message":str}
    # 执行遍事件（仅 dry_run=False 时出现；live=False 半程可逆 / live=True 全程不可逆）：
    {"type":"exec_start","live":bool,"spu_count":int}
    {"type":"exec_accel_step","phase":"close"|"open","step":"checking"|"acting",
                              "spu":str,"state":str|None,"note":str}  # 流量可视化中间态
    {"type":"exec_close","spu":str,"ok":bool,"live":bool,"note":str}       # 关流量
    {"type":"exec_rpa_step","activity":str,"spu":str|None,
                            "step":"open_activity"|"input_spu"|"query"|"select_product"|
                                   "set_sessions"|"fill_price"|"fill_stock"|"submit",
                            "ok":bool,"note":str}
    {"type":"exec_activity_skip","activity":str,"spu":str,
                                "reason":"detail_ineligible","note":str}
    # exec_fill：over_ref=True 表示申报价超过提报页参考价（疑 Excel 日常价与前端实际售价不一致），
    #            已跳过未报名；ref_price 为读到的参考价上限。
    {"type":"exec_fill","activity":str,"spu":str,"ok":bool,"submit_price":float,
                        "over_ref":bool,"ref_price":float|None,"note":str}  # 提报页填价
    # exec_enroll：活动级汇总（2026-10-03 起活动内逐 SPU 开页+填完立即单独提交——提报页
    #            搜索会重渲结果表格丢勾选，统一提交必然丢单）；filled=填价成功的 SPU，
    #            submitted/clicked_submit=是否所有/任一已填 SPU 提交成功，最终成败看 exec_log_verify。
    {"type":"exec_enroll","activity":str,"ok":bool,"submitted":bool,"verified":bool,
                          "filled":list,"live":bool,"note":str}     # 单活动逐 SPU 提交汇总
    {"type":"exec_log_verify","activity":str,"spu":str,"ok":bool,
                              "status":str,"note":str}               # 报名记录页最终对账
    {"type":"exec_reopen","spu":str,"ok":bool,"live":bool,
                          "tier":"normal"|"advanced"|"super"|None,"throttled":bool,"note":str}  # 重开流量
    {"type":"exec_product_done","spu":str,"status":"done"|"skip"|"fail"|"preview",
                               "planned":int,"submitted":int,"ineligible":int,
                               "skipped":int,"accel_ok":bool,"note":str}
    # exec_done.failed：报名未成功的逐条明细 [{spu,activity,submit_price,ref_price,over_ref,reason}]，
    #                   供操作者核对（尤其 over_ref 需更新该商品 Excel 日常价）。
    {"type":"exec_done","closed":int,"reopened":int,"activities":int,"errors":list,
                        "failed":list,"skipped_cells":[[spu,activity]],"product_results":list}
    # 识别扫描（scan_activity_matrix，只读）：
    # scan_start 一次发全骨架（价格判定是本地算的、零成本），矩阵整体先出现，资格再逐格点亮。
    # cells 元素另带 history 字段（该格上次报名结论 dict|None，只进事件不进矩阵落盘文件）。
    {"type":"scan_start","day":str,"spu_total":int,"activity_total":int,"cached":int,"path":str,
                        "activities":list,"cells":list,"counts":dict}
    {"type":"scan_activity_start","activity":str,"index":int,"total":int,"probe_count":int}
    # activity_cell：逐格资格结论；verdict=probe_failed（fail-closed，eligible=None）不落盘，
    #               下次重扫会重探。
    {"type":"activity_cell","spu":str,"activity":str,"eligible":bool|None,
                           "verdict":"eligible"|"ineligible"|"probe_failed",
                           "from_cache":bool,"scanned_at":str,"note":str}
    {"type":"scan_done","day":str,"path":str,"eligible":int,"ineligible":int,"unknown":int,
                        "cached":int,"probed":int,"activities":int}
    # 运行控制（暂停可恢复；收尾阶段拒绝暂停）：
    {"type":"paused","scope":"planning"|"scan"|"close"|"enroll","note":str}
    {"type":"resumed"}
    {"type":"pause_refused","reason":"reopen_phase","note":str}
    {"type":"exec_cell_skip","spu":str,"activity":str,
                             "where":"queued","note":str}   # 执行中被操作者逐格跳过
"""
import asyncio
import re
import time
from typing import Optional

from playwright.async_api import async_playwright

from app.activity import history, pipeline, source
from app.activity.reconciliation import active_registration, assess_registration
# 复用采集 service 已实测的 CDP 护栏 / 进度回调 / LLM token 清零，避免重复实现。
from app.collect.service import CDP_URL, _emit, ensure_cdp_alive, reset_pipeline_llms
from app.config import PROJECT_ROOT, get_config_section
from app.error_report import attach
from app.logger import logger

# 单 SPU 超时护栏（秒）：阶段1 只有只读 + 1 次 LLM，给足余量即可。
ACTIVITY_PRODUCT_TIMEOUT = 180
LOG_VERIFY_TRIES = 3
# 复查间隔递增退避（第 N 次复查前睡 LOG_VERIFY_RETRY_DELAYS[min(N-1, 末位)]）：
# 批次末尾的对账常落在平台限流窗口里（响应慢→分页组件加载态长），固定 2s 会让三次复查
# 全撞同一窗口（2026-10-03 批次「total=78 只拉到 4/8 页 → 19 个成功报名误判 not_verified」
# 的教训）；递增拉开才有机会落到窗口外。
LOG_VERIFY_RETRY_DELAYS = (5, 15, 30)
# 全局默认毛利率红线（config.toml [activity] 缺失时兜底）。
_FALLBACK_MIN_MARGIN = 0.15


def _normalize_margin(m) -> Optional[float]:
    """毛利率归一化：输入 20 或 0.2 都当 20%（m = m/100 if m > 1 else m）。非数字返回 None。"""
    if m is None:
        return None
    try:
        v = float(m)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if v > 1 else v


def global_min_margin() -> float:
    """读统一配置源 [activity].min_margin 作全局默认；缺失/损坏兜底 0.15（best-effort）。

    统一源（app/config.py 的 get_config_section）配了 [config_store] 走 MySQL
    配置中心、否则读本地 config.toml（文件模式下即原先「第一个存在的文件」那条链，
    显式写的 min_margin 不会被 example 默认值盖掉）。
    """
    try:
        m = _normalize_margin(get_config_section("activity").get("min_margin"))
        if m is not None:
            return m
    except Exception as e:
        logger.warning(f"读 [activity].min_margin 失败（用兜底 0.15）：{e}")
    return _FALLBACK_MIN_MARGIN


def parse_spu_list(text: str, batch_margin: float) -> list[dict]:
    """解析 SPU 清单文本，返回 [{"spu": str, "margin": float}, ...]。

    - 支持多行 / 逗号 / 空白混合分隔。
    - 逐品覆盖毛利率语法 `9072868889:0.25`（也接受全角冒号 `：`）：该品单独用 25%，
      缺省回退本批 batch_margin。
    - 毛利率归一化同 §11：输入 20 或 0.2 都当 20%。
    - 按 spu 去重（先到先得），保留顺序；非数字 token 跳过。
    """
    bm = _normalize_margin(batch_margin)
    if bm is None:
        bm = global_min_margin()

    out: list[dict] = []
    seen: set = set()
    # 逗号/中文逗号/分号/空白/换行统一切分
    for tok in re.split(r"[,，;\s]+", text or ""):
        tok = tok.strip()
        if not tok:
            continue
        # 逐品覆盖：spu:margin（半角/全角冒号）
        parts = re.split(r"[:：]", tok, maxsplit=1)
        spu = parts[0].strip()
        if not spu or not spu.isdigit():
            continue
        margin = bm
        if len(parts) == 2:
            om = _normalize_margin(parts[1].strip())
            if om is not None:
                margin = om
        if spu in seen:
            continue
        seen.add(spu)
        out.append({"spu": spu, "margin": margin})
    return out


async def _connect_pages(cdp_url: str, region_label: str = "", need_flux: bool = True):
    """连 CDP、确认作业区域，并在【该区域的域名下】新建本批专用的流量和活动页。

    不复用已有页签：其筛选条件、弹窗、局部状态和生命周期都不受管线控制，甚至可能正被
    操作者关闭。两个页面均由本批创建并加入 owned_pages，调用方负责及时关闭；用户原本打开
    的页签只【读一次区域】，不修改、不点击、也不关闭。

    区域为什么必须先确认：顶栏区域切换换的是域名（全球 agentseller.temu.com / 美国
    agentseller-us.temu.com）。本管线自己新开页面，若用写死的全球域，就会把操作者选定的
    美国区悄悄换回全球区——报名/开加速器这类写操作会打在错误的一批商品上。

    region_label：UI 上选定的区域，**以它为准**——浏览器停在别的区域会先切过去再开工作页。
    为空则沿用浏览器当前区域；读不到区域抛 RegionUnconfirmed，绝不默认全球域。
    need_flux=False（识别扫描）：只开活动页——扫描只读活动列表与详情页资格，不碰流量页，
    少开一个页签就少一处和操作者抢页面的机会。
    """
    from app.temu_region import confirm_region_from_context, url_in_region

    pw = await async_playwright().start()
    browser = None
    owned_pages = []
    try:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError("CDP 浏览器没有可用 context")
        ctx = browser.contexts[0]
        region = await confirm_region_from_context(ctx, region_label)

        async def open_owned_page(label, path):
            url = url_in_region(path, region)
            page = await ctx.new_page()
            owned_pages.append(page)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await pipeline.dismiss_all_page_popups(page)
                logger.info(f"活动批次：自动打开{label}：{url}")
                return page
            except Exception as exc:
                try:
                    await page.close()
                except Exception:
                    pass
                owned_pages.remove(page)
                raise RuntimeError(f"自动打开{label}失败：{exc}") from exc

        flux_page = await open_owned_page("流量页", pipeline.FLUX_PATH) if need_flux else None
        activity_page = await open_owned_page("活动页", pipeline.ACTIVITY_PATH)
        return pw, browser, flux_page, activity_page, None, owned_pages
    except Exception:
        for page in reversed(owned_pages):
            try:
                await page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
        raise


def _rate_text(rate) -> str:
    """折扣率 → 折文案：0.85 → "8.5折"、0.9 → "9折"（去掉多余的 0）。"""
    return f"{round(float(rate) * 10, 2):g}折"


def plan_spu_activities(items: list, activities: list) -> dict:
    """纯本地：给定「已校验的逐货号价格」与「活动列表」，算出该 SPU 对每个活动的价格判定。

    抽成纯函数是为了让两处共用同一份业务规则（判定只能有一份实现，否则迟早分叉）：
    - `_process_one_spu`（规划遍）：只取 selected 发 product_plan，行为与抽取前逐字一致；
    - `scan_activity_matrix`（识别遍）：取 cells 建「商品×活动」矩阵，**含被淘汰的格子**——
      不让人看见「这个活动为什么报不了」，识别矩阵就没有意义。

    返回 {"selected": [...], "cells": {活动名: cell}, "rejected": [str]}：
    - selected：入选活动的执行计划（与旧 enrolled_activities 元素同形，下游零改动）；
    - cells：全量格子，verdict 取 pass/under_floor/no_rate/no_cost，note 与淘汰原因同文案；
    - rejected：淘汰原因片段（保持旧格式，供 skip_nomatch 的 note 用）。

    判定规则（用户 2026-09-29 改定）：报名是 SPU 级（提报页勾选无法只报部分货号），门槛
    也按 SPU 级定——取【毛利率最低】的货号当门槛货号，其 底价÷日常价 就是该 SPU 能打的
    最低折扣；活动折扣率 ≥ 它即入选（精确比较、不打折到分，见 pipeline.rate_reaches_floor）。
    其它货号不再逐一票决：它们毛利更厚、折扣空间更大，门槛货号扛得住它们必然扛得住。
    （2026-07-16 旧规则「全部货号申报价都达底价」在 2879383652×限时秒杀 上被 8 厘截断
    误差误杀——底价就是按 85 折填的 160.548，截断后 160.54 < 160.548；且逐货号票决与
    SPU 级报名维度本就不匹配。）
    """
    # 门槛货号 = 毛利率最低者（min 按行序取第一个，同毛利率时取表内靠前的行，确定可复现）。
    # 毛利率读不到就没法定门槛货号：fail-closed 全部格子 no_cost，绝不猜（source 层正常
    # 已把这类 SPU 整组拦下，这里是给直接调用方的最后防线）。
    if any(not isinstance(it.get("margin"), (int, float)) for it in items):
        cells = {
            act["name"]: {
                "activity": act["name"], "verdict": "no_cost",
                "discount_rate": act.get("discount_rate"), "min_stock": act.get("min_stock"),
                "skus": [], "sku_count": 0, "submit_price": None, "floor_price": None,
                "within_floor": False,
                "note": "毛利率读不到，无法判定门槛货号（请核对成本表毛利列）",
            }
            for act in activities
        }
        return {"selected": [], "cells": cells, "rejected": []}
    binding = min(items, key=lambda it: it["margin"])

    selected = []
    rejected = []
    cells = {}
    for act in activities:
        name = act["name"]
        dr = act.get("discount_rate")
        if dr is None:
            # 无固定折扣（万人团/详见提报列表）阶段1 无法确定性算价，跳过
            cells[name] = {
                "activity": name, "verdict": "no_rate", "discount_rate": None,
                "min_stock": act.get("min_stock"), "skus": [], "sku_count": 0,
                "submit_price": None, "floor_price": None, "within_floor": False,
                "note": "活动无固定折扣率（万人团/详见提报列表），无法确定性算价",
            }
            continue
        # 逐货号算申报价（填价要用，截断口径不变）；门槛判定只看门槛货号（精确口径）。
        calcs = [(it, pipeline.compute_submit_price(it["daily"], dr, it["sale"]))
                 for it in items]
        sku_prices = [{
            "label": it["label"], "daily": it["daily"], "sale": it["sale"],
            "submit_price": c["submit_price"],
        } for it, c in calcs]
        single = len(sku_prices) == 1
        cell = {
            "activity": name, "discount_rate": dr, "min_stock": act.get("min_stock"),
            "skus": sku_prices, "sku_count": len(sku_prices),
            # 单值字段仅单货号时照填、多货号置 None：把某个货号的价当成整个 SPU 的价会误导核对。
            "submit_price": sku_prices[0]["submit_price"] if single else None,
            "floor_price": sku_prices[0]["sale"] if single else None,
            "within_floor": False, "note": "",
        }
        if not pipeline.rate_reaches_floor(binding["daily"], dr, binding["sale"]):
            note = (f"活动「{name}」{_rate_text(dr)} 低于该 SPU 可打的最低折扣 "
                    f"{_rate_text(binding['sale'] / binding['daily'])}"
                    f"（毛利率最低的货号{binding['label']}：底价{binding['sale']}÷日常价{binding['daily']}）")
            rejected.append(note)
            cells[name] = {**cell, "verdict": "under_floor", "note": note}
            continue
        cells[name] = {**cell, "verdict": "pass", "within_floor": True}
        selected.append({
            "activity": name, "sku_prices": sku_prices,
            # submit_price/daily_price 仅是单货号兼容展示字段，执行层一律以 sku_prices 为准。
            "submit_price": sku_prices[0]["submit_price"] if single else None,
            "daily_price": sku_prices[0]["daily"] if single else None,
            "discount_rate": dr,
            "registered_count": act.get("registered_count"),
            "registered_display": act.get("registered_display"),
        })
    return {"selected": selected, "cells": cells, "rejected": rejected}


async def _process_one_spu(
    entry: dict,
    flux_page,
    activity_page,
    dry_run: bool,
    on_progress,
    stock_map: Optional[dict] = None,
    cost_map: Optional[dict] = None,
    selection=None,
    history_map: Optional[dict] = None,
) -> dict:
    """单 SPU 确定性流程（重构版），返回结果 dict（供上层生成 product_done 事件）。

    判定规则（2026-09-29 用户改定，唯一实现在 plan_spu_activities）：遍历活动页全部活动，
    活动折扣率 ≥ 毛利率最低货号的「底价÷日常价」（该 SPU 能打的最低折扣）才列入报名计划。
    一个 SPU 可有多个货号行（合并单元格成本表），提报页勾选是 SPU 级，故门槛也按 SPU 级
    定、不再逐货号票决。商品实时采购，库存不参与初筛。每个入选活动发一条 product_plan
    （带逐货号明细 skus）。规划阶段不执行报名或切换流量。
    保守跳过：任一货号日常价/销售底价/毛利率读不到 → skip_nofloor；无活动入选 → skip_nomatch。
    history_map：activity_history 的本批摘要（spu→上次结论），只用于给事件挂徽标数据。
    """
    spu = entry["spu"]
    spu_history = (history_map or {}).get(spu) or {}
    activity_history_map = spu_history.get("activities") or {}
    # 多商品串行时，每个商品开始前都清一次随机公告遮罩；只在业务弹窗出现前调用。
    await pipeline.dismiss_all_page_popups(flux_page)
    await pipeline.dismiss_all_page_popups(activity_page)
    result = {
        "spu": spu, "status": "fail", "accel_closed": False, "enrolled": False,
        "submit_price": None, "accel_reopened": False, "verified": False, "note": "",
        "enrolled_activities": [],
    }

    # 1. 读底价：read_costs 已保证整组货号有效才给价（{spu: {"items": [...]}}），
    #    每个货号各自一套 日常价/销售底价。成本 purchase 仅展示、不判定。
    row = (cost_map or {}).get(spu)
    items_raw = (row or {}).get("items") or []
    if not items_raw:
        result.update(status="skip_nofloor", note="WPS 文档未找到该 SPU 的有效价格")
        return result
    # 防御性重解析（=开头公式拒判）：source 层已校验过，这里是最后一道，任一货号无效即整组跳过。
    # 毛利率同价：门槛判定按毛利率最低货号（plan_spu_activities），读不到就没法定门槛。
    items = []
    for item in items_raw:
        daily = pipeline._to_number(item.get("daily"))
        sale_raw = str(item.get("sale", "")).strip()
        sale = None if (not sale_raw or sale_raw.startswith("=")) else pipeline._to_number(sale_raw)
        margin = pipeline._to_number(str(item.get("margin", "")).strip().rstrip("%"))
        if daily is None or sale is None or margin is None:
            result.update(status="skip_nofloor",
                          note=f"货号{item.get('label')}（第{item.get('row_number')}行）"
                               f"的日常价、销售底价或毛利率读不到，整组跳过")
            return result
        items.append({**item, "daily": daily, "sale": sale, "margin": margin})
    # 成本仅展示：多货号各不相同，n>1 不下发单值（避免把某货号成本当成整个 SPU 的）。
    cost = pipeline._to_number(str(items[0].get("purchase", "")).strip().lstrip("=")) \
        if len(items) == 1 else None

    # 2. 流量页定位行、读加速态（只读）。dry-run 只记「将关」，绝不点。
    accel_state = await pipeline.read_accel_state(flux_page, spu, search=True) if flux_page else "unknown"
    accel_will_close = accel_state == "on"

    # 3. 读活动列表（只读，已去重/去垃圾行）。
    activities = await pipeline.read_activities(activity_page) if activity_page else []

    # 5. 逐活动价格判定（唯一实现在 plan_spu_activities 里，识别遍共用同一份规则）。
    plan = plan_spu_activities(items, activities)
    enrolled = plan["selected"]
    rejected = plan["rejected"]
    # 识别矩阵勾选的格子：只影响「报哪些」，不影响「能不能报」——未勾的格子照常算价并如实
    # 标成未入选（页面要能看见完整计划，以及某一行为什么这次不报）。
    chosen = None if selection is None else {(str(spu), str(act)) for spu, act in selection}
    picked_names = []
    for sel in enrolled:
        single = len(sel["sku_prices"]) == 1
        picked = chosen is None or (str(spu), str(sel["activity"])) in chosen
        if picked:
            picked_names.append(sel["activity"])
        await _emit(on_progress, {
            "type": "product_plan", "spu": spu, "activity": sel["activity"],
            # 逐货号明细是权威价格；旧单值字段仅单货号时照填、多货号置 None——
            # 把某一个货号的价格当成「该 SPU 的价」展示会误导核对。
            "skus": sel["sku_prices"], "sku_count": len(sel["sku_prices"]),
            "daily_price": sel["daily_price"],
            "sale": sel["sku_prices"][0]["sale"] if single else None,
            "cost": cost,
            "submit_price": sel["submit_price"],
            "within_floor": True,
            "stock": None, "min_stock": plan["cells"][sel["activity"]]["min_stock"],
            "stock_ok": None,
            "stock_policy": "on_demand",
            "selected": picked,
            "reason": ("活动折扣率达到门槛（毛利率最低货号可打的最低折扣）；实时采购，不按库存筛选" if picked
                       else "未勾选（本次只报识别矩阵里勾选的格子）"),
            # 上次该 SPU×活动的报名结论（activity_history 摘要）；无历史/查不到为 None。
            "history": activity_history_map.get(sel["activity"]),
        })

    result["enrolled_activities"] = enrolled
    result["accel_state"] = accel_state  # 供执行遍区分初始 on/off/unknown
    result["accel_will_close"] = accel_will_close  # 初始 on 才需先关
    # 折扣列只用于定加速器档位与加速价（用户规则 2026-09-29），读不到不连坐报名——
    # 但执行遍会因此不动它的流量（定不了档就绝不乱开关）。
    result["discount_missing"] = not any(
        isinstance(it.get("discount"), (int, float)) for it in items)
    # 逐货号价格（含底价/折扣列）：执行遍阶段三重开加速器时按折扣列定档、逐行设加速价。
    result["skus"] = [{
        "label": it["label"], "daily": it["daily"], "sale": it["sale"],
        "purchase": it.get("purchase"), "discount": it.get("discount"),
    } for it in items]
    if enrolled:
        result["submit_price"] = enrolled[0]["submit_price"]  # 兼容旧单值字段（多货号为 None）

    # 6. 无任何活动入选 → skip_nomatch。
    if not enrolled:
        detail = "；".join(rejected[:2]) if rejected else "无固定折扣活动"
        result.update(status="skip_nomatch",
                      note=f"没有折扣率达到该 SPU 门槛的活动（{detail}）")
        return result

    # 7. 计划可行（status=done，携带计划）。真正的变更在【执行遍】(_run_execution_phases) 里按
    #    活动维度做，本函数只负责【规划】、不点任何变更按钮。dry-run 与非 dry-run 规划完全一致，
    #    区别只在执行遍是否运行（dry_run=True 时执行遍整段跳过）。
    #    note 只按【勾选到的】活动写：全量 planned 里没勾的不算「将报名」，否则页面会说
    #    「将报名 4 个活动」而执行遍只跑 1 个（真机半程实测踩到）。
    parts = []
    if picked_names and result["discount_missing"]:
        # 定不了档就绝不乱动流量（执行遍阶段一/三有同一道闸），规划 note 必须同口径。
        parts.append("成本表折扣列读不到，不动流量（只报活动）")
    elif picked_names and accel_will_close:
        parts.append("将关加速器")
    if picked_names:
        parts.append(f"将报名 {len(picked_names)} 个活动：" + "、".join(picked_names))
        if accel_state in {"on", "off"} and not result["discount_missing"]:
            parts.append("将开启加速器")
    else:
        parts.append("本次未勾选该商品的任何活动（不会报名，也不动它的流量）")
    result.update(status="done", note=("dry-run：" if dry_run else "计划就绪：") + "，".join(parts))
    return result


async def _pause_gate(control, on_progress, scope: str, note: str = "") -> None:
    """暂停闸门：只在【安全边界】调用（SPU 之间、活动之间），绝不在点击动作中途调用。

    与发布管线同构：真正停住的那一刻发 `paused`（前端才把按钮文案从「暂停中」切到「已暂停」），
    恢复时发 `resumed`。事件走同一条 SSE 队列，刷新重连后仍能消费到，暂停态不会丢。
    control 为空（CLI/单测）时整个闸门是 no-op。
    """
    if control is None or not control.paused:
        return
    await _emit(on_progress, {"type": "paused", "scope": scope, "note": note})
    await control.wait_if_paused()
    await _emit(on_progress, {"type": "resumed"})


def _filter_result(res: dict, selection, activity_allow) -> None:
    """裁剪单个 SPU 的报名计划：(SPU,活动) 级勾选 + 活动级白名单。

    必须在【每个 SPU 处理完、计数与 product_done 事件之前】调用：否则统计与页面会显示
    「将报名 N 个活动」，而执行遍只跑勾选的那几格，操作者看到的是假计划。
    裁空且原 status=done → 降级 skip_nomatch，该 SPU 因此【不进执行遍】，包括不被关流量：
    没勾任何格子就别动它的流量，这是最安全的默认。
    """
    planned = res.get("enrolled_activities") or []
    ea = planned
    if activity_allow is not None:
        allow = set(activity_allow)
        ea = [e for e in ea if e["activity"] in allow]
    if selection is not None:
        chosen = {(str(spu), str(act)) for spu, act in selection}
        ea = [e for e in ea if (str(res["spu"]), str(e["activity"])) in chosen]
    if len(ea) == len(planned):
        return
    res["enrolled_activities"] = ea
    if not ea and res.get("status") == "done":
        res["status"] = "skip_nomatch"
        res["note"] = ("识别矩阵里没有勾选该商品的任何格子，跳过（不影响它的流量）"
                       if selection is not None else "活动白名单过滤后无可报活动，跳过")


async def _run_execution_phases(results, flux_page, activity_page, live, on_progress, control=None) -> dict:
    """执行遍（活动维度）：关流量 → 按活动分组报名 → 只重开我们关掉的流量。

    仅对规划遍里 status=done 的 SPU 执行。live=False（半程）：开提报页/填价但不提交、不真关/开
    （全部可逆，用于端到端验证）；live=True（全程）：真提交/真关/真开（不可逆，须授权）。
    control 给定时在各阶段的安全边界挂暂停闸门、并接受逐格跳过（见 app/activity/control.py）。
    进度经 exec_* 事件抛出。返回 {closed:[spu], enrolled_activities:{名:[spu]}, reopened:[spu]}。
    """
    plans = [r for r in results if r.get("status") == "done" and r.get("enrolled_activities")]
    # failed：报名遍里【填价/提交未成功】的逐条明细（含 Excel 与前端售价不一致导致的超参考价），
    # 供 exec_done 汇总回报操作者「哪个商品/哪个活动/为什么失败」。
    summary = {
        "closed": [], "filled_activities": {}, "enrolled_activities": {},
        "ineligible_activities": {}, "activity_results": {},
        "submitted_attempts": {}, "scan_failures": [], "log_verification": {},
        "log_baseline": {}, "accel_checks": {},
        "reopened": [], "errors": [], "failed": [], "skipped_cells": [],
        # 本批零提交、靠报名记录页的「已有生效报名」补开流量的 SPU（值=命中的活动名），
        # 以及查过记录页但确实没有生效报名的 SPU——两者都要，前者决定开不开，后者写清
        # 「为什么还是不开」。
        "opened_by_existing": {}, "no_existing_registration": [],
    }
    if not plans:
        return summary

    await _emit(on_progress, {"type": "exec_start", "live": live, "spu_count": len(plans)})

    # ---- 阶段一：逐 SPU 临场复查流量状态；on 才关闭，off 明确记录 no-op ----
    # 规划阶段读到的 accel_state 可能因多商品串行或人工操作变旧，不能直接决定执行动作。
    # 若临场读取 unknown，规划时为 on 的商品仍交给 close_accel 做一次带筛选的复查；其余保守不关。
    if control is not None:
        control.set_phase("close")
    for plan in plans:
        spu = plan["spu"]
        # 暂停只挂在 SPU 之间：此处上一个 SPU 已收尾，挂起不会留下半完成的动作。
        await _pause_gate(control, on_progress, "close", note=f"SPU {spu} 关流量前暂停")
        planned_state = plan.get("accel_state", "unknown")
        try:
            await _emit(on_progress, {
                "type": "exec_accel_step", "phase": "close", "step": "checking",
                "spu": spu, "state": None, "note": "正在按 SPU 查询关闭前状态",
            })
            try:
                await flux_page.bring_to_front()
                await asyncio.sleep(0.5)
            except Exception:
                pass
            current_state = await pipeline.read_accel_state(flux_page, spu, search=True)
            if current_state in {"on", "off"}:
                plan["accel_state"] = current_state
                plan["accel_will_close"] = current_state == "on"
            if current_state == "off":
                note = "执行时复查：流量加速器已关闭，无需关闭（no-op）"
                summary["accel_checks"][spu] = {"state": "off", "ok": True, "note": note}
                await _emit(on_progress, {
                    "type": "exec_close", "spu": spu, "ok": True, "live": live,
                    "state": "off", "already_off": True, "cooldown": False, "note": note,
                })
                continue
            if current_state == "unknown" and planned_state != "on":
                plan["accel_state"] = "unknown"
                plan["accel_will_close"] = False
                note = "执行时复查未识别流量状态；保守不执行关闭，报名仍继续"
                summary["accel_checks"][spu] = {"state": "unknown", "ok": False, "note": note}
                await _emit(on_progress, {
                    "type": "exec_close", "spu": spu, "ok": False, "live": live,
                    "state": "unknown", "already_off": False, "cooldown": False, "note": note,
                })
                continue

            # 折扣列读不到 → 定不了加速档位与加速价，阶段三重开不了 → 阶段一也不关：
            # 关掉再开不回来是本品最严重的不可逆后果。流量保持现状，活动照常报名。
            if plan.get("discount_missing"):
                note = "成本表折扣列读不到，无法确定加速档位与价格；不关流量（活动照常报名）"
                plan["accel_will_close"] = False
                summary["accel_checks"][spu] = {"state": current_state, "ok": False, "note": note}
                await _emit(on_progress, {
                    "type": "exec_close", "spu": spu, "ok": False, "live": live,
                    "state": current_state, "already_off": False, "cooldown": False, "note": note,
                })
                continue

            await _emit(on_progress, {
                "type": "exec_accel_step", "phase": "close", "step": "acting",
                "spu": spu, "state": current_state,
                "note": "已确认处于加速中，正在执行关闭并等待平台结果",
            })
            res = await pipeline.close_accel(flux_page, spu, allow=live)
            resolved_state = res.get("state", current_state)
            if resolved_state in {"on", "off"}:
                plan["accel_state"] = resolved_state
                plan["accel_will_close"] = resolved_state == "on"
            already_off = resolved_state == "off"
            action_closed = resolved_state == "on" and bool(res.get("closed"))
            ok = already_off or action_closed or (not live and resolved_state == "on")
            if action_closed:
                summary["closed"].append(spu)
            if res.get("cooldown"):
                summary.setdefault("cooldown", []).append(spu)  # 24h 冷却拦截，未关成
            summary["accel_checks"][spu] = {
                "state": resolved_state, "ok": ok, "already_off": already_off,
                "closed": action_closed, "note": res.get("note", ""),
            }
            await _emit(on_progress, {
                "type": "exec_close", "spu": spu, "ok": ok, "live": live,
                "state": resolved_state, "already_off": already_off,
                "cooldown": bool(res.get("cooldown")), "note": res.get("note", ""),
            })
        except Exception as e:
            summary["errors"].append(f"关流量 {spu}：{e}")
            await _emit(on_progress, {"type": "exec_close", "spu": spu, "ok": False, "note": f"异常：{e}"})

    # 关闭检查结束后、任何提报动作前保存报名记录基线。这里的 total 是 /log 查询结果总数，
    # 与规划阶段的“初筛活动数”不是同一个指标。
    if live:
        await _capture_activity_log_baseline(plans, activity_page, on_progress, summary)

    # ---- 阶段二：按活动分组报名（活动内逐 SPU 开页、填完立即单独提交）----
    # 【设计决策·用户确认 2026-07-17】关流量撞 24h 冷却（summary["cooldown"] 里的 SPU，关不掉）
    # 时，其报名【照常继续】——不因关流量失败而跳过。冷却只如实记录在 exec_close/summary，
    # 不拦截报名。故此处对全部 plans 分组，不排除 cooldown/关闭失败的 SPU。
    by_activity: dict = {}
    for r in plans:
        for e in r.get("enrolled_activities", []):
            by_activity.setdefault(e["activity"], []).append(
                {
                    "spu": r["spu"], "sku_prices": e["sku_prices"],
                    "submit_price": e.get("submit_price"),
                    "daily_price": e.get("daily_price"), "discount_rate": e.get("discount_rate"),
                    "registered_count": e.get("registered_count"),
                    "registered_display": e.get("registered_display"),
                    "registration_baseline_known": "registered_count" in e,
                }
            )
    # 报名遍整体包异常：无论报名成败/抛错，阶段三重开流量都必须照跑——否则阶段一关掉的
    # 流量会因报名异常而永久留在关闭状态（最严重的不可逆后果）。异常只记 errors、不中断。
    if control is not None:
        control.set_phase("enroll")
    try:
        await _enroll_by_activity(by_activity, activity_page, live, on_progress, summary,
                                  control=control)
    except Exception as e:
        summary["errors"].append(f"报名遍异常（不影响重开流量）：{e}")
        logger.error(f"报名遍异常：{e}")
        await _emit(on_progress, {"type": "log", "level": "error",
                                  "message": f"报名遍异常（继续重开流量）：{e}"})

    # 报名过程中的“页面跳转/弹窗提示/局部 RPA 失败”只记扫描状态，不直接决定最终成败。
    # 正式执行在所有活动扫描完后统一进入报名记录页，按 SPU + 活动名对账平台真实记录。
    # 被跳过的格子要对账豁免：它本来就没提交，拿去对账只会得到「未查到成功记录」的假失败。
    skips = {(spu, act) for spu, act in (control.skipped_cells() if control else [])}
    summary["skipped_cells"] = [list(key) for key in sorted(skips)]
    if live:
        await _reconcile_activity_log(plans, activity_page, on_progress, summary, skips=skips)

    # ---- 阶段三：开启流量 ----
    # 初始 on：仅关闭成功后重开；关闭失败/冷却时仍为 on，不重复操作。
    # 初始 off：业务允许先报名再开启，因此报名遍结束后也开启。
    # 初始 unknown：保守不操作。
    # 档位与加速价按成本表「折扣」列定（用户规则 2026-09-29）：85折→普通档、9/8折→高级档
    # （这两档用平台默认申报价，无自定义价入口）、75折及以下→超级档（自定义价=逐货号
    # max(底价+1, 最低折扣价÷0.9 向上取整)，与平台「活动价 ≤ 加速价×0.9」同向；抬不进对话框
    # 上限的档由 pipeline 退填底价+1 并注明活动将失效）。开之前先校验限流（pipeline 内做）：
    # 限流品停止流量加速动作、只做活动。折扣列读不到的品不动流量（阶段一已把关不关，
    # 这里拦的是初始 off 的新开启）。
    skus_by_spu = {r["spu"]: r.get("skus") for r in plans}
    plan_by_spu = {r["spu"]: r for r in plans}
    initially_off = [r["spu"] for r in plans if r.get("accel_state") == "off"]
    if control is not None:
        # 阶段三不接暂停：此刻流量已关，挂起 = 商品无流量在售。HTTP 侧按 phase 拒（409），
        # 这里再挡一次竞态——恰在阶段二末尾被置位时强制清掉并如实发事件，保住不变量
        # 「只要阶段一关过流量，阶段三必然跑到」。
        control.set_phase("reopen")
        if control.paused:
            control.set_paused(False)
            await _emit(on_progress, {
                "type": "pause_refused", "reason": "reopen_phase",
                "note": "收尾阶段不可暂停：已关闭的流量必须恢复，已自动继续",
            })
    if live:
        # 初始 off 属于新增开启动作，两档理由各自成立即开：
        #  (a) 本批真实提交了活动（该 SPU 的全部计划活动都已解析：提交/详情不符/跳过）；
        #  (b) 本批零提交，但报名记录页显示该 SPU 在这些活动下【已有生效报名】。
        # (b) 是 2026-09-30 用户改定。零提交的成因是「详情页按 SPU 查询可报名商品行数 0」，
        # 该判据区分不出「真不符合这个活动」和「早就报过了」——后者在平台上报名仍然生效，
        # 加速器关着时「活动价 ≤ 加速价×0.9」不成立、报上的活动会失效，必须照开。
        # 记录快照复用阶段二对账的结果，不额外查一次报名记录页；快照不完整时
        # active_registration 一律返回 {}（fail-closed：查不准就不开）。
        log_snapshot = summary.get("log_verification") or {}
        successful_off = []
        for plan in plans:
            spu = plan["spu"]
            if spu not in initially_off:
                continue
            planned_items = plan.get("enrolled_activities", [])
            submitted_count = sum(
                spu in (summary["enrolled_activities"].get(item["activity"]) or [])
                for item in planned_items
            )
            all_resolved = all(
                spu in (summary["enrolled_activities"].get(item["activity"]) or [])
                or spu in (summary["ineligible_activities"].get(item["activity"]) or [])
                # 被跳过的格子也算已解析：跳过是「不报这个活动」，不是「别开流量」，
                # 少了这一项会让「只勾了部分格子」的 SPU 永远开不回流量。
                or (spu, item["activity"]) in skips
                for item in planned_items
            )
            # 计划里还有没结论的格子：别开。半途开流量会把「这次到底报没报上」搅成一团。
            if not all_resolved:
                continue
            if submitted_count > 0:
                successful_off.append(spu)
                continue
            existing = [item["activity"] for item in planned_items
                        if active_registration(log_snapshot, spu, item["activity"])]
            if not existing:
                # 详情页无可报名商品行、记录页也没有生效报名 → 确认是「本来就报不了」，
                # 维持不开（省得为一个什么活动都没参加的商品白抬价）。
                summary["no_existing_registration"].append(spu)
                continue
            successful_off.append(spu)
            summary["opened_by_existing"][spu] = existing
            await _emit(on_progress, {
                "type": "log", "level": "info",
                "message": f"[开流量] SPU={spu} 本批未新增报名，但报名记录页显示已有生效报名"
                           f"（{'、'.join(existing)}），照常开启流量加速器",
            })
        # 初始 on 且已被本管线关闭的商品，无论报名是否失败都必须恢复开启。
        to_open = list(dict.fromkeys(summary["closed"] + successful_off))
    else:
        # 半程没有真关，仍对所有确定 on/off 的计划验证开流量页面，但 allow=False 不提交。
        known_accel = [r["spu"] for r in plans if r.get("accel_state") in {"on", "off"}]
        to_open = list(dict.fromkeys(known_accel))
    for spu in to_open:
        try:
            plat = (summary.get("platform_daily") or {}).get(spu) or {}
            skus = skus_by_spu.get(spu) or []
            # 一个 SPU 多行货号的折扣列理应一致；不一致时取最小值（折扣打得最深的那个）
            # 定档——档位跟错只会让价算保守，跟浅了才会破底价。
            discounts = [s["discount"] for s in skus
                         if isinstance(s.get("discount"), (int, float))]
            if not discounts:
                note = "成本表折扣列读不到，无法确定加速档位与价格；未开启流量加速器"
                summary.setdefault("reopen_notes", {})[spu] = note
                await _emit(on_progress, {
                    "type": "exec_reopen", "spu": spu, "ok": False, "live": live,
                    "tier": None, "note": note,
                })
                continue
            tier = pipeline.accel_tier_for_discount(min(discounts))
            accel_prices = None
            if tier == "super":
                # 超级档自定义价（用户 2026-09-29：「自定义价格就填写最低折扣价格/0.9得出的
                # 金额」）：最低折扣价 = 日常价×折扣列折扣率，向下取整到分（与平台参考价同口径，
                # 见 compute_submit_price）；再与底价+1 取高。
                accel_prices = []
                for s in skus:
                    if s.get("sale") is None:
                        continue
                    submit = pipeline.compute_submit_price(
                        s.get("daily"), s.get("discount"), s.get("sale")).get("submit_price")
                    need = pipeline.ceil_cent(submit / 0.9) if submit else 0.0
                    accel_prices.append({
                        "label": s.get("label"), "daily": s.get("daily"), "sale": s.get("sale"),
                        # 平台侧该货号的真实日常价（本次提报页读到的）：加速器对话框按平台价格档
                        # 列行，用它才能把行配到货号；读不到时退回表里的价（旧行为）。
                        "platform_daily": plat.get(s.get("label")),
                        "price": max(round(float(s["sale"]) + 1, 2), need or 0.0),
                    })
                accel_prices = accel_prices or None
            await _emit(on_progress, {
                "type": "exec_accel_step", "phase": "open", "step": "checking",
                "spu": spu, "state": None, "note": "正在按 SPU 查询开启前状态",
            })
            res = await pipeline.open_accel(flux_page, spu, allow=live,
                                            accel_prices=accel_prices, tier=tier)
            ok = res.get("opened", False)
            if ok:
                summary["reopened"].append(spu)
            if res.get("throttled"):
                # 限流品（用户 2026-09-29）：停止流量加速动作、只做活动。不进 reopened
                # （流量确实没开），但逐品结果要按「按规则不动流量」而不是失败合成。
                summary.setdefault("throttled", []).append(spu)
            # 存下开启失败的现场原因：逐品结果里要如实说「为什么没开起来」（限流 / 档上限低于
            # 底价 / 平台没给入口 / 回查仍非加速中），而不是笼统一句「流量未完成」。
            summary.setdefault("reopen_notes", {})[spu] = res.get("note", "")
            await _emit(on_progress, {
                "type": "exec_reopen", "spu": spu, "ok": ok, "live": live,
                "tier": tier,
                "throttled": bool(res.get("throttled")),
                "accel_prices": accel_prices,
                # 单货号兼容字段（模板展示用）；多货号看 accel_prices 明细。
                "accel_price": (accel_prices[0]["price"]
                                if accel_prices and len(accel_prices) == 1 else None),
                "precheck_state": res.get("precheck_state", res.get("state", "unknown")),
                "already_on": res.get("precheck_state", res.get("state")) == "on",
                "note": res.get("note", ""),
            })
        except Exception as e:
            summary["errors"].append(f"重开流量 {spu}：{e}")
            await _emit(on_progress, {"type": "exec_reopen", "spu": spu, "ok": False, "note": f"异常：{e}"})

    # 报名失败逐条回报（哪个商品/活动/为什么）——尤其 Excel 与前端售价不一致导致的超参考价，
    # 需操作者核对该商品 Excel 日常价。best-effort：告警只提示、不中断。
    for f in summary["failed"]:
        logger.warning(
            f"[报名失败] SPU={f['spu']} 活动「{f['activity']}」申报价={f.get('submit_price')} "
            f"参考价={f.get('ref_price')}：{f.get('reason')}"
        )
    product_results = _execution_product_results(plans, summary, live, skips=skips)
    summary["product_results"] = product_results
    for result in product_results:
        await _emit(on_progress, {"type": "exec_product_done", **result})
    if control is not None:
        control.set_phase("done")
    await _emit(on_progress, {"type": "exec_done", "closed": len(summary["closed"]),
                              "reopened": len(summary["reopened"]),
                              "activities": len(by_activity), "errors": summary["errors"],
                              "failed": summary["failed"],
                              "skipped_cells": summary["skipped_cells"],
                              "product_results": product_results})
    return summary


def _execution_product_results(plans, summary, live, skips=None) -> list[dict]:
    """把规划结果与实际提交/开流量结果合成逐 SPU 最终状态，避免用规划 done 冒充执行完成。

    skips 是执行期被跳过的 (spu, 活动) 集合：跳过的格子算「已解析」，否则一个 SPU 只勾了
    部分格子就会永远算不齐、被判 fail（假失败）。
    """
    submitted = summary.get("enrolled_activities") or {}
    ineligible = summary.get("ineligible_activities") or {}
    skipped = set(skips or ())
    reopened = set(summary.get("reopened") or [])
    closed = set(summary.get("closed") or [])
    cooldown = set(summary.get("cooldown") or [])
    by_existing = summary.get("opened_by_existing") or {}
    no_existing = set(summary.get("no_existing_registration") or [])
    outcomes = []
    for plan in plans:
        spu = plan["spu"]
        planned_names = [item["activity"] for item in plan.get("enrolled_activities", [])]
        submitted_names = [name for name in planned_names if spu in (submitted.get(name) or [])]
        ineligible_names = [name for name in planned_names if spu in (ineligible.get(name) or [])]
        skipped_names = [name for name in planned_names if (spu, name) in skipped]
        if not live:
            outcomes.append({
                "spu": spu, "status": "preview", "planned": len(planned_names),
                "submitted": 0, "ineligible": len(ineligible_names),
                "skipped": len(skipped_names),
                "accel_ok": False, "note": "半程预览，未提交/未开关流量",
            })
            continue

        state = plan.get("accel_state")
        throttled = spu in set(summary.get("throttled") or [])
        discount_missing = bool(plan.get("discount_missing"))
        if throttled or discount_missing:
            # 限流品/折扣列读不到的品按规则「只做活动、不动流量」（用户 2026-09-29）：
            # 流量维度不算成败——它不是没开成，是根本没动。
            accel_ok = True
        elif state == "off":
            accel_ok = spu in reopened
        elif state == "on" and spu in cooldown:
            accel_ok = True  # 冷却拦截后仍保持原有 ON
        elif state == "on":
            accel_ok = spu in closed and spu in reopened
        else:
            accel_ok = False
        all_resolved = (len(submitted_names) + len(ineligible_names) + len(skipped_names)
                        == len(planned_names))
        # 全都不是「报了但没成」而是「本来就报不了/被跳过」→ 这批没有失败可言，记 skip 不记 fail。
        all_ineligible = bool(planned_names) and len(submitted_names) == 0 and all_resolved
        activities_ok = all_resolved and bool(submitted_names)
        status = "skip" if all_ineligible else ("done" if activities_ok and accel_ok else "fail")
        reasons = []
        if spu in by_existing:
            # 本批零提交、靠报名记录页的已有生效报名补开流量（用户 2026-09-30）：本批没新增
            # 报名不是失败，但流量这一件事是实打实做了——开成了记 done，没开成记 fail。
            # 不能落到下面的 all_ineligible 分支记 skip：那会把补开失败吞掉。
            status = "done" if accel_ok else "fail"
            if throttled or discount_missing:
                # 2026-09-30 实跑踩到：限流品 accel_ok=True（流量不算成败）但文案写成
                # 「已按规则开启」——其实根本没动流量，会误导操作者以为开上了。
                accel_note = ("流量侧按规则不动作（商品限流，只做活动报名）" if throttled
                              else "流量侧按规则不动作（成本表折扣列读不到，只做活动报名）")
            else:
                accel_note = f"已按规则{'开启' if accel_ok else '尝试开启'}流量加速器"
            reasons.append(
                f"本批未新增报名，但报名记录页显示已有生效报名"
                f"（{'、'.join(by_existing[spu])}），{accel_note}"
            )
            if not accel_ok:
                why = (summary.get("reopen_notes") or {}).get(spu) or "未捕获开启失败原因"
                reasons.append(f"但加速器未能开启：{why}")
            outcomes.append({
                "spu": spu, "status": status, "planned": len(planned_names),
                "submitted": 0, "ineligible": len(ineligible_names),
                "skipped": len(skipped_names),
                "accel_ok": accel_ok, "note": "；".join(reasons),
            })
            continue
        if all_ineligible:
            parts = []
            if ineligible_names:
                parts.append(f"{len(ineligible_names)} 个初筛活动在详情页无可报名商品")
            if skipped_names:
                parts.append(f"{len(skipped_names)} 个活动按操作者指令跳过")
            if spu in no_existing:
                # 查过报名记录页、确实没有生效报名 → 说明这些活动是「本来就报不了」，
                # 维持不开流量。说清楚，免得操作者以为是漏开。
                parts.append("报名记录页也未查到生效报名，不新增开启流量")
            reasons.append("、".join(parts))
        elif not activities_ok:
            reasons.append(
                f"提交 {len(submitted_names)}、详情不符合 {len(ineligible_names)}、"
                f"用户跳过 {len(skipped_names)}、计划 {len(planned_names)} 个活动"
            )
            check = (summary.get("accel_checks") or {}).get(spu) or {}
            if (check.get("state") == "on" and not check.get("closed")
                    and summary.get("failed") and not discount_missing):
                # 加速器关不掉时前端售价按加速价，活动申报上限被压低 → 报名必被平台拒。
                # 真机 2026-09-25：24h 锁定期内关闭被拒，随后提报页参考价变成加速价×折扣
                # （89.04 = 98.94×0.9），按表里日常价算的申报价 146.9 必然超高。
                why = ("加速器处于开启后 24 小时锁定期，平台不允许手动关闭"
                       if check.get("cooldown") else "加速器未能关闭")
                reasons.append(
                    f"且{why}——加速器开着时前端售价按加速价，活动申报上限被压低、报名必被平台拒；"
                    f"请等锁定期结束后再报")
        if not accel_ok and not all_ineligible:
            # 说清是哪一种「流量没完成」：
            # - 加速器仍开着（关不掉）→ 前端售价按加速价，活动申报上限被压低，报名必被平台拒；
            # - 初始 on：我们关过它却没恢复 → 最严重；
            # - 初始 off：我们只是没把它开起来（报名本身可能已成功），要带上失败现场原因。
            check = (summary.get("accel_checks") or {}).get(spu) or {}
            if check.get("state") == "on" and not check.get("closed"):
                why = ("加速器处于开启后 24 小时锁定期，平台不允许手动关闭"
                       if check.get("cooldown") else "加速器关闭未成功")
                reasons.append(f"{why}；加速器仍开着时前端售价按加速价，"
                               f"活动申报上限被压低、报名必被平台拒——请等锁定期结束后再报")
            elif state == "on":
                reasons.append("流量未恢复开启（初始=on，本管线关过它）")
            elif state == "off":
                if spu in (summary.get("reopen_notes") or {}):
                    why = summary["reopen_notes"][spu] or "未捕获开启失败原因"
                    reasons.append(f"报名已提交，但加速器未开启：{why}")
                else:
                    # 本次根本没发起开启（例如报名没成功，to_open 不含它）：别写成
                    # 「已提交但未开启」那种自相矛盾的话（真机 2026-09-25 踩到）。
                    reasons.append("未发起加速器开启（本次报名未确认成功）")
            else:
                reasons.append(f"流量状态未确认（初始={state}），未操作")
        if throttled:
            # 限流是「按规则不动流量」而非失败，但 note 里必须点名——否则操作者看到 done
            # 会以为流量也开好了。
            reasons.append("商品限流，按规则只做活动报名、不动流量加速器")
        if discount_missing:
            reasons.append("成本表折扣列读不到，定不了加速档位；流量保持现状、只做活动报名")
        outcomes.append({
            "spu": spu, "status": status, "planned": len(planned_names),
            "submitted": len(submitted_names), "ineligible": len(ineligible_names),
            "skipped": len(skipped_names),
            "accel_ok": accel_ok,
            "note": "；".join(reasons) if reasons else "报名提交与流量开启步骤均完成",
        })
    return outcomes


def _log_snapshot(result: dict) -> dict:
    records = result.get("records") or []
    return {
        "complete": bool(result.get("complete")),
        "queries": result.get("queries") or [],
        "note": result.get("note", ""),
        "records": [
            {key: value for key, value in record.items() if key != "raw"}
            for record in records
        ],
    }


async def _capture_activity_log_baseline(plans, activity_page, on_progress, summary) -> None:
    """报名开始前保存各 SPU 的 /log total 与已有活动记录。"""
    spus = [plan["spu"] for plan in plans]
    try:
        result = await pipeline.read_activity_log_records(activity_page.context, spus)
    except Exception as exc:
        result = {
            "records": [], "complete": False, "queries": [],
            "note": f"报名前报名记录查询异常：{str(exc)[:120]}",
        }
    summary["log_baseline"] = _log_snapshot(result)
    totals = ", ".join(
        f"{query.get('spu')}={query.get('total', '?')}"
        for query in result.get("queries") or []
    ) or "未取得"
    await _emit(on_progress, {
        "type": "log", "level": "info" if result.get("complete") else "warning",
        "message": f"[报名基线] /log 记录 total：{totals}（{result.get('note', '')}）",
    })


async def _reconcile_activity_log(plans, activity_page, on_progress, summary, skips=None) -> None:
    """扫描结束后以报名记录页为准，对计划中的每个 SPU/活动做最终状态归并。

    skips：执行期被操作者跳过的 (spu, 活动)，直接不参与对账——它本来就没提交，
    拿去对账只能得到「未查到成功记录」，那是假失败。
    """
    skipped = set(skips or ())
    spus = [plan["spu"] for plan in plans]
    baseline = summary.get("log_baseline") or {}
    attempts = summary.get("submitted_attempts") or {}
    assessments = {}
    snapshots = []
    for attempt in range(LOG_VERIFY_TRIES):
        try:
            result = await pipeline.read_activity_log_records(activity_page.context, spus)
        except Exception as exc:
            result = {
                "records": [], "complete": False, "queries": [],
                "note": f"报名记录页查询异常：{str(exc)[:120]}",
            }
        snapshots.append(_log_snapshot(result))
        pending = False
        for plan in plans:
            spu = str(plan["spu"])
            for item in plan.get("enrolled_activities", []):
                activity = item["activity"]
                if (spu, activity) in skipped:
                    continue
                attempted = spu in attempts.get(activity, [])
                assessment = assess_registration(baseline, result, spu, activity, attempted)
                assessments[(spu, activity)] = assessment
                if attempted and not assessment["ok"]:
                    pending = True
        if not pending or attempt + 1 == LOG_VERIFY_TRIES:
            break
        await _emit(on_progress, {
            "type": "log", "level": "info",
            "message": f"报名记录仍有未确认项，等待后第 {attempt + 2} 次复查（最多 {LOG_VERIFY_TRIES} 次）",
        })
        await asyncio.sleep(LOG_VERIFY_RETRY_DELAYS[min(attempt, len(LOG_VERIFY_RETRY_DELAYS) - 1)])
    summary["log_verification"] = {
        **_log_snapshot(result),
        "baseline_queries": baseline.get("queries") or [],
        "attempts": snapshots,
    }
    scan_failures = summary.get("scan_failures") or []

    for plan in plans:
        spu = str(plan["spu"])
        for item in plan.get("enrolled_activities", []):
            activity = item["activity"]
            pair = (spu, activity)
            if pair in skipped:
                # 跳过格没进过对账（上面也 continue 了），此处必须同样跳过：
                # 既不取 assessments[pair]，也不往 enrolled_activities 里写。
                continue
            assessment = assessments[pair]
            success_record = assessment["record"] if assessment["ok"] else None
            summary["enrolled_activities"][activity] = [
                value for value in summary["enrolled_activities"].get(activity, []) if value != spu
            ]
            current = summary["activity_results"].setdefault(activity, {})
            current.setdefault("log_results", {})[spu] = assessment
            if success_record:
                enrolled = summary["enrolled_activities"].setdefault(activity, [])
                if spu not in enrolled:
                    enrolled.append(spu)
                ineligible = summary["ineligible_activities"].get(activity) or []
                remaining_ineligible = [value for value in ineligible if value != spu]
                if remaining_ineligible:
                    summary["ineligible_activities"][activity] = remaining_ineligible
                else:
                    summary["ineligible_activities"].pop(activity, None)
                summary["failed"] = [
                    failure for failure in summary["failed"]
                    if not (str(failure.get("spu")) == spu and failure.get("activity") == activity)
                ]
                # 场次失败原因不再否决成功（用户规则 2026-07-24），但仍附注出来供人工核对。
                note = assessment["note"]
                if success_record.get("session_failures"):
                    note += f"；注意存在场次失败原因：{'、'.join(success_record['session_failures'])}"
                current.update({
                    "verified": True, "status": "log_verified", "note": note,
                })
                await _emit(on_progress, {
                    "type": "exec_log_verify", "spu": spu, "activity": activity,
                    "ok": True, "status": "success",
                    "enroll_id": success_record.get("enroll_id"),
                    "note": current["note"],
                })
                continue

            if spu in (summary["ineligible_activities"].get(activity) or []):
                note = (summary["activity_results"].get(activity) or {}).get(
                    "note", "详情页按 SPU 查询无可报名商品"
                )
                await _emit(on_progress, {
                    "type": "exec_log_verify", "spu": spu, "activity": activity,
                    "ok": False, "status": "detail_ineligible", "note": note,
                })
                continue

            scan_failure = next(
                (failure for failure in scan_failures
                 if str(failure.get("spu") or "") in {"", spu}
                 and failure.get("activity") == activity),
                None,
            )
            reason = assessment["note"]
            if scan_failure and scan_failure.get("note"):
                reason = f"{reason}；扫描阶段：{scan_failure['note']}"
            if not any(
                str(failure.get("spu")) == spu and failure.get("activity") == activity
                for failure in summary["failed"]
            ):
                summary["failed"].append({
                    "spu": spu, "activity": activity,
                    "submit_price": item.get("submit_price"), "over_ref": False,
                    "reason": reason,
                })
            summary["errors"].append(f"报名记录未确认 {activity}/{spu}：{reason}")
            await _emit(on_progress, {
                "type": "exec_log_verify", "spu": spu, "activity": activity,
                "ok": False, "status": "not_verified", "note": reason,
            })
    for activity, current in summary["activity_results"].items():
        outcomes = current.get("log_results", {})
        enrolled = summary["enrolled_activities"].get(activity, [])
        ineligible = summary["ineligible_activities"].get(activity, [])
        current["verified"] = bool(enrolled) and all(
            outcome["ok"] or spu in ineligible for spu, outcome in outcomes.items()
        )
        current["status"] = "log_verified" if current["verified"] else "not_verified"


async def _enroll_by_activity(by_activity, activity_page, live, on_progress, summary, control=None) -> None:
    """逐活动串行；活动内【每 SPU 填完立即单独提交】，单活动失败只记录，最终统一由报名记录页对账。

    【为什么废弃「逐 SPU 填价、活动末统一提交」】2026-10-03 实测实锤：提报页（detail-new）
    每做一次 SPU 搜索就重渲结果表格，上一个 SPU 的勾选与填价随旧行卸载全部丢失——页面
    没有跨搜索保留的已选商品池。统一提交是 2026-07-17 单 SPU 时代的设计，多 SPU 时只要
    后面 SPU 详情查询为 0，前面 SPU 的勾选就被清掉，统一提交要么按钮禁用、要么只提交
    最后一个 SPU（8791757215 的限时秒杀填价被 3822224199 的搜索冲掉，结果页「已提交 1 个
    商品」实际是后者，旧「successCount>0 即成功」判据把部分丢失吞掉）。现改为：
      - 每 SPU 一张干净提报页，填完立即提交（live）；提交过的页面一律关掉（结果页/不确定
        现场都不复用），下一 SPU 重新 open_enroll_page——残留勾选有重复提交风险；
      - 半程（live=False）同理逐 SPU 一页：填好的页签逐 SPU 留给操作者人工核对提交；
      - 详情查询 0 行（无资格）/逐格跳过的 SPU 没有填价现场，页面直接复用给下一 SPU。

    control 给定时：活动之间可暂停（安全边界），内层可逐格跳过（见 app/activity/control.py）。
    """
    for act_name, items in by_activity.items():
        # 暂停只挂在活动之间：全程模式下每 SPU 提交后提报页都已在循环里关掉（或还没开），
        # 此处挂起不会留下操作到一半的 detail-new tab；半程保留的页签是填好价的完整现场。
        await _pause_gate(control, on_progress, "enroll", note=f"活动「{act_name}」开始前暂停")
        halt = None
        filled = []          # 填价成功的 SPU（=本活动要提交的）
        attempted = []       # 提交动作已被页面接受的 SPU（live；半程恒空）
        page_verified = []   # 提交当场拿到明确成功回执（结果页/成功提示）的 SPU
        submit_notes = []
        ineligible = []
        skipped = []
        page = None          # 当前提报页；只在「上面没有未提交的填价现场」时才复用
        try:
            for index, it in enumerate(items):
                # 执行中逐格跳过：操作者在「报名计划」表上点了跳过。跳过只影响本格，
                # 该 SPU 的流量等 SPU 级动作不受影响（阶段三照常重开）。
                if control is not None and control.is_skipped(it["spu"], act_name):
                    skipped.append(it["spu"])
                    await _emit(on_progress, {
                        "type": "exec_cell_skip", "spu": it["spu"], "activity": act_name,
                        "where": "queued", "note": "已按操作者指令跳过该商品在该活动的报名",
                    })
                    continue
                # 上一 SPU 填过价/提过交的页面已在各自分支留下或关掉（page 置 None）——
                # 搜索会重渲结果表格，上一 SPU 的现场保不住也不该保；page 非 None 说明
                # 上面只有跳过/详情不符的探测现场、或 over_ref 拦截留下的勾选（未填价未
                # 提交，下一 SPU 搜索重渲时随旧行一并卸载），均可安全复用。
                if page is None:
                    page = await pipeline.open_enroll_page(activity_page, act_name)
                    if page is None:
                        note = "打开提报页失败，已记录并继续扫描下一活动"
                        for later in items[index:]:
                            if control is not None and control.is_skipped(later["spu"], act_name):
                                continue
                            summary["scan_failures"].append({
                                "activity": act_name, "spu": later["spu"],
                                "step": "open_activity", "note": note,
                            })
                        await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                                  "spu": None, "step": "open_activity", "ok": False,
                                                  "note": note})
                        halt = {"activity": act_name, "spu": None,
                                "step": "open_activity", "note": note}
                        break
                    await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                              "spu": None, "step": "open_activity", "ok": True,
                                              "note": "活动详情页已打开并核对活动名"})
                try:
                    async def on_step(event, current=it):
                        await _emit(on_progress, {
                            "type": "exec_rpa_step", "activity": act_name,
                            "spu": current["spu"], **event,
                        })

                    r = await pipeline.enroll_activity(page, it["spu"], act_name,
                                                       it["sku_prices"], allow_submit=False,
                                                       on_step=on_step,
                                                       discount_rate=it.get("discount_rate"))
                    if r.get("detail_eligible") is False:
                        ineligible.append(it["spu"])
                        summary["ineligible_activities"].setdefault(act_name, []).append(it["spu"])
                        await _emit(on_progress, {
                            "type": "exec_activity_skip", "activity": act_name,
                            "spu": it["spu"], "reason": "detail_ineligible",
                            "note": r.get("note", "详情页无可报名商品行"),
                        })
                    elif r.get("filled"):
                        filled.append(it["spu"])
                        # 平台侧各货号的真实日常价（提报页读到的）：阶段三设加速价时按它配对
                        # 对话框的「日常价档」行——表里的价可能与平台不一致（用户口径：以表为准
                        # 算价，但配对得用平台自己的价档）。
                        summary.setdefault("platform_daily", {}).setdefault(
                            it["spu"], {}).update(r.get("platform_daily") or {})
                        # 填价成功后锁定这一格：随后立即提交（或半程保留页签），之后再跳过是假的。
                        if control is not None:
                            control.lock_cell(it["spu"], act_name)
                    else:
                        # 未填成功：逐条记入 failed（over_ref 标注疑数据不一致），供操作者核对。
                        summary["failed"].append({
                            "spu": it["spu"], "activity": act_name,
                            "submit_price": it.get("submit_price"),
                            "sku_count": len(it.get("sku_prices") or []),
                            "ref_price": r.get("ref_price"),
                            "over_ref": bool(r.get("over_ref")),
                            "reason": r.get("note", "填价未成功"),
                        })
                        # over_ref 是本 SPU 自己的加速价压制（SPU 级价格问题），不连坐同活动
                        # 其余 SPU——2026-10-03 批次 8791757215 被压制 halt 掉限时秒杀等 6 个
                        # 活动，排后面的 3822224199 全部没轮到处理、零提交漏报。其余填价失败
                        # （页面/活动级问题）仍 fail-fast：继续填多半同样失败。
                        if not r.get("over_ref"):
                            halt = {
                                "activity": act_name, "spu": it["spu"],
                                "step": r.get("failed_step") or "fill_price",
                                "note": r.get("note", "当前活动操作未完成"),
                            }
                    await _emit(on_progress, {"type": "exec_fill", "activity": act_name,
                                              "spu": it["spu"], "ok": r.get("filled", False),
                                              "over_ref": bool(r.get("over_ref")),
                                              "ineligible": r.get("detail_eligible") is False,
                                              "sku_count": len(it.get("sku_prices") or []),
                                              "ref_price": r.get("ref_price"),
                                              "submit_price": it.get("submit_price"),
                                              "note": r.get("note", "")})
                    if halt:
                        break
                    if r.get("filled") and live:
                        # ---- 逐 SPU 立即提交（2026-10-03 起；不再攒到活动末统一提交）----
                        # expected_spus 让提交层在点按钮前核对勾选还在、结果页数量对账。
                        try:
                            sub = await pipeline.submit_enroll_page(
                                page, allow=True, expected_spus=[it["spu"]])
                        except Exception as e:
                            sub = {
                                "submitted": False, "clicked_submit": False,
                                "note": f"提交调用异常，待记录页对账：{str(e)[:80]}",
                            }
                        sub_note = sub.get("note", "")
                        if sub_note:
                            submit_notes.append(f"{it['spu']}：{sub_note}")
                        sub_attempted = bool(sub.get("submitted") or sub.get("clicked_submit"))
                        if sub_attempted:
                            attempted.append(it["spu"])
                            if sub.get("verified"):
                                page_verified.append(it["spu"])
                        await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                                  "spu": it["spu"], "step": "submit",
                                                  "ok": sub_attempted, "note": sub_note})
                        # 提交过的页面一律关掉（结果页或不确定现场都不复用），下一 SPU 开新页。
                        # submit_enroll_page 返回前已等页面跳到 detail-new-result 结果页
                        # （2026-09-30 用户要求：提交后不能马上关页签，要等跳转完），此处只管关。
                        try:
                            await page.close()
                        except Exception as e:
                            logger.warning(f"关提报页 tab 失败（{act_name}/{it['spu']}）：{e}")
                        page = None
                        if not sub_attempted:
                            # 提交没发出去 = 这一格没报上；fail-fast 不再处理本活动后续 SPU
                            # （按钮持续禁用/弹窗点不动多半是活动级问题，继续只会同样失败）。
                            summary["failed"].append({
                                "spu": it["spu"], "activity": act_name,
                                "submit_price": it.get("submit_price"),
                                "sku_count": len(it.get("sku_prices") or []),
                                "reason": f"提交未成功：{sub_note or '提交按钮未接受'}",
                            })
                            halt = {"activity": act_name, "spu": it["spu"], "step": "submit",
                                    "note": sub_note or "提交未完成"}
                            break
                    elif r.get("filled"):
                        # 半程：已填价的页签逐 SPU 留给操作者人工核对+手动点提交——半程的
                        # 意义就是「管线填好、人来把关」，关了等于白填；一张页留不下两个
                        # SPU 的填价现场（搜索会重渲表格），故逐 SPU 各留一张。
                        try:
                            probe = await pipeline.submit_enroll_page(page, allow=False)
                            if probe.get("note"):
                                submit_notes.append(f"{it['spu']}：{probe['note']}")
                        except Exception as e:
                            logger.warning(f"半程定位提交按钮失败（{act_name}/{it['spu']}）：{e}")
                        logger.info(f"[活动] 半程模式保留提报页页签供人工核对提交："
                                    f"{act_name}/{it['spu']}")
                        page = None  # 页签保留不关；下一 SPU 开新页
                except Exception as e:
                    summary["failed"].append({"spu": it["spu"], "activity": act_name,
                                              "submit_price": it.get("submit_price"),
                                              "sku_count": len(it.get("sku_prices") or []),
                                              "reason": f"异常：{e}"})
                    await _emit(on_progress, {"type": "exec_fill", "activity": act_name,
                                              "spu": it["spu"], "ok": False, "note": f"异常：{e}"})
                    halt = {"activity": act_name, "spu": it["spu"],
                            "step": "unknown", "note": f"异常：{e}"}
                    break
            # 活动级汇总：filled/attempted 已逐 SPU 落定，这里只组装 summary 与事件，不再有
            # 统一提交动作。submitted_attempts 记【实际提交出去】的 SPU——对账据此区分
            # 「提交过待核验」与「根本没提交」，中断前已提交的部分不能丢（旧统一提交模型下
            # 中断=全员未提交，没有部分提交可言）。
            summary["filled_activities"][act_name] = filled
            summary["submitted_attempts"][act_name] = list(attempted)
            # 最终 enrolled_activities 只由扫描结束后的报名记录页对账写入。
            summary["enrolled_activities"].setdefault(act_name, [])
            actionable_count = len(items) - len(ineligible) - len(skipped)
            if halt is None and len(filled) != actionable_count:
                halt = {"activity": act_name, "spu": None, "step": "fill_price",
                        "note": "当前活动有商品未完成填价"}
            if actionable_count == 0 and halt is None:
                parts = []
                if ineligible:
                    parts.append(f"{len(ineligible)} 个商品详情页无可报名商品")
                if skipped:
                    parts.append(f"{len(skipped)} 个商品按操作者指令跳过")
                note = "；".join(parts) + "，跳过当前活动并继续下一活动"
                # 没有任何「报了但没成」的商品：详情不可报是平台正常业务结果、跳过是操作者选择，
                # 两种都不是失败，前端按 info 展示。
                status = "skipped_by_user" if skipped and not ineligible else "detail_ineligible"
                summary["activity_results"][act_name] = {
                    "filled": [], "ineligible": ineligible, "skipped": skipped,
                    "submitted": False, "status": status, "note": note,
                }
                await _emit(on_progress, {"type": "exec_enroll", "activity": act_name,
                                          "ok": False, "submitted": False, "filled": [],
                                          "status": status,
                                          "ineligible": ineligible, "skipped": skipped,
                                          "live": live, "note": note})
            else:
                submitted = live and bool(filled) and len(attempted) == len(filled)
                clicked_submit = bool(attempted)
                feedback_verified = bool(attempted) and len(page_verified) == len(attempted)
                if halt is not None:
                    note = halt.get("note", "当前活动操作未完整完成")
                elif not live and filled:
                    note = (f"已填价 {len(filled)} 个商品，提报页页签已逐商品保留，"
                            f"可人工核对后手动提交")
                else:
                    note = "；".join(submit_notes) or "等待报名记录页最终核验"
                summary["activity_results"][act_name] = {
                    "filled": filled, "submitted": submitted,
                    "clicked_submit": clicked_submit, "feedback_verified": feedback_verified,
                    "verified": False, "status": "pending_log" if clicked_submit else "submit_failed",
                    "note": note,
                }
                if halt is not None:
                    partial = f"已提交 {len(attempted)} 个（{'、'.join(attempted)}），" if attempted else ""
                    await _emit(on_progress, {"type": "exec_enroll", "activity": act_name,
                                              "ok": False,
                                              "submitted": submitted,
                                              "clicked_submit": clicked_submit,
                                              "verified": False,
                                              "feedback_verified": feedback_verified,
                                              "filled": filled, "live": live,
                                              "note": f"当前活动操作未完整完成，{partial}"
                                                      f"记录后继续：{note}"})
                else:
                    await _emit(on_progress, {"type": "exec_enroll", "activity": act_name,
                                              "ok": clicked_submit if live else bool(filled),
                                              "submitted": submitted,
                                              "clicked_submit": clicked_submit,
                                              "verified": False,
                                              "feedback_verified": feedback_verified,
                                              "filled": filled, "live": live, "note": note})
        finally:
            # 退出路径上关掉【没提交、也没保留给操作者】的当前页：live 下提交过的页在循环里
            # 已关，半程填好的页签在循环里已留下（page 已置 None，不会走到这里）。
            # 残留页不干扰后续开页：open_enroll_page 的 diff 按 Page 对象身份认新 tab
            # （非 URL），残留 detail-new 都在 before 快照里（2026-09-30 多残留实测验证）。
            if page is not None:
                try:
                    await page.close()
                except Exception as e:
                    logger.warning(f"关提报页 tab 失败（{act_name}）：{e}")
        if halt:
            summary["scan_failures"].append({"activity": act_name, **halt})
            await _emit(on_progress, {"type": "log", "level": "warning",
                                      "message": "当前活动未完整完成，已记录并继续扫描下一活动"})


async def run_activity_batch(
    spus,
    excel: str,
    sheet: str,
    min_margin: Optional[float] = None,
    dry_run: bool = False,
    on_progress=None,
    flux_page=None,
    activity_page=None,
    goods_page=None,
    stock_map=None,
    live: bool = False,
    activity_allow=None,
    region_label: str = "",
    cloud_url: str = "",
    selection=None,
    control=None,
) -> dict:
    """跑一批 SPU 的活动管理编排（关流量→报名→开流量），进度经 on_progress 抛出。

    - spus：parse_spu_list 产出的 [{spu, margin}] 列表；也接受原始字符串（内部转解析）或
      裸 SPU 字符串列表。
    - cloud_url：WPS 在线成本表分享链接；excel 参数兼容旧调用，但也只接受在线链接。
    - min_margin：保留兼容，价格筛选只使用文档日常价与销售底价。
    - dry_run=False（默认，2026-09-30 起）：跑执行遍（活动维度：关流量→报名→重开流量）。
      其中 live 再分两档——live=False（半程）：开提报页/填价但【不提交、不真关/开】，全部可逆，
      用于端到端验证；live=True（全程）：真提交/真关流量/真开流量，不可逆，须调用方显式授权。
    - dry_run=True：只算计划、发 product_plan，绝不点任何变更按钮、不跑执行遍；由调用方
      （Web 页的「正式执行」开关关掉、CLI 的 --dry-run）显式要求，不再作为默认。
    - selection：识别矩阵勾选的 [[spu, 活动名], ...]；给定时只报这些格子（没勾的 SPU 连
      流量都不动）。None = 不限制，与改造前一致。
    - control：ActivityControl，给定时可在安全边界暂停/继续、执行中逐格跳过。
    - flux_page/activity_page：测试注入用；均为 None 时走 CDP 连真实调试 Chrome 的已打开标签。
    返回汇总 {done, skip, fail, results:[每 SPU 结果], exec:执行遍汇总 或 None}。
    """
    # 失败事件切面：aborted / product_done fail 自动上报公网 MySQL（best-effort）。
    on_progress = attach(on_progress, "activity")
    # 活动历史切面：live 批次的报名结论/加速器开关落 activity_history 表（best-effort，
    # 未启用/写失败零感知）。包在 error_report 之后，两个切面互不感知。
    on_progress = history.attach_history(on_progress, region=region_label)
    bm = _normalize_margin(min_margin)
    if bm is None:
        bm = global_min_margin()

    # 归一化 spus 为 [{spu, margin}]
    if isinstance(spus, str):
        entries = parse_spu_list(spus, bm)
    else:
        entries = []
        seen: set = set()
        for it in spus or []:
            if isinstance(it, dict):
                spu = str(it.get("spu", "")).strip()
                mg = _normalize_margin(it.get("margin"))
                mg = mg if mg is not None else bm
            else:
                spu = str(it).strip()
                mg = bm
            if spu and spu.isdigit() and spu not in seen:
                seen.add(spu)
                entries.append({"spu": spu, "margin": mg})

    total = len(entries)
    if total == 0:
        await _emit(on_progress, {"type": "aborted", "reason": "SPU 清单为空或无有效 SPU。"})
        logger.error("活动批次：SPU 清单为空。")
        return {"done": 0, "skip": 0, "fail": 0, "results": []}

    try:
        document = source.validate_document(cloud_url or excel)
        if not sheet:
            raise ValueError("请先选择 WPS 文档的工作表。")
        await _emit(on_progress, {
            "type": "log", "level": "info", "message": "正在重新读取 WPS 文档价格…",
        })
        cost_map = await asyncio.to_thread(
            source.read_costs, document, sheet, [entry["spu"] for entry in entries],
        )
    except Exception as exc:
        await _emit(on_progress, {"type": "aborted", "reason": f"WPS 数据源读取失败：{exc}"})
        return {"done": 0, "skip": 0, "fail": total, "results": []}

    # 页面句柄：测试可注入；否则连 CDP 取已打开的流量页/活动页标签。
    injected = flux_page is not None or activity_page is not None
    pw = browser = None
    owned_pages = []
    if not injected:
        if not await ensure_cdp_alive():
            await _emit(on_progress, {
                "type": "aborted",
                "reason": "CDP 不可用（Chrome 未以 9222 调试端口运行？），已中止本批。",
            })
            return {"done": 0, "skip": 0, "fail": 0, "results": []}
        try:
            (pw, browser, flux_page, activity_page, goods_page,
             owned_pages) = await _connect_pages(CDP_URL, region_label)
        except Exception as e:
            await _emit(on_progress, {"type": "aborted", "reason": f"连接 CDP 失败：{e}"})
            logger.error(f"活动批次：连接 CDP 失败：{e}")
            return {"done": 0, "skip": 0, "fail": 0, "results": []}
        if flux_page is None or activity_page is None:
            miss = []
            if flux_page is None:
                miss.append("流量页(flux-analysis)")
            if activity_page is None:
                miss.append("活动页(marketing-activity)")
            reason = f"无法准备 {'、'.join(miss)} 标签，已中止本批。"
            await _emit(on_progress, {"type": "aborted", "reason": reason})
            logger.error("活动批次：" + reason)
            if browser is not None:
                try:
                    await browser.close()
                    await pw.stop()
                except Exception:
                    pass
            return {"done": 0, "skip": 0, "fail": 0, "results": []}

    done = skip = fail = 0
    results: list[dict] = []
    exec_summary = None  # 执行遍汇总（dry-run 保持 None）
    # 历史摘要一次性批量取（单条 WHERE spu IN，不在循环里逐 SPU 往返）：规划事件带
    # 「上次报名结论/加速器上次开启时间」徽标数据；dry-run 也查——计划表徽标正是
    # dry-run 决策时用的。查不到降级 {}，主流程零感知。
    history_map = await history.summarize([entry["spu"] for entry in entries])
    try:
        logger.info(
            f"=== 活动批次：SPU {total} 个，dry_run={dry_run}，本批毛利率默认={bm} "
            f"工作簿={excel} Sheet={sheet} ==="
        )
        await _emit(on_progress, {
            "type": "batch_start", "total": total, "todo": total,
            "batch": total, "dry_run": dry_run,
        })
        if control is not None:
            control.set_phase("planning")

        for i, entry in enumerate(entries, 1):
            spu = entry["spu"]
            # 暂停闸门放在 SPU 之间、wait_for 之外：放进去会被 180s 超时打断并误判该 SPU 失败。
            await _pause_gate(control, on_progress, "planning", note=f"SPU {spu} 规划前暂停")
            await _emit(on_progress, {
                "type": "product_start", "index": i, "total": total, "spu": spu, "name": "",
            })
            # 单商品护栏：清零 LLM 单例 token（跨商品累加会静默撞上限）。best-effort。
            try:
                reset_pipeline_llms()
            except Exception as e:
                logger.warning(f"reset_pipeline_llms 异常（忽略）：{e}")

            try:
                res = await asyncio.wait_for(
                    _process_one_spu(
                        entry, flux_page, activity_page, dry_run,
                        on_progress, stock_map,
                        cost_map, selection=selection,
                        history_map=history_map,
                    ),
                    timeout=ACTIVITY_PRODUCT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(f"SPU={spu} 活动流程超时（>{ACTIVITY_PRODUCT_TIMEOUT}s）")
                res = {
                    "spu": spu, "status": "fail", "accel_closed": False, "enrolled": False,
                    "submit_price": None, "accel_reopened": False, "verified": False,
                    "note": "超时",
                }
            except Exception as e:
                logger.error(f"SPU={spu} 活动流程异常：{e}")
                res = {
                    "spu": spu, "status": "fail", "accel_closed": False, "enrolled": False,
                    "submit_price": None, "accel_reopened": False, "verified": False,
                    "note": f"异常：{e}",
                }

            results.append(res)
            _filter_result(res, selection, activity_allow)
            status = res.get("status", "fail")
            if status == "done":
                done += 1
            elif status == "fail":
                fail += 1
            else:  # skip_redline / skip_nomatch / skip_nocost
                skip += 1

            await _emit(on_progress, {
                "type": "product_done", "spu": spu, "status": status,
                "accel_closed": res.get("accel_closed", False),
                "enrolled": res.get("enrolled", False),
                "submit_price": res.get("submit_price"),
                "enrolled_activities": res.get("enrolled_activities", []),
                "accel_reopened": res.get("accel_reopened", False),
                "verified": res.get("verified", False),
                # 历史上次成功开启加速器的时间（activity_history 摘要）：前端据此给
                # 「24h 内开启过，关闭可能撞锁定期」预警徽标（仅展示，不改流程）。
                "last_accel_open_at": ((history_map.get(spu) or {})
                                       .get("last_accel_open") or {}).get("at"),
                "accel_open_within_24h": bool(((history_map.get(spu) or {})
                                               .get("last_accel_open") or {})
                                              .get("within_24h")),
                "note": res.get("note", ""),
            })

        # 报名范围已在每个 SPU 处理完时裁剪（_filter_result），此处不再统一过滤：
        # 统计与 product_done 必须与真正要跑的范围一致。

        # 执行遍（活动维度：关流量→报名→重开流量）。dry_run 时整段跳过（纯规划）。
        # live=False 半程可逆、live=True 全程不可逆（须授权）。异常不吞：记 error 事件、不中断汇总。
        if not dry_run:
            try:
                exec_summary = await _run_execution_phases(
                    results, flux_page, activity_page, live, on_progress, control=control
                )
            except Exception as e:
                logger.error(f"活动批次：执行遍异常：{e}")
                await _emit(on_progress, {"type": "log", "level": "error",
                                          "message": f"执行遍异常：{e}"})
                exec_summary = {"errors": [str(e)]}

            # 正式执行的最终统计必须来自执行结果，不能沿用规划遍的 status=done。
            if live:
                planning_fail = fail
                product_results = exec_summary.get("product_results") or []
                done = sum(1 for item in product_results if item.get("status") == "done")
                execution_skip = sum(1 for item in product_results if item.get("status") == "skip")
                execution_fail = sum(1 for item in product_results if item.get("status") == "fail")
                planned_spus = sum(
                    1 for item in results
                    if item.get("status") == "done" and item.get("enrolled_activities")
                )
                # 执行遍整体异常、未生成逐品结果时，所有已规划 SPU 都按失败计。
                if not product_results and planned_spus:
                    execution_fail = planned_spus
                skip += execution_skip
                fail = planning_fail + execution_fail
    finally:
        if not injected and browser is not None:
            # 仅关闭本批自动创建的页签；用户原有页签保持原样。
            for page in reversed(owned_pages):
                try:
                    await page.close()
                except Exception as e:
                    logger.warning(f"关闭自动创建的任务页签失败：{e}")
            try:
                await browser.close()
                await pw.stop()
            except Exception:
                pass

    logger.info(f"=== 活动批次完成：done={done} skip={skip} fail={fail} ===")
    await _emit(on_progress, {
        "type": "batch_done", "done": done, "skip": skip, "fail": fail,
        "dry_run": dry_run, "live": live,
    })
    return {"done": done, "skip": skip, "fail": fail, "results": results, "exec": exec_summary}


async def scan_activity_matrix(
    spus,
    excel: str = "",
    sheet: str = "",
    *,
    on_progress=None,
    control=None,
    activity_page=None,
    cloud_url: str = "",
    region_label: str = "",
    day: str = "",
    force: bool = False,
) -> dict:
    """只读识别：算出「商品×活动」矩阵，并对价格初筛通过的格子逐格探测详情页资格。

    只读是硬约束：不勾选、不设场次、不填价、不提交、不碰流量（全程可逆，可随时重跑）。
    流程：读一次成本表 → 连 CDP（只开活动页）→ **读一次活动列表** → 本地建骨架
    （每个 SPU 的每个活动一行格子，含未达底价的）→ 发 scan_start 让矩阵先整体出现 →
    剪掉一个候选都没有的活动（活动页有近百个活动，不剪枝要为注定不报的格子白开几十个页）
    → 逐活动：开一次提报页、逐格探测、关页 → 结果并进当天文件。

    资格探测结果当天复用（force=True 忽略缓存全量重探）：命中缓存的格子直接回填、
    不再开页。价格层每次都从成本表重算，不进缓存——折扣率与日常价改了自动反映。

    返回 {"day", "path", "activities", "cells", "counts", "scanned", "from_cache"}。
    """
    from app.activity import matrix as matrix_store

    # 扫描只读、不产生平台事实，不挂 history 切面（它只认 exec_* 事件，挂上也是透传）；
    # 但历史【摘要】照常在下方注入 scan_start 的格子，供矩阵画「上次结论」徽标。
    on_progress = attach(on_progress, "activity")
    day = day or matrix_store.today()
    if control is not None:
        control.set_phase("scan")
    if isinstance(spus, str):
        # 扫描不参与毛利率筛选，但仍走同一个解析器（去重 + 过滤非法 token）。
        entries = parse_spu_list(spus, global_min_margin())
    else:
        entries = [{"spu": str(it.get("spu") if isinstance(it, dict) else it).strip()}
                   for it in (spus or [])]
    entries = [e for e in entries if e["spu"].isdigit()]
    if not entries:
        await _emit(on_progress, {"type": "aborted", "reason": "SPU 清单为空或无有效 SPU。"})
        return {"day": day, "cells": [], "counts": {}}
    spu_list = [e["spu"] for e in entries]
    # 历史摘要一次性批量取（单条 WHERE spu IN）：注入 scan_start 的格子画「上次结论」
    # 徽标；查不到降级 {}，扫描主流程零感知。
    history_map = await history.summarize(spu_list)

    # 1. 读成本表（宽松模式：单个商品缺价只影响它自己那几格，不该让整张矩阵扫不出来）。
    try:
        document = source.validate_document(cloud_url or excel)
        if not sheet:
            raise ValueError("请先选择 WPS 文档的工作表。")
        await _emit(on_progress, {"type": "log", "level": "info", "message": "正在读取 WPS 文档价格…"})
        snapshot = await asyncio.to_thread(source.read_document, document, sheet)
        cost_map = source.costs_from_snapshot(snapshot, spu_list, strict=False)
    except Exception as exc:
        await _emit(on_progress, {"type": "aborted", "reason": f"WPS 数据源读取失败：{exc}"})
        return {"day": day, "cells": [], "counts": {}}

    # 2. 连 CDP（注入 activity_page 时不连，单测走这条路）。
    injected = activity_page is not None
    pw = browser = None
    owned_pages = []
    if not injected:
        if not await ensure_cdp_alive():
            await _emit(on_progress, {
                "type": "aborted",
                "reason": "CDP 不可用（Chrome 未以 9222 调试端口运行？），已中止本次识别。",
            })
            return {"day": day, "cells": [], "counts": {}}
        try:
            pw, browser, _flux, activity_page, _goods, owned_pages = await _connect_pages(
                CDP_URL, region_label, need_flux=False)
        except Exception as e:
            await _emit(on_progress, {"type": "aborted", "reason": f"连接 CDP 失败：{e}"})
            return {"day": day, "cells": [], "counts": {}}

    try:
        # 3. 活动列表只读一次（不是每个 SPU 一次：那是 N 倍的无谓开销）。
        activities = await pipeline.read_activities(activity_page) if activity_page else []
        if not activities:
            await _emit(on_progress, {"type": "aborted",
                                      "reason": "活动页没读到任何活动，请确认页面已加载。"})
            return {"day": day, "cells": [], "counts": {}}

        # 4. 本地建骨架：逐 SPU 算价（与规划遍同一份规则），含未达底价的格子。
        price: dict = {}
        for entry in entries:
            spu = entry["spu"]
            items = ((cost_map.get(spu) or {}).get("items")) or []
            if not items:
                # 成本表里这个 SPU 不可用（缺失/某货号行价格无效整组连坐）：把原因摊到每格上，
                # 否则矩阵里整行消失，操作者只会以为「没这个商品」。
                reason = _spu_cost_issue(snapshot, spu)
                price[spu] = {"items": [], "cells": {
                    act["name"]: {"verdict": "no_cost", "discount_rate": act.get("discount_rate"),
                                  "min_stock": act.get("min_stock"), "skus": [], "sku_count": 0,
                                  "submit_price": None, "floor_price": None,
                                  "within_floor": False, "note": reason}
                    for act in activities}}
                continue
            plan = plan_spu_activities(items, activities)
            price[spu] = {"items": items, "cells": plan["cells"]}

        prev = matrix_store.load(day)
        cached = prev.get("eligibility") or {}
        cells = matrix_store.flatten_cells({"price": price, "eligibility": cached})
        counts_now = matrix_store.counts({"price": price, "eligibility": cached})
        # 逐格注上次报名结论（activity_history 摘要，只进事件不进矩阵落盘文件——
        # 历史有自己的库，矩阵文件只存资格这个客观事实）；无历史/未启用为 None。
        for cell in cells:
            cell["history"] = ((history_map.get(str(cell.get("spu"))) or {})
                               .get("activities") or {}).get(cell.get("activity"))
        await _emit(on_progress, {
            "type": "scan_start", "day": day, "spu_total": len(entries),
            "activity_total": len(activities), "cached": counts_now["eligible"] + counts_now["ineligible"],
            "path": matrix_store.matrix_path(day),
            "activities": activities, "cells": cells, "counts": counts_now,
        })

        # 5. 逐活动开一次提报页，只探「价格初筛通过且缓存未命中」的格子。
        targets = {}  # 活动名 → 待探 SPU
        for spu, entry in price.items():
            for name, cell in (entry.get("cells") or {}).items():
                if cell.get("verdict") != "pass":
                    continue
                hit = (cached.get(spu) or {}).get(name)
                if not force and hit and hit.get("eligible") is not None:
                    continue
                targets.setdefault(name, []).append(spu)
        todo = [name for name in targets]
        from_cache = counts_now["eligible"] + counts_now["ineligible"]
        probed = 0
        for index, name in enumerate(todo, 1):
            await _pause_gate(control, on_progress, "scan", note=f"活动「{name}」识别前暂停")
            await _emit(on_progress, {"type": "scan_activity_start", "activity": name,
                                      "index": index, "total": len(todo),
                                      "probe_count": len(targets[name])})
            page = None
            try:
                page = await pipeline.open_enroll_page(activity_page, name)
                for spu in targets[name]:
                    await _pause_gate(control, on_progress, "scan",
                                      note=f"SPU {spu} 在活动「{name}」识别前暂停")
                    if page is None:
                        result = {"detail_eligible": None, "note": "打开提报页失败，未探测"}
                    else:
                        result = await pipeline.probe_detail_eligibility(page, spu)
                    eligible = result.get("detail_eligible")
                    note = result.get("note", "")
                    if eligible is None:
                        # fail-closed：不落盘（一次卡顿不该被当成永久结论复用），下次重探。
                        probed += 1
                        await _emit(on_progress, {
                            "type": "activity_cell", "spu": spu, "activity": name,
                            "eligible": None, "verdict": "probe_failed",
                            "from_cache": False, "scanned_at": "", "note": note,
                        })
                        continue
                    record = {"eligible": eligible, "note": note,
                              "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    cached.setdefault(spu, {})[name] = record
                    probed += 1
                    await _emit(on_progress, {
                        "type": "activity_cell", "spu": spu, "activity": name,
                        "eligible": eligible,
                        "verdict": "eligible" if eligible else "ineligible",
                        "from_cache": False, "scanned_at": record["scanned_at"], "note": note,
                    })
            except Exception as e:
                # 单个活动开页/探测异常不该让整轮识别白跑（识别是长跑且只读）：记 warning、
                # 继续下一个活动。已经探到的格子照常落盘，出事的格子留给下次重探。
                logger.warning(f"识别活动「{name}」异常：{e}")
                await _emit(on_progress, {"type": "log", "level": "warning",
                                          "message": f"识别活动「{name}」异常，已跳过：{e}"})
            finally:
                # 与报名遍同一条纪律：每个活动用完立刻关掉 detail-new 页，绝不留下残留 tab。
                if page is not None:
                    try:
                        await page.close()
                    except Exception as e:
                        logger.warning(f"关闭提报页失败（{name}）：{e}")

        # 6. 并库落盘：消失的活动整列丢弃，缓存只保留仍是真/假结论的格子。
        data = matrix_store.apply_scan(prev, day, document, sheet, region_label,
                                       activities, price, cached)
        path = matrix_store.save(data)
        counts = data["counts"]
        await _emit(on_progress, {
            "type": "scan_done", "day": day, "path": path,
            "eligible": counts["eligible"], "ineligible": counts["ineligible"],
            "unknown": counts["unknown"], "cached": from_cache, "probed": probed,
            "activities": len(todo),
        })
        # 返回值的 cells 同样注历史摘要（与 scan_start 下发的那批同口径）。
        result_cells = matrix_store.flatten_cells(data)
        for cell in result_cells:
            cell["history"] = ((history_map.get(str(cell.get("spu"))) or {})
                               .get("activities") or {}).get(cell.get("activity"))
        return {"day": day, "path": path, "activities": activities,
                "cells": result_cells, "counts": counts,
                "scanned": probed, "from_cache": from_cache}
    finally:
        if not injected and browser is not None:
            for page in reversed(owned_pages):
                try:
                    await page.close()
                except Exception as e:
                    logger.warning(f"关闭自动创建的任务页签失败：{e}")
            try:
                await browser.close()
                await pw.stop()
            except Exception:
                pass


def _spu_cost_issue(snapshot: dict, spu: str) -> str:
    """从文档快照里找出该 SPU 不可用的原因（缺失 / 某货号行价格无效连坐）。"""
    rows = [r for r in snapshot.get("rows", []) if r.get("spu") == spu]
    if not rows:
        return "成本表里没有这个 SPU（可能已被删除或 SPU 号写错）"
    issues = [i for row in rows for i in (row.get("issues") or [])]
    detail = "；".join(dict.fromkeys(issues)) or "价格无效"
    return f"成本表该 SPU 不可选：{detail}"
