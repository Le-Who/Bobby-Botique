# /app/handlers/messages.py

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, filters, Application

from . import agent
from ..config import settings
from .. import database as db
from .. import state
from ..group_chat import group_chat_manager, log_group_message
from ..document_processor import process_uploaded_document
from ..metrics import metrics_collector

async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Проверяем авторизацию
    if not await db.is_authorized(user_id): 
        return
    
    # Для групповых чатов проверяем, зарегистрирована ли группа
    if update.effective_chat.type != 'private':
        group_info = await group_chat_manager.get_group_info(chat_id)
        if not group_info:
            # Если группа не зарегистрирована, игнорируем сообщение
            return
        
        # Проверяем, является ли пользователь участником группы
        if not await group_chat_manager.is_member(chat_id, user_id):
            return
    
    user_lock = state.USER_LOCKS[user_id]
    if user_lock.locked():
        await update.message.reply_text("Пожалуйста, подождите, я еще обрабатываю ваш предыдущий запрос.")
        return

    # Обрабатываем документы
    if update.message.document:
        await handle_document(update, context)
        return
    
    if update.message.text:
        chat_state = await db.get_user_chat(user_id)
        if chat_state.token_count >= settings.CHAT_TOKEN_LIMIT:
            await update.message.reply_text("Достигнут лимит токенов. Начните новый /newchat")
            return
    
    # Логируем сообщение в группе
    if update.effective_chat.type != 'private':
        await log_group_message(chat_id, user_id, update.message.text or update.message.caption or "")
            
    placeholder_message = await update.message.reply_text("⏳ Принято в обработку...")
    
    async def task_wrapper():
        async with user_lock:
            await agent.process_long_request(placeholder_message, update, context)
            
    asyncio.create_task(task_wrapper())

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает загруженные документы"""
    user_id = update.effective_user.id
    document = update.message.document
    
    # Проверяем размер файла (максимум 50MB)
    if document.file_size > 50 * 1024 * 1024:
        await update.message.reply_text("❌ Файл слишком большой. Максимальный размер: 50MB")
        return
    
    # Проверяем тип файла
    supported_formats = ['.pdf', '.docx', '.doc']
    file_ext = document.file_name.lower().split('.')[-1] if '.' in document.file_name else ''
    
    if f'.{file_ext}' not in supported_formats:
        await update.message.reply_text(f"❌ Неподдерживаемый формат файла. Поддерживаемые форматы: {', '.join(supported_formats)}")
        return
    
    # Отправляем сообщение о начале обработки
    processing_msg = await update.message.reply_text("📄 Обрабатываю документ...")
    
    try:
        # Скачиваем файл
        file = await document.get_file()
        file_data = await file.download_as_bytearray()
        
        # Обрабатываем документ
        result = await process_uploaded_document(file_data, document.file_name, user_id)
        
        if result.get("error"):
            if result.get("error") == "duplicate":
                # Обрабатываем дубликат
                duplicate_info = result.get("duplicate_info", {})
                duplicate_text = (
                    f"⚠️ **Файл уже загружен**\n\n"
                    f"Файл `{document.file_name}` уже был загружен ранее как:\n"
                    f"📄 **{duplicate_info.get('filename', 'Unknown')}**\n"
                    f"📅 Загружен: {duplicate_info.get('created_at', 'Unknown')[:10]}\n\n"
                    f"Хотите использовать существующий документ?"
                )
                
                # Создаем кнопки для работы с дубликатом
                keyboard = [
                    [InlineKeyboardButton("✅ Использовать существующий", callback_data=f"doc:use_existing:{duplicate_info.get('id')}")],
                    [InlineKeyboardButton("📄 Загрузить как новый", callback_data="doc:force_upload")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
                ]
                
                await processing_msg.edit_text(
                    duplicate_text,
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            else:
                await processing_msg.edit_text(f"❌ Ошибка обработки: {result['error']}")
                return
        
        # Отправляем результат
        success_text = (
            f"✅ Документ обработан успешно!\n\n"
            f"📄 **{document.file_name}**\n"
            f"📊 Страниц: {result.get('pages', 'N/A')}\n"
            f"📝 Символов: {result.get('text_length', 0):,}\n"
        )
        
        if result.get('paragraphs'):
            success_text += f"📄 Параграфов: {result['paragraphs']}\n"
        if result.get('tables'):
            success_text += f"📊 Таблиц: {result['tables']}\n"
        
        success_text += "\n\n💡 **Как задавать вопросы:**\n• Просто напишите ваш вопрос\n• Например: \"Какие основные пункты?\", \"Что говорится о...?\"\n• Система автоматически найдет ответ в документе"
        
        # Создаем кнопки для управления документом
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить другой документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")]
        ]
        
        await processing_msg.edit_text(
            success_text, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Записываем метрики
        await metrics_collector.record_api_call("document_processing", document.file_name)
        
    except Exception as e:
        logging.error(f"Error processing document: {e}")
        await processing_msg.edit_text(f"❌ Произошла ошибка при обработке документа: {str(e)}")
        await metrics_collector.record_error("document_processing", str(e))

def register(application: Application):
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request))
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
