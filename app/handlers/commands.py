# /app/handlers/commands.py
"""Core user commands and handler registration.

Admin commands: see cmd_admin.py
Conversation commands: see cmd_conversations.py
"""

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.bot_commands import (
    build_help_topic_rows,
    language_from_telegram,
    render_help_overview,
)
from app.handlers import menus
from app.i18n import t
from app.repos.chats import get_user_chat, update_user_chat
from app.repos.conversations import get_conversation_count
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter
from app.utils.json_compat import json


async def ignore_edited_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop edited command updates before CommandHandler sees effective_message."""
    raise ApplicationHandlerStop


@authorized_only
@safe_handler(t("error.command"))
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logging.info("Start command from user %s", user_id)

    # ── Deep link routing ────────────────────────────────────────────────────
    payload = (context.args[0] if context.args else "").strip()
    
    if payload.startswith("ctx_"):
        import html as _html

        from app.cache import get_inline_context

        token = payload[4:]  # strip "ctx_" prefix
        ctx = await get_inline_context(token)

        if ctx is None:
            await update.message.reply_text(
                "⏰ <b>Ссылка устарела.</b>\n"
                "Контекст хранится 24 часа. Задай вопрос боту напрямую.",
                parse_mode="HTML",
            )
            return

        chat_state = await get_user_chat(user_id)
        # Append inline exchange to existing history (it's the most recent interaction)
        inline_history = [
            {"role": "user", "parts": [ctx["q"]]},
            {"role": "model", "parts": [ctx["a"]]},
        ]
        # Keep up to 20 previous messages, then append the new 2 inline messages
        chat_state.history = chat_state.history[-20:] + inline_history
        # Force a full rewrite in the DB because we replaced the list 
        # (bypasses the bolt optimization that assumes only append-only changes)
        chat_state._original_length = 0
        await update_user_chat(user_id, chat_state)

        tone_hint = ""
        if ctx.get("tone") == "formal":
            tone_hint = " (официальный тон)"
        elif ctx.get("tone") == "friendly":
            tone_hint = " (дружеский тон)"
        elif ctx.get("tone") == "sarcastic":
            tone_hint = " (саркастический тон)"

        preview_q = ctx["q"][:80] + ("…" if len(ctx["q"]) > 80 else "")

        await update.message.reply_text(
            f"📎 <b>Контекст загружен</b>{tone_hint}\n\n"
            f"<i>Исходный вопрос:</i> <code>{_html.escape(preview_q)}</code>\n\n"
            "Можешь продолжать — я помню этот разговор.",
            parse_mode="HTML",
        )
        return

    if payload.startswith("trivia_q_"):
        # "Узнать больше (ИИ)" deep link from Daily Trivia review screen.
        # Payload format: trivia_q_{YYYYMMDD}_{question_index}
        # e.g.  trivia_q_20260724_2  → puzzle 2026-07-24, question index 2
        import html as _html
        from datetime import date as _date

        from app.repos.daily_trivia import get_question_by_date_and_index

        parts = payload[9:].rsplit("_", 1)  # strip "trivia_q_", split on last "_"
        try:
            date_str, idx_str = parts
            puzzle_date = _date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
            is_super = idx_str.startswith("s")
            question_index = int(idx_str[1:] if is_super else idx_str)
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Некорректная ссылка.", parse_mode="HTML")
            return

        q = await get_question_by_date_and_index(puzzle_date, question_index, is_super=is_super)
        if q is None:
            await update.message.reply_text(
                "❌ <b>Вопрос не найден.</b>\n"
                "Возможно, данные обновились. Открой Викторину и попробуй снова.",
                parse_mode="HTML",
            )
            return

        correct_answer = q.options[q.correct_index] if 0 <= q.correct_index < len(q.options) else ""
        ai_prompt = (
            f"Вопрос викторины: «{q.question}»\n"
            f"Правильный ответ: «{correct_answer}»\n"
            f"Краткий факт: «{q.explanation}»\n\n"
            "Расскажи об этом подробнее — интересно, живо, с реальными деталями. "
            "Около 150–200 слов, без лишних вступлений."
        )

        thinking_msg = await update.message.reply_text("🔍 Узнаю подробности…")

        try:
            from app.config import settings as _settings
            from app.providers import get_provider_router
            router = get_provider_router()
            ai_history = [{"role": "user", "parts": [ai_prompt]}]
            ai_response, _ = await router.get_response(
                _settings.DEFAULT_MODEL,
                ai_history,
                user_id=user_id,
            )
        except Exception as exc:
            logging.warning("trivia_q deep link AI failed user=%s: %s", user_id, exc)
            await thinking_msg.delete()
            # Fallback: show the fact directly, cleanly formatted
            await update.message.reply_text(
                f"🧠 <b>{_html.escape(q.question)}</b>\n"
                f"<i>Правильный ответ: {_html.escape(correct_answer)}</i>\n\n"
                f"💡 {_html.escape(q.explanation)}",
                parse_mode="HTML",
            )
            return

        # Write q+a to history so the user can continue the conversation
        chat_state = await get_user_chat(user_id)
        chat_state.history = chat_state.history[-20:] + [
            {"role": "user", "parts": [ai_prompt]},
            {"role": "model", "parts": [ai_response]},
        ]
        chat_state._original_length = 0
        await update_user_chat(user_id, chat_state)

        await thinking_msg.delete()
        header = (
            f"🧠 <b>{_html.escape(q.question)}</b>\n"
            f"<i>Правильный ответ: {_html.escape(correct_answer)}</i>\n\n"
        )
        full_text = header + ai_response
        if len(full_text) > 4096:
            full_text = full_text[:4090] + "…"
        await update.message.reply_text(full_text, parse_mode="HTML")
        return

    if payload.startswith("subscribe_horoscope_"):
        from app.handlers.horoscope_subscription import start_subscribe_horoscope

        context.user_data["horo_payload"] = payload
        await start_subscribe_horoscope(update, context)
        return

    chat_state = await get_user_chat(user_id)
    formatted_text, parse_mode, reply_markup = await menus.get_start_menu_content(chat_state, user_id=user_id)

    await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
    logging.info("Start command completed successfully for user %s", user_id)


@authorized_only
@safe_handler(t("error.command"))
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show localized public capabilities without exposing admin commands."""
    user_id = update.effective_user.id
    logging.info("Help command from user %s", user_id)

    lang = language_from_telegram(getattr(update.effective_user, "language_code", None))
    if update.callback_query:
        await update.callback_query.answer()

    chat_id = update.effective_chat.id if update.effective_chat else user_id
    await context.bot.send_message(
        chat_id=chat_id,
        text=render_help_overview(lang),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(build_help_topic_rows(lang)),
    )
    logging.info("Help command completed successfully for user %s", user_id)


@authorized_only
async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for получения argumentов команды
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    if not context.args:
        # UX Improvement: Show current status instead of clearing
        current_prompt = chat_state.system_prompt
        prompt_display = f"`{current_prompt}`" if current_prompt else "_(не задана, используется стандартная)_"

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
        await update.message.reply_text("✅ Системная инструкция сброшена. Использую стандартное поведение.")
        return

    # Set new prompt
    chat_state.system_prompt = " ".join(context.args)
    await update_user_chat(user_id, chat_state)

    # Show preview of what was set
    preview = (
        chat_state.system_prompt[:100] + "..." if len(chat_state.system_prompt) > 100 else chat_state.system_prompt
    )
    formatted_text, parse_mode = TelegramFormatter.format_text(f"✅ Системная инструкция обновлена:\n`{preview}`")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
async def roles_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    text, _, reply_markup = await menus.get_roles_menu_content(user_id, chat_state)
    await update.message.reply_text(text, reply_markup=reply_markup)


@authorized_only
async def new_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id

    chat_state = await get_user_chat(user_id)
    chat_state.history = []
    chat_state.token_count = 0
    chat_state.system_prompt = None
    await update_user_chat(user_id, chat_state)

    from app.middleware.dedup import clear_user_dedup

    clear_user_dedup(user_id)

    if context and context.user_data is not None:
        keys_to_clear = [k for k in context.user_data if str(k).startswith("voice_pending")]
        for k in keys_to_clear:
            context.user_data.pop(k, None)

    text = t("chat.new_started")

    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    keyboard = [
        [
            InlineKeyboardButton(t("chat.start_with_role"), callback_data="open_roles"),
            InlineKeyboardButton(t("chat.change_model"), callback_data="model_menu"),
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

    formatted_text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, context)

    if reply_markup is None:
        await update.message.reply_text(formatted_text)
    else:
        await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)


@authorized_only
async def research_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # context используется for совместимости с другими командами
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)
    chat_state.search_enabled = not chat_state.search_enabled
    await update_user_chat(user_id, chat_state)
    status_text = "ВКЛЮЧЕН" if chat_state.search_enabled else "ВЫКЛЮЧЕН"

    # Используем TelegramFormatter for правильного экранирования
    formatted_text, parse_mode = TelegramFormatter.format_text(f"🌐 Постоянный режим исследования *{status_text}*.")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
@safe_handler(t("error.command"))
async def documents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список документов пользователя и управляет ими"""
    from app.state import clear_document_state

    clear_document_state(update.effective_user.id)

    formatted_text, parse_mode, reply_markup = await menus.get_documents_menu_content(update.effective_user.id)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)


@authorized_only
@safe_handler(t("error.command"))
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает личную статистику пользователя"""
    user_id = update.effective_user.id
    logging.info("Stats command from user %s", user_id)

    from app.repos.analytics import get_engagement_summary, streak_badge

    engagement = await get_engagement_summary(user_id)
    streak = engagement["current_streak"]
    badge = streak_badge(streak)

    from app.repos.user_stats import (
        get_user_model_usage_today,
        get_user_today_request_count,
        get_user_weekly_stats,
    )

    today_count = await get_user_today_request_count(user_id)
    week_res = await get_user_weekly_stats(user_id)
    model_res = await get_user_model_usage_today(user_id)

    from app.document_processor import get_user_documents

    docs = await get_user_documents(user_id)
    doc_count = len(docs) if docs else 0
    conv_count = await get_conversation_count(user_id)

    text_parts = ["📊 **Ваша статистика**\n\n"]

    if streak > 0:
        text_parts.append(f"{badge} **Серия:** `{streak}` {'день' if streak == 1 else 'дней'}\n")
        if engagement["longest_streak"] > streak:
            text_parts.append(f"🏆 **Рекорд:** `{engagement['longest_streak']}` дней\n")
        text_parts.append("\n")

    text_parts.append(f"📅 **Сегодня:** `{today_count}` запросов\n")
    text_parts.append(
        f"📈 **7 дней:** `{engagement['total_requests_7d']}` запросов ({engagement['active_days_7d']}/7 дней)\n\n"
    )

    if week_res:
        text_parts.append("📊 **По дням:**\n")
        for row in week_res:
            date_str = (
                row["metric_date"].strftime("%d.%m")
                if hasattr(row["metric_date"], "strftime")
                else str(row["metric_date"])[:5]
            )
            bar = "█" * min(int(row["cnt"]), 20)
            text_parts.append(f"  `{date_str}` {bar} `{row['cnt']}`\n")
        text_parts.append("\n")

    if model_res:
        text_parts.append("🤖 **Модели сегодня:**\n")
        for row in model_res:
            text_parts.append(f"  • `{row['model_name']}`: `{row['cnt']}` запросов\n")
        text_parts.append("\n")

    text_parts.append(f"📄 **Документов:** `{doc_count}`\n📝 **Сохранённых бесед:** `{conv_count}`\n")
    text = "".join(text_parts)

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)
    logging.info("Stats command completed for user %s", user_id)


@authorized_only
@safe_handler(t("error.command"))
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export the current active chat as a Markdown document."""
    import io

    user_id = update.effective_user.id
    logging.info("Export command from user %s", user_id)

    chat_state = await get_user_chat(user_id)
    if not chat_state or not chat_state.history:
        await update.message.reply_text("📭 Нет активного чата для экспорта.\nНачните диалог и попробуйте снова.")
        return

    # Build Markdown
    lines = ["# Экспорт чата GemAI Bot\n"]
    if chat_state.model:
        lines.append(f"**Модель:** `{chat_state.model}`\n")
    lines.append(f"**Сообщений:** {len(chat_state.history)}\n")
    lines.append("---\n")

    for msg in chat_state.history:
        role = msg.get("role", "unknown")
        parts = msg.get("parts", [])
        content = parts[0] if parts else ""
        if isinstance(content, dict):
            content = content.get("text", str(content))
        role_label = "👤 **Вы**" if role == "user" else "🤖 **AI**"
        lines.append(f"### {role_label}\n")
        lines.append(f"{content}\n")
        lines.append("---\n")

    md_text = "\n".join(lines)
    md_bytes = md_text.encode("utf-8")
    doc = io.BytesIO(md_bytes)
    doc.name = "chat_export.md"

    await update.message.reply_document(
        document=doc,
        filename="chat_export.md",
        caption=f"📄 Экспорт чата ({len(chat_state.history)} сообщений)",
    )
    logging.info("Export completed for user %s: %d messages", user_id, len(chat_state.history))


_VALID_THINKING_LEVELS = {"off", "low", "medium", "high"}
_THINKING_LABELS = {
    None: "🔄 Авто (по умолчанию модели)",
    "off": "⚡ Выключен — быстрые ответы",
    "low": "💡 Низкий — минимальное рассуждение",
    "medium": "🧠 Средний — сбалансированный",
    "high": "🔬 Высокий — глубокий анализ",
}


@authorized_only
async def thinking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the thinking level for AI reasoning."""
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    if not context.args:
        # Show current level
        current = chat_state.thinking_level
        label = _THINKING_LABELS.get(current, current)
        text = (
            f"🧠 *Текущий уровень мышления:* {label}\n\n"
            "*Доступные уровни:*\n"
            "• `/thinking off` — ⚡ быстрые ответы без рассуждений\n"
            "• `/thinking low` — 💡 минимальное рассуждение\n"
            "• `/thinking medium` — 🧠 сбалансированный\n"
            "• `/thinking high` — 🔬 максимальная глубина\n"
            "• `/thinking auto` — 🔄 вернуть авто-режим\n\n"
            f"Текущая модель: `{chat_state.model}`"
        )
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        return

    level: str | None = context.args[0].lower()

    if level in ("auto", "default", "reset"):
        level = None
    elif level not in _VALID_THINKING_LEVELS:
        await update.message.reply_text(
            f"❌ Неизвестный уровень `{level}`.\nДопустимые: `off`, `low`, `medium`, `high`, `auto`"
        )
        return

    from app.repos.chats import update_thinking_level

    await update_thinking_level(user_id, level)
    chat_state.thinking_level = level

    label = _THINKING_LABELS.get(level, level)
    formatted_text, parse_mode = TelegramFormatter.format_text(f"✅ Уровень мышления: {label}")
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
@safe_handler(t("error.command"))
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unified user preferences with inline controls."""
    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    model = chat_state.model or "(по умолчанию)"
    thinking = _THINKING_LABELS.get(chat_state.thinking_level, chat_state.thinking_level or "🔄 Авто")
    search = "✅ Включён" if chat_state.search_enabled else "❌ Выключен"
    role = chat_state.system_prompt
    if role and len(role) > 60:
        role = role[:60] + "…"
    elif not role:
        role = "(стандартная)"

    text = (
        "⚙️ **Настройки**\n\n"
        f"🧠 **Модель:** `{model}`\n"
        f"💡 **Мышление:** {thinking}\n"
        f"🌐 **Поиск:** {search}\n"
        f"🎭 **Роль:** {role}\n"
        f"📚 **Долгосрочная память:** {'✅ Включена' if chat_state.ltm_enabled else '❌ Выключена'}\n"
    )

    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    keyboard = [
        [
            InlineKeyboardButton(t("settings.btn_change_model"), callback_data="model_menu"),
            InlineKeyboardButton(t("settings.btn_thinking"), callback_data="settings_thinking"),
        ],
        [
            InlineKeyboardButton(t("settings.btn_search"), callback_data="toggle_search"),
            InlineKeyboardButton(t("settings.btn_roles"), callback_data="open_roles"),
        ],
        [
            InlineKeyboardButton(
                f"📚 Память: {'Вкл' if chat_state.ltm_enabled else 'Выкл'}",
                callback_data="toggle_ltm",
            ),
        ],
    ]

    # Add Mini App button if WEBHOOK_URL is configured (HTTPS required for WebApp)
    webapp_base = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
    if webapp_base and webapp_base.startswith("https://"):
        keyboard.append(
            [
                InlineKeyboardButton(
                    "📱 Открыть панель настроек",
                    web_app=WebAppInfo(url=f"{webapp_base}/webapp/?tab=settings"),
                ),
            ]
        )
    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── GDPR commands ────────────────────────────────────────────────────────────


@authorized_only
@safe_handler(t("error.command"))
async def mydata_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export chat settings, conversations, and long-term memory as JSON."""
    import io

    if getattr(update.effective_chat, "type", None) != "private":
        await update.message.reply_text(
            "🔒 Экспорт личных данных доступен только в приватном чате с ботом."
        )
        return

    user_id = update.effective_user.id
    chat_state = await get_user_chat(user_id)

    # Gather all user data
    chat_settings = {
        "model": chat_state.model,
        "thinking_level": chat_state.thinking_level,
        "search_enabled": chat_state.search_enabled,
        "system_prompt": chat_state.system_prompt,
        "context_summary": getattr(chat_state, "context_summary", None),
        "token_count": chat_state.token_count,
        "ltm_enabled": getattr(chat_state, "ltm_enabled", False),
        "memory_epoch": getattr(chat_state, "memory_epoch", 0),
        "branch_id": getattr(chat_state, "branch_id", None),
        "temperature": getattr(chat_state, "temperature", None),
        "voice_id": getattr(chat_state, "voice_id", None),
        "tts_temperature": getattr(chat_state, "tts_temperature", None),
        "live_voice_name": getattr(chat_state, "live_voice_name", None),
        "live_thinking_level": getattr(chat_state, "live_thinking_level", None),
        "live_connection_mode": getattr(chat_state, "live_connection_mode", None),
        "is_deep_dive": getattr(chat_state, "is_deep_dive", False),
        "deep_dive_thread_id": getattr(chat_state, "deep_dive_thread_id", None),
    }
    user_data = {
        "user_id": user_id,
        "username": update.effective_user.username,
        "current_model": chat_state.model,
        "thinking_level": chat_state.thinking_level,
        "search_enabled": chat_state.search_enabled,
        "conversation_history_length": len(chat_state.history),
        "token_count": chat_state.token_count,
        "has_system_prompt": bool(chat_state.system_prompt),
        "chat_settings": chat_settings,
        "active_conversation": chat_state.history,
    }

    # Add conversation count
    try:
        conv_count = await get_conversation_count(user_id)
        user_data["total_conversations"] = conv_count
    except Exception:
        user_data["total_conversations"] = "unknown"

    from app.repos.conversations import export_user_conversations

    user_data["saved_conversations"] = await export_user_conversations(user_id)

    # Export the actual LTM/knowledge-graph records.  This call is intentionally
    # required: returning a successful but incomplete GDPR archive would be
    # misleading if the memory export failed.
    from app.repos.memory import export_user_memory

    user_data["long_term_memory"] = await export_user_memory(user_id)

    # Add aggregate memory stats as a convenience, without making them a
    # prerequisite for the complete record export above.
    try:
        from app.repos.memory import get_memory_stats

        mem_stats = await get_memory_stats(user_id)
        user_data["memories"] = mem_stats
    except Exception:
        pass

    data_json = json.dumps(user_data, indent=2, ensure_ascii=False, default=str)
    doc = io.BytesIO(data_json.encode("utf-8"))
    doc.name = f"user_data_{user_id}.json"

    await update.message.reply_document(
        document=doc,
        caption=(
            "📦 Экспорт данных чата: настройки, активная и сохранённая история, "
            "долгосрочная память и связанные с ней факты."
        ),
    )


@authorized_only
@safe_handler(t("error.command"))
async def deleteme_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Request account deletion with confirmation (GDPR Article 17)."""
    user_id = update.effective_user.id

    if getattr(update.effective_chat, "type", None) != "private":
        await update.message.reply_text(
            "🔒 Удаление аккаунта доступно только в приватном чате с ботом."
        )
        return

    from app.repos.users import erase_user_account, is_admin

    if is_admin(user_id):
        await update.message.reply_text(
            "🛡️ Системный администратор не может удалить свой аккаунт этой командой."
        )
        return

    # Check if already in confirmation
    args = (update.message.text or "").split()
    if len(args) > 1 and args[1].upper() == "CONFIRM":
        await update.message.reply_text("🗑️ Удаление данных...")
        try:
            await erase_user_account(user_id)
        except Exception as exc:
            logging.error("Account erasure failed for user %s: %s", user_id, exc, exc_info=True)
            await update.message.reply_text(
                "❌ Не удалось удалить аккаунт: изменения отменены. Попробуйте позже."
            )
            return

        text = (
            "✅ **Аккаунт и сохранённые данные удалены**\n\n"
            "Удалены беседы, сообщения, документы, настройки, подписки, "
            "игровая статистика и долгосрочная память. Идентификатор в общих "
            "объектах обезличен, а администрирование групп передано участнику."
        )
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        return

    # Show confirmation prompt
    text = (
        "⚠️ **Запрос на удаление данных**\n\n"
        "Эта операция удалит:\n"
        "• Все беседы и историю сообщений\n"
        "• Все сохранённые воспоминания\n"
        "• Все пользовательские настройки\n\n"
        "**Это действие необратимо!**\n\n"
        "Для подтверждения отправьте:\n"
        "`/deleteme CONFIRM`"
    )
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
@safe_handler(t("error.command"))
async def clearmemory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all long-term semantic memories for the user."""
    user_id = update.effective_user.id

    from app.repos.memory import delete_user_memories

    try:
        deleted = await delete_user_memories(user_id)
    except Exception as exc:
        logging.error("Memory deletion failed for user %s: %s", user_id, exc, exc_info=True)
        await update.message.reply_text(
            "❌ Не удалось очистить долгосрочную память: изменения отменены. Попробуйте позже."
        )
        return
    formatted_text, parse_mode = TelegramFormatter.format_text(
        f"🗑️ Удалено **{deleted}** воспоминаний из долгосрочной памяти."
    )
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


@authorized_only
@safe_handler("Произошла ошибка при запуске Live Audio")
async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Launch the Gemini Live Audio Mini App."""
    webapp_base = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
    if not webapp_base or not webapp_base.startswith("https://"):
        await update.message.reply_text(
            "🎙️ Live Audio сейчас недоступно. Попробуйте позже или продолжите общение сообщениями."
        )
        return

    text = "🎙️ **Gemini Live Audio**\n\nНажмите кнопку ниже, чтобы начать голосовое общение в реальном времени."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    keyboard = [
        [
            InlineKeyboardButton(
                "🎙️ Начать Live звонок",
                web_app=WebAppInfo(url=f"{webapp_base}/webapp/live"),
            )
        ]
    ]

    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@authorized_only
@safe_handler("Произошла ошибка при запуске игрового хаба")
async def games_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Launch the external CC-GH Telegram Mini App."""
    from app.config import settings

    game_hub_url = getattr(settings, "GAME_HUB_URL", "").strip().rstrip("/")
    if not game_hub_url or not game_hub_url.startswith("https://"):
        await update.message.reply_text("❌ Игровой хаб сейчас не настроен.")
        return

    direct_link = getattr(settings, "GAME_HUB_DIRECT_LINK", "").strip()
    if not direct_link:
        short_name = getattr(settings, "GAME_HUB_MINIAPP_SHORT_NAME", "games").strip()
        bot_username = getattr(getattr(context, "bot", None), "username", "") or ""
        direct_link = f"https://t.me/{bot_username}/{short_name}" if bot_username and short_name else game_hub_url

    text = "🎮 **Игровой хаб**\n\nОткройте коллекцию мини-игр."
    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    chat_type = getattr(getattr(update, "effective_chat", None), "type", "")
    if chat_type == "private":
        button = InlineKeyboardButton("🎮 Играть", web_app=WebAppInfo(url=game_hub_url))
    else:
        button = InlineKeyboardButton("🎮 Играть", url=direct_link)

    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup([[button]]),
    )


def register(application: Application) -> None:
    application.add_handler(
        MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.COMMAND, ignore_edited_command),
        group=-100,
    )

    # Core user commands
    application.add_handler(CommandHandler("start", start_command))
    from app.handlers.natal_chart import build_natal_chart_handler
    from app.natal.city_catalog import warm_city_catalog

    try:
        warm_city_catalog()
    except Exception as exc:
        logging.warning("Failed to warm natal city catalog: %s", exc)
    application.add_handler(build_natal_chart_handler())
    application.add_handler(CommandHandler("live", live_command))
    application.add_handler(CommandHandler("games", games_command))
    from app.handlers.daily_2048 import daily2048_command
    from app.handlers.daily_crocodile import dailycroc_command
    from app.handlers.daily_trivia import daily_trivia_command

    application.add_handler(CommandHandler("dailycroc", dailycroc_command))
    application.add_handler(CommandHandler("daily2048", daily2048_command))
    application.add_handler(CommandHandler(["trivia", "dailytrivia"], daily_trivia_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(help_command, pattern=r"^help_cmd$"))
    application.add_handler(CommandHandler("newchat", new_chat_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("setprompt", set_prompt_command))
    application.add_handler(CommandHandler("res", research_mode_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("documents", documents_command))
    application.add_handler(CommandHandler("roles", roles_command))
    application.add_handler(CommandHandler("thinking", thinking_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("mydata", mydata_command))
    application.add_handler(CommandHandler("deleteme", deleteme_command))
    application.add_handler(CommandHandler("clearmemory", clearmemory_command))
    
    from app.handlers.cmd_tarot import tarot_command
    application.add_handler(CommandHandler("tarot", tarot_command))
    application.add_handler(MessageHandler(filters.Regex(r'(?i)^/(?:таро|расклад)(?:\s+|$)'), tarot_command))
    application.add_handler(CallbackQueryHandler(tarot_command, pattern=r"^start_tarot$"))

    # Image generation commands (/draw, /img, /image, /generate)
    from app.handlers.cmd_image import draw_command

    application.add_handler(CommandHandler(["draw", "img", "image", "generate"], draw_command))

    # Admin commands (from cmd_admin)
    from app.handlers.cmd_admin import (
        add_user_command,
        admin_command,
        cache_stats_command,
        check_gemini_keys_command,
        check_tavily_keys_command,
        clear_cache_command,
        clear_old_documents_command,
        clear_old_metrics_command,
        dailycroc_status_command,
        del_user_command,
        document_stats_command,
        group_stats_command,
        list_models_command,
        list_users_command,
        metrics_command,
        queue_stats_command,
        register_group_command,
        reload_config_command,
        role_conv_metrics_command,
        set_daily2048_cover_command,
        set_daily_game_command,
        set_dailycroc_delivery_command,
        set_dailycroc_model_command,
        set_dailycroc_placeholder_command,
        set_dailytrivia_cover_command,
        set_game_cover_command,
        set_inline_model_command,
        set_inline_tabs_command,
        set_inline_thinking_command,
        set_provider_command,
        update_tavily_keys_command,
        wb_callback,
        wordbank_command,
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
    application.add_handler(CommandHandler("checkgeminikeys", check_gemini_keys_command))
    application.add_handler(CommandHandler("registergroup", register_group_command))
    application.add_handler(CommandHandler("groupstats", group_stats_command))
    application.add_handler(CommandHandler("set_dailycroc_delivery", set_dailycroc_delivery_command))
    application.add_handler(CommandHandler("set_daily_game", set_daily_game_command))
    application.add_handler(CommandHandler("set_dailycroc_model", set_dailycroc_model_command))
    application.add_handler(CommandHandler("set_inline_model", set_inline_model_command))
    application.add_handler(CommandHandler("dailycroc_status", dailycroc_status_command))
    application.add_handler(CommandHandler("set_dailycroc_placeholder", set_dailycroc_placeholder_command))
    application.add_handler(CommandHandler("set_daily2048_cover", set_daily2048_cover_command))
    application.add_handler(CommandHandler("set_dailytrivia_cover", set_dailytrivia_cover_command))
    application.add_handler(CommandHandler("set_game_cover", set_game_cover_command))
    application.add_handler(CommandHandler("rolemetrics", role_conv_metrics_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("reloadconfig", reload_config_command))
    application.add_handler(CommandHandler("set_inline_thinking", set_inline_thinking_command))
    application.add_handler(CommandHandler("set_inline_tabs", set_inline_tabs_command))
    application.add_handler(CommandHandler("set_provider", set_provider_command))
    application.add_handler(CommandHandler("wordbank", wordbank_command))


    application.add_handler(CallbackQueryHandler(wb_callback, pattern=r"^wb:"))

    # Conversation commands (from cmd_conversations)
    from app.handlers.cmd_conversations import (
        conversations_command,
        delete_conversation_command,
        rename_conversation_command,
        save_conversation_command,
        switch_conversation_command,
    )

    application.add_handler(CommandHandler("save", save_conversation_command))
    application.add_handler(CommandHandler("conversations", conversations_command))
    application.add_handler(CommandHandler("switch", switch_conversation_command))
    application.add_handler(CommandHandler("rename", rename_conversation_command))
    application.add_handler(CommandHandler("delete", delete_conversation_command))

    # Scheduled briefs commands (from scheduled_briefs)
    from app.handlers.scheduled_briefs import subscribe_command, unsubscribe_command

    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))

    # Horoscope subscription wizard (from horoscope_subscription)
    from app.handlers.horoscope_subscription import (
        build_horoscope_subscription_handler,
        horoscope_settings_callback,
        horoscope_stop_command,
    )

    application.add_handler(build_horoscope_subscription_handler())
    application.add_handler(CommandHandler("horoscope_stop", horoscope_stop_command))
    application.add_handler(CallbackQueryHandler(horoscope_settings_callback, pattern=r"^horo_settings:"))

    # Tarot Daily callbacks
    from app.handlers.tarot_daily import tarot_daily_callback
    application.add_handler(CallbackQueryHandler(tarot_daily_callback, pattern=r"^tarot_daily:"))

    # Reminder command (from cmd_reminders)
    from app.handlers.cmd_reminders import remind_command

    application.add_handler(CommandHandler("remind", remind_command))

    # Hidden developer commands
    from app.handlers.cmd_asr_test import asr_test_command

    application.add_handler(CommandHandler("asr", asr_test_command))

    # ── Admin /keys wizard (unified provider key management) ─────────────────
    from app.handlers.cmd_keys import build_keys_conversation_handler

    application.add_handler(build_keys_conversation_handler())

    # ── Admin /models wizard (zero-downtime model list management) ────────────
    from app.handlers.cmd_models import build_models_conversation_handler

    application.add_handler(build_models_conversation_handler())
