"""Standardized factories for generating test objects across the test suite."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

try:
    import telegram

    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False


def make_chat_state(
    history=None,
    model="gemini-2.0-flash",
    system_prompt=None,
    token_count=0,
    is_deep_dive=False,
    search_enabled=False,
    context_summary=None,
    thinking_level=None,
):
    """Create a minimal ChatState-like object."""
    return SimpleNamespace(
        history=history if history is not None else [],
        model=model,
        system_prompt=system_prompt,
        token_count=token_count,
        is_deep_dive=is_deep_dive,
        search_enabled=search_enabled,
        context_summary=context_summary,
        thinking_level=thinking_level,
    )


def make_telegram_user(user_id=123, username="test_user"):
    """Create a mock telegram.User."""
    user = MagicMock(spec=telegram.User if HAS_TELEGRAM else object)
    user.id = user_id
    user.username = username
    return user


def make_telegram_chat(chat_id=456, chat_type="private"):
    """Create a mock telegram.Chat."""
    chat = MagicMock(spec=telegram.Chat if HAS_TELEGRAM else object)
    chat.id = chat_id
    chat.type = chat_type
    return chat


def make_telegram_message(text="Hello", user_id=123, chat_id=456, chat_type="private"):
    """Create a mock telegram.Message."""
    msg = MagicMock(spec=telegram.Message if HAS_TELEGRAM else object)
    msg.text = text
    msg.chat = make_telegram_chat(chat_id=chat_id, chat_type=chat_type)
    msg.from_user = make_telegram_user(user_id=user_id)
    msg.id = 999
    msg.message_id = 999

    # Defaults for media
    msg.photo = ()
    msg.document = None
    msg.voice = None
    msg.video = None

    # Async methods
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()

    return msg


def make_telegram_update(message_text="Hello", user_id=123, chat_id=456):
    """Create a fully mocked telegram.Update containing a Message."""
    update = MagicMock(spec=telegram.Update if HAS_TELEGRAM else object)
    update.message = make_telegram_message(text=message_text, user_id=user_id, chat_id=chat_id)
    update.effective_user = update.message.from_user
    update.effective_chat = update.message.chat
    return update


def make_telegram_context():
    """Create a mock telegram.ext.CallbackContext."""
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.user_data = {}
    context.chat_data = {}
    return context
