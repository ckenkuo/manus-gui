"""简单探测脚本 - 直接写结果到文件"""
import asyncio
from playwright.async_api import async_playwright
import json

async def main():
    # CDP 接管已有的 Chrome
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # 导航到测试页面
        print("导航到 1688 页面...")
        await page.goto("https://detail.1688.com/offer/912718898367.html", wait_until="load")

        # 等待 3 秒
        print("等待 3 秒...")
        await asyncio.sleep(3)

        # 执行探测 JS
        print("执行探测...")
        result = await page.evaluate("""
        () => {
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
                info.hasData = typeof ctx.result.data !== 'undefined';

                // 尝试序列化整个 result
                try {
                    const resultStr = JSON.stringify(ctx.result);
                    info.resultJsonLength = resultStr.length;
                    // 取前 2000 字符
                    info.resultSample = resultStr.substring(0, 2000);
                } catch (e) {
                    info.error = String(e);
                }
            }

            return info;
        }
        """)

        # 写入文件
        output_file = "probe_result.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"\n结果已写入 {output_file}")
        print("\n=== 探测结果 ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
