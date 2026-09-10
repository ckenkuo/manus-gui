"""店小秘发布共用能力：stages.prewarm_access。各来源流程由 workflows/ 独立定义。"""

from app.logger import logger


async def _await_prewarm(ctx: dict, key: str):
    """等预热任务跑完（若还在跑），然后取一次该项结果；没有就返回 None。

    【为什么要等而不是「有就用、没有就现场算」】预热是与 ②③ 并行的后台任务，到 ⑤ 时
    多半已完成；但类目命中缓存时 ②③ 只花 50s 左右，标题这类可能还差几秒。此时直接
    判「没有」就会再发一次同样的请求——两条路都在跑同一个判断，既慢又多花一次调用。
    等它反而是快的：最差情况等到的时刻，与不预热时现场算完的时刻相同。

    等待本身不设超时：_run_prewarm 内部每一项都吞了异常、必然会返回，而它包住的都是
    带自身重试上限的 LLM 调用。这里再加一层超时只会造出「等了一半又去重算」的浪费。
    """
    task = ctx.get("prewarm_task")
    if task is not None and not task.done():
        try:
            await task
        except Exception as e:
            # done 回调已记过，这里只是别让它把当前阶段带下水
            logger.warning(f"等待预热任务时出错（现场计算）：{e}")
    return _take_prewarm(ctx, key)


def _take_prewarm(ctx: dict, key: str):
    """取一次预热结果并【摘掉】，取不到返回 None。

    摘掉而不是留着：⑤~⑬ 的成果 save 前一重载就丢，同一批次内该阶段可能被重跑
    （见模块头 _stale_form_stages 那段）。重跑时页面状态已变，而预热结果是按【第一轮
    的本地产物】算的——标题/包装估算这类与页面无关的仍然有效，但让它只用一次更稳妥：
    重跑走现场那条路，与不带预热时的行为完全一致，不必再论证「复用第二次是否安全」。
    """
    pool = ctx.get("prewarm") or {}
    return pool.pop(key, None)
