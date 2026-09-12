"""店小秘发布操作：sku_codes。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import re
from app.logger import logger
from app.publish import variant_dom
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

# 平台硬校验：Temu 的 SKC/SKU External Code 不能超过 120 字符。
# 2026-09-11 真站取证（rowid 184807703145882711，墙贴 30 行）：该商品 1688 源的
# 【尺码】维度里，卖家写的是一段销售说明「拍多件默认10米1件发，有要求5米找客服
# 备注【可封装/贴标/代发等】」，逐行翻译后 80 字符，30 行全都挂上这条尾巴——
# 第 16、22 行到 122 字符，发布被 Temu「Product SKC External Code cannot exceed
# 120 characters」拦下。故货号除翻译外还要过两道闸：丢恒定维度（见 fix_sku_codes
# 第 2 步）与按此上限截断（第 3 步）。
SKU_CODE_MAX = 120


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


# 【变种维列必须按表头结构定位，不能写死 tds[0]/tds[1]、也不能按名字穷举】
# 2026-08-28 真站取证（草稿 173539495451708963，类目仿真花）：无尺码类目的表头是
#   ["预览图( 批量)", "颜色", "SKU货号…", "EAN…", "申报价格…", …]
# 即 tds[0] 是【预览图】（文本空）、tds[1] 才是颜色，压根没有尺码列。原实现按下标取，
# 于是 color='' / size='【大吉大梨】梨花筒（life盆）'——颜色尺码整体错位一列，
# 货号会拼成「颜色名当尺码」的形状，且中文颜色被当尺码送去翻译。
# 服装类目下 tds[0] 恰好是颜色纯属巧合（那边表头第一列就是颜色）。
#
# 2026-09-11 派对桌布（rowid 184807703146387073）唯一那维装在「尺码」列、2026-09-12
# 车贴（商品 601101104447803）唯一那维叫【型号】——按名字认列的路子已经补过两次，
# 每换一个类目就再补一次。故改按结构位置认（预览图之后、SKU货号之前即变种维列），
# 判据收敛进 variant_dom._JS_DIM_COLS 一处、⑦a/⑦b/⑩a/⑩/续跑判定共用，
# 与项目「按 Sheet 真实表头写入」的既有取向一致（见 CLAUDE.md 的已知陷阱）。
# 第二维不存在时 sizeIdx=-1，该列按空串处理——单维商品的货号只用第一维，
# 见 fix_sku_codes 的拼装逻辑。
_JS_READ_SKU_CODES = r"""(() => {
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[0];
  if (!tb) return JSON.stringify({err: 'no-first-tbody'});
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const heads = Array.from(sku.querySelectorAll('thead th')).map(th => txt(th));
  __DIM_COLS__
  const {colorIdx, sizeIdx} = dimIdx(heads);
  // 维度列一个都认不出来（连 SKU货号 锚点都没有）：页面结构与实测的全不一样，
  // 报出真实表头交人工，别猜下标（静默按下标猜正是 2026-08-28 那次的病根）。
  if (colorIdx < 0) return JSON.stringify({err: 'no-dim-column', heads: heads});
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
  // 列下标与 _JS_READ_SKU_CODES 用同一段判据（都注入 variant_dom._JS_DIM_COLS）：
  // 逐行核对必须比对同样两列，否则无尺码类目下会因为「读的是颜色、核的是预览图」
  // 而全行 row-moved、货号一个都填不进去。别在这里另写一套。
  const heads = Array.from(sku.querySelectorAll('thead th')).map(th => txt(th));
  __DIM_COLS__
  const {colorIdx, sizeIdx} = dimIdx(heads);
  if (colorIdx < 0) return JSON.stringify({err: 'no-dim-column', heads: heads});
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

    【源词常是长描述，得给模型立规矩】2026-09-11 取证（rowid 184807703145882711）：
    同一批 30 个颜色，原先只说「翻成英文名」，模型就自由发挥——`Zoo` 干净利落、
    `HotAirBalloonL5MW70Cm` 带上尺寸、`CultureBrickHoneyPotBearLength5MWidth70CM`
    连尺寸全拼，货号长度从 3 到 41 字符乱跳，同一批货号看着像两拨人做的。故提示词
    改成给判据 + 给长名示例：认款必需的词（颜色/图案/款式/型号）留，说明性的（售后
    文案、数量、卖点、纯尺寸）丢。原提示词还写死了「服装」，对墙贴/水枪这类非服装
    类目是误导，一并去掉。

    注意口径只能靠规则和示例锚定：本阶段按词并发调用，模型看不到同批其他词，
    「跟同批保持一致」这种话对它没有约束力。
    """
    from app.publish.llm import ask_json

    prompt = (
        f"你是跨境电商 Listing 专家。请把下面这个商品{kind}的中文名，"
        "翻译成跨境电商货号里用的英文短名。\n"
        f"中文{kind}：{term}\n"
        "要求（只做加减法：该留的一个不落，该丢的一个不留，同一批词才不会有长有短）：\n"
        "1. 保留：颜色、图案、款式、型号、版本、尺寸档位（如「大号40CM」）、"
        "产品类型（如「水枪电池」）；颜色/图案即使在【】（）里也要保留；\n"
        "2. 丢掉：售后与优惠话术（如「拍多件默认…」）、数量、材质与功能卖点"
        "（如「储水量700ml」「可外接水瓶」「手自一体」）、括号里的附加尺寸规格"
        "（如「长5米*宽70厘米」）；\n"
        "3. 译文尽量不超过 30 个字符；\n"
        "4. 用电商通行译法：粉红色→Pink、藏青色→Navy、均码→OneSize；\n"
        "5. 只允许英文字母和数字，不要空格、连字符和任何标点（多个单词首字母大写连写）。\n"
        "示例：\n"
        "  粉红色 → Pink\n"
        "  白砖纹【长5米*宽70厘米】 → WhiteBrick\n"
        "  大号40CM版【灰色】水枪电池 → L40GrayWaterGunBattery\n"
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

    维度取舍：某维度在全部行里取值恒定，就区分不了任何 SKU——那是卖家把说明文案
    写在了属性位上（真站取证见 SKU_CODE_MAX）。拼进货号不增加任何区分能力，却把
    每行都撑长，故丢掉。两个维度各有多值时是正常的「颜色-尺码」，照旧都留。
    这一步排在翻译之前：要丢的多半是整句说明，按翻译规则译不出短名（模型返回空串），
    白翻一趟不说，还会撞在下面 untranslated 那道闸上把整个阶段判失败。

    长度上限：丢完恒定维度仍可能越界（颜色名本身就是长描述，如水枪的
    「超大号62CM手自一体【科技白】储水量700ml（可外接水瓶）」），按 SKU_CODE_MAX
    截尾——货号难看是小，整条商品被平台拒了发不出去是大。

    重名保护：不同中文颜色可能译成同一个英文（「粉色」「粉红色」都译 Pink），
    那样两行货号会撞。撞了就在尾部加序号（`Pink-80-2`）保证逐行唯一——货号是 SKU
    的唯一标识，重复了平台侧对不上账。

    翻译失败不兜底（不转拼音、不留中文）：货号填错会一路带到发布，比这一阶段直接
    失败更糟，交上层重试处理。
    """
    read = await session.eval_json(
        _JS_READ_SKU_CODES.replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    if read.get("err"):
        # 【失败必须带上真实表头】单看 no-dim-column 只知道「没认出维度列」，而表头长
        # 什么样决定了它是「这个类目的变种结构本就不同」还是「页面没渲染完」——
        # 2026-09-11 商品 1044382261282 排查时就卡在日志里只有 err、没有现场。
        return {"status": "error",
                "reason": f"读货号列失败：{read['err']}（表头 {read.get('heads')}）"}
    rows = read.get("rows") or []
    if not rows:
        return {"status": "error", "reason": "变种表无 variationSku 输入框（尺码未勾选？）"}
    # 单维类目（仿真花/玩具/车贴等）只有一个变种维，货号退化成纯第一维。记一行日志说明
    # 这不是读漏了列——sizeIdx=-1 是页面事实，见 _JS_READ_SKU_CODES 上方的表头取证。
    # 维度名一并报出来：车贴那单的唯一维叫「型号」，只说「无尺码列」会让人以为读漏了。
    if read.get("sizeIdx", -1) < 0:
        logger.info(f"变种表只有一个变种维（第 {read.get('colorIdx')} 列），"
                    f"货号按单维拼；表头：{read.get('heads')}")

    # 1) 词表：颜色与尺码各自去重
    colors = sorted({r["color"] for r in rows if r.get("color")})
    sizes = sorted({r["size"] for r in rows if r.get("size")})

    # 2) 维度取舍：某维度全部行同值 = 它区分不了任何 SKU（见 docstring 的「维度取舍」）。
    # 两个维度都恒定的情况不存在——那意味着只有一行变种，判不出「恒定」也就无从丢起。
    # 【必须在翻译之前判】要丢的多半是整句说明（「拍多件默认10米1件发…」），按翻译规则
    # 译不出短名、模型会返回空串，白花一次调用还让整个阶段失败在 untranslated 那道闸上。
    drop_color = bool(colors and sizes) and len(colors) == 1 and len(sizes) > 1
    drop_size = bool(colors and sizes) and len(sizes) == 1 and len(colors) > 1
    dropped = [k for k, d in (("颜色", drop_color), ("尺码", drop_size)) if d]
    if dropped:
        logger.info(f"货号丢掉恒定维度 {'、'.join(dropped)}"
                    f"（该列全部行同值，区分不了 SKU，拼进去只会撑长货号）")

    # 3) 只把【保留维度】里含非 ASCII 的词送去翻译
    todo = [(c, "颜色") for c in colors if has_cjk(c) and not drop_color] + \
           [(s, "尺码") for s in sizes if has_cjk(s) and not drop_size]

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

    # 4) 逐行拼装 + 截断 + 重名加序号
    # 截断要给重名序号留位（最长的序号是 "-" + 行数位数），否则加完序号又越界。
    cap = SKU_CODE_MAX - (len(str(len(rows))) + 1)
    plan, used = [], {}
    for r in rows:
        parts = []
        if not drop_color:
            parts.append(mapping.get(r["color"], ""))
        if not drop_size:
            parts.append(mapping.get(r["size"], ""))
        base = "-".join(p for p in parts if p)
        if not base:
            # 第一维也空：这才是真读不到（单维类目下第二维空是正常的，见上方日志）
            return {"status": "error",
                    "reason": f"第 {r['i'] + 1} 行两个变种维列都读不到，无法拼货号"
                              f"（表头 {read.get('heads')}，维度列 "
                              f"{read.get('colorIdx')}/{read.get('sizeIdx')}）"}
        if len(base) > cap:
            # 硬切在单词中间也比被平台拒了强；切完的尾部可能是那个分隔符，去掉更干净
            base = base[:cap].rstrip("-")
        used[base] = used.get(base, 0) + 1
        code = base if used[base] == 1 else f"{base}-{used[base]}"
        plan.append({"i": r["i"], "color": r["color"], "size": r["size"], "code": code})

    res = await session.eval_json(
        _JS_FILL_SKU_CODES.replace("__PLAN__", J(plan))
                          .replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    if res.get("err"):
        return {"status": "error", "reason": f"填货号失败：{res['err']}"}

    ok = not res.get("bad") and not res.get("mismatch")
    return {"status": "ok" if ok else "validation-error",
            "rowCount": len(rows),
            "translated": {w: mapping[w] for w, _ in todo},
            "dropped": dropped,
            "codes": [p["code"] for p in plan],
            "filled": res.get("filled"),
            "mismatch": res.get("mismatch"),
            "bad": res.get("bad"),
            "sample": res.get("sample")}
