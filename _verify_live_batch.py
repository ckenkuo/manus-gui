# -*- coding: utf-8 -*-
"""2026-10-04 晚 879 锁定期满后的补报批次：只跑 8791757215（live=True）。

背景：2026-10-03 三轮验证批次跑完，879 因加速器 24h 锁定（10-03 19:37 重开）
6 个活动被加速价压制未报：官方大促、限时秒杀、85折起手黑五、破冰、半托管85折、
万圣节85折。锁定 10-04 19:37 期满，关加速器后申报上限恢复（Excel 日常价 163.23）。
1619974426 四活动详情全无资格（skip 已定）、3822224199 活动已报完（done）且其
加速器 10-03 22:49 才开、锁到 10-04 22:49——两者本批都不带，只聚焦 879。
这批同时是「逐 SPU 提交」修复的首个真实提交实机验证（前两轮 382 无可报活动零提交）。

链接结尾是大写 I（cbm5lcTiEIGc）不是小写 l——截图字体易混，抄错会 4xx「链接不存在」。
"""
import asyncio

from app.activity import service as activity_service
from activity_manage import _print_progress

DOCUMENT = "https://www.kdocs.cn/l/cbm5lcTiEIGc"
SHEET = "LEONOVAFINDS全球"
SPUS = "8791757215"


def _print_all(event: dict) -> None:
    """全事件打印：_print_progress 只打批次级事件，exec_close/exec_reopen 等执行事件
    （流量 unknown 的具体 note 在这里面）CLI 默认看不到，诊断期全量打出。"""
    _print_progress(event)
    t = event.get("type") or ""
    if t.startswith("exec_"):
        note = str(event.get("note") or "")[:200]
        print(f"[{t}] spu={event.get('spu')} activity={event.get('activity')} "
              f"ok={event.get('ok')} status={event.get('status')} note={note}", flush=True)


async def main():
    summary = await activity_service.run_activity_batch(
        SPUS, DOCUMENT, SHEET,
        dry_run=False, live=True, region_label="",
        cloud_url=DOCUMENT,
        on_progress=_print_all,
    )
    print("=== summary ===", flush=True)
    print(summary, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
