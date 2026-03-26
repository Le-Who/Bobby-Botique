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
import random
import re
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types
from google.genai.errors import APIError
from telegram.error import TelegramError

from app.metrics import metrics_collector
from app.request_context import get_request_id
from app.utils.formatting import TelegramFormatter
from app.utils.text_format import sanitize_html_tags, strip_formatting

if TYPE_CHECKING:
    from telegram import Message

    from app.adapters.ui_adapter import StreamingUIAdapter


# ── Configuration ────────────────────────────────────────────────────────────

# Classic mode (editMessageText) — used for groups and as fallback.
EDIT_DEBOUNCE_S = 0.6
MIN_CHUNK_SIZE = 60

# Draft mode (sendMessageDraft) — used for private chats.
DRAFT_DEBOUNCE_S = 0.3
DRAFT_MIN_CHUNK = 20

# Indicator appended while streaming is in progress (classic mode only).
STREAMING_INDICATOR = " ▍"
# Safe limit for Telegram messages (leaves margin for HTML tag overhead).
STREAM_MSG_LIMIT = 4000

import contextvars

_last_finish_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar("last_finish_reason", default=None)
_last_token_count: contextvars.ContextVar[int] = contextvars.ContextVar("last_token_count", default=0)


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

    Supports two modes:

    **Draft mode** (private chats): Uses ``sendMessageDraft`` from Bot API 9.5
    for fast, animated streaming with relaxed rate limits (0.3 s debounce).

    **Classic mode** (groups / fallback): Edits a placeholder message via
    ``editMessageText`` with debouncing (0.6 s).

    When the current message's formatted text approaches STREAM_MSG_LIMIT,
    the writer finalizes the current message and creates a new one via
    reply_text, continuing streaming seamlessly into it.
    """

    def __init__(
        self,
        adapter: StreamingUIAdapter,
        *,
        chat_type: str = "private",
    ):
        self._adapter = adapter  # Generic UI adapter
        self._buffer = ""  # Buffer for CURRENT message only
        self._full_text = ""  # Entire accumulated text across all messages
        self._last_edit_time = 0.0
        self._pending_chars = 0
        self._edit_count = 0
        self._msg_count = 1  # How many messages in chain

        # Draft mode: supported if adapter has draft capability
        self._use_drafts = chat_type == "private" and getattr(adapter, "_bot", None) is not None
        # Track whether placeholder was deleted for draft mode
        self._placeholder_deleted = False

        # Mode-specific debounce
        if self._use_drafts:
            self._debounce_s = DRAFT_DEBOUNCE_S
            self._min_chunk = DRAFT_MIN_CHUNK
        else:
            self._debounce_s = EDIT_DEBOUNCE_S
            self._min_chunk = MIN_CHUNK_SIZE

    def _switch_to_classic(self) -> None:
        """Switch from draft mode to classic mode (edit-based streaming)."""
        self._use_drafts = False
        self._debounce_s = EDIT_DEBOUNCE_S
        self._min_chunk = MIN_CHUNK_SIZE

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

    async def _prepare_draft_mode(self) -> bool:
        """One-time: delete placeholder before first draft to prevent dual-display.

        Returns True if ready for drafts, False if fell back to classic.
        """
        if self._placeholder_deleted:
            return True
        try:
            await self._adapter.delete_placeholder()
            self._placeholder_deleted = True
            return True
        except Exception as e:
            logging.warning(
                "Cannot delete placeholder for draft mode: %s — falling back to classic",
                e,
            )
            self._switch_to_classic()
            return False

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._full_text += delta
        self._pending_chars += len(delta)

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= self._min_chunk:
            await self._flush(final=False)

    async def finalize(self, reply_markup: object | None = None) -> str:
        """Send the final version of the message (no cursor indicator).

        If the placeholder was deleted (draft mode), sends a new permanent
        message via ``send_final_message`` — the draft auto-clears when
        the bot sends a real message.  Otherwise uses ``edit_text``.

        Args:
            reply_markup: Optional reply markup to attach to the final message
                atomically, avoiding a separate edit_reply_markup call.

        Returns:
            The complete accumulated text (across all messages).
        """
        await self._flush(final=True, reply_markup=reply_markup)
        return self._full_text

    async def _flush(self, *, final: bool = False, reply_markup: object | None = None, _depth: int = 0) -> None:
        """Send the current buffer to Telegram.

        Draft mode: calls ``send_message_draft`` for mid-stream updates.
        On first draft, deletes the placeholder to prevent dual-display.
        Classic mode: always uses ``edit_text``.
        """
        text = self._buffer
        if not text.strip():
            return

        # Draft mode mid-stream: use sendMessageDraft (no cursor needed)
        if self._use_drafts and not final:
            # One-time: delete placeholder before first draft
            if not await self._prepare_draft_mode():
                # Fell back to classic, retry flush
                await self._flush(final=final)
                return

            try:
                formatted_text, parse_mode = self._format_for_telegram(text)

                if len(formatted_text) > STREAM_MSG_LIMIT:
                    if getattr(self, "_overflow_failed", False):
                        return  # Circuit breaker: don't loop if overflow is failing
                    # Overflow: finalize frozen text, continue in classic
                    await self._overflow_to_new_message()
                    return

                await self._adapter.send_draft(
                    text=formatted_text,
                    parse_mode=parse_mode,  # type: ignore[arg-type]
                )
                self._last_edit_time = time.monotonic()
                self._pending_chars = 0
                self._edit_count += 1
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logging.warning(
                        "Draft streaming failed (attempt %d): %s — falling back to classic",
                        self._edit_count,
                        e,
                    )
                    self._switch_to_classic()
                    # If placeholder was deleted, create a recovery message
                    if self._placeholder_deleted:
                        try:
                            display = text + STREAMING_INDICATOR
                            fmt, pm = self._format_for_telegram(display)
                            await self._adapter.send_final_message(fmt, parse_mode=pm)  # type: ignore[arg-type]
                            self._placeholder_deleted = False
                            self._last_edit_time = time.monotonic()
                            self._pending_chars = 0
                            self._edit_count += 1
                        except Exception as recovery_err:
                            logging.error("Draft fallback recovery failed: %s", recovery_err)
                    else:
                        await self._flush(final=final)
            return

        # Draft mode finalize — send new permanent message (placeholder was deleted)
        if self._placeholder_deleted and final:
            try:
                formatted_text, parse_mode = self._format_for_telegram(text)

                if len(formatted_text) > STREAM_MSG_LIMIT:
                    if getattr(self, "_overflow_failed", False):
                        # Force send clamped text to avoid losing everything
                        formatted_text = sanitize_html_tags(formatted_text[:STREAM_MSG_LIMIT])
                    else:
                        await self._overflow_to_new_message()
                        await self._flush(final=True, reply_markup=reply_markup)
                        return

                await self._adapter.send_final_message(
                    formatted_text,
                    parse_mode=parse_mode,  # type: ignore[arg-type]
                    reply_markup=reply_markup,
                )
                self._last_edit_time = time.monotonic()
                self._pending_chars = 0
                self._edit_count += 1
            except Exception as e:
                logging.error("Failed to send final message after draft streaming: %s", e)
            return

        # Classic mode (or final flush when placeholder exists)
        display_text = text if final else text + STREAMING_INDICATOR

        try:
            formatted_text, parse_mode = self._format_for_telegram(display_text)

            if len(formatted_text) > STREAM_MSG_LIMIT and not final:
                if getattr(self, "_overflow_failed", False):
                    return  # Circuit breaker
                # Mid-stream overflow: finalize current, start new message
                await self._overflow_to_new_message()
                return

            if len(formatted_text) > STREAM_MSG_LIMIT and final:
                if getattr(self, "_overflow_failed", False) or _depth > 5 or self._msg_count > 8:
                    formatted_text = sanitize_html_tags(formatted_text[:STREAM_MSG_LIMIT])
                else:
                    # Final flush overflows — split into finalize + new message
                    await self._overflow_to_new_message()
                    # Now flush the remainder (recursion with reduced buffer)
                    await self._flush(final=True, reply_markup=reply_markup, _depth=_depth + 1)
                    return

            await self._adapter.edit_message(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)  # type: ignore[arg-type]
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
        try:
            if pre_formatted is not None:
                # Reuse the formatted text from _find_split_point (avoids re-formatting)
                formatted_frozen = pre_formatted
                parse_mode = "HTML"
            else:
                formatted_frozen, parse_mode = self._format_for_telegram(frozen_text)  # type: ignore[assignment]  # parse_mode is always str from TelegramFormatter

            if self._placeholder_deleted:
                # No placeholder to edit — send frozen text as new message
                await self._adapter.send_final_message(formatted_frozen, parse_mode=parse_mode)  # type: ignore[arg-type]
                self._placeholder_deleted = False
            else:
                await self._adapter.edit_message(formatted_frozen, parse_mode=parse_mode)  # type: ignore[arg-type]

            # BUG-1 fix: new message is a regular reply, not draft-capable.
            # Switch to classic mode to prevent deleting the continuation message.
            self._switch_to_classic()

            self._edit_count += 1
        except Exception as e:
            if "not modified" not in str(e).lower():
                logging.warning("Failed to freeze message on overflow: %s", e)

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
    placeholder_message,
    model_name: str,
    history: list,
    system_instruction: str | None = None,
    thinking_level: str | None = None,
    user_id: int | None = None,
    *,
    bot=None,
    chat_id: int = 0,
    chat_type: str = "private",
    reply_markup: Any | None = None,
    footer_text: str | None = None,
) -> tuple[str, bool, Message | None, int]:
    """High-level: stream AI response and progressively update Telegram message.

    Supports multi-message streaming: when a single message exceeds
    Telegram's ~4096 char limit, the writer seamlessly continues into
    a new message while maintaining visual streaming UX.

    In private chats, uses ``sendMessageDraft`` (Bot API 9.5) for fast,
    animated streaming with relaxed rate limits.  In groups or when the
    bot instance is unavailable, falls back to classic ``editMessageText``.

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

    Returns:
        (response_text, success, last_message, token_count) tuple.
        token_count is 0 when not available from the provider.
        last_message is the final message in the chain (may differ from
        placeholder_message if overflow occurred). Callers should use it
        for post-stream edits like adding buttons.
    """
    from app.adapters.ui_adapter import TelegramMessageAdapter

    adapter = TelegramMessageAdapter(
        message=placeholder_message,
        bot=bot,
        chat_id=chat_id,
        draft_id=random.randint(1, 2**31 - 1) if bot and chat_type == "private" else 0,
    )

    writer = StreamingWriter(
        adapter,
        chat_type=chat_type,
    )

    try:
        from app.providers import get_provider_router

        router = get_provider_router()

        async for delta in router.stream_response(
            preferred_model=model_name,
            history=history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            thinking_level=thinking_level,
            max_key_retries=3,
        ):
            await writer.write(delta)

        # Inject optional footer (e.g. memory indicator) as part of the stream
        # so the user sees it arrive smoothly — no post-hoc edit_text jump.
        if footer_text:
            await writer.write(footer_text)

        final_text = await writer.finalize(reply_markup=reply_markup)

        if not final_text.strip():
            return "", False, placeholder_message, 0

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
        return final_text, True, writer.last_message, actual_tokens

    except TimeoutError:
        partial = writer.text
        if partial:
            await writer.finalize()
            return (
                partial + "\n\n⏰ _(ответ был прерван по таймауту)_",
                True,
                writer.last_message,
                0,
            )
        return (
            "⏰ Превышено время ожидания ответа. Попробуйте позже.",
            False,
            placeholder_message,
            0,
        )

    except APIError as e:
        logging.error("Streaming API error: %s", e)
        partial = writer.text
        if partial:
            await writer.finalize()
            return (
                partial + "\n\n⚠️ _(ответ был прерван из-за ошибки API)_",
                True,
                writer.last_message,
                0,
            )
        return (
            "❌ Ошибка API при потоковой генерации. Попробуйте ещё раз.",
            False,
            placeholder_message,
            0,
        )

    except Exception as e:
        logging.error("Streaming failed: %s", e, exc_info=True)
        return (
            "❌ Ошибка при потоковой генерации. Попробуйте ещё раз.",
            False,
            placeholder_message,
            0,
        )
