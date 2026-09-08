"""店小秘发布管线批量入口（app/publish/service.py 的 CLI）。

用法：
  python publish_run.py --tasks tasks.json --store Pawly --site 美国 [--from-stage material]
  python publish_run.py --url <1688链接> [--title 标题] --store Pawly
  python publish_run.py --rowid <rowid> --info <product-info.json> [--title 标题] --store Pawly

tasks.json 格式（两种元素可混）：
  [{"url": "https://detail.1688.com/offer/....html", "title": "可选"},
   {"rowid": "173539495450551101", "info_path": ".../product-info.json"}]

断点续跑：直接重跑同一命令即可——workspace/publish-state/<offer>.json 里已完成的
阶段自动跳过；--from-stage <阶段id> 从指定阶段起重跑（阶段 id 见 --list-stages）。

申报价：⑩ 变种表的申报价默认 188.88，用 --price 传实际售价可覆盖（会归一成两位小数，
非法值退回默认值）；建议售价一律按申报价 ÷ 7 折算成美元。

发布闸门：⑮「立即发布」默认不执行，流程收尾在「保存落库」；加 --publish 才会在
⑭ 保存成功后点「发布」→「立即发布」真正上架（不可逆，真实商家账号，慎用）。
"""
import argparse
import asyncio
import json
import sys

from app.logger import logger
from app.publish import alert
from app.publish.service import STAGES, run_batch, set_image_concurrency


def _gbk(s: str) -> str:
    """GBK 控制台兜底：⑪-⑮ 圈号与 ▶ 都不在 GBK 里，直接 print 会 UnicodeEncodeError
    （实测 Windows 终端 + colorama 包装后 stdout 是 GBK）。这些先翻成 (11) / > 等
    可读形式，其余编码不了的字符换成 ?。"""
    s = str(s)
    for c, r in (("⑪", "(11)"), ("⑫", "(12)"), ("⑬", "(13)"), ("⑭", "(14)"),
                 ("⑮", "(15)"), ("▶", ">")):
        s = s.replace(c, r)
    return s.encode("gbk", errors="replace").decode("gbk")


def _p(s: str = "", flush: bool = False) -> None:
    """所有进度输出的唯一出口：整行过 GBK 兜底再打。

    原先只把「参数」过 _gbk，f-string 里的字面符号（▶）没过滤，
    stage_start 每行都抛 UnicodeEncodeError 被 service._emit 吞掉，
    表现为阶段开始行整行丢失（2026-08-24 实测）。"""
    print(_gbk(s), flush=flush)


def _print_progress(event: dict) -> None:
    t = event.get("type")
    if t == "batch_start":
        _p(f"[批次] 共 {event['total']} 个商品 → {event['store']} / {event['site']}")
    elif t == "product_start":
        _p(f"\n[{event['index']}/{event['total']}] {event['offer']} "
              f"{_gbk((event.get('title') or '')[:30])}")
    elif t == "stage_start":
        _p(f"  ▶ {_gbk(event['name'])}", flush=True)
    elif t == "stage_done":
        mark = {"ok": "√", "skipped": "-", "fail": "×"}.get(event["status"], "?")
        note = f" | {_gbk(event['note'])}" if event.get("note") else ""
        _p(f"  {mark} {_gbk(event['name'])} ({event.get('elapsed_s', 0)}s){note}", flush=True)
    elif t == "manual_check":
        _p(f"  ! 人工检查[{event.get('stage')}]：{_gbk(event.get('message'))}", flush=True)
    elif t == "product_done":
        mark = "√" if event["status"] == "ok" else "×"
        stage = f"（卡在 {event['failed_stage']}）" if event.get("failed_stage") else ""
        _p(f"[{event['index']}] {mark} {event.get('elapsed_s', 0)}s {stage} {_gbk(event.get('note'))}")
    elif t == "batch_done":
        _p(f"\n[批次完成] 成功 {event['ok']} / 失败 {event['fail']} "
              f"| 总耗时 {event.get('elapsed_s', 0)}s")
    elif t == "aborted":
        _p(f"[中止] {_gbk(event.get('reason'))}")
    elif t == "log":
        _p(f"[{event.get('level')}] {_gbk(event.get('message'))}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="店小秘发布管线批量编排")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--tasks", help="任务清单 JSON 文件（批量）")
    src.add_argument("--url", help="单个 1688 链接（全流程）")
    src.add_argument("--rowid", help="单个草稿 rowid（跳过采集认领，需配 --info）")
    ap.add_argument("--title", default="", help="商品标题（--url/--rowid 模式可选）")
    ap.add_argument("--info", default="", help="product-info.json 路径（--rowid 模式必填）")
    ap.add_argument("--store", default="", help="目标店铺名（如 Pawly）")
    ap.add_argument("--site", default="", help="目标站点（必填，如 美国；店小秘没有「全球」站点）")
    ap.add_argument("--warehouse", default="",
                    help="⑪ 选择仓库要勾的仓库名（须是该店该站点仓库列表里的真实选项）；"
                         "不给则按 config 的 [publish] 站点映射选择")
    ap.add_argument("--price", default="",
                    help="⑩ 变种表申报价（人民币），不给按 188.88；建议售价按它 ÷7 折算")
    ap.add_argument("--from-stage", default="",
                    help="从指定阶段起重跑（阶段 id，见 --list-stages）")
    ap.add_argument("--list-stages", action="store_true", help="列出阶段 id 后退出")
    ap.add_argument("--test-alert", action="store_true",
                    help="只发一条飞书测试告警后退出（验证 [publish.alert] 配好没有）")
    ap.add_argument("--publish", action="store_true",
                    help="⑭ 保存成功后继续点「发布」→「立即发布」真正上架"
                         "（不可逆，不给这个开关时 ⑮ 阶段跳过）")
    # 视频取向做成「关掉才丢弃」而不是「开了才保留」：默认行为与加这个开关之前一致，
    # 老命令行照抄不变（同 --publish 的取向——改变默认行为的那一侧才需要显式加参数）
    ap.add_argument("--no-video", action="store_true",
                    help="⑬b 丢弃产品视频：编辑页直接点「删除」，不做比例合规化"
                         "（不给则保留视频、裁到 1:1/3:4/16:9 后回填）")
    ap.add_argument("--image-concurrency", type=int, default=0,
                    help="生图并发数（⑤b 图片清理与 ⑬ 描述图英化共用）。"
                         "不给则用发布页配置的值（默认 30）；给了会写进配置、长期生效")
    args = ap.parse_args()

    if args.list_stages:
        for sid, name in STAGES:
            _p(f"  {sid:<10} {_gbk(name)}")
        return 0

    if args.test_alert:
        cfg = alert.load_alert_config()
        if not cfg["enabled"]:
            _p("飞书告警未启用：在 config/config.toml 的 [publish.alert] 段配 "
               "webhook（或设环境变量 PUBLISH_ALERT_WEBHOOK）")
            return 1
        ok = await alert.send_text("[自检] manus-gui 发布告警通道正常")
        _p("已发送，去群里确认" if ok else "发送失败，详见日志")
        return 0 if ok else 1

    if args.tasks:
        with open(args.tasks, encoding="utf-8") as f:
            tasks = json.load(f)
    elif args.url:
        tasks = [{"url": args.url, "title": args.title}]
    elif args.rowid:
        if not args.info:
            ap.error("--rowid 模式必须带 --info <product-info.json>")
        tasks = [{"rowid": args.rowid, "info_path": args.info, "title": args.title}]
    else:
        ap.error("必须给 --tasks / --url / --rowid 之一")

    # 并发数落的是同一份 prefs（Web 页与 CLI 共用），故这里是「设置」而不是「本次覆盖」。
    # 刻意不做成临时值：用户在链路差的环境里调小它，下次跑批照样该小，
    # 每次都要重新加参数反而容易忘（同 save_prefs 合并语义那段的理由）。
    if args.image_concurrency:
        try:
            n = set_image_concurrency(args.image_concurrency)
            logger.info(f"生图并发数已设为 {n}（写入配置，后续跑批沿用）")
        except ValueError as e:
            ap.error(str(e))

    # run_batch 内部的中断（aborted / 单商品 fail / 收尾有失败）由 service 的告警钩子
    # 统一报；这里兜的是 run_batch 本身抛出来的异常——钩子在它里面，抛出来就报不了了。
    try:
        r = await run_batch(tasks, store=args.store, site=args.site,
                            on_progress=_print_progress, from_stage=args.from_stage,
                            do_publish=args.publish, price=args.price,
                            keep_video=not args.no_video, warehouse=args.warehouse)
    except Exception as e:
        await alert.alert_batch_crash(str(e), store=args.store, site=args.site)
        raise
    logger.info(f"批次结束：{r}")
    return 0 if r.get("fail") == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
