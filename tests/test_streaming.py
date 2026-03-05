"""Tests for app.streaming — constants, blocked/truncated finish reasons, StreamingWriter init."""

import pytest

from app.streaming import (
    EDIT_DEBOUNCE_S,
    MIN_CHUNK_SIZE,
    STREAMING_INDICATOR,
    STREAM_MSG_LIMIT,
    _BLOCKED_FINISH_REASONS,
    _TRUNCATED_FINISH_REASONS,
    StreamingWriter,
)


class TestStreamingConstants:
    """Verify streaming constants are sensible."""

    def test_debounce_is_positive(self):
        assert EDIT_DEBOUNCE_S > 0

    def test_min_chunk_size_positive(self):
        assert MIN_CHUNK_SIZE > 0

    def test_stream_msg_limit_under_telegram_max(self):
        # Telegram max is 4096, stream limit should have margin
        assert 3000 < STREAM_MSG_LIMIT < 4096

    def test_streaming_indicator_non_empty(self):
        assert len(STREAMING_INDICATOR) > 0

    def test_blocked_reasons_include_safety(self):
        assert "SAFETY" in _BLOCKED_FINISH_REASONS

    def test_blocked_reasons_include_recitation(self):
        assert "RECITATION" in _BLOCKED_FINISH_REASONS

    def test_truncated_reasons_include_max_tokens(self):
        assert "MAX_TOKENS" in _TRUNCATED_FINISH_REASONS

    def test_blocked_and_truncated_no_overlap(self):
        overlap = _BLOCKED_FINISH_REASONS & _TRUNCATED_FINISH_REASONS
        assert len(overlap) == 0, f"Overlap: {overlap}"


class TestStreamingWriterInit:
    """Test StreamingWriter construction (no real Telegram message needed)."""

    def test_custom_debounce(self):
        from unittest.mock import MagicMock
        msg = MagicMock()
        writer = StreamingWriter(msg, debounce_s=2.5)
        assert writer._debounce_s == 2.5

    def test_initial_state(self):
        from unittest.mock import MagicMock
        msg = MagicMock()
        writer = StreamingWriter(msg)
        assert writer.text == ""
        assert writer.edit_count == 0
        assert writer.message_count == 1  # Initial placeholder counts as 1


class TestOverflowFormattingContext:
    """Verify formatting context is carried across overflow message boundaries."""

    @pytest.fixture()
    def mock_message(self):
        """Create a mock Telegram message that tracks edit_text and reply_text calls."""
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        msg = MagicMock()
        msg.edit_text = AsyncMock()
        # reply_text returns a new mock message (the overflow message)
        new_msg = MagicMock()
        new_msg.edit_text = AsyncMock()
        new_msg.reply_text = AsyncMock(return_value=MagicMock())
        msg.reply_text = AsyncMock(return_value=new_msg)
        return msg

    @pytest.mark.asyncio
    async def test_overflow_preserves_bold_context(self, mock_message, monkeypatch):
        """When bold opens in msg1 and closes in msg2, both get proper markers."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_message, debounce_s=0)

        # Simulate text with bold opening but not closing
        long_text = "Normal intro paragraph.\n\n**This is a bold section that goes on and on and keeps going forever**"
        writer._buffer = long_text
        writer._full_text = long_text

        await writer._overflow_to_new_message()

        # The frozen text (first message) should have balanced <b> tags
        frozen_edit_call = mock_message.edit_text.call_args
        frozen_html = frozen_edit_call[0][0]
        assert frozen_html.count("<b>") == frozen_html.count("</b>")

        # The remainder buffer should have ** prepended to continue bold
        # (the bold was open in frozen, so it should be reopened in remainder)
        assert writer._buffer.startswith("**")

    @pytest.mark.asyncio
    async def test_overflow_preserves_code_block_context(self, mock_message, monkeypatch):
        """When a code block opens in msg1, msg2 gets a reopened fence."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_message, debounce_s=0)

        text = "Some intro text:\n```python\ndef hello():\n    print('hello world')\n    return True\n"
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        # The frozen message should contain a <pre> with </pre> (closed code block)
        frozen_call = mock_message.edit_text.call_args
        frozen_html = frozen_call[0][0]
        assert frozen_html.count("<pre>") == frozen_html.count("</pre>")

    @pytest.mark.asyncio
    async def test_overflow_preserves_inline_code_context(self, mock_message, monkeypatch):
        """When inline code ` opens in msg1, msg2 gets a reopened backtick."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_message, debounce_s=0)

        text = "Use the `very_long_function_name_that_keeps_going_and_going_across_boundary"
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        frozen_call = mock_message.edit_text.call_args
        frozen_html = frozen_call[0][0]
        # <code> should be balanced
        assert frozen_html.count("<code>") == frozen_html.count("</code>")

    @pytest.mark.asyncio
    async def test_overflow_no_extra_markers_when_clean(self, mock_message, monkeypatch):
        """Clean split (all formatting closed) should not add extra markers."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_message, debounce_s=0)

        text = "**Bold section done.** Now normal text that is long enough to overflow."
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        # reply_text should not start with bold
        reply_call = mock_message.reply_text.call_args
        reply_html = reply_call[0][0]
        # Should not have <b> tag at the very beginning (before content)
        stripped = reply_html.lstrip()
        assert not stripped.startswith("<b>")

