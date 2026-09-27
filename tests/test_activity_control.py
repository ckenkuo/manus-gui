"""ActivityControl 的用例：暂停闸门语义、阶段三拒绝暂停、跳过与「已填价后拒绝跳过」。"""
import asyncio

from app.activity.control import ActivityControl


def test_pause_gate_blocks_until_resumed():
    """暂停后闸门阻塞，恢复后返回 True（调用方据此发 paused/resumed 事件）。"""
    control = ActivityControl()

    async def scenario():
        assert await control.wait_if_paused() is False  # 没暂停：不阻塞
        assert control.set_paused(True) == {"paused": True, "refused": None}
        task = asyncio.create_task(control.wait_if_paused())
        await asyncio.sleep(0)  # 让闸门进入等待
        assert not task.done()
        control.set_paused(False)
        return await asyncio.wait_for(task, timeout=1)

    assert asyncio.run(scenario()) is True


def test_resume_then_pause_again_blocks_again():
    """恢复后再次暂停仍能拦住——闸门是循环等待，不是一次性开关。"""
    control = ActivityControl()
    control.set_paused(True)
    control.set_paused(False)

    async def scenario():
        control.set_paused(True)
        task = asyncio.create_task(control.wait_if_paused())
        await asyncio.sleep(0)
        assert not task.done()
        control.set_paused(False)
        await asyncio.wait_for(task, timeout=1)
        return await control.wait_if_paused()

    assert asyncio.run(scenario()) is False


def test_reopen_phase_refuses_pause():
    """阶段三（重开流量）拒绝暂停：此时流量已关，挂起会把商品留在无流量在售状态。"""
    control = ActivityControl()
    control.set_phase("reopen")
    assert control.set_paused(True) == {"paused": False, "refused": "reopen_phase"}
    assert control.paused is False
    assert asyncio.run(control.wait_if_paused()) is False  # 不会被拦住，流程照跑
    control.set_phase("enroll")
    assert control.set_paused(True)["paused"] is True  # 其它阶段照常可暂停


def test_skip_is_per_cell_and_reversible():
    control = ActivityControl()
    assert control.request_skip("111", "活动A") == {"accepted": True, "reason": None}
    assert control.is_skipped("111", "活动A") is True
    assert control.is_skipped("111", "活动B") is False  # 只跳这一格
    assert control.skipped_cells() == [["111", "活动A"]]
    control.request_skip("111", "活动A", skipped=False)
    assert control.is_skipped("111", "活动A") is False


def test_skip_refused_after_cell_is_filled():
    """已填价的格子拒绝跳过：本活动的报名是一次提交全部已填商品，勾选与填价都已生效。"""
    control = ActivityControl()
    control.lock_cell("111", "活动A")
    assert control.request_skip("111", "活动A") == {"accepted": False, "reason": "already_filled"}
    assert control.is_skipped("111", "活动A") is False
    assert control.is_locked("111", "活动A") is True
