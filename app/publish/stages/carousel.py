"""店小秘发布共用能力：stages.carousel。各来源流程由 workflows/ 独立定义。"""

import asyncio
import hashlib
import json
import os
import shutil
from app.logger import logger
from app.publish import extract, images, preferences, state, text_erase, vision
from app.publish.browser import BrowserSession
from app.publish.media import space as media_space
from app.publish.media.carousel import (
    CAROUSEL_MAX_BYTES,
    CAROUSEL_MAX_PICKED,
    CAROUSEL_MIN_PICKED,
    CAROUSEL_MIN_SIDE,
    carousel_state,
    expand_carousel_pool,
    open_carousel_space,
    toggle_carousel,
)
from app.publish.upload import upload_many


# 单张生图超时，与 ⑤b 的 CLEAN_TIMEOUT 同值：同一种「curl 生图」调用、同一类图，
# 没有理由在这里另定一套。两者现在都直接取 images.EDIT_TIMEOUT（原先各写 90，
# 依据的是单发耗时，并发下盖不住服务端实际的 30~79s，理由见那个常量的注释）。
EN_TIMEOUT = images.EDIT_TIMEOUT
EN_QC_TRIES = 3


# ---- 候选池 → 本地文件 -------------------------------------------------------



async def _resolve_pool(items: list, prep: str, emit) -> dict:
    """下载页面实际图片，避免本地已英化文件与页面原图内容不一致。"""
    out, missing = {}, list(items)

    if missing:
        conc = preferences.get_image_concurrency()
        sem = asyncio.Semaphore(conc)

        async def _one(it: dict) -> tuple:
            name = f"pool-{it['i']:02d}.jpg"
            dst = os.path.join(prep, name)
            async with sem:
                try:
                    n = await asyncio.to_thread(extract._download_image, it["url"], dst)
                except Exception as e:
                    return it["i"], None, f"下载异常：{e}"[:80]
            if not n:
                return it["i"], None, "源站取不到（404 等）"
            return it["i"], {"file": name, "path": dst, "note": None,
                             "url": it.get("url") or ""}, ""

        logger.info(f"轮播候选：下载 {len(missing)} 张页面实际图片（并发 {conc}）")
        got = await asyncio.gather(*(_one(it) for it in missing))
        failed = 0
        for i, entry, err in got:
            if entry:
                out[i] = entry
            else:
                failed += 1
                logger.warning(f"轮播图第 {i + 1} 张源图取不到（{err}），本张不参与判定")
        if failed:
            await emit({"type": "log", "stage": "carousel",
                        "message": f"{failed} 张候选源图取不到（404 等），"
                                   "本张既不补勾也不参与质检"})
    return out


# ---- 备料：英化 / 合规化 -----------------------------------------------------

def _en_cache_path(workdir: str, url: str) -> str:
    """英化产物的落盘路径，按【源 URL 哈希】命名。

    【为什么不用格子上标】下标会随「新图插入候选列表」整体后移，同一个下标下次可能是
    另一张图；源 URL 是稳定标识。这与 ⑬ 的 _desc_cache_paths 同一理由。

    【必须与临时目录分开】carousel/ 是每次跑都整个清掉的备料目录，缓存产物落在那里面
    等于每轮都当缓存未命中、把同一张图重烧一遍生图。故另起 carousel-edit/，与 ⑬ 的
    desc-edit/ 同构。
    """
    h = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]
    return os.path.join(workdir, "carousel-edit", f"{h}-en.jpg")


async def _english_one(local_path: str, url: str, workdir: str,
                       sizechart: bool = False, initial_qc: dict = None) -> dict:
    """缓存与新产物均先双检，带具体失败原因最多生图三次，再交页面复检。

    QC 唯一不满是材质文字时不走重生，改走 text_erase 定点抹除救援：生图模型
    对规格表的选择性抹除是确定性失败（三组对照实验，见 text_erase 模块头），
    重生多少次都是同一个错。
    """
    out = _en_cache_path(workdir, url)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    last_qc = initial_qc or {}
    if os.path.exists(out) and os.path.getsize(out) > 0:
        sz = images.image_size(out)
        if (sz and sz[0] == sz[1] and min(sz) >= CAROUSEL_MIN_SIDE
                and os.path.getsize(out) <= CAROUSEL_MAX_BYTES):
            last_qc = await vision.check_cleaned_twice(out)
            if last_qc.get("clean") is True:
                return {"ok": True, "path": out, "how": "cached"}
        os.remove(out)
        logger.warning(f"轮播图缓存未通过复核，已作废：{url}；{last_qc.get('issues') or sz}")
        if last_qc.get("status") == "error":
            return {"ok": False, "why": f"缓存质检失败：{last_qc.get('issues')}"[:150]}
    base = (images.SIZECHART_TRANSLATE_PROMPT if sizechart
            else images.DEFAULT_TRANSLATE_PROMPT)
    base += " 将完整内容排入1:1画布，保留客观商品介绍及尺码、数值和单位，不裁掉信息。"
    erase_tried = False
    brand_erase_tried = False
    for attempt in range(EN_QC_TRIES):
        prompt = base
        if last_qc.get("issues"):
            prompt += (
                f" 本张图质检指出：{str(last_qc['issues'])[:200]}。请逐项修正，"
                "纯英文文案也必须修正：拼写错误改成正确英文，中文说明翻译成英文；"
                "夸大宣传、情绪标语、水印直接抹除，不要翻译或换一个同义标语；"
                "环保声明、品牌标识、材质成分说明同样直接抹除。"
                "这些修正优先于保留原文案，但商品实物（装饰性印花与刺绣）及客观参数、"
                "使用说明必须保留。")
        try:
            # 走 edit_image_async：并发闸门在那一层全局共用，不再由本阶段自己限流
            # （各阶段各建 Semaphore 会把旋钮值乘上并行路数，见 images._EDIT_GATES）
            edited = await images.edit_image_async(
                local_path, prompt=prompt, out_path=out,
                size="2048x2048", timeout=EN_TIMEOUT)
            prepared = _to_carousel_size(edited["output"], out, preserve_info=True)
            if not prepared:
                raise RuntimeError("生图产物尺寸/体积处理失败")
            last_qc = await vision.check_cleaned_twice(prepared)
        except Exception as error:
            if os.path.exists(out):
                os.remove(out)
            return {"ok": False, "why": f"英化失败：{error}"[:150]}
        if last_qc.get("clean") is True:
            return {"ok": True, "path": prepared, "how": "edited"}
        # 【材质行定点抹除救援】QC 唯一不满的是材质文字时，别再带反馈重生——
        # 那对本类问题是确定性失败（模块 docstring 与 text_erase 模块头的取证），
        # 改成按 QC 给出的原文（materialTexts）定点抹除，抹完复检过了照样放行。
        # 只试一次：定位失败/背景非纯色抹不成、或抹完复检仍不过，都落回下面
        # 原有的重试路径（下一发生图仍从原图重来，抹过的产物不进入下一轮）。
        if not erase_tried and _material_only(last_qc):
            erase_tried = True
            rescued = await text_erase.erase_text_lines(
                prepared, last_qc.get("materialTexts") or [])
            if rescued.get("ok"):
                last_qc = await vision.check_cleaned_twice(prepared)
                if last_qc.get("clean") is True:
                    return {"ok": True, "path": prepared, "how": "erased"}
                logger.warning(f"轮播图材质行抹除后复检仍未过："
                               f"{last_qc.get('issues') or '无结论'}")
            else:
                logger.warning(f"轮播图材质行定点抹除未执行（{rescued.get('why')}），"
                               "回到带反馈重生")
        # 【品牌标识定点抹除救援】同上一段的材质行救援：位置、取向、只试一次都一致，
        # 补的是与 ⑦b（preview 那条 erase_marks 救援）对称的缺口——⑦b 早就有了，
        # ⑤c 只有材质行。轮播图上的品牌 logo 常常正好压在近纯色块上（角标、页眉条），
        # 是这条确定性链路够得着的场景。定位失败/底色不纯/复检仍不过都落回下面的
        # 重生路径，不新增失败面（2026-10-09 1048494210610 第 1 张的「荆牌」压在
        # 橙红渐变顶栏上，就是底色不纯那一种，仍交人工——但纯色底的同类标能救回）。
        if not brand_erase_tried and _brand_only(last_qc) and last_qc.get("brandMarkTexts"):
            brand_erase_tried = True
            rescued = await text_erase.erase_marks(
                prepared, last_qc["brandMarkTexts"])
            if rescued.get("ok"):
                last_qc = await vision.check_cleaned_twice(prepared)
                if last_qc.get("clean") is True:
                    return {"ok": True, "path": prepared, "how": "brand_erased"}
                logger.warning(f"轮播图品牌标识抹除后复检仍未过："
                               f"{last_qc.get('issues') or '无结论'}")
            else:
                logger.warning(f"轮播图品牌标识定点抹除未执行（{rescued.get('why')}），"
                               "回到带反馈重生")
        if os.path.exists(out):
            os.remove(out)
        logger.warning(f"轮播图上传前质检未通过（{attempt + 1}/{EN_QC_TRIES}）："
                       f"{last_qc.get('issues') or '无结论'}")
        if last_qc.get("status") == "error":
            break
    return {"ok": False, "why": f"英化质检未过：{last_qc.get('issues') or '无结论'}"[:150]}


def _to_carousel_size(path: str, out_path: str, preserve_info: bool = False) -> str:
    """把一张图收口到轮播图的尺寸口径：1:1、≥800、≤2M。返回产物路径。

    preserve_info 使用补白保留完整内容；生图产物本身已是 1:1，走这里只是复核。
    体积那条是页面原文「大小在2M以内」，
    此前全链路无人校验（upload_image 只查尺寸），超了就降 quality 重存一次。
    """
    sz = images.image_size(path)
    if not sz:
        return ""
    out = path
    if sz[0] != sz[1] or min(sz) < CAROUSEL_MIN_SIDE:
        if preserve_info:
            from PIL import Image, ImageOps
            with Image.open(path) as source:
                side = max(*source.size, images.MATERIAL_TARGET)
                square = ImageOps.pad(source.convert("RGB"), (side, side),
                                      method=Image.Resampling.LANCZOS, color="white")
                square.save(out_path, quality=85)
            out = out_path
        else:
            out = images.square_image(path, out_path=out_path)["output"]
    if os.path.getsize(out) > CAROUSEL_MAX_BYTES:
        # 下限传轮播图自己的口径：套服装闸门会把 2048 的产物先放大再缩，白糊一次
        out = images.compress(out, quality=70,
                              min_w=CAROUSEL_MIN_SIDE, min_h=CAROUSEL_MIN_SIDE)
        if os.path.getsize(out) > CAROUSEL_MAX_BYTES:
            logger.warning(f"轮播图产物压缩后仍超 2M：{out}")
            return ""
    return out


# ---- 挑补勾的信息图 ----------------------------------------------------------

def _fingerprint(path: str) -> str:
    """整幅画面摘要：只用来识别「内容完全相同的文件」，不做相似度判断。"""
    with open(path, "rb") as source:
        return hashlib.sha256(source.read()).hexdigest()


def _pick_spares(cands: list, pool: dict, verdicts: dict, refs: set,
                 taken: set, need: int) -> list:
    """挑「不需要英化就能直接用」的候选图，用来顶替英化失败的已选图。

    【为什么只挑干净的】失败那张的病根通常是画面里抹不掉的顽固元素——2026-10-09
    1048494210610 第 1 张的「荆牌」角标压在渐变顶栏上，生图三发纹丝不动、定点抹除
    又栽在底色不纯。换一张同样要英化的候选，只是把同一次赌博再押一遍（钱照烧、
    未必赢）；挑 plan_carousel 判 chinese=false 的图直接顶上，是零成本、确定性的
    那一档。一张都没有时退到「取消这张」那条路，见 _st_carousel 的换图段。

    refs 是已选用/已补勾画面的指纹集（画面重复的不要，与 _pick_adds 同一条判据）；
    taken 是这次已经挑走的候选格位下标。
    """
    picked_spares = []
    for it in sorted(cands, key=lambda entry: entry["i"]):
        if len(picked_spares) >= need:
            break
        if it["i"] in taken:
            continue
        entry = pool.get(it["i"])
        if not entry:
            continue
        v = verdicts.get(entry["file"])
        # 判定缺项按「未知」处理、不当干净用（同 plan_carousel 不补默认值的取向）
        if not v or v.get("chinese") is not False:
            continue
        digest = _fingerprint(entry["path"])
        if digest in refs:
            continue
        refs.add(digest)
        taken.add(it["i"])
        picked_spares.append((it, entry))
    return picked_spares


def _pick_adds(cands: list, pool: dict, verdicts: dict, picked: list,
               add_max: int) -> list:
    """按信息价值挑图，仅去除内容完全相同的文件，避免漏掉相似版式中的不同参数。
    """
    ranked = []
    for it in cands:
        entry = pool.get(it["i"])
        if not entry:
            continue
        v = verdicts.get(entry["file"])
        if not v or not v.get("isInfo"):
            continue
        ranked.append((int(v.get("value") or 0), it, entry))
    # 信息价值高的优先（尺码/尺寸 3 > 材质/规格 2 > 其它说明 1）
    ranked.sort(key=lambda t: -t[0])

    fingerprint = _fingerprint

    chosen = []
    refs = {fingerprint(pool[it["i"]]["path"]) for it in picked if it["i"] in pool}
    for _value, it, entry in ranked:
        if len(chosen) >= add_max:
            break
        digest = fingerprint(entry["path"])
        if digest in refs:
            logger.info(f"轮播图第 {it['i'] + 1} 张是信息图，"
                        "但与已选用的图画面重复，不补勾")
            continue
        chosen.append((it, entry))
        refs.add(digest)
    return chosen


def _material_only(qc: dict) -> bool:
    """QC 的【唯一】阻断项是材质文字。只有这种情形才上定点抹除救援：
    断词/残留中文/水印/宣称/品牌标是生图质量或另一类内容问题，定点抹除
    治不了（也不该抹——挖掉的是商品信息），仍走带反馈重生。
    """
    if qc.get("status") == "error" or not qc.get("materialText"):
        return False
    return not any(qc.get(k) for k in (
        "residualChinese", "garbled", "watermark", "marketingClaim",
        "bannedTerm", "brandMark"))


def _brand_only(qc: dict) -> bool:
    """QC 的【唯一】阻断项是品牌标识。与 _material_only 对称的另一类定点抹除救援。

    【为什么品牌标算「治得了的那一类」】它和材质文字同性质：要抹的不是商品本身，
    而是一块贴在画面上的标记，重生多少次模型都只会照抄（2026-10-09 1048494210610
    第 1 张的「荆牌」角标三发纹丝不动）。而断词/残留中文/水印/禁词/宣称是翻译与
    内容问题——挖掉一块画面治不了，仍走带反馈重生。
    """
    if qc.get("status") == "error" or not qc.get("brandMark"):
        return False
    return not any(qc.get(k) for k in (
        "residualChinese", "garbled", "watermark", "marketingClaim",
        "bannedTerm", "materialText"))


# ---- 页面复检：对称口径（放行两票好、拦下两票坏）-----------------------------
# 判据本体（vision.qc_failed 等）与 ⑦b 的英化产物判定共用一份；本文件这里的口径
# 是 2026-10-08 用户定的「两票一致才拦」，与备料段/⑦b 的取严口径不同，理由见下。


async def _confirm_page_qc(path: str, qc: dict, tag: str) -> tuple:
    """收尾复检的判定确认：固定问两次，**两票都判坏才拦，一票好即放行**。

    返回 (据此放行/拦截的结论, 是否属于「两次不一致、按放行处理」)。

    【为什么改成对称口径】备料段放行一份上传字节，要求 vision.check_cleaned_twice
    连问两次都判好；收尾复检是对【同一份字节】的第二道判定，原先却只要一次判坏就拦
    ——等于放行一侧要两票、拦下一侧只要一票。2026-10-08 商品 1048494210610 一晚上
    四次卡在 ⑤c，其中三次拦的是我用同一套质检复问 2~3 次全干净的图（第 23 张 3/3、
    第 17 张 2/2、第 16 张 2/2）；更关键的反证是：同一套质检对另一张整页中文的图
    （final-17）**一次都没报出中文**（复问只报品牌/禁词）——单票判坏买到的红线安全感
    并没有当初设想的那样足，而它的误报成本每次都是一整单停摆加一轮页面改动丢失。
    故改成两票一致才拦：真缺陷要连过备料段双检与收尾复检两关都漏掉，才会发出去。
    【error 票算坏票】status=error 是「没读到结论」，未知在这条红线上不能当安全
    （同 check_cleaned 的取向）——「两票都读不出结论」照样拦；但它单独一票不足以判死
    （另一票说好就放行，毕竟我们拿到过一次明确结论）。
    【判好那一侧不再提前放行】本函数固定问满两次，2026-09-18 那种「首问答好就够」的
    写法取消了——两次都问满才谈得上「两票一致」。
    """
    first_bad = vision.qc_failed(qc)
    if first_bad:
        logger.warning(f"轮播图{tag}复检首问未通过（{qc.get('issues') or '无结论'}），"
                       "再问一次定夺")
    again = await vision.check_cleaned(path)
    second_bad = vision.qc_failed(again)
    if first_bad and second_bad:
        logger.warning(f"轮播图{tag}复检两问都判不合格"
                       f"（{again.get('issues') or qc.get('issues') or '无结论'}），拦下")
        return again, False
    if first_bad != second_bad:
        # 一票坏一票好：两次不一致就不当结论用，按合格放行并留痕（note 里会带上张数）
        issues = qc.get("issues") if first_bad else again.get("issues")
        logger.info(f"轮播图{tag}复检两次结论不一致（{issues or '无结论'}），"
                    "按合格放行")
        return (again if first_bad else qc), True
    return qc, False


def _erase_route(qc: dict) -> tuple:
    """收尾复检这份字节上的问题，哪一类能靠定点抹除解决。返回 (route, texts)。

    【为什么判据与备料段共用】_brand_only / _material_only 表达的是「唯一阻断项就是这一类」，
    这条口径在备料段（对生图产物）与这里（对页面上那份字节）是同一条，不另立一套。
    另要求质检给出可见原文：定位那一步是靠原文内容配框的（text_erase._match_boxes），
    没有原文就没有目标，抹除链路注定空手而归（那种情形交给「取消勾选」兜底）。
    """
    if _brand_only(qc) and qc.get("brandMarkTexts"):
        return "brand", qc["brandMarkTexts"]
    if _material_only(qc) and qc.get("materialTexts"):
        return "material", qc["materialTexts"]
    return "", []


async def _restore_old(session: BrowserSession, old_url: str) -> None:
    """换图失败时把「腾位」取消掉的旧图勾回去（与主循环的 _restore 同语义）。

    【为什么重读后还要再判一次上限】「新图没认出来」不等于「新图没插进去」——认图靠 fileId
    的文件名子串匹配，可能只是没匹配上；那时再勾回旧图就把选用数顶到上限之上。故重读后要求
    「旧图确实未勾」且「当前勾选数 < 上限」两条都成立才勾。
    """
    current = await carousel_state(session)
    items = current.get("items") or []
    if sum(bool(entry.get("checked")) for entry in items) >= CAROUSEL_MAX_PICKED:
        return
    old = next((entry for entry in items if entry.get("url") == old_url), None)
    if old is not None and not old.get("checked"):
        await toggle_carousel(session, old["i"], True)


async def _swap_one(session: BrowserSession, old_url: str, local_file: str) -> dict:
    """把页面上 old_url 那一格换成 local_file（先增后减 / 先减后增，选用数净 0）。

    次序照抄主替换循环（见 _st_carousel 里「分批把新图选进候选列表」那段），理由是同一批：
    满了先腾位、插完新图再取消旧图，选用数全程不掉到下限以下。
    【腾位失败必须立刻收工】不腾位硬插会被平台整批丢弃（2026-09-17 实测的「最大支持10张
    图片」），插进去的东西认不到、账目全乱，宁可原样报失败。
    【必须传轮播尺寸下限】upload_many 不传下限时落回服装闸门 1340×1785，800×800 的轮播图
    会被它直接拒掉——那样每次救援都白烧一次抹除（可能含一发生图）。
    【选图失败要关弹窗】_pick_many_from_space 失败时刻意不点确定、把弹窗留着，不关会挡住
    后面所有阶段的点击（主循环同一处置）。
    """
    current = await carousel_state(session)
    items = current.get("items") or []
    old = next((entry for entry in items
                if entry.get("url") == old_url and entry.get("checked")), None)
    if old is None:
        return {"ok": False, "why": "页面上找不到要替换的那一格"}
    freed = False
    if sum(bool(entry.get("checked")) for entry in items) >= CAROUSEL_MAX_PICKED:
        moved = await toggle_carousel(session, old["i"], False)
        if moved.get("stage") != "ok":
            return {"ok": False, "why": "选用位已满且腾位失败"}
        freed = True

    up = await upload_many(session, [local_file],
                           min_w=CAROUSEL_MIN_SIDE, min_h=CAROUSEL_MIN_SIDE)
    uploaded = up.get("uploaded") or []
    if not uploaded or not uploaded[0].get("fileId"):
        if freed:
            await _restore_old(session, old_url)
        return {"ok": False, "why": f"重传失败：{str(up)[:100]}"}

    opened = await open_carousel_space(session)
    if opened.get("err") or not opened.get("opened"):
        if freed:
            await _restore_old(session, old_url)
        return {"ok": False, "why": f"打不开选图弹窗：{opened.get('err') or ''}"[:120]}
    picked = await media_space._pick_many_from_space(session, [uploaded[0]["fileId"]])
    if picked.get("err"):
        # 弹窗还开着，先尽力关掉再报失败（best-effort，关不掉不影响失败返回）
        await media_space._close_space_modal(session)
        if freed:
            await _restore_old(session, old_url)
        return {"ok": False, "why": f"空间选图失败：{picked.get('err')}"[:120]}

    # 插完重读：新图按 fileId 文件名认（同主循环），旧图按 URL 认
    current = await carousel_state(session)
    items = current.get("items") or []
    file_id = uploaded[0]["fileId"].rsplit("/", 1)[-1]
    hit = next((entry for entry in items if file_id in (entry.get("url") or "")), None)
    if hit is None:
        if freed:
            await _restore_old(session, old_url)
        return {"ok": False, "why": "认不到刚插入的新图"}
    if not hit.get("checked"):
        if sum(bool(entry.get("checked")) for entry in items) >= CAROUSEL_MAX_PICKED:
            if freed:
                await _restore_old(session, old_url)
            return {"ok": False, "why": "选用位已满，新图勾不上"}
        checked = await toggle_carousel(session, hit["i"], True)
        if checked.get("stage") != "ok":
            if freed:
                await _restore_old(session, old_url)
            return {"ok": False, "why": "新图勾选失败"}
    if not freed:
        latest = await carousel_state(session)
        old_now = next((entry for entry in (latest.get("items") or [])
                        if entry.get("url") == old_url and entry.get("checked")), None)
        if old_now is not None and (await toggle_carousel(session, old_now["i"], False)
                                    ).get("stage") != "ok":
            # 新图已挂上、旧图没取消：不合规的那张仍在选用里，照旧按失败交人工。不去动刚换好
            # 的那张——调用方随后的「取消该格勾选」正好会把这个多出来的旧格收掉。
            return {"ok": False, "why": "旧图取消失败（新图已挂上，选用数多一张）"}
    return {"ok": True, "url": hit.get("url") or ""}


async def _erase_rescue(session: BrowserSession, item: dict, page_path: str,
                        route: str, texts: list, prep: str) -> dict:
    """收尾复检抓到「可定点抹除类」问题时的救援：抹掉那处标记，再把页面上那一格换掉。

    【为什么抹除在前、页面动作在后】两条抹除链路的 ok=False 都承诺图片未被改动，放在上传与
    勾选之前，失败时页面零改动，与「交人工」的现状完全一致。
    【上传前必须再复检一次，且查的是收口后的字节】只有 erase_marks 自带 _recheck
    （= check_cleaned_twice），erase_text_lines 不自带（所以备料段那条材质行救援也是自己
    再查一遍）；而 _to_carousel_size 还可能补白/重编码。查收口后的产物，才等于「勾上去的
    是什么就验的是什么」——这是本阶段最后一道能拦住脏图的门，放行口径与别处一致：两票都判干净。
    【成功信号只认返回值】失败分支会把原图拷到 out_path，光看文件在不在会误判成功。
    """
    erased = os.path.join(prep, f"rescue-{item['i']:02d}.jpg")
    if route == "brand":
        result = await text_erase.erase_marks(page_path, texts, out_path=erased)
    else:
        result = await text_erase.erase_text_lines(page_path, texts, out_path=erased)
    if not result.get("ok"):
        return {"ok": False, "why": result.get("why") or "定点抹除失败"}
    # 【用返回值、别假定写了 out_path】_to_carousel_size 对已经合规的图直接返回入参、不写盘
    fixed = _to_carousel_size(erased, os.path.join(prep, f"rescue-{item['i']:02d}-s.jpg"),
                              preserve_info=True)
    if not fixed:
        return {"ok": False, "why": "尺寸/体积处理失败"}
    verified = await vision.check_cleaned_twice(fixed)
    if verified.get("clean") is not True:
        return {"ok": False,
                "why": f"抹除后复检未过：{verified.get('issues') or '无结论'}"[:120]}
    return await _swap_one(session, item["url"], fixed)


async def _drop_cell(session: BrowserSession, url: str) -> str:
    """把一格从轮播选用里取消掉（定点抹除救不回来时的放弃处置）。空串＝取消成功。

    【为什么不复用 _apply_drops】它按整批 dropped 重跑：已经取消过的条目重读后会「取不到
    那一格」，被再记一次 still，而它在收尾复检之前已经跑过一轮。这里一次只处置一格，判据
    与它对齐：取不到格位、取消后会掉到平台下限以下、toggle 失败，三种都算没取消成。
    """
    current = await carousel_state(session)
    items = current.get("items") or []
    hit = next((entry for entry in items
                if entry.get("url") == url and entry.get("checked")), None)
    if hit is None:
        return "页面上找不到该格位"
    if sum(bool(entry.get("checked")) for entry in items) <= CAROUSEL_MIN_PICKED:
        return f"取消后会低于平台下限 {CAROUSEL_MIN_PICKED} 张"
    if (await toggle_carousel(session, hit["i"], False)).get("stage") != "ok":
        return "取消勾选失败"
    return ""


def _kind_of(entry: dict, verdict: dict) -> str:
    """这张图的信息图类目（提示词里给的那个词），两处来路取其一。"""
    return (entry.get("note") or {}).get("kind") or (verdict or {}).get("kind") or ""


# ---- 阶段主体 ---------------------------------------------------------------

async def _st_carousel(ctx: dict, session: BrowserSession, emit) -> dict:
    """补勾关键信息图，上传前带反馈修图并双检，页面复检失败停止发布交人工。
    """
    # 【等图格渲染这件事只在 carousel_state 里做，本阶段不再补等重读】
    # 首跑（2026-09-12~13 夜间批）这里原本是「supported 为 None 就 sleep 2 秒再读一次」，
    # 但本文件没有 import asyncio，那句 sleep 一执行就是 NameError，被上层吞成阶段错误：
    # 补救分支形同虚设，报错落在第一次读的结果上（4 条 no-carousel-list / 零个图格）。
    # 修的时候没有把 import 补上了事——那样等待会分裂成两处（媒体层 8s 轮询 + 阶段层
    # 2s 死等 + 再来一轮 8s 轮询），既是两处各留一半的折中，「无条件 sleep 2 秒」也正是
    # _poll_until 那段注释要消灭的东西。真正的短板是媒体层的 timeout 给紧了，故只在
    # carousel_state 一处把上限调到位（见那边 docstring 的取证），这里退回单次读取。
    #
    # 【先展开候选列表】池子超过约 21 格时页面把后面的折叠掉、格子压根不渲染（真站实测
    # 21 -> 36），不展开就只能看见前 21 张候选——尺码表/产品介绍图若排在后面，永远
    # 补勾不到，而补勾正是本阶段的职责之一。
    #
    # 【没有这一区就先退出，别去展开】展开那步按「查看更多」文案全局找按钮，本类目
    # 没有轮播区时它找的就只能是别处的折叠入口（实测服装类该页 view-more 为 0，点不到
    # 东西，但这依赖于页面别处恰好没有折叠），且展开本身也无意义。故先判「有没有这一区」。
    st = await carousel_state(session)
    if st.get("supported") is False:
        # 【本类目没有产品轮播图区】2026-09-20 实测服装类（682542618799 男童牛仔夹克、
        # 1011826279690 男童牛仔衬衫）：产品信息栏里只有「产品素材图」1 格，没有轮播图
        # 那一区——首屏 Vue 短暂渲染过 18 格，类目一确定就把整个表单项移除，0.6s 起
        # 恒为零且不再回来。这与玩具类（184807703147300533，18 格稳定在）是两种类目形态，
        # 不是渲染慢，故判 skipped 而不是 fail：页面上没有这一区，本阶段无事可做，
        # 也不存在「发布被拒轮播图尺寸」的风险（取向同 ⑦ 的「本类目无 SKC 颜色图位」）。
        await emit({"type": "log", "stage": "carousel",
                    "message": "本类目没有产品轮播图区（只有产品素材图），跳过"})
        return {"status": "skipped", "note": "本类目无产品轮播图区"}
    if st.get("supported") is None:
        # 证据不足（产品信息栏都没渲染完/页面改版）：不当成「不支持」静默跳过，如实报
        # 出来。静默跳过的下场就是首跑那单——发布时才被拒，回头还得从日志里反推。
        return {"status": "fail",
                "note": f"读不到产品轮播图区（{st.get('err') or '零个图格'}），"
                        "无法核对尺寸"}
    ex = await expand_carousel_pool(session)
    if ex.get("expanded"):
        await emit({"type": "log", "stage": "carousel",
                    "message": "候选列表有折叠，已展开，按全部候选判定"})
        st = await carousel_state(session)

    items = st.get("items") or []
    picked = [it for it in items if it.get("checked")]
    cands = [it for it in items if not it.get("checked")]
    replacements_path = os.path.join(ctx["workdir"], "carousel-replacements.json")
    try:
        with open(replacements_path, encoding="utf-8") as source:
            replacements = json.load(source)
        if not isinstance(replacements, dict):
            replacements = {}
    except (OSError, ValueError):
        replacements = {}
    selected_urls = {item["url"] for item in picked}
    cands = [item for item in cands
             if replacements.get(item["url"]) not in selected_urls]

    info = state._load_info(ctx["info_path"]) if ctx.get("info_path") else {}
    prep = os.path.join(ctx["workdir"], "carousel")
    shutil.rmtree(prep, ignore_errors=True)
    os.makedirs(prep, exist_ok=True)
    pool = await _resolve_pool(items, prep, emit)

    # ---- 判定：先挑补勾的信息图，再给出选用图的质检结论 ----
    # 【判定调用失败如实抛出去】判不了＝不知道有没有中文，而中文是硬红线，未知不能
    # 当安全（同 ⑤b「带中文的图不能一路发上真店」）。吞掉异常继续跑等于把「没看」
    # 伪装成「看过且干净」，故这里只补一句上下文再抛，由 service 记 fail 并留现场。
    verdicts = {}
    room = max(0, CAROUSEL_MAX_PICKED - len(picked))
    add_max = room

    async def _scan(targets: list, why: str) -> dict:
        """对一批格子发一次判定，返回 {文件名: 结论}。取不到图的格子不进输入。"""
        entries = [{"file": pool[t["i"]]["file"], "path": pool[t["i"]]["path"]}
                   for t in targets if t["i"] in pool]
        entries = [e for e in entries if os.path.isfile(e["path"])]
        if not entries:
            return {}
        try:
            r = await vision.plan_carousel(entries, info)
        except Exception as e:
            raise RuntimeError(f"轮播图{why}失败：{e}") from e
        return r.get("items") or {}

    if cands:
        verdicts.update(await _scan(cands, "信息图判定"))
    wanted = _pick_adds(cands, pool, verdicts, picked, len(cands))
    adds = wanted[:add_max]
    incomplete = [f"第 {it['i'] + 1} 张候选图未完成识别" for it in cands
                  if it["i"] not in pool or pool[it["i"]]["file"] not in verdicts]
    # 【放不下的信息图单独一类，不能混进 unknown】它不是「读不出结论」——结论读得很
    # 清楚（是信息图、价值几分），只是选用位已被占满、本阶段无位可补。混进 unknown 的
    # 代价：上报讲成「N 张读不出结论」，用户照这条只会去查图片/模型，而真正该做的是
    # 「取消一张次要图腾个位」；1040482047185 与 1005064778878 两单的「2 张读不出结论」
    # 全是这么来的（日志里 28/28、9/9 张都有结论）。
    overflow = [f"第 {it['i'] + 1} 张" for it, _ in wanted[add_max:]]

    target = [{"i": it["i"], "add": False} for it in picked] + \
             [{"i": it["i"], "add": True} for it, _ in adds]
    if not target:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "轮播图区没有可选用的图片，需人工补图"})
        return {"status": "fail", "note": "轮播图区没有可选用的图片，需人工补图"}

    # ---- 备料：脏的英化、不合规的裁方 ----
    ready, unknown, failed_adds, failed_kept = [], incomplete, [], []
    # 失败已选图的源 URL：换图/放弃都得靠它回页面定位（格位下标在插入后全作废）
    failed_kept_url: dict = {}
    for t in target:
        i = t["i"]
        entry = pool.get(i)
        tag = f"第 {i + 1} 张"
        if not entry:
            unknown.append(tag)
            continue
        verdict = verdicts.get(entry["file"]) or {}
        try:
            qc = await vision.check_cleaned_twice(entry["path"])
        except Exception as exc:
            unknown.append(f"{tag}英化质检异常：{exc}")
            continue
        dirty = (not qc["clean"] if isinstance(qc.get("clean"), bool)
                 and qc.get("status") != "error" else None)
        kind = _kind_of(entry, verdict)
        sizechart = "尺" in kind
        # 【尺寸判据取本地件的真实像素，不取页面上的 .img-size 文本】文本读不到时是
        # 「未知」，而下载件才是我们真正要上传的那个东西；读不出尺寸同样按未知处理
        # （未知不等于不合格，同 carousel_state 的取向）。
        sz = images.image_size(entry["path"])
        need_size = bool(sz) and (sz[0] != sz[1]
                                  or min(sz) < CAROUSEL_MIN_SIDE)

        if dirty is None:
            unknown.append(tag)
            continue
        # 【补勾的图必须走完】它的目的就是出现在轮播里，干净合规也得上传后勾选；
        # 已选用且干净合规的图才不动它（省一次上传与替换，画面本来就没变）。
        if not sz:
            unknown.append(f"{tag}读不到图片尺寸")
            continue
        if not dirty and not need_size and not t["add"] \
                and os.path.getsize(entry["path"]) <= CAROUSEL_MAX_BYTES:
            # 【「不上传」不等于「不再验」】这种格子不重传（画面本来就没变），但照样要过
            # 收尾复检——判的就是页面上那份字节，多一道漏判兜底（2026-10-09 定案，理由
            # 见收尾复检那段的注释）。
            continue

        path = entry["path"]
        if dirty:
            await emit({"type": "log", "stage": "carousel",
                        "message": f"轮播图 {tag} 英化质检未通过"
                                   f"（{qc.get('issues') or ''}），"
                                   "生图英化后替换"})
            en = await _english_one(path, entry["url"], ctx["workdir"], sizechart,
                                    initial_qc=qc)
            if not en.get("ok"):
                logger.warning(f"轮播图 {tag} {en.get('why')}")
                await emit({"type": "manual_check", "stage": "carousel",
                            "message": f"轮播图 {tag} 英化失败，保留现场交人工：{en.get('why')}"})
                (failed_adds if t["add"] else failed_kept).append(tag)
                if not t["add"]:
                    failed_kept_url[tag] = entry["url"]
                continue
            path = en["path"]
        out_path = os.path.join(prep, f"pic{i:02d}.jpg")
        fixed = _to_carousel_size(path, out_path, preserve_info=True)
        if not fixed:
            await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"轮播图 {tag} 尺寸/体积处理失败，保留原图交人工"})
            (failed_adds if t["add"] else failed_kept).append(tag)
            if not t["add"]:
                failed_kept_url[tag] = entry["url"]
            continue
        ready.append({"i": i, "tag": tag, "path": fixed,
                      "sourceUrl": entry["url"],
                      "oldUrl": None if t["add"] else entry["url"],
                      "add": t["add"]})

    # ---- 英化失败的已选图：换一张干净候选顶上，换不上就取消它 ----------------
    # 【为什么不再一律交人工】原先失败即整单「发布可能被拒」，而这类失败的病根多半在
    # 某一张图的画面里：2026-10-09 1048494210610 第 1 张的「荆牌」角标压在渐变顶栏上，
    # 生图三发纹丝不动、定点抹除又栽在底色不纯。用户 2026-10-09 定案：这类图不重要就
    # 可以放弃、换别的图，别让一张脏图卡死整单。
    # 【换谁】只用判过 chinese=false 的干净候选（理由见 _pick_spares）：零成本、确定性。
    # 一张都没有时不硬凑，退到「取消该图」——取消后仍不低于平台下限才做，否则保留原状
    # 交人工（宁可交人工，也不把选用数压到不合规）。
    # 【怎么换】补位项做成 add=False + oldUrl=失败图 URL，直接复用既有的「插新图、再
    # 取消旧图」流程：选用数全程在满位附近小幅动，不会像「先全取消再插」那样先掉一大截。
    spares = []
    if failed_kept:
        refs = {_fingerprint(pool[it["i"]]["path"])
                for it in (list(picked) + [it for it, _ in adds])
                if it["i"] in pool}
        taken = {it["i"] for it, _ in adds}
        spares = _pick_spares(cands, pool, verdicts, refs, taken,
                              min(len(failed_kept), len(cands)))
    for tag, (it, entry) in zip(list(failed_kept), spares):
        i = it["i"]
        if tag not in failed_kept_url:
            continue
        # 【补位图也要过完整双检】_pick_spares 用的 verdict 是 plan_carousel 的粗判
        # （它自己的 docstring 就写明「最终选用图另由 check_cleaned 执行完整英化质检」），
        # 拿粗判当合格证会把一张没验过的图勾上去、同时把原来那张取消掉——比不换更糟。
        try:
            qc = await vision.check_cleaned_twice(entry["path"])
        except Exception as exc:
            logger.warning(f"轮播图第 {i + 1} 张候选质检异常，顶不上 {tag}：{exc}")
            continue
        if qc.get("status") == "error" or qc.get("clean") is not True:
            logger.warning(f"轮播图第 {i + 1} 张候选质检未过，顶不上 {tag}："
                           f"{qc.get('issues') or '无结论'}")
            continue
        fixed = _to_carousel_size(entry["path"],
                                  os.path.join(prep, f"pic{i:02d}.jpg"),
                                  preserve_info=True)
        if not fixed:
            logger.warning(f"轮播图第 {i + 1} 张候选尺寸/体积处理失败，顶不上 {tag}")
            continue
        ready.append({"i": i, "tag": f"第 {i + 1} 张", "path": fixed,
                      "sourceUrl": entry["url"],
                      "oldUrl": failed_kept_url[tag], "add": False})
        failed_kept.remove(tag)
        await emit({"type": "log", "stage": "carousel",
                    "message": f"轮播图 {tag} 英化失败，改用画面干净的候选"
                               f"第 {i + 1} 张顶上"})

    # ---- 没有干净候选可顶的失败图：取消勾选（用户 2026-10-09 定案「可以放弃」）----
    # 【宁可交人工也不越下限】取消后若会掉到平台下限以下，就不动它、按原样报人工：
    # 少一张合规图和「选用数不合规」是两种不同程度的坏。
    dropped = []
    if failed_kept and len(picked) - len(failed_kept) >= CAROUSEL_MIN_PICKED:
        dropped = [{"tag": tag, "url": failed_kept_url[tag]}
                   for tag in failed_kept if tag in failed_kept_url]
        failed_kept = [tag for tag in failed_kept if tag not in failed_kept_url]
        await emit({"type": "log", "level": "info", "stage": "carousel",
                    "message": f"{len(dropped)} 张英化失败的已选图没有干净候选可换"
                               f"（{'、'.join(d['tag'] for d in dropped[:4])}），"
                               "按「不重要就放弃」取消勾选，本商品轮播图相应少几张"})

    # 计划中的最终勾选集合。定义提到这里是为了让下面「没有图要上传」的早退分支也能
    # 用上 _apply_drops（那一支同样可能带着 dropped）。
    expected_urls = {it["url"] for it in picked}

    async def _apply_drops() -> list:
        """把决定放弃的失败图从选用里取消掉，返回没能取消成的 tag。

        【为什么每次重读页面】取消会让后续格位下标全部作废，与替换流程同一条理由。
        【为什么可能取消不成】取不到那一格、或取消后会掉到平台下限以下，都原样留着
        交人工——少一张合规图与「选用数不合规」是两种不同程度的坏。
        """
        still = []
        for item in dropped:
            current = await carousel_state(session)
            items_now = current.get("items") or []
            hit = next((entry for entry in items_now
                        if entry.get("url") == item["url"] and entry.get("checked")),
                       None)
            if hit is None:
                # 【取不到那一格也算没取消成】docstring 早就写了「取不到那一格……原样留着
                # 交人工」，代码原先却直接 continue：既不算进 still（alive 白少算一张），
                # 也不留痕——读页面失败时 items_now 为空，所有 dropped 全走这一支，
                # 早退分支里因为没有收尾比对，会把「一张都没取消成」当成功报出去。
                still.append(item["tag"])
                continue
            if sum(bool(entry.get("checked")) for entry in items_now) <= CAROUSEL_MIN_PICKED:
                still.append(item["tag"])
                continue
            result = await toggle_carousel(session, hit["i"], False)
            if result.get("stage") != "ok":
                still.append(item["tag"])
                continue
            expected_urls.discard(item["url"])
            await emit({"type": "log", "stage": "carousel",
                        "message": f"已取消 {item['tag']} 的勾选"
                                   "（原图不合规、且没有干净候选可换）"})
        return still

    if unknown:
        # 尺寸/中文都判不了（图未加载、源图取不到、模型漏答）的格子不硬判，
        # 但要说出来：它可能藏着不合规或带中文的图，而本阶段确实没依据去动它。
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(unknown)} 项轮播图检查未完成"
                               f"（{'、'.join(unknown[:6])}），"
                               "未通过处理，暂停发布并交人工核对"})

    if overflow:
        await emit({"type": "log", "level": "info", "stage": "carousel",
                    "message": f"按 {CAROUSEL_MAX_PICKED} 张选用上限，计划补勾 {len(adds)} 张信息图；"
                               f"另有 {len(overflow)} 张候选信息图因选用位已满未纳入本轮"
                               f"（{'、'.join(overflow[:6])}），"
                               "不影响已选图片的合规判定"})

    if failed_adds:
        # 【提示但不硬失败】补勾失败与 overflow 是同一类「没能更好」：图没上到页面，
        # 页面状态=压根没打算补，仍是一个可发布状态（理由见本阶段末尾 blocked 那段）。
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(failed_adds)} 张信息图处理失败、未补勾"
                               f"（{'、'.join(failed_adds[:4])}），已跳过继续，"
                               "不影响已选图片的合规判定；想补需人工加图"})

    if not ready:
        # 【选用位满导致的 overflow 与补勾失败都不阻断】选用图本身全部合格、数量也合规，
        # 页面这就是一个可发布状态；能补的信息图补不进去只是「没能更好」，不是「不能发」。
        # 把它算进 fail 等于让一单本可落库的商品停在人工队列里（这正是 1040482047185
        # 那单的处境）。
        # 【没有图要上传不等于没有页面改动要做】dropped 里的失败图正等着被取消勾选，
        # 取消成了就不该再按「原图仍在选用中」报 fail，故先落地、再按落地后的选用数算。
        still = await _apply_drops()
        # 【取消不成的并回失败清单】与替换后那条路径（见「落地放弃项」那段）同一个口径，
        # 免得同一个 _apply_drops 在两处调用点有两种记账。
        # 说清楚现状：这一支【今天走不到 still 非空】——走到这里说明没有任何图备料成功，
        # 而那只能是所有已选图都失败了，于是上面 `len(picked) - len(failed_kept) >= 3`
        # 不成立、dropped 恒空、still 也恒空。留着这行是为了将来哪天这一支能带上 dropped
        # 时，不会出现「一张已知不合规的图仍留在选用里、却被算成取消成功」而报 ok。
        failed_kept.extend(still)
        alive = len(picked) - len(dropped) + len(still)
        status = "fail" if (failed_kept or unknown
                            or not CAROUSEL_MIN_PICKED <= alive <= CAROUSEL_MAX_PICKED) else "ok"
        note = f"轮播图已选 {alive} 张"
        if unknown:
            note += f"；{len(unknown)} 张读不出结论"
        if overflow:
            note += f"；{len(overflow)} 张信息图因选用位已满未补勾"
        if failed_kept:
            note = (f"{len(failed_kept)} 张已选轮播图未通过处理"
                    f"（{'、'.join(failed_kept[:4])}），原图仍在选用中，"
                    "发布会被拒，请人工换图")
        elif failed_adds:
            note += f"；{len(failed_adds)} 张信息图未能补勾"
        if not CAROUSEL_MIN_PICKED <= alive <= CAROUSEL_MAX_PICKED:
            note += "；选用数量不满足平台要求，需人工补选/取消"
            await emit({"type": "manual_check", "stage": "carousel", "message": note})
        return {"status": status, "note": note[:200]}

    # ---- 直传图床：串行保序，与 ⑦ 同一理由（空间弹窗按入库时间排序）----
    up = await upload_many(session, [r["path"] for r in ready],
                           min_w=CAROUSEL_MIN_SIDE, min_h=CAROUSEL_MIN_SIDE)
    uploaded = up.get("uploaded") or []
    if not uploaded:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(ready)} 张轮播图产物直传全失败，仍是原图"
                               "（发布会被拦，请人工换图）"})
        return {"status": "fail", "note": f"轮播图直传全失败：{str(up)[:150]}"}
    # 【按 path 剔除失败项，不能 zip 截断】upload_many 的 uploaded 只含成功项：
    # 直接 zip(ready, uploaded) 会把后一张的 fileId 安到前一张头上，再按 ready 的长度
    # 截尾，于是「该换的没换、不该取消的被取消」。按失败的 path 保序剔除才对得上。
    failed_paths = {f.get("path") for f in (up.get("failed") or [])}
    failed_up = [r["tag"] for r in ready if r["path"] in failed_paths]
    ready = [r for r in ready if r["path"] not in failed_paths]
    if len(ready) != len(uploaded) or any(not item.get("fileId") for item in uploaded):
        return {"status": "fail", "note": "轮播图上传回执不完整，停止替换并交人工核对"}
    for r, u in zip(ready, uploaded):
        r["fileId"] = u["fileId"]
    if failed_up:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(failed_up)} 张轮播图产物直传失败"
                               f"（{'、'.join(failed_up[:4])}），保持原图，请人工换图"})
    if not ready:
        return {"status": "fail", "note": "轮播图产物直传全失败，保持原图"}

    # ---- 分批把新图选进候选列表：每轮只插「还剩几个选用位」那么多张 ----
    # 【为什么不能一次全插】2026-09-17 真站实测（1005064778878）：空间弹窗点「确定」后
    # 新图【自动进入选用态】，而「最多选用 10 张」平台在插入那一刻就卡死——已选 5 张时
    # 一次插 9 张，页面弹「最大支持10张图片,上传成功5张!」，多出的 4 张【连候选列表都
    # 没进】，本阶段按 fileId 认不到，只能逐张报「未补勾」（那单 4 张信息图就是这么丢
    # 的，而日志里只留下一句 toast）。
    # 【为什么不改成「先把要替换的旧图全取消、再一次性插」】那样选用数会先掉一大截
    # （4 张替换让它从 5 掉到 1），插入或认图任一步失败，页面就停在「选用数低于下限
    # 3 张」，比原先「旧图还在、只是不合规」更糟。分批则让选用数始终在满位附近小幅
    # 动：插满位 -> 取消刚被顶替的旧图 -> 再插下一批。
    # 【满位时先取消一张旧图腾位】已选 10 张（上一轮刚补满就是这样）时 room=0，不腾位
    # 这一批就会被平台整批丢掉；取消一张待替换项的旧图、本轮立刻把新图插回来，选用数
    # 最多只低一张。
    ok_tags, added, freed = [], [], {}
    pending = sorted(ready, key=lambda entry: entry["add"])

    async def _restore(item: dict, items: list) -> None:
        """把腾位时取消掉的旧图勾回去（新图没进列表或勾不上时），别让选用数白掉一张。"""
        if item["sourceUrl"] not in freed:
            return
        old = next((entry for entry in items
                    if entry.get("url") == item["oldUrl"]), None)
        if old is not None and not old.get("checked"):
            await toggle_carousel(session, old["i"], True)
        freed.pop(item["sourceUrl"], None)

    while pending:
        current = await carousel_state(session)
        current_items = current.get("items") or []
        if not current_items:
            return {"status": "fail",
                    "note": f"选图前读不到轮播图区（{current.get('err') or '零个图格'}），"
                            "无法核对替换结果"}
        room = CAROUSEL_MAX_PICKED - sum(bool(entry.get("checked"))
                                         for entry in current_items)
        if room <= 0:
            waiting = next((entry for entry in pending if not entry["add"]
                            and entry["sourceUrl"] not in freed), None)
            if waiting is None:
                # 位子满了又没有可腾位的替换项：剩下的全是补勾项，如实报出来不硬挤
                for entry in pending:
                    (failed_adds if entry["add"] else failed_kept).append(entry["tag"])
                break
            old_item = next((entry for entry in current_items
                             if entry.get("url") == waiting["oldUrl"]
                             and entry.get("checked")), None)
            result = ({} if old_item is None
                      else await toggle_carousel(session, old_item["i"], False))
            if result.get("stage") != "ok":
                failed_kept.append(waiting["tag"])
                pending = [entry for entry in pending if entry is not waiting]
                continue
            freed[waiting["sourceUrl"]] = waiting["oldUrl"]
            continue

        batch, pending = pending[:room], pending[room:]
        opened = await open_carousel_space(session)
        if opened.get("err") or not opened.get("opened"):
            await emit({"type": "manual_check", "stage": "carousel",
                        "message": "打不开轮播图「选择图片」弹窗，这一批图没能替换/补勾"
                                   "（发布会被拦，请人工核对勾选）"})
            return {"status": "fail",
                    "note": f"打开选图弹窗失败[{opened.get('stage') or '?'}]："
                            f"{opened.get('err') or ''}"[:200]}

        picked_res = await media_space._pick_many_from_space(
            session, [entry["fileId"] for entry in batch])
        if picked_res.get("err"):
            # 弹窗可能还开着挡住后续操作，尽力关掉（best-effort，失败不影响错误返回）
            await media_space._close_space_modal(session)
            await emit({"type": "manual_check", "stage": "carousel",
                        "message": "在图片空间里选不中新传的轮播图，这一批图没能替换/补勾"
                                   "（发布会被拦，请人工核对勾选）"})
            return {"status": "fail",
                    "note": f"空间选图失败[{picked_res.get('stage') or '?'}]："
                            f"{picked_res.get('err') or ''}"[:200]}

        # ---- 插完重读一次：新图进了列表，旧下标全部作废 ----
        # 新图按 fileId 文件名在列表里认（同 _pick_from_space：弹窗与列表的 URL 前缀
        # 不同但文件名一致）。认不到就不能瞎勾——宁可保留原图报人工。
        for item in batch:
            current = await carousel_state(session)
            current_items = current.get("items") or []
            file_id = item["fileId"].rsplit("/", 1)[-1]
            hit = next((entry for entry in current_items
                        if file_id in (entry.get("url") or "")), None)
            old_item = next((entry for entry in current_items
                             if entry.get("url") == item["oldUrl"]
                             and entry.get("checked")), None)
            failures = failed_adds if item["add"] else failed_kept
            # 腾过位的替换项，它的旧图【本来就已取消】，不能再按「旧图还在选用中」判失败
            spared = item["sourceUrl"] in freed
            if hit is None or (not item["add"] and old_item is None and not spared):
                await _restore(item, current_items)
                failures.append(item["tag"])
                continue
            if not hit.get("checked"):
                if sum(bool(entry.get("checked"))
                       for entry in current_items) >= CAROUSEL_MAX_PICKED:
                    await _restore(item, current_items)
                    failures.append(item["tag"])
                    continue
                result = await toggle_carousel(session, hit["i"], True)
                if result.get("stage") != "ok":
                    await _restore(item, current_items)
                    failures.append(item["tag"])
                    continue
            added.append((item, hit))
            if old_item is not None:
                result = await toggle_carousel(session, old_item["i"], False)
                if result.get("stage") != "ok":
                    failures.append(item["tag"])
                    continue
            expected_urls.discard(item["oldUrl"])
            expected_urls.add(hit["url"])
            replacements = {source: hit["url"] if destination == item["sourceUrl"] else destination
                            for source, destination in replacements.items()}
            replacements[item["sourceUrl"]] = hit["url"]
            ok_tags.append(item["tag"])

    # ---- 落地放弃项：没有干净候选可顶的失败图，在这里取消勾选 ----
    # 【为什么放在替换循环之后】先把要插的图插完再取消，与替换项「插新图、再取消旧图」
    # 同一个次序理由：选用数全程不掉到下限以下。取消不成的并回失败清单交人工。
    failed_kept.extend(await _apply_drops())

    # ---- 收尾复核：勾选数与残留不合规图 ----
    st3 = await carousel_state(session)
    picked3 = [it for it in (st3.get("items") or []) if it.get("checked")]
    with open(replacements_path, "w", encoding="utf-8") as destination:
        json.dump(replacements, destination, ensure_ascii=False, indent=2)
    bad3 = [it for it in picked3 if it.get("bad")]
    if {it.get("url") for it in picked3} != expected_urls:
        unknown.append("最终勾选结果与计划不一致")
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": "轮播图最终勾选结果与计划不一致，暂停发布并交人工核对"})
    mixed_released = 0
    # 收尾复检里判为「可定点抹除类」的格子：这一段只收集，页面动作留到循环之后统一做。
    # 【为什么不在循环里就地改页面】插入/取消会让后续格位的下标全部作废（同主循环的取证），
    # 而这一段后面还要拿 picked3 这个快照算账；分开之后快照先用完、账目集中在改完后重算。
    rescue_targets = []
    # 【每个已选用的格子都查，不再跳过「备料段已放行、无需重做」的那些】2026-10-09 用户定案。
    # 那些格子判的虽说是同一份字节，但备料段那两票与这里这两票是两次独立判定，而出事的形态
    # 恰恰是「备料两票判干净、上线后才看出问题」（抖动取证见 _confirm_page_qc）。代价是每单
    # 多几次下载与两倍视觉调用，换掉的是「漏判没有第二道关口」这件事。
    for item in picked3:
        path = os.path.join(prep, f"final-{item['i']:02d}.jpg")
        # 【编号是候选格位号】item["i"] 来自 carousel_state 的图格序号（与上面
        # final-{i:02d}.jpg 同一套命名口径），不是「选用位第几张」——选用位是 1..10
        # 的勾选顺序，格位号才是页面上那张图的位置，报给用户要能对上页面。
        tag = f"第 {item['i'] + 1} 张"
        released = False
        try:
            downloaded = await asyncio.to_thread(extract._download_image, item["url"], path)
            qc = await vision.check_cleaned(path) if downloaded else {}
            # 【复问口径见 _confirm_page_qc】固定问两次、两票都判坏才拦（对称口径）。
            # 这道判定对的是【备料段刚双检放行、原样上传的同一份字节】，单票判坏的
            # 误报在 2026-10-08 那单上连兑三次（复问 2~3 次全干净），而同一套质检又
            # 漏报过整页中文——取舍与取证都写在 _confirm_page_qc 的 docstring 里。
            if downloaded:
                qc, released = await _confirm_page_qc(path, qc, tag)
        except Exception as exc:
            qc, released = {"issues": str(exc)}, False
        if released:
            mixed_released += 1
        if vision.qc_failed(qc):
            # 【「没读到结论」与「模型判它脏」分开报】两者的处置完全不同：前者是质检
            # 环节没给出结论（重跑 / 查链路），后者才是图的问题（去看图、换图）。
            # 2026-10-08 那次复检两问都撞上响应不完整，却报成「1 张未换成合规图…请
            # 人工换图」，用户照着一批本来就干净的图去查。这一支仍按 fail 拦（未知在
            # 这条红线上不能当安全），只是把它并进 unknown、按「读不出结论」报。
            if vision.qc_unread(qc):
                # 这一支【不删 -en 缓存】（下面 failed_kept 那支才删）：没读到结论不等于
                # 图有问题，重跑该直接复用那份已双检放行的产物，不该白烧一发生图。
                unknown.append(tag)
                await emit({"type": "manual_check", "stage": "carousel",
                            "message": f"轮播图 {tag} 替换后英化复检两次都没读到结论"
                                       f"（{qc.get('issues') or '响应不完整'}），"
                                       "未通过处理，暂停发布并交人工核对"})
                continue
            # 【可定点抹除的那两类先救，不直接交人工】品牌标与材质行都是「贴在画面上的一块
            # 标记」：生图只会照抄、带反馈重生多少次都一样，而收尾复检是最后一道关口——
            # 这一段的判定是整条链路上唯一还会回头看它的地方。判据与备料段的救援同源。
            route, texts = _erase_route(qc)
            if route:
                rescue_targets.append((item, path, tag, route, texts))
                continue
            # 【同一格位会被报两次】备料段英化失败的图仍留在选用里（不替换、原图不动），
            # 收尾复检遍历已选图时它还在，于是同一张再报一次；汇总处按格位去重。
            failed_kept.append(tag)
            for source_url, destination_url in replacements.items():
                if destination_url == item.get("url"):
                    cached = _en_cache_path(ctx["workdir"], source_url)
                    if os.path.exists(cached):
                        os.remove(cached)
            await emit({"type": "manual_check", "stage": "carousel",
                        "message": f"轮播图 {tag} 替换后英化复检未通过，停止自动处理并交人工："
                                   f"{qc.get('issues') or '无法取得质检结论'}"})

    # ---- 定点抹除救援：抹掉那处标记，再把页面上那一格换掉 ----
    # 【抹不成按「不重要就放弃」取消该格勾选】用户 2026-10-09 定案：别让一张脏图卡死整单，
    # 取消后仍不低于平台下限才做，取消不成才交人工（与备料段失败图的处置同一取向）。
    rescued_erased, tail_dropped = 0, []
    for item, path, tag, route, texts in rescue_targets:
        # 【三条路都要作废英化缓存】缓存按源 URL 存在 carousel-edit/ 下，不清的话下一轮
        # _english_one 会从缓存里把这份带标记的旧产物再复用一次，原样复发。
        for source_url, destination_url in replacements.items():
            if destination_url == item.get("url"):
                cached = _en_cache_path(ctx["workdir"], source_url)
                if os.path.exists(cached):
                    os.remove(cached)
        rescued = await _erase_rescue(session, item, path, route, texts, prep)
        if rescued.get("ok"):
            rescued_erased += 1
            # 【账目按「源 URL → 最终 URL」记】该格可能是本轮替换出来的（账里有源 URL 指向
            # 它），也可能是沿用上轮产物、账里没有它；后者就以它自己的 URL 为键补一条，
            # 作用是下一轮把这张带标记的旧图挡在候选之外（见本阶段开头读 replacements 那段）。
            sources = [s for s, d in replacements.items() if d == item.get("url")]
            if sources:
                for source_url in sources:
                    replacements[source_url] = rescued["url"]
            else:
                replacements[item.get("url")] = rescued["url"]
            expected_urls.discard(item.get("url"))
            expected_urls.add(rescued["url"])
            await emit({"type": "log", "stage": "carousel",
                        "message": f"轮播图 {tag} 的"
                                   f"{'品牌标识' if route == 'brand' else '材质文字'}"
                                   "已定点抹除，重新上传后换图"})
            continue
        why = await _drop_cell(session, item.get("url"))
        if not why:
            tail_dropped.append(tag)
            expected_urls.discard(item.get("url"))
            await emit({"type": "log", "stage": "carousel",
                        "message": f"轮播图 {tag} 抹除换图未成（{rescued.get('why')}），"
                                   "按「不重要就放弃」取消该格勾选，轮播图相应少一张"})
            continue
        failed_kept.append(tag)
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"轮播图 {tag} 抹除换图未成（{rescued.get('why')}），"
                               f"取消该格勾选也不成（{why}），停止自动处理并交人工"})

    # 【改过页面就得重算账】picked3/bad3 是循环前的快照：继续用它会出事——旧图 bad=True
    # 被换成合规图、bad3 仍非空，blocked 会把一单已经修好的商品判 fail；放弃掉的格子仍被
    # 算进选用数，上下限判断随之失真。
    if rescued_erased or tail_dropped:
        st4 = await carousel_state(session)
        picked3 = [it for it in (st4.get("items") or []) if it.get("checked")]
        bad3 = [it for it in picked3 if it.get("bad")]
        if {it.get("url") for it in picked3} != expected_urls:
            unknown.append("最终勾选结果与计划不一致")
            await emit({"type": "manual_check", "stage": "carousel",
                        "message": "轮播图最终勾选结果与计划不一致，暂停发布并交人工核对"})
        with open(replacements_path, "w", encoding="utf-8") as destination:
            json.dump(replacements, destination, ensure_ascii=False, indent=2)
    if len(picked3) < CAROUSEL_MIN_PICKED:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"处理完轮播图只选用 {len(picked3)} 张，低于平台下限 "
                               f"{CAROUSEL_MIN_PICKED} 张，需人工补选"})
    if len(picked3) > CAROUSEL_MAX_PICKED:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"处理完轮播图选用 {len(picked3)} 张，超过平台上限 "
                               f"{CAROUSEL_MAX_PICKED} 张，需人工取消多余勾选"})
    # 【按格位去重再报】同一张不合规图会被备料段（英化失败、原图留在选用里）与收尾
    # 复检段各报一次，不去重时「N 张未换成合规图」的 N 会虚高（2026-10-08 1045936299360
    # 报的「第 3 张、第 3 张」就是同一张被报两次，看着像两张图失败）。
    kept_tags = list(dict.fromkeys(failed_kept))
    if kept_tags:
        await emit({"type": "manual_check", "stage": "carousel",
                    "message": f"{len(kept_tags)} 张已选轮播图仍带中文/不合规"
                               f"（{'、'.join(kept_tags[:6])}），发布可能被拒，"
                               "请人工换图"})

    n_add = sum(1 for r, _ in added if r["add"] and r["tag"] in ok_tags)
    note = (f"替换 {len(ok_tags) - n_add} 张、补勾信息图 {n_add} 张"
            f"（现选用 {len(picked3)} 张，尺寸不合规 {len(bad3)} 张）")
    if unknown:
        note += f"；{len(unknown)} 张读不出结论"
    if mixed_released:
        # 【文案对两次结论的先后不敏感】两支都会走到这里：先坏后好、先好后坏，
        # 只说「两次不一致」就不会把顺序讲反。类别也不提：新口径下放行与类别无关。
        note += f"；{mixed_released} 张复检两次结论不一致，按合格放行"
    if rescued_erased:
        note += f"；{rescued_erased} 张定点抹除后换图"
    if tail_dropped:
        # 放弃是【有意为之的可发布状态】（用户 2026-10-09 定），不进 failed_kept、不阻断，
        # 只在 note 里说清楚少了几张、少的是哪几张
        note += (f"；{len(tail_dropped)} 张抹除换图未成已取消勾选："
                 f"{'、'.join(tail_dropped[:4])}")
    if kept_tags:
        note += f"；{len(kept_tags)} 张未换成合规图：{'、'.join(kept_tags[:4])}"
    if failed_adds:
        note += f"；{len(failed_adds)} 张信息图未补勾"
    if overflow:
        note += f"；{len(overflow)} 张信息图因选用位已满未补勾"
    if failed_up:
        note += f"；{len(failed_up)} 张上传失败"
    # 【不阻断的两类：overflow 与 failed_adds】二者的共同点是「补勾没成」——overflow 是
    # 选用位满没去补，failed_adds 是去补了但没补上，但【图都没上到页面】，页面状态与
    # 「压根没打算补」完全一样：选用图全部合格、数量合规，这就是一个可发布状态。fail
    # 等于让一单本可落库的商品停在人工队列里（理由同上面 not ready 那支的 overflow）。
    # 补勾失败仍走上面那条 manual_check 提示，不再升级成硬失败（用户 2026-10-08 定）。
    blocked = (failed_kept or failed_up or unknown or bad3
               or st3.get("supported") is not True
               or not CAROUSEL_MIN_PICKED <= len(picked3) <= CAROUSEL_MAX_PICKED)
    return {"status": "fail" if blocked else "ok", "note": note[:200]}
