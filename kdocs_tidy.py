# -*- coding: utf-8 -*-
"""协作表格整理 CLI：拆开跨行合并 + 数据区行序反转（最新在最上）。

默认 dry-run，只打印计划；加 --write 才真正落笔，且落笔前默认复制一份 Sheet 备份。

用法示例（PowerShell）：

    # 看计划（不改表）
    python kdocs_tidy.py --url "https://www.kdocs.cn/l/xxxx" --list
    python kdocs_tidy.py --url "https://www.kdocs.cn/l/xxxx" --sheet "VibeMakers美国"

    # 真正执行（带备份）
    python kdocs_tidy.py --url "https://www.kdocs.cn/l/xxxx" --sheet "VibeMakers美国" --write

    # 多个 Sheet；--sheets-like 按前缀/包含匹配
    python kdocs_tidy.py --url "..." --sheet "A" --sheet "B" --write
    python kdocs_tidy.py --url "..." --sheets-like "VibeMakers" --write
"""
import argparse
import sys

from app.kdocs.cli import KdocsCliError
from app.kdocs.sheet_tidy import SheetTidy, col_letter
from app.logger import logger


def _print_plan(p) -> None:
    print(f"\n=== {p.sheet_name}（worksheet_id={p.worksheet_id}）")
    if p.skipped_reason:
        print(f"  跳过：{p.skipped_reason}")
        return
    print(f"  表头第 {p.header_row} 行；数据区 "
          f"{p.data_row_from + 1}..{p.data_row_to + 1} 行（{p.data_rows} 行）"
          f"，A..{col_letter(p.col_to)} 列")
    print(f"  图片单元格 {p.pic_cells} 个")
    print(f"  跨行合并 {len(p.row_merges)} 处" +
          ("：" + "、".join(m.label() for m in p.row_merges[:12]) +
           (f" 等（共 {len(p.row_merges)} 处）" if len(p.row_merges) > 12 else "")
           if p.row_merges else "（无）"))
    if p.col_merges:
        # 跨列合并也必须拆：range_sort 遇区域内任何合并都会静默放弃
        print(f"  仅跨列合并 {len(p.col_merges)} 处（也要拆，否则排序不生效）："
              + "、".join(m.label() for m in p.col_merges[:12])
              + (f" 等（共 {len(p.col_merges)} 处）" if len(p.col_merges) > 12 else ""))
    # 行序校验：反转等于「时间倒序」的前提，必须让用户看见依据
    r = p.asc_ratio
    if p.date_col:
        verdict = "升序（可反转）" if p.order_ok else "非升序，存疑"
        ratio = f"{r:.0%}（{p.asc_pairs}/{p.cmp_pairs} 对）" if r is not None else "无可比对"
        print(f"  行序校验：按 {p.date_col} 列「{p.date_col_title}」→ {verdict}，"
              f"升序占比 {ratio}")
        if p.date_samples:
            print(f"    该列取值（上→下）：{' | '.join(p.date_samples)}")
    else:
        print("  行序校验：表头里找不到时间列，无法判断现有行序")


def _print_result(p) -> None:
    if p.skipped_reason:
        return
    print(f"  已拆合并 {p.unmerged} 处，回填 {p.filled_cells} 个单元格，"
          f"反转 {p.reversed_rows} 行"
          + (f"，备份副本：{p.backup_sheet}" if p.backup_sheet else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="金山协作表格整理：拆合并 + 行序反转")
    ap.add_argument("--url", required=True, help="协作文档链接或 file_id")
    ap.add_argument("--sheet", action="append", default=[],
                    help="工作表名，可重复指定")
    ap.add_argument("--sheets-like", default="",
                    help="按包含关系批量选取工作表（与 --sheet 取并集）")
    ap.add_argument("--list", action="store_true", help="只列出文档里的工作表")
    ap.add_argument("--header-row", type=int, default=None,
                    help="表头行号（1-based）；默认自动探测")
    ap.add_argument("--write", action="store_true", help="真正落笔（默认只出计划）")
    ap.add_argument("--no-backup", action="store_true", help="落笔前不复制备份副本")
    ap.add_argument("--verify-cols", default="",
                    help="校验用列字母，逗号分隔（如 C,E）；默认前 3 列")
    ap.add_argument("--date-col", default="",
                    help="指定用于校验行序的时间列字母；默认按表头自动挑")
    ap.add_argument("--force", action="store_true",
                    help="现有行序无法判定为时间升序时仍强行反转")
    args = ap.parse_args()

    tidy = SheetTidy(args.url)
    infos = tidy.sheets_info()

    if args.list:
        print(f"文档共 {len(infos)} 个工作表：")
        for name, info in infos.items():
            flag = "" if info["visible"] else "（隐藏）"
            print(f"  {name}{flag}  行 1..{info['row_to'] + 1}，"
                  f"列 A..{col_letter(info['col_to'])}，sheetId={info['id']}")
        return 0

    targets = list(dict.fromkeys(args.sheet))
    if args.sheets_like:
        targets += [n for n in infos if args.sheets_like in n and n not in targets]
    if not targets:
        print("未指定工作表。用 --sheet 指定，或 --sheets-like 批量匹配，"
              "或 --list 先看有哪些表。", file=sys.stderr)
        return 2

    missing = [n for n in targets if n not in infos]
    if missing:
        print(f"以下工作表不存在：{missing}\n现有：{list(infos)}", file=sys.stderr)
        return 2

    from app.orders.kdocs_sheet import col_to_index

    verify_cols = None
    if args.verify_cols:
        verify_cols = [col_to_index(c) for c in args.verify_cols.split(",") if c.strip()]
    date_col = col_to_index(args.date_col) if args.date_col.strip() else None

    if not args.write:
        print("[dry-run] 只出计划，不改动文档；确认后加 --write 执行")

    failed = []
    skipped = []
    for name in targets:
        try:
            p = tidy.tidy(name, header_row=args.header_row, write=args.write,
                          do_backup=not args.no_backup, verify_cols=verify_cols,
                          date_col=date_col, force=args.force)
            _print_plan(p)
            if args.write:
                _print_result(p)
            if p.skipped_reason and args.write:
                skipped.append(name)
        except KdocsCliError as e:
            # 单表失败不连坐：把剩下的表继续处理完，最后统一报
            failed.append(name)
            logger.error(f"「{name}」整理失败：{e}")
            print(f"\n=== {name}\n  失败：{str(e)[:300]}")

    if skipped:
        print(f"\n有 {len(skipped)} 个工作表被跳过（未改动）：{skipped}")
        print("  原因见上方各表的「跳过」说明；确认要反转可加 --force")
    if failed:
        print(f"\n有 {len(failed)} 个工作表失败：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
