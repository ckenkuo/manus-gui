"""接管 CDP 探测 window.context 结构"""
import asyncio
from app.publish.browser import BrowserSession
from app.logger import logger
import json

async def probe_window_context():
    session = BrowserSession()
    try:
        await session.open()

        # 导航到测试页面
        test_url = "https://detail.1688.com/offer/912718898367.html"
        logger.info(f"导航到：{test_url}")
        await session.navigate(test_url)

        # 等待页面稳定
        logger.info("等待 3 秒让页面稳定...")
        await asyncio.sleep(3)

        # 探测 window.context 的完整结构
        result = await session.eval_json("""
        (() => {
            if (typeof window.context === 'undefined') {
                return {error: 'window.context 不存在'};
            }

            const ctx = window.context;
            const info = {
                contextExists: true,
                contextKeys: Object.keys(ctx),
                resultExists: typeof ctx.result !== 'undefined'
            };

            if (ctx.result) {
                info.resultKeys = Object.keys(ctx.result);
                info.resultType = typeof ctx.result;

                // 检查各种可能的数据路径
                info.paths = {
                    'result.data': typeof ctx.result.data,
                    'result.content': typeof ctx.result.content,
                    'result.model': typeof ctx.result.model
                };

                // 如果 result.data 存在,看看它的键
                if (typeof ctx.result.data !== 'undefined') {
                    info.dataExists = true;
                    info.dataKeys = Object.keys(ctx.result.data).slice(0, 30);
                    info.dataType = typeof ctx.result.data;
                } else {
                    info.dataExists = false;
                }

                // 尝试序列化 result 的前 1000 字符
                try {
                    const resultStr = JSON.stringify(ctx.result);
                    info.resultJsonLength = resultStr.length;
                    info.resultSample = resultStr.slice(0, 1000);
                } catch (e) {
                    info.resultJsonError = String(e);
                }
            }

            return info;
        })()
        """)

        logger.info("=== window.context 探测结果 ===")
        logger.info(json.dumps(result, indent=2, ensure_ascii=False))

        # 如果 data 不存在,再详细看看 result 里到底有什么
        if result.get('resultExists') and not result.get('dataExists'):
            logger.warning("⚠️ window.context.result 存在但 data 不存在!")
            logger.info("resultSample 内容:")
            logger.info(result.get('resultSample', ''))

    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(probe_window_context())
