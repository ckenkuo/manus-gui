"""一次性验证脚本：等加速器 24h 锁定期结束后，跑完整闭环（关流量 → 搞活动 → 开流量）。

背景（2026-09-25 实测，SPU 8791757215）
- 18:29 开启加速器成功；平台锁 24 小时不允许手动关闭（「开启后需满24小时才可手动关闭」）。
- 锁定期内前端售价按加速价（98.94 / 64.0）→ 活动申报上限被压到 89.04 / 57.6（=×0.9），
  按成本表日常价算的申报价 146.9 必被平台拒。
- 锁定期结束后应当能：关掉加速器 → 价格基准恢复 → 报名成功 → 再把加速器开回去。

怎么跑（已注册两个 Windows 计划任务，都不依赖本会话是否开着）
    manus-activity-lockcheck     2026-09-26 18:35 跑完整闭环
    manus-activity-lockcheck-2   2026-09-26 19:10 只回查状态（开启生效有延迟，约半小时后才看得到）
不需要参数（`--state-only` 只用来回读状态）。结果写到
桌面「manus输出/活动识别矩阵/锁定期验证-<时间戳>.log」，同时打印到 stdout。

跑完确认无误后，本脚本与两个计划任务都可删：
    schtasks /delete /tn manus-activity-lockcheck /f
    schtasks /delete /tn manus-activity-lockcheck-2 /f
"""
import asyncio
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.activity import service           # noqa: E402
from app.config import get_output_dir      # noqa: E402

DOC = "https://www.kdocs.cn/l/cbm5lcTiEIGc"
SHEET = "LEONOVAFINDS全球"
SPU = "8791757215"
SELECTION = [[SPU, "官方大促"]]
LOG_PATH = Path(get_output_dir("activity")) / f"锁定期验证-{time.strftime('%Y%m%d-%H%M%S')}.log"

_lines = []


def log(text: str = "") -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {text}"
    _lines.append(line)
    print(line, flush=True)
    try:
        LOG_PATH.write_text("\n".join(_lines), encoding="utf-8")
    except Exception:
        pass


async def ensure_seller_tab() -> None:
    """管线按「已打开的卖家后台页签」确认作业区域，没有就先开一个（不猜区域）。"""
    from playwright.async_api import async_playwright

    from app.collect.service import CDP_URL
    from app.temu_region import read_region

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=30000)
    try:
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "agentseller" in (p.url or "")), None)
        if page is None:
            page = await ctx.new_page()
            await page.goto("https://agentseller.temu.com/activity/marketing-activity",
                            wait_until="commit", timeout=120000)
            log("已新开卖家后台页签")
        for _ in range(45):
            await asyncio.sleep(2)
            try:
                region = await read_region(page)
            except Exception:
                region = None
            if region is not None and getattr(region, "ok", False):
                log(f"作业区域：{region.describe()}")
                return
        log("警告：区域仍读不到，批次可能中止")
    finally:
        try:
            await browser.close()
            await pw.stop()
        except Exception:
            pass


async def read_state() -> str:
    """只读回查该 SPU 的加速状态（判断锁定期是否已过、开启是否生效）。"""
    from playwright.async_api import async_playwright

    from app.activity import pipeline
    from app.collect.service import CDP_URL

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=30000)
    try:
        page = await browser.contexts[0].new_page()
        try:
            await page.goto("https://agentseller.temu.com/main/flux-analysis",
                            wait_until="commit", timeout=120000)
            for _ in range(40):
                await asyncio.sleep(1)
                if "SPU ID" in await page.evaluate("() => document.body.innerText || ''"):
                    break
            state = await pipeline.read_accel_state(page, SPU, search=True)
            log(f"回查加速状态：{state}")
            return state
        finally:
            try:
                await page.close()
            except Exception:
                pass
    finally:
        try:
            await browser.close()
            await pw.stop()
        except Exception:
            pass


async def main() -> None:
    log("=" * 70)
    log(f"锁定期验证开始：SPU={SPU}，活动={SELECTION[0][1]}，日志={LOG_PATH}")
    await ensure_seller_tab()

    before = await read_state()
    log(f"批次前状态：{before}")

    events = []
    summary = await service.run_activity_batch(
        SPU, DOC, SHEET, dry_run=False, live=True,
        selection=SELECTION, on_progress=lambda ev: events.append(ev),
    )
    for ev in events:
        t = ev.get("type")
        if t == "exec_close":
            log(f"  关流量 ok={ev.get('ok')} state={ev.get('state')} {ev.get('note', '')[:160]}")
        elif t == "exec_rpa_step" and ev.get("step") in {"fill_price", "submit"}:
            log(f"  {ev['step']} ok={ev.get('ok')} {ev.get('note', '')[:200]}")
        elif t == "exec_enroll":
            log(f"  提交 submitted={ev.get('submitted')} {ev.get('note', '')[:160]}")
        elif t == "exec_log_verify":
            log(f"  对账 {ev.get('status')} {ev.get('note', '')[:160]}")
        elif t == "exec_reopen":
            log(f"  开流量 ok={ev.get('ok')} {ev.get('note', '')[:240]}")
        elif t == "exec_product_done":
            log(f"  SPU 结果 {ev.get('status')}：{ev.get('note', '')[:240]}")
        elif t == "exec_done":
            log(f"  exec_done closed={ev.get('closed')} reopened={ev.get('reopened')}")
    log(f"批次汇总：done={summary.get('done')} skip={summary.get('skip')} fail={summary.get('fail')}")

    await asyncio.sleep(20)   # 开启生效有延迟（实测约半小时），这里只作参考回查
    after = await read_state()
    log(f"批次后状态：{after}（开启生效有延迟，由 manus-activity-lockcheck-2 在 19:10 再回查一次）")
    log("=" * 70)


async def state_only() -> None:
    """只回读一次状态（第二个计划任务用：确认重开的加速器是否已生效）。"""
    log("=" * 70)
    log(f"状态回查：SPU={SPU}")
    await ensure_seller_tab()
    await read_state()
    log("=" * 70)


if __name__ == "__main__":
    if "--state-only" in sys.argv:
        asyncio.run(state_only())
    else:
        asyncio.run(main())
