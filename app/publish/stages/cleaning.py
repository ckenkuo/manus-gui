"""店小秘发布共用能力：stages.cleaning。各来源流程由 workflows/ 独立定义。"""

import asyncio
import json
import os
import shutil
from app.logger import logger
from app.publish import claims, images, preferences, state, vision
from app.publish.browser import BrowserSession
from app.publish.media.skc import SKC_ROW_MIN_IMAGES
from app.publish.stages import (
    cleaning_rules as stages_cleaning_rules,
    description_images as stages_description_images,
    prewarm_access as stages_prewarm_access,
)


CLEAN_TIMEOUT = 90      # 单张清理超时（实测一张约 35s）；超了走原图兜底，不拖住整批


def _save_info(info_path: str, info: dict) -> None:
    """回写 product-info.json（best-effort：写坏了不影响当前批次内存里的决策）。"""
    try:
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"回写 product-info.json 失败（忽略）：{e}")


def _fail_message(r: dict) -> str:
    """把一条清理失败翻成【与事实相符】的人工提醒（manual_check 用）。

    【为什么必须分类，不能一句话套所有失败】清理失败有三种，处置完全不同：
    出图那一步失败时图片根本没被碰过、原图原样，重跑即可；质检环节自己报错时
    产物合规与否根本没判过；只有「质检判定不合格」才是产物真带中文/乱码，得人工换图。
    原先三种共用「该图带中文/水印不能发布，请人工换图」，只有第三种说得对——
    2026-09-17 凌晨代理节点 cf.yfjc.sbs 失效那次，11 张图全栽在「出图失败」上，
    却逐张报成「该图带中文不能发布、请人工换图」，用户据此去换一批本就合格的图，
    白费工。

    出图失败里再分「出网链路异常」和「接口/本地报错」：前者重发就可能好，后者
    重跑多少次都是同一个错（判据是 images.TransientNetError，见 _CURL_TRANSIENT_RC）。

    内容审核拒绝（kind="blocked"）单列一类：它既不是链路问题也不是产物不合格，
    重跑无用、换图也无从下手（拒的是这张图本身），处置是「这张图不参与后续选图」，
    已由本阶段自动做掉，故文案讲清「已自动排除、不用处理」。判据是
    images.ModerationBlocked，见那边的实测取证。
    """
    why = r.get("why") or ""
    kind = r.get("kind")
    if kind == "blocked":
        return (f"{r['file']} 被出图服务的内容审核拒收、无法英化（重跑与换提示词都无效），"
                f"已把它标为不可用并排除出后续选图，其余图照常发布，不用处理。{why}")
    if kind == "edit":
        s = ("出网链路异常、重试后仍不通，请检查网络/代理后重跑本步"
             if r.get("transient") else "出图接口或本地报错，请按报错处理后重跑本步")
        return f"{r['file']} 英化出图失败（{s}）：原图未被改动、不必换图。{why}"
    if kind == "qc_error":
        return (f"{r['file']} 清理后的质检环节报错、没能判定产物是否合规："
                f"原图未被改动、不必换图，请查看报错后重跑本步。{why}")
    # 夸大宣传单独一句：这类图往往一个汉字都没有（纯英文的 BEST-SELLER 角标），
    # 讲成「该图带中文/水印」会让用户对着一张全英文的图找中文（同本函数开头
    # 「文案必须与事实相符」的取向）。处置也不同：换图或换一张没有营销角标的同款图。
    if r.get("marketingClaim"):
        return (f"{r['file']} 英化质检未通过、仍是原图：图上的夸大宣传文案"
                f"（BEST-SELLER、热卖、爆款这类角标或标语）没能抹除干净，"
                f"平台按虚假宣传处罚，请人工换一张无营销标语的图后续跑：{why}")
    return (f"{r['file']} 英化质检未通过、仍是原图，"
            f"该图带中文/水印不能发布，请人工换图后续跑：{why}")


async def _clean_main_images(ctx: dict, emit) -> dict:
    """把带中文/水印/他人 logo 的主图送 gpt-image-2 清理，产物顶替原图。

    返回 {"status": "ok"|"skipped", "note": ...}，直接就是 ⑤b 阶段的返回值形状。

    为什么单列一步而不是塞进⑥：产物要被⑥素材图和⑦SKC颜色图【共用】。塞进⑥
    就得在⑦再清一遍同一批图，而 gpt-image-2 每张都是一次生图调用。这里清完直接把
    complianceNotes 里对应条目改成 clean=true 并指向新文件，⑥⑦ 的选图逻辑一行不用改
    就自动挑到干净图。

    【失败判 fail，停在现场交人工】2026-09-15 起改的取向。原先是「清理是增益路径，
    单张失败也照常 ok、该图以脏图身份参与⑥⑦ 兜底打分」——但那意味着一张带中文的图
    会被⑥⑦ 挑去当素材图/颜色图，一路发上真店，而中文是 Temu 最硬的红线。
    单张失败仍保留原标注、原图不被顶替（那是安全底线，见下面 copy 前的 continue），
    但本步如实判 fail：商品停在⑤b、现场还在，人工换图后可续跑。
    manual_check 照旧逐张发出，说明是哪张、为什么。

    并发而非串行：单张实测约 35s，4 张串行 140s 会明显拖慢单商品耗时。
    edit_image 是同步 curl 子进程，故用 to_thread 丢线程池 + Semaphore 限流。
    并发数取用户配置（见 get_image_concurrency）：最佳值随出网链路变化，写死不了。

    【不接 session 参数】整段只读写本地图与 product-info.json，与店小秘页面无关——
    这正是它能被提前到 ② 认领之前跑的前提（见 _run_prewarm）。原先它作为阶段函数
    带着 session 形参却一次没用到，抽出来时一并去掉，免得让人误以为它碰页面。
    """
    if not ctx.get("info_path"):
        return {"status": "skipped", "note": "无 product-info.json，跳过清理"}
    info = state._load_info(ctx["info_path"])
    # 下限传 ⑦ 的行下限：⑤b 的产物是 ⑥⑦ 共用，只按 ⑥「一张素材图」算会让 ⑦ 的
    # 颜色行凑不够合规图（见 vision.plan_clean 的说明）
    plan = vision.plan_clean(info, ctx["workdir"], min_clean=SKC_ROW_MIN_IMAGES)
    items = plan.get("items") or []
    # 轮播图会被素材图和 SKU 预览图复用。plan_clean 为控制成本只补最低张数，
    # 但明确标记含中文的其余主图不能原样发布，必须同样经过英化和质检。
    planned = {item.get("file") for item in items}
    notes = info.get("complianceNotes") or {}
    main_dir = ctx["workdir"]
    for entry in notes.get("files") or []:
        filename = entry.get("file") if isinstance(entry, dict) else ""
        if not filename or filename in planned or not entry.get("chinese"):
            continue
        path = os.path.join(main_dir, filename)
        if not os.path.isfile(path) or entry.get("clean") or entry.get("duplicate"):
            continue
        # 上一轮已被审核拒收的图不再送：重跑必然同样被拒（见 images.ModerationBlocked），
        # 白等一发还会把这张图重新报一次人工确认。
        if entry.get("unusable"):
            continue
        # 【尺码表图走专用提示词，并在末发豁免「全抹掉」】2026-09-17 修。
        # plan_clean 的 _SKIP_KINDS 只在【它自己的候选池】里排除了尺码表，本段补充
        # 循环是按 chinese 标注直接追加的，带中文的尺码表照样会走进来（_one 的
        # docstring 原先写着「尺码表已排除、不会走到这里」，与事实不符）。
        # 而 _one 的末发会把「原位译写」改成【把文字层全部抹掉】——尺码表被抹掉文字
        # 就只剩一张空网格，买家靠它选码，等于废图。⑬ 早为这件事单开了豁免
        # （description_images 的 sizechart 分支），这里对齐它：换成要求 cm→英寸的
        # 专用提示词，并让末发不进「全抹掉」那一档。
        is_sizechart = (entry.get("kind") or "") == "尺码表"
        items.append({
            "file": filename,
            "path": path,
            # 非尺码表那条也要带上营销标语的处置口径：这段是按 chinese 标注追加的补充
            # 循环，原先只说「中文译成英文 + 移除水印」，纯英文的 BEST-SELLER 角标两头
            # 都不沾、被原样留下（SIZECHART_TRANSLATE_PROMPT 那条已由
            # DEFAULT_TRANSLATE_PROMPT 带上，不必重复）。
            "prompt": (images.SIZECHART_TRANSLATE_PROMPT if is_sizechart else
                       "将图片中的所有中文文字翻译成自然英文并原位替换，保留商品主体、"
                       "构图和颜色；同时移除水印、店铺名和第三方 logo。"
                       + claims.CLAIM_REMOVE_RULE
                       + claims.BANNED_REMOVE_RULE),
            "sizechart": is_sizechart,
            "note": "轮播图中文复核",
        })
    if not items:
        return {"status": "skipped", "note": plan.get("reason") or "无可清理项"}

    outdir = os.path.join(ctx["workdir"], "cleaned")
    os.makedirs(outdir, exist_ok=True)
    conc = preferences.get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _one(item: dict) -> dict:
        """清一张：出图 → 质检 → 通过才算成功。

        成功返回 {"file", "ok": True, "path"}；失败返回 {"file", "ok": False,
        "kind", "why"}，kind 标出栽在哪一环（edit 出图 / qc_error 质检环节报错 /
        qc 质检判定不合格），供 _fail_message 给出与事实相符的提醒。

        质检未过时再烧一发（DESC_QC_TRIES）：生图有随机性，同图同提示词两发结果不同，
        理由与 ⑬ 那边同源，见 _prepare_desc_image 里那段实测记录。这一路的失败代价
        更大——主图脏着会以脏图身份参与⑥⑦选图（vision._dirty_score）。

        【最后一发改成「全抹掉」而不是再赌一次翻译】2026-09-15 定案，理由见
        cleaning_rules._retry_hint 的 last_chance 段。走到末发说明「原位译写」这件事
        在这张图上做不成，同一个要求再发一遍只是换随机种子；抹除是容易得多的任务，
        产物「无文案的干净商品图」对主图/轮播图够用。

        【尺码表图不进这一档】抹掉文字后它只剩一张空网格，买家靠它选码，等于废图。
        原先这里写着「尺码表已由 plan_clean 的 _SKIP_KINDS 排除、不会走到这里」，那是
        错的——补充循环（调用方那段按 chinese 标注追加的）不受 _SKIP_KINDS 约束，
        2026-09-17 修：那一段给尺码表换专用提示词并打上 sizechart 标记，这里按标记
        豁免（同 ⑬ 的 sizechart 分支）。
        """
        async with sem:
            dst = os.path.join(outdir, os.path.splitext(item["file"])[0] + "-clean.png")
            last_why, cjk_left, claim_left = "", False, False
            banned_left = False
            attempt, tries = 0, stages_description_images.DESC_QC_TRIES
            while attempt < tries:
                attempt += 1
                # 残留中文时重试要加码提示词（同 _prepare_desc_image 的理由）：
                # 原样重发只是赌随机性，把上一发残留了什么当新约束喂回去命中率更高。
                prompt = item["prompt"]
                if attempt > 1 and last_why:
                    # tries 会在循环里被抬高（中文残留/乱码抬到 DESC_QC_TRIES_TEXT），
                    # 故「是不是末发」要拿当前的 tries 判，不能用固定常量。
                    # 尺码表图豁免末发的「全抹掉」（理由见本函数 docstring 末段）
                    prompt += stages_cleaning_rules._retry_hint(
                        last_why, cjk_left, claim=claim_left, banned=banned_left,
                        last_chance=attempt == tries and not item.get("sizechart"))
                try:
                    # 素材图是轮播首图，糊了最伤转化，故这一路不降采样出图（见 pick_size 注释）
                    ed = await asyncio.to_thread(
                        images.edit_image, item["path"], prompt=prompt,
                        out_path=dst, no_downscale=True, timeout=CLEAN_TIMEOUT)
                except images.ModerationBlocked as e:
                    # 【内容审核拒绝：永久性失败，别再烧后面几发】同一张图连发都是同一个
                    # 拒绝（见 images.ModerationBlocked 的实测），重烧只是白花时间。
                    # kind 单列 blocked，让调用方把它标成不可用图而不是判整阶段失败。
                    return {"file": item["file"], "ok": False, "kind": "blocked",
                            "why": str(e)[:120]}
                except Exception as e:
                    # 【失败要带类型，供 _fail_message 分类】这一步栽了说明图压根
                    # 没被处理过，与「质检判定不合格」是两回事，上报文案不能共用。
                    # transient 只用来区分链路抖动和确定性报错（两者处置不同）。
                    return {"file": item["file"], "ok": False, "kind": "edit",
                            "transient": isinstance(e, images.TransientNetError),
                            "why": str(e)[:120]}
                try:
                    qc = await vision.check_cleaned(ed["output"])
                except Exception as e:
                    # 质检自己报错时产物合规与否没判过，同样不能报成「图带中文」
                    return {"file": item["file"], "ok": False, "kind": "qc_error",
                            "why": str(e)[:120]}
                if qc.get("clean"):
                    return {"file": item["file"], "ok": True, "path": ed["output"]}
                last_why = f"质检未过：{qc.get('issues') or ''}"
                cjk_left = bool(qc.get("residualChinese"))
                claim_left = bool(qc.get("marketingClaim"))
                banned_left = bool(qc.get("bannedTerm"))
                # 同 _prepare_desc_image：中文残留与生图乱码都算「这发没弄好」，多烧
                # 有救。main-04 那次两发全是 garbled，只给 2 发正好白放弃。
                # marketingClaim 同样抬发数（理由见 _prepare_desc_image 那处注释）：
                # 这一路的图是 ⑥⑦ 的选图来源，退回原图等于把一张带营销标语的图
                # 以脏图身份放回打分池。
                # 禁词与中文/乱码/夸大同属「重烧一发常常就过」（抹除比译写容易），
                # 故一并抬高发数，理由见 vision.check_cleaned 对 marketingClaim 那段。
                if (cjk_left or qc.get("garbled") or qc.get("marketingClaim")
                        or banned_left):
                    tries = max(tries, stages_description_images.DESC_QC_TRIES_TEXT)
                if attempt < tries:
                    # 脏法要进日志：加码话术按它分支（见 _retry_hint），不带就无法从
                    # 「烧了 4 发还没过」反推当时喂的是哪套加码
                    flags = "，".join(f for f, v in (("残留中文", cjk_left),
                                                    ("夸大宣传", claim_left),
                                                    ("平台禁词", banned_left)) if v)
                    logger.info(f"{item['file']} 清理质检未过（{attempt}/{tries}"
                                f"{'，' + flags if flags else ''}），"
                                f"重烧一发：{(qc.get('issues') or '')[:60]}")
            return {"file": item["file"], "ok": False, "kind": "qc",
                    "why": last_why[:120], "marketingClaim": claim_left,
                    "bannedTerm": banned_left}

    logger.info(f"图片清理：{len(items)} 张待处理（并发 {conc}）")
    results = await asyncio.gather(*(_one(it) for it in items))

    notes = info.get("complianceNotes") or {}
    by_name = {e.get("file"): e for e in (notes.get("files") or []) if isinstance(e, dict)}
    ok_files, fail_files, blocked_files = [], [], []
    for r in results:
        if not r.get("ok"):
            # 【审核拒收的图不算「未完成」，标成不可用后绕开它】它与其余失败的处置相反：
            # 那几类是「条件弄好再来一次」，这一类重跑必然同样被拒（见
            # images.ModerationBlocked）。原先它落进 fail_files 判整阶段 fail，而这张图
            # 每次重跑都被上面那段「轮播图中文复核」补充循环重新送进来，商品就永久卡在
            # ⑤b——2026-09-18 1051951604789 的 main-03.jpg 就是这么卡住的（那单另有
            # 10 张干净图，plan_clean 本身早判了「无需清理」）。
            if r.get("kind") == "blocked":
                blocked_files.append(r["file"])
                entry = by_name.get(r["file"])
                if entry is not None:
                    # unusable 是硬排除标记：带中文/水印还留着（如实），但所有会把图挂到
                    # 页面上的选图路径都要跳过它，理由与判据见 vision.is_unusable。
                    entry.update({"unusable": True, "unusableReason": "moderation",
                                  "note": "出图服务内容审核拒收，无法英化，已排除"})
                logger.warning(f"{r['file']} 被出图服务内容审核拒收，标为不可用并排除出选图："
                               f"{r.get('why')}")
            else:
                fail_files.append(r["file"])
            await emit({"type": "manual_check", "stage": "clean_images",
                        "message": _fail_message(r)})
            continue
        ok_files.append(r["file"])
        # 产物顶替原文件：⑥⑦ 都按 main-NN 文件名找图（vision._main_files 的 _IMG_RE），
        # 直接覆盖原图最省事——原图在 cleaned/ 外已被 edit_image 读过，
        # 且 raw.json 里留着源 URL，需要时能重新下载。
        shutil.copy(r["path"], os.path.join(ctx["workdir"], r["file"]))
        e = by_name.get(r["file"])
        if e is not None:
            # claim 必须跟着一起清零：它是 is_dirty 的判据之一，留着 True 会让 ⑥⑦
            # 继续把这张【已清干净且质检通过】的图当脏图排除，表现是「清了也白清」。
            e.update({"clean": True, "chinese": False, "watermark": False, "logo": False,
                      "claim": False, "cleaned": True, "note": "AI 清理后质检通过"})
    if by_name:
        notes["files"] = [by_name[k] for k in sorted(by_name)]
        notes["cleanFiles"] = sorted(k for k, v in by_name.items() if v.get("clean"))
        info["complianceNotes"] = notes
        _save_info(ctx["info_path"], info)

    note = f"清理 {len(ok_files)}/{len(items)} 张"
    if blocked_files:
        # 审核拒收的单独说，且【不进 fail】：本步对它的处置（标不可用、排除出选图）
        # 已经做完了，阶段结论就是 ok。讲成「未完成」会让用户去重跑一件重跑不好的事。
        note += f"（{len(blocked_files)} 张被审核拒收已排除：{'、'.join(blocked_files)}）"
    if fail_files:
        # 「未完成」而不是「未通过」：出图失败/质检报错的那几张压根没走到判定，
        # 说成「未通过」会把「没做成」讲成「做出来不合格」
        note += f"（未完成：{'、'.join(fail_files)}）"
        return {"status": "fail", "note": note[:300]}
    return {"status": "ok", "note": note[:300]}


async def _st_clean_images(ctx: dict, session: BrowserSession, emit) -> dict:
    """⑤b 图片清理：预热已经清过就直接复用它的结论，否则现在清。

    【为什么这一步的预热结果能整体复用，而 ⑥⑦ 不能】清理的产物是磁盘上的文件和
    product-info.json 里的标注，两者都已落盘、与页面无关；而 ⑥⑦ 的判断要读【清理之后】
    的标注，提前跑就会读到旧值（详见 _st_material 里那段）。所以正确的提前量是把这一步
    整段挪早，让 ⑥⑦ 留在原位读它的成果。
    """
    done = await stages_prewarm_access._await_prewarm(ctx, "clean_images")
    if done:
        note = done.get("note") or ""
        logger.info(f"图片清理沿用提前预热的结果：{note}")
        return {**done, "note": (note + "（已在采集后提前完成）")[:200]}
    return await _clean_main_images(ctx, emit)
