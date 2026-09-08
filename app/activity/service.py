"""活动管理服务层：把「关流量加速器 → 报活动 → 重开加速器」的逐 SPU 编排抽成
UI / CLI 共用入口，进度经【结构化回调】抛出——对标 app/collect/service.py。

为什么单独一层（与采集同理）：这是【确定性批处理作业】，不是给大模型 function-calling
用的 tool，也用不上 LangGraph。故不封 BaseTool，而抽成 service：CLI（activity_manage.py）
和 UI（app.py 的 /activity 接口）都调这里。进度以结构化事件（dict）经 on_progress 抛出，
UI 直接走 SSE 渲染。

判定逻辑（2026-07-16 用户重定义，见 pipeline 模块 docstring）：确定性筛选——申报价 ≥ 销售底价
(Excel 销售价列) 且 库存 ≥ 活动库存门槛 的活动全部报名（满足的都报，一个 SPU 报多个活动）。
销售底价完全替代旧毛利率红线；不再用 LLM 选活动。

最高优先级安全约束（真实商家账号、操作不可逆）：
- 阶段1 只做【只读 + dry-run】：只定位/只读/算计划/发 product_plan 事件，绝不点任何变更
  按钮（关加速/报名/开加速）。变更函数在 pipeline 里留桩、正式分支 raise NotImplementedError。
- 保守跳过：日常价或销售底价读不到（含公式单元格 `=` 开头）→ skip_nofloor；无任何活动同时
  满足达底价+够库存 → skip_nomatch。一律不提交。库存读不到时，卡门槛的活动视为不达标（保守）。
- 辅助路径（进度回调、CDP 探活、库存读取）best-effort：异常只 logger.warning 吞掉、不中断主流程。

事件契约（on_progress 收到的 dict，均含 "type"）：
    {"type":"batch_start","total":int,"todo":int,"batch":int,"dry_run":bool}
    {"type":"product_start","index":int,"total":int,"spu":str,"name":str}
    # product_plan：一个 SPU 会发多条（每个达销售底价的活动一条）
    {"type":"product_plan","spu":str,"activity":str,"daily_price":float,"sale":float,
                           "cost":float|None,"submit_price":float,"within_floor":bool,
                           "stock":int|None,"min_stock":int|None,"stock_ok":bool,
                           "selected":bool,"reason":str}
    {"type":"product_done","spu":str,"status":"done"|"skip_nofloor"|"skip_nomatch"|"fail",
                           "accel_closed":bool,"enrolled":bool,"submit_price":float|None,
                           "enrolled_activities":list,"accel_reopened":bool,"verified":bool,"note":str}
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
                                   "set_sessions"|"fill_price"|"submit",
                            "ok":bool,"note":str}
    {"type":"exec_activity_skip","activity":str,"spu":str,
                                "reason":"detail_ineligible","note":str}
    # exec_fill：over_ref=True 表示申报价超过提报页参考价（疑 Excel 日常价与前端实际售价不一致），
    #            已跳过未报名；ref_price 为读到的参考价上限。
    {"type":"exec_fill","activity":str,"spu":str,"ok":bool,"submit_price":float,
                        "over_ref":bool,"ref_price":float|None,"note":str}  # 提报页填价
    {"type":"exec_enroll","activity":str,"ok":bool,"submitted":bool,"verified":bool,
                          "filled":list,"live":bool,"note":str}     # 单活动提交
    {"type":"exec_log_verify","activity":str,"spu":str,"ok":bool,
                              "status":str,"note":str}               # 报名记录页最终对账
    {"type":"exec_reopen","spu":str,"ok":bool,"live":bool,"note":str}      # 重开流量
    {"type":"exec_product_done","spu":str,"status":"done"|"skip"|"fail"|"preview",
                               "planned":int,"submitted":int,"ineligible":int,
                               "accel_ok":bool,"note":str}
    # exec_done.failed：报名未成功的逐条明细 [{spu,activity,submit_price,ref_price,over_ref,reason}]，
    #                   供操作者核对（尤其 over_ref 需更新该商品 Excel 日常价）。
    {"type":"exec_done","closed":int,"reopened":int,"activities":int,"errors":list,
                        "failed":list,"product_results":list}
"""
import asyncio
import re
from typing import Optional

from playwright.async_api import async_playwright

from app.activity import pipeline  # 以模块引用调用其函数，便于测试 monkeypatch（judge_activity 等）
# 复用采集 service 已实测的 CDP 护栏 / 进度回调 / LLM token 清零，避免重复实现。
from app.collect.service import CDP_URL, _emit, ensure_cdp_alive, reset_pipeline_llms
from app.config import PROJECT_ROOT, config_search_dirs
from app.error_report import attach
from app.logger import logger
from app.tool.wps_excel_tool import WpsExcelTool

# 单 SPU 超时护栏（秒）：阶段1 只有只读 + 1 次 LLM，给足余量即可。
ACTIVITY_PRODUCT_TIMEOUT = 180
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
    """读 config.toml [activity].min_margin 作全局默认；缺失/损坏兜底 0.15（best-effort）。"""
    import tomllib

    # 目录维度也要遍历：冻结后 example 只存在于随包只读侧（_internal/config）。
    # 保留原先「只认第一个存在的文件」语义（break），避免 config.toml 显式写了
    # min_margin 却被 example 的默认值盖掉。
    for cfg_dir in config_search_dirs():
        for name in ("config.toml", "config.example.toml"):
            p = cfg_dir / name
            if not p.exists():
                continue
            try:
                with p.open("rb") as f:
                    data = tomllib.load(f)
                m = _normalize_margin((data.get("activity") or {}).get("min_margin"))
                if m is not None:
                    return m
            except Exception as e:
                logger.warning(f"读 [activity].min_margin 失败（用兜底 0.15）：{e}")
            break
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


def _read_cost(excel: str, sheet: str, spu: str) -> dict:
    """按 SPU 从成本核算表读该行 {purchase, daily, sale} 原文（§9）。

    - 各 Sheet 列序不同，先 resolve_field_columns 解析逻辑字段→真实列，绝不硬编码列号。
    - 找不到该 SPU 行返回 {}。纯读、异常内部已被 WpsExcelTool 吞成 {}。
    """
    cols = WpsExcelTool.resolve_field_columns(excel, sheet)
    return WpsExcelTool.read_row_by_key(
        excel,
        sheet,
        key=spu,
        cols={
            "purchase": cols.get("purchase", "J"),
            "daily": cols.get("daily", "G"),
            "sale": cols.get("sale", "I"),
        },
        key_col=cols.get("spu", "D"),
    )


async def _connect_pages(cdp_url: str, region_label: str = ""):
    """连 CDP、确认作业区域，并在【该区域的域名下】新建本批专用的流量、活动、商品页。

    不复用已有页签：其筛选条件、弹窗、局部状态和生命周期都不受管线控制，甚至可能正被
    操作者关闭。三个页面均由本批创建并加入 owned_pages，调用方负责及时关闭；用户原本打开
    的页签只【读一次区域】，不修改、不点击、也不关闭。

    区域为什么必须先确认：顶栏区域切换换的是域名（全球 agentseller.temu.com / 美国
    agentseller-us.temu.com）。本管线自己新开页面，若用写死的全球域，就会把操作者选定的
    美国区悄悄换回全球区——报名/开加速器这类写操作会打在错误的一批商品上。

    region_label：UI 上选定的区域，**以它为准**——浏览器停在别的区域会先切过去再开工作页。
    为空则沿用浏览器当前区域；读不到区域抛 RegionUnconfirmed，绝不默认全球域。
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

        flux_page = await open_owned_page("流量页", pipeline.FLUX_PATH)
        activity_page = await open_owned_page("活动页", pipeline.ACTIVITY_PATH)
        goods_page = await open_owned_page("商品页", pipeline.GOODS_LIST_PATH)
        return pw, browser, flux_page, activity_page, goods_page, owned_pages
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


async def _process_one_spu(
    entry: dict,
    excel: str,
    sheet: str,
    flux_page,
    activity_page,
    dry_run: bool,
    on_progress,
    stock_map: Optional[dict] = None,
) -> dict:
    """单 SPU 确定性流程（重构版），返回结果 dict（供上层生成 product_done 事件）。

    2026-07-16 用户重定义判定：不再 LLM 选一个活动、不再用毛利率红线，改为【确定性筛选】——
    遍历活动页全部活动，把「申报价(=日常价×活动折扣率) ≥ 销售底价(Excel 销售价格列) 且
    商品库存 ≥ 活动库存门槛」的活动全部列入报名计划（满足的都报）。每个入选活动发一条
    product_plan。阶段1 仍全 dry-run、零变更（变更桩绝不在 dry-run 调用）。
    保守跳过：日常价或销售底价读不到 → skip_nofloor；无任何活动入选 → skip_nomatch。
    """
    spu = entry["spu"]
    # 多商品串行时，每个商品开始前都清一次随机公告遮罩；只在业务弹窗出现前调用。
    await pipeline.dismiss_all_page_popups(flux_page)
    await pipeline.dismiss_all_page_popups(activity_page)
    result = {
        "spu": spu, "status": "fail", "accel_closed": False, "enrolled": False,
        "submit_price": None, "accel_reopened": False, "verified": False, "note": "",
        "enrolled_activities": [],
    }

    # 1. 读底价：daily(日常价) + sale(销售价=底价，用户手填常量)。成本 purchase 仅展示、不判定。
    #    daily 或 sale 缺失/公式(=开头)/解析不出 → skip_nofloor（底价读不到，保守跳过）。
    row = _read_cost(excel, sheet, spu)
    if not row:
        result.update(status="skip_nofloor", note="成本核算表未找到该 SPU 行")
        return result
    daily_price = pipeline._to_number(row.get("daily"))
    sale_raw = str(row.get("sale", "")).strip()
    sale = None if (not sale_raw or sale_raw.startswith("=")) else pipeline._to_number(sale_raw)
    if daily_price is None or sale is None:
        result.update(status="skip_nofloor",
                      note=f"日常价或销售底价读不到（日常价={row.get('daily')} 销售价={sale_raw or '空'}）")
        return result
    purchase_raw = str(row.get("purchase", "")).strip().lstrip("=")
    cost = pipeline._to_number(purchase_raw)  # 仅展示，可为 None

    # 2. 流量页定位行、读加速态（只读）。dry-run 只记「将关」，绝不点。
    accel_state = await pipeline.read_accel_state(flux_page, spu, search=True) if flux_page else "unknown"
    accel_will_close = accel_state == "on"

    # 3. 读活动列表（只读，已去重/去垃圾行）。
    activities = await pipeline.read_activities(activity_page) if activity_page else []

    # 4. 该 SPU 库存（卡活动门槛用）。stock_map 由批次统一读；取不到 → None（保守：卡库存的
    #    活动会因库存未知而不入选，见下）。
    stock = None
    if stock_map is not None:
        stock = stock_map.get(str(spu))

    # 5. 遍历活动：达底价 且 够库存 → 入选。只对「达底价」的活动发 product_plan（否则近百个
    #    活动会刷屏）；达底价但库存不足的也发一条（标注原因），便于前端展示为何没入选。
    enrolled = []
    for act in activities:
        dr = act.get("discount_rate")
        if dr is None:
            continue  # 无固定折扣（万人团/详见提报列表）阶段1 无法确定性算价，跳过
        calc = pipeline.compute_submit_price(daily_price, dr, sale)
        submit_price = calc.get("submit_price")
        within_floor = bool(calc.get("within_floor"))
        if not within_floor:
            continue  # 申报价够不到销售底价，直接淘汰、不发 plan
        min_stock = act.get("min_stock")
        # 库存门槛：门槛为 None 视为无门槛；库存未知(None)时保守视为不达标（卡库存策略）。
        stock_ok = (min_stock is None) or (stock is not None and stock >= min_stock)
        selected = stock_ok
        reason = "达底价且够库存" if selected else (
            f"达底价但库存不足（有{stock if stock is not None else '未知'}<门槛{min_stock}）"
        )
        await _emit(on_progress, {
            "type": "product_plan", "spu": spu, "activity": act["name"],
            "daily_price": daily_price, "sale": sale, "cost": cost,
            "submit_price": submit_price, "within_floor": within_floor,
            "stock": stock, "min_stock": min_stock, "stock_ok": stock_ok,
            "selected": selected, "reason": reason,
        })
        if selected:
            enrolled.append({
                "activity": act["name"], "submit_price": submit_price,
                # daily_price/discount_rate 仅在超参考价失败时反推「建议核对的日常价」用，
                # 不参与任何主流程判定（申报价仍是既算好的 submit_price）。
                "daily_price": daily_price, "discount_rate": dr,
                "registered_count": act.get("registered_count"),
                "registered_display": act.get("registered_display"),
            })

    result["enrolled_activities"] = enrolled
    result["accel_state"] = accel_state  # 供执行遍区分初始 on/off/unknown
    result["accel_will_close"] = accel_will_close  # 初始 on 才需先关
    result["sale"] = sale  # 销售底价，供执行遍重开加速器时设加速价=底价+1
    if enrolled:
        result["submit_price"] = enrolled[0]["submit_price"]  # 兼容旧单值字段

    # 6. 无任何活动入选 → skip_nomatch。
    if not enrolled:
        result.update(status="skip_nomatch",
                      note="无活动同时满足 达底价 且 够库存"
                           + ("（本 SPU 库存未读到）" if stock is None else f"（库存={stock}）"))
        return result

    # 7. 计划可行（status=done，携带计划）。真正的变更在【执行遍】(_run_execution_phases) 里按
    #    活动维度做，本函数只负责【规划】、不点任何变更按钮。dry-run 与非 dry-run 规划完全一致，
    #    区别只在执行遍是否运行（dry_run=True 时执行遍整段跳过）。
    parts = []
    if accel_will_close:
        parts.append("将关加速器")
    names = "、".join(e["activity"] for e in enrolled)
    parts.append(f"将报名 {len(enrolled)} 个活动：{names}")
    if accel_state in {"on", "off"}:
        parts.append("将开启加速器")
    result.update(status="done", note=("dry-run：" if dry_run else "计划就绪：") + "，".join(parts))
    return result


async def _run_execution_phases(results, flux_page, activity_page, live, on_progress) -> dict:
    """执行遍（活动维度）：关流量 → 按活动分组报名 → 只重开我们关掉的流量。

    仅对规划遍里 status=done 的 SPU 执行。live=False（半程）：开提报页/填价但不提交、不真关/开
    （全部可逆，用于端到端验证）；live=True（全程）：真提交/真关/真开（不可逆，须授权）。
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
        "reopened": [], "errors": [], "failed": [],
    }
    if not plans:
        return summary

    await _emit(on_progress, {"type": "exec_start", "live": live, "spu_count": len(plans)})

    # ---- 阶段一：逐 SPU 临场复查流量状态；on 才关闭，off 明确记录 no-op ----
    # 规划阶段读到的 accel_state 可能因多商品串行或人工操作变旧，不能直接决定执行动作。
    # 若临场读取 unknown，规划时为 on 的商品仍交给 close_accel 做一次带筛选的复查；其余保守不关。
    for plan in plans:
        spu = plan["spu"]
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

    # ---- 阶段二：按活动分组报名（每活动开一次提报页、逐 SPU 填价、一次提交）----
    # 【设计决策·用户确认 2026-07-17】关流量撞 24h 冷却（summary["cooldown"] 里的 SPU，关不掉）
    # 时，其报名【照常继续】——不因关流量失败而跳过。冷却只如实记录在 exec_close/summary，
    # 不拦截报名。故此处对全部 plans 分组，不排除 cooldown/关闭失败的 SPU。
    by_activity: dict = {}
    for r in plans:
        for e in r.get("enrolled_activities", []):
            by_activity.setdefault(e["activity"], []).append(
                {
                    "spu": r["spu"], "submit_price": e["submit_price"],
                    "daily_price": e.get("daily_price"), "discount_rate": e.get("discount_rate"),
                    "registered_count": e.get("registered_count"),
                    "registered_display": e.get("registered_display"),
                    "registration_baseline_known": "registered_count" in e,
                }
            )
    # 报名遍整体包异常：无论报名成败/抛错，阶段三重开流量都必须照跑——否则阶段一关掉的
    # 流量会因报名异常而永久留在关闭状态（最严重的不可逆后果）。异常只记 errors、不中断。
    try:
        await _enroll_by_activity(by_activity, activity_page, live, on_progress, summary)
    except Exception as e:
        summary["errors"].append(f"报名遍异常（不影响重开流量）：{e}")
        logger.error(f"报名遍异常：{e}")
        await _emit(on_progress, {"type": "log", "level": "error",
                                  "message": f"报名遍异常（继续重开流量）：{e}"})

    # 报名过程中的“页面跳转/弹窗提示/局部 RPA 失败”只记扫描状态，不直接决定最终成败。
    # 正式执行在所有活动扫描完后统一进入报名记录页，按 SPU + 活动名对账平台真实记录。
    if live:
        await _reconcile_activity_log(plans, activity_page, on_progress, summary)

    # ---- 阶段三：开启流量 ----
    # 初始 on：仅关闭成功后重开；关闭失败/冷却时仍为 on，不重复操作。
    # 初始 off：业务允许先报名再开启，因此报名遍结束后也开启。
    # 初始 unknown：保守不操作。加速价统一为 Excel 底价+1（前端显示价以加速价为准）。
    sale_by_spu = {r["spu"]: r.get("sale") for r in plans}
    initially_off = [r["spu"] for r in plans if r.get("accel_state") == "off"]
    if live:
        # 初始 off 属于新增开启动作：只有该 SPU 的全部计划活动都完成提交后才允许开启。
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
                for item in planned_items
            )
            # 至少真实提交一个活动；全都详情不符合时不新增开启流量。
            if all_resolved and submitted_count > 0:
                successful_off.append(spu)
        # 初始 on 且已被本管线关闭的商品，无论报名是否失败都必须恢复开启。
        to_open = list(dict.fromkeys(summary["closed"] + successful_off))
    else:
        # 半程没有真关，仍对所有确定 on/off 的计划验证开流量页面，但 allow=False 不提交。
        known_accel = [r["spu"] for r in plans if r.get("accel_state") in {"on", "off"}]
        to_open = list(dict.fromkeys(known_accel))
    for spu in to_open:
        try:
            sale = sale_by_spu.get(spu)
            accel_price = round(float(sale) + 1, 2) if sale is not None else None
            await _emit(on_progress, {
                "type": "exec_accel_step", "phase": "open", "step": "checking",
                "spu": spu, "state": None, "note": "正在按 SPU 查询开启前状态",
            })
            res = await pipeline.open_accel(flux_page, spu, allow=live, accel_price=accel_price)
            ok = res.get("opened", False)
            if ok:
                summary["reopened"].append(spu)
            await _emit(on_progress, {
                "type": "exec_reopen", "spu": spu, "ok": ok, "live": live,
                "accel_price": accel_price,
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
    product_results = _execution_product_results(plans, summary, live)
    summary["product_results"] = product_results
    for result in product_results:
        await _emit(on_progress, {"type": "exec_product_done", **result})
    await _emit(on_progress, {"type": "exec_done", "closed": len(summary["closed"]),
                              "reopened": len(summary["reopened"]),
                              "activities": len(by_activity), "errors": summary["errors"],
                              "failed": summary["failed"],
                              "product_results": product_results})
    return summary


def _execution_product_results(plans, summary, live) -> list[dict]:
    """把规划结果与实际提交/开流量结果合成逐 SPU 最终状态，避免用规划 done 冒充执行完成。"""
    submitted = summary.get("enrolled_activities") or {}
    ineligible = summary.get("ineligible_activities") or {}
    reopened = set(summary.get("reopened") or [])
    closed = set(summary.get("closed") or [])
    cooldown = set(summary.get("cooldown") or [])
    outcomes = []
    for plan in plans:
        spu = plan["spu"]
        planned_names = [item["activity"] for item in plan.get("enrolled_activities", [])]
        submitted_names = [name for name in planned_names if spu in (submitted.get(name) or [])]
        ineligible_names = [name for name in planned_names if spu in (ineligible.get(name) or [])]
        if not live:
            outcomes.append({
                "spu": spu, "status": "preview", "planned": len(planned_names),
                "submitted": 0, "ineligible": len(ineligible_names),
                "accel_ok": False, "note": "半程预览，未提交/未开关流量",
            })
            continue

        state = plan.get("accel_state")
        if state == "off":
            accel_ok = spu in reopened
        elif state == "on" and spu in cooldown:
            accel_ok = True  # 冷却拦截后仍保持原有 ON
        elif state == "on":
            accel_ok = spu in closed and spu in reopened
        else:
            accel_ok = False
        all_resolved = len(submitted_names) + len(ineligible_names) == len(planned_names)
        all_ineligible = bool(planned_names) and len(ineligible_names) == len(planned_names)
        activities_ok = all_resolved and bool(submitted_names)
        status = "skip" if all_ineligible else ("done" if activities_ok and accel_ok else "fail")
        reasons = []
        if all_ineligible:
            reasons.append(f"{len(ineligible_names)} 个初筛活动在详情页均无可报名商品")
        elif not activities_ok:
            reasons.append(
                f"提交 {len(submitted_names)}、详情不符合 {len(ineligible_names)}、"
                f"计划 {len(planned_names)} 个活动"
            )
        if not accel_ok and not all_ineligible:
            reasons.append(f"流量最终状态未完成（初始={state}）")
        outcomes.append({
            "spu": spu, "status": status, "planned": len(planned_names),
            "submitted": len(submitted_names), "ineligible": len(ineligible_names),
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


async def _reconcile_activity_log(plans, activity_page, on_progress, summary) -> None:
    """扫描结束后以报名记录页为准，对计划中的每个 SPU/活动做最终状态归并。"""
    spus = [plan["spu"] for plan in plans]
    try:
        result = await pipeline.read_activity_log_records(activity_page.context, spus)
    except Exception as exc:
        result = {
            "records": [], "complete": False, "queries": [],
            "note": f"报名记录页查询异常：{str(exc)[:120]}",
        }
    baseline = summary.get("log_baseline") or {}
    after_records = result.get("records") or []
    baseline_records = baseline.get("records") or []
    records = []
    seen = set()
    for record in [*baseline_records, *after_records]:
        key = record.get("enroll_id") or (
            str(record.get("spu")), record.get("activity"), record.get("enroll_time")
        )
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    summary["log_verification"] = {
        **_log_snapshot(result),
        "baseline_queries": baseline.get("queries") or [],
        "records": [{key: value for key, value in record.items() if key != "raw"} for record in records],
    }
    success_pairs = {
        (str(record.get("spu")), record.get("activity")): record
        for record in records if record.get("success")
    }
    record_pairs = {
        (str(record.get("spu")), record.get("activity")): record for record in records
    }
    scan_failures = summary.get("scan_failures") or []

    for plan in plans:
        spu = str(plan["spu"])
        for item in plan.get("enrolled_activities", []):
            activity = item["activity"]
            pair = (spu, activity)
            success_record = success_pairs.get(pair)
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
                current = summary["activity_results"].setdefault(activity, {})
                # 场次失败原因不再否决成功（用户规则 2026-07-24），但仍附注出来供人工核对。
                note = f"报名记录页确认成功（enrollId={success_record.get('enroll_id')}）"
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

            record = record_pairs.get(pair)
            scan_failure = next(
                (failure for failure in scan_failures
                 if str(failure.get("spu") or "") in {"", spu}
                 and failure.get("activity") == activity),
                None,
            )
            if record:
                # 走到这里说明记录在列表但被判「已退出」（2026-07-24 起列表内非退出即成功）。
                reason = (
                    f"报名记录存在但已退出（enrollStatus={record.get('enroll_status')}"
                    f"，场次原因={record.get('session_failures') or '无'}）"
                )
            elif not result.get("complete"):
                reason = f"报名记录查询不完整，无法确认：{result.get('note', '')}"
            else:
                reason = "报名记录页未查到该 SPU/活动的成功记录"
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


async def _enroll_by_activity(by_activity, activity_page, live, on_progress, summary) -> None:
    """逐活动串行扫描；单活动失败只记录，最终统一由报名记录页对账。"""
    for act_name, items in by_activity.items():
        page = await pipeline.open_enroll_page(activity_page, act_name)
        if page is None:
            note = "打开提报页失败，已记录并继续扫描下一活动"
            summary["activity_results"][act_name] = {
                "filled": [], "submitted": False, "note": note,
            }
            for item in items:
                summary["scan_failures"].append({
                    "activity": act_name, "spu": item["spu"],
                    "step": "open_activity", "note": note,
                })
            await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                      "spu": None, "step": "open_activity", "ok": False,
                                      "note": note})
            await _emit(on_progress, {"type": "exec_enroll", "activity": act_name, "ok": False,
                                      "submitted": False, "note": note})
            continue
        await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                  "spu": None, "step": "open_activity", "ok": True,
                                  "note": "活动详情页已打开并核对活动名"})
        # 该活动全程包在 try/finally：无论报名成功/失败/异常，处理完都【立刻关掉这个提报页
        # (detail-new) tab】再开下一个。实测教训（2026-07-20）：报完不关 → 提报页一个个堆着，
        # 下个活动 open_enroll_page 靠 diff 认新 tab 时被多个残留 tab 干扰，出现首个开不出、
        # 末个搜 0 行等时序失败。始终只留一个提报页，diff 才可靠。
        halt = None
        try:
            filled = []
            ineligible = []
            for it in items:
                try:
                    async def on_step(event, current=it):
                        await _emit(on_progress, {
                            "type": "exec_rpa_step", "activity": act_name,
                            "spu": current["spu"], **event,
                        })

                    r = await pipeline.enroll_activity(page, it["spu"], act_name,
                                                       it["submit_price"], allow_submit=False,
                                                       on_step=on_step,
                                                       daily_price=it.get("daily_price"),
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
                    else:
                        # 未填成功：逐条记入 failed（over_ref 标注疑数据不一致），供操作者核对。
                        summary["failed"].append({
                            "spu": it["spu"], "activity": act_name,
                            "submit_price": it["submit_price"],
                            "ref_price": r.get("ref_price"),
                            "over_ref": bool(r.get("over_ref")),
                            "reason": r.get("note", "填价未成功"),
                        })
                        halt = {
                            "activity": act_name, "spu": it["spu"],
                            "step": r.get("failed_step") or "fill_price",
                            "note": r.get("note", "当前活动操作未完成"),
                        }
                    await _emit(on_progress, {"type": "exec_fill", "activity": act_name,
                                              "spu": it["spu"], "ok": r.get("filled", False),
                                              "over_ref": bool(r.get("over_ref")),
                                              "ineligible": r.get("detail_eligible") is False,
                                              "ref_price": r.get("ref_price"),
                                              "submit_price": it["submit_price"], "note": r.get("note", "")})
                    if halt:
                        break
                except Exception as e:
                    summary["failed"].append({"spu": it["spu"], "activity": act_name,
                                              "submit_price": it["submit_price"], "reason": f"异常：{e}"})
                    await _emit(on_progress, {"type": "exec_fill", "activity": act_name,
                                              "spu": it["spu"], "ok": False, "note": f"异常：{e}"})
                    halt = {"activity": act_name, "spu": it["spu"],
                            "step": "unknown", "note": f"异常：{e}"}
                    break
            # 一次提交本活动页所有已填 SPU。无任何已填 SPU 时不提交（否则点 disabled 的「提交」
            # 会等满超时抛异常）。submit 也包异常保护，单活动失败不扩散。
            summary["filled_activities"][act_name] = filled
            actionable_count = len(items) - len(ineligible)
            if halt or len(filled) != actionable_count:
                if halt is None:
                    halt = {"activity": act_name, "spu": None, "step": "fill_price",
                            "note": "当前活动有商品未完成填价"}
                note = (halt or {}).get("note", "当前活动有商品未完成填价")
                summary["enrolled_activities"][act_name] = []
                summary["activity_results"][act_name] = {
                    "filled": filled, "submitted": False, "note": note,
                }
                await _emit(on_progress, {"type": "exec_enroll", "activity": act_name, "ok": False,
                                          "submitted": False, "filled": filled, "live": live,
                                          "note": f"当前活动操作未完整完成，未提交；记录后继续：{note}"})
            elif actionable_count == 0:
                note = "详情页查询后无可报名商品，跳过当前活动并继续下一活动"
                summary["enrolled_activities"][act_name] = []
                summary["activity_results"][act_name] = {
                    "filled": [], "ineligible": ineligible,
                    "submitted": False, "status": "detail_ineligible", "note": note,
                }
                await _emit(on_progress, {"type": "exec_enroll", "activity": act_name,
                                          "ok": False, "submitted": False, "filled": [],
                                          "ineligible": ineligible, "live": live, "note": note})
            else:
                try:
                    sub = await pipeline.submit_enroll_page(page, allow=live)
                except Exception as e:
                    sub = {
                        "submitted": False, "clicked_submit": False,
                        "note": f"提交调用异常，待记录页对账：{str(e)[:80]}",
                    }
                submitted = bool(sub.get("submitted"))
                feedback_verified = bool(sub.get("verified", submitted))
                clicked_submit = bool(sub.get("clicked_submit", submitted))
                attempted = submitted or clicked_submit
                note = sub.get("note", "")
                summary["submitted_attempts"][act_name] = filled if attempted else []
                # 最终 enrolled_activities 只由扫描结束后的报名记录页对账写入。
                summary["enrolled_activities"].setdefault(act_name, [])
                summary["activity_results"][act_name] = {
                    "filled": filled, "submitted": submitted,
                    "clicked_submit": clicked_submit, "feedback_verified": feedback_verified,
                    "verified": False, "status": "pending_log" if attempted else "submit_failed",
                    "note": note or "等待报名记录页最终核验",
                }
                await _emit(on_progress, {"type": "exec_rpa_step", "activity": act_name,
                                          "spu": None, "step": "submit",
                                          "ok": attempted if live else True,
                                          "note": note})
                await _emit(on_progress, {"type": "exec_enroll", "activity": act_name,
                                          "ok": attempted if live else bool(filled),
                                          "submitted": submitted, "clicked_submit": clicked_submit,
                                          "verified": False,
                                          "feedback_verified": feedback_verified,
                                          "filled": filled, "live": live, "note": note})
                if live and not attempted:
                    halt = {"activity": act_name, "spu": None, "step": "submit",
                            "note": note or "提交未完成"}
        finally:
            # 报完（成功/失败/异常）立刻关掉本提报页 tab，保证任意时刻只有一个 detail-new。
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
    dry_run: bool = True,
    on_progress=None,
    flux_page=None,
    activity_page=None,
    goods_page=None,
    stock_map=None,
    live: bool = False,
    activity_allow=None,
    region_label: str = "",
) -> dict:
    """跑一批 SPU 的活动管理编排（关流量→报名→开流量），进度经 on_progress 抛出。

    - spus：parse_spu_list 产出的 [{spu, margin}] 列表；也接受原始字符串（内部转解析）或
      裸 SPU 字符串列表。
    - min_margin：本批统一毛利率红线（三级回退的中层）；None 时取 config 全局默认。逐品覆盖
      在 parse_spu_list 阶段已并入各 entry 的 margin。
    - dry_run=True（默认）：只算计划、发 product_plan，绝不点任何变更按钮、不跑执行遍。
    - dry_run=False：跑执行遍（活动维度：关流量→报名→重开流量）。其中 live 再分两档——
      live=False（半程，默认）：开提报页/填价但【不提交、不真关/开】，全部可逆，用于端到端验证；
      live=True（全程）：真提交/真关流量/真开流量，不可逆，须调用方显式授权。
    - flux_page/activity_page：测试注入用；均为 None 时走 CDP 连真实调试 Chrome 的已打开标签。
    返回汇总 {done, skip, fail, results:[每 SPU 结果], exec:执行遍汇总 或 None}。
    """
    # 失败事件切面：aborted / product_done fail 自动上报公网 MySQL（best-effort）。
    on_progress = attach(on_progress, "activity")
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
    try:
        logger.info(
            f"=== 活动批次：SPU {total} 个，dry_run={dry_run}，本批毛利率默认={bm} "
            f"工作簿={excel} Sheet={sheet} ==="
        )
        await _emit(on_progress, {
            "type": "batch_start", "total": total, "todo": total,
            "batch": total, "dry_run": dry_run,
        })

        # 批次统一读本批 SPU 库存（一次捕获全店列表，按 productId 匹配），供卡活动库存门槛。
        # 测试可经 stock_map 注入直接跳过。best-effort：读不到 → {}，按「库存未知」保守处理。
        if stock_map is None:
            if goods_page is not None:
                try:
                    stock_map = await pipeline.read_stock(
                        goods_page, [e["spu"] for e in entries]
                    )
                    logger.info(f"活动批次：读到 {len(stock_map)} 个 SPU 库存")
                except Exception as e:
                    logger.warning(f"活动批次：库存读取失败（按库存未知保守处理）：{e}")
                    stock_map = {}
                finally:
                    # 商品页只用于批次开头统一读库存；若由本任务创建，读完立即关闭。
                    if goods_page in owned_pages:
                        try:
                            await goods_page.close()
                        except Exception as e:
                            logger.warning(f"关闭自动创建的商品页失败：{e}")
                        owned_pages.remove(goods_page)
                        goods_page = None
            else:
                stock_map = {}

        for i, entry in enumerate(entries, 1):
            spu = entry["spu"]
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
                        entry, excel, sheet, flux_page, activity_page, dry_run,
                        on_progress, stock_map,
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
                "note": res.get("note", ""),
            })

        # 活动白名单（分批验证用）：只保留 activity_allow 里的活动进执行遍，其余从计划里剔除。
        # 规划遍不受影响（仍算全部）；仅约束真正报名的范围。None=不限制（报全部达底价活动）。
        if activity_allow is not None:
            allow = set(activity_allow)
            for r in results:
                ea = [e for e in (r.get("enrolled_activities") or []) if e["activity"] in allow]
                r["enrolled_activities"] = ea
                if not ea and r.get("status") == "done":
                    r["status"] = "skip_nomatch"  # 白名单过滤后无活动可报 → 不进执行遍

        # 执行遍（活动维度：关流量→报名→重开流量）。dry_run 时整段跳过（纯规划）。
        # live=False 半程可逆、live=True 全程不可逆（须授权）。异常不吞：记 error 事件、不中断汇总。
        if not dry_run:
            try:
                exec_summary = await _run_execution_phases(
                    results, flux_page, activity_page, live, on_progress
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
