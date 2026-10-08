# -*- coding: utf-8 -*-
"""一次性诊断（用完即删）：3822224199 限时秒杀带网络监听真提交，读报名接口响应体。

背景：9:26 批次 8 个活动「结果页确认已提交」但 11 分钟后记录页仍 total=0——
结果页确认是前端跳转，平台可能接口层就拒了。响应体里有真实成败与原因。
"""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from app.activity import pipeline, service

SPU = "3822224199"
ACT = "限时秒杀"

# 日志口径：单货号，申报价 71.22（底价 71.23、85 折 → 日常价 83.79）
SKU_PRICES = [
    {"label": "默认", "daily": 83.79, "sale": 71.23, "submit_price": 71.22},
]


async def main():
    pw, browser, _flux, act_page, _g, owned = await service._connect_pages(
        "http://localhost:9222", need_flux=False)
    posts = []
    try:
        page = await pipeline.open_enroll_page(act_page, ACT)
        if page is None:
            print("open_enroll_page 返回 None")
            return
        page.on("response", lambda r: posts.append(r)
                if r.request.method == "POST" and "enroll" in r.url else None)

        async def on_step(ev):
            print(f"  step {ev['step']} ok={ev['ok']} {(ev.get('note') or '')[:70]}")

        res = await pipeline.enroll_activity(
            page, SPU, ACT, SKU_PRICES, allow_submit=False, on_step=on_step,
            discount_rate=0.85)
        print("ENROLL:", json.dumps({k: v for k, v in res.items() if k != "sku_rows"},
                                    ensure_ascii=False)[:400])
        if not res.get("filled"):
            print("走到提交前失败，不提交")
            return

        print("=== 真点提交，等接口响应 ===")
        sub = await pipeline.submit_enroll_page(page, allow=True)
        print("SUBMIT:", json.dumps(sub, ensure_ascii=False)[:300])
        for resp in posts:
            body = ""
            try:
                body = (await resp.text())[:800]
            except Exception as exc:
                body = f"(读响应体失败: {exc})"
            print(f"[{resp.status}] {resp.url[:110]}")
            print("  REQ:", (resp.request.post_data or "")[:400])
            print("  RESP:", body)
    finally:
        for p in owned:
            try:
                await p.close()
            except Exception:
                pass
        await browser.close()
        await pw.stop()


asyncio.run(main())
