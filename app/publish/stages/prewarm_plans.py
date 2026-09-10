"""店小秘发布共用能力：stages.prewarm_plans。各来源流程由 workflows/ 独立定义。"""

import json
import os
from app.logger import logger
from app.publish import images, vision
from app.publish.stages import description_images as stages_description_images


def _desc_modules_from_raw(workdir: str) -> list:
    """按 raw.json + 本地 desc-NN.jpg 拼一份 plan_desc 能吃的 modules（供预热用）。

    尺寸从本地文件读而不是靠网络：extract 下载时 `enumerate(imgs, 1)` 保证
    desc-NN.jpg 与 descImages 按下标一一对应，故第 i 张的尺寸就是 desc-{i:02d}.jpg 的。
    读不到尺寸的不标 tooSmall——与 desc_map 的取向一致（naturalWidth 为 0 时按
    「读不到」处理，不当成不达标），免得把好图误判成要放大。
    """
    raw_path = os.path.join(workdir, "raw.json")
    try:
        with open(raw_path, encoding="utf-8") as f:
            urls = (json.load(f) or {}).get("descImages") or []
    except Exception as e:
        logger.warning(f"预热读 raw.json 失败（跳过描述图预热）：{e}")
        return []
    mods = []
    for i, u in enumerate(urls, 1):
        if not isinstance(u, str) or not u:
            continue
        m = {"pos": i, "url": u, "onDxmHost": "dianxiaomi.com" in u}
        wh = images.image_size(os.path.join(workdir, f"desc-{i:02d}.jpg"))
        if wh:
            m["size"] = f"{wh[0]}x{wh[1]}"
            # 描述图按描述图的规则判（比例 0.5~2、两边 >= 480），不套服装 1340x1785：
            # 套错会把 1000x1000 这种合格图标成 needsUpscale，每跑白烧一轮生图
            # （见 images.check_desc_size 上方的截图取证）
            chk = images.check_desc_size(wh[0], wh[1])
            m["tooSmall"] = chk["ok"] is False
            if chk["ok"] is False:
                m["sizeReasons"] = chk["reasons"]
        mods.append(m)
    return mods


def _replan_desc_by_url(pre_plan: dict, mods: list) -> dict:
    """把预热出的规划按页面 modules 的【当前序号】重挂，返回同 plan_desc 形状的产物。

    【为什么必须重挂而不能直接用】预热的 pos 是 raw.json 里 descImages 的源顺序；页面
    描述区的 pos 由 desc_map 按 .desc-img-box img 现数（且文字模块处理后还会重排）。
    两套序号只是常常相同、并不保证相同——直接拿源 pos 去删/换，就是本项目已经踩过的
    那类错位（见 pipeline._JS_DESC_IDX_MAP 上方那次「删 pos 3/2 实际删掉 pos 2/1」）。
    URL 是稳定标识，故一律按 URL 重新对齐 pos。

    动作从预热计划里按 URL 取；页面上有而预热没判过的图【一律按 keep 处理】——与
    plan_desc 对漏判项的取向一致（保守方向，不删不该删的）。needsUpscale 则按页面
    现测的 tooSmall 重算：尺寸是页面事实，预热按本地文件算的只是估计，两者不一致时
    要信页面（本地 desc-NN.jpg 与页面挂的图理论同源，但页面可能已被替换过）。
    """
    # plan_desc 的 delete 只回 pos，要还原成 URL 得靠预热时那份 modules 的映射，
    # 故 _prewarm_desc 会把它翻成 deleteUrls 一起落下来（见那边）。
    act_by_url, reason_by_url, sizechart_by_url = {}, {}, {}
    for u in pre_plan.get("deleteUrls") or []:
        act_by_url[u] = "delete"
    for r in pre_plan.get("replace") or []:
        if r.get("url"):
            act_by_url[r["url"]] = "replace"
            reason_by_url[r["url"]] = r.get("reason") or ""
            if r.get("sizechart"):
                sizechart_by_url[r["url"]] = True

    delete, replace = [], []
    for m in mods:
        u, pos = m.get("url"), m.get("pos")
        if not u or not pos:
            continue
        act = act_by_url.get(u, "keep")
        if act == "delete":
            delete.append(pos)
            continue
        # 尺寸按页面现测重算：keep 但不达标的照 plan_desc 的规矩改判 replace + 放大
        too_small = bool(m.get("tooSmall"))
        if act == "replace":
            rep = {"pos": pos, "url": u, "reason": reason_by_url.get(u, "")}
            if sizechart_by_url.get(u):
                rep["sizechart"] = True
            replace.append(rep)
        elif too_small:
            replace.append({"pos": pos, "url": u, "needsUpscale": True,
                            "reason": f"尺寸 {m.get('size')} 不符合描述图要求："
                                      + "、".join(m.get("sizeReasons") or [])})
    replace.sort(key=lambda r: r["pos"])
    keep = [m["pos"] for m in mods
            if m.get("pos") not in set(delete)
            and all(r["pos"] != m.get("pos") for r in replace)]
    return {"status": "ok", "delete": sorted(set(delete)),
            "replace": replace, "keep": sorted(set(keep))}


async def _prewarm_desc(ctx: dict, info: dict, emit) -> dict:
    """预热描述图：按源 URL 出规划 + 并发把英化产物烧进 desc-edit/ 缓存。

    返回 {"plan": plan_desc 产物, "prepared": {url: 备料结果}}。⑬ 阶段拿 plan 当
    页面实况对得上时的现成计划，prepared 则由 _prewarm_desc_images 的落盘缓存自然生效
    （产物在磁盘上，⑬ 那边 how=cached 直接命中）。
    """
    mods = _desc_modules_from_raw(ctx["workdir"])
    if not mods:
        return {}
    plan = await vision.plan_desc(mods, info)
    # delete 只回 pos，而 ⑬ 那边要按 URL 重挂序号（见 _replan_desc_by_url），故这里
    # 就把 pos 翻成 URL 存下来——翻译要用的映射只在此刻手上有。
    by_pos = {m["pos"]: m["url"] for m in mods}
    plan = {**plan,
            "deleteUrls": [by_pos[p] for p in (plan.get("delete") or []) if p in by_pos]}
    prepared = await stages_description_images._prewarm_desc_images(ctx["workdir"], plan.get("replace") or [], emit)
    return {"plan": plan, "prepared": prepared}
