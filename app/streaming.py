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
import contextvars
import inspect
import logging
import random
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from google.genai.errors import APIError

from app.metrics import metrics_collector
from app.utils.formatting import TelegramFormatter
from app.utils.text_format import sanitize_html_tags, strip_formatting

if TYPE_CHECKING:
    from telegram import Message

    from app.adapters.ui_adapter import StreamingUIAdapter


# ── Configuration ────────────────────────────────────────────────────────────

# Classic mode (editMessageText) — used for groups and as fallback.
EDIT_DEBOUNCE_S = 0.6
MIN_CHUNK_SIZE = 60

# Indicator appended while streaming is in progress.
STREAMING_INDICATOR = " ▍"
# Safe limit for Telegram messages (leaves margin for HTML tag overhead).
STREAM_MSG_LIMIT = 4000


_last_finish_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar("last_finish_reason", default=None)
_last_token_count: contextvars.ContextVar[int] = contextvars.ContextVar("last_token_count", default=0)
_voice_requested: contextvars.ContextVar[bool] = contextvars.ContextVar("voice_requested", default=False)

# Tag emitted by the LLM when the user asks for voice output.
_VOICE_TAG = "[VOICE]"

_HALLUCINATED_TOOL_INLINE_RE = re.compile(
    r"\[tool_code\]\s*(?:print\()?(?:google_search\.search)\([^)\n]*\)\)?\s*",
)
_HALLUCINATED_TOOL_LINE_RE = re.compile(
    r"^(?:import google_search|(?:print\()?(?:google_search\.search)\([^)\n]*\)\)?)\s*$",
)


def set_last_finish_reason(reason: str | None) -> None:
    """Pass finish_reason from the provider back to the streaming loop."""
    _last_finish_reason.set(reason)


def set_last_token_count(count: int) -> None:
    """Pass total token count from the provider back to the streaming caller."""
    _last_token_count.set(count)


# Finish reasons that indicate the model was blocked mid-response
_BLOCKED_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "2",
        "FINISH_REASON_SAFETY",
        "RECITATION",
        "4",
        "FINISH_REASON_RECITATION",
    }
)

_TRUNCATED_FINISH_REASONS = frozenset(
    {
        "MAX_TOKENS",
        "3",
        "FINISH_REASON_MAX_TOKENS",
    }
)


def _detect_open_markdown(text: str) -> tuple[str, str]:
    """Detect unclosed markdown formatting in *text* and return (suffix, prefix).

    *suffix* should be appended to the frozen (first) message to close any
    open constructs so that ``markdown_to_html`` produces balanced HTML.

    *prefix* should be prepended to the remainder (second message) so that
    the open formatting is visually continued.

    Handles:
    - Fenced code blocks (`` ``` ``)
    - Inline code (`` ` ``)
    - Bold (``**``)
    - Italic (``*`` / ``_``)
    """
    suffix_parts: list[str] = []
    prefix_parts: list[str] = []

    # --- 1. Fenced code blocks (``` ... ```) ---------------------------------
    #   Count occurrences of triple-backtick fences *outside* of themselves.
    #   An odd count means we are inside an unclosed code block.
    fence_re = re.compile(r"^```", re.MULTILINE)
    fence_count = len(fence_re.findall(text))
    if fence_count % 2 == 1:
        # Inside an unclosed code block —
        # close it in the first message, reopen in the second.
        # Try to detect original language specifier for reopening.
        last_fence_idx = text.rfind("```")
        after_fence = text[last_fence_idx + 3 :]
        lang_match = re.match(r"([a-zA-Z0-9+#._-]{1,20})", after_fence)
        lang = lang_match.group(1) if lang_match else ""
        suffix_parts.append("\n```")
        prefix_parts.append(f"```{lang}\n" if lang else "```\n")
        # Inside a code block no inline formatting applies, return early.
        return "".join(suffix_parts), "".join(prefix_parts)

    # --- 2. Strip ALL code blocks for further inline analysis -----------------
    # Fences are completely closed (fence_count is even).
    # We must remove them before counting `_` or `*` so we don't count formatting
    # characters that are safely nested inside code blocks.
    stripped_fences = re.sub(r"```.*?```", "", text, flags=re.DOTALL)

    # --- 3. Inline code (` ... `) outside of fenced blocks --------------------
    backtick_count = stripped_fences.count("`")
    if backtick_count % 2 == 1:
        suffix_parts.append("`")
        prefix_parts.append("`")
        # Inside inline code no other formatting applies.
        return "".join(suffix_parts), "".join(prefix_parts)

    # Strip inline code before analyzing styling marks
    stripped_code = re.sub(r"`[^`]*`", "", stripped_fences)

    # Strip completed markdown links — their content shouldn't affect marker counts
    # [text](url) → text  (preserves link text for marker analysis)
    stripped_code = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", stripped_code)

    # --- 4. Bold (**...**) ----------------------------------------------------
    bold_count = stripped_code.count("**")
    if bold_count % 2 == 1:
        suffix_parts.append("**")
        prefix_parts.append("**")

    # --- 5. Italic (*...* or _..._) -------------------------------------------
    #   After removing ** pairs, count remaining lone * characters.
    no_bold = stripped_code.replace("**", "")
    lone_star = no_bold.count("*")
    if lone_star % 2 == 1:
        suffix_parts.append("*")
        prefix_parts.append("*")

    #   Also check for _..._ italic (after removing __ pairs).
    no_double_under = stripped_code.replace("__", "")
    lone_under = no_double_under.count("_")
    if lone_under % 2 == 1:
        suffix_parts.append("_")
        prefix_parts.append("_")

    # --- 6. Strikethrough (~~...~~) -------------------------------------------
    tilde_pairs = stripped_code.count("~~")
    if tilde_pairs % 2 == 1:
        suffix_parts.append("~~")
        prefix_parts.append("~~")

    return "".join(suffix_parts), "".join(prefix_parts)


class StreamingWriter:
    """Debounced Telegram message updater for streaming AI responses.

    Edits a placeholder message via ``editMessageText`` with debouncing (0.6 s).

    When the current message's formatted text approaches STREAM_MSG_LIMIT,
    the writer finalizes the current message and creates a new one via
    reply_text, continuing streaming seamlessly into it.
    """

    def __init__(
        self,
        adapter: StreamingUIAdapter,
        use_telegraph_fallback: bool = True,
    ):
        self._adapter = adapter  # Generic UI adapter
        self._buffer = ""  # Buffer for CURRENT message only
        self._full_text = ""  # Entire accumulated text across all messages
        self._last_edit_time = 0.0
        self._pending_chars = 0
        self._edit_count = 0
        self._msg_count = 1  # How many messages in chain

        self._debounce_s = EDIT_DEBOUNCE_S
        self._min_chunk = MIN_CHUNK_SIZE
        self._use_telegraph = use_telegraph_fallback
        self._telegraph_engaged = False

    @staticmethod
    def _is_rate_limited(error: Exception) -> bool:
        """Check if an error is a Telegram rate-limit (429 / flood control)."""
        msg = str(error).lower()
        return "429" in msg or "flood" in msg or "too many requests" in msg or "retry_after" in msg

    async def _retry_edit(
        self,
        text: str,
        parse_mode: str | None,
        *,
        reply_markup: object | None = None,
        max_retries: int = 3,
    ) -> bool:
        """Edit message with exponential backoff on rate-limit errors.

        Returns True if the edit succeeded, False if all retries exhausted.
        """
        for attempt in range(max_retries):
            try:
                await self._adapter.edit_message(
                    text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )  # type: ignore[arg-type]
                return True
            except Exception as e:
                err_str = str(e).lower()
                if "not modified" in err_str:
                    return True  # Not an error — text unchanged
                if self._is_rate_limited(e) and attempt < max_retries - 1:
                    backoff = (0.5 * (2**attempt)) + random.uniform(0, 0.3)
                    logging.debug(
                        "Rate-limited on edit (attempt %d/%d), backing off %.2fs",
                        attempt + 1,
                        max_retries,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    # Adaptive debounce: escalate interval to reduce future pressure
                    self._debounce_s = min(self._debounce_s * 1.5, 3.0)
                    continue
                # Non-retriable error
                if attempt == max_retries - 1 or not self._is_rate_limited(e):
                    logging.warning(
                        "Streaming edit failed (attempt %d/%d): %s",
                        attempt + 1,
                        max_retries,
                        e,
                    )
                    return False
        return False

    def _format_for_telegram(self, text: str) -> tuple[str, str | None]:
        """Format text and sanitize HTML in one step.

        Ensures every path through the writer produces valid, balanced
        Telegram HTML — prevents the bug where sanitize_html_tags was
        missing from the draft finalize path.
        """
        formatted, parse_mode = TelegramFormatter.format_text(text)
        sanitized = sanitize_html_tags(formatted)
        assert sanitized is not None  # guaranteed: input is str, not None
        return sanitized, parse_mode

    @staticmethod
    def _strip_hallucinated_tool_trace(text: str) -> str:
        """Remove only clearly hallucinated internal tool-execution traces.

        Preserve normal code samples, fenced blocks, and explanatory mentions.
        """
        if "[tool_code]" not in text:
            return text

        cleaned = StreamingWriter._remove_tool_code_lines(text)
        return _HALLUCINATED_TOOL_INLINE_RE.sub("", cleaned)

    @staticmethod
    def _remove_tool_code_lines(text: str) -> str:
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        skip = False
        for line in lines:
            stripped = line.strip()
            if stripped == "[tool_code]":
                skip = True
                continue
            if skip and (not stripped or _HALLUCINATED_TOOL_LINE_RE.match(stripped)):
                continue
            if skip:
                skip = False
            kept.append(line)
        return "".join(kept)

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._full_text += delta
        self._pending_chars += len(delta)

        # Clean clearly hallucinated tool execution traces without stripping legitimate code samples.
        if "[tool_code]" in self._buffer:
            old_len = len(self._buffer)
            new_buf = self._strip_hallucinated_tool_trace(self._buffer)

            if new_buf != self._buffer:
                diff = old_len - len(new_buf)
                self._buffer = new_buf
                # Keep full_text synchronized
                new_full = self._strip_hallucinated_tool_trace(self._full_text)
                self._full_text = new_full
                self._pending_chars = max(0, self._pending_chars - diff)

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= self._min_chunk:
            await self._flush(final=False)

    async def finalize(self, reply_markup: object | None = None) -> str:
        """Send the final version of the message (no cursor indicator).

        Args:
            reply_markup: Optional reply markup to attach to the final message
                atomically, avoiding a separate edit_reply_markup call.

        Returns:
            The complete accumulated text (across all messages).
        """
        await self._flush(final=True, reply_markup=reply_markup)
        return self._full_text

    async def _flush(self, *, final: bool = False, reply_markup: object | None = None, _depth: int = 0) -> None:
        """Send the current buffer to Telegram using classic edit_text."""
        text = self._buffer
        if not text.strip():
            return
        display_text = text if final else text + STREAMING_INDICATOR

        try:
            formatted_text, parse_mode = self._format_for_telegram(display_text)

            if getattr(self, "_telegraph_engaged", False):
                return  # Stream is frozen in UI, doing silent generation

            if len(formatted_text) > STREAM_MSG_LIMIT and not final:
                if getattr(self, "_overflow_failed", False):
                    return  # Circuit breaker
                # Mid-stream overflow: finalize current, start new message (or freeze for Telegraph)
                await self._overflow_to_new_message()
                return

            if len(formatted_text) > STREAM_MSG_LIMIT and final:
                if getattr(self, "_overflow_failed", False) or _depth > 5 or self._msg_count > 8:
                    formatted_text = sanitize_html_tags(formatted_text[:STREAM_MSG_LIMIT])
                else:
                    # Final flush overflows — split into finalize + new message (or freeze for Telegraph)
                    await self._overflow_to_new_message()
                    # Now flush the remainder (recursion with reduced buffer)
                    await self._flush(final=True, reply_markup=reply_markup, _depth=_depth + 1)
                    return

            success = await self._retry_edit(
                formatted_text,
                parse_mode,
                reply_markup=reply_markup,
            )
            if success:
                self._last_edit_time = time.monotonic()
                self._pending_chars = 0
                self._edit_count += 1
        except Exception as e:
            logging.warning("Streaming flush unexpected error: %s", e)

    async def _overflow_to_new_message(self) -> None:
        """Finalize current message and create new placeholder for continued streaming."""
        # 1. Finalize the current message with the text that fits
        #    Two-phase split: returns (split_point, pre-formatted HTML)
        split_point, pre_formatted = self._find_split_point()

        if split_point > 0:
            frozen_text = self._buffer[:split_point]
            remainder = self._buffer[split_point:].lstrip()
        else:
            # No good split point — take what we have minus some margin
            frozen_text = self._buffer
            remainder = ""

        # Carry open markdown formatting across the message boundary:
        # close constructs in the frozen part, reopen them in the remainder.
        md_suffix, md_prefix = _detect_open_markdown(frozen_text)
        if md_suffix:
            frozen_text += md_suffix
            pre_formatted = None  # Invalidate: frozen_text was modified
        if md_prefix and remainder:
            remainder = md_prefix + remainder

        # Freeze current message (or send new if placeholder was deleted)
        if self._use_telegraph:
            frozen_text += "\n\n... 📝 _[Текст получается очень длинным, формирую статью]_"
            pre_formatted = None

        try:
            if pre_formatted is not None:
                # Reuse the formatted text from _find_split_point (avoids re-formatting).
                # MUST sanitize: the split point may fall inside a code block, leaving
                # open <pre> or <code> tags in the cached HTML fragment.
                formatted_frozen = sanitize_html_tags(pre_formatted)
                parse_mode = "HTML"
            else:
                formatted_frozen, parse_mode = self._format_for_telegram(frozen_text)  # type: ignore[assignment]  # parse_mode is always str from TelegramFormatter

            # Failsafe limit if indicator pushed it over
            if len(formatted_frozen) > STREAM_MSG_LIMIT:
                formatted_frozen = sanitize_html_tags(formatted_frozen[:STREAM_MSG_LIMIT])

            await self._adapter.edit_message(formatted_frozen, parse_mode=parse_mode)  # type: ignore[arg-type]

            self._edit_count += 1
        except Exception as e:
            if "not modified" not in str(e).lower():
                logging.warning("Failed to freeze message on overflow: %s", e)

        if self._use_telegraph:
            self._telegraph_engaged = True
            return

        # 2. Create new message with cursor indicator
        try:
            initial_text = remainder + STREAMING_INDICATOR if remainder.strip() else STREAMING_INDICATOR
            formatted_initial, parse_mode = self._format_for_telegram(initial_text)  # type: ignore[assignment]  # parse_mode always str

            self._adapter = await self._adapter.reply_new_message(
                formatted_initial,
                parse_mode=parse_mode,  # type: ignore[arg-type]
            )
            # Swap to the new message
            self._buffer = remainder
            self._pending_chars = 0
            self._last_edit_time = time.monotonic()
            self._msg_count += 1
            logging.info(
                "Streaming overflow → message #%d (%d chars in previous)",
                self._msg_count,
                len(frozen_text),
            )
        except Exception as e:
            self._overflow_retries = getattr(self, "_overflow_retries", 0) + 1
            if self._overflow_retries < 3:
                logging.warning(
                    "Overflow retry %d/3: %s",
                    self._overflow_retries,
                    e,
                )
                self._last_edit_time = time.monotonic() + 2.0  # Backoff
                return  # Will retry on next flush

            # 3 retries exhausted → fallback: send plain text
            logging.error(
                "Overflow failed after 3 retries (%d chars buffered). Sending plain text fallback.",
                len(self._buffer),
                exc_info=True,
            )
            try:
                plain = strip_formatting(self._buffer)
                await self._adapter.edit_message(
                    plain[:STREAM_MSG_LIMIT],
                    parse_mode="",  # type: ignore[arg-type]
                )
            except Exception:
                pass  # Last resort — text is still in _full_text
            self._overflow_failed = True

    def _find_split_point(self) -> tuple[int, str | None]:
        """Find the best point to split the buffer so the formatted head fits within limits.

        Uses a two-phase approach to minimize expensive format_text calls:
        1. Estimate a raw-text limit from the expansion ratio of the full buffer.
        2. Find a natural break near that estimate and verify with one format call.

        Returns:
            (split_point, formatted_text) — formatted_text is the pre-formatted
            HTML for the frozen chunk (or None if it couldn't be determined),
            allowing the caller to skip a redundant format_text call.
        """
        text = self._buffer

        # Phase 1: estimate the expansion ratio (raw → HTML)
        full_formatted, _ = self._format_for_telegram(text)
        ratio = len(full_formatted) / max(len(text), 1)
        estimated_raw_limit = int(STREAM_MSG_LIMIT / ratio * 0.95)  # 5% safety margin

        # Phase 2: find a natural break near the estimated limit
        search_lo = max(0, estimated_raw_limit - 500)
        search_hi = min(len(text), estimated_raw_limit + 100)

        for sep in ["\n\n", "\n", ". ", " "]:
            idx = text.rfind(sep, search_lo, search_hi)
            if idx > 0:
                end = idx + len(sep)
                formatted, _ = self._format_for_telegram(text[:end])
                if len(formatted) <= STREAM_MSG_LIMIT:
                    return end, formatted

        # Fallback: try the raw estimate directly
        formatted, _ = self._format_for_telegram(text[:estimated_raw_limit])
        if len(formatted) <= STREAM_MSG_LIMIT:
            return estimated_raw_limit, formatted

        # Last resort: step back in larger increments
        step = max(estimated_raw_limit // 10, 50)
        for offset in range(step, estimated_raw_limit, step):
            candidate_len = estimated_raw_limit - offset
            if candidate_len <= 0:
                break
            formatted, _ = self._format_for_telegram(text[:candidate_len])
            if len(formatted) <= STREAM_MSG_LIMIT:
                return candidate_len, formatted

        return 0, None

    @property
    def last_message(self):
        """Return the last (most recent) message in the chain.

        This is the message that buttons should be attached to.
        """
        return self._adapter.last_message

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
    placeholder_message: Message,
    model_name: str,
    history: list[dict[str, Any]],
    system_instruction: str | None = None,
    thinking_level: str | None = None,
    user_id: int | None = None,
    *,
    bot: Any | None = None,
    chat_id: int = 0,
    reply_markup: Any | None = None,
    footer_text: str | None = None,
    enable_web_search: bool = False,
    yield_hook: Any | None = None,
    post_processor: Callable[[str], tuple[str, object | None]] | None = None,
) -> tuple[str, bool, Message, int, bool, bool]:
    """High-level: stream AI response and progressively update Telegram message.

    Supports multi-message streaming: when a single message exceeds
    Telegram's ~4096 char limit, the writer seamlessly continues into
    a new message while maintaining visual streaming UX.

    Args:
        placeholder_message: Telegram message to edit progressively.
        model_name: Model to use.
        history: Chat history.
        system_instruction: Optional system instruction.
        thinking_level: Optional thinking level (e.g. "low", "high").
        user_id: User ID for rate limiting.
        bot: Bot instance (required for draft mode).
        chat_id: Target chat ID.
        chat_type: Chat type ("private", "group", "supergroup", etc.).
        reply_markup: Optional reply markup to attach atomically to the
            final message (avoids a separate edit_reply_markup call).
        yield_hook: Optional callable to invoke immediately before processing the VERY FIRST
            chunk. Used to terminate heartbeats exactly when text begins.

    Returns:
        (response_text, success, last_message, token_count, was_interrupted,
         voice_requested) tuple.
        token_count is 0 when not available from the provider.
        last_message is the final message in the chain (may differ from
        placeholder_message if overflow occurred). Callers should use it
        for post-stream edits like adding buttons.
        was_interrupted is True when the stream was interrupted mid-flight
        by an API error and the response is only partial.
        voice_requested is True when the LLM detected the user wants voice
        output and emitted the [VOICE] tag.
    """
    from app.adapters.ui_adapter import TelegramMessageAdapter

    adapter = TelegramMessageAdapter(
        message=placeholder_message,
        bot=bot,
        chat_id=chat_id,
        draft_id=0,
    )

    writer = StreamingWriter(adapter)

    # Reset voice intent flag for this stream
    _voice_requested.set(False)
    _was_interrupted = False

    # ── UX State Indication: delayed feedback if API is slow ─────────
    # If no chunks arrive within 5 seconds, update placeholder to inform
    # the user and offer a [Cancel] button.
    _first_chunk_received = False
    _ux_feedback_task: asyncio.Task | None = None

    async def _send_delayed_feedback() -> None:
        """Show 'high load' status after 5s if no chunks arrived."""
        await asyncio.sleep(5.0)
        if _first_chunk_received:
            return
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            cancel_kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("❌ Отменить", callback_data="cancel_generation")],
                ]
            )
            await placeholder_message.edit_text(
                "⏳ Запрос в обработке: высокая нагрузка на сервера...",
                reply_markup=cancel_kb,
            )
        except Exception as e:
            logging.debug("Delayed UX feedback failed (non-critical): %s", e)

    _ux_feedback_task = asyncio.create_task(_send_delayed_feedback())

    try:
        from app.providers import get_provider_router

        router = get_provider_router()
        _voice_tag_checked = False

        # Mark task as waiting for network headers (TTFB tracking)
        if user_id:
            from app import state as _state_mod

            _state_mod.mark_network_waiting(user_id)

        async for delta in router.stream_response(
            preferred_model=model_name,
            history=history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            thinking_level=thinking_level,
            max_key_retries=3,
            enable_web_search=enable_web_search,
        ):
            # Skip non-str sentinels emitted by the Gemini provider
            # (e.g. _GroundingMeta for grounding citations).  This layer
            # has no grounding consumer, so they are safely discarded.
            if not isinstance(delta, str):
                continue

            # ── [VOICE] tag detection (first chunk only) ─────────────────
            # The LLM emits "[VOICE] ..." when user asks for audio output.
            # We strip the tag on the fly so the user never sees it.
            if not _voice_tag_checked:
                stripped = delta.lstrip()
                if stripped.startswith(_VOICE_TAG):
                    _voice_requested.set(True)
                    delta = stripped[len(_VOICE_TAG) :].lstrip()
                    logging.info("Voice intent detected via [VOICE] tag")
                    # Fire-and-forget metrics recording to avoid blocking stream
                    try:
                        from app.metrics import role_conv_metrics
                        from app.utils.background_tasks import submit_task

                        submit_task(role_conv_metrics.record_voice_intent())
                    except Exception as metric_err:
                        logging.warning("Failed to record voice intent metric: %s", metric_err)
                # Only check the very first non-empty chunk
                if stripped:
                    _voice_tag_checked = True

            if delta:
                # Cancel the delayed UX feedback on first real chunk
                if not _first_chunk_received:
                    _first_chunk_received = True
                    if _ux_feedback_task and not _ux_feedback_task.done():
                        _ux_feedback_task.cancel()
                    # Mark task as alive (receiving data) — disables stall cancellation
                    if user_id:
                        from app import state as _state_mod

                        _state_mod.mark_network_alive(user_id)

                if yield_hook is not None:
                    if inspect.iscoroutinefunction(yield_hook):
                        await yield_hook()
                    else:
                        yield_hook()
                    yield_hook = None
                await writer.write(delta)

        # Inject optional footer (e.g. memory indicator) as part of the stream
        # so the user sees it arrive smoothly — no post-hoc edit_text jump.
        if footer_text:
            await writer.write(footer_text)

    except TimeoutError:
        _was_interrupted = True
        logging.warning("Stream timeout")
        if not writer.text:
            return (
                "⏰ Превышено время ожидания ответа. Попробуйте позже.",
                False,
                placeholder_message,  # type: ignore[return-value]
                0,
                False,
                False,
            )
        writer._full_text += "\n\n⏰ _(ответ был прерван по таймауту)_"
        writer._buffer += "\n\n⏰ _(ответ был прерван по таймауту)_"

    except APIError as e:
        _was_interrupted = True
        logging.error("Streaming API error: %s", e)
        if not writer.text:
            return (
                "❌ Ошибка API при потоковой генерации. Попробуйте ещё раз.",
                False,
                placeholder_message,  # type: ignore[return-value]
                0,
                False,
                False,
            )
        writer._full_text += "\n\n⚠️ _(ответ был прерван из-за ошибки сервера)_"
        writer._buffer += "\n\n⚠️ _(ответ был прерван из-за ошибки сервера)_"

    except httpx.HTTPStatusError as e:
        _was_interrupted = True
        logging.error(
            "Streaming HTTP provider error: status=%d url=%s body=%s",
            e.response.status_code,
            e.request.url,
            e.response.text[:200],
        )
        if not writer.text:
            return (
                f"❌ Ошибка поставщика (HTTP {e.response.status_code}). Попробуйте ещё раз.",
                False,
                placeholder_message,  # type: ignore[return-value]
                0,
                False,
                False,
            )
        writer._full_text += "\n\n⚠️ _(ответ был прерван из-за ошибки HTTP)_"
        writer._buffer += "\n\n⚠️ _(ответ был прерван из-за ошибки HTTP)_"

    except Exception as e:
        _was_interrupted = True
        logging.error("Streaming failed: %s", e, exc_info=True)
        if not writer.text:
            return (
                "❌ Ошибка при потоковой генерации. Попробуйте ещё раз.",
                False,
                placeholder_message,  # type: ignore[return-value]
                0,
                False,
                False,
            )
        writer._full_text += "\n\n⚠️ _(ответ был прерван из-за непредвиденной ошибки)_"
        writer._buffer += "\n\n⚠️ _(ответ был прерван из-за непредвиденной ошибки)_"

    # Ensure the delayed UX feedback timer is always cancelled
    if _ux_feedback_task and not _ux_feedback_task.done():
        _ux_feedback_task.cancel()

    # Clean up network stall tracking
    if user_id:
        from app import state as _state_mod

        _state_mod.clear_network_stall(user_id)

    markup = reply_markup
    if post_processor:
        # Strip tags natively before flushing final text to Telegram
        clean_text, markup = post_processor(writer._full_text)
        if clean_text != writer._full_text:
            # Recompute _buffer as the corresponding tail of clean_text.
            # Old approach (writer._buffer[:-removed]) only worked when the
            # tag was literally the last bytes of _buffer; any trailing text
            # after the tag caused it to cut the wrong bytes.
            buf_start_in_full = len(writer._full_text) - len(writer._buffer)
            writer._buffer = clean_text[buf_start_in_full:] if buf_start_in_full < len(clean_text) else ""
            writer._full_text = clean_text

    final_text = await writer.finalize(reply_markup=markup)

    if not final_text.strip():
        return "", False, placeholder_message, 0, False, False

    # Long Read transition: stream exceeded threshold → publish via Mini App / Telegraph
    if getattr(writer, "_telegraph_engaged", False):
        import uuid

        from app.config import settings as _settings
        from app.utils.telegraph import create_telegraph_page

        title = "Ответ ИИ"
        if history:
            for item in reversed(history):
                if item.get("role") == "user" and item.get("parts"):
                    raw = strip_formatting(item["parts"][0])
                    if raw:
                        title = raw[:60].strip()
                        if len(raw) > 60:
                            title += "…"
                    break

        webapp_base = getattr(_settings, "WEBAPP_BASE_URL", "").rstrip("/")

        if webapp_base:
            # ── Hybrid path: Redis primary + Telegraph background fallback ──
            from app.cache import store_long_message, store_telegraph_url

            uid = str(uuid.uuid4())
            stored = await store_long_message(uid, final_text)

            if stored:
                reader_url = f"{webapp_base}/webapp/reader?id={uid}"
                logging.info("Long read stored in Redis uid=%s (%d chars)", uid, len(final_text))
            else:
                # Redis write failed — fall through to plain Telegraph
                reader_url = None
                logging.warning("Redis unavailable for long read uid=%s; falling back to Telegraph", uid)

            if reader_url:
                # Build the frozen message update immediately
                summary_lines = final_text[:800].strip()
                if len(final_text) > 800:
                    summary_lines += "…"

                from app.utils.ux_improvements import wrap_in_expandable_blockquote

                sanitized_summary = sanitize_html_tags(TelegramFormatter.format_text(summary_lines)[0]) or summary_lines
                summary_html = wrap_in_expandable_blockquote(sanitized_summary)

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                full_text_html = f'{summary_html}\n\n<i>(...текст превышает лимит. Продолжение доступно по кнопке <b>«Развернуть статью»</b> 👇)</i> <a href="{reader_url}">&#8203;</a>'

                buttons = []
                if markup and getattr(markup, "inline_keyboard", None):
                    buttons = list(getattr(markup, "inline_keyboard", []))

                from telegram import WebAppInfo

                buttons.insert(
                    0, [InlineKeyboardButton("📄 Развернуть статью (Mini App)", web_app=WebAppInfo(url=reader_url))]
                )
                new_markup = InlineKeyboardMarkup(buttons)

                try:
                    await writer._adapter.edit_message(
                        full_text_html,
                        parse_mode="HTML",
                        reply_markup=new_markup,
                    )
                except Exception as e:
                    logging.warning("Failed to update frozen long-read message: %s", e)

                # Background: create Telegraph as permanent fallback (non-blocking)
                async def _bg_telegraph(uid: str, title: str, text: str) -> None:
                    try:
                        t_url = await create_telegraph_page(title, text)
                        if t_url:
                            await store_telegraph_url(uid, t_url)
                            logging.info("Telegraph fallback created for uid=%s → %s", uid, t_url)
                    except Exception as exc:
                        logging.warning("Background Telegraph creation failed uid=%s: %s", uid, exc)

                _bg_tasks: set[asyncio.Task[None]] = getattr(writer, "_bg_tasks", set())
                task = asyncio.create_task(_bg_telegraph(uid, title, final_text))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
                writer._bg_tasks = _bg_tasks  # type: ignore[attr-defined]

                # We're done — skip the Telegraph-only path below
                webapp_handled = True
            else:
                webapp_handled = False
        else:
            webapp_handled = False

        if not webapp_handled:
            # ── Telegraph-only fallback (no WEBAPP_BASE_URL configured) ──
            t_url = await create_telegraph_page(title, final_text)
            if t_url:
                logging.info("Stream transitioned to Telegraph (no webapp): %s", t_url)
                summary_lines = final_text[:800].strip()
                if len(final_text) > 800:
                    summary_lines += "…"

                from app.utils.ux_improvements import wrap_in_expandable_blockquote

                sanitized_summary = sanitize_html_tags(TelegramFormatter.format_text(summary_lines)[0]) or summary_lines
                summary_html = wrap_in_expandable_blockquote(sanitized_summary)

                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                full_text_html = f'{summary_html}\n\n📖 <a href="{t_url}">Читать статью (Instant View)</a>'

                buttons = []
                if markup and getattr(markup, "inline_keyboard", None):
                    buttons = list(getattr(markup, "inline_keyboard", []))
                buttons.insert(0, [InlineKeyboardButton("📖 Открыть статью", url=t_url)])
                new_markup = InlineKeyboardMarkup(buttons)

                try:
                    await writer._adapter.edit_message(
                        full_text_html,
                        parse_mode="HTML",
                        reply_markup=new_markup,
                    )
                except Exception as e:
                    logging.warning("Failed to update frozen telegraph message: %s", e)
            else:
                logging.warning("Telegraph creation failed for frozen stream; sending long message fallback.")
                from app.utils.messaging import send_long_message

                await send_long_message(writer.last_message, final_text, reply_markup=markup)  # type: ignore[arg-type]

    # Check finish_reason for blocked/truncated responses
    fr = _last_finish_reason.get()
    fr_upper = (fr or "").upper()

    if fr_upper in _BLOCKED_FINISH_REASONS:
        logging.warning(
            "Streaming response blocked by model (finish_reason=%s, %d chars generated)",
            fr,
            len(final_text),
        )
        # User still gets what was generated, plus a note
        final_text += "\n\n⚠️ _Ответ был прерван фильтром безопасности._"

    elif fr_upper in _TRUNCATED_FINISH_REASONS:
        logging.warning(
            "Streaming response truncated (finish_reason=%s, %d chars generated)",
            fr,
            len(final_text),
        )
        final_text += "\n\n⚠️ _Ответ был обрезан из-за ограничения длины._"

    elif len(final_text) < 150 and fr_upper not in (
        "STOP",
        "1",
        "FINISH_REASON_STOP",
        "",
    ):
        logging.warning(
            "Suspiciously short streaming response: %d chars, finish_reason=%s",
            len(final_text),
            fr,
        )

    logging.info(
        "Streaming complete: %d chars, %d edits, %d message(s), finish_reason=%s",
        len(final_text),
        writer.edit_count,
        writer.message_count,
        fr,
    )
    await metrics_collector.record_api_call("gemini_streaming", model_name)
    actual_tokens = _last_token_count.get()
    voice_requested = _voice_requested.get()
    return final_text, True, writer.last_message, actual_tokens, _was_interrupted, voice_requested  # type: ignore[return-value]
