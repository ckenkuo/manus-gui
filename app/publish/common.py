"""店小秘发布操作：common。模块导航见 docs/publish-pipeline-refactor.md。"""

import asyncio


async def _poll_until(probe, ok, timeout: float, interval: float = 0.12):
    """先立即探一次，未成立再按 interval 轮询到 timeout。返回最后一次结果。

    【为什么加这层：把「固定 sleep」换成「条件等待」】属性写入的每一项原先是
    sleep(0.6) 开下拉 + sleep(0.8) 点选项 + 回读轮询首次也先 sleep(0.5)，
    合计 1.9s 是【无条件付出】的——而这些值是按最坏情况定的保守量，绝大多数
    情况下页面早就到位了。2026-09-03 按断点状态统计：命中选项缓存的样本里
    写 0-4 项中位 48.5s、写 5-9 项中位 179.0s，即每项约 25-30s，固定等待占了
    可观一块（attrs 阶段本身占全流程 22.5%，是最大头）。

    改成条件等待【不放宽任何判据】：原来的 sleep 时长成为这里的 timeout 上限，
    最坏情况与改前等价；页面提前就绪时才提前返回。收敛条件由调用方给，
    与原先 sleep 之后那次检查读的是同一个信号。
    """
    data = await probe()
    if ok(data):
        return data
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)
        data = await probe()
        if ok(data):
            return data
    return data
