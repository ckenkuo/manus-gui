"""店小秘发布操作：variant_colors。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import json
import os
import re
from app.logger import logger
from app.publish import variant_dom
from app.publish.browser import BrowserSession, J


# ---- 配件色（变种表里缺源数据的颜色）识别 -----------------------------------
# 【判据落在变种信息表，不落在源 skus】2026-09-01 真站取证（草稿
# 184807703138719835，源 offer 890185900190「魔法精灵蓬蓬裙」）：1688 商家把
# 【主商品 + 配件】放在同一个 offer 的颜色维里卖——主色 5 个码，配件「紫精灵头纱」
# 只有 M 一个码（那个 M 是凑数占位，头纱本身没尺码）。
#
# 源颜色名与页面颜色维【永远对不上】：页面是平台色板（113 项 白色/米白色/…/红色/
# 桔红色/…），认领时把「魔法精灵蓬蓬裙」映射成「白色」、「紫精灵头纱」映射成
# 「红色」。故一切按源颜色名去找复选框的做法都必然找不到目标。
#
# 平台按 颜色×尺码 展开【完整笛卡尔积】（2×5=10 行），源只有 6 个真实 SKU，于是配件色
# 多出来的 4 行没有任何源数据。实测那 10 行的货号/申报价/重量：
#   白色 XS/S/M/L/XL → 魔法精灵蓬蓬裙-XS…-XL，价 26.03，重 120   （5 行齐全）
#   红色 M           → 紫精灵头纱-M，价 14.02，重 120            （仅此 1 行）
#   红色 XS/S/L/XL   → 三列全空，平台报「申报价格不能为空」「请填写重量」
# 这也是 ⑦b 那 4 行预览图替换失败的真因：它们压根不对应任何源 SKU。
#
# 故判据取【页面实况】：某颜色在变种表里只有 1 行有源数据、而别的颜色多行齐全 →
# 它是配件色，反选掉，不发它的 SKC/SKU。
_JS_VARIANT_ROW_FILL = r"""(() => {
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const t0 = sku.querySelector('table');
  if (!t0) return JSON.stringify({err: 'no-table'});
  const heads = Array.from(t0.querySelectorAll('thead th')).map(th => txt(th));
  // 变种维列按【结构位置】找（预览图之后、SKU货号之前），不按名字穷举：车贴类目那维
  // 叫「型号」、3C 类目见过「存储容量」，按名字认每换一个类目就得再补一次判据。
  // 判据与 ⑩a 共用 variant_dom._JS_DIM_COLS，见那里的取证。
  __DIM_COLS__
  const {colorIdx: iColor, sizeIdx: iSize} = dimIdx(heads);
  const iCode = heads.findIndex(h => h.includes('SKU货号'));
  const iPrice = heads.findIndex(h => h.includes('申报价格'));
  const iWeight = heads.findIndex(h => h.includes('重量'));
  if (iColor < 0) return JSON.stringify({err: 'no-dim-column', heads: heads});
  const val = (tds, k) => {
    if (k < 0 || !tds[k]) return '';
    const ins = Array.from(tds[k].querySelectorAll('input, textarea'));
    for (const x of ins) { const v = (x.value || '').trim(); if (v) return v; }
    return '';
  };
  const rows = Array.from(t0.querySelectorAll('tbody tr')).map((tr, i) => {
    const tds = Array.from(tr.children);
    const code = val(tds, iCode), price = val(tds, iPrice), weight = val(tds, iWeight);
    return {i: i, color: txt(tds[iColor] || null),
            size: iSize >= 0 ? txt(tds[iSize] || null) : '',
            code: code, price: price, weight: weight,
            // 有源数据 = 货号/申报价/重量任一有值；三个都空才是「凭空多出来的行」
            filled: !!(code || price || weight)};
  });
  const byColor = {};
  rows.forEach(r => {
    if (!byColor[r.color]) byColor[r.color] = {total: 0, filled: 0, filledSizes: []};
    const b = byColor[r.color];
    b.total += 1;
    if (r.filled) { b.filled += 1; b.filledSizes.push(r.size); }
  });
  return JSON.stringify({heads: heads, rows: rows, byColor: byColor});
})()"""


def accessory_colors_from_rows(by_color: dict) -> list:
    """从变种表的按色统计里挑出配件色：只有 1 行有源数据、而别的颜色多行齐全。

    by_color 形如 {"白色": {"total": 5, "filled": 5}, "红色": {"total": 5, "filled": 1}}。

    【判据刻意收得很窄】只认「别的颜色都是多行齐全、只有它是单行」这一种形态：
      - 各色 filled 全等 → 正常商品（含各色都只有 1 行的无尺码维商品），一个都不剔
      - 某色 filled == 0 → 不剔。整列空的成因是上游还没填（价格/重量阶段没跑），
        不是配件色；据此反选会误伤正常颜色。
    2026-09-01 核对已落盘 52 个商品（28 个多颜色）：27 个各色齐全，仅本单是 (5, 1)。
    """
    if not isinstance(by_color, dict) or len(by_color) < 2:
        return []
    filled = {c: (v or {}).get("filled", 0) for c, v in by_color.items()}
    if len(set(filled.values())) == 1:
        return []
    if max(filled.values()) <= 1:
        return []
    return [c for c, n in filled.items() if n == 1]


def _raw_sku_count(info_path: str) -> int:
    """读同目录 raw.json 的 skuMap 条数，供 fix_sizes 的报错指认真因。best-effort。"""
    try:
        raw_path = os.path.join(os.path.dirname(os.path.abspath(info_path)), "raw.json")
        with open(raw_path, encoding="utf-8") as f:
            return len(json.load(f).get("skuMap") or [])
    except Exception as e:
        logger.warning(f"读 raw.json 判 SKU 条数失败（不影响报错主体）：{e}")
        return 0


async def wait_variant_table_stable(session: BrowserSession,
                                    rounds: int = 20) -> int:
    """等变种表重建稳定（行数连续两轮不变），返回最后读到的行数。

    反选任何一个变种维选项都会让平台重建整张变种表，重建期间读到的行序是中间态。
    ⑦a 剔配件色与 ⑦b「补不上图就反选该规格」都要等这一下，故抽出来共用——原先
    ⑦a 内嵌一份，⑦b 再抄一遍就是两份会各自漂移的等待逻辑。判据与 ⑧ 一致。
    """
    last_n, stable, n = -1, 0, 0
    for _ in range(rounds):
        n = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
        if n == last_n:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_n = n
        await asyncio.sleep(0.6)
    return n


async def uncheck_variant_option(session: BrowserSession, name: str,
                                 why: str = "") -> dict:
    """反选一个变种维选项（颜色/型号/尺码…任一维），返回 {"status", "reason"}。

    status 取值：
      ok        —— 点了且已变成未勾选
      skipped   —— 本来就没勾选（无事可做，不是失败）
      error     —— 各维度组里都找不到它，或点了仍是勾选态

    【只反选，不勾选】见 _JS_UNCHECK_COLOR 上方说明。维度组按「颜色优先、其余变种维
    兜底」找，故车贴那种唯一维叫「型号」的类目也能反选。
    why 只进日志，用来交代这次为什么要剔（配件色 / 伪选项 / 补不上预览图）。
    """
    r = await session.eval_json(
        variant_dom._JS_UNCHECK_COLOR.replace("__WANT__", J(name)))
    tail = f"（{why}）" if why else ""
    if not isinstance(r, dict) or not r.get("found"):
        detail = r if isinstance(r, dict) else {}
        logger.warning(f"规格「{name}」在变种维复选框里找不到{tail}，未反选"
                       f"（维度组 {detail.get('groups')}，"
                       f"已勾选项 {detail.get('checkedOptions')}）")
        return {"status": "error",
                "reason": f"「{name}」在变种维复选框里找不到",
                "detail": detail}
    if not r.get("wasChecked"):
        logger.info(f"规格「{name}」本来就没勾选{tail}，无需反选")
        return {"status": "skipped", "reason": f"「{name}」本来就未勾选"}
    if r.get("err") == "last-checked-in-group":
        # 整维只剩它一个：反选会让平台清掉整张变种表（见 _JS_UNCHECK_COLOR 的说明），
        # 宁可留着这一个不合格规格交人工，也不能把整单的变种表搞没
        logger.warning(f"规格「{name}」是「{r.get('group')}」维里最后一个已勾选项"
                       f"{tail}，不反选（变种维不能为空，清空会毁掉整张变种表）")
        return {"status": "error",
                "reason": f"「{name}」是{r.get('group') or '该维'}里最后一个已勾选项，"
                          f"反选会清掉整张变种表，需人工处理"}
    if r.get("checked"):
        logger.warning(f"规格「{name}」点了反选但仍是勾选态{tail}")
        return {"status": "error", "reason": f"「{name}」点了反选但仍是勾选态"}
    logger.info(f"已反选规格「{name}」（{r.get('group') or '变种维'} 组）{tail}")
    return {"status": "ok", "group": r.get("group")}


async def drop_accessory_colors(session: BrowserSession,
                                max_rounds: int = 6) -> dict:
    """阶段⑦a：把配件色（变种表里只有单行有源数据的颜色）从颜色维里反选掉。

    【为什么排在 ⑦ 之前】反选让平台重建变种表，该色的 SKC 图位与 SKU 行一起消失，
    于是 ⑦/⑦b 天然不会碰它——不必在那两个阶段各自判「这个颜色跳不跳」。

    【判据读页面，不读源 skus】源颜色名（「紫精灵头纱」）与页面颜色维（平台色板的
    「红色」）永远对不上，只能按变种表里「哪些行有源数据」来认。取证见
    _JS_VARIANT_ROW_FILL 上方。故本函数不需要 info_path。

    【只反选，不勾选】职责是剔除，不是对齐颜色维；源里有而页面没勾的颜色一律不管
    （那是认领阶段的事）。
    """
    st = await session.eval_json(
        _JS_VARIANT_ROW_FILL.replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    if st.get("err"):
        # 与 ⑧ 同一取向：读不到变种表时不硬判失败，交由后续阶段暴露真问题
        return {"status": "skipped",
                "reason": f"读不到变种信息表（{st.get('err')}），本阶段跳过"}
    by_color = st.get("byColor") or {}
    targets = accessory_colors_from_rows(by_color)
    stats = {c: {"total": v.get("total"), "filled": v.get("filled"),
                 "filledSizes": v.get("filledSizes")} for c, v in by_color.items()}
    if not targets:
        return {"status": "skipped", "byColor": stats,
                "reason": "变种表里各颜色的有效行数一致，没有配件色需要剔除"}

    dropped, missed = [], []
    for name in targets:
        stat = by_color.get(name) or {}
        why = (f"变种表 {stat.get('total')} 行里只有 {stat.get('filled')} 行有源数据"
               f"，尺码 {stat.get('filledSizes')}，不发它的 SKC/SKU")
        r = await uncheck_variant_option(session, name, why=f"配件色：{why}")
        if r.get("status") == "ok":
            dropped.append(name)
        elif r.get("status") == "error":
            missed.append(name)
        # skipped（本来没勾）既不算剔掉也不算失败，与原先行为一致
        # 原固定 sleep(1.2) 已去掉：下方已有变种表稳定等待（行数连续两轮不变）

    if not dropped:
        return {"status": "error" if missed else "skipped",
                "targets": targets, "missed": missed, "byColor": stats,
                "reason": (f"配件色未能反选：{missed}" if missed
                           else "配件色本来就未勾选")}

    # 反选会触发变种表重建，等它稳定（与 ⑧ 同一等待机制）
    await wait_variant_table_stable(session)

    after = await session.eval_json(
        _JS_VARIANT_ROW_FILL.replace("__DIM_COLS__", variant_dom._JS_DIM_COLS))
    left = set((after.get("byColor") or {}).keys())
    still = [c for c in dropped if c in left]
    if still:
        return {"status": "error", "targets": targets, "dropped": dropped,
                "byColor": stats,
                "reason": f"反选后变种表里仍有这些颜色的行：{still}"}
    return {"status": "ok", "targets": targets, "dropped": dropped,
            "missed": missed, "byColor": stats,
            "rowCount": len(after.get("rows") or [])}


def _norm_color_name(s: str) -> str:
    """颜色名归一（比对用）：剥空白/连字符/标点、转小写。

    源颜色「白色-good动物城」与页面颜色「白色-good动物城replay」只差平台后缀
    replay，名字本体一致；不做完整翻译、只按归一后前缀/子串匹配（见 _identify_fake_colors）。
    """
    return re.sub(r"[\s\-_/·、,，]+", "", str(s or "")).lower()


def _strip_replay(s: str) -> str:
    """剥掉页面颜色名尾部的 replay 后缀（店小秘认领加标），返回比对用基名。"""
    t = str(s or "").strip()
    return t[:-6] if t.lower().endswith("replay") else t


async def _identify_fake_colors(session: BrowserSession, color_states: list,
                                info_path: str) -> list:
    """颜色维伪选项视觉识别：每个颜色喂「源名+代表图」给视觉模型判是不是真款式。

    【为什么要带图】2026-09-07 商品 1052702353345 的「短袖款式随机」，缩略图对应主图
    main-16.jpg 是一件印着「店内库存 随机款式 随机码数 介意勿拍」的衣服——名字再起得
    正常（比如叫「白色-热卖款」），文字图也藏不住。纯文本判只能抓「名字本身就暴露」的，
    抓不到「名不符图」的。

    【输入用源颜色键，不用页面名】colorImages / colors 用的是源颜色名（「短袖款式随机」），
    页面复选框是加了 replay 后缀的自定义项（「短袖款式随机replay」），两者靠
    _norm_color_name + 前缀匹配对上。代表图取 colorImages[c].mainFile 指向的本地主图
    （阶段①已落盘），没有 mainFile 时退到裸 URL（ask_json_with_images 会下载）。

    【视觉兜底文本】图全取不到（无色板/本地图缺失）时退回纯文本判，与尺码维同一提示词。
    返回源颜色名列表（调用方再映射回页面名去反选）。
    """
    from app.publish.llm import ask_json, ask_json_with_images

    # 页面上勾选着哪些颜色（伪选项只有已勾选才需要剔；未勾的不用管）
    page_checked = [str(s.get("t") or "").strip() for s in (color_states or [])
                    if s.get("c") and str(s.get("t") or "").strip()]
    if not page_checked:
        return []

    # 源颜色 → 代表图（mainFile 本地路径优先，裸 URL 兜底）
    info = {}
    if info_path:
        try:
            with open(info_path, encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:
            logger.warning(f"读 product-info.json 失败，颜色伪识别退纯文本：{e}")
    workdir = os.path.dirname(os.path.abspath(info_path)) if info_path else ""
    src_colors = [c for c in (info.get("colors") or []) if c]
    color_images = info.get("colorImages") or {}

    def _rep_image(c: str) -> str:
        entry = color_images.get(c)
        if isinstance(entry, dict):
            mf = (entry.get("mainFile") or "").strip()
            if mf:
                fp = os.path.join(workdir, mf)
                if os.path.isfile(fp):
                    return fp
            u = (entry.get("url") or "").strip()
            if u:
                return u
        elif isinstance(entry, str) and entry.strip():
            return entry.strip()
        return ""

    # 只把「页面上勾着的颜色」里能对上源颜色的喂给模型（伪颜色必在已勾选的里）。
    # 映射：页面名剥 replay → 归一，与源颜色归一后做前缀/全等匹配。
    pairs = []   # (源颜色名, 页面名, 代表图)
    for src in src_colors:
        ns = _norm_color_name(src)
        page = next((p for p in page_checked
                     if _norm_color_name(_strip_replay(p)) == ns
                     or _norm_color_name(p).startswith(ns)
                     or ns.startswith(_norm_color_name(_strip_replay(p)))), None)
        if page is not None:
            pairs.append((src, page, _rep_image(src)))
    # 页面上勾着但源 colors 里对不上的（认领带进来的自定义项）：用页面名本身当名字也喂进去，
    # 伪选项「短袖款式随机replay」若在源 colors 缺席，这条兜底让它也能被判。
    paired_pages = {p for _, p, _ in pairs}
    for p in page_checked:
        if p not in paired_pages:
            base = _strip_replay(p)
            pairs.append((base, p, _rep_image(base)))

    if not pairs:
        return []

    rule = (
        "【伪颜色】不是某个真实可售的具体款式，而是商家挂的占位项。典型：\n"
        "- 图上或名字是「随机发货 / 款式随机 / 颜色随机 / 店内库存随机 / 介意勿拍」——不指定款式；\n"
        "- 图是一张「尺码表 / 尺寸参考 / 说明文字图」，不是商品本身。\n"
        "【不要误判】图是真实商品、名字是某个具体款式/颜色（哪怕带 replay 后缀）就不算伪。"
        "宁可漏判，别把真款式当伪。"
    )
    names = [src for src, _, _ in pairs]
    imgs = [img for _, _, img in pairs]

    try:
        if any(imgs):
            # 全量图+文一次判：位次与颜色名一一对应。缺图的颜色不给图会错位，
            # 故只保留「有图」的配对进视觉请求；没图的配对落入下方文本兜底合并。
            with_img = [(s, p, i) for s, p, i in pairs if i]
            if with_img:
                vn = [s for s, _, _ in with_img]
                vi = [i for _, _, i in with_img]
                listing = "\n".join(f"第{k+1}张 = 「{n}」" for k, n in enumerate(vn))
                prompt = (
                    "你是电商发布审核助手。下面每个颜色配了它在源站的代表图，"
                    "判断哪些颜色是【伪颜色】（不是真实可售款式）。\n\n" + rule +
                    f"\n\n颜色与图的对应：\n{listing}\n\n"
                    "只输出JSON：{\"fake\": [\"<伪颜色名，与上面名字完全一致>\", ...]}，没有就 {\"fake\": []}"
                )
                data = await ask_json_with_images(
                    prompt, vi, what="伪颜色视觉识别", stage="fix_sizes")
                fake = [str(x).strip() for x in ((data or {}).get("fake") or [])]
                # 只认确实喂进去的名字
                return [f for f in fake if f in vn]
            return []
        # 一张图都没有：退回纯文本
        prompt = (
            "你是电商发布助手。下面是某商品的颜色选项名，判断哪些是【伪颜色】。\n\n" + rule +
            f"\n\n颜色选项：{json.dumps(names, ensure_ascii=False)}\n\n"
            "只输出JSON：{\"fake\": [\"<伪颜色名，与上面完全一致>\", ...]}，没有就 {\"fake\": []}"
        )
        data = await ask_json(prompt, what="伪颜色识别(文本)", stage="fix_sizes")
        fake = [str(x).strip() for x in ((data or {}).get("fake") or [])]
        return [f for f in fake if f in names]
    except Exception as e:
        logger.warning(f"伪颜色识别失败，按没有伪颜色放行：{e}")
        return []


async def _drop_fake_variants(session: BrowserSession,
                              size_states: list,
                              info_path: str = "") -> dict:
    """阶段⑧前置：把混进颜色/尺码维的伪变种选项反选掉。

    源商品会把不是可售规格的东西当成一个选项挂着卖，认领后混进变种维：
      - 尺码维：「2XL:尺寸参考选项图」（2026-09-07 商品 1071369483770）——商家把
        尺寸表图当成尺码选项，让买家下单时勾选查看。
      - 颜色维：「短袖款式随机」（2026-09-07 商品 1052702353345）——「随机发货」占位，
        不是某个具体图案。它每个尺码都有价（9.9，比真实图案的 18.9 便宜一半）、
        8 行全 filled，drop_acc 的「单行有源数据」判据抓不到。
    这类伪行留在表里会拖出无换图入口 / 无真实归属的行，⑦b 报「该行没有预览图 trigger」、
    ⑦a 因它不在另一维而反选不到。故在勾选对齐前先反选，让平台重建变种表把伪行抹掉。

    【判据交 LLM，不写死词表】2026-09-07 用户明确：硬编码词表容易误伤
    「XL（参考尺码表）」这类真实尺码、也盖不住「随机」这类没固定词形的占位。
    输入用页面【原始文本】——norm_size 会把「2XL:尺寸参考选项图」归一成「2XL」、
    丢掉伪特征；颜色文本同理不能归一。

    【两维判法不同】颜色维走【视觉】（_identify_fake_colors：喂每个颜色的源名+代表图，
    抓「名字正常但图是随机/尺码表文字图」的）；尺码维走【纯文本】（同码共用颜色图，
    按图判尺码会把正常尺码全误判，且「尺寸参考选项图」这类名字本身就带「参考图」字样，
    文本足够判出）。

    best-effort：识别失败 / 点了没生效都不拦整单，记日志即放（与 drop_acc 对
    missed 的取向一致）。返回 {"colors": [...], "sizes": [...]}（各自实际反选掉的原文）。
    """
    from app.publish.llm import ask_json

    def _texts(states):
        return [t for t in (str(s.get("t") or "").strip() for s in (states or [])) if t]

    # 尺码维直接用 fix_sizes 已读到的 states0（不重复 eval）；颜色维单独读一次。
    sizes = _texts(size_states)
    try:
        color_states = await session.eval_json(variant_dom._JS_COLOR_GROUP_STATES)
    except Exception as e:
        logger.warning(f"读颜色组勾选态失败，伪颜色不剔：{e}")
        color_states = []
    colors = _texts(color_states if isinstance(color_states, list) else [])
    if not sizes and not colors:
        return {"colors": [], "sizes": []}

    # 颜色维：视觉判伪，返回【源颜色名】，需映射回页面名（加 replay 后缀那个）去反选
    fake_color_srcs = []
    if colors:
        fake_color_srcs = await _identify_fake_colors(session, color_states, info_path)
    # 源名 → 页面名：剥 replay 归一后全等/前缀匹配
    def _to_page(src: str) -> str:
        ns = _norm_color_name(src)
        for p in colors:
            if (_norm_color_name(_strip_replay(p)) == ns
                    or _norm_color_name(p).startswith(ns)
                    or ns.startswith(_norm_color_name(_strip_replay(p)))):
                return p
        return ""
    fake_colors = []
    for s in fake_color_srcs:
        pg = _to_page(s)
        if pg:
            fake_colors.append(pg)
        else:
            logger.warning(f"伪颜色「{s}」在页面颜色组里找不到对应项，未反选")

    # 尺码维：纯文本判伪
    fake_sizes = []
    if sizes:
        sprompt = "\n".join([
            "你是电商发布助手。下面是某商品在 Temu 后台「尺码」维的全部选项。",
            "判断哪些是【伪尺码】——不是真实可售的尺码，而是商家挂的占位项，",
            "典型是「尺寸参考图 / 尺码表图 / 参考选项图 / size chart / 尺寸表」这类"
            "让买家查看的图片或说明，名称里常带「图 / 参考 / 表 / chart」。",
            "注意别把真实尺码误判成伪尺码：含「参考」的正常尺码"
            "（如「XL(参考尺码表)」「均码 参考身高」）只要本身是个可售尺码就【不算】。",
            "",
            f"尺码选项：{json.dumps(sizes, ensure_ascii=False)}",
            "",
            "只输出JSON：{\"fake\": [\"<与上面某一项完全一致的原文>\", ...]}，没有就 {\"fake\": []}",
        ])
        try:
            sdata = await ask_json(sprompt, what="伪尺码识别", stage="fix_sizes")
            fake_sizes = [x for x in (str(t).strip() for t in ((sdata or {}).get("fake") or []))
                          if x in sizes]
        except Exception as e:
            logger.warning(f"伪尺码识别失败，按没有伪尺码放行：{e}")

    checked = {str(s.get("t") or "").strip(): bool(s.get("c")) for s in (size_states or [])}
    if isinstance(color_states, list):
        checked.update({str(s.get("t") or "").strip(): bool(s.get("c")) for s in color_states})

    dropped_colors, dropped_sizes = [], []

    async def _wait_rebuild():
        n_before = (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0)
        for _ in range(12):  # 1.2s / 0.1s
            await asyncio.sleep(0.1)
            if (await session.eval_json(variant_dom._JS_SKU_ROW_COUNT)).get("n", 0) != n_before:
                break

    for name in fake_colors:
        if not checked.get(name):
            logger.info(f"伪颜色「{name}」本来就未勾选，无需反选")
            continue
        # 反选走共用件（颜色组优先、其余变种维兜底），失败只记警告放行——不拦整单
        r = await uncheck_variant_option(
            session, name, why="伪颜色：随机发货之类占位，不发它的 SKU")
        if r.get("status") != "ok":
            continue
        dropped_colors.append(name)
        await _wait_rebuild()

    for name in fake_sizes:
        if not checked.get(name):
            logger.info(f"伪尺码「{name}」本来就未勾选，无需反选")
            continue
        r = await session.eval_json(variant_dom._JS_CLICK_SIZE_CB.replace("__T__", J(name)))
        if not r.get("clicked"):
            logger.warning(f"伪尺码「{name}」在尺码复选框里点不到，未反选")
            continue
        dropped_sizes.append(name)
        logger.info(f"已反选伪尺码「{name}」（尺寸参考图之类占位，不发它的 SKU）")
        await _wait_rebuild()

    return {"colors": dropped_colors, "sizes": dropped_sizes}
