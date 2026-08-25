"""1688 商品信息提炼 CLI（发布管线阶段①的单独入口，用于验证浏览器适配层）。

用法：
  python publish_extract.py https://detail.1688.com/offer/123456789.html
  python publish_extract.py 123456789 --no-images        # 只提数据不下图
  python publish_extract.py 123456789 --out D:\\某目录     # 指定工作目录

只读操作：只导航 1688 详情页、页面内 fetch 详情接口、下载图片，不写任何店小秘数据。
前提：已登录的 Chrome 带 --remote-debugging-port=9222 运行（与采集管线同一个前提）。
"""
import argparse
import asyncio
import json
import sys

from app.logger import logger
from app.publish.browser import ensure_cdp_alive
from app.publish.extract import extract_product


async def main() -> int:
    ap = argparse.ArgumentParser(description="1688 商品源页信息提炼")
    ap.add_argument("offer", help="offer 完整链接或 offerId")
    ap.add_argument("--out", default=None, help="输出目录，默认桌面 manus输出/商品发布/product-<offerId>")
    ap.add_argument("--no-images", action="store_true", help="只提取数据，不下载图片")
    args = ap.parse_args()

    if not await ensure_cdp_alive():
        return 2
    try:
        result = await extract_product(
            args.offer, outdir=args.out, with_images=not args.no_images
        )
    except Exception as e:
        logger.error(f"提炼失败：{e}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    mc = result.get("materialCheck") or {}
    if mc.get("needsProcessing"):
        logger.warning(f"素材图需处理：{mc.get('reason')}")
    logger.info(
        f"完成：{result['title']} | 属性{result['attrCount']} 项 | "
        f"SKU {result['skuCount']} 个 | 主图{result['mainImgs']} 详情{result['descImgs']}"
    )
    logger.info(f"工作目录：{result['outdir']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
