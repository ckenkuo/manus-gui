"""Gemini 官网对话的独立命令行入口（CDP 直连，不经反代）。

用法：
    python gemini_ask.py "你的问题"
    python gemini_ask.py "这张图是什么颜色" --image a.png --image b.png

前提：已登录的 Chrome 以 --remote-debugging-port=9222 运行，并已打开
https://gemini.google.com/app 页签（端口见 config.toml 的 [browser].cdp_url）。
"""
import argparse
import asyncio
import sys

from app.gemini_web import GeminiWebClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini 官网对话（CDP 直连）")
    parser.add_argument("prompt", help="要发送的文本")
    parser.add_argument("--image", action="append", default=None,
                        help="贴图（本地图片路径，可多次传入）")
    args = parser.parse_args()

    async with GeminiWebClient() as g:
        text = await g.ask(args.prompt, images=args.image)
    print(text)


if __name__ == "__main__":
    # Windows 控制台默认 GBK，直接 print 中文回答会 UnicodeEncodeError；
    # 显式切到 UTF-8 让中文正文能正常落出来。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(main())
