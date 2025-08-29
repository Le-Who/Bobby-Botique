import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from app.config import settings
from app import database as db
from app.utils.formatting import TelegramFormatter
from app.error_handler import handle_telegram_error, safe_execute

@handle_telegram_error("callback_handler")
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает все callback запросы"""
    try:
        query = update.callback_query
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Подтверждаем получение callback
        await query.answer()
        
        # Разбираем callback_data
        callback_data = query.data
        
        if callback_data.startswith("doc:"):
            await _handle_document_callback(query, callback_data, user_id, chat_id)
        elif callback_data.startswith("model:"):
            await _handle_model_callback(query, callback_data, user_id, chat_id)
        elif callback_data.startswith("search:"):
            await _handle_search_callback(query, callback_data, user_id, chat_id)
        else:
            await query.edit_message_text("❌ Неизвестный callback")
            
    except Exception as e:
        logging.error(f"Error in callback handler: {e}")
        try:
            await update.callback_query.answer("❌ Произошла ошибка", show_alert=True)
        except:
            pass

async def _handle_document_callback(query, callback_data: str, user_id: int, chat_id: int):
    """Обрабатывает callback'и для документов"""
    try:
        action = callback_data.split(":")[1]
        
        if action == "upload_new":
            await _handle_upload_new_document(query, user_id, chat_id)
        elif action == "select_document":
            await _handle_select_document(query, user_id, chat_id)
        elif action == "cancel":
            await _handle_cancel_documents(query, user_id, chat_id)
        elif action.startswith("use_existing"):
            document_id = callback_data.split(":")[2]
            await _handle_use_existing_document(query, document_id, user_id, chat_id)
        elif action == "force_upload":
            await _handle_force_upload_document(query, user_id, chat_id)
        else:
            await query.edit_message_text("❌ Неизвестное действие с документом")
            
    except Exception as e:
        logging.error(f"Error handling document callback: {e}")
        await query.edit_message_text("❌ Ошибка при обработке действия с документом")

async def _handle_upload_new_document(query, user_id: int, chat_id: int):
    """Обрабатывает загрузку нового документа"""
    try:
        text = (
            "📄 *Загрузка нового документа*\n\n"
            "💡 *Инструкции:*\n"
            "1. Отправьте файл (PDF, DOC, DOCX)\n"
            "2. Максимальный размер: 50MB\n"
            "3. Дождитесь обработки\n"
            "4. Задавайте вопросы по содержимому\n\n"
            "📋 *Поддерживаемые форматы:*\n"
            "• PDF (.pdf)\n"
            "• Word (.doc, .docx)\n\n"
            "🚀 Отправьте ваш документ!"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in upload new document: {e}")
        await query.edit_message_text("❌ Ошибка при отображении инструкций")

async def _handle_select_document(query, user_id: int, chat_id: int):
    """Обрабатывает выбор документа"""
    try:
        # Получаем список документов пользователя
        documents = await safe_execute(
            db.get_user_documents,
            user_id,
            context="document_selection",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(documents, str) and documents.startswith("❌"):
            await query.edit_message_text(documents)
            return
        
        if not documents:
            await query.edit_message_text("📄 У вас пока нет документов для выбора.")
            return
        
        # Создаем список документов с кнопками
        text = "📋 *Выберите документ:*\n\n"
        keyboard = []
        
        for i, doc in enumerate(documents[:10], 1):
            text += f"{i}. 📄 {doc.get('filename', 'Unknown')}\n"
            if doc.get('created_at'):
                text += f"   📅 {doc['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
            text += "\n"
            
            # Создаем кнопку для выбора документа
            keyboard.append([
                InlineKeyboardButton(
                    f"📄 {doc.get('filename', 'Unknown')[:30]}...",
                    callback_data=f"doc:select:{doc['id']}"
                )
            ])
        
        if len(documents) > 10:
            text += f"... и еще {len(documents) - 10} документов\n\n"
        
        text += "💡 Выберите документ для работы с ним."
        
        # Добавляем кнопку возврата
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="doc:back")])
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        logging.error(f"Error in select document: {e}")
        await query.edit_message_text("❌ Ошибка при получении списка документов")

async def _handle_cancel_documents(query, user_id: int, chat_id: int):
    """Обрабатывает отмену работы с документами"""
    try:
        # Очищаем состояние работы с документами
        await safe_execute(
            db.clear_document_mode,
            chat_id,
            context="document_mode_clear",
            user_id=user_id,
            chat_id=chat_id
        )
        
        text = (
            "❌ *Работа с документами отменена*\n\n"
            "💡 Теперь вы можете:\n"
            "• Отправлять обычные сообщения\n"
            "• Анализировать изображения\n"
            "• Использовать поиск и AI\n\n"
            "🔄 Для возврата к документам используйте /documents"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in cancel documents: {e}")
        await query.edit_message_text("❌ Ошибка при отмене работы с документами")

async def _handle_use_existing_document(query, document_id: str, user_id: int, chat_id: int):
    """Обрабатывает использование существующего документа"""
    try:
        # Получаем информацию о документе
        document = await safe_execute(
            db.get_document_by_id,
            int(document_id),
            user_id,
            context="document_retrieval",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(document, str) and document.startswith("❌"):
            await query.edit_message_text(document)
            return
        
        if not document:
            await query.edit_message_text("❌ Документ не найден.")
            return
        
        # Устанавливаем режим работы с документами
        await safe_execute(
            db.set_document_mode,
            chat_id,
            int(document_id),
            context="document_mode_set",
            user_id=user_id,
            chat_id=chat_id
        )
        
        text = (
            f"✅ *Документ выбран:* {document.get('filename', 'Unknown')}\n\n"
            "💡 *Теперь вы можете:*\n"
            "• Задавать вопросы по содержимому\n"
            "• Получать анализ документа\n"
            "• Работать с текстом\n\n"
            "📝 Просто напишите ваш вопрос!"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in use existing document: {e}")
        await query.edit_message_text("❌ Ошибка при выборе документа")

async def _handle_force_upload_document(query, user_id: int, chat_id: int):
    """Обрабатывает принудительную загрузку документа"""
    try:
        text = (
            "📄 *Принудительная загрузка*\n\n"
            "⚠️ *Внимание:* Файл будет загружен как новый документ,\n"
            "даже если он уже существует в системе.\n\n"
            "💡 *Инструкции:*\n"
            "1. Отправьте файл (PDF, DOC, DOCX)\n"
            "2. Максимальный размер: 50MB\n"
            "3. Дождитесь обработки\n\n"
            "🚀 Отправьте ваш документ!"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in force upload document: {e}")
        await query.edit_message_text("❌ Ошибка при отображении инструкций")

async def _handle_model_callback(query, callback_data: str, user_id: int, chat_id: int):
    """Обрабатывает callback'и для выбора модели"""
    try:
        model_name = callback_data.split(":")[1]
        
        # Обновляем модель пользователя
        success = await safe_execute(
            db.update_user_model,
            user_id,
            model_name,
            context="model_update",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(success, str) and success.startswith("❌"):
            await query.edit_message_text(success)
            return
        
        text = f"✅ *Модель обновлена:* `{model_name}`\n\n💡 Теперь все ваши запросы будут обрабатываться с использованием этой модели."
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in model callback: {e}")
        await query.edit_message_text("❌ Ошибка при обновлении модели")

async def _handle_search_callback(query, callback_data: str, user_id: int, chat_id: int):
    """Обрабатывает callback'и для поиска"""
    try:
        action = callback_data.split(":")[1]
        
        if action == "enable":
            await _handle_enable_search(query, user_id, chat_id)
        elif action == "disable":
            await _handle_disable_search(query, user_id, chat_id)
        else:
            await query.edit_message_text("❌ Неизвестное действие поиска")
            
    except Exception as e:
        logging.error(f"Error in search callback: {e}")
        await query.edit_message_text("❌ Ошибка при обработке поиска")

async def _handle_enable_search(query, user_id: int, chat_id: int):
    """Включает режим поиска"""
    try:
        # Включаем режим поиска
        success = await safe_execute(
            db.enable_search_mode,
            chat_id,
            context="search_enable",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(success, str) and success.startswith("❌"):
            await query.edit_message_text(success)
            return
        
        text = (
            "🔍 *Режим поиска ВКЛЮЧЕН*\n\n"
            "💡 Теперь при использовании '??' система будет:\n"
            "• Автоматически искать актуальную информацию\n"
            "• Предоставлять источники\n"
            "• Давать более точные ответы\n\n"
            "🚀 Попробуйте: ?? [ваш вопрос]"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in enable search: {e}")
        await query.edit_message_text("❌ Ошибка при включении поиска")

async def _handle_disable_search(query, user_id: int, chat_id: int):
    """Отключает режим поиска"""
    try:
        # Отключаем режим поиска
        success = await safe_execute(
            db.disable_search_mode,
            chat_id,
            context="search_disable",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(success, str) and success.startswith("❌"):
            await query.edit_message_text(success)
            return
        
        text = (
            "🔍 *Режим поиска ОТКЛЮЧЕН*\n\n"
            "💡 Теперь система будет работать только с:\n"
            "• Встроенными знаниями AI\n"
            "• Контекстом разговора\n"
            "• Загруженными документами\n\n"
            "🔄 Для включения поиска используйте /search"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await query.edit_message_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        logging.error(f"Error in disable search: {e}")
        await query.edit_message_text("❌ Ошибка при отключении поиска")

# Регистрация обработчиков
def register(application: Application):
    """Регистрирует обработчики callback'ов"""
    application.add_handler(CallbackQueryHandler(handle_callback))
