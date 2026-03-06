"""Tests for app.streaming — constants, finish reasons, StreamingWriter (draft+classic)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.streaming import (
    _BLOCKED_FINISH_REASONS,
    _TRUNCATED_FINISH_REASONS,
    DRAFT_DEBOUNCE_S,
    DRAFT_MIN_CHUNK,
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

    def test_draft_debounce_faster_than_classic(self):
        assert DRAFT_DEBOUNCE_S < EDIT_DEBOUNCE_S

    def test_draft_min_chunk_smaller_than_classic(self):
        assert DRAFT_MIN_CHUNK < MIN_CHUNK_SIZE

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


class TestStreamingWriterInit:
    """Test StreamingWriter construction (no real Telegram message needed)."""

    def test_classic_mode_defaults(self):
        """Without bot, writer uses classic mode with edit debounce."""
        msg = MagicMock()
        writer = StreamingWriter(msg)
        assert writer._use_drafts is False
        assert writer._debounce_s == EDIT_DEBOUNCE_S
        assert writer._min_chunk == MIN_CHUNK_SIZE

    def test_draft_mode_in_private_chat(self):
        """With bot + private chat → draft mode, faster debounce."""
        msg = MagicMock()
        bot = MagicMock()
        writer = StreamingWriter(msg, bot=bot, chat_id=123, chat_type="private")
        assert writer._use_drafts is True
        assert writer._debounce_s == DRAFT_DEBOUNCE_S
        assert writer._min_chunk == DRAFT_MIN_CHUNK
        assert writer._draft_id > 0
        assert writer._chat_id == 123

    def test_classic_mode_in_group_chat(self):
        """In group chat → classic mode even with bot."""
        msg = MagicMock()
        bot = MagicMock()
        writer = StreamingWriter(msg, bot=bot, chat_id=456, chat_type="group")
        assert writer._use_drafts is False
        assert writer._debounce_s == EDIT_DEBOUNCE_S

    def test_classic_mode_in_supergroup_chat(self):
        """In supergroup chat → classic mode."""
        msg = MagicMock()
        bot = MagicMock()
        writer = StreamingWriter(msg, bot=bot, chat_id=789, chat_type="supergroup")
        assert writer._use_drafts is False

    def test_classic_mode_without_bot(self):
        """Private chat but no bot → classic mode (fallback)."""
        msg = MagicMock()
        writer = StreamingWriter(msg, chat_id=123, chat_type="private")
        assert writer._use_drafts is False

    def test_initial_state(self):
        msg = MagicMock()
        writer = StreamingWriter(msg)
        assert writer.text == ""
        assert writer.edit_count == 0
        assert writer.message_count == 1  # Initial placeholder counts as 1


# ── Draft mode streaming ─────────────────────────────────────────────────────


class TestStreamingWriterDraftMode:
    """Test sendMessageDraft calls during streaming."""

    @pytest.fixture()
    def draft_writer(self, monkeypatch):
        """Create a draft-mode writer with zeroed debounce for testing."""
        monkeypatch.setattr("app.streaming.DRAFT_DEBOUNCE_S", 0)
        monkeypatch.setattr("app.streaming.DRAFT_MIN_CHUNK", 0)

        msg = MagicMock()
        msg.edit_text = AsyncMock()
        bot = MagicMock()
        bot.send_message_draft = AsyncMock(return_value=True)
        writer = StreamingWriter(msg, bot=bot, chat_id=42, chat_type="private")
        # Zero debounce for tests (monkeypatch affects module, not instance)
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_draft_mode_calls_send_message_draft(self, draft_writer):
        """Mid-stream flush uses sendMessageDraft, not edit_text."""
        await draft_writer.write("Hello world")

        draft_writer._bot.send_message_draft.assert_called_once()
        call_kwargs = draft_writer._bot.send_message_draft.call_args
        assert call_kwargs.kwargs["chat_id"] == 42
        assert call_kwargs.kwargs["draft_id"] == draft_writer._draft_id
        assert "Hello world" in call_kwargs.kwargs["text"]

    @pytest.mark.asyncio
    async def test_draft_mode_no_cursor_indicator(self, draft_writer):
        """Draft mode should NOT append cursor indicator ▍."""
        await draft_writer.write("Hello")

        call_kwargs = draft_writer._bot.send_message_draft.call_args
        assert STREAMING_INDICATOR not in call_kwargs.kwargs["text"]

    @pytest.mark.asyncio
    async def test_finalize_uses_edit_text(self, draft_writer):
        """Final flush uses edit_text for a permanent message (not draft)."""
        draft_writer._buffer = "Final answer"
        draft_writer._full_text = "Final answer"

        await draft_writer.finalize()

        draft_writer._msg.edit_text.assert_called_once()
        call_args = draft_writer._msg.edit_text.call_args
        assert "Final answer" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_draft_fallback_on_error(self, draft_writer):
        """On TelegramError, draft mode falls back to classic edit_text."""
        from telegram.error import TelegramError

        draft_writer._bot.send_message_draft.side_effect = TelegramError("Forbidden")

        await draft_writer.write("Test text that triggers error")

        assert draft_writer._use_drafts is False
        assert draft_writer._debounce_s == EDIT_DEBOUNCE_S
        # Fallback should have called edit_text
        draft_writer._msg.edit_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_draft_mode_increments_edit_count(self, draft_writer):
        """Each successful draft send increments edit_count."""
        await draft_writer.write("chunk one")
        await draft_writer.write("chunk two")
        assert draft_writer.edit_count >= 2


# ── Classic mode streaming ───────────────────────────────────────────────────


class TestStreamingWriterClassicMode:
    """Test classic editMessageText path (groups, fallback)."""

    @pytest.fixture()
    def classic_writer(self, monkeypatch):
        monkeypatch.setattr("app.streaming.EDIT_DEBOUNCE_S", 0)
        monkeypatch.setattr("app.streaming.MIN_CHUNK_SIZE", 0)

        msg = MagicMock()
        msg.edit_text = AsyncMock()
        writer = StreamingWriter(msg, chat_type="group")
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_classic_mode_calls_edit_text(self, classic_writer):
        """Non-draft mode uses edit_text for mid-stream updates."""
        await classic_writer.write("Hello classic")

        classic_writer._msg.edit_text.assert_called_once()
        call_args = classic_writer._msg.edit_text.call_args
        # Classic mode appends cursor indicator
        assert STREAMING_INDICATOR in call_args[0][0]

    @pytest.mark.asyncio
    async def test_classic_finalize_no_cursor(self, classic_writer):
        """Finalize in classic mode should NOT have cursor indicator."""
        classic_writer._buffer = "Final text"
        classic_writer._full_text = "Final text"

        await classic_writer.finalize()

        call_args = classic_writer._msg.edit_text.call_args
        assert STREAMING_INDICATOR not in call_args[0][0]


# ── Overflow formatting context ──────────────────────────────────────────────


class TestOverflowFormattingContext:
    """Verify formatting context is carried across overflow message boundaries."""

    @pytest.fixture()
    def mock_message(self):
        """Create a mock Telegram message that tracks edit_text and reply_text calls."""
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
        writer = StreamingWriter(mock_message)
        writer._debounce_s = 0
        writer._min_chunk = 0

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
        assert writer._buffer.startswith("**")

    @pytest.mark.asyncio
    async def test_overflow_preserves_code_block_context(self, mock_message, monkeypatch):
        """When a code block opens in msg1, msg2 gets a reopened fence."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        writer = StreamingWriter(mock_message)
        writer._debounce_s = 0
        writer._min_chunk = 0

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
        writer = StreamingWriter(mock_message)
        writer._debounce_s = 0
        writer._min_chunk = 0

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
        writer = StreamingWriter(mock_message)
        writer._debounce_s = 0
        writer._min_chunk = 0

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
