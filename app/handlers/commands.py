import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from ..config import settings
from .. import database as db
from ..utils.formatting import format_key_for_display, TelegramFormatter
from ..utils import time as time_utils
from ..services import genai
from ..metrics import metrics_collector
from ..cache import search_cache
from ..queue import task_queue
from ..group_chat import group_chat_manager

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    chat_state = await db.get_user_chat(user_id)
    search_status = "🟢 ВКЛЮЧЕН" if chat_state.search_enabled else "🔴 ВЫКЛЮЧЕН"
    prompt_status = f"`{chat_state.system_prompt[:50]}...`" if chat_state.system_prompt else "Не задана"
    
    start_text = (
        "🤖 **Добро пожаловать в Gemini Bot!**\n\n"
        "Я ваш умный ассистент с возможностями:\n"
        "• 💬 Обычный чат с AI\n"
        "• 🔍 Веб-поиск и анализ\n"
        "• 🖼️ Поиск по изображениям\n"
        "• 📄 Обработка документов\n\n"
        "**📊 Ваши настройки:**\n"
        f"• Модель: `{chat_state.model}`\n"
        f"• Поиск: {search_status}\n"
        f"• Инструкция: {prompt_status}\n\n"
        "**🚀 Быстрый старт:**\n"
        "• Просто напишите сообщение для чата\n"
        "• `? вопрос` — быстрый ответ\n"
        "• `?? вопрос` — глубокий анализ\n"
        "• Отправьте фото для анализа\n\n"
        "**⚙️ Основные команды:**\n"
        "• `/help` — подробная справка\n"
        "• `/res` — режим поиска вкл/выкл\n"
        "• `/newchat` — новый чат\n"
        "• `/model` — выбрать модель\n"
        "• `/setprompt` — задать инструкцию\n"
        "• `/documents` — управление документами\n"
        "• `/metrics` — статистика системы\n\n"
        "**💡 Совет:** Начните с простого вопроса!"
    )
    
    formatted_text, parse_mode = TelegramFormatter.format_text(start_text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подробную справку по использованию бота"""
    if not await db.is_authorized(update.effective_user.id):
        return
    
    help_text = (
        "📚 **Подробная справка по Gemini Bot**\n\n"
        "**💬 Обычный чат:**\n"
        "Просто напишите сообщение для общения с AI\n\n"
        "**🔍 Поиск и анализ:**\n"
        "• `? вопрос` — быстрый фактический ответ\n"
        "• `?? вопрос` — глубокое исследование с источниками\n"
        "• `??` + фото — поиск по изображению\n\n"
        "**📄 Работа с документами:**\n"
        "• Отправьте PDF или DOCX файл\n"
        "• Задавайте вопросы по содержимому\n"
        "• `/documents` — управление документами\n\n"
        "**⚙️ Настройки:**\n"
        "• `/model` — выбор AI модели\n"
        "• `/setprompt` — системная инструкция\n"
        "• `/res` — режим поиска вкл/выкл\n"
        "• `/newchat` — новый чат\n\n"
        "**📊 Статистика:**\n"
        "• `/metrics` — полная сводка (метрики, ключи, кредиты)\n\n"
        "**💡 Советы:**\n"
        "• Используйте `?` для быстрых фактов\n"
        "• `??` для глубокого анализа\n"
        "• Фото + текст для анализа изображений"
    )
    
    formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)

async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    if not context.args:
        chat_state.system_prompt = None
    else:
        chat_state.system_prompt = " ".join(context.args)
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("✅ Системная инструкция обновлена.")

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("Новый чат создан. История и системная инструкция сброшены.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await db.is_authorized(update.effective_user.id): return
    keyboard = [[InlineKeyboardButton(m, callback_data=f"model_{m}")] for m in settings.AVAILABLE_MODELS]
    await update.message.reply_text("Выберите основную модель для разговора:", reply_markup=InlineKeyboardMarkup(keyboard))

async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"🌐 Постоянный режим исследования **{status_text}**.", parse_mode='Markdown')

# Команды /keystatus и /credits объединены с /metrics

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    key_data = await db.get_available_gemini_key(settings.DEFAULT_MODEL)
    if not key_data:
        await update.message.reply_text("Нет доступных API ключей для выполнения запроса.")
        return
    await update.message.reply_text("Запрашиваю список моделей у Google API...")
    try:
        genai.configure(api_key=key_data['api_key'])
        models_list = [f"- `{m.name}`" for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        await update.message.reply_text("✅ **Доступные модели:**\n" + "\n".join(models_list), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_add = int(context.args[0])
        await db.db_query("INSERT INTO users (user_id, is_authorized) VALUES (?, 1) ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1", (user_to_add,))
        await update.message.reply_text(f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /adduser <user_id>")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_del = int(context.args[0])
        if user_to_del == settings.ADMIN_ID:
            await update.message.reply_text("Нельзя удалить администратора.")
            return
        await db.db_query("UPDATE users SET is_authorized = 0 WHERE user_id = ?", (user_to_del,))
        await update.message.reply_text(f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /deluser <user_id>")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    user_ids = [str(row['user_id']) for row in rows]
    await update.message.reply_text("Авторизованные пользователи:\n" + "\n".join(user_ids))

async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает полную сводку метрик, статуса ключей и кредитов"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        # Получаем метрики производительности
        metrics = await metrics_collector.get_metrics_summary()
        
        # Получаем статус ключей Gemini
        today_pacific = time_utils.get_pacific_date()
        gemini_keys = await db.db_query("SELECT * FROM api_keys")
        
        # Получаем статус кредитов Tavily
        current_month = time_utils.get_current_month_str()
        tavily_keys = await db.db_query("SELECT * FROM tavily_api_keys")
        
        # Формируем основной текст
        text = (
            "📊 **Полная сводка системы:**\n\n"
            "**🚀 Производительность:**\n"
            f"• Всего запросов: `{metrics['total_requests']}`\n"
            f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
            f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
            f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
            f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
        )
        
        # Добавляем использование API и моделей
        if metrics['api_calls']:
            text += "**🔌 Использование API:**\n"
            for api, count in metrics['api_calls'].items():
                text += f"• {api}: `{count}`\n"
            text += "\n"
        
        if metrics['model_usage']:
            text += "**🤖 Использование моделей:**\n"
            for model, count in metrics['model_usage'].items():
                text += f"• {model}: `{count}`\n"
            text += "\n"
        
        # Добавляем статус ключей Gemini
        if gemini_keys:
            text += "**🔑 Статус ключей Gemini (сегодня):**\n"
            for key_row in gemini_keys:
                display_name = format_key_for_display(key_row['api_key'])
                usage_data = await db.db_query(
                    "SELECT model_name, request_count FROM key_usage WHERE key_hash = ? AND usage_date = ?", 
                    (key_row['key_hash'], today_pacific)
                )
                if not usage_data:
                    text += f"• `{display_name}`: не использовался\n"
                else:
                    for usage in usage_data:
                        model_name = usage['model_name']
                        count = usage['request_count']
                        limit = settings.DAILY_LIMITS.get(model_name, 'N/A')
                        text += f"• `{display_name}` ({model_name}): {count} / {limit}\n"
            text += f"Сброс лимитов: **{time_utils.get_kyiv_reset_time()}** по Киеву\n\n"
        
        # Добавляем статус кредитов Tavily
        if tavily_keys:
            text += "**💳 Кредиты Tavily (текущий месяц):**\n"
            for key_row in tavily_keys:
                display_name = format_key_for_display(key_row['api_key'])
                usage = await db.db_query(
                    "SELECT credit_usage FROM tavily_key_usage WHERE key_hash = ? AND usage_month = ?", 
                    (key_row['key_hash'], current_month)
                )
                count = usage[0]['credit_usage'] if usage else 0
                limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
                text += f"• `{display_name}`: {count} / {limit}\n"
            text += "Сброс лимитов: 1-го числа каждого месяца\n\n"
        
        # Добавляем историю за последние дни
        if metrics['daily_metrics']:
            text += "**📈 История за последние дни:**\n"
            for date_str, daily_data in list(metrics['daily_metrics'].items())[:5]:  # Последние 5 дней
                text += f"• {date_str}: {daily_data['requests']} запросов, {daily_data['errors']} ошибок\n"
            text += "\n"
        
        # Добавляем последние ошибки
        if metrics['recent_errors']:
            text += "**⚠️ Последние ошибки:**\n"
            for error in metrics['recent_errors'][:3]:  # Последние 3 ошибки
                text += f"• {error['type']}: {error['message'][:40]}...\n"
        
        # Используем TelegramFormatter для надежного форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения метрик: {e}")
        logging.error(f"Error in metrics command: {e}", exc_info=True)

async def cache_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику кэша"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await search_cache.get_stats()
        
        text = (
            "🗄️ **Статистика кэша:**\n\n"
            f"Всего записей: `{stats['total_entries']}`\n"
            f"Максимальный размер: `{stats['max_size']}`\n"
            f"Попадания в кэш: `{stats['cache_hit_rate']:.1f}%`\n"
            f"Среднее количество обращений: `{stats['avg_access_count']:.1f}`\n"
        )
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики кэша: {e}")

async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список документов пользователя и управляет ими"""
    if not await db.is_authorized(update.effective_user.id):
        return
    
    try:
        from ..document_processor import get_user_documents
        
        documents = await get_user_documents(update.effective_user.id)
        
        if not documents:
            text = (
                "📋 **Ваши документы**\n\n"
                "У вас пока нет загруженных документов.\n\n"
                "💡 **Как загрузить документ:**\n"
                "• Отправьте PDF или DOCX файл\n"
                "• Максимальный размер: 50MB\n"
                "• После загрузки вы сможете задавать вопросы по содержимому"
            )
        else:
            text = "📋 **Ваши документы:**\n\n"
            
            for i, doc in enumerate(documents[:10], 1):  # Показываем только первые 10
                text += f"{i}. **{doc['filename']}**\n"
                text += f"   📄 Страниц: {doc['pages']}\n"
                text += f"   📅 Загружен: {doc['created_at'][:10]}\n"
                text += f"   📊 Размер: {doc['file_size']:,} символов\n\n"
            
            if len(documents) > 10:
                text += f"... и еще {len(documents) - 10} документов\n\n"
            
            text += (
                "💡 **Действия:**\n"
                "• Отправьте новый документ для загрузки\n"
                "• Задайте вопрос по последнему документу\n"
                "• Используйте кнопки под сообщениями для управления"
            )
        
        # Создаем кнопки для управления
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить новый документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("🗑️ Очистить все документы", callback_data="doc:clear_all")]
        ]
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(
            formatted_text, 
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка получения документов: {e}")
        logging.error(f"Error in documents command: {e}", exc_info=True)

async def queue_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику очереди задач"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await task_queue.get_queue_stats()
        
        text = (
            "📋 **Статистика очереди задач:**\n\n"
            f"Всего задач: `{stats['total_tasks']}`\n"
            f"В ожидании: `{stats['pending_tasks']}`\n"
            f"Выполняется: `{stats['running_tasks']}`\n"
            f"Завершено: `{stats['completed_tasks']}`\n"
            f"Ошибок: `{stats['failed_tasks']}`\n"
            f"Размер очереди: `{stats['queue_size']}`\n"
            f"Активных воркеров: `{stats['active_workers']}`\n"
        )
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики очереди: {e}")

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает кэш"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        await search_cache.clear()
        await update.message.reply_text("✅ Кэш очищен.")
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки кэша: {e}")

async def clear_old_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает старые метрики (старше 30 дней)"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        # Удаляем метрики старше 30 дней
        result = await db.db_query("""
            DELETE FROM metrics 
            WHERE metric_date < CURRENT_DATE - INTERVAL '30 days'
        """)
        
        # Удаляем старые ошибки (старше 7 дней)
        await db.db_query("""
            DELETE FROM error_logs 
            WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
        """)
        
        await update.message.reply_text("✅ Старые метрики очищены (старше 30 дней).")
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки метрик: {e}")

async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрирует групповой чат"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("Эта команда работает только в групповых чатах.")
        return
    
    try:
        success = await group_chat_manager.register_group(chat.id, chat.title, user_id)
        if success:
            await update.message.reply_text(f"✅ Группа '{chat.title}' зарегистрирована!")
        else:
            await update.message.reply_text("❌ Группа уже зарегистрирована или произошла ошибка.")
            
    except Exception as e:
        await update.message.reply_text(f"Ошибка регистрации группы: {e}")

async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику группы"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    chat = update.effective_chat
    if chat.type == 'private':
        await update.message.reply_text("Эта команда работает только в групповых чатах.")
        return
    
    try:
        stats = await group_chat_manager.get_group_stats(chat.id)
        
        text = (
            f"📊 **Статистика группы '{chat.title}':**\n\n"
            f"Всего сообщений: `{stats['total_messages']}`\n"
            f"Сообщений за 24ч: `{stats['recent_messages']}`\n"
            f"Активных пользователей за 24ч: `{stats['active_users_24h']}`\n"
            f"Участников: `{stats['member_count']}`\n"
        )
        
        await update.message.reply_text(text, parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики группы: {e}")

def register(application: Application):
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("newchat", new_chat_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("setprompt", set_prompt_command))
    application.add_handler(CommandHandler("res", research_mode_command))
    # Команды /keystatus и /credits объединены с /metrics
    application.add_handler(CommandHandler("listmodels", list_models_command))
    application.add_handler(CommandHandler("adduser", add_user_command))
    application.add_handler(CommandHandler("deluser", del_user_command))
    application.add_handler(CommandHandler("listusers", list_users_command))
    
    # Новые команды для мониторинга и управления
    application.add_handler(CommandHandler("metrics", metrics_command))
    application.add_handler(CommandHandler("cachestats", cache_stats_command))
    application.add_handler(CommandHandler("queuestats", queue_stats_command))
    application.add_handler(CommandHandler("clearcache", clear_cache_command))
    application.add_handler(CommandHandler("clearoldmetrics", clear_old_metrics_command))
    
    # Команды для групповых чатов
    application.add_handler(CommandHandler("registergroup", register_group_command))
    application.add_handler(CommandHandler("groupstats", group_stats_command))
    
    # Команды для работы с документами
    application.add_handler(CommandHandler("documents", documents_command))
