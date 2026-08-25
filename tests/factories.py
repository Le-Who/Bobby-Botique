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
    model="gemini-3.1-flash-lite",
    system_prompt=None,
    token_count=0,
    is_deep_dive=False,
    search_enabled=False,
    context_summary=None,
    thinking_level=None,
    ltm_enabled=True,
    memory_epoch=0,
    private_data_blocked=False,
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
        ltm_enabled=ltm_enabled,
        memory_epoch=memory_epoch,
        private_data_blocked=private_data_blocked,
        branch_id=None,
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
    update.effective_message = update.message
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


def make_crocodile_game(
    game_id: str = "test-game-1234",
    target_word: str = "крокодил",
    category: str = "Животные",
    lang: str = "ru",
    inline_message_id: str = "inl_test_msg",
    creator_id: int = 42,
    guesser_id: int | None = None,
    status: str = "active",
    attempts: list | None = None,
    max_attempts: int = 10,
):
    """Synchronous factory for CrocodileGame — no I/O, no Redis, no LLM."""
    from app.games.crocodile import CrocodileGame

    return CrocodileGame(
        game_id=game_id,
        target_word=target_word,
        category=category,
        lang=lang,
        inline_message_id=inline_message_id,
        creator_id=creator_id,
        guesser_id=guesser_id,
        status=status,  # type: ignore[arg-type]
        attempts=attempts if attempts is not None else [],
        max_attempts=max_attempts,
    )


def make_valid_init_data(
    bot_token: str,
    user_id: int = 999,
    username: str = "testuser",
    *,
    auth_date: int | None = None,
) -> str:
    """Generate a valid HMAC-SHA256 signed Telegram initData string.

    Implements the same algorithm as _validate_init_data so tests can produce
    real tokens without calling the Telegram API.
    """
    import hashlib
    import hmac
    import json
    import time
    import urllib.parse

    user_payload = json.dumps({"id": user_id, "username": username}, separators=(",", ":"))
    params = {
        "user": user_payload,
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    params["hash"] = computed_hash
    return urllib.parse.urlencode(params)
