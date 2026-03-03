# /app/streaming.py
"""Streaming response layer — progressive message updates from AI providers.

Uses Gemini's generate_content_stream for token-by-token generation
and debounced edit_message_text for real-time user feedback.
Falls back to send_message_draft when Telegram forum topics are available.

Architecture:
    stream_gemini_response()  →  async generator yielding text chunks
    StreamingWriter            →  debounced Telegram message updater
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator

from google import genai
from google.genai import types
from google.genai.errors import APIError

from app.config import settings
from app.metrics import metrics_collector
from app.request_context import get_request_id
from app.utils.formatting import TelegramFormatter

# ── Configuration ────────────────────────────────────────────────────────────

# Minimum interval (seconds) between edit_message_text calls to avoid flood.
EDIT_DEBOUNCE_S = 1.2
# Minimum chars accumulated before sending an update (avoids word fragments).
MIN_CHUNK_SIZE = 80
# Indicator appended while streaming is in progress.
STREAMING_INDICATOR = " ▍"


async def stream_gemini_response(
    api_key: str,
    model_name: str,
    contents: list,
    config: types.GenerateContentConfig,
    timeout: float = 100.0,
) -> AsyncGenerator[str, None]:
    """Async generator that yields progressive text chunks from Gemini streaming API.

    Yields:
        Text delta strings as they arrive from the model.

    Raises:
        TimeoutError: If streaming exceeds timeout.
        APIError: On Gemini API errors.
    """
    request_id = get_request_id()
    client_kwargs = {"api_key": api_key}
    http_opts = {"timeout": 90_000}
    if request_id:
        http_opts["headers"] = {"X-Request-ID": request_id}
    client_kwargs["http_options"] = types.HttpOptions(**http_opts)
    client = genai.Client(**client_kwargs)

    async def _stream():
        response_stream = await client.aio.models.generate_content_stream(
            model=model_name, contents=contents, config=config,
        )
        async for chunk in response_stream:
            if chunk.text:
                yield chunk.text

    try:
        async with asyncio.timeout(timeout):
            async for delta in _stream():
                yield delta
    except TimeoutError:
        logging.error("Streaming timed out for model %s", model_name)
        raise
    except APIError:
        raise
    except Exception as e:
        logging.error("Streaming error: %s", e, exc_info=True)
        raise


class StreamingWriter:
    """Debounced Telegram message updater for streaming AI responses.

    Progressively edits a placeholder message as text chunks arrive,
    with flood-control debouncing and a cursor indicator.
    """

    def __init__(self, placeholder_message, *, debounce_s: float = EDIT_DEBOUNCE_S):
        self._msg = placeholder_message
        self._debounce_s = debounce_s
        self._buffer = ""
        self._last_edit_time = 0.0
        self._pending_chars = 0
        self._edit_count = 0

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._pending_chars += len(delta)

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= MIN_CHUNK_SIZE:
            await self._flush(final=False)

    async def finalize(self) -> str:
        """Send the final version of the message (no cursor indicator).

        Returns:
            The complete accumulated text.
        """
        await self._flush(final=True)
        return self._buffer

    async def _flush(self, *, final: bool = False) -> None:
        """Edit the placeholder message with current buffer content."""
        text = self._buffer
        if not text.strip():
            return

        if not final:
            text += STREAMING_INDICATOR

        try:
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            # Telegram message limit is 4096 chars; truncate to prevent Message_too_long
            if len(formatted_text) > 4096:
                formatted_text = formatted_text[:4080] + "\n\n… _(обрезано)_"
            await self._msg.edit_text(formatted_text, parse_mode=parse_mode)
            self._last_edit_time = time.monotonic()
            self._pending_chars = 0
            self._edit_count += 1
        except Exception as e:
            # "Message is not modified" is expected if text hasn't changed enough
            if "not modified" not in str(e).lower():
                logging.warning("Streaming edit failed (attempt %d): %s", self._edit_count, e)

    @property
    def text(self) -> str:
        """Return the full accumulated text."""
        return self._buffer

    @property
    def edit_count(self) -> int:
        return self._edit_count


async def stream_and_display(
    placeholder_message,
    api_key: str,
    model_name: str,
    contents: list,
    config: types.GenerateContentConfig,
    timeout: float = 100.0,
) -> tuple[str, bool]:
    """High-level: stream Gemini response and progressively update Telegram message.

    Args:
        placeholder_message: Telegram message to edit progressively.
        api_key: Gemini API key.
        model_name: Model to use.
        contents: Prepared contents list.
        config: GenerateContentConfig.
        timeout: Max streaming time.

    Returns:
        (response_text, success) tuple.
    """
    writer = StreamingWriter(placeholder_message)

    try:
        async for delta in stream_gemini_response(
            api_key, model_name, contents, config, timeout=timeout,
        ):
            await writer.write(delta)

        final_text = await writer.finalize()

        if not final_text.strip():
            return "", False

        logging.info(
            "Streaming complete: %d chars, %d edits",
            len(final_text), writer.edit_count,
        )
        await metrics_collector.record_api_call("gemini_streaming", model_name)
        return final_text, True

    except TimeoutError:
        partial = writer.text
        if partial:
            await writer.finalize()
            return partial + "\n\n⏰ _(ответ был прерван по таймауту)_", True
        return "⏰ Превышено время ожидания ответа. Попробуйте позже.", False

    except APIError as e:
        logging.error("Streaming API error: %s", e)
        partial = writer.text
        if partial:
            await writer.finalize()
            return partial + "\n\n⚠️ _(ответ был прерван из-за ошибки API)_", True
        return f"❌ Ошибка API: {e}", False

    except Exception as e:
        logging.error("Streaming failed: %s", e, exc_info=True)
        return f"❌ Ошибка при потоковой генерации: {e}", False
