"""活动管理 CLI 薄壳：关流量加速器 → 报活动 → 重开加速器，逐 SPU 编排。

编排逻辑已全部下沉到 app/activity/service.py（UI 与 CLI 共用同一套确定性编排 +
结构化进度）。本文件只负责解析命令行参数、把进度打到控制台——对标 batch_collect.py。

为什么不用 tool / LangGraph：活动管理是【确定性批处理作业】（见 app/activity/pipeline.py
开头），本质是「for 每个 SPU：只读定位 + 算申报价守红线 + 变更动作」，既不是给大模型
function-calling 用的 tool，也用不上有状态图。故抽成 service 层，CLI（本文件）和 UI
（app.py 的 FastAPI 接口）都调它。

最高优先级安全（真实商家账号、操作不可逆）：默认 dry-run（只读 + 算计划 + 出报名清单，
不点任何变更按钮）。--execute 进入半程检查，正式报名请使用活动管理页面。

用法：
    python activity_manage.py --document "https://www.kdocs.cn/l/…" --sheet 店A --spus "9072868889"
    python activity_manage.py --document "https://www.kdocs.cn/l/…" --sheet 店A --spus-file spus.txt --execute
"""
import argparse
import asyncio
from pathlib import Path

from app.activity import service as activity_service
from app.logger import logger


def _print_progress(event: dict) -> None:
    """把 service 抛的结构化进度事件打到控制台（CLI 展示）。"""
    t = event.get("type")
    if t == "batch_start":
        logger.info(
            f"=== 活动批次：SPU {event['total']} 个，本批 {event['batch']} 个，"
            f"{'dry-run 只读' if event.get('dry_run') else '正式执行'} ==="
        )
    elif t == "product_start":
        logger.info(
            f"--- [{event['index']}/{event['total']}] 处理 SPU={event['spu']}"
            f"（{(event.get('name') or '')[:20]}）---"
        )
    elif t == "product_plan":
        logger.info(
            f"[计划] SPU={event['spu']} 活动「{event.get('activity') or '无'}」 "
            f"日常价={event.get('daily_price')} 成本={event.get('cost')} "
            f"申报价={event.get('submit_price')} 红线={event.get('red_line')} "
            f"守红线={'是' if event.get('within_red_line') else '否'} "
            f"（{event.get('reason') or ''}）"
        )
    elif t == "product_done":
        status = event.get("status")
        if status == "done":
            logger.info(f"[完成] SPU={event['spu']} 完成（{event.get('note') or ''}）")
        elif status == "fail":
            logger.warning(f"[失败] SPU={event['spu']} 失败（{event.get('note') or ''}）")
        else:  # skip_redline / skip_nomatch / skip_nocost
            logger.info(f"[跳过] SPU={event['spu']} {status}（{event.get('note') or ''}）")
    elif t == "batch_done":
        logger.info(
            f"=== 本批完成：done={event['done']} skip={event['skip']} fail={event['fail']} ==="
        )
    elif t == "aborted":
        logger.error(f"中止：{event.get('reason')}")
    elif t == "log":
        level = event.get("level", "info")
        msg = event.get("message", "")
        getattr(logger, level if level in ("info", "warning", "error") else "info")(msg)


async def main():
    parser = argparse.ArgumentParser(description="活动管理：关加速器→报活动→重开加速器")
    parser.add_argument("--document", "--excel", dest="document", required=True,
                        help="金山 WPS 在线成本表分享链接")
    parser.add_argument("--sheet", required=True, help="目标 Sheet 名（对应店铺的成本表）")
    parser.add_argument("--spus", default="", help="SPU 清单文本（换行/逗号/空格分隔）")
    parser.add_argument(
        "--spus-file",
        dest="spus_file",
        default=None,
        help="从文件读 SPU 清单（与 --spus 二选一或叠加）",
    )
    parser.add_argument(
        "--min-margin",
        dest="min_margin",
        type=float,
        default=None,
        help="本批统一毛利率红线（输入 20 或 0.2 都当 20%%；缺省走 config 全局默认）",
    )
    # dry-run / execute：安全默认 dry-run，只有显式 --execute 才正式执行
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="只读 + 算计划 + 出报名清单，不点任何变更按钮（默认）",
    )
    parser.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="半程检查：读取云端价格、打开活动页并填价，不提交、不切换流量",
    )
    args = parser.parse_args()

    # 拼装 SPU 清单文本：--spus 与 --spus-file 内容合并（best-effort 读文件，失败只告警）
    spus_text = args.spus or ""
    if args.spus_file:
        try:
            spus_text = (spus_text + "\n" + Path(args.spus_file).read_text(encoding="utf-8")).strip()
        except Exception as e:
            logger.error(f"读 --spus-file 失败：{e}")
            return
    if not spus_text.strip():
        logger.error("SPU 清单为空：请用 --spus 或 --spus-file 提供清单。")
        return

    if not args.dry_run:
        logger.info("半程检查模式：填价后停止，不提交报名。")

    await activity_service.run_activity_batch(
        spus_text,
        args.document,
        args.sheet,
        min_margin=args.min_margin,
        dry_run=args.dry_run,
        on_progress=_print_progress,
    )


if __name__ == "__main__":
    asyncio.run(main())
