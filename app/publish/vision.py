"""发布管线的视觉决策层：图片三件套（⑥素材图/⑦SKC颜色图/⑬描述长图）的「选哪张/删哪张」。

为什么单独一层而不是塞进 pipeline.py：pipeline 保持纯执行原语（页面操作），
「哪张图干净、哪张该删」是判断。判断与执行分开后两边都能单测——本层不碰浏览器，
mock 掉 ask_json_with_images 就能离线跑（见 tests/test_publish_service.py）。

判断依据优先用阶段①已回填的 complianceNotes（extract.enrich_vision 的产物，便宜且
已含逐张中文/水印/logo 标注，SKILL.md 的约定就是阶段⑥⑦⑪ 直接读这个字段）；
字段为空时才发起新的视觉请求兜底。

拿不准一律 uncertain=True 交 service 层发 manual_check 事件，不硬猜——选错图会上真店。
LLM 调用失败按主流程语义抛异常（同 app/publish/llm.py 开头说明），由 service 记 fail。
"""
import os
import re
from typing import Optional

from app.logger import logger
from app.publish.llm import ask_json, ask_json_with_images

_IMG_RE = re.compile(r"^main-\d+\.(jpg|jpeg|png|webp)$", re.I)

# 清理前置里直接跳过的图类：这些不是「修一修能用」的商品图，而是阶段⑬ 该删掉的图
_SKIP_KINDS = ("尺码表", "工厂图", "中文海报")

_SYS = (
    "你是跨境电商选品与合规审核助手，在为 Temu 半托管发布挑选/审核商品图片。"
    "Temu 的硬规则：不接受任何中文文字、水印、他人品牌 logo。"
    "只描述你在图里真正看到的东西，认不准就标 uncertain，绝对不要推测或编造。"
    "只输出 JSON，不要加 ``` 围栏、不要任何解释文字。"
)


def _main_files(workdir: str) -> list:
    """workdir 下的 main-NN 图（绝对路径，按编号排序）。"""
    if not workdir or not os.path.isdir(workdir):
        return []
    return [os.path.join(workdir, f)
            for f in sorted(os.listdir(workdir)) if _IMG_RE.match(f)]


def _notes_by_file(info: dict) -> dict:
    """complianceNotes.files → {文件名: 标注}。没跑过视觉回填时返回 {}。"""
    notes = (info or {}).get("complianceNotes") or {}
    files = notes.get("files")
    if not isinstance(files, list):
        return {}
    return {e["file"]: e for e in files
            if isinstance(e, dict) and e.get("file")}


def _listing(paths: list) -> str:
    return "\n".join(f"第{i} 张：{os.path.basename(p)}" for i, p in enumerate(paths, 1))


def _dirty_score(n: dict) -> tuple:
    """脏图排序键（越小越优）：中文 > 水印 > logo > 重复。

    只在「一张干净图都没有」时用。原先兜底只排除 duplicate、其余按编号取第一张，
    2026-08-22 实测踩坑：某商品 6 张全脏，first 恰好是带中文店名水印的 main-01，
    等于在一堆脏图里挑了最脏的当轮播首图（中文是 Temu 最硬的红线，水印次之）。
    """
    return (bool(n.get("chinese")), bool(n.get("watermark")),
            bool(n.get("logo")), bool(n.get("duplicate")))


def plan_clean(info: dict, workdir: str) -> dict:
    """阶段⑥前置：按 complianceNotes 挑出「值得送 AI 清理」的脏图并配好提示词。

    返回 {"status": "ok", "items": [{"file", "path", "prompt", "note"}], "reason"}。
    本函数【不调 LLM 也不调图像 API】，纯读标注做筛选，故可离线单测。
    实际清理（images.edit_image + check_cleaned 质检）由 service 层并发执行。

    筛选口径就是省钱口径——gpt-image-2 每张都是一次生图调用：
      - 已有 clean=true 的图 → 整个阶段跳过，一次调用都不发；
      - duplicate=true 的跳过（阶段① md5 去重已标出，清了也是白清同一张画面）；
      - kind 为尺码表/工厂图的跳过：那是阶段⑬ 该删的图，不是该修的图。
    提示词按标注定制（edit_image docstring 的建议）：有中文才提「翻译成英文」，
    只有水印/logo 时不提中文，免得模型去改本来没问题的地方。
    """
    notes = _notes_by_file(info)
    if not notes:
        return {"status": "ok", "items": [], "reason": "无 complianceNotes 标注，跳过清理"}
    mains = _main_files(workdir)
    if not mains:
        return {"status": "ok", "items": [], "reason": f"{workdir} 下没有 main-NN 图"}
    if any((notes.get(os.path.basename(p)) or {}).get("clean") for p in mains):
        return {"status": "ok", "items": [], "reason": "已有干净图，无需清理"}

    items = []
    for p in mains:
        name = os.path.basename(p)
        n = notes.get(name) or {}
        if n.get("clean") or n.get("duplicate"):
            continue
        if (n.get("kind") or "") in _SKIP_KINDS:
            continue
        parts = ["移除图片中所有水印、店铺名、拍摄者账号文字和他人品牌 logo"
                 "（含商品吊牌/标牌上的品牌字样）"]
        if n.get("chinese"):
            # 商品图上的中文若是有效信息（如尺码标注）直接删会丢信息，故先英化再删装饰性中文
            parts.append("图中若有中文文字，翻译成简洁英文并原位替换，字体风格和排版尽量保持一致；"
                         "属于店铺宣传/装饰性质的中文直接移除")
        parts.append("商品主体、配色、图案和构图完全不变，被移除处按周围内容自然补全")
        items.append({"file": name, "path": p, "prompt": "，".join(parts) + "。",
                      "note": (n.get("note") or "")[:30]})
    if not items:
        return {"status": "ok", "items": [],
                "reason": "脏图全是重复图/尺码表/工厂图，无可清理项"}
    return {"status": "ok", "items": items, "reason": ""}


async def pick_material(info: dict, workdir: str) -> dict:
    """阶段⑥：从 main 图里挑一张做素材图（店小秘素材图 = 轮播第一张）。

    返回 {"status": "ok", "image": 绝对路径, "reason", "uncertain", "source"}
    或 {"status": "error", "reason"}（一张 main 图都没有）。
    合规化（裁方/放大）不在本层——service 拿到选择后调 images.square_image。
    """
    mains = _main_files(workdir)
    if not mains:
        return {"status": "error", "reason": f"{workdir} 下没有 main-NN 图"}

    by_name = {os.path.basename(p): p for p in mains}
    notes = _notes_by_file(info)
    if notes:
        for p in mains:  # 按编号序取第一张干净图
            n = notes.get(os.path.basename(p))
            if n and n.get("clean"):
                return {"status": "ok", "image": p, "source": "notes", "uncertain": False,
                        "reason": f"complianceNotes 标注干净（{(n.get('note') or '无中文/水印/logo')[:20]}）"}
        # 没有干净图：按脏度打分取最不脏的一张兜底，标 uncertain 交人工
        # （不能只排除 duplicate 就取首张，见 _dirty_score 注释里的实测踩坑）
        cand = min(mains, key=lambda p: _dirty_score(notes.get(os.path.basename(p)) or {}))
        n = notes.get(os.path.basename(cand)) or {}
        flags = "、".join(k for k, v in (("含中文", n.get("chinese")),
                                        ("有水印", n.get("watermark")),
                                        ("有logo", n.get("logo"))) if v)
        return {"status": "ok", "image": cand, "source": "notes-fallback", "uncertain": True,
                "reason": f"无干净图，取最不脏的一张兜底（{flags or '标注不全'}），需人工确认"}

    # complianceNotes 为空（没跑视觉回填）→ 现场看图挑
    prompt = f"""商品标题：{info.get('title') or '（无）'}

下面按顺序给你 {len(mains)} 张候选主图，编号与文件名对应：
{_listing(mains)}

请挑出最适合做 Temu 产品素材图的一张：
- 无任何中文文字、水印、他人品牌 logo；
- 画面就是商品本身（白底/干净背景优先），主体完整；
- 模特实拍图可以，但不能带中文海报文案。

只输出 JSON：{{"image": "<文件名>", "reason": "<20字内>", "uncertain": true/false}}
一张都不合格时也选相对最好的一张，并把 uncertain 置 true。"""
    data = await ask_json_with_images(prompt, mains, what="阶段⑥素材图选图", system=_SYS,
                                      stage="material")
    picked = by_name.get(data.get("image") or "")
    uncertain = bool(data.get("uncertain"))
    if not picked:
        picked, uncertain = mains[0], True
        logger.warning(f"素材图选图：LLM 返回了不存在的文件名 {data.get('image')!r}，兜底取第一张")
    return {"status": "ok", "image": picked, "source": "vision",
            "reason": (data.get("reason") or "")[:80], "uncertain": uncertain}


async def plan_skc(info: dict, workdir: str) -> dict:
    """阶段⑦：按颜色分组选 SKC 图。

    返回 {"status": "ok", "rows": [{"keyword": 颜色原名, "images": [绝对路径…],
    "uncertain": bool}], "uncertain_rows": [颜色…]}。
    每个颜色的 images 第一位是该颜色主图（service 复制时命名 main-01.jpg 落首位、免拖拽，
    与 skc_replace_row 的「按文件名排序挂图」约定对齐）。
    颜色只有一个时【不发视觉请求】：所有主图都属于这唯一颜色，没有「哪张归哪色」
    可判。2026-08-22 实测踩坑：单色商品照样发请求，模型按提示词里「含水印/logo 的
    图不要选」把 6 张全否掉、返回空 images，于是整个阶段被跳过、还多一次人工确认——
    而水印问题本该由前置清理阶段统一解决，不该在这里二次否决。
    """
    colors = [c for c in (info.get("colors") or []) if c]
    mains = _main_files(workdir)
    if not colors or not mains:
        return {"status": "ok", "rows": [], "uncertain_rows": [],
                "reason": "无颜色分组或无 main 图"}

    notes = _notes_by_file(info)

    def _usable(paths: list) -> list:
        """排除重复图与尺码表/工厂图；没有标注时原样返回。

        【分两级兜底，不再一次性放回全量】原实现过滤后为空就 `return out or paths`，
        把重复图和尺码表图一起放回来，还是静默的——2026-08-24 追查 SKC 行出现两张
        重复图时才发现这个口子（那次的直接成因是近重复没被 md5 抓到，但这里同样能
        把已标 duplicate 的图放回去）。
        现在改成：先只放回重复图（一张画面重复好过一张图都没有），仍为空才放回
        全量（尺码表图挂上去总比整行留 1688 原始破线图强），两级都打日志说清放回了什么。
        """
        if not notes:
            return paths

        def _note(p: str) -> dict:
            return notes.get(os.path.basename(p)) or {}

        out = [p for p in paths
               if not _note(p).get("duplicate")
               and (_note(p).get("kind") or "") not in _SKIP_KINDS]
        if out:
            return out
        # 第一级：放回重复图，仍然排掉尺码表/工厂图这类非商品图
        relaxed = [p for p in paths if (_note(p).get("kind") or "") not in _SKIP_KINDS]
        if relaxed:
            logger.warning(f"可用图过滤后为空，放回 {len(relaxed)} 张重复图"
                           f"（仍排除 {_SKIP_KINDS}）：{[os.path.basename(p) for p in relaxed]}")
            return relaxed
        # 第二级：连非商品图都得用上，此时必须让人知道
        logger.warning(f"可用图过滤后为空且全是 {_SKIP_KINDS} 类图，只能放回全部 "
                       f"{len(paths)} 张，请人工复核：{[os.path.basename(p) for p in paths]}")
        return paths

    if len(colors) == 1:
        imgs = _usable(mains)
        # 干净图优先排前面，其余按脏度排——首位会成为该颜色主图
        if notes:
            imgs = sorted(imgs, key=lambda p: (
                not (notes.get(os.path.basename(p)) or {}).get("clean"),
                _dirty_score(notes.get(os.path.basename(p)) or {})))
        return {"status": "ok", "uncertain_rows": [],
                "rows": [{"keyword": colors[0], "images": imgs, "uncertain": False}],
                "reason": f"单颜色，{len(imgs)} 张主图全部归属该色（免视觉请求）"}

    prompt = f"""商品标题：{info.get('title') or '（无）'}
源商品颜色（SKU 里的颜色名）：{colors}

下面按顺序给你 {len(mains)} 张主图，编号与文件名对应：
{_listing(mains)}

请把每张图分配给对应的颜色，为每个颜色挑出该颜色的展示图集合：
- 只选「画面是该颜色商品本身」的图（模特实拍/平铺/细节均可）；
- 【只按颜色归属判断，不要因为图上有水印或文字而弃选】水印已在上一步统一处理过；
  但纯尺码表图、工厂/公司介绍图这类非商品图不要选；
- 每个颜色把最能代表该颜色整体外观的图排在第一位（它会作为该颜色的主图）；
- 认不准某张图属于哪个颜色就不要分；某颜色一张合适的图都没有就留空数组。

只输出 JSON：{{"rows": [{{"color": "<颜色名，必须用上面给出的原名>",
"images": ["<文件名>", ...], "uncertain": true/false}}]}}
rows 必须覆盖上面每一个颜色（没图的给 "images": []），uncertain 表示你对该颜色的归属判断没把握。"""
    data = await ask_json_with_images(prompt, mains, what="阶段⑦SKC分色选图", system=_SYS,
                                      stage="skc")

    by_name = {os.path.basename(p): p for p in mains}
    usable = set(_usable(mains))
    rows, uncertain_rows = [], []
    for entry in data.get("rows") or []:
        if not isinstance(entry, dict):
            continue
        color = entry.get("color")
        if color not in colors:
            continue
        imgs = [by_name[f] for f in (entry.get("images") or [])
                if f in by_name and by_name[f] in usable]
        if not imgs:
            uncertain_rows.append(color)
            continue
        uncertain = bool(entry.get("uncertain"))
        if uncertain:
            uncertain_rows.append(color)
        rows.append({"keyword": color, "images": imgs, "uncertain": uncertain})
    planned = {r["keyword"] for r in rows}
    for c in colors:  # LLM 漏掉的颜色不能静默丢
        if c not in planned and c not in uncertain_rows:
            uncertain_rows.append(c)
    return {"status": "ok", "rows": rows, "uncertain_rows": uncertain_rows}


async def plan_desc_text(texts: list, info: Optional[dict] = None) -> dict:
    """阶段⑬：判断描述区每个【文字模块】该英化保留还是删除。

    texts: [{"idx": "0", "text": "...", "len": int}]（pipeline.desc_text_map 的产物）
    返回 {"status": "ok", "plan": [{"idx", "action", "text", "reason"}]}，
    action ∈ translate / delete / keep；translate 时 text 是英文正文。

    两类真实样本（2026-08-24）：
    - 该删：1688 关联商品 JSON 残留
      `{"styleType":"offer-type-1","items":"888688384773,…","usemap":"_sdmap_0"}`
      ——这是采集时带过来的结构化垃圾，买家看到就是一串乱码
    - 该译：尺码对照 `80【身高65-75cm】…`——对买家有实际价值，英化保留

    纯文本判断不需要视觉，走 ask_json（比传图便宜且快）。
    LLM 漏判或异常时该模块按 keep 处理——保守方向，不删不该删的
    （与 plan_desc 对漏判的处理一致）。
    """
    items = [t for t in (texts or []) if (t.get("text") or "").strip()]
    if not items:
        return {"status": "ok", "plan": [], "reason": "描述区无文字模块"}

    listing = "\n\n".join(
        f"[模块 idx={t['idx']}]\n{(t.get('text') or '')[:600]}" for t in items)
    prompt = f"""商品标题：{(info or {}).get('title') or '（无）'}

下面是该商品 Temu 描述区的 {len(items)} 个「文字模块」原文（从 1688 采集带过来的）：

{listing}

逐个决定怎么处理：

- "delete"：无意义内容——采集残留的 JSON/代码片段（如
  {{"styleType":"offer-type-1","items":"8886...","usemap":"_sdmap_0"}}）、
  采集失败留下的占位符（整段只有 null / undefined / NaN / 空白反复出现）、
  乱码、店铺广告/关注引导、工厂或公司介绍、与商品无关的说明、纯符号。
- "translate"：对海外买家有价值的信息——尺码/身高对照、洗涤保养说明、
  材质说明、搭配建议等。把它翻译成**自然的英文**，保留原有分行与数字，
  单位保持 cm。译文不得超过 500 字符（平台上限），超了就精简掉次要信息。
- "keep"：原文已经是纯英文且无需改动。

翻译要求：不要逐字硬译，用海外买家习惯的表达；不要加价格、折扣、
运费承诺等原文没有的内容；不要出现年份。

只输出 JSON：{{"plan": [{{"idx": "<原样照抄模块 idx>",
"action": "translate|delete|keep", "text": "<translate 时填英文正文，其余留空>",
"reason": "<10字内理由>"}}]}}
plan 必须覆盖上面每一个模块。"""

    data = await ask_json(prompt, what="阶段⑬文字模块规划", stage="desc")

    valid = {str(t["idx"]): t for t in items}
    plan, seen = [], set()
    for p in data.get("plan") or []:
        if not isinstance(p, dict):
            continue
        idx = str(p.get("idx"))
        if idx not in valid or idx in seen:
            continue
        act = p.get("action")
        if act not in ("translate", "delete", "keep"):
            continue
        text = (p.get("text") or "").strip()
        if act == "translate" and not text:
            # 说要译却没给正文，按 keep 处理而不是留个空模块
            act, text = "keep", ""
        seen.add(idx)
        plan.append({"idx": idx, "action": act, "text": text,
                     "reason": (p.get("reason") or "")[:40]})

    for idx in valid:                       # LLM 漏判的一律保留（保守）
        if idx not in seen:
            plan.append({"idx": idx, "action": "keep", "text": "",
                         "reason": "LLM 未判，保守保留"})
    # 【一个都没判中必须告警】保守保留本身没错，但「全员 keep」与「模型认为都该留」
    # 长得一模一样，静默下去就查不出提示词/解析出了问题——2026-08-25 实测：本函数
    # 拼好的 listing 忘了拼进 prompt，模型收到「下面是 1 个模块」却看不到原文，回了
    # {"plan": [], "error": "未收到需要处理的模块原文"}，于是描述区的 `null null null`
    # 一路被判 keep 发到页面上，日志里一行异常都没有。
    if not seen:
        logger.warning(
            f"阶段⑬ 文字模块规划一项都没判中（{len(valid)} 个模块全按 keep 保留），"
            f"模型原始返回：{str(data)[:200]}")
    return {"status": "ok", "plan": plan}


async def plan_desc(modules: list, info: Optional[dict] = None) -> dict:
    """阶段⑬：对 desc_map 列出的描述模块逐个判「删/留/英化替换」。

    modules: [{"pos": int, "url": str, "onDxmHost": bool}]（pipeline.desc_map 的产物，
    序号从 1 起）。返回 {"status": "ok", "delete": [pos…],
    "replace": [{"pos", "url", "reason"}], "keep": [pos…]}。
    英化本身（images.edit_image）由 service 执行，本层只出计划。

    规则（SKILL.md 阶段⑪）：工厂/公司/尺码表/与商品无关的图删、重复图删、
    含中文或水印/他人 logo 的商品图标记待清理（英化+去水印）。Temu 只关心商品图。

    【尺寸是与内容正交的第二条判据，不交给模型判】服装类下限 1340×1785 是平台硬
    校验，而模型看图判的是「脏不脏」——一张干净的 900×1200 商品图内容上该 keep，
    却过不了保存校验（2026-08-23 真站取证：描述区 10 张 1688 外链全是 1000×1000
    与 900×1200，save 被静默弹回）。故凡 desc_map 标了 tooSmall 的，判 keep 后一律
    改判 replace 并带 needsUpscale 标记——这类图只缺像素、内容是干净的，交给
    service 走纯几何放大即可，不必烧一次生图。已判 delete 的不动（要删的不必管尺寸）。
    """
    mods = [m for m in (modules or []) if m.get("url")]
    if not mods:
        return {"status": "ok", "delete": [], "replace": [], "keep": [],
                "reason": "描述区无模块"}

    listing = "\n".join(f"第{m['pos']} 张（pos={m['pos']}）" for m in mods)
    prompt = f"""商品标题：{(info or {}).get('title') or '（无）'}

下面是该商品详情描述区的 {len(mods)} 张图，按页面展示顺序，序号就是 pos：
{listing}

Temu 半托管发布只关心商品图，请逐张决定动作：
- "delete"：工厂/公司介绍、尺码表、与商品无关的图、与前面重复出现的图——直接删；
- "replace"：图本身是商品展示图，但含中文文字、水印、店铺名或他人品牌 logo
  ——保留画面，把中文英化并移除水印后替换；
- "keep"：干净的商品图（无中文/水印/他人 logo）——保留。

只输出 JSON：{{"actions": [{{"pos": 1, "action": "keep|delete|replace",
"reason": "<10字内>"}}]}}
actions 必须覆盖每一张（pos 从 1 到 {len(mods)}）。"""
    data = await ask_json_with_images(
        prompt, [m["url"] for m in mods], what="阶段⑬描述图规划", system=_SYS,
        stage="desc")

    valid_pos = {m["pos"]: m for m in mods}
    delete, replace, keep = [], [], []
    for a in data.get("actions") or []:
        if not isinstance(a, dict):
            continue
        pos, act = a.get("pos"), a.get("action")
        if pos not in valid_pos:
            continue
        if act == "delete":
            delete.append(pos)
        elif act == "replace":
            replace.append({"pos": pos, "url": valid_pos[pos]["url"],
                            "reason": (a.get("reason") or "")[:40]})
        else:
            keep.append(pos)
    missed = [p for p in valid_pos if p not in delete
              and p not in keep and all(r["pos"] != p for r in replace)]
    keep.extend(missed)  # LLM 漏判的一律按保留处理——保守方向，不删不该删的

    # 尺寸兜底：keep 里但尺寸不达标的，改判 replace + needsUpscale（理由见 docstring）。
    # 已在 replace 里的不用管：它本来就要重新出图，出图收尾的 compress 会把尺寸拉够。
    kept_small = [p for p in keep if valid_pos[p].get("tooSmall")]
    if kept_small:
        keep = [p for p in keep if p not in set(kept_small)]
        for p in kept_small:
            replace.append({"pos": p, "url": valid_pos[p]["url"],
                            "needsUpscale": True,
                            "reason": f"尺寸 {valid_pos[p].get('size')} 低于 1340x1785"})
        replace.sort(key=lambda r: r["pos"])
    return {"status": "ok", "delete": sorted(set(delete)),
            "replace": replace, "keep": sorted(set(keep))}


async def check_cleaned(image_path: str) -> dict:
    """AI 英化后的质检：残留中文/拼音/乱码或破坏主体都算不过。

    返回 {"status": "ok", "clean": bool, "issues": str}。
    实测生图会残留拼音、误译品类（pipeline.desc_replace 注释），只看「中文没了」
    会把带乱码文案的图挂上去，故替换前必须过这道。
    """
    prompt = """这张图片刚经过 AI 英化处理（把中文文案改成英文）。请质检：
- 是否还残留任何中文字符、拼音、或乱码/无意义文字；
- 是否有明显修图痕迹破坏商品主体。

只输出 JSON：{"clean": true/false, "issues": "<20字内，没有问题留空>"}"""
    data = await ask_json_with_images(prompt, [image_path], what="英化质检", system=_SYS,
                                      stage="clean_images")
    return {"status": "ok", "clean": bool(data.get("clean")),
            "issues": (data.get("issues") or "")[:80]}
