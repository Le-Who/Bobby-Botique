# /app/handlers/commands.py
"""Core user commands and handler registration.

Admin commands: see cmd_admin.py
Conversation commands: see cmd_conversations.py
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from app.config import settings
from app.repos.chats import get_user_chat, update_user_chat
from app.repos.conversations import get_conversation_count
from app.utils.formatting import TelegramFormatter
from app.utils.decorators import authorized_only
from app.handlers import menus
from app.request_context import set_request_id


@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    logging.info("Start command from user %s", user_id)

    try:
        chat_state = await get_user_chat(user_id)
        formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(
            chat_state, user_id=user_id
        )

        await update.message.reply_text(
            formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
        )
        logging.info("Start command completed successfully for user %s", user_id)
    except Exception as e:
        logging.error("Error in start command for user %s: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Произошла ошибка при обработке команды. Попробуйте позже."
        )


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показывает подробную справку по использованию бота"""
    user_id = update.effective_user.id
    logging.info("Help command from user %s", user_id)

    try:
        help_text = (
            "📚 **Справка**\n\n"
            "💬 **Чат** — просто напишите сообщение\n"
            "🌐 **Поиск** — `?` или `??` перед вопросом\n"
            "📄 **Документы** — отправьте PDF/DOCX\n"
            "🎭 **Роли** — специализация бота\n\n"
            "Нажмите кнопку для подробностей:"
        )

        formatted_text, parse_mode = TelegramFormatter.format_text(help_text)
        keyboard = [
            [
                InlineKeyboardButton("💬 Чат", callback_data="help_topic:chat"),
                InlineKeyboardButton("🌐 Поиск", callback_data="help_topic:search"),
            ],
            [
                InlineKeyboardButton("📄 Документы", callback_data="help_topic:docs"),
                InlineKeyboardButton("🎭 Роли", callback_data="help_topic:roles"),
            ],
        ]
        await update.message.reply_text(
            formatted_text, parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logging.info("Help command completed successfully for user %s", user_id)
    except Exception as e:
        logging.error("Error in help command for user %s: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Произошла ошибка при обработке команды. Попробуйте позже."
        )


@authorized_only
async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

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
    if command_arg in ("clear", "reset") and len(context.args) == 1:
        chat_state.system_prompt = None
        await update_user_chat(user_id, chat_state)
        await update.message.reply_text(
            "✅ Системная инструкция сброшена. Использую стандартное поведение."
        )
        return

    # Set new prompt
    chat_state.system_prompt = " ".join(context.args)
    await update_user_chat(user_id, chat_state)

    # Show preview of what was set
    preview = (
        chat_state.system_prompt[:100] + "..."
        if len(chat_state.system_prompt) > 100
        else chat_state.system_prompt
    )
    formatted_text, parse_mode = TelegramFormatter.format_text(
        f"✅ Системная инструкция обновлена:\n`{preview}`"
    )
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
async def roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    text, parse_mode, reply_markup = await menus.get_roles_menu_content(
        user_id, chat_state
    )
    await update.message.reply_text(text, reply_markup=reply_markup)


@authorized_only
async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    set_request_id(f"tgcmd-newchat-{chat_id}-{getattr(update, 'update_id', 'na')}")

    chat_state = await get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await update_user_chat(user_id, chat_state)

    text = (
        "✨ **Новый чат начат!**\n\n"
        "Контекст и роль сброшены. Напишите что-нибудь. 👇"
    )

    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    keyboard = [
        [
            InlineKeyboardButton("🎭 Начать с роли", callback_data="open_roles"),
            InlineKeyboardButton("🧠 Сменить модель", callback_data="model_menu"),
        ]
    ]

    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@authorized_only
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(
        chat_state, context
    )

    if reply_markup is None:
        await update.message.reply_text(formatted_text)
    else:
        await update.message.reply_text(
            formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
        )


@authorized_only
async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"

    # Используем TelegramFormatter for правильного экранирования
    formatted_text, parse_mode = TelegramFormatter.format_text(
        f"🌐 Постоянный режим исследования *{status_text}*."
    )
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    """Показывает список документов пользователя и управляет ими"""

    # Clean up state работы с documentами on входе в команду
    from app.state import clear_document_state

    clear_document_state(update.effective_user.id)

    try:
        (
            formatted_text,
            parse_mode,
            reply_markup,
        ) = await menus.get_documents_menu_content(update.effective_user.id)
        await update.message.reply_text(
            formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка получения документов: {e}")
        logging.error("Error in documents command: %s", e, exc_info=True)


@authorized_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает личную статистику пользователя"""
    user_id = update.effective_user.id
    logging.info("Stats command from user %s", user_id)

    try:
        from app.repos.analytics import get_engagement_summary, streak_badge

        # Engagement summary (streak + 7-day stats)
        engagement = await get_engagement_summary(user_id)
        streak = engagement["current_streak"]
        badge = streak_badge(streak)

        # Сегодня (personal — per-user metrics)
        from app.repos.user_stats import (
            get_user_today_request_count,
            get_user_weekly_stats,
            get_user_model_usage_today,
        )
        today_count = await get_user_today_request_count(user_id)

        # Последние 7 дней (personal)
        week_res = await get_user_weekly_stats(user_id)

        # Использование моделей за сегодня (personal, from JSONB model_usage)
        model_res = await get_user_model_usage_today(user_id)

        # Документы
        from app.document_processor import get_user_documents
        docs = await get_user_documents(user_id)
        doc_count = len(docs) if docs else 0

        # Беседы
        conv_count = await get_conversation_count(user_id)

        # Build text
        text = "📊 **Ваша статистика**\n\n"

        # Streak badge
        if streak > 0:
            text += f"{badge} **Серия:** `{streak}` {'день' if streak == 1 else 'дней'}\n"
            if engagement["longest_streak"] > streak:
                text += f"🏆 **Рекорд:** `{engagement['longest_streak']}` дней\n"
            text += "\n"

        text += f"📅 **Сегодня:** `{today_count}` запросов\n"
        text += f"📈 **7 дней:** `{engagement['total_requests_7d']}` запросов ({engagement['active_days_7d']}/7 дней)\n\n"

        # Недельная history
        if week_res:
            text += "📊 **По дням:**\n"
            for row in week_res:
                date_str = row["metric_date"].strftime("%d.%m") if hasattr(row["metric_date"], "strftime") else str(row["metric_date"])[:5]
                bar = "█" * min(int(row["cnt"]), 20)
                text += f"  `{date_str}` {bar} `{row['cnt']}`\n"
            text += "\n"

        # Модели
        if model_res:
            text += "🤖 **Модели сегодня:**\n"
            for row in model_res:
                text += f"  • `{row['model_name']}`: `{row['cnt']}` запросов\n"
            text += "\n"

        text += (
            f"📄 **Документов:** `{doc_count}`\n"
            f"📝 **Сохранённых бесед:** `{conv_count}`\n"
        )

        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        logging.info("Stats command completed for user %s", user_id)

    except Exception as e:
        logging.error("Error in stats command for user %s: %s", user_id, e, exc_info=True)
        await update.message.reply_text(
            "❌ Ошибка получения статистики. Попробуйте позже."
        )


def register(application: Application) -> None:
    # Core user commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("newchat", new_chat_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("setprompt", set_prompt_command))
    application.add_handler(CommandHandler("res", research_mode_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("documents", documents_command))
    application.add_handler(CommandHandler("roles", roles_command))

    # Admin commands (from cmd_admin)
    from app.handlers.cmd_admin import (
        list_models_command, add_user_command, del_user_command,
        list_users_command, metrics_command, cache_stats_command,
        queue_stats_command, clear_cache_command, clear_old_metrics_command,
        update_tavily_keys_command, check_tavily_keys_command,
        register_group_command, group_stats_command,
        document_stats_command, clear_old_documents_command,
        role_conv_metrics_command, reload_config_command, admin_command,
    )

    application.add_handler(CommandHandler("listmodels", list_models_command))
    application.add_handler(CommandHandler("adduser", add_user_command))
    application.add_handler(CommandHandler("deluser", del_user_command))
    application.add_handler(CommandHandler("listusers", list_users_command))
    application.add_handler(CommandHandler("metrics", metrics_command))
    application.add_handler(CommandHandler("cachestats", cache_stats_command))
    application.add_handler(CommandHandler("queuestats", queue_stats_command))
    application.add_handler(CommandHandler("clearcache", clear_cache_command))
    application.add_handler(CommandHandler("clearoldmetrics", clear_old_metrics_command))
    application.add_handler(CommandHandler("clearolddocs", clear_old_documents_command))
    application.add_handler(CommandHandler("docstats", document_stats_command))
    application.add_handler(CommandHandler("updatetavilykeys", update_tavily_keys_command))
    application.add_handler(CommandHandler("checktavilykeys", check_tavily_keys_command))
    application.add_handler(CommandHandler("registergroup", register_group_command))
    application.add_handler(CommandHandler("groupstats", group_stats_command))
    application.add_handler(CommandHandler("rolemetrics", role_conv_metrics_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("reloadconfig", reload_config_command))

    # Conversation commands (from cmd_conversations)
    from app.handlers.cmd_conversations import (
        save_conversation_command, conversations_command,
        switch_conversation_command, rename_conversation_command,
        delete_conversation_command,
    )

    application.add_handler(CommandHandler("save", save_conversation_command))
    application.add_handler(CommandHandler("conversations", conversations_command))
    application.add_handler(CommandHandler("switch", switch_conversation_command))
    application.add_handler(CommandHandler("rename", rename_conversation_command))
    application.add_handler(CommandHandler("delete", delete_conversation_command))
