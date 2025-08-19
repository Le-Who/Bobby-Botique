import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from ..config import settings
from google import genai
from .. import database as db
from ..utils.formatting import format_key_for_display, TelegramFormatter
from ..utils import time as time_utils
from ..metrics import metrics_collector
from ..cache import search_cache
from ..queue import task_queue
from ..group_chat import group_chat_manager

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"Start command from user {user_id}")
    
    if not await db.is_authorized(user_id):
        logging.warning(f"Unauthorized user {user_id} attempted to use /start command")
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    try:
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
        logging.info(f"Start command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in start command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подробную справку по использованию бота"""
    user_id = update.effective_user.id
    logging.info(f"Help command from user {user_id}")
    
    if not await db.is_authorized(user_id):
        logging.warning(f"Unauthorized user {user_id} attempted to use /help command")
        return
    
    try:
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
        logging.info(f"Help command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in help command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

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
        client = genai.Client(api_key=key_data['api_key'])
        models_list = [f"- `{m.name}`" for m in client.models.list() if 'generateContent' in m.supported_generation_methods]
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
                # Пропускаем записи, которые содержат имена файлов (это ошибки в логике)
                if not any(char in model for char in ['/', '\\', '.pdf', '.docx', '.doc']):
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
                requests = daily_data.get('requests', 0)
                errors = daily_data.get('errors', 0)
                text += f"• {date_str}: {requests} запросов, {errors} ошибок\n"
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
            f"Всего записей: `