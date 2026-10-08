# -*- coding: utf-8 -*-
"""只读诊断：拉 rowid=184807703160556911 草稿的 edit.json，看色名映射原料长什么样。

cmap 对 1072478434320 返回空（2026-10-07 重跑时无「源色名已映射」日志、换图仍报
找不到颜色行），而同批 717941628394 映射成功。要分清是 labels 空 / vars 空 /
extCode 前缀配不上 / attrMap 值不在 labels 里的哪一种。

独立开新页签、只 fetch 一个接口，不碰发布管线正在用的编辑页。
"""
import asyncio
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from playwright.async_api import async_playwright

ROWID = "184807703160556911"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto("https://www.dianxiaomi.com/", wait_until="domcontentloaded",
                            timeout=20000)
            d = await page.evaluate("""(async (rowid) => {
              const out = {labels: [], vars: [], http: 0, keys: []};
              try {
                const e = await fetch('/api/popTemuProduct/edit.json?id=' + rowid,
                                      {credentials: 'include'});
                out.http = e.status;
                const j = await e.json();
                const p = ((j || {}).data || {}).product || {};
                out.keys = Object.keys(p);
                out.vars = (p.variations || []).map(v => ({
                  ext: String(v.extCode || ''), attrs: v.attrMap || {}}));
                out.skuAttrs = p.skuAttrs || p.attrs || null;
              } catch (err) { out.err = String(err); }
              return out;
            })""" + f"('{ROWID}')")
            print("http:", d.get("http"), "| product keys:", d.get("keys"))
            print("variations 数:", len(d.get("vars") or []))
            for v in (d.get("vars") or [])[:8]:
                print("  ext=", repr(v["ext"]), "| attrs=", v["attrs"])
            if d.get("err"):
                print("fetch 异常:", d["err"])
        finally:
            await page.close()


asyncio.run(main())
