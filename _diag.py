from playwright.sync_api import sync_playwright
import json
rows=[{"rowid":f"r{i}","offerId":f"1{i}","sourceUrl":"https://x/1","platform":"1688","platformName":"1688",
       "title":"t","cat":"c","stage":"none","marks":[],"probeError":"","img":"","shop":"P","shopId":"1",
       "site":"US","siteValue":"US","createdAt":"2026-09-15"} for i in range(1,46)]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.on("pageerror", lambda e: print("PAGEERROR:", e))
    pg.on("console", lambda m: print("CONSOLE:", m.type, m.text[:120]) if m.type=="error" else None)
    pg.route("**/publish/collectbox", lambda r: r.fulfill(json={"items": rows}))
    pg.route("**/publish/crawlbox", lambda r: r.fulfill(json={"items": []}))
    pg.route("**/publish/banjia", lambda r: r.fulfill(json={"items": []}))
    pg.goto("http://127.0.0.1:5199/publish", wait_until="load")
    for t in range(0, 12):
        pg.wait_for_timeout(500)
        st = pg.evaluate("""() => ({
            n: typeof scanItems==='undefined'? 'undef' : scanItems.length,
            page: typeof scanPager==='undefined'? 'undef' : scanPager.page,
            bodyHidden: document.getElementById('scanBody').classList.contains('d-none'),
            nextVisible: !!document.getElementById('scanNext').offsetParent,
            info: document.getElementById('scanPageInfo').textContent
        })""")
        print(t*0.5, "s ->", json.dumps(st, ensure_ascii=False))
    b.close()
