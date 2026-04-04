"""
E2E test: Mid-stream API error recovery.

Risk covered: When the AI provider throws a mid-stream APIError (e.g. 503),
the StreamingWriter must:
  1. Append a user-visible error footer to the partial response
  2. Call finalize() so the incomplete text is written to Telegram
  3. NOT leave the message as a bare loading cursor (▍)
  4. NOT crash the entire handler (swallow and surface cleanly)

Level: E2E (hooks into stream_and_display directly; no real Telegram API)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.errors import APIError

from app.streaming import stream_and_display

# ─── Fake Telegram message adapter ────────────────────────────────────────────


def make_placeholder() -> MagicMock:
    """Create a fake Telegram Message placeholder."""
    msg = MagicMock()
    msg.edit_text = AsyncMock(return_value=None)
    msg.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    msg.get_bot = MagicMock(return_value=MagicMock())
    msg.chat = MagicMock()
    msg.chat.id = 456
    msg.message_id = 1
    return msg


# ─── Helper: fake API stream that fails mid-way ───────────────────────────────


async def _stream_fails_mid_way(*args, **kwargs):
    """Yields 2 chunks then raises a simulated 503 APIError."""
    yield "Partial "
    yield "response"
    # Simulate mid-stream server overload using the correct google.genai.errors.APIError signature:
    # APIError(code: int, response_json: Any, response=None)
    raise APIError(503, {"error": {"message": "Service Unavailable"}})


async def _stream_fully_succeeds(*args, **kwargs):
    """Yields a complete response without errors."""
    yield "The "
    yield "capital "
    yield "of France is Paris."


# ─── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_midstream_api_error_appends_error_footer():
    """When stream raises APIError mid-way, the partial text must include an error footer."""
    # Arrange
    placeholder = make_placeholder()
    fake_router = MagicMock()
    fake_router.stream_response = _stream_fails_mid_way

    # Act
    with patch("app.providers.get_provider_router", return_value=fake_router):
        result_text, success, _, token_count, was_interrupted, _ = await stream_and_display(
            placeholder_message=placeholder,
            model_name="gemini-2.0-flash",
            history=[],
            user_id=999,
            chat_id=456,
        )

    # Assert — partial content preserved
    assert "Partial" in result_text or "response" in result_text

    # Assert — interrupted flag set
    assert was_interrupted is True

    # Assert — the text contains an error indicator (not a clean response)
    assert "⚠️" in result_text or "❌" in result_text


@pytest.mark.asyncio
async def test_midstream_api_error_does_not_leave_bare_cursor():
    """After mid-stream failure, Telegram message must NOT end with raw cursor indicator '▍'."""
    # Arrange
    placeholder = make_placeholder()
    fake_router = MagicMock()
    fake_router.stream_response = _stream_fails_mid_way

    # Act
    with patch("app.providers.get_provider_router", return_value=fake_router):
        result_text, _, _, _, _, _ = await stream_and_display(
            placeholder_message=placeholder,
            model_name="gemini-2.0-flash",
            history=[],
            user_id=999,
            chat_id=456,
        )

    # Assert — edit_text was called at least once (finalize ran)
    assert placeholder.edit_text.call_count >= 1

    # The final edit must not contain the streaming cursor
    final_args = placeholder.edit_text.call_args_list[-1]
    final_text = final_args[0][0] if final_args[0] else final_args[1].get("text", "")
    assert " ▍" not in final_text, "Streaming cursor must be removed from finalized message"


@pytest.mark.asyncio
async def test_successful_stream_returns_complete_text():
    """When stream succeeds, result_text must contain all yielded chunks."""
    # Arrange
    placeholder = make_placeholder()
    fake_router = MagicMock()
    fake_router.stream_response = _stream_fully_succeeds

    # Act
    with patch("app.providers.get_provider_router", return_value=fake_router):
        result_text, success, _, _, was_interrupted, _ = await stream_and_display(
            placeholder_message=placeholder,
            model_name="gemini-2.0-flash",
            history=[],
            user_id=999,
            chat_id=456,
        )

    # Assert — all chunks joined
    assert "The capital of France is Paris." in result_text
    assert success is True
    assert was_interrupted is False


@pytest.mark.asyncio
async def test_timeout_error_marks_stream_as_interrupted():
    """TimeoutError during streaming must set was_interrupted=True."""
    # Arrange
    placeholder = make_placeholder()

    async def _stream_timeout(*args, **kwargs):
        yield "Starting..."
        raise TimeoutError("Stream timed out")

    fake_router = MagicMock()
    fake_router.stream_response = _stream_timeout

    # Act
    with patch("app.providers.get_provider_router", return_value=fake_router):
        _, _, _, _, was_interrupted, _ = await stream_and_display(
            placeholder_message=placeholder,
            model_name="gemini-2.0-flash",
            history=[],
            user_id=999,
            chat_id=456,
        )

    # Assert
    assert was_interrupted is True


@pytest.mark.asyncio
async def test_empty_stream_returns_empty_text_and_false():
    """A stream that yields nothing must return empty text with success=False."""
    # Arrange
    placeholder = make_placeholder()

    async def _empty_stream(*args, **kwargs):
        return  # yields nothing
        yield  # make this an async generator

    fake_router = MagicMock()
    fake_router.stream_response = _empty_stream

    # Act
    with patch("app.providers.get_provider_router", return_value=fake_router):
        result_text, success, _, _, _, _ = await stream_and_display(
            placeholder_message=placeholder,
            model_name="gemini-2.0-flash",
            history=[],
            user_id=999,
            chat_id=456,
        )

    # Assert
    assert result_text == "" or result_text.strip() == ""
    assert success is False
