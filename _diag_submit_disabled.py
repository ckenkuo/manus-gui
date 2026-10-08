# -*- coding: utf-8 -*-
"""2026-10-03 诊断：8791757215 在官方大促等 5 活动「填价成功但提交按钮禁用」根因。

假设（来自 _verify_live_batch.log 事件流的完全分离）：
提报页再次搜索会重渲表格，上一 SPU 的勾选+填价随旧行卸载而丢失（无跨搜索的已选池）。
中招 5 活动都是「8791757215 填价后，尾随 3822224199/1619974426 查询为 0 行」，
提交时页面无任何已勾选商品 → 提交按钮 disabled。

验证步骤（全程绝不点提交，全部可逆）：
A. 官方大促页：搜索+勾选+全选场次+填价 8791757215（146.9）
   1) 读提交按钮状态（预期：可用）
   2) 搜索 3822224199（该活动对它查询为 0 行）→ 再读按钮（预期：禁用=复现）
   3) 再搜回 8791757215 → 读行的勾选/填值是否还在（决定性证据）
B. 限时秒杀页（对照组，该平台第二 SPU 可报）：填 8791757215（138.74）→ 填 3822224199（71.22）
   → 再搜回 8791757215 看勾选/填值是否被第二个 SPU 的搜索冲掉。
结束关掉自己开的所有页签。
"""
import asyncio
import json
import time

from app.activity import pipeline
from app.activity import service
from app.collect.service import CDP_URL

SPU_A = "8791757215"
SPU_B = "3822224199"

# 只读：商品行勾选状态 + 可报名场次文本
_ROW_STATE_JS = r"""
() => {
  let sy = null; let sessText = null;
  for (const el of document.querySelectorAll('td,div,span')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
    const m = own.match(/可报名场次[：:]\s*\d[^]*/);
    if (m) { sy = el.getBoundingClientRect().y; sessText = own.trim().slice(0, 50); break; }
  }
  if (sy === null) return {sessText: null, cbx: []};
  const ws = [...document.querySelectorAll('[class*=CBX_squareInputWrapper],label[class*=CBX_outerWrapper]')]
    .filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && Math.abs(b.y - sy) < 30; });
  return {sessText, cbx: ws.map(w => {
    const inp = w.querySelector('input');
    return {checked: !!(inp && inp.checked), cls: String(w.className || '').slice(0, 90)};
  })};
}
"""

# 只读：全页「提交」按钮枚举
_BUTTONS_JS = r"""
() => {
  const out = [];
  for (const b of document.querySelectorAll('button,[role=button]')) {
    const t = (b.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t.includes('提交')) continue;
    const r = b.getBoundingClientRect();
    out.push({
      tag: b.tagName, text: t.slice(0, 40),
      disabled_attr: !!b.disabled,
      aria_disabled: b.getAttribute('aria-disabled'),
      cls: String(b.className || '').slice(0, 140),
      visible: r.width > 0 && r.height > 0,
      outer: (b.outerHTML || '').slice(0, 300),
    });
  }
  return out;
}
"""

# 只读：行内报错/红字/可见 error·warn·tip 类元素
_ERROR_SCAN_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const out = [];
  for (const el of document.querySelectorAll('[class*=error],[class*=Error],[class*=warn],[class*=Warn],[class*=tip],[class*=Tip]')) {
    if (el.closest('[class*="dxm-"]')) continue;
    if (!visible(el)) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 200 && !out.includes(t)) out.push('CLS:' + t);
    if (out.length >= 12) break;
  }
  for (const el of document.querySelectorAll('span,div,p,td,li')) {
    if (el.closest('[class*="dxm-"]')) continue;
    if (!visible(el)) continue;
    const c = getComputedStyle(el).color;
    const m = c.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (!m) continue;
    const r = Number(m[1]), g = Number(m[2]), b = Number(m[3]);
    if (!(r > 150 && g < 110 && b < 110)) continue;
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, ' ').trim();
    const t = own || (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t && t.length <= 200 && !out.some(x => x.endsWith(t))) out.push('RED:' + t);
    if (out.length >= 28) break;
  }
  return out;
}
"""

# 只读：全页未勾选复选框（协议前置排查）
_UNCHECKED_JS = r"""
() => {
  const out = [];
  for (const w of document.querySelectorAll('[class*=CBX_squareInputWrapper]')) {
    if (w.closest('[class*="dxm-"]')) continue;
    const r = w.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) continue;
    const inp = w.querySelector('input');
    if (inp && inp.checked) continue;
    const label = w.closest('label') || w.parentElement;
    const t = (((label && label.textContent) || '')).replace(/\s+/g, ' ').trim().slice(0, 80);
    out.push({text: t, x: Math.round(r.x), y: Math.round(r.y)});
    if (out.length >= 24) break;
  }
  return out;
}
"""

# 只读：「已选/已报」商品池迹象（页面有没有跨搜索保留选择的托盘）
_POOL_SCAN_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('span,div,td,button,a')) {
    if (el.closest('[class*="dxm-"]')) continue;
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').replace(/\s+/g, '');
    if (/已选|已勾选|已添加|待提交/.test(own) && own.length <= 30) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && !out.includes(own)) out.push(own);
    }
    if (out.length >= 10) break;
  }
  return out;
}
"""

# 只读：本 SPU 价格行的输入框现值
_ROWS_DUMP_JS = r"""
(spu) => {
  const target = String(spu);
  const spuRe = /SPU\s*ID[：:\s]*([0-9]+)/;
  const anchor = [...document.querySelectorAll('tr')].find(tr => {
    const m = (tr.innerText || '').match(spuRe);
    return m && m[1] === target;
  });
  if (!anchor) return {rows: []};
  const group = [];
  for (let tr = anchor; tr; tr = tr.nextElementSibling) {
    if (tr.tagName !== 'TR') continue;
    const m = (tr.innerText || '').match(spuRe);
    if (tr !== anchor && m && m[1] !== target) break;
    group.push(tr);
  }
  return {rows: group.map(tr => ({
    text: (tr.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
    inputs: [...tr.querySelectorAll('input')].map(i => ({
      v: i.value, disabled: i.disabled, type: i.type || 'text',
    })),
  }))};
}
"""


async def observe(page, tag, shot=False):
    """抓一次提交按钮与页面现场快照（全只读）。"""
    snap = {"tag": tag}
    btns = page.get_by_role("button", name="提交")
    try:
        snap["role_count"] = await btns.count()
    except Exception as e:
        snap["role_count"] = f"err:{e}"
    try:
        snap["first_is_disabled"] = await btns.first.is_disabled()
    except Exception as e:
        snap["first_is_disabled"] = f"err:{e}"
    for key, js in (("buttons", _BUTTONS_JS), ("row", _ROW_STATE_JS),
                    ("errors", _ERROR_SCAN_JS), ("unchecked", _UNCHECKED_JS),
                    ("pool", _POOL_SCAN_JS)):
        try:
            snap[key] = await page.evaluate(js)
        except Exception as e:
            snap[key] = f"err:{e}"
    print("SNAP " + json.dumps(snap, ensure_ascii=False), flush=True)
    if shot:
        path = f"_diag_submit_{tag}.png"
        try:
            await page.screenshot(path=path, full_page=False)
            print(f"SCREENSHOT {path}", flush=True)
        except Exception as e:
            print(f"截图失败：{e}", flush=True)


async def fill_spu(page, spu, price_str, sku_prices):
    """与 enroll_activity 同序列：勾选→全选场次→逐行填价→库存栏（不提交）。返回是否填成功。"""
    chk = await page.evaluate(pipeline._CHECK_ROW_JS)
    await asyncio.sleep(1.5)
    print(f"  CHECK_ROW {spu} -> {chk}", flush=True)
    if chk != "ok":
        return False
    sess = await pipeline._set_sessions_all(page)
    print(f"  SESSIONS {spu} -> {json.dumps(sess, ensure_ascii=False)}", flush=True)
    if not sess.get("ok"):
        return False
    info = None
    for _ in range(6):
        info = await page.evaluate(pipeline._MARK_ENROLL_SKU_ROWS_JS, spu)
        if info and info.get("rows"):
            break
        await asyncio.sleep(0.5)
    rows = (info or {}).get("rows") or []
    print(f"  ROWS {spu} -> {json.dumps(rows, ensure_ascii=False)}", flush=True)
    assigned = pipeline.match_enroll_rows(sku_prices, rows)
    print(f"  MATCHED {spu} -> {json.dumps(assigned, ensure_ascii=False)}", flush=True)
    if assigned is None:
        return False
    for row in rows:
        a = assigned[row["idx"]]
        inp = page.locator(f'[data-enroll-sku-idx="{row["idx"]}"]').first
        await inp.click()
        await inp.fill(str(a["submit_price"]))
        back = (await inp.input_value() or "").strip()
        print(f"  FILL {spu} row{row['idx']} {a['label']} wrote={a['submit_price']} back={back}", flush=True)
    stock = await page.evaluate(pipeline._MARK_STOCK_INPUT_JS)
    print(f"  STOCK {spu} -> {json.dumps(stock, ensure_ascii=False)}", flush=True)
    if stock.get("found"):
        inp = page.locator('[data-enroll-stock="1"]').first
        await inp.click()
        await inp.fill(str(stock["refStock"]))
        back = (await inp.input_value() or "").strip()
        print(f"  STOCK_FILL wrote={stock['refStock']} back={back}", flush=True)
    return True


async def search_only(page, spu):
    """只搜索（probe_detail_eligibility 只读路径：不勾选不填价）。"""
    probe = await pipeline.probe_detail_eligibility(page, spu)
    print(f"  SEARCH {spu} -> {json.dumps(probe, ensure_ascii=False)}", flush=True)
    return probe


async def scenario_A(activity_page, owned):
    """官方大促：填 8791757215 → 搜 3822224199(0 行) → 搜回 8791757215 看现场。"""
    print("\n===== 场景A：官方大促（中招活动）=====", flush=True)
    page = await pipeline.open_enroll_page(activity_page, "官方大促")
    if page is None:
        print("打开官方大促提报页失败", flush=True)
        return
    owned.append(page)
    sku_prices = [
        {"label": "70cm0.2kg", "daily": 163.23, "sale": 63.0, "submit_price": 146.9},
        {"label": "90cm0.4kg", "daily": 163.23, "sale": 97.938, "submit_price": 146.9},
    ]
    probe = await search_only(page, SPU_A)
    if not probe.get("detail_eligible"):
        print("8791757215 在官方大促查不到，场景A中止", flush=True)
        return
    ok = await fill_spu(page, SPU_A, "146.9", sku_prices)
    print(f"FILL_DONE {SPU_A} ok={ok}", flush=True)
    if not ok:
        return
    await observe(page, "A1_filled_A", shot=True)          # 预期：按钮可用
    await asyncio.sleep(3)
    await observe(page, "A1b_filled_A_3s")                  # 排除瞬时禁用
    await asyncio.sleep(7)
    await observe(page, "A1c_filled_A_10s")

    await search_only(page, SPU_B)                          # 尾随搜索（0 行）
    await observe(page, "A2_after_search_B", shot=True)     # 预期：按钮禁用=复现批次
    await asyncio.sleep(5)
    await observe(page, "A2b_after_search_B_5s")

    await search_only(page, SPU_A)                          # 搜回 A
    await observe(page, "A3_research_A", shot=True)
    dump = await page.evaluate(_ROWS_DUMP_JS, SPU_A)
    print(f"ROWS_DUMP_A {json.dumps(dump, ensure_ascii=False)}", flush=True)
    # 输 30s 后再读一次按钮（看解禁假设是否成立）
    await asyncio.sleep(20)
    await observe(page, "A4_research_A_20s")


async def scenario_B(activity_page, owned):
    """限时秒杀（对照：第二 SPU 可报）：填 A → 填 B → 搜回 A 看勾选/填值。"""
    print("\n===== 场景B：限时秒杀（对照组，双 SPU 可报）=====", flush=True)
    page = await pipeline.open_enroll_page(activity_page, "限时秒杀")
    if page is None:
        print("打开限时秒杀提报页失败", flush=True)
        return
    owned.append(page)
    sku_a = [
        {"label": "70cm0.2kg", "daily": 163.23, "sale": 63.0, "submit_price": 138.74},
        {"label": "90cm0.4kg", "daily": 163.23, "sale": 97.938, "submit_price": 138.74},
    ]
    probe = await search_only(page, SPU_A)
    if not probe.get("detail_eligible"):
        print("8791757215 在限时秒杀查不到，场景B中止", flush=True)
        return
    ok = await fill_spu(page, SPU_A, "138.74", sku_a)
    print(f"FILL_DONE {SPU_A} ok={ok}", flush=True)
    if not ok:
        return
    await observe(page, "B1_filled_A", shot=True)

    probe_b = await search_only(page, SPU_B)
    if not probe_b.get("detail_eligible"):
        print("3822224199 在限时秒杀查不到（与批次不符？），只做到 B1", flush=True)
        return
    sku_b = [{"label": "默认", "daily": 83.8, "sale": 71.23, "submit_price": 71.22}]
    ok_b = await fill_spu(page, SPU_B, "71.22", sku_b)
    print(f"FILL_DONE {SPU_B} ok={ok_b}", flush=True)
    await observe(page, "B2_filled_B", shot=True)           # 此刻按钮可用但池里可能只剩 B

    await search_only(page, SPU_A)                          # 搜回 A：勾选/填值还在吗？
    await observe(page, "B3_research_A", shot=True)
    dump = await page.evaluate(_ROWS_DUMP_JS, SPU_A)
    print(f"ROWS_DUMP_B {json.dumps(dump, ensure_ascii=False)}", flush=True)


async def main():
    pw, browser, _flux, activity_page, _goods, owned = await service._connect_pages(
        CDP_URL, "", need_flux=False)
    try:
        await scenario_A(activity_page, owned)
        await scenario_B(activity_page, owned)
    finally:
        for page in owned:
            try:
                await page.close()
            except Exception:
                pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass
    print("诊断结束（未点击任何提交按钮）", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
