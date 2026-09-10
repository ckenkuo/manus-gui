"""店小秘发布操作：sku_codes。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import re
from app.logger import logger
from app.publish.browser import BrowserSession, J


# ---- 阶段⑩ SKU 货号（fix_sku_codes）-----------------------------------------
# 平台硬校验：SKU 货号不能包含中文和中文符号（input 的 placeholder 与行内红字都这么写）。
# 而店小秘的「一键生成」是把【源 SKU 名】原样拼进去的，中文商品必然生成
# `粉红色-80cm（适合身高70）` 这种「中文 + 全角括号」的值，一路带到 ⑭ save 就被拦下。
#
# 2026-08-23 真站取证（rowid 173539495453435641，8 行变种）：
#   - 只有 2 行有货号，且都是上面那种中文值，其余 6 行是空的（空货号同样过不了校验）；
#   - 平台生成的那 2 行连尺码都与所在行对不上——尺码列是「90」的那行，货号写的是
#     `粉红色-100cm（适合身高90）`。它拼的是源 SKU 原名，与页面归一后的尺码不同源。
# 故本阶段【完全重写】货号、不沿用也不修补页面现值：以行自己的颜色/尺码两列为准
# （那是页面上唯一可信的行标识），中文词交 LLM 翻译成英文后拼 `<颜色>-<尺码>`。
#
# 为什么翻译而不是转拼音：货号会出现在 Temu 后台与对账单里给运营看，`Fenhongse-80`
# 没人读得懂，`Pink-80` 才是跨境电商的通行写法。
#
# 【并发按「词」而不是按「行」】8 行只有 2 个颜色，按行翻会重复问 4 遍同一个词。
# 故先把颜色/尺码去重成词表，一词一次调用并发问，再按行拼装——LLM 调用数从行数
# 降到词数，同一个词在所有行里也必然译得一致（按行并发做不到这点）。

SKU_CODE_CONCURRENCY = 4    # 词表翻译并发数（每次输入极小，主要是压往返延迟）


# 货号只保留 ASCII 字母数字：内部连字符/空格/标点一律去掉，让整个货号里唯一的 `-`
# 就是「颜色-尺码」那个分隔符，回读校验也好判。
_SKU_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def sku_token(text: str) -> str:
    """把一段词清成货号可用的 token（只留 ASCII 字母数字，各词段首字母大写）。

    `Navy Blue` → `NavyBlue`，`off-white` → `OffWhite`，`80cm` → `80cm`，`XL` → `XL`。
    按分隔符切开再逐段首字母大写，而不是整串只大写第一个字符——后者会把
    `off-white` 压成 `Offwhite`，词界糊掉后运营看货号要费劲。段内其余字符原样保留
    （`XL` 不能被压成 `Xl`）。
    清完为空说明这个词还是中文/纯符号（翻译没生效），交调用方判失败。
    """
    parts = [x for x in _SKU_TOKEN_RE.split(str(text or "")) if x]
    return "".join(x[:1].upper() + x[1:] for x in parts)


def has_cjk(text: str) -> bool:
    """是否含非 ASCII 字符（中文与中文符号都落在这里）。

    平台文案说的是「中文和中文符号」，但校验放宽到非 ASCII 更安全：全角括号、
    全角空格、日文假名等都不该出现在货号里，且这条判据与页面 input 的实际表现一致。
    """
    return any(ord(c) > 127 for c in str(text or ""))


# 【颜色/尺码列必须按表头定位，不能写死 tds[0]/tds[1]】2026-08-28 真站取证
# （草稿 173539495451708963，类目仿真花）：无尺码类目的表头是
#   ["预览图( 批量)", "颜色", "SKU货号…", "EAN…", "申报价格…", …]
# 即 tds[0] 是【预览图】（文本空）、tds[1] 才是颜色，压根没有尺码列。原实现按下标取，
# 于是 color='' / size='【大吉大梨】梨花筒（life盆）'——颜色尺码整体错位一列，
# 货号会拼成「颜色名当尺码」的形状，且中文颜色被当尺码送去翻译。
# 服装类目下 tds[0] 恰好是颜色纯属巧合（那边表头第一列就是颜色）。
# 故改为读 thead 找「颜色」「尺码」两列的真实下标，与项目「按 Sheet 真实表头写入」
# 的既有取向一致（见 CLAUDE.md 的已知陷阱）。尺码列不存在时 sizeIdx=-1，
# 该列按空串处理——单维商品的货号只用颜色，见 fix_sku_codes 的拼装逻辑。
_JS_READ_SKU_CODES = r"""(() => {
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[0];
  if (!tb) return JSON.stringify({err: 'no-first-tbody'});
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  // 表头定位：取第一个精确等于「颜色」/含「尺码」（排除「尺码表」）的列下标。
  // 表头文案带「(批量)」这类后缀，故颜色用「以颜色开头」而不是全等（实测第 9 列
  // 也叫「颜色」——那是 SKU分类区的列，取第一个即可）。
  const heads = Array.from(sku.querySelectorAll('thead th')).map(th => txt(th));
  const colorIdx = heads.findIndex(h => /^颜色/.test(h));
  const sizeIdx = heads.findIndex(h => h.includes('尺码') && !h.includes('尺码表'));
  if (colorIdx < 0) return JSON.stringify({err: 'no-color-column', heads: heads});
  const rows = [];
  Array.from(tb.querySelectorAll('tr')).forEach((tr, i) => {
    const inp = tr.querySelector('input[name=variationSku]');
    if (!inp) return;
    const tds = Array.from(tr.querySelectorAll('td'));
    rows.push({i, color: txt(tds[colorIdx]),
               size: sizeIdx >= 0 ? txt(tds[sizeIdx]) : '',
               cur: inp.value || ''});
  });
  return JSON.stringify({rows, colorIdx: colorIdx, sizeIdx: sizeIdx, heads: heads});
})()"""


# 按【行下标】填，不按颜色/尺码匹配：读与写之间本阶段不点任何东西，行序不会变。
# 但仍逐行核对颜色/尺码是否与读到时一致——Vue 若在两次 eval 之间重排过，宁可跳过
# 那行报出来，也不能把货号填到别的 SKU 上（填错比不填更难查）。
_JS_FILL_SKU_CODES = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setVal = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const PLAN = __PLAN__;
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[0];
  if (!tb) return JSON.stringify({err: 'no-first-tbody'});
  const rows = Array.from(tb.querySelectorAll('tr'));
  // 列下标与 _JS_READ_SKU_CODES 用同一套表头判据：逐行核对必须比对同样两列，
  // 否则无尺码类目下会因为「读的是颜色、核的是预览图」而全行 row-moved（见那边注释）。
  const heads = Array.from(sku.querySelectorAll('thead th')).map(th => txt(th));
  const colorIdx = heads.findIndex(h => /^颜色/.test(h));
  const sizeIdx = heads.findIndex(h => h.includes('尺码') && !h.includes('尺码表'));
  if (colorIdx < 0) return JSON.stringify({err: 'no-color-column', heads: heads});
  const cellAt = (tds, idx) => (idx >= 0 ? txt(tds[idx]) : '');
  const filled = [], mismatch = [];
  for (const p of PLAN) {
    const tr = rows[p.i];
    if (!tr) { mismatch.push({i: p.i, why: 'no-row'}); continue; }
    const tds = Array.from(tr.querySelectorAll('td'));
    if (cellAt(tds, colorIdx) !== p.color || cellAt(tds, sizeIdx) !== p.size) {
      mismatch.push({i: p.i, why: 'row-moved',
                     now: cellAt(tds, colorIdx) + '/' + cellAt(tds, sizeIdx)});
      continue;
    }
    const inp = tr.querySelector('input[name=variationSku]');
    if (!inp) { mismatch.push({i: p.i, why: 'no-input'}); continue; }
    if (inp.value !== p.code) setVal(inp, p.code);
    filled.push(p.i);
  }
  await sleep(600);
  // 回读：目标值写进去了没有 + 还有没有非 ASCII 残留（含没进 PLAN 的行）
  const bad = [], sample = [];
  const want = {};
  PLAN.forEach(p => { want[p.i] = p.code; });
  Array.from(tb.querySelectorAll('tr')).forEach((tr, i) => {
    const inp = tr.querySelector('input[name=variationSku]');
    if (!inp) return;
    const v = inp.value || '';
    if (sample.length < 4) sample.push([i, v]);
    if (want[i] !== undefined && v !== want[i]) bad.push({i, v, want: want[i], why: 'not-written'});
    else if (!v) bad.push({i, v, why: 'empty'});
    else if (/[^\x00-\x7F]/.test(v)) bad.push({i, v, why: 'non-ascii'});
  });
  return JSON.stringify({filled: filled.length, mismatch, bad, sample});
})()"""


async def _translate_term(term: str, kind: str, sem: asyncio.Semaphore) -> tuple:
    """把一个中文词译成英文货号 token，返回 (原词, token)。

    kind 只用来给模型交代这词是颜色还是尺码——同一个「灰」字，颜色该译 Gray、
    尺码语境下可能是别的说法，说清类别能少一轮返工。
    """
    from app.publish.llm import ask_json

    prompt = (
        f"你是跨境电商 Listing 专家。请把下面这个服装{kind}的中文名，"
        "翻译成跨境电商货号里常用的英文名。\n"
        f"中文{kind}：{term}\n"
        "要求：\n"
        "1. 只给英文名本身，不要解释、不要中文；\n"
        "2. 用电商通行译法，例如 粉红色→Pink、藏青色→Navy、卡其色→Khaki、均码→OneSize；\n"
        "3. 只允许英文字母和数字，不要空格、连字符和任何标点（多个单词直接首字母大写连写）。\n"
        '只输出严格JSON：{"en":"英文名"}'
    )
    async with sem:
        data = await ask_json(prompt, what=f"{kind}翻译（{term}）", stage="sku_code")
    return term, sku_token(data.get("en", ""))


async def fix_sku_codes(session: BrowserSession) -> dict:
    """阶段⑩前置：重写全部 SKU 货号为纯 ASCII 的 `<颜色英文>-<尺码>`。

    平台不收含中文/中文符号的货号，而平台自己「一键生成」的就是中文值（见本节
    上方的真站取证）。这里按行的颜色/尺码列重新拼：中文词去重后并发交 LLM 翻译，
    英文/数字词（80、XL 这类）直接用，不浪费一次调用。

    重名保护：不同中文颜色可能译成同一个英文（「粉色」「粉红色」都译 Pink），
    那样两行货号会撞。撞了就在尾部加序号（`Pink-80-2`）保证逐行唯一——货号是 SKU
    的唯一标识，重复了平台侧对不上账。

    翻译失败不兜底（不转拼音、不留中文）：货号填错会一路带到发布，比这一阶段直接
    失败更糟，交上层重试处理。
    """
    read = await session.eval_json(_JS_READ_SKU_CODES)
    if read.get("err"):
        return {"status": "error", "reason": f"读货号列失败：{read['err']}"}
    rows = read.get("rows") or []
    if not rows:
        return {"status": "error", "reason": "变种表无 variationSku 输入框（尺码未勾选？）"}
    # 无尺码类目（仿真花/玩具等）没有尺码列，货号退化成纯颜色。记一行日志说明这不是
    # 读漏了列——sizeIdx=-1 是页面事实，见 _JS_READ_SKU_CODES 上方的表头取证。
    if read.get("sizeIdx", -1) < 0:
        logger.info(f"变种表无尺码列（本类目无尺码维），货号按纯颜色拼；"
                    f"表头：{read.get('heads')}")

    # 1) 词表：颜色与尺码各自去重，只把含非 ASCII 的词送去翻译
    colors = sorted({r["color"] for r in rows if r.get("color")})
    sizes = sorted({r["size"] for r in rows if r.get("size")})
    todo = [(c, "颜色") for c in colors if has_cjk(c)] + \
           [(s, "尺码") for s in sizes if has_cjk(s)]

    mapping = {w: sku_token(w) for w in colors + sizes if not has_cjk(w)}
    if todo:
        sem = asyncio.Semaphore(SKU_CODE_CONCURRENCY)
        logger.info(f"SKU 货号：{len(todo)} 个中文词待翻译（并发 {SKU_CODE_CONCURRENCY}）")
        pairs = await asyncio.gather(*(_translate_term(w, k, sem) for w, k in todo))
        for term, token in pairs:
            mapping[term] = token

    # 译完仍为空或仍含非 ASCII 的词：拼进货号必然过不了平台校验，直接失败
    untranslated = sorted(w for w, t in mapping.items() if not t or has_cjk(t))
    if untranslated:
        return {"status": "error",
                "reason": f"这些词未能译成合法货号 token：{untranslated}"}

    # 2) 逐行拼装 + 重名加序号
    plan, used = [], {}
    for r in rows:
        parts = [mapping.get(r["color"], ""), mapping.get(r["size"], "")]
        base = "-".join(p for p in parts if p)
        if not base:
            # 颜色列也空：这才是真读不到（无尺码类目下 size 空是正常的，见上方日志）
            return {"status": "error",
                    "reason": f"第 {r['i'] + 1} 行颜色/尺码列都读不到，无法拼货号"
                              f"（表头 {read.get('heads')}，颜色列 {read.get('colorIdx')}）"}
        used[base] = used.get(base, 0) + 1
        code = base if used[base] == 1 else f"{base}-{used[base]}"
        plan.append({"i": r["i"], "color": r["color"], "size": r["size"], "code": code})

    res = await session.eval_json(_JS_FILL_SKU_CODES.replace("__PLAN__", J(plan)))
    if res.get("err"):
        return {"status": "error", "reason": f"填货号失败：{res['err']}"}

    ok = not res.get("bad") and not res.get("mismatch")
    return {"status": "ok" if ok else "validation-error",
            "rowCount": len(rows),
            "translated": {w: mapping[w] for w, _ in todo},
            "codes": [p["code"] for p in plan],
            "filled": res.get("filled"),
            "mismatch": res.get("mismatch"),
            "bad": res.get("bad"),
            "sample": res.get("sample")}
