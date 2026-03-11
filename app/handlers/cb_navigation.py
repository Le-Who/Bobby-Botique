"""
Callback handlers — navigation and menus.

Handles new_topic, new_chat, model_menu, deep_dive, help,
help_topic, open_documents, open_conversations, toggle_search callbacks.
"""

__all__ = [
    "deep_dive_callback",
    "help_callback",
    "help_topic_callback",
    "model_menu_callback",
    "new_chat_callback",
    "new_topic_callback",
    "open_conversations_callback",
    "open_documents_callback",
    "toggle_search_callback",
]

import contextlib

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers import menus
from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.formatting import TelegramFormatter


async def deep_dive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles callbacks from deep dive mode buttons."""
    query = update.callback_query
    await query.answer()

    action = query.data.split(":")[1]
    user_id = query.from_user.id

    if action == "new_topic" and _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    if action == "new_topic":
        chat_state = await get_user_chat(user_id)
        chat_state.history = []
        chat_state.token_count = 0
        chat_state.system_prompt = None
        chat_state.context_summary = None
        chat_state.is_deep_dive = False
        await update_user_chat(user_id, chat_state)
        await query.message.reply_text("✅ Новый чат создан. История и системная инструкция сброшены.")
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "exit_search":
        chat_state = await get_user_chat(user_id)
        chat_state.is_deep_dive = False
        await update_user_chat(user_id, chat_state)
        await query.answer("💬 Режим исследования завершён")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("💬 Вы вернулись в обычный чат. История сохранена — можете продолжить общение!")

    elif action == "deeper_dive":
        await query.edit_message_reply_markup(reply_markup=None)
        text = "Супер! Мы готовы *копнуть глубже*! 😉 \nЧто еще вы хотели бы узнать по этой теме?"
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.message.reply_text(formatted_text, parse_mode=parse_mode)


async def new_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'new_topic' button press, clearing the chat context."""
    query = update.callback_query
    await query.answer("Начинаем новую тему...")

    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    # Clear chat history and system prompt, similar to /newchat command
    chat_state = await get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    chat_state.context_summary = None
    await update_user_chat(user_id, chat_state)

    # Remove the old inline keyboard
    await query.edit_message_reply_markup(reply_markup=None)

    # Send confirmation message
    await query.message.reply_text("✅ Новый чат создан. История и системная инструкция сброшены.")


async def new_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline new chat — edits current message, no flooding."""
    query = update.callback_query
    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    chat_state = await get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    chat_state.context_summary = None
    await update_user_chat(user_id, chat_state)

    text = "✨ **Новый чат начат!**\n\nКонтекст и роль сброшены. Напишите что-нибудь. 👇"
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    keyboard = [
        [
            InlineKeyboardButton("🎭 Начать с роли", callback_data="open_roles"),
            InlineKeyboardButton("🧠 Сменить модель", callback_data="model_menu"),
        ]
    ]
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    await query.answer("✨ Чат очищен!")


async def model_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline model menu — edits current message, no flooding."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_state = await get_user_chat(user_id)
    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, context)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Categorized help with sub-topic buttons — no flooding."""
    query = update.callback_query
    await query.answer()

    help_text = (
        "📚 **Справка**\n\n"
        "💬 **Чат** — просто напишите сообщение\n"
        "🌐 **Поиск** — `?` или `??` перед вопросом\n"
        "📄 **Документы** — отправьте PDF/DOCX\n"
        "🎭 **Роли** — специализация бота\n\n"
        "Нажмите кнопку для подробностей:"
    )
    formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
    keyboard = [
        [
            InlineKeyboardButton("💬 Чат", callback_data="help_topic:chat"),
            InlineKeyboardButton("🌐 Поиск", callback_data="help_topic:search"),
        ],
        [
            InlineKeyboardButton("📄 Документы", callback_data="help_topic:docs"),
            InlineKeyboardButton("🎭 Роли", callback_data="help_topic:roles"),
        ],
        [InlineKeyboardButton("⬅️ Меню", callback_data="start_menu")],
    ]
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ── Help sub-topic handlers ──────────────────────────────────────────────────

_HELP_TOPICS = {
    "chat": (
        "💬 **Как общаться**\n\n"
        "Просто напишите сообщение в чат — бот ответит "
        "с помощью AI.\n\n"
        "• Отправьте 🖼️ фото — бот проанализирует изображение\n"
        "• `/newchat` — начать новый диалог\n"
        "• `/setprompt` — задать системную инструкцию\n"
        "• `/save` — сохранить текущую беседу"
    ),
    "search": (
        "🌐 **Поиск в интернете**\n\n"
        "• `? вопрос` — быстрый фактический ответ\n"
        "• `?? вопрос` — глубокое исследование с источниками\n"
        "• `??` + фото — поиск по изображению\n\n"
        "💡 `/res` — включить/выключить поиск для всех сообщений"
    ),
    "docs": (
        "📄 **Работа с документами**\n\n"
        "Отправьте PDF или DOCX файл в чат — "
        "бот извлечёт текст и будет отвечать "
        "на основе содержимого.\n\n"
        "• Максимум: 5 документов\n"
        "• Хранение: 3 дня\n"
        "• `/documents` — управление документами"
    ),
    "roles": (
        "🎭 **Роли**\n\n"
        "Роль — это специализация бота: он будет "
        "отвечать как эксперт в выбранной области.\n\n"
        "• 6 готовых ролей: преподаватель, IT-инженер, доктор…\n"
        "• ✨ Сгенерировать роль по описанию\n"
        "• 📝 Написать свою вручную\n"
        "• `/roles` — открыть меню ролей"
    ),
}


async def help_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows a specific help topic with back-to-help button."""
    query = update.callback_query
    await query.answer()
    topic = query.data.split(":", 1)[1]
    text = _HELP_TOPICS.get(topic, "❓ Тема не найдена.")
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    keyboard = [
        [InlineKeyboardButton("⬅️ К справке", callback_data="help")],
    ]
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def open_documents_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opens documents menu from start menu button."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    formatted_text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)


async def open_conversations_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Opens conversations menu from start menu button."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def toggle_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    chat_state = await get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await update_user_chat(user_id, chat_state)

    formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(chat_state)

    await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)

    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await query.answer(f"Поиск {status_text}")
