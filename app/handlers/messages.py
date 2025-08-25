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

# Глобальный словарь для хранения групп изображений
MEDIA_GROUPS = {}

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
    
    # Если это изображение с media_group_id, группируем его
    if is_photo and media_group_id:
        logging.info(f"📸 Получено изображение из группы {media_group_id} от пользователя {user_id}")
        
        # Инициализируем группу, если её нет
        if media_group_id not in MEDIA_GROUPS:
            MEDIA_GROUPS[media_group_id] = {
                'user_id': user_id,
                'chat_id': chat_id,
                'messages': [],
                'caption': update.message.caption,
                'created_at': asyncio.get_event_loop().time(),
                'placeholder_message': None
            }
        
        # Добавляем сообщение в группу
        MEDIA_GROUPS[media_group_id]['messages'].append(update.message)
        
        # Если это первое сообщение группы, создаем placeholder
        if len(MEDIA_GROUPS[media_group_id]['messages']) == 1:
            placeholder_message = await update.message.reply_text("🖼️ Обрабатываю группу изображений...")
            MEDIA_GROUPS[media_group_id]['placeholder_message'] = placeholder_message
            logging.info(f"📸 Создан placeholder для группы {media_group_id}")
        
        # Ждем немного, чтобы собрать все изображения группы
        await asyncio.sleep(0.5)
        
        # Проверяем, все ли изображения группы получены
        # Если прошло больше 2 секунд с создания группы, обрабатываем её
        current_time = asyncio.get_event_loop().time()
        group_age = current_time - MEDIA_GROUPS[media_group_id]['created_at']
        
        if group_age >= 2.0:  # Ждем 2 секунды для сбора всех изображений
            await process_media_group(media_group_id, context)
        
        return  # Выходим, не обрабатывая отдельно
    
    # Детальное логирование Telegram API запроса
    message_type = "photo" if update.message.photo else "text" if update.message.text else "other"
    start_time = api_logger.log_telegram_request(
        method="handle_message",
        chat_id=chat_id,
        user_id=user_id,
        message_type=message_type
    )
    
    # Валидация текста сообщения
    message_text = update.message.text if update.message and update.message.text else 'No text'
    if len(message_text) > settings.TELEGRAM_MESSAGE_LIMIT:
        logging.warning("Message too long from user %s: %d chars", user_id, len(message_text))
        await update.message.reply_text("❌ Сообщение слишком длинное. Максимум 4096 символов.")
        return
    
    logging.info("Received message from user %s: %s", user_id, message_text[:100])
    
    if not await db.is_authorized(user_id):
        logging.warning("Unauthorized user %s attempted to use bot", user_id)
        return
    
    # Обрабатываем документы
    if update.message.document:
        logging.info("Processing document from user %s: %s", user_id, update.message.document.file_name)
        await handle_document(update, context)
        return
    
    # Проверяем, находится ли пользователь в режиме работы с документами
    from app.state import is_in_document_mode, get_selected_document_id
    
    if is_in_document_mode(user_id):
        document_id = get_selected_document_id(user_id)
        logging.info("User %s is in document mode, document_id: %s", user_id, document_id)
        if document_id:
            await handle_document_question(update, context, document_id)
        else:
            await update.message.reply_text(
                "📋 Вы находитесь в режиме работы с документами.\n\n"
                "💡 *Доступные действия:*\n"
                "• Загрузите новый документ\n"
                "• Выберите документ из списка\n"
                "• Используйте кнопки под сообщениями\n\n"
                "🔄 *Для выхода из режима документов:*\n"
                "• Нажмите кнопку '❌ Отменить работу с документами'\n"
                "• Или отправьте команду /documents"
            )
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
                try:
                    from app.handlers.agent import process_long_request
                    await process_long_request(placeholder_message, update, context)
                except ImportError:
                    # Fallback если agent недоступен
                    await placeholder_message.edit_text("🤔 Обрабатываю ваш запрос... (упрощенный режим)")
                    
                logging.info("Completed task processing for user %s", user_id)
                
                # Логируем успешный ответ Telegram API
                api_logger.log_telegram_response(
                    start_time=start_time,
                    method="handle_message",
                    success=True,
                    chat_id=chat_id,
                    user_id=user_id
                )
                
        except Exception as e:
            logging.error(f"Error in task wrapper for user {user_id}: {e}", exc_info=True)
            try:
                await placeholder_message.edit_text("❌ Произошла ошибка при обработке запроса.")
            except Exception as edit_error:
                logging.error(f"Could not edit placeholder message: {edit_error}")
            
            # Логируем ошибку Telegram API
            api_logger.log_telegram_response(
                start_time=start_time,
                method="handle_message",
                success=False,
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
    
    # Запускаем обработку в фоне
    asyncio.create_task(task_wrapper())

async def process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает группу изображений как единое целое"""
    if media_group_id not in MEDIA_GROUPS:
        logging.error(f"Media group {media_group_id} not found")
        return
    
    group_data = MEDIA_GROUPS[media_group_id]
    user_id = group_data['user_id']
    chat_id = group_data['chat_id']
    messages = group_data['messages']
    caption = group_data['caption']
    placeholder_message = group_data['placeholder_message']
    
    logging.info(f"🔄 Обрабатываю группу изображений {media_group_id}: {len(messages)} изображений")
    
    try:
        async with state.get_user_lock(user_id):
            # Создаем мок Update для совместимости с существующим кодом
            mock_update = type('MockUpdate', (), {
                'message': messages[0],  # Используем первое сообщение как основное
                'effective_user': messages[0].from_user,
                'effective_chat': messages[0].chat
            })()
            
            # Обрабатываем группу через agent
            from app.handlers.agent import process_media_group_request
            await process_media_group_request(placeholder_message, mock_update, context, messages, caption)
            
    except Exception as e:
        logging.error(f"Error processing media group {media_group_id}: {e}", exc_info=True)
        try:
            await placeholder_message.edit_text("❌ Произошла ошибка при обработке группы изображений.")
        except Exception as edit_error:
            logging.error(f"Could not edit placeholder message: {edit_error}")
    
    finally:
        # Очищаем группу из памяти
        if media_group_id in MEDIA_GROUPS:
            del MEDIA_GROUPS[media_group_id]
            logging.info(f"🧹 Очищена группа изображений {media_group_id}")

async def handle_document_question(update: Update, context: ContextTypes.DEFAULT_TYPE, document_id: int):
    """Обрабатывает вопрос по конкретному документу"""
    user_id = update.effective_user.id
    user_message = update.message.text
    
    try:
        from app.document_processor import get_document_content, get_document_by_id
        
        # Получаем информацию о документе
        document = await get_document_by_id(document_id, user_id)
        if not document:
            await update.message.reply_text("❌ Документ не найден.")
            from app.state import clear_document_state
            clear_document_state(user_id)
            return
        
        # Получаем содержимое документа
        document_content = await get_document_content(document_id, user_id)
        if not document_content:
            await update.message.reply_text("❌ Не удалось получить содержимое документа.")
            return
        
        # Обрабатываем вопрос через AI
        from app.handlers.agent import _handle_document_question
        from app import database as db
        
        chat_state = await db.get_user_chat(user_id)
        
        # Передаем оригинальное сообщение пользователя как placeholder
        # _handle_document_question сама создаст нужное сообщение
        await _handle_document_question(update.message, user_id, user_message, chat_state)
        
    except Exception as e:
        logging.error(f"Error handling document question: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка при обработке вопроса: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает загруженные документы"""
    user_id = update.effective_user.id
    
    # Проверяем, что это действительно документ, а не изображение
    if not update.message.document:
        return  # Если это не документ, просто выходим
    
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
                
                # Правильно обрабатываем datetime
                created_date = duplicate_info.get('created_at', 'Unknown')
                if hasattr(created_date, 'strftime'):
                    # Это объект datetime
                    date_str = created_date.strftime('%Y-%m-%d')
                else:
                    # Это строка
                    date_str = str(created_date)[:10] if created_date != 'Unknown' else 'Unknown'
                
                duplicate_text = (
                    f"⚠️ *Файл уже загружен*\n\n"
                    f"Файл `{document.file_name}` уже был загружен ранее как:\n"
                    f"📄 *{duplicate_info.get('filename', 'Unknown')}*\n"
                    f"📅 Загружен: {date_str}\n\n"
                    f"Хотите использовать существующий документ?"
                )
                
                # Создаем кнопки для работы с дубликатом
                keyboard = [
                    [InlineKeyboardButton("✅ Использовать существующий", callback_data=f"doc:use_existing:{duplicate_info.get('id')}")],
                    [InlineKeyboardButton("📄 Загрузить как новый", callback_data="doc:force_upload")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")]
                ]
                
                formatted_text, parse_mode = TelegramFormatter.format_text(duplicate_text)
                await processing_msg.edit_text(
                    formatted_text,
                    parse_mode=parse_mode,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            else:
                await processing_msg.edit_text(f"❌ Ошибка обработки: {result['error']}")
                return
        
        # Получаем статистику пользователя для отображения лимитов
        from app.document_processor import document_processor
        user_stats = await document_processor.get_user_document_stats(user_id)
        
        # Отправляем результат
        success_text = (
            f"✅ Документ обработан успешно!\n\n"
            f"📄 *{document.file_name}*\n"
            f"📊 Страниц: {result.get('pages', 'N/A')}\n"
            f"📝 Символов: {result.get('text_length', 0):,}\n"
        )
        
        if result.get('paragraphs'):
            success_text += f"📄 Параграфов: {result['paragraphs']}\n"
        if result.get('tables'):
            success_text += f"📊 Таблиц: {result['tables']}\n"
        
        success_text += f"\n📋 *Ваши документы:* {user_stats['document_count']}/5\n"
        if user_stats['limit_reached']:
            success_text += "⚠️ Достигнут лимит документов (5). Старые документы будут автоматически удалены.\n"
        
        success_text += "\n💡 *Как задавать вопросы:*\n• Просто напишите ваш вопрос\n• Например: \"Какие основные пункты?\", \"Что говорится о...?\"\n• Система автоматически найдет ответ в документе\n\n"
        success_text += "📅 *Срок хранения:* 3 дня (автоматическая очистка)"
        
        # Создаем кнопки для управления документом
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить другой документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")]
        ]
        
        formatted_text, parse_mode = TelegramFormatter.format_text(success_text)
        await processing_msg.edit_text(
            formatted_text, 
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Устанавливаем состояние работы с документами
        from app.state import set_document_mode
        set_document_mode(user_id, True)
        
        # Записываем метрики
        await metrics_collector.record_api_call("document_processing")
        
    except Exception as e:
        error_msg = f"❌ Произошла ошибка при обработке документа: {str(e)[:100]}"
        logging.error(f"Error processing document for user {user_id}: {e}", exc_info=True)
        await processing_msg.edit_text(error_msg)
        await metrics_collector.record_error("document_processing", str(e))

def register(application: Application):
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request))
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
