# -*- coding: utf-8 -*-
"""实跑验证三个问题商品的修复效果（2026-10-07）。

- 1072478434320：⑦ 行换图失败（找不到颜色行）+ publish 被「服装类图片尺寸」拒。
  save 已成功 → 实况失配检查不启用 → 普通续跑只重跑 publish 必再撞，故 from_stage=skc
  全链重跑（验证色板映射修复 → 行图合规 → 发布通过）。
- 717941628394：阶段全绿到 save，publish 被上轮 do_publish=False 跳过；
  skipped ∈ _DONE，普通续跑不会重跑 publish，故 from_stage=publish 强制只跑发布。
- 939043735707：状态文件显示 16:31 已发布，无需重跑。
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

    print("==== 1072478434320：skc 起全链重跑 + 发布", flush=True)
    r1 = await publish_service.run_batch(
        [{"url": "https://detail.1688.com/offer/1072478434320.html",
          "rowid": "184807703160556911"}],
        on_progress=on_progress, from_stage="skc", do_publish=True)
    print("==== 1072478434320 结果:", r1, flush=True)

    print("==== 717941628394：只补发布", flush=True)
    r2 = await publish_service.run_batch(
        [{"url": "https://detail.1688.com/offer/717941628394.html",
          "rowid": "184807703160556915"}],
        on_progress=on_progress, from_stage="publish", do_publish=True)
    print("==== 717941628394 结果:", r2, flush=True)


asyncio.run(main())
