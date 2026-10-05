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
    from telegram.ext import Application

_bot: Bot | None = None
_application: Application | None = None


def register_bot(bot: Bot, *, application: Application | None = None) -> None:
    """Called once from bot.py after PTB application is initialized."""
    global _bot, _application
    _bot = bot
    _application = application


def get_bot() -> Bot | None:
    """Return the running PTB Bot, or None if not yet initialized."""
    return _bot


def get_application() -> Application | None:
    """Return the owner of private conversation data for Mini App delivery."""
    return _application
