"""在 CDP 里那个【已保留的编辑页签】上实跑阶段⑨ 尺码表（不另开页签、不导航）。

为什么不用 publish_inspect.py add-sizechart：那条路会 open_edit(rowid)，而会话挑页签时
刻意跳过编辑页签（见 BrowserSession.open），于是它会另开一个页签导航到同一草稿；而失败
保留的那个页签里还开着「添加尺码表」弹窗，两个页签同时编辑同一草稿容易打架。这里直接把
会话挂到那个页签上跑，验证完即弃。

用法：python run_sizechart_live.py <rowid> <product-info.json>
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

from app.logger import logger
from app.publish.browser import CDP_URL, BrowserSession
from app.publish.pipeline import add_sizechart


async def attach_existing_edit_tab(session: BrowserSession, rowid: str):
    """把会话挂到 URL 里带该 rowid 的编辑页签上（open() 不会挑它，故单独接）。"""
    session._pw = await async_playwright().start()
    session._browser = await session._pw.chromium.connect_over_cdp(session.cdp_url)
    if not session._browser.contexts:
        raise RuntimeError(f"CDP 连上了但没有 context（{session.cdp_url}）")
    ctx = session._browser.contexts[0]
    page = next((p for p in ctx.pages if f"id={rowid}" in (p.url or "")), None)
    if page is None:
        raise RuntimeError(f"没有停在编辑页上的页签（id={rowid}），先手工打开该草稿")
    session._page = page
    session._watch_page(page)
    session._cdp = await ctx.new_cdp_session(page)
    await session.fix_hidden_tab()
    await session.install_toast_watch()
    return page


async def main() -> int:
    rowid, info_path = sys.argv[1], sys.argv[2]
    session = BrowserSession()
    page = await attach_existing_edit_tab(session, rowid)
    logger.info(f"已接管编辑页签：{page.url}")
    try:
        r = await add_sizechart(session, info_path)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("status") == "ok" else 1
    finally:
        await session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
