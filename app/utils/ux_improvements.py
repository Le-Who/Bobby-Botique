"""
UX Improvements — shared helpers for modern Telegram Bot API UX patterns.

Implements:
  1. Message Reactions as status indicators (setMessageReaction)
     - set_thinking_reaction()  → 🔍 while searching
     - set_done_reaction()      → ⚡ on success
     - set_error_reaction()     → ⚠️ on failure

  2. Message Effects — celebratory effect on notable responses
     - EFFECT_ID_FIRE / EFFECT_ID_CONFETTI etc.

  3. Expandable Blockquote formatting
     - wrap_in_expandable_blockquote()  — collapses long content

  4. HTML-safe  <tg-time> formatting for digests
     - tg_time_tag() — localized timestamps in Telegram clients

All functions are fire-and-forget safe: errors are swallowed and logged
at DEBUG level so they NEVER block the primary response flow.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot, InlineKeyboardButton

logger = logging.getLogger(__name__)

# ── Message Effect IDs ────────────────────────────────────────────────────────
# Telegram-defined static effect IDs (as of Bot API 7.2).
# Source: https://core.telegram.org/bots/api#sendmessage  (message_effect_id)
EFFECT_FIRE = "5104841245755180586"  # 🔥
EFFECT_CONFETTI = "5046509860389126442"  # 🎉
EFFECT_HEART = "5159385139981059251"  # ❤️
EFFECT_THUMBSUP = "5107584321108051014"  # 👍

# ── Reaction Emoji Constants ──────────────────────────────────────────────────
REACTION_SEARCH = "🔍"  # searching / thinking
REACTION_BRAIN = "🧠"  # synthesizing
REACTION_LIGHTNING = "⚡"  # done fast
REACTION_WARNING = "⚠️"  # error / interruption
REACTION_EYES = "👀"  # reading / processing


# ── Reaction Helpers ──────────────────────────────────────────────────────────


async def set_message_reaction(
    bot: Bot,
    chat_id: int,
    message_id: int,
    emoji: str,
    *,
    is_big: bool = False,
) -> None:
    """Set a single emoji reaction on a message (fire-and-forget, never raises).

    Uses ReactionTypeEmoji — available on Bot API 7.0+ (PTB 20.7+).
    Silently no-ops if the API call fails (e.g. bot lacks permissions).
    """
    try:
        from telegram import ReactionTypeEmoji

        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
            is_big=is_big,
        )
    except Exception as exc:
        logger.debug("set_message_reaction(%s) failed (non-critical): %s", emoji, exc)


async def clear_message_reaction(
    bot: Bot,
    chat_id: int,
    message_id: int,
) -> None:
    """Remove all reactions from a message (pass empty list)."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=[],
        )
    except Exception as exc:
        logger.debug("clear_message_reaction failed (non-critical): %s", exc)


async def set_thinking_reaction(bot: Bot, chat_id: int, message_id: int) -> None:
    """Put 🔍 on the user's message while the bot is working."""
    await set_message_reaction(bot, chat_id, message_id, REACTION_SEARCH)


async def set_synthesizing_reaction(bot: Bot, chat_id: int, message_id: int) -> None:
    """Put 🧠 on the user's message while the LLM is synthesizing."""
    await set_message_reaction(bot, chat_id, message_id, REACTION_BRAIN)


async def set_done_reaction(bot: Bot, chat_id: int, message_id: int) -> None:
    """Put ⚡ on the user's message after a successful response."""
    await set_message_reaction(bot, chat_id, message_id, REACTION_LIGHTNING)


async def set_error_reaction(bot: Bot, chat_id: int, message_id: int) -> None:
    """Put ⚠️ on the user's message when the response was interrupted/failed."""
    await set_message_reaction(bot, chat_id, message_id, REACTION_WARNING)


# ── Expandable Blockquote Formatting ─────────────────────────────────────────

# Telegram HTML expandable blockquote (Bot API 7.0+)
# Syntax:  <blockquote expandable>...</blockquote>
# The block is collapsed by default and expanded on tap.
_EXPANDABLE_OPEN = "<blockquote expandable>"
_EXPANDABLE_CLOSE = "</blockquote>"

# Threshold: responses longer than this (in chars) get wrapped.
LONG_RESPONSE_THRESHOLD = 1800


def wrap_in_expandable_blockquote(content: str, *, label: str | None = None) -> str:
    """Wrap *content* in a Telegram HTML expandable blockquote.

    Args:
        content: Plain or HTML-formatted inner text. Must be pre-escaped for HTML
                 if it contains special characters.
        label: Optional bold label shown before the collapsed block, e.g.
               "[Частичный ответ]". Will be rendered as a separate line.

    Returns:
        HTML string with the expandable blockquote.
    """
    body = content.strip()
    result = ""
    if label:
        result += f"<b>{html.escape(label)}</b>\n"
    result += f"{_EXPANDABLE_OPEN}{body}{_EXPANDABLE_CLOSE}"
    return result


def maybe_wrap_long_response(
    html_text: str,
    *,
    threshold: int = LONG_RESPONSE_THRESHOLD,
    summary_chars: int = 600,
) -> str:
    """If *html_text* exceeds *threshold* chars, split it into a visible summary
    + an expandable blockquote containing the rest.

    The visible part ends at the last paragraph break before *summary_chars*.
    The remainder is placed in an expandable blockquote.

    If text is short enough, returns it unchanged.
    """
    if len(html_text) <= threshold:
        return html_text

    # Find a natural split point near summary_chars
    split_at = summary_chars
    for sep in ["\n\n", "\n", ". "]:
        idx = html_text.rfind(sep, 0, summary_chars)
        if idx > 0:
            split_at = idx + len(sep)
            break

    visible = html_text[:split_at].rstrip()
    rest = html_text[split_at:].lstrip()

    if not rest:
        return html_text  # Nothing to collapse

    collapsed = wrap_in_expandable_blockquote(rest)
    return f"{visible}\n\n{collapsed}"


def wrap_partial_response(html_text: str) -> str:
    """Wrap an interrupted/partial response in an expandable blockquote
    with a localized '[Частичный ответ]' label.
    """
    return wrap_in_expandable_blockquote(html_text, label="Частичный ответ / Сбой сети")


# ── tg-time HTML tag ──────────────────────────────────────────────────────────


def tg_time_tag(unix_timestamp: int, *, fmt: str = "wDT") -> str:
    """Render a <tg-time> HTML tag for Telegram's localized time display.

    The tag auto-localizes to the user's Telegram client timezone.

    Args:
        unix_timestamp: POSIX timestamp (seconds since epoch).
        fmt: Telegram time format string:
            "t"  — time only (22:45)
            "d"  — date only (Mar 20)
            "D"  — long date (March 20, 2026)
            "f"  — date + time (March 20, 2026 22:45)
            "F"  — long date + time (Thursday, March 20, 2026 22:45)
            "r"  — relative (in 2 hours)
            "R"  — relative short (2h)
            "wDT"— weekday + date + time (Thu, Mar 20, 22:45)  ← default

    Returns:
        HTML string like: <tg-time unix="1742500000" format="wDT">...</tg-time>

    Note: The inner text is a fallback for unsupported clients; we use an ISO
    representation as a reasonable default.
    """
    from datetime import UTC, datetime

    dt = datetime.fromtimestamp(unix_timestamp, tz=UTC)
    fallback = dt.strftime("%a, %d %b %Y %H:%M UTC")
    return f'<tg-time unix="{unix_timestamp}" format="{fmt}">{html.escape(fallback)}</tg-time>'


# ── RLHF Feedback Buttons (Inline Keyboard) ──────────────────────────────────

# Telegram Bot API limits non-premium bots to 1 reaction per message.
# Pre-placing two reactions (👍+👎) via setMessageReaction is therefore BROKEN.
# Instead, we use inline keyboard buttons — the industry-standard pattern.
FEEDBACK_THUMBSUP = "👍"
FEEDBACK_THUMBSDOWN = "👎"


def make_feedback_buttons() -> list:
    """Return a single-row list of 👍/👎 InlineKeyboardButtons for RLHF feedback.

    Append this row to any inline keyboard on the bot's AI response messages.
    Handled by ``cb_feedback.feedback_callback`` via ``feedback:up`` / ``feedback:down``
    callback data patterns.

    Returns:
        A list containing two InlineKeyboardButtons (one row for InlineKeyboardMarkup).
    """
    from telegram import InlineKeyboardButton

    return [
        InlineKeyboardButton(FEEDBACK_THUMBSUP, callback_data="feedback:up"),
        InlineKeyboardButton(FEEDBACK_THUMBSDOWN, callback_data="feedback:down"),
    ]


# ── CopyTextButton helper ─────────────────────────────────────────────────────


def make_copy_text_button(text: str, button_label: str = "📋 Скопировать") -> InlineKeyboardButton | None:
    """Create a Telegram InlineKeyboardButton that copies *text* to clipboard.

    Uses Bot API CopyTextButton  (Bot API 7.4+, PTB 21.3+).

    If CopyTextButton is unavailable (older PTB), returns None so callers can
    skip it gracefully.

    Args:
        text: Text to copy (1-256 chars per API spec; will be truncated).
        button_label: Label shown on the button. Defaults to clipboard emoji.

    Returns:
        InlineKeyboardButton or None.
    """
    try:
        from telegram import CopyTextButton, InlineKeyboardButton

        copy_payload = text[:256]  # API limit
        return InlineKeyboardButton(
            text=button_label,
            copy_text=CopyTextButton(text=copy_payload),
        )
    except ImportError:
        logger.debug("CopyTextButton not available in this PTB version — skipping")
        return None
