# -*- coding: utf-8 -*-
"""一次性只读核查（用完即删）：3822224199 在 8 个「提交成功」活动里现在还搜不搜得到。

判别逻辑：提交生效的商品会从「可报名商品」列表消失（已报名/审核中不可再报）。
- 8 个活动全搜不到 → 批次提交其实都生效了，记录页收录延迟
- 还搜得到 → 提交没生效，继续深挖
只搜索，不勾选不提交。
"""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.activity import pipeline, service

SPU = "3822224199"
ACTS = [
    "限时秒杀",
    "官方大促",
    "【营销热点】85折起手黑五&双十一！早报早赚先发先赢",
    "商品85折破冰激活专属通道（10月）",
    "【营销热点】半托管活动85折专区",
    "【营销热点】Temu Week&万圣节85折大促",
    "商品85折破冰激活专属通道（9月）",
]


async def main():
    pw, browser, _flux, act_page, _g, owned = await service._connect_pages(
        "http://localhost:9222", need_flux=False)
    try:
        for act in ACTS:
            page = await pipeline.open_enroll_page(act_page, act)
            if page is None:
                print(f"{act}: 开页失败")
                continue
            try:
                probe = await pipeline.probe_detail_eligibility(page, SPU)
                print(f"{act}: detail_eligible={probe['detail_eligible']} {probe['note'][:60]}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
    finally:
        for p in owned:
            try:
                await p.close()
            except Exception:
                pass
        await browser.close()
        await pw.stop()


asyncio.run(main())
