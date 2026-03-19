"""Tests for decorator context propagation (set_user_context, set_request_id)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.decorators import admin_only, authorized_only


def _make_update(
    *,
    user_id: int = 111,
    chat_id: int = 222,
    update_id: int = 999,
    is_callback: bool = False,
):
    """Create a mock Update with standard fields."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.update_id = update_id

    if is_callback:
        update.callback_query = MagicMock()
        update.callback_query.id = "cb-42"
        update.callback_query.from_user.id = user_id
        update.callback_query.message.chat.id = chat_id
        update.message = None
    else:
        update.callback_query = None
        update.message = MagicMock()

    return update


# ── authorized_only ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorized_only_sets_user_context():
    update = _make_update(user_id=100, chat_id=200)
    context = MagicMock()

    @authorized_only
    async def handler(u, c):
        pass

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.set_user_context") as mock_ctx,
        patch("app.utils.decorators.set_request_id"),
    ):
        await handler(update, context)
        mock_ctx.assert_called_once_with(100, 200)


@pytest.mark.asyncio
async def test_authorized_only_sets_request_id_for_message():
    update = _make_update(user_id=100, chat_id=200, update_id=555)
    context = MagicMock()

    @authorized_only
    async def handler(u, c):
        pass

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.set_user_context"),
        patch("app.utils.decorators.set_request_id") as mock_rid,
    ):
        await handler(update, context)
        mock_rid.assert_called_once_with("tgcmd-200-555")


@pytest.mark.asyncio
async def test_authorized_only_sets_request_id_for_callback():
    update = _make_update(user_id=100, chat_id=200, is_callback=True)
    context = MagicMock()

    @authorized_only
    async def handler(u, c):
        pass

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.set_user_context"),
        patch("app.utils.decorators.set_request_id") as mock_rid,
    ):
        await handler(update, context)
        mock_rid.assert_called_once_with("tgcb-100-cb-42")


@pytest.mark.asyncio
async def test_authorized_only_context_set_even_if_unauthorized():
    """Context should be set BEFORE the auth check so denial logs include user_id."""
    update = _make_update(user_id=100, chat_id=200)
    update.message.reply_text = AsyncMock()
    context = MagicMock()

    @authorized_only
    async def handler(u, c):
        pass

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.utils.decorators.set_user_context") as mock_ctx,
        patch("app.utils.decorators.set_request_id"),
    ):
        await handler(update, context)
        mock_ctx.assert_called_once_with(100, 200)


# ── admin_only ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_only_sets_user_context():
    update = _make_update(user_id=300, chat_id=400)
    context = MagicMock()

    @admin_only
    async def handler(u, c):
        pass

    with (
        patch("app.utils.decorators.is_admin", return_value=True),
        patch("app.utils.decorators.set_user_context") as mock_ctx,
        patch("app.utils.decorators.set_request_id"),
    ):
        await handler(update, context)
        mock_ctx.assert_called_once_with(300, 400)


@pytest.mark.asyncio
async def test_admin_only_sets_request_id():
    update = _make_update(user_id=300, chat_id=400, update_id=777)
    context = MagicMock()

    @admin_only
    async def handler(u, c):
        pass

    with (
        patch("app.utils.decorators.is_admin", return_value=True),
        patch("app.utils.decorators.set_user_context"),
        patch("app.utils.decorators.set_request_id") as mock_rid,
    ):
        await handler(update, context)
        mock_rid.assert_called_once_with("tgcmd-400-777")
