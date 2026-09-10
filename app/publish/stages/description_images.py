"""店小秘发布共用能力：stages.description_images。各来源流程由 workflows/ 独立定义。"""

import asyncio
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
        rr = await desc_replace(session, cur_pos, path, expect_url=mod["url"])
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


async def _prepare_desc_image(workdir: str, rep: dict) -> dict:
    """把一张待替换的描述图备好本地产物，返回 {"ok", "path", "how", "why"}。

    how ∈ cached（复用落盘产物）/ upscaled（纯几何放大）/ edited（生图英化）。
    三条分支的判据与取舍原样保留自原 _st_desc 内联实现——【不要在这里重新发明】：
    needsUpscale 走 compress 不烧生图、质检未过必须删产物、产物一律落 en_path
    （那是缓存键），每一条都是踩过坑换来的，理由见各分支注释。

    抽成独立函数【只为了能并发预热】：本函数不碰浏览器页面（输入是源 URL、输出是
    本地文件），故 N 张可以同时跑；而定位序号与替换必须逐张串行（见
    _resolve_desc_pos）。原实现把两者写在同一个循环里，生图就只能一张一张来——
    单张实测约 35s，10 张串行 350s，是 ⑬ 阶段耗时的主体（2026-08-25 状态文件实测
    971877978455 该阶段 792s）。

    同步阻塞调用（下载/生图/压缩都是 requests 与 curl 子进程）一律过 to_thread：
    并发跑时若直接调用会把事件循环整个占住，等于白并发（⑤b 清理已是这个写法）。
    """
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
        if sz and (sz[0] < images.DESC_MIN_W or sz[1] < images.DESC_MIN_H):
            logger.warning(
                f"描述图落盘产物尺寸不达标（{sz[0]}x{sz[1]}，要求两边 >= "
                f"{images.DESC_MIN_W}），当缓存未命中重做：{os.path.basename(en_path)}")
        else:
            return {"ok": True, "path": en_path, "how": "cached"}

    if rep.get("needsUpscale"):
        # 【只缺像素的图走纯几何放大，不烧生图】plan_desc 判 needsUpscale 的图内容
        # 是干净的（模型本来判 keep），只是尺寸不符合描述图要求过不了保存校验。
        # 走 compress 放大即可：gpt-image-2 每张都是一次付费调用，为「像素不够」
        # 去重画一遍画面既贵又可能改坏内容。也因此不需要 check_cleaned 质检——
        # 画面根本没动过。
        try:
            n = await asyncio.to_thread(extract._download_image, rep["url"], local)
            if not n:
                return {"ok": False, "why": "放大失败：源站取不到原图（404 等）"}
            # 【产物必须落到 en_path】那是缓存键（见 _desc_cache_paths）。若就地
            # 改 local，重跑时 cached 判定看不到产物，每轮都要重新下载再放大一次。
            shutil.copy(local, en_path)
            # 【下限传描述图的 480，不能用 compress 的服装默认值 1340x1785】
            # 2026-08-28：不传的话这里会把 1000x1000 硬放大到 1340x1785，既无必要
            # （描述图只要两边 >= 480）又会插值放大糊掉画面、体积还涨。
            out = await asyncio.to_thread(
                images.compress, en_path, quality=88,
                min_w=images.DESC_MIN_W, min_h=images.DESC_MIN_H)
        except Exception as e:
            # 【单张图下载失败时返回失败而不抛异常】2026-09-02：原先 upscale 分支的
            # _download_image 调用没有被 try-except 包裹，一张 404 就让整个商品失败。
            # 改为返回失败状态，由上层 _replace_round continue 跳过该图、继续处理其余图。
            return {"ok": False, "why": f"放大失败：{e}"[:150]}
        return {"ok": True, "path": out, "how": "upscaled",
                "note": f"{rep.get('reason')} -> {images.image_size(out)}"}

    try:
        # 同包复用，带过浏览器头的下载
        n = await asyncio.to_thread(extract._download_image, rep["url"], local)
        if not n:
            return {"ok": False, "why": "英化失败：源站取不到原图（404 等）"}
    except Exception as e:
        return {"ok": False, "why": f"英化失败：下载原图 {e}"[:150]}

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
    last_issues, cjk_left = "", False
    attempt, tries = 0, DESC_QC_TRIES
    while attempt < tries:
        attempt += 1
        # 【重试要加码提示词，不能原样再发一遍】残留中文说明上一发没把那块文案吃掉，
        # 同样的话再说一次只是赌随机性；把「上一发残留了什么」当成新约束喂回去，
        # 命中率明显高于原样重发（同 llm._JSON_RETRY_HINT 的取向）。
        # 第一发走【翻译优先】而非 DEFAULT_CLEAN_PROMPT 的「移除中文」：描述区这些图是
        # plan_desc 判 replace 的商品图，图上中文多是材质成分/工艺/卖点等有效信息，该翻译
        # 成英文原位保留，不能看到中文就消除（2026-09-03 用户要求）。
        # 尺码表图（plan_desc 标了 sizechart）用专用提示词：额外要求 cm 换算成英寸，
        # 买家按它选码（见 images.SIZECHART_TRANSLATE_PROMPT）。
        base = (images.SIZECHART_TRANSLATE_PROMPT if rep.get("sizechart")
                else images.DEFAULT_TRANSLATE_PROMPT)
        prompt = base
        if attempt > 1 and last_issues:
            prompt = base + stages_cleaning_rules._retry_hint(last_issues, cjk_left)
        try:
            # desc_mode：按描述图口径出图与收尾，不套服装 1340x1785 闸门
            # （见 images.edit_image 的 desc_mode 说明）
            ed = await asyncio.to_thread(images.edit_image, local, prompt=prompt,
                                         out_path=en_path, desc_mode=True)
            qc = await vision.check_cleaned(ed["output"])
        except Exception as e:
            return {"ok": False, "why": f"英化失败：{e}"[:150]}
        if qc.get("clean"):
            return {"ok": True, "path": ed["output"], "how": "edited"}
        # 质检未过的产物必须删掉：留着会被下次重跑（以及下一轮重试）当成
        # 「已通过的缓存」复用——en_path 就是缓存键，见 _desc_cache_paths
        last_issues = qc.get("issues") or ""
        cjk_left = bool(qc.get("residualChinese"))
        # 文字层没清干净（中文残留 or 生图吐了乱码）都给到 DESC_QC_TRIES_TEXT 发，
        # 理由见该常量注释。发数在循环里抬而不是一开始就取大值：只有确实是这两类才
        # 多烧，brokenSubject 照旧 2 发。
        if cjk_left or qc.get("garbled"):
            tries = max(tries, DESC_QC_TRIES_TEXT)
        try:
            os.remove(ed["output"])
        except OSError:
            pass
        if attempt < tries:
            logger.info(f"描述图英化质检未过（{attempt}/{tries}"
                        f"{'，残留中文' if cjk_left else ''}），重烧一发："
                        f"{last_issues[:60]}")
    return {"ok": False, "why": f"英化质检未过：{last_issues}"[:150],
            "residualChinese": cjk_left}


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
    conc = preferences.get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _one(rep: dict) -> tuple:
        async with sem:
            try:
                return rep["url"], await _prepare_desc_image(workdir, rep)
            except Exception as e:
                # _prepare_desc_image 内部已分支吞异常，这里只兜住意料外的（如磁盘满）
                return rep["url"], {"ok": False, "why": f"备料异常：{e}"[:150]}

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
