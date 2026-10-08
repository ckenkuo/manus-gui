# -*- coding: utf-8 -*-
"""2026-10-03 记录页分页结构探针（只读，无副作用）：
1) 读分页 ul 完整结构，确证「下一页 >」与「快进 »」两个 li 的 class/icon 差异（MARK JS 误认实锤）
2) 点开 sizeChanger 读每页条数选项，切到最大档，等响应后读新分页状态
   ——若 pageSize 可到 50/100，78 条 1-2 页拉完，跳页块机制不再触发。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.collect.service import CDP_URL


async def main():
    # 裸连 CDP 自开页签：不走 service._connect_pages（它的区域确认依赖浏览器当前页签
    # 已停在业务页，刚重新登录时不可靠；本探针只读记录页，不需要区域语义）。
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = None
    try:
        page = await context.new_page()
        await page.goto("https://agentseller.temu.com/activity/marketing-activity/log",
                        wait_until="domcontentloaded", timeout=30000)
        await page.bring_to_front()
        await asyncio.sleep(4)

        # 1) 分页 ul 每个 li 的结构（class / 文本 / aria / 内含 svg icon）
        items = await page.evaluate(r"""() => {
          const p = document.querySelector('ul[data-testid="beast-core-pagination"]');
          if (!p) return {status: p.getAttribute('data-status'), items: null, html: 'NO_PAGINATION'};
          return {
            status: p.getAttribute('data-status'),
            items: [...p.querySelectorAll('li')].map(li => ({
              cls: String(li.className || ''),
              text: (li.textContent || '').trim().slice(0, 20),
              aria: li.getAttribute('aria-label'),
              title: li.getAttribute('title'),
              ariaDisabled: li.getAttribute('aria-disabled'),
              icons: [...li.querySelectorAll('svg')].map(s => s.getAttribute('data-icon')),
            })),
          };
        }""")
        print("分页 data-status:", items.get("status"))
        for i, it in enumerate(items.get("items") or []):
            print(f"  li[{i}] cls={it['cls']!r} text={it['text']!r} aria={it['aria']!r} "
                  f"title={it['title']!r} disabled={it['ariaDisabled']} icons={it['icons']}")

        # 2) sizeChanger：点开下拉读选项
        await page.evaluate(r"""() => {
          const sel = document.querySelector('.PGT_sizeChanger_5-120-1 [data-testid="beast-core-select-header"]')
            || document.querySelector('[class*="PGT_sizeChanger"] [class*="ST_head"]');
          if (sel) sel.click();
        }""")
        await asyncio.sleep(1.5)
        options = await page.evaluate(r"""() => {
          return [...document.querySelectorAll('[data-testid="beast-core-select-option"],[class*="ST_option"],[role="option"]')]
            .map(o => ({text: (o.textContent || '').trim(), cls: String(o.className || '').slice(0, 80)}))
            .filter(o => o.text);
        }""")
        print("sizeChanger 选项:", options)

        # 3) 切到最大档（选项文本含数字，取最大）
        picked = await page.evaluate(r"""() => {
          const opts = [...document.querySelectorAll('[data-testid="beast-core-select-option"],[class*="ST_option"],[role="option"]')]
            .filter(o => /\d+/.test(o.textContent || ''));
          if (!opts.length) return null;
          const best = opts.reduce((a, b) =>
            parseInt(a.textContent) >= parseInt(b.textContent) ? a : b);
          const label = (best.textContent || '').trim();
          best.click();
          return label;
        }""")
        print("已切换每页条数:", picked)
        await asyncio.sleep(4)
        after = await page.evaluate(r"""() => {
          const p = document.querySelector('ul[data-testid="beast-core-pagination"]');
          const lis = p ? [...p.querySelectorAll('li')].map(li => (li.textContent || '').trim()) : [];
          return {status: p ? p.getAttribute('data-status') : None, pages: lis};
        }""".replace("None", "null"))
        print("切换后分页状态:", after)
    finally:
        if page is not None:
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
