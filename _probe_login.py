# -*- coding: utf-8 -*-
"""2026-10-04 登录态纯探测：不新开页签、不导航，只读现有业务页签的顶栏区域标签。
输出去IOK / KICKED / NO_TAB，供定时探测决定能否接续批次（登录掉线期间
_warmup_region 每次会新开一个页签，堆积污染浏览器，故探测不开页）。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.collect.service import CDP_URL
from app.temu_region import read_region


async def main():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "agentseller" in (p.url or "") and "/auth/" not in p.url:
                    page = p
                    break
            if page:
                break
        if page is None:
            print("NO_TAB：没有可用业务页签")
        else:
            try:
                region = await read_region(page)
                if region and getattr(region, "label", None):
                    print(f"OK：区域={region.label}")
                else:
                    print("KICKED：业务页读不到区域标签")
            except Exception as e:
                print(f"KICKED：读区域异常 {str(e)[:100]}")
        try:
            await browser.close()
        except Exception:
            pass
    except Exception as e:
        print(f"NO_BROWSER：{str(e)[:120]}")
    finally:
        try:
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
