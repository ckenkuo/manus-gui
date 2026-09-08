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
import asyncio
import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.logger import logger
from app.publish import images
from app.publish.llm import (
    NonEmptyStr,
    ask_json,
    ask_json_with_images,
    image_ref,
)

_IMG_RE = re.compile(r"^main-\d+\.(jpg|jpeg|png|webp)$", re.I)

# 清理前置里直接跳过的图类：这些不是「修一修能用」的商品图，而是阶段⑬ 该删掉的图
_SKIP_KINDS = ("尺码表", "工厂图", "中文海报")

# 【合规图的目标张数 = ⑦ SKC 每行下限】⑤b 清理与 ⑦ 选图共用这个下限：清理清到够
# 这么多张就停（省生图调用），⑦ 选图也按它决定「要不要放回不合规图凑数」。
# 与 pipeline.SKC_ROW_MIN_IMAGES 同值，刻意不 import 那边——vision 是纯判断层，
# 不该为一个常量吃进整个 pipeline 模块（它会拖进浏览器那一串依赖）。
# 调用方（service）传的就是 SKC_ROW_MIN_IMAGES，这里的默认值只是离线单测的兜底。
MIN_CLEAN_IMAGES = 3

# 颜色缩略图与主图「比画面」时的距离上限（images.color_distance 的平均通道差，0~255）。
# 2026-09-03 用真实产物 product-969144784315（11 色 16 主图）标定：缩略图与它自己那张
# 主图的距离 1.7~3.1，与最近的另一张主图 9.1~41。取 6 落在这道缝里，两边都留余量。
# 【宁可配不上也不硬配】超过就判没配上、交 LLM——错配的代价是把 A 色的图挂到 B 色行下，
# 比多烧一次视觉调用贵得多。
COLOR_THUMB_MAX_DISTANCE = 6

_SYS = (
    "你是跨境电商选品与合规审核助手，在为 Temu 半托管发布挑选/审核商品图片。"
    "Temu 的硬规则：不接受任何中文文字、水印、他人品牌 logo。"
    "只描述你在图里真正看到的东西，认不准就标 uncertain，绝对不要推测或编造。"
    "只输出 JSON，不要加 ``` 围栏、不要任何解释文字。"
)


# ---- 三个看图判断点的输出契约 ----------------------------------------------------
# 校验与重试机制见 llm._check_result；一律 strict=True，理由（lax 会把 "1" 悄悄
# 当成 1 放行、而下游拿到的仍是那个字符串）也在那里。
# 【只声明「答错了会静默出错」的字段】uncertain 缺失只是少一次人工复核提示、
# reason 只进日志，都不值当为它们把整个阶段判失败——声明多了是在给自己找重试。


class MaterialPick(BaseModel):
    """阶段⑥ 素材图选图。

    image 拿不到就走 pick_material 里「兜底取第一张」那条，而那一张未必干净——
    素材图是直接发上去的轮播首图，宁可先重问几次。
    """

    model_config = ConfigDict(strict=True)

    image: NonEmptyStr


class SkcRow(BaseModel):
    """阶段⑦ 的一行：一个颜色配一组图。

    images 允许是空数组（提示词明说「一张合适的都没有就留空」），故只校验类型
    不校验非空——plan_skc 本就会把空 images 的颜色记进 uncertain_rows 交人工。
    """

    model_config = ConfigDict(strict=True)

    color: NonEmptyStr
    images: list


class SkcPlan(BaseModel):
    """阶段⑦ SKC 分色选图。

    原先非 dict 的条目在 plan_skc 里是 continue 静默丢掉——模型答错一行就少配
    一个颜色的图，产物里看不出来，故改成先重问。
    """

    model_config = ConfigDict(strict=True)

    rows: list[SkcRow]


class DescAction(BaseModel):
    """阶段⑬ 对一张描述图的处置。

    【pos 必须是真整数】模型偶尔回字符串 "1"，plan_desc 的 `pos not in valid_pos`
    于是不成立，整条动作被丢掉：那张图既不删也不换，1688 原图就这么留在描述区。

    【action 用 Literal 而不是 str】plan_desc 是 if delete / elif replace / else
    的写法，任何没见过的词都会落进 else 当成 keep。模型要是回个 "remove"，
    表现就是「该删的图安安静静留下了」并发上真店。这里宁可判错重问：
    提示词里白纸黑字给的就是这三个小写词，照抄的成本比什么都低。
    """

    model_config = ConfigDict(strict=True)

    pos: int
    action: Literal["keep", "delete", "replace", "sizechart"]


class DescPlan(BaseModel):
    """阶段⑬ 描述图规划。"""

    model_config = ConfigDict(strict=True)

    actions: list[DescAction]


class DescAuditItem(BaseModel):
    """阶段⑬ keep 复核里一张脏图的结论。

    【pos 必须是真整数】同 DescAction 的坑：收到字符串 "1" 时复核结论对不上
    真实 pos，含中文的图就继续漏在网上。
    """

    model_config = ConfigDict(strict=True)

    pos: int
    sizeTable: bool = False
    what: str = ""


class DescAudit(BaseModel):
    """阶段⑬ keep 复核（专查中文，只列脏的）。"""

    model_config = ConfigDict(strict=True)

    dirty: list[DescAuditItem]


# 中文复核每批传图数：复核是单任务判断、不需要跨图对比，小批量比一次几十张可靠
# （初判 50 张一次过漏掉中文卡片的实测见 plan_desc 里的复核段注释）。
_DESC_AUDIT_CHUNK = 8


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


def is_dirty(n: dict) -> bool:
    """这条标注是否仍带中文/水印/他人 logo（⑤b 清理成功的会被回写成三项全 False）。

    【标注不全时算合规】三项都没标、也没有 clean 时返回 False：不能因为模型漏标就
    把图丢掉，那会静默排除一张本来能用的图——与 images.ahash「算不出就别当重复」
    同取向。判据放模块级是因为 service._pad_row_images 补图时要用同一口径，
    两处口径不一致就会出现「选图排除了、补图又补回来」。
    """
    return bool(n.get("chinese") or n.get("watermark") or n.get("logo"))


def _dirty_score(n: dict) -> tuple:
    """脏图排序键（越小越优）：中文 > 水印 > logo > 重复。

    只在「一张干净图都没有」时用。原先兜底只排除 duplicate、其余按编号取第一张，
    2026-08-22 实测踩坑：某商品 6 张全脏，first 恰好是带中文店名水印的 main-01，
    等于在一堆脏图里挑了最脏的当轮播首图（中文是 Temu 最硬的红线，水印次之）。
    """
    return (bool(n.get("chinese")), bool(n.get("watermark")),
            bool(n.get("logo")), bool(n.get("duplicate")))


def _norm_color(s: str) -> str:
    """颜色名归一，供「DOM 抓到的名字」与「SKU 里的名字」比对。

    两边同源（都是商家在 1688 后台填的那套颜色名），但取数路径不同：SKU 侧来自
    skuMapOriginal 的 specAttrs，页面侧来自颜色按钮的 title/alt/文本，于是常差
    一个空格或分隔符（「军绿色 / L」的裁切残留、「MJ-20」对「MJ20」）。只归一
    空白与分隔符 + 转小写，【不做同义词映射】——猜「墨绿≈深绿」会把两个真实存在
    的不同颜色配成一个，代价比配不上大。
    """
    return re.sub(r"[\s\-_/·、,，]+", "", str(s or "")).lower()


def _thumb_path(entry, workdir: str) -> str:
    """colorImages 的一项 → 本地缩略图绝对路径；拿不到本地文件时返回空串。

    【三种形状都要认】阶段① 落盘的新形状是 {"url":…, "mainFile"|"file": …}：URL 能
    直接对上某张主图时给 mainFile（这时不下载缩略图），对不上才下载并给 file。
    老形状是裸 URL 字符串——那批产物落盘时还没下载缩略图，本地无图可比，返回空串
    让该颜色配不上、退回 LLM。续跑老商品会走到这一支。
    """
    if isinstance(entry, dict):
        f = (entry.get("file") or "").strip()
        if f:
            fp = os.path.join(workdir, f)
            return fp if os.path.isfile(fp) else ""
    return ""


def _match_color_thumbs(color_images: dict, colors: list, cands: list,
                        workdir: str) -> tuple:
    """把颜色缩略图配到候选主图，返回（{颜色原名: 主图路径}, 诊断串）。

    两级判据，【先精确后模糊】：
      一、URL 归一（阶段① 已算好、落在 mainFile 里）：1688 的颜色缩略图多数就是某张
          轮播主图的缩放版，剥掉 CDN 尺寸后缀两边精确相等。没有阈值、没有歧义。
      二、颜色指纹（images.color_signature）：商家单独上传色卡图时 URL 对不上，才用
          画面比。用 RGB 而不是 ahash——ahash 转灰度，而分色要区分的恰恰是颜色
          （实测同款红/蓝两版 ahash 距离为 0，见 images.color_signature 的说明）。
          阈值 COLOR_THUMB_MAX_DISTANCE 卡得严，配不上宁可交 LLM 也不硬配。

    【为什么不按顺序对应】main-NN 的编号来自轮播 offerImgList，colors 的顺序来自
    skuMapOriginal 的 specAttrs 首现序，两个独立来源没有任何东西保证一致；候选池
    一过滤（重复图/尺码表/不合规图）下标还会整体错位。按顺序是在赌运气。

    【配到的图必须在候选池里】mainFile 指到的主图如果被合规过滤掉了（该颜色的图本身
    带中文/水印），视为配不上——按「不合规图优先不用」的口径，这一色交给补图兜底，
    而不是把脏图当它的首图挂上去。

    【全局贪心而不是逐色取最近】画面比对那一级按距离升序扫所有（颜色×主图）组合，
    颜色与主图都未被占用才确认。逐色独立取最近会让两个颜色抢到同一张主图——那样两行
    首图相同，SKC 的颜色区分就没了。
    """
    by_norm = {}
    for c in colors:
        by_norm.setdefault(_norm_color(c), c)
    in_pool = {os.path.basename(p): p for p in cands}

    matched, used, todo, skipped = {}, set(), {}, []
    for dom_name, entry in (color_images or {}).items():
        real = by_norm.get(_norm_color(dom_name))
        if not real or real in matched or real in todo:
            skipped.append(f"{dom_name}(名字对不上SKU颜色)")
            continue
        # 一级：URL 已在阶段① 对上主图
        mf = (entry or {}).get("mainFile") if isinstance(entry, dict) else None
        if mf:
            mp = in_pool.get(mf)
            if mp and mp not in used:
                matched[real] = mp
                used.add(mp)
                continue
            skipped.append(f"{dom_name}(URL对上{mf}但它不在候选池，多半被合规过滤了)")
            continue
        # 二级：留待画面比对
        fp = _thumb_path(entry, workdir)
        if not fp:
            skipped.append(f"{dom_name}(本地无缩略图)")
            continue
        sig = images.color_signature(fp)
        if sig is None:
            skipped.append(f"{dom_name}(缩略图读不出)")
            continue
        todo[real] = sig

    by_url = len(matched)
    if todo:
        main_sig = {}
        for mp in cands:
            if mp in used:
                continue
            sig = images.color_signature(mp)
            if sig is not None:
                main_sig[mp] = sig
        pairs = sorted(
            (images.color_distance(ts, ms), color, mp)
            for color, ts in todo.items() for mp, ms in main_sig.items()
        )
        for dist, color, mp in pairs:
            if dist > COLOR_THUMB_MAX_DISTANCE:
                break                   # 已按距离升序，后面只会更远
            if color in matched or mp in used:
                continue
            matched[color] = mp
            used.add(mp)
        for c in todo:
            if c not in matched:
                skipped.append(f"{c}(画面比对超出距离上限 {COLOR_THUMB_MAX_DISTANCE})")

    diag = (f"{len(matched)}/{len(colors)} 色配上主图"
            f"（URL 精确 {by_url} 个，画面比对 {len(matched) - by_url} 个）")
    if skipped:
        diag += f"；未配上：{skipped}"
    return matched, diag


def plan_clean(info: dict, workdir: str, min_clean: int = MIN_CLEAN_IMAGES) -> dict:
    """阶段⑥⑦前置：按 complianceNotes 挑出「值得送 AI 清理」的脏图并配好提示词。

    返回 {"status": "ok", "items": [{"file", "path", "prompt", "note"}], "reason"}。
    本函数【不调 LLM 也不调图像 API】，纯读标注做筛选，故可离线单测。
    实际清理（images.edit_image + check_cleaned 质检）由 service 层并发执行。

    筛选口径就是省钱口径——gpt-image-2 每张都是一次生图调用：
      - 干净图已够 min_clean 张 → 整个阶段跳过，一次调用都不发；
      - 不够时【只清缺口那几张】，凑到 min_clean 就停，剩下的脏图不清；
      - duplicate=true 的跳过（阶段① md5 去重已标出，清了也是白清同一张画面）；
      - kind 为尺码表/工厂图的跳过：那是阶段⑬ 该删的图，不是该修的图。

    【为什么闸门从「有任意一张干净图」改成「够 min_clean 张」】原口径是照阶段⑥
    的需求写的——素材图只取一张，有一张干净的就够。但本步产物是⑥⑦ 共用（见
    service._clean_main_images），而⑦ SKC 每个颜色行要挂 3~10 张、下限 3 张。
    于是「6 张里有 1 张干净」的商品整段跳过清理，剩下 5 张带中文/水印/logo 的图
    被⑦ 原样挂到颜色行上，一路发到真店。⑦ 的选图刻意不做合规否决（提示词明写
    「不要因为图上有水印或文字而弃选」），前提就是本步已统一清过——那个前提正是
    被这条早退作废的。故闸门改成按⑦ 的行下限算。

    【清的顺序按脏度从轻到重】只清缺口张数时「先清哪几张」有讲究：只有水印/logo
    的图比带中文的容易修成功（中文要英化重排版，check_cleaned 还会因残留拼音/
    乱码判不过，见 service._retry_hint），故按 _dirty_score 升序取——同样的调用
    次数换更高的成功率。

    提示词按标注定制（edit_image docstring 的建议）：有中文才提「翻译成英文」，
    只有水印/logo 时不提中文，免得模型去改本来没问题的地方。
    """
    notes = _notes_by_file(info)
    if not notes:
        return {"status": "ok", "items": [], "reason": "无 complianceNotes 标注，跳过清理"}
    mains = _main_files(workdir)
    if not mains:
        return {"status": "ok", "items": [], "reason": f"{workdir} 下没有 main-NN 图"}
    want = max(1, int(min_clean or 1))
    clean_n = sum(1 for p in mains
                  if (notes.get(os.path.basename(p)) or {}).get("clean"))
    # 【颜色主图强制清理】颜色缩略图 URL 精确对应的 mainFile 是「该颜色的权威归属」，
    # 带中文/水印也必须清——不清则阶段⑦ 配不上该颜色（2026-09-04 1071736188944：
    # 8 个颜色的 mainFile 全是带中文的图，却因为干净图已够下限被整段跳过，全配不上）。
    # 与「缺口张数」是两套目标：前者是颜色归属必需，后者是凑够行下限的通用图。
    color_main = {v.get("mainFile") for v in (info.get("colorImages") or {}).values()
                  if isinstance(v, dict) and v.get("mainFile")}

    def _color_dirty(p: str) -> bool:
        n = notes.get(os.path.basename(p)) or {}
        return (os.path.basename(p) in color_main
                and not n.get("clean")
                and (n.get("kind") or "") not in _SKIP_KINDS)

    if clean_n >= want and not any(_color_dirty(p) for p in mains):
        return {"status": "ok", "items": [],
                "reason": f"已有 {clean_n} 张干净图（够 {want} 张下限），无需清理"}

    cands = []
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
            # 商品图上的中文若是商品介绍/说明类（材质成分、尺码、工艺、卖点、使用说明等）直接删
            # 会丢信息，故先英化保留再删非商品信息；材质/质量特写图上的说明正是商品要传达的信息，
            # 绝不能当装饰抹掉（2026-09-03 用户要求）。范围用开放措辞、别只列举几个类别——
            # 用户明确「商品介绍只要不违规都翻译」。标点要单独点出来：只说「中文文字」时模型会
            # 留下『』这类中日韩标点，过不了 check_cleaned（理由见 images._NO_CJK_PUNCT）
            parts.append("图中若有中文文字，翻译成简洁英文并原位替换，字体风格和排版尽量保持一致；"
                         "凡是商品介绍、说明类文字（材质成分、工艺、尺寸、功能卖点、使用说明、注意事项等）"
                         "都翻译保留、不要删除；"
                         "仅水印、店铺名、拍摄者账号文字、他人品牌 logo 这类非商品信息直接移除；"
                         "中文标点（『』「」、，。！？等）也必须一并去掉或换成英文标点")
        parts.append("商品主体、配色、图案和构图完全不变，被移除处按周围内容自然补全")
        cands.append((_dirty_score(n),
                      {"file": name, "path": p, "prompt": "，".join(parts) + "。",
                       "note": (n.get("note") or "")[:30]}))
    if not cands:
        return {"status": "ok", "items": [],
                "reason": "脏图全是重复图/尺码表/工厂图，无可清理项"}
    # 脏度轻的排前面（成功率高）；颜色主图的脏图必须清，其余只取缺口张数凑够下限
    cands.sort(key=lambda t: t[0])
    need = want - clean_n
    must = [it for _, it in cands if it["file"] in color_main]
    rest = [it for _, it in cands if it["file"] not in color_main]
    items = must + rest[:max(0, need - len(must))]
    reason = ""
    if must:
        reason = (f"干净图 {clean_n} 张，清 {len(items)}/{len(cands)} 张脏图"
                  f"（含颜色主图 {len(must)} 张），另有 {len(cands) - len(items)} 张留着不清")
    elif len(cands) > need:
        reason = (f"干净图 {clean_n} 张不足 {want} 张，只清缺口 {need} 张"
                  f"（另有 {len(cands) - need} 张脏图留着不清）")
    return {"status": "ok", "items": items, "reason": reason}


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
    # schema 只把 image 列成必答：它拿不到就走下面「兜底取第一张」，而那一张未必
    # 干净（阶段⑥ 挑的是要直接发上去的素材图），宁可先重问几次。
    # reason 只进日志、uncertain 缺失只是少一次人工复核提示，都不值当把整个阶段判失败。
    data = await ask_json_with_images(prompt, mains, what="阶段⑥素材图选图", system=_SYS,
                                      stage="material", result_model=MaterialPick)
    picked = by_name.get(data.get("image") or "")
    uncertain = bool(data.get("uncertain"))
    if not picked:
        picked, uncertain = mains[0], True
        logger.warning(f"素材图选图：LLM 返回了不存在的文件名 {data.get('image')!r}，兜底取第一张")
    return {"status": "ok", "image": picked, "source": "vision",
            "reason": (data.get("reason") or "")[:80], "uncertain": uncertain}


async def plan_skc(info: dict, workdir: str,
                   min_clean: int = MIN_CLEAN_IMAGES) -> dict:
    """阶段⑦：按颜色分组选 SKC 图。

    返回 {"status": "ok", "rows": [{"keyword": 颜色原名, "images": [绝对路径…],
    "uncertain": bool}], "uncertain_rows": [颜色…], "dirtyUsed": [文件名…]}。
    每个颜色的 images 第一位是该颜色主图（service 复制时命名 main-01.jpg 落首位、免拖拽，
    与 skc_replace_row 的「按文件名排序挂图」约定对齐）。
    颜色只有一个时【不发视觉请求】：所有主图都属于这唯一颜色，没有「哪张归哪色」
    可判。2026-08-22 实测踩坑：单色商品照样发请求，模型按提示词里「含水印/logo 的
    图不要选」把 6 张全否掉、返回空 images，于是整个阶段被跳过、还多一次人工确认——
    而水印问题本该由前置清理阶段统一解决，不该在这里二次否决。

    【合规优先，凑不够才放回】选图候选一律走 _usable：带中文/水印/他人 logo 的图
    默认【不用】，只有整行凑不够 min_clean 张时才被迫放回，放回的文件名进返回值的
    dirtyUsed，由 service 报人工确认。原先这里对合规完全不设闸（只用 _dirty_score
    做排序偏好），加上 ⑤b 清理「有一张干净图就整段跳过」的早退，实测会把带中文的
    图挂到颜色行上发出去——两处一起改，见 plan_clean 的说明。

    【2026-09-03 优先路径：用颜色缩略图配首图】info.colorImages 有值时（1688 从页面
    颜色选择器抓到了每个颜色的缩略图并已落盘），不调 LLM 猜——MJ20/MJ21 这类编码或
    非中文词命名时，LLM 凭画面判断不准。配对靠 images.ahash 比【画面】：1688 的颜色
    缩略图通常就是某张轮播主图的缩略版，取汉明距离最近且在 COLOR_THUMB_MAX_DISTANCE
    以内的那张主图当该颜色首图。
    【为什么不按顺序对应】main-NN 的编号来自轮播 offerImgList，colors 的顺序来自
    skuMapOriginal 的 specAttrs 首现序，两个独立来源没有任何东西保证一致；而且
    _usable 一过滤（重复图/尺码表图）下标就整体错位。按顺序对应是在赌运气，赌错了
    还因为 uncertain 恒为 False 而不报人工确认。
    【其余图只管合规、不管顺序】首图定了之后，同一行剩下的位置由 service 的
    _pad_row_images 用合规图补齐——它们是平铺/细节/材质图，挂在哪个颜色行下都说得通。
    一个颜色都没配上时退回原先的 LLM 判断逻辑。
    """
    colors = [c for c in (info.get("colors") or []) if c]
    mains = _main_files(workdir)
    if not colors or not mains:
        return {"status": "ok", "rows": [], "uncertain_rows": [], "dirtyUsed": [],
                "reason": "无颜色分组或无 main 图"}

    notes = _notes_by_file(info)
    # _usable 被迫放回的不合规图文件名：本函数只记录，报不报人工确认由 service 定
    # （与本层「只判断、不产生副作用」的分工一致）
    forced: set = set()

    def _note(p: str) -> dict:
        return notes.get(os.path.basename(p)) or {}

    def _dirty(p: str) -> bool:
        return is_dirty(_note(p))

    def _usable(paths: list, min_keep: int = min_clean) -> list:
        """挑候选：不合规图优先不用，再排除重复图与尺码表/工厂图。

        【三级兜底，但降级判据不同，刻意的】
          合规这一级按【张数】降级：带中文/水印/logo 是 Temu 的硬红线，本该一张都
          不用，但整行凑不够 min_keep 张会让阶段⑫ save 被静默拦下（只有区块变红，
          见 _pad_row_images），两害相权只放回缺口那几张，并记进 forced 交 service
          报人工确认。放回的是「脏但是商品本身」的图，还能靠 ⑤b 清理救回来。
          重复图/非商品图两级仍按【为空】降级（2026-08-24 定的口径，不动）：尺码表图
          挂到颜色行上是确定的坏结果，不能因为「还差一张就到 3 张」就把它放回来——
          那是用一个确定的坏换另一个确定的坏。只有一张可用图都没有时才轮到它们。
        每一级都打 warning 说清放回了什么——静默是这套兜底最坏的性质
        （2026-08-24 追查 SKC 行出现两张重复图，就是被静默的 `return out or paths` 坑的）。
        """
        if not notes:
            return paths

        def _base_ok(p: str) -> bool:
            return (not _note(p).get("duplicate")
                    and (_note(p).get("kind") or "") not in _SKIP_KINDS)

        out = [p for p in paths if _base_ok(p) and not _dirty(p)]
        if len(out) < min_keep:
            # 合规图不够行下限：只放回缺口张数的不合规图，够了就停
            add = [p for p in paths if _base_ok(p) and _dirty(p)][:min_keep - len(out)]
            if add:
                names = [os.path.basename(p) for p in add]
                forced.update(names)
                logger.warning(f"合规图只有 {len(out)} 张，凑不够 {min_keep} 张行下限，"
                               f"被迫放回 {len(add)} 张仍带中文/水印/logo 的图：{names}")
                out += add
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

    def _clean_first(paths: list) -> list:
        """clean=true 的排最前，其余按脏度——首位会成为该颜色主图。

        _usable 已经把不合规图排到了最后（凑不够下限才放回的那几张），这里再排一次
        是为了把「明确标了 clean」的顶到首位：标注不全的图在 _dirty_score 里与干净图
        同分，但没被模型确认过，不该抢首图位。
        """
        if not notes:
            return paths
        return sorted(paths, key=lambda p: (not _note(p).get("clean"),
                                            _dirty_score(_note(p))))

    if len(colors) == 1:
        imgs = _clean_first(_usable(mains))
        return {"status": "ok", "uncertain_rows": [], "dirtyUsed": sorted(forced),
                "rows": [{"keyword": colors[0], "images": imgs, "uncertain": False}],
                "reason": f"单颜色，{len(imgs)} 张主图全部归属该色（免视觉请求）"}

    # 【优先路径：颜色缩略图按画面配首图，不调 LLM】2026-09-03
    # 【下限就是行下限，不要按颜色数放大】_usable 返回的是【全部】合规图，min_keep
    # 只决定「合规图少到什么程度才被迫放回不合规图」。传 max(下限, 颜色数) 会让
    # 「9 张合规图 / 11 个颜色」这种常见情形凭空触发放回，把带中文的图配成某色首图
    # ——而 9 张合规图足够让 11 行各凑 3 张（同一张通用图可以跨行复用）。
    # 配不上首图的颜色走 uncertain + 补图，比挂一张不合规图强。
    color_images = info.get("colorImages") or {}
    if color_images:
        usable = _usable(mains)
        matched, diag = _match_color_thumbs(color_images, colors, usable, workdir)
        if matched:
            logger.info(f"颜色缩略图配对：{diag}")
            rows, uncertain_rows = [], []
            for c in colors:
                mp = matched.get(c)
                # 配不上的颜色不硬塞图：images 留空 + uncertain，由 service 的
                # _pad_row_images 补合规通用图，并报一次人工确认
                rows.append({"keyword": c, "images": [mp] if mp else [],
                             "uncertain": not mp})
                if not mp:
                    uncertain_rows.append(c)
            # 没被当成首图的其余图【不在这里分配】：它们是平铺/细节/材质图，挂在哪个
            # 颜色行下都说得通，由 _pad_row_images 按行补齐即可（顺序无硬性要求，
            # 只要合规）。原实现把剩余图全塞给第一个颜色，等于凭空断言它们属于那一色。
            return {"status": "ok", "rows": rows, "uncertain_rows": uncertain_rows,
                    "dirtyUsed": sorted(forced),
                    "reason": f"按颜色缩略图配画面（{diag}），未调 LLM"}
        logger.info(f"颜色缩略图一个都没配上，退回 LLM 判断：{diag}")

    # 【原有 LLM 判断逻辑】
    # 【只把候选池里的图传给模型，不再传全量 mains】过滤放在模型之后时，模型分给某色
    # 的图会被 usable 过滤掉、那一行变空 → 记进 uncertain_rows，等于白烧一次视觉调用
    # 还少配一个颜色。传进去的就是能用的，模型分什么都作数。
    # 下限同优先路径：就是行下限，不按颜色数放大（理由见上面那段）。
    cands = _usable(mains)
    prompt = f"""商品标题：{info.get('title') or '（无）'}
源商品颜色（SKU 里的颜色名）：{colors}

下面按顺序给你 {len(cands)} 张主图，编号与文件名对应：
{_listing(cands)}

请把每张图分配给对应的颜色，为每个颜色挑出该颜色的展示图集合：
- 只选「画面是该颜色商品本身」的图（模特实拍/平铺/细节均可）；
- 【只按颜色归属判断，不要因为图上有水印或文字而弃选】不合规的图已在上游筛过/清过；
  但纯尺码表图、工厂/公司介绍图这类非商品图不要选；
- 每个颜色把最能代表该颜色整体外观的图排在第一位（它会作为该颜色的主图）；
- 认不准某张图属于哪个颜色就不要分；某颜色一张合适的图都没有就留空数组。

只输出 JSON：{{"rows": [{{"color": "<颜色名，必须用上面给出的原名>",
"images": ["<文件名>", ...], "uncertain": true/false}}]}}
rows 必须覆盖上面每一个颜色（没图的给 "images": []），uncertain 表示你对该颜色的归属判断没把握。"""
    # rows 里 images 允许是空数组（提示词明说「一张合适的都没有就留空」），故只校验
    # 类型不校验非空；下面对空 images 的颜色本就会记进 uncertain_rows 交人工。
    # 原先非 dict 条目是 continue 静默丢掉——那等于模型答错一行、就少配一个颜色的图，
    # 事后从产物里看不出来，故改成先重问。
    data = await ask_json_with_images(
        prompt, cands, what="阶段⑦SKC分色选图", system=_SYS, stage="skc",
        result_model=SkcPlan)

    by_name = {os.path.basename(p): p for p in cands}
    rows, uncertain_rows = [], []
    for entry in data.get("rows") or []:
        if not isinstance(entry, dict):
            continue
        color = entry.get("color")
        if color not in colors:
            continue
        imgs = [by_name[f] for f in (entry.get("images") or []) if f in by_name]
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
    return {"status": "ok", "rows": rows, "uncertain_rows": uncertain_rows,
            "dirtyUsed": sorted(forced)}


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

    【取不到的图必须在问模型之前剔掉，且不能靠"少传一张"】提示词按 pos 标识每张图，
    少传一张会让其后每张的位次整体前移、模型判断落到错误的图上（2026-09-02 实测
    offer 654598552346：28 张少传 1 张，pos 16/19 拿到邻图的结论，两张超比例长图
    被漏判）。故这里先逐张解析 data URL、把解析不出的连同它的 pos 一起摘掉，
    listing 与传图列表始终一一对应，pos 仍是页面真实序号。
    摘掉的图按 keep 处理并在 unreachable 里报出来：源图已从源站消失，英化/放大都
    无从下手（下载不到原图），只能留着原图交人工——⑬ 的 desc_save 会把它报成
    「仍有外链图未转存」，那条告警此时是准确的。

    【keep 侧必须再过一道专查中文的复核】keep 与 needsUpscale 的共同点是【原画面
    原样上架】：keep 走转存、needsUpscale 走纯几何放大，两条路都按设计不动画面，
    save 又只校验「还是不是 1688 外链」——全链路没有任何环节再看一眼画面内容。
    而初判是几十张图一次过、每张四选一，漏一张中文图它就径直上真店：2026-09-08
    实测 offer 1013502117778，50 张里「商品信息/吊牌尺码」模特卡、两张中文尺码表
    （450 宽破线、经尺寸兜底放大）、两张中文海报共 5 张全被判 keep 原样发布。
    故收尾时对「将按原画面发布」的图做第二遍只查中文的复核（_audit_desc_keeps），
    复核发现中文的一律改判生图英化（破线的摘掉 needsUpscale——纯放大救不了中文）。
    replace/sizechart 的图要过 edit_image + check_cleaned 质检，不在复核范围。
    """
    mods = [m for m in (modules or []) if m.get("url")]
    if not mods:
        return {"status": "ok", "delete": [], "replace": [], "keep": [],
                "reason": "描述区无模块"}

    # 先解析成 data URL：空串表示源站取不到（见 llm.image_ref）。解析结果直接传给
    # ask_json_with_images（它对已是 data URL 的字符串原样透传），不会重复下载。
    refs = await asyncio.to_thread(lambda: [image_ref(m["url"]) for m in mods])
    unreachable = [m["pos"] for m, r in zip(mods, refs) if not r]
    pairs = [(m, r) for m, r in zip(mods, refs) if r]
    if not pairs:
        return {"status": "error", "reason": "描述区所有图都取不到（源站图已失效）",
                "delete": [], "replace": [], "keep": [m["pos"] for m in mods],
                "unreachable": unreachable}
    if unreachable:
        logger.warning(f"阶段⑬描述图规划：pos {unreachable} 源站取不到，"
                       f"不交模型判断、按保留处理（页面留原图，需人工换图）")
    mods = [m for m, _ in pairs]

    listing = "\n".join(f"第{m['pos']} 张（pos={m['pos']}）" for m in mods)
    all_pos = [m["pos"] for m in mods]
    # 尺码表图/尺寸图的处置随「实测尺寸有没有识别到」而变：没识别到时（sizeMeasurements
    # 为空，阶段⑨ 靠模型估算）它是买家唯一的准确尺码来源，要英化 + cm→英寸后保留；已识别到
    # （平台尺码表已有真实数据）时照旧删除。故 prompt 与动作枚举都按此开关裁剪。
    # 【尺寸图也走 sizechart 动作】非服装类（手提袋/包包/玩具/杂货）没有平台尺码表栏，
    # 买家要凭描述区那张标注长宽高的尺寸示意图了解整体大小，故它和服装尺码表一样要
    # 英化 + cm→英寸后保留、不能当「与商品无关的图」删（2026-09-04 用户要求）。非服装
    # 类 sizeMeasurements 恒为空，天然落在 size_missing 这条分支，无需单开开关。
    size_missing = not bool((info or {}).get("sizeMeasurements"))
    sizechart_line = (
        '- "sizechart"：尺码表图或尺寸图（标注商品尺码/长宽高/规格尺寸的图，服装尺码表、'
        "包包玩具的尺寸示意图都算）——保留，但把图中中文翻译成英文、并把长度单位 cm "
        "换算成英寸(in)后替换（买家靠它选码或了解整体尺寸，别删）；\n"
        '- "delete"：工厂/公司介绍、与商品无关的图、与前面重复出现的图——直接删；\n'
        if size_missing else
        '- "delete"：工厂/公司介绍、尺码表、与商品无关的图、与前面重复出现的图——直接删；\n'
    )
    acts_enum = "keep|delete|replace" + ("|sizechart" if size_missing else "")
    prompt = f"""商品标题：{(info or {}).get('title') or '（无）'}

下面是该商品详情描述区的 {len(mods)} 张图，按页面展示顺序，序号就是 pos：
{listing}

Temu 半托管发布只关心商品图，请逐张决定动作：
{sizechart_line}- "replace"：图本身是商品展示图，但含中文文字、水印、店铺名或他人品牌 logo
  ——保留画面，把中文英化并移除水印后替换；
- "keep"：干净的商品图（无中文/水印/他人 logo）——保留。

只输出 JSON：{{"actions": [{{"pos": 1, "action": "{acts_enum}",
"reason": "<10字内>"}}]}}
actions 必须覆盖上面列出的每一张（pos 取值：{all_pos}）。"""
    # pos 必须是整数：模型偶尔回字符串 "1"，下面 `pos not in valid_pos` 不成立就把
    # 那张图整条动作丢掉（既不删也不换、静默留着 1688 原图），故在这里先重问。
    # reason 只进日志不列必答。
    data = await ask_json_with_images(
        prompt, [r for _, r in pairs], what="阶段⑬描述图规划", system=_SYS,
        stage="desc", result_model=DescPlan)

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
        elif act in ("replace", "sizechart"):
            rep = {"pos": pos, "url": valid_pos[pos]["url"],
                   "reason": (a.get("reason") or "")[:40]}
            if act == "sizechart":
                # 尺码表图要英化 + cm→英寸，替换时用专用提示词（见 service._prepare_desc_image）
                rep["sizechart"] = True
            replace.append(rep)
        else:
            keep.append(pos)
    missed = [p for p in valid_pos if p not in delete
              and p not in keep and all(r["pos"] != p for r in replace)]
    keep.extend(missed)  # LLM 漏判的一律按保留处理——保守方向，不删不该删的

    # 尺寸兜底：keep 里但尺寸不达标的，改判 replace + needsUpscale（理由见 docstring）。
    # 已在 replace 里的不用管：它本来就要重新出图，出图收尾的 compress 会把尺寸拉够。
    # 【取不到的图不进兜底】它下载不到原图，放大与英化都无从下手，只能留原图交人工。
    unreach = set(unreachable)
    kept_small = [p for p in keep
                  if valid_pos[p].get("tooSmall") and p not in unreach]
    if kept_small:
        keep = [p for p in keep if p not in set(kept_small)]
        for p in kept_small:
            # 理由取 desc_map 算好的 sizeReasons（可能是「小于 480x480」也可能是
            # 「宽高比超出 0.5~2.0」两类，见 images.check_desc_size）；写死尺寸
            # 文案会把超比例的长条图说成「像素不够」，人工复核时看不出真因。
            why = "、".join(valid_pos[p].get("sizeReasons") or []) \
                or f"尺寸 {valid_pos[p].get('size')} 不符合描述图要求"
            replace.append({"pos": p, "url": valid_pos[p]["url"],
                            "needsUpscale": True, "reason": why[:60]})
        replace.sort(key=lambda r: r["pos"])

    # ---- keep 侧中文复核（理由见 docstring 末段）----
    # 复核对象是「将按原画面发布」的全部图：keep（转存）+ needsUpscale（纯放大）。
    # 取不到的图（unreachable）没有 ref 可传，天然不在复核范围。
    ref_by_pos = {m["pos"]: r for m, r in pairs}
    audit_pos = [p for p in sorted(set(keep) | {r["pos"] for r in replace
                                                if r.get("needsUpscale")})
                 if p in ref_by_pos]
    if audit_pos:
        dirty = await _audit_desc_keeps(audit_pos, ref_by_pos, info)
        if dirty:
            keep = [p for p in keep if p not in dirty]
            rep_by_pos = {r["pos"]: r for r in replace}
            for p, d in dirty.items():
                rep = rep_by_pos.get(p)
                if rep is None:
                    rep = {"pos": p, "url": valid_pos[p]["url"]}
                    replace.append(rep)
                    rep_by_pos[p] = rep
                # 有中文就必须走生图英化：纯放大不动画面，中文会原样带出去
                rep.pop("needsUpscale", None)
                if d.get("sizeTable"):
                    rep["sizechart"] = True
                rep["reason"] = ("复核发现中文：" + (d.get("what") or ""))[:60]
            replace.sort(key=lambda r: r["pos"])

    out = {"status": "ok", "delete": sorted(set(delete)),
           "replace": replace, "keep": sorted(set(keep) | unreach)}
    if unreachable:
        out["unreachable"] = sorted(unreach)
    return out


async def _audit_desc_keeps(audit_pos: list, ref_by_pos: dict,
                            info: Optional[dict] = None) -> dict:
    """对「将按原画面发布」的描述图专查中文，返回 {pos: {"sizeTable", "what"}}（只含脏的）。

    与初判分两遍的原因（单任务小批量比几十张混审可靠）见 plan_desc docstring 末段。
    传图复用初判已解析好的 data URL（ref_by_pos），不重复下载。

    【响应只列脏图、空数组即全干净】不采用「逐张回 verdicts」：那种形状下模型对
    全干净的批次天然回 {"verdicts": []}，「全干净」与「没干活」无从区分，再把
    漏答按脏处理就会每轮都重写一堆干净图。复核的可靠性靠「单任务 + 每批 8 张」
    这个输入形状保证，不靠响应形状。
    """
    dirty: dict = {}

    async def _one(chunk: list) -> None:
        listing = "\n".join(f"第{p} 张（pos={p}）" for p in chunk)
        prompt = f"""商品标题：{(info or {}).get('title') or '（无）'}

下面是同一商品描述区里的 {len(chunk)} 张图，序号是 pos：
{listing}

它们初判为「干净的商品图」，将【原样】发布到 Temu 海外站。你的唯一任务：复核
每张图上是否存在任何【中文字符或中文标点】——标题大字、小字说明、表格文字、
水印、吊牌/标签上的字都算；英文、数字、符号不算。

只输出 JSON：{{"dirty": [{{"pos": 1, "sizeTable": true/false,
"what": "<10 字内说清中文在哪，如「模特信息卡全文」「左上角海报标题」>"}}]}}
dirty 只列【有中文】的图（pos 必须从 {chunk} 里取），全部干净就返回空数组。
sizeTable 表示该图是不是尺码表/尺寸示意图。
拿不准一律算有中文——漏掉的代价是它原样发上真店。"""
        data = await ask_json_with_images(
            prompt, [ref_by_pos[p] for p in chunk], what="阶段⑬keep图中文复核",
            system=_SYS, stage="desc", result_model=DescAudit)
        for v in data.get("dirty") or []:
            if not isinstance(v, dict):
                continue
            p = v.get("pos")
            if p not in chunk or p in dirty:
                continue
            dirty[p] = {"sizeTable": bool(v.get("sizeTable")),
                        "what": (v.get("what") or "")[:20]}

    await asyncio.gather(*(_one(c) for c in
                           (audit_pos[i:i + _DESC_AUDIT_CHUNK]
                            for i in range(0, len(audit_pos), _DESC_AUDIT_CHUNK))))
    if dirty:
        logger.warning(f"阶段⑬中文复核：{len(dirty)} 张初判保留的图发现中文，"
                       f"改判生图英化：pos {sorted(dirty)}")
    return dirty


async def translate_size_texts(texts: list, info: Optional[dict] = None) -> dict:
    """把疑似尺码的文字模块翻译成英文、并把长度单位 cm 换算成英寸。

    texts: [{"idx": "0", "text": "..."}]（调用方已用 extract._RE_SIZE_HINT 筛过，只传
    尺码文字）。返回 {"status": "ok", "plan": [{"idx", "action": "translate",
    "text", "reason"}]}，plan 只含 translate 动作（删除/保留由调用方按 idx 归属决定）。

    【为什么单独一个函数而不是恢复旧的 plan_desc_text】旧版对文字模块做「删/译/留」
    全量分类，2026-09-01 起用户只要求删除、不再英化保留；这次只恢复【尺码】这一类
    （当实测尺寸没识别到、平台尺码表靠估算时，描述区尺码原文是买家唯一准确来源）。
    判「是不是尺码」用 extract._RE_SIZE_HINT 确定性完成，这里只做翻译 + 单位换算。

    【cm 必须换算成英寸】2026-09-04 用户要求：海外买家看 cm 陌生，尺码数值按 1cm≈0.39in
    换算成英寸（保留 1 位小数），翻译与换算一步做完，避免再跑一次文本调用。
    """
    items = [t for t in (texts or []) if (t.get("text") or "").strip()]
    if not items:
        return {"status": "ok", "plan": []}

    listing = "\n\n".join(
        f"[模块 idx={t['idx']}]\n{(t.get('text') or '')[:600]}" for t in items)
    prompt = f"""商品标题：{(info or {}).get('title') or '（无）'}

下面是该商品描述区的 {len(items)} 个「尺码文字模块」原文（从 1688 采集带过来的）：

{listing}

请把每个模块翻译成**自然的英文**，要求：
1. 保留原有分行与尺码档位对应关系（如 80/90/100 各自的一行/一段）；
2. 长度单位 cm 一律换算成英寸 in，数值保留 1 位小数（如 59cm → 23.2in）；只换算长度，
   胸围/腰围等全围仍按全围写、不要当半围；
3. 只翻译尺码/尺寸相关信息，不要添加价格、折扣、运费、年份等原文没有的内容；
4. 每个模块译文不超过 500 字符（平台上限），超了就精简次要信息。

只输出 JSON：{{"plan": [{{"idx": "<原样照抄模块 idx>", "text": "<英文译文>"}}]}}
plan 必须覆盖上面每一个模块。"""

    data = await ask_json(prompt, what="阶段⑬尺码文字英化", stage="desc")

    valid = {str(t["idx"]): t for t in items}
    plan, seen = [], set()
    for p in data.get("plan") or []:
        if not isinstance(p, dict):
            continue
        idx = str(p.get("idx"))
        if idx not in valid or idx in seen:
            continue
        text = (p.get("text") or "").strip()
        if not text:
            continue
        seen.add(idx)
        plan.append({"idx": idx, "action": "translate", "text": text,
                     "reason": "尺码英化"})
    return {"status": "ok", "plan": plan}


async def check_cleaned(image_path: str) -> dict:
    """AI 英化后的质检：残留中文/拼音/乱码或破坏主体都算不过。

    返回 {"status": "ok", "clean": bool, "issues": str, "residualChinese": bool}。
    实测生图会残留拼音、误译品类（pipeline.desc_replace 注释），只看「中文没了」
    会把带乱码文案的图挂上去，故替换前必须过这道。

    【residualChinese 单独回一个字段，因为它决定重试次数】残留中文是「必须清干净」
    的一类（Temu 最硬的红线），而生图有随机性，多烧一发常常就过；其它 issues
    （修图痕迹之类）多烧也是同样结果。调用方据此给中文那一类更多次数，见
    service.DESC_QC_TRIES_CJK。

    【商品实物上的图案/刺绣/品牌织标一律不算问题】2026-08-26 实测这条提示词的代价：
    一张棒球服图被判「衣服上有品牌logo及疑似乱码英文字符」而退回原图，可那是衣服上
    真实的绣标与装饰字母——人工选品阶段已经筛掉了不能用的款，实物的一部分不是「待
    清理的文字层」。误报的后果不是保守而是更糟：退回原图 = 中文外链图留在描述区，
    既过不了 1340×1785 闸门、也过不了合规。故这里把判据收窄到【叠加在图上的文案层】。
    """
    prompt = """这张图片刚经过 AI 英化处理（把叠加在图上的中文文案改成英文）。请质检：

只看【叠加在图片上的文字层】（标题文案、说明文字、水印、店铺名这类后期加的字）：
- residualChinese：是否还残留任何中文字符或中文标点（『』「」、，。！？；：《》等）；
- garbled：是否有拼音、乱码、断词、无意义字母串（正常英文单词不算）；
- brokenSubject：是否有明显修图痕迹破坏了商品主体（糊掉、变形、缺块）。

【以下一律不算问题，不要报】：
- 商品实物本身的印花、刺绣、织标、袖标、胸标、图案上的字母或品牌标识
  —— 那是实物的一部分，选品时已人工确认过，不需要清理；
- 图片本身的构图、留白、配色。

只输出 JSON：{"residualChinese": true/false, "garbled": true/false,
"brokenSubject": true/false, "issues": "<20字内，没有问题留空>"}"""
    # 【这一处刻意不传 schema】本判断点是【双形状契约】：新形状三个布尔，老形状只回
    # 一个 clean，下面那段兼容代码是特意留的（模型偶尔仍按老约定答）。把新形状列成
    # 必答，等于把一个已经能正确读懂的回答判成错、重问三次再抛——而它是逐张图调用
    # 的（一个商品十几张），代价按张放大。形状风险在这里已由下面的兼容分支接住，
    # 不需要靠重试解决。
    data = await ask_json_with_images(prompt, [image_path], what="英化质检", system=_SYS,
                                      stage="clean_images")
    cjk = bool(data.get("residualChinese"))
    garbled = bool(data.get("garbled"))
    bad = cjk or garbled or bool(data.get("brokenSubject"))
    # 【兼容只回 clean 的旧形状】提示词换过好几版，模型偶尔仍按老约定只回一个
    # clean 布尔（老提示词的字段）。此时以它为准，免得把「模型说不干净」读成干净——
    # 三个新字段缺失时 bad 恒 False，那才是真的危险。
    if "clean" in data and not any(
            k in data for k in ("residualChinese", "garbled", "brokenSubject")):
        bad = not bool(data.get("clean"))
    # 【residualChinese 与 garbled 都要透出去】service 侧靠这两个字段决定重试发数
    # （见 DESC_QC_TRIES_TEXT）。garbled 原先只参与算 bad、没进返回值，于是调用方
    # 的 `qc.get("garbled")` 恒为 None，加长重试对乱码那一路形同虚设——而 ⑤b main-04
    # 两发恰好全是 garbled，正是要救的那种。
    return {"status": "ok", "clean": not bad,
            "issues": (data.get("issues") or "")[:80],
            "residualChinese": cjk, "garbled": garbled}
