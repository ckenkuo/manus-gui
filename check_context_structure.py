"""查看 window.context.result 的实际结构"""
import asyncio
from app.publish.browser import BrowserSession
from app.logger import logger
import json

async def check_context_structure():
    session = BrowserSession()
    await session.open()

    test_url = "https://detail.1688.com/offer/912718898367.html"
    logger.info(f"导航到：{test_url}")
    await session.navigate(test_url)

    # 等待页面稳定
    await asyncio.sleep(3)

    # 查看 window.context 的完整结构
    result = await session.eval_json("""
    (() => {
        if (typeof window.context === 'undefined') {
            return {error: 'window.context 不存在'};
        }

        const ctx = window.context;

        // 获取结构概览
        const overview = {
            contextExists: true,
            contextKeys: Object.keys(ctx),
            resultExists: typeof ctx.result !== 'undefined',
            resultKeys: ctx.result ? Object.keys(ctx.result) : []
        };

        // 如果 result 存在，看看它的内容
        if (ctx.result) {
            overview.resultType = typeof ctx.result;
            overview.resultIsArray = Array.isArray(ctx.result);

            // 检查常见的数据路径
            overview.hasData = typeof ctx.result.data !== 'undefined';
            overview.hasContent = typeof ctx.result.content !== 'undefined';
            overview.hasModel = typeof ctx.result.model !== 'undefined';

            // 如果有 data，看看它的键
            if (ctx.result.data) {
                overview.dataKeys = Object.keys(ctx.result.data).slice(0, 20);
            }

            // 打印 result 的 JSON（限制大小）
            try {
                const resultStr = JSON.stringify(ctx.result);
                overview.resultJsonLength = resultStr.length;
                overview.resultSample = resultStr.slice(0, 500);
            } catch (e) {
                overview.resultJsonError = String(e);
            }
        }

        return overview;
    })()
    """)

    logger.info("=== window.context 结构分析 ===")
    logger.info(json.dumps(result, indent=2, ensure_ascii=False))

    await session.close()

if __name__ == "__main__":
    asyncio.run(check_context_structure())
