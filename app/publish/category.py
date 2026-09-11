"""店小秘发布操作：category。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import re
from app.logger import logger
from app.publish import cache, category_api, common, navigation, size_rules, workflows
from app.publish.browser import BrowserSession, J
from typing import Optional


# ---- 阶段③ 产品类目（写入）---------------------------------------------------
# 【候选从接口取，选中走 UI】逐级候选取自 POST /api/popTemuCategory/list.json
# （见 category_api），叶子由它的 isLeaf 定夺；选中仍要逐列点弹窗，因为最终得把这条
# 路径落进表单。
#
# 【历史：为什么曾经不走 API，现在又走回来了】原 skill 先调 list.json 拉树、再去弹窗里
# 点，随之而来的 item-not-found 被归因为「API 树与 UI 树可能不一致」。2026-08-19 实测
# 发现真正成因是【列定位错了】：
#   - 弹窗打开时若草稿已有类目，会【回显完整路径】——6 级类目就是 6 列全开，
#     每列有个带 active class 的已选项；
#   - 原脚本每级都点「最后一列」，于是拿第 1 级的名字去第 6 列里找，必然找不到。
# 当时还实测到「类目树整棵在弹窗 DOM 里、打开弹窗零网络请求」，且 shopId 页面上取不到
# （旧注释的结论），于是改成读 UI 列、按列索引点——树不一致与接口参数两个顾虑都绕开了。
# 2026-09-11 重新走回接口，是因为两条前提都变了：
#   - shopId 现在取得到：草稿的 product.shopId 就在 edit.json 里（category_api 现取）；
#   - 「读 UI 列」这条路会被页面状态拖累（同类问题在阶段④ 已实测：反复开合下拉会让
#     页面的下拉面板退化），而接口给的是与 UI 逐项一致的稳定数据。
# 实测对照：一级候选接口 25 个与弹窗第 1 列 25 个，名字与顺序完全相同。
# 真正修掉 item-not-found 的是「按列索引点」（列索引恒为该级序号），那一条原样保留。
#
# 联动实测（2026-08-19）：点第 N 列的项 → 右侧所有列销毁、只新建下一级列。
# 故点第 N 级（0 起）时目标列索引恒为 N，点完列数变成 N+2。

_JS_OPEN_CAT_MODAL = r"""(() => {
  const sec = document.getElementById('productBasicInfo');
  if (!sec) return JSON.stringify({opened: false, reason: 'section-not-found'});
  const btn = Array.from(sec.querySelectorAll('button, a'))
    .find(b => (b.textContent||'').trim() === '选择分类');
  if (!btn) return JSON.stringify({opened: false, reason: 'button-not-found'});
  btn.click();
  return JSON.stringify({opened: true});
})()"""


# 读弹窗所有列：每列的选项名、以及带 active class 的已选项（用于回读校验）
_JS_CAT_COLUMNS = r"""(() => {
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => (m.textContent||'').includes('选择类目') && m.offsetHeight > 0)[0];
  if (!modal) return JSON.stringify({ready: false});
  const cols = Array.from(modal.querySelectorAll('.categories-box'))
    .filter(b => b.offsetHeight > 0);
  return JSON.stringify({
    ready: true,
    n: cols.length,
    cols: cols.map(c => ({
      items: Array.from(c.querySelectorAll('.categories-item-name'))
        .map(e => (e.textContent||'').trim()).filter(Boolean),
      active: (() => {
        const a = Array.from(c.querySelectorAll('.categories-item'))
          .find(el => /active|selected|current|checked/i.test(el.className));
        return a ? ((a.querySelector('.categories-item-name')||{}).textContent||'').trim() : null;
      })(),
    })),
  });
})()"""


async def _cat_columns(session: BrowserSession) -> dict:
    """读选择类目弹窗的列状态。"""
    return await session.eval_json(_JS_CAT_COLUMNS)


async def _click_cat_in_column(session: BrowserSession, col_idx: int, name: str) -> dict:
    """在【指定列索引】里点类目项（不是总点最后一列，见上方注释）。

    找不到时把该列所有选项一起返回，供调用方交给 LLM 重选——这比原脚本从异常
    字符串里正则抠选项列表可靠得多。
    """
    js = r"""(() => {
      const modal = Array.from(document.querySelectorAll('.ant-modal'))
        .filter(m => (m.textContent||'').includes('选择类目') && m.offsetHeight > 0)[0];
      if (!modal) return JSON.stringify({clicked: false, reason: 'no-modal'});
      const boxes = Array.from(modal.querySelectorAll('.categories-box'))
        .filter(b => b.offsetHeight > 0);
      const col = boxes[__IDX__];
      if (!col) return JSON.stringify({clicked: false, reason: 'no-such-column',
        columns: boxes.length});
      const item = Array.from(col.querySelectorAll('.categories-item'))
        .find(el => ((el.querySelector('.categories-item-name')||{}).textContent||'').trim() === __NAME__);
      if (!item) return JSON.stringify({clicked: false, reason: 'item-not-found',
        options: Array.from(col.querySelectorAll('.categories-item-name'))
          .map(s => (s.textContent||'').trim()).filter(Boolean)});
      item.click();
      return JSON.stringify({clicked: true});
    })()""".replace("__IDX__", str(col_idx)).replace("__NAME__", J(name))
    return await session.eval_json(js)


async def _confirm_cat(session: BrowserSession) -> dict:
    """点弹窗底部「选择」确认（footer 只有「选择」「关闭」两个按钮，实测）。"""
    return await session.eval_json(r"""(() => {
      const modal = Array.from(document.querySelectorAll('.ant-modal'))
        .filter(m => (m.textContent||'').includes('选择类目') && m.offsetHeight > 0)[0];
      if (!modal) return JSON.stringify({confirmed: false, reason: 'no-modal'});
      const btn = Array.from(modal.querySelectorAll('.ant-modal-footer button'))
        .find(b => (b.textContent||'').trim() === '选择');
      if (!btn) return JSON.stringify({confirmed: false, reason: 'no-confirm-button'});
      btn.click();
      return JSON.stringify({confirmed: true});
    })()""")


async def read_current_category(session: BrowserSession) -> Optional[str]:
    """读基本信息区当前显示的产品分类路径（只读，用于回读校验）。"""
    r = await session.eval_json(r"""(() => {
      const sec = document.getElementById('productBasicInfo');
      if (!sec) return JSON.stringify({snippet: null});
      const t = sec.textContent || '';
      const i = t.indexOf('产品分类');
      return JSON.stringify({snippet: i >= 0
        ? t.slice(i, i + 300).replace(/\s+/g, ' ') : null});
    })()""")
    return r.get("snippet")


def cat_clues(info: Optional[dict]) -> str:
    """从 product-info.json 内容里凑出一段年龄/尺码线索，供类目提示词用。

    拿不到任何线索就返回空串（调用方据此整段省掉，不给模型留空占位诱导它编）。
    best-effort：info 结构不符预期一律当没线索，绝不让类目阶段因此失败。
    """
    if not isinstance(info, dict):
        return ""
    bits = []
    try:
        attrs = info.get("attributes") or {}
        rules = workflows.rules_for(info)
        age_keys = rules.AGE_ATTR_KEYS if rules else size_rules._AGE_ATTR_KEYS
        for k in age_keys:
            v = str((attrs.get(k) or "")).strip()
            if not v:
                continue
            # 1688 的「适合身高」常把后面几个属性黏进同一个值里（实测 1055568943470：
            # `6-9m,...,2-3y主要下游平台ebay,亚马逊...主要销售地区非洲,欧洲...`）。
            # extract 那边按已知键名切不开这种连写，这里在黏连处再切一刀，免得噪音
            # 把年龄信号淹掉（单纯按长度硬截会把 ebay/亚马逊 留在线索里）。
            v = (rules.clean_age_value(v) if rules else
                 re.split(r"主要下游平台|主要销售地区|下游平台|销售地区", v)[0].strip(" ,，"))
            if v:
                bits.append(f"{k}: {v[:40]}")
        sizes = info.get("sizes") or list((info.get("skus") or {}).keys())
        if sizes:
            shown = "、".join(str(s) for s in list(sizes)[:8])
            tier = size_rules.size_tier(sizes)
            if tier == "baby" and size_rules._has_older_than_one_year(sizes):
                # 岁码 ≥2 岁是童装不是婴儿：明确写「非婴儿」，免得 LLM 把「婴幼童」
                # 读成「婴儿服饰」这个类目分支（2026-09-04 5~8 岁被分进婴儿类目）。
                note = "（岁码 2 岁及以上 = 童装，非婴儿，也不可能是成人商品）"
            elif tier == "adult" and size_rules._age_is_child(info.get("attributes")):
                # 字母码 + 年龄段明确童装：童装商家用字母码表示童装码，不是成人商品
                # （见 _age_is_child docstring）。不软化这里，size_tier 的「成人码」结论
                # 会连同提示词规则 2 一起把商品往成人分支带偏。
                note = "（字母码，但适合年龄段明确是童装，按童装处理，不得选成人分支）"
            else:
                note = {"baby": "（月龄码/岁码/身高码 = 婴幼童，不可能是成人商品）",
                        "adult": "（成人字母码）"}.get(tier, "")
            bits.append(f"源商品尺码: {shown}{note}")
    except Exception as e:
        logger.warning(f"提取类目年龄线索失败（不影响类目判断）：{e}")
        return ""
    return "；".join(bits)


# 类目选择提示词：前四条规则是原 skill 踩出来的（套装优先、性别年龄段必须一致、
# "其他"类目只在全都不符时才选），第 5 条是本项目 2026-08-19 实测补的——
# 只看本级名字会挑"名字最像"的那个而错过更贴切的分支，故候选都带上下级子类目
# （子类目改由 category_api 一次并发取回，不再逐个点弹窗探测）。
_CAT_PROMPT = """你是跨境电商类目分类助手。根据商品标题，从候选类目列表中选出语义上最合适的一个。
每个候选后面的「└ 子类目:」列出了它下一级包含什么，用来判断该分支往下走能否到达更贴切的叶子类目。

规则：
1. 若商品标题表明是套装类商品（含"套装""两件套""三件套""套裙"等），候选中有套装/两件套类类目时，必须优先选套装类类目，而不是按单品材质/上衣/裤子归类。
2. 性别、年龄段（婴儿/女童/男童/女士/男士）必须与标题一致；若下方给出了「商品线索」，以线索为准。【线索里的年龄段/尺码写法优先于标题措辞】：1688 标题常把"女"（指女款）与"宝宝/童"并列，只看"女"字会误判成成人女装。源尺码是月龄码（6-9m）、岁码（2-3y/3T）或身高码（90/110cm）时，商品必为婴幼童/童装，禁止选女士/男士等成人分支；源尺码是 S/M/L/XL 等成人字母码时，禁止选婴儿/女童/男童分支。源尺码里出现 2 岁及以上的岁码（2y/3T/5岁/7-8岁）时，商品必为童装（女童/男童），禁止选「婴儿」分支；只有月龄码（≤12m，如 6-9m/9-12m）或 1 岁码（1y/1岁/12m）才可能是婴儿。
3. "其他（...）"类目只有在所有其他候选都语义不符时才能选；只要存在更具体的候选（如按裙/长裤/短裤、开衫/套头衫区分），就必须选最具体匹配的那个，依据标题中的实体信息判断（如"牛仔裤"=长裤下装）。
4. 商品的风格属性（运动/休闲/正装/居家）要与类目分支一致：运动风商品（含"运动""卫衣""POLO""卫裤""速干"等）应优先进"运动服/休闲运动"这类分支。
5. 【重要】选择时必须综合本级名字和它的子类目：若某候选本身不如另一个贴切，但其子类目里有明显更匹配商品的项，应选它。反之，某候选名字看着匹配但子类目全都不符，则不该选。

商品标题：{title}
{clues}已选路径：{path}
候选类目：
{options}

只输出JSON: {{"index": <序号>, "reason": "<一句话理由，说明是依据本级名字还是子类目做的判断>"}}"""


async def _pick_category(title: str, options: list, path: list,
                         children: Optional[dict] = None,
                         clues: str = "") -> tuple:
    """让 LLM 从候选类目里选一个，返回 (index, reason)。

    children 给出每个候选的子类目清单（前瞻结果），会拼进提示词。
    子类目多时只取前 12 个——提示词太长反而稀释重点，12 个足够看出分支性质。

    clues 是 cat_clues() 产出的年龄段/尺码线索（见其上方的 2026-08-29 取证）。
    为空时整行省掉，不给模型留空占位——空占位会诱导模型自己编线索。
    """
    from app.publish.llm import ask_json

    lines = []
    for i, name in enumerate(options):
        lines.append(f"{i}. {name}")
        kids = (children or {}).get(name) or []
        if kids:
            shown = "、".join(kids[:12])
            more = f"（另有 {len(kids) - 12} 项）" if len(kids) > 12 else ""
            lines.append(f"   └ 子类目: {shown}{more}")
        elif children is not None:
            lines.append("   └ 子类目: （无，是叶子类目）")
    data = await ask_json(
        _CAT_PROMPT.format(
            title=title,
            clues=(f"商品线索：{clues}\n" if clues else ""),
            path=" > ".join(path) or "（根级）",
            options="\n".join(lines),
        ),
        what="类目选择", stage="auto_cat",
    )
    idx = data.get("index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(options):
        raise RuntimeError(f"类目选择：LLM 返回非法 index={idx}（候选 {len(options)} 个）")
    return idx, str(data.get("reason", ""))


# ---- 阶段③ 类目缓存快路径 ----------------------------------------------------
# 慢路径（下方 auto_cat 的逐级遍历）实测 110.1s：5 级 = 5 次 LLM + 每级前瞻逐个点
# 候选。而按【已知路径】逐列直点、不前瞻不调 LLM，走完同样 5 级只花 7.6s
# （2026-08-22 真站实测）。用过的类目就是恒定的那么一些，故值得先试缓存。
#
# 【这条路径的风险与慢路径不对称，务必读懂再改】慢路径的每一步都被 DOM 约束着：
# 选出的名字必须能在那一列点中，点不中就抛带诊断的 RuntimeError；走错分支会在后续
# 层级暴露成明显不合理的候选；前瞻更是专门为「只看本级名字会选错分支」加的。
# 快路径把这一切换成【一次对完整路径的判断】，而一旦它选了条「像但不对」的兄弟路径
# （女童针织套头衫 vs 女童针织开衫），下游全部校验都会通过——路径真实存在所以 5 列
# 都点得中、确认成功、回读能找到叶子名、属性行照常渲染。错误一路带到人工看列表才
# 可能被发现。所以：
#   - 提示词把「代价不对称」明确讲给模型（规则 6），宁可答不匹配也不硬选；
#   - 只在回读校验真见到叶子名时才 remember_category，不让可疑路径污染缓存；
#   - 选中的路径与理由走 logger.info（经 _install_log_bridge 直达 UI 日志），人看得见。

_CAT_FROM_CACHE_PROMPT = """你是跨境电商类目分类助手。下面是本店历史上真实用过的类目路径清单（每条都是从根到叶子的完整路径）。判断当前商品是否属于其中某一条路径的叶子类目。

规则：
1. 若商品标题表明是套装类商品（含"套装""两件套""三件套""套裙"等），清单中有套装/两件套类叶子类目时，必须优先选它，而不是按单品材质/上衣/裤子归类。
2. 性别、年龄段（婴儿/女童/男童/女士/男士）必须与标题一致；若下方给出了「商品线索」，以线索为准。路径中任何一级的性别或年龄段与商品不符，这条路径就不能选。【线索里的年龄段/尺码写法优先于标题措辞】：1688 标题常把"女"（指女款）与"宝宝/童"并列，只看"女"字会误判成成人女装。源尺码是月龄码（6-9m）、岁码（2-3y/3T）或身高码（90/110cm）时商品必为婴幼童/童装，成人分支的路径一概不能选；反之成人字母码（S/M/L/XL）不能选婴儿/童装分支。源尺码里出现 2 岁及以上的岁码（2y/3T/5岁/7-8岁）时，商品必为童装（女童/男童），禁止选「婴儿」分支；只有月龄码（≤12m，如 6-9m/9-12m）或 1 岁码（1y/1岁/12m）才可能是婴儿。
3. "其他（...）"类叶子类目只有在清单里所有更具体的候选都语义不符时才能选。
4. 商品的风格属性（运动/休闲/正装/居家）要与路径分支一致：运动风商品（含"运动""卫衣""POLO""卫裤""速干"等）应走"运动服/休闲运动"这类分支。
5. 【重要】要逐级复核整条路径，不是只看叶子名。叶子名看着匹配但中间某一级（品类大类、性别、年龄段、风格分支）与商品不符的，不能选。
6. 【最重要】拿不准就选「以上都不匹配」。选错一条路径会让商品发布到错误类目、必须人工回滚；选「不匹配」只是让程序多花约两分钟重新走一遍类目树，代价小得多。只有当某条路径的每一级都与商品相符、且叶子类目就是这件商品该去的地方时才选它；仅仅是"大类相同"（都是女装、都是童装）远远不够。

商品标题：{title}
{clues}已知类目路径：
{paths}

只输出JSON: {{"index": <序号>, "reason": "<一句话理由：说明是依据哪一级判断的，或为什么都不匹配>"}}"""


def _format_cached_paths(known: list) -> str:
    """把已知路径清单排版进提示词：按第一级分组、序号全局连续、末尾加不匹配哨兵。

    分组只是排版（把共同前缀提出来省 token、让同分支的路径挨着好比较），序号仍全局
    唯一，解析逻辑不受影响。「曾用于」的历史标题是本提示词的主要判据来源——缓存
    分支下弹窗还没打开、没有子类目可给，慢路径靠前瞻拿到的那份信息在这里不存在，
    「这条路径以前用在这些商品上」正好补上这个缺口，比让模型凭类目名做语义猜测可靠。
    """
    n_titles = 1 if len(known) > 30 else 2   # 清单长了就少给样本，免得长度失控
    groups: dict = {}
    for i, p in enumerate(known):
        groups.setdefault(p["path"][0], []).append((i, p))
    lines = []
    for head, items in groups.items():
        lines.append(f"【{head}】")
        for i, p in items:
            lines.append(f"  {i}. " + " > ".join(p["path"]))
            titles = [t for t in (p.get("titles") or []) if t][:n_titles]
            if titles:
                lines.append("     └ 曾用于: " + " / ".join(titles))
    lines.append(f"  {len(known)}. 【以上都不匹配，需要重新走一遍类目树】")
    return "\n".join(lines)


async def _pick_cached_category(title: str, known: list,
                                clues: str = "") -> tuple:
    """让 LLM 从已知路径里选一条，返回 (path_or_None, reason)。

    与 _pick_category 的关键差异：这里【答不出来是合法答案】，非法/越界 index 一律
    返回 (None, reason) 让调用方落回全量遍历，不像 _pick_category 那样抛异常。
    因为两者代价不对称：选错一条路径要人工回滚，答不匹配只是多花约 110s。

    「都不匹配」做成清单里的最后一个选项（哨兵），而不是要求模型返回 -1 或额外字段：
    这样它表达「不匹配」用的是表达其他一切答案的同一个机制（给一个合法 index），
    合法答案空间里没有洞。解析侧同时接受哨兵、-1 与任何越界值，全当不匹配。
    """
    from app.publish.llm import ask_json

    data = await ask_json(
        _CAT_FROM_CACHE_PROMPT.format(
            title=title, clues=(f"商品线索：{clues}\n" if clues else ""),
            paths=_format_cached_paths(known)),
        what="类目选择（缓存路径）", stage="auto_cat",
    )
    idx = data.get("index")
    reason = str(data.get("reason", ""))
    if not isinstance(idx, int) or idx < 0 or idx >= len(known):
        return None, reason or f"未匹配已知路径（index={idx}）"
    return known[idx]["path"], reason


async def _click_cat_path(session: BrowserSession, path: list) -> dict:
    """按已知路径逐列直点，不前瞻、不调 LLM。

    某级 clicked=false 即【立刻返回，不再点后面的级】：类目树变了，继续点只会在错位
    的列里乱点，把弹窗状态搅得更脏。
    """
    for level, name in enumerate(path):
        cr = await _click_cat_in_column(session, level, name)
        if not cr.get("clicked"):
            return {"ok": False, "clickedLevels": level, "failedLevel": level,
                    "reason": cr.get("reason") or "", "options": cr.get("options") or []}
        # 等右侧列重建（原固定 sleep(1.5)，上限不变）。判据同慢路径：点第 N 级会
        # 销毁右侧所有列、只新建下一级，故 n > level+1 即新列已挂上。
        # 【最后一级不等】它是叶子，本就不会有新列，等满 1.5s 纯属白等——
        # 这一条让 5 级路径省掉最后那 1.5s。
        if level < len(path) - 1:
            await common._poll_until(
                lambda: _cat_columns(session),
                lambda d, lv=level: (d or {}).get("n", 0) > lv + 1, timeout=1.5)
    return {"ok": True, "clickedLevels": len(path), "failedLevel": None,
            "reason": "", "options": []}


async def _lookup_cat_ids(session: BrowserSession, rowid: str, path: list) -> list:
    """按类目路径名沿接口逐级反查 catId，给【没有 catIds 的老缓存条目】补一份。

    为什么需要：阶段④ 要用页面上当前生效的叶子类目 id 查属性选项，而缓存路径命中时
    阶段③ 并没有调接口拿候选（见 _try_cached_category）。catIds 从 2026-09-11 起才随
    路径一起记，此前攒下的几十条都没有这个键——不补的话，「改了类目又命中缓存」的
    商品会继续按草稿已保存的旧类目查选项，本次修的问题原样重现。

    走的是与逐级遍历同一套接口（候选与弹窗列实测同源，见 category_api），故反查结果
    可以直接当权威值。best-effort：取不到店铺 id、或某一级名字对不上（类目树变了）
    就返回 []，调用方退回原有行为，绝不让这条补齐路径把阶段搞挂。
    """
    shop_id = await category_api.fetch_shop_id(session, rowid)
    if not shop_id or not path:
        return []
    ids: list = []
    parent = ""
    for name in path:
        rows = await category_api.fetch_children(session, shop_id, parent)
        hit = next((r for r in rows if r["catName"] == name), None)
        if not hit:
            logger.warning(f"按路径反查类目 id 时第 {len(ids) + 1} 级「{name}」不在候选里，放弃")
            return []
        ids.append(hit["catId"])
        parent = hit["catId"]
    return ids


async def _try_cached_category(session: BrowserSession, title: str,
                               site: str = "",
                               clues: str = "", rowid: str = "") -> Optional[dict]:
    """缓存快路径：LLM 从已知路径里选一条 → 逐列直点 → 确认 → 回读校验。

    返回值与 auto_cat 的成功返回【同构】（status/path/pathList/leaf/levels/trace/
    catSnippet），多一个 source="cache"；任何一步不成立返回 None，由 auto_cat 落回
    原有的逐级遍历。

    落回时【不需要清理弹窗状态】：点第 N 列的项会销毁右侧所有列（见本文件类目区块
    开头的联动实测），而慢路径从 level=0 起，第一次 _click_cat_in_column(0, ...) 就会
    把列 0 右侧全部销毁重建，快路径留下的痕迹随之消失；列 0 的内容任何时候都是根级
    全量。故这里失败直接返回 None 即可。
    """
    known = cache.prompt_paths()
    if not known:
        return None
    try:
        path, reason = await _pick_cached_category(title, known, clues)
    except Exception as e:
        # 缓存是加速手段，它自己的 LLM 调用失败不该拖垮整个阶段：落回慢路径
        logger.warning(f"缓存类目选择失败，落回逐级遍历：{e}")
        return None
    if not path:
        logger.info(f"已知路径均不匹配（{reason}），走逐级遍历")
        return None

    logger.info(f"命中已知类目路径：{' > '.join(path)}（{reason}）")
    clicked = await _click_cat_path(session, path)
    if not clicked.get("ok"):
        logger.warning(
            f"缓存路径第{clicked['failedLevel'] + 1}级「{path[clicked['failedLevel']]}」"
            f"点不中（{clicked.get('reason')}），类目树可能已变，落回逐级遍历")
        return None

    cr = await _confirm_cat(session)
    if not cr.get("confirmed"):
        logger.warning(f"缓存路径确认类目失败（{cr}），落回逐级遍历")
        return None
    # 等回显更新（原固定 sleep(2)，上限不变）：确认后 Vue 更新 productBasicInfo 区
    # 的类目显示，判据就是叶子名出现在回显里
    leaf = path[-1]
    await common._poll_until(
        lambda: read_current_category(session),
        lambda snippet: snippet and leaf in snippet,
        timeout=2.0)
    snippet = await read_current_category(session)
    if not (snippet and leaf in snippet):
        logger.warning(f"缓存路径回读未见「{leaf}」，落回逐级遍历。实际：{(snippet or '')[:120]}")
        return None

    # 命中缓存路径时页面上的类目也是这条（刚逐列点完并回读确认过），叶子 catId 优先从
    # 缓存条目取——它由当初走逐级遍历跑通时记下。老条目没有这个键，就按路径名沿接口
    # 反查一次补上（见 _lookup_cat_ids），补到即回写缓存，下个同类商品就不必再查。
    ids = cache.cat_ids_for(path)
    if not ids and rowid:
        ids = await _lookup_cat_ids(session, rowid, path)
    cache.remember_category(path, title, ids)
    return {"status": "ok", "path": " > ".join(path), "pathList": list(path),
            "leaf": leaf, "levels": len(path), "source": "cache",
            "leafCatId": ids[-1] if ids else "",
            "trace": [{"level": 0, "picked": "（缓存路径）", "reason": reason,
                       "cachedPath": list(path)}],
            "catSnippet": snippet}


# ---- 阶段③ 草稿默认类目快路径 ------------------------------------------------
# 【为什么有这一条】编辑页草稿本来就带着类目：认领时店小秘按源商品推的，或上次发布
# 留下的。它命中的时候，慢路径那一整轮（逐级遍历 5~6 级、每级一次 LLM + 前瞻逐个点
# 候选，实测 110s）全是白跑。故先花一次 LLM 判「草稿里这条对不对」，对就一个 DOM
# 都不点、直接沿用，只把类目弹窗关掉。
#
# 【取向与缓存快路径同源】判错的代价同样是「发错类目、要人工回滚」，判「不对」只是
# 多花约两分钟重走一遍，故拿不准一律判不对（见 _CAT_DEFAULT_PROMPT 规则 6）。
#
# 【回读校验必须做在关弹窗之前】草稿里挂着一条类目【不等于】它生效：平台可以把类目
# 删掉（页面弹「该分类已在平台删除」），或分类行压根显示「未选择分类」。这些都要落回
# 原有逻辑，而落回时弹窗必须还开着——缓存快路径与逐级遍历都从当前弹窗状态起步
# （见 _try_cached_category 的落回说明），故校验一律排在关弹窗前。

_JS_CAT_ROW_STATE = r"""(() => {
  const sec = document.getElementById('productBasicInfo');
  if (!sec) return JSON.stringify({ok: false, why: 'no-section'});
  const row = Array.from(sec.querySelectorAll('.ant-form-item')).find(it => {
    const lab = it.querySelector('.ant-form-item-label');
    return lab && (lab.textContent || '').includes('产品分类');
  });
  const sel = row ? row.querySelector('.ant-select-selection-item') : null;
  return JSON.stringify({
    ok: true,
    catText: sel ? (sel.textContent || '').trim() : '',
    catListText: ((sec.querySelector('.category-list') || {}).textContent || '').trim(),
    catDeleted: Array.from(document.querySelectorAll('.d-message-error, .d-message'))
      .map(e => (e.textContent || '').trim()).some(t => t.includes('分类已在平台删除')),
  });
})()"""


_JS_CLOSE_CAT_MODAL = r"""(() => {
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(m => (m.textContent||'').includes('选择类目') && m.offsetHeight > 0)[0];
  if (!modal) return JSON.stringify({closed: true, reason: 'no-modal'});
  const btn = Array.from(modal.querySelectorAll('.ant-modal-footer button'))
    .find(b => (b.textContent||'').trim() === '关闭');
  if (!btn) return JSON.stringify({closed: false, reason: 'no-close-button'});
  btn.click();
  return JSON.stringify({closed: true});
})()"""


def _default_path(state: dict) -> list:
    """从弹窗列状态里拼出草稿当前的类目路径（各列的 active 项）。

    回显一条完整路径时每列都有一个 active 项（见本文件类目区块开头的实测），故判据
    取「每一列都有 active 且列数对得上」——中间某列没有 active 就说明这不是一条完整
    回显（草稿没有类目时只有第 0 列且无 active），整条作废。

    best-effort：列数据结构不符预期一律当「没有默认类目」，绝不让这条加速路径把
    阶段搞挂（假 session / 页面改版都从这里安全退出）。
    """
    cols = state.get("cols") or []
    n = state.get("n") or 0
    path = []
    for i in range(min(n, len(cols))):
        active = (cols[i] or {}).get("active")
        if not isinstance(active, str) or not active.strip():
            return []
        path.append(active.strip())
    return path if len(path) == n else []


# 默认类目判断提示词：与 _CAT_FROM_CACHE_PROMPT 同一套判据（套装优先、性别年龄段必须
# 一致、"其他"只在无更具体类目时才对、风格分支要一致、逐级复核而非只看叶子名），
# 差别只在候选从「一份清单」变成「草稿里这一条」，故第 6 条把「选错要人工回滚」
# 同样讲明白——保守取向是这条快路径唯一的安全阀。
_CAT_DEFAULT_PROMPT = """你是跨境电商类目分类助手。下面是编辑页草稿里【当前已有的】类目路径（从根到叶子）。判断这件商品是否就该放在这条路径的叶子类目下。

规则：
1. 若商品标题表明是套装类商品（含"套装""两件套""三件套""套裙"等），而这条路径是按单品材质/上衣/裤子归的类，则不正确。
2. 性别、年龄段（婴儿/女童/男童/女士/男士）必须与标题一致；若下方给出了「商品线索」，以线索为准。【线索里的年龄段/尺码写法优先于标题措辞】：1688 标题常把"女"（指女款）与"宝宝/童"并列，只看"女"字会误判成成人女装。源尺码是月龄码（6-9m）、岁码（2-3y/3T）或身高码（90/110cm）时商品必为婴幼童/童装，成人分支的路径一概不正确；反之成人字母码（S/M/L/XL）不能走婴儿/童装分支。源尺码里出现 2 岁及以上的岁码（2y/3T/5岁/7-8岁）时，商品必为童装（女童/男童）；只有月龄码（≤12m，如 6-9m/9-12m）或 1 岁码（1y/1岁/12m）才可能是婴儿。
3. "其他（...）"这类占位类目只有在商品确实没有更具体的类目可归时才正确。
4. 商品的风格属性（运动/休闲/正装/居家）要与路径分支一致：运动风商品（含"运动""卫衣""POLO""卫裤""速干"等）应走"运动服/休闲运动"这类分支。
5. 【重要】要逐级复核整条路径，不是只看叶子名。叶子名看着匹配但中间某一级（品类大类、性别、年龄段、风格分支）与商品不符的，就是不正确。
6. 【最重要】拿不准就判不正确。沿用错的路径会让商品发布到错误类目、必须人工回滚；判「不正确」只是让程序多花约两分钟重新走一遍类目树，代价小得多。只有当这条路径的每一级都与商品相符、且叶子类目就是这件商品该去的地方时才判正确；仅仅是"大类相同"（都是女装、都是童装）远远不够。

商品标题：{title}
{clues}草稿当前类目路径：{path}

只输出JSON: {{"match": true 或 false, "reason": "<一句话理由：说明是依据哪一级判断的>"}}"""


async def _confirm_default_category(title: str, path: list, clues: str = "") -> tuple:
    """让 LLM 判断草稿里这条类目路径对不对，返回 (match, reason)。

    只有明确答 true 才算命中：字符串 "true" 也认（模型偶尔给字符串），缺字段、
    答非所问一律当「不适用」落回原有逻辑——沿用错路径的代价不对称（见上方注释块）。
    """
    from app.publish.llm import ask_json

    data = await ask_json(
        _CAT_DEFAULT_PROMPT.format(
            title=title, clues=(f"商品线索：{clues}\n" if clues else ""),
            path=" > ".join(path)),
        what="默认类目判断", stage="auto_cat",
    )
    m = data.get("match")
    matched = m is True or (isinstance(m, str) and m.strip().lower() == "true")
    return matched, str(data.get("reason", ""))


async def _close_cat_modal(session: BrowserSession) -> None:
    """关掉选择类目弹窗（命中默认类目后的收尾）。

    不点「选择」确认：草稿里本来就是这条类目，不需要重新写入一遍。关不掉就退回页面级
    清理（kill_stuck_modals，与打开弹窗前那次是同一个手段）——残留遮罩会让后续每个
    阶段都点不中，比弹窗本身开着更糟。
    """
    r = await session.eval_json(_JS_CLOSE_CAT_MODAL)
    # 等它真消失（判据与别处一致：读不到可见弹窗即消失），免得后续阶段撞上正在关闭的遮罩
    gone = await common._poll_until(
        lambda: _cat_columns(session),
        lambda d: not (d or {}).get("ready"), timeout=2.0)
    if gone.get("ready"):
        logger.warning(f"类目弹窗关闭失败（{r.get('reason') or r}），强制清理残留遮罩")
        await session.kill_stuck_modals()


async def _try_default_category(session: BrowserSession, title: str,
                                clues: str = "") -> Optional[dict]:
    """默认类目快路径：读草稿类目 → 判可用性 → LLM 判对不对 → 关弹窗沿用。

    返回值与 auto_cat 的成功返回【同构】（status/path/pathList/leaf/levels/trace/
    catSnippet），多一个 source="default"；任何一步不成立返回 None，由 auto_cat
    接着走原有的缓存快路径、再不行走逐级遍历。

    落回时【弹窗保持打开】——两条原有路径都从当前弹窗状态起步，不必重开；只有真命中
    才关它。故这里所有 return None 的分支都不动弹窗。
    """
    cols = await _cat_columns(session)
    path = _default_path(cols)
    if not path:
        return None
    state = await session.eval_json(_JS_CAT_ROW_STATE)
    if not state.get("ok"):
        logger.warning(f"读分类行状态失败（{state.get('why') or state}），默认类目快路径跳过")
        return None
    if state.get("catDeleted"):
        logger.info("页面提示「该分类已在平台删除」，草稿默认类目不可用，走原有路径")
        return None
    if "未选择分类" in (state.get("catListText") or ""):
        logger.info("分类行显示「未选择分类」，草稿默认类目不可用，走原有路径")
        return None
    try:
        matched, reason = await _confirm_default_category(title, path, clues)
    except Exception as e:
        # 与缓存快路径同一取向：加速手段自己的 LLM 调用失败不该拖垮整个阶段
        logger.warning(f"默认类目判断失败，落回原有路径：{e}")
        return None
    if not matched:
        logger.info(f"草稿默认类目不适用（{reason}），走原有路径")
        return None
    # 【弹窗回显着一条路径，不等于分类行真认它】认领旧值残留、平台侧把类目清掉，都会
    # 让分类行显示别的东西（甚至为空）。对不上就不关弹窗，交回原有逻辑重选。
    leaf = path[-1]
    cat_text = state.get("catText") or ""
    if leaf not in cat_text:
        logger.warning(f"草稿默认类目「{leaf}」未出现在分类行"
                       f"（实际显示「{cat_text[:60]}」），走原有路径")
        return None
    logger.info(f"沿用草稿默认类目：{' > '.join(path)}（{reason}）")
    await _close_cat_modal(session)
    snippet = await read_current_category(session)
    # 走通的路径记进缓存（与慢路径同判据：它已过 LLM 逐级复核 + 分类行回读两道）
    cache.remember_category(path, title)
    return {"status": "ok", "path": " > ".join(path), "pathList": list(path),
            "leaf": leaf, "levels": len(path), "source": "default",
            # 这条路径【没改过类目】，故 leafCatId 留空：草稿已保存的类目就是页面上
            # 生效的那个，阶段④ 按它查选项本来就是对的（见 attributes/server_options）。
            "leafCatId": "",
            "trace": [{"level": 0, "picked": "（草稿默认类目）", "reason": reason,
                       "defaultPath": list(path)}],
            "catSnippet": snippet}


async def auto_cat(session: BrowserSession, rowid: str, title: str,
                   max_levels: int = 8, lookahead: bool = True,
                   use_cache: bool = True, site: str = "",
                   info: Optional[dict] = None) -> dict:
    """阶段③：逐级选定产品类目到叶子，确认后留在页面（不刷新，状态交后续阶段）。

    走 UI 树而非类目 API（理由见上方注释块）：打开弹窗读第 0 列 → 前瞻各候选的子类目 →
    LLM 选 → 点列 0 → 读新出现的列 → …… 直到某级点完不再出现新列（= 到叶子），
    然后点「选择」确认并回读校验。

    use_cache=True（默认）时先试两条快路径，都不中才落回下面这条完整遍历，一行逻辑
    都不跳：
      1. 草稿默认类目（_try_default_category）——编辑页本来就带着类目，一次 LLM 判它
         对不对，对就一个 DOM 都不点直接沿用；
      2. 历史路径清单（_try_cached_category）——命中约 15s。
    两条快路径的风险取向见各自上方的注释块。use_cache=False（怀疑既有值选错类目、
    要全量重判时）把两条都跳过：草稿默认类目也是「既有值」，同样不该信。

    lookahead=True（默认）时每级都先把各候选的子类目一并给 LLM，判断质量明显更好
    （只看本级名字会挑"名字最像"的而错过更贴切的分支）。子类目由 category_api 一次
    并发取回，不再逐个点弹窗探测；LLM 调用次数不变（仍是每级一次）。候选只有 1 个时
    跳过前瞻（没得选，白花时间）。

    【候选与叶子判据都来自接口】逐级候选取自 popTemuCategory/list.json，叶子由它的
    isLeaf 定夺（不再靠「点完有没有新列冒出来」猜）。弹窗仍要打开、选中仍要逐列点，
    因为最终得把这条路径落进表单。

    【必须先关掉回显】弹窗打开时若草稿已有类目会回显 6 列全开；本函数第一步就在
    列 0 重新点选，右侧列随之全部销毁重建，因此不必特意清理——但列索引必须按层级
    算（第 N 级点列 N），不能沿用原脚本「总点最后一列」的写法。

    info 传 product-info.json 的内容时，会从中提取年龄段/尺码线索一并喂给 LLM
    （见 cat_clues 上方的 2026-08-29 取证：只给标题会把「女…宝宝」并列的童装
    判成成人女装，一路错到叶子，直到阶段⑧ 尺码对不上才暴露）。不传则退化成
    原来的只看标题，判断逻辑一行不变。

    会真实修改草稿的类目值。类目变更会清空「关联BestSeller款」（页面自带行为）。
    """
    clues = cat_clues(info)
    if clues:
        logger.info(f"类目判断线索：{clues}")
    await navigation.open_edit(session, rowid)
    # 等页面自己的数据加载完（原固定 sleep(2)）。
    #
    # 【判据必须是「商家账号已回填」，不是按钮渲染出来】2026-09-03 踩坑取证：
    # open_edit 只等到 skuDataInfo 出现就返回，而页面随后还在异步回填自身数据
    # （店铺/站点/类目等）。我先试过两个更弱的判据，都会 0~几十 ms 就返回：
    #   - 只判 productBasicInfo 存在  → 点击落空，报「选择类目弹窗未就绪」
    #   - 判「选择分类」按钮可见      → 按钮早就在了，点开弹窗时平台弹
    #                                  「错误：请选择店铺!」并拒开弹窗
    # 原 sleep(2) 真正兜住的是这段【数据回填】，不是 DOM 挂载。故判据取商家账号
    # 那一行有值——它正是平台校验的前置项，有值即说明页面数据到位。
    # 上限 8s：数据回填比 DOM 慢，2s 只是经验值而非上界；提前满足就立刻返回，
    # 故放宽上限不会让正常情况变慢。
    filled = await common._poll_until(
        lambda: session.eval_json(r"""(() => {
            const sec = document.getElementById('productBasicInfo');
            if (!sec) return JSON.stringify({ready: false, why: 'no-section'});
            const row = Array.from(sec.querySelectorAll('.ant-form-item'))
              .find(el => {
                const l = el.querySelector('.ant-form-item-label label');
                const t = l ? (l.getAttribute('title') || l.textContent || '') : '';
                return t.replace(/[*\s]/g, '').includes('商家账号');
              });
            if (!row) return JSON.stringify({ready: false, why: 'no-shop-row'});
            const sel = row.querySelector('.ant-select-selection-item');
            const v = sel ? (sel.textContent || '').trim() : '';
            return JSON.stringify({ready: !!v, value: v.slice(0, 40)});
        })()"""),
        lambda d: d.get("ready"),
        timeout=8.0)
    if not filled.get("ready"):
        # 顺手把 productBasicInfo 里实际存在的表单字段 dump 出来，方便定位为什么
        # 找不到「商家账号」行（站点英文文案 / 平台改措辞 / 该行压根没渲染）。
        labels = None
        try:
            labels = await session.eval_json(r"""(() => {
                const sec = document.getElementById('productBasicInfo');
                if (!sec) return {labels: []};
                return {labels: Array.from(sec.querySelectorAll('.ant-form-item-label label'))
                    .map(l => (l.getAttribute('title') || l.textContent || '').trim())
                    .filter(Boolean)};
            })()""")
        except Exception:
            logger.warning("等商家账号回填超时后 dump 表单字段失败")
        # 【不抛】这一步只是等待，判不出来就按原样继续——下方打开弹窗本身会报错，
        # 那条路径的诊断信息（含平台 toast）比在这里抛更有用。
        logger.warning(f"等商家账号回填超时（{filled.get('why') or filled}），"
                       f"实际表单字段：{(labels or {}).get('labels')}，"
                       "仍继续尝试打开类目弹窗")
    await session.kill_stuck_modals()  # 上一轮残留的遮罩会挡住「选择分类」按钮

    r = await session.eval_json(_JS_OPEN_CAT_MODAL)
    if not r.get("opened"):
        raise RuntimeError(f"打开选择类目弹窗失败: {r}")
    cols = await session.wait_for(
        _JS_CAT_COLUMNS, lambda d: d.get("ready") and d.get("n", 0) > 0, timeout=15)
    if not cols.get("ready"):
        raise RuntimeError("选择类目弹窗未就绪")

    # 两条快路径：先判草稿默认类目，再判历史路径清单。都不中/不适用就落回下方原有
    # 逐级遍历（不需清场，理由见 _try_cached_category 的 docstring）
    if use_cache:
        hit = await _try_default_category(session, title, clues)
        if not hit:
            hit = await _try_cached_category(session, title, site, clues, rowid)
        if hit:
            return hit

    path: list = []
    trace: list = []
    # 【候选改从接口取，选中仍点 UI】见 category_api 的模块说明：接口给的候选与弹窗
    # 那一列实测逐项一致（含顺序），前瞻更是接口直接就有——不必对每个候选「点一下看
    # 子级再复位」。叶子也由接口的 isLeaf 判定，不再靠「点完有没有新列出现」去猜。
    shop_id = await category_api.fetch_shop_id(session, rowid)
    if not shop_id:
        raise RuntimeError("取不到店铺 id（shopId），无法查类目候选")
    parent_id = ""
    for level in range(max_levels):
        rows = await category_api.fetch_children(session, shop_id, parent_id)
        if not rows:
            break
        options = [r["catName"] for r in rows]
        children = None
        if lookahead and len(options) > 1:
            children = await category_api.fetch_children_map(
                session, shop_id, [r["catId"] for r in rows])
        idx, reason = await _pick_category(title, options, path, children, clues)
        name = options[idx]
        cr = await _click_cat_in_column(session, level, name)
        if not cr.get("clicked"):
            # 接口候选与 UI 列实测同源，但类目树可能因店/站点而异；对不上就如实失败，
            # 带上该列实际选项便于定位，绝不静默换一个名字点下去。
            raise RuntimeError(
                f"第{level + 1}级点击「{name}」失败: {cr.get('reason')}；"
                f"该列实际选项：{(cr.get('options') or [])[:20]}"
            )
        path.append(name)
        step = {"level": level + 1, "picked": name, "reason": reason,
                "candidateCount": len(options), "catId": rows[idx].get("catId")}
        if children is not None:
            step["lookaheadChildren"] = {k: len(v) for k, v in children.items()}
        trace.append(step)
        logger.info(f"类目第{level + 1}级：{name}（{reason}）")
        if rows[idx].get("isLeaf"):
            break  # 接口说这一级已是叶子，点完即完成
        parent_id = rows[idx]["catId"]
        # 等右侧列重建：下一次点击要点在新列上，故这里仍要等它挂出来（判据与改前一致：
        # 点第 N 级会销毁右侧所有列、只新建下一级，故 n > level+1）。等不到不当作
        # 「到叶子」——叶子由上面的 isLeaf 定夺，这里等不到就让下一次点击如实报错。
        after = await common._poll_until(
            lambda: _cat_columns(session),
            lambda d, lv=level: (d or {}).get("n", 0) > lv + 1, timeout=1.5)
        if after.get("n", 0) <= level + 1:
            logger.warning(f"第{level + 1}级点选后右侧新列未出现，继续按接口路径往下走")
    else:
        raise RuntimeError(f"超过 {max_levels} 级仍未到叶子类目：{' > '.join(path)}")

    cr = await _confirm_cat(session)
    if not cr.get("confirmed"):
        raise RuntimeError(f"确认类目失败: {cr}")
    # 等回显更新（原固定 sleep(2)，上限不变）：确认后 Vue 更新 productBasicInfo 区
    # 的类目显示，判据就是叶子名出现在回显里
    leaf = path[-1] if path else ""
    if leaf:
        await common._poll_until(
            lambda: read_current_category(session),
            lambda snippet: snippet and leaf in snippet,
            timeout=2.0)
    snippet = await read_current_category(session)
    # 回读校验：叶子类目名必须出现在基本信息区，否则确认没生效
    if leaf and snippet and leaf not in snippet:
        logger.warning(f"类目回读未见「{leaf}」，实际显示：{snippet[:120]}")
    # 走通的路径记进缓存，下一个同类商品就能走快路径。
    # 【只在回读真见到叶子名时才记】上面那行回读不符目前只 warning 不失败，把没生效
    # 的路径记下来会永久污染缓存（缓存按既定决策不设过期），下游又没有任何环节能
    # 发现它是错的。
    if use_cache and path and leaf and snippet and leaf in snippet:
        cache.remember_category(path, title, [s.get("catId") for s in trace])
    return {"status": "ok", "path": " > ".join(path), "pathList": list(path),
            "leaf": leaf, "levels": len(path), "source": "walk",
            # 叶子 catId 交给阶段④ 查属性选项用（类目是运行中改的、还没保存，
            # 服务端只认已保存的那版，见 attributes/server_options）。
            "leafCatId": (trace[-1].get("catId") if trace else "") or "",
            "trace": trace, "catSnippet": snippet}
