"""店小秘发布共用能力：stages.description_images。各来源流程由 workflows/ 独立定义。"""

import asyncio
import contextlib
import json
import os
import shutil
from app.logger import logger
from app.publish import extract, images, preferences, vision
from app.publish.browser import BrowserSession
from app.publish.media.description import desc_map
from app.publish.media.description_replace import desc_replace
from app.publish.stages import cleaning_rules as stages_cleaning_rules


# 描述图英化的「出图 + 质检」总发数：质检未过时再烧一发（理由见 _prepare_desc_image）
DESC_QC_TRIES = 2

# 【文字层没清干净的给更多发数】判据是 vision.check_cleaned 的 residualChinese 或
# garbled——这两类都是「这一发生图碰巧没弄好」，正是重试能救的随机失败：
#   - residualChinese：中文是 Temu 最硬的红线，退回原图的代价是这张图既带中文又撞
#     1340×1785 闸门（2026-08-26 实测那批：pos 2/3/5 三张退回原图，desc_save 回读
#     同时报「仍有外链图未转存」和三张破线）。
#   - garbled：生图自己吐出的无意义英文，随机性最强的一类。2026-08-26 ⑤b main-04
#     两发都是这个（「AI英化后英文为无意义拼写」→「疑似乱码/无意义文字」），恰好
#     2 发用完就放弃、那张主图于是以脏图身份参与 ⑥⑦ 选图（vision._dirty_score）。
# brokenSubject 仍只给 DESC_QC_TRIES：模型在改坏商品主体时多烧只会得到另一张坏图，
# 且风险方向相反（宁可退回原图，也不要一张主体被改烂的图上真店）。
DESC_QC_TRIES_TEXT = 4


def _desc_cache_paths(workdir: str, url: str) -> tuple:
    """描述图英化产物的本地缓存路径（原图、英化图），按【源 URL】哈希命名。

    【为什么不用 pos 当键】pos 是描述区里的当前序号，删图后整体前移——同一个
    pos-03 下次可能是另一张图，拿旧产物去替换会张冠李戴。源 URL 是稳定标识。

    2026-08-23 换成 URL 哈希前，产物是 desc-edit/pos-NN-edited.jpg。那批旧文件
    认领不回来（pos 是删图后的序号，反推不出源 URL），会被这里的缓存判定忽略、
    也不会被清理；靠猜的映射把 A 图的产物贴到 B 图上，比重烧一次生图糟得多。
    需要腾空间时人工删 desc-edit/pos-*.jpg 即可。
    """
    import hashlib

    # 【英化产物的扩展名必须是 .jpg】edit_image 收尾会 compress()，它把非 jpg 输入
    # 转成 JPEG q80（控图床体积）并【删掉原 png】。若这里按 -en.png 探测缓存，
    # 那个路径永远不存在，缓存一次都不会命中——白烧生图还看不出问题。
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    base = os.path.join(workdir, "desc-edit")
    return os.path.join(base, f"{h}.jpg"), os.path.join(base, f"{h}-en.jpg")


def _carousel_en_product(workdir: str, url: str) -> str:
    """同一源 URL 在 ⑤c 轮播图阶段已英化过的产物路径；没有就返回空串。

    【为什么会有这张图】⑤c 与 ⑬ 都会把带中文的图送生图英化，而轮播图与描述区常挂
    着同一张源图。两边各按【源 URL 哈希】缓存产物，只是落在不同目录（⑤c 的
    carousel-edit/ 与本阶段的 desc-edit/），彼此不看，于是同一张图被烧两次。
    2026-09-23 在 93 单实测产物里量过：10 单两个阶段都出过图，其中 9 单存在重复
    出图，41 对重复里 37 对【两侧 URL 哈希键完全相同】——即绝大多数重复只差这一次
    跨目录探测，不需要任何画面判重。剩下 4 对是 URL 不同而画面相似，刻意不管：
    那要引入 ahash 或视觉判重，而 ahash 分不清同款不同色（见 extract.dedup_images
    记的两个坑）、视觉判定本身有抖动（见 carousel 复检那段），为一成的残余承担
    这些误伤面不值当。

    【只认逐字符相同的 URL，不做归一】本函数刻意用裸 URL 哈希：⑤c 与 ⑬ 两边的键都是
    这么算的（见 carousel._en_cache_path），要归一得两边一起改，否则新键探不到旧产物。
    跨 CDN 后缀的那种重复由 _cleaned_main_product 那条路覆盖（它比的是归一键）。

    【方向是单向的，只能 ⑬ 探 ⑤c】STAGES 里 ⑤c 在 ⑬ 之前，反向那一刻产物还不存在。
    且口径只有这个方向安全：轮播产物是 1:1、边长 >= 800，必然落在描述图要求的
    比例 0.5~2 与两边 >= 480 之内；反过来描述图可能是超长通栏图，喂给轮播要先过
    _to_carousel_size 才行。

    不在这里做尺寸复核：调用方对自己目录的缓存本来就要复核一遍（check_desc_size），
    复用这张图走的是同一道闸，没有理由另写一份判断。
    """
    import hashlib

    if not workdir or not url:
        return ""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(workdir, "carousel-edit", f"{h}-en.jpg")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except OSError:
        # 探缓存是辅助路径，取不到文件状态一律当未命中（本项目 best-effort 取向）
        return ""
    return ""


def _cleaned_main_product(workdir: str, url: str) -> str:
    """同一张图在 ⑤b 已清理过的主图产物路径（main-NN.jpg）；没有就返回空串。

    【为什么 ⑤c 那条路覆盖不到它】⑤b 清完是直接 shutil.copy 顶替原文件 main-NN.jpg
    （⑥⑦ 按文件名找图，故它不需要 URL 键），于是它的成果没有任何以源 URL 为键的落盘，
    _carousel_en_product 探不到。而 1688 常把同一张图既挂在主图区又放进详情区：⑤b 为
    ⑥⑦ 把它清干净了，⑬ 到点又按描述区的源 URL 重烧一发，两次做的是同一件事。

    【下标对应是可靠的】main-NN.jpg 的 NN 来自 extract 下载主图时的 enumerate(imgs, 1)，
    下载失败那张只 continue、不改后续编号，所以 raw.json 的 images[N-1] 恒等于
    main-NN.jpg 的源 URL。注意字段名是 images 而不是 mainImages——后者只存在于提取期的
    prod 对象上，prod.as_raw() 落盘时已展开成 images（2026-09-23 按 mainImages 读过一次，
    一单都匹配不上）。

    【必须按归一键比，不能比裸 URL】同一张原图在主图区与详情区常带不同的 CDN 尺寸/格式
    后缀（a.jpg_960x960.jpg 与 a.jpg_q75.webp），裸串比会大面积漏判。extract.image_url_key
    就是为这件事写的，主图侧的 main_by_key 也已在用它，这里沿用同一把尺子。

    【三个条件同时成立才复用，缺一不可】判据取 complianceNotes 里那条记录的
    cleaned（⑤b 确实处理过并质检通过）+ clean（当前判定是干净的）+ chinese 不为真。
    只看 cleaned 不够：⑤b 单张失败时【保留原标注、不顶替原文件】（见 cleaning.py 里
    copy 前的 continue），此时 main-NN.jpg 仍是带中文的原图。把它复用到描述区就是让
    中文图直接上真店，而中文是 Temu 最硬的红线、后面没有第二道闸会再拦它。宁可漏一次
    复用白烧一发生图，不可漏一张中文图（同 vision 模块头「拿不准一律交人工」的取向）。

    读不到文件/结构不对一律返回空串当未命中：这是省钱的辅助路径，坏了只是没省下，
    不能影响主流程（本项目 best-effort 取向）。
    """
    if not workdir or not url:
        return ""
    try:
        raw_path = os.path.join(workdir, "raw.json")
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        mains = [u for u in (raw.get("images") or []) if isinstance(u, str)]
        if not mains:
            return ""
        want = extract.image_url_key(url)
        # setdefault：同一张图重复挂在主图区时认第一个，与 extract.main_by_key 同口径
        by_key = {}
        for i, u in enumerate(mains, 1):
            by_key.setdefault(extract.image_url_key(u), f"main-{i:02d}.jpg")
        fname = by_key.get(want)
        if not fname:
            return ""

        info_path = os.path.join(workdir, "product-info.json")
        with open(info_path, encoding="utf-8") as f:
            info = json.load(f)
        notes = (info.get("complianceNotes") or {}).get("files") or []
        note = next((n for n in notes
                     if isinstance(n, dict) and n.get("file") == fname), None)
        if not note:
            return ""
        if not (note.get("cleaned") and note.get("clean")) or note.get("chinese"):
            return ""

        path = os.path.join(workdir, fname)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except Exception as e:
        logger.warning(f"探 ⑤b 已清理产物失败（忽略，照常英化）：{e}")
    return ""


def _desc_geometry_fix(workdir: str, url: str, src: str) -> str:
    """把描述图几何修正到合规（两边 >= 480、比例 0.5~2），返回产物路径。

    只治「画面干净、几何不达标」的图：先按描述图下限放大（compress），再补白边修
    比例（fit_desc_ratio）。全程纯 PIL、无 AI——与 needsUpscale 同一取向：生图既贵
    又可能把一张本来干净的原图改坏。

    产物落 <url哈希>-fit.jpg（与英化产物同目录、同一套 URL 哈希键），存在且复核通过
    就复用。已合规的图原样返回 src，不做任何 IO——绝大多数 keep 图走的就是这一支。
    """
    sz = images.image_size(src)
    # 读不到尺寸按「无法判断」处理（同 check_desc_size 的取向）：宁可原样转存，
    # 也不要把未知判成不合规、白做一遍几何处理
    if not sz or images.check_desc_size(*sz).get("ok") is not False:
        return src
    local, _en = _desc_cache_paths(workdir, url)
    dst = os.path.splitext(local)[0] + "-fit.jpg"
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        dsz = images.image_size(dst)
        if dsz and images.check_desc_size(*dsz).get("ok") is not False:
            return dst
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(src, dst)
    out = images.compress(dst, quality=88,
                          min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
    return images.fit_desc_ratio(out, out)["output"]


async def _rehost_desc_keeps(ctx: dict, session: BrowserSession, mods: list,
                             keep_pos: list, emit) -> dict:
    """把判 keep 的描述图【原图转存到店小秘图床】，返回 {"done", "failed", "skipped"}。

    【为什么必须做这一步】认领时平台把 1688 的描述图【按外链原样】挂在描述区，只有
    被我们替换过的那几张才落到店小秘图床。于是判 keep 的图一直是 cbu01.alicdn.com
    外链，desc_save 回读必然报「仍有外链图未转存」——2026-08-30 实测取证
    （rowid 173539495458369319）：描述区 10 张图【全部】在 cbu01，而当轮计划只删 3
    换 1，剩下 6 张 keep 的外链谁都不会去动，那条告警于是每轮必现。
    原先的解释「未转存＝有图替换失败」只对「替换失败」那一种成因成立，keep 的图从来
    没人管过，属于漏了一条链路，不是替换失败的连带现象。

    转存＝下载原图 + 直传图床 + desc_replace 换成图床地址，【画面一个像素都不动】：
    这些图模型判过是干净的，走生图既贵又可能改坏内容（同 needsUpscale 走纯几何放大
    的取向）。产物落 desc-edit/<url哈希>.jpg 复用同一套缓存键，重跑不重复下载。

    尺寸不达标的不在这里处理：plan_desc 已把它们改判 replace + needsUpscale，走
    放大分支（那条路本来就会转存）。这里只碰真正 keep 的图；万一原图尺寸破线，
    upload_image 的闸门会拒掉，按 best-effort 记一笔保留原样，不拖垮本阶段。
    """
    by_pos = {m.get("pos"): m for m in mods if m.get("pos")}
    todo = [by_pos[p] for p in keep_pos
            if p in by_pos and by_pos[p].get("url")
            and not by_pos[p].get("onDxmHost")]
    if not todo:
        return {"done": 0, "failed": [], "skipped": 0}
    logger.info(f"描述图转存：{len(todo)} 张 keep 的图仍是外链，逐张下载后重挂到店小秘图床")

    # 【下载可以并发，替换必须串行】与 _prewarm_desc_images / _replace_round 同一个
    # 分工：下载不碰页面，desc_replace 要现查 pos。
    conc = preferences.get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _fetch(mod: dict) -> tuple:
        local, _en = _desc_cache_paths(ctx["workdir"], mod["url"])
        async with sem:
            if os.path.exists(local) and os.path.getsize(local) > 0:
                return mod["url"], local, ""
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                n = await asyncio.to_thread(extract._download_image, mod["url"], local)
                if not n:
                    return mod["url"], "", "源站取不到原图（404 等），保留页面上的外链图"
            except Exception as e:
                return mod["url"], "", f"下载原图失败：{e}"[:150]
            return mod["url"], local, ""

    got = dict()
    for url, path, err in await asyncio.gather(*(_fetch(m) for m in todo)):
        got[url] = (path, err)

    done, failed = 0, []
    for mod in todo:
        path, err = got.get(mod["url"], ("", "备料结果缺失"))
        if not path:
            failed.append({"url": mod["url"], "why": err})
            continue
        # 【转存前复核尺寸与比例】判 keep 的依据是 desc_map 当场读到的尺寸，而图没
        # 加载完时读不到（naturalWidth=0 就不标 tooSmall，见 desc_map），那种图会被
        # 判 keep 原样转存；等 desc_save 回读时图已加载、尺寸现形，⑬ 便以「1 张描述图
        # 不符合要求」整单失败——2026-09-10 pdd-917366346213 / pdd-983579420547 两单
        # 报的 750×330、1201×481 就是这一类：两边都够 480，只有比例超。
        # 故这里无条件复核一次：不合规的走纯几何修正（画面不动），合规的原样转
        # （绝大多数图走这一支，只是一次读文件头）。
        fixed = await asyncio.to_thread(_desc_geometry_fix, ctx["workdir"], mod["url"], path)
        if fixed != path:
            logger.info(f"描述图keep转存前做了几何修正（画面未动）："
                        f"{images.image_size(path)} -> {images.image_size(fixed)}")
        cur_pos, perr, fatal = await _resolve_desc_pos(session, mod["url"])
        if perr:
            # 页签被导航走对后续每一张都成立，立刻收工（同 _replace_round 的取向）
            if fatal:
                failed.append({"url": mod["url"], "why": perr})
                await emit({"type": "manual_check", "stage": "desc",
                            "message": f"{perr}；剩余 keep 图未转存，请恢复编辑页后续跑"})
                break
            failed.append({"url": mod["url"], "why": perr})
            continue
        rr = await desc_replace(session, cur_pos, fixed, expect_url=mod["url"])
        if rr.get("status") == "ok":
            done += 1
        else:
            failed.append({"url": mod["url"], "why": str(rr)[:150]})
    if failed:
        logger.warning(f"描述图转存：{done} 张成功，{len(failed)} 张仍是外链"
                       f"（保留原图）：{str(failed[:2])[:200]}")
    else:
        logger.info(f"描述图转存完成：{done} 张 keep 的图已落店小秘图床")
    return {"done": done, "failed": failed, "skipped": 0}


async def _resolve_desc_pos(session: BrowserSession, url: str) -> tuple:
    """按源 URL 查它在描述区【当前】的序号，返回 (pos, err, fatal)。

    fatal=True 表示这个失败对后续每一张都成立（页签被导航走了），调用方该收工而不是
    逐张重试——判据取 desc_map 带回的 navigatedAway 标志而不是错误文案，文案会被截断
    也会被改写（见 _desc_ensure_open 里那段实测记录）。

    【为什么必须重查】plan_desc 出的 pos 是删图【之前】的序号，desc_delete 一执行
    描述区就整体前移，旧 pos 全部失效：越界的报错、没越界的静默替换到别的模块上
    （2026-08-24 实测：删 3 张后按旧 pos 6 替换，描述区只剩 5 个模块）。
    URL 是稳定标识——这与 _desc_cache_paths 用 URL 哈希当缓存键是同一个理由。

    每张替换前都重查一次而不是删完只重查一次：替换本身也会改 src，逐张重查最贴近
    页面实况，代价只是一次 evaluate，与生图开销相比可忽略。
    """
    m = await desc_map(session)
    if m.get("status") != "ok":
        return 0, (m.get("err") or "desc_map 失败")[:200], bool(m.get("navigatedAway"))
    hits = [x["pos"] for x in (m.get("modules") or []) if x.get("url") == url]
    if not hits:
        return 0, "描述区已找不到这张源图（可能已被删除或已替换）", False
    # 同一 URL 出现多次时取最小序号：重复图本该被 plan_desc 判 delete，真漏了也
    # 只是先替换靠前那张，下一轮重查会落到剩下那张，不会错位到别的图上
    return hits[0], "", False


async def _prepare_desc_image(workdir: str, rep: dict, dl_sem=None) -> dict:
    """把一张待替换的描述图备好本地产物，返回 {"ok", "path", "how", "why"}。

    dl_sem 是【只管源站下载】的信号量（出图并发由 images.edit_image_async 的全局闸门
    管，两者各管一段，理由见调用方那段注释）。缺省 None 表示不限流，便于单测直调。

    how ∈ cached（复用落盘产物）/ upscaled（纯几何放大）/ edited（生图英化）。
    三条分支的判据与取舍原样保留自原 _st_desc 内联实现——【不要在这里重新发明】：
    needsUpscale 走 compress 不烧生图、质检未过必须删产物、产物一律落 en_path
    （那是缓存键），每一条都是踩过坑换来的，理由见各分支注释。

    失败时返回 {"ok": False, "kind": ..., "why": ...}，kind ∈ fetch（源站取不到）/
    edit（出图链路报错）/ qc（质检发数用尽仍不过）。调用方按 kind 分流（见
    description._replace_round）：qc 的这张图被判死、直接丢弃；另两类是「条件弄好再来
    一次」，保留原图、重跑即可。2026-09-22 用户定案：最后一律丢弃，不再停下等人工换图。

    抽成独立函数【只为了能并发预热】：本函数不碰浏览器页面（输入是源 URL、输出是
    本地文件），故 N 张可以同时跑；而定位序号与替换必须逐张串行（见
    _resolve_desc_pos）。原实现把两者写在同一个循环里，生图就只能一张一张来——
    单张实测约 35s，10 张串行 350s，是 ⑬ 阶段耗时的主体（2026-08-25 状态文件实测
    971877978455 该阶段 792s）。

    同步阻塞调用（下载/生图/压缩都是 requests 与 curl 子进程）一律过 to_thread：
    并发跑时若直接调用会把事件循环整个占住，等于白并发（⑤b 清理已是这个写法）。
    """
    # dl_sem 为 None 时退化成空上下文，免得两条下载各写一遍 if 分支
    dl_gate = dl_sem if dl_sem is not None else contextlib.nullcontext()
    local, en_path = _desc_cache_paths(workdir, rep["url"])
    if os.path.exists(en_path) and os.path.getsize(en_path) > 0:
        # 缓存命中不再重复质检：check_cleaned 也是一次视觉调用，而落盘的前提就是它已通过。
        # 【但尺寸要复查一次】质检管的是画面内容（残留中文/乱码/主体改坏），管不到
        # 像素数。2026-08-29 实测：源图 80×80 的图英化后落盘 480×480，恰好卡在
        # 描述图下限上——够 480 但比源图放大 6 倍，且历史产物可能是更早的口径出的。
        # 尺寸不达标就当缓存未命中往下走（needsUpscale 分支会放大），而不是把不合格
        # 的产物交给替换去撞上传闸门、最后以「保留原图 + 页面留 1688 外链」收场。
        # 【读不出尺寸时仍按命中处理】把「未知」判成不合格会触发无谓的放大/重新
        # 生图，正是 images.check_desc_size 那段注释要避免的；只有确实读到了
        # 且低于下限才作废。
        sz = images.image_size(en_path)
        # 【比例也要一起复查】产物不合规有两类：像素不够（低于 480）与比例超限
        # （超长通栏图，见 images.fit_desc_ratio）。原先只查前者，比例超限的旧产物
        # 会被当合规缓存复用，替换上去照样被 desc_save 判 tooSmall。
        chk = (images.check_desc_size(*sz) if sz
               else {"ok": None, "reasons": ["宽高读取失败"]})
        if chk.get("ok") is False:
            logger.warning(
                f"描述图落盘产物不合规（{sz[0]}x{sz[1]}：{'、'.join(chk['reasons'])}），"
                f"当缓存未命中重做：{os.path.basename(en_path)}")
        else:
            return {"ok": True, "path": en_path, "how": "cached"}

    # 【本目录未命中时，再探前面阶段已经处理过同一张图的产物】同一张图在 ⑤c（轮播图
    # 英化）或 ⑤b（主图清理）已经烧过一发生图，⑬ 到点又按自己的键重烧一发，两次做的是
    # 同一件事。实测中这是最主要的重复开销，理由与各自的判据见两个探测函数。
    # 顺序上先 ⑤c 后 ⑤b：⑤c 的产物是 1:1、且走的是轮播专用英化提示词，口径与描述图
    # 更接近；⑤b 的产物是按服装 1340×1785 出的主图。两条都按归一/哈希键认图，互不影响。
    # 探测函数传的是【函数本身】而不是调用结果：⑤c 命中时就不必再去读 ⑤b 那两个
    # JSON（raw.json 与 product-info.json 都是每张图探一次，白读没有意义）。
    for stage_name, probe in (("⑤c 轮播图", _carousel_en_product),
                              ("⑤b 主图清理", _cleaned_main_product)):
        reuse = probe(workdir, rep["url"])
        if not reuse:
            continue
        # 【必须过和本目录缓存同一道尺寸闸】两边的产物理论上都落在描述图口径内
        # （轮播 1:1/>=800；主图 1340×1785 比例 0.751），但历史产物可能是更早的口径、
        # 也可能被各自阶段的压缩改过，故不凭推理放行，一律实测一次再用。
        rsz = images.image_size(reuse)
        rchk = (images.check_desc_size(*rsz) if rsz
                else {"ok": None, "reasons": ["宽高读取失败"]})
        if rchk.get("ok") is False:
            logger.warning(
                f"{stage_name}产物不合描述图口径"
                f"（{rsz[0]}x{rsz[1]}：{'、'.join(rchk['reasons'])}），不复用，照常英化")
            continue
        # 【复制到 en_path 而不是直接返回来源路径】en_path 是本阶段的缓存键：不落一份
        # 的话下次重跑这里仍是未命中，还要再探一遍；而来源产物随时可能变——⑤c 的图在它
        # 自己复检未过时会被删掉（carousel 那段 os.remove），⑤b 的 main-NN.jpg 则可能
        # 被后续阶段再次顶替。复制一份等于就地定格，与其它两条分支的行为也一致。
        try:
            os.makedirs(os.path.dirname(en_path), exist_ok=True)
            shutil.copy(reuse, en_path)
        except Exception as e:
            # 复制失败不算失败：照常往下走生图，只是没省下这一发
            logger.warning(f"复用{stage_name}产物时落盘失败（忽略，改为照常英化）：{e}")
            continue
        logger.info(f"描述图复用{stage_name}已处理的产物，省掉一次生图："
                    f"{os.path.basename(reuse)} <- {rep['url']}")
        return {"ok": True, "path": en_path, "how": "cached"}

    if rep.get("needsUpscale"):
        # 【只缺像素的图走纯几何放大，不烧生图】plan_desc 判 needsUpscale 的图内容
        # 是干净的（模型本来判 keep），只是尺寸不符合描述图要求过不了保存校验。
        # 走 compress 放大即可：gpt-image-2 每张都是一次付费调用，为「像素不够」
        # 去重画一遍画面既贵又可能改坏内容。也因此不需要 check_cleaned 质检——
        # 画面根本没动过。
        try:
            async with dl_gate:
                n = await asyncio.to_thread(extract._download_image, rep["url"], local)
            if not n:
                return {"ok": False, "kind": "fetch",
                        "why": "放大失败：源站取不到原图（404 等）"}
            # 【产物必须落到 en_path】那是缓存键（见 _desc_cache_paths）。若就地
            # 改 local，重跑时 cached 判定看不到产物，每轮都要重新下载再放大一次。
            shutil.copy(local, en_path)
            # 【下限传描述图的 480，不能用 compress 的服装默认值 1340x1785】
            # 2026-08-28：不传的话这里会把 1000x1000 硬放大到 1340x1785，既无必要
            # （描述图只要两边 >= 480）又会插值放大糊掉画面、体积还涨。
            out = await asyncio.to_thread(
                images.compress, en_path, quality=88,
                min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
            # 【compress 治不了比例】它只把两边拉到 >= 480，宽高比原样保留：超比例
            # 的长条图（750×330、1201×481）到这里仍是 2.27/2.50，替换上去被 desc_save
            # 判 tooSmall、⑬ 整单失败。故再补一道比例合规化，产物写回 en_path——那是
            # 缓存键，不写回的话重跑时上面那道复核会判它不合规、白重做一遍。
            out = (await asyncio.to_thread(images.fit_desc_ratio, out, en_path))["output"]
        except Exception as e:
            # 【单张图下载失败时返回失败而不抛异常】2026-09-02：原先 upscale 分支的
            # _download_image 调用没有被 try-except 包裹，一张 404 就让整个商品失败。
            # 改为返回失败状态，由上层 _replace_round continue 跳过该图、继续处理其余图。
            return {"ok": False, "kind": "edit", "why": f"放大失败：{e}"[:150]}
        return {"ok": True, "path": out, "how": "upscaled",
                "note": f"{rep.get('reason')} -> {images.image_size(out)}"}

    try:
        # 同包复用，带过浏览器头的下载
        async with dl_gate:
            n = await asyncio.to_thread(extract._download_image, rep["url"], local)
        if not n:
            return {"ok": False, "kind": "fetch",
                    "why": "英化失败：源站取不到原图（404 等）"}
    except Exception as e:
        return {"ok": False, "kind": "fetch", "why": f"英化失败：下载原图 {e}"[:150]}

    # 【质检未过要再烧一发】生图有随机性，同一张图同一个提示词两发结果就不同：
    # 2026-08-26 实测 700640528493 那张 749×513 的面料细节图，第一发被判
    # 「残留英文品牌字 QSMYSTYLE」（衣服上的实物刺绣被质检当成待清理的品牌字），
    # 重烧一发就把底部整条中文说明去干净、刺绣完好、质检通过。一发不中就放弃的代价
    # 不只是这张图退回原图，还连带撞 1340×1785 闸门、在描述区留下 1688 外链
    # （日志末尾那两条 manual_check 就是这么来的）。
    #
    # 【发数按失败类型分档】重试能救的是随机性，救不了确定性失败（字太密、嵌在花纹里
    # 译不干净），而 gpt-image-2 每发都是一次付费生图，故不能一律多烧：
    #   - 残留中文 / 生图乱码 → DESC_QC_TRIES_TEXT 发。中文是 Temu 最硬的红线，退回原图的代价是
    #     连带撞 1340×1785 闸门、在描述区留下 1688 外链（2026-08-26 那批日志末尾
    #     「仍有外链图未转存」+「三张破线」就是三张图退回原图的后果，不是独立故障）。
    #   - 其它 issues（修图痕迹之类）→ DESC_QC_TRIES 发，多烧是同样结果。
    last_issues, cjk_left, claim_left = "", False, False
    banned_left, brand_left, material_left = False, False, False
    attempt, tries = 0, DESC_QC_TRIES
    # 【累积失败历史 + 下一发的加码在这里预备好】加码话术由 vision.build_retry_hint
    # 看着上一发的产物图实时生成，而产物在本次循环末尾就要被删掉（缓存键，见下面
    # os.remove 那段），故生成必须发生在删之前、"下一发"的事在本发末尾一并做掉。
    history, next_hint = [], ""
    while attempt < tries:
        attempt += 1
        # 【重试要加码提示词，不能原样再发一遍】残留中文说明上一发没把那块文案吃掉，
        # 同样的话再说一次只是赌随机性；把「上一发残留了什么」当成新约束喂回去，
        # 命中率明显高于原样重发（同 llm._JSON_RETRY_HINT 的取向）。
        # 【加码话术按图实时生成，不再是四选一的固定模板】固定模板一次只能说一类问题，
        # 而卡住的图常在一张上同时踩好几类（2026-09-22 实测 desc-03：「High-Quality」
        # 该删、「Cotton Denim Fabric」该译、「38 斤」该换算、「FASHION.STREET」该抹），
        # 于是每发只修一块、修完又冒另一块、四发烧完才过。生成式逐块给动作，一次说清。
        # 第一发走【翻译优先】而非 DEFAULT_CLEAN_PROMPT 的「移除中文」：描述区这些图是
        # plan_desc 判 replace 的商品图，图上中文多是工艺/卖点等有效信息，该翻译成英文
        # 原位保留，不能看到中文就消除（2026-09-03 用户要求）。
        # （材质成分 2026-09-25 起从「翻译保留」里移出，由 DEFAULT_TRANSLATE_PROMPT 带的
        # MARK_REMOVE_RULE 讲明要抹掉，理由见 images.DEFAULT_TRANSLATE_PROMPT 那段。）
        # 尺码表图（plan_desc 标了 sizechart）用专用提示词：额外要求 cm 换算成英寸，
        # 买家按它选码（见 images.SIZECHART_TRANSLATE_PROMPT）。
        base = (images.SIZECHART_TRANSLATE_PROMPT if rep.get("sizechart")
                else images.DEFAULT_TRANSLATE_PROMPT)
        prompt = base + next_hint
        next_hint = ""
        try:
            # desc_mode：按描述图口径出图与收尾，不套服装 1340x1785 闸门
            # （见 images.edit_image 的 desc_mode 说明）
            # 超时显式传，不吃默认值：这条原先是四条出图路径里唯一不传的，于是拿到
            # 280 而另三条是 90，同一种调用凭阶段分出两套值（见 images.EDIT_TIMEOUT）
            ed = await images.edit_image_async(local, prompt=prompt,
                                               out_path=en_path, desc_mode=True,
                                               timeout=images.EDIT_TIMEOUT)
            qc = await vision.check_cleaned(ed["output"])
        except Exception as e:
            return {"ok": False, "kind": "edit", "why": f"英化失败：{e}"[:150]}
        if qc.get("clean"):
            return {"ok": True, "path": ed["output"], "how": "edited"}
        # 质检未过的产物必须删掉：留着会被下次重跑（以及下一轮重试）当成
        # 「已通过的缓存」复用——en_path 就是缓存键，见 _desc_cache_paths
        last_issues = qc.get("issues") or ""
        cjk_left = bool(qc.get("residualChinese"))
        claim_left = bool(qc.get("marketingClaim"))
        banned_left = bool(qc.get("bannedTerm"))
        brand_left = bool(qc.get("brandMark"))
        material_left = bool(qc.get("materialText"))
        # 文字层没清干净（中文残留 or 生图吐了乱码）都给到 DESC_QC_TRIES_TEXT 发，
        # 理由见该常量注释。发数在循环里抬而不是一开始就取大值：只有确实是这两类才
        # 多烧，brokenSubject 照旧 2 发。
        # marketingClaim 一并进这一档：CLAIM_REMOVE_RULE 要求的是【抹除】而不是译写，
        # 比翻译容易得多（不必读懂、不必排版），多烧一发命中率明显高；而它漏过去的代价
        # 与中文同级（平台按虚假宣传实罚），不该只给 2 发就退回原图。
        # 禁词一并抬高发数，理由同 marketingClaim（抹除比译写容易，多烧常常就过）
        # 品牌标识与材质说明（2026-09-25 加）也是抹除类，且漏过去的代价是商标侵权与
        # 「图上材质与属性材质不符」两类实罚，同样不该只给 2 发。
        if (cjk_left or qc.get("garbled") or qc.get("marketingClaim")
                or banned_left or brand_left or material_left):
            tries = max(tries, DESC_QC_TRIES_TEXT)
        # 记一笔失败历史：带上该发实际追加的加码话术（prompt 去掉 base 的那部分），
        # 模型才知道自己上次要求过什么，不会重复给一个已经失败过的要求。
        history.append({**qc, "attempt": attempt, "hint": prompt[len(base):]})
        # 【下一发的加码在这里预备，因为产物马上要删】末发仍走固定话术里的「全抹掉」
        # （尺码表豁免）：2026-09-22 实测 desc-03/desc-04 都是前几发全废、靠末发全抹掉
        # 才过，这一发是目前唯一真正有效的兜底，不交给生成式替换。其余各发走按图生成，
        # 它失败（额度/断尾/产物读不到）时落回 _retry_hint 的固定话术——辅助路径
        # best-effort，不能因为一次生成失败就让这张图连原样退回都做不到。
        if attempt < tries:
            if attempt + 1 == tries and not rep.get("sizechart"):
                next_hint = stages_cleaning_rules._retry_hint(
                    last_issues, cjk_left, claim=claim_left, banned=banned_left,
                    mark=brand_left or material_left, last_chance=True)
            else:
                next_hint = await vision.build_retry_hint(
                    base, history, ed["output"],
                    sizechart=bool(rep.get("sizechart")))
                if not next_hint:
                    next_hint = stages_cleaning_rules._retry_hint(
                        last_issues, cjk_left, claim=claim_left, banned=banned_left,
                        mark=brand_left or material_left)
        try:
            os.remove(ed["output"])
        except OSError:
            pass
        if attempt < tries:
            # 脏法要标出来：日志不带就无法从「烧了 4 发还没过」反推当时脏的是哪几类
            flags = "，".join(f for f, v in (("残留中文", cjk_left),
                                            ("夸大宣传", claim_left),
                                            ("平台禁词", banned_left),
                                            ("品牌标识", brand_left),
                                            ("材质说明", material_left)) if v)
            logger.info(f"描述图英化质检未过（{attempt}/{tries}"
                        f"{'，' + flags if flags else ''}），重烧一发："
                        f"{last_issues[:60]}")
    # kind 供调用方分流（见 _replace_round）：「qc」= 发数用尽仍不过，这张图被判死、该丢弃；
    # 「fetch」/「edit」= 源站取不到 / 出图链路报错，图本身没被判死，重跑就该好，仍保留原图。
    return {"ok": False, "kind": "qc", "why": f"英化质检未过：{last_issues}"[:150],
            "residualChinese": cjk_left, "marketingClaim": claim_left,
            "bannedTerm": banned_left, "brandMark": brand_left,
            "materialText": material_left}


async def _prewarm_desc_images(workdir: str, replace_plan: list, emit) -> dict:
    """并发把 replace 计划里每张图的本地产物备好，返回 {url: _prepare_desc_image 结果}。

    【为什么值得单开一轮】生图与页面完全无关，而替换必须串行。先并发烧完再串行替换，
    墙钟从「N × 单张耗时」压到「单张耗时 + N 次替换」。并发数取用户配置的生图并发
    （与 ⑤b 同一个旋钮，本质是同一个 gpt-image-2 端点，见 get_image_concurrency）。

    【best-effort】某张备料失败只记原因，由调用方按原有的 manual_check 路径报出来、
    保留页面原图；本函数不抛，一张烧不出来不该让整个 ⑬ 阶段失败。
    """
    if not replace_plan:
        return {}
    # conc 只用于日志：出图限流已下沉到 images.edit_image_async 的全局闸门。
    # 【为什么这里不再自己建 Semaphore】本轮与 ⑤b 清理常常同时在跑，各建一个计数器
    # 等于把用户设的并发数乘上路数（见 images._EDIT_GATES 那段实测）。
    conc = preferences.get_image_concurrency()

    # 【下载仍要单独限一层】出图闸门管不到 _prepare_desc_image 里那两条
    # extract._download_image（upscale 分支与英化前置下载）：它们打的是 1688 源站，
    # 而源站 CDN 对并发很敏感（见项目已知陷阱：裸 requests 会被连接重置/403）。
    # 原先这两条是顺带受阶段 Semaphore 约束的，闸门下沉后若一并放开，18 张图会同时
    # 砸源站。故这里保留一个只管下载的信号量，与出图闸门各管一段。
    dl_sem = asyncio.Semaphore(conc)

    async def _one(rep: dict) -> tuple:
        try:
            return rep["url"], await _prepare_desc_image(workdir, rep, dl_sem)
        except Exception as e:
            # _prepare_desc_image 内部已分支吞异常，这里只兜住意料外的（如磁盘满）
            return rep["url"], {"ok": False, "kind": "edit",
                                "why": f"备料异常：{e}"[:150]}

    logger.info(f"描述图备料：{len(replace_plan)} 张待处理（并发 {conc}）")
    pairs = await asyncio.gather(*(_one(r) for r in replace_plan))
    out = dict(pairs)
    n_ok = sum(1 for v in out.values() if v.get("ok"))
    n_new = sum(1 for v in out.values() if v.get("how") in ("edited", "upscaled"))
    logger.info(f"描述图备料完成：{n_ok}/{len(replace_plan)} 张就绪"
                f"（新出图/放大 {n_new} 张，其余复用落盘产物）")
    await emit({"type": "log", "stage": "desc",
                "message": f"描述图备料完成 {n_ok}/{len(replace_plan)} 张"
                           f"（并发 {conc}，新处理 {n_new} 张）"})
    return out
