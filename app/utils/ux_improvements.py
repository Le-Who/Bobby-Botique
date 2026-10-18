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

    **Atomic replacement**: Telegram's ``setMessageReaction`` replaces ALL
    existing reactions from the caller (bot) on the target message.  There is
    no need to check for or clear previous bot reactions — calling this with
    a new emoji will implicitly remove the old one (e.g. 🔍 → ⚡).
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


async def set_thinking_reaction(bot: Bot, chat_id: int, message_id: int) -> None:
    """Put 🔍 on the user's message while the bot is working."""
    await set_message_reaction(bot, chat_id, message_id, REACTION_SEARCH)


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
#
# UX: To avoid cluttering every response (already 6-8 action buttons),
# we show a single "Оценить" toggle. Tapping it reveals 👍/👎 choices.
FEEDBACK_THUMBSUP = "👍"
FEEDBACK_THUMBSDOWN = "👎"


def make_feedback_buttons() -> list:
    """Return a single-row list with a compact "📝 Оценить" toggle button.

    When tapped, ``cb_feedback.feedback_reveal_callback`` replaces this button
    with the actual 👍/👎 choice pair. This keeps the response keyboard clean.

    Returns:
        A list containing one InlineKeyboardButton (one row for InlineKeyboardMarkup).
    """
    from telegram import InlineKeyboardButton

    return [
        InlineKeyboardButton("📝 Оценить", callback_data="feedback:reveal"),
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
