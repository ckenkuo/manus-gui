"""店小秘发布共用能力：stages.description_text。各来源流程由 workflows/ 独立定义。"""

from app.logger import logger
from app.publish import extract, vision
from app.publish.media.description import desc_text_apply, desc_text_delete_all, desc_text_map


def _size_evidence(info: dict, texts: list) -> str:
    """⑬ 删文字模块前给尺码留取证：返回疑似写着尺码的原文摘要，没有则返回空串。

    【为什么需要这道闸】①b（extract.enrich_desc_text）已在采集阶段把详情纯文字里的
    尺码抽进 product-info.json，⑨ 也已据此填完平台尺码表（⑨ 在 ⑬ 之前跑），故页面上
    这些文字模块只是冗余副本、删掉不丢尺码。但 ①b 有两道跳过闸——descText 为空的纯图
    详情、文字里没命中 _RE_SIZE_HINT——命中时 info 里没有实测值、⑨ 全靠模型估算。
    那种情况下模块原文若真写着尺码，删掉就等于把唯一的准确来源丢在页面上没人看见。

    【只取证、不阻拦】用户 2026-09-01 明确要求文字板块一律移除，故这里不改变删除行为，
    只把原文记进日志 + manual_check 留追溯线索（原文另有一份在 info 的 descText 里，
    见 extract.py 的 descText 落盘）。判据复用 ①b 那套尺码信号词，不另造一套。
    """
    if info.get("sizeMeasurements"):
        return ""                          # ①b 或视觉已抽到实测值，无需取证
    hit = [t for t in texts
           if extract._RE_SIZE_HINT.search((t.get("text") or ""))]
    if not hit:
        return ""
    detail = "；".join(f"idx={t.get('idx')}：{(t.get('text') or '')[:200]}"
                      for t in hit[:3])
    logger.warning(f"描述文字模块疑似写着尺码、而源数据里没有实测尺寸"
                   f"（⑨ 尺码表已按模型估算填过）：{detail}")
    return detail


async def _keep_size_text(session, info_for_desc, emit) -> tuple:
    """⑬ 实测尺寸缺失时的文字模块处理：尺码文字英化 + cm→英寸后保留，其余照删。

    当 ①b/视觉 没从文字/图片识别到实测尺寸（sizeMeasurements 为空）、阶段⑨ 只能靠
    模型估算时，描述区里商家白纸黑字写的尺码原文是买家唯一的准确来源，删掉就丢了
    （与 _size_evidence 同款取证）。故这里把命中 _RE_SIZE_HINT 的尺码模块英化并把
    cm 换算成英寸后保留，其余模块（采集残留 JSON、店铺宣传等）照旧删除。

    返回 (text_note, n_text_deleted)。尺码文字翻译失败的按 best-effort 保留中文原文
    并发 manual_check 交人工（删掉比留中文更糟，买家连对照都没得看）。
    """
    tm = await desc_text_map(session)
    if tm.get("status") != "ok":
        logger.warning(f"文字模块枚举失败（保留原文，继续图片处理）：{str(tm)[:150]}")
        return "文字模块处理异常", 0
    texts = [t for t in (tm.get("texts") or []) if str(t.get("idx") or "").strip()]
    size_texts = [t for t in texts
                  if extract._RE_SIZE_HINT.search(t.get("text") or "")]
    if not size_texts:
        tr = await desc_text_delete_all(session)
        n = len(tr.get("deleted") or [])
        note = f"文字模块 删 {n}"
        if tr.get("failed"):
            note += f" / 失败 {len(tr['failed'])}"
            await emit({"type": "manual_check", "stage": "desc",
                        "message": f"文字模块删除有 {len(tr['failed'])} 项未成功"
                                   f"（原文仍留在页面上）：{str(tr['failed'])[:150]}"})
        logger.info(f"描述文字模块处理完成：{note}")
        return note, n

    tplan = await vision.translate_size_texts(size_texts, info_for_desc)
    acts = list(tplan.get("plan") or [])
    translated_idx = {a["idx"] for a in acts}
    size_idx = {t["idx"] for t in size_texts}
    for t in texts:
        if t["idx"] in translated_idx:
            continue
        # 尺码文字但没译成（模型漏判/译文空）：保留中文原文，不删（删掉丢唯一准确来源）
        acts.append({"idx": t["idx"], "text": "",
                     "action": "keep" if t["idx"] in size_idx else "delete"})
    tr = await desc_text_apply(session, acts)
    n_tr = len(tr.get("translated") or [])
    n_del = len(tr.get("deleted") or [])
    kept_cn = sum(1 for a in acts if a["action"] == "keep")
    note = f"文字模块 尺码英化 {n_tr} / 删 {n_del}"
    if kept_cn:
        note += f" / 保留中文原文 {kept_cn}"
        await emit({"type": "manual_check", "stage": "desc",
                    "message": f"{kept_cn} 段尺码文字未能英化、已保留中文原文，"
                               f"请人工确认或改为英文"})
    if tr.get("failed"):
        note += f" / 失败 {len(tr['failed'])}"
        await emit({"type": "manual_check", "stage": "desc",
                    "message": f"文字模块处理有 {len(tr['failed'])} 项未成功"
                               f"（原文仍留在页面上）：{str(tr['failed'])[:150]}"})
    logger.info(f"描述文字模块处理完成：{note}")
    return note, n_del
