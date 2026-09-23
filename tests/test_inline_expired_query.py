from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from app.handlers.inline import handle_inline_query


def _inline_update(error: BadRequest):
    query = SimpleNamespace(
        query="hello",
        from_user=SimpleNamespace(id=7, language_code="ru"),
        answer=AsyncMock(side_effect=error),
    )
    return SimpleNamespace(inline_query=query), SimpleNamespace(bot=SimpleNamespace(first_name="TestBot"))


@pytest.mark.asyncio
async def test_expired_inline_answer_is_discarded_without_admin_critical_error() -> None:
    update, context = _inline_update(BadRequest("Query is too old and response timeout expired or query ID is invalid"))

    await handle_inline_query(update, context)

    update.inline_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_inline_result_still_surfaces_as_error() -> None:
    update, context = _inline_update(BadRequest("Wrong file identifier"))

    with pytest.raises(BadRequest, match="Wrong file identifier"):
        await handle_inline_query(update, context)
