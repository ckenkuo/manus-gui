"""批量采集 CLI 薄壳：Temu「已发布到站点」→ 1688 以图搜图比价 → 写 Excel。

编排逻辑已全部下沉到 app/collect/service.py（UI 与 CLI 共用同一套确定性管道 +
结构化进度）。本文件只负责解析命令行参数、把进度打到控制台。

为什么不用 tool / LangGraph：采集是【确定性批处理作业】（见 app/collect/pipeline.py
开头），本质是「for 每个未入库商品：确定性步骤 + 2 次单发 LLM」，既不是给大模型
function-calling 用的 tool，也用不上有状态图/持久化状态机。故抽成 service 层，
CLI（本文件）和 UI（app.py 的 FastAPI 接口）都调它。

默认【基础采集】：只采 Temu 基础信息（站点/类目/SPU/售价/主图），采购价/重量留空
待人工填——不跑 1688、不开浏览器/CDP、不用 LLM。加 --with-1688 才走自动采价。

用法：
    python batch_collect.py --enumerate-only        # 枚举所有已打开店铺标签，写 worklist.json
    python batch_collect.py --limit 1               # 冒烟：基础采集 1 个（价/重留空）
    python batch_collect.py --limit 20              # 基础采集 20 个（复用已有清单）
    python batch_collect.py --refresh --limit 20    # 先重新枚举再基础采集
    python batch_collect.py --with-1688 --limit 20  # 走 1688 自动采价（确定性管道）
    python batch_collect.py --with-1688 --no-pipeline --limit 20 # 1688 采价但用纯 agent 兜底
    # 多店铺 / 多工作簿 / 多 Sheet（缺省回填上次选择，再兜底出厂默认）：
    python batch_collect.py --store <mallid> --sheet <表名> --limit 20
    python batch_collect.py --excel "D:\\另一个核算表.xlsx" --sheet 店A --store <mallidA>
    # 云端协作文档（金山 Kdocs）：--excel 直接粘贴链接，水位/判重/写入全在云端
    python batch_collect.py --excel "https://www.kdocs.cn/l/..." --sheet <表名> --limit 1
"""
import argparse
import asyncio

from app.collect import service
from app.logger import logger


def _print_progress(event: dict) -> None:
    """把 service 抛的结构化进度事件打到控制台（CLI 展示）。"""
    t = event.get("type")
    if t == "batch_start":
        logger.info(
            f"=== 清单 {event['total']} 个，已入库 {event['done_existing']}，"
            f"待采 {event['todo']}，本批 {event['batch']} 个 ==="
        )
    elif t == "product_start":
        logger.info(
            f"--- [{event['index']}/{event['total']}] 采集 SPU={event['spu']}"
            f"（{(event.get('name') or '')[:20]}）---"
        )
    elif t == "product_done":
        if event["status"] == "ok":
            logger.info(
                f"✅ SPU={event['spu']} 采购价={event.get('purchase_price')} "
                f"运费={event.get('shipping')} 重量={event.get('weight_g')}g "
                f"[{event.get('via')}]"
            )
        elif event["status"] == "base":
            logger.info(f"⬜ SPU={event['spu']} 基础行已写入（采购价/重量待人工填）")
        elif event["status"] == "empty":
            logger.info(f"⬜ SPU={event['spu']} 无同款 → 留空待人工补")
        else:
            logger.warning(f"⚠️ SPU={event['spu']} 未入库（{event.get('note') or '失败'}）")
    elif t == "batch_done":
        logger.info(f"=== 本批完成：成功 {event['ok']}，失败/存疑 {event['fail']} ===")
    elif t == "aborted":
        logger.error(f"❌ 中止：{event.get('reason')}")


async def main():
    parser = argparse.ArgumentParser(description="批量采集 Temu→1688→Excel")
    parser.add_argument("--limit", type=int, default=20, help="本批处理的未入库商品数量")
    parser.add_argument("--refresh", action="store_true", help="重新枚举工作清单")
    parser.add_argument("--enumerate-only", action="store_true", help="只枚举，不采购")
    parser.add_argument(
        "--with-1688",
        dest="with_1688",
        action="store_true",
        help="走 1688 自动采价（默认只采基础信息、采购价/重量留空待人工填）",
    )
    parser.add_argument(
        "--no-pipeline",
        action="store_true",
        help="仅 --with-1688 时生效：关掉确定性管道，改走纯 agent 自由循环",
    )
    parser.add_argument(
        "--excel",
        default=None,
        help="目标工作簿路径，或直接粘贴协作文档链接（https://www.kdocs.cn/l/...，"
        "链接时写云端协作表格）；缺省=上次选择/出厂默认",
    )
    parser.add_argument("--sheet", default=None, help="目标 Sheet 名（缺省=上次选择/出厂默认）")
    parser.add_argument("--store", default=None, help="只采该店铺（mallid 或店名；缺省=全部）")
    parser.add_argument(
        "--region", default="",
        help="目标区域（顶栏「全球 / 美国 / 欧区」）。以此为准：浏览器停在别的区域会先"
             "替你切过去再采；缺省=跟随浏览器当前区域",
    )
    parser.add_argument(
        "--doc-mode", choices=["auto", "local", "cloud"], default="auto",
        help="写本地工作簿还是协作文档。local=只写本地 xlsx（kdocs 配额用满时切这个；"
             "未给 --excel 则用上次选过的那个，没有就报错让你显式指定）；"
             "cloud=只写协作文档；auto（默认）=按 --excel 形态与 config 自动判",
    )
    args = parser.parse_args()

    if args.refresh or args.enumerate_only or not service.WORKLIST.exists():
        # 区域未确认是要人去浏览器里处理的事（选定区域 / 只留一个区域的列表页），
        # 打清楚提示直接退出，不带 traceback 也不继续往下跑空批。
        try:
            await service.enumerate_worklist(region_label=args.region)
        except service.RegionNotConfirmed as e:
            logger.error(f"❌ {e}")
            return
    if args.enumerate_only:
        return

    # 解析目标工作簿（显式 > 上次选择 > 出厂默认），用于预检占用
    excel = service.resolve_excel(args.excel)

    # 采集前预检：Excel 被占用就别启动 agent（省去浏览器/MCP 初始化与 LLM 开销）。
    # 走协作文档（显式链接，或未显式指定但 prefs/config 配了云端目标）时没有本地
    # 占用锁，跳过预检——否则默认本地表恰好被 WPS 打开会误拦本要走云端的批次。
    if (service.resolve_cloud(args.excel, doc_mode=args.doc_mode) is None
            and service.excel_write_locked(excel)):
        logger.error(
            f"❌ Excel 正被占用（疑似 WPS/Excel 打开中），无法写入：{excel}\n"
            "   请先在 WPS/Excel 里【关闭该文件】，再重新运行。"
        )
        return

    await service.run_batch(
        limit=args.limit,
        base_only=not args.with_1688,
        use_pipeline=not args.no_pipeline,
        on_progress=_print_progress,
        excel=args.excel,
        sheet=args.sheet,
        store=args.store,
        doc_mode=args.doc_mode,
    )


if __name__ == "__main__":
    asyncio.run(main())
