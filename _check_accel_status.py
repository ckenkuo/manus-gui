# -*- coding: utf-8 -*-
"""2026-10-04 只读复查三个 SPU 的流量加速器实时状态。

背景：3822224199 于 10-03 22:49 批次阶段三点「立即加速」平台受理成功，但当刻回查
尚未显示加速中（生效延迟约半小时），之后未再确认；8791757215/1619974426 的加速器
10-03 19:37 重开至今。本脚本只读查询，不做任何开关操作。
裸连 CDP 自开流量页：不走 service._connect_pages（区域确认依赖现有业务页签，
只读复查不需要区域语义，避免无谓中止）。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.activity import pipeline
from app.collect.service import CDP_URL

SPUS = ["8791757215", "3822224199", "1619974426"]


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = None
    own_page = False
    try:
        # 优先复用已开着的流量页页签（新开页签 goto 实测会卡 60s 超时）
        for p in context.pages:
            if "flux-analysis" in (p.url or ""):
                page = p
                break
        if page is None:
            page = await context.new_page()
            own_page = True
            await page.goto("https://agentseller.temu.com/main/flux-analysis",
                            wait_until="domcontentloaded", timeout=60000)
        await page.bring_to_front()
        await asyncio.sleep(5)
        for spu in SPUS:
            try:
                r = await pipeline._search_flux_product(page, spu)
                print(f"SPU={spu} -> {r}", flush=True)
            except Exception as e:
                print(f"SPU={spu} -> 查询异常：{e}", flush=True)
            await asyncio.sleep(2)
    finally:
        if page is not None and own_page:
            try:
                await page.close()
            except Exception:
                pass
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
