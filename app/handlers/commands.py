import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from app.config import settings
from google import genai
from app import database as db
from app.utils.formatting import TelegramFormatter
from app.utils import time as time_utils
from app.cache import get_cache_stats
from app.queue import task_queue
from app.group_chat import group_chat_manager
from app import prompts
from app.metrics import role_conv_metrics
from app.utils.decorators import authorized_only, admin_only
from app.handlers import menus

@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    logging.info(f"Start command from user {user_id}")
    
    try:
        chat_state = await db.get_user_chat(user_id)
        formatted_text, parse_mode, reply_markup = menus.get_start_menu_content(chat_state)
        
        await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
        logging.info(f"Start command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in start command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает подробную справку по использованию бота"""
    user_id = update.effective_user.id
    logging.info(f"Help command from user {user_id}")
    
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
            "**🧩 Роли:**\n"
            "• `/roles` — выбрать предустановленную роль или создать свою\n"
        )
        
        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        logging.info(f"Help command completed successfully for user {user_id}")
    except Exception as e:
        logging.error(f"Error in help command for user {user_id}: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")

@authorized_only
async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)

    if not context.args:
        # UX Improvement: Show current status instead of clearing
        current_prompt = chat_state.system_prompt
        if current_prompt:
            prompt_display = f"`{current_prompt}`"
        else:
            prompt_display = "_(не задана, используется стандартная)_"

        help_text = (
            f"⚙️ **Текущая системная инструкция:**\n{prompt_display}\n\n"
            "📝 **Как изменить:**\n"
            "`/setprompt Вы - опытный программист Python...`\n\n"
            "🧹 **Как сбросить:**\n"
            "`/setprompt clear`"
        )
        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        return

    # Check for clear command
    command_arg = context.args[0].lower()
    if command_arg in ('clear', 'reset') and len(context.args) == 1:
        chat_state.system_prompt = None
        await db.update_user_chat(user_id, chat_state)
        await update.message.reply_text("✅ Системная инструкция сброшена. Использую стандартное поведение.")
        return

    # Set new prompt
    chat_state.system_prompt = " ".join(context.args)
    await db.update_user_chat(user_id, chat_state)

    # Show preview of what was set
    preview = chat_state.system_prompt[:100] + "..." if len(chat_state.system_prompt) > 100 else chat_state.system_prompt
    formatted_text, parse_mode = TelegramFormatter.format_text(f"✅ Системная инструкция обновлена:\n`{preview}`")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)

@authorized_only
async def roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(user_id, chat_state)
    await update.message.reply_text(text, reply_markup=reply_markup)


@authorized_only
async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await db.update_user_chat(user_id, chat_state)

    # Формируем статус для UX
    search_icon = "🟢" if chat_state.search_enabled else "🔴"
    search_status = "ВКЛ" if chat_state.search_enabled else "ВЫКЛ"

    text = (
        "🧹 **Чат очищен!**\n"
        "История и контекст сброшены.\n\n"
        "⚙️ **Текущие настройки:**\n"
        f"• Модель: `{chat_state.model}`\n"
        f"• Поиск: {search_icon} {search_status}\n"
        f"• Роль: 👤 Базовая\n\n"
        "Готов к новой теме!"
    )

    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    keyboard = [
        [
            InlineKeyboardButton("⚙️ Настройки модели", callback_data="model_menu"),
            InlineKeyboardButton("🎭 Выбрать роль", callback_data="open_roles")
        ]
    ]

    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@authorized_only
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)
    
    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, context)
    
    if reply_markup is None:
         await update.message.reply_text(formatted_text)
    else:
         await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)

@authorized_only
async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await db.get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await db.update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"
    
    # Используем TelegramFormatter для правильного экранирования
    formatted_text, parse_mode = TelegramFormatter.format_text(f"🌐 Постоянный режим исследования *{status_text}*.")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)

# Команды /keystatus и /credits объединены с /metrics

@admin_only
async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
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

@admin_only
async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    try:
        user_to_add = int(context.args[0])
        await db.db_query("INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1", (user_to_add,))
        await db.invalidate_user_auth_cache(user_to_add)
        await update.message.reply_text(f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /adduser <user_id>")

@admin_only
async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    try:
        user_to_del = int(context.args[0])
        if user_to_del == settings.ADMIN_ID:
            await update.message.reply_text("Нельзя удалить администратора.")
            return
        await db.db_query("UPDATE users SET is_authorized = 0 WHERE user_id = $1", (user_to_del,))
        await db.invalidate_user_auth_cache(user_to_del)
        await update.message.reply_text(f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /deluser <user_id>")

@admin_only
async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    user_ids = [str(row['user_id']) for row in rows]
    await update.message.reply_text("Авторизованные пользователи:\n" + "\n".join(user_ids))

@admin_only
async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает полную сводку метрик, статуса ключей и кредитов"""
    try:
        text = await menus.get_metrics_content()
        
        # Используем TelegramFormatter для надежного форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        
        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_metrics")]]
        
        await update.message.reply_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    except Exception as e:
        error_msg = f"❌ Ошибка получения метрик: {str(e)[:100]}"
        await update.message.reply_text(error_msg)
        logging.error(f"Error in metrics command for user {update.effective_user.id}: {e}", exc_info=True)

@admin_only
async def cache_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику кэша"""
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

@authorized_only
async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает список документов пользователя и управляет ими"""
    
    # Очищаем состояние работы с документами при входе в команду
    from app.state import clear_document_state
    clear_document_state(update.effective_user.id)
    
    try:
        formatted_text, parse_mode, reply_markup = await menus.get_documents_menu_content(update.effective_user.id)
        await update.message.reply_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка получения документов: {e}")
        logging.error(f"Error in documents command: {e}", exc_info=True)

@admin_only
async def queue_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику очереди задач"""
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

@admin_only
async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает кэш"""
    try:
        from app.cache import clear_cache
        await clear_cache()
        await update.message.reply_text("✅ Кэш очищен.")
        
    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки кэша: {e}")

@admin_only
async def clear_old_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает старые метрики (старше 30 дней)"""
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

@admin_only
async def update_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Команда для обновления ключей Tavily API"""
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

@admin_only
async def check_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Команда для проверки статуса ключей Tavily API"""
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

@authorized_only
async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Регистрирует групповой чат"""
    user_id = update.effective_user.id
    
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

@authorized_only
async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику группы"""
    user_id = update.effective_user.id
    
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

@admin_only
async def document_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает статистику документов"""
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

@admin_only
async def clear_old_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Очищает старые документы (старше 3 дней)"""
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

@authorized_only
async def save_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Сохранить текущую беседу"""
    user_id = update.effective_user.id
    
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

@authorized_only
async def conversations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Показать список сохранённых бесед"""
    user_id = update.effective_user.id

    # Парсим аргументы для пагинации
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])

    text, parse_mode, reply_markup = await menus.get_conversations_menu_content(user_id, page)

    if reply_markup is None:
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

@authorized_only
async def switch_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Переключиться на беседу"""
    user_id = update.effective_user.id
    
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

@authorized_only
async def rename_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Переименовать беседу"""
    user_id = update.effective_user.id
    
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

@authorized_only
async def delete_conversation_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для получения аргументов команды
    """Удалить беседу"""
    user_id = update.effective_user.id
    
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

@admin_only
async def role_conv_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показать метрики ролей и бесед"""
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

@admin_only
async def reload_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Перезагружает конфигурацию из переменных окружения"""
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

@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context используется для совместимости с другими командами
    """Показывает справку по админским командам"""
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