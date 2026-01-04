import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from app.config import settings, get_model_hash
from google import genai
from app import database as db
from app.utils.formatting import format_key_for_display, TelegramFormatter
from app.utils import time as time_utils
from app.metrics import metrics_collector
from app.cache import get_cache_stats
from app.queue import task_queue
from app.group_chat import group_chat_manager
from app import prompts
from app.metrics import role_conv_metrics

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
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
            "🤖 *Добро пожаловать в Gemini Bot!*\n\n"
            "Я ваш умный ассистент с возможностями:\n"
            "• 💬 Обычный чат с AI\n"
            "• 🔍 Веб-поиск и анализ\n"
            "• 🖼️ Поиск по изображениям\n"
            "• 📄 Обработка документов\n\n"
            "*📊 Ваши настройки:*\n"
            f"• Модель: `{chat_state.model}`\n"
            f"• Поиск: {search_status}\n"
            f"• Инструкция: {prompt_status}\n\n"
            "*🚀 Быстрый старт:*\n"
            "• Просто напишите сообщение для чата\n"
            "• `? вопрос` — быстрый ответ\n"
            "• `?? вопрос` — глубокий анализ\n"
            "• Отправьте фото для анализа\n\n"
            "*⚙️ Основные команды:*\n"
            "• `/help` — подробная справка\n"
            "• `/res` — режим поиска вкл/выкл\n"
            "• `/newchat` — новый чат\n"
            "• `/model` — выбрать модель\n"
            "• `/setprompt` — задать инструкцию\n"
            "• `/documents` — управление документами\n"
            "• `/metrics` — статистика системы\n"
            "• `/roles` — выбор ролей и создание своей\n\n"
            "*💡 Совет:* Начните с простого вопроса!"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(start_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        logging.info(f"Start command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in start command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает подробную справку по использованию бота"""
    user_id = update.effective_user.id
    logging.info(f"Help command from user {user_id}")
    
    if not await db.is_authorized(user_id):
        logging.warning(f"Unauthorized user {user_id} attempted to use /help command")
        return
    
    try:
        help_text = (
            "📚 *Подробная справка по Gemini Bot*\n\n"
            "*💬 Обычный чат:*\n"
            "Просто напишите сообщение для общения с AI\n\n"
            "*🔍 Поиск и анализ:*\n"
            "• `? вопрос` — быстрый фактический ответ\n"
            "• `?? вопрос` — глубокое исследование с источниками\n"
            "• `??` + фото — поиск по изображению\n\n"
            "*📄 Работа с документами:*\n"
            "• Отправьте PDF или DOCX файл\n"
            "• Задавайте вопросы по содержимому\n"
            "• `/documents` — управление документами\n\n"
            "*⚙️ Настройки:*\n"
            "• `/model` — выбор AI модели\n"
            "• `/setprompt` — системная инструкция\n"
            "• `/res` — режим поиска вкл/выкл\n"
            "• `/newchat` — новый чат\n\n"
            "*📊 Статистика:*\n"
            "• `/metrics` — полная сводка (метрики, ключи, кредиты)\n\n"
            "*🧩 Роли:*\n"
            "• `/roles` — выбрать предустановленную роль или создать свою\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        logging.info(f"Help command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in help command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    if not context.args:
        chat_state.system_prompt = None
    else:
        chat_state.system_prompt = " ".join(context.args)
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("✅ Системная инструкция обновлена.")

async def roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    # Загружаем кастомные роли пользователя (сначала пользовательские)
    custom_roles = await db.db_query(
        "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
        (user_id,)
    )

    # Формируем список кнопок: кастомные сверху, затем стандартные
    btn_rows = []
    if custom_roles:
        for role in custom_roles:
            title = f"🎭 {role['title']}"
            btn_rows.append([
                InlineKeyboardButton(title, callback_data=f"role_apply:user_role:{role['id']}"),
                InlineKeyboardButton("🗑️", callback_data=f"role_delete:{role['id']}")
            ])

    for key, meta in prompts.DEFAULT_ROLES.items():
        title = meta.get("title", key)
        btn_rows.append([InlineKeyboardButton(title, callback_data=f"role_apply:{key}")])

    # Разбиваем в две колонки равномерно
    two_col = []
    temp = []
    for row in btn_rows:
        if len(row) == 2 and row[1].text == "🗑️":
            two_col.append(row)
        else:
            temp.append(row[0])
            if len(temp) == 2:
                two_col.append(temp)
                temp = []
    if temp:
        two_col.append(temp)

    two_col.append([InlineKeyboardButton("🧹 Сбросить роль", callback_data="role_clear"), InlineKeyboardButton("✏️ Переименовать роль", callback_data="role_rename_menu")])
    two_col.append([InlineKeyboardButton("➕ Создать свою роль", callback_data="role_create")])

    text = "Выберите роль или создайте свою:"
    if custom_roles:
        text += f"\n\n🎭 *Ваши кастомные роли:* {len(custom_roles)}"

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(two_col))


async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)
    await update.message.reply_text("Новый чат создан. История и системная инструкция сброшены.")

async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    if not await db.is_authorized(update.effective_user.id): return
    
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)
    current_model = chat_state.model
    
    # Определяем, какой провайдер используется для текущей модели
    from app.config import get_openrouter_keys
    openrouter_available = bool(get_openrouter_keys())
    is_current_openrouter = "/" in current_model if current_model else False
    
    # Создаем единый список всех моделей для индексации
    all_models = []
    if settings.AVAILABLE_MODELS:
        all_models.extend(settings.AVAILABLE_MODELS)
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)
    
    if not all_models:
        await update.message.reply_text("❌ Нет доступных моделей. Проверьте настройки.")
        return
    
    # Сохраняем маппинг моделей в context для использования в callback
    if not hasattr(context, 'user_data'):
        context.user_data = {}
    context.user_data['model_list'] = all_models
    
    keyboard = []
    model_index = 0
    
    # Добавляем модели Gemini
    if settings.AVAILABLE_MODELS:
        for m in settings.AVAILABLE_MODELS:
            is_selected = "✅ " if m == current_model and not is_current_openrouter else ""
            # Используем индекс + хэш для валидации (ограничение Telegram: 64 байта)
            model_hash = get_model_hash(m)
            keyboard.append([InlineKeyboardButton(f"{is_selected}🤖 {m}", callback_data=f"model:{model_index}:{model_hash}")])
            model_index += 1
    
    # Добавляем разделитель, если есть оба провайдера
    if settings.AVAILABLE_MODELS and openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        keyboard.append([InlineKeyboardButton("─────────────", callback_data="model_none")])
    
    # Добавляем модели OpenRouter, если доступны
    if openrouter_available and settings.OPENROUTER_AVAILABLE_MODELS:
        for m in settings.OPENROUTER_AVAILABLE_MODELS:
            is_selected = "✅ " if m == current_model and is_current_openrouter else ""
            # Показываем короткое имя модели с иконкой провайдера
            display_name = m.split("/")[-1] if "/" in m else m
            provider_icon = "🌐"
            # Используем индекс + хэш для валидации
            model_hash = get_model_hash(m)
            keyboard.append([InlineKeyboardButton(f"{is_selected}{provider_icon} {display_name}", callback_data=f"model:{model_index}:{model_hash}")])
            model_index += 1
    
    # Формируем текст с информацией о текущей модели
    provider_name = "OpenRouter" if is_current_openrouter else "Google Gemini"
    text = f"*Выберите модель для разговора:*\n\n"
    text += f"*Текущая модель:* `{current_model}`\n"
    text += f"*Провайдер:* {provider_name}\n\n"
    text += "Нажмите на модель для выбора."
    
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=InlineKeyboardMarkup(keyboard))

async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    
    # Используем TelegramFormatter для правильного экранирования
    formatted_text, parse_mode = TelegramFormatter.format_text(f"🌐 Постоянный режим исследования *{status_text}*.")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)

# Команды /keystatus и /credits объединены с /metrics

async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    if not db.is_admin(update.effective_user.id): return
    key_data = await db.get_available_gemini_key(settings.DEFAULT_MODEL)
    if not key_data:
        await update.message.reply_text("Нет доступных API ключей для выполнения запроса.")
        return
    await update.message.reply_text("Запрашиваю список моделей у Google API...")
    try:
        client = genai.Client(api_key=key_data['api_key'])
        models_list = [f"- `{m.name}`" for m in client.models.list() if 'generateContent' in m.supported_generation_methods]
        
        # Используем TelegramFormatter для правильного экранирования
        formatted_text, parse_mode = TelegramFormatter.format_text("✅ *Доступные модели:*\n" + "\n".join(models_list))
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_add = int(context.args[0])
        await db.db_query("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1", (user_to_add,))
        await update.message.reply_text(f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /adduser <user_id>")

async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    if not db.is_admin(update.effective_user.id): return
    try:
        user_to_del = int(context.args[0])
        if user_to_del == settings.ADMIN_ID:
            await update.message.reply_text("Нельзя удалить администратора.")
            return
        await db.db_query("UPDATE users SET is_authorized = 0 WHERE user_id = $1", (user_to_del,))
        await update.message.reply_text(f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /deluser <user_id>")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    if not db.is_admin(update.effective_user.id): return
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    user_ids = [str(row['user_id']) for row in rows]
    await update.message.reply_text("Авторизованные пользователи:\n" + "\n".join(user_ids))

async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
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
            "📊 *Полная сводка системы:*\n\n"
            "*🚀 Производительность:*\n"
            f"• Всего запросов: `{metrics['total_requests']}`\n"
            f"• Среднее время ответа: `{metrics['average_response_time']:.2f}s`\n"
            f"• Процент ошибок: `{metrics['error_rate']:.1f}%`\n"
            f"• Попадания в кэш: `{metrics['cache_hit_rate']:.1f}%`\n"
            f"• Поисковых запросов: `{metrics['search_queries']}`\n\n"
        )
        
        # Добавляем использование API и моделей
        if metrics.get('api_calls'):
            text += "*🔌 Использование API:*\n"
            for api, count in metrics['api_calls'].items():
                if isinstance(api, str) and isinstance(count, (int, float)):
                    text += f"• {api}: `{count}`\n"
            text += "\n"
        
        if metrics.get('model_usage'):
            text += "*🤖 Использование моделей:*\n"
            for model, count in metrics['model_usage'].items():
                # Пропускаем записи, которые содержат имена файлов (это ошибки в логике)
                if isinstance(model, str) and isinstance(count, (int, float)) and not any(char in model for char in ['/', '\\', '.pdf', '.docx', '.doc']):
                    text += f"• {model}: `{count}`\n"
            text += "\n"
        
        # Добавляем статус ключей Gemini
        if gemini_keys:
            text += "*🔑 Статус ключей Gemini (сегодня):*\n"
            for key_row in gemini_keys:
                display_name = format_key_for_display(key_row['api_key'])
                usage_data = await db.db_query(
                    "SELECT model_name, request_count FROM key_usage WHERE key_hash = $1 AND usage_date = $2", 
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
            text += f"Сброс лимитов: *{time_utils.get_kyiv_reset_time()}* по Киеву\n\n"
        
        # Добавляем статус кредитов Tavily
        if tavily_keys:
            text += "*💳 Кредиты Tavily (текущий месяц):*\n"
            for key_row in tavily_keys:
                display_name = format_key_for_display(key_row['api_key'])
                usage = await db.db_query(
                    "SELECT credit_usage FROM tavily_key_usage WHERE key_hash = $1 AND usage_month = $2", 
                    (key_row['key_hash'], current_month)
                )
                count = usage[0]['credit_usage'] if usage else 0
                limit = settings.TAVILY_MONTHLY_CREDIT_LIMIT
                text += f"• `{display_name}`: {count} / {limit}\n"
            text += "Сброс лимитов: 1-го числа каждого месяца\n\n"
        
        # Добавляем историю за последние дни
        if metrics['daily_metrics']:
            text += "*📈 История за последние дни:*\n"
            for date_str, daily_data in list(metrics['daily_metrics'].items())[:5]:  # Последние 5 дней
                requests = daily_data.get('requests', 0)
                errors = daily_data.get('errors', 0)
                text += f"• {date_str}: {requests} запросов, {errors} ошибок\n"
            text += "\n"
        
        # Добавляем последние ошибки
        if metrics['recent_errors']:
            text += "*⚠️ Последние ошибки:*\n"
            for error in metrics['recent_errors'][:3]:  # Последние 3 ошибки
                text += f"• {error['type']}: {error['message'][:40]}...\n"
        
        # Используем TelegramFormatter для надежного форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        error_msg = f"❌ Ошибка получения метрик: {str(e)[:100]}"
        await update.message.reply_text(error_msg)
        logging.error(f"Error in metrics command for user {update.effective_user.id}: {e}", exc_info=True)

async def cache_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику кэша"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await get_cache_stats()
        
        text = (
            "🗄️ *Статистика кэша:*\n\n"
            f"Всего ключей: `{stats.get('total_keys', 'N/A')}`\n"
            f"Используемая память: `{stats.get('used_memory', 'N/A')}`\n"
            f"Время работы: `{stats.get('uptime_in_days', 'N/A')} дней`\n"
            f"Попадания в кэш: `{stats.get('cache_hit_rate', 'N/A')}`\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        error_msg = f"❌ Ошибка получения статистики кэша: {str(e)[:100]}"
        await update.message.reply_text(error_msg)
        logging.error(f"Error in cache_stats command for user {update.effective_user.id}: {e}", exc_info=True)

async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает список документов пользователя и управляет ими"""
    if not await db.is_authorized(update.effective_user.id):
        return
    
    # Очищаем состояние работы с документами при входе в команду
    from app.state import clear_document_state
    clear_document_state(update.effective_user.id)
    
    try:
        from app.document_processor import get_user_documents
        documents = await get_user_documents(update.effective_user.id)
        if not documents:
            text = (
                "📋 *Ваши документы*\n\n"
                "У вас пока нет загруженных документов.\n\n"
                "💡 *Как загрузить документ:*\n"
                "• Отправьте PDF или DOCX файл\n"
                "• Максимальный размер: 50MB\n"
                "• После загрузки вы сможете задавать вопросы по содержимому\n\n"
                "📋 *Политика хранения:*\n"
                "• Максимум документов: 5\n"
                "• Срок хранения: 3 дня"
            )
        else:
            text = "📋 *Ваши документы:*\n\n"
            for i, doc in enumerate(documents[:10], 1):
                text += f"{i}. *{doc['filename']}*\n"
                text += f"   📄 Страниц: {doc['pages']}\n"
                text += f"   📅 Загружен: {doc['created_at'][:10]}\n"
                text += f"   📊 Размер: {doc['file_size']:,} символов\n\n"
            if len(documents) > 10:
                text += f"... и еще {len(documents) - 10} документов\n\n"
            text += (
                "💡 *Действия:*\n"
                "• Отправьте новый документ для загрузки\n"
                "• Задайте вопрос по последнему документу\n"
                "• Используйте кнопки под сообщениями для управления\n\n"
                "📋 *Политика хранения:*\n"
                "• Максимум документов: 5\n"
                "• Срок хранения: 3 дня"
            )
        keyboard = [
            [InlineKeyboardButton("📄 Загрузить новый документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
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
    # context используется для совместимости с другими командами
    """Показывает статистику очереди задач"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        stats = await task_queue.get_queue_stats()
        
        text = (
            "📋 *Статистика очереди задач:*\n\n"
            f"Всего задач: `{stats['total_tasks']}`\n"
            f"В ожидании: `{stats['pending_tasks']}`\n"
            f"Выполняется: `{stats['running_tasks']}`\n"
            f"Завершено: `{stats['completed_tasks']}`\n"
            f"Ошибок: `{stats['failed_tasks']}`\n"
            f"Размер очереди: `{stats['queue_size']}`\n"
            f"Активных воркеров: `{stats['active_workers']}`\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        error_msg = f"❌ Ошибка получения статистики очереди: {str(e)[:100]}"
        await update.message.reply_text(error_msg)
        logging.error(f"Error in queue_stats command for user {update.effective_user.id}: {e}", exc_info=True)

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает кэш"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        from app.cache import clear_cache
        await clear_cache()
        await update.message.reply_text("✅ Кэш очищен.")
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки кэша: {e}")

async def clear_old_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает старые метрики (старше 30 дней)"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        # Удаляем метрики старше 30 дней
        await db.db_query("""
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

async def update_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Команда для обновления ключей Tavily API"""
    if not db.is_admin(update.effective_user.id): 
        return
    
    try:
        await update.message.reply_text("🔄 Обновляю ключи Tavily API...")
        
        # Принудительно обновляем ключи
        success = await db.force_update_tavily_keys()
        
        if success:
            await update.message.reply_text(
                "✅ Ключи Tavily API успешно обновлены!\n"
                "💡 Система готова к работе с новыми ключами."
            )
        else:
            await update.message.reply_text(
                "❌ Не удалось обновить ключи Tavily API.\n"
                "🔍 Проверьте логи для получения дополнительной информации."
            )
            
    except Exception as e:
        error_msg = f"Ошибка при обновлении ключей Tavily: {e}"
        logging.error(error_msg)
        await update.message.reply_text(f"💥 {error_msg}")

async def check_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Команда для проверки статуса ключей Tavily API"""
    if not db.is_admin(update.effective_user.id): 
        return
    
    try:
        await update.message.reply_text("🔍 Проверяю статус ключей Tavily API...")
        
        # Получаем текущие ключи из базы данных
        keys_result = await db.db_query("SELECT key_hash, api_key FROM tavily_api_keys")
        
        if not keys_result:
            await update.message.reply_text("❌ В базе данных нет ключей Tavily API")
            return
        
        # Формируем отчет
        report = f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"
        
        for i, row in enumerate(keys_result, 1):
            key_hash = row['key_hash']
            api_key = row['api_key']
            report += f"🔑 *Ключ {i}:*\n"
            report += f"   Хэш: `{key_hash[:16]}...`\n"
            report += f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"
        
        # Проверяем использование
        current_month = time_utils.get_current_month_str()
        usage_result = await db.db_query("""
            SELECT 
                key_hash,
                credit_usage
            FROM tavily_key_usage 
            WHERE usage_month = $1
        """, (current_month,))
        
        if usage_result:
            report += f"📊 *Использование за {current_month}:*\n"
            for row in usage_result:
                key_preview = row['key_hash'][:16] + "..."
                usage = row['credit_usage']
                report += f"   `{key_preview}`: {usage} кредитов\n"
        else:
            report += f"📊 *Использование за {current_month}:*\n   Нет данных\n"
        
        # Добавляем информацию о лимитах
        report += "\n⚡ *Лимиты:*\n"
        report += f"   Месячный лимит: {settings.TAVILY_MONTHLY_CREDIT_LIMIT} кредитов\n"
        report += f"   Порог предупреждения: {settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100}%\n"
        
        await update.message.reply_text(report, parse_mode='Markdown')
        
    except Exception as e:
        error_msg = f"Ошибка при проверке ключей Tavily: {e}"
        logging.error(error_msg)
        await update.message.reply_text(f"💥 {error_msg}")

async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
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
    # context используется для совместимости с другими командами
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
            f"📊 *Статистика группы '{chat.title}':*\n\n"
            f"Всего сообщений: `{stats['total_messages']}`\n"
            f"Сообщений за 24ч: `{stats['recent_messages']}`\n"
            f"Активных пользователей за 24ч: `{stats['active_users_24h']}`\n"
            f"Участников: `{stats['member_count']}`\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики группы: {e}")

async def document_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику документов"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        from app.document_processor import document_processor
        
        stats = await document_processor.get_document_stats()
        
        text = (
            f"📊 *Статистика документов:*\n\n"
            f"• Всего документов: `{stats['total_documents']}`\n"
            f"• Общий размер: `{stats['total_size_mb']:.2f} MB`\n"
            f"• Средний размер: `{stats['average_size_chars']:.0f} символов`\n"
            f"• Общий размер в символах: `{stats['total_size_chars']:,}`\n\n"
            f"📋 *Политика хранения:*\n"
            f"• Максимум документов на пользователя: `5`\n"
            f"• Срок хранения: `3 дня`\n"
            f"• Автоматическая очистка: `ежедневно в 3:00`\n\n"
            f"💡 Используйте `/clearolddocs` для ручной очистки старых документов."
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики документов: {e}")
        logging.error(f"Error in document_stats_command: {e}", exc_info=True)

async def clear_old_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает старые документы (старше 3 дней)"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        from app.document_processor import document_processor
        
        # Очищаем документы старше 3 дней
        deleted_count = await document_processor.cleanup_old_documents(3)
        
        # Получаем статистику после очистки
        stats = await document_processor.get_document_stats()
        
        text = (
            f"🗑️ *Очистка документов завершена*\n\n"
            f"Удалено документов: `{deleted_count}`\n\n"
            f"📊 *Текущая статистика:*\n"
            f"• Всего документов: `{stats['total_documents']}`\n"
            f"• Общий размер: `{stats['total_size_mb']:.2f} MB`\n"
            f"• Средний размер: `{stats['average_size_chars']:.0f} символов`\n\n"
            f"💡 Документы старше 3 дней удаляются автоматически."
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки документов: {e}")
        logging.error(f"Error in clear_old_documents_command: {e}", exc_info=True)

# ============================================================================
# CONVERSATION MANAGEMENT COMMANDS
# ============================================================================

async def save_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Сохранить текущую беседу"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    args = context.args
    if not args:
        # Автогенерация названия на основе последних сообщений
        chat_state = await db.get_user_chat(user_id)
        if chat_state and chat_state.history:
            # Берем последнее сообщение пользователя для генерации названия
            last_user_msg = None
            if isinstance(chat_state.history, list):
                for msg in reversed(chat_state.history):
                    if isinstance(msg, dict) and msg.get('role') == 'user':
                        content = msg.get('content', '')
                        if isinstance(content, list):
                            content = ' '.join(str(part) for part in content)
                        last_user_msg = str(content)[:50]  # Первые 50 символов
                        break
            
            if last_user_msg:
                title = f"Беседа: {last_user_msg}..."
            else:
                from datetime import datetime
                title = f"Беседа от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        else:
            from datetime import datetime
            title = f"Беседа от {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    else:
        title = " ".join(args)
    
    if len(title) > 100:
        title = title[:97] + "..."
    
    # Определяем текущую роль
    chat_state = await db.get_user_chat(user_id)
    role_type = None
    role_id = None
    
    if chat_state and chat_state.system_prompt:
        # Проверяем, есть ли активная роль
        for key, role_data in prompts.DEFAULT_ROLES.items():
            if role_data['prompt'] in chat_state.system_prompt:
                role_type = 'role'
                role_id = key
                break
    
    conv_id = await db.save_conversation(user_id, title, role_type, role_id)
    if conv_id:
        await role_conv_metrics.record_conversation_saved()
        await update.message.reply_text(f"✅ Беседа сохранена с ID: {conv_id}")
    else:
        await update.message.reply_text("❌ Ошибка при сохранении беседы")

async def conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Показать список сохранённых бесед"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    # Парсим аргументы для пагинации
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    
    limit = 5
    offset = (page - 1) * limit
    
    conversations = await db.get_user_conversations(user_id, limit, offset)
    total_count = await db.get_conversation_count(user_id)
    
    if not conversations:
        await update.message.reply_text("📝 У вас пока нет сохранённых бесед.\n\nИспользуйте /save <название> для сохранения текущей беседы.")
        return
    
    text = f"📝 *Сохранённые беседы* (страница {page})\n\n"
    
    for conv in conversations:
        role_info = f" | {conv['role_title']}" if conv['role_title'] else ""
        created = conv['created_at'].strftime("%d.%m.%Y %H:%M") if conv['created_at'] else "Неизвестно"
        text += f"🆔 *{conv['id']}* | {conv['title']}{role_info}\n"
        text += f"📅 {created} | 💬 {conv['token_budget'] or 0} токенов\n\n"
    
    # Кнопки навигации
    keyboard = []
    if page > 1:
        keyboard.append([InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"conv_page:{page-1}")])
    if len(conversations) == limit and offset + limit < total_count:
        keyboard.append([InlineKeyboardButton("➡️ Следующая", callback_data=f"conv_page:{page+1}")])
    
    # Кнопки действий
    if conversations:
        keyboard.append([InlineKeyboardButton("🔄 Переключиться", callback_data="conv_switch")])
        keyboard.append([InlineKeyboardButton("✏️ Переименовать", callback_data="conv_rename")])
        keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data="conv_delete")])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def switch_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Переключиться на беседу"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /switch <ID беседы>\n\nИспользуйте /conversations для просмотра списка бесед.")
        return
    
    conv_id = int(args[0])
    success = await db.switch_to_conversation(user_id, conv_id)
    
    if success:
        await role_conv_metrics.record_conversation_switched()
        await update.message.reply_text(f"✅ Переключились на беседу {conv_id}")
    else:
        await update.message.reply_text("❌ Беседа не найдена или у вас нет доступа к ней")

async def rename_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Переименовать беседу"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    args = context.args
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text("Использование: /rename <ID беседы> <новое название>")
        return
    
    conv_id = int(args[0])
    new_title = " ".join(args[1:])
    
    if len(new_title) > 100:
        await update.message.reply_text("❌ Название беседы слишком длинное (максимум 100 символов)")
        return
    
    success = await db.rename_conversation(user_id, conv_id, new_title)
    
    if success:
        await role_conv_metrics.record_conversation_renamed()
        await update.message.reply_text(f"✅ Беседа {conv_id} переименована в '{new_title}'")
    else:
        await update.message.reply_text("❌ Беседа не найдена или у вас нет доступа к ней")

async def delete_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Удалить беседу"""
    user_id = update.effective_user.id
    if not await db.is_authorized(user_id): return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /delete <ID беседы>\n\nИспользуйте /conversations для просмотра списка бесед.")
        return
    
    conv_id = int(args[0])
    
    # Подтверждение удаления
    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"conv_delete_confirm:{conv_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="conv_delete_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ Вы уверены, что хотите удалить беседу {conv_id}?\n\nЭто действие нельзя отменить!",
        reply_markup=reply_markup
    )

async def role_conv_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показать метрики ролей и бесед"""
    if not db.is_admin(update.effective_user.id): return
    
    try:
        metrics = await role_conv_metrics.get_metrics_summary()
        
        text = "📊 *Метрики ролей и бесед:*\n\n"
        
        # Метрики ролей
        text += "*🎭 Роли:*\n"
        text += f"• Применений ролей: `{sum(metrics['roles']['applications'].values())}`\n"
        text += f"• Кастомных ролей создано: `{metrics['roles']['custom_created']}`\n"
        text += f"• Сбросов ролей: `{metrics['roles']['clears']}`\n"
        text += f"• Сохранений ролей: `{metrics['roles']['saves']}`\n\n"
        
        # Популярные роли
        if metrics['roles']['applications']:
            text += "*🔥 Популярные роли:*\n"
            sorted_roles = sorted(metrics['roles']['applications'].items(), key=lambda x: x[1], reverse=True)
            for role_key, count in sorted_roles[:5]:
                role_title = prompts.DEFAULT_ROLES.get(role_key, {}).get('title', role_key)
                text += f"• {role_title}: `{count}`\n"
            text += "\n"
        
        # Метрики бесед
        text += "*💬 Беседы:*\n"
        text += f"• Сохранено: `{metrics['conversations']['saved']}`\n"
        text += f"• Переключений: `{metrics['conversations']['switched']}`\n"
        text += f"• Переименований: `{metrics['conversations']['renamed']}`\n"
        text += f"• Удалений: `{metrics['conversations']['deleted']}`\n\n"
        
        # Метрики суммаризации
        text += "*📝 Суммаризация:*\n"
        text += f"• Срабатываний: `{metrics['summarization']['triggered']}`\n"
        text += f"• Мягких лимитов: `{metrics['summarization']['soft_limit']}`\n"
        text += f"• Жёстких лимитов: `{metrics['summarization']['hard_limit']}`\n"
        text += f"• Токенов сэкономлено: `{metrics['summarization']['tokens_saved']}`\n"
        text += f"• Средняя длина суммаризации: `{metrics['summarization']['avg_summary_length']:.0f}` символов\n"
        
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка получения метрик: {e}")
        logging.error(f"Error in role_conv_metrics_command: {e}", exc_info=True)

async def reload_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Перезагружает конфигурацию из переменных окружения"""
    if not db.is_admin(update.effective_user.id): 
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    
    try:
        await update.message.reply_text("🔄 Перезагружаю конфигурацию...")
        
        # Используем существующий ConfigManager
        from app.config import config_manager
        await config_manager.force_reload()
        
        # Получаем обновленные настройки
        new_settings = config_manager.settings
        
        # Формируем отчет
        report = "✅ *Конфигурация перезагружена*\n\n"
        report += f"🔑 *API ключи:*\n"
        report += f"• Gemini: `{len(new_settings.GEMINI_API_KEYS)}` ключей\n"
        report += f"• Tavily: `{len(new_settings.TAVILY_API_KEYS)}` ключей\n"
        report += f"• OpenRouter: `{len(new_settings.OPENROUTER_API_KEYS)}` ключей\n\n"
        report += f"🤖 *Модели:*\n"
        report += f"• Gemini: `{len(new_settings.AVAILABLE_MODELS)}` моделей\n"
        report += f"• OpenRouter: `{len(new_settings.OPENROUTER_AVAILABLE_MODELS)}` моделей\n"
        report += f"• По умолчанию: `{new_settings.DEFAULT_MODEL}`\n\n"
        report += f"⚙️ *Настройки:*\n"
        report += f"• PORT: `{new_settings.PORT}`\n"
        report += f"• ADMIN_ID: `{new_settings.ADMIN_ID}`\n"
        report += f"• Лимитов моделей: `{len(new_settings.DAILY_LIMITS)}`\n\n"
        report += "💡 Все настройки загружены из переменных окружения."
        
        formatted_text, parse_mode = TelegramFormatter.format_text(report)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        
        logging.info(f"Configuration reloaded by admin {update.effective_user.id}")
        
    except Exception as e:
        error_msg = f"❌ Ошибка перезагрузки: {str(e)[:200]}"
        await update.message.reply_text(error_msg)
        logging.error(f"Error reloading config: {e}", exc_info=True)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает справку по админским командам"""
    if not db.is_admin(update.effective_user.id): 
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    
    try:
        user_id = update.effective_user.id
        logging.info(f"Admin command from user {user_id}")
        
        help_text = (
            "🔧 *Админские команды Gemini Bot*\n\n"
            
            "*👥 Управление пользователями:*\n"
            "• `/adduser user_id` — добавить пользователя\n"
            "• `/deluser user_id` — удалить пользователя\n"
            "• `/listusers` — список авторизованных пользователей\n\n"
            
            "*📊 Мониторинг и статистика:*\n"
            "• `/metrics` — полная сводка метрик, ключей, кредитов\n"
            "• `/cachestats` — статистика кэша\n"
            "• `/queuestats` — статистика очереди задач\n"
            "• `/docstats` — статистика документов\n"
            "• `/rolemetrics` — метрики ролей и бесед\n"
            "• `/groupstats` — статистика групповых чатов\n\n"
            
            "*🔧 Управление системой:*\n"
            "• `/reloadconfig` — перезагрузить конфигурацию из env\n"
            "• `/clearcache` — очистить кэш\n"
            "• `/clearoldmetrics` — очистить старые метрики 30\\+ дней\n"
            "• `/clearolddocs` — очистить старые документы 3\\+ дня\n"
            "• `/listmodels` — список доступных моделей\n\n"
            
            "*🌐 API ключи:*\n"
            "• `/updatetavilykeys` — обновить ключи Tavily API\n"
            "• `/checktavilykeys` — проверить статус ключей Tavily\n\n"
            
            "*👥 Групповые чаты:*\n"
            "• `/registergroup` — зарегистрировать групповой чат\n"
            "• `/groupstats` — статистика групповых чатов\n\n"
            
            "*💬 Управление беседами:*\n"
            "• `/save` — сохранить текущую беседу\n"
            "• `/conversations` — список сохраненных бесед\n"
            "• `/switch` — переключиться между беседами\n"
            "• `/rename` — переименовать беседу\n"
            "• `/delete` — удалить беседу\n\n"
            
            "*📄 Документы:*\n"
            "• `/documents` — управление документами пользователя\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        logging.info(f"Admin command completed successfully for user {user_id}")
        
    except Exception as e:
        logging.error(f"Error in admin command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

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
    application.add_handler(CommandHandler("clearolddocs", clear_old_documents_command))
    application.add_handler(CommandHandler("docstats", document_stats_command))
    application.add_handler(CommandHandler("updatetavilykeys", update_tavily_keys_command))
    application.add_handler(CommandHandler("checktavilykeys", check_tavily_keys_command))
    
    # Команды для групповых чатов
    application.add_handler(CommandHandler("registergroup", register_group_command))
    application.add_handler(CommandHandler("groupstats", group_stats_command))
    
    # Команды для работы с документами
    application.add_handler(CommandHandler("documents", documents_command))
    # Новая команда ролей
    application.add_handler(CommandHandler("roles", roles_command))
    
    # Команды для работы с беседами
    application.add_handler(CommandHandler("save", save_conversation_command))
    application.add_handler(CommandHandler("conversations", conversations_command))
    application.add_handler(CommandHandler("switch", switch_conversation_command))
    application.add_handler(CommandHandler("rename", rename_conversation_command))
    application.add_handler(CommandHandler("delete", delete_conversation_command))
    
    # Команды метрик
    application.add_handler(CommandHandler("rolemetrics", role_conv_metrics_command))
    
    # Админская справка
    application.add_handler(CommandHandler("admin", admin_command))
    
    # Команда перезагрузки конфигурации
    application.add_handler(CommandHandler("reloadconfig", reload_config_command))