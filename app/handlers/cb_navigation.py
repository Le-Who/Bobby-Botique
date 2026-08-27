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
    "settings_thinking_callback",
    "toggle_ltm_callback",
]

import contextlib

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot_commands import (
    build_help_topic_rows,
    language_from_telegram,
    render_help_overview,
    render_help_topic,
)
from app.handlers import menus
from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.i18n import t
from app.repos.chats import get_user_chat, set_ltm_enabled, update_user_chat
from app.utils.formatting import TelegramFormatter


def _lang(update: Update) -> str:
    """Detect UI language from Telegram language_code with ru fallback."""
    return language_from_telegram(getattr(update.effective_user, "language_code", None))


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
        await query.message.reply_text(t("chat.new_topic", _lang(update)))
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "exit_search":
        chat_state = await get_user_chat(user_id)
        chat_state.is_deep_dive = False
        await update_user_chat(user_id, chat_state)
        await query.answer(t("chat.research_ended", _lang(update)))
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(t("chat.returned_to_chat", _lang(update)))

    elif action == "deeper_dive":
        await query.edit_message_reply_markup(reply_markup=None)
        text = t("chat.deeper_dive", _lang(update))
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.message.reply_text(formatted_text, parse_mode=parse_mode)


async def new_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'new_topic' button press, clearing the chat context."""
    query = update.callback_query
    lang = _lang(update)
    await query.answer("...")

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
    chat_state.is_deep_dive = False
    chat_state.deep_dive_thread_id = None
    await update_user_chat(user_id, chat_state)

    # Remove the old inline keyboard
    await query.edit_message_reply_markup(reply_markup=None)

    # Send confirmation message
    await query.message.reply_text(t("chat.new_topic", lang))


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
    chat_state.is_deep_dive = False
    chat_state.deep_dive_thread_id = None
    await update_user_chat(user_id, chat_state)

    lang = _lang(update)
    text = t("chat.new_started", lang)
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    keyboard = [
        [
            InlineKeyboardButton(t("chat.start_with_role", lang), callback_data="open_roles"),
            InlineKeyboardButton(t("chat.change_model", lang), callback_data="model_menu"),
        ]
    ]
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    await query.answer(t("chat.new_cleared_toast", lang))


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

    lang = _lang(update)
    keyboard = build_help_topic_rows(lang)
    keyboard.append([InlineKeyboardButton(t("menu.back_to_menu", lang), callback_data="start_menu")])
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            render_help_overview(lang),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ── Help sub-topic handlers ──────────────────────────────────────────────────

async def help_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows a specific help topic with back-to-help button."""
    query = update.callback_query
    await query.answer()
    lang = _lang(update)
    topic = str(query.data or "").partition(":")[2]
    keyboard = [
        [InlineKeyboardButton(t("help.back_to_help", lang), callback_data="help")],
    ]
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            render_help_topic(topic, lang),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
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

    status_key = "search.on" if chat_state.search_enabled else "search.off"
    await query.answer(f"{t('menu.search_toggle', _lang(update))} {t(status_key, _lang(update))}")


async def settings_thinking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    chat_state = await get_user_chat(user_id)
    cycle = [None, "off", "low", "medium", "high"]
    try:
        idx = cycle.index(chat_state.thinking_level)
    except ValueError:
        idx = 0
    next_level = cycle[(idx + 1) % len(cycle)]

    from app.repos.chats import update_thinking_level

    await update_thinking_level(user_id, next_level)
    chat_state.thinking_level = next_level

    # Rebuild settings menu
    from app.handlers.commands import _THINKING_LABELS

    lang = _lang(update)
    formatted_text, parse_mode, keyboard = _build_settings_view(chat_state, lang)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
        )
    thinking_str = _THINKING_LABELS.get(next_level, next_level or "🔄 Auto")
    await query.answer(f"{t('settings.thinking', lang)} {thinking_str}")


async def toggle_ltm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle long-term memory on/off from /settings inline keyboard."""
    query = update.callback_query
    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    chat_state = await get_user_chat(user_id)
    chat_state.ltm_enabled = not chat_state.ltm_enabled
    chat_state.memory_epoch = await set_ltm_enabled(user_id, chat_state.ltm_enabled)
    if not chat_state.ltm_enabled:
        from app.repos.memory_autosave import cancel_user_memory_tasks

        await cancel_user_memory_tasks(user_id)

    # Rebuild settings menu
    lang = _lang(update)
    formatted_text, parse_mode, keyboard = _build_settings_view(chat_state, lang)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
        )
    ltm_key = "settings.enabled" if chat_state.ltm_enabled else "settings.disabled"
    await query.answer(f"{t('settings.ltm_label', lang)} {t(ltm_key, lang)}")


def _build_settings_view(chat_state, lang: str):
    """Build settings menu text and keyboard (shared by thinking + ltm toggles)."""
    from app.handlers.commands import _THINKING_LABELS

    model_name = chat_state.model or t("settings.default_model", lang)
    thinking_str = _THINKING_LABELS.get(chat_state.thinking_level, chat_state.thinking_level or "🔄 Auto")
    search_str = (
        t("settings.search_enabled", lang) if chat_state.search_enabled else t("settings.search_disabled", lang)
    )

    role = chat_state.system_prompt
    if role and len(role) > 60:
        role = role[:60] + "…"
    elif not role:
        role = t("settings.default_role", lang)

    ltm_str = t("settings.enabled", lang) if chat_state.ltm_enabled else t("settings.disabled", lang)

    text = (
        f"{t('settings.title', lang)}\n\n"
        f"{t('settings.model', lang)} `{model_name}`\n"
        f"{t('settings.thinking', lang)} {thinking_str}\n"
        f"{t('settings.search', lang)} {search_str}\n"
        f"{t('settings.role_label', lang)} {role}\n"
        f"{t('settings.ltm_label', lang)} {ltm_str}\n"
    )

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    ltm_btn_label = (
        f"📚 {'On' if chat_state.ltm_enabled else 'Off'}"
        if lang == "en"
        else f"📚 {'Вкл' if chat_state.ltm_enabled else 'Выкл'}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t("settings.btn_change_model", lang), callback_data="model_menu"),
                InlineKeyboardButton(t("settings.btn_thinking", lang), callback_data="settings_thinking"),
            ],
            [
                InlineKeyboardButton(t("settings.btn_search", lang), callback_data="toggle_search"),
                InlineKeyboardButton(t("settings.btn_roles", lang), callback_data="open_roles"),
            ],
            [
                InlineKeyboardButton(ltm_btn_label, callback_data="toggle_ltm"),
            ],
        ]
    )
    return formatted_text, parse_mode, keyboard
