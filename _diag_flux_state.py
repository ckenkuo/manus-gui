# -*- coding: utf-8 -*-
"""2026-10-02 复现：close_accel 半程（allow=False 只打开「查看效果」面板、不点停止）
模拟 success 早退路径的面板残留，紧接着按批次方式查询下一个 SPU，验证面板残留
是否导致 unknown、以及死在哪条路径。全程无副作用（不点停止按钮）。"""
import asyncio

from app.activity import pipeline
from app.activity import service
from app.collect.service import CDP_URL

ON_SPU = "1619974426"      # 当前加速中（阶段三已重开），用于打开面板
NEXT_SPU = "3822224199"    # 批次里 unknown 的下一个 off SPU


async def main():
    pw, browser, flux_page, _act, _goods, owned = await service._connect_pages(
        CDP_URL, "", need_flux=True)
    try:
        # 半程 close：面板打开、定位到停止按钮即返回（面板保持打开，同 success 早退残留）
        res = await pipeline.close_accel(flux_page, ON_SPU, allow=False)
        print(f"半程 close_accel: state={res.get('state')} note={str(res.get('note') or '')[:80]}",
              flush=True)
        # 紧接着模拟批次里下一个 SPU 的阶段一查询
        await flux_page.bring_to_front()
        await asyncio.sleep(0.5)
        result = await pipeline._search_flux_product(flux_page, NEXT_SPU)
        print(f"面板残留下查询 {NEXT_SPU} -> found={result.get('found')} "
              f"state={result.get('state')} total={result.get('total')} "
              f"note={str(result.get('note') or '')[:150]}", flush=True)
    finally:
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
