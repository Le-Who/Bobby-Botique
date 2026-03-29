"""Tests for app.streaming — constants, finish reasons, StreamingWriter (draft+classic)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.streaming import (
    _BLOCKED_FINISH_REASONS,
    _TRUNCATED_FINISH_REASONS,
    EDIT_DEBOUNCE_S,
    MIN_CHUNK_SIZE,
    STREAM_MSG_LIMIT,
    STREAMING_INDICATOR,
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


# ── StreamingWriter init ─────────────────────────────────────────────────────


class TestOverflowFormattingContext:
    """Verify formatting context is carried across overflow message boundaries."""

    @pytest.fixture()
    def mock_adapter(self):
        """Create a mock adapter that tracks edit_message and reply_new_message calls."""
        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        new_adapter = MagicMock()
        new_adapter.edit_message = AsyncMock()
        new_adapter.reply_new_message = AsyncMock(return_value=MagicMock())
        adapter.reply_new_message = AsyncMock(return_value=new_adapter)
        adapter._bot = None
        return adapter

    @pytest.mark.asyncio
    async def test_overflow_preserves_bold_context(self, mock_adapter, monkeypatch):
        """When bold opens in msg1 and closes in msg2, both get proper markers."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_adapter)
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Simulate text with bold opening but not closing
        long_text = "Normal intro paragraph.\n\n**This is a bold section that goes on and on and keeps going forever**"
        writer._buffer = long_text
        writer._full_text = long_text

        await writer._overflow_to_new_message()

        # The frozen text (first message) should have balanced <b> tags
        frozen_edit_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_edit_call[0][0]
        assert frozen_html.count("<b>") == frozen_html.count("</b>")

        # The remainder buffer should have ** prepended to continue bold
        assert writer._buffer.startswith("**")

    @pytest.mark.asyncio
    async def test_overflow_preserves_code_block_context(self, mock_adapter, monkeypatch):
        """When a code block opens in msg1, msg2 gets a reopened fence."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_adapter)
        writer._debounce_s = 0
        writer._min_chunk = 0

        text = "Some intro text:\n```python\ndef hello():\n    print('hello world')\n    return True\n"
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        # The frozen message should contain a <pre> with </pre> (closed code block)
        frozen_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_call[0][0]
        assert frozen_html.count("<pre>") == frozen_html.count("</pre>")

    @pytest.mark.asyncio
    async def test_overflow_preserves_inline_code_context(self, mock_adapter, monkeypatch):
        """When inline code ` opens in msg1, msg2 gets a reopened backtick."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_adapter)
        writer._debounce_s = 0
        writer._min_chunk = 0

        text = "Use the `very_long_function_name_that_keeps_going_and_going_across_boundary"
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        frozen_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_call[0][0]
        # <code> should be balanced
        assert frozen_html.count("<code>") == frozen_html.count("</code>")

    @pytest.mark.asyncio
    async def test_overflow_no_extra_markers_when_clean(self, mock_adapter, monkeypatch):
        """Clean split (all formatting closed) should not add extra markers."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_adapter)
        writer._debounce_s = 0
        writer._min_chunk = 0

        text = "**Bold section done.** Now normal text that is long enough to overflow."
        writer._buffer = text
        writer._full_text = text

        await writer._overflow_to_new_message()

        # reply_text should not start with bold
        reply_call = mock_adapter.reply_new_message.call_args
        reply_html = reply_call[0][0]
        # Should not have <b> tag at the very beginning (before content)
        stripped = reply_html.lstrip()
        assert not stripped.startswith("<b>")


class TestDetectOpenMarkdownExtended:
    """Tests for _detect_open_markdown underscore italic and strikethrough tracking."""

    def test_open_underscore_italic(self):
        from app.streaming import _detect_open_markdown

        suffix, prefix = _detect_open_markdown("Hello _italic text")
        assert "_" in suffix
        assert "_" in prefix

    def test_closed_underscore_italic(self):
        from app.streaming import _detect_open_markdown

        suffix, prefix = _detect_open_markdown("Hello _italic_ text")
        assert "_" not in suffix
        assert "_" not in prefix

    def test_open_strikethrough(self):
        from app.streaming import _detect_open_markdown

        suffix, prefix = _detect_open_markdown("Hello ~~deleted text")
        assert "~~" in suffix
        assert "~~" in prefix

    def test_closed_strikethrough(self):
        from app.streaming import _detect_open_markdown

        suffix, prefix = _detect_open_markdown("Hello ~~deleted~~ text")
        assert "~~" not in suffix
        assert "~~" not in prefix

    def test_combined_bold_underscore_strikethrough(self):
        from app.streaming import _detect_open_markdown

        suffix, prefix = _detect_open_markdown("**bold _italic ~~strike")
        assert "**" in suffix
        assert "_" in suffix
        assert "~~" in suffix


# ── Regression tests for multi-message streaming bugs ────────────────────────


class TestOverflowRetryStorm:
    """BUG-10: Verify overflow error handling and hot-loop prevention."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_on_overflow_failure(self):
        """If reply_new_message fails 3 times, it should circuit-break and not hot-loop."""
        from app.streaming import STREAM_MSG_LIMIT, StreamingWriter

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.send_draft = AsyncMock()
        adapter.reply_new_message = AsyncMock(side_effect=Exception("Unmatched end tag"))
        adapter.send_final_message = AsyncMock()
        # Mock prepare_draft_mode to succeed
        adapter.delete_placeholder = AsyncMock()

        writer = StreamingWriter(adapter)
        writer._use_drafts = True

        oversized = "A" * (STREAM_MSG_LIMIT + 100)

        # Write 1: hits overflow, replies new message, 1st failure → retry
        await writer.write(oversized)
        assert not getattr(writer, "_overflow_failed", False), "Should not circuit-break after 1 retry"

        # Write 2: 2nd failure → retry again
        writer._last_edit_time = 0  # Force flush
        await writer.write("")
        assert not getattr(writer, "_overflow_failed", False), "Should not circuit-break after 2 retries"

        # Write 3: 3rd failure → circuit breaker engages
        writer._last_edit_time = 0  # Force flush
        await writer.write("")
        assert hasattr(writer, "_overflow_failed")
        assert writer._overflow_failed is True


class TestSanitizeOverflowRemainder:
    """BUG-10: Maintain balanced HTML across overflow chunks."""

    @pytest.mark.asyncio
    async def test_remainder_is_sanitized(self):
        """The remainder of an overflow should be sanitized before sending."""
        from app.streaming import STREAM_MSG_LIMIT, StreamingWriter

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.send_draft = AsyncMock()
        new_adapter = MagicMock()
        adapter.reply_new_message = AsyncMock(return_value=new_adapter)
        adapter.delete_placeholder = AsyncMock()

        writer = StreamingWriter(adapter)
        writer._use_drafts = True

        # Create text that will be split right after the open markdown
        # _detect_open_markdown will prefix the remainder with `_`
        # When formatted, `_` matches the second `_`, creating `<i>` tags
        # By overlapping it with a `<code>` block, we verify it is sanitized
        oversized = "A" * STREAM_MSG_LIMIT + "\n_italic text that `overflows_ and overlaps`"

        await writer.write(oversized)

        # The remainder message uses reply_new_message
        adapter.reply_new_message.assert_called_once()
        call_args = adapter.reply_new_message.call_args
        formatted_initial = call_args[0][0]

        # If it was sanitized, the initial `<i>` (from `_`) must be properly closed
        # before the `<code>` tag ends, or properly nested
        assert "<i>" in formatted_initial
        assert "</i>" in formatted_initial
        assert "</i>" in formatted_initial
