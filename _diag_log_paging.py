# -*- coding: utf-8 -*-
"""2026-10-03 诊断：报名记录对账全量拉取 4/8 卡死根因（只读，无副作用）。

两段：
1) 原样调用 pipeline.read_activity_log_records（生产代码路径），context 级挂钩
   记录每个 enroll/list 请求的发出/响应时间、状态码、success/errorMsg/total。
2) 自己开页手动复现翻页：每次点下一页前打印按钮 found/disabled；若 disabled
   则抓分页组件 outerHTML 并每秒轮询观察是否恢复；点击后记录响应耗时。
"""
import asyncio
import json
import time

from playwright.async_api import async_playwright

from app.activity import pipeline
from app.collect.service import CDP_URL
from app.temu_region import Region, host_of, url_in_region

SPUS = ["8791757215", "3822224199", "1619974426"]
T0 = time.time()
req_log = []
resp_log = []
_seq = [0]


def _now():
    return round(time.time() - T0, 2)


def hook_context(ctx):
    def on_request(req):
        if "/marketing/enroll/list" in (req.url or ""):
            _seq[0] += 1
            try:
                body = req.post_data or ""
            except Exception:
                body = ""
            req_log.append({"seq": _seq[0], "t": _now(), "method": req.method, "body": body[:300]})

    async def on_response(resp):
        if "/marketing/enroll/list" in (resp.url or ""):
            info = {"t": _now(), "status": resp.status}
            try:
                payload = await resp.json()
                res = payload.get("result") if isinstance(payload, dict) else None
                info["success"] = payload.get("success") if isinstance(payload, dict) else None
                info["errorMsg"] = payload.get("errorMsg") if isinstance(payload, dict) else None
                if isinstance(res, dict):
                    info["total"] = res.get("total")
                    info["list_len"] = len(res.get("list") or [])
                    info["pageSize"] = res.get("pageSize")
                else:
                    info["result_type"] = type(res).__name__
            except Exception as exc:
                info["json_err"] = str(exc)[:100]
            resp_log.append(info)

    ctx.on("request", on_request)
    ctx.on("response", lambda r: asyncio.create_task(on_response(r)))


async def phase1(ctx, region):
    print("=== 阶段1：原样调用生产函数 read_activity_log_records ===", flush=True)
    try:
        result = await pipeline.read_activity_log_records(ctx, SPUS, region=region)
        summary = {k: v for k, v in result.items() if k != "records"}
        print("返回:", json.dumps(summary, ensure_ascii=False, default=str), flush=True)
        print("records 条数:", len(result.get("records") or []), flush=True)
    except Exception as exc:
        print("抛异常:", repr(exc), flush=True)


async def phase2(ctx, region):
    print("=== 阶段2：手动插桩翻页 ===", flush=True)
    page = await ctx.new_page()
    try:
        url = url_in_region(pipeline.ACTIVITY_LOG_PATH, region)
        async with page.expect_response(
            lambda r: "/marketing/enroll/list" in r.url, timeout=30000
        ) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        resp = await resp_info.value
        payload = await resp.json()
        first = payload.get("result") or {}
        total = int(first.get("total") or 0)
        size = int(first.get("pageSize") or len(first.get("list") or []) or 10)
        expected = max(1, (total + size - 1) // size)
        print(f"第1页: total={total} pageSize={size} expected_pages={expected}", flush=True)
        if "authentication" in page.url:
            print("!! 被跳到登录页:", page.url, flush=True)
            return

        for page_no in range(2, expected + 1):
            state = await page.evaluate(pipeline._MARK_ACTIVITY_LOG_NEXT_JS)
            print(f"-> 第{page_no}页前 按钮状态: {state}", flush=True)
            if state.get("disabled"):
                dom = await page.evaluate(
                    "() => { const ul = document.querySelector('ul[data-testid=\"beast-core-pagination\"]');"
                    " return ul ? ul.outerHTML.slice(0, 800) : 'NO_PAGINATION'; }"
                )
                print(f"   disabled 时分页DOM: {dom}", flush=True)
                # 轮询观察是否恢复
                for wait in range(1, 16):
                    await asyncio.sleep(1.0)
                    again = await page.evaluate(pipeline._MARK_ACTIVITY_LOG_NEXT_JS)
                    print(f"   等{wait}s后再查: {again}", flush=True)
                    if again.get("found") and not again.get("disabled"):
                        break
                if again.get("disabled") or not again.get("found"):
                    print("   !! 等15s仍不可用，停止手动翻页", flush=True)
                    break
            if not state.get("found"):
                print("   !! 未找到下一页按钮，停止", flush=True)
                break
            await asyncio.sleep(2.0)  # 与生产代码一致
            t_click = _now()
            try:
                async with page.expect_response(
                    lambda r: "/marketing/enroll/list" in r.url, timeout=15000
                ) as next_info:
                    await page.evaluate(
                        "() => document.querySelector('[data-kiro-log-next=\"1\"]').click()"
                    )
                next_resp = await next_info.value
                next_payload = await next_resp.json()
                res = next_payload.get("result")
                ok = isinstance(res, dict)
                print(f"   第{page_no}页响应: 耗时{round(_now()-t_click,2)}s "
                      f"success={next_payload.get('success')} errorMsg={next_payload.get('errorMsg')} "
                      f"result={'dict' if ok else type(res).__name__} "
                      f"list_len={len(res.get('list') or []) if ok else '-'}", flush=True)
                if not ok:
                    print(f"   !! 被限流/异常响应: {json.dumps(next_payload, ensure_ascii=False)[:200]}", flush=True)
                    break
            except Exception as exc:
                print(f"   !! 第{page_no}页点击/等响应异常: {repr(exc)[:150]}", flush=True)
                break
        print("手动翻页结束，当前URL:", page.url, flush=True)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main():
    # 不用 service._connect_pages：它会 confirm_region_from_context 读顶栏，
    # 而此刻浏览器现有页签顶栏读不到（页签状态问题），诊断只需要 host。
    # host 从现有卖家页签的 URL 运行时取，不写死域名。
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0]
    hook_context(ctx)
    try:
        print("现有页签:", flush=True)
        host = ""
        for p in ctx.pages:
            u = getattr(p, "url", "") or ""
            print("  -", u[:110], flush=True)
            h = host_of(u)
            if not host and "agentseller" in h:
                host = h
        if not host:
            print("!! 没有卖家后台页签可推导 host，诊断中止", flush=True)
            return
        region = Region(host=host)
        print("区域 host:", host, flush=True)
        await phase1(ctx, region)
        await phase2(ctx, region)
    finally:
        print("=== enroll/list 请求时间线 ===", flush=True)
        for item in req_log:
            print("REQ ", item, flush=True)
        for item in resp_log:
            print("RESP", item, flush=True)
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
