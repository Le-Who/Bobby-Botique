# /app/handlers/messages.py

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, filters, Application

from app.config import settings
from app import database as db
from app import state
from app.group_chat import group_chat_manager, log_group_message
from app.document_processor import process_uploaded_document
from app.metrics import metrics_collector
from app.utils.formatting import TelegramFormatter
from app.utils.api_logger import api_logger
from app.error_handler import handle_telegram_error, safe_execute

# Глобальный словарь для хранения групп изображений
MEDIA_GROUPS = {}

@handle_telegram_error("message_handler")
async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие сообщения"""
    # Валидация входных данных
    if not update or not update.effective_user:
        logging.error("Invalid update object received")
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Валидация user_id
    if not isinstance(user_id, int) or user_id <= 0:
        logging.error("Invalid user_id: %s", user_id)
        return
    
    # Проверяем, есть ли изображение и media_group_id
    is_photo = bool(update.message.photo)
    media_group_id = update.message.media_group_id if update.message else None
    
    # Если это изображение с media_group_id, проверяем, действительно ли это группа
    if is_photo and media_group_id:
        logging.info(f"📸 Получено изображение с media_group_id {media_group_id} от пользователя {user_id}")
        
        # Инициализируем группу, если её нет
        if media_group_id not in MEDIA_GROUPS:
            MEDIA_GROUPS[media_group_id] = {
                'user_id': user_id,
                'chat_id': chat_id,
                'photos': [],
                'processed': False
            }
        
        # Добавляем фото в группу
        MEDIA_GROUPS[media_group_id]['photos'].append(update.message.photo[-1])
        
        # Если это последнее фото в группе, обрабатываем всю группу
        if len(MEDIA_GROUPS[media_group_id]['photos']) >= 2:
            await _process_media_group(media_group_id, update, context)
        
        return
    
    # Обрабатываем документы
    if update.message.document:
        await _handle_document(update, context)
        return
    
    # Проверяем, есть ли изображение (одиночное)
    is_photo = bool(update.message.photo)
    
    if is_photo:
        logging.info(f"Processing single photo from user {user_id}")
        placeholder_message = await update.message.reply_text("🖼️ Обрабатываю изображение...")
    else:
        logging.info(f"Processing text message from user {user_id}")
        placeholder_message = await update.message.reply_text("🤔 Думаю...")
    
    # Обычная обработка сообщений
    async def task_wrapper():
        try:
            async with state.get_user_lock(user_id):
                logging.info("Starting task processing for user %s", user_id)
                
                # Восстанавливаем обработку через agent
                from app.handlers.agent import handle_agent_request
                
                if is_photo:
                    # Обработка изображения
                    result = await safe_execute(
                        handle_agent_request,
                        placeholder_message,
                        user_id,
                        update.message.photo[-1],
                        chat_state=None,
                        context="image_processing",
                        user_id=user_id,
                        chat_id=chat_id
                    )
                else:
                    # Обработка текста
                    result = await safe_execute(
                        handle_agent_request,
                        placeholder_message,
                        user_id,
                        update.message.text,
                        chat_state=None,
                        context="text_processing",
                        user_id=user_id,
                        chat_id=chat_id
                    )
                
                # Проверяем результат
                if isinstance(result, str) and result.startswith("❌"):
                    # Это сообщение об ошибке, не редактируем placeholder
                    return
                
                # Успешная обработка
                if result and not isinstance(result, str):
                    # Результат уже отправлен в handle_agent_request
                    pass
                else:
                    # Fallback сообщение
                    await placeholder_message.edit_text("✅ Обработка завершена!")
                    
        except Exception as e:
            logging.error(f"Error in task_wrapper: {e}")
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке. Попробуйте позже.")
    
    # Запускаем задачу в очереди
    from app.queue import task_queue
    await task_queue.add_task(
        user_id=user_id,
        task_type="message_processing",
        data={"message": update.message.text, "chat_id": chat_id},
        priority=1
    )

async def _process_media_group(media_group_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает группу изображений"""
    try:
        group_data = MEDIA_GROUPS[media_group_id]
        user_id = group_data['user_id']
        chat_id = group_data['chat_id']
        photos = group_data['photos']
        
        if group_data['processed']:
            return
        
        group_data['processed'] = True
        
        # Отправляем сообщение о начале обработки
        placeholder_message = await update.message.reply_text(
            f"🖼️ Обрабатываю группу из {len(photos)} изображений..."
        )
        
        # Обрабатываем группу через agent
        from app.handlers.agent import handle_agent_request
        
        result = await safe_execute(
            handle_agent_request,
            placeholder_message,
            user_id,
            photos,
            chat_state=None,
            context="media_group_processing",
            user_id=user_id,
            chat_id=chat_id
        )
        
        # Очищаем группу
        del MEDIA_GROUPS[media_group_id]
        
    except Exception as e:
        logging.error(f"Error processing media group {media_group_id}: {e}")
        # Очищаем группу при ошибке
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]

async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает загруженные документы"""
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Проверяем режим документов
        chat_state = await db.get_chat_state(chat_id)
        if not chat_state or not chat_state.document_mode:
            await update.message.reply_text(
                "📄 Для работы с документами сначала включите режим документов:\n\n"
                "🔄 *Для выхода из режима документов:*\n"
                "• Нажмите кнопку '❌ Отменить работу с документами'\n"
                "• Или отправьте команду /documents"
            )
            return
        
        # Обрабатываем документ
        result = await safe_execute(
            process_uploaded_document,
            update.message.document,
            user_id,
            chat_id,
            context="document_processing",
            user_id=user_id,
            chat_id=chat_id
        )
        
        if isinstance(result, str) and result.startswith("❌"):
            # Ошибка уже обработана
            return
        
        # Успешная обработка
        await update.message.reply_text("✅ Документ успешно обработан!")
        
    except Exception as e:
        logging.error(f"Error handling document: {e}")
        await update.message.reply_text("❌ Ошибка при обработке документа. Попробуйте позже.")

# Регистрация обработчиков
def register(application: Application):
    """Регистрирует обработчики сообщений"""
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request))
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
