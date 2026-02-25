"""
Callback handlers — central facade and registration hub.

This module keeps core/navigation callbacks and the ``register()`` function.
Domain-specific callbacks are imported from sub-modules:
    cb_roles          — role management (apply, create, delete, rename, etc.)
    cb_documents      — document management (upload, select, delete, etc.)
    cb_conversations  — conversation management (switch, rename, delete, etc.)
"""

import asyncio
import logging
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from app.handlers import agent
from app import database as db
from app.config import settings, get_model_hash, get_openrouter_keys
from app import state
from app.utils.formatting import TelegramFormatter
from app.request_context import set_request_id
from app.handlers import menus

# ── Concurrency limiter for heavy callback branches ──────────────────────────
_HEAVY_CALLBACK_LIMIT = max(
    1, int(getattr(settings, "MAX_CONCURRENT_HEAVY_CALLBACKS", 4))
)
_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore(_HEAVY_CALLBACK_LIMIT)


# ── Re-exports from domain modules ──────────────────────────────────────────
from app.handlers.cb_roles import (  # noqa: F401
    DummyUpdate,
    role_rename_menu_callback,
    role_rename_pick_callback,
    start_menu_callback,
    role_apply_callback,
    role_clear_callback,
    role_create_callback,
    role_create_cancel_callback,
    role_rename_cancel_callback,
    role_custom_apply_callback,
    role_custom_save_callback,
    role_custom_retry_callback,
    role_delete_ask_callback,
    role_delete_cancel_callback,
    role_delete_confirm_callback,
    role_detail_callback,
    role_view_prompt_callback,
    role_nav_callback,
    role_page_callback,
    open_roles_callback,
)

from app.handlers.cb_documents import (  # noqa: F401
    document_callback,
)

from app.handlers.cb_conversations import (  # noqa: F401
    send_conversation_selection,
    conv_page_callback,
    conv_switch_callback,
    conv_switch_to_callback,
    conv_rename_callback,
    conv_delete_callback,
    conv_delete_ask_callback,
    conv_rename_ask_callback,
    conv_rename_cancel_callback,
    conv_delete_confirm_callback,
    conv_delete_cancel_callback,
    refresh_metrics_callback,
)


# ── Core / Navigation callbacks (stay here — thin and tightly coupled) ───────

async def model_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Игнорируем клики на разделитель
    if query.data == "model_none":
        return

    user_id = query.from_user.id

    # Get model from индекса (new формат с хэшем) or from полного имени (old формат for совместимости)
    if query.data.startswith("model:"):
        # Новый формат: model:index:hash or model:index (old формат without хэша)
        try:
            parts = query.data.split(":")
            model_index = int(parts[1])
            expected_hash = parts[2] if len(parts) > 2 else None

            # Get актуальный list моделей from настроек
            all_models = []
            if settings.AVAILABLE_MODELS:
                all_models.extend(settings.AVAILABLE_MODELS)
            openrouter_available = bool(get_openrouter_keys())
            if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
                all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)

            if 0 <= model_index < len(all_models):
                model_name = all_models[model_index]

                # If есть хэш, проверяем валидность
                if expected_hash:
                    actual_hash = get_model_hash(model_name)
                    if actual_hash != expected_hash:
                        # Модель fromменилась (удалена/добавлена), просим выбрать заново
                        await query.edit_message_text(
                            "⚠️ Список моделей обновился. Пожалуйста, выберите модель заново через /model"
                        )
                        return
            else:
                await query.edit_message_text("❌ Ошибка: неверный индекс модели.")
                return
        except (ValueError, IndexError) as e:
            await query.edit_message_text("❌ Ошибка: неверный формат callback_data.")
            logging.error("Error parsing model callback: %s, data: %s", e, query.data)
            return
    else:
        # Старый формат for совместимости: model_gemini-2.5-pro
        model_name = query.data.split("_", 1)[1] if "_" in query.data else None
        if not model_name:
            await query.edit_message_text("❌ Ошибка: неверный формат callback_data.")
            return
    chat_state = await db.get_user_chat(user_id)
    chat_state.model = model_name
    await db.update_user_chat(user_id, chat_state)

    # Update menu с новой выбранной modelю
    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(
        chat_state, context
    )

    # Определяем имя for тоста
    is_openrouter = "/" in model_name
    display_name = model_name.split("/")[-1] if is_openrouter else model_name

    try:
        await query.edit_message_text(
            formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e
    await query.answer(f"✅ Модель изменена на {display_name}")


async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    await query.answer()

    action = query.data.split(":")[1]
    placeholder_message = query.message

    if action == "cancel":
        await placeholder_message.delete()
        return

    # Get оригинальное message from contextа or from reply_to_message
    original_message = None
    if hasattr(context, "user_data") and "original_message" in context.user_data:
        original_message = context.user_data["original_message"]
    else:
        original_message = query.message.reply_to_message

    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return

    user_id = original_message.from_user.id
    user_lock = state.get_user_lock(user_id)

    if user_lock.locked():
        return

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # 1. Определяем, какую задачу будем запускать.
    task_to_run = None
    if action == "vision_only":
        # 2. СРАЗУ даем обратную связь пользователю.
        await placeholder_message.edit_text("🖼️ Описываю изображение...")
        chat_state = await db.get_user_chat(user_id)
        task_to_run = agent._handle_photo(
            placeholder_message, original_message, chat_state
        )
    elif action == "confirm":
        # У этой функции своя обратная связь ("Аналfromирую..."), поэтому здесь ничего не меняем.
        search_prefix = (
            "??"
            if (original_message.caption and original_message.caption.startswith("??"))
            else "?"
        )
        task_to_run = agent._handle_complex_agent_search(
            placeholder_message, original_message, search_prefix
        )

    # 3. If задача определена, запускаем ее в фоне под блокировкой.
    if task_to_run:

        async def task_wrapper():
            async with _HEAVY_CALLBACK_SEMAPHORE:
                async with user_lock:
                    await task_to_run

        asyncio.create_task(task_wrapper())


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    await query.answer()

    _, action, model_override = query.data.split(":")
    placeholder_message = query.message

    if action == "cancel":
        await placeholder_message.edit_text("Операция отменена.")
        return

    # Get оригинальное message from contextа or from reply_to_message
    original_message = None
    if hasattr(context, "user_data") and "original_message" in context.user_data:
        original_message = context.user_data["original_message"]
    else:
        original_message = query.message.reply_to_message

    if not original_message:
        await placeholder_message.edit_text("Не удалось найти оригинальное сообщение.")
        return

    user_id = original_message.from_user.id
    user_lock = state.get_user_lock(user_id)

    if user_lock.locked():
        return

    async def task_wrapper():
        async with _HEAVY_CALLBACK_SEMAPHORE:
            async with user_lock:
                if action == "confirm":
                    chat_state = await db.get_user_chat(user_id)
                    user_message = original_message.text
                    await agent._handle_regular_chat(
                        placeholder_message,
                        user_id,
                        user_message,
                        chat_state,
                        model_override=model_override,
                    )

    asyncio.create_task(task_wrapper())


async def deep_dive_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles callbacks from deep dive mode buttons."""
    query = update.callback_query
    await query.answer()

    action = query.data.split(":")[1]
    user_id = query.from_user.id

    if action == "new_topic":
        chat_state = await db.get_user_chat(user_id)
        chat_state.history = []
        chat_state.token_count = 0
        chat_state.system_prompt = None
        chat_state.is_deep_dive = False
        await db.update_user_chat(user_id, chat_state)
        await query.message.reply_text(
            "✅ Новый чат создан. История и системная инструкция сброшены."
        )
        await query.edit_message_reply_markup(reply_markup=None)

    elif action == "deeper_dive":
        await query.edit_message_reply_markup(reply_markup=None)
        text = "Супер! Мы готовы *копнуть глубже*! 😉 \nЧто еще вы хотели бы узнать по этой теме?"
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.message.reply_text(formatted_text, parse_mode=parse_mode)


async def new_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'new_topic' button press, clearing the chat context."""
    query = update.callback_query
    await query.answer("Начинаем новую тему...")

    user_id = query.from_user.id

    # Clear chat history and system prompt, similar to /newchat command
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)

    # Remove the old inline keyboard
    await query.edit_message_reply_markup(reply_markup=None)

    # Send confirmation message
    await query.message.reply_text(
        "✅ Новый чат создан. История и системная инструкция сброшены."
    )


async def retry_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повтор последнего пользовательского запроса по кнопке."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Hydrate persisted state from DB
    from app.state import ensure_state_loaded, get_last_sent_message
    await ensure_state_loaded(user_id)

    chat_state = await db.get_user_chat(user_id)
    last_text = None
    try:
        last_text = get_last_sent_message(user_id)
    except Exception:
        last_text = None
    if not last_text:
        await query.edit_message_text("Нет запроса для повтора.")
        return
    # Create плейсхолдер и запускаем обычную обработку как on новом сообщении
    placeholder_message = await query.message.reply_text(
        "🔁 Повторяю предыдущий запрос…"
    )
    from app.handlers.agent import _handle_regular_chat

    await _handle_regular_chat(placeholder_message, user_id, last_text, chat_state)


async def new_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import new_chat_command

    await new_chat_command(DummyUpdate(query.message, query.from_user), context)


async def model_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import model_command

    await model_command(DummyUpdate(query.message, query.from_user), context)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    from app.handlers.commands import help_command

    await help_command(DummyUpdate(query.message, query.from_user), context)


async def toggle_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)

    formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(chat_state)

    await query.edit_message_text(
        formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
    )

    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await query.answer(f"Поиск {status_text}")


# ── Feedback callbacks ───────────────────────────────────────────────────────

async def _noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-op callback for decorative buttons (e.g. confirmed feedback indicator)."""
    await update.callback_query.answer()


async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 👍/👎 feedback on AI responses."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data  # "feedback:up" or "feedback:down"
    rating = data.split(":", 1)[1] if ":" in data else "up"
    message_id = query.message.message_id if query.message else 0

    # Save to DB
    try:
        await db.save_feedback(user_id, message_id, rating)
    except Exception as e:
        logging.warning("Feedback save failed: %s", e)

    # Visual confirmation: replace the feedback row with a "thanks" indicator
    emoji = "👍" if rating == "up" else "👎"
    try:
        # Preserve existing keyboard but replace feedback row
        old_markup = query.message.reply_markup
        new_buttons = []
        if old_markup and old_markup.inline_keyboard:
            for row in old_markup.inline_keyboard:
                # Skip the original feedback row (contains feedback: callbacks)
                if any(
                    (getattr(btn, "callback_data", "") or "").startswith("feedback:")
                    for btn in row
                ):
                    continue
                new_buttons.append(row)

        # Add confirmed feedback indicator
        confirmed_row = [
            InlineKeyboardButton(f"{emoji} Спасибо за отзыв!", callback_data="noop")
        ]
        new_buttons.insert(0, confirmed_row)

        await query.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(new_buttons)
        )
    except Exception:
        pass  # Best-effort UI update


# ── Helper ───────────────────────────────────────────────────────────────────

def _add_fast_callback(application: Application, callback, pattern: str):
    """Register lightweight UI callbacks in non-blocking mode."""
    application.add_handler(
        CallbackQueryHandler(callback, pattern=pattern, block=False), group=-1
    )


# ── Registration ─────────────────────────────────────────────────────────────

def register(application: Application):
    # Быстрый канал for UI-настроек: callback выполняется without блокировки update loop.
    _add_fast_callback(application, toggle_search_callback, "^toggle_search$")
    _add_fast_callback(application, new_chat_callback, "^new_chat$")
    _add_fast_callback(application, model_menu_callback, "^model_menu$")
    _add_fast_callback(application, help_callback, "^help$")
    _add_fast_callback(application, start_menu_callback, "^start_menu$")
    _add_fast_callback(application, model_button_callback, "^model")
    _add_fast_callback(application, open_roles_callback, "^open_roles$")
    _add_fast_callback(application, role_apply_callback, "^role_apply:")
    _add_fast_callback(application, role_clear_callback, "^role_clear$")
    _add_fast_callback(application, role_nav_callback, "^role_nav:")
    _add_fast_callback(application, role_page_callback, "^role_page:")
    _add_fast_callback(application, conv_page_callback, "^conv_page:")
    _add_fast_callback(application, conv_switch_callback, "^conv_switch$")
    _add_fast_callback(application, conv_switch_to_callback, "^conv_switch_to:")

    # Process оба формата: model:0 (new) и model_none (разделитель)
    application.add_handler(
        CallbackQueryHandler(complex_search_callback, pattern="^complex:")
    )
    application.add_handler(
        CallbackQueryHandler(fallback_callback, pattern="^fallback:")
    )
    application.add_handler(CallbackQueryHandler(document_callback, pattern="^doc:"))
    application.add_handler(
        CallbackQueryHandler(deep_dive_callback, pattern="^deepdive:")
    )
    application.add_handler(
        CallbackQueryHandler(new_topic_callback, pattern="^new_topic")
    )
    application.add_handler(
        CallbackQueryHandler(retry_last_callback, pattern="^retry_last$")
    )
    # Feedback buttons (👍/👎)
    _add_fast_callback(application, feedback_callback, "^feedback:")
    _add_fast_callback(
        application,
        _noop_callback,
        "^noop$",
    )
    # Роль: apply/clear/create
    application.add_handler(
        CallbackQueryHandler(role_create_callback, pattern="^role_create$")
    )
    application.add_handler(
        CallbackQueryHandler(
            role_create_cancel_callback, pattern="^role_create_cancel$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(role_custom_apply_callback, pattern="^role_custom_apply$")
    )
    application.add_handler(
        CallbackQueryHandler(role_custom_save_callback, pattern="^role_custom_save$")
    )
    application.add_handler(
        CallbackQueryHandler(role_custom_retry_callback, pattern="^role_custom_retry$")
    )
    # New Role management
    application.add_handler(
        CallbackQueryHandler(role_detail_callback, pattern="^role_detail:")
    )
    application.add_handler(
        CallbackQueryHandler(role_view_prompt_callback, pattern="^role_view_prompt:")
    )
    application.add_handler(
        CallbackQueryHandler(role_delete_ask_callback, pattern="^role_delete_ask:")
    )
    application.add_handler(
        CallbackQueryHandler(
            role_delete_confirm_callback, pattern="^role_delete_confirm:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            role_delete_cancel_callback, pattern="^role_delete_cancel:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(role_rename_menu_callback, pattern="^role_rename_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(role_rename_pick_callback, pattern="^role_rename_pick:")
    )
    application.add_handler(
        CallbackQueryHandler(
            role_rename_cancel_callback, pattern="^role_rename_cancel$"
        )
    )

    # Role Navigation (New)
    application.add_handler(
        CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$")
    )

    # Conversation management callbacks
    application.add_handler(
        CallbackQueryHandler(conv_rename_callback, pattern="^conv_rename$")
    )
    application.add_handler(
        CallbackQueryHandler(conv_rename_ask_callback, pattern="^conv_rename_ask:")
    )
    application.add_handler(
        CallbackQueryHandler(
            conv_rename_cancel_callback, pattern="^conv_rename_cancel$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(conv_delete_callback, pattern="^conv_delete$")
    )
    application.add_handler(
        CallbackQueryHandler(conv_delete_ask_callback, pattern="^conv_delete_ask:")
    )
    application.add_handler(
        CallbackQueryHandler(
            conv_delete_confirm_callback, pattern="^conv_delete_confirm:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            conv_delete_cancel_callback, pattern="^conv_delete_cancel$"
        )
    )

    # Refresh metrics
    application.add_handler(
        CallbackQueryHandler(refresh_metrics_callback, pattern="^refresh_metrics$")
    )
