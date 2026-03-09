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

from app.config import settings
from app.metrics import metrics_collector
from app.request_context import get_request_id
from app.utils.formatting import TelegramFormatter
from app.utils.text_format import sanitize_html_tags

if TYPE_CHECKING:
    from telegram import Message


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

_last_finish_reason: str | None = None
"""Side-channel: finish_reason from the last ``stream_gemini_response`` call."""

# (Stream_gemini_response removed: logic moved to providers)


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

    # --- 2. Inline code (` ... `) outside of fenced blocks --------------------
    #   Strip fenced blocks first, then count backticks.
    stripped = fence_re.sub("", text)  # rough strip — fences already balanced here
    backtick_count = stripped.count("`")
    if backtick_count % 2 == 1:
        suffix_parts.append("`")
        prefix_parts.append("`")
        # Inside inline code no other formatting applies.
        return "".join(suffix_parts), "".join(prefix_parts)

    # --- 3. Bold (**...**) ----------------------------------------------------
    bold_count = text.count("**")
    if bold_count % 2 == 1:
        suffix_parts.append("**")
        prefix_parts.append("**")

    # --- 4. Italic (*...* or _..._) -------------------------------------------
    #   After removing ** pairs, count remaining lone * characters.
    no_bold = text.replace("**", "")
    lone_star = no_bold.count("*")
    if lone_star % 2 == 1:
        suffix_parts.append("*")
        prefix_parts.append("*")

    #   Also check for _..._ italic (after removing __ pairs).
    no_double_under = text.replace("__", "")
    lone_under = no_double_under.count("_")
    if lone_under % 2 == 1:
        suffix_parts.append("_")
        prefix_parts.append("_")

    # --- 5. Strikethrough (~~...~~) -------------------------------------------
    tilde_pairs = text.count("~~")
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

        # Mode-specific debounce
        if self._use_drafts:
            self._debounce_s = DRAFT_DEBOUNCE_S
            self._min_chunk = DRAFT_MIN_CHUNK
        else:
            self._debounce_s = EDIT_DEBOUNCE_S
            self._min_chunk = MIN_CHUNK_SIZE

    async def write(self, delta: str) -> None:
        """Accumulate a text delta and flush to Telegram if debounce allows."""
        self._buffer += delta
        self._full_text += delta
        self._pending_chars += len(delta)

        now = time.monotonic()
        elapsed = now - self._last_edit_time

        if elapsed >= self._debounce_s and self._pending_chars >= self._min_chunk:
            await self._flush(final=False)

    async def finalize(self) -> str:
        """Send the final version of the message (no cursor indicator).

        In both modes the final text is committed via ``edit_text`` on the
        placeholder message so that the permanent message supports
        reply_markup (keyboards, buttons).

        Returns:
            The complete accumulated text (across all messages).
        """
        # Always finalize via classic edit_text (drafts are ephemeral)
        await self._flush(final=True)
        return self._full_text

    async def _flush(self, *, final: bool = False) -> None:
        """Send the current buffer to Telegram.

        Draft mode: calls ``send_message_draft`` for mid-stream updates,
        falls back to ``edit_text`` for the final commit.
        Classic mode: always uses ``edit_text``.
        """
        text = self._buffer
        if not text.strip():
            return

        # Draft mode mid-stream: use sendMessageDraft (no cursor needed)
        if self._use_drafts and not final:
            try:
                formatted_text, parse_mode = TelegramFormatter.format_text(text)
                if not final:
                    formatted_text = sanitize_html_tags(formatted_text)

                if len(formatted_text) > STREAM_MSG_LIMIT:
                    # Overflow: switch to classic for this + future flushes
                    await self._overflow_to_new_message()
                    return

                await self._adapter.send_draft(
                    text=formatted_text,
                    parse_mode=parse_mode,
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
                    # Disable draft mode and retry via classic
                    self._use_drafts = False
                    self._debounce_s = EDIT_DEBOUNCE_S
                    self._min_chunk = MIN_CHUNK_SIZE
                    await self._flush(final=final)
            return

        # Classic mode (or final flush in draft mode)
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

            await self._adapter.edit_message(formatted_text, parse_mode=parse_mode)
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

        # Carry open markdown formatting across the message boundary:
        # close constructs in the frozen part, reopen them in the remainder.
        md_suffix, md_prefix = _detect_open_markdown(frozen_text)
        if md_suffix:
            frozen_text += md_suffix
        if md_prefix and remainder:
            remainder = md_prefix + remainder

        # Edit current message with frozen text (no cursor)
        try:
            formatted_frozen, parse_mode = TelegramFormatter.format_text(frozen_text)
            formatted_frozen = sanitize_html_tags(formatted_frozen)
            await self._adapter.edit_message(formatted_frozen, parse_mode=parse_mode)
            self._edit_count += 1
        except Exception as e:
            if "not modified" not in str(e).lower():
                logging.warning("Failed to freeze message on overflow: %s", e)

        # 2. Create new message with cursor indicator
        try:
            initial_text = remainder + STREAMING_INDICATOR if remainder.strip() else STREAMING_INDICATOR
            formatted_initial, parse_mode = TelegramFormatter.format_text(initial_text)
            self._adapter = await self._adapter.reply_new_message(
                formatted_initial,
                parse_mode=parse_mode,
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
                candidate = text[: idx + len(sep)]
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
) -> tuple[str, bool, Message | None]:
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

    Returns:
        (response_text, success, last_message) tuple.
        last_message is the final message in the chain (may differ from
        placeholder_message if overflow occurred). Callers should use it
        for post-stream edits like adding buttons.
    """
    from app.adapters.ui_adapter import TelegramMessageAdapter
    
    adapter = TelegramMessageAdapter(
        message=placeholder_message,
        bot=bot,
        chat_id=chat_id,
        draft_id=random.randint(1, 2**31 - 1) if bot and chat_type == "private" else 0
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

        final_text = await writer.finalize()

        if not final_text.strip():
            return "", False, placeholder_message

        # Check finish_reason for blocked/truncated responses
        fr = _last_finish_reason
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

        elif len(final_text) < 150 and fr_upper not in ("STOP", "1", "FINISH_REASON_STOP", ""):
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
