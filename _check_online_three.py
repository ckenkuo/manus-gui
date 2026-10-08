# -*- coding: utf-8 -*-
"""只读核实：三个商品的店小秘 dxmState（online=已上架）。不导航、不改表单。"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from playwright.async_api import async_playwright

ROWIDS = {
    "939043735707": "184807703160556917",
    "717941628394": "184807703160556915",
    "1072478434320": "184807703160556911",
}


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto("https://www.dianxiaomi.com/", wait_until="domcontentloaded",
                            timeout=20000)
            for offer, rowid in ROWIDS.items():
                d = await page.evaluate("""(async (rowid) => {
                  try {
                    const r = await fetch('/api/popTemuProduct/edit.json?id=' + rowid,
                                          {credentials: 'include'});
                    const j = await r.json();
                    const p = ((j || {}).data || {}).product || {};
                    return {state: p.dxmState || '', name: (p.productName || '').slice(0, 40)};
                  } catch (e) { return {err: String(e)}; }
                })""" + f"('{rowid}')")
                print(offer, "->", d, flush=True)
        finally:
            await page.close()


asyncio.run(main())
