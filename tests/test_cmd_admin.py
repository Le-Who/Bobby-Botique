"""Tests for app.handlers.cmd_admin — admin-only commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_admin_update(user_id=1, args=None):
    """Create a minimal mock Update + Context for admin commands."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 100
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []
    return update, context


# ── /admin ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_command_shows_help():
    """admin_command returns help text with list of commands."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
    ):
        from app.handlers.cmd_admin import admin_command

        await admin_command(update, context)

    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Админ" in reply_text or "admin" in reply_text.lower()


# ── /adduser ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_user_no_args():
    """add_user_command with no args shows usage."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
    ):
        from app.handlers.cmd_admin import add_user_command

        await add_user_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "Использование" in reply_text or "ID" in reply_text


@pytest.mark.asyncio
async def test_add_user_success():
    """add_user_command with valid ID."""
    update, context = make_admin_update(args=["999"])

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
        patch("app.handlers.cmd_admin.authorize_user", new_callable=AsyncMock) as mock_auth,
        patch("app.handlers.cmd_admin.invalidate_user_auth_cache", new_callable=AsyncMock),
    ):
        from app.handlers.cmd_admin import add_user_command

        await add_user_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "999" in reply_text or "добавлен" in reply_text.lower()
    mock_auth.assert_awaited_once_with(999)


# ── /deluser ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_del_user_no_args():
    """del_user_command with no args shows usage."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
    ):
        from app.handlers.cmd_admin import del_user_command

        await del_user_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "Использование" in reply_text or "ID" in reply_text


# ── /listusers ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_command():
    """list_users_command returns user list."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
        patch(
            "app.handlers.cmd_admin.list_authorized_users",
            new_callable=AsyncMock,
            return_value=[123, 456],
        ),
    ):
        from app.handlers.cmd_admin import list_users_command

        await list_users_command(update, context)

    update.message.reply_text.assert_awaited_once()


# ── /clearcache ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_cache_command():
    """clear_cache_command clears cache and reports."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
        patch("app.handlers.cmd_admin.get_cache_stats", return_value={"size": 0}),
    ):
        from app.handlers.cmd_admin import clear_cache_command

        await clear_cache_command(update, context)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "✅" in reply_text or "кэш" in reply_text.lower() or "cache" in reply_text.lower()


# ── Non-admin access ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_command_denied_for_non_admin():
    """Admin commands deny access for non-admin users."""
    update, context = make_admin_update(user_id=999)

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=False),
    ):
        from app.handlers.cmd_admin import admin_command

        await admin_command(update, context)

    # Should reply with access denied
    reply_text = update.message.reply_text.call_args[0][0]
    assert "нет прав" in reply_text.lower() or "❌" in reply_text


# ── /reload ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_config_command():
    """reload_config_command reloads config and confirms."""
    update, context = make_admin_update()

    with (
        patch(
            "app.utils.decorators.is_authorized",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.utils.decorators.is_admin", return_value=True),
        patch("app.handlers.cmd_admin.settings") as mock_settings,
    ):
        mock_settings.reload = MagicMock()

        from app.handlers.cmd_admin import reload_config_command

        await reload_config_command(update, context)

    assert update.message.reply_text.await_count >= 1
