from unittest.mock import AsyncMock, Mock

import pytest

from app.publish import category, category_api


@pytest.fixture
def no_retry_delay(monkeypatch):
    delay = AsyncMock()
    monkeypatch.setattr(category_api.asyncio, "sleep", delay)
    return delay


@pytest.mark.asyncio
async def test_category_request_recovers_from_busy_response(no_retry_delay):
    rows = [{"catId": "25886", "catName": "婴幼儿音乐玩具", "isLeaf": True}]
    session = AsyncMock()
    session.eval_json.side_effect = [
        {"ok": False, "msg": "系统繁忙,请稍后重试!!!"},
        {"ok": True, "rows": rows},
    ]

    assert await category_api.fetch_children(session, "shop", "25885") == rows
    assert session.eval_json.await_count == 2
    assert all(call.kwargs["arg"] == "25885" for call in session.eval_json.call_args_list)
    no_retry_delay.assert_awaited_once_with(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"ok": False, "msg": "系统繁忙"},
    {"ok": False, "status": 503},
    {"ok": False, "err": "Failed to fetch"},
])
async def test_category_request_failure_raises_after_bounded_retries(no_retry_delay, failure):
    session = AsyncMock()
    session.eval_json.return_value = failure

    with pytest.raises(RuntimeError, match="父级 25885.*已尝试 3 次"):
        await category_api.fetch_children(session, "shop", "25885")

    assert session.eval_json.await_count == 3
    assert no_retry_delay.await_count == 2


@pytest.mark.asyncio
async def test_successful_empty_response_is_distinct_from_failure(no_retry_delay):
    session = AsyncMock()
    session.eval_json.return_value = {"ok": True, "rows": []}

    assert await category_api.fetch_children(session, "shop", "25885") == []
    session.eval_json.assert_awaited_once()
    no_retry_delay.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_shop_fails_without_request():
    session = AsyncMock()
    with pytest.raises(RuntimeError, match="缺少店铺"):
        await category_api.fetch_children(session, "")
    session.eval_json.assert_not_awaited()


@pytest.fixture
def category_walk(monkeypatch, no_retry_delay):
    responses = {
        "": [{"ok": True, "rows": [
            {"catId": "25439", "catName": "玩具与游戏", "isLeaf": False}]}],
        "25439": [{"ok": True, "rows": [
            {"catId": "25885", "catName": "婴幼玩具", "isLeaf": False}]}],
        "25885": [{"ok": True, "rows": [
            {"catId": "25886", "catName": "婴幼儿音乐玩具", "isLeaf": True}]}],
    }

    async def evaluate(code, *, arg=None):
        if code == category._JS_CATEGORY_FORM_READY:
            return {"ready": True}
        if code == category._JS_OPEN_CAT_MODAL:
            return {"opened": True}
        if "/api/popTemuCategory/list.json" in code:
            return responses[arg].pop(0)
        raise AssertionError(code)

    async def poll(probe, predicate, **kwargs):
        return await probe()

    session = AsyncMock()
    session.eval_json.side_effect = evaluate
    session.wait_for.return_value = {"ready": True, "n": 1}
    monkeypatch.setattr(category.navigation, "open_edit", AsyncMock())
    monkeypatch.setattr(category.common, "_poll_until", poll)
    monkeypatch.setattr(category_api, "fetch_shop_id", AsyncMock(return_value="shop"))
    monkeypatch.setattr(category, "_try_default_category", AsyncMock(return_value=None))
    monkeypatch.setattr(category, "_try_cached_category", AsyncMock(return_value=None))
    monkeypatch.setattr(category, "_cat_columns", AsyncMock(return_value={"n": 8}))
    monkeypatch.setattr(category, "_pick_category", AsyncMock(return_value=(0, "匹配")))
    monkeypatch.setattr(category, "_click_cat_in_column", AsyncMock(return_value={"clicked": True}))
    monkeypatch.setattr(category, "_confirm_cat", AsyncMock(return_value={"confirmed": True}))
    monkeypatch.setattr(category, "read_current_category", AsyncMock(
        return_value="玩具与游戏 > 婴幼玩具 > 婴幼儿音乐玩具"))
    monkeypatch.setattr(category.cache, "remember_category", Mock())
    return session, responses


@pytest.mark.asyncio
async def test_busy_parent_never_confirms_or_returns_false_leaf(category_walk):
    session, responses = category_walk
    responses["25885"] = [{"ok": False, "msg": "系统繁忙"}] * 3

    with pytest.raises(RuntimeError, match="父级 25885.*系统繁忙"):
        await category.auto_cat(session, "184807703152221335", "宝宝手风琴玩具", lookahead=False)

    category._confirm_cat.assert_not_awaited()
    category.cache.remember_category.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_id", ["", "25885"])
async def test_empty_candidates_cannot_be_treated_as_leaf(category_walk, parent_id):
    session, responses = category_walk
    responses[parent_id] = [{"ok": True, "rows": []}]

    with pytest.raises(RuntimeError, match="尚未到达叶子类目"):
        await category.auto_cat(session, "rowid", "宝宝手风琴玩具", lookahead=False)

    category._confirm_cat.assert_not_awaited()
    category.cache.remember_category.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("snippet", [None, "", "玩具与游戏 > 音乐玩具 > 其他（音乐玩具）"])
async def test_readback_failure_stops_before_attributes(category_walk, snippet):
    session, responses = category_walk
    category.read_current_category.return_value = snippet

    with pytest.raises(RuntimeError, match="类目确认未生效"):
        await category.auto_cat(session, "rowid", "宝宝手风琴玩具", lookahead=False)

    category._confirm_cat.assert_awaited_once()
    category.cache.remember_category.assert_not_called()


@pytest.mark.asyncio
async def test_recovered_request_reaches_verified_leaf(category_walk):
    session, responses = category_walk
    responses["25885"].insert(0, {"ok": False, "msg": "系统繁忙"})

    result = await category.auto_cat(session, "rowid", "宝宝手风琴玩具", lookahead=False)

    assert result["status"] == "ok"
    assert result["leafCatId"] == "25886"
    assert result["pathList"] == ["玩具与游戏", "婴幼玩具", "婴幼儿音乐玩具"]
    category._confirm_cat.assert_awaited_once()
    category.cache.remember_category.assert_called_once_with(
        result["pathList"], "宝宝手风琴玩具", ["25439", "25885", "25886"])


@pytest.mark.asyncio
async def test_depth_limit_cannot_confirm_parent(category_walk):
    session, responses = category_walk

    with pytest.raises(RuntimeError, match="仍未到叶子类目"):
        await category.auto_cat(session, "rowid", "宝宝手风琴玩具", max_levels=2, lookahead=False)

    category._confirm_cat.assert_not_awaited()
    category.cache.remember_category.assert_not_called()
