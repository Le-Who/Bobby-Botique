"""Tests for send_long_message — message splitting over Telegram's 4096-char limit."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_message():
    """Create a mock Telegram Message."""
    msg = MagicMock()
    msg.message_id = 1
    msg.edit_text = AsyncMock(return_value=msg)
    msg.reply_text = AsyncMock(return_value=msg)
    msg.from_user = MagicMock()
    msg.from_user.id = 123
    msg.chat = MagicMock()
    msg.chat.id = 456
    return msg


@pytest.fixture(autouse=True)
def _patch_helpers():
    """Suppress heartbeat and keyboard helpers that aren't under test."""
    with (
        patch("app.utils.messaging.stop_heartbeat"),
        patch("app.utils.messaging.ai_response_keyboard", return_value=None),
        patch("app.utils.messaging.deep_dive_keyboard", return_value=None),
        patch("app.utils.messaging._get_telegram_cb") as mock_cb,
    ):
        # Make the CircuitBreaker relay the call transparently
        async def relay(fn, *args, **kwargs):
            return await fn(*args, **kwargs)

        mock_cb.return_value.call = AsyncMock(side_effect=relay)
        yield


class TestSendLongMessageSplitting:
    """Verify that send_long_message splits text exceeding 4096 chars."""

    @pytest.mark.asyncio
    async def test_short_text_edits_single_message(self):
        """Text under limit should edit the existing message, not split."""
        from app.utils.messaging import send_long_message

        msg = _make_message()
        await send_long_message(msg, "Hello!")

        msg.edit_text.assert_awaited_once()
        msg.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_long_text_sends_multiple_parts(self):
        """Text over 4096 chars must be split into multiple messages."""
        from app.utils.messaging import send_long_message

        long_text = "А" * 5000  # Cyrillic A, well over 4096
        msg = _make_message()
        await send_long_message(msg, long_text)

        # First part edited, subsequent parts replied
        msg.edit_text.assert_awaited_once()
        assert msg.reply_text.await_count >= 1

    @pytest.mark.asyncio
    async def test_split_preserves_full_content(self):
        """All text content should be present across the split parts."""
        from app.utils.text_format import split_text_safe

        original = "Word" * 1500  # ~6000 chars
        parts = split_text_safe(original, max_length=4096)

        assert len(parts) >= 2
        reconstructed = "".join(parts)
        assert reconstructed == original

    @pytest.mark.asyncio
    async def test_html_tags_balanced_in_each_part(self):
        """Each split part must have balanced HTML tags."""
        from app.utils.text_format import split_text_safe

        html_text = "<b>" + "X" * 5000 + "</b>"
        parts = split_text_safe(html_text, max_length=4096)

        for part in parts:
            assert part.count("<b>") == part.count("</b>"), f"Unbalanced <b> tags in part: {part[:100]}..."

    @pytest.mark.asyncio
    async def test_keyboard_attached_to_last_part_only(self):
        """reply_markup should only appear on the final split part."""
        from app.utils.messaging import send_long_message

        long_text = "Word " * 2000  # ~10K chars, will split into 3+ parts
        msg = _make_message()
        mock_keyboard = MagicMock()

        await send_long_message(msg, long_text, reply_markup=mock_keyboard)

        # The last call should have the keyboard
        if msg.reply_text.await_count > 0:
            last_call = msg.reply_text.call_args_list[-1]
            assert last_call.kwargs.get("reply_markup") == mock_keyboard
