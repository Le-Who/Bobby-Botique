# /app/handlers/cmd_admin.py
"""Admin-only commands: user management, metrics, cache, queue, config, tavily, cleanup."""

import logging

from google import genai
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.cache import get_cache_stats
from app.config import settings
from app.group_chat import group_chat_manager
from app.handlers import menus
from app.metrics import role_conv_metrics
from app.prompt_registry import DEFAULT_ROLES
from app.queue import task_queue
from app.repos.admin import (
    authorize_user,
    clear_old_metrics,
    get_all_tavily_keys,
    get_tavily_usage_for_month,
    list_authorized_users,
    revoke_user,
)
from app.repos.keys import force_update_tavily_keys, get_available_gemini_key
from app.repos.users import invalidate_user_auth_cache
from app.utils import time as time_utils
from app.utils.decorators import admin_only, authorized_only
from app.utils.formatting import TelegramFormatter


@admin_only
async def list_models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    key_data = await get_available_gemini_key(settings.RESEARCH_MODEL)
    if not key_data:
        await update.message.reply_text("Нет доступных API ключей для выполнения запроса.")
        return
    await update.message.reply_text("Запрашиваю список моделей у Google API...")
    try:
        client = genai.Client(api_key=key_data["api_key"])

        # google-genai SDK: Model has .name and .supported_actions (list of str)
        api_models = set()
        models_list = []
        for m in client.models.list():
            # Filter to models that support generateContent
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            short_name = m.name.replace("models/", "") if m.name else str(m.name)
            api_models.add(short_name)
            models_list.append(f"- `{short_name}`")

        # Cross-reference with configured models
        configured = set(settings.AVAILABLE_MODELS)
        available = configured & api_models
        missing = configured - api_models

        header = f"✅ *Доступные модели ({len(models_list)}):*\n"
        body = "\n".join(models_list[:50])  # Cap output

        # Show config validation
        validation = ["\n\n*🔍 Проверка конфигурации:*\n"]
        validation.append(f"✅ Доступны: `{', '.join(sorted(available)) or 'нет'}`\n")
        if missing:
            validation.append(f"❌ НЕ найдены в API: `{', '.join(sorted(missing))}`\n")
            validation.append("⚠️ Запросы к этим моделям будут вызывать ошибки ключей!\n")
        else:
            validation.append("✅ Все настроенные модели доступны в API\n")

        full_text = header + body + "".join(validation)
        formatted_text, parse_mode = TelegramFormatter.format_text(full_text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


@admin_only
async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    try:
        args = context.args
        if not args:
            raise IndexError
        user_to_add = int(args[0])
        await authorize_user(user_to_add)
        await invalidate_user_auth_cache(user_to_add)
        await update.message.reply_text(f"Пользователь {user_to_add} добавлен.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /adduser <user_id>")


@admin_only
async def del_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    try:
        args = context.args
        if not args:
            raise IndexError
        user_to_del = int(args[0])
        if user_to_del == settings.ADMIN_ID:
            await update.message.reply_text("Нельзя удалить администратора.")
            return
        await revoke_user(user_to_del)
        await invalidate_user_auth_cache(user_to_del)
        await update.message.reply_text(f"Доступ для пользователя {user_to_del} отозван.")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /deluser <user_id>")


@admin_only
async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_ids = await list_authorized_users()
    await update.message.reply_text("Авторизованные пользователи:\n" + "\n".join(str(uid) for uid in user_ids))


@admin_only
async def metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показывает полную сводку метрик, статуса ключей и кредитов"""
    try:
        text = await menus.get_metrics_content()

        # Используем TelegramFormatter for надежного форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(text)

        keyboard = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_metrics")]]

        await update.message.reply_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        error_msg = f"❌ Ошибка получения метрик: {str(e)[:100]}"
        await update.message.reply_text(error_msg)
        logging.error(
            "Error in metrics command: %s",
            e,
            exc_info=True,
        )


@admin_only
async def cache_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
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
        logging.error(
            "Error in cache_stats command: %s",
            e,
            exc_info=True,
        )


@admin_only
async def queue_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
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
        logging.error(
            "Error in queue_stats command: %s",
            e,
            exc_info=True,
        )


@admin_only
async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Очищает кэш"""
    try:
        from app.cache import clear_cache

        await clear_cache()
        await update.message.reply_text("✅ Кэш очищен.")

    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки кэша: {e}")


@admin_only
async def clear_old_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Очищает старые метрики (старше 30 дней)"""
    try:
        # Delete metrics старше 30 дней
        await clear_old_metrics()

        await update.message.reply_text("✅ Старые метрики очищены (старше 30 дней).")

    except Exception as e:
        await update.message.reply_text(f"Ошибка очистки метрик: {e}")


@admin_only
async def update_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Команда для обновления ключей Tavily API"""
    try:
        await update.message.reply_text("🔄 Обновляю ключи Tavily API...")

        # Принудительно обновляем keys
        success = await force_update_tavily_keys()

        if success:
            await update.message.reply_text(
                "✅ Ключи Tavily API успешно обновлены!\n💡 Система готова к работе с новыми ключами."
            )
        else:
            await update.message.reply_text(
                "❌ Не удалось обновить ключи Tavily API.\n🔍 Проверьте логи для получения дополнительной информации."
            )

    except Exception as e:
        error_msg = f"Ошибка при обновлении ключей Tavily: {e}"
        logging.error(error_msg)
        await update.message.reply_text(f"💥 {error_msg}")


@admin_only
async def check_tavily_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Команда для проверки статуса ключей Tavily API"""
    try:
        await update.message.reply_text("🔍 Проверяю статус ключей Tavily API...")

        # Get текущие keys from базы данных
        keys_result = await get_all_tavily_keys()

        if not keys_result:
            await update.message.reply_text("❌ В базе данных нет ключей Tavily API")
            return

        # Build отчет
        parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]

        from app.crypto import safe_decrypt

        for i, row in enumerate(keys_result, 1):
            key_hash = row["key_hash"]

            api_key = safe_decrypt(row["api_key"])
            parts.append(f"🔑 *Ключ {i}:*\n")
            parts.append(f"   Хэш: `{key_hash[:16]}...`\n")
            parts.append(f"   API: `{api_key[:10]}...{api_key[-4:]}`\n\n")

        # Check использование
        current_month = time_utils.get_current_month_str()
        usage_result = await get_tavily_usage_for_month(current_month)

        if usage_result:
            parts.append(f"📊 *Использование за {current_month}:*\n")
            for row in usage_result:
                key_preview = row["key_hash"][:16] + "..."
                usage = row["credit_usage"]
                parts.append(f"   `{key_preview}`: {usage} кредитов\n")
        else:
            parts.append(f"📊 *Использование за {current_month}:*\n   Нет данных\n")

        # Add информацию о limitах
        parts.append("\n⚡ *Лимиты:*\n")
        parts.append(f"   Месячный лимит: {settings.TAVILY_MONTHLY_CREDIT_LIMIT} кредитов\n")
        parts.append(f"   Порог предупреждения: {settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100}%\n")

        report = "".join(parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(report)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)

    except Exception as e:
        error_msg = f"Ошибка при проверке ключей Tavily: {e}"
        logging.error(error_msg)
        await update.message.reply_text(f"💥 {error_msg}")


@authorized_only
async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Регистрирует групповой чат"""
    user_id = update.effective_user.id

    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Эта команда работает только в групповых чатах.")
        return

    try:
        success = await group_chat_manager.register_group(chat.id, chat.title or "", user_id)
        if success:
            await update.message.reply_text(f"✅ Группа '{chat.title}' зарегистрирована!")
        else:
            await update.message.reply_text("❌ Группа уже зарегистрирована или произошла ошибка.")

    except Exception as e:
        await update.message.reply_text(f"Ошибка регистрации группы: {e}")


@authorized_only
async def group_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показывает статистику группы"""

    chat = update.effective_chat
    if chat.type == "private":
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
async def document_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
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
            f"• Срок хранения: `3 дня`\n\n"
            f"💡 Используйте `/clearolddocs` для ручной очистки старых документов."
        )

        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)

    except Exception as e:
        await update.message.reply_text(f"Ошибка получения статистики документов: {e}")
        logging.error("Error in document_stats_command: %s", e, exc_info=True)


@admin_only
async def clear_old_documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Очищает старые документы (старше 3 дней)"""
    try:
        from app.document_processor import document_processor

        # Clean up documents старше 3 дней
        deleted_count = await document_processor.cleanup_old_documents(3)

        # Get статистику after очистки
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
        logging.error("Error in clear_old_documents_command: %s", e, exc_info=True)


@admin_only
async def role_conv_metrics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показать метрики ролей и бесед"""
    try:
        metrics = await role_conv_metrics.get_metrics_summary()

        parts = ["📊 *Метрики ролей и бесед:*\n\n"]

        # Метрики ролей
        parts.append("*🎭 Роли:*\n")
        parts.append(f"• Применений ролей: `{sum(metrics['roles']['applications'].values())}`\n")
        parts.append(f"• Кастомных ролей создано: `{metrics['roles']['custom_created']}`\n")
        parts.append(f"• Сбросов ролей: `{metrics['roles']['clears']}`\n")
        parts.append(f"• Сохранений ролей: `{metrics['roles']['saves']}`\n\n")

        # Популярные roles
        if metrics["roles"]["applications"]:
            parts.append("*🔥 Популярные роли:*\n")
            sorted_roles = sorted(
                metrics["roles"]["applications"].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for role_key, count in sorted_roles[:5]:
                role_title = DEFAULT_ROLES.get(role_key, {}).get("title", role_key)
                parts.append(f"• {role_title}: `{count}`\n")
            parts.append("\n")

        # Метрики бесед
        parts.append("*💬 Беседы:*\n")
        parts.append(f"• Сохранено: `{metrics['conversations']['saved']}`\n")
        parts.append(f"• Переключений: `{metrics['conversations']['switched']}`\n")
        parts.append(f"• Переименований: `{metrics['conversations']['renamed']}`\n")
        parts.append(f"• Удалений: `{metrics['conversations']['deleted']}`\n\n")

        # Метрики суммаризации
        parts.append("*📝 Суммаризация:*\n")
        parts.append(f"• Срабатываний: `{metrics['summarization']['triggered']}`\n")
        parts.append(f"• Мягких лимитов: `{metrics['summarization']['soft_limit']}`\n")
        parts.append(f"• Жёстких лимитов: `{metrics['summarization']['hard_limit']}`\n")
        parts.append(f"• Токенов сэкономлено: `{metrics['summarization']['tokens_saved']}`\n")
        parts.append(f"• Средняя длина суммаризации: `{metrics['summarization']['avg_summary_length']:.0f}` символов\n")

        text = "".join(parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)

    except Exception as e:
        await update.message.reply_text(f"Ошибка получения метрик: {e}")
        logging.error("Error in role_conv_metrics_command: %s", e, exc_info=True)


@admin_only
async def reload_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Перезагружает конфигурацию из переменных окружения"""
    try:
        await update.message.reply_text("🔄 Перезагружаю конфигурацию...")

        # Используем существующий ConfigManager
        from app.config import config_manager

        await config_manager.force_reload()

        # Get обновленные settings
        new_settings = config_manager.settings

        # Build отчет
        parts = ["✅ *Конфигурация перезагружена*\n\n"]
        parts.append("🔑 *API ключи:*\n")
        parts.append(f"• Gemini: `{len(new_settings.GEMINI_API_KEYS)}` ключей\n")
        parts.append(f"• Tavily: `{len(new_settings.TAVILY_API_KEYS)}` ключей\n")
        parts.append(f"• OpenRouter: `{len(new_settings.OPENROUTER_API_KEYS)}` ключей\n\n")
        parts.append("🤖 *Модели:*\n")
        parts.append(f"• Gemini: `{len(new_settings.AVAILABLE_MODELS)}` моделей\n")
        parts.append(f"• OpenRouter: `{len(new_settings.OPENROUTER_AVAILABLE_MODELS)}` моделей\n")
        parts.append(f"• По умолчанию: `{new_settings.DEFAULT_MODEL}`\n\n")
        parts.append("⚙️ *Настройки:*\n")
        parts.append(f"• PORT: `{new_settings.PORT}`\n")
        parts.append(f"• ADMIN_ID: `{new_settings.ADMIN_ID}`\n")
        parts.append(f"• Лимитов моделей: `{len(new_settings.DAILY_LIMITS)}`\n\n")
        parts.append("💡 Все настройки загружены из переменных окружения.")

        report = "".join(parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(report)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)

        logging.info("Configuration reloaded by admin")

    except Exception as e:
        error_msg = f"❌ Ошибка перезагрузки: {str(e)[:200]}"
        await update.message.reply_text(error_msg)
        logging.error("Error reloading config: %s", e, exc_info=True)


@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показывает справку по админским командам"""
    try:
        logging.info("Admin command")

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
        logging.info("Admin command completed successfully")

    except Exception as e:
        logging.error("Error in admin command: %s", e, exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при обработке команды. Попробуйте позже.")
