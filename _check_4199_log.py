# -*- coding: utf-8 -*-
"""一次性核查（用完即删）：3822224199 现在报名记录页有没有收录。

9:26-9:30 批次 8 个活动「结果页确认已提交」但对账 3 次零收录。
判据：现在能查到 = 收录延迟超出对账窗口；仍没有 = 提交真没生效。
"""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright

from app.activity import pipeline


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
    try:
        ctx = browser.contexts[0]
        log = await pipeline.read_activity_log_records(ctx, ["3822224199"])
        recs = log.get("records") or []
        print(f"records={len(recs)} complete={log.get('complete')}")
        for rec in recs:
            print(json.dumps({k: rec.get(k) for k in
                              ("spu", "activity", "enroll_status", "success", "enroll_id", "enroll_time")},
                             ensure_ascii=False))
    finally:
        await browser.close()
        await pw.stop()


asyncio.run(main())
