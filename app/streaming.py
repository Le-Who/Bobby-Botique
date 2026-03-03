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
from app.utils.text_format import split_text_safe

# ── Configuration ────────────────────────────────────────────────────────────

# Minimum interval (seconds) between edit_message_text calls to avoid flood.
EDIT_DEBOUNCE_S = 1.2
# Minimum chars accumulated before sending an update (avoids word fragments).
MIN_CHUNK_SIZE = 80
# Indicator appended while streaming is in progress.
STREAMING_INDICATOR = " ▍"
# Safe limit for Telegram messages (leaves margin for HTML tag overhead).
STREAM_MSG_LIMIT = 4000


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

    When the formatted text exceeds STREAM_MSG_LIMIT, the writer stops
    editing the current message and accumulates the overflow. On finalize(),
    overflow text is sent as one or more new messages via split_text_safe.
    """

    def __init__(self, placeholder_message, *, debounce_s: float = EDIT_DEBOUNCE_S):
        self._msg = placeholder_message
        self._debounce_s = debounce_s
        self._buffer = ""
        self._last_edit_time = 0.0
        self._pending_chars = 0
        self._edit_count = 0
        self._overflow = False  # True once we've hit the message limit
        self._frozen_text = ""   # Last text that fit in the first message

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._pending_chars += len(delta)

        # Don't attempt edits once we've overflowed — just accumulate
        if self._overflow:
            return

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= MIN_CHUNK_SIZE:
            await self._flush(final=False)

    async def finalize(self) -> str:
        """Send the final version of the message (no cursor indicator).

        If there's overflow text, sends it as additional message(s).

        Returns:
            The complete accumulated text (across all messages).
        """
        if not self._overflow:
            # Everything fits in one message
            await self._flush(final=True)
        else:
            # First message is already frozen; send the rest as new message(s)
            await self._send_overflow()

        return self._buffer

    async def _flush(self, *, final: bool = False) -> None:
        """Edit the placeholder message with current buffer content."""
        text = self._buffer
        if not text.strip():
            return

        display_text = text if final else text + STREAMING_INDICATOR

        try:
            formatted_text, parse_mode = TelegramFormatter.format_text(display_text)

            if len(formatted_text) > STREAM_MSG_LIMIT:
                # Overflow! Freeze the current message and stop editing.
                await self._freeze_message()
                return

            await self._msg.edit_text(formatted_text, parse_mode=parse_mode)
            self._last_edit_time = time.monotonic()
            self._pending_chars = 0
            self._edit_count += 1
            # Snapshot the raw buffer at this point — last known good state
            self._frozen_text = self._buffer
        except Exception as e:
            # "Message is not modified" is expected if text hasn't changed enough
            if "not modified" not in str(e).lower():
                logging.warning("Streaming edit failed (attempt %d): %s", self._edit_count, e)

    async def _freeze_message(self) -> None:
        """Freeze the first message at the last content that fit, then switch to overflow mode."""
        self._overflow = True

        # Find the largest prefix of buffer whose formatted version fits
        # Use the frozen snapshot from the last successful edit
        # (we know it was under the limit because it was successfully sent)
        if self._frozen_text:
            freeze_text = self._frozen_text
        else:
            freeze_text = self._buffer

        try:
            formatted, parse_mode = TelegramFormatter.format_text(freeze_text)
            if len(formatted) <= STREAM_MSG_LIMIT:
                await self._msg.edit_text(formatted, parse_mode=parse_mode)
        except Exception as e:
            if "not modified" not in str(e).lower():
                logging.warning("Failed to freeze stream message: %s", e)

        logging.info(
            "Streaming overflow at %d chars, switching to multi-message",
            len(self._buffer),
        )

    async def _send_overflow(self) -> None:
        """Send the portion of the buffer that didn't fit as new message(s)."""
        # The frozen text is already displayed in message #1.
        # Send everything after the freeze point as new messages.
        frozen_len = len(self._frozen_text) if self._frozen_text else 0
        overflow_text = self._buffer[frozen_len:].strip()

        if not overflow_text:
            return

        try:
            formatted, parse_mode = TelegramFormatter.format_text(overflow_text)
            chunks = split_text_safe(formatted)

            for chunk in chunks:
                await self._msg.reply_text(
                    text=chunk,
                    parse_mode=parse_mode,
                )

            logging.info(
                "Sent %d overflow message(s) (%d chars)",
                len(chunks), len(overflow_text),
            )
        except Exception as e:
            logging.error("Failed to send overflow messages: %s", e)

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
