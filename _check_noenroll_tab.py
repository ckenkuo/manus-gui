# -*- coding: utf-8 -*-
"""一次性只读核查（用完即删）：破冰（10月）「不可报名商品」tab 搜 3822224199。

提交后商品从「可报名商品」消失有两种解释：已报上（审核中） or 被挪进「不可报名商品」。
点开不可报名 tab 搜一下，若有行则行文本多半带原因。
"""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.activity import pipeline, service
from app.config import get_output_dir

SPU = "3822224199"
ACT = "商品85折破冰激活专属通道（10月）"

_CLICK_TAB_JS = r"""
() => {
  for (const el of document.querySelectorAll('*')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
    if (own === '不可报名商品') {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.width < 200) { el.click(); return 'ok'; }
    }
  }
  return 'no-tab';
}
"""


async def main():
    pw, browser, _flux, act_page, _g, owned = await service._connect_pages(
        "http://localhost:9222", need_flux=False)
    try:
        page = await pipeline.open_enroll_page(act_page, ACT)
        if page is None:
            print("open_enroll_page 返回 None")
            return
        clicked = await page.evaluate(_CLICK_TAB_JS)
        print("click 不可报名 tab:", clicked)
        await asyncio.sleep(2)
        probe = await pipeline.probe_detail_eligibility(page, SPU)
        print("PROBE:", json.dumps(probe, ensure_ascii=False))
        # 若搜到行，读行全文（找原因列）
        row_text = await page.evaluate(
            r""" (spu) => {
              const tr = [...document.querySelectorAll('tr')].find(t => (t.textContent || '').includes('SPU ID: ' + spu));
              return tr ? (tr.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 500) : '';
            }""", SPU)
        print("ROW:", row_text)
        shot = str(get_output_dir("screenshot") / "pobing_noenroll_tab.png")
        await page.screenshot(path=shot, full_page=False)
        print("SHOT:", shot)
        await page.close()
    finally:
        for p in owned:
            try:
                await p.close()
            except Exception:
                pass
        await browser.close()
        await pw.stop()


asyncio.run(main())
