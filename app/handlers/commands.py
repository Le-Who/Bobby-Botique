import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from ..config import settings
from .. import database as db
from ..utils.formatting import format_key_for_display
from ..utils import time as time_utils
from ..services import genai
from ..metrics import metrics_collector
from ..cache import search_cache
from ..queue import task_queue
from ..group_chat import group_chat_manager

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return
    chat_state = await db.get_user_chat(user_id)
    search_status = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    prompt_status = f"`{chat_state.system_prompt[:100]}...`" if chat_state.system_prompt else "Не задан"
    from ..utils.messaging import send_formatted_message
    
    start_text = (
        "Привет! Я ваш личный ассистент.\n\n"
        f"Ваша основная модель для чата: {chat_state.model}\n"
        f"Системная инструкция: {prompt_status}\n"
        f"Режим исследования: {search_status}\n\n"
        "Как работает поиск:\n"
        "• ? вопрос - быстрый фактический ответ\n"
        "• ?? вопрос - глубокое исследование с анализом\n"
        "• ?? + картинка - поиск по картинке\n\n"
        "Команды:\n"
        "/res - вкл/выкл постоянный режим исследования\n"
        "/newchat - начать новый чат\n"
        "/setprompt [текст] - задать инструкцию\n"
        "/model - выбрать основную модель для чата\n\n"
        "Админ-команды:\n"
        "/keystatus, /credits, /listmodels, /adduser, /deluser, /listusers"
    )
    
    await send_formatted_message(
        update.message, 
        start_text,
        bold_parts=["Привет! Я ваш личный ассистент", "Как работает поиск", "Команды", "Админ-команды"]
    )

async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    if not context.args:
        chat_state.system_prompt = None
    else:
        chat_state.system_prompt = " ".join(context.args)
    await db.update_user_chat(user_id, chat_state)
    from ..utils.messaging import send_simple_message
    await send_simple_message(update.message, "✅ Системная инструкция обновлена.")

async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    from ..utils.messaging import send_simple_message
    await send_simple_message(update.message, "Новый чат создан. История и системная инструкция сброшены.")

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
    from ..utils.messaging import send_formatted_message
    
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    await send_formatted_message(
        update.message,
        f"🌐 Постоянный режим исследования {status_text}.",
        bold_parts=[status_text]
    )

async def key_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    today_pacific = time_utils.get_pacific_date()
    all_keys = await db.db_query("SELECT * FROM api_keys")
    if not all_keys:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Нет ключей Gemini в базе данных.")
        return
    from ..utils.messaging import send_formatted_message
    
    status_lines = ["📊 Статус ключей Gemini на сегодня:\n"]
    for key_row in all_keys:
        display_name = format_key_for_display(key_row['api_key'])
        status_lines.append(f"🔑 Ключ {display_name}")
        usage_data = await db.db_query("SELECT model_name, request_count FROM key_usage WHERE key_hash = ? AND usage_date = ?", (key_row['key_hash'], today_pacific))
        if not usage_data:
            status_lines.append("  • Сегодня не использовался")
        else:
            for usage in usage_data:
                model_name = usage['model_name']
                count = usage['request_count']
                limit = settings.DAILY_LIMITS.get(model_name, 'N/A')
                status_lines.append(f"  • {model_name}: {count} / {limit}")
    status_lines.append(f"\nСброс лимитов произойдет в {time_utils.get_kyiv_reset_time()} по Киеву.")
    
    status_text = "\n".join(status_lines)
    await send_formatted_message(
        update.message,
        status_text,
        bold_parts=["📊 Статус ключей Gemini на сегодня"]
    )

async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    current_month = time_utils.get_current_month_str()
    all_keys = await db.db_query("SELECT * FROM tavily_api_keys")
    if not all_keys:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Нет ключей Tavily в базе данных.")
        return
    from ..utils.messaging import send_formatted_message
    
    status_lines = [f"📊 Расход кредитов Tavily на {current_month}:\n"]
    for key_row in all_keys:
        display_name = format_key_for_display(key_row['api_key'])
        usage = await db.db_query("SELECT credit_usage FROM tavily_key_usage WHERE key_hash = ? AND usage_month = ?", (key_row['key_hash'], current_month))
        count = usage[0]['credit_usage'] if usage else 0
        limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
        status_lines.append(f"🔑 Ключ {display_name}: {count} / {limit}")
    status_lines.append(f"\nЛимиты сбрасываются 1-го числа каждого месяца.")
    
    status_text = "\n".join(status_lines)
    await send_formatted_message(
        update.message,
        status_text,
        bold_parts=[f"📊 Расход кредитов Tavily на {current_month}"]
    )

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    key_data = await db.get_available_gemini_key(settings.DEFAULT_MODEL)
    if not key_data:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Нет доступных API ключей для выполнения запроса.")
        return
    from ..utils.messaging import send_simple_message
    await send_simple_message(update.message, "Запрашиваю список моделей у Google API...")
    try:
        genai.configure(api_key=key_data['api_key'])
        from ..utils.messaging import send_list_message
        
        models_list = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        await send_list_message(update.message, "✅ Доступные модели:", models_list)
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка: {e}")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_add = int(context.args[0])
        await db.db_query("INSERT INTO users (user_id, is_authorized) VALUES (?, 1) ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1", (user_to_add,))
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Использование: /adduser <user_id>")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_del = int(context.args[0])
        if user_to_del == settings.ADMIN_ID:
            from ..utils.messaging import send_simple_message
            await send_simple_message(update.message, "Нельзя удалить администратора.")
            return
        await db.db_query("UPDATE users SET is_authorized = 0 WHERE user_id = ?", (user_to_del,))
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Использование: /deluser <user_id>")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id): return
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    user_ids = [str(row['user_id']) for row in rows]
    from ..utils.messaging import send_simple_message
    await send_simple_message(update.message, "Авторизованные пользователи:\n" + "\n".join(user_ids))

async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает метрики производительности"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        metrics = await metrics_collector.get_metrics_summary()
        
        from ..utils.messaging import send_formatted_message
        
        text = (
            "📊 Метрики производительности:\n\n"
            f"Всего запросов: {metrics['total_requests']}\n"
            f"Среднее время ответа: {metrics['average_response_time']:.2f}s\n"
            f"Процент ошибок: {metrics['error_rate']:.1f}%\n"
            f"Попадания в кэш: {metrics['cache_hit_rate']:.1f}%\n"
            f"Поисковых запросов: {metrics['search_queries']}\n\n"
            "Использование API:\n"
        )
        
        for api, count in metrics['api_calls'].items():
            text += f"• {api}: {count}\n"
        
        text += "\nИспользование моделей:\n"
        for model, count in metrics['model_usage'].items():
            text += f"• {model}: {count}\n"
        
        # Добавляем историю за последние дни
        if metrics['daily_metrics']:
            text += "\nИстория за последние дни:\n"
            for date_str, daily_data in list(metrics['daily_metrics'].items())[:7]:  # Последние 7 дней
                text += f"• {date_str}: {daily_data['requests']} запросов, {daily_data['errors']} ошибок\n"
        
        # Добавляем последние ошибки
        if metrics['recent_errors']:
            text += "\nПоследние ошибки:\n"
            for error in metrics['recent_errors'][:5]:  # Последние 5 ошибок
                text += f"• {error['type']}: {error['message'][:50]}...\n"
        
        await send_formatted_message(
            update.message,
            text,
            bold_parts=["📊 Метрики производительности"]
        )
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка получения метрик: {e}")

async def cache_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику кэша"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await search_cache.get_stats()
        
        from ..utils.messaging import send_formatted_message
        
        text = (
            "🗄️ Статистика кэша:\n\n"
            f"Всего записей: {stats['total_entries']}\n"
            f"Максимальный размер: {stats['max_size']}\n"
            f"Попадания в кэш: {stats['cache_hit_rate']:.1f}%\n"
            f"Среднее количество обращений: {stats['avg_access_count']:.1f}\n"
        )
        
        await send_formatted_message(
            update.message,
            text,
            bold_parts=["🗄️ Статистика кэша"]
        )
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка получения статистики кэша: {e}")

async def queue_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику очереди задач"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await task_queue.get_queue_stats()
        
        from ..utils.messaging import send_formatted_message
        
        text = (
            "📋 Статистика очереди задач:\n\n"
            f"Всего задач: {stats['total_tasks']}\n"
            f"В ожидании: {stats['pending_tasks']}\n"
            f"Выполняется: {stats['running_tasks']}\n"
            f"Завершено: {stats['completed_tasks']}\n"
            f"Ошибок: {stats['failed_tasks']}\n"
            f"Размер очереди: {stats['queue_size']}\n"
            f"Активных воркеров: {stats['active_workers']}\n"
        )
        
        await send_formatted_message(
            update.message,
            text,
            bold_parts=["📋 Статистика очереди задач"]
        )
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка получения статистики очереди: {e}")

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает кэш"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        await search_cache.clear()
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "✅ Кэш очищен.")
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка очистки кэша: {e}")

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
        
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "✅ Старые метрики очищены (старше 30 дней).")
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка очистки метрик: {e}")

async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Регистрирует групповой чат"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    chat = update.effective_chat
    if chat.type == 'private':
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Эта команда работает только в групповых чатах.")
        return
    
    try:
        success = await group_chat_manager.register_group(chat.id, chat.title, user_id)
        if success:
            from ..utils.messaging import send_simple_message
            await send_simple_message(update.message, f"✅ Группа '{chat.title}' зарегистрирована!")
        else:
            from ..utils.messaging import send_simple_message
            await send_simple_message(update.message, "❌ Группа уже зарегистрирована или произошла ошибка.")
            
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка регистрации группы: {e}")

async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику группы"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    chat = update.effective_chat
    if chat.type == 'private':
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, "Эта команда работает только в групповых чатах.")
        return
    
    try:
        stats = await group_chat_manager.get_group_stats(chat.id)
        
        from ..utils.messaging import send_simple_message
        
        text = (
            f"📊 Статистика группы '{chat.title}':\n\n"
            f"Всего сообщений: {stats['total_messages']}\n"
            f"Сообщений за 24ч: {stats['recent_messages']}\n"
            f"Активных пользователей за 24ч: {stats['active_users_24h']}\n"
            f"Участников: {stats['member_count']}\n"
        )
        
        await send_simple_message(update.message, text)
        
    except Exception as e:
        from ..utils.messaging import send_simple_message
        await send_simple_message(update.message, f"Ошибка получения статистики группы: {e}")

def register(application: Application):
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newchat", new_chat_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("setprompt", set_prompt_command))
    application.add_handler(CommandHandler("res", research_mode_command))
    application.add_handler(CommandHandler("keystatus", key_status_command))
    application.add_handler(CommandHandler("credits", credits_command))
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
