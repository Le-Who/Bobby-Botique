# /app/handlers/cmd_admin.py
"""Admin-only commands: user management, metrics, cache, queue, config, tavily, cleanup."""

import asyncio
import logging

import httpx
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
from app.repos import crocodile_daily as daily_croc_repo
from app.repos.admin import (
    authorize_user,
    clear_old_metrics,
    get_all_gemini_keys,
    get_all_tavily_keys,
    get_tavily_usage_for_month,
    list_authorized_users,
    revoke_user,
)
from app.repos.keys import force_update_tavily_keys, get_available_gemini_key
from app.repos.settings_repo import get_global_setting, set_global_setting
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
        validation_parts = [
            "\n\n*🔍 Проверка конфигурации:*\n",
            f"✅ Доступны: `{', '.join(sorted(available)) or 'нет'}`\n",
        ]
        if missing:
            validation_parts.append(
                f"❌ НЕ найдены в API: `{', '.join(sorted(missing))}`\n"
                "⚠️ Запросы к этим моделям будут вызывать ошибки ключей!\n"
            )
        else:
            validation_parts.append("✅ Все настроенные модели доступны в API\n")

        full_text = header + body + "".join(validation_parts)
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
        report_parts = [f"📋 Найдено {len(keys_result)} ключей Tavily API:\n\n"]
        from app.crypto import safe_decrypt

        for i, row in enumerate(keys_result, 1):
            key_hash = row["key_hash"]
            api_key = safe_decrypt(row["api_key"])
            report_parts.append(
                f"🔑 *Ключ {i}:*\n   Хэш: `{key_hash[:16]}...`\n   API: `{api_key[:10]}...{api_key[-4:]}`\n\n"
            )

        # Check использование
        current_month = time_utils.get_current_month_str()
        usage_result = await get_tavily_usage_for_month(current_month)

        if usage_result:
            report_parts.append(f"📊 *Использование за {current_month}:*\n")
            for row in usage_result:
                key_preview = row["key_hash"][:16] + "..."
                usage = row["credit_usage"]
                report_parts.append(f"   `{key_preview}`: {usage} кредитов\n")
        else:
            report_parts.append(f"📊 *Использование за {current_month}:*\n   Нет данных\n")

        # Add информацию о limitах
        report_parts.append(
            "\n⚡ *Лимиты:*\n"
            f"   Месячный лимит: {settings.TAVILY_MONTHLY_CREDIT_LIMIT} кредитов\n"
            f"   Порог предупреждения: {settings.TAVILY_LIMIT_THRESHOLD_PERCENT * 100}%\n"
        )

        report = "".join(report_parts)
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

        text_parts = ["📊 *Метрики ролей и бесед:*\n\n"]

        # Метрики ролей
        text_parts.append(
            "*🎭 Роли:*\n"
            f"• Применений ролей: `{sum(metrics['roles']['applications'].values())}`\n"
            f"• Кастомных ролей создано: `{metrics['roles']['custom_created']}`\n"
            f"• Сбросов ролей: `{metrics['roles']['clears']}`\n"
            f"• Сохранений ролей: `{metrics['roles']['saves']}`\n\n"
        )

        # Популярные roles
        if metrics["roles"]["applications"]:
            text_parts.append("*🔥 Популярные роли:*\n")
            sorted_roles = sorted(
                metrics["roles"]["applications"].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for role_key, count in sorted_roles[:5]:
                role_title = DEFAULT_ROLES.get(role_key, {}).get("title", role_key)
                text_parts.append(f"• {role_title}: `{count}`\n")
            text_parts.append("\n")

        # Метрики бесед
        text_parts.append(
            "*💬 Беседы:*\n"
            f"• Сохранено: `{metrics['conversations']['saved']}`\n"
            f"• Переключений: `{metrics['conversations']['switched']}`\n"
            f"• Переименований: `{metrics['conversations']['renamed']}`\n"
            f"• Удалений: `{metrics['conversations']['deleted']}`\n\n"
        )

        # Метрики суммаризации
        text_parts.append(
            "*📝 Суммаризация:*\n"
            f"• Срабатываний: `{metrics['summarization']['triggered']}`\n"
            f"• Мягких лимитов: `{metrics['summarization']['soft_limit']}`\n"
            f"• Жёстких лимитов: `{metrics['summarization']['hard_limit']}`\n"
            f"• Токенов сэкономлено: `{metrics['summarization']['tokens_saved']}`\n"
            f"• Средняя длина суммаризации: `{metrics['summarization']['avg_summary_length']:.0f}` символов\n"
        )

        formatted_text, parse_mode = TelegramFormatter.format_text("".join(text_parts))
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
        report_parts = [
            "✅ *Конфигурация перезагружена*\n\n",
            "🔑 *API ключи:*\n",
            f"• Gemini: `{len(new_settings.GEMINI_API_KEYS)}` ключей\n",
            f"• Opencode: `{len(new_settings.OPENCODE_API_KEYS)}` ключей\n",
            f"• OpenRouter: `{len(new_settings.OPENROUTER_API_KEYS)}` ключей\n",
            f"• Tavily: `{len(new_settings.TAVILY_API_KEYS)}` ключей\n\n",
            "🤖 *Модели:*\n",
            f"• Gemini: `{len(new_settings.AVAILABLE_MODELS)}` моделей\n",
            f"• Opencode: `{len(new_settings.OPENCODE_AVAILABLE_MODELS)}` моделей\n",
            f"• OpenRouter: `{len(new_settings.OPENROUTER_AVAILABLE_MODELS)}` моделей\n",
            f"• По умолчанию: `{new_settings.DEFAULT_MODEL}`\n\n",
            "⚙️ *Настройки:*\n",
            f"• PORT: `{new_settings.PORT}`\n",
            f"• ADMIN_ID: `{new_settings.ADMIN_ID}`\n",
            f"• Лимитов моделей: `{len(new_settings.DAILY_LIMITS)}`\n\n",
            "💡 Все настройки загружены из переменных окружения.",
        ]

        formatted_text, parse_mode = TelegramFormatter.format_text("".join(report_parts))
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
            "*🐊 Daily Crocodile:*\n"
            "• `/set_dailycroc_delivery on|off` — включить/выключить исходящую daily-рассылку\n"
            "• `/dailycroc_status` — снимок подписок и игры на сегодня\n"
            "• `/set_dailycroc_placeholder` — реплайни на фото, чтобы задать баннер рассылки\n\n"
            "*🌐 API ключи:*\n"
            "• `/checkgeminikeys` — проверить статус ключей Gemini\n"
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


async def _check_single_gemini_key(client: httpx.AsyncClient, key_hash: str, api_key: str) -> str:
    # A fast, lightweight check bypassing the SDK to avoid retry-loops.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash?key={api_key}"
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            return f"✅ `{key_hash[:8]}...` — ОК"
        elif resp.status_code == 400:
            return f"❌ `{key_hash[:8]}...` — ОШИБКА 400 (Invalid Key/Method)"
        elif resp.status_code == 403:
            return f"🚫 `{key_hash[:8]}...` — ОШИБКА 403 (No Access/Suspended)"
        else:
            return f"⚠️ `{key_hash[:8]}...` — ОШИБКА {resp.status_code}"
    except Exception as e:
        return f"💥 `{key_hash[:8]}...` — СЕТЕВАЯ ОШИБКА ({type(e).__name__})"


@admin_only
async def check_gemini_keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Секретная команда для асинхронной проверки всех ключей Gemini в БД."""
    try:
        msg = await update.message.reply_text("🔄 Запускаю прямую проверку ключей Gemini к Google API...")

        keys_result = await get_all_gemini_keys()
        if not keys_result:
            await msg.edit_text("❌ В базе данных нет ключей Gemini API")
            return

        report_parts = [f"📋 Результаты проверки {len(keys_result)} ключей:\n\n"]
        from app.crypto import safe_decrypt

        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = []
            for row in keys_result:
                key_hash = row["key_hash"]
                api_key = safe_decrypt(row["api_key"])
                tasks.append(_check_single_gemini_key(client, key_hash, api_key))

            results = await asyncio.gather(*tasks)

        for res in results:
            report_parts.append(f"{res}\n")

        report_parts.append("\n💡 *Совет*: Удалите нерабочие ключи из .env и сделайте /reloadconfig")

        report = "".join(report_parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(report)
        await msg.edit_text(text=formatted_text, parse_mode=parse_mode)

    except Exception as e:
        error_msg = f"Ошибка при проверке ключей Gemini: {e}"
        logging.error(error_msg, exc_info=True)
        await update.message.reply_text(f"💥 {error_msg}")


_VALID_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})
_VALID_TABS_VALUES = frozenset({"on", "off"})


@admin_only
async def set_inline_thinking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the inline generation thinking level on-the-fly (no restart required).

    Usage: /set_inline_thinking <level>
    Valid levels: minimal, low, medium, high
    """
    args = context.args or []
    if not args or args[0].lower() not in _VALID_THINKING_LEVELS:
        current = await get_global_setting("inline_thinking_level", settings.INLINE_THINKING_LEVEL)
        await update.message.reply_text(
            f"⚙️ <b>Текущий уровень:</b> <code>{current}</code>\n\n"
            "Использование: <code>/set_inline_thinking &lt;level&gt;</code>\n"
            "Допустимые значения: <code>minimal</code>, <code>low</code>, <code>medium</code>, <code>high</code>\n\n"
            "📝 <b>Рекомендации:</b>\n"
            "• <code>low</code> — оптимальный баланс скорости и качества (default)\n"
            "• <code>medium</code> — лучше для сложных/многошаговых запросов\n"
            "• <code>minimal</code> — максимальная скорость, минимум рассуждений\n"
            "• <code>high</code> — медленно, только для отладки",
            parse_mode="HTML",
        )
        return

    level = args[0].lower()
    await set_global_setting("inline_thinking_level", level)
    logging.info("Admin %s set inline_thinking_level → %s", update.effective_user.id, level)
    await update.message.reply_text(
        f"✅ Уровень размышлений для inline-режима установлен в: <code>{level}</code>\n"
        f"Изменение вступит в силу немедленно (кеш сброшен).",
        parse_mode="HTML",
    )


@admin_only
async def set_inline_tabs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the Tabbed Response UI for inline mode (no restart required).

    Usage: /set_inline_tabs <on|off>
    Default: off
    """
    args = context.args or []
    if not args or args[0].lower() not in _VALID_TABS_VALUES:
        current = await get_global_setting("inline_tabs_enabled", "off")
        await update.message.reply_text(
            f"⚙️ <b>Текущее состояние вкладок:</b> <code>{current}</code>\n\n"
            "Использование: <code>/set_inline_tabs &lt;on|off&gt;</code>\n\n"
            "📝 <b>Описание:</b>\n"
            "• <code>on</code>  — включить структурированные вкладки (TL;DR / Подробнее / Источники)\n"
            "• <code>off</code> — обычный режим (по умолчанию)\n\n"
            "При включении inline-ответы разбиваются на сегменты с кнопками переключения.",
            parse_mode="HTML",
        )
        return

    value = args[0].lower()
    await set_global_setting("inline_tabs_enabled", value)
    logging.info("Admin %s set inline_tabs_enabled → %s", update.effective_user.id, value)
    state_label = "включены ✅" if value == "on" else "выключены ❌"
    await update.message.reply_text(
        f"🗂️ Вкладки для inline-режима: <b>{state_label}</b>\nИзменение вступит в силу немедленно.",
        parse_mode="HTML",
    )


# ── Provider routing ───────────────────────────────────────────────────────────

_VALID_PROVIDERS = frozenset({"opencode", "gemini"})
_VALID_ON_OFF_VALUES = frozenset({"on", "off"})


@admin_only
async def set_provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the PRIMARY_PROVIDER at runtime (no restart required).

    Usage: /set_provider <opencode|gemini>

    - ``opencode``: Route all requests through Opencode Go models first,
      with automatic fallback to Gemini on key exhaustion.
    - ``gemini``: Revert to Gemini-only mode; Opencode Go models are
      still accessible via /model if explicitly selected.
    """
    args = context.args or []
    current_raw = await get_global_setting("primary_provider", settings.PRIMARY_PROVIDER)

    if not args or args[0].lower() not in _VALID_PROVIDERS:
        await update.message.reply_text(
            f"⚙️ <b>Текущий провайдер:</b> <code>{current_raw}</code>\n\n"
            "Использование: <code>/set_provider &lt;opencode|gemini&gt;</code>\n\n"
            "📝 <b>Описание:</b>\n"
            "• <code>opencode</code> — приоритет Opencode Go (с авто-фоллбэком на Gemini)\n"
            "• <code>gemini</code>   — только Gemini (классический режим)\n\n"
            "Смена вступает в силу немедленно без перезапуска.",
            parse_mode="HTML",
        )
        return

    provider = args[0].lower()
    await set_global_setting("primary_provider", provider)

    # Invalidate the in-process cache in config.py so the next request
    # picks up the new value without needing a restart.
    from app.config import _invalidate_primary_provider_cache  # noqa: PLC0415

    _invalidate_primary_provider_cache()

    logging.info("Admin %s switched PRIMARY_PROVIDER → %s", update.effective_user.id, provider)

    label = "🚀 Opencode Go (фоллбэк: Gemini)" if provider == "opencode" else "🔵 Gemini (классический режим)"
    await update.message.reply_text(
        f"✅ <b>Провайдер переключён:</b> {label}\n"
        "Все новые запросы будут маршрутизироваться через выбранный провайдер.",
        parse_mode="HTML",
    )


@admin_only
async def set_dailycroc_delivery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle outgoing Daily Crocodile delivery without stopping preparation."""

    args = context.args or []
    current = await get_global_setting(daily_croc_repo.DAILY_DELIVERY_SETTING_KEY, "on")

    if not args or args[0].lower() not in _VALID_ON_OFF_VALUES:
        state = "включена ✅" if current == "on" else "выключена ❌"
        await update.message.reply_text(
            f"🐊 <b>Текущая daily-отправка:</b> {state}\n\n"
            "Использование: <code>/set_dailycroc_delivery &lt;on|off&gt;</code>\n\n"
            "• <code>on</code> — отправлять ежедневные daily-сообщения и discovery\n"
            "• <code>off</code> — не отправлять daily-сообщения, но продолжать pre-generation пазлов\n",
            parse_mode="HTML",
        )
        return

    value = args[0].lower()
    await set_global_setting(daily_croc_repo.DAILY_DELIVERY_SETTING_KEY, value)
    logging.info("Admin %s set Daily Crocodile delivery → %s", update.effective_user.id, value)

    state = "включена ✅" if value == "on" else "выключена ❌"
    await update.message.reply_text(
        f"🐊 Daily-отправка {state}\n"
        "Pre-generation пазлов, подсказок и изображений продолжает работать.",
        parse_mode="HTML",
    )


@admin_only
async def dailycroc_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a snapshot of today's Daily Crocodile delivery pipeline."""
    from app.games.crocodile_flags import get_crocodile_runtime_switches
    from app.games.crocodile_runtime import get_runtime_health_snapshot
    from app.games.hinting import get_hint_prewarm_health
    from app.providers.gemini import get_vertex_client
    from app.web_miniapp import _get_live_model_cooldown_seconds

    today = daily_croc_repo.today_puzzle_date()
    stats = await daily_croc_repo.get_delivery_status(today)
    puzzles = await daily_croc_repo.get_puzzles_for_date(today)
    delivery_on = await get_global_setting(daily_croc_repo.DAILY_DELIVERY_SETTING_KEY, "on")
    placeholder = await get_global_setting("daily_croc_placeholder_file_id", "")
    switches = await get_crocodile_runtime_switches()
    hint_health = await get_hint_prewarm_health()
    runtime_health = get_runtime_health_snapshot()
    vertex_ready = get_vertex_client() is not None
    live_cooldown = _get_live_model_cooldown_seconds()

    prepared_lines = []
    for difficulty in daily_croc_repo.DAILY_DIFFICULTIES:
        puzzle = puzzles.get(difficulty)
        if not puzzle:
            prepared_lines.append(f"  {difficulty}: <code>missing</code>")
            continue
        prepared_lines.append(
            f"  {difficulty}: <code>{'ready' if daily_croc_repo.is_puzzle_fully_prepared(puzzle) else 'warming'}</code>"
        )

    lines = [
        f"🐊 <b>Daily Crocodile — {today.isoformat()}</b>",
        "",
        f"📨 <b>Рассылка:</b> {'включена ✅' if delivery_on == 'on' else 'выключена ❌'}",
        f"🖼 <b>Placeholder:</b> {'установлен ✅' if placeholder else 'не задан ⚠️'}",
        "",
        "<b>Runtime switches:</b>",
        f"  live_audio_enabled: <code>{'on' if switches.get('live_audio_enabled') else 'off'}</code>",
        f"  crocodile_hint_prewarm_enabled: <code>{'on' if switches.get('crocodile_hint_prewarm_enabled') else 'off'}</code>",
        f"  daily_dual_track_enabled: <code>{'on' if switches.get('daily_dual_track_enabled') else 'off'}</code>",
        "",
        "<b>Health:</b>",
        f"  Vertex Live ready: <code>{'yes' if vertex_ready else 'no'}</code>",
        f"  Live cooldown: <code>{live_cooldown}s</code>",
        f"  Hint queue depth: <code>{hint_health.get('queue_depth', 0)}</code>",
        f"  Hint worker running: <code>{'yes' if hint_health.get('worker_running') else 'no'}</code>",
        f"  Replay buffers: <code>{runtime_health.get('history_buffers', 0)}</code>",
        f"  Pending dedupe buckets: <code>{runtime_health.get('pending_result_buckets', 0)}</code>",
        "",
        "<b>Daily prep:</b>",
        *prepared_lines,
        "",
        "<b>Подписки:</b>",
        f"  Всего подписано:      <code>{stats.get('total_subscribed', 0)}</code>",
        f"  Отправлено сегодня:   <code>{stats.get('sent_today', 0)}</code>",
        f"  Осталось отправить:   <code>{stats.get('pending_today', 0)}</code>",
        "",
        "<b>Игра сегодня:</b>",
        f"  Завершили:  <code>{stats.get('finished', 0)}</code>",
        f"  Выиграли:   <code>{stats.get('won', 0)}</code>",
        f"  В процессе: <code>{stats.get('active', 0)}</code>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@admin_only
async def set_dailycroc_placeholder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a placeholder image file_id for Daily Crocodile delivery.

    Usage: reply to a photo with /set_dailycroc_placeholder
    """
    msg = update.message
    # Accept both a direct photo and a photo replied-to
    photo_msg = msg.reply_to_message if (msg.reply_to_message and msg.reply_to_message.photo) else msg
    if not photo_msg or not photo_msg.photo:
        current = await get_global_setting("daily_croc_placeholder_file_id", "")
        status = f"<code>{current[:40]}…</code>" if current else "<i>не задан</i>"
        await msg.reply_text(
            f"🖼 Текущий placeholder: {status}\n\n"
            "Реплайни на фото командой <code>/set_dailycroc_placeholder</code>, "
            "чтобы сохранить его как баннер ежедневной рассылки.",
            parse_mode="HTML",
        )
        return

    file_id = photo_msg.photo[-1].file_id  # largest size
    await set_global_setting("daily_croc_placeholder_file_id", file_id)

    # Invalidate the in-process cache in the handler module.
    import app.handlers.daily_crocodile as _dc_mod  # noqa: PLC0415
    from app.handlers.daily_crocodile import _PLACEHOLDER_KEY  # noqa: PLC0415

    _dc_mod._placeholder_cache = ""
    _dc_mod._placeholder_cache_ts = 0.0

    await msg.reply_text(
        f"✅ Placeholder сохранён.\n<code>file_id: {file_id[:60]}…</code>",
        parse_mode="HTML",
    )
