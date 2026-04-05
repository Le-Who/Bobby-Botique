"""Tests for app.streaming — constants, finish reasons, StreamingWriter (draft+classic).

Each test follows strict AAA. Tests covering overflow behaviour are aligned with
the current StreamingWriter implementation which has two paths:
  - Telegraph path (use_telegraph_fallback=True, default): freezes the current
    message with an indicator when it overflows, instead of creating a new one.
  - Classic path (use_telegraph_fallback=False): creates a new reply message on overflow.
"""

import asyncio
import time
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


# ── Markdown context detection tests ─────────────────────────────────────────


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


# ── Overflow formatting context tests ────────────────────────────────────────


class TestOverflowFormattingContext:
    """Verify formatting context is carried across overflow message boundaries.

    These tests use use_telegraph_fallback=False so that overflow triggers the
    classic path (reply_new_message) rather than the telegraph frozen path.
    This allows us to inspect both the frozen and the continuation message.
    """

    @pytest.fixture()
    def mock_adapter(self):
        """Create a mock adapter that tracks edit_message and reply_new_message calls."""
        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        new_adapter = MagicMock()
        new_adapter.edit_message = AsyncMock()
        adapter.reply_new_message = AsyncMock(return_value=new_adapter)
        adapter._bot = None
        return adapter

    @pytest.mark.asyncio
    async def test_overflow_frozen_message_has_balanced_bold_tags(self, mock_adapter, monkeypatch):
        """When bold opens but doesn't close before the overflow split point,
        the frozen (first) message must have balanced <b> HTML tags.

        Uses classic path (use_telegraph_fallback=False) so we can inspect the
        frozen edit.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        # Use classic path: telegraph=False → overflow creates new message
        writer = StreamingWriter(mock_adapter, use_telegraph_fallback=False)
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Text with bold that definitely runs over 80 chars when formatted
        # The split will happen somewhere in the middle, bold may be open
        long_text = "Intro paragraph.\n\n**This is a bold section that goes on and on and keeps going forever.**"
        writer._buffer = long_text
        writer._full_text = long_text

        # Act
        await writer._overflow_to_new_message()

        # Assert — frozen message HTML has balanced <b> tags
        frozen_edit_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_edit_call[0][0]
        assert frozen_html.count("<b>") == frozen_html.count("</b>"), (
            f"Unbalanced <b> tags in frozen HTML:\n{frozen_html}"
        )

    @pytest.mark.asyncio
    async def test_overflow_frozen_message_has_balanced_pre_tags(self, mock_adapter, monkeypatch):
        """When a fenced code block opens in the overflowed message, the frozen
        message must have balanced <pre>...</pre> tags.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_adapter, use_telegraph_fallback=False)
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Text with open code fence that will be split
        text = "Some intro text:\n```python\ndef hello():\n    print('hello world')\n    return True\n"
        writer._buffer = text
        writer._full_text = text

        # Act
        await writer._overflow_to_new_message()

        # Assert — frozen HTML has balanced pre tags
        frozen_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_call[0][0]
        assert frozen_html.count("<pre>") == frozen_html.count("</pre>"), (
            f"Unbalanced <pre> tags in frozen HTML:\n{frozen_html}"
        )

    @pytest.mark.asyncio
    async def test_overflow_frozen_message_has_balanced_code_tags(self, mock_adapter, monkeypatch):
        """When inline code ` opens before the split, the frozen message must
        have balanced <code> HTML tags.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_adapter, use_telegraph_fallback=False)
        writer._debounce_s = 0
        writer._min_chunk = 0

        text = "Use the `very_long_function_name_that_keeps_going_and_going_across_boundary"
        writer._buffer = text
        writer._full_text = text

        # Act
        await writer._overflow_to_new_message()

        # Assert
        frozen_call = mock_adapter.edit_message.call_args
        frozen_html = frozen_call[0][0]
        assert frozen_html.count("<code>") == frozen_html.count("</code>"), (
            f"Unbalanced <code> tags in frozen HTML:\n{frozen_html}"
        )

    @pytest.mark.asyncio
    async def test_overflow_clean_split_does_not_prepend_bold_to_continuation(self, mock_adapter, monkeypatch):
        """When bold is properly closed before the split point, the continuation
        message (reply_new_message) must NOT start with a stray <b> tag.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)
        writer = StreamingWriter(mock_adapter, use_telegraph_fallback=False)
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Bold is closed ('**' pair is even), then normal text that overflows
        text = "**Bold section done.** Now normal text that is long enough to overflow."
        writer._buffer = text
        writer._full_text = text

        # Act
        await writer._overflow_to_new_message()

        # Assert — reply_new_message was called (classic path)
        mock_adapter.reply_new_message.assert_called_once()
        reply_html = mock_adapter.reply_new_message.call_args[0][0]
        # Continuation must not open with a dangling bold marker
        stripped = reply_html.lstrip()
        assert not stripped.startswith("<b>"), f"Continuation message starts with unexpected <b> tag:\n{stripped[:100]}"


# ── Overflow retry and circuit-breaker tests ─────────────────────────────────


class TestOverflowRetryStorm:
    """BUG-10: Verify overflow error handling and hot-loop prevention.

    _overflow_retries tracks consecutive reply_new_message failures.
    _overflow_failed (circuit-breaker) is set only after the 3rd failure,
    stopping further overflow attempts.
    """

    @pytest.mark.asyncio
    async def test_circuit_breaker_activates_after_three_consecutive_failures(self):
        """After 3 consecutive reply_new_message failures, _overflow_failed
        must be True and subsequent flushes must skip the overflow attempt.

        We call _overflow_to_new_message() directly (classic path) to count
        retries precisely without fighting the debounce/min_chunk gate.
        """
        # Arrange
        from app.streaming import StreamingWriter

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.reply_new_message = AsyncMock(side_effect=Exception("Unmatched end tag"))

        writer = StreamingWriter(adapter, use_telegraph_fallback=False)
        # Pre-populate buffer so overflow logic does something meaningful
        writer._buffer = "A" * 200
        writer._full_text = writer._buffer

        # Act — three direct overflow calls to exhaust retries
        await writer._overflow_to_new_message()  # _overflow_retries = 1
        assert not getattr(writer, "_overflow_failed", False), "Should NOT circuit-break after retry 1"

        await writer._overflow_to_new_message()  # _overflow_retries = 2
        assert not getattr(writer, "_overflow_failed", False), "Should NOT circuit-break after retry 2"

        await writer._overflow_to_new_message()  # _overflow_retries = 3 → breaker
        # Assert — circuit breaker engaged after the 3rd failure
        assert getattr(writer, "_overflow_failed", False) is True, (
            "_overflow_failed must be True after 3 failed overflow retries"
        )

    @pytest.mark.asyncio
    async def test_circuit_breaker_prevents_further_overflows(self):
        """Once _overflow_failed is True, _flush() must return early without
        calling reply_new_message again (prevents infinite retry loop).
        """
        # Arrange
        from app.streaming import STREAM_MSG_LIMIT, StreamingWriter

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.reply_new_message = AsyncMock(side_effect=Exception("still broken"))

        writer = StreamingWriter(adapter, use_telegraph_fallback=False)
        writer._overflow_failed = True  # Pre-set circuit breaker
        writer._buffer = "A" * (STREAM_MSG_LIMIT + 100)
        writer._full_text = writer._buffer
        writer._last_edit_time = 0
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Act — flush with overflow buffer while breaker is engaged
        await writer._flush(final=False)

        # Assert — reply_new_message must never be called
        adapter.reply_new_message.assert_not_called()


# ── Telegraph frozen path tests ───────────────────────────────────────────────


class TestTelegraphFallback:
    """Verify Telegraph fallback freezes the stream instead of overflowing."""

    @pytest.mark.asyncio
    async def test_telegraph_engaged_flag_set_on_overflow(self, monkeypatch):
        """When use_telegraph_fallback=True, the first overflow sets _telegraph_engaged=True
        and does NOT call reply_new_message (the stream is frozen in the placeholder).

        We use the production STREAM_MSG_LIMIT (4000) because the `_find_split_point`
        search_hi logic (estimated + 100) relies on a large limit to leave enough
        headroom for the 60-character telegraph indicator.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 4000)

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.reply_new_message = AsyncMock()

        writer = StreamingWriter(adapter, use_telegraph_fallback=True)
        writer._debounce_s = 0
        writer._min_chunk = 0

        # Text larger than 4000 chars (100 sentences of 45 chars = 4500)
        text = ("The quick brown fox jumps over the lazy dog. " * 100).rstrip()
        assert len(text) > 4000

        # Act
        await writer.write(text)

        # Assert 1 — telegraph engaged flag is set
        assert getattr(writer, "_telegraph_engaged", False) is True, (
            "_telegraph_engaged must be True after first overflow with use_telegraph_fallback=True"
        )

        # Assert 2 — no new message created (stream is frozen in placeholder)
        adapter.reply_new_message.assert_not_called()

        # Assert 3 — frozen placeholder was updated (may be called multiple times
        # by _find_split_point + freeze edit; we care that the LAST edit contains
        # the telegraph indicator which is appended in _overflow_to_new_message)
        adapter.edit_message.assert_called()
        # Find the call that contains the telegraph indicator (should be the freeze edit)
        all_calls = adapter.edit_message.call_args_list
        indicator_found = any("формирую статью" in str(call[0][0]) for call in all_calls)
        assert indicator_found, (
            f"Expected 'формирую статью' in one of the edit_message calls.\n"
            f"Actual calls (last 150 chars): {[str(c[0][0])[-150:] for c in all_calls]}"
        )

    @pytest.mark.asyncio
    async def test_telegraph_subsequent_writes_silently_accumulate(self, monkeypatch):
        """After _telegraph_engaged=True, further write() calls must accumulate
        text silently (for later creating the Telegraph page) without editing
        the frozen Telegram message again.
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.reply_new_message = AsyncMock()

        writer = StreamingWriter(adapter, use_telegraph_fallback=True)
        writer._telegraph_engaged = True  # Pre-set: already frozen
        writer._debounce_s = 0
        writer._min_chunk = 0
        writer._last_edit_time = 0

        initial_edit_count = adapter.edit_message.call_count

        # Act — write more text after freeze
        await writer.write("Additional content after freeze.")

        # Assert — no new edit calls (stream frozen)
        assert adapter.edit_message.call_count == initial_edit_count, (
            "edit_message must NOT be called when _telegraph_engaged is True"
        )


# ── Overflow sanitization tests ───────────────────────────────────────────────


class TestClassicOverflowSanitization:
    """BUG-10: Maintain balanced HTML across overflow chunks (classic path only)."""

    @pytest.mark.asyncio
    async def test_continuation_message_has_balanced_italic_tags(self, monkeypatch):
        """The continuation message (reply_new_message) must have balanced <i> tags
        even when the split point cuts through an italic span.

        Uses classic path (use_telegraph_fallback=False).
        """
        # Arrange
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 50)

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        new_adapter = MagicMock()
        adapter.reply_new_message = AsyncMock(return_value=new_adapter)

        writer = StreamingWriter(adapter, use_telegraph_fallback=False)
        writer._debounce_s = 0
        writer._min_chunk = 0
        writer._last_edit_time = 0

        # Text: large chunk of normal text forces overflow, then italic remainder
        oversized = "A" * 55 + "\n_This is italic text that continues after the split_"

        # Act
        await writer.write(oversized)

        # Assert — reply_new_message was called for the continuation
        adapter.reply_new_message.assert_called_once()
        continuation_html = adapter.reply_new_message.call_args[0][0]

        # The continuation HTML must have balanced <i> tags (sanitize_html_tags ensures this)
        assert continuation_html.count("<i>") == continuation_html.count("</i>"), (
            f"Unbalanced <i> tags in continuation:\n{continuation_html}"
        )
