"""活动批次的运行控制：暂停闸门 + 逐格跳过。

为什么单独一个模块：同一份控制状态要被三处用——service 的循环查闸门、app.py 的 HTTP
接口置位、测试直接驱动。放进 service 会让 app.py 反向依赖业务模块，放进 app.py 则
service 又要 import Web 层，故独立成只依赖 stdlib 的小对象。

为什么暂停是「可恢复」而不是「停」：跑批次时想看一眼浏览器或核对数据，停掉再重跑等于
把已填好的格子全部重做。代价是暂停期间流量停在关闭态——这正是阶段三（重开流量）**拒绝
暂停**的原因：只要阶段一关过流量，就必须跑到把它开回来，见 PAUSE_REFUSED_PHASES。
"""

import asyncio

# 拒绝暂停的阶段：此时流量已关闭，暂停 = 商品在无流量状态下卖货，是本管线最严重的
# 不可逆后果（service 里阶段三的注释点过名）。被拒时如实回事件，但不阻塞流程。
PAUSE_REFUSED_PHASES = frozenset({"reopen"})


class ActivityControl:
    """一次批次的暂停/继续与逐格跳过状态。"""

    def __init__(self) -> None:
        self.paused = False
        # idle → planning/scan → close → enroll → reopen → done
        self.phase = "idle"
        self._resume = asyncio.Event()
        self._resume.set()  # 初始不暂停：paused=False 时闸门根本不看它
        self._skips: set = set()   # 用户要求跳过的格子 (spu, activity)
        self._locked: set = set()  # 已填价/已提交的格子：不能再跳过（提交是活动级一次提交）

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def set_paused(self, paused: bool) -> dict:
        """HTTP 侧置位，返回 {"paused": 生效值, "refused": 拒绝原因|None}。

        拒绝不是错误而是保护：阶段三已关掉流量，此时挂起会把商品留在无流量在售状态。
        """
        if paused and self.phase in PAUSE_REFUSED_PHASES:
            return {"paused": False, "refused": "reopen_phase"}
        self.paused = bool(paused)
        if self.paused:
            self._resume.clear()
        else:
            self._resume.set()
        return {"paused": self.paused, "refused": None}

    async def wait_if_paused(self) -> bool:
        """暂停闸门：paused 置位时阻塞到恢复为止。

        返回 True 表示真的阻塞过——调用方据此在阻塞前后各发一条事件（paused/resumed），
        前端才能把按钮文案从「暂停中」切到「已暂停」。只在【安全边界】调用（活动之间、
        SPU 之间），绝不在点击动作中途或 asyncio.wait_for 的计时窗口内调用。
        """
        if not self.paused:
            return False
        while self.paused:
            await self._resume.wait()
        return True

    def request_skip(self, spu, activity, skipped: bool = True) -> dict:
        """置/撤某格的跳过标记，返回 {"accepted": bool, "reason": str|None}。

        已填价的格子拒绝跳过：本活动的报名是「一次提交全部已填商品」，勾选与填价都已生效，
        反填不了；如实拒绝比假装跳过好（提示到提报页手动取消勾选）。
        """
        key = (str(spu), str(activity))
        if skipped and key in self._locked:
            return {"accepted": False, "reason": "already_filled"}
        if skipped:
            self._skips.add(key)
        else:
            self._skips.discard(key)
        return {"accepted": True, "reason": None}

    def lock_cell(self, spu, activity) -> None:
        """标记某格已填价/已提交，之后不再接受跳过。"""
        self._locked.add((str(spu), str(activity)))

    def is_locked(self, spu, activity) -> bool:
        return (str(spu), str(activity)) in self._locked

    def is_skipped(self, spu, activity) -> bool:
        return (str(spu), str(activity)) in self._skips

    def skipped_cells(self) -> list:
        """已跳过的格子（排序后输出，便于事件与汇总稳定可比对）。"""
        return [list(key) for key in sorted(self._skips)]
