from unittest.mock import AsyncMock

import pytest

from app.publish.persistence import is_online_product


@pytest.mark.asyncio
@pytest.mark.parametrize("state, expected", [
    ("online", True), ("draft", False), ("offline", False),
])
async def test_exact_product_state(state, expected):
    session = AsyncMock()
    session.eval_json.return_value = {"rowid": "184807703149199877", "state": state}
    assert await is_online_product(session, "184807703149199877") is expected
    session.navigate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {}, {"error": "HTTP 500"}, {"rowid": "another", "state": "online"},
    {"rowid": "184807703149199877"},
])
async def test_unknown_or_mismatched_product_is_not_editable(response):
    session = AsyncMock()
    session.eval_json.return_value = response
    with pytest.raises(RuntimeError, match="184807703149199877"):
        await is_online_product(session, "184807703149199877")
