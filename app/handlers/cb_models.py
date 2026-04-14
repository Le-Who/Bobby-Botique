"""
Callback handlers — model selection.

Handles model:* and switch_model:* callback buttons.
"""

__all__ = ["model_button_callback", "switch_model_callback"]

import logging

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from app.config import get_model_hash, get_openrouter_keys, settings
from app.handlers import menus
from app.handlers.callbacks import _BUSY_TOAST, _is_user_busy
from app.repos.chats import get_user_chat, update_user_chat


async def model_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Игнорируем клики на разделитель
    if query.data == "model_none":
        return

    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    # Get model from индекса (new формат с хэшем) or from полного имени (old формат for совместимости)
    model_name: str | None = None
    if query.data.startswith("model:"):
        # Новый формат: model:index:hash or model:index (old формат without хэша)
        try:
            parts = query.data.split(":")
            model_index = int(parts[1])
            expected_hash = parts[2] if len(parts) > 2 else None

            # Get актуальный list моделей from настроек
            all_models: list[str] = []
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
                        from app.utils.keyboards import error_with_back_keyboard

                        await query.edit_message_text(
                            "⚠️ Список моделей обновился. Пожалуйста, выберите модель заново.",
                            reply_markup=error_with_back_keyboard("model_menu", "🧠 Выбрать модель"),
                        )
                        return
            else:
                from app.utils.keyboards import error_with_back_keyboard

                await query.edit_message_text(
                    "❌ Ошибка: неверный индекс модели.",
                    reply_markup=error_with_back_keyboard("model_menu", "🧠 Выбрать модель"),
                )
                return
        except (ValueError, IndexError) as e:
            from app.utils.keyboards import error_with_back_keyboard

            await query.edit_message_text(
                "❌ Ошибка: неверный формат callback_data.",
                reply_markup=error_with_back_keyboard("model_menu", "🧠 Выбрать модель"),
            )
            logging.error("Error parsing model callback: %s, data: %s", e, query.data)
            return
    else:
        data = query.data or ""
        model_name = data.split("_", 1)[1] if "_" in data else None
        if not model_name:
            from app.utils.keyboards import error_with_back_keyboard

            await query.edit_message_text(
                "❌ Ошибка: неверный формат callback_data.",
                reply_markup=error_with_back_keyboard("model_menu", "🧠 Выбрать модель"),
            )
            return
    chat_state = await get_user_chat(user_id)
    chat_state.model = model_name
    await update_user_chat(user_id, chat_state)

    # Update menu с новой выбранной modelю
    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, context)

    # Определяем имя for тоста
    is_openrouter = "/" in model_name
    display_name = model_name.split("/")[-1] if is_openrouter else model_name

    try:
        await query.edit_message_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e
    await query.answer(f"✅ Модель изменена на {display_name}")


async def switch_model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle model switch from smart suggestion hint."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if _is_user_busy(user_id):
        await query.answer(_BUSY_TOAST, show_alert=True)
        return

    model_name = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else None
    if not model_name:
        return

    # Verify model is available
    all_models = list(settings.AVAILABLE_MODELS or [])
    if settings.OPENROUTER_AVAILABLE_MODELS:
        all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)

    if model_name not in all_models:
        await query.edit_message_text("⚠️ Эта модель больше недоступна.")
        return

    chat_state = await get_user_chat(user_id)
    chat_state.model = model_name
    await update_user_chat(user_id, chat_state)

    display_name = model_name.split("/")[-1] if "/" in model_name else model_name
    await query.edit_message_text(f"✅ Модель переключена на **{display_name}**")
