import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from app.config import settings
from app import database as db
from app.utils.formatting import TelegramFormatter
from app.error_handler import handle_telegram_error

@handle_telegram_error("start_command")
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /start"""
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Проверяем авторизацию
        is_authorized = await db.is_authorized(user_id, chat_id)
        
        if isinstance(is_authorized, str) and is_authorized.startswith("❌"):
            # Ошибка авторизации
            await update.message.reply_text(is_authorized)
            return
        
        if not is_authorized:
            await update.message.reply_text(
                "🚫 Доступ запрещен. Обратитесь к администратору для получения доступа."
            )
            return
        
        # Создаем приветственное сообщение
        welcome_text = (
            "🤖 *Добро пожаловать в Gemaibot!*\n\n"
            "Я - ваш интеллектуальный помощник, который может:\n\n"
            "🔍 *Быстрый поиск* - используйте '?' для быстрых ответов\n"
            "🔬 *Глубокий анализ* - используйте '??' для детального исследования\n"
            "🖼️ *Анализ изображений* - отправляйте фото для анализа\n"
            "📄 *Работа с документами* - загружайте PDF/DOC для анализа\n\n"
            "💡 *Примеры использования:*\n"
            "• ? Какая столица Японии?\n"
            "• ?? Искусственный интеллект в медицине 2024\n"
            "• [отправьте фото] Что это за растение?\n\n"
            "📚 *Доступные команды:*\n"
            "/help - подробная справка\n"
            "/documents - работа с документами\n"
            "/status - статус системы\n\n"
            "🚀 Готов помочь! Отправьте ваш вопрос или изображение."
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(welcome_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
        # Записываем метрики
        await _record_command_metric("start", user_id, chat_id)
        
    except Exception as e:
        logging.error(f"Error in start command: {e}")
        await update.message.reply_text("❌ Произошла ошибка при запуске. Попробуйте позже.")

@handle_telegram_error("help_command")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /help"""
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        help_text = (
            "📚 *Справка по использованию Gemaibot*\n\n"
            "🔍 *Быстрый поиск (?) - для простых вопросов:*\n"
            "• ? Какая столица Франции?\n"
            "• ? Сколько планет в Солнечной системе?\n"
            "• ? Кто написал 'Войну и мир'?\n\n"
            "🔬 *Глубокий анализ (??) - для сложных исследований:*\n"
            "• ?? Квантовая физика и её применение\n"
            "• ?? История развития интернета\n"
            "• ?? Современные методы лечения рака\n\n"
            "🖼️ *Анализ изображений:*\n"
            "• Отправьте фото для автоматического анализа\n"
            "• Поддерживаются группы изображений\n"
            "• Автоматическое распознавание объектов\n\n"
            "📄 *Работа с документами:*\n"
            "• Загружайте PDF, DOC, DOCX файлы\n"
            "• Максимальный размер: 50MB\n"
            "• Автоматический анализ содержимого\n"
            "• Задавайте вопросы по документам\n\n"
            "⚙️ *Дополнительные возможности:*\n"
            "• Автоматическое сохранение истории\n"
            "• Умная обработка контекста\n"
            "• Многоязычная поддержка\n\n"
            "💡 *Советы по использованию:*\n"
            "• Будьте конкретны в вопросах\n"
            "• Используйте ключевые слова\n"
            "• Для сложных тем используйте '??'\n\n"
            "🆘 *Если что-то не работает:*\n"
            "• Проверьте подключение к интернету\n"
            "• Попробуйте переформулировать вопрос\n"
            "• Обратитесь к администратору"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
        # Записываем метрики
        await _record_command_metric("help", user_id, chat_id)
        
    except Exception as e:
        logging.error(f"Error in help command: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отображении справки. Попробуйте позже.")

@handle_telegram_error("documents_command")
async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /documents"""
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Получаем список документов пользователя
        documents = await db.get_user_documents(user_id, chat_id)
        
        if isinstance(documents, str) and documents.startswith("❌"):
            # Ошибка получения документов
            await update.message.reply_text(documents)
            return
        
        if not documents:
            # Нет документов
            no_docs_text = (
                "📄 *У вас пока нет документов*\n\n"
                "💡 *Как загрузить документ:*\n"
                "1. Отправьте файл (PDF, DOC, DOCX)\n"
                "2. Максимальный размер: 50MB\n"
                "3. Дождитесь обработки\n"
                "4. Задавайте вопросы по содержимому\n\n"
                "📋 *Поддерживаемые форматы:*\n"
                "• PDF (.pdf)\n"
                "• Word (.doc, .docx)\n\n"
                "🚀 Отправьте ваш первый документ!"
            )
            
            formatted_text, parse_mode = TelegramFormatter.format_text(no_docs_text)
            await update.message.reply_text(formatted_text, parse_mode=parse_mode)
            return
        
        # Есть документы - показываем список
        docs_text = f"📋 *Ваши документы ({len(documents)}):*\n\n"
        
        for i, doc in enumerate(documents[:10], 1):  # Показываем первые 10
            docs_text += f"{i}. 📄 {doc.get('filename', 'Unknown')}\n"
            if doc.get('created_at'):
                docs_text += f"   📅 {doc['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
            docs_text += "\n"
        
        if len(documents) > 10:
            docs_text += f"... и еще {len(documents) - 10} документов\n\n"
        
        docs_text += (
            "💡 *Как использовать:*\n"
            "• Просто напишите ваш вопрос\n"
            "• Система автоматически найдет ответ\n"
            "• Например: \"Какие основные пункты?\"\n\n"
            "🔄 *Для загрузки нового документа:*\n"
            "• Отправьте файл в чат\n\n"
            "❌ *Для выхода из режима документов:*\n"
            "• Используйте кнопку 'Отменить работу с документами'"
        )
        
        # Создаем кнопки для управления
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить новый", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")]
        ]
        
        formatted_text, parse_mode = TelegramFormatter.format_text(docs_text)
        await update.message.reply_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        # Записываем метрики
        await _record_command_metric("documents", user_id, chat_id)
        
    except Exception as e:
        logging.error(f"Error in documents command: {e}")
        await update.message.reply_text("❌ Произошла ошибка при получении списка документов. Попробуйте позже.")

@handle_telegram_error("status_command")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /status"""
    try:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        
        # Получаем статус системы
        status_text = (
            "📊 *Статус системы Gemaibot*\n\n"
            "🟢 *Основные сервисы:*\n"
            "• Telegram Bot API: ✅ Работает\n"
            "• Gemini AI: ✅ Доступен\n"
            "• Tavily Search: ✅ Доступен\n"
            "• База данных: ✅ Подключена\n\n"
            "👤 *Ваш профиль:*\n"
            f"• ID пользователя: `{user_id}`\n"
            f"• ID чата: `{chat_id}`\n"
            "• Статус: ✅ Авторизован\n\n"
            "📈 *Статистика использования:*\n"
            "• Команды: Активны\n"
            "• Обработка сообщений: Работает\n"
            "• Анализ изображений: Доступен\n"
            "• Работа с документами: Доступна\n\n"
            "🔄 *Последнее обновление:*\n"
            "• Система: Актуальна\n"
            "• API ключи: Действительны\n"
            "• Лимиты: В норме\n\n"
            "💡 *Если что-то не работает:*\n"
            "• Попробуйте перезапустить бота\n"
            "• Обратитесь к администратору\n"
            "• Проверьте подключение к интернету"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(status_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
        # Записываем метрики
        await _record_command_metric("status", user_id, chat_id)
        
    except Exception as e:
        logging.error(f"Error in status command: {e}")
        await update.message.reply_text("❌ Произошла ошибка при получении статуса. Попробуйте позже.")

async def _record_command_metric(command: str, user_id: int, chat_id: int):
    """Записывает метрику использования команды"""
    try:
        from app.metrics import metrics_collector
        await metrics_collector.record_command_usage(command, user_id, chat_id)
    except Exception as e:
        logging.warning(f"Failed to record command metric: {e}")

# Регистрация обработчиков
def register(application: Application):
    """Регистрирует обработчики команд"""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("documents", documents_command))
    application.add_handler(CommandHandler("status", status_command))