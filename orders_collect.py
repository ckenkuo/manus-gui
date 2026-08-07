# -*- coding: utf-8 -*-
"""订单登记 CLI 薄壳：Temu「待发货」订单 → 本地订单登记表（含 DISPIMG 主图）。

编排全在 app/orders/service.py（UI 与 CLI 共用同一套确定性管线 + 结构化进度）。
本文件只解析参数、把进度打到控制台。

**默认 dry-run**：只导出+解析+算出「将写入什么」，不碰登记表。方案要求人工核对
行数与字段后，再加 --write 真正落盘（写入不可逆，虽有自动备份）。

用法：
    python orders_collect.py                      # dry-run，看会写什么
    python orders_collect.py --store StoreA        # 显式指定店铺（识别不到时必须给）
    python orders_collect.py --write              # 真正写入登记表
    python orders_collect.py --max-pages 2        # 冒烟：只翻 2 页
"""
import argparse
import asyncio

from app.logger import logger
from app.orders import service


def _print_progress(event: dict) -> None:
    """把 service 抛的结构化进度事件打到控制台。"""
    t = event.get("type")
    if t == "started":
        mode = "试跑（不写入）" if event.get("dry_run") else "写入模式"
        logger.info(f"=== 订单登记开始 · {mode} · 登记表={event.get('workbook')} ===")
    elif t == "store":
        logger.info(f"当前登录店铺：{event.get('store')}")
    elif t == "watermark":
        if event.get("enabled"):
            logger.info(
                f"增量采集已启用：「{event.get('sheet')}」已登记 {event.get('known')} 个订单号，"
                f"翻到全部已登记就停"
            )
        else:
            logger.info(f"全量采集：{event.get('reason')}")
    elif t == "page":
        inc = ""
        if event.get("new_on_page") or event.get("known_on_page"):
            inc = (f"，本页新 {event.get('new_on_page')} / 已登记 "
                   f"{event.get('known_on_page')}")
        logger.info(
            f"--- 第 {event.get('page')} 页（累计 {event.get('pages_done')} 页）"
            f" 已抓图 {event.get('images')} 张，已选 {event.get('selected')}"
            f"/{event.get('total')} 条{inc} ---"
        )
    elif t == "swept":
        logger.info(
            f"翻页完成：{event.get('pages')} 页，共 {event.get('total')} 条，"
            f"已勾选 {event.get('selected')}，抓到 {event.get('images')} 个子订单的图"
        )
        if event.get("stopped_early"):
            logger.info(f"增量早停：{event.get('stop_reason')}")
        if event.get("fell_back"):
            logger.warning("增量护栏触发，本批已退回全量 sweep（详见上文告警）")
    elif t == "exported":
        logger.info(f"官方导出已落地：{event.get('file')}")
    elif t == "parsed":
        logger.info(
            f"解析导出：{event.get('rows')} 行 / {event.get('orders')} 个订单，"
            f"匹配到图 {event.get('image')} 张，成交价 {event.get('price')} 条"
            + (f"，{event.get('miss')} 条无图" if event.get("miss") else "")
        )
    elif t == "images":
        logger.info(f"主图下载：成功 {event.get('ok')}，失败 {event.get('fail')}")
    elif t == "qty_column":
        for item in event.get("sheets") or []:
            if item.get("inserted"):
                logger.info(
                    f"「{item.get('sheet')}」已在「尺码」右侧插入「数量」列"
                    f"（{item.get('column')} 列）；插列前备份：{item.get('backup')}"
                )
    elif t == "purchase_summary":
        logger.info(
            f"采购统计：新增 {event.get('rows')} 条，涉及 {event.get('products')} 个商品"
            f"（{event.get('variants')} 个规格），其中 {event.get('multi_products')} 个"
            f"需一次买多规格，总件数 {event.get('total_qty')}"
        )
        logger.info(
            f"    汇总表嵌图：商品级 {event.get('product_images', 0)} 张，"
            f"SKU 级 {event.get('sku_images', 0)} 张"
        )
        for label, key in (("汇总表", "file"), ("统计 md", "md_file")):
            if event.get(key):
                logger.info(f"    {label}：{event[key]}")
    elif t == "plan":
        note = "（判重列缺失，跳过该表）" if event.get("no_key") else ""
        logger.info(
            f"计划写入「{event.get('sheet')}」：{event.get('pending')} 行"
            f"（带图 {event.get('with_image')}），判重跳过 {event.get('dup')} {note}"
        )
    elif t == "unmapped":
        logger.warning(
            f"⚠️ {event.get('count')} 条订单未命中 sheet_map（已跳过，不臆测落点）："
            f"{event.get('samples')}"
        )
    elif t == "unpriced":
        logger.warning(
            f"{event.get('count')} 条订单页面暂无「成交单价」，因开了 --require-price "
            f"本批不登记、留到下批（默认口径是允许空价照常登记）"
        )
    elif t == "writing":
        logger.info(f"正在写入「{event.get('sheet')}」{event.get('rows')} 行…")
    elif t == "written":
        logger.info(
            f"✅ 「{event.get('sheet')}」已写入 {event.get('written')} 行"
            f"（第 {event.get('first_row')}~{event.get('last_row')} 行，"
            f"嵌图 {event.get('images')} 张）；备份：{event.get('backup')}"
        )
    elif t == "write_failed":
        logger.error(f"❌ 写入「{event.get('sheet')}」失败：{event.get('error')}")
    elif t == "aborted":
        logger.error(f"❌ 已中止：{event.get('reason')}")
    elif t == "done":
        _print_summary(event)


def _print_summary(s: dict) -> None:
    """收尾汇总。dry-run 时额外打前几行预览，供人工核对字段是否对得上。"""
    if s.get("aborted"):
        return
    logger.info(
        f"=== 完成：解析 {s.get('parsed_rows')} 行 → 待写 {s.get('pending')} 行，"
        f"判重跳过 {s.get('dup_skipped')}，未映射跳过 {s.get('unmapped_skipped')}，"
        f"无价留待下批 {s.get('unpriced_skipped', 0)} ==="
    )
    inc = s.get("incremental") or {}
    if inc.get("enabled"):
        logger.info(
            f"增量：水位 {inc.get('watermark')}（登记表最新一条），"
            f"本批翻 {inc.get('pages_swept')} 页"
            + ("（追上后早停）" if inc.get("stopped_early") else "（未触发早停，已翻到底）")
        )
    if s.get("no_key_sheets"):
        logger.warning(f"⚠️ 判重列缺失被整表跳过：{s['no_key_sheets']}")
    if s.get("failed_sheets"):
        logger.error(f"❌ 写入失败的表：{s['failed_sheets']}")

    if s.get("dry_run"):
        for sheet, info in (s.get("sheets") or {}).items():
            logger.info(f"--- 预览「{sheet}」（待写 {info.get('pending')} 行，前 3 行）---")
            for row in info.get("preview") or []:
                logger.info(f"    {row}")
        for f in s.get("plan_files") or []:
            logger.info(f"完整待写计划（逐行核对用）：{f}")
        logger.info("试跑结束，未写入任何数据。核对无误后加 --write 正式写入。")
    else:
        logger.info(f"实际写入 {s.get('written_rows')} 行。")
    purchase = s.get("purchase") or {}
    if purchase.get("file"):
        logger.info(f"本批采购汇总表：{purchase['file']}")
    if purchase.get("md_file"):
        logger.info(
            f"本批采购统计（按商品合并，{purchase.get('multi_products', 0)} 个商品"
            f"需一次买多规格）：{purchase['md_file']}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Temu 待发货订单 → 本地订单登记表")
    parser.add_argument(
        "--write", action="store_true",
        help="真正写入登记表（默认只试跑不写；写入不可逆，虽有自动备份）",
    )
    parser.add_argument(
        "--store", default="",
        help="当前登录店铺名（缺省=从页面识别；识别不到会中止，此时必须显式指定）",
    )
    parser.add_argument("--workbook", default="", help="登记表路径（缺省=[orders].workbook）")
    parser.add_argument(
        "--sheet", default="",
        help="目标 Sheet：指定后本批全部写这张表，不再按 sheet_map 的站点分流"
             "（须同时给 --store，它要写进「订单店铺」列）",
    )
    parser.add_argument("--list-url", default="", help="待发货订单页 URL（缺省=配置）")
    parser.add_argument(
        "--region", default="",
        help="目标区域（顶栏「全球 / 美国 / 欧区」）。以此为准：浏览器停在别的区域会先切"
             "过去，list_url 的域名也按该区域改写；缺省=跟随浏览器当前区域",
    )
    parser.add_argument(
        "--max-pages", type=int, default=200, help="最多翻几页（冒烟用，缺省 200）"
    )
    parser.add_argument(
        "--no-incremental", action="store_true",
        help="关掉增量：翻完全部页。默认增量——给了 --sheet 时先读该表已登记的订单号当水位，"
             "翻页追上就停，不必每次翻十几页（未给 --sheet 时本就按 sheet_map 分流，"
             "水位有歧义，自动全量）",
    )
    parser.add_argument(
        "--require-price", action="store_true",
        help="严格模式：页面暂无「成交单价」的订单本批不登记、留到下批。"
             "默认不开——允许成交价为空，订单照常登记（插件回填约一天延迟，等价会积压"
             "当天全部订单；代价是那一格后续需人工补填，重跑不会补）",
    )
    parser.add_argument(
        "--doc-mode", choices=["auto", "local", "cloud"], default="auto",
        help="写本地登记表还是协作文档。local=只写本地 xlsx（kdocs 配额用满时切这个；"
             "未给 --workbook 则用上次选过的、再退 [orders].workbook，都没有就报错）；"
             "cloud=只写协作文档；auto（默认）=按 --workbook 形态与 config 自动判",
    )
    args = parser.parse_args()

    await service.run_orders_batch(
        store=args.store,
        dry_run=not args.write,
        workbook=args.workbook,
        sheet=args.sheet,
        list_url=args.list_url,
        on_progress=_print_progress,
        max_pages=args.max_pages,
        require_price=args.require_price,
        incremental=not args.no_incremental,
        doc_mode=args.doc_mode,
        region_label=args.region,
    )


if __name__ == "__main__":
    asyncio.run(main())
