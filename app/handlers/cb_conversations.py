"""
Conversation management callbacks — switch, rename, delete, pagination.
Also includes admin-only metrics refresh callback.
"""

import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers import menus
from app.metrics import role_conv_metrics
from app.repos.conversations import delete_conversation, get_user_conversations, switch_to_conversation
from app.utils.decorators import admin_only
from app.utils.formatting import TelegramFormatter


async def send_conversation_selection(
    query: telegram.CallbackQuery, user_id: int, action_prefix: str, title: str
) -> None:
    """
    Helper to send a list of conversations for selection.

    Args:
        query: The callback query object
        user_id: The user ID
        action_prefix: The prefix for the callback data (e.g. 'conv_switch_to', 'conv_delete_ask')
        title: The title text to display
    """
    # Get list бесед for выбора
    conversations = await get_user_conversations(user_id, 10, 0)
    if not conversations:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "📝 У вас нет сохранённых бесед.\n\nИспользуйте /save <название> для сохранения текущей беседы.",
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    text = f"{title}\n\n"
    buttons = []

    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv["role_title"] else ""
        created = conv["created_at"].strftime("%d.%m %H:%M") if conv["created_at"] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created}\n\n"

        buttons.append(
            [
                InlineKeyboardButton(
                    f"🆔 {conv['id']} | {conv['title'][:30]}{'...' if len(conv['title']) > 30 else ''}",
                    callback_data=f"{action_prefix}:{conv['id']}",
                )
            ]
        )

    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="conv_page:1")])

    fmt_text, fmt_pm = TelegramFormatter.format_text(text)
    await query.edit_message_text(fmt_text, parse_mode=fmt_pm, reply_markup=InlineKeyboardMarkup(buttons))


async def conv_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка пагинации списка бесед"""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    user_id = query.from_user.id

    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, page)

    if reply_markup is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def conv_switch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переключение на беседу"""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_switch_to",
        "🔄 *Выберите беседу для переключения:*",
    )


async def conv_switch_to_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переключение на конкретную беседу"""
    query = update.callback_query
    user_id = query.from_user.id

    conv_id = int(query.data.split(":")[1])

    try:
        success = await switch_to_conversation(user_id, conv_id)
        if success:
            await role_conv_metrics.record_conversation_switched()
            # Показываем list бесед с тостом
            text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, 1)
            await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            await query.answer(f"✅ Переключились на беседу ID: {conv_id}")
        else:
            from app.utils.keyboards import error_with_back_keyboard

            await query.edit_message_text(
                "❌ Ошибка при переключении на беседу.",
                reply_markup=error_with_back_keyboard("conv_page:1", "⬅️ К беседам"),
            )
            await query.answer("❌ Ошибка при переключении на беседу.")
    except Exception as e:
        logging.error("Error switching to conversation %s: %s", conv_id, e, exc_info=True)
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Ошибка при переключении на беседу.", reply_markup=error_with_back_keyboard("conv_page:1", "⬅️ К беседам")
        )
        await query.answer("❌ Ошибка при переключении на беседу.")


async def conv_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переименование беседы"""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_rename_ask",
        "✏️ *Выберите беседу для переименования:*",
    )


async def conv_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаление беседы"""
    query = update.callback_query
    assert query is not None
    await query.answer()
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_delete_ask",
        "🗑️ *Выберите беседу для удаления:*",
    )


async def conv_delete_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Спрашивает подтверждение удаления беседы"""
    query = update.callback_query
    await query.answer()

    conv_id = int(query.data.split(":")[1])

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"conv_delete_confirm:{conv_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="conv_delete_cancel")],
    ]

    del_text = f"⚠️ **Удалить беседу {conv_id}?**\n\n🚨 **Вся история сообщений будет потеряна безвозвратно.**"
    fmt_text, fmt_pm = TelegramFormatter.format_text(del_text)
    await query.edit_message_text(
        fmt_text,
        parse_mode=fmt_pm,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def conv_rename_ask_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Спрашивает новое название беседы"""
    query = update.callback_query
    await query.answer()

    conv_id = int(query.data.split(":")[1])
    if context.user_data is not None:
        context.user_data["rename_conv_id"] = conv_id

    keyboard = [[InlineKeyboardButton("↩️ Отмена", callback_data="conv_rename_cancel")]]

    await query.edit_message_text(
        f"✏️ Введите новое название для беседы {conv_id} (одной строкой):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def conv_rename_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена переименования"""
    query = update.callback_query
    await query.answer("❌ Переименование отменено")

    if context.user_data is not None:
        context.user_data.pop("rename_conv_id", None)

    assert query is not None
    await send_conversation_selection(
        query,
        query.from_user.id,
        "conv_rename_ask",
        "✏️ *Выберите беседу для переименования:*",
    )


async def conv_delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение удаления беседы"""
    query = update.callback_query

    conv_id = int(query.data.split(":")[1])
    user_id = query.from_user.id

    success = await delete_conversation(user_id, conv_id)

    if success:
        await role_conv_metrics.record_conversation_deleted()

        # Update list
        text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, 1)
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

        await query.answer(f"✅ Беседа {conv_id} удалена")
    else:
        from app.utils.keyboards import error_with_back_keyboard

        await query.edit_message_text(
            "❌ Ошибка при удалении беседы.", reply_markup=error_with_back_keyboard("conv_page:1", "⬅️ К беседам")
        )
        await query.answer("❌ Ошибка при удалении беседы")


async def conv_delete_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отмена удаления беседы"""
    query = update.callback_query

    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(query.from_user.id, 1)
    await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    await query.answer("❌ Удаление отменено")


@admin_only
async def refresh_metrics_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refreshes the metrics dashboard."""
    query = update.callback_query

    try:
        text = await menus.get_metrics_content()
        formatted_text, parse_mode = TelegramFormatter.format_text(text)

        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_metrics")]]

        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await query.answer("🔄 Метрики обновлены")

    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            await query.answer("✅ Данные актуальны", show_alert=False)
        else:
            logging.error("Error refreshing metrics: %s", e)
            await query.answer("❌ Ошибка обновления")
    except Exception as e:
        logging.error("Error in refresh metrics callback: %s", e, exc_info=True)
        await query.answer("❌ Внутренняя ошибка")
