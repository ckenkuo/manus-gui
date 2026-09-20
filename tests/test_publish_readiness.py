import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from playwright.async_api import Error, async_playwright

from app.publish import browser, persistence
from app.publish.sources import alibaba1688
from app.publish.stages import saving


@pytest_asyncio.fixture
async def isolated_page():
    async with async_playwright() as playwright:
        try:
            instance = await playwright.chromium.launch(headless=True)
        except Error:
            instance = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await instance.new_context(offline=True)
        page = await context.new_page()
        try:
            yield page
        finally:
            await instance.close()


def session_for(page):
    session = browser.BrowserSession()
    session._page = page
    session.fix_hidden_tab = AsyncMock()
    session.install_toast_watch = AsyncMock()
    session.kill_stuck_modals = AsyncMock()
    return session


async def edit_page(page):
    markup = """
      <button>发布</button>
      <div id="skuAttrsInfo"></div>
      <div id="skuDataInfo"></div>
      <div class="ant-dropdown"><ul>
        <li class="ant-dropdown-menu-item" onclick="window.publishClicks++">立即发布</li>
      </ul></div>
      <script>
        window.publishClicks = 0;
        window.populate = () => {
          document.getElementById('skuAttrsInfo').innerHTML =
            '<label class="d-checkbox"><input type="checkbox" checked>毛驴</label>';
          document.getElementById('skuDataInfo').innerHTML =
            '<table><thead><tr><th>颜色</th><th>SKU货号</th></tr></thead>' +
            '<tbody><tr><td>毛驴</td><td><input name="variationSku" value="Donkey"></td></tr></tbody></table>';
        };
      </script>
    """

    async def serve(route):
        await route.fulfill(content_type="text/html; charset=utf-8", body=markup)

    await page.route("**/*", serve)
    await page.goto("https://publish.test/web/popTemu/edit?id=123")


@pytest.mark.asyncio
async def test_publish_waits_for_variant_hydration(isolated_page, monkeypatch):
    await edit_page(isolated_page)
    session = session_for(isolated_page)
    evaluate = session.eval_json
    click_states = []

    async def evaluate_publish(code, **kwargs):
        if code == persistence._JS_CLICK_PUBLISH_NOW:
            click_states.append(await evaluate(persistence._JS_PUBLISH_READY))
        if code == persistence._JS_CONFIRM_PUBLISH_MODAL:
            return {"confirmed": None}
        if code == persistence._JS_PUBLISH_FEEDBACK:
            return {"messages": ["已提交"], "errors": []}
        return await evaluate(code, **kwargs)

    session.eval_json = evaluate_publish
    monkeypatch.setattr(persistence, "_publish_landed", AsyncMock(return_value={"published": True}))
    await isolated_page.evaluate("setTimeout(window.populate, 800)")

    result = await persistence.publish_now(session, "123", confirm=True)

    assert result["status"] == "ok"
    assert len(click_states) == 1
    assert click_states[0]["checkedOptions"] == 1
    assert click_states[0]["skuRowCount"] == 1
    assert await isolated_page.evaluate("window.publishClicks") == 1
    session.install_toast_watch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["empty", "unchecked", "blank-code", "inventory-only"])
async def test_incomplete_form_never_clicks_publish(isolated_page, monkeypatch, partial):
    await edit_page(isolated_page)
    if partial != "empty":
        await isolated_page.evaluate("window.populate()")
    if partial == "unchecked":
        await isolated_page.locator('input[type="checkbox"]').uncheck()
    elif partial == "blank-code":
        await isolated_page.locator('input[name="variationSku"]').fill("")
    elif partial == "inventory-only":
        await isolated_page.evaluate("document.querySelector('thead').textContent = '库存'")
    monkeypatch.setattr(persistence, "_PUBLISH_READY_TIMEOUT", 0.1)
    session = session_for(isolated_page)

    result = await persistence.publish_now(session, "123", confirm=True)

    assert result["status"] == "not-ready"
    assert "未点击发布" in result["reason"]
    assert await isolated_page.evaluate("window.publishClicks") == 0
    session.kill_stuck_modals.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_form_for_another_product_is_not_published(isolated_page):
    await edit_page(isolated_page)
    await isolated_page.evaluate("window.populate()")
    result = await persistence.publish_now(session_for(isolated_page), "456", confirm=True)
    assert result["status"] == "not-ready"
    assert "不一致" in result["reason"]
    assert await isolated_page.evaluate("window.publishClicks") == 0


@pytest.mark.asyncio
async def test_variant_disappearing_during_hover_prevents_click(isolated_page):
    await edit_page(isolated_page)
    await isolated_page.evaluate("""() => {
      window.populate();
      setTimeout(() => document.getElementById('skuAttrsInfo').replaceChildren(), 150);
    }""")
    result = await session_for(isolated_page).eval_json(persistence._JS_CLICK_PUBLISH_NOW)
    assert result["reason"] == "publish-form-not-ready"
    assert result["readiness"]["checkedOptions"] == 0
    assert await isolated_page.evaluate("window.publishClicks") == 0


@pytest.mark.asyncio
async def test_publish_requires_confirmation_before_any_browser_action():
    session = AsyncMock()
    result = await persistence.publish_now(session, "123")
    assert result["status"] == "refused"
    assert not session.mock_calls


@pytest.mark.asyncio
async def test_not_ready_stage_reports_saved_draft(monkeypatch):
    monkeypatch.setattr(saving, "publish_now", AsyncMock(return_value={
        "status": "not-ready", "reason": "变种未回填，未点击发布"}))
    emit = AsyncMock()
    result = await saving._st_publish({"rowid": "123", "do_publish": True,
        "state": {"stages": {"save": {"status": "ok"}}}}, None, emit)
    assert result["status"] == "fail"
    assert "未点击发布" in result["note"]
    assert "草稿已保存" in emit.call_args.args[0]["message"]


@pytest.mark.asyncio
async def test_1688_extracts_delayed_data_while_load_is_blocked(isolated_page):
    release_image = asyncio.Event()
    markup = """
      <title>采集测试商品</title><img src="/slow.png">
      <script>
        setTimeout(() => {
          window.context = {result: {data: {
            gallery: {fields: {subject: '采集测试商品', offerImgList: []}},
            mainPrice: {fields: {finalPriceModel: {tradeWithoutPromotion: {
              skuMapOriginal: [{specAttrs: '毛驴>14cm', price: 20, canBookCount: 10}]
            }}}}
          }}};
        }, 2500);
      </script>
    """

    async def serve(route):
        if route.request.url.endswith("/slow.png"):
            await release_image.wait()
            await route.fulfill(status=204)
        else:
            await route.fulfill(content_type="text/html; charset=utf-8", body=markup)

    await isolated_page.route("**/*", serve)
    session = session_for(isolated_page)
    navigate = session.navigate

    async def bounded_navigate(url):
        return await navigate(url, timeout=1)

    session.navigate = bounded_navigate
    try:
        product = await alibaba1688.fetch(
            session, "https://detail.1688.com/offer/1039846340287.html", timeout=5)
        assert product.productId == "1039846340287"
        assert product.title == "采集测试商品"
        assert product.skuMap[0]["spec"] == "毛驴>14cm"
        assert await isolated_page.evaluate("document.readyState") == "interactive"
        assert await isolated_page.evaluate("performance.getEntriesByType('navigation')[0].loadEventEnd") == 0
    finally:
        release_image.set()
        await isolated_page.wait_for_load_state("load")


@pytest.mark.asyncio
async def test_navigation_failure_is_not_treated_as_loaded():
    page = AsyncMock()
    page.goto.side_effect = RuntimeError("net::ERR_CONNECTION_REFUSED")
    session = session_for(page)
    result = await session.navigate("https://detail.1688.com/offer/1.html")
    assert result["ok"] is False
    assert "ERR_CONNECTION_REFUSED" in result["err"]
    session.install_toast_watch.assert_not_awaited()
