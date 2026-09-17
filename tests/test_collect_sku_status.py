"""在隔离浏览器里执行实际枚举脚本；接口数据全部由本地样本替代。"""
import pytest
from playwright.sync_api import Error, sync_playwright

from app.collect import service as S


@pytest.fixture(scope="module")
def script_page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except Error as error:
                pytest.skip(f"未安装测试用 Chromium/Chrome：{error}")
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _sku(sku_id, status, site_status=None):
    return {
        "skuId": sku_id,
        "priceReviewStatus": status,
        "productPropertyList": [{"value": f"规格{sku_id}"}],
        "siteSupplierPriceList": [{
            "siteId": 163, "siteName": "秘鲁", "supplierPrice": "39.00¥",
            "priceReviewStatus": site_status,
        }],
    }


def _enumerate(script_page, skus, reviews=None):
    product = {
        "productId": "SPU-1", "siteName": "秘鲁", "supplierPrice": "39.00¥",
        "skcList": [{"skuList": skus, "supplierPriceReviewInfoList": reviews or []}],
    }
    script_page.evaluate("""product => {
        window.fetch = async () => ({json: async () => ({result: {dataList: [product]}})});
    }""", product)
    return script_page.evaluate(S._FETCH_ALL_JS, {
        "mallid": "test-store", "url": "/fake-list", "body": '{"pageSize":50}',
    })


@pytest.mark.parametrize("status", [3, "3"])
def test_voided_sku_is_excluded_but_live_sibling_remains(script_page, status):
    rows = _enumerate(script_page, [_sku(11, status), _sku(12, 2)])

    assert len(rows) == 2
    assert [row["sku_id"] for row in S.collapse_spus(rows)] == ["12"]


def test_all_voided_skus_do_not_reappear_as_spu_fallback(script_page):
    rows = _enumerate(script_page, [_sku(11, 3), _sku(12, 3)])

    assert all(row["sku_id"] for row in rows)
    assert S.collapse_spus(rows) == []


@pytest.mark.parametrize("site_status, expected", [(0, 0), (2, 2), (3, 3), (None, 3)])
def test_site_status_takes_precedence_over_sku_status(script_page, site_status, expected):
    rows = _enumerate(script_page, [_sku(11, 3, site_status)])

    assert rows[0]["price_review_status"] == expected


def test_displayed_review_status_takes_precedence_and_matches_sku_and_site(script_page):
    reviewed = _sku(11, 2)
    reviews = [
        {"status": 2, "productSkuList": [_sku(12, 2)]},
        {"status": 2, "siteList": [{"siteId": 100}], "productSkuList": [reviewed]},
        {"status": 3, "siteList": [{"siteId": 163}], "productSkuList": [reviewed]},
    ]
    rows = _enumerate(script_page, [reviewed, _sku(12, 2)], reviews)

    assert [row["price_review_status"] for row in rows] == [3, 2]
    assert [row["sku_id"] for row in S.collapse_spus(rows)] == ["12"]


def test_another_sites_voided_status_does_not_cancel_current_site(script_page):
    current = _sku(11, 2)
    other_site = _sku(11, 3)
    other_site["siteSupplierPriceList"][0].update(siteId=100, siteName="美国站")
    rows = _enumerate(script_page, [current], [{"status": 3, "productSkuList": [other_site]}])

    assert rows[0]["price_review_status"] == 2


def test_no_sku_structure_still_produces_status_checked_spu_fallback(script_page):
    rows = _enumerate(script_page, [])

    assert len(rows) == 1 and rows[0]["sku_id"] == ""
    assert "price_review_status" in rows[0]
    assert rows[0]["price_review_status"] is None
