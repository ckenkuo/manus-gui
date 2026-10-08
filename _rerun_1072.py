# -*- coding: utf-8 -*-
"""实跑验证 1072478434320：cmap 兜底配对（extCode 纯尺码形态）落地后的全链重跑。

上一轮（2026-10-07 17:5x）cmap 还是旧版：extCode 全是 '90'~'140' 纯尺码、前缀匹配
零命中 → 「找不到颜色行: 8095蝴蝶结喇叭裤」→ publish 被尺寸闸拒。本轮验证：
唯一未配对源色 + 剩余 variation 颜色值收拢 → 源「8095蝴蝶结喇叭裤」→ 行「蝴蝶结喇叭裤」。
"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.publish import service as publish_service


async def main() -> None:
    async def on_progress(ev: dict) -> None:
        t = ev.get("type")
        if t in ("log", "manual_check", "stage_done", "product_done",
                 "batch_done", "error", "aborted"):
            msg = ev.get("message") or ev.get("note") or ""
            print(f"[{str(ev.get('offer', ''))[:10]}][{t}][{ev.get('stage', '')}] "
                  f"{str(msg)[:220]}", flush=True)

    r = await publish_service.run_batch(
        [{"url": "https://detail.1688.com/offer/1072478434320.html",
          "rowid": "184807703160556911"}],
        on_progress=on_progress, from_stage="skc", do_publish=True)
    print("==== 1072478434320 结果:", r, flush=True)


asyncio.run(main())
