"""Reminder command and delivery system.

Provides the ``/remind`` command for users to set time-based follow-ups,
and the ``check_and_deliver_reminders`` job for the bot scheduler.

Supports **Smart Reminders**: if the user's prompt requires an AI action
(e.g. "find news about X", "research Y"), the system automatically
classifies the intent at creation time and dispatches the appropriate
AI pipeline (QnA search, agentic research, or regular chat) when the
reminder fires — delivering the full AI response directly in TG chat.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config import UTC_TZ
from app.repos import scheduled_deliveries as scheduled
from app.repos.reminders import (
    create_reminder,
    delete_reminder,
    get_pending_reminders,
    get_user_reminders,
)
from app.scheduled_delivery import run_scheduled_delivery
from app.utils.background_tasks import try_submit_task
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

# Time parsing patterns: "30m", "2h", "1d", "90min", "3 hours"
_TIME_PATTERN = re.compile(
    r"^(\d+)\s*"
    r"(m|min|mins|minutes|мин|минут[аыу]?"
    r"|h|hr|hrs|hours|час|часа|часов"
    r"|d|day|days|день|дня|дней)"
    r"\s+(.+)$",
    re.IGNORECASE,
)

_UNIT_TO_SECONDS = {
    "m": 60,
    "min": 60,
    "mins": 60,
    "minutes": 60,
    "мин": 60,
    "минута": 60,
    "минуты": 60,
    "минуту": 60,
    "минут": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hours": 3600,
    "час": 3600,
    "часа": 3600,
    "часов": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "день": 86400,
    "дня": 86400,
    "дней": 86400,
}

# ── Intent classification ────────────────────────────────────────────────────

# Heuristic keyword sets for fast local classification (no LLM call needed).
# Designed to be accurate and comprehensive per user feedback.
_AI_KEYWORDS_RU = {
    # Research / search
    "найди",
    "найти",
    "поищи",
    "поиск",
    "искать",
    "ищи",
    "загугли",
    "проверь",
    "проверить",
    "узнай",
    "узнать",
    # Analysis
    "проанализируй",
    "анализ",
    "сравни",
    "сравнить",
    "оценить",
    "оцени",
    "исследуй",
    "исследовать",
    "изучи",
    "изучить",
    "разбери",
    "разобрать",
    # Generation
    "напиши",
    "написать",
    "составь",
    "составить",
    "сгенерируй",
    "генерация",
    "создай",
    "создать",
    "придумай",
    "придумать",
    "сформируй",
    "сформулируй",
    "подготовь",
    "подготовить",
    "сделай",
    "сделать",
    # Summarization
    "суммаризируй",
    "резюмируй",
    "подведи итоги",
    "кратко",
    "перескажи",
    "пересказать",
    "выдели главное",
    # Actuality / freshness
    "актуальн",
    "свежи",
    "последни",
    "новост",
    "сегодня",
    "сейчас",
    "курс",
    "цена",
    "котировк",
    "прогноз",
    "погод",
}

_AI_KEYWORDS_EN = {
    # Research / search
    "find",
    "search",
    "look up",
    "lookup",
    "google",
    "check",
    "research",
    "investigate",
    "explore",
    "discover",
    # Analysis
    "analyze",
    "analyse",
    "compare",
    "evaluate",
    "assess",
    "review",
    "study",
    "examine",
    # Generation
    "write",
    "compose",
    "generate",
    "create",
    "draft",
    "prepare",
    "summarize",
    "summarise",
    "outline",
    # Actuality
    "latest",
    "current",
    "today",
    "news",
    "price",
    "stock",
    "weather",
    "forecast",
    "update",
}

# Simple notification patterns that should NOT trigger AI
_NOTIFY_PATTERNS_RU = {
    "выключи",
    "выключить",
    "включи",
    "включить",
    "позвони",
    "позвонить",
    "перезвони",
    "покорми",
    "покормить",
    "выгуляй",
    "выгулять",
    "купи",
    "купить",
    "забери",
    "забрать",
    "прими",
    "принять",
    "выпей",
    "выпить",
    "поешь",
    "поесть",
    "поспи",
    "поспать",
}

_NOTIFY_PATTERNS_EN = {
    "turn off",
    "turn on",
    "call",
    "feed",
    "walk",
    "buy",
    "pick up",
    "take",
    "eat",
    "sleep",
    "drink",
    "wake up",
}

# Deep-research signals: if ANY of these appear, use "research" mode
_RESEARCH_SIGNALS = {
    "глубок",
    "подробн",
    "детальн",
    "исследуй",
    "исследовать",
    "research",
    "deep",
    "detailed",
    "thorough",
    "in-depth",
    "agentic",
    "agent",
    "проанализируй",
    "analyze",
    "analyse",
    "сравни",
    "compare",
}


def _classify_reminder_intent(prompt: str) -> dict:
    """Classify the user's reminder prompt into an action type.

    Returns a dict with:
      - is_ai: bool — whether this requires an AI pipeline
      - mode: "notify" | "qna" | "research" | "chat"

    The heuristic is designed to be accurate and comprehensive:
    1. First checks for explicit "do NOT use AI" patterns (notifications).
    2. Then checks for AI-action keywords in both RU and EN.
    3. Uses research-signal keywords to distinguish deep search from quick QnA.
    4. Defaults to "notify" (plain text) when uncertain — safe fallback.
    """
    lower = prompt.lower().strip()
    words = set(re.findall(r"[\w]+", lower))

    # ── Step 1: Explicit notification patterns (high confidence) ────────
    # RU patterns use substring matching (handles morphological suffixes)
    for pattern in _NOTIFY_PATTERNS_RU:
        if pattern in lower:
            return {"is_ai": False, "mode": "notify"}
    # EN patterns use word-boundary matching to avoid false positives
    # (e.g. "eat" matching inside "weather")
    for pattern in _NOTIFY_PATTERNS_EN:
        if " " in pattern:
            # Multi-word pattern: full substring check is safe
            if pattern in lower:
                return {"is_ai": False, "mode": "notify"}
        else:
            # Single-word pattern: check against word set
            if pattern in words:
                return {"is_ai": False, "mode": "notify"}

    # ── Step 2: Check for AI-action keywords ───────────────────────────
    has_ai_signal = False

    # Check full prompt for substring matches (handles prefixes like "актуальн*")
    for keyword in _AI_KEYWORDS_RU:
        if keyword in lower:
            has_ai_signal = True
            break

    if not has_ai_signal:
        for keyword in _AI_KEYWORDS_EN:
            if keyword in lower:
                has_ai_signal = True
                break

    if not has_ai_signal:
        return {"is_ai": False, "mode": "notify"}

    # ── Step 3: Determine AI mode (research vs qna) ────────────────────
    is_research = False
    for signal in _RESEARCH_SIGNALS:
        if signal in lower:
            is_research = True
            break

    # Long prompts (>80 chars) with AI signals are more likely research tasks
    if len(prompt) > 80 and has_ai_signal:
        is_research = True

    mode = "research" if is_research else "qna"
    return {"is_ai": True, "mode": mode}


def _parse_reminder_args(text: str) -> tuple[timedelta | None, str | None]:
    """Parse reminder text into (timedelta, prompt) or (None, None)."""
    match = _TIME_PATTERN.match(text.strip())
    if not match:
        return None, None

    amount = int(match.group(1))
    unit = match.group(2).lower()
    prompt = match.group(3).strip()

    seconds = _UNIT_TO_SECONDS.get(unit)
    if not seconds or amount <= 0 or amount > 365 * 24:  # max 1 year
        return None, None

    return timedelta(seconds=amount * seconds), prompt


@authorized_only
@safe_handler("❌ Ошибка создания напоминания.")
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a reminder: /remind 30m Проверить результаты."""
    user_id = update.effective_user.id

    # No args → show usage + pending reminders
    if not context.args:
        pending = await get_user_reminders(user_id, limit=5)
        text_parts = [
            "⏰ **Напоминания**\n\n"
            "**Формат:** `/remind <время> <текст>`\n"
            "**Примеры:**\n"
            "• `/remind 30m Проверить результаты`\n"
            "• `/remind 2h Написать отчёт`\n"
            "• `/remind 1d Созвон с командой`\n"
            "• `/remind 4h Найди актуальные новости о Tesla` 🤖\n"
            "• `/remind 3d Проанализируй тренды в AI` 🤖\n",
        ]

        buttons: list[list[InlineKeyboardButton]] = []
        if pending:
            text_parts.append("\n📋 **Ваши напоминания:**\n")
            for r in pending:
                trigger = r["trigger_at"]
                time_str = trigger.strftime("%d.%m %H:%M") if hasattr(trigger, "strftime") else str(trigger)[:16]
                prompt_preview = r["prompt"][:40] + ("…" if len(r["prompt"]) > 40 else "")
                # Show AI badge if this is an AI reminder
                ctx = r.get("context_history")
                ai_badge = ""
                if isinstance(ctx, dict) and ctx.get("is_ai"):
                    mode = ctx.get("mode", "qna")
                    ai_badge = " 🔬" if mode == "research" else " 🔎"
                text_parts.append(f"  • `{time_str}` — {prompt_preview}{ai_badge}\n")
                if r.get("delivery_state") == "unknown_delivery":
                    text_parts.append("    ⚠️ Доставка не подтверждена; автоматический повтор остановлен.\n")
                elif r.get("delivery_state") == "failed" and not r.get("retryable"):
                    text_parts.append("    ❌ Не доставлено. Создайте новое напоминание для повтора.\n")
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"❌ {prompt_preview[:25]}",
                            callback_data=f"reminder_cancel:{r['id']}",
                        )
                    ]
                )

        text = "".join(text_parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
        return

    # Parse time + prompt
    raw_text = " ".join(context.args)
    delta, prompt = _parse_reminder_args(raw_text)

    if not delta or not prompt:
        await update.message.reply_text(
            "❌ Не удалось разобрать формат.\n"
            "Используйте: `/remind 30m Текст напоминания`\n"
            "Единицы: m/мин, h/час, d/день",
            parse_mode="Markdown",
        )
        return

    # ── Classify intent (local heuristic, zero latency) ──────────────
    intent = _classify_reminder_intent(prompt)
    context_history = intent if intent["is_ai"] else None

    trigger_at = datetime.now(UTC_TZ) + delta
    reminder_id = await create_reminder(user_id, trigger_at, prompt, context_history)

    if not reminder_id:
        await update.message.reply_text("❌ Не удалось сохранить напоминание. Попробуйте позже.")
        return

    # Friendly time display
    if delta.total_seconds() < 3600:
        time_label = f"{int(delta.total_seconds() / 60)} мин"
    elif delta.total_seconds() < 86400:
        hours = delta.total_seconds() / 3600
        time_label = f"{hours:.0f} ч" if hours == int(hours) else f"{hours:.1f} ч"
    else:
        days = delta.total_seconds() / 86400
        time_label = f"{days:.0f} д" if days == int(days) else f"{days:.1f} д"

    # Build confirmation with AI badge
    if intent["is_ai"]:
        mode_label = "🔬 глубокий анализ" if intent["mode"] == "research" else "🔎 поиск"
        confirm_text = (
            f"✅ Напоминание через **{time_label}** (_{mode_label}_):\n\n"
            f"_{prompt}_\n\n"
            f"💡 _Бот автоматически выполнит ИИ-запрос и пришлёт результат._"
        )
    else:
        confirm_text = f"✅ Напоминание через **{time_label}**:\n\n_{prompt}_"

    formatted_text, parse_mode = TelegramFormatter.format_text(confirm_text)
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


# Maximum number of concurrent AI reminder executions to prevent API key exhaustion
_AI_REMINDER_SEMAPHORE = asyncio.Semaphore(3)
# Maximum time for an AI reminder execution before timeout (seconds)
_AI_REMINDER_TIMEOUT = 300  # 5 minutes
# Track active AI reminder tasks to prevent GC collection (RUF006)
_background_ai_tasks: set[asyncio.Task] = set()


async def _execute_ai_reminder(
    bot,
    user_id: int,
    prompt: str,
    mode: str,
    reminder_id: int,
    *,
    claim: dict,
) -> None:
    """Keep the durable owner through generation and the final response receipt."""

    async def execute(owner):
        async with _AI_REMINDER_SEMAPHORE:
            await owner.checkpoint()
            from app.repos.chats import get_user_chat

            chat_state = await get_user_chat(user_id)
            # Loading state must not adopt a new account generation for old work.
            if chat_state.memory_epoch != claim["privacy_epoch"]:
                return
            await owner.checkpoint()
            mode_emoji = "🔬" if mode == "research" else "🔎"
            placeholder_text = f"⏰ **Отложенный ИИ-запрос** {mode_emoji}\n\n_{prompt}_\n\n⏳ Выполняю..."
            formatted_text, parse_mode = TelegramFormatter.format_text(placeholder_text)
            placeholder = await owner.send_message(chat_id=user_id, text=formatted_text, parse_mode=parse_mode)
            if mode == "research":
                from app.handlers.ai_search import _handle_research_agent

                work = _handle_research_agent(
                    placeholder,
                    user_id,
                    prompt,
                    chat_state,
                    delivery_observer=owner.observe_response,
                    voice_delivery_bot=bot,
                )
            else:
                from app.handlers.ai_search import _handle_qna_search

                work = _handle_qna_search(
                    placeholder,
                    prompt,
                    chat_state,
                    user_id=user_id,
                    delivery_observer=owner.observe_response,
                )
            # Timeout/cancellation after a placeholder is a terminal uncertain
            # execution. Replaying it would duplicate a possibly visible answer.
            await asyncio.wait_for(work, timeout=_AI_REMINDER_TIMEOUT)

    await run_scheduled_delivery(claim, bot, execute, ai_response=True)


async def check_and_deliver_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback: poll pending reminders and deliver them via bot.

    Called by job_queue.run_repeating every 60 seconds.

    For plain-text reminders: sends the text immediately.
    For AI reminders: dispatches a non-blocking background task that
    sends a shadow placeholder message, runs the AI pipeline, and
    streams the result back to the user's chat.
    """
    try:
        await scheduled.recover_abandoned()
        pending = await get_pending_reminders()
        if not pending:
            return

        async def _process_single_reminder(reminder: dict) -> None:
            try:
                claim = await scheduled.claim_reminder(reminder["id"])
                if claim is None:
                    return
                user_id = reminder["user_id"]
                prompt = reminder["prompt"]
                ctx = reminder.get("context_history")
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except json.JSONDecodeError, TypeError:
                        ctx = None
                is_ai = isinstance(ctx, dict) and bool(ctx.get("is_ai"))
                if is_ai:
                    task = try_submit_task(
                        _execute_ai_reminder(
                            context.bot,
                            user_id,
                            prompt,
                            ctx.get("mode", "qna"),
                            reminder["id"],
                            claim=claim,
                        ),
                        operation="reminder.ai",
                    )
                    if task is None:
                        await scheduled.release_unstarted(claim)
                        logger.warning("AI reminder %d admission rejected; leaving it pending", reminder["id"])
                        return
                    task.set_name(f"ai_remind_{reminder['id']}")
                    _background_ai_tasks.add(task)
                    task.add_done_callback(_background_ai_tasks.discard)
                else:

                    async def send(owner):
                        text = f"⏰ **Напоминание**\n\n{prompt}"
                        formatted_text, parse_mode = TelegramFormatter.format_text(text)
                        await owner.send_message(chat_id=user_id, text=formatted_text, parse_mode=parse_mode)

                    await run_scheduled_delivery(claim, context.bot, send)
            except Exception as e:
                logger.warning("Failed to deliver reminder %d: %s", reminder["id"], e)

        # ⚡ Bolt Optimization: Process all pending reminders concurrently
        await asyncio.gather(*[_process_single_reminder(r) for r in pending])

    except Exception as e:
        logger.error("Reminder poll loop error: %s", e)


# ── Inline cancel callback ───────────────────────────────────────────────────


async def reminder_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline ❌ button press to delete a reminder."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user_id = query.from_user.id
    try:
        reminder_id = int(query.data.split(":")[1])
    except IndexError, ValueError:
        logger.warning("Invalid reminder_cancel callback data: %s", query.data)
        return

    success = await delete_reminder(reminder_id, user_id)

    if success:
        # Remove the button that was pressed from the keyboard
        from telegram import Message

        if isinstance(query.message, Message) and query.message.reply_markup:
            old_buttons = query.message.reply_markup.inline_keyboard
            new_buttons = [
                row
                for row in old_buttons
                if not any(btn.callback_data == f"reminder_cancel:{reminder_id}" for btn in row)
            ]
            new_markup = InlineKeyboardMarkup(new_buttons) if new_buttons else None
            try:
                await query.message.edit_reply_markup(reply_markup=new_markup)
            except Exception:
                pass

        await query.answer("✅ Напоминание удалено", show_alert=False)
        logger.info("Reminder %d cancelled by user %s", reminder_id, user_id)
    else:
        await query.answer("❌ Не удалось удалить", show_alert=True)
