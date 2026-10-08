# -*- coding: utf-8 -*-
"""2026-10-04 补轮后对账：批次五收尾时对账「查询不完整」（登录疑似波动），
3 个已结果页确认的提交（清仓甩卖/破冰/半托管85折）未核验。本脚本只读：
开记录页拉全量，匹配 8791757215，看记录是否 20→23。万圣节85折未执行，不在预期内。
"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright

from app.activity import pipeline
from app.collect.service import CDP_URL

SPUS = ["8791757215"]


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    try:
        rec = await pipeline.read_activity_log_records(context, SPUS)
        rows = (rec.get("by_spu") or {}).get("8791757215") or []
        print(f"error={rec.get('error')}")
        print(f"8791757215 共 {len(rows)} 条")
        for x in rows:
            name = str(x.get("activityName") or x.get("activity") or "")[:45]
            status = x.get("enrollStatus")
            eid = x.get("enrollId")
            print(f"  - {name} | enrollStatus={status} | {eid}", flush=True)
    except Exception as e:
        print(f"对账异常：{e}", flush=True)
    finally:
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
