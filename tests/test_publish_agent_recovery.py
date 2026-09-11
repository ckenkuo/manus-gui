import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.publish import agent_tools, recovery, service, state
from app.publish.workflows import get_workflow
from app.publish.workflows.base import Stage


def context(stage="titles", handler=None, **values):
    session = SimpleNamespace(is_alive=lambda: True, eval_json=AsyncMock(return_value={}))
    return agent_tools.RecoveryContext(
        session, {"rowid": "123", **values}, stage,
        {stage: handler or AsyncMock(return_value={"status": "ok"})}, AsyncMock(),
    )


def test_page_tool_registry_covers_public_browser_functions():
    from app.publish import pipeline
    import inspect

    tools = agent_tools.build_publish_tools(context())
    for name, function in vars(pipeline).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(function):
            continue
        if "session" not in inspect.signature(function).parameters:
            continue
        if name not in {"save", "publish_now"}:
            assert f"dxm_{name}" in tools.tool_map
    assert "dxm_save" not in tools.tool_map
    assert "dxm_publish_now" not in tools.tool_map
    for tool in tools:
        schema = tool.to_param()["function"]["parameters"]
        assert schema["additionalProperties"] is False
        assert "session" not in schema.get("properties", {})
    json.dumps(tools.to_params())


@pytest.mark.asyncio
async def test_tool_binds_session_and_task_and_rejects_overrides(monkeypatch):
    calls = []

    async def update(session, rowid: str, warehouse: str, value: str):
        calls.append((session, rowid, warehouse, value))
        return {"ok": True}

    monkeypatch.setattr(agent_tools, "page_functions", lambda: {"update": update})
    current = context(warehouse="指定仓")
    tool = agent_tools.build_publish_tools(current).get_tool("dxm_update")
    assert (await tool.execute(value="10", rowid="other")).error
    assert (await tool.execute(value="10", warehouse="别的仓")).error
    assert (await tool.execute(value="10", arbitrary=True)).error
    assert not calls
    assert not (await tool.execute(value="10")).error
    assert calls == [(current.session, "123", "指定仓", "10")]


@pytest.mark.asyncio
async def test_failed_tool_returns_observation_and_allows_next_action(monkeypatch):
    async def broken(session):
        raise RuntimeError("下拉框遮挡")

    monkeypatch.setattr(agent_tools, "page_functions", lambda: {"broken": broken})
    current = context()
    tools = agent_tools.build_publish_tools(current)
    assert "下拉框遮挡" in (await tools.execute(name="dxm_broken", tool_input={})).error
    result = await tools.execute(name="dxm_stage_titles", tool_input={})
    assert not result.error
    assert current.verified == {"status": "ok"}
    assert len(current.history) == 2


@pytest.mark.asyncio
async def test_later_operation_invalidates_previous_verification():
    current = context()
    tools = agent_tools.build_publish_tools(current)
    await tools.execute(name="dxm_stage_titles", tool_input={})
    assert current.verified
    await tools.execute(name="dxm_evaluate", tool_input={"code": "({ok:true})"})
    assert current.verified is None


@pytest.mark.asyncio
async def test_screenshot_is_image_and_cdp_rejects_other_commands():
    current = context()
    # 【mock 必须照 browser.cdp 的真实返回形状】它返回的是 {"ok", "data": <CDP 返回体>}
    # 这层包装，Page.captureScreenshot 的图像数据在里层 data 里。原 mock 少了这层，
    # 于是「把外层 data 当 base64」的缺陷在测试里看不出来、一路带到真机
    # （2026-09-11 实测每次截图都报 base64_image 校验错）。
    current.session.cdp = AsyncMock(return_value={"ok": True, "data": {"data": "base64-image"}})
    tools = agent_tools.build_publish_tools(current)
    assert (await tools.execute(name="dxm_screenshot", tool_input={})).base64_image == "base64-image"
    current.session.cdp.reset_mock()
    assert (await tools.execute(name="dxm_cdp_input", tool_input={
        "method": "Browser.close", "params": {},
    })).error
    current.session.cdp.assert_not_awaited()


@pytest.mark.parametrize("do_publish,stage", [(False, "publish"), (True, "save")])
def test_publish_tool_requires_current_stage_and_authorization(do_publish, stage):
    current = context(stage, do_publish=do_publish)
    current.handlers["publish"] = AsyncMock()
    assert "dxm_stage_publish" not in agent_tools.build_publish_tools(current).tool_map


@pytest.mark.asyncio
@pytest.mark.parametrize("landed,expected,calls", [
    ({"online": {"found": True}}, "ok", 0),
    ({"online": {"found": False}, "draft": {"found": False}}, "fail", 0),
    ({"draft": {"found": True}}, "ok", 1),
    ({"fail": {"found": True}}, "ok", 1),
])
async def test_publish_checks_existing_result_before_resubmitting(monkeypatch, landed, expected, calls):
    from app.publish import persistence

    monkeypatch.setattr(persistence, "_publish_landed", AsyncMock(return_value=landed))
    current = context("publish", do_publish=True, state={"stages": {"save": {"status": "ok"}}})
    result = await current.run_stage("publish")
    assert result["status"] == expected
    assert current.handlers["publish"].await_count == calls


@pytest.mark.asyncio
async def test_recovery_preserves_original_failure_without_verified_tool(monkeypatch):
    monkeypatch.setattr(recovery, "_create_agent", lambda tools: SimpleNamespace(run=AsyncMock(return_value="success")))
    current = context()
    result = await recovery.recover_stage(current.ctx, current.session, "titles", {
        "status": "fail", "note": "原错误",
    }, current.handlers, current.emit)
    assert result["status"] == "fail"
    assert "原错误" in result["note"]


@pytest.mark.asyncio
async def test_recovery_uses_verified_result_and_propagates_context(monkeypatch):
    current = context()

    async def handler(ctx, session, emit):
        ctx["title"] = "回写标题"
        return {"status": "ok", "note": "标题已回读"}

    current.handlers["titles"] = handler

    def factory(tools):
        async def run(request):
            assert json.loads(request)["failed_stage"] == "titles"
            await tools.execute(name="dxm_stage_titles", tool_input={})
            return "完成"
        return SimpleNamespace(run=run)

    monkeypatch.setattr(recovery, "_create_agent", factory)
    result = await recovery.recover_stage(current.ctx, current.session, "titles", {
        "status": "fail", "note": "原错误",
    }, current.handlers, current.emit)
    assert result["status"] == "ok"
    assert current.ctx["title"] == "回写标题"
    assert result["recovery"]["history"][0]["tool"] == "dxm_stage_titles"


@pytest.mark.asyncio
async def test_recovery_timeout_and_cancellation(monkeypatch):
    current = context()
    cancelled = asyncio.Event()

    async def run(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(recovery, "_create_agent", lambda tools: SimpleNamespace(run=run))
    monkeypatch.setattr(recovery, "TIMEOUT_SECONDS", 0.01)
    result = await recovery.recover_stage(current.ctx, current.session, "titles", {
        "status": "fail", "note": "原错误",
    }, current.handlers, current.emit)
    assert result["status"] == "fail" and "超时" in result["note"]
    assert cancelled.is_set()
    monkeypatch.setattr(recovery, "TIMEOUT_SECONDS", 30)
    task = asyncio.create_task(recovery.recover_stage(
        current.ctx, current.session, "titles", {}, current.handlers, current.emit,
    ))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["1688", "pdd", "temu", "amazon"])
@pytest.mark.parametrize("raises", [False, True])
async def test_every_workflow_recovers_and_continues(tmp_path, monkeypatch, platform, raises):
    monkeypatch.setattr(state, "STATE_DIR", str(tmp_path))
    calls = []

    async def failed(ctx, session, emit):
        calls.append("initial")
        if raises:
            raise RuntimeError("失败异常")
        return {"status": "fail", "note": "失败返回"}

    async def next_stage(ctx, session, emit):
        calls.append("next")
        assert ctx["title"] == "已恢复"
        return {"status": "ok"}

    async def recover(ctx, session, stage, failure, handlers, emit):
        assert stage == "test_failure"
        assert failure["status"] == "fail"
        calls.append("agent")
        ctx["title"] = "已恢复"
        return {"status": "ok", "note": "恢复", "recovery": {"status": "ok"}}

    workflow = get_workflow(platform)
    monkeypatch.setattr(workflow.rules, "build_stages", lambda: (
        Stage("test_failure", "失败阶段", failed), Stage("test_next", "后续阶段", next_stage),
    ))
    monkeypatch.setattr(service, "recover_stage", recover)
    result = await service.publish_one(None, {"rowid": "123", "source_platform": platform}, "shop")
    assert result["status"] == "ok"
    assert calls == ["initial", "agent", "next"]
    saved = state.load_state("rowid-123")
    assert saved["title"] == "已恢复"
    assert saved["stages"]["test_failure"]["recovery"]["status"] == "ok"
