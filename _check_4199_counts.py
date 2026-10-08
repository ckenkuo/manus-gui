# -*- coding: utf-8 -*-
"""一次性只读核查（用完即删）：活动列表页读「已报名」计数变化。

批次前（矩阵落盘）限时秒杀等 8 个活动 registered_count=0。若现在变 1 → 提交生效石锤，
记录页 total=0 只是收录延迟/审核中。顺带再查一次记录页。
"""
import asyncio
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.activity import pipeline, service

ACTS = ["限时秒杀", "官方大促", "商品85折破冰激活专属通道（10月）", "商品85折破冰激活专属通道（9月）"]


async def main():
    pw, browser, _flux, act_page, _g, owned = await service._connect_pages(
        "http://localhost:9222", need_flux=False)
    try:
        await asyncio.sleep(3)
        # 活动列表行文本里含「已报名 N」或报名按钮前的数字；直接抓行文本
        text = await act_page.evaluate("() => document.body.innerText || ''")
        for act in ACTS:
            # 找活动名所在行（列表是卡片/行，行文本含活动名与报名计数）
            idx = text.find(act)
            if idx < 0:
                print(f"{act}: 列表页未找到（可能在更下面，需滚动）")
                continue
            seg = text[idx:idx + 200].replace("\n", " ")
            print(f"{act}: {seg}")
        print("=== 再查记录页 ===")
        ctx = browser.contexts[0]
        log = await pipeline.read_activity_log_records(ctx, ["3822224199"])
        print(f"records={len(log.get('records') or [])} complete={log.get('complete')}")
        for rec in log.get("records") or []:
            print(json.dumps({k: rec.get(k) for k in
                              ("activity", "enroll_status", "success", "enroll_id")},
                             ensure_ascii=False))
    finally:
        for p in owned:
            try:
                await p.close()
            except Exception:
                pass
        await browser.close()
        await pw.stop()


asyncio.run(main())
