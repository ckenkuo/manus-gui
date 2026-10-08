# -*- coding: utf-8 -*-
"""2026-10-03 实机验证：_restore_flux_page_after_panel 修复有效性。

流程：close_accel 半程（allow=False，只开「查看效果」面板不点停止，零副作用）留下残留面板
→ 残留状态下查询下一个 SPU（预期复现 unknown）→ 调 _restore_flux_page_after_panel
→ 再查同一 SPU（预期恢复 found=True）→ 只读确认 8791757215 当前状态。
finally 里无论如何再恢复一次页面，不留面板给人。
"""
import asyncio

from app.activity import pipeline
from app.activity import service
from app.collect.service import CDP_URL

ON_SPU = "1619974426"      # 加速中，用于打开面板复现残留
NEXT_SPU = "3822224199"    # 2026-10-02 批次里连环 unknown 的下一个 SPU
FAILED_SPU = "8791757215"  # 2026-10-02 批次关闭失败的商品，只读确认状态


async def main():
    pw, browser, flux_page, _act, _goods, owned = await service._connect_pages(
        CDP_URL, "", need_flux=True)
    try:
        # 第一步：半程 close，留下打开的面板（复现残留现场）
        res = await pipeline.close_accel(flux_page, ON_SPU, allow=False)
        print(f"[步骤1] 半程 close_accel({ON_SPU}): state={res.get('state')} "
              f"note={str(res.get('note') or '')[:120]}", flush=True)

        # 第二步：面板残留下直接查询（预期复现 unknown）
        await flux_page.bring_to_front()
        await asyncio.sleep(0.5)
        r1 = await pipeline._search_flux_product(flux_page, NEXT_SPU)
        print(f"[步骤2] 残留下查询 {NEXT_SPU} -> found={r1.get('found')} "
              f"state={r1.get('state')} note={str(r1.get('note') or '')[:150]}", flush=True)

        # 第三步：调恢复函数后再查（预期 found=True，修复有效的判据）
        await pipeline._restore_flux_page_after_panel(flux_page)
        r2 = await pipeline._search_flux_product(flux_page, NEXT_SPU)
        print(f"[步骤3] 恢复后查询 {NEXT_SPU} -> found={r2.get('found')} "
              f"state={r2.get('state')} note={str(r2.get('note') or '')[:150]}", flush=True)

        # 第四步（只读）：确认 2026-10-02 批次关闭失败商品的当前状态
        r3 = await pipeline.read_accel_state(flux_page, FAILED_SPU, search=True)
        print(f"[步骤4] read_accel_state({FAILED_SPU}, search=True) -> {r3}", flush=True)
    finally:
        # 无论如何把流量页恢复干净，不留面板给人
        try:
            await pipeline._restore_flux_page_after_panel(flux_page)
        except Exception:
            pass
        for page in owned:
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
