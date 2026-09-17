"""玩具描述图按视觉销售重点筛选，并在发布前限制为最多 10 张。"""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.publish import vision
from app.publish.stages import description, description_images, prewarm_access, prewarm_plans


def _modules(count):
    return [{"pos": position, "url": f"https://cdn/{position}.jpg"}
            for position in range(1, count + 1)]


def _mock_vision(monkeypatch, modules, *, toy=True, deleted=(), unreachable=()):
    calls = {"rankings": [], "audits": []}
    unavailable = {f"https://cdn/{position}.jpg" for position in unreachable}
    monkeypatch.setattr(vision, "image_ref", lambda url: "" if url in unavailable else url)

    async def ask(prompt, images, **kwargs):
        if kwargs["what"] == "阶段⑬玩具描述图销售重点排序":
            calls["rankings"].append(images)
            assert "尺寸" in prompt and "功能" in prompt and "玩法" in prompt
            assert "不能直接按原始位置截断" in prompt
            positions = [int(url.rsplit("/", 1)[1].split(".")[0]) for url in images]
            return {"ranking": sorted(positions, reverse=True)}
        if kwargs["what"] == "阶段⑬keep图中文复核":
            calls["audits"].extend(images)
            return {"dirty": []}
        assert kwargs["what"] == "阶段⑬描述图规划"
        assert "即使已有实测尺寸也要保留" in prompt
        actions = []
        for module in modules:
            position = module["pos"]
            if position in unreachable:
                continue
            action = "delete" if position in deleted else "keep"
            if position == 20:
                action = "sizechart"
            elif position in (1, 19):
                action = "replace"
            actions.append({"pos": position, "action": action})
        return {"isToy": toy, "actions": actions}

    monkeypatch.setattr(vision, "ask_json_with_images", ask)
    return calls


@pytest.mark.asyncio
async def test_late_sales_images_retained_before_editing_and_audit(monkeypatch):
    modules = _modules(20)
    calls = _mock_vision(monkeypatch, modules)
    plan = await vision.plan_desc(modules, {"title": "弹珠机玩具", "sizeMeasurements": {"长": 30}})

    assert plan["maxImages"] == 10
    assert plan["delete"] == list(range(1, 11))
    assert {item["pos"] for item in plan["replace"]} == {19, 20}
    assert next(item for item in plan["replace"] if item["pos"] == 20)["sizechart"]
    assert len(plan["keep"]) + len(plan["replace"]) == 10
    assert plan["rankedUrls"][:2] == ["https://cdn/20.jpg", "https://cdn/19.jpg"]
    assert len(calls["rankings"][0]) == 20
    assert set(calls["audits"]) == {f"https://cdn/{position}.jpg" for position in range(11, 19)}


@pytest.mark.asyncio
@pytest.mark.parametrize("count,toy,deleted", [(10, True, ()), (20, False, ()), (20, True, tuple(range(2, 12)))])
async def test_no_ranking_when_not_needed(monkeypatch, count, toy, deleted):
    modules = _modules(count)
    calls = _mock_vision(monkeypatch, modules, toy=toy, deleted=deleted)
    plan = await vision.plan_desc(modules, {"sizeMeasurements": {"长": 30}})

    assert not calls["rankings"]
    assert plan["delete"] == list(deleted)
    assert len(plan["keep"]) + len(plan["replace"]) == count - len(deleted)
    if not toy:
        assert "maxImages" not in plan


@pytest.mark.asyncio
async def test_unreachable_images_reserve_slots_without_shifting_positions(monkeypatch):
    modules = _modules(20)
    calls = _mock_vision(monkeypatch, modules, unreachable=(2,))
    plan = await vision.plan_desc(modules, {"sizeMeasurements": {"长": 30}})

    assert plan["unreachable"] == [2]
    assert 2 in plan["keep"]
    assert len(plan["keep"]) + len(plan["replace"]) == 10
    assert plan["delete"] == [1] + list(range(3, 12))
    assert "https://cdn/2.jpg" not in calls["rankings"][0]


@pytest.mark.asyncio
async def test_too_many_unreachable_images_cannot_bypass_cap(monkeypatch):
    modules = _modules(20)
    _mock_vision(monkeypatch, modules, unreachable=tuple(range(1, 12)))
    with pytest.raises(RuntimeError, match="取不到的图片过多"):
        await vision.plan_desc(modules, {"sizeMeasurements": {"长": 30}})


@pytest.mark.asyncio
@pytest.mark.parametrize("ranking", [[1, 1], [1], [1, 3], ["1", 2], [True, 2]])
async def test_invalid_visual_ranking_rejected(monkeypatch, ranking):
    async def ask(prompt, images, **kwargs):
        assert kwargs["result_model"]
        return {"ranking": ranking}

    monkeypatch.setattr(vision, "ask_json_with_images", ask)
    pairs = [(module, module["url"]) for module in _modules(2)]
    with pytest.raises(ValidationError):
        await vision._rank_toy_desc(pairs)


@pytest.mark.asyncio
async def test_prewarm_edits_only_selected_images_and_remaps_cap(monkeypatch):
    modules = _modules(20)
    _mock_vision(monkeypatch, modules)
    monkeypatch.setattr(prewarm_plans, "_desc_modules_from_raw", lambda workdir: modules)
    prepare = AsyncMock(return_value={})
    monkeypatch.setattr(description_images, "_prewarm_desc_images", prepare)
    result = await prewarm_plans._prewarm_desc(
        {"workdir": "unused"}, {"sizeMeasurements": {"长": 30}}, AsyncMock())

    assert {item["pos"] for item in prepare.call_args.args[1]} == {19, 20}
    page_modules = [{**module, "pos": position}
                    for position, module in enumerate(reversed(modules), 1)]
    remapped = prewarm_plans._replan_desc_by_url(result["plan"], page_modules)
    assert remapped["maxImages"] == 10
    assert remapped["delete"] == list(range(11, 21))
    assert {item["pos"] for item in remapped["replace"]} == {1, 2}
    assert next(item for item in remapped["replace"] if item["pos"] == 1)["sizechart"]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_delete,after_save,expected", [(10, 10, "ok"), (11, 10, "fail"), (10, 11, "fail"), (10, None, "fail")])
async def test_stage_verifies_actual_count(monkeypatch, tmp_path, after_delete, after_save, expected):
    modules = _modules(20)
    plan = {"status": "ok", "delete": list(range(1, 11)), "replace": [],
            "keep": list(range(11, 21)), "maxImages": 10}
    monkeypatch.setattr(description, "desc_map", AsyncMock(return_value={"status": "ok", "modules": modules}))
    monkeypatch.setattr(description.state, "_load_info", lambda path: {"sizeMeasurements": {"长": 30}})
    monkeypatch.setattr(description, "desc_text_delete_all", AsyncMock(return_value={"status": "ok"}))
    monkeypatch.setattr(prewarm_access, "_await_prewarm", AsyncMock(return_value=None))
    monkeypatch.setattr(vision, "plan_desc", AsyncMock(return_value=plan))
    monkeypatch.setattr(description, "desc_delete", AsyncMock(return_value={
        "status": "ok", "deleted": plan["delete"], "countAfter": after_delete}))
    prepare = AsyncMock(return_value={})
    monkeypatch.setattr(description_images, "_prewarm_desc_images", prepare)
    monkeypatch.setattr(description_images, "_rehost_desc_keeps", AsyncMock(return_value={"done": 10}))
    monkeypatch.setattr(description, "desc_save", AsyncMock(return_value={"status": "ok", "descImgs": after_save}))
    close = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(description, "ensure_desc_closed", close)

    result = await description._st_desc({"info_path": "unused", "workdir": str(tmp_path)}, None, AsyncMock())

    assert result["status"] == expected
    close.assert_awaited()
    if expected == "fail":
        assert "10" in result["note"]
    if after_delete > 10:
        prepare.assert_not_awaited()
