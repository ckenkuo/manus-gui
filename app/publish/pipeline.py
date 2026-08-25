"""店小秘 Temu 半托管编辑页操作（发布管线阶段②-⑫）。

从 skill 的 scripts/dianxiaomi_edit.py（3423 行）+ dianxiaomi_claim.py 移植。
搬运策略：按【只读优先】分批进行，每批搬完就对真站验证再搬下一批——原脚本里
113 处 JS 是对真实页面逐个试出来的，一次性全搬进来无法定位哪一处水土不服。

已搬（只读，不改店小秘任何数据）：
    find_rowid   草稿列表按标题关键词找 rowid
    open_edit    拼 URL 进编辑页（比点「编辑」按钮可靠：那个按钮开新标签页会跑出会话）
    inspect      导出当前表单状态（各区块字段/变种表/运输信息）
    list_images  提取素材图/颜色图/描述图 URL 并下载，供逐张合规检查

待搬（写入类，按 SKILL.md 阶段顺序）：
    set_material / skc_replace_row / desc_*

【发布闸门】阶段⑮ publish_now 是全管线唯一不可逆的一步（真实商家账号、上架后要
下架才能改）。它【必须显式传 confirm=True】才会执行，否则直接返回 refused——闸门在
调用方手上，不在这里。2026-08-24 前本模块刻意没有 publish 入口，那条约定已按用户
要求解除，但「不显式要求就绝不发布」的取向不变。

【点击方式因场景而异，勿统一】原脚本实测出三条互相矛盾的结论：
  - 编辑页按钮（保存等）：JS el.click() 有效；mouse_click 与 CDP dispatchMouseEvent 被吞
  - 认领弹窗店铺复选框：必须真实点击，JS .click() 不触发 Vue
  - 素材图悬停菜单：必须 CDP 真实鼠标移动，且要先移开再移入
搬到 Playwright 后每条都要重验，不能假定继承（见 browser.mouse_click 的注释）。
"""
import asyncio
import json
import os
import re
from typing import Optional

from app.logger import logger
from app.publish import cache, images
from app.publish.browser import (
    DRAFT_LIST_URL,
    EDIT_URL,
    ONLINE_LIST_URL,
    J,
    BrowserSession,
)
from app.publish.upload import upload_image

# 编辑页右侧锚点导航的 7 个区块 ID（用 getElementById 定位比文本匹配稳定）
SECTION_IDS = {
    "productBasicInfo": "基本信息",
    "dxmInfo": "店小秘信息",
    "productProductInfo": "产品信息",
    "skuAttrsInfo": "变种属性",
    "skuDataInfo": "变种信息",
    "describeInfo": "产品描述",
    "shipmentInfo": "运输信息",
}


async def open_edit(session: BrowserSession, rowid: str) -> dict:
    """在会话内打开编辑页，等表单加载完成。

    刻意拼 URL 而不点草稿列表里的「编辑」按钮：那是个无 href 的 <button>，点击后
    在【新标签页】打开，会跑出当前会话拿不到（原脚本实测结论）。
    以 skuDataInfo 区块出现为加载完成判据——它在页面靠后，出现说明表单渲染完了。
    """
    url = EDIT_URL.format(rowid=rowid)
    logger.info(f"打开编辑页：{url}")
    r = await session.navigate(url)
    if not r.get("ok"):
        raise RuntimeError(f"导航失败: {r}")
    data = await session.wait_for(
        "JSON.stringify({loaded: !!document.getElementById('skuDataInfo'), "
        "title: document.title})",
        lambda d: d.get("loaded"), timeout=40)
    if not data.get("loaded"):
        raise RuntimeError("编辑页加载超时（未登录或 rowid 无效）")
    logger.info(f"编辑页已加载：{data.get('title')}")
    return {"rowid": rowid, "url": url, "title": data.get("title")}


async def find_rowid(session: BrowserSession, keyword: str) -> dict:
    """草稿列表按标题关键词找 rowid（只读，不点任何按钮）。

    rowid 是草稿列表行的 <tr rowid="..."> 属性（18 位数字），后续所有阶段都靠它拼
    编辑页 URL。一个商品可能被认领到多个站点、出现多行，故返回全部匹配交调用方按
    店铺/站点筛（见 service 层的 pick_rowid）。
    """
    r = await session.navigate(DRAFT_LIST_URL)
    if not r.get("ok"):
        raise RuntimeError(f"导航失败: {r}")
    js = r"""(() => {
      const rows = Array.from(document.querySelectorAll('tr[rowid]'));
      const m = rows.filter(r => (r.textContent||'').includes(__KW__))
        .map(r => ({rowid: r.getAttribute('rowid'),
                    text: (r.textContent||'').replace(/\s+/g,' ').slice(0,120)}));
      return JSON.stringify({total: rows.length, matched: m});
    })()""".replace("__KW__", J(keyword))
    data = await session.wait_for(js, lambda d: d.get("total", 0) > 0, timeout=30)
    return data


# ---- inspect（只读导出表单状态）---------------------------------------------
# 按 label 读当前值：ant-design 的值可能在 input、已选下拉文本、或勾中的 radio 上，
# 三处都要试。顺序有讲究——radio 优先于 select，因为部分行两者都存在
# （如「敏感属性」是 radio，但同一 form-item 里可能还有个联动下拉的残留 DOM）。
# 【必须用 r"""】JS 正则里的 \s 若走普通字符串就得写成 \\s，一旦被格式化工具"修正"成
# \s，Python 转义后 JS 收到的是字面 s，正则 [*\s] 就变成「匹配星号或字母s」，会把
# label 里的 s 删掉。raw string 让两边看到的都是同一个 \s，不给这种改动留缝隙。
_JS_INSPECT = r"""(() => {
  const out = {sections: {}, form: [], skus: [], shipping: {}};

  document.querySelectorAll('.ant-form-item').forEach(item => {
    const lab = item.querySelector('.ant-form-item-label');
    const label = lab ? (lab.textContent||'').trim().replace(/[*\s]/g,'') : '';
    if (!label) return;
    const ctl = item.querySelector('.ant-form-item-control') || item;
    const sel = ctl.querySelector('.ant-select-selection-item');
    const inp = ctl.querySelector('input:not([type=hidden]), textarea');
    const radio = ctl.querySelector('.ant-radio-wrapper-checked');
    let value = null;
    if (radio) value = (radio.textContent||'').trim();
    else if (sel) value = (sel.textContent||'').trim();
    else if (inp) value = inp.value;
    if (value !== null && value !== '') out.form.push({label, value: String(value).slice(0,120)});
  });

  const skuSec = document.getElementById('skuDataInfo');
  if (skuSec) {
    skuSec.querySelectorAll('tr').forEach(tr => {
      const tds = Array.from(tr.querySelectorAll('td'));
      if (tds.length < 3) return;
      const rowText = tds.map(td => (td.textContent||'').trim().slice(0,20));
      const inputs = Array.from(tr.querySelectorAll('input:not([type=hidden])'))
        .map(i => i.value || '');
      if (rowText.some(t => t)) out.skus.push({cells: rowText.slice(0,4), inputs});
    });
  }

  const ship = document.getElementById('shipmentInfo');
  if (ship) {
    const checked = ship.querySelector('.ant-radio-wrapper-checked, .ant-radio-button-wrapper-checked');
    const tpl = ship.querySelector('.ant-select-selection-item');
    out.shipping = {
      deadline: checked ? (checked.textContent||'').trim() : null,
      freightTemplate: tpl ? (tpl.textContent||'').trim() : null,
    };
  }

  for (const id of ['productBasicInfo','dxmInfo','productProductInfo','skuAttrsInfo','skuDataInfo','describeInfo','shipmentInfo'])
    out.sections[id] = !!document.getElementById(id);

  return JSON.stringify(out);
})()"""


async def inspect(session: BrowserSession, rowid: str) -> dict:
    """打开编辑页并导出当前表单状态（只读，不改任何字段）。

    用途：搬运写入类子命令前先看清页面此刻长什么样；出问题时对比预期与实际。
    """
    info = await open_edit(session, rowid)
    await asyncio.sleep(2)  # 等 Vue 把各区块渲染完，否则 form 项读不全
    data = await session.eval_json(_JS_INSPECT)
    return {"status": "ok", **info, **data}


# ---- 图片清单（只读，发布前合规检查用）--------------------------------------
# 三组图分别在不同区块：素材图在产品信息区、颜色图在变种属性区（按行分组）、
# 描述长图在产品描述区。只收 http(s) 图，过滤掉 base64 占位图和图标。
_JS_ALL_IMAGES = r"""(() => {
  const out = {material: [], colors: [], desc: []};
  const httpImg = el => (el.currentSrc || el.src || '').startsWith('http');

  const pi = document.getElementById('productProductInfo');
  if (pi) out.material = Array.from(pi.querySelectorAll('img')).filter(httpImg)
    .map(i => i.currentSrc || i.src);

  const sku = document.getElementById('skuAttrsInfo');
  if (sku) sku.querySelectorAll('tr').forEach(tr => {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) return;
    const color = (tds[0].textContent||'').trim().slice(0,20);
    const urls = Array.from(tr.querySelectorAll('img')).filter(httpImg)
      .map(i => i.currentSrc || i.src);
    if (urls.length) out.colors.push({color, urls});
  });

  const desc = document.getElementById('describeInfo');
  if (desc) out.desc = Array.from(desc.querySelectorAll('img')).filter(httpImg)
    .map(i => i.currentSrc || i.src);

  return JSON.stringify(out);
})()"""


async def list_images(session: BrowserSession, rowid: str,
                      outdir: Optional[str] = None) -> dict:
    """提取素材图/颜色图/描述图 URL，可选下载到本地供逐张合规检查。

    只读页面，不改任何数据。outdir 为 None 则只返回 URL 清单不下载。
    图片来源全是采集源（1688 等），可能含中文/水印/第三方 logo——发布前必须逐张过目，
    这是服装类发布最常被弹回的一环（见 SKILL.md 的图片硬校验）。
    """
    await open_edit(session, rowid)
    await asyncio.sleep(2)
    data = await session.eval_json(_JS_ALL_IMAGES)
    groups = {
        "material": len(data.get("material", [])),
        "colors": sum(len(g["urls"]) for g in data.get("colors", [])),
        "desc": len(data.get("desc", [])),
    }
    result = {"status": "ok", "urls": data, "groups": groups}
    if not outdir:
        return result

    # 下载复用 extract 里那套带浏览器头 + 重试的实现：店小秘图床同样会拦裸请求，
    # 原脚本用的 urlretrieve 无 UA、一次失败就丢图。
    from app.publish.extract import _download_image
    os.makedirs(outdir, exist_ok=True)
    files, failed = [], []

    def dl(url: str, name: str) -> None:
        ext = ".jpg"
        for e in (".jpeg", ".png", ".webp"):
            if e in url:
                ext = e
                break
        path = os.path.join(outdir, name + ext)
        try:
            _download_image(url, path)
            files.append(path)
        except Exception as e:
            failed.append({"url": url[:120], "err": str(e)})
            logger.warning(f"{name} 下载失败：{e}")

    for i, u in enumerate(data.get("material", []), 1):
        dl(u, f"material-{i:02d}")
    for grp in data.get("colors", []):
        # 颜色名可能含 Windows 文件名非法字符（斜杠等），清一遍再当文件名用
        color = re.sub(r'[\/:*?"<>|]', "_", grp["color"] or "unknown")
        for i, u in enumerate(grp["urls"], 1):
            dl(u, f"color-{color}-{i:02d}")
    for i, u in enumerate(data.get("desc", []), 1):
        dl(u, f"desc-{i:02d}")

    result.update({
        "status": "ok" if not failed else "partial",
        "outdir": outdir, "count": len(files), "files": files, "failed": failed,
        "note": "请逐张检查中文文字/水印/第三方logo后再决定是否发布",
    })
    return result


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


# 类目选择提示词：前四条规则是原 skill 踩出来的（套装优先、性别年龄段必须一致、
# "其他"类目只在全都不符时才选），第 5 条是本项目 2026-08-19 实测补的——
# 见 _lookahead_children 的注释：只看本级名字会选错分支。
_CAT_PROMPT = """你是跨境电商类目分类助手。根据商品标题，从候选类目列表中选出语义上最合适的一个。
每个候选后面的「└ 子类目:」列出了它下一级包含什么，用来判断该分支往下走能否到达更贴切的叶子类目。

规则：
1. 若商品标题表明是套装类商品（含"套装""两件套""三件套""套裙"等），候选中有套装/两件套类类目时，必须优先选套装类类目，而不是按单品材质/上衣/裤子归类。
2. 性别、年龄段（婴儿/女童/男童/女士/男士）必须与标题一致。
3. "其他（...）"类目只有在所有其他候选都语义不符时才能选；只要存在更具体的候选（如按裙/长裤/短裤、开衫/套头衫区分），就必须选最具体匹配的那个，依据标题中的实体信息判断（如"牛仔裤"=长裤下装）。
4. 商品的风格属性（运动/休闲/正装/居家）要与类目分支一致：运动风商品（含"运动""卫衣""POLO""卫裤""速干"等）应优先进"运动服/休闲运动"这类分支。
5. 【重要】选择时必须综合本级名字和它的子类目：若某候选本身不如另一个贴切，但其子类目里有明显更匹配商品的项，应选它。反之，某候选名字看着匹配但子类目全都不符，则不该选。

商品标题：{title}
已选路径：{path}
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
                         children: Optional[dict] = None) -> tuple:
    """让 LLM 从候选类目里选一个，返回 (index, reason)。

    children 给出每个候选的子类目清单（前瞻结果），会拼进提示词。
    子类目多时只取前 12 个——提示词太长反而稀释重点，12 个足够看出分支性质。
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
2. 性别、年龄段（婴儿/女童/男童/女士/男士）必须与标题一致。路径中任何一级的性别或年龄段与商品不符，这条路径就不能选。
3. "其他（...）"类叶子类目只有在清单里所有更具体的候选都语义不符时才能选。
4. 商品的风格属性（运动/休闲/正装/居家）要与路径分支一致：运动风商品（含"运动""卫衣""POLO""卫裤""速干"等）应走"运动服/休闲运动"这类分支。
5. 【重要】要逐级复核整条路径，不是只看叶子名。叶子名看着匹配但中间某一级（品类大类、性别、年龄段、风格分支）与商品不符的，不能选。
6. 【最重要】拿不准就选「以上都不匹配」。选错一条路径会让商品发布到错误类目、必须人工回滚；选「不匹配」只是让程序多花约两分钟重新走一遍类目树，代价小得多。只有当某条路径的每一级都与商品相符、且叶子类目就是这件商品该去的地方时才选它；仅仅是"大类相同"（都是女装、都是童装）远远不够。

商品标题：{title}
已知类目路径：
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


async def _pick_cached_category(title: str, known: list) -> tuple:
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
            title=title, paths=_format_cached_paths(known)),
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
        await asyncio.sleep(1.5)   # 等右侧列重建（与慢路径同一实测值）
    return {"ok": True, "clickedLevels": len(path), "failedLevel": None,
            "reason": "", "options": []}


async def _try_cached_category(session: BrowserSession, title: str,
                               site: str = "") -> Optional[dict]:
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
        path, reason = await _pick_cached_category(title, known)
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
    await asyncio.sleep(2)
    snippet = await read_current_category(session)
    leaf = path[-1]
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
                   use_cache: bool = True, site: str = "") -> dict:
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

    会真实修改草稿的类目值。类目变更会清空「关联BestSeller款」（页面自带行为）。
    """
    await open_edit(session, rowid)
    await asyncio.sleep(2)
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
        hit = await _try_cached_category(session, title, site)
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
        idx, reason = await _pick_category(title, options, path, children)
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
        await asyncio.sleep(1.5)  # 等右侧列重建
        after = await _cat_columns(session)
        if after.get("n", 0) <= level + 1:
            break  # 点完没有新列出现 = 到叶子
    else:
        raise RuntimeError(f"超过 {max_levels} 级仍未到叶子类目：{' > '.join(path)}")

    cr = await _confirm_cat(session)
    if not cr.get("confirmed"):
        raise RuntimeError(f"确认类目失败: {cr}")
    await asyncio.sleep(2)
    snippet = await read_current_category(session)
    # 回读校验：叶子类目名必须出现在基本信息区，否则确认没生效
    leaf = path[-1] if path else ""
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


# ---- 阶段④ 产品属性（下拉交互辅助）------------------------------------------
# 这批函数是原 skill 里最难缠的部分，几乎每一行都对应一个踩过的坑。搬运时原样保留
# 那些看似多余的清理/重试，别"简化"：
#
# 坑1「幽灵浮层」：合成事件展开的 ant-select 下拉，Vue 内部状态关了但内联样式残留
#   定位，页面上会挂一堆看不见却仍能响应点击的浮层。若扫 DOM 找选项，同选项列表的
#   字段（上装成分/下装成分/材质/辅料成分都是同一份纤维列表）会全命中，点在隐藏浮层
#   上事件照样生效——结果把别的字段改掉了。故一切选项查找都限定在「目标行附近唯一
#   可见浮层」内（top>-1000 且 width>50，距行最近者），并在每步前后清幽灵。
# 坑2「视口外坐标不可靠」：行在视口外时下拉开在负坐标处，可见性判定误判为未打开。
#   故每次定位前先 scrollIntoView({block:'center'})。
# 坑3「虚拟列表」：选项用 rc-virtual-list 只渲染可视窗口约 10 条，静态扫 DOM 既拿不
#   全也会错配。故读选项要滚动收集，点选项时目标不在窗口内要滚动到它渲染出来再点。
# 坑4「动态增删行」：改里料纹理等字段会触发表单重渲染并联动新增必填行（里衬成分、
#   里料克重）。固定等待的单次回读会读到旧值/空行造成假失败，故回读改轮询等值稳定。
# 坑5「必填标记在内层 span」（2026-08-19 本项目实测修正原脚本的缺陷）：
#   固定字段是 <label class="ant-form-item-required" title="产品分类">，
#   而动态属性行是 <label class="ant-form-item-no-colon" title="">
#                   └ <span class="attr-label required">适用人群</span>
#   —— 必填 class 在【内层 span】上，且 label 的 title 是【空的】。原脚本只看
#   labelEl.className 和行内 .ant-form-item-required，于是 33 个属性行全被判成非必填。
#   这会直接毁掉 check_attrs 的核心策略（「只填必填项、非必填一律留空」）：必填判断
#   全 false 时，要么该填的必填项一个都不填，要么反过来把几十个非必填项全填上，
#   正是原脚本想避免的「填得多错得多」。故 label 与 required 都必须先取内层 span。

_JS_LIST_ATTR_ROWS = r"""(() => {
  const sec = document.getElementById('productBasicInfo');
  if (!sec) return JSON.stringify({found: false});
  const skip = ['店铺账号', '经营站点', '产品分类'];
  const out = [];
  sec.querySelectorAll('.ant-form-item').forEach(it => {
    const labelEl = it.querySelector('.ant-form-item-label label');
    if (!labelEl) return;
    // 动态属性行的 label 结构与固定字段不同（2026-08-19 实测，见下方注释）：
    // 名字在内层 span.attr-label 里、label 的 title 是空的，故先取内层 span。
    const attrSpan = labelEl.querySelector('.attr-label');
    const label = attrSpan
      ? (attrSpan.textContent || '').trim()
      : (labelEl.getAttribute('title') || labelEl.textContent || '').trim();
    if (!label || skip.includes(label)) return;
    // 【kind 判据是控件序里的第一个控件，不是「有没有 .ant-select」】
    // 2026-08-25 真站取证（rowid 173539495454695591）纠正了原先的错误前提——
    // 原注释断言「纯数值属性没有 .ant-select」，实际「里料克重（g/m²)」是
    // 【数值输入框 + 单位下拉】的复合行：
    //     里料克重（g/m²)  →  input[type=text].ant-input, select[cur=g/㎡]
    //     里衬成分         →  select[ph=请选择], input（百分比）
    //     成分             →  select[cur=棉], input[80], select[聚酯纤维], input[20]
    // 按「有 select 就算下拉行」会把克重判成 select，后果是三连错：current 读成
    // 单位文本「g/㎡」（于是这行看起来【已填】，LLM 不会填、末尾的必填复扫也不报），
    // options 读成单位列表 ['g/㎡'] 并落进缓存，写入还会走下拉分支去点单位下拉。
    // 数值框就一直空着，保存卡「请输入产品属性」。
    // 反过来「按 input 存在就算数值行」也不行——成分行同样有百分比 input。
    // 区分点在【顺序】：数值行的输入框在单位下拉之前，成分行的下拉在百分比之前。
    // （旁证：单位下拉内部的搜索框带 readonly，是纯展示的固定单位，不必也不该改。）
    const ctrl = it.querySelector('.ant-form-item-control') || it;
    // 控件序：select 与「可填 input」按 DOM 顺序排一列。select 内部的搜索框
    // （.ant-select-selection-search-input）会一并匹配到，按 class 剔除。
    const seq = Array.from(ctrl.querySelectorAll('.ant-select, input')).filter(el => {
      if (el.tagName !== 'INPUT') return true;
      if (el.type === 'hidden' || el.type === 'radio' || el.type === 'checkbox') return false;
      return !(el.className || '').includes('ant-select');
    });
    if (!seq.length) return;                      // 既无下拉也无可填输入，不是属性行
    const firstIsInput = seq[0].tagName === 'INPUT';
    const kind = firstIsInput ? 'number' : 'select';
    const fillable = seq.filter(el => el.tagName === 'INPUT');
    // 【成分类复合行的结构判据】下拉打头、后面还跟着可填 input = 「纤维 + 百分比」
    // 复合行（里衬成分、上装成分…）。纯下拉行（里料纹理、季节）没有这个 input。
    // 为什么必须给出这个信号：下游原先靠「LLM 有没有给 num」来认成分行，模型漏给
    // num 时那行既跳过了「合计=100」闸、又不填百分比框，表现为纤维选上了、百分比
    // 空着，平台报「请完善里衬成分信息」（2026-08-25 用户截图实测）。行是不是成分
    // 行由 DOM 结构决定，不该由模型的输出完整性决定。
    const hasPercent = !firstIsInput
      && seq.slice(1).some(el => el.tagName === 'INPUT');
    // 【数值行必须是动态属性行才收】固定字段（产品标题/来源URL/产品货号/
    // 站外产品链接）同样是输入框打头，误收进来会让 LLM 去改标题，与阶段⑤ 打架。
    // 判据用内层 span.attr-label：只有动态属性行有它（2026-08-19 实测，见上方注释）。
    if (firstIsInput && !attrSpan) return;
    it.setAttribute('data-attr-label', label);
    const curEl = it.querySelector('.ant-select-selection-item');
    const phEl = it.querySelector('.ant-select-selection-placeholder');
    const numInputs = Array.from(it.querySelectorAll('input'))
      .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))
      .map(i => i.value).filter(v => v !== '');
    // 数值行的填写线索：单位与 placeholder 决定该填什么量级。
    // 【单位主要来自那个只读的单位下拉】原先只找 .ant-input-suffix / -group-addon /
    // .unit，而克重行的单位是 select 里的 selection-item（文本 'g/㎡'），三个选择器
    // 一个都命中不了，unit 恒为空、模型少了唯一的量级线索。故数值行优先取【输入框
    // 之后】的那个 select 的当前文本，再退回原来的三个后缀选择器。
    const unitSel = kind === 'number'
      ? seq.slice(1).find(el => el.tagName !== 'INPUT') : null;
    const unitFromSel = unitSel
      ? ((unitSel.querySelector('.ant-select-selection-item') || {}).textContent || '').trim()
      : '';
    const unitEl = it.querySelector('.ant-input-suffix, .ant-input-group-addon, .unit');
    const numHint = kind !== 'number' ? null : {
      placeholder: (fillable[0] && fillable[0].placeholder) || '',
      unit: unitFromSel || (unitEl ? (unitEl.textContent || '').trim() : ''),
      value: (fillable[0] && fillable[0].value) || '',
    };
    // 必填判定三处都要看：动态属性行是 span.attr-label.required（主力），
    // 固定字段是 label.ant-form-item-required，末项是 antd 的通用兜底。
    const required = (attrSpan && attrSpan.classList.contains('required'))
      || labelEl.classList.contains('ant-form-item-required')
      || !!it.querySelector('.ant-form-item-required');
    out.push({label: label,
      // 【数值行的 current 只能读输入框，绝不能读 selection-item】克重行那个
      // selection-item 是【单位】'g/㎡'，读它等于把空行报成已填（原缺陷的核心）。
      // 空值按 "(请输入)" 表达「未填」，与下拉行的 "(请选择)" 语义一致，
      // 下游判空（cur.startswith('(')）不必分叉。
      current: kind === 'number'
        ? ((numHint && numHint.value) ? numHint.value : '(请输入)')
        : (curEl ? (curEl.textContent || '').trim()
                 : (phEl ? '(' + (phEl.textContent||'').trim() + ')' : null)),
      kind: kind,
      hasPercent: hasPercent,
      numHint: numHint,
      numValues: numInputs,
      required: required,
      visible: it.offsetHeight > 0});
  });
  return JSON.stringify({found: true, attrs: out});
})()"""


async def _expand_attr_section(session: BrowserSession) -> dict:
    """展开产品属性的「+展开」折叠开关。

    收起状态下大量属性行 display:none，下拉根本点不开。已展开时不动（找不到开关就
    当已展开，不报错）。
    """
    return await session.eval_json(r"""(() => {
      const sec = document.getElementById('productBasicInfo');
      if (!sec) return JSON.stringify({expanded: false, reason: 'no-section'});
      const t = Array.from(sec.querySelectorAll('span'))
        .find(el => el.offsetHeight > 0 && /^\+?\s*展开/.test((el.textContent||'').trim())
             && (el.textContent||'').trim().length < 8);
      if (!t) return JSON.stringify({expanded: false, reason: 'already-or-not-found'});
      t.click();
      return JSON.stringify({expanded: true});
    })()""")


async def _park_ghost_dropdowns(session: BrowserSession) -> dict:
    """把视觉残留的幽灵浮层恢复成停靠态（清 left/top、宽高归零）。见坑1。"""
    return await session.eval_json(r"""(() => {
      let parked = 0;
      Array.from(document.querySelectorAll('.ant-select-dropdown')).forEach(d => {
        const r = d.getBoundingClientRect();
        if (r.top > -1000 && r.width > 50) {
          d.style.left = ''; d.style.top = '';
          d.style.width = '0px'; d.style.minWidth = '0px';
          parked++;
        }
      });
      return JSON.stringify({parked: parked});
    })()""")


async def _press_escape(session: BrowserSession) -> None:
    """派发 Escape 关下拉（合成事件，比 keyboard.press 更贴近原脚本行为）。"""
    await session.eval_json(
        "(() => { document.dispatchEvent(new KeyboardEvent('keydown', "
        "{key: 'Escape', bubbles: true})); return JSON.stringify({ok: true}); })()"
    )


async def _visible_dropdown_near(session: BrowserSession, label: str,
                                 max_dist: int = 600) -> dict:
    """检测指定属性行附近是否有可见（未停靠）的下拉浮层。见坑1、坑2。"""
    js = r"""(() => {
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!row) return JSON.stringify({found: false, reason: 'row-not-found'});
      row.scrollIntoView({block: 'center'});
      const rowY = row.getBoundingClientRect().top;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({found: false});
      drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const dist = Math.abs(drops[0].r.top - rowY);
      if (dist > __MAXD__) return JSON.stringify({found: false, nearestDist: Math.round(dist)});
      return JSON.stringify({found: true, top: Math.round(drops[0].r.top)});
    })()""".replace("__LABEL__", J(label)).replace("__MAXD__", str(max_dist))
    return await session.eval_json(js)


async def _open_attr_dropdown(session: BrowserSession, label: str,
                              sel_idx: int = 0) -> dict:
    """点开指定属性行的下拉，以「行附近是否有可见浮层」为准做幂等打开。

    已打开则不重复点（重复点会 toggle 关掉）；点了没开就再点一次——Vue 内部状态与
    视觉停靠态错位时第一次点击会反向 toggle（原脚本实测）。
    sel_idx：成分类复合字段行内第 N 个下拉（0 起）。
    """
    if (await _visible_dropdown_near(session, label)).get("found"):
        return {"opened": True, "already": True}
    js_click = r"""(() => {
      const it = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!it) return JSON.stringify({dispatched: false, reason: 'row-not-found'});
      it.scrollIntoView({block: 'center'});
      const sels = Array.from(it.querySelectorAll('.ant-select'));
      const box = sels[__IDX__] || sels[0];
      const sel = box ? (box.querySelector('.ant-select-selector') || box) : null;
      if (!sel) return JSON.stringify({dispatched: false, reason: 'no-select'});
      ['mousedown','mouseup','click'].forEach(t =>
        sel.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      return JSON.stringify({dispatched: true});
    })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(sel_idx))
    for attempt in (1, 2):
        r = await session.eval_json(js_click)
        if not r.get("dispatched"):
            return {"opened": False, **r}
        await asyncio.sleep(0.5)
        if (await _visible_dropdown_near(session, label)).get("found"):
            return {"opened": True, "attempts": attempt}
    return {"opened": False, "reason": "open-verify-failed"}


async def _read_active_options(session: BrowserSession, label: str,
                               with_meta: bool = False):
    """主动点开该行下拉、滚动虚拟列表读出全部选项，读完关闭并清幽灵。

    不点任何选项，不改表单值。为什么必须点开读而不静态扫 DOM：选项懒渲染 +
    虚拟列表只渲染可视窗口约 10 条（坑3）。

    with_meta=True 时返回 (options, meta) 而不是裸列表，meta 带 virtual/scrollHeight/
    scrolledToEnd 等完整性判据——【落盘缓存前必须看它】：被截断的首屏 10 条写进缓存
    会让 _rebuild_main_comp 的纤维匹配静默失效（成分是 67 项的长列表，最容易截断），
    而静默截断本来就是这一带最难发现的一类错。默认 False 保持老调用方的裸列表契约。
    """
    await _park_ghost_dropdowns(session)
    await _press_escape(session)
    r = await _open_attr_dropdown(session, label)
    if not r.get("opened"):
        return []
    await asyncio.sleep(0.5)
    js = r"""(async () => {
      const sleep = ms => new Promise(res => setTimeout(res, ms));
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      const rowY = row ? row.getBoundingClientRect().top : null;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({open: false, options: []});
      if (drops.length > 1 && rowY !== null)
        drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const target = drops[0].d;
      const holder = target.querySelector('.rc-virtual-list-holder');
      const seen = [];
      const collect = () => Array.from(target.querySelectorAll('.ant-select-item-option-content'))
        .forEach(o => { const t = (o.textContent || '').trim();
          if (t && !seen.includes(t)) seen.push(t); });
      if (!holder) {           // 短列表没有虚拟滚动容器，直接收
        collect();
        return JSON.stringify({open: true, options: seen, virtual: false});
      }
      // 【滚动参数是实测值，别下调】2026-08-19 实测：上装成分共 67 项、
      // scrollHeight=1704 / clientHeight=256（可视约 10 条）。每屏等 180ms 才能
      // 稳定采到新渲染的条目；原脚本的 120ms 在 Playwright 直连下太短——
      // WebBridge 时代每次 evaluate 的往返开销间接补足了等待，换成直连就暴露了，
      // 表现为只读到首屏 10 条（静默截断，最难发现的一类错）。
      // 终止条件用「滚到底」而非「scrollTop 不再变」：后者在 smooth-scroll 或
      // 惯性未结束时会提前判定到底。
      holder.scrollTop = 0;
      await sleep(200);
      collect();
      for (let k = 0; k < 40; k++) {
        holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
        await sleep(180);
        collect();
        if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
      }
      holder.scrollTop = holder.scrollHeight;   // 末屏可能不足一屏高，补一次
      await sleep(250);
      collect();
      return JSON.stringify({open: true, options: seen, virtual: true,
                             visibleDrops: drops.length,
                             scrollHeight: holder.scrollHeight,
                             clientHeight: holder.clientHeight,
                             // 滚到底了才算读全（没滚到底说明 40 次循环用完还没走完，
                             // 那份清单是截断的，不能进缓存）
                             scrolledToEnd: holder.scrollTop + holder.clientHeight
                                            >= holder.scrollHeight - 2});
    })()""".replace("__LABEL__", J(label))
    res = await session.eval_json(js)
    await _press_escape(session)
    await asyncio.sleep(0.2)
    await _park_ghost_dropdowns(session)
    opts = res.get("options", []) if res.get("open") else []
    if not with_meta:
        return opts
    # 非虚拟列表（短列表）一次收全，天然完整；虚拟列表要真滚到底才算完整
    complete = bool(opts) and (not res.get("virtual") or res.get("scrolledToEnd"))
    return opts, {"virtual": bool(res.get("virtual")),
                  "scrollHeight": res.get("scrollHeight"),
                  "scrolledToEnd": bool(res.get("scrolledToEnd")),
                  "complete": complete}


async def dump_attrs(session: BrowserSession, skip_options: bool = False,
                     required_only: bool = True,
                     cat_path=None, use_cache: bool = True, site: str = "") -> dict:
    """阶段④(只读)：导出产品属性当前值 + 每项下拉的真实选项。

    不导航——必须紧跟 auto_cat 在同一页执行：类目决定了有哪些属性行，换页就得重选。

    required_only=True（默认）只读【必填项】的选项：非必填项一律不填（见 _ATTR_PROMPT
    规则4「填得多错得多」），拿到它们的选项也用不上，而每项都要点开下拉+滚动虚拟列表
    收集，读全部 33 项要 3-5 分钟、只读必填项能省掉近一半。非必填项仍返回当前值，
    只是 options 为空并标 optionsEmptyReason=optional-skipped。
    要看全部选项（排查/探查场景）传 required_only=False。

    【2026-08-24 撤掉「源商品给了值的非必填行也读」这个例外】原先按源属性键与表单行名
    做双向子串包含匹配（_labels_matching_src），给匹配上的非必填行也读 options 交给 LLM
    填。撤掉的原因是那个匹配偏松，实测把语义无关的行也开了口子：源「货源类型」命中表单
    「类型」（那是夹克款式）、源「主面料成分/面料工艺」命中表单「面料」（那是弹力档位）；
    且源键本身存在解析粘连的长串（「是否跨境出口专供货源…主要下游平台ebay,亚马逊」），
    短行名被长键包含就命中，误开口子的面还会继续扩大。现在的规则回到单一口径：
    非必填一律留空，源商品写了也不填。

    skip_options=True 时一个下拉都不点，只读当前值——最快，但没有 options 就不能喂给
    LLM 做修改建议，只能看现状。

    cat_path（类目路径列表）+ use_cache 命中时，options 从缓存注入、跳过该行的「点开
    下拉 + 滚虚拟列表」，那正是本阶段 92.9s 的主体。cat_path 给不出来（续跑、
    publish_inspect 单跑）时为 None → 当未命中 → 全量现场读，行为与加缓存前完全一致。
    注意【缓存只补 options，永不造行】：行集、current、required、visible 一律从活页面
    读——required 是 _validate_attr_changes 第 1 道闸的依据，用缓存里的旧值等于拿旧
    策略判新表单。

    隐藏行（依赖字段未触发、行仍 display:none）跳过并标 optionsEmptyReason=row-hidden，
    不强行点开——那种行点不开是正常的，不是故障。
    """
    exp = await _expand_attr_section(session)
    if exp.get("expanded"):
        await asyncio.sleep(1.2)  # 等折叠区展开动画结束，否则新出现的行还量不到高度
    # 属性行是懒渲染：open_edit 只等到 skuDataInfo 出现，跟在后面立刻读经常
    # 读到 0 行（2026-08-21 实测：续跑补开编辑页后 2.5s 就判空失败）。
    # 故这里轮询等到行出来再判，超时返回的最后一次结果交给下方空行分支。
    # 【展开与轮询都不能因命中缓存而跳过】命中省掉的只是逐行读选项，行本身仍要渲染出来。
    rows = await session.wait_for(
        _JS_LIST_ATTR_ROWS,
        lambda d: d.get("found") and bool(d.get("attrs")),
        timeout=20, interval=1.5)
    if not rows.get("found"):
        raise RuntimeError("productBasicInfo 未找到（当前页不是编辑页？）")
    attrs = [a for a in rows.get("attrs", []) if a["label"] != "产品属性"]  # 分组标题行
    if skip_options:
        # 这条路径语义是「一个下拉都不点、只看现状」，故【不注入缓存】：注入了会返回
        # optionsRead=False 却带着 options，把它的返回契约打乱。
        return {"status": "ok", "count": len(attrs), "attrs": attrs,
                "optionsRead": False}
    n_required = sum(1 for a in attrs if a.get("required"))
    # 同类目的 options 清单不随商品变，能从缓存拿就不必逐行点开下拉
    cached = (cache.load_attr_options(cat_path[-1], cat_path, site)
              if use_cache and cat_path else {})
    logger.info(f"属性行 {len(attrs)} 条（必填 {n_required}），"
                + (f"缓存 {len(cached)} 行可用，" if cached else "")
                + "逐个读必填项下拉选项…")
    active_read = 0
    skipped_optional = 0
    cache_read = 0
    for a in attrs:
        a["options"] = []
        if a.get("visible") is False:
            a["optionsEmptyReason"] = "row-hidden"
            continue
        if required_only and not a.get("required"):
            a["optionsEmptyReason"] = "optional-skipped"
            skipped_optional += 1
            continue
        # 【数值行不读选项】它行内那个 select 是【只读的单位】（克重行是 'g/㎡'），
        # 点开读到的是单位清单、不是可选值。读了有三重害处：白花一次开合下拉、
        # 一份 ['g/㎡'] 落进缓存（缓存文件里的「面料克重1（g/m²) → ['g/㎡']」就是
        # 这么来的），以及让 _validate_attr_changes 里「数值行 options 恒为空」这个
        # 前提失效——那道量级闸靠 kind 分流，前提失效不至于出错，但缓存脏了会一直脏。
        if a.get("kind") == "number":
            a["optionsEmptyReason"] = "number-row"
            continue
        # 【这个 guard 必须在上面两个 skip 之后，顺序不能调】_validate_attr_changes 的
        # 第 2 个分支靠「非必填 + 未填 + options 空」判定「按策略留空」；提前注入会给
        # 那些行填上 options，把这条既定策略打穿（填得多错得多）。
        hit = cached.get(a["label"])
        if hit:
            a["options"] = hit
            a["optionsFrom"] = "cache"   # 写入失败后要不要重读该行，就看这个标记
            cache_read += 1
            continue
        opts, meta = await _read_active_options(session, a["label"], with_meta=True)
        logger.info(f"读选项：{a['label']} -> {len(opts)} 个"
                    + ("" if meta["complete"] else "（未滚到底，不进缓存）"))
        if opts:
            a["options"] = opts
            a["optionsFrom"] = "live"
            # 只有确认读全的清单才允许落盘：截断的清单进了缓存，之后每个同类目商品
            # 都会拿一份缺项的 options 去做校验与纤维匹配，且没人会发现
            a["optionsComplete"] = meta["complete"]
            active_read += 1
        else:
            a["optionsEmptyReason"] = "open-failed"
        await asyncio.sleep(0.3)
    parked = await _park_ghost_dropdowns(session)
    # 现场读到的行写回缓存（按 label 并集合并，见 cache.save_attr_options）
    if use_cache and cat_path and active_read:
        cache.save_attr_options(cat_path[-1], cat_path, attrs, site)
    return {"status": "ok", "count": len(attrs), "attrs": attrs,
            "optionsRead": True, "requiredOnly": required_only,
            "activeRead": active_read, "cacheRead": cache_read,
            "skippedOptional": skipped_optional,
            "parkedGhosts": parked.get("parked", 0)}


# ---- 阶段④ 属性写入 ---------------------------------------------------------

async def _click_dropdown_option(session: BrowserSession, label: str,
                                 value: str) -> dict:
    """在【目标行附近唯一可见浮层】里点选项。

    绝不扫全页浮层（坑1）：上装成分/下装成分/材质/辅料成分共用同一份 67 项纤维列表，
    按文本匹配会在多个浮层里全命中；而点在隐藏浮层上事件照样生效，结果改掉别的字段。
    找不到目标选项时返回 option-not-rendered，交 _scroll_click_option 滚动去找。
    """
    js = r"""(() => {
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (!row) return JSON.stringify({clicked: false, reason: 'row-not-found'});
      row.scrollIntoView({block: 'center'});
      const rowY = row.getBoundingClientRect().top;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({clicked: false, reason: 'no-visible-dropdown'});
      drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      if (Math.abs(drops[0].r.top - rowY) > 600)
        return JSON.stringify({clicked: false, reason: 'dropdown-too-far'});
      const opt = Array.from(drops[0].d.querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent || '').trim() === __VALUE__);
      if (!opt) return JSON.stringify({clicked: false, reason: 'option-not-rendered',
        value: __VALUE__});
      opt.click();
      return JSON.stringify({clicked: true, value: __VALUE__});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))
    return await session.eval_json(js)


async def _scroll_click_option(session: BrowserSession, label: str,
                               value: str) -> dict:
    """滚动虚拟列表找到目标选项渲染出来后点击（选项在可视窗口外时用）。

    滚动等待同 _read_active_options 的 180ms（见那里的注释）：原脚本 120ms 在
    Playwright 直连下不够，会滚过目标却没采到，误报 option-not-rendered。
    """
    js = r"""(async () => {
      const sleep = ms => new Promise(res => setTimeout(res, ms));
      const row = Array.from(document.querySelectorAll('.ant-form-item[data-attr-label]'))
          .find(el => el.getAttribute('data-attr-label') === __LABEL__)
        || Array.from(document.querySelectorAll('.ant-form-item'))
          .find(el => { const l = el.querySelector('.ant-form-item-label label');
            return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
      if (row) row.scrollIntoView({block: 'center'});
      const rowY = row ? row.getBoundingClientRect().top : null;
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .map(d => ({d, r: d.getBoundingClientRect()}))
        .filter(x => x.r.top > -1000 && x.r.width > 50);
      if (!drops.length) return JSON.stringify({clicked: false, reason: 'no-visible-dropdown'});
      if (drops.length > 1 && rowY !== null)
        drops.sort((a, b) => Math.abs(a.r.top - rowY) - Math.abs(b.r.top - rowY));
      const target = drops[0].d;
      const hitNow = () => Array.from(target.querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent || '').trim() === __VALUE__);
      const holder = target.querySelector('.rc-virtual-list-holder');
      if (!holder) {
        const h = hitNow();
        if (h) { h.click(); return JSON.stringify({clicked: true, value: __VALUE__}); }
        return JSON.stringify({clicked: false, reason: 'option-not-rendered', value: __VALUE__});
      }
      holder.scrollTop = 0;
      await sleep(200);
      for (let k = 0; k < 60; k++) {
        const hit = hitNow();
        if (hit) { hit.click(); return JSON.stringify({clicked: true, value: __VALUE__, scrolled: k}); }
        if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
        holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
        await sleep(180);
      }
      const last = hitNow();
      if (last) { last.click(); return JSON.stringify({clicked: true, value: __VALUE__, atEnd: true}); }
      return JSON.stringify({clicked: false, reason: 'option-not-rendered', value: __VALUE__});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))
    return await session.eval_json(js)


async def _readback_attr(session: BrowserSession, label: str, row_no: int = 1) -> Optional[dict]:
    """回读某属性行第 row_no 个下拉的当前值（1 起）。"""
    if row_no <= 1:
        rows = await session.eval_json(_JS_LIST_ATTR_ROWS)
        return next((a for a in rows.get("attrs", []) if a["label"] == label), None)
    js = r"""(() => {
      const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          if (!l) return false;
          const sp = l.querySelector('.attr-label');
          const name = sp ? (sp.textContent||'').trim()
                          : (l.getAttribute('title')||l.textContent||'').trim();
          return name === __LABEL__; });
      if (!it) return JSON.stringify(null);
      const sel = Array.from(it.querySelectorAll('.ant-select'))[__IDX__];
      const c = sel ? sel.querySelector('.ant-select-selection-item') : null;
      const inp = Array.from(it.querySelectorAll('input'))
        .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
      return JSON.stringify({label: __LABEL__,
        current: c ? (c.textContent || '').trim() : null,
        numValues: inp && inp.value !== '' ? [inp.value] : []});
    })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(row_no - 1))
    return await session.eval_json(js)


async def _ensure_comp_rows(session: BrowserSession, label: str, row_no: int) -> dict:
    """成分类字段行数不够时点 .icon_add 加行，直到有 row_no 个下拉。"""
    js = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          if (!l) return false;
          const sp = l.querySelector('.attr-label');
          const name = sp ? (sp.textContent||'').trim()
                          : (l.getAttribute('title')||l.textContent||'').trim();
          return name === __LABEL__; });
      if (!it) return JSON.stringify({err: 'row-not-found'});
      let n = it.querySelectorAll('.ant-select').length;
      let guard = 0;
      while (n < __ROWNO__ && guard++ < 10) {
        const add = it.querySelector('.icon_add');
        if (!add) return JSON.stringify({err: 'no-add-btn', rows: n});
        add.click();
        await sleep(1200);
        n = it.querySelectorAll('.ant-select').length;
      }
      return JSON.stringify({rows: n});
    })()""".replace("__LABEL__", J(label)).replace("__ROWNO__", str(row_no))
    return await session.eval_json(js)


# 往【动态属性行】的数值输入框里直填（里料克重 g/m² 这类没有下拉的必填项）。
#
# 行定位先用 data-attr-label（_JS_LIST_ATTR_ROWS 枚举时打上的），【但必须带 label
# 文本兜底】：同一批补填里先写的下拉行会触发联动重渲染，排在后面的行若节点被 Vue
# 重建，那个手工打的属性就随旧节点一起没了，只认属性等于报 attr-row-not-found、
# 该行永远填不上。下拉侧的 5 处定位一直是「属性 + 文本」双路（见
# _visible_dropdown_near 等），数值侧原先漏了这层，多轮联动补填会放大它。
# 找到后顺手补打标记，后续回读轮询才不必每轮都走兜底分支。
#
# 占位符走 J() 转义（自带引号）而非裸文本：属性名里有「（g/m²)」这类字符，裸拼进
# JS 字符串字面量一旦出现引号或反斜杠就破语法。
#
# 【属性名绝不拼进 CSS 选择器】取行只枚举 [data-attr-label] 再按 getAttribute 比对，
# 不写成 [data-attr-label=<名字>]：2026-08-25 实测「里料克重（g/m²)」拼出的选择器带
# 全角括号，querySelector 直接抛 SyntaxError，阶段④整个异常、商品未落库。给属性值补
# 引号只是把红线推远——名字里真出现 " 照样抛，出现 \ 则静默 miss 白走一趟兜底；而属性
# 名是平台下发的，我们控制不了。故下拉侧那 5 处定位也一并收敛成同一套比对方式。
#
# 【必须派发 input 事件】Vue 只认事件，直接赋 value 保存时会丢（与 _js_fill_by_label
# 同一个坑）。回读带轮询：填克重可能触发联动重渲染，读太早拿到旧值是假失败。
_JS_SET_ATTR_NUM = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const LB = __LABELQ__;
  // 取行：先认标记，丢了就按 label 文本重找（动态属性行的名字在内层 .attr-label）
  const pick = () => {
    const byMark = Array.from(
      document.querySelectorAll('.ant-form-item[data-attr-label]'))
      .find(el => el.getAttribute('data-attr-label') === LB);
    if (byMark) return byMark;
    const hit = Array.from(
      document.querySelectorAll('#productBasicInfo .ant-form-item')).find(el => {
        const l = el.querySelector('.ant-form-item-label label');
        if (!l) return false;
        const sp = l.querySelector('.attr-label');
        const name = sp ? (sp.textContent || '').trim()
                        : (l.getAttribute('title') || l.textContent || '').trim();
        return name === LB; });
    if (hit) hit.setAttribute('data-attr-label', LB);
    return hit || null;
  };
  const it = pick();
  if (!it) return JSON.stringify({status: 'error', reason: 'attr-row-not-found'});
  const inp = Array.from(it.querySelectorAll('input')).find(i =>
    i.type !== 'hidden' && i.type !== 'radio' && i.type !== 'checkbox'
    && !(i.className || '').includes('ant-select'));
  if (!inp) return JSON.stringify({status: 'error', reason: 'input-not-found'});
  inp.scrollIntoView({block: 'center'});
  await sleep(300);
  const before = inp.value;
  const desc = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value');
  desc.set.call(inp, __VALUEQ__);
  inp.dispatchEvent(new Event('input', {bubbles: true}));
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  inp.dispatchEvent(new Event('blur', {bubbles: true}));   // 有些字段 blur 才校验
  // 回读轮询等稳定（联动重渲染会换掉 input 实例，故每轮重新查）
  for (let i = 0; i < 8; i++) {
    await sleep(400);
    const now = pick();
    const cur = now && Array.from(now.querySelectorAll('input')).find(x =>
      x.type !== 'hidden' && x.type !== 'radio' && x.type !== 'checkbox'
      && !(x.className || '').includes('ant-select'));
    if (cur && String(cur.value).trim() === __VALUEQ__) {
      return JSON.stringify({status: 'ok', before: before, readback: cur.value});
    }
  }
  const last = pick();
  const lastInp = last && last.querySelector('input');
  return JSON.stringify({status: 'error', reason: 'readback-mismatch',
    before: before, readback: lastInp ? lastInp.value : null});
})()"""


def _readback_current(r: dict):
    """从 set_attr 的返回里取回读值，形状不认识就返回 None。

    【为什么要这层】readback 只是写给人看的诊断字段（记进 applied 供 UI 展示），
    却因为「下拉行给字典、数值行给字符串」的形状差异让阶段④整个抛异常、商品未落库
    （2026-08-25）。形状已在 set_attr 里统一，这里再兜一道：诊断字段绝不该有能力
    中断主流程——判成功失败看的是 status，不是这个值。
    """
    back = r.get("readback")
    if isinstance(back, dict):
        return back.get("current")
    return back if isinstance(back, str) else None


async def set_attr(session: BrowserSession, label: str, value: str,
                   num: Optional[int] = None, row_no: int = 1,
                   kind: str = "select") -> dict:
    """把指定属性改选为指定选项文本，回读校验。不导航（须在编辑页且类目已选）。

    num：成分类复合字段的百分比数值。row_no（1 起）：成分字段的第 N 个成分行，
    行不够会自动点 .icon_add 加行。
    kind："select"（默认，开下拉点选项）或 "number"（纯数值输入框，如里料克重
    g/m²，直接写 input）。数值行没有下拉可开，走同一套流程会必然失败。

    可靠性设计（原脚本 2026-08-17 排查结论，原样保留）：
    改里料纹理等字段会触发表单动态增删行重渲染，固定等待的单次回读会读到旧值/空行
    造成【假失败】；点击也可能落在重渲染前的游离 DOM 上造成【真失败】。因此
    回读改成轮询等值稳定（最多约 4s），且回读不符时「打开→点选→回读」整流程自愈
    重试一次。
    """
    # 【数值输入行走独立分支】里料克重这类没有下拉可开，下面「开下拉→点选项」
    # 整套流程都不适用。判据由调用方按 dump_attrs 枚举出的 kind 传入。
    if kind == "number":
        r = await session.eval_json(
            _JS_SET_ATTR_NUM.replace("__LABELQ__", J(label))
                            .replace("__VALUEQ__", J(str(value))))
        # 【readback 必须与下拉分支同形状】上面那段 JS 回读的是 input.value（字符串），
        # 而下拉分支给的是 _readback_attr 的字典，调用方（_apply_attr_changes、
        # _refresh_row_and_retry）一律按 r["readback"]["current"] 取值。原样透出字符串
        # 会把阶段④整个炸掉：2026-08-25 联动补填写「里料克重（g/m²)」实测
        # AttributeError: 'str' object has no attribute 'get'，商品未落库。
        # 包成 current 不是硬凑：_JS_LIST_ATTR_ROWS 对 kind=number 的 current 读的
        # 就是输入框值，两边语义本来一致。error 分支同样要归一——那条路径也带 readback。
        back = r.get("readback")
        rb = {"label": label, "current": None if back is None else str(back)}
        if r.get("status") == "ok":
            return {"status": "ok", "label": label, "value": value,
                    "kind": "number", "readback": rb}
        return {"status": "error", "label": label, "value": value,
                "kind": "number", **r, "readback": rb}

    if row_no > 1:
        add = await _ensure_comp_rows(session, label, row_no)
        if add.get("err"):
            return {"status": "error", "stage": "add-row", **add}
        await asyncio.sleep(0.5)

    clicked: dict = {}
    cur: Optional[dict] = None
    for attempt in (1, 2):
        await _open_attr_dropdown(session, label, sel_idx=row_no - 1)
        await asyncio.sleep(0.6)
        clicked = await _click_dropdown_option(session, label, value)
        if not clicked.get("clicked"):
            # 目标选项可能在虚拟列表可视窗口之外，滚动去找
            clicked = await _scroll_click_option(session, label, value)
        await asyncio.sleep(0.8)
        if not clicked.get("clicked"):
            await _park_ghost_dropdowns(session)  # 点开未点中也要清，别留浮层
            continue
        for _ in range(8):  # 回读轮询，等重渲染稳定到目标值
            cur = await _readback_attr(session, label, row_no)
            if cur and cur.get("current") == value:
                break
            await asyncio.sleep(0.5)
        if cur and cur.get("current") == value:
            break
        logger.warning(f"{label} 第{attempt}次设为「{value}」后回读不符，重试")

    if not clicked.get("clicked"):
        return {"status": "error", "label": label, "value": value, **clicked}

    result: dict = {"status": "ok", "label": label, "value": value}
    if num is not None:
        await asyncio.sleep(0.5)
        js = r"""(() => {
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => {
              const l = el.querySelector('.ant-form-item-label label');
              if (!l) return false;
              const sp = l.querySelector('.attr-label');
              const name = sp ? (sp.textContent||'').trim()
                              : (l.getAttribute('title')||l.textContent||'').trim();
              return name === __LABEL__; });
          if (!it) return JSON.stringify({filled: false, reason: 'row-not-found'});
          const inp = Array.from(it.querySelectorAll('input'))
            .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
          if (!inp) return JSON.stringify({filled: false, reason: 'no-num-input'});
          const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
          setter.call(inp, __NUM__);
          inp.dispatchEvent(new Event('input', {bubbles: true}));
          inp.dispatchEvent(new Event('change', {bubbles: true}));
          inp.dispatchEvent(new Event('blur', {bubbles: true}));
          return JSON.stringify({filled: true, readback: inp.value});
        })()""".replace("__LABEL__", J(label)).replace("__NUM__", J(str(num))) \
            .replace("__IDX__", str(row_no - 1))
        result["num"] = await session.eval_json(js)

    # 最终回读（填数值也可能触发重渲染，再轮询一次）
    for _ in range(6):
        cur = await _readback_attr(session, label, row_no)
        if cur and cur.get("current") == value:
            break
        await asyncio.sleep(0.5)
    result["readback"] = cur
    result["status"] = "ok" if cur and cur.get("current") == value else "error"
    parked = await _park_ghost_dropdowns(session)
    result["parkedGhosts"] = parked.get("parked", 0)
    return result


async def _set_select_by_label(session: BrowserSession, label: str, value: str,
                                row_no: int = 1) -> dict:
    """填写单个下拉属性（支持成分行扩行、虚拟列表滚动、回读轮询、自愈重试）。

    row_no=1 时操作表单第一行（默认），row_no>1 时先点 .icon_add 加行到够数再操作对应行。
    回读改为轮询等值稳定（最多 ~4s），回读不符时「打开→点选→回读」整流程自愈重试一次。
    """
    def _js_readback():
        if row_no <= 1:
            # 普通属性行：用现成的列表接口读
            return """(() => {
              const rows = JSON.parse((""" + _JS_LIST_ATTR_ROWS + """)());
              const hit = rows.attrs.find(a => a.label === __LABEL__);
              return JSON.stringify(hit || null);
            })()""".replace("__LABEL__", J(label))
        # 成分第 N 行：用选择器定位该 form-item 下第 N-1 个下拉
        return r"""(() => {
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => { const l = el.querySelector('.ant-form-item-label label');
              return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
          if (!it) return JSON.stringify(null);
          const sel = Array.from(it.querySelectorAll('.ant-select'))[__IDX__];
          const c = sel ? sel.querySelector('.ant-select-selection-item') : null;
          const inp = Array.from(it.querySelectorAll('input'))
            .filter(i => i.type !== 'hidden' && !i.className.includes('ant-select'))[__IDX__];
          return JSON.stringify({label: __LABEL__,
            current: c ? (c.textContent || '').trim() : null,
            numValues: inp && inp.value !== '' ? [inp.value] : []});
        })()""".replace("__LABEL__", J(label)).replace("__IDX__", str(row_no - 1))

    # 成分第 N 行：行不够先点 .icon_add 加行
    if row_no > 1:
        js_add = r"""(async () => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const it = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item'))
            .find(el => { const l = el.querySelector('.ant-form-item-label label');
              return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__; });
          if (!it) return JSON.stringify({err: 'row-not-found'});
          let n = it.querySelectorAll('.ant-select').length;
          while (n < __ROWNO__) {
            const add = it.querySelector('.icon_add');
            if (!add) return JSON.stringify({err: 'no-add-btn', rows: n});
            add.click();
            await sleep(1200);
            n = it.querySelectorAll('.ant-select').length;
          }
          return JSON.stringify({rows: n});
        })()""".replace("__LABEL__", J(label)).replace("__ROWNO__", str(row_no))
        add = await session.eval_json(js_add)
        if add.get("err"):
            return {"status": "error", "stage": "add-row", **add}
        await asyncio.sleep(0.5)

    cur = None
    for attempt in (1, 2):
        # 打开下拉
        await _open_attr_dropdown(session, label, sel_idx=row_no - 1)
        await asyncio.sleep(0.6)
        # 点选项
        c = await _click_dropdown_option(session, label, value)
        if not c.get("clicked"):
            # 目标选项可能在虚拟列表可视窗口之外：滚动该行的下拉找到后再点
            c = await _scroll_click_option(session, label, value)
        await asyncio.sleep(0.8)
        if not c.get("clicked"):
            await _park_ghost_dropdowns(session)  # 点开未点中也清一次，避免残留浮层
            continue
        # 回读轮询，等重渲染稳定到目标值
        for _ in range(8):
            cur = await session.eval_json(_js_readback())
            if cur and cur.get("current") == value:
                break
            await asyncio.sleep(0.5)
        if cur and cur.get("current") == value:
            break  # 成功，退出重试循环
    await _park_ghost_dropdowns(session)
    return {"status": "ok" if (cur and cur.get("current") == value) else "error",
            "label": label, "value": value, "rowNo": row_no, "readback": cur}


def _js_fill_by_label(label: str, value: str) -> str:
    """按 label 填 input/textarea（产品标题/英文标题等文本框）。返回 JS 代码字符串。"""
    return r"""(() => {
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === __LABEL__;
        });
      if (!it) return JSON.stringify({filled: false, reason: 'item-not-found'});
      const inp = it.querySelector('input:not([type=hidden]), textarea');
      if (!inp) return JSON.stringify({filled: false, reason: 'no-input'});
      const setter = Object.getOwnPropertyDescriptor(
        inp.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype,
        'value').set;
      setter.call(inp, __VALUE__);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return JSON.stringify({filled: true, readback: inp.value});
    })()""".replace("__LABEL__", J(label)).replace("__VALUE__", J(value))


async def _set_origin(session: BrowserSession, country: str, province: str) -> dict:
    """填写产地（两级下拉：国家 + 省份）。产地字段有两个并列的 .ant-select。"""
    # 第一步：选国家
    js1 = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === '产地';
        });
      if (!it) return JSON.stringify({status: 'error', reason: 'item-not-found'});
      const selectors = it.querySelectorAll('.ant-select-selector');
      if (selectors.length < 1) return JSON.stringify({status: 'error', reason: 'no-select'});
      const sel1 = selectors[0];
      ['mousedown','mouseup','click'].forEach(t =>
        sel1.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      await sleep(600);
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .filter(d => d.offsetHeight > 0);
      if (!drops.length) return JSON.stringify({status: 'error', reason: 'dropdown-not-open'});
      const opt = Array.from(drops[drops.length - 1].querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent||'').trim() === __COUNTRY__);
      if (!opt) return JSON.stringify({status: 'error', reason: 'country-not-found'});
      opt.click();
      await sleep(800);
      const readback1 = it.querySelectorAll('.ant-select-selection-item')[0];
      return JSON.stringify({status: 'ok', country: readback1 ? readback1.textContent.trim() : null});
    })()""".replace("__COUNTRY__", J(country))
    r1 = await session.eval_json(js1)
    if r1.get("status") != "ok":
        return {"status": "error", "step": "country", **r1}

    # 第二步：等省份下拉出现，选省份
    await asyncio.sleep(0.8)
    js2 = r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const it = Array.from(document.querySelectorAll('.ant-form-item'))
        .find(el => {
          const l = el.querySelector('.ant-form-item-label label');
          return l && (l.getAttribute('title')||l.textContent||'').trim() === '产地';
        });
      const selectors = it.querySelectorAll('.ant-select-selector');
      if (selectors.length < 2) return JSON.stringify({status: 'error', reason: 'province-select-not-appeared'});
      const sel2 = selectors[1];
      ['mousedown','mouseup','click'].forEach(t =>
        sel2.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
      await sleep(600);
      const drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
        .filter(d => d.offsetHeight > 0);
      if (!drops.length) return JSON.stringify({status: 'error', reason: 'dropdown-not-open'});
      const opt = Array.from(drops[drops.length - 1].querySelectorAll('.ant-select-item-option'))
        .find(o => (o.textContent||'').trim() === __PROVINCE__);
      if (!opt) return JSON.stringify({status: 'error', reason: 'province-not-found'});
      opt.click();
      await sleep(400);
      const readback2 = it.querySelectorAll('.ant-select-selection-item')[1];
      return JSON.stringify({status: 'ok', province: readback2 ? readback2.textContent.trim() : null});
    })()""".replace("__PROVINCE__", J(province))
    r2 = await session.eval_json(js2)
    if r2.get("status") != "ok":
        return {"status": "error", "step": "province", "country": r1, **r2}

    return {"status": "ok", "country": r1.get("country"), "province": r2.get("province")}


def _strip_dated(text: str) -> str:
    """剥掉标题里的年份与「新款/新品」这类时效词。

    1688 源标题惯用「2026新款冬季…」，年份一旦进了 Temu 标题或尺码表模板名，
    次年就成了过期信息（商品生命周期跨年，标题却停在去年），用户 2026-08-24 明确
    要求去掉。这里只删年份与紧随的新款/新品/上新，不动季节词（冬季/加厚是真实卖点）。
    """
    text = str(text or "")
    # 「2026新款」「2026年新品」「20 春夏新款」等：年份 + 可选「年」+ 可选新款词
    text = re.sub(r"(19|20)\d{2}\s*年?\s*(新款|新品|上新)?", "", text)
    # 年份删掉后可能剩下光秃秃的「新款」，一并去掉
    text = re.sub(r"(新款|新品|上新)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# 明显不是品牌的词：1688 的「品牌」属性经常被卖家填成品类/功能描述（实测
# offer 971877978455 填的是「无功能保暖」），源标题开头也常是「儿童秋冬季…」这类
# 品类词。这些一旦进了品牌违禁词表，任何正常重写的中文标题都会被判「含品牌词」，
# 阶段⑤ 会连着两次重试全废（表现为 title-generation-failed，且看不出为什么）。
_NON_BRAND_WORDS = (
    "品牌", "其他", "其它", "无", "功能", "保暖", "加厚", "加绒", "抗菌", "德绒",
    "徳绒", "纯棉", "棉", "套装", "两件套", "三件套", "家居服", "睡衣", "秋衣",
    "秋裤", "内衣", "童装", "儿童", "宝宝", "男童", "女童", "男女", "通用",
    "春", "夏", "秋", "冬", "季", "款", "新", "版", "自有", "工厂", "代工",
)


def _brand_words(brand: str, src_title: str) -> list:
    """算出真正该拦的品牌违禁词。

    【为什么不再切源标题前 3 字】原实现把 `src_title[:3]` 无条件当品牌词。中文源标题
    开头绝大多数是品类+季节（「儿童秋」「女童冬」），这等于禁掉了中文标题必须出现的
    核心品类词——LLM 无论怎么重写都过不了闸，阶段⑤ 必然失败。改为只认源标题开头的
    拉丁字母商标 token（真商标才会用英文打头，如「MODAL 儿童…」），中文开头一律不猜。

    品牌属性值同样要过滤：剔掉 _NON_BRAND_WORDS 后若不剩 2 个字符以上的实词，就说明
    它是描述而不是品牌（「无功能保暖」→「无」→ 丢弃），不进违禁词表。
    """
    words = []
    b = (brand or "").strip()
    if b:
        core = b
        for w in _NON_BRAND_WORDS:
            core = core.replace(w, "")
        core = re.sub(r"[\s　（）()【】\[\]/、,，.。-]", "", core)
        if len(core) >= 2:
            words.append(b)
    m = re.match(r"[A-Za-z][A-Za-z0-9&.\-]{1,}", (src_title or "").strip())
    if m and len(m.group(0)) >= 3:
        words.append(m.group(0))
    return words


async def set_titles(session: BrowserSession, info_path: str) -> dict:
    """阶段⑤：LLM 生成中英文标题并填写；产地一律填广东省。

    标题生成规则（实测沉淀）：
      - 英文标题 40-70 字符（确保手机端完整显示），纯 ASCII（禁 emoji/特殊符号）
      - 中文标题重写（不照抄源标题），突出卖点，≤60 字
      - 品牌红线：属性里的品牌值（过滤掉「无功能保暖」这类描述性假品牌）
        + 源标题开头的英文商标 token，见 _brand_words
      - 多候选兜底：LLM 一次生成 10 个英文标题，按推荐序取第一个合规的
    生成失败时最多重试一次（喂上次不合格的原因）；两次都不合格才报错。
    """
    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    src_attrs = json.dumps(info.get("attributes", {}), ensure_ascii=False)
    img_sum = json.dumps(info.get("imageUnderstanding", {}), ensure_ascii=False)
    skus_sum = json.dumps(info.get("skus", {}), ensure_ascii=False)[:600]
    brand = (info.get("attributes") or {}).get("品牌", "")
    src_title = (info.get("title") or "").strip()
    # 品牌违禁词：过滤后的品牌属性值 + 源标题开头的英文商标（见 _brand_words 说明，
    # 不再无条件切前 3 字——那会把「儿童秋」这类品类词当品牌拦掉）
    forbidden = _brand_words(brand, src_title)
    if forbidden:
        logger.info(f"标题品牌违禁词：{forbidden}")

    def _has_brand(text: str) -> bool:
        low = text.lower()
        return any(f.lower() in low for f in forbidden)

    def _has_year(text: str) -> bool:
        """年份硬闸：提示词是软约束，模型照抄源标题里的「2026」是常态，
        必须在校验层拦。四位年份按 19xx/20xx 匹配；两位年份只认紧跟
        季节/品类词的形式（如 26FW/25AW），避免误伤尺码 24/26 这类数字。"""
        return bool(re.search(r"(19|20)\d{2}", text)
                    or re.search(r"\b\d{2}(FW|AW|SS|SP)\b", text, re.I)
                    or re.search(r"New Arrival|Latest|This Year", text, re.I))

    def _has_price_claim(text: str) -> bool:
        """价格/优惠宣称硬闸——虚假宣传与平台风控的高发项。

        阶段⑤（本函数）跑在阶段⑩定价之前，此刻真实售价还不存在：
        LLM 只看到 1688 人民币源价，据此折算出的美元价必然与最终
        申报价（默认 188.88 ÷ 7 ≈ 27 USD，见 DECLARE_PRICE_DEFAULT）不符。
        实测 offer 855602801145 生成 "Under 10USD" 而真实约 24USD（当时申报价
        168 的口径），差 3 倍；换成 188.88 后差得更多。故价格一律不许进标题。
        运费/折扣同理：都由平台活动决定，不是标题能承诺的。
        """
        pats = (
            r"\$\s*\d",                     # $9.99 / $ 9
            r"\d+\s*(USD|usd|dollars?)",     # 10USD / 10 dollars
            r"\bunder\s*\d",                # Under 10
            r"\b(cheap|cheapest|budget|bargain|lowest|affordable)\b",
            r"\b(sale|discount|deal|clearance|promo|coupon)\b",
            r"\d+\s*%\s*off|\boff\b\s*\d+\s*%",
            r"\bfree\s*(shipping|delivery|gift)\b",
            r"\b(buy\s*\d+\s*get|bogo)\b",
        )
        return any(re.search(p, text, re.I) for p in pats)

    def _en_reject(en: str) -> Optional[str]:
        """英文标题不合格的具体原因；合格返回 None。

        【为什么要返回原因而不是布尔】两次重试都不合格时只能报
        title-generation-failed，日志里看不出到底是超长、含中文还是撞了品牌词——
        实测排查一次失败要去翻模型原始响应。返回原因后失败信息里直接带着，
        重试时也能把具体原因喂回模型（比笼统的「上次不合格」有效）。
        """
        if not en:
            return "空标题"
        if re.search(r"[一-鿿]", en):
            return "含中文字符"
        if not 40 <= len(en) <= 70:
            return f"长度 {len(en)} 不在 40-70"
        if not all(ord(ch) < 128 for ch in en):
            return "含非 ASCII 字符（emoji/特殊符号）"
        if _has_brand(en):
            return f"含品牌词 {forbidden}"
        if _has_year(en):
            return "含年份或时效词"
        if _has_price_claim(en):
            return "含价格/优惠宣称"
        return None

    def _zh_reject(zh: str) -> Optional[str]:
        """中文标题不合格的具体原因；合格返回 None。"""
        if not zh:
            return "空标题"
        if zh == src_title:
            return "照抄源标题"
        if _has_brand(zh):
            return f"含品牌词 {forbidden}"
        if _has_year(zh) or re.search(r"新款|新品|上新", zh):
            return "含年份或新款等时效词"
        if _has_price_claim(zh) or re.search(
                r"包邮|免邮|特价|清仓|折扣|秒杀|亏本|甩卖", zh):
            return "含价格/优惠宣称"
        return None

    # 【候选数 3 不是 10】2026-08-24 耗时实测：原先要 10 个候选、每个还带 logic 字段，
    # 推理模型为 10 个候选各推演一遍，一次烧掉 15896 completion token / 2 分 13 秒——
    # 占当次批次全部 completion 的 64%，是整条管线最慢的单点。而下面的挑选逻辑只取
    # 第一个合规的，其余 9 个基本是废品。降到 3 个仍留够「首选不合规还有备选」的余量
    # （_en_ok 卡 40-70 字符 + 禁中文/品牌词，单个候选不合规是常态，故不能只要 1 个）。
    prompt = (
        "你是深谙美国消费者心理和 Temu 平台流量算法的标题优化师，擅长用「超低价感知、场景化解忧、\n"
        "视觉冲击力」写高点击高转化的商品标题。请根据商品信息生成 3 个符合 Temu 规范的英文爆款标题，\n"
        "并选出最适合本商品的 1 个。\n"
        "核心写作规则（必须严格遵守）：\n"
        "1. 字符限制：每个标题严格控制在 40-70 个字符（含空格），确保手机端完整显示。\n"
        "2. 关键词布局：必须包含 [核心品类词] + [材质/功能属性] + [使用场景] + [人群]。\n"
        "3. 流量钩子（二选一或组合）：数量策略（Set of 2 / 2 Pcs / Value Pack）、\n"
        "   解决痛点（Must Have / Upgrade / No More...）。\n"
        "   【严禁任何价格与优惠表述】不许出现 Under $X / Cheap / Budget / Sale / Off /\n"
        "   Discount / Deal / Free Shipping 等：写标题时真实售价尚未确定（由后续申报价决定），\n"
        "   任何价格数字都是编的，属虚假价格宣称。\n"
        "4. 严禁使用 Emoji 和任何特殊符号，只允许英文字母、数字、空格、连字符。\n"
        "5. 禁用：严禁最高级（Best/Perfect 等）、严禁长难句、严禁中文字符。\n"
        "6. 严禁年份（2025/2026/25/26 等）与 New Arrival/Latest/This Year 这类时效表述——\n"
        "   商品生命周期跨年，年份次年即成过期信息。季节词（Winter/Fall）可保留。\n"
        "标题结构公式：\n"
        "A 视觉冲击型：[Emoji]+[形容词]+[核心产品]+[关键属性]+[场景]\n"
        "B 套装型：[数量套装]+[核心产品]+[卖点]+[材质/功能]+[人群]（不带价格）\n"
        "C 痛点解决型：[问题场景]+[产品方案]+[亮点]+[送礼属性]\n"
        "分析时必须结合商品实际信息（从下方商品标题、源商品参数、SKU 价格、图片理解摘要中提炼\n"
        "人群/品类/材质/特点/款式/季节/场景，禁止套用与本商品无关的模板词）。\n"
        f"品牌红线：品牌名严禁出现在任何标题中（本商品品牌为 {brand or '无'}，\n"
        f"源标题前几个字符也可能是品牌词，一律剔除）。\n"
        "中文标题要求：去掉品牌名，不得照抄源标题——重新组织关键词和语序，突出本商品实际卖点\n"
        "（从源参数/图片理解中提炼，如材质/套装件数/风格/季节/装饰元素等），通顺简洁≤60字；\n"
        "同样严禁年份和「新款/新品/上新」这类时效词（季节词可留）。\n"
        # 不要 logic 字段：它只进日志不进表单，却让模型为每个候选多写一段推演
        "只输出 JSON：{\"candidates\": [{\"enTitle\": \"...\"}...共3个],\n"
        "\"recommend\": <0-2 的序号，选最贴合商品且合规的>, \"title\": \"<重写后的中文标题>\"}\n\n"
        f"商品标题：{src_title}\n"
        f"源商品参数（1688）：{src_attrs}\n"
        f"SKU 价格（人民币）：{skus_sum}\n"
        f"图片理解摘要：{img_sum}"
    )
    titles, gen_err, last_reasons = None, None, ""
    for attempt in (1, 2):  # 英文标题含中文等不合格时重生成一次
        try:
            # 重试时喂上一轮的具体拒因（哪个候选因为什么被拒），比原先笼统罗列所有
            # 可能原因有效得多——模型不必猜自己到底犯了哪条。
            hint = ""
            if attempt > 1 and last_reasons:
                hint = ("\n\n上次输出不合格，逐条原因：" + last_reasons
                        + "\n请针对上述原因逐一修正后重新生成。")
            t = await ask_json(prompt + hint, what="标题生成", stage="titles")
            cands = t.get("candidates", [])
            rec = t.get("recommend", 0)
            # 按推荐优先、其余顺序兜底，取第一个合规的
            order = [rec] + [i for i in range(len(cands)) if i != rec]
            picked, reasons = None, []
            for i in order:
                if 0 <= i < len(cands):
                    en = (cands[i].get("enTitle") or "").strip()
                    why = _en_reject(en)
                    if why is None:
                        picked = {"enTitle": en, "logic": cands[i].get("logic", "")}
                        break
                    reasons.append(f"英文候选{i}「{en[:60]}」{why}")
            zh = (t.get("title") or "").strip()
            zh_why = _zh_reject(zh)
            if zh_why is not None:
                reasons.append(f"中文标题「{zh[:40]}」{zh_why}")
                zh = None
            if picked and zh:
                titles = {"title": zh, "enTitle": picked["enTitle"],
                          "logic": picked["logic"],
                          "candidateCount": len(cands),
                          "allCandidates": [c.get("enTitle") for c in cands]}
                break
            last_reasons = "；".join(reasons)
            logger.warning(f"标题第 {attempt}/2 次校验未通过：{last_reasons}")
            gen_err = "校验未通过: " + last_reasons[:300]
        except Exception as e:
            gen_err = str(e)
    if not titles:
        return {"status": "error", "reason": "title-generation-failed", "err": gen_err}

    result = {"status": "ok", "generated": titles}
    # 填写中文标题
    r1 = await session.eval_json(_js_fill_by_label("产品标题", titles["title"]))
    result["title"] = r1
    # 填写英文标题
    r2 = await session.eval_json(_js_fill_by_label("英文标题", titles["enTitle"]))
    result["enTitle"] = r2
    ok = (r1.get("filled") and r1.get("readback") == titles["title"] and
          r2.get("filled") and r2.get("readback") == titles["enTitle"])
    # 产地固定填「中国大陆 → 广东省」（两级下拉：先选国家，省份下拉才会动态出现）。
    # 【别改成读源商品的产地】2026-08-24 用户明确：本店是**从 1688 采购再由广东仓
    # 发货**的半托管模式，Temu 这里要的是**实际发货地**，不是 1688 卖家所在地。
    # 源属性里的「产地」（浙江织里/福建石狮等）填进来反而是申报不实。
    # 曾按「源产地抓了却没用」把它改成读源值，方向错了，已回退。
    await asyncio.sleep(0.5)
    origin = await _set_origin(session, "中国大陆", "广东省")
    result["origin"] = origin
    if origin.get("status") != "ok":
        ok = False
    result["status"] = "ok" if ok else "error"
    return result



# ---- 阶段⑧ 尺码勾选（fix_sizes）--------------------------------------------

# 【单一尺码的同义写法必须映射到同一个键】2026-08-24 真站取证（offer 846106032776，
# 成人女装针织开衫）：源 skus 键是中文「均码」，而成人女装类目的页面选项全是英文
# —— `one-size`（带连字符）、`Asian One-size`、`Petite One-size` 等 37 项。
# 「均」与 `one-size` 字面上永远对不上，于是 fix_sizes 把页面原本勾着的尺码取消完、
# 还返回 ok，阶段⑨ 才以「错误：请先选择尺码」暴露出来。
#
# 只收【确定同义】的写法：均码/单码/通用/F/free size/one size/onesize。
# 刻意不收 `Asian One-size` / `Petite One-size` / `Tall …` —— 那些是平台的版型变体
# （亚洲版/娇小版/高挑版），与通用均码不是一回事，混进来会让程序在多个选项间乱勾。
_SIZE_ALIASES = {
    # 「码」后缀会先被剥掉，故单字形态（均/单）也要各自登记
    "均": "onesize", "均码": "onesize", "单": "onesize", "单码": "onesize",
    "通用": "onesize",
    "通用码": "onesize", "f": "onesize", "free": "onesize", "freesize": "onesize",
    "onesize": "onesize", "one-size": "onesize", "one size": "onesize",
}


def norm_size(s: str) -> str:
    """尺码名归一，供源 SKU 键与页面复选框文本【两侧共用】后再比较。

    只剥后缀不够。2026-08-21 实测：1688 的尺码键会把身高建议塞进同一个键
    （`110cm建议身高100-110cm`），页面复选框却只有 `110`——原先只 `re.sub` 尾部
    `cm码` 的做法归一出 `110cm建议身高100-110`，与页面永远匹配不上，
    于是 fix_sizes 把已勾选的尺码全部取消后才报错（先破坏再失败）。

    故改为：先砍掉「建议身高/参考身高/适合身高」等描述性尾巴，再取第一段
    数字（童装尺码主体就是数字），然后过一遍同义别名表（均码 ↔ one-size，
    见 _SIZE_ALIASES 上方的实测说明），最后才回落到剥 cm/码 后缀（保留 M/XL）。
    """
    t = str(s).strip()
    # 描述性尾巴：建议/参考/适合 + 身高/体重…，以及括号补充说明
    t = re.split(r"[（(]|建议|参考|适合|推荐", t)[0].strip()
    # 童装数字码主体：110cm → 110、110-120 → 110
    m = re.match(r"^(\d+)", t)
    if m:
        return m.group(1)
    # 字母码（M/XL/XXL）：只剥 cm 与「码」后缀
    t = re.sub(r"(cm)?码?$", "", t, flags=re.I).strip()
    # 同义别名（均码/one-size 等）：别名表命中就用统一键，否则保留原样。
    # 别名比对忽略大小写与内部空格/连字符（`One Size`、`one-size` 都要能命中），
    # 但返回值保留原大小写形态的字母码（M/XL 走不到这一步的替换）。
    key = re.sub(r"[\s_-]+", "", t).lower()
    if key in _SIZE_ALIASES:
        return _SIZE_ALIASES[key]
    # 「均」被上面的「码」后缀剥出来时字面就是「均」，也要能命中
    if t in _SIZE_ALIASES:
        return _SIZE_ALIASES[t]
    return t


_JS_SIZE_GROUP_STATES = r"""(() => {
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'))) {
      const cbs = Array.from(it.querySelectorAll('label.d-checkbox'));
      if (cbs.length) return JSON.stringify(cbs.map(l => ({t: (l.textContent||'').trim(), c: l.querySelector('input').checked})));
    }
  }
  return JSON.stringify([]);
})()"""

_JS_CLICK_SIZE_CB = r"""(() => {
  const target = __T__;
  const items = Array.from(document.querySelectorAll('#skuAttrsInfo .ant-form-item'));
  for (const it of items) {
    const labEl = it.querySelector('.ant-form-item-label');
    const lab = (labEl ? labEl.textContent : '').trim();
    if (lab === '尺码' || (lab.includes('尺码') && !lab.includes('尺码表'))) {
      const cb = Array.from(it.querySelectorAll('label.d-checkbox'))
        .find(l => (l.textContent||'').trim() === target);
      if (cb) { cb.click(); return JSON.stringify({clicked: true}); }
    }
  }
  return JSON.stringify({clicked: false});
})()"""

_JS_SKU_ROW_COUNT = """(() => {
  const tb = document.querySelector('#skuDataInfo tbody');
  return JSON.stringify({n: tb ? tb.querySelectorAll('tr').length : 0});
})()"""


async def fix_sizes(session: BrowserSession, info_path: str,
                    max_rounds: int = 25) -> dict:
    """阶段⑧：尺码勾选修正，使勾选状态与源商品 SKU 一致。

    读 product-info.json 的 skus 键提取源尺码列表，逐个点击复选框直到勾选状态匹配，
    然后等 SKU 表重生成稳定（行数连续两轮不变）。

    不填表格（货号/价格/重量），那些由后续阶段处理。max_rounds 防止死循环。
    """
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    # 源尺码键与页面复选框文本都过 norm_size 再比，避免「110cm建议身高100-110cm」这类
    # 带描述的键匹配不上页面的「110」（见 norm_size 的实测说明）
    wanted = sorted({norm_size(k) for k in (info.get("skus") or {})})
    if not wanted:
        return {"status": "error", "reason": "product-info.json 无 skus 数据"}

    # 1) 【先校验再动手】源尺码在页面选项里一个都找不到时立刻报错，不许往下走。
    # 2026-08-24 实测（offer 846106032776「均码」× 成人女装英文尺码）：没有这道闸，
    # 下面的循环会把页面原本勾着的尺码逐个取消（它们「不在 wanted 里」），最后
    # 返回 status=ok 只带一句 warning，阶段⑨ 才以「请先选择尺码」暴露——典型的
    # 先破坏再失败。归一函数补别名只能覆盖已知写法，这道闸兜住所有未知写法。
    states0 = await session.eval_json(_JS_SIZE_GROUP_STATES)
    if not states0:
        return {"status": "error", "reason": "未找到尺码复选框组（不在编辑页？）"}
    page_norms = {norm_size(s["t"]) for s in states0}
    hit = [w for w in wanted if w in page_norms]
    if not hit:
        return {"status": "error",
                "reason": f"源尺码在页面选项里全部不存在，未改动任何勾选："
                          f"源 {wanted} / 页面 {sorted(page_norms)[:12]}",
                "wantedSizes": wanted,
                "pageSizes": [s["t"] for s in states0]}

    # 2) 勾选修正
    toggles = []
    for _ in range(max_rounds):
        states = await session.eval_json(_JS_SIZE_GROUP_STATES)
        if not states:
            return {"status": "error", "reason": "未找到尺码复选框组（不在编辑页？）"}
        # 找第一个状态不符的：wanted 里的应该勾上，不在 wanted 里的应该不勾
        bad = next((s for s in states if (norm_size(s["t"]) in wanted) != bool(s["c"])), None)
        if not bad:
            break  # 全部正确，退出循环
        # 点击按页面【原始文本】定位（_JS_CLICK_SIZE_CB 是文本匹配，不能传归一值）
        await session.eval_json(_JS_CLICK_SIZE_CB.replace("__T__", J(bad["t"])))
        toggles.append(bad["t"])
        await asyncio.sleep(1.2)

    # 验证：再读一次，确认全部正确
    states = await session.eval_json(_JS_SIZE_GROUP_STATES)
    still_bad = [s["t"] for s in states if (norm_size(s["t"]) in wanted) != bool(s["c"])]
    if still_bad:
        return {"status": "error", "reason": f"尺码勾选未修正: {still_bad}", "toggled": toggles}
    missing = [w for w in wanted if w not in [norm_size(s["t"]) for s in states]]

    # 3) 等 SKU 表重生成稳定（行数连续两轮不变说明 Vue 渲染完了）
    last_n, stable = -1, 0
    for _ in range(20):
        n = (await session.eval_json(_JS_SKU_ROW_COUNT)).get("n", 0)
        if n == last_n:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_n = n
        await asyncio.sleep(0.6)

    final_count = (await session.eval_json(_JS_SKU_ROW_COUNT)).get("n", 0)
    result = {"status": "ok", "wantedSizes": wanted, "toggled": toggles,
              "rowCount": final_count}
    if missing:
        result["warning"] = f"源尺码在页面复选框中不存在: {missing}"
    return result


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


_JS_READ_SKU_CODES = r"""(() => {
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[0];
  if (!tb) return JSON.stringify({err: 'no-first-tbody'});
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const rows = [];
  Array.from(tb.querySelectorAll('tr')).forEach((tr, i) => {
    const inp = tr.querySelector('input[name=variationSku]');
    if (!inp) return;
    const tds = Array.from(tr.querySelectorAll('td'));
    rows.push({i, color: txt(tds[0]), size: txt(tds[1]), cur: inp.value || ''});
  });
  return JSON.stringify({rows});
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
  const filled = [], mismatch = [];
  for (const p of PLAN) {
    const tr = rows[p.i];
    if (!tr) { mismatch.push({i: p.i, why: 'no-row'}); continue; }
    const tds = Array.from(tr.querySelectorAll('td'));
    if (txt(tds[0]) !== p.color || txt(tds[1]) !== p.size) {
      mismatch.push({i: p.i, why: 'row-moved', now: txt(tds[0]) + '/' + txt(tds[1])});
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
            return {"status": "error",
                    "reason": f"第 {r['i'] + 1} 行颜色/尺码列都读不到，无法拼货号"}
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


# ---- 阶段⑩ 变种信息（set_variant）------------------------------------------

_JS_FILL_VARIANT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setVal = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const PRICE = __PRICE__, DIMS = __DIMS__, WEIGHT = __WEIGHT__, MSRP = __MSRP__;
  const sku = document.getElementById('skuDataInfo');
  const tb = sku.querySelectorAll('tbody')[0];
  const rows = Array.from(tb.querySelectorAll('tr'));
  const results = [];
  for (const r of rows) {
    const tds = Array.from(r.querySelectorAll('td'));
    if (tds.length < 7) continue;
    const color = (tds[0].textContent||'').trim();
    const size = (tds[1].textContent||'').trim();
    const priceInp = tds[3].querySelector('input');
    const dimInps = Array.from(tds[4].querySelectorAll('input'));
    const weightInp = tds[5].querySelector('input');
    const msrpInp = tds[6].querySelector('input');
    if (priceInp && priceInp.value !== PRICE) setVal(priceInp, PRICE);
    dimInps.forEach((d, i) => { if (DIMS[i] && d.value !== DIMS[i]) setVal(d, DIMS[i]); });
    if (weightInp && weightInp.value !== WEIGHT) setVal(weightInp, WEIGHT);
    if (msrpInp && msrpInp.value !== MSRP) setVal(msrpInp, MSRP);
    results.push([color, size, priceInp?priceInp.value:'', dimInps.map(d=>d.value).join('x'),
                  weightInp?weightInp.value:'', msrpInp?msrpInp.value:'']);
  }
  await sleep(400);
  const bad = results.filter(x => x[2]!==PRICE || x[3]!==DIMS.join('x') || x[4]!==WEIGHT || x[5]!==MSRP);
  return JSON.stringify({count: rows.length, bad, sample: results.slice(0,4)});
})()"""


# 申报价默认值（人民币，写进变种表「申报价」列）。
# 【为什么是 188.88 而不是原来的 168】2026-08-25 用户确认：申报价按本店统一口径填，
# 不逐商品估；UI/CLI 想填实际售价时传 price 覆盖，不填就用这个默认值。
# 建议售价由它 ÷ 7 折算成美元（见 set_variant），改这里两列一起变。
DECLARE_PRICE_DEFAULT = "188.88"


def normalize_declare_price(price) -> str:
    """把外部传入的申报价归一成可直接写进输入框的数字串，非法值退回默认值。

    UI 是自由输入框，用户可能填 "￥188"、"188.888"、空格甚至中文，直接
    setVal 进去平台要么拦、要么静默截断。这里统一处理：
      - 抽第一段数字（含小数点），最多保留 2 位小数（平台申报价就是两位）
      - 落在 0 < p <= 100000 之外的一律退默认值并告警（不静默）
    不抛异常：定价填错不该让整个商品发布中断，退默认值是可核对的确定行为。
    """
    raw = str(price if price is not None else "").strip()
    if not raw:
        return DECLARE_PRICE_DEFAULT
    # 【负号必须纳入匹配】只写 \d+ 会从 "-5" 里提出 "5"，负数就被当成合法价放行了
    # （与数值属性行校验踩过的同一个坑，见 _validate_attr_changes 里的同名注释）。
    m = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not m:
        logger.warning(f"申报价 {raw!r} 里没有数字，按默认 {DECLARE_PRICE_DEFAULT} 处理")
        return DECLARE_PRICE_DEFAULT
    val = round(float(m.group()), 2)
    if not 0 < val <= 100000:
        logger.warning(f"申报价 {raw!r} 超出 0~100000 合理范围，"
                       f"按默认 {DECLARE_PRICE_DEFAULT} 处理")
        return DECLARE_PRICE_DEFAULT
    # 去掉多余的 .0 / .00：页面回读的是 "188" 而不是 "188.0"，
    # 不归一会让 _JS_FILL_VARIANT 的回读比对全判成 bad。
    out = f"{val:.2f}".rstrip("0").rstrip(".")
    if out != raw:
        logger.info(f"申报价归一：{raw!r} → {out}")
    return out


# 非服装类包裹的兜底尺寸/重量：模型两次都给出越界值时用它，保证流程不停
# （服装类不走模型，见 _APPAREL_DIMS）。取常见快递袋尺度，宁可略偏保守（不低报运费）。
_PACK_FALLBACK = {"长": 30, "宽": 24, "高": 5, "重量": 400}

# 服装类包裹的确定性申报尺寸（cm，长x宽x高）。
# 【为什么服装类不问模型】2026-08-25 用户确认：服装压平装快递袋，尺寸只由袋规格
# 决定、与款式无关，本店一律 30x25x3；让模型逐个商品去估，只会在同一批服装里估出
# 一堆互不相同的数（同款不同色都能差出 5cm），既不更准也不可复核。
# 需要按实物体积算的（玩具、鞋盒、家居用品等有刚性包装的品类）才交模型估。
_APPAREL_DIMS = ("30", "25", "3")

# 服装类判定词：命中类目路径或标题即按服装处理。
# 只用于选「固定尺寸 or 模型估算」，判错的代价是尺寸偏差而非流程中断，故不做校验。
#
# 【不收「套装」这类量词】它跨品类通用（积木套装、餐具套装都是它），单靠它判服装
# 会把带刚性包装的品类误判成压平袋装、高只填 3cm。判服装要靠品类本身的词。
_APPAREL_WORDS = (
    "服装", "服饰", "童装", "女装", "男装", "内衣", "内裤", "上装", "下装",
    "外套", "夹克", "卫衣", "毛衣", "衬衫", "T恤", "背心", "吊带", "裤", "裙",
    "连体衣", "泳装", "泳衣", "睡衣", "家居服", "袜", "围巾", "手套",
)

# 排除词：命中这些就不按服装处理，优先于 _APPAREL_WORDS。
# 「帽/袜」等配件本身贴着服装类，但硬壳收纳、玩具、鞋盒这些一旦沾上服装词
# （如「玩具服装」「鞋袜收纳盒」）就会被固定成 30x25x3，实际体积差一截。
_APPAREL_EXCLUDE = ("玩具", "积木", "鞋盒", "收纳", "餐具", "水杯", "保温杯",
                    "文具", "家具", "电器", "礼盒", "模型")


def _is_apparel(cat_path, title: str) -> bool:
    """判断是否服装类（决定包裹尺寸走固定值还是模型估算）。

    类目路径优先：那是店小秘表单里已生效的真实类目，比标题可靠。
    类目为空（续跑时状态文件没回填到）才退到标题匹配。
    排除词先判：宁可多问一次模型（几秒），也不要把带刚性包装的品类按压平袋装填。
    """
    blob = " ".join(str(x) for x in (cat_path or [])) or (title or "")
    if any(w in blob for w in _APPAREL_EXCLUDE):
        return False
    return any(w in blob for w in _APPAREL_WORDS)


def _check_pack_est(est: dict, need_dims: bool, need_weight: bool) -> list:
    """包装估算量级闸，返回问题列表（空 = 通过）。

    只拦明显离谱的量级，不判断具体数值准不准（那要看实物）：
      - 三边各自 1~150cm，且不能三边全 <= 2（模型偶发返回 1x1x1）
      - 重量 10~30000g
    快递包裹超过 150cm 单边基本不存在，低于 1cm 更不可能，这两头都是模型
    输出异常而非真实取值。
    """
    problems = []
    if need_dims:
        try:
            dims = [float(est[k]) for k in ("长", "宽", "高")]
        except (KeyError, TypeError, ValueError):
            return ["尺寸字段缺失或非数值"]
        for k, v in zip(("长", "宽", "高"), dims):
            if not (1 <= v <= 150):
                problems.append(f"{k}={v}cm 超出 1~150cm 合理范围")
        if all(v <= 2 for v in dims):
            problems.append(f"三边全 <= 2cm（{dims}），不是真实包裹尺度")
    if need_weight:
        try:
            w = float(est["重量"])
        except (KeyError, TypeError, ValueError):
            return problems + ["重量字段缺失或非数值"]
        if not (10 <= w <= 30000):
            problems.append(f"重量={w}g 超出 10~30000g 合理范围")
    return problems


async def set_variant(session: BrowserSession, info_path: str,
                      price: str = "", dims: Optional[str] = None,
                      weight: Optional[str] = None, cat_path=None) -> dict:
    """阶段⑩：变种信息批量填写（申报价/尺寸/重量/建议售价）。

    规则（2026-08-18 定，2026-08-25 按用户结论修订申报价与尺寸）：
    - 货号不动（fix_sizes 已保证 <颜色>-<尺码>）
    - 申报价默认 188.88（DECLARE_PRICE_DEFAULT），UI/CLI 可传 price 覆盖
    - 尺寸 = 打包后长宽高（cm）。取值优先级：显式 dims > 服装类固定 30x25x3
      > 源 packInfo > LLM 预估。服装类不问模型的理由见 _APPAREL_DIMS 注释。
    - 重量 = 打包后克重(g)：源 packInfo.unitWeightKg×1000，没有交 LLM 预估；weight 覆盖
    - 建议售价 = 申报价 ÷ 7（币种列保持页面默认 USD）

    cat_path：编辑页已生效的类目路径，用来判服装类；为空退到标题判定。
    """
    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    pack = info.get("packInfo") or {}
    title = info.get("title", "")
    price = normalize_declare_price(price)

    # 重量：参数 → 源 packInfo → LLM（与尺寸一样，显式传入的最高优先）
    w_g = weight
    if not w_g and pack.get("unitWeightKg"):
        w_g = str(round(float(pack["unitWeightKg"]) * 1000))

    # 尺寸：参数 → 服装类固定值 → 源 packInfo → LLM
    # 【服装类排在源 packInfo 之前】源 packInfo 是 1688 卖家填的整箱/散件口径，
    # 与本店压平装快递袋的实际包裹无关；服装既已有确定规格，就不该被源值带偏。
    d_list = None
    apparel = _is_apparel(cat_path, title)
    if dims:
        d_list = [x.strip() for x in re.split(r"[x×*]", dims)]
    elif apparel:
        d_list = list(_APPAREL_DIMS)
        logger.info(f"服装类包裹尺寸按固定规格 {'x'.join(d_list)}cm（不走模型估算）")
    elif pack.get("dimsCm"):
        d_list = [str(x) for x in pack["dimsCm"]]

    # LLM 预估兜底
    if not w_g or not d_list:
        prompt = (
            f"你是跨境电商打包专家。商品：{title}。\n"
            "请预估单个包裹打包后的"
            + ("尺寸 长x宽x高（cm）" if not d_list else "")
            + ("和" if (not d_list and not w_g) else "")
            + ("重量（g）" if not w_g else "")
            + "。\n"
            # 【包装形式不能写死成快递袋】原提示词固定写「opp袋/快递袋」，而走到
            # 这里的都已是非服装类（服装走 _APPAREL_DIMS 固定值）：玩具/鞋类/家居
            # 用品多数带彩盒或硬壳，按袋装估会把高估成 3~5cm，与实际体积差一截。
            "要求：先判断这个品类的真实包装形式（快递袋、彩盒、纸箱、含注塑外壳等），"
            "再按商品本体的实际体积给出含包装的外尺寸；有刚性包装的不得按压平袋装估。"
            "数值为整数。\n"
            # 【不许写「偏紧凑」】原提示词这么写，是系统性向下偏置：申报尺寸/重量
            # 低报会压低体积重运费，属虚假申报。要的是准，不是小。
            "不要刻意压小或放大，低报运费属虚假申报、高报自己吃亏。\n"
            '只输出严格JSON：{"长":x,"宽":y,"高":z,"重量":w}'
        )
        est = await ask_json(prompt, what="包装尺寸重量估算", stage="variant")
        # 量级闸：原先无任何范围校验，模型返回 {"长":1,"宽":1,"高":1} 会原样填进
        # 申报字段。全自动路线下不转人工，越界就回喂问题重试一次。
        bad = _check_pack_est(est, need_dims=not d_list, need_weight=not w_g)
        if bad:
            logger.warning("包装估算越界，重生成一次：" + "；".join(bad))
            est2 = await ask_json(
                prompt + "\n\n上次输出不合理：" + "；".join(bad)
                + "\n请按真实快递包裹尺度重新给值。",
                what="包装尺寸重量估算(重试)", stage="variant",
            )
            if not _check_pack_est(est2, need_dims=not d_list, need_weight=not w_g):
                est = est2
            else:
                logger.warning("包装估算重试后仍越界，按通用快递包裹常识兜底")
                est = {**est2, **_PACK_FALLBACK}
        if not d_list:
            d_list = [str(est["长"]), str(est["宽"]), str(est["高"])]
        if not w_g:
            w_g = str(est["重量"])

    if len(d_list) != 3:
        return {"status": "error", "reason": f"尺寸需为 长x宽x高 三个值: {d_list}"}

    msrp = str(round(float(price) / 7, 2)).rstrip("0").rstrip(".")
    js = (_JS_FILL_VARIANT
          .replace("__PRICE__", J(str(price)))
          .replace("__DIMS__", J(d_list))
          .replace("__WEIGHT__", J(str(w_g)))
          .replace("__MSRP__", J(msrp)))
    res = await session.eval_json(js)
    ok = res.get("count", 0) > 0 and not res.get("bad")
    return {"status": "ok" if ok else "validation-error",
            "price": price, "dims": d_list, "weight": w_g, "msrp": msrp,
            "rowCount": res.get("count"), "bad": res.get("bad"),
            "sample": res.get("sample")}


# ---- 阶段⑪ 库存与SKU分类（set_stock）---------------------------------------

# 仓库是多选（ant-select-multiple），选中项回读 .ant-select-selection-item。
# listId 来自 select 内 input 的 aria-owns，用来在多个常驻浮层里精确认出自己那一个。
_JS_WH_STATE = r"""(() => {
  const lab = Array.from(document.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent||'').trim().startsWith('选择仓库'))[0];
  if (!lab) return JSON.stringify({err: 'no-选择仓库-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 5 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const selected = Array.from(sel.querySelectorAll('.ant-select-selection-item'))
    .map(x => (x.title || x.textContent || '').trim());
  const inp = sel.querySelector('input[aria-owns]');
  return JSON.stringify({selected, open: sel.classList.contains('ant-select-open'),
    listId: inp ? inp.getAttribute('aria-owns') : null});
})()"""

# 展开仓库下拉并勾选目标仓库。滚动、展开、点选、回读全塞在一次 eval 里。
# 【2026-08-23 修此处的 no-dropdown】原实现踩了三个坑，逐条对应下面的写法：
# 1. 用 elementFromPoint(x, y) 合成点击——先在 python 侧读坐标、再另发一次 eval 点，
#    两次之间 Vue 只要重渲染或页面滚一下，坐标就打在别的元素上。改成直接点
#    .ant-select-selector（antd 把点击处理绑在 selector 那层，点外层 .ant-select
#    无效，同 [[dianxiaomi-antselect-open-and-ghost]]）。
# 2. 按 getBoundingClientRect().height > 0 找浮层——隐藏的浮层高度恒为 0，但可见那个
#    在页面已滚动时 top 是文档坐标（实测 top: 4618px），仍可能算出 0 高度而漏掉。
#    改成按 listId 直接 getElementById 定位，再用 inline display 判可见。
# 3. 点开下拉后固定 sleep 1.5s 就去找——浮层首次挂载慢于此就报 no-dropdown。改成
#    轮询 6s。
_JS_PICK_WAREHOUSE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const WH = __WH__;
  const lab = Array.from(document.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent||'').trim().startsWith('选择仓库'))[0];
  if (!lab) return JSON.stringify({err: 'no-选择仓库-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 5 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const inp = sel.querySelector('input[aria-owns]');
  const listId = inp ? inp.getAttribute('aria-owns') : null;
  if (!listId) return JSON.stringify({err: 'no-aria-owns'});
  sel.scrollIntoView({block: 'center', behavior: 'instant'});
  await sleep(300);
  const visible = () => {
    const lb = document.getElementById(listId);
    const c = lb ? lb.closest('.ant-select-dropdown') : null;
    return (c && !/display:\s*none/.test(c.getAttribute('style') || '')) ? c : null;
  };
  let dd = visible();
  if (!dd) {
    const inner = sel.querySelector('.ant-select-selector') || sel;
    ['mousedown', 'mouseup', 'click'].forEach(t =>
      inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
    for (let i = 0; i < 30 && !dd; i++) { await sleep(200); dd = visible(); }
  }
  if (!dd) return JSON.stringify({err: 'no-dropdown', listId,
    open: sel.classList.contains('ant-select-open')});
  const opts = Array.from(dd.querySelectorAll('.ant-select-item-option'));
  const o = opts.find(x => (x.textContent || '').trim().includes(WH));
  if (!o) return JSON.stringify({err: 'no-option',
    available: opts.map(x => (x.textContent || '').trim()).slice(0, 8)});
  o.click();
  await sleep(1000);
  document.body.click();
  await sleep(1200);
  return JSON.stringify({
    selected: Array.from(sel.querySelectorAll('.ant-select-selection-item'))
      .map(x => (x.title || x.textContent || '').trim()),
    open: sel.classList.contains('ant-select-open')});
})()"""

_JS_FILL_STOCK_ONLY = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const STOCK = __STOCK__;
  let inps = [];
  for (let i = 0; i < 10; i++) {
    inps = Array.from(document.querySelectorAll('input[name=stock]'));
    if (inps.length) break;
    await sleep(500);
  }
  if (!inps.length) return JSON.stringify({err: 'no-stock-inputs'});
  for (const inp of inps) {
    if (inp.value !== STOCK) {
      setter.call(inp, STOCK);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
    }
  }
  await sleep(500);
  const bad = Array.from(document.querySelectorAll('input[name=stock]')).filter(i => i.value !== STOCK).length;
  return JSON.stringify({filled: inps.length, bad});
})()"""

_JS_FILL_STOCK_CAT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setInp = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const setSel = (sel, v) => { sel.value = String(v); sel.dispatchEvent(new Event('change', {bubbles:true})); };
  const CAT = __CAT__, QTY = __QTY__, UNIT = __UNIT__;
  const sku = document.getElementById('skuDataInfo');
  if (!sku) return JSON.stringify({err: 'no-skuDataInfo'});
  const tb = sku.querySelectorAll('tbody')[1];
  if (!tb) return JSON.stringify({err: 'no-second-tbody'});
  const rows = Array.from(tb.querySelectorAll('tr'));
  const findCatTd = tds => tds.find(td =>
    Array.from(td.querySelectorAll('select option')).some(o => o.text === '混合套装'));
  let processed = 0;
  for (const r of rows) {
    const tds = Array.from(r.querySelectorAll('td'));
    if (tds.length < 4) continue;
    const catTd = findCatTd(tds);
    if (!catTd) continue;
    processed++;
    let sels = Array.from(catTd.querySelectorAll('select'));
    if (sels[0] && sels[0].value !== CAT) { setSel(sels[0], CAT); await sleep(400); }
    sels = Array.from(catTd.querySelectorAll('select'));
    if (sels[1] && sels[1].value !== UNIT) setSel(sels[1], UNIT);
    const qtyInp = catTd.querySelector('input[name=skuCategoryNum]');
    if (qtyInp && qtyInp.value !== QTY) setInp(qtyInp, QTY);
  }
  await sleep(600);
  const bad = [], sample = [];
  Array.from(tb.querySelectorAll('tr')).forEach(r => {
    const tds = Array.from(r.querySelectorAll('td'));
    const catTd = findCatTd(tds);
    if (!catTd) return;
    const sels = Array.from(catTd.querySelectorAll('select'));
    const qtyInp = catTd.querySelector('input[name=skuCategoryNum]');
    const row = [sels[0]?sels[0].value:'', sels[1]?sels[1].value:'', qtyInp?qtyInp.value:''];
    sample.push(row);
    if (row[0] !== CAT || row[1] !== UNIT || row[2] !== QTY) bad.push(row);
  });
  return JSON.stringify({processed, bad, sample: sample.slice(0, 4)});
})()"""


async def set_stock(session: BrowserSession, info_path: str,
                    stock: str = "100", warehouse: str = "飞特COL仓库") -> dict:
    """阶段⑪：仓库/库存/SKU分类批量填写。

    完整流程（2026-08-18用户确认）：
    1. 选择仓库：勾选目标仓库（默认「飞特COL仓库」），**勾选后库存列才渲染**
    2. 填库存：统一值（默认100），等仓库勾选后 input[name=stock] 出现
    3. SKU分类：按标题+套装件数交 LLM 判断（单品/同款多件/混合套装 + 数量 + 单位）
    4. 包装清单：判断是否需要配件（暂未实现，留「请选择配件」不动）
    """
    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    title = info.get("title", "")
    attrs = info.get("attributes") or {}

    # 1. 选择仓库
    wh = await session.eval_json(_JS_WH_STATE)
    if wh.get("err"):
        return {"status": "error", "stage": "warehouse", **wh}

    if warehouse not in (wh.get("selected") or []):
        # 滚动+展开+点选+回读一次 eval 做完：跨 eval 传坐标或依赖固定 sleep 都会被
        # Vue 重渲染/浮层延迟挂载打断（见 _JS_PICK_WAREHOUSE 注释）。
        opt = await session.eval_json(_JS_PICK_WAREHOUSE.replace("__WH__", J(warehouse)))
        if opt.get("err"):
            return {"status": "error", "stage": "warehouse-option", **opt}
        if warehouse not in (opt.get("selected") or []):
            return {"status": "error", "stage": "warehouse",
                    "reason": "勾选后回读未选中", **opt}

    # 2. 填库存（勾选仓库后才渲染）
    st = await session.eval_json(_JS_FILL_STOCK_ONLY.replace("__STOCK__", J(str(stock))))
    if st.get("err") or st.get("bad"):
        return {"status": "error", "stage": "stock", **st}

    # 3. SKU分类：交 LLM 判断
    prompt = (
        "你是跨境电商 Listing 专家。店小秘 Temu 半托管发布时需要为每个 SKU 填写「SKU分类」。\n\n"
        f"商品信息：\n- 标题：{title}\n- 套装件数：{attrs.get('套装件数', '单件')}"
        f"\n- 套装类型：{attrs.get('套装类型', '无')}\n\n"
        "SKU分类选项：1=单品（一个SKU只含一件商品） 2=同款多件（多件相同商品） 3=混合套装（多件不同商品组合）\n"
        "单位选项：1=件 2=双 3=包\n\n"
        "请判断这个商品的 SKU分类（含数量、单位）。\n"
        '只输出严格JSON：{"skuCat":"1|2|3","qty":数字,"unit":"1|2|3","reason":"一句话理由"}'
    )
    judge = await ask_json(prompt, what="SKU分类判断", stage="stock")
    cat, qty, unit = str(judge.get("skuCat", "1")), str(judge.get("qty", 1)), str(judge.get("unit", "1"))

    res = await session.eval_json(_JS_FILL_STOCK_CAT
                                   .replace("__CAT__", J(cat))
                                   .replace("__QTY__", J(qty))
                                   .replace("__UNIT__", J(unit)))
    if res.get("err"):
        return {"status": "error", "stage": "sku-category", **res}

    ok = res.get("processed", 0) > 0 and not res.get("bad")
    return {"status": "ok" if ok else "validation-error",
            "warehouse": warehouse, "stock": stock,
            "skuCategory": {"cat": cat, "qty": qty, "unit": unit, "reason": judge.get("reason")},
            "processed": res.get("processed"), "bad": res.get("bad"),
            "sample": res.get("sample")}

# ---- 阶段⑨ 尺码表（add_sizechart）-------------------------------------------

# 【要自己等 .skuAttrSizeChart 渲染出来，别指望调用方 sleep】2026-08-22 实测：
# open_edit 的加载判据是 #skuDataInfo 出现，而尺码表入口在 SKU 属性区里、渲染更晚。
# CLI 路径每条命令后面都跟着 asyncio.sleep(2) 掩盖了这一点，service 续跑补开编辑页
# 那条路没有，于是导航完同一秒就跑阶段⑨，报 no-link（同商品 947662049255）。
# 更坏的情形是 _JS_SIZECHART_STATE 此时返回 found:false ——「尺码表已存在则跳过」
# 的判断跟着失效，会对已有尺码表的商品重复走一遍新增。故两处都改成轮询等待。
_JS_SIZECHART_STATE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let area = null;
  for (let i = 0; i < 40; i++) {
    area = document.querySelector('.skuAttrSizeChart');
    if (area && area.querySelector('.ant-form-item-control-input')) break;
    await sleep(300);
  }
  if (!area) return JSON.stringify({found: false});
  const ctrl = area.querySelector('.ant-form-item-control-input');
  return JSON.stringify({found: true, text: (ctrl ? ctrl.textContent : '').trim()});
})()"""

_JS_OPEN_SIZECHART_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 同 _JS_SIZECHART_STATE：轮询等区域与入口渲染完，别假定调用方已经 sleep 过
  let link = null;
  for (let i = 0; i < 40; i++) {
    const a = document.querySelector('.skuAttrSizeChart');
    link = a ? a.querySelector('span.link') : null;
    if (link) break;
    await sleep(300);
  }
  if (!link) return JSON.stringify({opened: false, reason: 'no-link'});
  link.scrollIntoView({block: 'center'});
  await sleep(500);
  link.click();
  await sleep(1500);
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  return JSON.stringify({opened: !!wrap});
})()"""

# 尺码分类：默认【跟随平台按已选类目预选的值】，不再拿写死关键词去点选项。
#
# 【为什么废掉写死的「上装」】2026-08-22 实测 947662049255（女童网纱连衣裙）报
# option-not-found：弹窗里这个下拉的选项**由页面已选类目决定**，只有「女童装-连衣裙」
# 一项，且平台已经替你选好了。原实现假设它是「上装/下装/套装」这样的固定枚举，拿
# 「上装」去 includes 匹配必然一个都命中不了——不是时序问题，是假设错了。故改为：
# 已有预选值就照用（平台按类目给的比关键词猜的准），只在没预选时才去展开挑。
#
# 另修两处定位隐患：
# 1. 【按「尺码分类」form-item 锚定 select】弹窗里有 2 个 .ant-select（尺码分类、
#    引用模板），原来 wrap.querySelector('.ant-select-selector') 取第一个、靠 DOM
#    顺序侥幸命中，字段一调序就会去改「引用模板」。
# 2. 【判浮层可见只看 inline display】原来按 offsetHeight > 0 找，与本项目已验证的
#    结论相反（隐藏浮层高度恒为 0 但 ant-select-dropdown-hidden 类不一定加，见
#    _JS_PICK_FIRST_TPL 注释）。多个浮层同时可见时，再用 search input 的
#    aria-controls 指向的 list id 精确挑出属于本 select 的那个。
_JS_SET_SIZECHART_CAT = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const keyword = __CAT__;   // null 表示跟随平台预选，不指定具体分类
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({ok: false, reason: 'no-modal'});

  const item = Array.from(wrap.querySelectorAll('.ant-form-item'))
    .find(it => (((it.querySelector('.ant-form-item-label')||{}).textContent)||'').includes('尺码分类'));
  const sel = (item || wrap).querySelector('.ant-select');
  if (!sel) return JSON.stringify({ok: false, reason: 'no-select'});
  const curOf = () => {
    const x = sel.querySelector('.ant-select-selection-item');
    return x ? (x.title || x.textContent || '').trim() : '';
  };

  // 预选值可用就直接收工：不展开下拉、不点任何东西（幂等，也少一次浮层残留风险）
  const cur = curOf();
  if (cur && (!keyword || cur.includes(keyword)))
    return JSON.stringify({ok: true, selected: cur, source: 'preset'});

  const inner = sel.querySelector('.ant-select-selector') || sel;
  ['mousedown','mouseup','click'].forEach(t =>
    inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  await sleep(900);

  const search = sel.querySelector('.ant-select-selection-search-input');
  const listId = search ? search.getAttribute('aria-controls') : null;
  let drops = Array.from(document.querySelectorAll('.ant-select-dropdown'))
    .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''));
  if (listId) {
    const mine = drops.filter(d => d.querySelector('#' + listId));
    if (mine.length) drops = mine;
  }
  const drop = drops[drops.length - 1];
  if (!drop) return JSON.stringify({ok: false, reason: 'dropdown-not-open', cur});

  // 分类可能是长列表（rc-virtual-list 只渲染可视约 10 条），滚动逐屏收集。
  // 每屏 180ms 是本项目实测值，别下调（见 _read_active_options 注释）。
  const holder = drop.querySelector('.rc-virtual-list-holder');
  const opts = [];
  const collect = () => Array.from(drop.querySelectorAll('.ant-select-item-option'))
    .forEach(o => { const t = (o.textContent || '').trim();
      if (t && !opts.includes(t)) opts.push(t); });
  collect();
  if (holder) {
    for (let k = 0; k < 40; k++) {
      if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
      holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
      await sleep(180);
      collect();
    }
  }

  // 没指定关键词时只认「唯一选项」——多选项又没预选值，说明平台没按类目定死，
  // 这时替用户瞎挑一个会填出错档尺码表，宁可报错让人工指定。
  let target = null;
  if (keyword) {
    target = Array.from(drop.querySelectorAll('.ant-select-item-option'))
      .find(o => (o.textContent || '').includes(keyword));
    if (!target && opts.some(t => t.includes(keyword))) {
      // 命中的那条被虚拟列表滚出了渲染窗口：滚回顶再逐屏找回来
      if (holder) holder.scrollTop = 0;
      await sleep(200);
      for (let k = 0; k < 40 && !target; k++) {
        target = Array.from(drop.querySelectorAll('.ant-select-item-option'))
          .find(o => (o.textContent || '').includes(keyword));
        if (target || !holder) break;
        if (holder.scrollTop + holder.clientHeight >= holder.scrollHeight - 2) break;
        holder.scrollTop = holder.scrollTop + (holder.clientHeight || 200);
        await sleep(180);
      }
    }
    if (!target) return JSON.stringify({ok: false, reason: 'option-not-found',
                                        keyword, cur, options: opts});
  } else {
    const all = Array.from(drop.querySelectorAll('.ant-select-item-option'));
    if (opts.length !== 1 || !all.length)
      return JSON.stringify({ok: false, reason: 'no-preset-and-ambiguous',
                             cur, options: opts});
    target = all[0];
  }
  target.click();
  await sleep(800);
  const now = curOf();
  if (!now) return JSON.stringify({ok: false, reason: 'click-no-effect',
                                   options: opts});
  return JSON.stringify({ok: true, selected: now, source: 'picked', options: opts});
})()"""

_JS_SIZECHART_PARAMS = r"""(() => {
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({params: [], sizes: []});
  const table = wrap.querySelector('table');
  if (!table) return JSON.stringify({params: [], sizes: []});
  const ths = Array.from(table.querySelectorAll('thead th'));
  // 【参数名只取 th 的直接文本节点】2026-08-22 实测：表头是
  //   <th>裙长 <div class="flex..."><div class="link">(批量)</div></div></th>
  // 用 textContent 会得到「裙长 (批量)」——那是批量填充按钮的 UI 文案，不是参数名。
  // 带着后缀往下走，会污染 LLM 提示词（让模型去估一个叫「裙长 (批量)」的参数）、
  // 也让源实测值的模糊对齐失准，最终 data 的键全是脏的。
  const thName = th => Array.from(th.childNodes)
    .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
    .filter(Boolean).join('') || (th.textContent||'').trim();
  const params = ths.slice(1).map(thName)
    .filter(t => t && !t.includes('身高') && !t.includes('体重'));
  const trs = Array.from(table.querySelectorAll('tbody tr'));
  const sizes = trs.map(tr => {
    const td = tr.querySelector('td');
    return td ? (td.textContent||'').trim() : '';
  }).filter(t => t);
  return JSON.stringify({params, sizes});
})()"""

_JS_FILL_SIZECHART = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const tplName = __NAME__;
  const data = __DATA__;
  const params = __PARAMS__;
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({ok: false, reason: 'no-modal'});
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const setVal = (inp, v) => { setter.call(inp, String(v)); inp.dispatchEvent(new Event('input', {bubbles:true})); inp.dispatchEvent(new Event('change', {bubbles:true})); };
  const nameInp = wrap.querySelector('input[placeholder*="模板名称"]');
  if (nameInp && nameInp.value !== tplName) setVal(nameInp, tplName);
  const table = wrap.querySelector('table');
  if (!table) return JSON.stringify({ok: false, reason: 'no-table'});
  const ths = Array.from(table.querySelectorAll('thead th'));
  // 参数名提取须与 _JS_SIZECHART_PARAMS 的 thName 一致（剥掉「(批量)」子元素文案），
  // 否则这里按 textContent 比对，params 里的干净名一个都匹配不上、colIdx 全空。
  const thName = th => Array.from(th.childNodes)
    .filter(n => n.nodeType === 3).map(n => n.textContent.trim())
    .filter(Boolean).join('') || (th.textContent||'').trim();
  const colIdx = {};
  ths.forEach((th, i) => {
    const t = thName(th);
    if (params.includes(t)) colIdx[t] = i;
  });
  const trs = Array.from(table.querySelectorAll('tbody tr'));
  const empty = [];
  trs.forEach(tr => {
    const tds = Array.from(tr.querySelectorAll('td'));
    const size = (tds[0].textContent||'').trim();
    if (!data[size]) return;
    for (const p of params) {
      const idx = colIdx[p];
      if (idx === undefined) continue;
      const inp = tds[idx] ? tds[idx].querySelector('input') : null;
      if (!inp) continue;
      const v = String(data[size][p] || '');
      if (v && inp.value !== v) setVal(inp, v);
    }
  });
  await sleep(600);
  trs.forEach(tr => {
    const tds = Array.from(tr.querySelectorAll('td'));
    const size = (tds[0].textContent||'').trim();
    if (!data[size]) return;
    for (const p of params) {
      const idx = colIdx[p];
      if (idx === undefined) continue;
      const inp = tds[idx] ? tds[idx].querySelector('input') : null;
      if (!inp || !inp.value) empty.push(`${size}-${p}`);
    }
  });
  return JSON.stringify({ok: empty.length === 0, empty});
})()"""

_JS_CLICK_SIZECHART_OK = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const _scList = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  let wrap = null;
  for (const w of _scList) {
    const r = w.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + 300, innerHeight - 10));
    if (hit && w.contains(hit)) { wrap = w; break; }
  }
  if (!wrap) wrap = _scList[_scList.length - 1];
  if (!wrap) return JSON.stringify({stillOpen: false, reason: 'no-modal'});
  const okBtn = Array.from(wrap.querySelectorAll('button')).find(b => (b.textContent||'').trim() === '确定');
  if (!okBtn) return JSON.stringify({stillOpen: false, reason: 'no-ok-btn'});
  okBtn.click();
  await sleep(2000);
  const stillOpen = Array.from(document.querySelectorAll('.ant-modal-wrap'))
    .some(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
  return JSON.stringify({stillOpen});
})()"""


def _guess_size_kind(title: str, sizes: list) -> dict:
    """按尺码字样与标题推断人群档位，供估算提示词用。

    判据优先级：尺码里出现 cm 身高档（80/90/…/160）或标题含童装词 → 童装；
    出现成人字母码（XS/S/M/L/XL）或标题含成人词 → 成人；都不明确 → 通用。
    只影响提示词措辞（专家身份、版型、档差描述），判错不会写坏数据，
    故不做严格校验、也不报错。
    """
    joined = " ".join(str(x) for x in (sizes or []))
    is_kid = bool(re.search(r"\b(80|90|100|110|120|130|140|150|160)\s*cm", joined, re.I)
                  or re.search(r"童|宝宝|婴|幼|kid|child|toddler|girls?|boys?", title, re.I))
    is_adult = bool(re.search(r"\b(XS|S|M|L|XL|XXL|XXXL)\b", joined)
                    or re.search(r"女士|男士|成人|women|men\b", title, re.I))
    # 童装标题里也常出现 girls，故童装判据优先（成人女装不会带 cm 身高档）
    if is_kid and not re.search(r"\b(80|90|100|110|120|130|140|150|160)\s*cm", joined, re.I) and is_adult:
        is_kid = False
    if is_kid:
        return {"expert": "童装尺码专家", "fit": "常见童装版型",
                "step": "衣长约差 3~4cm、胸围全围约差 4~5cm"}
    if is_adult:
        return {"expert": "成人服装尺码专家", "fit": "常见成人版型",
                "step": "胸围全围约差 4cm、衣长约差 2cm"}
    return {"expert": "服装尺码专家", "fit": "该品类常见版型",
            "step": "梯度均匀、不出现跳档"}


def _check_measurements(est: dict, sizes: list, need: list) -> list:
    """校验估算表：单调递增 + 全围量级。返回问题描述列表（空 = 通过）。

    提示词里「单调递增」「全围不是半围」原本没有任何程序校验，而本项目已有
    共识「提示词是软约束，必须在校验层拦」（见 set_titles 的年份闸注释）。
    这里只做能客观判定的两条，不猜绝对值对不对：
      - 递增：按 sizes 给定顺序（源尺码本身有序），后一档不得小于前一档
      - 全围量级：全围类参数若小于同尺码衣长的 0.6 倍，极可能填了半围
    """
    def _row(sz):
        """两种键都试：调用方可能传归一键（"90"）也可能传原始尺码（"90cm"）。
        只按归一键取会在传原始键时静默跳过全部校验——那比不校验更危险。"""
        return (est.get(norm_size(sz)) or est.get(str(sz)) or {})

    problems = []
    for p in need:
        seq = []
        for sz in sizes:
            v = _row(sz).get(p)
            if isinstance(v, (int, float)):
                seq.append((sz, float(v)))
        for (s1, v1), (s2, v2) in zip(seq, seq[1:]):
            if v2 < v1:
                problems.append(f"{p} 在 {s1}->{s2} 反向（{v1}->{v2}），应随尺码递增")
                break
    # 全围疑似半围：与同尺码【衣长】比。
    # 【阈值 0.95 不是 0.6】上装成衣的胸围全围通常 >= 衣长（童装 90cm：衣长约 40、
    # 胸围全围约 60+）；填成半围才会明显小于衣长。原先取 0.6 倍太松——半围 31
    # 对衣长 40 是 0.78 倍，直接漏过，等于这条校验形同虚设。
    # 只拿「衣长」作参照：裤长/裙长远大于腰围全围，用它们比会误报。
    for sz in sizes:
        row = _row(sz)
        length = next((float(v) for k, v in row.items()
                       if str(k).strip() in ("衣长", "上衣长") and isinstance(v, (int, float))),
                      None)
        if not length:
            continue
        for k, v in row.items():
            if ("全围" in str(k) and isinstance(v, (int, float))
                    and float(v) < length * 0.95):
                problems.append(
                    f"{sz} 的 {k}={v} 明显小于衣长 {length}，疑似填了半围（应为绕一圈的全围）")
    return problems


async def _estimate_measurements(title: str, size_ref: dict, sizes: list,
                                 need: list, known: dict) -> dict:
    """按身高体重参考 + 已有实测列，估算弹窗缺的那几个测量参数。

    走 llm.ask_json：它内部用 get_llm()，自动跟随发布页 UI 的模型下拉，顺带白拿
    JSON 解析与三次重试。本管线原先有个写死 [llm.publish] 段的 _llm_publish，与
    下拉脱钩（用户切了 DeepSeek，标题/包装/SKU分类三处还在打 grok，撞上 Packy
    503 限流才暴露），2026-08-23 已全部并到 ask_json 上、该函数删除。

    把已有实测列一起塞进提示词（known）是为了让估算与实测同档：源已给衣长 44 时，
    模型报的胸围应当是同一件衣服的胸围，而不是脱开源数据另算一套版型。
    """
    from app.publish.llm import ask_json

    ref_text = "\n".join(f"- {k}：{v}" for k, v in size_ref.items()
                         if k != "note") or "（无源参考）"
    known_text = "\n".join(
        f"- {s}：" + "、".join(f"{p}{v}" for p, v in (known.get(norm_size(s)) or {}).items())
        for s in sizes if known.get(norm_size(s))
    ) or "（无）"
    # 【别写死童装】档差随人群变：童装按身高 10cm 一档，成人按胸围 4cm 一档，
    # 套错档会让整表系统性失真。本管线确实跑成人女装（见 set_titles 的真站取证
    # offer 846106032776），故按尺码字样与标题推断，拿不准按通用处理。
    kind = _guess_size_kind(title, sizes)
    prompt = (
        f"你是{kind['expert']}。商品：{title}\n"
        f"源商品提供的试穿参考（身高/体重）：\n{ref_text}\n\n"
        f"源商品已给出的实测尺寸（同一件衣服，估算须与之协调）：\n{known_text}\n\n"
        f"请给出尺码 {'/'.join(sizes)} 的实际成衣测量值（单位cm）："
        f"{'、'.join(need)}。\n"
        f"要求：1. 符合{kind['fit']}，数值随尺码单调递增、梯度合理"
        f"（相邻尺码{kind['step']}）；\n"
        "2. 全围类参数（胸围/腰围/臀围全围）是绕一圈的全围，不是半围——"
        "半围写成全围会差一倍，这是买家退货的高发成因；\n"
        "3. 每个尺码都要给，且只给上面列出的参数；\n"
        f"只输出严格JSON，不要其他文字："
        f"{{\"{sizes[0]}\":{{\"{need[0]}\":x}},...}}"
    )
    data = await ask_json(prompt, what="尺码表测量值估算", stage="sizechart")
    est = {norm_size(k): v for k, v in data.items() if isinstance(v, dict)}

    # 校验不过就把具体问题回喂重生成一次。全自动路线：不转人工、不中断，
    # 重试后仍不过也照用（估算值本身是兜底数据，卡住整个商品代价更大），
    # 但把问题写进 warning 留痕，便于事后按日志抽查。
    problems = _check_measurements(est, sizes, need)
    if problems:
        logger.warning("尺码估算校验未过，重生成一次：" + "；".join(problems[:3]))
        retry = await ask_json(
            prompt + "\n\n上次输出有这些问题，请修正后重新给全表："
            + "；".join(problems) + "\n注意全围是绕一圈的周长，数值必须随尺码递增。",
            what="尺码表测量值估算(重试)", stage="sizechart",
        )
        est2 = {norm_size(k): v for k, v in retry.items() if isinstance(v, dict)}
        left = _check_measurements(est2, sizes, need)
        if len(left) < len(problems):
            est = est2
            problems = left
        if problems:
            logger.warning("尺码估算重试后仍有疑点（照用，已留痕）："
                           + "；".join(problems[:3]))
    return est


async def add_sizechart(session: BrowserSession, info_path: str,
                        category: Optional[str] = None,
                        name: Optional[str] = None) -> dict:
    """阶段⑨：添加尺码表（尺码分类 + 测量参数填表）。

    流程：
    1. 点「添加尺码表」入口（.skuAttrSizeChart 里的 span.link）
    2. 确认尺码分类：默认跟随平台按已选类目预选的值，category 给了才按关键词改
    3. 读取弹窗参数列表（衣长/胸围全围/袖长...）和尺码行（80/90/100...）
    4. 测量值来源（按参数逐列组合，不是二选一）：
       - 源 product-info.json 的 sizeMeasurements（实测平铺尺寸）给了哪列就用哪列
       - 弹窗要而源没有的列，交当前选择的模型估算（跟随发布页 UI 下拉，见 llm.get_llm）
    5. 参数键模糊对齐（胸围 ↔ 胸围全围），对齐后仍缺值报错不填半残表
    6. 填模板名 + 表格，点确定，回读验证

    实测要点（2026-08-18）：
    - 多弹窗陷阱：重复执行会叠多个「添加尺码表」弹窗，开新前先全关掉
    - 测量参数：分类强制的，取消勾选无效，必须填值
    - 基码表（身高/体重）：系统自动填充，不支持手动录入

    【尺码分类不要写死关键词】2026-08-22 实测 947662049255（女童网纱连衣裙）：
    该下拉的选项由页面已选类目决定（这里只有「女童装-连衣裙」一项）且平台已预选好，
    原先默认拿 "上装" 去匹配，报 option-not-found 使整个商品未落库。类目在阶段③
    已经选定，平台据此给的分类比这里猜的准，故默认不指定、只做确认。
    """
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    title = info.get("title", "")
    size_ref = info.get("sizeChart") or {}
    # 模板名去年份：源标题惯用「2026新款」，前 10 字硬截取会把年份带进模板名
    clean_title = _strip_dated(title)
    tpl_name = name or ((clean_title[:10] + "尺码表") if clean_title else "通用尺码表")

    # 已添加则直接返回
    st = await session.eval_json(_JS_SIZECHART_STATE)
    if st.get("found") and "添加尺码表" not in st.get("text", ""):
        return {"status": "ok", "skipped": True, "reason": "尺码表已存在",
                "current": st.get("text")}

    # 多弹窗陷阱：开新前先关闭残留弹窗
    await session.eval_json(r"""(async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const list = () => Array.from(document.querySelectorAll('.ant-modal-wrap'))
        .filter(m => (m.textContent||'').includes('添加尺码表') && getComputedStyle(m).display !== 'none');
      let n = 0;
      for (const w of list()) {
        const cancel = Array.from(w.querySelectorAll('button')).find(b => /取消|关闭/.test((b.textContent||'').trim()))
          || w.querySelector('.ant-modal-close');
        if (cancel) { cancel.click(); await sleep(1000); n++; }
      }
      return JSON.stringify({closed: n});
    })()""")

    # 打开弹窗
    opened = await session.eval_json(_JS_OPEN_SIZECHART_MODAL)
    if not opened.get("opened"):
        return {"status": "error", "reason": f"添加尺码表弹窗未打开: {opened}"}

    # 确认尺码分类（默认跟随平台预选，category 给了才按关键词改）
    sel = await session.eval_json(_JS_SET_SIZECHART_CAT.replace("__CAT__", J(category)))
    if not sel.get("ok"):
        return {"status": "error", "reason": f"尺码分类选择失败: {sel}"}

    # 读取参数列表和尺码行
    meta = await session.eval_json(_JS_SIZECHART_PARAMS)
    params, sizes = meta.get("params", []), meta.get("sizes", [])
    if not params or not sizes:
        return {"status": "error", "reason": f"弹窗参数/尺码行读取失败: {meta}"}

    # 参数键对齐（弹窗强制参数名可能是「胸围全围」，而源数据返回「胸围」）
    def _align_params(vals):
        out = {}
        for p in params:
            if p in vals:
                out[p] = vals[p]
                continue
            cand = [k for k in vals if k and (k in p or p in k)]
            if cand:
                out[p] = vals[cand[0]]
        return out

    # 测量值来源：源商品实测平铺尺寸（sizeMeasurements）优先，弹窗要而源没有的参数交模型估算。
    # 尺码键与弹窗尺码行两侧都过 norm_size 再比（源键可能带「建议身高」描述，见 norm_size）
    #
    # 【为什么按参数补而不是整块判死】2026-08-22 实测 984360345330（套头衫）：源尺码表只有
    # 衣长/裤长两列（店家套了套装模板），弹窗按类目强制的胸围全围/袖长一个都没有。原先
    # 「有源数据就必须全齐，否则报错要人工清空字段走兜底」——源给了一半反倒比完全没有更糟。
    # 改为源给的照用（实测值比估算准），只把缺的那几列问模型，人工不必再介入。
    src_meas = info.get("sizeMeasurements") or {}
    norm = {norm_size(k): _align_params(v or {}) for k, v in src_meas.items()}
    need = [p for p in params
            if any(p not in (norm.get(norm_size(s)) or {}) for s in sizes)]
    if need:
        est = await _estimate_measurements(title, size_ref, sizes, need, norm)
        for s in sizes:
            key = norm_size(s)
            row = dict(norm.get(key) or {})
            # 只填 need 里且本行确实没有的：模型值绝不覆盖源实测值
            for p, v in _align_params(est.get(norm_size(s)) or {}).items():
                if p in need and p not in row:
                    row[p] = v
            norm[key] = row
        gen = "source+model" if src_meas else "model"
    else:
        gen = "source"

    # _JS_FILL_SIZECHART 按【页面原始尺码文本】取 data[size]，故归一只用于匹配，
    # 最终 norm 的键必须换回页面原文，否则填表时全部取不到值
    norm = {s: norm.get(norm_size(s), {}) for s in sizes}
    lacking = [s for s in sizes
               if not norm.get(s) or any(p not in norm[s] for p in params)]
    if lacking:
        return {"status": "error",
                "reason": f"测量数据缺参数（对齐后仍缺）: 尺码{lacking} × 参数{params}"
                          f"；来源 {gen}，模型应补 {need}",
                "data": norm}

    # 填表格
    fill = await session.eval_json(_JS_FILL_SIZECHART
                                    .replace("__NAME__", J(tpl_name))
                                    .replace("__DATA__", J(norm))
                                    .replace("__PARAMS__", J(params)))
    if not fill.get("ok"):
        return {"status": "error", "reason": f"表格填充不完整: {fill.get('empty')}",
                "data": norm}

    # 点确定
    done = await session.eval_json(_JS_CLICK_SIZECHART_OK)
    if done.get("stillOpen"):
        return {"status": "error", "reason": "点确定后弹窗未关闭（校验未过？）", "fill": fill}

    # 回读验证
    final = await session.eval_json(_JS_SIZECHART_STATE)
    ok = tpl_name in final.get("text", "")
    return {"status": "ok" if ok else "validation-error",
            "tplName": tpl_name, "category": sel.get("selected"),
            "categorySource": sel.get("source"),
            "params": params, "measureSource": gen, "estimated": need,
            "data": norm, "formText": final.get("text")}


# 属性审核提示词：七条规则全部来自原 skill 的实战积累，别精简。
# 规则4（只填必填）是「填得多错得多」的直接对策，2026-08-24 起口径收紧为「非必填未填的
# 一律留空、源商品写了也不填」（原先给源里有值的非必填行开的口子已撤，见 dump_attrs）；
# 规则5（成分和=100）对应平台硬校验；
# 规则6（里料纹理）单独点出「一体绒不算单独里衬」——这条错过就会联动出一堆必填行。
_ATTR_PROMPT = """你是跨境电商商品属性审核助手。下面是 1688 源商品信息和店小秘 Temu 半托管表单的属性现状。
任务：判断表单每个属性是否与商品实际相符，产出需要修改的清单。
规则：
1. 修改值必须从该行 options 列表中选取，禁止编造；options 为空且没有合适选项时不要改。
2. current 与商品明显矛盾的必须改（这是本任务的核心职责，不属于"拿不准"）：
   例如圆领套头毛衣的"细节"却是"露肩"、一体绒商品的"里料纹理"却是"无里料"——
   这类要直接从 options 中选语义最接近的一项改掉。装饰/图案/细节类以图片理解摘要为准，
   在 options 里找包含关键特征的精确选项（如图示蝴蝶结在胸前→"前蝴蝶结"而非"后蝴蝶结"）。
3. current 已正确或确实拿不准（源信息和图片都无法判断）的才不动，放 notes。
4. 填写范围：只填【必填项】（required=true），且**必填项必须填全**——
   平台对必填项是硬校验，留空会直接卡在保存、整个商品发不出去。
   current 为 "(请选择)" 的必填项一律要给值；options 里实在没有对应项时，
   选语义最接近的一项，不要留空。
   【源头无信息时怎么填】按字段性质分两类：
   (a) 客观物性（里料克重 g/m²、里衬成分、里料纹理、版型、季节、厚薄等）：
       按该品类的面料常识给行业通行值即可，这类不构成对买家的承诺。
   (b) 对买家的功能性承诺（护理说明能否机洗/干洗、安全等级、功效宣称等）：
       源商品没写时选 options 里**最保守**的那一项（如洗涤只选「可手洗」
       而不选「可机洗且可干洗」，拿不准的护理方式不要许诺），
       并在 notes 里记一句便于事后抽查。填错这类要担责且买家可证伪。
   非必填项（required=false）当前未填的一律留空，即使「源商品参数」里给了对应值也不要填
   —— 这是硬规则，不要为了信息完整去补。非必填项【已有值】且与商品明显矛盾时才纠正
   （那是改错，不属于多填）。
5. 成分类属性（上装成分/下装成分/里衬成分等带百分比的字段）：同一字段所有成分行的
   百分比之和必须恰好等于 100（平台硬性校验）。源商品只给主成分含量（如棉90%）时，
   差额按常见配比补足：优先加一行「聚酯纤维(涤纶）」凑到 100%；options 没有聚酯纤维时
   再按属性自洽推导（如面料=微弹→加「氨纶」）。多成分输出多条同 label 的 change，
   用 row 字段（从 1 起）区分第几行：row=1 覆盖当前行，row>=2 是新增成分行。
   上装成分/下装成分等【主面料】字段的第 1 行纤维必须选与「源主面料成分」一致的选项
   （options 里的写法可能带括号注解，选语义相同的那个），其 num 由程序按源含量覆盖，
   你给的数值不作准；里衬/里料/辅料/填充类字段不受源主面料含量约束。
   【源没给含量时】不要自己拆成两行去编比例：程序会按「该纤维 100%」单行写入
   （源连主面料成分都没写时按聚酯纤维 100%）。此时主面料字段只需给第 1 行。
6. 里料纹理/里衬类字段有联动必填：选「光面」「绒面/PU」等会动态新增必填行
   （里衬成分、里料克重），填错风险大。源商品没有明确单独里衬时一律选「无里料/无内衬」。
   特别注意：「一体绒」是绒与面料一体成型，不算单独里衬，必须选「无里料/无内衬」。
   含"针织""毛衣"时织造方式应为针织类。
7. 源商品的"风格"若在 options 中没有，保留当前值。
8. 【数值输入行】kind="number" 的行（如里料克重、含绒量）没有 options，是纯数字输入框：
   value 只给**纯数字**，不要带单位、不要给选项文本（numHint.unit 告诉你单位，numHint.placeholder 可能有取值提示）。
   源商品没写时按该品类常识给行业通行值（如童装梭织里布 60~90 g/m²、摇粒绒 180~260 g/m²），
   这类是客观物性、不构成对买家的承诺，必须填而不能留空——留空平台会拦，整个商品发不出去。

商品标题：{title}
源商品参数（1688）：{src_attrs}
源主面料成分（已从源参数解析，百分比以此为准）：{main_composition}
图片理解摘要：{image_understanding}
表单属性现状：{rows}

只输出JSON: {{"changes": [{{"label": "属性名", "value": "选项文本", "num": 数值或null, "row": 行号或null, "reason": "一句话理由"}}], "notes": ["需要人工判断的存疑点"]}}"""

# 成分补差的候选填充纤维（按优先级）。聚酯纤维是最常见的混纺配料；
# options 里没有时退到氨纶（弹性面料常见）。都没有则拒绝该组、交人工。
#
# 【2026-08-21 补】候选必须剔除该组已占用的纤维：源主成分本身就是聚酯纤维 90% 时，
# 原实现会再补一行聚酯纤维 10%，同一字段两行同纤维，平台校验必拦。
_COMP_FILLERS = ("聚酯纤维(涤纶）", "聚酯纤维", "氨纶", "棉")

# 成分字段里哪些算「主面料」：只有主面料行的百分比能用源商品的主面料成分含量覆盖。
# 里衬/里料/辅料/填充/内衬是另一块布料，与源主面料含量无关，必须排除。
_COMP_SUB_MARKS = ("里衬", "里料", "内衬", "辅料", "填充", "内里")


def _is_main_comp_label(label: str) -> bool:
    """判断某成分字段是否属于主面料（上装成分/下装成分/材质 等）。"""
    if not label or ("成分" not in label and "材质" not in label):
        return False
    return not any(m in label for m in _COMP_SUB_MARKS)


def _norm_fiber(name: str) -> str:
    """纤维名归一：只留中文，去掉半/全角括号与注解差异。

    源属性写「聚酯纤维（涤纶）」（全角括号），表单 options 写「聚酯纤维(涤纶）」
    （半角左括号 + 全角右括号，平台自己就不对称），直接字符串比必然不等。
    """
    return "".join(ch for ch in (name or "") if "一" <= ch <= "鿿")


# 纤维同义组：同一根纤维在源属性/表单 options 里的不同写法。
# 【为什么不能只靠去括号归一】_norm_fiber 只删括号，「聚酯纤维(涤纶）」「聚酯纤维」
# 「涤纶」归一后是三个不同字符串，去重全部失效——实测会填出「涤纶 80% + 聚酯纤维
# 20%」，同一字段两行同纤维，平台必拦（2026-08-24 用户报错）。正确结果是合并成
# 一行 100% 涤纶。每组第一个元素当规范键，仅用于判同、不用于填表。
_FIBER_SYNONYMS = (
    ("聚酯纤维", "涤纶", "涤", "聚酯", "PET", "polyester"),
    ("氨纶", "莱卡", "弹性纤维", "spandex", "elastane", "lycra"),
    ("锦纶", "尼龙", "聚酰胺纤维", "nylon", "polyamide"),
    ("粘纤", "粘胶纤维", "黏胶纤维", "粘胶", "人造棉", "viscose", "rayon"),
    ("腈纶", "聚丙烯腈纤维", "acrylic"),
    ("棉", "棉纤维", "cotton"),
    ("羊毛", "毛", "wool"),
    ("羊绒", "山羊绒", "cashmere"),
    ("莫代尔", "modal"),
    ("亚麻", "麻", "linen"),
    ("蚕丝", "真丝", "桑蚕丝", "silk"),
    ("竹纤维", "竹浆纤维"),
    ("醋酸纤维", "醋酸", "acetate"),
)


def _fiber_key(name: str) -> str:
    """纤维同义归一：返回规范键，用于判断两个写法是否同一根纤维。

    先按 _norm_fiber 去括号（表单写法括号半全角不对称），再查同义组。
    命中同义组里任一别名就返回该组第一个元素作为键；没命中就返回去括号结果
    （未知纤维仍能自比，只是享受不到同义合并）。

    注意匹配用「包含」而非相等：表单选项常写「聚酯纤维(涤纶）」这种带注解的形式。
    别名按长度降序试，避免「棉」抢在「棉纤维」之前误命中更长的名字。
    """
    norm = _norm_fiber(name)
    if not norm:
        return ""
    low = (name or "").lower()
    for group in _FIBER_SYNONYMS:
        for alias in sorted(group, key=len, reverse=True):
            a = _norm_fiber(alias)
            if (a and a in norm) or (not a and alias.lower() in low):
                return group[0]
    return norm


def _merge_same_fiber(items: list, opts: list) -> list:
    """把同一根纤维的多行合并成一行（百分比相加），并重排 row。

    这是「涤纶 80% + 聚酯纤维 20%」的正解：不是并列两行，而是一行 100% 涤纶。
    值取组内在 options 里存在的写法（表单只认 options 内的值），都不在则取首个。
    """
    groups: dict = {}
    order: list = []
    for it in items:
        k = _fiber_key(it.get("value", ""))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    merged = []
    for idx, k in enumerate(order, start=1):
        rows = groups[k]
        if len(rows) == 1:
            merged.append({**rows[0], "row": idx})
            continue
        total = sum(float(r.get("num") or 0) for r in rows)
        value = next((r["value"] for r in rows if r.get("value") in opts),
                     rows[0].get("value"))
        pcts = "+".join(str(r.get("num")) for r in rows)
        merged.append({**rows[0], "value": value, "num": int(total), "row": idx,
                       "reason": f"同一纤维 {len(rows)} 行已合并（{pcts}={int(total)}%）"})
        logger.info(f"成分合并：{[r.get('value') for r in rows]} → {value} {int(total)}%")
    return merged


def _match_fiber(src_fiber: str, values: list) -> Optional[str]:
    """在候选值里找与源主纤维对应的那一项，找不到返回 None（不猜）。

    先按归一后完全相等匹配；不中再退到包含关系（源「聚酯纤维」对上表单
    「聚酯纤维(涤纶）」），包含命中多项时取最短的，避免「棉」误配到长名纤维上。
    """
    src = _norm_fiber(src_fiber)
    if not src:
        return None
    exact = [v for v in values if _norm_fiber(v) == src]
    if exact:
        return exact[0]
    part = [v for v in values if src in _norm_fiber(v)]
    if part:
        return min(part, key=lambda v: len(_norm_fiber(v)))
    return None


# 按源属性推断配料纤维的依据表：(属性文本匹配正则, 目标纤维关键词)。
# 顺序即优先级，越靠前依据越硬。这是为了少走 _COMP_FILLERS 那条纯编造的路。
_COMP_HINTS = (
    (r"氨纶|弹力|微弹|莱卡|spandex", "氨纶"),
    (r"摇粒绒|珊瑚绒|法兰绒|抓绒|涤纶|涤|聚酯", "聚酯纤维"),
    (r"锦纶|尼龙|nylon", "锦纶"),
    (r"粘纤|粘胶|人造棉|莫代尔|viscose", "粘纤"),
    (r"羊毛|羊绒|马海毛", "腈纶"),
    (r"棉", "棉"),
)

# 常见纤维名，用于在源属性里找「第二种纤维」——源自己写了两种纤维时那是最硬的依据。
_FIBER_NAMES = ("聚酯纤维", "涤纶", "氨纶", "锦纶", "尼龙", "棉", "腈纶",
                "粘纤", "粘胶", "莫代尔", "羊毛", "羊绒", "亚麻", "麻",
                "蚕丝", "真丝", "竹纤维", "醋酸")


def _infer_filler(attrs: dict, main_fiber: str, opts: list, used_key: str):
    """按源属性推断配料纤维，返回 (选项文本, 依据说明) 或 (None, None)。

    先找源属性里明写的第二种纤维（最硬依据），再按面料/工艺特征推导。
    两条都不中时返回 None，由调用方退到 _COMP_FILLERS。
    """
    blob = " ".join(str(v) for v in (attrs or {}).values())[:800]

    # (a) 源属性里明写的第二种纤维
    for nm in _FIBER_NAMES:
        if nm in blob and _fiber_key(nm) != used_key:
            hit = _match_fiber(nm, opts)
            if hit:
                return hit, f"源属性提到「{nm}」"

    # (b) 按面料/工艺特征推导
    for pat, fiber in _COMP_HINTS:
        if re.search(pat, blob, re.I) and _fiber_key(fiber) != used_key:
            hit = _match_fiber(fiber, opts)
            if hit:
                return hit, f"源属性含「{re.search(pat, blob, re.I).group(0)}」推导"
    return None, None


def _rebuild_main_comp(label: str, items: list, opts: list, main_comp: dict) -> tuple:
    """按源商品的主面料成分含量重建某个主面料成分字段的成分行，返回 (rows, reject)。

    这是「成分比例不靠模型」的落点。源页面只给一个确定事实——主面料纤维 + 其含量
    （见 extract.parse_main_composition），所以能确定性构造的也只有两行：
      第 1 行 = 源主纤维 @ 源含量；第 2 行 = 剩余份额给一种配料纤维。
    模型给的百分比一律丢弃（它在两次调用间会从 55/45 漂到 90/10），但模型选的
    【配料纤维种类】保留——那是它看图/看参数得出的定性判断（如微弹→氨纶），
    比写死的候选表更贴近实物，只是数值不由它定。

    源主纤维在 options 里找不到对应项时返回 (None, None)，交回原有路径，不硬凑。

    【2026-08-25 起 main_comp 恒非空】parse_main_composition 在源没写含量、甚至没写
    主面料成分时也会给出「某纤维 100%」并打 assumed 标记；此时 pct=100，下面单行返回、
    不进补差分支——正是用户要的「源没写就全部写一种纤维 100%」。
    """
    target = _match_fiber(main_comp.get("fiber", ""), opts)
    if not target:
        return None, None
    pct = int(main_comp["percent"])
    rows = [{"label": label, "value": target, "num": pct, "row": 1,
             "reason": (main_comp.get("assumed")
                        or f"源主面料成分含量 {main_comp.get('raw') or pct}，按源值写入")}]
    if pct >= 100:
        return rows, None
    # 配料纤维三级依据（越靠前越硬）：
    #   1. 模型选的（它看图/看参数得出的定性判断）
    #   2. 源属性推断（明写的第二种纤维，或面料/工艺特征）
    #   3. 写死的候选表——纯编造，只在前两条都不中时用
    # 用 _fiber_key 判同（同义归一）：_norm_fiber 只去括号，认不出涤纶=聚酯纤维
    used = _fiber_key(target)
    basis = "配料种类沿用模型判断"
    second = next((i["value"] for i in items
                   if _fiber_key(i.get("value", "")) != used and i.get("value") in opts), None)
    if not second:
        second, why = _infer_filler(main_comp.get("srcAttrs") or {}, target, opts, used)
        if second:
            basis = why
    if not second:
        second = next((o for o in _COMP_FILLERS
                       if o in opts and _fiber_key(o) != used), None)
        if second:
            # 这一行是全流程唯一「凭常见配比编出来」的成分数据，留痕便于抽查
            basis = "无源依据，按最常见混纺配料填充"
            logger.warning(f"成分补差无源依据，{label} 第2行填「{second}」"
                           f"{100 - pct}%（源只给了 {target} {pct}%）")
    if not second:
        return None, {"label": label,
                      "rejectReason": f"主成分 {pct}% 需补差但 options 无可用配料纤维"}
    rows.append({"label": label, "value": second, "num": 100 - pct, "row": 2,
                 "reason": f"补差 {100 - pct}% 凑足 100%（{basis}）"})
    return rows, None


def _validate_attr_changes(changes: list, attrs: list,
                           main_comp: Optional[dict] = None) -> tuple:
    """对 LLM 的修改清单做二次校验，返回 (valid, rejected)。

    LLM 会编造不存在的选项、也会违反「非必填不填」的策略，故不能直接执行。四道闸：
      1. 非必填且当前未填 → 拒（策略：填得多错得多）
      2. 下拉行 value 不在该行 options 内 → 拒（编造值点不中，白跑一趟还留幽灵浮层）；
         数值输入行（kind=number，如里料克重）没有 options，改校验量级合理性，
         并把 LLM 常带的单位剥掉只留数字
      3. 主面料成分字段：有源含量时按源值重建成分行，模型给的百分比不作准
      4. 其余成分类合计必须 100%：不足则自动补一行填充纤维，超过则整组拒绝交人工

    main_comp 为 extract.parse_main_composition 的产物（可能为 {}）：为空时第 3 闸
    整个跳过，退回原有的「模型给数 + 合计校验」路径——不为了凑数去猜源页面没写的东西。
    """
    import json as _json

    valid: list = []
    rejected: list = []
    row_map = {a["label"]: a for a in attrs}
    opt_map = {a["label"]: a.get("options", []) for a in attrs}
    for c in changes:
        label = c.get("label")
        row = row_map.get(label, {})
        cur = row.get("current") or ""
        if not row:
            rejected.append({**c, "rejectReason": "表单没有这个属性行"})
        elif not row.get("required") and cur.startswith("("):
            # 【不看 options 是否为空，只看必填与否】2026-08-24 起非必填未填的行一律
            # 留空，源商品写了也不填。原先这里放行「options 非空」的非必填行（那是
            # dump_attrs 按源键匹配特意读出来的），随那个例外一起撤掉；required_only=
            # False 的探查场景会给所有行都读上 options，用旧写法等于把闸门整个打开。
            rejected.append({**c, "rejectReason": "非必填且当前未填，按策略留空"})
        elif row.get("kind") == "number":
            # 【数值行不查 options】里料克重这类纯输入行 options 恒为空，走 options 闸
            # 会把每个值都拒掉，该行永远填不上、保存卡在「请输入产品属性」
            # （2026-08-24 用户截图的自动化断点）。改为只校验量级合理性。
            # 取值：优先 value，空则回退 num（LLM 两处都可能放数值）
            raw = str(c.get("value") or "").strip()
            if not raw and c.get("num") is not None:
                raw = str(c.get("num")).strip()
            # 【负号必须纳入匹配】只写 \d+ 会从 "-5" 里提出 "5"，负数就被放行了
            m = re.search(r"-?\d+(?:\.\d+)?", raw)
            if not m:
                rejected.append({**c, "rejectReason": f"数值行但给的不是数字：{raw!r}"})
            elif not (0 < float(m.group()) <= 100000):
                rejected.append({**c,
                                 "rejectReason": f"数值 {m.group()} 超出 0~100000 合理范围"})
            else:
                # 统一成纯数字串写入：LLM 常带上单位（"120g/m²"），输入框只收数字
                valid.append({**c, "value": m.group(), "kind": "number"})
        elif c.get("value") in opt_map.get(label, []):
            valid.append(c)
        else:
            rejected.append({**c, "rejectReason": "value 不在 options 内，已拒绝"})

    # 成分和=100 校验：同一成分字段的 num 合计
    #
    # 【分组判据是行结构，不是「LLM 给没给 num」】原先写 `if c.get("num")`：模型漏给
    # num（或给 0/null）时那行根本不进分组，于是「合计=100」这道闸整个不执行，
    # set_attr 里 `num is not None` 也不成立、百分比框不填——纤维选上了、百分比空着，
    # 平台报「请完善里衬成分信息」（2026-08-25 用户截图实测）。闸本身没坏，是该进闸
    # 的行没进来，而这恰恰在模型输出不全时才发生，正是最该兜住的场景。
    # 现在按枚举给的 hasPercent（下拉后面跟可填 input）认成分行：是不是成分行由 DOM
    # 决定，模型漏 num 就走下面的补差逻辑填满 100%，而不是静默留空。
    comp: dict = {}
    for c in valid:
        # 【数值属性行必须排除】里料克重 90 g/m² 这类的数值可能放在 num 里，被这里
        # 收进成分分组就会按「合计必须 100」拒掉（实测报「成分合计 90%<100%」），
        # 该行又填不上了。克重是物性数值，与成分百分比无关。
        if c.get("kind") == "number":
            continue
        row_meta = row_map.get(c["label"]) or {}
        is_comp = row_meta.get("hasPercent") or bool(c.get("num"))
        if is_comp:
            comp.setdefault(c["label"], []).append(c)
    for label, items in comp.items():
        opts = opt_map.get(label, [])
        # 主面料成分：源含量说了算，先按源值重建整组，重建成功就不再走合计校验
        if main_comp and main_comp.get("percent") and _is_main_comp_label(label):
            rows, reject = _rebuild_main_comp(label, items, opts, main_comp)
            if rows:
                valid = [v for v in valid if v["label"] != label] + rows
                continue
            if reject:
                rejected.append(reject)
                valid = [v for v in valid if v["label"] != label]
                continue
            logger.warning(
                f"{label}: 源主纤维「{main_comp.get('fiber')}」不在 options 内，"
                "退回模型给数 + 合计校验")
        # 先合并同一根纤维的多行：模型可能同时给出「涤纶 80%」和「聚酯纤维 20%」，
        # 那是同一根纤维的两种写法，平台不接受同字段两行同纤维（2026-08-24 实测报错），
        # 正解是合并成一行 100%。合并后再算合计，多数情况直接就等于 100 了。
        merged = _merge_same_fiber(items, opts)
        if len(merged) != len(items):
            valid = [v for v in valid if v["label"] != label] + merged
            items = merged
        total = sum(i["num"] for i in items if i.get("num"))
        # 【模型漏给 num 的行先按「独占剩余」补满，再判合计】否则 total=0 会掉进下面的
        # 补差分支，去补一根【别的】纤维 100%，把模型真正选中的那根留在 0%——里衬成分
        # 只有一行时表现就是「棉Cotton 选上了、百分比空着」。单行独占 100 是成分行最
        # 常见的形态（里衬/辅料基本都是单一纤维），故这里按剩余量补给缺 num 的行：
        # 一行缺就补满剩余，多行缺就均分（除不尽的余数给第一行，保证合计精确等于 100）。
        blank = [i for i in items if not i.get("num")]
        if blank and total < 100:
            share, rest = divmod(100 - total, len(blank))
            for k, i in enumerate(blank):
                i["num"] = share + (rest if k == 0 else 0)
                i["reason"] = (str(i.get("reason") or "")
                               + f"（模型未给含量，自动补 {i['num']}%）")
            total = sum(i["num"] for i in items if i.get("num"))
        if total == 100:
            continue
        if total > 100:
            rejected.append({"label": label,
                             "rejectReason": f"成分合计 {total}%>100%，整组拒绝，需人工核对"})
            valid = [v for v in valid if v["label"] != label]
            continue
        # 填充纤维要排除该组已占用的：主成分本身是聚酯纤维时再补一行聚酯纤维必被平台拦。
        # 【必须用 _fiber_key 而不是 _norm_fiber】后者只去括号，「涤纶」与「聚酯纤维」
        # 归一后不相等，排除会失效并填出同字段两行同纤维。
        used = {_fiber_key(i.get("value", "")) for i in items}
        filler = next((o for o in _COMP_FILLERS
                       if o in opts and _fiber_key(o) not in used), None)
        if filler:
            valid.append({"label": label, "value": filler, "num": 100 - total,
                          "row": max((i.get("row") or 1) for i in items) + 1,
                          "reason": f"补差 {100 - total}% 凑足 100%（自动）"})
        else:
            rejected.append({
                "label": label,
                "rejectReason": f"成分合计 {total}%<100% 且 options 无可用填充纤维"})
            valid = [v for v in valid if v["label"] != label]

    # 同字段多行按 row 升序：先覆盖第 1 行再加新行，否则加行时行号对不上
    valid.sort(key=lambda c: (c["label"], c.get("row") or 1))
    return valid, rejected


# 单行重选的窄提示词：只给一行的信息，用重读到的真实 options 重新选一个值。
# 规则只从 _ATTR_PROMPT 抽相关的两条（必须从 options 里选、拿不准就不改），
# 并允许 value=null 明确表示「没有合适的，别动这行」。
_ATTR_ROW_PROMPT = """你是跨境电商商品属性审核助手。表单某一行的下拉选项已经变化，需要按最新选项重新选值。

规则：
1. 值必须从下面的 options 列表里原样选取，禁止编造、禁止改写措辞。
2. 没有语义合适的选项时返回 {{"value": null}}——保持原样比填错好。

商品标题：{title}
属性名：{label}
该行当前值：{current}
是否必填：{required}
源商品对应参数值：{src_value}
最新可选项：{options}

只输出JSON: {{"value": "选项文本或null", "reason": "<一句话理由>"}}"""


async def _refresh_row_and_retry(session: BrowserSession, change: dict, row: dict,
                                 cat_path, title: str = "", src_value: str = "",
                                 use_cache: bool = True, site: str = "") -> dict:
    """某行按缓存 options 写入失败时，只重读这一行的真实选项再试一次。

    为什么只重读一行而不整体退回全量遍历：变的是【那一个下拉】，其余行的缓存仍然
    有效；整体回退要重花 90s 去证明另外二十多行没变。

    两条分支的分工：
      - 重读后目标值仍在 options 里 → 缓存没过期，失败是点击/重渲染问题，直接原值
        再试一次，不白花一次 LLM 调用（set_attr 内部已自愈重试过一次，但这里隔了一次
        真实的下拉开合，DOM 状态已刷新）。
      - 目标值确实没了 → 选项集真的变了，用新 options 单独问一次 LLM 重选。

    成分类行（num 不为 None）【不该进这个函数】，由调用方拦住：_read_active_options
    内部调 _open_attr_dropdown 时不传 sel_idx（走默认 0），成分第 2 行根本读不到；
    且单换一根纤维会破坏 _validate_attr_changes 第 4 道闸保证的「同字段合计 100%」，
    平台硬性校验必拦。
    """
    from app.publish.llm import ask_json

    label = change["label"]
    opts, meta = await _read_active_options(session, label, with_meta=True)
    if not opts:
        return {"status": "error", "value": change.get("value"), "options": [],
                "askedLLM": False, "readback": None,
                "reason": "重读选项为空（行隐藏或下拉点不开）"}

    # 【回灌缓存必须在分支之前】不管这次能不能救回来，那份过期数据都得换掉，
    # 否则下一个同类目商品还会撞同一堵墙。这是「写入即校验」策略真正起作用的地方。
    # 但截断的清单不回灌——那会把过期数据换成缺项数据，同样没人能发现。
    if use_cache and cat_path and meta["complete"]:
        cache.update_attr_row(cat_path[-1], cat_path, label, opts, site)

    if change.get("value") in opts:
        r = await set_attr(session, label, change["value"], None, 1)
        return {"status": r.get("status"), "value": change["value"], "options": opts,
                "askedLLM": False, "readback": _readback_current(r),
                "reason": "选项未变，原值重试"}

    data = await ask_json(
        _ATTR_ROW_PROMPT.format(
            title=title or "", label=label, current=row.get("current") or "",
            required=bool(row.get("required")), src_value=src_value or "（源未给）",
            options=json.dumps(opts, ensure_ascii=False)),
        what=f"属性单行重选（{label}）", stage="attrs",
    )
    value = data.get("value")
    reason = str(data.get("reason", ""))
    # 模型照样会编造，必须再过一遍 options 闸
    if not value or value not in opts:
        return {"status": "error", "value": value, "options": opts, "askedLLM": True,
                "readback": None,
                "reason": f"LLM 重选的值不在 options 内或为空（{reason}）"}
    r = await set_attr(session, label, value, None, 1)
    return {"status": r.get("status"), "value": value, "options": opts,
            "askedLLM": True, "readback": _readback_current(r),
            "reason": reason}


# 属性审核拆组的行数阈值：超过就分两组并发问。
#
# 【为什么要拆】2026-08-24 实测：34 行一次问，推理型模型烧 7523 completion token
# 要 65 秒，而推理量基本随行数走——拆两组并发后墙钟约减半。输入侧不是瓶颈
# （34 行连 options 才 4KB / 5131 input token），所以拆组几乎不增加成本。
#
# 阈值 20 而不是更小：行数少时拆组省下的时间抵不过多一次调用的固定开销
# （建连 + 首 token 延迟），而且组越小模型能看到的上下文越少、判断越容易漂。
_ATTR_SPLIT_MIN_ROWS = 20


def _split_attr_rows(rows: list) -> list:
    """把属性行分成若干组供并发审核，返回 [组1, 组2, ...]；不值得拆时返回 [rows]。

    【成分类字段必须整组落在同一次调用里】_ATTR_PROMPT 规则 5 要求「同一字段所有
    成分行的百分比之和恰好 100」（平台硬性校验），模型得同时看到该字段的全部行才
    算得出来。把「上装成分」的两行劈到两次调用里，两边各自凑 100%，合起来 200%
    必被平台拦。故先把同名字段的行绑成不可分的整体，再按整体分配到组。

    其余行之间没有跨行约束（每行只看自己的 options 与源值），可以任意切分。
    分配用「轮流放入当前较小的组」而不是按下标对半砍：成分字段可能占好几行，
    按下标砍容易切出一个 20 行 + 一个 8 行的组，并发就白拆了。
    """
    if len(rows) < _ATTR_SPLIT_MIN_ROWS:
        return [rows]

    # 同 label 的行绑成一个不可分单元（成分类字段会有多行同名）
    units: dict = {}
    order: list = []
    for r in rows:
        label = r.get("label")
        if label not in units:
            units[label] = []
            order.append(label)
        units[label].append(r)

    groups: list = [[], []]
    # 先放行数多的单元，避免大单元最后进来把两组撑得一边倒
    for label in sorted(order, key=lambda k: -len(units[k])):
        target = min(groups, key=len)
        target.extend(units[label])
    return [g for g in groups if g]


async def _ask_attr_review(rows: list, info: dict, main_comp: Optional[dict]) -> dict:
    """问 LLM 要属性修改清单；行多时拆两组并发问，结果合并后返回。

    返回形状与单次调用一致（{"changes": [...], "notes": [...]}），故调用方不必知道
    这里拆没拆——校验（_validate_attr_changes）拿到的仍是完整清单，四道闸照旧
    在全量 attrs 上跑一遍。

    【一组失败就整体失败】不做「用成功的那组凑合」的兜底：属性填一半比不填更糟，
    缺的那些行会静默留空一路带到发布。asyncio.gather 默认就是这个语义（任一异常
    立即上抛），上层 service 会把该阶段标失败、可续跑。
    """
    import json as _json

    from app.publish.llm import ask_json

    def _prompt(part: list) -> str:
        return _ATTR_PROMPT.format(
            title=info.get("title"),
            src_attrs=_json.dumps(info.get("attributes", {}), ensure_ascii=False),
            main_composition=_json.dumps(main_comp or {}, ensure_ascii=False),
            image_understanding=_json.dumps(
                info.get("imageUnderstanding", {}), ensure_ascii=False),
            rows=_json.dumps(part, ensure_ascii=False),
        )

    groups = _split_attr_rows(rows)
    if len(groups) == 1:
        logger.info(f"属性现状 {len(rows)} 行已备齐，调用 LLM 审核（推理模型可能要几分钟）…")
        return await ask_json(_prompt(rows), what="属性审核", stage="attrs")

    sizes = "+".join(str(len(g)) for g in groups)
    logger.info(f"属性现状 {len(rows)} 行已备齐，拆 {len(groups)} 组（{sizes}）并发审核…")
    parts = await asyncio.gather(*(
        ask_json(_prompt(g), what=f"属性审核（第{i}组 {len(g)} 行）", stage="attrs")
        for i, g in enumerate(groups, 1)))

    changes: list = []
    notes: list = []
    for p in parts:
        changes.extend(p.get("changes") or [])
        notes.extend(p.get("notes") or [])
    return {"changes": changes, "notes": notes}


async def _apply_attr_changes(session: BrowserSession, changes: list, row_map: dict,
                              info: dict, cat_path, use_cache: bool = True,
                              site: str = "") -> tuple:
    """逐项把校验通过的修改写进表单，返回 (applied, cacheRefreshed, compFailed)。

    从 check_attrs 里摘出来【只为了能跑第二轮】：改「里料纹理」这类字段会联动新增
    必填行（里衬成分、里料克重），那些行在第一轮 dump_attrs 时还不存在，因此第一轮
    的清单里必然没有它们——摘成函数后，补填轮可以原样复用这里的重试与成分保护，
    不必另写一套写入逻辑（另写一套就会与这里的行为漂移）。
    """
    src_attrs = info.get("attributes") or {}
    applied: list = []
    cache_refreshed: list = []
    comp_failed: list = []
    for c in changes:
        # kind 必须透传：数值行（里料克重等）走 input 直填，下拉流程对它无效。
        # 优先用校验层标好的 kind，兜底查表单行的 kind——漏传会让数值行静默走
        # 下拉分支、必然失败，正是本次修复要消除的断点。
        kind = c.get("kind") or (row_map.get(c["label"], {}).get("kind") or "select")
        r = await set_attr(session, c["label"], c["value"],
                           c.get("num"), c.get("row") or 1, kind=kind)
        rec = {"label": c["label"], "value": c["value"],
               "num": c.get("num"), "row": c.get("row"),
               "result": r.get("status"),
               "readback": _readback_current(r)}
        # 【触发闸是 optionsFrom == "cache"，不是 use_cache】现场刚读来的选项立刻点不中，
        # 重读大概率还是同一份、救不回来；缓存来的可能隔了好几天，重读才有意义。
        # 这样非缓存路径保持零改动。
        if (r.get("status") == "error"
                and row_map.get(c["label"], {}).get("optionsFrom") == "cache"):
            if c.get("num") is not None:
                # 成分组不做单行重试（理由见 _refresh_row_and_retry 的 docstring）
                comp_failed.append(c["label"])
            else:
                fix = await _refresh_row_and_retry(
                    session, c, row_map[c["label"]], cat_path,
                    title=info.get("title") or "",
                    src_value=str(src_attrs.get(c["label"], "")),
                    use_cache=use_cache, site=site)
                rec["refresh"] = fix
                cache_refreshed.append(c["label"])
                if fix.get("status") == "ok":
                    rec["result"] = "ok"
                    rec["value"] = fix.get("value")
                    rec["readback"] = fix.get("readback")
        applied.append(rec)
        # 项间留 1s：动态增删行重渲染期间立刻动下一项容易点击落空
        await asyncio.sleep(1.0)
    return applied, cache_refreshed, comp_failed


# 联动补填的最大轮数：一轮 = 重扫新增必填行 → 读选项 → 问 LLM → 写入。
#
# 【为什么必须循环而不是跑一轮】里料纹理只要不选「无内衬/无里料」，就会联动出必须
# 继续选的行，而【补填自己写的值同样会再联动】：给「里衬成分」选上纤维后平台会带出
# 该成分的百分比/克重行，那些行在上一轮重扫时还不在 DOM 里。原实现只跑一轮，第二层
# 行一律落进 unfilledRequired 交人工——与「发布流程全自动优先」的取向相反。
#
# 上限 3 而不是无界：每轮固定要花「1.5s 等渲染 + 读选项 + 一次 LLM + 逐项写入」，
# 无界会让本阶段耗时失控；实测联动最深就是「里料纹理 → 里衬成分 → 其百分比」这两层，
# 留一轮余量。到顶仍有空行就交末尾复扫报人工，那是设计内的出口而非失败。
_LINKAGE_MAX_ROUNDS = 3


async def _scan_linkage_rows(session: BrowserSession, seen: set) -> list:
    """重扫表单，挑出 seen 之外的必填空行（即刚联动出来的那些）。

    【差集要对累积的 seen 取而不是只对上一轮】第二层联动行是第 N 轮写入才冒出来的，
    只跟上一轮比会把早先见过的行反复挑出来重填（写失败的行更是每轮都中），既浪费
    LLM 调用又可能把已填对的值改掉。
    """
    # 联动行在写入后才渲染出来，且重渲染要时间，故等一下再读
    await asyncio.sleep(1.5)
    rows = await session.eval_json(_JS_LIST_ATTR_ROWS)
    return [a for a in rows.get("attrs", [])
            if a["label"] not in seen and a["label"] != "产品属性"
            and a.get("required") and a.get("visible") is not False
            and str(a.get("current") or "").startswith("(")]


async def _read_linkage_options(session: BrowserSession, new_rows: list, cat_path,
                                use_cache: bool, site: str) -> None:
    """给联动行就地补上 options（原地改 new_rows），规矩与主轮 dump_attrs 一致。

    缓存优先、未命中现场读并回灌；数值行不读选项——它行内的 select 是只读单位，
    点开读到的是单位清单（见 dump_attrs 里的 number-row 分支）。
    """
    cached = (cache.load_attr_options(cat_path[-1], cat_path, site)
              if use_cache and cat_path else {})
    for a in new_rows:
        a["options"] = []
        if a.get("kind") == "number":
            a["optionsEmptyReason"] = "number-row"
            continue
        hit = cached.get(a["label"])
        if hit:
            a["options"] = hit
            a["optionsFrom"] = "cache"
            continue
        opts, meta = await _read_active_options(session, a["label"], with_meta=True)
        logger.info(f"读选项（联动行）：{a['label']} -> {len(opts)} 个"
                    + ("" if meta["complete"] else "（未滚到底，不进缓存）"))
        if opts:
            a["options"] = opts
            a["optionsFrom"] = "live"
            a["optionsComplete"] = meta["complete"]
        else:
            a["optionsEmptyReason"] = "open-failed"
        await asyncio.sleep(0.3)
    if use_cache and cat_path:
        cache.save_attr_options(cat_path[-1], cat_path, new_rows, site)


async def _fill_linkage_round(session: BrowserSession, new_rows: list, info: dict,
                              main_comp: Optional[dict], cat_path,
                              use_cache: bool, site: str) -> tuple:
    """补填一轮：读选项 → 问 LLM → 校验 → 写入，返回 (applied, compFailed)。

    与主轮共用 _validate_attr_changes 与 _apply_attr_changes：闸门（非必填留空、
    options 校验、数值量级、成分合计 100）一条都不能少，另写一套必然与主轮漂移。
    成分行同理走 row/num 那套，故这里不做「每行只问一次」的简化。

    【best-effort】读不到行、LLM 不给值、写入失败一律只记录不抛，交末尾复扫报人工。
    """
    await _read_linkage_options(session, new_rows, cat_path, use_cache, site)
    # 问 LLM：喂法与主轮完全一致（同一套提示词、同样带 kind/numHint），只是行少。
    # 【提示词不能换】规则 5（成分合计 100）、规则 8（数值行只给纯数字）对里衬成分和
    # 里料克重恰好都适用，换一套简版提示词等于把这两条闸的前提抽掉。
    ask_rows = [{"label": a["label"], "current": a.get("current"),
                 "required": bool(a.get("required")),
                 "kind": a.get("kind") or "select",
                 "numHint": a.get("numHint"),
                 "numValues": a.get("numValues"), "options": a.get("options", [])}
                for a in new_rows]
    try:
        decision = await _ask_attr_review(ask_rows, info, main_comp)
    except Exception as e:
        logger.warning(f"联动行补填问 LLM 失败（忽略，交末尾复扫报人工）：{e}")
        return [], []
    valid, rejected = _validate_attr_changes(
        decision.get("changes", []), new_rows, main_comp)
    if rejected:
        logger.warning(f"联动行补填驳回 {len(rejected)} 项："
                       + "、".join(f"{r.get('label')}({r.get('rejectReason')})"
                                   for r in rejected))
    if not valid:
        return [], []
    applied, _refreshed, comp_failed = await _apply_attr_changes(
        session, valid, {a["label"]: a for a in new_rows}, info, cat_path,
        use_cache=use_cache, site=site)
    ok_n = sum(1 for a in applied if a.get("result") == "ok")
    logger.info(f"本轮补填完成：{ok_n}/{len(applied)} 项写入成功")
    return applied, comp_failed


async def _fill_linkage_rows(session: BrowserSession, pre_labels: set, info: dict,
                             main_comp: Optional[dict], cat_path,
                             use_cache: bool = True, site: str = "") -> dict:
    """补填「联动新增的必填行」，循环追到不再冒新行为止。

    为什么必须单独一轮而不能并进主轮：这些行是【改了别的行才出现的】——阶段④开头
    dump_attrs 扫表单时它们根本不在 DOM 里，LLM 看不到、也就不会给值。原实现扫出来
    只记进 linkageNewRequired 就返回了，没有任何调用方消费，于是这两行一直空着，
    保存卡在「请输入产品属性」（2026-08-25 用户截图）。

    【为什么是多轮】里料纹理只要不选「无内衬/无里料」就必然联动，而补填写进去的值
    会再带出下一层行（里衬成分选定后出现其百分比）。单轮只能填第一层，第二层静默留空
    到保存才炸。故循环重扫，直到收敛或到 _LINKAGE_MAX_ROUNDS 上限。

    两个终止条件都必要：
      - 扫不到新行 → 真收敛，正常出口；
      - 扫到新行但本轮一项都没写成功 → 停。再转一轮扫到的还是同一批（label 已进
        seen，其实连扫都扫不到了），继续只是白烧 LLM 调用。
    """
    seen = set(pre_labels)
    all_applied: list = []
    all_new: list = []
    all_comp_failed: list = []
    for rnd in range(1, _LINKAGE_MAX_ROUNDS + 1):
        new_rows = await _scan_linkage_rows(session, seen)
        if not new_rows:
            if rnd > 1:
                logger.info(f"联动补填第 {rnd} 轮无新增行，已收敛")
            break
        labels = [a["label"] for a in new_rows]
        seen.update(labels)
        all_new.extend(labels)
        logger.info(f"联动新增必填行 {len(labels)} 条（第 {rnd}/{_LINKAGE_MAX_ROUNDS} 轮），"
                    f"开始补填：{'、'.join(labels)}")
        applied, comp_failed = await _fill_linkage_round(
            session, new_rows, info, main_comp, cat_path, use_cache, site)
        all_applied.extend(applied)
        all_comp_failed.extend(comp_failed)
        if not any(a.get("result") == "ok" for a in applied):
            logger.warning(f"第 {rnd} 轮补填无一项成功，停止追加轮次（交末尾复扫报人工）")
            break
    else:
        # 跑满轮次而不是靠「扫不到新行」退出：最后一轮仍在冒新行，说明联动比实测更深，
        # 是否还有残留由 check_attrs 末尾的必填复扫认定。这里只留痕。
        logger.warning(f"联动补填已跑满 {_LINKAGE_MAX_ROUNDS} 轮仍在冒新行，"
                       "是否留空交末尾复扫认定")
    return {"applied": all_applied, "newRequired": all_new,
            "compFailed": all_comp_failed}


async def check_attrs(session: BrowserSession, info_path: str,
                      apply: bool = False, required_only: bool = True,
                      cat_path=None, use_cache: bool = True,
                      site: str = "") -> dict:
    """阶段④：LLM 比对源商品信息与表单属性，产出修改清单（apply=True 时执行）。

    不导航——须紧跟 auto_cat 在同一页执行（类目决定属性行）。
    apply=False 是 dry-run：只出清单不动表单，供人工过目。这是本阶段的推荐用法，
    因为属性填错会一路带到发布，而 dry-run 几乎零成本。

    cat_path + use_cache 透传给 dump_attrs 用作 options 缓存的键（见那边的说明）。
    命中缓存的行若写入失败，走 _refresh_row_and_retry 只重读该行——缓存过期的表现
    就是「点不中」，而 set_attr 本来就会回读校验发现它。

    成分比例防漂移（原脚本踩的坑）：LLM 两次调用结果会漂移（55/45 变成 90/10）。
    2026-08-21 起改成【源值确定性覆盖】：主面料成分字段的纤维与百分比按
    product-info.json 的 mainComposition（源页面「主面料成分含量」解析结果）重建，
    模型给的数值一律丢弃，故不再需要调用方事后回读修正。源没写含量时（mainComposition
    为 {}）仍退回模型给数 + 合计 100% 校验的老路径。
    """
    import json as _json

    from app.publish.llm import ask_json

    with open(info_path, encoding="utf-8") as f:
        info = _json.load(f)
    dump = await dump_attrs(session, required_only=required_only,
                            cat_path=cat_path, use_cache=use_cache, site=site)
    attrs = dump["attrs"]
    if not attrs:
        # 属性行空着喂给 LLM 只会逼它瞎编（2026-08-21 实测 grok 收到空表单后
        # 开始「去工作区找字段」并吐出 shell 调用残骸），不如直接失败可续跑
        return {"status": "fail",
                "reason": "编辑页属性行读出来是空的（页面未渲染完或类目丢失？），可续跑"}
    # kind/numHint 必须一起喂：数值输入行（里料克重 g/m²）没有 options，模型看不到
    # kind 就会给个选项文本或带单位的字符串，两者都会被数值校验拒掉、该行填不上。
    rows = [{"label": a["label"], "current": a.get("current"),
             "required": bool(a.get("required")),
             "kind": a.get("kind") or "select",
             "numHint": a.get("numHint"),
             "numValues": a.get("numValues"), "options": a.get("options", [])}
            for a in attrs]

    # 旧的 product-info.json 没有 mainComposition 字段，这里就地补算一次：
    # 阶段①与阶段④可能隔着几天跑，不该因为文件是旧版就丢掉源含量这个确定事实。
    # 【必须用 falsy 判断而不是 is None】2026-08-25 前解析不出含量时落盘的是 {}，
    # 只判 None 会让这批旧文件带着空字典走下去，新的「没写就按纤维 100%」默认规则
    # 对它们完全不生效——又是一次「接线没接上」的静默失效。
    main_comp = info.get("mainComposition")
    if not main_comp:
        from app.publish.extract import parse_main_composition
        main_comp = parse_main_composition(info.get("attributes") or {})
    # 把源属性挂进去供 _infer_filler 推断配料纤维：它读 main_comp["srcAttrs"]，
    # 而 parse_main_composition 只产出 fiber/percent/raw。不挂上去推断恒拿到 {}、
    # 静默退回写死候选表（等于没接），与 dimsCm 那个死键是同一类失效。
    main_comp = {**main_comp, "srcAttrs": info.get("attributes") or {}}

    decision = await _ask_attr_review(rows, info, main_comp)
    valid, rejected = _validate_attr_changes(
        decision.get("changes", []), attrs, main_comp)
    logger.info(
        f"LLM 审核完成：建议改 {len(valid)} 项"
        + (f"，驳回 {len(rejected)} 项" if rejected else "")
        + ("，开始逐项写入…" if apply and valid else ""))
    result = {"status": "ok", "attrCount": len(rows), "proposed": valid,
              "rejected": rejected, "notes": decision.get("notes", []),
              "cacheRead": dump.get("cacheRead") or 0,
              "activeRead": dump.get("activeRead") or 0,
              "mainComposition": main_comp or None}
    if not apply:
        result["applied"] = None
        return result

    row_map = {a["label"]: a for a in attrs}   # 查 optionsFrom / current / required
    applied, cache_refreshed, comp_failed = await _apply_attr_changes(
        session, valid, row_map, info, cat_path, use_cache=use_cache, site=site)
    result["applied"] = applied
    result["cacheRefreshed"] = cache_refreshed
    result["compFailed"] = comp_failed

    # ---- 联动补填轮 --------------------------------------------------------
    # 改「里料纹理」为「光面」会联动新增必填行（里衬成分、里料克重 g/m²）。这些行在
    # 第一轮 dump_attrs 时【还不存在】，所以第一轮的清单里必然没有它们，留空就卡保存。
    # 原实现只把它们记进 linkageNewRequired 交出去，而 service 层并不消费这个字段
    # （2026-08-25 用户截图：里料克重、里衬成分两行空着）——检测到了却没人填。
    # 故这里补一轮「重扫 → 读选项 → 问 LLM → 写入」，闸门与主轮完全共用。
    #
    # 【补填是多轮的】里料纹理只要不选「无内衬/无里料」就必然联动，而补填写进去的值
    # 会再带出下一层行（里衬成分选定后出现其百分比）。故 _fill_linkage_rows 内部循环
    # 重扫直到不再冒新行，上限 _LINKAGE_MAX_ROUNDS 轮防耗时失控；仍有残留的由下面
    # 的必填复扫报人工。别改回「只跑一轮」——那会让第二层行静默留空到保存才炸。
    fill = await _fill_linkage_rows(
        session, {a["label"] for a in attrs}, info, main_comp,
        cat_path, use_cache=use_cache, site=site)
    result["linkageFilled"] = fill.get("applied") or []
    result["linkageNewRequired"] = fill.get("newRequired") or []
    if fill.get("compFailed"):
        result["compFailed"] = list(comp_failed) + list(fill["compFailed"])

    # 末尾复扫：补填轮之后仍空着的必填行，一律报出来（含补填没救回来的）。
    # 这是本阶段交给调用方的唯一「还差什么」清单，service 层据此提示人工。
    rows3 = await session.eval_json(_JS_LIST_ATTR_ROWS)
    result["unfilledRequired"] = [
        a["label"] for a in rows3.get("attrs", [])
        if a.get("required") and a.get("visible") is not False
        and str(a.get("current") or "").startswith("(")
    ]
    parked = await _park_ghost_dropdowns(session)
    result["parkedGhosts"] = parked.get("parked", 0)
    return result


# ---- 阶段⑩ 运输信息（set_shipping）------------------------------------------
# 承诺发货时效（radio 组）+ 运费模板（ant-select），都在 #shipmentInfo 区块内。
#
# 【搬的是不导航的那个版本】原脚本有两个入口：cmd_set_shipping 会先 open_edit 刷新页面，
# cmd_set_shipping_page 不导航。刷新会把前面 9 个阶段填的、尚未保存的内容全丢掉，
# 故这里只搬不导航版——与本模块其它写入阶段一致，由调用方（CLI / service）负责先
# open_edit 一次，之后各阶段共用同一页面。

_JS_SHIPPING_RADIOS = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const opts = Array.from(sec.querySelectorAll('.ant-radio-button-wrapper, .ant-radio-wrapper'))
    .map(o => (o.textContent || '').trim()).filter(Boolean);
  const c = sec.querySelector('.ant-radio-button-wrapper-checked, .ant-radio-wrapper-checked');
  return JSON.stringify({options: opts, checked: c ? (c.textContent || '').trim() : null});
})()"""

# 【必须点选项内部的 input】点外层 .ant-radio-wrapper 不触发 Vue 的 v-model
# （2026-08-14 实测）：wrapper 上的 click 被 antd 自己的事件代理吃掉，只有内层
# 原生 input 的 click 会走到 change 回调。
_JS_CLICK_SHIPPING_RADIO = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({clicked: false, reason: 'section-not-found'});
  const opt = Array.from(sec.querySelectorAll('.ant-radio-button-wrapper, .ant-radio-wrapper'))
    .find(o => (o.textContent || '').trim() === __T__);
  if (!opt) return JSON.stringify({clicked: false, reason: 'option-not-found'});
  opt.scrollIntoView({block: 'center', behavior: 'instant'});
  const inp = opt.querySelector('input');
  if (!inp) return JSON.stringify({clicked: false, reason: 'no-inner-input'});
  inp.click();
  return JSON.stringify({clicked: true});
})()"""

# 运费模板没有 ant-form-item 包裹、也没有稳定的 name/id，只能从「运费模板」这个纯文本
# 叶子节点出发往上找最近的 .ant-select（最多 6 层）。同时回读坐标，供合成点击展开下拉。
_JS_FREIGHT_TPL_STATE = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const lab = Array.from(sec.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent || '').trim() === '运费模板')[0];
  if (!lab) return JSON.stringify({err: 'no-运费模板-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 6 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  const selected = Array.from(sel.querySelectorAll('.ant-select-selection-item'))
    .map(x => (x.title || x.textContent || '').trim()).filter(Boolean);
  const r = sel.getBoundingClientRect();
  return JSON.stringify({selected, open: sel.classList.contains('ant-select-open'),
    x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
})()"""

_JS_SCROLL_FREIGHT_TPL = r"""(() => {
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'section-not-found'});
  const lab = Array.from(sec.querySelectorAll('*'))
    .filter(el => el.childElementCount === 0 && (el.textContent || '').trim() === '运费模板')[0];
  if (!lab) return JSON.stringify({err: 'no-运费模板-label'});
  let box = lab.parentElement, sel = null;
  for (let i = 0; i < 6 && box; i++) { sel = box.querySelector('.ant-select'); if (sel) break; box = box.parentElement; }
  if (!sel) return JSON.stringify({err: 'no-select'});
  sel.scrollIntoView({block: 'center', behavior: 'instant'});
  return JSON.stringify({ok: true});
})()"""

# 展开运费模板下拉。点内部 .ant-select-selector 而不是外层 .ant-select（实测后者无效）。
_JS_OPEN_FREIGHT_TPL = r"""(() => {
  // 【定位走 #shipmentInfo 区块内的 .ant-select，不按「运费模板」文本反查】
  // 2026-08-20 实测：按叶子元素文本恰等于「运费模板」去找会 no-select——该文案在
  // DOM 里不是独立叶子节点。而运输信息区块里 .ant-select 只有这一个（12 个发货时效
  // 是 radio，不是 select），故按区块取更可靠。
  const sec = document.getElementById('shipmentInfo');
  if (!sec) return JSON.stringify({err: 'no-shipmentInfo'});
  const sel = sec.querySelector('.ant-select');
  if (!sel) return JSON.stringify({err: 'no-select'});
  const inner = sel.querySelector('.ant-select-selector') || sel;
  ['mousedown', 'mouseup', 'click'].forEach(t =>
    inner.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  return JSON.stringify({clicked: true, open: sel.classList.contains('ant-select-open')});
})()"""


# 运费模板一般只有一个（唯一模板，2026-08-18 用户确认），直接取下拉第一项。
_JS_PICK_FIRST_TPL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 【判可见必须看 inline display，不能用 getBoundingClientRect().height】
  // 2026-08-20 实测：页面上常驻 2 个 .ant-select-dropdown（仓库那个 + 运费模板那个），
  // 隐藏时高度恒为 0 但 style 里留着上次的 left/top，且 ant-select-dropdown-hidden
  // 这个类也不一定加上。按 height>0 找会一个都找不到（no-dropdown）。
  const dd = Array.from(document.querySelectorAll('.ant-select-dropdown'))
    .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
    .pop();
  if (!dd) return JSON.stringify({err: 'no-dropdown'});
  const o = dd.querySelector('.ant-select-item-option');
  if (!o) return JSON.stringify({err: 'no-option'});
  const text = (o.textContent || '').trim();
  o.click();
  await sleep(800);
  document.body.click();
  return JSON.stringify({picked: text});
})()"""


def _longest_deadline(options: list) -> Optional[str]:
    """从时效选项里挑「最长」的那个（SKILL.md 阶段⑩规则）。

    选项文本形如「1个工作日内发货」「15个工作日内发货」，按前导整数比大小——不能按
    字符串排序（"9" > "15"），也不能按选项在 DOM 里的顺序取最后一个（页面上是
    1/2/7/8/…/16 递增，但顺序是平台给的、不保证）。取不出数字的选项直接排除。
    """
    scored = []
    for text in options:
        m = re.match(r"\s*(\d+)", text)
        if m:
            scored.append((int(m.group(1)), text))
    return max(scored)[1] if scored else None


async def set_shipping(session: BrowserSession, deadline: str = "") -> dict:
    """阶段⑩：承诺发货时效（radio）+ 运费模板（ant-select）。不导航。

    deadline 留空时按 SKILL.md 规则选【最长】时效——时效越长越不容易因超时被平台罚，
    这是业务侧定的默认。给了具体值就按文本精确匹配（匹配不上会把可选项列出来）。

    两处踩过的坑，对应下面两段处理：
    1. radio 必须点选项内部的 input（见 _JS_CLICK_SHIPPING_RADIO 注释）
    2. 点完 Vue 会重渲染整个区块，回读必须【重新查询】元素。故这里回读是另发一次
       eval_json（_JS_SHIPPING_RADIOS）从 document 重新找 checked 的那一项，
       不复用点击时拿到的任何引用。
    """
    state = await session.eval_json(_JS_SHIPPING_RADIOS)
    if state.get("err"):
        return {"status": "error", "stage": "deadline-locate", **state}
    options = state.get("options") or []
    if not options:
        return {"status": "error", "stage": "deadline-locate",
                "reason": "运输信息区未找到发货时效选项"}

    target = deadline or _longest_deadline(options)
    if not target:
        return {"status": "error", "stage": "deadline-pick",
                "reason": "选项里解析不出工作日天数", "options": options}
    if target not in options:
        return {"status": "error", "stage": "deadline-pick",
                "reason": f"指定的时效不在选项里: {target}", "options": options}

    if state.get("checked") != target:
        clicked = await session.eval_json(
            _JS_CLICK_SHIPPING_RADIO.replace("__T__", J(target)))
        if not clicked.get("clicked"):
            return {"status": "error", "stage": "deadline-click",
                    "options": options, **clicked}
        await asyncio.sleep(1.5)  # 等重渲染，太早回读会读到旧的 checked
        state = await session.eval_json(_JS_SHIPPING_RADIOS)
        if state.get("checked") != target:
            return {"status": "error", "stage": "deadline-verify",
                    "reason": "点选后回读不符", "want": target,
                    "checked": state.get("checked")}

    # 运费模板：已选中就不动（幂等），未选中才展开下拉取第一项
    tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
    if tpl.get("err"):
        return {"status": "error", "stage": "tpl-locate",
                "checked": state.get("checked"), **tpl}
    picked = None
    if not tpl.get("selected"):
        # 先滚到视野中央再另起一次 eval 读坐标：滚动未停就读到的 rect 会让点击落空
        await session.eval_json(_JS_SCROLL_FREIGHT_TPL)
        await asyncio.sleep(0.8)
        tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
        if not tpl.get("open"):
            # 【必须点内部的 .ant-select-selector，点外层 .ant-select 容器无效】
            # 2026-08-20 实测：sel.click() 与 elementFromPoint 取到的外层容器都打不开
            # 下拉（open 仍为 false），antd 把点击处理绑在 selector 那一层。
            await session.eval_json(_JS_OPEN_FREIGHT_TPL)
            await asyncio.sleep(1.5)
        opt = await session.eval_json(_JS_PICK_FIRST_TPL)
        if opt.get("err"):
            return {"status": "error", "stage": "tpl-option",
                    "checked": state.get("checked"), **opt}
        picked = opt.get("picked")
        await asyncio.sleep(1.0)
        tpl = await session.eval_json(_JS_FREIGHT_TPL_STATE)
        if not tpl.get("selected"):
            return {"status": "error", "stage": "tpl-verify",
                    "reason": "选中后回读为空", "checked": state.get("checked"),
                    "picked": picked, **tpl}

    return {"status": "ok", "deadline": state.get("checked"),
            "autoPicked": not deadline, "options": options,
            "freightTemplate": (tpl.get("selected") or [None])[0],
            "templatePicked": picked}


# ---- 阶段⑫ 保存（save）------------------------------------------------------
# 【本阶段是全管线的落库点】前面 9 个阶段改的都只是页面上的 Vue 状态，不点保存一律不入库。
#
# 【本函数只点「保存」，绝不碰「发布」】发布是独立的阶段⑮ publish_now，必须显式
# confirm=True 才执行（见该节注释）。save 与它严格分开：落库是可重复的，上架不可逆。
# 顶部操作栏上「保存」（btn-orange）与「发布」（btn-green）相邻，故按钮定位一律用
# 文本【精确等于】'保存' —— 用 includes 会同时命中「保存并移入待发布」（那个会把草稿
# 移出采集箱，行为不同且不可逆）。
#
# 编辑页按钮上 JS el.click() 有效（2026-08-14 实测，触发 POST /api/popTemuProduct/*.json），
# 而 mouse_click / CDP dispatchMouseEvent 在本页被吞（坐标命中、零网络请求），所以这里
# 刻意不用 session.mouse_click。

_JS_CLICK_SAVE = r"""(() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => (b.textContent || '').trim() === '保存');
  if (!btns.length) return JSON.stringify({clicked: false, reason: 'save-button-not-found'});
  const btn = btns[0];
  btn.click();
  return JSON.stringify({clicked: true, orange: btn.className.includes('btn-orange'),
    candidates: btns.length});
})()"""

# 成功判据里「校验错误」这一半：失败时页面【没有 toast】，只有各区块里的
# .ant-form-item-explain-error 会出现文案。.ant-message 一并读是因为个别提示走的是
# 通用组件，但保存成功的提示用 .ant-message 捕获不到（店小秘自定义实现），
# 所以「读到 messages」不能当成功判据，只作诊断信息。
_JS_SAVE_FEEDBACK = r"""(() => {
  const msgs = Array.from(document.querySelectorAll('.ant-message span, .ant-notification div'))
    .map(e => (e.textContent || '').trim()).filter(t => t && t.length < 200);
  const errs = Array.from(document.querySelectorAll('.ant-form-item-explain-error, [class*="explain-error"]'))
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  return JSON.stringify({messages: msgs.slice(0, 10), errors: Array.from(new Set(errs)).slice(0, 10)});
})()"""

# 校验失败时右侧锚点导航里对应区块的链接变红（class 含 f-red），页面静默滚到该区块、
# 不弹任何提示。逐节读锚点是唯一能说清「哪块红了」的办法——只报 explain-error 文案
# 常常是「请选择」这种无区块归属的字样，人看不出该去哪一节改。
_JS_RED_ANCHORS = r"""(() => {
  const ids = __IDS__;
  const out = [];
  for (const a of Array.from(document.querySelectorAll('a, li, span, div'))) {
    const cls = a.className;
    if (typeof cls !== 'string' || !cls.includes('f-red')) continue;
    const txt = (a.textContent || '').trim();
    if (!txt || txt.length > 20) continue;
    const href = a.getAttribute('href') || '';
    const hit = ids.find(x => href.includes(x.id) || txt === x.name);
    if (hit && !out.some(o => o.id === hit.id)) out.push({id: hit.id, name: hit.name});
  }
  return JSON.stringify({redSections: out});
})()"""

# 保存成功后弹「继续编辑 / 返回列表」确认框。【必须关掉】：它是 .ant-modal 级弹窗，
# 遮罩盖住整个页面，不关会挡住顶部操作栏（包括发布按钮），后续任何点击都落在遮罩上。
# 点「继续编辑」而不是「返回列表」——留在编辑页，后续阶段/人工复核还要用这个页面。
_JS_CLOSE_SAVE_CONFIRM = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let closed = null, seen = [];
  for (let k = 0; k < 3; k++) {
    const modal = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
      .find(m => m.offsetHeight > 0 && (m.textContent || '').includes('继续编辑'));
    if (!modal) break;
    const btns = Array.from(modal.querySelectorAll('button'));
    seen = btns.map(b => (b.textContent || '').trim());
    const go = btns.find(b => (b.textContent || '').trim() === '继续编辑')
      || btns.find(b => (b.textContent || '').trim() === '确定');
    if (!go) break;
    go.click();
    closed = (go.textContent || '').trim();
    await sleep(1200);
  }
  const still = !!Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .find(m => m.offsetHeight > 0 && (m.textContent || '').includes('继续编辑'));
  // 「继续编辑」这个文案取自 SKILL.md、原脚本没处理过这个确认框，故文案未经实测。
  // 一旦不符，上面的检测会整体落空（closed 与 stillOpen 双 false，静默漏报），
  // 所以把当前【所有可见弹窗】的标题和按钮一并报出来，供真站验证时对照改文案。
  const visible = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .filter(m => m.offsetHeight > 0)
    .map(m => ({
      title: ((m.querySelector('.ant-modal-title, .ant-modal-confirm-title') || {})
        .textContent || '').trim().slice(0, 40),
      buttons: Array.from(m.querySelectorAll('button'))
        .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 6)
    }));
  return JSON.stringify({closed, buttons: seen, stillOpen: still, visibleModals: visible});
})()"""


async def _draft_update_time(session: BrowserSession, rowid: str) -> Optional[str]:
    """在【新页签】读草稿列表里该 rowid 行的更新时间。

    为什么必须新页签：保存前后各读一次做对比，若在当前页签导航去列表就把编辑页丢了，
    未保存内容全没。best-effort——读不到返回 None，只是少一条成功证据，不该让保存失败。
    """
    page_backup = session._page
    cdp_backup = session._cdp
    try:
        await session.navigate(DRAFT_LIST_URL, new_tab=True)
        js = r"""(() => {
          const tr = document.querySelector('tr[rowid=' + __RID__ + ']');
          if (!tr) return JSON.stringify({found: false});
          const m = (tr.textContent || '').match(/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?/g);
          return JSON.stringify({found: true, times: m || []});
        })()""".replace("__RID__", J('"' + rowid + '"'))
        data = await session.wait_for(js, lambda d: d.get("found"), timeout=25)
        times = data.get("times") or []
        return times[-1] if times else None
    except Exception as e:
        logger.warning(f"读草稿更新时间失败（忽略）：{e}")
        return None
    finally:
        # 关掉临时页签、把会话切回编辑页
        try:
            if session._page is not page_backup:
                await session._page.close()
        except Exception as e:
            logger.warning(f"关闭临时列表页签失败（忽略）：{e}")
        session._page = page_backup
        session._cdp = cdp_backup
        # 开临时页签期间编辑页转入后台，rAF 节流那两个开关要重发一次，
        # 否则后续阶段的浮层定位又会读到 -9999（见 browser.fix_hidden_tab）
        await session.fix_hidden_tab()


async def save(session: BrowserSession, rowid: str = "") -> dict:
    """阶段⑫：点顶部「保存」把前面各阶段的修改落库。不导航（当前页直接点）。

    【只点「保存」】发布是独立的阶段⑮ publish_now，本函数绝不碰「发布」按钮
    （顶栏两个按钮相邻，故定位用文本精确等于'保存'，见本节顶部注释）。

    成功判据（原脚本实测结论，别换）：
      无 .ant-form-item-explain-error + 草稿列表更新时间变化。
      不能用 toast：保存成功的提示店小秘是自定义实现，.ant-message 捕获不到。
    校验失败时页面【完全静默】——不弹提示，只静默滚到出错区块、右侧锚点变红 f-red，
    故失败分支要把红了的区块名逐节读出来报给人（否则只有一句「请选择」没法定位）。

    rowid 给了才做更新时间对比（要新开页签读草稿列表，见 _draft_update_time）；
    不给就只靠「无校验错误 + 确认框出现」判断，够用但证据弱一档。
    """
    before = await _draft_update_time(session, rowid) if rowid else None

    clicked = await session.eval_json(_JS_CLICK_SAVE)
    if not clicked.get("clicked"):
        return {"status": "error", "stage": "click", **clicked}

    # 轮询 8s 收集反馈：保存请求往返 + 校验错误渲染都要时间，读太早两边都是空的。
    # 原脚本是 5s，这里放宽——Playwright 直连没有 WebBridge 的 HTTP 往返开销垫时间。
    feedback = {"messages": [], "errors": []}
    for _ in range(11):
        feedback = await session.eval_json(_JS_SAVE_FEEDBACK)
        if feedback.get("messages") or feedback.get("errors"):
            break
        await asyncio.sleep(0.7)

    # 无论成败都先把确认框关掉：它的遮罩会挡住后续一切点击（含发布按钮）
    confirm = await session.eval_json(_JS_CLOSE_SAVE_CONFIRM)
    if confirm.get("stillOpen"):
        # 关不掉就按卡住的 modal 处理（ant-fade-leave-active 残留的老毛病）
        killed = await session.kill_stuck_modals()
        confirm["killedStuck"] = killed.get("removed")
    elif not confirm.get("closed") and confirm.get("visibleModals"):
        # 没匹配到「继续编辑」但页面上确实有可见弹窗：多半是文案与 SKILL.md 不一致。
        # 这属于「没关掉但也没报错」的静默态，必须显式告警，否则遮罩会一路挡住后续点击。
        logger.warning(
            f"保存确认框文案可能不是「继续编辑」，未关闭；实际可见弹窗："
            f"{confirm['visibleModals']}"
        )

    errors = feedback.get("errors") or []
    if errors:
        ids = [{"id": k, "name": v} for k, v in SECTION_IDS.items()]
        red = await session.eval_json(_JS_RED_ANCHORS.replace("__IDS__", J(ids)))
        sections = red.get("redSections") or []
        logger.error(
            "保存校验失败（页面无 toast，靠红锚点定位）："
            + ("、".join(s["name"] for s in sections) or "未识别出红色区块")
        )
        return {"status": "validation-error", "errors": errors,
                "redSections": sections, "messages": feedback.get("messages"),
                "confirmDialog": confirm}

    after = await _draft_update_time(session, rowid) if rowid else None
    # 更新时间只在两次都读到、且相等时才判定「没落库」：读不到（None）属证据缺失，
    # 不能当失败——列表分页/筛选变化都可能读不到那一行。
    if before and after and before == after:
        return {"status": "validation-error",
                "reason": "无校验错误但草稿更新时间未变，保存可能未生效",
                "updateTime": {"before": before, "after": after},
                "messages": feedback.get("messages"), "confirmDialog": confirm}

    return {"status": "ok", "rowid": rowid or None,
            "updateTime": {"before": before, "after": after},
            "messages": feedback.get("messages"),
            "confirmDialog": confirm, "published": False}


# ---- 阶段⑮ 立即发布 --------------------------------------------------------
# 【这里是全管线唯一不可逆的一步】原 skill 与本模块此前刻意不实现发布入口（真实商家
# 账号、发布后要下架才能改）。2026-08-24 按用户明确要求补上：⑭ save 落库之后点顶部
# 「发布」下拉里的「立即发布」，把草稿真正推到平台。调用方必须显式传 confirm=True，
# 防止别处 import 后误触发。
#
# 顶栏三个按钮相邻：「保存」btn-orange、「保存并移入待发布」、「发布 ∨」btn-green
# （带下拉箭头）。故按钮定位一律用文本【精确等于】'发布'——用 includes 会同时命中
# 「保存并移入待发布」（那个只挪列表、不上架，行为完全不同）。
#
# 【这个下拉是 hover 出来的，且 hover 一停就收起】2026-08-24 真站取证：
# `btn.click()` 之后页面上一个含「立即发布」的节点都没有；合成
# mouseover/mouseenter/mousemove 之后才渲染出 .ant-dropdown（inline style 带 left/top）
# + ul.ant-dropdown-menu，项是「立即发布 / 定时发布」。
#
# 【必须等入场动画放完再点，offsetHeight 不能当判据】同日逐帧取证（hover 后 0 /
# 900 / 2400ms 三次采样）：
#   t+900ms   实例已建，但 inline style 是 opacity:0、transform matrix(0,0,0,0,0,0)
#             → 容器与菜单项的 getBoundingClientRect 都是 0x0，
#             而 offsetHeight 此时【已经是最终值】72 / 32
#   t+2400ms  opacity:1、transform none → 容器 rect 84x72、「立即发布」项 rect 76x32
# 所以收敛判据只能用【菜单项的 rect.height > 0】：用 offsetHeight 会在动画中途就通过，
# 拿到 0x0 的坐标（表现为 publish-now-item-zero-height，或更坏——点在页面外）。
#
# 顺带纠正一个我一度写下的错误结论：菜单【不会】因为 evaluate 结束而收起。
# 早先 dry-run 拿到「定位不到」不是菜单消失，而是第二段 JS 赶在动画中途跑。
# 展开与点击仍合并在一个 evaluate 里——少一次往返，也不必依赖「菜单会一直留着」。
#
# 顶栏与页脚各有一个同 class 的「发布」按钮（rect.top 84 / 7286），取靠上的可见实例：
# 页脚那个 hover 也能展开，但菜单渲染在页面底部，后续判定与取证都更难对齐。
#
# 【菜单容器只认 .ant-dropdown 且必须非零高度】页面上另有两个无 class、height=0、
# width=2552 的包裹层同样含「立即发布」文本，宽度与 top 能过「幽灵浮层」那套过滤，
# 从里面挑节点去点等于点在页面外、什么都不会发生。
#
# 【必须排除「定时发布」】它与「立即发布」同菜单相邻，行为完全不同（定时上架）。
# 故菜单项文本用【精确等于】，不用 includes。

# 只读探测：发布按钮在不在、hover 能否展开菜单。给 publish_now 前置校验与排查用，
# 它【不点】任何菜单项（真正的展开+点击在 _JS_CLICK_PUBLISH_NOW 里一气做完）。
_JS_OPEN_PUBLISH_DROPDOWN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => b.offsetHeight > 0 && (b.textContent || '').trim() === '发布')
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  if (!btns.length) {
    const all = Array.from(document.querySelectorAll('button'))
      .filter(b => b.offsetHeight > 0)
      .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 20);
    return JSON.stringify({opened: false, reason: 'publish-button-not-found', buttons: all});
  }
  const btn = btns[0];
  btn.scrollIntoView({block: 'center'});
  await sleep(600);
  // 【判据用项的 rect 高度，不用 offsetHeight】见本节顶部逐帧取证：动画中途
  // offsetHeight 已是最终值而 rect 仍 0x0，用它收敛会点在 0x0 坐标上。
  const liveMenu = () => Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (String(d.className).includes('ant-dropdown-hidden')) return false;
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const it = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .find(i => (i.textContent || '').trim() === '立即发布');
    if (!it) return false;
    return it.getBoundingClientRect().height > 0;   // 动画放完才算可点
  });
  const r = btn.getBoundingClientRect();
  const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  // 首轮 hover 后按 300ms 步长轮询等动画放完（实测约 1.5s 到位）；
  // 菜单一直没建则每 6 轮补发一次 hover（Vue 的监听未必挂在同一个事件上）。
  for (let k = 0; k < 20 && !liveMenu(); k++) {
    if (k % 6 === 0) {
      for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
        btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
          view: window, clientX: cx, clientY: cy}));
      }
    }
    await sleep(300);
  }
  const menu = liveMenu();
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('div, ul'))
      .filter(e => (e.textContent || '').includes('立即发布'))
      .map(e => ({cls: String(e.className).slice(0, 60), h: e.offsetHeight}))
      .slice(0, 6);
    return JSON.stringify({opened: false, reason: 'dropdown-did-not-render',
      btnRect: {top: Math.round(r.top), left: Math.round(r.left)}, seen});
  }
  return JSON.stringify({opened: true,
    green: String(btn.className).includes('btn-green'),
    candidates: btns.length,
    items: Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim())});
})()"""

# hover 展开 + 点「立即发布」，一个 evaluate 里做完（见本节顶部：菜单跨 evaluate 会收起）。
_JS_CLICK_PUBLISH_NOW = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => b.offsetHeight > 0 && (b.textContent || '').trim() === '发布')
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  if (!btns.length) return JSON.stringify({clicked: false, reason: 'publish-button-not-found'});
  const btn = btns[0];
  btn.scrollIntoView({block: 'center'});
  await sleep(600);

  // 【判据用项的 rect 高度，不用 offsetHeight】见本节顶部逐帧取证：动画中途
  // offsetHeight 已是最终值而 rect 仍 0x0，用它收敛会点在 0x0 坐标上。
  const liveMenu = () => Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (String(d.className).includes('ant-dropdown-hidden')) return false;
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const it = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .find(i => (i.textContent || '').trim() === '立即发布');
    if (!it) return false;
    return it.getBoundingClientRect().height > 0;   // 动画放完才算可点
  });

  // hover 三连（Vue 的监听未必挂在同一个事件上），最多试 3 轮
  const r = btn.getBoundingClientRect();
  const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  // 首轮 hover 后按 300ms 步长轮询等动画放完（实测约 1.5s 到位）；
  // 菜单一直没建则每 6 轮补发一次 hover（Vue 的监听未必挂在同一个事件上）。
  for (let k = 0; k < 20 && !liveMenu(); k++) {
    if (k % 6 === 0) {
      for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
        btn.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true,
          view: window, clientX: cx, clientY: cy}));
      }
    }
    await sleep(300);
  }

  const menu = liveMenu();
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => d.offsetHeight > 0)
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()).filter(Boolean).slice(0, 8))
      .filter(a => a.length);
    return JSON.stringify({clicked: false, reason: 'dropdown-did-not-render', seen});
  }

  const items = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'));
  const item = items.find(i => (i.textContent || '').trim() === '立即发布');
  if (!item) {
    return JSON.stringify({clicked: false, reason: 'publish-now-item-not-found',
      seen: [items.map(i => (i.textContent || '').trim())]});
  }
  const rect = item.getBoundingClientRect();
  if (rect.height <= 0) {
    return JSON.stringify({clicked: false, reason: 'publish-now-item-zero-height'});
  }
  // 菜单还开着的这一刻直接点，中间不回 Python
  item.click();
  await sleep(1500);
  return JSON.stringify({clicked: true,
    menuItems: items.map(i => (i.textContent || '').trim()),
    rect: {top: Math.round(rect.top), left: Math.round(rect.left),
           w: Math.round(rect.width), h: Math.round(rect.height)},
    // 菜单收起是「点中了」的旁证（ant 的菜单项点击后会关闭浮层）
    menuGone: !liveMenu()});
})()"""

# 「立即发布」后平台可能再弹一次二次确认（.ant-modal）。按钮文案未经实测，故兜
# 「确定/确认/立即发布/发布/是」几种常见文案，并把实际弹窗结构一并报出来备查。
_JS_CONFIRM_PUBLISH_MODAL = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let confirmed = null;
  const dump = () => Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
    .filter(m => m.offsetHeight > 0)
    .map(m => ({
      title: ((m.querySelector('.ant-modal-title, .ant-modal-confirm-title') || {})
        .textContent || '').trim().slice(0, 60),
      body: ((m.querySelector('.ant-modal-body') || {}).textContent || '').trim().slice(0, 150),
      buttons: Array.from(m.querySelectorAll('button'))
        .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 6)
    }));
  const before = dump();
  for (let k = 0; k < 3 && !confirmed; k++) {
    const modal = Array.from(document.querySelectorAll('.ant-modal, .ant-modal-confirm'))
      .find(m => m.offsetHeight > 0);
    if (!modal) { await sleep(800); continue; }
    const btns = Array.from(modal.querySelectorAll('button'))
      .filter(b => b.offsetHeight > 0);
    // 【不点取消类】只认肯定语义的文案
    const go = btns.find(b => ['确定', '确认', '立即发布', '发布', '是']
      .includes((b.textContent || '').trim()));
    if (!go) break;
    go.click();
    confirmed = (go.textContent || '').trim();
    await sleep(1500);
  }
  return JSON.stringify({confirmed, modalsBefore: before, modalsAfter: dump()});
})()"""

# 发布结果判据：页面 toast（店小秘自定义 d-message 与 ant-message 并存，两边都读）
# + 校验错误块。发布走与保存同一套前端校验，失败同样静默滚到红区块、不弹提示。
_JS_PUBLISH_FEEDBACK = r"""(() => {
  const msgs = Array.from(document.querySelectorAll(
    '.ant-message span, .ant-notification div, [class*="d-message"], [class*="message-content"]'))
    .map(e => (e.textContent || '').trim())
    .filter(t => t && t.length < 200);
  const errs = Array.from(document.querySelectorAll(
    '.ant-form-item-explain-error, [class*="explain-error"]'))
    .map(e => (e.textContent || '').trim()).filter(Boolean);
  return JSON.stringify({messages: Array.from(new Set(msgs)).slice(0, 10),
    errors: Array.from(new Set(errs)).slice(0, 10), url: location.href});
})()"""


# 【发布成功只能去列表取证，前端两个信号都不可用】2026-08-24 真站实测
# （rowid 173539495454560681，确认已上架：在线产品列表有它，平台 ID 2319138008）：
#   - 成功 toast 抓不到：店小秘的提示是自定义实现且转瞬即逝，轮询 12s 一条没有
#     （与 save 那边「保存成功提示 .ant-message 捕获不到」是同一条既有结论）；
#   - 页面不跳转：发布后【留在编辑页】，故「离开编辑页」这个判据恒为 False。
# 靠这两个判的话，明明成功了却报 unknown，人工还得自己去列表确认一遍。
#
# 服务端事实是清楚的：该行从草稿箱消失、出现在在线产品列表。两个列表各读一次，
# 任一确认即算发布成功（草稿箱没了但在线还没刷出来，属于平台侧的短暂延迟）。
#
# 与 _draft_update_time 同样的做法：在【新页签】里读，读完关掉、把会话切回编辑页——
# 当前页签一导航就把编辑页丢了。best-effort：读不到只是少一条证据，不抛异常。
_JS_LIST_HAS_ROW = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 等表格渲染：读太早会把「还没加载」误判成「不在这个列表里」
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('tr[rowid]')) break;
    await sleep(400);
  }
  const tr = document.querySelector('tr[rowid="' + __RID__ + '"]');
  const total = document.querySelectorAll('tr[rowid]').length;
  if (!tr) return JSON.stringify({found: false, rowsOnPage: total});
  return JSON.stringify({found: true, rowsOnPage: total,
    text: (tr.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200)});
})()"""


async def _publish_landed(session: BrowserSession, rowid: str) -> dict:
    """发布取证：草稿箱里没了 + 在线产品列表里有 → 判定已上架（见上方注释）。

    返回 {"published": bool, "draft": {...}, "online": {...}}。
    读列表全程 best-effort：任何一步失败只是少一条证据，绝不抛（对齐 _draft_update_time）。
    """
    page_backup = session._page
    cdp_backup = session._cdp
    js = _JS_LIST_HAS_ROW.replace("__RID__", rowid)
    out: dict = {}
    try:
        for name, url in (("draft", DRAFT_LIST_URL), ("online", ONLINE_LIST_URL)):
            try:
                await session.navigate(url, new_tab=(name == "draft"))
                out[name] = await session.eval_json(js)
            except Exception as e:
                logger.warning(f"读{name}列表失败（忽略）：{e}")
                out[name] = {"err": str(e)}
        # 在线有它即成功；草稿箱没它是辅证（平台可能延迟几秒才刷进在线列表）
        online_found = bool((out.get("online") or {}).get("found"))
        draft_gone = (out.get("draft") or {}).get("found") is False
        out["published"] = online_found or draft_gone
        out["reason"] = ("在线产品列表已有该行" if online_found
                         else "草稿箱已无该行" if draft_gone else "两个列表都未确认")
        return out
    finally:
        try:
            if session._page is not page_backup:
                await session._page.close()
        except Exception as e:
            logger.warning(f"关闭临时列表页签失败（忽略）：{e}")
        session._page = page_backup
        session._cdp = cdp_backup
        # 开临时页签期间编辑页转后台，rAF 节流开关要重发（同 _draft_update_time）
        await session.fix_hidden_tab()


async def publish_now(session: BrowserSession, rowid: str = "",
                      confirm: bool = False) -> dict:
    """阶段⑮：点顶部「发布」→「立即发布」，把已落库的草稿真正上架。

    【不可逆】必须显式 confirm=True 才执行，否则直接返回 refused——这个闸门是故意的，
    防止其它调用方（或续跑逻辑）在没人盯着时把真实商家草稿推上平台。

    前提：⑭ save 必须已成功。发布走与保存同一套前端校验，草稿没落库就点发布只会
    重复卡在同一批校验错误上。

    判据：
      成功 = 无 .ant-form-item-explain-error，且【列表取证】通过——草稿箱里没了
             或在线产品列表里有它（见 _publish_landed；成功 toast 抓不到、页面也
             不跳转，两个前端信号都不可用）。
      失败 = 有 explain-error → 逐节读红锚点定位区块（页面静默，不弹提示）。
    """
    if not confirm:
        return {"status": "refused", "reason": "发布不可逆，必须显式传 confirm=True"}

    # 遗留遮罩会把点击全部吞掉（保存确认框的老坑），发布前先清一次
    await session.kill_stuck_modals()

    # 展开下拉与点「立即发布」在同一段 JS 里完成（菜单跨 evaluate 就收起了，
    # 见 _JS_CLICK_PUBLISH_NOW 上方注释）。整段重试 3 轮：hover 偶发不触发。
    clicked = {}
    for attempt in (1, 2, 3):
        clicked = await session.eval_json(_JS_CLICK_PUBLISH_NOW)
        if clicked.get("clicked"):
            break
        logger.warning(f"第 {attempt} 次展开/点击「立即发布」未成功：{clicked}")
        await asyncio.sleep(1.5)
    if not clicked.get("clicked"):
        return {"status": "error", "stage": "click-publish-now", **clicked}
    logger.info(f"已点「立即发布」：{clicked}")

    # 二次确认弹窗（若有）
    modal = await session.eval_json(_JS_CONFIRM_PUBLISH_MODAL)
    if modal.get("confirmed"):
        logger.info(f"已确认发布弹窗：{modal.get('confirmed')}")

    # 轮询 12s 收反馈：发布请求往返比保存慢（服务端要过一遍平台校验）
    feedback = {"messages": [], "errors": []}
    for _ in range(16):
        feedback = await session.eval_json(_JS_PUBLISH_FEEDBACK)
        if feedback.get("messages") or feedback.get("errors"):
            break
        await asyncio.sleep(0.75)

    errors = feedback.get("errors") or []
    if errors:
        ids = [{"id": k, "name": v} for k, v in SECTION_IDS.items()]
        red = await session.eval_json(_JS_RED_ANCHORS.replace("__IDS__", J(ids)))
        sections = red.get("redSections") or []
        logger.error("发布校验失败（页面无 toast，靠红锚点定位）："
                     + ("、".join(s["name"] for s in sections) or "未识别出红色区块"))
        return {"status": "validation-error", "errors": errors,
                "redSections": sections, "messages": feedback.get("messages"),
                "publishModal": modal}

    msgs = feedback.get("messages") or []
    bad = [m for m in msgs if any(w in m for w in ("失败", "错误", "不能", "请先", "请选择"))]
    if bad:
        logger.error(f"发布被平台拒绝：{bad}")
        return {"status": "rejected", "messages": msgs, "publishModal": modal}

    # 【成功判据去列表取服务端事实，不看前端提示】理由见 _publish_landed 上方注释：
    # 成功 toast 抓不到、页面也不跳转，两个前端信号都恒为「没有」。
    landed = await _publish_landed(session, rowid) if rowid else {}
    if landed.get("published"):
        logger.info(f"发布完成（列表取证）：{landed}")
        return {"status": "ok", "rowid": rowid, "published": True,
                "messages": msgs, "evidence": landed, "publishModal": modal}

    ok_words = [m for m in msgs if any(w in m for w in ("成功", "已发布", "提交"))]
    if ok_words:
        # 列表没读到但抓到了成功提示：也算成功，但把证据缺口说清楚
        logger.info(f"发布完成（据页面提示，列表未取到证）：{ok_words}")
        return {"status": "ok", "rowid": rowid or None, "published": True,
                "messages": msgs, "evidence": landed, "publishModal": modal}

    logger.warning(f"发布后列表未取到证且无成功提示，判据不足：{landed} / {msgs}")
    return {"status": "unknown", "reason": "列表未取到证且未捕获成功提示",
            "messages": msgs, "evidence": landed,
            "publishModal": modal, "publishClick": clicked}


# ---- 编辑页实况探测（续跑判据）---------------------------------------------
# 【为什么需要它：状态文件记的是「跑过」，不是「存住了」】2026-08-23 实测
# 947662049255：状态文件里 ③~⑬ 十几个阶段全是 ok，但重开编辑页取证发现服务端
# 几乎什么都没存——标题空、变种属性区连尺码行都没有（0 行）、尺码表回到「添加尺码表」、
# 运费模板「请选」、描述图全部退回 cbu01.alicdn.com 的 1688 原始外链。
#
# 根因是页面生命周期：⑤~⑬ 全部只改【未保存的表单】，成果要靠 ⑭ save 一次性提交。
# save 没成功过，这些阶段就等于没跑——页签一关、Chrome 一退、或 open_edit 重新导航，
# 全丢。而续跑只看状态文件、把它们当 ok 跳过，直奔 save 拿一张空表单去提交，于是
# 每次都卡在同一句「产品信息、变种信息」校验未过，人工怎么点都出不来。
#
# 【③ 类目与 ④ 属性同样不保证存住，2026-08-24 实测推翻旧结论】原先这里写「服务端
# 持久化的只有 ③④，故可按状态文件跳过」——那是 2026-08-21 在一个 save 成功过的草稿上
# 取的证，看到的是已落库的值。890843533224 这单证明：save 从未成功时，③ 选定的
# 「女士运动卫裤」一样丢，页面回落到认领时带来的旧类目「其他（女装长裤）」，而该类目
# 已被平台下线，编辑页据此弹 d-message-error「该分类已在平台删除！」、分类行下方显示
# 暗红「未选择分类」。类目没定 → 变种属性区不渲染尺码行（skuRowCount=0）→ ⑧⑨⑩⑪ 全部
# 无处可填 → save 必然卡在「产品信息、变种信息」校验未过。
# 故 ③④ 也要读实况：catUnset / catDeleted 即判定类目丢了，须从 ③ 重跑（属性行由类目
# 决定，类目一换属性区整体重建，只补 ④ 没有意义，见 service._stale_form_stages）。
#
# 【判据刻意取「粗而确定」的信号】比如 ⑧ 只看变种表有没有行、⑤ 只看标题非空，
# 不去逐字比对内容是否与源一致——那需要重算一遍 LLM 产物，成本高且会引入新的
# 判错风险。重跑一个已完成的表单阶段是幂等的（各阶段本身都做了「已有值就不动」
# 或直接覆盖），代价只是时间；漏跑一个丢了的阶段才会导致整单卡死。
_JS_LIVE_STATE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // 表单懒渲染：等 SKU 属性区出现再读，否则会把「还没渲染」误判成「数据丢了」
  for (let i = 0; i < 40; i++) {
    if (document.querySelector('.skuAttrSizeChart')) break;
    await sleep(300);
  }
  const txt = el => ((el || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const host = u => { try { return new URL(u, location.href).host; } catch (e) { return ''; } };

  // ⑤ 标题：基本信息区有非空文本输入即算已填
  const basic = document.getElementById('productBasicInfo');
  const titleLens = Array.from((basic || document).querySelectorAll('input, textarea'))
    .map(i => (i.value || '').trim().length).filter(n => n > 0);

  // ⑧ 尺码勾选：变种信息表的行数（0 行 → ⑨⑩⑪ 全都无处可填，必须从 ⑧ 起重跑）
  const sku = document.getElementById('skuDataInfo');
  const skuRows = Array.from((sku || document).querySelectorAll('tbody tr'));

  // ⑩⑪ 变种/库存：行内 input 有值即算填过（申报价/重量/库存都在这张表里）
  const skuFilled = skuRows.filter(tr =>
    Array.from(tr.querySelectorAll('input')).some(i => (i.value || '').trim())).length;

  // ⑩a SKU 货号：空或含非 ASCII（中文/全角符号）都过不了平台校验，要重跑。
  // 【不能靠 skuFilled 顺带判】货号列自己就是 input，平台「一键生成」的中文值会让
  // skuFilled 非 0，看起来像填过了，实际正是要修的那个值（2026-08-23 实测）。
  const skuCodeInps = Array.from((sku || document).querySelectorAll('input[name=variationSku]'));
  const skuCodeBad = skuCodeInps.filter(i => {
    const v = (i.value || '').trim();
    return !v || Array.from(v).some(c => c.charCodeAt(0) > 127);
  }).length;

  // ⑨ 尺码表：控件文本仍是「添加尺码表」说明没加
  const scArea = document.querySelector('.skuAttrSizeChart');
  const scText = txt(scArea && scArea.querySelector('.ant-form-item-control-input'));

  // ⑥⑦ 素材图/SKC：变种属性区的图（重载后一张都没有说明全丢）
  const attrArea = document.getElementById('skuAttrsInfo');
  const attrImgs = Array.from((attrArea || document).querySelectorAll('img'))
    .filter(i => /^https?:/.test(i.src || ''));
  // 【张数够 ≠ 尺寸合规】某颜色行的图可能压根没被 ⑦ 换过（视觉分不出该色的图时整行
  // 跳过），留着的是 1688 原始小图，attrImgCount 照样非 0。2026-08-24 实测：咖啡色
  // 行 6 张全是 1000x1000 / 1200x1200，save 报「服装类图片尺寸不能小于1340px*1785px」。
  const attrImgBad = attrImgs.filter(i => {
    const w = i.naturalWidth || 0, h = i.naturalHeight || 0;
    return w && h && (w < __MINW__ || h < __MINH__);
  }).length;

  // ③ 类目：三个信号一起读，任一命中即判「类目丢了/失效」，须从 ③ 重跑。
  // catText 取分类下拉的当前值（回落到认领旧值时这里是「其他（...）」这类占位类目）；
  // catUnset 读分类行下方那个暗红提示块（.category-list，实测文案「未选择分类」）——
  //   下拉有值不代表类目有效，这个块才是页面自己的判定结论；
  // catDeleted 抓页面弹的 d-message-error「该分类已在平台删除！」——它几秒后自动消失，
  //   故实况探测顺手取一次，不然日志里查不到这条关键线索。
  const catItem = Array.from(document.querySelectorAll('.ant-form-item')).find(it => {
    const lab = it.querySelector('.ant-form-item-label');
    return lab && (lab.textContent || '').includes('产品分类');
  });
  const catText = catItem ? txt(catItem.querySelector('.ant-select-selection-item')) : '';
  const catListEl = (document.getElementById('productBasicInfo') || document)
    .querySelector('.category-list');
  const catListText = txt(catListEl);
  const catDeleted = Array.from(document.querySelectorAll('.d-message-error, .d-message'))
    .map(e => txt(e)).some(t => t.includes('分类已在平台删除'));

  // ⑫ 运输：时效 radio 与运费模板
  const ship = document.getElementById('shipmentInfo');
  const shipSel = ship && ship.querySelector('.ant-select-selection-item');
  const shipRadio = ship && ship.querySelector(
    '.ant-radio-button-wrapper-checked, .ant-radio-wrapper-checked');

  // ⑬ 描述：全是 1688 外链（cbu*.alicdn.com）说明删图/英化的成果没了
  const desc = document.getElementById('describeInfo');
  const descImgs = Array.from((desc || document).querySelectorAll('img'))
    .map(i => host(i.src)).filter(Boolean);
  const foreign = descImgs.filter(h => /alicdn\.com$/.test(h));

  return JSON.stringify({
    rendered: !!scArea,
    catText: catText,
    catListText: catListText,
    catUnset: catListText.includes('未选择分类'),
    catDeleted: catDeleted,
    titleFilled: titleLens.length > 0,
    skuRowCount: skuRows.length,
    skuFilledRows: skuFilled,
    skuCodeCount: skuCodeInps.length,
    skuCodeBad: skuCodeBad,
    sizechartAdded: !!scText && !scText.includes('添加尺码表'),
    attrImgCount: attrImgs.length,
    attrImgBad: attrImgBad,
    shippingSet: !!(shipSel && txt(shipSel)) && !!shipRadio,
    descImgCount: descImgs.length,
    descForeignCount: foreign.length,
  });
})()"""


async def live_state(session: BrowserSession) -> dict:
    """读编辑页实况，返回各表单阶段「成果是否还在页面上」。

    只读、不点任何东西。给 service 层的续跑判定用（见 _JS_LIVE_STATE 的注释）。
    """
    st = await session.eval_json(
        _JS_LIVE_STATE.replace("__MINW__", str(images.CLOTH_MIN_W))
                      .replace("__MINH__", str(images.CLOTH_MIN_H)))
    if not st.get("rendered"):
        logger.warning("编辑页 SKU 区未渲染完，实况判定按「全部需重跑」处理")
    # 类目异常单独打一行：这是整单卡死的上游根因，而页面那条 d-message 几秒就自己消失，
    # 不在这里落进日志的话，用户只看到后面 ⑧⑨⑩⑪ 一片「无处可填」，查不到源头。
    if st.get("catDeleted") or st.get("catUnset"):
        logger.warning(
            f"编辑页类目异常：当前分类「{st.get('catText') or '（空）'}」"
            f"{'，平台提示该分类已删除' if st.get('catDeleted') else ''}"
            f"{'，分类行显示「未选择分类」' if st.get('catUnset') else ''}"
            f"。类目未生效时变种属性区不渲染尺码行，须从阶段③ 重选类目。")
    return st


# ==================== 阶段⑥⑦⑪ 图片替换的公共机制 ====================
# 这三个阶段都走同一条路：图片直传图床（upload.upload_image）→ 在页面上打开
# 「空间图片」弹窗 → 选中刚传的图 → 确定。差别只在【入口】怎么打开菜单：
#   ⑥ 素材图：悬停图片展开 4 项菜单（本地图片/空间图片/网络图片/引用采集图片）
#   ⑦ SKC 行：点行内「选择图片」按钮
#   ⑪ 描述图：在描述编辑器里点图片
# 故把「弹窗内选图 + 确定」抽成 _pick_from_space 共用，入口各自实现。
#
# 【2026-08-20 真站实测修正了原 skill 的三条结论，勿照搬原脚本】
# 1. 悬停菜单用【合成 mouseenter 即可展开】，不必 CDP 真实鼠标移动、也不必「先移开
#    再移入」。原脚本走 WebBridge 时的结论在 Playwright 直连下不成立。
# 2. 弹窗标题是「从图片空间选择」，不是原脚本匹配的「图片空间」（原脚本用 includes
#    恰好命中，但按全等匹配就会失败）。
# 3. 判断菜单是否展开【不能用 offsetHeight】：菜单 off-screen 停靠，隐藏时 style 里
#    仍留着上次的 left/top，只有 display:none 是可靠信号。
#
# 页面上有 13 个 .ant-dropdown 实例并存（多组「应用到全部/同颜色/同尺码」、
# 「小秘美图/图片翻译/...」等干扰项），故一律按【菜单项集合】定位，绝不取第一个。

# 素材图悬停菜单的 4 个菜单项文本，用于在众多 dropdown 实例里认出它
MATERIAL_MENU_ITEMS = ("本地图片", "空间图片", "网络图片", "引用采集图片")

# 空间图片弹窗的标题（2026-08-20 实测原文）
SPACE_MODAL_TITLE = "从图片空间选择"


# 在空间图片弹窗里按 fileId 片段选中目标图并点确定。
# 为什么整段放在一个 evaluate 里：弹窗和菜单都会因失焦自动收起，分成多次往返时
# 中间那一步可能落在已消失的 DOM 上（原脚本记录的坑，这条在 Playwright 下仍然成立）。
# 选中判据用 fileId 的【文件名部分】而不是完整 URL：弹窗里的 src 是缩略图地址，
# 与直传返回的 URL 前缀不同，但文件名一致。
_JS_PICK_FROM_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  if (!modal) return JSON.stringify({stage: 'modal', err: '空间图片弹窗没打开'});

  // 每张图在弹窗里有缩略图和预览两个 img 元素，故用 .img-item 单元去重
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const hit = items.find(it => Array.from(it.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  if (!hit) {
    return JSON.stringify({stage: 'pick', err: '弹窗里找不到刚上传的图',
      itemCount: items.length,
      firstSrcs: items.slice(0, 3).map(it => {
        const i = it.querySelector('img'); return i ? (i.src || '').slice(-40) : null;})});
  }
  hit.click();
  await sleep(900);

  // 确认选中状态：ant 系一般加 -selected/-active 类，但店小秘这个弹窗是自绘的，
  // 故不硬依赖类名，只把类名回传供排查，真正的判据是确定后素材图 src 变化。
  const cls = String(hit.className || '');
  const ok = Array.from(modal.querySelectorAll('button'))
    .find(b => (b.textContent || '').trim() === '确定');
  if (!ok) return JSON.stringify({stage: 'confirm', err: '找不到确定按钮', itemClass: cls});
  ok.click();
  await sleep(1800);
  const stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
    .some(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  return JSON.stringify({stage: 'ok', picked: true, itemClass: cls, stillOpen});
})()"""


async def _pick_from_space(session: BrowserSession, file_id: str) -> dict:
    """在已打开的空间图片弹窗里选中指定图片并点确定。

    file_id 传直传返回的 fileId（形如 /wxalbum/2525332/2026.../abc.jpg），
    内部只取文件名部分做匹配——弹窗里是缩略图地址，前缀与直传返回的 URL 不同。
    """
    fid = file_id.rsplit("/", 1)[-1]
    js = (_JS_PICK_FROM_SPACE
          .replace("__TITLE__", J(SPACE_MODAL_TITLE))
          .replace("__FID__", J(fid)))
    return await session.eval_json(js)


# 悬停素材图展开菜单并点「空间图片」。
# 合成 mouseenter 对图片元素和它的 .single-image 容器都派发：实测容器才是绑事件的
# 那一层，但两个都发更稳（多余的那次无副作用）。
_JS_OPEN_MATERIAL_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const mod = document.querySelector('.material-img-module');
  if (!mod) return JSON.stringify({stage: 'locate', err: '找不到素材图区块'});
  const img = mod.querySelector('.single-image img') || mod.querySelector('img');
  if (!img) return JSON.stringify({stage: 'locate', err: '素材图区块里没有图片'});
  const before = img.src || '';
  img.scrollIntoView({block: 'center'});
  await sleep(900);

  const box = img.closest('.single-image') || img.parentElement;
  [img, box].forEach(el => {
    if (!el) return;
    ['mouseenter', 'mouseover', 'mousemove'].forEach(t =>
      el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
  });
  await sleep(1500);

  // 按【菜单项集合】在众多 dropdown 实例里认出素材图那个菜单，并且排除
  // display:none 的隐藏实例（offsetHeight 对 off-screen 停靠的菜单不可靠）
  const want = __ITEMS__;
  const menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return want.every(w => txt.includes(w));
  });
  if (!menu) return JSON.stringify({stage: 'menu', err: '悬停后菜单未展开'});

  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间图片');
  if (!item) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间图片」项'});
  item.click();
  await sleep(3000);
  const opened = Array.from(document.querySelectorAll('.ant-modal'))
    .some(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  return JSON.stringify({stage: 'ok', opened, srcBefore: before.slice(-50)});
})()"""


# 回读素材图 src，用于替换前后对比（成功判据）
_JS_MATERIAL_SRC = r"""(() => {
  const mod = document.querySelector('.material-img-module');
  const img = mod && (mod.querySelector('.single-image img') || mod.querySelector('img'));
  return JSON.stringify({src: img ? (img.currentSrc || img.src || '') : null,
    w: img ? img.naturalWidth : 0, h: img ? img.naturalHeight : 0});
})()"""


async def set_material(session: BrowserSession, image_path: str,
                       full_cid: Optional[str] = None) -> dict:
    """阶段⑥ 素材图替换：本地图 → 直传图床 → 悬停菜单选「空间图片」→ 弹窗选图 → 确定。

    image_path 应当是【已做过合规化】的图（images.square_image 出的 1785² 方图）：
    素材图要求 1:1 且不小于 1340×1785 的短边，原图 800×800 会被发布校验拦下。
    本函数不代做合规化——那是纯本地的确定性变换，调用方（service 层）先做好再传进来，
    免得这里既管页面交互又管图片处理、失败时分不清是哪一层的问题。

    不导航：与其它写入阶段一致，须在编辑页当前会话执行。
    """
    up = await upload_image(session, image_path, full_cid=full_cid)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "upload": up}

    before = await session.eval_json(_JS_MATERIAL_SRC)

    opened = await session.eval_json(
        _JS_OPEN_MATERIAL_SPACE
        .replace("__ITEMS__", J(list(MATERIAL_MENU_ITEMS)))
        .replace("__TITLE__", J(SPACE_MODAL_TITLE))
    )
    if opened.get("err") or not opened.get("opened"):
        return {"status": "error", "stage": "open-space", "detail": opened, "upload": up}

    picked = await _pick_from_space(session, up["fileId"])
    if picked.get("err"):
        # 弹窗可能还开着挡住后续操作，尽力关掉（best-effort，失败不影响错误返回）
        await _close_space_modal(session)
        return {"status": "error", "stage": "pick", "detail": picked, "upload": up}

    after = await session.eval_json(_JS_MATERIAL_SRC)
    fid = up["fileId"].rsplit("/", 1)[-1]
    # 成功判据是回读到的 src 含新 fileId：只看「src 变了」不够，
    # 选错图或挂到别处时 src 同样会变（原脚本的素材图误替换事故就是这么发生的）
    ok = fid in (after.get("src") or "")
    if not ok:
        logger.error(f"素材图替换后回读不含新 fileId：before={before.get('src')} "
                     f"after={after.get('src')}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "readback",
            "upload": up, "srcBefore": before.get("src"), "srcAfter": after.get("src"),
            "sizeAfter": {"w": after.get("w"), "h": after.get("h")}, "picked": picked}


async def _close_space_modal(session: BrowserSession) -> dict:
    """关掉空间图片弹窗（点「取消」而非「确定」）。

    best-effort：关不掉只记警告不抛。但必须尽力关——弹窗遮罩会挡住后续一切点击，
    留着它会让下一个阶段莫名其妙地全部失败。
    """
    try:
        r = await session.eval_json(r"""(async () => {
          const sleep = ms => new Promise(r => setTimeout(r, ms));
          const modal = Array.from(document.querySelectorAll('.ant-modal'))
            .find(m => m.offsetHeight > 0 &&
              ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
          if (!modal) return JSON.stringify({wasOpen: false});
          const btn = Array.from(modal.querySelectorAll('button'))
            .find(b => (b.textContent || '').trim() === '取消')
            || modal.querySelector('.ant-modal-close');
          if (btn) { btn.click(); await sleep(1000); }
          return JSON.stringify({wasOpen: true, clicked: !!btn,
            stillOpen: Array.from(document.querySelectorAll('.ant-modal'))
              .some(m => m.offsetHeight > 0 &&
                ((m.querySelector('.ant-modal-title')||{}).textContent||'').includes(__TITLE__))});
        })()""".replace("__TITLE__", J(SPACE_MODAL_TITLE)))
        if r.get("stillOpen"):
            killed = await session.kill_stuck_modals()
            r["killedStuck"] = killed.get("removed")
        return r
    except Exception as e:
        logger.warning(f"关空间图片弹窗失败（不影响主流程判定）：{e}")
        return {"err": str(e)}


# ==================== 阶段⑦ SKC 颜色图整行替换 ====================
# 【顺序关键，别改成「先删后挂」】2026-08-18 原脚本实测：行被删空后点「选择图片」
# 建立不了行绑定，空间弹窗会把图挂到别的行（驼色行的图挂进了卡其行）。故行内
# 【任何时刻都不能为空】：删旧图必须在挂了新图之后。
#
# 【一挂一删地交替，不是「先全挂再全删」——2026-08-24 改】原实现挂完 N 张再删 N 张，
# 峰值 = 旧图数 + 新图数，6 旧 + 6 新 = 12 直接超上限 10，于是不得不在开头「预删」
# 2 张腾位置。预删是个坏补丁：预删完若挂图失败，行内图数就【净减少】（实测咖啡色行
# 6 张预删 2 张后 open-space 失败，只剩 4 张），反复重跑会一路把行削到下限以下、
# 换出另一种保存报错。
# 改成交替后峰值只有 max(旧, 新) + 1：
#     6 旧 → 挂 new-01（7）→ 删第 1 张（6）→ 挂 new-02（7）→ 删（6）…
# 既不触顶也不破下限，预删整个不需要了。旧图恒在前列这个不变式没变（新图都挂在行末），
# 所以仍是「固定删第 1 张」，不必处理索引位移。
# 【行已满 10 张时这一轮要先删后挂】否则先挂就变 11 张、挂不进去（离线穷举
# 0..10 旧 × 1..10 新发现的反例，见 tests/test_publish_skc_order.py）。此时行内
# 从 10 掉到 9 再回到 10，【始终非空】，开头那条「不能删空」的约束仍然成立。
#
# 【行按钮必须 CDP 真实点击，JS click 无效——2026-08-20 亲自踩坑复现】
# 一开始以为 JS btn.click() 够用：菜单确实展开了。但那展开的是【素材图的菜单实例】
# （上一次 set_material 留下的残留绑定），于是选的图挂到了素材图上，把素材图替换掉了
# 且不报任何错——原脚本记录的「素材图误替换事故」就这么复现了一次。
# 用 CDP 真实点击（mouseMoved→mousePressed→mouseReleased，每步隔 120ms）后，页面才
# 新建出【SKC 专属菜单实例】，它有 5 项、带「应用到所有颜色」，与素材图的 4 项菜单
# 可以明确区分。故：
#   - 定位 SKC 菜单必须用「含『应用到所有颜色』」这条判据（原交接文档是对的）
#   - 行按钮点击必须走 CDP，且要 滚动 → 等停 → 另起 evaluate 读坐标 →
#     elementFromPoint 校验命中 再点（平滑滚动未停就读坐标会点偏）
#   - 每挂一张后【必须回读本行 src】：挂错位置时本行数量不增却不报错

# 判「图是否已入店小秘图床」的域名特征。图床域名是
# wxalbum-10001658-file.dianxiaomi.com（见 upload.WXALBUM_HOST），但这里只匹配主域：
# 未入图床的图是 1688 的 cbu01.alicdn.com 外链，两者主域就能区分，
# 匹配主域可避免图床子域变更（存储桶编号变了）时误判成外链。
DXM_IMAGE_HOST_MARK = "dianxiaomi.com"

# 每行图片上限（超过挂不进去）
SKC_ROW_MAX_IMAGES = 10
# 每行图片下限：表头写的是「图片(3-10张)」，少于 3 张同样过不了保存校验。
# 【这个下限不在 skc_replace_row 里把关，别在那儿加校验】它是【行最终状态】的约束，
# 而最终状态不只由换图这一步决定：多颜色商品里视觉按颜色归属给每行只分到 1~2 张是
# 常态（一件衣服的某个颜色不会有 6 张独立照片），换完不足 3 张时由 service 的
# _skc_size_fallback 把该行现有图原地做合规化补齐。2026-08-24 曾在换图入口拦
# 「新图 < 3 张」，结果 product-985713733384 的 4 个颜色行全被拦死、还把「入口拦下」
# 虚报成「换图失败」，掩盖了真失败。故这里只作为兜底那一层的判据。
SKC_ROW_MIN_IMAGES = 3

# 读某颜色行的图片状态：数量 + 前几张 src（用于替换前后对比）
_JS_SKC_ROW_STATE = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => (tr.textContent || '').includes(__KEY__));
  if (!row) return JSON.stringify({err: '找不到颜色行: ' + __KEY__});
  const imgs = Array.from(row.querySelectorAll('img'))
    .filter(im => (im.currentSrc || im.src || '').startsWith('http'));
  // srcs 是给日志看的短尾；urls 是完整地址（尺寸兜底要按它把图下载回来重做合规化），
  // sizes 用来判服装类下限——0 表示还没加载完，调用方按「读不到」处理。
  return JSON.stringify({count: imgs.length,
    srcs: imgs.map(im => (im.currentSrc || im.src || '').slice(-46)),
    urls: imgs.map(im => im.currentSrc || im.src || ''),
    sizes: imgs.map(im => [im.naturalWidth || 0, im.naturalHeight || 0])});
})()"""


# 删某颜色行的第 1 张图。
# 为什么固定删第 1 张而不按索引删：先挂新图后，旧图仍在前 N 位，逐次删第 1 张删 N 次
# 即可清掉全部旧图，且每次删完 DOM 重排后「第 1 张」始终是下一张待删的旧图——
# 不需要处理索引位移（原脚本同样的取向）。
# 删除按钮是 .single-image 内的 a.icon_delete（2026-08-20 实测，6 张图对应 6 个）。
_JS_SKC_DEL_FIRST = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => (tr.textContent || '').includes(__KEY__));
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const cells = Array.from(row.querySelectorAll('.single-image'))
    .filter(c => Array.from(c.querySelectorAll('img'))
      .some(im => (im.currentSrc || im.src || '').startsWith('http')));
  if (!cells.length) return JSON.stringify({err: '行内已无图片'});
  const before = cells.length;
  const del = cells[0].querySelector('a.icon_delete, .icon_delete');
  if (!del) return JSON.stringify({err: '第 1 张图上找不到删除图标'});
  del.click();
  await sleep(1200);
  // 回读：删除无二次确认框，直接生效
  const after = Array.from(row.querySelectorAll('.single-image'))
    .filter(c => Array.from(c.querySelectorAll('img'))
      .some(im => (im.currentSrc || im.src || '').startsWith('http'))).length;
  return JSON.stringify({deleted: after < before, before, after});
})()"""


# SKC 菜单的判据：比素材图菜单【多一项「应用到所有颜色」】。
# 这是区分两个菜单实例的唯一可靠特征（2026-08-20 实测，见本节开头的踩坑记录）。
SKC_MENU_EXTRA_ITEM = "应用到所有颜色"

# 第 1 步：滚到行按钮并回传坐标。
# 【必须分两次 evaluate】滚动是平滑动画，同一次 evaluate 里读到的坐标是动画中途的值，
# CDP 按那个坐标点会点偏（原脚本记录的坑，本项目同样成立）。故这里只滚动，
# 等待后另起一次 _JS_SKC_BTN_POS 读坐标。
# 【block 位置做成参数】固定 center 时按钮恒落在残留 fixed 菜单停靠的那条带上，
# 整块 9 个候选瞄点一起被盖住；换 nearest/start/end 能把按钮挪开，
# 见 _skc_aim_row_button。
_JS_SKC_BTN_SCROLL = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  if (!sec) return JSON.stringify({err: '找不到变种属性区块'});
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => (tr.textContent || '').includes(__KEY__));
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const btn = Array.from(row.querySelectorAll('button'))
    .find(b => (b.textContent || '').includes('选择图片'));
  if (!btn) return JSON.stringify({err: '行内找不到「选择图片」按钮'});
  btn.scrollIntoView({block: __BLOCK__});
  return JSON.stringify({ok: true});
})()"""

# 第 2 步：读坐标并用 elementFromPoint 校验瞄点确实落在按钮上。
# 不校验就点的话，页面稍有位移就会点到隔壁行的按钮——那会把图挂到错误的颜色行。
#
# 【多瞄点，不是只试中心点】2026-08-24 实测（846106032776 紫罗兰行、890843533224
# 黑色行）：上一张图留下的图片菜单是 position:fixed、z-index 高于表格，正好盖住按钮
# 中心点，瞄点落在 '引用采集图片' 上。那个菜单 Escape 和合成 click 都收不掉
# （见 _park_image_menus），但它通常只盖住按钮的一部分——换个点就能命中。
# 故按「中心 → 左右 → 上下 → 四角内缩」的顺序试，取第一个 elementFromPoint 确实
# 命中按钮的点。安全性不打折：每个候选点都过同一道 elementFromPoint 校验才会被采用，
# 绝不会点到隔壁行。
_JS_SKC_BTN_POS = r"""(() => {
  const sec = document.getElementById('skuAttrsInfo');
  const row = Array.from(sec.querySelectorAll('tr'))
    .find(tr => (tr.textContent || '').includes(__KEY__));
  if (!row) return JSON.stringify({err: '找不到颜色行'});
  const btn = Array.from(row.querySelectorAll('button'))
    .find(b => (b.textContent || '').includes('选择图片'));
  if (!btn) return JSON.stringify({err: '行内找不到「选择图片」按钮'});
  const r = btn.getBoundingClientRect();
  // 候选瞄点：中心优先，其余都在按钮矩形内（4px 内缩，避开边框与圆角）
  const inset = 4;
  const cands = [
    ['center', r.x + r.width / 2,        r.y + r.height / 2],
    ['left',   r.x + inset,              r.y + r.height / 2],
    ['right',  r.right - inset,          r.y + r.height / 2],
    ['top',    r.x + r.width / 2,        r.y + inset],
    ['bottom', r.x + r.width / 2,        r.bottom - inset],
    ['tl',     r.x + inset,              r.y + inset],
    ['tr',     r.right - inset,          r.y + inset],
    ['bl',     r.x + inset,              r.bottom - inset],
    ['br',     r.right - inset,          r.bottom - inset],
  ];
  let x = Math.round(cands[0][1]), y = Math.round(cands[0][2]);
  let at = document.elementFromPoint(x, y);
  let hit = !!(at && (at === btn || btn.contains(at)));
  let aimAt = 'center';
  if (!hit) {
    for (const [name, cx, cy] of cands.slice(1)) {
      const px = Math.round(cx), py = Math.round(cy);
      const el = document.elementFromPoint(px, py);
      if (el && (el === btn || btn.contains(el))) {
        x = px; y = py; at = el; hit = true; aimAt = name;
        break;
      }
    }
  }
  // 未命中时要说清【被什么遮着】：只报「瞄点未命中」会让人以为是滚动时序问题，
  // 而最常见的原因是有全屏弹窗盖着（2026-08-24 实测：描述编辑器 modal 没关，
  // 2560x1257 盖满整页，瞄点落在它的 .page-content 上，连着两次误判成时序脆点）。
  const cls = el => (el && (el.className || '').toString().slice(0, 60)) || '';
  // 【遮挡物必须连 .ant-dropdown 一起查】2026-08-24 实测（890843533224 黑色行）：
  // 瞄点落在 '引用采集图片' 上——上一张图的 SKC/素材图菜单没收起，浮在行按钮上方。
  // 原先只查 .ant-modal-*，那次 blockers 报的是空数组，于是错误信息看着像滚动时序
  // 问题，实际根因是残留浮层。菜单是 position:fixed 且 z-index 高于表格，必查。
  const blockers = hit ? [] : [
    ...Array.from(document.querySelectorAll('.ant-modal-wrap, .ant-modal-mask'))
      .filter(d => d.offsetHeight > 0)
      .map(d => cls(d) + '|' + ((d.querySelector('.ant-modal-title') || {}).textContent || '').trim().slice(0, 20)),
    ...Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || '') && d.offsetHeight > 0)
      .map(d => 'ant-dropdown|' + Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()).join('/').slice(0, 40)),
  ];
  return JSON.stringify({x, y, hit, aimAt: hit ? aimAt : null,
    atText: at ? (at.textContent || at.tagName).trim().slice(0, 16) : null,
    atClass: hit ? '' : cls(at), blockers});
})()"""

# 第 3 步：在 CDP 点击后新建出的 SKC 菜单里点「空间图片」。
# 靠「含应用到所有颜色」认出 SKC 菜单，绝不能只按 4 项菜单文本找——那会命中
# 素材图的菜单实例，导致图挂到素材图上（已实测复现过一次）。
_JS_SKC_CLICK_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const menus = Array.from(document.querySelectorAll('.ant-dropdown')).filter(d => {
    if (/display:\s*none/.test(d.getAttribute('style') || '')) return false;
    const txt = Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return txt.includes('空间图片') && txt.includes(__EXTRA__);
  });
  if (!menus.length) {
    // 把当前所有可见菜单报出来，便于判断是不是又拿到了素材图那个实例
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()));
    return JSON.stringify({err: 'CDP 点击后没出现 SKC 菜单（含「' + __EXTRA__ + '」）',
      visibleMenus: seen});
  }
  // 多个 SKC 菜单实例并存时取最后一个：新建的实例追加在 DOM 末尾
  const menu = menus[menus.length - 1];
  const item = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间图片');
  if (!item) return JSON.stringify({err: 'SKC 菜单里没有「空间图片」项'});
  item.click();
  await sleep(3000);
  const opened = Array.from(document.querySelectorAll('.ant-modal'))
    .some(m => m.offsetHeight > 0 &&
      ((m.querySelector('.ant-modal-title') || {}).textContent || '').includes(__TITLE__));
  return JSON.stringify({opened});
})()"""


# 收起残留的图片菜单浮层（素材图/SKC 那套 4~5 项菜单）。
# 【为什么必须主动收】菜单是 position:fixed、z-index 高于表格，一张图挂完后若没收起，
# 下一行/下一张的「选择图片」按钮就被它盖住：瞄点校验会命中菜单项而不是按钮
# （2026-08-24 实测 890843533224 黑色行，落在 '引用采集图片' 上，整行换图就此中断）。
# 判据只按「含空间图片项」认，避免误伤属性行那些 ant-select 浮层（它们是
# .ant-select-dropdown，另有 _park_ghost_dropdowns 负责，两套别混）。
# 收法是派发 Escape + 点空白：ant 的 dropdown 没有关闭按钮，直接 remove 会让下次
# 点击复用不到实例。返回收了几个，供调用方记日志。
_JS_PARK_IMAGE_MENUS = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const live = () => Array.from(document.querySelectorAll('.ant-dropdown')).filter(d =>
    !/display:\s*none/.test(d.getAttribute('style') || '') && d.offsetHeight > 0
    && Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
      .some(i => (i.textContent || '').trim() === '空间图片'));
  const before = live().length;
  if (!before) return JSON.stringify({parked: 0});
  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  await sleep(400);
  if (live().length) {
    // Escape 收不掉时点页面空白处：ant 的 dropdown 靠 document 上的 click 收起
    document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    await sleep(500);
  }
  const after = live().length;
  return JSON.stringify({parked: before - after, before, after});
})()"""


# 找一个可以安全真实点击的空白点，用来收起 rc-trigger 系浮层。
# 【为什么要真实点击】ant 的 dropdown（rc-trigger）收起逻辑挂在 document 的
# 【真实】mousedown 上，合成 MouseEvent('click') 与合成 Escape 都进不了那条路
# （2026-08-24 实测 846106032776 紫罗兰行：parked 恒 0，display 属性根本没被设回 none）。
# 与「SKC 行按钮必须 CDP 真实点击」是同一类事实：这个页面上开与关都吃真实事件。
#
# 候选点要排掉一切交互元素（按钮/链接/输入/浮层本身），落在容器 div 或 body 上才用——
# 点错地方可能触发别的表单交互。取不到安全点就不点，宁可退回滚动挪位那条路。
_JS_BLANK_POINT = r"""(() => {
  const bad = 'button, a, input, textarea, select, label, .ant-dropdown, .ant-modal,'
    + ' .ant-select, .ant-checkbox, .ant-radio, [role=button], [class*=btn], [class*=icon]';
  const W = window.innerWidth, H = window.innerHeight;
  const pts = [];
  for (const fy of [0.5, 0.35, 0.65, 0.85, 0.2]) {
    pts.push([6, Math.round(H * fy)]);
    pts.push([W - 6, Math.round(H * fy)]);
  }
  for (const [x, y] of pts) {
    const el = document.elementFromPoint(x, y);
    if (!el || el.closest(bad)) continue;
    return JSON.stringify({x, y, tag: el.tagName,
      at: (el.className || '').toString().slice(0, 40)});
  }
  return JSON.stringify({err: '找不到可安全点击的空白点'});
})()"""


async def _park_image_menus(session: BrowserSession) -> dict:
    """收起残留的图片菜单浮层（best-effort，坏了不影响主流程）。

    先走合成事件那一版（便宜，某些实例吃这套），收不掉再用【CDP 真实点击空白点】——
    rc-trigger 的关闭只认真实 mousedown，见 _JS_BLANK_POINT 上方说明。
    """
    try:
        r = await session.eval_json(_JS_PARK_IMAGE_MENUS)
        if r.get("parked"):
            logger.info(f"收起残留图片菜单浮层 {r['parked']} 个（否则会盖住行按钮）")
        if not r.get("after"):
            return r
        # 合成事件收不掉：换真实鼠标点空白处
        bp = await session.eval_json(_JS_BLANK_POINT)
        if bp.get("err"):
            logger.warning(f"没找到安全空白点，跳过真实点击收浮层：{bp['err']}")
            return r
        await _cdp_click_xy(session, bp["x"], bp["y"])
        await asyncio.sleep(0.6)
        r2 = await session.eval_json(_JS_PARK_IMAGE_MENUS)
        left = r2.get("after") if "after" in r2 else r2.get("parked")
        logger.info(f"真实点击空白点({bp['x']},{bp['y']} on {bp.get('tag')})后"
                    f"残留图片菜单 {left} 个")
        return {"parked": r.get("before", 0) - (r2.get("after") or 0),
                "before": r.get("before"), "after": r2.get("after") or 0,
                "realClick": True}
    except Exception as e:
        logger.warning(f"收起图片菜单浮层失败（忽略）：{e}")
        return {"err": str(e)}


async def _skc_aim_row_button(session: BrowserSession, row_keyword: str) -> dict:
    """滚到某颜色行的「选择图片」按钮并求一个经 elementFromPoint 校验过的瞄点。

    【为什么要按不同 scrollIntoView 位置重试】残留的图片菜单是 position:fixed，
    上一张图挂完时它就停在视口中段；而每一行都 `block:'center'` 滚动，下一行的按钮
    被滚到的正是同一片区域，于是 9 个候选瞄点【整块】都被盖住（2026-08-24 起反复出现
    「落在 '引用采集图片' 上」的同一条报错，start/end 之外的退让从未被试过）。
    换 block 让按钮离开固定浮层覆盖的那条带，不动菜单也能命中，是最便宜的一招。

    顺序刻意是「center → 收浮层 → center → nearest/start/end」：先便宜后重，且每次
    都重新 elementFromPoint 校验，绝不会点到隔壁行。返回值同 _JS_SKC_BTN_POS。
    """
    last: dict = {}
    for i, block in enumerate(("center", "center", "nearest", "start", "end")):
        sc = await session.eval_json(
            _JS_SKC_BTN_SCROLL.replace("__KEY__", J(row_keyword))
            .replace("__BLOCK__", J(block)))
        if sc.get("err"):
            return {"err": sc["err"], "stage": "scroll"}
        # 等平滑滚动停稳再读坐标：动画中途的坐标会点偏到隔壁行
        await asyncio.sleep(1.2)
        bp = await session.eval_json(_JS_SKC_BTN_POS.replace("__KEY__", J(row_keyword)))
        if bp.get("err"):
            return {"err": bp["err"], "stage": "locate"}
        if bp.get("hit"):
            if i or bp.get("aimAt") != "center":
                logger.info(f"行按钮瞄点：第 {i + 1} 轮 block={block} "
                            f"aimAt={bp.get('aimAt')} 命中")
            return bp
        last = bp
        if i == 0:
            # 第 1 轮未命中先收浮层：菜单能收掉时后面几轮都省了
            parked = await _park_image_menus(session)
            await asyncio.sleep(0.5)
            logger.info(f"行按钮被浮层遮挡（落在 {bp.get('atText')!r}），"
                        f"已尝试收浮层 parked={parked.get('parked')}，换位重瞄")
    return last


async def _skc_open_space(session: BrowserSession, row_keyword: str) -> dict:
    """点某颜色行的「选择图片」→「空间图片」，打开空间弹窗。

    分三步而不是一个 evaluate 搞定，每一步都是为了绕开一个实测过的坑，见各 JS 常量
    上方的注释。核心是行按钮【必须 CDP 真实点击】才会新建 SKC 专属菜单实例；
    瞄点由 _skc_aim_row_button 负责（含收浮层与换滚动位置的重试）。
    """
    bp = await _skc_aim_row_button(session, row_keyword)
    if bp.get("err"):
        return bp
    if not bp.get("hit"):
        # 带上遮挡物：blockers 同时覆盖弹窗与残留菜单，非空即能定位到具体是谁挡的
        bl = bp.get("blockers") or []
        hint = f"，疑似有浮层未收起：{bl}" if bl else ""
        return {"err": (f"瞄点未命中行按钮（落在 {bp.get('atText')!r}"
                        f" class={bp.get('atClass')!r}）{hint}"),
                "stage": "aim", "pos": bp}

    # CDP 真实点击三连（JS click 不会新建 SKC 菜单，见本节开头）
    await _cdp_click_xy(session, bp["x"], bp["y"])
    await asyncio.sleep(1.6)

    return await session.eval_json(
        _JS_SKC_CLICK_SPACE
        .replace("__EXTRA__", J(SKC_MENU_EXTRA_ITEM))
        .replace("__TITLE__", J(SPACE_MODAL_TITLE))
    )


async def _skc_row_state(session: BrowserSession, row_keyword: str) -> dict:
    """读某颜色行当前的图片数量、完整 URL 与尺寸，并标出破服装类下限的那些。

    tooSmall 是 [{"idx", "url", "size"}]：服装类 1340×1785 是保存时的硬校验，而这
    一行的图可能压根没被阶段⑦ 换过（视觉分不出该颜色的图时整行会被跳过），留在页面
    上的就是 1688 原始小图。2026-08-24 真站实测：咖啡色行 6 张全是 1000×1000 /
    1200×1200 的 cbu01.alicdn.com 外链，阶段⑫ save 报「服装类图片尺寸不能小于
    1340px * 1785px」。故读行状态时一并把这个判据给出来，供调用方做尺寸兜底。

    foreign 是仍挂在非店小秘图床上的图（1688 外链），与 tooSmall 一起构成「这一行
    还没被本管线换过」的判据——续跑时靠它跳过已换好的行，见 _skc_row_matches。
    """
    st = await session.eval_json(
        _JS_SKC_ROW_STATE.replace("__KEY__", J(row_keyword)))
    if st.get("err"):
        return st
    urls, sizes = st.get("urls") or [], st.get("sizes") or []
    small = []
    for i, u in enumerate(urls):
        wh = sizes[i] if i < len(sizes) else None
        w, h = (wh or [0, 0])[:2]
        if w and h and (w < images.CLOTH_MIN_W or h < images.CLOTH_MIN_H):
            small.append({"idx": i, "url": u, "size": f"{w}x{h}"})
    st["tooSmall"] = small
    st["foreign"] = [u for u in urls if DXM_IMAGE_HOST_MARK not in u]
    return st


def _skc_row_matches(state: dict, file_ids: list, expect_count: int) -> dict:
    """判某颜色行是否已经就是「这一批」图，返回 {"done", "reason"}。

    【为什么需要这个】阶段⑤~⑬ 的表单成果一重载就丢，续跑很常见；而原先主路径
    拿到视觉选图就无条件整行换，上一轮已经换好的行（图已入店小秘图床、尺寸达标）
    会被再换一遍——6 张图约一分钟纯白工。

    【判据分两档，宽判据刻意不足以判定"已完成"】
      - 有 file_ids（上一轮换图成功时落进状态文件的清单）：逐个比对页面 src，
        全中才算完成。这是唯一能真正认出"就是这一批图"的判据。
      - 无 file_ids（旧状态文件、或上一轮没跑到这一行）：不判完成，照常换。
        宽判据（数量对 + 都在店小秘图床 + 尺寸达标）认不出"6 张达标图但内容不是
        这一批"，误判会把错图留在页面上还不报错，代价远高于白跑一轮。
    """
    if not file_ids:
        return {"done": False, "reason": "无上一轮的 fileId 清单，无法确认页面上是这一批图"}
    if state.get("count") != expect_count:
        return {"done": False,
                "reason": f"行内 {state.get('count')} 张，本轮要挂 {expect_count} 张，数量不符"}
    if state.get("foreign"):
        return {"done": False,
                "reason": f"仍有 {len(state['foreign'])} 张非店小秘图床的外链图"}
    if state.get("tooSmall"):
        return {"done": False,
                "reason": f"仍有 {len(state['tooSmall'])} 张图低于 "
                          f"{images.CLOTH_MIN_W}x{images.CLOTH_MIN_H}"}
    srcs = state.get("srcs") or []
    missing = [f for f in file_ids
               if not any(f.rsplit("/", 1)[-1] in x for x in srcs)]
    if missing:
        return {"done": False,
                "reason": f"{len(missing)}/{len(file_ids)} 张上一轮的图不在页面上，内容已变"}
    return {"done": True, "reason": f"页面上就是上一轮挂的 {len(file_ids)} 张图"}


async def skc_replace_row(session: BrowserSession, row_keyword: str, img_dir: str,
                          full_cid: Optional[str] = None) -> dict:
    """阶段⑦ 整行替换某颜色的 SKC 图（服装类需 3:4 且不小于 1340×1785）。

    img_dir 里的图按【文件名排序】逐张挂上，故颜色专属图命名 01.jpg 就落在首位、
    免拖拽。图应当已做过 fit_34 合规化（本函数不代做，理由同 set_material）。

    流程：一挂一删地交替（见本节开头的顺序说明），行内数量在 旧/新 数量之间小幅
    摆动，从不触顶、从不为空。任一步失败都立即返回并带上 attached/deletedOld 计数——
    此时行内是「新旧混杂」的中间态，需要人工看一眼再决定是重跑还是手工收拾，所以
    刻意不做自动回滚（回滚同样要靠这套脆弱的页面交互，失败时反而更难判断现场状态）。
    交替顺序让这个中间态比原先的「先全挂再全删」更安全：任意时刻行内图数都不低于
    min(旧图数, 新图数)，不会像预删那样把行削残。
    """
    exts = (".jpg", ".jpeg", ".png", ".webp")
    try:
        files = sorted(f for f in os.listdir(img_dir) if f.lower().endswith(exts))
    except OSError as e:
        return {"status": "error", "stage": "precheck", "err": f"读目录失败: {e}"}
    if not files:
        return {"status": "error", "stage": "precheck", "err": f"目录无图片: {img_dir}"}
    # 【只拦上限，不拦下限——2026-08-24 踩坑后改回】超过 10 张是真的挂不进去，必须拦；
    # 但新图少于 3 张【不能拦】：多颜色商品里视觉按颜色归属给每行分到 1~2 张是常态
    # （实测 product-985713733384：8 张主图分 4 个颜色，每行 1~2 张），一件衣服的某个
    # 颜色本来就不会有 6 张独立照片。行的下限 3 张是【最终状态】的约束，而最终状态
    # 不只由本轮新图决定——换完不足 3 张时 service 的 _skc_size_fallback 会把该行
    # 现有图原地做合规化补齐（实测那 4 行最后都补到了 6 张达标图）。
    # 在这里拦死等于把兜底的工作机会掐掉，还会把「我拦的」虚报成「换图失败」，
    # 掩盖真正的失败。故下限只由兜底那一层负责。
    if len(files) > SKC_ROW_MAX_IMAGES:
        return {"status": "error", "stage": "precheck",
                "err": (f"新图 {len(files)} 张超过每行上限 {SKC_ROW_MAX_IMAGES} 张，"
                        "多出来的会静默挂不进去；请减少图片张数"),
                "newFiles": len(files)}

    base = await _skc_row_state(session, row_keyword)
    if base.get("err"):
        return {"status": "error", "stage": "locate", **base}
    old_count = base["count"]
    logger.info(f"SKC 行「{row_keyword}」现有 {old_count} 张，准备换成 {len(files)} 张新图"
                f"（一挂一删交替，不再预删）")

    async def _del_first(stage: str, ctx: dict):
        """删本行第 1 张（恒为最老的旧图）。成功返回 None，失败返回给调用方的错误 dict。"""
        d = await session.eval_json(_JS_SKC_DEL_FIRST.replace("__KEY__", J(row_keyword)))
        if not d.get("deleted"):
            return {"status": "error", "stage": stage, "detail": d, **ctx}
        await asyncio.sleep(0.4)
        return None

    attached: list = []
    remaining_old = old_count      # 还没删掉的旧图数，恒在行内最前面
    deleted = 0

    for fname in files:
        ctx = {"attached": [a["file"] for a in attached], "deletedOld": deleted,
               "file": fname}
        # 行已满时这一轮先删后挂：先挂会变 11 张挂不进去。此时行内从上限掉 1 张再回来，
        # 【始终非空】，「删空丢行绑定」那条约束仍然成立。
        if remaining_old + len(attached) >= SKC_ROW_MAX_IMAGES and remaining_old > 0:
            err = await _del_first("delete-old", ctx)
            if err:
                return err
            remaining_old -= 1
            deleted += 1

        path = os.path.join(img_dir, fname)
        up = await upload_image(session, path, full_cid=full_cid)
        if up.get("status") != "ok":
            return {"status": "error", "stage": "upload", "upload": up, **ctx}
        opened = await _skc_open_space(session, row_keyword)
        if opened.get("err") or not opened.get("opened"):
            return {"status": "error", "stage": "open-space", "detail": opened, **ctx}
        picked = await _pick_from_space(session, up["fileId"])
        if picked.get("err"):
            await _close_space_modal(session)
            return {"status": "error", "stage": "pick", "detail": picked, **ctx}
        # 逐张回读校验挂到了【本行】：菜单实例全页共用，挂错行时本行数量不增却不报错
        st = await _skc_row_state(session, row_keyword)
        fid = up["fileId"].rsplit("/", 1)[-1]
        landed = any(fid in x for x in (st.get("srcs") or []))
        if not landed:
            logger.error(f"图 {fname} 挂载后未出现在「{row_keyword}」行，可能挂到了别的行")
            return {"status": "error", "stage": "verify-row", "rowState": st, **ctx}
        attached.append({"file": fname, "fileId": up["fileId"]})
        # 【每挂完一张就主动收菜单，不要等下一张被挡了再救】菜单是 position:fixed 停在
        # 视口中段，而下一行按钮也会被滚到视口中段，几何上正好重叠——事后补救要靠
        # 换滚动位置绕（见 _skc_aim_row_button），成本远高于这里顺手收一次。
        # best-effort：收不掉也继续，瞄点那一层还有退让。
        await _park_image_menus(session)

        # 挂成功后立刻删掉一张旧图，把位置还回去——这是交替的核心
        if remaining_old > 0:
            err = await _del_first("delete-old",
                                   {"attached": [a["file"] for a in attached],
                                    "deletedOld": deleted, "file": fname})
            if err:
                return err
            remaining_old -= 1
            deleted += 1
        logger.info(f"已挂 {fname}（行内现 {len(attached) + remaining_old} 张，"
                    f"待删旧图 {remaining_old} 张）")

    # 新图比旧图少时（如 6 旧换 3 新）还有剩余旧图，收尾删干净
    while remaining_old > 0:
        err = await _del_first("delete-old",
                               {"attached": [a["file"] for a in attached],
                                "deletedOld": deleted})
        if err:
            return err
        remaining_old -= 1
        deleted += 1

    final = await _skc_row_state(session, row_keyword)
    ok = final.get("count") == len(files)
    if not ok:
        logger.error(f"行「{row_keyword}」收尾数量不符：期望 {len(files)}，实际 {final.get('count')}")
    return {"status": "ok" if ok else "error",
            "stage": "" if ok else "final-count",
            "row": row_keyword, "oldCount": base["count"],
            "attached": [a["file"] for a in attached],
            # fileId 清单供续跑判「本行是否已是这一批图」（见 _skc_row_matches）
            "fileIds": [a["fileId"] for a in attached], "deletedOld": deleted,
            "finalCount": final.get("count"), "finalSrcs": final.get("srcs")}


# ==================== 阶段⑪ 产品描述长图 ====================
# 业务规则（SKILL.md 阶段⑪）：Temu 只关心商品图。工厂/公司介绍图、促销海报、
# 中文尺码表、与商品无关的图一律删；重复图删；含中文的商品图英化后替换。
#
# 【编辑器是全屏 .ant-modal，不是新页面/新标签】打开后盖住整个编辑页，此时页面上
# 任何 elementFromPoint 都会命中编辑器内的元素，【别误判成「遮挡」】。
#
# 【每点一次「编辑描述」都新叠一个 modal，旧的不销毁】屏幕上看到的是最后打开的那个。
# 用 querySelector 取第一个会把删除/替换写进被盖住的旧弹窗、用户看不到效果。
# 故一律用 _JS_DESC_MODAL 取「最顶层」实例，并在写操作前先关掉多余的。
#
# 【确认框按钮文字是带空格的「确 定」】所有按钮文本匹配前先去掉全部空白再比。
#
# 2026-08-20 实测确认（基准 rowid 173539495450551101，5 个模块）：
#   - 「编辑描述」按钮 offsetHeight 为 0（靠 CSS 悬停显示），但 JS click 直接有效
#   - .using-item 的 data-idx 从 0 起，每项都有 .icon_delete
#   - 编辑器底部按钮只有「保存」「关闭」
#   - 本商品 5 张描述图全是 cbu01.alicdn.com 外链（未落店小秘图床）

# 取「最顶层」描述编辑器弹窗的 JS 表达式（内联进其它 JS 用，故不带外层括号调用）。
# 判据不用弹窗中心点：中心可能落在图片间隙里；用 y+300 并夹到视口内。
# 兜底取 DOM 最后一个——同 z-index 时 DOM 序最后者盖在最上面。
_JS_DESC_MODAL = r"""(() => {
  const list = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content'));
  if (!list.length) return null;
  for (const m of list) {
    const r = m.getBoundingClientRect();
    if (r.width === 0) continue;
    const hit = document.elementFromPoint(r.x + r.width / 2,
      Math.min(r.y + 300, innerHeight - 10));
    if (hit && m.contains(hit)) return m;
  }
  return list[list.length - 1];
})()"""

# 探测编辑器状态：是否已开、有无「编辑描述」按钮、模块图 URL（DOM 顺序即展示顺序）。
# imgs 刻意【不过滤】非 http 的 src：过滤会让这里的下标与删除/替换用的
# querySelectorAll 下标错位（原实现有这个隐患，懒加载未填 src 时会错位）。
_JS_DESC_STATE = r"""(() => {
  const m = __MODAL__;
  const sec = document.getElementById('describeInfo');
  const btn = sec && sec.querySelector('.wireless-description-shadow button');
  if (!m) return JSON.stringify({open: false, hasButton: !!btn,
    editPageImgs: sec ? sec.querySelectorAll('img').length : 0});
  const imgs = Array.from(m.querySelectorAll('.smt-desc-content .desc-img-box img'));
  const modalCount = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content')).length;
  return JSON.stringify({open: true, hasButton: !!btn, modalCount,
    count: imgs.length,
    srcs: imgs.map(i => i.currentSrc || i.src || ''),
    // 真实像素尺寸：服装类下限 1340x1785 要靠它判，naturalWidth 是现成的，
    // 不必为了量尺寸把每张图再下载一遍。未加载完时为 0，调用方按「读不到」处理。
    sizes: imgs.map(i => [i.naturalWidth || 0, i.naturalHeight || 0]),
    usingCount: m.querySelectorAll('.using-item').length});
})()"""


# 打开描述编辑器：JS click 有效（按钮 offsetHeight 为 0 也照样能点）
_JS_DESC_OPEN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const sec = document.getElementById('describeInfo');
  if (!sec) return JSON.stringify({err: '找不到产品描述区块'});
  const btn = sec.querySelector('.wireless-description-shadow button');
  if (!btn) return JSON.stringify({err: '找不到「编辑描述」按钮'});
  btn.scrollIntoView({block: 'center'});
  await sleep(600);
  btn.click();
  await sleep(4000);
  const list = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content'));
  return JSON.stringify({opened: list.length > 0, modalCount: list.length});
})()"""

# 关掉多余的描述编辑器实例，只留最顶层那一个。
# 为什么必须做：每次点「编辑描述」都新叠一层且旧层不销毁，多层并存时写操作可能
# 落在被盖住的旧层上——页面看不出变化，排查时会以为是选择器不对。
_JS_DESC_CLOSE_EXTRA = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const list = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content'));
  if (list.length <= 1) return JSON.stringify({closed: 0, remain: list.length});
  let closed = 0;
  // 留最后一个（最顶层），其余点「关闭」
  for (const m of list.slice(0, -1)) {
    const btn = Array.from(m.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
    if (btn) { btn.click(); closed++; await sleep(1000); }
    // 关闭可能弹二次确认，按钮文字是带空格的「确 定」
    for (let k = 0; k < 3; k++) {
      const cf = Array.from(document.querySelectorAll('.ant-modal-confirm'))
        .find(c => c.offsetHeight > 0);
      if (!cf) break;
      const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
      if (!ok) break;
      ok.click();
      await sleep(1200);
    }
  }
  const remain = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content')).length;
  return JSON.stringify({closed, remain});
})()"""


async def _desc_ensure_open(session: BrowserSession) -> dict:
    """确保描述编辑器已打开且只有一层，返回模块状态。

    所有描述写操作的前置：既保证编辑器在，又顺带做「不在编辑页」的早失败。

    【「没有编辑描述按钮」必须报出当前 URL】2026-08-24 实测（890843533224）：⑬ 逐张
    替换到第 11 张时，本进程的页签被【另一个进程】导航去了草稿列表，此后每一张都报
    「当前页面没有「编辑描述」按钮」——报错本身没错，但它把「页面被换走了」说成
    「可能不在编辑页」，五条一模一样的信息看不出是同一个外部原因，只会以为是选择器
    失效。eval 能跑通说明页签活着（真关了会抛 TargetClosedError），故区别只在 URL：
    带上它，「被导航走」与「编辑页结构变了」当场就能分开。
    """
    st = await session.eval_json(_JS_DESC_STATE.replace("__MODAL__", _JS_DESC_MODAL))
    if not st.get("open"):
        if not st.get("hasButton"):
            url = ""
            try:
                url = session.page.url or ""
            except Exception:
                pass          # 只是错误信息的补充，取不到不影响判定
            if url and "popTemu/edit" not in url:
                return {"err": f"页签已不在编辑页（当前 {url}），"
                               "描述阶段无法继续。常见原因：另一个发布/采集进程"
                               "共用了这个页签并把它导航走了",
                        "url": url, "navigatedAway": True}
            return {"err": "当前页面没有「编辑描述」按钮，可能不在编辑页"
                           + (f"（当前 {url}）" if url else ""),
                    "url": url}
        opened = await session.eval_json(_JS_DESC_OPEN)
        if opened.get("err") or not opened.get("opened"):
            return {"err": opened.get("err") or "编辑器打开失败", "detail": opened}
        st = await session.eval_json(_JS_DESC_STATE.replace("__MODAL__", _JS_DESC_MODAL))
    if (st.get("modalCount") or 0) > 1:
        extra = await session.eval_json(_JS_DESC_CLOSE_EXTRA)
        logger.info(f"关掉多余的描述编辑器实例：{extra}")
        st = await session.eval_json(_JS_DESC_STATE.replace("__MODAL__", _JS_DESC_MODAL))
    return st


async def desc_map(session: BrowserSession, info_path: str = "") -> dict:
    """阶段⑪ 只读：列出描述模块（序号 + URL + 是否已落店小秘图床）。

    序号从 1 起，与 desc_delete / desc_replace 的入参一致。
    info_path 给了就顺带把 product-info.json 里的 complianceNotes 带出来，
    供调用方（或人）按「哪张该删」对照——判断本身不在这里做，这个函数保持只读。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    # 尺寸一并带出：服装类下限 1340×1785 是硬红线，而「内容干净」与「尺寸达标」
    # 是两回事——一张干净的 900×1200 商品图内容上该 keep，却过不了保存校验
    # （2026-08-23 真站取证：描述区 10 张 1688 外链全部 1000×1000 / 900×1200）。
    # naturalWidth 为 0 表示还没加载完，按「读不到」处理，不当成不达标。
    sizes = st.get("sizes") or []
    mods = []
    for i, src in enumerate(st.get("srcs") or [], 1):
        wh = sizes[i - 1] if i - 1 < len(sizes) else None
        w, h = (wh or [0, 0])[:2]
        m = {"pos": i, "url": src, "onDxmHost": "dianxiaomi.com" in src}
        if w and h:
            m["size"] = f"{w}x{h}"
            m["tooSmall"] = w < images.CLOTH_MIN_W or h < images.CLOTH_MIN_H
        mods.append(m)
    out = {"status": "ok", "count": len(mods), "modules": mods}
    if info_path and os.path.exists(info_path):
        try:
            with open(info_path, encoding="utf-8") as f:
                info = json.load(f)
            notes = info.get("complianceNotes") or {}
            out["complianceNotes"] = notes
            out["cleanFiles"] = notes.get("cleanFiles")
        except Exception as e:
            # 只是附带信息，读不到不影响主结果
            logger.warning(f"读 complianceNotes 失败：{e}")
    return out


# 枚举描述区的【文字模块】：data-idx + 文本内容。
#
# 【为什么单独一个 JS 而不并进 _JS_DESC_STATE】那个函数按 .desc-img-box img 枚举，
# 下标即 desc_map 的 pos，混进非图片模块会让 pos 与删除/替换的下标错位（见下方
# _JS_DESC_IDX_MAP 的注释，那次错位真删错了两张图）。文字模块用 data-idx 直接寻址，
# 与图片的 pos 体系互不干扰。
#
# 2026-08-24 真站探查（rowid 173539495454339053，19 个模块）：data-idx=0 是文字模块，
# 内容是 1688 的关联商品 JSON 残留 `{"styleType":"offer-type-1","items":"8886...}`，
# 纯垃圾；另有商品确实用文字模块放尺码对照（`80【身高65-75cm】`），那种要英化保留。
_JS_DESC_TEXT_MAP = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const mods = Array.from(m.querySelectorAll('.smt-content-center .smt-desc-content'));
  const items = [];
  mods.forEach(c => {
    if (c.querySelector('.desc-img-box img')) return;   // 图片模块不管
    const box = c.querySelector('.desc-content');
    const txt = ((box ? box.innerText : c.innerText) || '').trim();
    items.push({idx: c.getAttribute('data-idx'), text: txt, len: txt.length});
  });
  return JSON.stringify({items: items, modCount: mods.length});
})()"""


# 描述图序号（pos，从 1 起）到左侧「使用中模块」列表 data-idx 的映射表。
#
# 【为什么不能拿 pos-1 当 data-idx】描述区是【图文混排】的：每个内容模块都是一个
# .smt-desc-content，自带与左侧列表一致的 data-idx，但「文字」模块不含图片盒子。
# 而 desc_map 按 .desc-img-box img 枚举 pos，只数图片。于是只要描述区里混有非图片
# 模块，两套序号就整体错位——2026-08-24 实测（rowid 173539495454339053）：19 个模块
# 里 data-idx=0 是「文字」模块（存 offer JSON），18 张图对应 data-idx 1..18，
# pos-1 全部偏 1；删 pos 3/2 实际删掉的是 pos 2/1，删 pos 1 命中文字模块——
# 图片数不变，被判定为「删除失败」。真实后果比报错更糟：前两张删错了对象。
#
# 映射按【模块自身的 data-idx】建立而不是按出现次序计数：data-idx 是平台自己维护的
# 关联键（与左侧 .using-item 一一对应），比「第几个含图模块」更贴近页面真相。
_JS_DESC_IDX_MAP = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const mods = Array.from(m.querySelectorAll('.smt-content-center .smt-desc-content'));
  const map = [];
  mods.forEach(c => {
    // 只收含图片盒子的模块，顺序即 desc_map 的 pos 顺序（实测 src 逐张吻合）
    if (c.querySelector('.desc-img-box img')) map.push(c.getAttribute('data-idx'));
  });
  return JSON.stringify({map, modCount: mods.length});
})()"""


# 改写 data-idx 为 __IDX__ 的文字模块内容。
#
# 【必须走右侧面板的 textarea】模块自身的 div.desc-content 不是 contenteditable、
# 也不是 input，直接改 innerText 保存时不生效（组件状态没变）。流程是：
# 点左侧 .using-item → 右侧 .smt-content-right 渲染「文字模块」面板 → 写它的
# textarea.ant-input。写完必须派发 input 事件，否则 Vue 不同步、保存后回到原文
# （本项目其它表单字段同样的坑，见 _js_fill_by_label）。
#
# 500 字符是面板自己标的上限（「总字符数:160 / 500」），超了平台会截断，
# 故调用方传进来前就该截好；这里再兜一刀，避免静默截断。
_JS_DESC_TEXT_SET = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({status: 'error', reason: '编辑器不在'});
  const item = m.querySelector('.using-item[data-idx="__IDX__"]');
  if (!item) return JSON.stringify({status: 'error', reason: 'using-item-not-found'});
  item.scrollIntoView({block: 'center'});
  await sleep(300);
  item.click();
  await sleep(1200);            // 面板渲染有入场过程，读太早拿不到 textarea

  const panel = m.querySelector('.smt-content-right');
  if (!panel) return JSON.stringify({status: 'error', reason: 'panel-not-found'});
  const ta = panel.querySelector('textarea.ant-input, textarea');
  if (!ta) return JSON.stringify({status: 'error', reason: 'textarea-not-found'});

  const before = ta.value;
  const text = __TEXT__;
  if (text.length > 500) {
    return JSON.stringify({status: 'error', reason: 'text-too-long',
                           len: text.length});
  }
  // 原生 setter + input 事件：Vue 只认事件，直接赋 value 不同步
  const desc = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value');
  desc.set.call(ta, text);
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.dispatchEvent(new Event('change', {bubbles: true}));
  await sleep(600);

  // 回读：面板 textarea 与模块本体都要变（后者证明组件真的同步了）
  const mod = m.querySelector(
    '.smt-content-center .smt-desc-content[data-idx="__IDX__"] .desc-content');
  return JSON.stringify({
    status: 'ok', before: before.slice(0, 80),
    readback: ta.value.slice(0, 80),
    modText: mod ? (mod.innerText || '').trim().slice(0, 80) : null,
    filled: ta.value.trim() === text.trim(),
  });
})()"""


# 删左侧列表里 data-idx 为 __IDX__ 的模块。
# 走左侧「使用中模块」列表的垃圾桶图标，JS click 有效、无二次确认框。
#
# 【判据用 data-id 而不是图片计数】计数只能看出「少了一个」，看不出少的是不是目标：
# 删到非图片模块时图片数不变，会被误判成失败（见 _JS_DESC_IDX_MAP 的实测记录）。
# data-id 是模块的稳定标识，点击前先记下，删后确认它真的从列表里消失了。
_JS_DESC_DELETE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const ids = () => Array.from(m.querySelectorAll('.using-item'))
    .map(i => i.getAttribute('data-id'));
  const before = m.querySelectorAll('.smt-desc-content .desc-img-box img').length;
  const item = m.querySelector('.using-item[data-idx="' + __IDX__ + '"]');
  if (!item) return JSON.stringify({err: '找不到 data-idx=' + __IDX__ + ' 的模块'});
  const targetId = item.getAttribute('data-id');
  const del = item.querySelector('.icon_delete');
  if (!del) return JSON.stringify({err: '模块上没有删除图标'});
  del.click();
  await sleep(1200);
  const after = m.querySelectorAll('.smt-desc-content .desc-img-box img').length;
  return JSON.stringify({deleted: !ids().includes(targetId), targetId,
    before, after, imgDropped: after < before});
})()"""


async def desc_delete(session: BrowserSession, positions: list) -> dict:
    """阶段⑪ 删除指定序号的描述模块（序号从 1 起）。

    【倒序删除】positions 去重后从大到小删：删掉一个后 data-idx 会重排，
    从后往前删则每次只影响比它更大的下标，已处理过的不受影响。正序删会错位。

    【入参是图片序号，不是列表下标】pos 与左侧列表的 data-idx 在图文混排时不相等，
    删除前先建映射（见 _JS_DESC_IDX_MAP 里 2026-08-24 的实测记录）。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    n = st.get("count") or 0
    want = sorted({int(p) for p in positions}, reverse=True)
    bad = [p for p in want if p < 1 or p > n]
    if bad:
        # 先整体校验再动手：删一半才发现越界会留下难以判断的中间态
        return {"status": "error", "stage": "precheck",
                "err": f"序号越界 {bad}（当前共 {n} 个模块）"}

    # pos 是「第几张图」，data-idx 是左侧列表下标，图文混排时两者不等（见
    # _JS_DESC_IDX_MAP）。映射一次全批复用：倒序删只影响比它大的 data-idx，
    # 前面待删项的映射不受影响。
    mp = await session.eval_json(_JS_DESC_IDX_MAP.replace("__MODAL__", _JS_DESC_MODAL))
    if mp.get("err"):
        return {"status": "error", "stage": "idxmap", **mp}
    idx_map = mp.get("map") or []
    if len(idx_map) != n:
        # 对不上说明页面结构与预期不符，宁可不动手：错位删除会删掉不该删的图
        return {"status": "error", "stage": "idxmap",
                "err": f"图片模块数 {len(idx_map)} 与描述图数 {n} 不一致，"
                       f"拒绝删除以免错位", "detail": mp}

    log = []
    for pos in want:
        r = await session.eval_json(
            _JS_DESC_DELETE.replace("__MODAL__", _JS_DESC_MODAL)
                           .replace("__IDX__", str(idx_map[pos - 1])))
        if r.get("err") or not r.get("deleted"):
            return {"status": "error", "stage": f"delete-pos{pos}",
                    "dataIdx": idx_map[pos - 1], "detail": r, "done": log}
        log.append({"pos": pos, "dataIdx": idx_map[pos - 1], "left": r.get("after")})
        await asyncio.sleep(0.4)

    final = await session.eval_json(_JS_DESC_STATE.replace("__MODAL__", _JS_DESC_MODAL))
    return {"status": "ok", "deleted": [x["pos"] for x in log],
            "countBefore": n, "countAfter": final.get("count"), "log": log}


# 保存描述。保存成功弹窗会自动关闭，故「弹窗还在」本身就是异常信号。
# 判据不看 toast：编辑页的消息提示是自定义实现，.ant-message 捕获不到。
_JS_DESC_SAVE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const save = Array.from(m.querySelectorAll('button')).find(b => norm(b.textContent) === '保存');
  if (!save) return JSON.stringify({err: '找不到保存按钮'});
  save.click();
  await sleep(3000);

  // 保存成功编辑器会自己关闭；没关就主动点「关闭」并处理二次确认（「确 定」带空格）
  let stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
    .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0);
  if (stillOpen) {
    const top = __MODAL__;
    const cl = top && Array.from(top.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
    if (cl) { cl.click(); await sleep(1500); }
    for (let k = 0; k < 3; k++) {
      const cf = Array.from(document.querySelectorAll('.ant-modal-confirm')).find(c => c.offsetHeight > 0);
      if (!cf) break;
      const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
      if (!ok) break;
      ok.click();
      await sleep(1200);
    }
    stillOpen = Array.from(document.querySelectorAll('.ant-modal'))
      .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0);
  }

  // 回读【编辑页】描述区（不是弹窗）。两条业务校验：
  //   1. 所有描述图都已落店小秘图床（外链没被平台转存，发布可能被拦）
  //   2. 尺寸都达到服装类下限——【与图床无关的独立一条】：一张已转存到店小秘的图
  //      也可能仍是 900x1200（本商品的原始状态就是如此），只查图床看不出来，
  //      而它照样会让阶段⑫ save 静默弹回（2026-08-23 实测）。
  const sec = document.getElementById('describeInfo');
  const imgs = sec ? Array.from(sec.querySelectorAll('img'))
    .filter(i => (i.currentSrc || i.src || '').startsWith('http')) : [];
  const urls = imgs.map(i => i.currentSrc || i.src || '');
  const small = [];
  imgs.forEach((i, k) => {
    const w = i.naturalWidth || 0, h = i.naturalHeight || 0;
    // 0 表示还没加载完，按「读不到」跳过而不是当成不达标
    if (w && h && (w < __MINW__ || h < __MINH__))
      small.push({pos: k + 1, size: w + 'x' + h});
  });
  return JSON.stringify({stillOpen, descImgs: urls.length,
    dxmHosted: urls.filter(u => u.includes('dianxiaomi.com')).length,
    tooSmall: small,
    foreignHosts: [...new Set(urls.filter(u => !u.includes('dianxiaomi.com'))
      .map(u => { try { return new URL(u).host; } catch (e) { return '?'; } }))]});
})()"""


async def desc_text_map(session: BrowserSession) -> dict:
    """阶段⑬ 只读：列出描述区的【文字模块】（data-idx + 文本）。

    与 desc_map 分开：那个按 .desc-img-box img 枚举、序号是「第几张图」；
    文字模块不含图片盒子，用 data-idx 直接寻址，两套序号互不干扰
    （混用会错位，见 _JS_DESC_IDX_MAP 里 2026-08-24 删错图的实测记录）。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    r = await session.eval_json(
        _JS_DESC_TEXT_MAP.replace("__MODAL__", _JS_DESC_MODAL))
    if r.get("err"):
        return {"status": "error", **r}
    items = r.get("items") or []
    return {"status": "ok", "count": len(items), "texts": items,
            "modCount": r.get("modCount")}


async def desc_text_apply(session: BrowserSession, plan: list) -> dict:
    """阶段⑬ 按计划处理文字模块：英化改写 或 删除。

    plan: [{"idx": "0", "action": "translate"|"delete"|"keep",
            "text": "<translate 时的英文正文>", "reason": "..."}]
    idx 用 desc_text_map 返回的 data-idx 原值。

    【顺序：先全部 translate，再倒序 delete】两个动作都按 data-idx 寻址，而删除
    会让后续 data-idx 重排。先删就会把待翻译项的 idx 改掉，写到别的模块上去。
    删除本身倒序（大→小），理由同 desc_delete。

    单项失败不拖垮整体（best-effort）：记进 failed 继续，交调用方决定是否告警。
    描述文字不是必填项，改不动就保留原文，比中断整个商品划算。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}

    translated, deleted, failed, kept = [], [], [], []

    # 1) 先改写（此时 data-idx 还没被删除动作扰动）
    for p in plan:
        if (p.get("action") or "") != "translate":
            continue
        idx, text = str(p.get("idx")), (p.get("text") or "").strip()
        if not text:
            failed.append({"idx": idx, "err": "translate 但没给 text"})
            continue
        if len(text) > 500:
            text = text[:500]          # 面板上限，超了平台会静默截断
            logger.warning(f"文字模块 idx={idx} 译文超 500 字符，已截断")
        r = await session.eval_json(
            _JS_DESC_TEXT_SET.replace("__MODAL__", _JS_DESC_MODAL)
                             .replace("__IDX__", idx)
                             .replace("__TEXT__", J(text)))
        if r.get("status") == "ok" and r.get("filled"):
            translated.append({"idx": idx, "text": text[:60],
                               "reason": p.get("reason", "")})
        else:
            failed.append({"idx": idx, "action": "translate", **r})
            logger.warning(f"文字模块 idx={idx} 改写失败：{r.get('reason') or r}")

    # 2) 再倒序删除
    dels = sorted({str(p.get("idx")) for p in plan
                   if (p.get("action") or "") == "delete"},
                  key=lambda x: int(x) if str(x).isdigit() else 0, reverse=True)
    for idx in dels:
        r = await session.eval_json(
            _JS_DESC_DELETE.replace("__MODAL__", _JS_DESC_MODAL)
                           .replace("__IDX__", idx))
        if r.get("err") or not r.get("deleted"):
            failed.append({"idx": idx, "action": "delete", **r})
            logger.warning(f"文字模块 idx={idx} 删除失败：{r.get('err') or r}")
        else:
            deleted.append(idx)

    kept = [str(p.get("idx")) for p in plan if (p.get("action") or "") == "keep"]
    return {"status": "ok" if not failed else "partial",
            "translated": translated, "deleted": deleted,
            "kept": kept, "failed": failed}


async def desc_save(session: BrowserSession) -> dict:
    """阶段⑪ 保存描述编辑器的改动。

    注意这只保存【描述编辑器】，整个商品还要再走一次阶段⑫ save 才落库。

    返回 status="validation-error" 而不是 error 的情形：保存后编辑页描述区仍有非
    店小秘图床的外链图。那说明这些图没被平台转存，发布时可能被拦——但也可能是本商品
    本来就没替换过描述图（外链是采集时的原始状态），故不当硬错误、交调用方判断。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    r = await session.eval_json(_JS_DESC_SAVE
                                .replace("__MODAL__", _JS_DESC_MODAL)
                                .replace("__MINW__", str(images.CLOTH_MIN_W))
                                .replace("__MINH__", str(images.CLOTH_MIN_H)))
    if r.get("err"):
        return {"status": "error", "stage": "save", **r}
    if r.get("stillOpen"):
        killed = await session.kill_stuck_modals()
        r["killedStuck"] = killed.get("removed")
        return {"status": "error", "stage": "close", "detail": r}
    if not r.get("descImgs"):
        return {"status": "error", "stage": "readback",
                "err": "保存后编辑页描述区没有图片", "detail": r}
    all_hosted = r["descImgs"] == r["dxmHosted"]
    small = r.get("tooSmall") or []
    notes = []
    if not all_hosted:
        notes.append("仍有非店小秘图床的外链图，发布可能被拦")
    if small:
        notes.append(f"仍有 {len(small)} 张图低于 "
                     f"{images.CLOTH_MIN_W}x{images.CLOTH_MIN_H}，阶段⑫ 保存会被静默弹回")
    return {"status": "ok" if (all_hosted and not small) else "validation-error",
            "descImgs": r["descImgs"], "dxmHosted": r["dxmHosted"],
            "foreignHosts": r.get("foreignHosts"), "tooSmall": small,
            "note": "；".join(notes)}


# 关掉描述编辑器（若开着）。点「关闭」而非「保存」——只负责让出屏幕，
# 不替调用方决定改动是否落库。二次确认按钮文案带空格（「确 定」），故 norm 掉空白再比。
_JS_DESC_CLOSE_IF_OPEN = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s/g, '');
  const top = Array.from(document.querySelectorAll('.ant-modal'))
    .filter(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0).pop();
  if (!top) return JSON.stringify({already: true});
  const cl = Array.from(top.querySelectorAll('button')).find(b => norm(b.textContent) === '关闭');
  if (!cl) return JSON.stringify({err: '描述编辑器里找不到「关闭」按钮'});
  cl.click();
  await sleep(1500);
  for (let k = 0; k < 3; k++) {
    const cf = Array.from(document.querySelectorAll('.ant-modal-confirm')).find(c => c.offsetHeight > 0);
    if (!cf) break;
    const ok = Array.from(cf.querySelectorAll('button')).find(b => norm(b.textContent) === '确定');
    if (!ok) break;
    ok.click();
    await sleep(1200);
  }
  return JSON.stringify({
    stillOpen: Array.from(document.querySelectorAll('.ant-modal'))
      .some(x => x.querySelector('.smt-desc-content') && x.offsetHeight > 0),
    visibleMasks: Array.from(document.querySelectorAll('.ant-modal-mask'))
      .filter(m => m.offsetHeight > 0).length});
})()"""


async def ensure_desc_closed(session: BrowserSession) -> dict:
    """确认描述编辑器已关闭；没关就点「关闭」（含二次确认）。

    【为什么必须单独有这一步】描述编辑器是全屏 modal（实测 2560×1257），开着时它盖住
    整个编辑页，后续所有靠坐标点击的阶段全部失效——而失败信息是「瞄点未命中」，看着
    像滚动时序问题。2026-08-24 实测：跑完阶段⑬ 后编辑器留着，阶段⑦ SKC 换图连续两次
    报 open-space 失败，诊断才发现瞄点落在描述弹窗的 .page-content 上。

    不点「保存」：这里只负责关，改动该不该落库由调用方在 desc_save 里决定。
    也不用 kill_stuck_modals 暴力 remove——那会把正常打开、内容未保存的编辑器一起
    掀掉；只有点「关闭」走不通时才交由调用方去做那种兜底。
    """
    st = await session.eval_json(_JS_DESC_CLOSE_IF_OPEN)
    if st.get("already"):
        return {"status": "ok", "wasOpen": False}
    if st.get("err"):
        return {"status": "error", "reason": st["err"]}
    if st.get("stillOpen"):
        return {"status": "error", "reason": "点了关闭但编辑器仍开着",
                "visibleMasks": st.get("visibleMasks")}
    return {"status": "ok", "wasOpen": True,
            "visibleMasks": st.get("visibleMasks")}


# ---- 阶段⑪ 描述图替换（中文图英化后回填）------------------------------------
# 【描述编辑器有自己的一套菜单，菜单项与素材图/SKC 完全不同】2026-08-20 实测：
#   素材图 / SKC 行：本地图片 / 空间图片 / 网络图片 / 引用采集图片[ / 应用到所有颜色]
#   描述编辑器：    本地上传 / 空间上传 / 网络上传 / 引用skc轮播图 / 引用采集图片 / 小秘美图
# 注意是「空间【上传】」不是「空间【图片】」。我起初照素材图那套去找「空间图片」，
# 在 JS click / CDP 点击 / 清浮层之间反复试错都失败——可见菜单始终是素材图那个残留
# 实例，而真正的描述菜单一直隐藏着没被触发到。判据用「含空间上传 + 含引用skc轮播图」
# 最稳（两项组合在页面上唯一）。
#
# 触发序列（缺一步都不行）：
#   1. CDP 真实点击模块图 → 右侧面板出现「更换图片」链接（JS click 建立不了绑定）
#   2. CDP 真实点击「更换图片」→ 描述专属菜单展开在链接旁（left/top 贴着链接）
#   3. 点菜单里「空间上传」→ 空间弹窗打开
#   4. 弹窗内选中刚直传的图 → 确定
# 第 3、4 步必须在同一个 evaluate 里连贯做完：菜单会自动收起。
#
# 【不要对描述编辑器做 elementFromPoint 校验】编辑器是全屏 modal，任何坐标都会命中
# 编辑器内的 IMG，看着像「被遮挡」其实正常——我在这里差点误判并放弃这条路。

# 描述编辑器菜单的两个特征项（组合在页面上唯一）
DESC_MENU_ITEMS = ("空间上传", "引用skc轮播图")

# 点「空间上传」→ 空间弹窗选图 → 确定。
# 空间弹窗的识别要【排除描述编辑器自身】：编辑器也是 .ant-modal 且文本里可能含
# 「图片空间」字样，不排除会命中自己然后在里面找不到 .img-item。
_JS_DESC_PICK_SPACE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const menu = Array.from(document.querySelectorAll('.ant-dropdown')).find(x => {
    if (/display:\s*none/.test(x.getAttribute('style') || '')) return false;
    const t = Array.from(x.querySelectorAll('.ant-dropdown-menu-item'))
      .map(i => (i.textContent || '').trim());
    return __ITEMS__.every(w => t.includes(w));
  });
  if (!menu) {
    const seen = Array.from(document.querySelectorAll('.ant-dropdown'))
      .filter(d => !/display:\s*none/.test(d.getAttribute('style') || ''))
      .map(d => Array.from(d.querySelectorAll('.ant-dropdown-menu-item'))
        .map(i => (i.textContent || '').trim()));
    return JSON.stringify({stage: 'menu', err: '描述专属菜单未展开', visibleMenus: seen});
  }
  const it = Array.from(menu.querySelectorAll('.ant-dropdown-menu-item'))
    .find(i => (i.textContent || '').trim() === '空间上传');
  if (!it) return JSON.stringify({stage: 'menu', err: '菜单里没有「空间上传」项'});
  it.click();
  await sleep(3000);

  const modal = Array.from(document.querySelectorAll('.ant-modal'))
    .find(m => m.offsetHeight > 0 && !m.querySelector('.smt-desc-content')
      && (m.textContent || '').includes('图片空间'));
  if (!modal) {
    return JSON.stringify({stage: 'modal', err: '空间弹窗没打开',
      openTitles: Array.from(document.querySelectorAll('.ant-modal'))
        .filter(m => m.offsetHeight > 0)
        .map(m => ((m.querySelector('.ant-modal-title') || {}).textContent || '').trim())});
  }
  const items = Array.from(modal.querySelectorAll('.img-item'));
  const hit = items.find(x => Array.from(x.querySelectorAll('img'))
    .some(i => (i.src || '').includes(__FID__)));
  if (!hit) return JSON.stringify({stage: 'pick', err: '弹窗里找不到刚上传的图',
    itemCount: items.length});
  hit.click();
  await sleep(900);
  const ok = Array.from(modal.querySelectorAll('button'))
    .find(b => (b.textContent || '').replace(/\s/g, '') === '确定');
  if (!ok) return JSON.stringify({stage: 'confirm', err: '找不到确定按钮'});
  ok.click();
  await sleep(2000);
  return JSON.stringify({stage: 'ok'});
})()"""


async def _cdp_click_xy(session: BrowserSession, x: int, y: int) -> None:
    """CDP 真实鼠标点击指定坐标（mouseMoved → mousePressed → mouseReleased）。

    每步间隔 120ms：连发太快时页面的事件处理来不及跟上，表现为点了没反应。
    描述编辑器与 SKC 行按钮都必须走这条路，JS click 建立不了绑定。
    """
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        params = {"type": kind, "x": x, "y": y}
        if kind != "mouseMoved":
            params.update(button="left", clickCount=1)
        await session.cdp("Input.dispatchMouseEvent", params)
        await asyncio.sleep(0.12)


# 滚动到第 pos 个模块图（pos 从 1 起）。与读坐标分成两次 evaluate，理由同 SKC：
# 平滑滚动未停就读坐标会点偏。
_JS_DESC_BOX_SCROLL = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const boxes = m.querySelectorAll('.smt-desc-content .desc-img-box');
  const b = boxes[__IDX__];
  if (!b) return JSON.stringify({err: '没有第 ' + (__IDX__ + 1) + ' 个模块'});
  b.scrollIntoView({block: 'center'});
  return JSON.stringify({ok: true, total: boxes.length});
})()"""

# 读第 pos 个模块图的中心坐标。
# 刻意【不做 elementFromPoint 校验】：编辑器是全屏 modal，任何坐标都会命中编辑器内的
# IMG，校验必然「失败」——那不是遮挡（见本节开头说明）。
_JS_DESC_BOX_POS = r"""(() => {
  const m = __MODAL__;
  const b = m.querySelectorAll('.smt-desc-content .desc-img-box')[__IDX__];
  if (!b) return JSON.stringify({err: '模块消失了'});
  const r = b.getBoundingClientRect();
  const img = b.querySelector('img');
  return JSON.stringify({x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
    srcBefore: img ? (img.currentSrc || img.src || '') : null});
})()"""

# 读右侧面板「更换图片」链接的坐标（点模块图后才出现）
_JS_DESC_REPLACE_LINK = r"""(() => {
  const m = __MODAL__;
  if (!m) return JSON.stringify({err: '编辑器不在'});
  const a = Array.from(m.querySelectorAll('.smt-content-right a'))
    .find(x => (x.textContent || '').trim() === '更换图片');
  if (!a) return JSON.stringify({err: '右侧面板没有「更换图片」链接（模块图可能没点中）'});
  const r = a.getBoundingClientRect();
  return JSON.stringify({x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
})()"""


async def desc_replace(session: BrowserSession, pos: int, image_path: str,
                       full_cid: Optional[str] = None,
                       expect_url: Optional[str] = None) -> dict:
    """阶段⑪ 把第 pos 个描述模块的图换成本地图（序号从 1 起）。

    典型用途：中文描述图经 images.edit_image 英化后回填。
    英化质量【必须另做视觉质检】——实测生图会残留拼音、误译品类，只看「中文没了」
    会把带乱码文案的图挂上去（见 llm.ask_json_with_images）。本函数只负责替换动作。

    【expect_url 是防错位的硬闸门】pos 是描述区的【当前】序号，删过模块后整体前移，
    调用方拿旧序号进来会张冠李戴：越界时报错还算好的，没越界则静默把 A 图的英化产物
    贴到 B 图上——收尾的 landed 只校验「新 fileId 落在了 pos」，看不出位置错。故调用方
    只要知道该替换哪张源图，就把它的 URL 传进来，替换前后各比对一次；不一致直接拒绝
    动作，把静默错位变成显式失败（2026-08-24 实测：⑬ 删 3 张后按旧 pos 6 替换，
    描述区只剩 5 个模块）。

    改完记得调 desc_save：描述编辑器点「关闭」即丢弃改动。
    """
    st = await _desc_ensure_open(session)
    if st.get("err"):
        return {"status": "error", **st}
    n = st.get("count") or 0
    if pos < 1 or pos > n:
        return {"status": "error", "stage": "precheck",
                "err": f"序号 {pos} 越界（当前共 {n} 个模块）"}
    # 先用已读到的 srcs 比对，能在上传之前就拦下错位，省掉一次白传
    if expect_url:
        cur = (st.get("srcs") or [])[pos - 1] if pos <= len(st.get("srcs") or []) else ""
        if cur != expect_url:
            return {"status": "error", "stage": "expect-mismatch",
                    "err": f"第 {pos} 个模块当前不是期望的源图，拒绝替换以免错位",
                    "expect": expect_url, "actual": cur}

    up = await upload_image(session, image_path, full_cid=full_cid)
    if up.get("status") != "ok":
        return {"status": "error", "stage": "upload", "upload": up}
    fid = up["fileId"].rsplit("/", 1)[-1]

    idx = str(pos - 1)
    sc = await session.eval_json(
        _JS_DESC_BOX_SCROLL.replace("__MODAL__", _JS_DESC_MODAL).replace("__IDX__", idx))
    if sc.get("err"):
        return {"status": "error", "stage": "scroll", **sc}
    await asyncio.sleep(1.0)

    bp = await session.eval_json(
        _JS_DESC_BOX_POS.replace("__MODAL__", _JS_DESC_MODAL).replace("__IDX__", idx))
    if bp.get("err"):
        return {"status": "error", "stage": "locate", **bp}
    # 滚动过程中若页面自己动过（懒加载补图等），这里再比一次才算真的对上
    if expect_url and bp.get("srcBefore") != expect_url:
        return {"status": "error", "stage": "expect-mismatch",
                "err": f"滚动后第 {pos} 个模块的图与期望源图不一致，拒绝替换",
                "expect": expect_url, "actual": bp.get("srcBefore"), "upload": up}
    # 1. 点模块图，让右侧面板出现「更换图片」
    await _cdp_click_xy(session, bp["x"], bp["y"])
    await asyncio.sleep(1.5)

    lp = await session.eval_json(_JS_DESC_REPLACE_LINK.replace("__MODAL__", _JS_DESC_MODAL))
    if lp.get("err"):
        return {"status": "error", "stage": "replace-link", **lp}
    # 2. 点「更换图片」，展开描述专属菜单
    await _cdp_click_xy(session, lp["x"], lp["y"])
    await asyncio.sleep(1.8)

    # 3+4. 点「空间上传」并在弹窗里选图确定（同一 evaluate，菜单会自动收起）
    picked = await session.eval_json(
        _JS_DESC_PICK_SPACE
        .replace("__ITEMS__", J(list(DESC_MENU_ITEMS)))
        .replace("__FID__", J(fid)))
    if picked.get("err"):
        return {"status": "error", "stage": picked.get("stage") or "pick",
                "detail": picked, "upload": up}

    after = await session.eval_json(_JS_DESC_STATE.replace("__MODAL__", _JS_DESC_MODAL))
    srcs = after.get("srcs") or []
    landed = pos <= len(srcs) and fid in srcs[pos - 1]
    if not landed:
        logger.error(f"描述模块 {pos} 替换后回读不含新 fileId；"
                     f"该位置现为 {srcs[pos-1][-40:] if pos <= len(srcs) else '不存在'}")
    return {"status": "ok" if landed else "error",
            "stage": "" if landed else "readback",
            "pos": pos, "upload": up,
            "srcBefore": bp.get("srcBefore"),
            "srcAfter": srcs[pos - 1] if pos <= len(srcs) else None,
            "countAfter": after.get("count"),
            "note": "" if landed else "位置对不上，可能替换到了别的模块",
            "hint": "改动尚未生效，需再调 desc_save 保存" if landed else ""}
