"""使用隔离的本地页面验证翻页等待与遮挡恢复，不连接真实订单后台。"""
from time import monotonic

import pytest
import pytest_asyncio
from playwright.async_api import Error, async_playwright

from app.orders import pipeline as P


@pytest_asyncio.fixture
async def pagination_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Error:
            try:
                browser = await playwright.chromium.launch(channel="chrome", headless=True)
            except Error as error:
                pytest.skip(f"未安装测试用 Chromium/Chrome：{error}")
        try:
            page = await browser.new_page()
            await page.set_content("""
                <ul data-testid="beast-core-pagination" data-status="beast-core-pagination-20-1">
                  <li data-testid="beast-core-pagination-next" class="PGT_next_123"
                      style="display:none;width:100px;height:40px"
                      onclick="window.clicks++;this.parentElement.setAttribute('data-status','beast-core-pagination-20-2')">Next</li>
                </ul>
                <table><tbody><tr><td>order</td></tr></tbody></table>
                <script>window.clicks = 0;</script>
            """)
            yield page
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_next_button_appearing_after_four_seconds_still_advances_once(pagination_page):
    await pagination_page.evaluate("""() => {
        setTimeout(() => document.querySelector('li').style.display = 'block', 4000);
    }""")

    assert await P.goto_next_page(pagination_page, timeout_ms=8000)
    assert await pagination_page.evaluate("window.clicks") == 1
    assert await pagination_page.locator(P._PAGINATION).get_attribute("data-status") == "beast-core-pagination-20-2"


@pytest.mark.asyncio
async def test_permanently_hidden_next_button_respects_budget_without_dom_click(pagination_page):
    started = monotonic()

    with pytest.raises(P.PlaywrightTimeoutError):
        await P.goto_next_page(pagination_page, timeout_ms=700)

    assert monotonic() - started < 3
    assert await pagination_page.evaluate("window.clicks") == 0


@pytest.mark.asyncio
async def test_persistent_pointer_overlay_still_recovers_with_single_dom_click(pagination_page):
    await pagination_page.evaluate("""() => {
        document.querySelector('li').style.display = 'block';
        const overlay = document.createElement('div');
        overlay.className = '_2aO2Moy7';
        overlay.style.cssText = 'position:fixed;inset:0;z-index:9999';
        document.body.appendChild(overlay);
    }""")

    assert await P.goto_next_page(pagination_page, timeout_ms=10000)
    assert await pagination_page.evaluate("window.clicks") == 1
