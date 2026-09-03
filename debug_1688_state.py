"""诊断 1688 页面状态：检查登录态、window.context 数据、反爬拦截"""
import asyncio
from app.publish.browser import BrowserSession
from app.logger import logger

async def diagnose():
    session = BrowserSession()
    await session.open()

    # 测试 URL
    test_url = "https://detail.1688.com/offer/912718898367.html"
    logger.info(f"导航到：{test_url}")
    r = await session.navigate(test_url)
    logger.info(f"导航结果：{r}")

    # 等待页面稳定
    await asyncio.sleep(3)

    # 检查 window.context
    result = await session.eval_json("""
    (() => {
        const hasContext = typeof window.context !== 'undefined';
        const hasResult = hasContext && typeof window.context.result !== 'undefined';
        const hasData = hasResult && typeof window.context.result.data !== 'undefined';

        // 检查登录态相关元素
        const loginBtn = document.querySelector('[class*="login"]') ||
                         document.querySelector('a[href*="login"]');
        const hasLoginButton = !!loginBtn;
        const loginBtnText = loginBtn ? loginBtn.textContent.trim() : '';

        // 检查反爬拦截
        const hasSlider = !!document.querySelector('[class*="slider"]') ||
                         !!document.querySelector('[class*="verify"]') ||
                         !!document.querySelector('#nc_1_wrapper');

        // 页面标题
        const title = document.title;

        // 页面主要内容区域是否存在
        const hasMainContent = !!document.querySelector('.obj-content') ||
                              !!document.querySelector('.mod-detail-page');

        return {
            hasContext,
            hasResult,
            hasData,
            hasLoginButton,
            loginBtnText,
            hasSlider,
            title,
            hasMainContent,
            url: location.href,
            // 如果有 data，取一点样本
            sampleData: hasData ? {
                subject: window.context.result.data.subject,
                offerId: window.context.result.data.offerId
            } : null
        };
    })()
    """)

    logger.info(f"页面诊断结果：\n{result}")

    # 如果没有 context，截图看看页面长什么样
    if not result.get("hasContext"):
        screenshot_path = "C:/Users/Administrator/Desktop/manus输出/1688_no_context.png"
        await session.page.screenshot(path=screenshot_path)
        logger.info(f"已截图保存到：{screenshot_path}")

    await session.close()

if __name__ == "__main__":
    asyncio.run(diagnose())
