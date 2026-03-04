# /app/streaming.py
"""Streaming response layer — progressive message updates from AI providers.

Uses Gemini's generate_content_stream for token-by-token generation
and debounced edit_message_text for real-time user feedback.
Falls back to send_message_draft when Telegram forum topics are available.

Architecture:
    stream_gemini_response()  →  async generator yielding text chunks
    StreamingWriter            →  debounced Telegram message updater (multi-message)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from google import genai
from google.genai import types
from google.genai.errors import APIError

from app.config import settings
from app.metrics import metrics_collector
from app.request_context import get_request_id
from app.utils.formatting import TelegramFormatter
from app.utils.text_format import sanitize_html_tags

if TYPE_CHECKING:
    from telegram import Message


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

    After iteration completes, check ``stream_gemini_response.last_finish_reason``
    for the model's finish reason (e.g. 'STOP', 'SAFETY', 'RECITATION', 'MAX_TOKENS').

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

    # Side-channel: store finish_reason after iteration
    finish_reason_holder: list[str | None] = [None]

    async def _stream():
        response_stream = await client.aio.models.generate_content_stream(
            model=model_name, contents=contents, config=config,
        )
        async for chunk in response_stream:
            # Inspect finish_reason on each chunk (usually set on the last one)
            try:
                candidates = getattr(chunk, "candidates", None)
                if candidates:
                    fr = getattr(candidates[0], "finish_reason", None)
                    if fr:
                        finish_reason_holder[0] = str(fr)
            except (IndexError, AttributeError):
                pass

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
    finally:
        # Expose finish_reason via function attribute (read by stream_and_display)
        stream_gemini_response.last_finish_reason = finish_reason_holder[0]


# Initialize module-level attribute
stream_gemini_response.last_finish_reason = None

# Finish reasons that indicate the model was blocked mid-response
_BLOCKED_FINISH_REASONS = frozenset({
    "SAFETY", "2", "FINISH_REASON_SAFETY",
    "RECITATION", "4", "FINISH_REASON_RECITATION",
})

_TRUNCATED_FINISH_REASONS = frozenset({
    "MAX_TOKENS", "3", "FINISH_REASON_MAX_TOKENS",
})


class StreamingWriter:
    """Debounced Telegram message updater for streaming AI responses.

    Progressively edits a placeholder message as text chunks arrive,
    with flood-control debouncing and a cursor indicator.

    When the current message's formatted text approaches STREAM_MSG_LIMIT,
    the writer finalizes the current message and creates a new one via
    reply_text, continuing streaming seamlessly into it.
    """

    def __init__(self, placeholder_message, *, debounce_s: float = EDIT_DEBOUNCE_S):
        self._msg = placeholder_message          # Current message being edited
        self._first_msg = placeholder_message     # Original placeholder (never changes)
        self._debounce_s = debounce_s
        self._buffer = ""                         # Buffer for CURRENT message only
        self._full_text = ""                      # Entire accumulated text across all messages
        self._last_edit_time = 0.0
        self._pending_chars = 0
        self._edit_count = 0
        self._msg_count = 1                       # How many messages in chain

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._full_text += delta
        self._pending_chars += len(delta)

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= MIN_CHUNK_SIZE:
            await self._flush(final=False)

    async def finalize(self) -> str:
        """Send the final version of the message (no cursor indicator).

        Returns:
            The complete accumulated text (across all messages).
        """
        await self._flush(final=True)
        return self._full_text

    async def _flush(self, *, final: bool = False) -> None:
        """Edit the current message with the current buffer content."""
        text = self._buffer
        if not text.strip():
            return

        display_text = text if final else text + STREAMING_INDICATOR

        try:
            formatted_text, parse_mode = TelegramFormatter.format_text(display_text)

            # Mid-stream flushes may have unclosed HTML tags from incomplete markdown
            if not final:
                formatted_text = sanitize_html_tags(formatted_text)

            if len(formatted_text) > STREAM_MSG_LIMIT and not final:
                # Mid-stream overflow: finalize current, start new message
                await self._overflow_to_new_message()
                return

            if len(formatted_text) > STREAM_MSG_LIMIT and final:
                # Final flush overflows — split into finalize + new message
                await self._overflow_to_new_message()
                # Now flush the remainder (recursion with reduced buffer)
                await self._flush(final=True)
                return

            await self._msg.edit_text(formatted_text, parse_mode=parse_mode)
            self._last_edit_time = time.monotonic()
            self._pending_chars = 0
            self._edit_count += 1
        except Exception as e:
            # "Message is not modified" is expected if text hasn't changed enough
            if "not modified" not in str(e).lower():
                logging.warning("Streaming edit failed (attempt %d): %s", self._edit_count, e)

    async def _overflow_to_new_message(self) -> None:
        """Finalize current message and create new placeholder for continued streaming."""
        # 1. Finalize the current message with the text that fits
        #    Find the last paragraph break that keeps formatted text under limit
        split_point = self._find_split_point()

        if split_point > 0:
            frozen_text = self._buffer[:split_point]
            remainder = self._buffer[split_point:].lstrip()
        else:
            # No good split point — take what we have minus some margin
            frozen_text = self._buffer
            remainder = ""

        # Edit current message with frozen text (no cursor)
        try:
            formatted_frozen, parse_mode = TelegramFormatter.format_text(frozen_text)
            formatted_frozen = sanitize_html_tags(formatted_frozen)
            await self._msg.edit_text(formatted_frozen, parse_mode=parse_mode)
            self._edit_count += 1
        except Exception as e:
            if "not modified" not in str(e).lower():
                logging.warning("Failed to freeze message on overflow: %s", e)

        # 2. Create new message with cursor indicator
        try:
            initial_text = remainder + STREAMING_INDICATOR if remainder.strip() else STREAMING_INDICATOR
            formatted_initial, parse_mode = TelegramFormatter.format_text(initial_text)
            new_msg = await self._msg.reply_text(
                formatted_initial,
                parse_mode=parse_mode,
            )
            # Swap to the new message
            self._msg = new_msg
            self._buffer = remainder
            self._pending_chars = 0
            self._last_edit_time = time.monotonic()
            self._msg_count += 1
            logging.info(
                "Streaming overflow → message #%d (%d chars in previous)",
                self._msg_count, len(frozen_text),
            )
        except Exception as e:
            logging.error("Failed to create overflow message: %s", e)
            # If we can't create a new message, stop further edits
            # but don't lose text (it's still in _full_text)

    def _find_split_point(self) -> int:
        """Find the best point to split the buffer so the formatted head fits within limits.

        Searches backward from the buffer end for a paragraph or line break.
        """
        text = self._buffer

        # Try progressively shorter prefixes at natural break points
        for sep in ["\n\n", "\n", ". ", " "]:
            # Look for breaks in the last 30% of the buffer (near the limit)
            search_start = max(0, len(text) * 7 // 10)
            idx = text.rfind(sep, search_start)
            if idx > 0:
                candidate = text[:idx + len(sep)]
                formatted, _ = TelegramFormatter.format_text(candidate)
                if len(formatted) <= STREAM_MSG_LIMIT:
                    return idx + len(sep)

        # Fallback: binary search for a length that fits
        lo, hi = 0, len(text)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            formatted, _ = TelegramFormatter.format_text(text[:mid])
            if len(formatted) <= STREAM_MSG_LIMIT:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    @property
    def last_message(self):
        """Return the last (most recent) message in the chain.

        This is the message that buttons should be attached to.
        """
        return self._msg

    @property
    def text(self) -> str:
        """Return the full accumulated text across all messages."""
        return self._full_text

    @property
    def edit_count(self) -> int:
        return self._edit_count

    @property
    def message_count(self) -> int:
        return self._msg_count


async def stream_and_display(
    placeholder_message,
    api_key: str,
    model_name: str,
    contents: list,
    config: types.GenerateContentConfig,
    timeout: float = 100.0,
) -> tuple[str, bool, Message | None]:
    """High-level: stream Gemini response and progressively update Telegram message.

    Supports multi-message streaming: when a single message exceeds
    Telegram's ~4096 char limit, the writer seamlessly continues into
    a new message while maintaining visual streaming UX.

    Args:
        placeholder_message: Telegram message to edit progressively.
        api_key: Gemini API key.
        model_name: Model to use.
        contents: Prepared contents list.
        config: GenerateContentConfig.
        timeout: Max streaming time.

    Returns:
        (response_text, success, last_message) tuple.
        last_message is the final message in the chain (may differ from
        placeholder_message if overflow occurred). Callers should use it
        for post-stream edits like adding buttons.
    """
    writer = StreamingWriter(placeholder_message)

    try:
        async for delta in stream_gemini_response(
            api_key, model_name, contents, config, timeout=timeout,
        ):
            await writer.write(delta)

        final_text = await writer.finalize()

        if not final_text.strip():
            return "", False, placeholder_message

        # Check finish_reason for blocked/truncated responses
        fr = stream_gemini_response.last_finish_reason
        fr_upper = (fr or "").upper()

        if fr_upper in _BLOCKED_FINISH_REASONS:
            logging.warning(
                "Streaming response blocked by model (finish_reason=%s, %d chars generated)",
                fr, len(final_text),
            )
            # User still gets what was generated, plus a note
            final_text += "\n\n⚠️ _Ответ был прерван фильтром безопасности._"

        elif fr_upper in _TRUNCATED_FINISH_REASONS:
            logging.warning(
                "Streaming response truncated (finish_reason=%s, %d chars generated)",
                fr, len(final_text),
            )
            final_text += "\n\n⚠️ _Ответ был обрезан из-за ограничения длины._"

        elif len(final_text) < 150 and fr_upper not in ("STOP", "1", "FINISH_REASON_STOP", ""):
            logging.warning(
                "Suspiciously short streaming response: %d chars, finish_reason=%s",
                len(final_text), fr,
            )

        logging.info(
            "Streaming complete: %d chars, %d edits, %d message(s), finish_reason=%s",
            len(final_text), writer.edit_count, writer.message_count, fr,
        )
        await metrics_collector.record_api_call("gemini_streaming", model_name)
        return final_text, True, writer.last_message

    except TimeoutError:
        partial = writer.text
        if partial:
            await writer.finalize()
            return partial + "\n\n⏰ _(ответ был прерван по таймауту)_", True, writer.last_message
        return "⏰ Превышено время ожидания ответа. Попробуйте позже.", False, placeholder_message

    except APIError as e:
        logging.error("Streaming API error: %s", e)
        partial = writer.text
        if partial:
            await writer.finalize()
            return partial + "\n\n⚠️ _(ответ был прерван из-за ошибки API)_", True, writer.last_message
        return "❌ Ошибка API при потоковой генерации. Попробуйте ещё раз.", False, placeholder_message

    except Exception as e:
        logging.error("Streaming failed: %s", e, exc_info=True)
        return "❌ Ошибка при потоковой генерации. Попробуйте ещё раз.", False, placeholder_message

