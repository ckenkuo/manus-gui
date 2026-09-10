"""店小秘发布共用能力：stages.cleaning。各来源流程由 workflows/ 独立定义。"""

import asyncio
import json
import os
import shutil
from app.logger import logger
from app.publish import images, preferences, state, vision
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


async def _clean_main_images(ctx: dict, emit) -> dict:
    """把带中文/水印/他人 logo 的主图送 gpt-image-2 清理，产物顶替原图。

    返回 {"status": "ok"|"skipped", "note": ...}，直接就是 ⑤b 阶段的返回值形状。

    为什么单列一步而不是塞进⑥：产物要被⑥素材图和⑦SKC颜色图【共用】。塞进⑥
    就得在⑦再清一遍同一批图，而 gpt-image-2 每张都是一次生图调用。这里清完直接把
    complianceNotes 里对应条目改成 clean=true 并指向新文件，⑥⑦ 的选图逻辑一行不用改
    就自动挑到干净图。

    【绝不阻塞流程】这是用户明确要求：单张失败/超时/质检不过一律保留原标注，
    该图仍以脏图身份参与⑥⑦ 的兜底打分（见 vision._dirty_score），本步照常 ok。
    清理是「能修就修」的增益路径，不是硬前置。

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
    if not items:
        return {"status": "skipped", "note": plan.get("reason") or "无可清理项"}

    outdir = os.path.join(ctx["workdir"], "cleaned")
    os.makedirs(outdir, exist_ok=True)
    conc = preferences.get_image_concurrency()
    sem = asyncio.Semaphore(conc)

    async def _one(item: dict) -> dict:
        """清一张：出图 → 质检 → 通过才算成功。返回 {"file", "ok", "path", "why"}。

        质检未过时再烧一发（DESC_QC_TRIES）：生图有随机性，同图同提示词两发结果不同，
        理由与 ⑬ 那边同源，见 _prepare_desc_image 里那段实测记录。这一路的失败代价
        更大——主图脏着会以脏图身份参与⑥⑦选图（vision._dirty_score）。
        """
        async with sem:
            dst = os.path.join(outdir, os.path.splitext(item["file"])[0] + "-clean.png")
            last_why, cjk_left = "", False
            attempt, tries = 0, stages_description_images.DESC_QC_TRIES
            while attempt < tries:
                attempt += 1
                # 残留中文时重试要加码提示词（同 _prepare_desc_image 的理由）：
                # 原样重发只是赌随机性，把上一发残留了什么当新约束喂回去命中率更高。
                prompt = item["prompt"]
                if attempt > 1 and last_why:
                    prompt += stages_cleaning_rules._retry_hint(last_why, cjk_left)
                try:
                    # 素材图是轮播首图，糊了最伤转化，故这一路不降采样出图（见 pick_size 注释）
                    ed = await asyncio.to_thread(
                        images.edit_image, item["path"], prompt=prompt,
                        out_path=dst, no_downscale=True, timeout=CLEAN_TIMEOUT)
                except Exception as e:
                    return {"file": item["file"], "ok": False, "why": f"出图失败：{e}"[:120]}
                try:
                    qc = await vision.check_cleaned(ed["output"])
                except Exception as e:
                    return {"file": item["file"], "ok": False, "why": f"质检失败：{e}"[:120]}
                if qc.get("clean"):
                    return {"file": item["file"], "ok": True, "path": ed["output"]}
                last_why = f"质检未过：{qc.get('issues') or ''}"
                cjk_left = bool(qc.get("residualChinese"))
                # 同 _prepare_desc_image：中文残留与生图乱码都算「这发没弄好」，多烧
                # 有救。main-04 那次两发全是 garbled，只给 2 发正好白放弃。
                if cjk_left or qc.get("garbled"):
                    tries = max(tries, stages_description_images.DESC_QC_TRIES_TEXT)
                if attempt < tries:
                    logger.info(f"{item['file']} 清理质检未过（{attempt}/{tries}"
                                f"{'，残留中文' if cjk_left else ''}），"
                                f"重烧一发：{(qc.get('issues') or '')[:60]}")
            return {"file": item["file"], "ok": False, "why": last_why[:120]}

    logger.info(f"图片清理：{len(items)} 张待处理（并发 {conc}）")
    results = await asyncio.gather(*(_one(it) for it in items))

    notes = info.get("complianceNotes") or {}
    by_name = {e.get("file"): e for e in (notes.get("files") or []) if isinstance(e, dict)}
    ok_files, fail_files = [], []
    for r in results:
        if not r.get("ok"):
            fail_files.append(r["file"])
            await emit({"type": "manual_check", "stage": "clean_images",
                        "message": f"{r['file']} 清理未成功（仍用原图，不影响流程）：{r.get('why')}"})
            continue
        ok_files.append(r["file"])
        # 产物顶替原文件：⑥⑦ 都按 main-NN 文件名找图（vision._main_files 的 _IMG_RE），
        # 直接覆盖原图最省事——原图在 cleaned/ 外已被 edit_image 读过，
        # 且 raw.json 里留着源 URL，需要时能重新下载。
        shutil.copy(r["path"], os.path.join(ctx["workdir"], r["file"]))
        e = by_name.get(r["file"])
        if e is not None:
            e.update({"clean": True, "chinese": False, "watermark": False, "logo": False,
                      "cleaned": True, "note": "AI 清理后质检通过"})
    if by_name:
        notes["files"] = [by_name[k] for k in sorted(by_name)]
        notes["cleanFiles"] = sorted(k for k, v in by_name.items() if v.get("clean"))
        info["complianceNotes"] = notes
        _save_info(ctx["info_path"], info)

    note = f"清理 {len(ok_files)}/{len(items)} 张"
    if fail_files:
        note += f"（未成功：{'、'.join(fail_files)}，按原图继续）"
    return {"status": "ok", "note": note}


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
