# -*- coding: utf-8 -*-
"""2026-10-04 只读核查用户点名的 3 个 SPU（6645544251/50884498257/3847562936）：
1) 流量分析页查加速器开关状态；2) 报名记录页全量拉取本地匹配它们的报名记录。
用户反馈「这 3 个没开流量加速」——它们不在 10-03 批次（879/382/161）范围内，
先弄清是没报活动、还是报过但加速器没开。只读，不做任何开关/报名操作。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.activity import pipeline
from app.collect.service import CDP_URL

SPUS = ["6645544251", "50884498257", "3847562936"]


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    try:
        # 1) 加速器状态：复用已开的流量页页签
        flux = None
        for p in context.pages:
            if "flux-analysis" in (p.url or ""):
                flux = p
                break
        if flux is None:
            flux = await context.new_page()
            await flux.goto("https://agentseller.temu.com/main/flux-analysis",
                            wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)
        await flux.bring_to_front()
        for spu in SPUS:
            try:
                r = await pipeline._search_flux_product(flux, spu)
                state = r.get("accel_state") or r.get("state")
                print(f"[加速器] SPU={spu} found={r.get('found')} state={state} "
                      f"note={r.get('note')}", flush=True)
            except Exception as e:
                print(f"[加速器] SPU={spu} 查询异常：{e}", flush=True)
            await asyncio.sleep(2)

        # 2) 报名记录：全量拉取本地分组（记录页搜索失效，只能全量拉）
        try:
            rec = await pipeline.read_activity_log_records(context, SPUS)
            for spu in SPUS:
                rows = (rec.get("by_spu") or {}).get(spu) or []
                names = [str(x.get("activityName") or x.get("activity") or "")[:40] for x in rows]
                print(f"[报名记录] SPU={spu} 共 {len(rows)} 条：{names}", flush=True)
            if rec.get("error"):
                print(f"[报名记录] 拉取不完整：{rec.get('error')}", flush=True)
        except Exception as e:
            print(f"[报名记录] 查询异常：{e}", flush=True)
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
