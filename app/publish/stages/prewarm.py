"""店小秘发布共用能力：stages.prewarm。各来源流程由 workflows/ 独立定义。"""

import asyncio
from app.logger import logger
from app.publish import state
from app.publish.accessories import judge_sku_category
from app.publish.packaging import estimate_pack
from app.publish.stages import cleaning as stages_cleaning, prewarm_plans as stages_prewarm_plans
from app.publish.titles import generate_titles


def _start_prewarm(ctx: dict, emit) -> None:
    """把预热任务丢到后台跑，立刻返回，不等它（见 _run_prewarm 的说明）。

    【为什么是后台任务而不是一个阶段】它的价值恰恰在于与 ② 认领、③ 类目【并行】：
    做成阶段就又变成串行等待，一点提速都没有。任务句柄挂在 ctx 里由 publish_one
    在收尾时统一回收（见那边的 finally），免得批次结束还留着悬挂任务。

    【失败一律不影响主流程】_run_prewarm 内部逐项吞异常；这里再兜一层 done 回调只为
    把「整个预热协程炸了」也写成 warning——预热没跑成，各阶段照原路现场算而已。
    """
    if not ctx.get("info_path") or not ctx.get("workdir"):
        return

    task = asyncio.ensure_future(_run_prewarm(ctx, emit))

    def _done(t) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(f"预热任务整体失败（各阶段将现场计算）：{exc}")

    task.add_done_callback(_done)
    ctx["prewarm_task"] = task


# ---- 纯本地判断的提前预热 ----------------------------------------------------
#
# 【为什么能提前】① extract 一跑完，product-info.json 与 main/desc 图就全在磁盘上了。
# ⑤ 标题、⑥ 素材图、⑦ SKC 分色、⑩ 包装估算、⑪ SKU 分类、⑬ 描述图规划这六个判断的
# 输入【只有这些本地产物】，与店小秘页面无关（逐个核对过各自的取值来源：set_titles 读
# title/attributes/skus/imageUnderstanding，pick_material 与 plan_skc 读 workdir 的
# main-NN + complianceNotes，set_variant 读 title/packInfo，set_stock 的分类判断读
# title/套装件数，plan_desc 读描述图 URL 与尺寸）。
#
# 【为什么值得提前】它们原先各自等在自己的阶段里串行发请求，而 ② 认领 + 打开编辑页
# （实测 33~37s）和 ③ 类目（缓存命中 15s，未命中曾达 314s）这段时间浏览器在忙、
# LLM 完全空闲。把这六个判断挪到 ① 之后并发起跑，正好填进这段空窗。
#
# 【必须是纯增益，绝不改变判定】预热只把结果放进 ctx["prewarm"]，各阶段命中就用、
# 没有就照原路现场算。任何一个预热失败都只写 warning：这是本项目辅助路径的既定取向
# （见模块头「best-effort」那段），不是 fallback——现场算那条路本来就一直在。
#
# 【描述图规划为什么也算纯本地】plan_desc 的输入是 modules 的 url + tooSmall，而
# 认领后店小秘描述区挂的就是 1688 源外链（desc_save 的校验项「仍有外链图未转存」即
# 此，2026-08-23 真站取证），与 raw.json 的 descImages 同源。故可按源 URL 预先出计划、
# 并把英化产物按 URL 哈希烧进 desc-edit/ 缓存；⑬ 到点仍按页面实况重查一遍 modules，
# 命中缓存直接复用、URL 对不上就现场跑。预热在这里【只做备料，不替代 plan_desc】。
_PREWARM_KEYS = ("titles", "material", "skc", "variant", "stock", "desc")


async def _run_prewarm(ctx: dict, emit) -> None:
    """并发跑完六个纯本地判断，结果写进 ctx["prewarm"]（每项独立 best-effort）。

    【为什么用 gather 而不是逐个 await】它们互不依赖，且都是纯网络等待（LLM 调用）。
    单项失败不能影响其它项，故 return_exceptions=True 逐项收，异常只写 warning。

    【为什么这个函数自己不 emit stage 事件】它不是阶段，是阶段的提前量。跑成什么样
    由各阶段照常报——预热命中时那个阶段的 elapsed_s 自然就短了，这比多一路事件清楚。
    """
    info = state._load_info(ctx["info_path"])

    async def _titles():
        # 只做生成，不填页面：填写要 session，且必须在编辑页打开之后。
        return await generate_titles(info)

    async def _variant():
        # 按超集问（要尺寸也要重量）：此刻 cat_path 还没有，判不了服装类是否走固定尺寸，
        # 也不知道源 packInfo 会不会补上重量。多问的字段 set_variant 用不到即可，
        # 缺字段才会让它补问一次（见 estimate_pack 的 need_dims/need_weight 说明）。
        return await estimate_pack(info, need_dims=True, need_weight=True)

    async def _stock():
        return await judge_sku_category(info, cat_path=ctx.get("cat_path"))

    async def _desc():
        return await stages_prewarm_plans._prewarm_desc(ctx, info, emit)

    async def _clean():
        return await stages_cleaning._clean_main_images(ctx, emit)

    jobs = {"titles": _titles, "variant": _variant, "stock": _stock,
            "desc": _desc, "clean_images": _clean}
    keys = list(jobs)
    logger.info(f"提前预热 {len(keys)} 个纯本地判断（与认领/类目阶段并行）：{'、'.join(keys)}")
    results = await asyncio.gather(*(jobs[k]() for k in keys), return_exceptions=True)

    out: dict = {}
    ok_keys, bad_keys = [], []
    for k, r in zip(keys, results):
        if isinstance(r, BaseException):
            bad_keys.append(k)
            logger.warning(f"预热 {k} 失败（该阶段照原路现场算）：{r}")
            continue
        if r:
            out[k] = r
            ok_keys.append(k)
    ctx["prewarm"] = out
    logger.info(f"预热完成：命中 {len(ok_keys)} 项（{'、'.join(ok_keys) or '无'}）"
                + (f"，失败 {len(bad_keys)} 项（{'、'.join(bad_keys)}）" if bad_keys else ""))
    await emit({"type": "log", "stage": "extract",
                "message": f"提前预热完成：{len(ok_keys)}/{len(keys)} 项可直接复用"
                           + (f"，{len(bad_keys)} 项失败将现场重算" if bad_keys else "")})
