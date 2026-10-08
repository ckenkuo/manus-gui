# -*- coding: utf-8 -*-
"""2026-10-03 实跑前基线：查 1619974426/8791757215/3822224199 的活动历史记录，
用于实跑后对比「8791757215 这次是否真正报上」。"""
import asyncio

from app.activity import history

SPUS = ["1619974426", "8791757215", "3822224199"]


async def main():
    cfg = history.load_config()
    if not cfg["enabled"]:
        print("history 未启用")
        return

    def _load():
        return history._query_rows(cfg, spus=SPUS, limit=200)

    rows = await asyncio.to_thread(_load)
    print(f"共 {len(rows)} 行（新在前）")
    for r in rows:
        act = str(r.get("activity") or "")[:40]
        print(f"{str(r.get('created_at'))[:19]} {r.get('batch_uid')} spu={r.get('spu')} "
              f"{r.get('kind')} ok={r.get('ok')} {r.get('status')} {act}")


if __name__ == "__main__":
    asyncio.run(main())
