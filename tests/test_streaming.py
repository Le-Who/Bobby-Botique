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
        adapter = MagicMock()
        adapter._bot = None
        writer = StreamingWriter(adapter, chat_type="group")
        assert writer._use_drafts is False
        assert writer._debounce_s == EDIT_DEBOUNCE_S
        assert writer._min_chunk == MIN_CHUNK_SIZE

    def test_draft_mode_in_private_chat(self):
        """With bot + private chat → draft mode, faster debounce."""
        adapter = MagicMock()
        adapter._bot = MagicMock()
        writer = StreamingWriter(adapter, chat_type="private")
        assert writer._use_drafts is True
        assert writer._debounce_s == DRAFT_DEBOUNCE_S
        assert writer._min_chunk == DRAFT_MIN_CHUNK

    def test_classic_mode_in_group_chat(self):
        """In group chat → classic mode even with bot."""
        adapter = MagicMock()
        adapter._bot = MagicMock()
        writer = StreamingWriter(adapter, chat_type="group")
        assert writer._use_drafts is False
        assert writer._debounce_s == EDIT_DEBOUNCE_S

    def test_classic_mode_in_supergroup_chat(self):
        """In supergroup chat → classic mode."""
        adapter = MagicMock()
        adapter._bot = MagicMock()
        writer = StreamingWriter(adapter, chat_type="supergroup")
        assert writer._use_drafts is False

    def test_classic_mode_without_bot(self):
        """Private chat but no bot → classic mode (fallback)."""
        adapter = MagicMock()
        adapter._bot = None
        writer = StreamingWriter(adapter, chat_type="private")
        assert writer._use_drafts is False

    def test_initial_state(self):
        adapter = MagicMock()
        adapter._bot = None
        writer = StreamingWriter(adapter, chat_type="private")
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

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.send_draft = AsyncMock()
        adapter.delete_placeholder = AsyncMock()  # P1: required for draft mode
        adapter.send_final_message = AsyncMock()  # P1: used on finalize after draft
        adapter._bot = MagicMock()

        writer = StreamingWriter(adapter, chat_type="private")
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_draft_mode_calls_send_message_draft(self, draft_writer):
        """Mid-stream flush uses sendMessageDraft, not edit_text."""
        await draft_writer.write("Hello world")

        draft_writer._adapter.send_draft.assert_called_once()
        call_kwargs = draft_writer._adapter.send_draft.call_args
        assert "Hello world" in call_kwargs.kwargs["text"]

    @pytest.mark.asyncio
    async def test_draft_mode_no_cursor_indicator(self, draft_writer):
        """Draft mode should NOT append cursor indicator ▍."""
        await draft_writer.write("Hello")

        call_kwargs = draft_writer._adapter.send_draft.call_args
        assert STREAMING_INDICATOR not in call_kwargs.kwargs["text"]

    @pytest.mark.asyncio
    async def test_finalize_uses_send_final_message(self, draft_writer):
        """In draft mode (placeholder deleted), finalize sends a new permanent message."""
        # Simulate that a draft was already sent (which deletes placeholder)
        await draft_writer.write("First chunk")
        draft_writer._adapter.delete_placeholder.assert_called_once()  # P1

        draft_writer._buffer = "Final answer"
        draft_writer._full_text = "Final answer"

        await draft_writer.finalize()

        # P1: placeholder was deleted, so finalize uses send_final_message
        draft_writer._adapter.send_final_message.assert_called_once()
        call_kwargs = draft_writer._adapter.send_final_message.call_args
        assert "Final answer" in call_kwargs[0][0] or "Final answer" in call_kwargs.kwargs.get(
            "text", call_kwargs[0][0]
        )

    @pytest.mark.asyncio
    async def test_draft_fallback_on_error(self, draft_writer):
        """On TelegramError, draft mode falls back to classic.

        Since placeholder was deleted before the first draft attempt,
        the fallback creates a recovery message via send_final_message.
        """
        draft_writer._adapter.send_draft.side_effect = Exception("Forbidden")

        await draft_writer.write("Test text that triggers error")

        assert draft_writer._use_drafts is False
        assert draft_writer._debounce_s == EDIT_DEBOUNCE_S
        # P1: placeholder was deleted, so recovery sends a new message
        draft_writer._adapter.send_final_message.assert_called_once()

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

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter._bot = None

        writer = StreamingWriter(adapter, chat_type="group")
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_classic_mode_calls_edit_text(self, classic_writer):
        """Non-draft mode uses edit_message for mid-stream updates."""
        await classic_writer.write("Hello classic")

        classic_writer._adapter.edit_message.assert_called_once()
        call_args = classic_writer._adapter.edit_message.call_args
        # Classic mode appends cursor indicator
        assert STREAMING_INDICATOR in call_args[0][0]

    @pytest.mark.asyncio
    async def test_classic_finalize_no_cursor(self, classic_writer):
        """Finalize in classic mode should NOT have cursor indicator."""
        classic_writer._buffer = "Final text"
        classic_writer._full_text = "Final text"

        await classic_writer.finalize()

        call_args = classic_writer._adapter.edit_message.call_args
        assert STREAMING_INDICATOR not in call_args[0][0]


# ── Overflow formatting context ──────────────────────────────────────────────


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
        writer = StreamingWriter(mock_adapter, chat_type="group")
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
        writer = StreamingWriter(mock_adapter, chat_type="group")
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
        writer = StreamingWriter(mock_adapter, chat_type="group")
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
        writer = StreamingWriter(mock_adapter, chat_type="group")
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


class TestDraftOverflowSwitchesToClassic:
    """BUG-1: After overflow in draft mode, writer must switch to classic mode.

    Without the fix, _use_drafts stays True after overflow, causing the next
    write() to call delete_placeholder() on the NEW continuation message —
    potentially deleting visible content.
    """

    @pytest.fixture()
    def draft_overflow_writer(self, monkeypatch):
        """Create a draft-mode writer with low overflow limit for testing."""
        monkeypatch.setattr("app.streaming.STREAM_MSG_LIMIT", 80)
        monkeypatch.setattr("app.streaming.DRAFT_DEBOUNCE_S", 0)
        monkeypatch.setattr("app.streaming.DRAFT_MIN_CHUNK", 0)

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.send_draft = AsyncMock()
        adapter.delete_placeholder = AsyncMock()
        adapter.send_final_message = AsyncMock()
        adapter._bot = MagicMock()

        # reply_new_message returns a fresh adapter (simulates the real flow)
        new_adapter = MagicMock()
        new_adapter.edit_message = AsyncMock()
        new_adapter.reply_new_message = AsyncMock()
        new_adapter._bot = None  # New adapter is NOT draft-capable
        adapter.reply_new_message = AsyncMock(return_value=new_adapter)

        writer = StreamingWriter(adapter, chat_type="private")
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_overflow_switches_use_drafts_to_false(self, draft_overflow_writer):
        """After overflow, _use_drafts must be False."""
        writer = draft_overflow_writer
        assert writer._use_drafts is True  # Starts in draft mode

        # Trigger overflow with long text
        long_text = "A" * 200
        writer._buffer = long_text
        writer._full_text = long_text
        # Simulate that placeholder was already deleted (first draft was sent)
        writer._placeholder_deleted = True

        await writer._overflow_to_new_message()

        assert writer._use_drafts is False, "_use_drafts must be False after draft overflow"
        assert writer._debounce_s == EDIT_DEBOUNCE_S, "debounce must match classic mode"
        assert writer._min_chunk == MIN_CHUNK_SIZE, "min_chunk must match classic mode"

    @pytest.mark.asyncio
    async def test_overflow_sends_frozen_via_send_final_message(self, draft_overflow_writer):
        """Frozen text should go via send_final_message when placeholder is deleted."""
        writer = draft_overflow_writer
        writer._placeholder_deleted = True

        long_text = "A" * 200
        writer._buffer = long_text
        writer._full_text = long_text

        # Capture original adapter before overflow swaps it
        original_adapter = writer._adapter

        await writer._overflow_to_new_message()

        # Frozen text was sent via send_final_message on the ORIGINAL adapter
        original_adapter.send_final_message.assert_called_once()


class TestClassicFinalFlushPassesReplyMarkup:
    """BUG-2: reply_markup must be forwarded to edit_message in classic finalize."""

    @pytest.fixture()
    def classic_writer(self, monkeypatch):
        monkeypatch.setattr("app.streaming.EDIT_DEBOUNCE_S", 0)
        monkeypatch.setattr("app.streaming.MIN_CHUNK_SIZE", 0)

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter._bot = None

        writer = StreamingWriter(adapter, chat_type="group")
        writer._debounce_s = 0
        writer._min_chunk = 0
        return writer

    @pytest.mark.asyncio
    async def test_finalize_passes_reply_markup_to_edit(self, classic_writer):
        """finalize(reply_markup=X) should forward X to edit_message()."""
        classic_writer._buffer = "Final text"
        classic_writer._full_text = "Final text"

        mock_markup = MagicMock()
        await classic_writer.finalize(reply_markup=mock_markup)

        call_kwargs = classic_writer._adapter.edit_message.call_args
        assert call_kwargs.kwargs.get("reply_markup") is mock_markup or (
            len(call_kwargs) > 2 and call_kwargs[0][2] is mock_markup
        ), "reply_markup must be forwarded to edit_message"

    @pytest.mark.asyncio
    async def test_finalize_without_reply_markup(self, classic_writer):
        """finalize() without reply_markup should not pass it to edit_message."""
        classic_writer._buffer = "Final text"
        classic_writer._full_text = "Final text"

        await classic_writer.finalize()

        call_kwargs = classic_writer._adapter.edit_message.call_args
        # reply_markup should be None (not present)
        assert call_kwargs.kwargs.get("reply_markup") is None


class TestSendFinalMessageReplyThreading:
    """BUG-3: send_final_message must include reply_to_message_id for threading."""

    @pytest.mark.asyncio
    async def test_uses_reply_to_message_id_if_present(self):
        from app.adapters.ui_adapter import TelegramMessageAdapter
        
        mock_bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 999
        mock_msg.reply_to_message = MagicMock()
        mock_msg.reply_to_message.message_id = 123  # The original user's message

        adapter = TelegramMessageAdapter(message=mock_msg, bot=mock_bot, chat_id=1)
        await adapter.send_final_message("hello", parse_mode="HTML")

        mock_bot.send_message.assert_called_once_with(
            chat_id=1,
            text="hello",
            parse_mode="HTML",
            reply_to_message_id=123,
            allow_sending_without_reply=True,
        )

    @pytest.mark.asyncio
    async def test_fallback_to_message_id_if_no_reply_to(self):
        from app.adapters.ui_adapter import TelegramMessageAdapter
        
        mock_bot = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 999
        # No reply_to_message
        mock_msg.reply_to_message = None

        adapter = TelegramMessageAdapter(message=mock_msg, bot=mock_bot, chat_id=1)
        await adapter.send_final_message("hello", parse_mode="HTML")

        mock_bot.send_message.assert_called_once_with(
            chat_id=1,
            text="hello",
            parse_mode="HTML",
            reply_to_message_id=999,
            allow_sending_without_reply=True,
        )

class TestDetectOpenMarkdown:
    def test_ignores_safely_closed_code_blocks(self):
        from app.streaming import _detect_open_markdown
        text = "Here is some code:\n```python\ndef test_feature():\n    pass\n```\nAnd a **bold** statement."
        suf, pref = _detect_open_markdown(text)
        assert suf == ""
        assert pref == ""

    def test_ignores_inline_code(self):
        from app.streaming import _detect_open_markdown
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


class TestOverflowRetryStorm:
    """BUG-10: Verify overflow error handling and hot-loop prevention."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_on_overflow_failure(self):
        """If reply_new_message fails, it should circuit-break and not hot-loop."""
        from app.streaming import STREAM_MSG_LIMIT, StreamingWriter

        adapter = MagicMock()
        adapter.edit_message = AsyncMock()
        adapter.send_draft = AsyncMock()
        adapter.reply_new_message = AsyncMock(side_effect=Exception("Unmatched end tag"))
        adapter.send_final_message = AsyncMock()
        # Mock prepare_draft_mode to succeed
        adapter.delete_placeholder = AsyncMock()

        writer = StreamingWriter(adapter, chat_type="private")
        writer._use_drafts = True

        oversized = "A" * (STREAM_MSG_LIMIT + 100)
        
        # Write 1: hits overflow, replies new message, throws error. 
        # Should record the failure.
        await writer.write(oversized)
        
        # Verify the exception was handled and state was updated to prevent hot loop
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

        writer = StreamingWriter(adapter, chat_type="private")
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



