from playwright.sync_api import sync_playwright
import json
rows=[{"rowid":f"r{i}","offerId":f"1{i}","sourceUrl":"https://x/1","platform":"1688","platformName":"1688",
       "title":"t","cat":"c","stage":"none","marks":[],"probeError":"","img":"","shop":"P","shopId":"1",
       "site":"US","siteValue":"US","createdAt":"2026-09-15"} for i in range(1,46)]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page()
    pg.route("**/publish/collectbox", lambda r: r.fulfill(json={"items": rows}))
    pg.route("**/publish/crawlbox", lambda r: r.fulfill(json={"items": []}))
    pg.route("**/publish/banjia", lambda r: r.fulfill(json={"items": []}))
    pg.goto("http://127.0.0.1:5199/publish", wait_until="load")
    pg.wait_for_timeout(1500)
    print(pg.evaluate("""() => {
      const el = document.getElementById('scanNext');
      const chain = [];
      let n = el;
      while (n && n !== document.documentElement) {
        const cs = getComputedStyle(n);
        chain.push({
          tag: n.tagName, id: n.id, cls: n.className.toString().slice(0,60),
          display: cs.display, visibility: cs.visibility, height: n.getBoundingClientRect().height
        });
        n = n.parentElement;
      }
      return JSON.stringify(chain, null, 1);
    }"""))
    b.close()
