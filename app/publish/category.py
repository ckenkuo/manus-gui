"""店小秘发布操作：category。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio
import re
from app.logger import logger
from app.publish import cache, common, navigation, size_rules, workflows
from app.publish.browser import BrowserSession, J
from typing import Optional


# ---- 阶段③ 产品类目（写入）---------------------------------------------------
# 【与原 skill 的关键分歧：走 UI 树，不走类目 API】
# 原脚本先调 /api/popTemuCategory/list.json 逐级拉类目树、交 LLM 选，再去弹窗里点。
# SKILL.md 把随之而来的 item-not-found 归因为「API 树与 UI 树可能不一致」，并加了
# 一层「把 UI 当前列选项交 LLM 再选一次」的兜底。
# 2026-08-19 对真站实测发现真正的成因不是树不一致，而是【列定位错了】：
#   - 弹窗打开时若草稿已有类目，会【回显完整路径】——6 级类目就是 6 列全开，
#     每列有个带 active class 的已选项；
#   - 原脚本每级都点「最后一列」，于是拿第 1 级的名字去第 6 列里找，必然找不到。
# 而且实测确认类目树【本来就整棵在弹窗 DOM 里】（打开弹窗零网络请求），
# 因此根本不需要那个 API：直接读 UI 树的列、按列索引点，既绕开树不一致的可能，
# 也不用维护接口参数（shopId 是账号级常量，页面上还取不到，硬编码换账号就废）。
#
# 联动实测（同日）：点第 N 列的项 → 右侧所有列销毁、只新建下一级列。
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
# 见 _lookahead_children 的注释：只看本级名字会选错分支。
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


async def _lookahead_children(session: BrowserSession, col_idx: int,
                              options: list, restore: Optional[str] = None) -> dict:
    """前瞻：逐个点本级候选、读它的子类目名，读完把列状态复位。

    为什么需要（2026-08-19 实测）：只把本级的名字给 LLM，它看不到分支往下通向哪里，
    会在同级里挑"名字最像"的那个而错过更贴切的分支。实例：POLO衫+卫裤运动套装，
    第4级候选里既有「男童休闲运动服」又有「男童时尚套装」，只看名字会按"套装优先"
    规则选后者；但前者往下才有「男童休闲运动运动服套装 > 男童休闲套装」这条真正
    对应运动套装的路径。把子类目一并给出，判断依据就完整了。

    代价只是每级多 N 次 DOM 点击+读取，不额外调 LLM（LLM 仍是每级一次）。

    restore：探测完要重新点回的项名（通常是探测前该列的 active 项）。不复位会留下
    脏状态——探测停在最后一个候选上，右侧列是它的子列，后续按索引点击就会错位
    （实测踩到过）。
    """
    children: dict = {}
    for name in options:
        cr = await _click_cat_in_column(session, col_idx, name)
        if not cr.get("clicked"):
            # 探测是辅助信息，单项失败不该让整个类目选择崩掉：记空清单继续
            logger.warning(f"前瞻「{name}」点击失败（跳过）：{cr.get('reason')}")
            children[name] = []
            continue
        await asyncio.sleep(0.8)
        state = await _cat_columns(session)
        kids = (state["cols"][col_idx + 1]["items"]
                if state.get("n", 0) > col_idx + 1 else [])
        children[name] = kids
    if restore:
        rr = await _click_cat_in_column(session, col_idx, restore)
        if not rr.get("clicked"):
            logger.warning(f"前瞻后复位到「{restore}」失败：{rr.get('reason')}")
        await asyncio.sleep(0.8)
    return children


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


async def _try_cached_category(session: BrowserSession, title: str,
                               site: str = "",
                               clues: str = "") -> Optional[dict]:
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

    cache.remember_category(path, title)
    return {"status": "ok", "path": " > ".join(path), "pathList": list(path),
            "leaf": leaf, "levels": len(path), "source": "cache",
            "trace": [{"level": 0, "picked": "（缓存路径）", "reason": reason,
                       "cachedPath": list(path)}],
            "catSnippet": snippet}


async def auto_cat(session: BrowserSession, rowid: str, title: str,
                   max_levels: int = 8, lookahead: bool = True,
                   use_cache: bool = True, site: str = "",
                   info: Optional[dict] = None) -> dict:
    """阶段③：逐级选定产品类目到叶子，确认后留在页面（不刷新，状态交后续阶段）。

    走 UI 树而非类目 API（理由见上方注释块）：打开弹窗读第 0 列 → 前瞻各候选的子类目 →
    LLM 选 → 点列 0 → 读新出现的列 → …… 直到某级点完不再出现新列（= 到叶子），
    然后点「选择」确认并回读校验。

    use_cache=True（默认）时先试缓存快路径（_try_cached_category）：命中约 15s、
    不命中就落回下面这条完整遍历，一行逻辑都不跳。快路径的风险取向见它上方的注释块。

    lookahead=True（默认）时每级都先把各候选的子类目读出来一起给 LLM，判断质量明显
    更好（见 _lookahead_children 里的实例）；代价是每级多 N 次 DOM 点击，LLM 调用
    次数不变（仍是每级一次）。候选只有 1 个时跳过前瞻（没得选，白花时间）。

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

    # 缓存快路径：命中就 7.6s 走完，未命中/不匹配落回下方原有逐级遍历（不需清场，
    # 理由见 _try_cached_category 的 docstring）
    if use_cache:
        hit = await _try_cached_category(session, title, site, clues)
        if hit:
            return hit

    path: list = []
    trace: list = []
    for level in range(max_levels):
        state = await _cat_columns(session)
        if not state.get("ready"):
            raise RuntimeError(f"第{level + 1}级读列失败：弹窗消失")
        if level >= state.get("n", 0):
            break  # 没有第 level 列了，说明上一级已是叶子
        options = state["cols"][level]["items"]
        if not options:
            break
        # 前瞻一级：把每个候选的子类目读出来一起给 LLM，避免只看本级名字选错分支。
        # restore 传本列探测前的 active 项，让探测不留脏状态（见 _lookahead_children）。
        children = None
        if lookahead and len(options) > 1:
            children = await _lookahead_children(
                session, level, options,
                restore=state["cols"][level].get("active"))
        idx, reason = await _pick_category(title, options, path, children, clues)
        name = options[idx]
        cr = await _click_cat_in_column(session, level, name)
        if not cr.get("clicked"):
            # 理论上不该发生（名字就是从这一列读出来的），但 Vue 重渲染可能让
            # DOM 在读与点之间变化，故保留诊断信息而不是静默失败
            raise RuntimeError(
                f"第{level + 1}级点击「{name}」失败: {cr.get('reason')}；"
                f"该列实际选项：{(cr.get('options') or [])[:20]}"
            )
        path.append(name)
        step = {"level": level + 1, "picked": name, "reason": reason,
                "candidateCount": len(options)}
        if children is not None:
            step["lookaheadChildren"] = {k: len(v) for k, v in children.items()}
        trace.append(step)
        logger.info(f"类目第{level + 1}级：{name}（{reason}）")
        # 等右侧列重建：原先固定 sleep(1.5) 再读一次列。收敛信号就是下一次要读的
        # 那个 n——点第 N 级会销毁右侧所有列、只新建下一级（见本文件类目区块开头的
        # 联动实测），故「n > level + 1」即新列已挂上。到叶子时不会有新列，
        # 那种情况等满 1.5s 后 n <= level+1，与改前完全一致（break 到叶子）。
        after = await common._poll_until(
            lambda: _cat_columns(session),
            lambda d, lv=level: (d or {}).get("n", 0) > lv + 1, timeout=1.5)
        if after.get("n", 0) <= level + 1:
            break  # 点完没有新列出现 = 到叶子
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
        cache.remember_category(path, title)
    return {"status": "ok", "path": " > ".join(path), "pathList": list(path),
            "leaf": leaf, "levels": len(path), "source": "walk",
            "trace": trace, "catSnippet": snippet}
