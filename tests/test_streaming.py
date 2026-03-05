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
