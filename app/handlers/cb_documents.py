"""
Document management callbacks — upload, select, delete, clear, cancel.
"""

import logging

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.document_processor import (
    delete_all_user_documents,
    delete_user_document,
    get_document_by_id,
    get_user_documents,
)
from app.handlers import menus
from app.state import clear_document_state, get_selected_document_id, set_document_mode
from app.utils.formatting import TelegramFormatter
from app.utils.keyboards import (
    back_button,
    build_keyboard,
    cancel_button,
    confirm_cancel_row,
)


async def _handle_document_upload_new(query, context, user_id):
    text = "📄 **Загрузите новый документ**\n\nОтправьте PDF или DOCX файл, и я обработаю его для вас."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(back_button("doc:list")),
    )


async def _handle_document_list(query, context, user_id):
    text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
    await query.edit_message_text(
        text, parse_mode=parse_mode, reply_markup=reply_markup
    )


async def _handle_document_cancel(query, context, user_id):
    clear_document_state(user_id)

    text = "✅ **Режим работы с документами отключен**\n\nТеперь ваши сообщения будут обрабатываться в обычном режиме чата.\nЧтобы снова работать с документами, загрузите новый файл или используйте команду /documents."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="start_menu")]]
    )
    await query.edit_message_text(
        formatted_text, parse_mode=parse_mode, reply_markup=kb
    )


async def _handle_document_clear_all(query, context, user_id):
    # Get все documents user
    documents = await get_user_documents(user_id)
    if not documents:
        await query.answer("У вас нет документов для удаления.")
        return

    text = (
        "⚠️ **Вы уверены?**\n\n"
        "🚨 **Все документы будут удалены безвозвратно.**\n"
        "Вы потеряете все загруженные файлы и их обработку."
    )
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            confirm_cancel_row(
                "doc:clear_all_confirm", "doc:list", "✅ Да, удалить все", "❌ Отмена"
            )
        ),
    )


async def _handle_document_clear_all_confirm(query, context, user_id):
    # Delete все documents одной оптимfromированной операцией
    deleted_count = await delete_all_user_documents(user_id)

    if deleted_count == 0:
        await query.answer("У вас нет документов для удаления.")
        text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
        return

    # Clean up state работы с documentами
    clear_document_state(user_id)

    # Update menu
    text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
    await query.edit_message_text(
        text, parse_mode=parse_mode, reply_markup=reply_markup
    )
    await query.answer(f"🗑️ Удалено {deleted_count} документов.")


async def _handle_document_use_existing(query, context, user_id):
    # Используем существующий document
    document_id = int(query.data.split(":")[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        from app.utils.keyboards import error_with_back_keyboard
        await query.edit_message_text(
            "❌ Документ не найден.",
            reply_markup=error_with_back_keyboard("doc:list", "📄 К документам")
        )
        return

    # Устанавливаем state работы с documentами
    set_document_mode(user_id, True, document_id)

    text = f"✅ **Используется существующий документ**\n\n📄 **{document['filename']}**\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 **Просто напишите ваш вопрос** - система автоматически найдет ответ в документе.\n\n🔄 **Для выхода из режима документов:**\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(cancel_button("doc:cancel")),
    )


async def _handle_document_force_upload(query, context, user_id):
    text = "📄 *Загрузите файл как новый документ*\n\nОтправьте файл еще раз, и он будет сохранен как новый документ."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            back_button("doc:list"), cancel_button("doc:cancel")
        ),
    )


async def _handle_document_select_document(query, context, user_id):
    # Показываем menu выбора documentа
    documents = await get_user_documents(user_id)
    if not documents:
        # If documentов нет, показываем главное menu documentов
        text, parse_mode, reply_markup = await menus.get_documents_menu_content(user_id)
        try:
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise e
        return

    # Create buttons for каждого documentа
    keyboard = []
    for doc in documents[:10]:  # Максимум 10 documentов
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"📄 {doc['filename'][:30]}...",
                    callback_data=f"doc:select:{doc['id']}",
                ),
                InlineKeyboardButton(
                    "🗑️", callback_data=f"doc:delete_document:{doc['id']}"
                ),
            ]
        )

    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="doc:list")])

    text = "📋 **Выберите документ для работы:**\n\nНажмите на документ, чтобы начать работу с ним."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_document_select(query, context, user_id):
    # Выбираем конкретный document
    document_id = int(query.data.split(":")[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        from app.utils.keyboards import error_with_back_keyboard
        await query.edit_message_text(
            "❌ Документ не найден.",
            reply_markup=error_with_back_keyboard("doc:list", "📄 К документам")
        )
        return

    # Устанавливаем state работы с documentами
    set_document_mode(user_id, True, document_id)

    text = f"✅ **Выбран документ**\n\n📄 **{document['filename']}**\n📊 Страниц: {document['pages']}\n📅 Загружен: {document['created_at'][:10]}\n\nТеперь вы можете задавать вопросы по этому документу.\n\n💡 **Просто напишите ваш вопрос** - система автоматически найдет ответ в документе.\n\n🔄 **Для выхода из режима документов:**\n• Нажмите кнопку '❌ Отмена' ниже\n• Или отправьте команду /documents"
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await query.edit_message_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=build_keyboard(
            back_button("doc:select_document", "⬅️ Назад к списку"),
            cancel_button("doc:cancel"),
        ),
    )


async def _handle_document_delete_document(query, context, user_id):
    # Delete конкретный document
    document_id = int(query.data.split(":")[2])

    document = await get_document_by_id(document_id, user_id)
    if not document:
        await query.answer("❌ Документ не найден.")
        return

    success = await delete_user_document(document_id, user_id)
    if success:
        # Check, был ли это выбранный document
        selected_doc_id = get_selected_document_id(user_id)
        if selected_doc_id == document_id:
            # If удалor выбранный document, очищаем state
            clear_document_state(user_id)

        documents = await get_user_documents(user_id)
        if not documents:
            # If documentов не осталось, показываем главное menu documentов
            text, parse_mode, reply_markup = await menus.get_documents_menu_content(
                user_id
            )
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        else:
            # Otherwise перестраиваем list выбора
            keyboard = []
            for doc in documents[:10]:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"📄 {doc['filename'][:30]}...",
                            callback_data=f"doc:select:{doc['id']}",
                        ),
                        InlineKeyboardButton(
                            "🗑️", callback_data=f"doc:delete_document:{doc['id']}"
                        ),
                    ]
                )
            keyboard.append(
                [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
            )

            text = "📋 *Выберите документ для работы:*\n\nНажмите на документ, чтобы начать работу с ним."
            formatted_text, parse_mode = TelegramFormatter.format_text(text)
            await query.edit_message_text(
                formatted_text,
                parse_mode=parse_mode,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        await query.answer(f"🗑️ Документ '{document['filename']}' удален.")
    else:
        await query.answer("❌ Ошибка при удалении документа.")


async def document_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает callback-кнопки для управления документами"""
    query = update.callback_query
    await query.answer()

    action = query.data.split(":")[1]
    user_id = query.from_user.id

    handlers = {
        "upload_new": _handle_document_upload_new,
        "list": _handle_document_list,
        "cancel": _handle_document_cancel,
        "clear_all": _handle_document_clear_all,
        "clear_all_confirm": _handle_document_clear_all_confirm,
        "use_existing": _handle_document_use_existing,
        "force_upload": _handle_document_force_upload,
        "select_document": _handle_document_select_document,
        "select": _handle_document_select,
        "delete_document": _handle_document_delete_document,
    }

    handler = handlers.get(action)
    if handler:
        await handler(query, context, user_id)
    else:
        logging.warning("Unknown document action: %s", action)
