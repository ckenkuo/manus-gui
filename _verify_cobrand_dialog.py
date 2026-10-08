# -*- coding: utf-8 -*-
"""2026-10-02 验证联报推荐弹窗修复：SPU=7083399274 官方大促正式执行（live=True）。

复现路径：点提交 → 联报推荐弹窗 → 修复后应自动点「仅提交当前活动」→ 跳结果页
→ 报名记录页核验成功 → 开加速器。跑完确认后可删。
"""
import asyncio

from app.activity import service as activity_service
from activity_manage import _print_progress

# 链接结尾是大写 I（cbm5lcTiEIGc）不是小写 l——截图字体易混，抄错会 4xx「链接不存在」。
DOCUMENT = "https://www.kdocs.cn/l/cbm5lcTiEIGc"
SHEET = "LEONOVAFINDS全球"
SPUS = "7083399274"


def _print_all(event: dict) -> None:
    """全事件打印：_print_progress 只打批次级事件，exec_close/exec_reopen 等执行事件
    （流量 unknown 的具体 note 在这里面）CLI 默认看不到，诊断期全量打出。"""
    _print_progress(event)
    t = event.get("type") or ""
    if t.startswith("exec_"):
        note = str(event.get("note") or "")[:200]
        print(f"[{t}] spu={event.get('spu')} activity={event.get('activity')} "
              f"ok={event.get('ok')} status={event.get('status')} note={note}")


async def main():
    summary = await activity_service.run_activity_batch(
        SPUS, DOCUMENT, SHEET,
        dry_run=False, live=True, region_label="",
        cloud_url=DOCUMENT,
        on_progress=_print_all,
    )
    print("=== summary ===")
    print(summary)


if __name__ == "__main__":
    asyncio.run(main())
