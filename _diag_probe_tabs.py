# -*- coding: utf-8 -*-
"""只读探针：列出 CDP 浏览器所有 context / 页签 URL，判断是否有存活登录态。"""
import asyncio

from playwright.async_api import async_playwright

from app.collect.service import CDP_URL


async def main():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        for i, ctx in enumerate(browser.contexts):
            print(f"context[{i}] pages={len(ctx.pages)}", flush=True)
            for p in ctx.pages:
                print("   -", (getattr(p, "url", "") or "")[:130], flush=True)
        await browser.close()
    finally:
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
