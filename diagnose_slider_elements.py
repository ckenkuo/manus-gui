"""诊断 1688 页面上被误判为滑块的元素"""
import asyncio
from app.publish.browser import BrowserSession
from app.logger import logger

async def diagnose_slider():
    session = BrowserSession()
    await session.open()

    test_url = "https://detail.1688.com/offer/912718898367.html"
    logger.info(f"导航到：{test_url}")
    await session.navigate(test_url)

    # 等待页面稳定
    await asyncio.sleep(3)

    # 详细诊断所有匹配的元素
    result = await session.eval_json("""
    (() => {
        const checks = [];

        // 检查所有可能被误判为滑块的选择器
        const selectors = [
            '[class*="slider"]',
            '[class*="verify"]',
            '#nc_1_wrapper',
            '#baxia-dialog',
            '.baxia-dialog',
            '.nc-container',
            '#nocaptcha',
            '.nch-container'
        ];

        selectors.forEach(sel => {
            const els = document.querySelectorAll(sel);
            els.forEach(el => {
                const r = el.getBoundingClientRect();
                const visible = r.width > 20 && r.height > 20;
                const display = window.getComputedStyle(el).display;
                const visibility = window.getComputedStyle(el).visibility;

                checks.push({
                    selector: sel,
                    id: el.id || '',
                    className: el.className || '',
                    tagName: el.tagName,
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    visible: visible,
                    display: display,
                    visibility: visibility,
                    textContent: (el.textContent || '').trim().slice(0, 100)
                });
            });
        });

        // 检查 window.context 数据
        const hasContext = typeof window.context !== 'undefined';
        const hasResult = hasContext && typeof window.context.result !== 'undefined';
        const hasData = hasResult && typeof window.context.result.data !== 'undefined';

        return {
            matchedElements: checks,
            hasContext,
            hasResult,
            hasData,
            contextKeys: hasContext ? Object.keys(window.context) : [],
            resultKeys: hasResult ? Object.keys(window.context.result) : [],
            dataKeys: hasData ? Object.keys(window.context.result.data) : []
        };
    })()
    """)

    logger.info("=== 匹配到的元素 ===")
    for el in result.get("matchedElements", []):
        logger.info(f"选择器: {el['selector']}")
        logger.info(f"  标签: {el['tagName']}, ID: {el['id']}, Class: {el['className']}")
        logger.info(f"  尺寸: {el['width']}×{el['height']}, 可见: {el['visible']}")
        logger.info(f"  display: {el['display']}, visibility: {el['visibility']}")
        logger.info(f"  文本: {el['textContent'][:50]}")
        logger.info("")

    logger.info("=== window.context 状态 ===")
    logger.info(f"hasContext: {result.get('hasContext')}")
    logger.info(f"hasResult: {result.get('hasResult')}")
    logger.info(f"hasData: {result.get('hasData')}")
    logger.info(f"context keys: {result.get('contextKeys')}")
    logger.info(f"result keys: {result.get('resultKeys')}")
    logger.info(f"data keys: {result.get('dataKeys')[:10] if result.get('dataKeys') else []}")

    await session.close()

if __name__ == "__main__":
    asyncio.run(diagnose_slider())
