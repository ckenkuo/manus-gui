# -*- coding: utf-8 -*-
"""2026-10-03 批次前预热：裸连 CDP 开一个流量页页签并等顶栏区域标签渲染，
确认 confirm_region_from_context 能读到区域后【页签留着不关】——run_activity_batch 的
_connect_pages 要从现有页签读区域，刚重新登录时浏览器里没有业务页签会直接中止批次。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.collect.service import CDP_URL
from app.temu_region import confirm_region_from_context


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = await context.new_page()
    await page.goto("https://agentseller.temu.com/main/flux-analysis",
                    wait_until="domcontentloaded", timeout=60000)
    await page.bring_to_front()
    await asyncio.sleep(8)
    region = await confirm_region_from_context(context, "")
    print(f"区域确认：{region}（页签保留，供批次复用）", flush=True)
    # 不关闭 page/browser：页签留给批次；仅断开本次 CDP 连接
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
