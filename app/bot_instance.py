# /app/bot_instance.py
"""Thin singleton for the PTB Bot instance.

bot.py calls ``register_bot(application.bot)`` after PTB initializes.
Non-PTB code (e.g. the Crocodile WebSocket handler) retrieves it via
``get_bot()``.  Returns None until the bot is registered (startup race).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Bot

_bot: Bot | None = None


def register_bot(bot: Bot) -> None:
    """Called once from bot.py after PTB application is initialized."""
    global _bot
    _bot = bot


def get_bot() -> Bot | None:
    """Return the running PTB Bot, or None if not yet initialized."""
    return _bot
