"""
Inline mode handlers — cross-chat bot interaction via @mention.

Flow:
  1. User types ``@gemaibotv2 <query>`` in any Telegram chat.
  2. Bot instantly returns 3 InlineQueryResultArticle entries (tone variants),
     each seeded with a styled "thinking…" placeholder as its initial content.
  3. User selects the desired tone → placeholder is posted to the chat.
  4. ``handle_chosen_inline_result`` captures ``inline_message_id`` + query
     + chosen tone, then fires ``_generate_and_edit_inline`` as a background task.
  5. ``_generate_and_edit_inline``:
       a) Calls ``tavily_search_agent`` (QnA mode) for a real-time context snippet.
       b) Injects context + tone hint into the system prompt of
          ``gemini-3.1-flash-lite-preview`` via ``_get_ai_response_with_routing``.
       c) Converts the Markdown answer to Telegram HTML and edits the inline
          placeholder message in-place using ``bot.edit_message_text(inline_message_id=…)``.

Prerequisites (one-time BotFather setup):
  - ``/setinline``         — enable inline mode; set placeholder text.
  - ``/setinlinefeedback`` — set to **100%** so the bot receives
                             ``ChosenInlineResult`` updates with ``inline_message_id``.
"""

import asyncio
import html as _html
import logging
from datetime import UTC, datetime

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import ContextTypes

from app.utils.text_format import markdown_to_html, strip_formatting

# ── Constants ────────────────────────────────────────────────────────────────

_INLINE_MODEL = "gemini-3.1-flash-lite-preview"

# Placeholder sent to the chat immediately upon selection (before generation).
_PLACEHOLDER_HTML = "⚡️ <b>Gemaibotv2</b> генерирует ответ…"

# (result_id, display_label, system_tone_hint)
_TONES: list[tuple[str, str, str]] = [
    (
        "formal",
        "📋 Формальный ответ",
        "Отвечай строго, профессионально и по делу. Только факты, без юмора.",
    ),
    (
        "friendly",
        "😊 Дружеский ответ",
        "Отвечай тепло, понятно и неформально, как близкий друг. Допускай эмодзи.",
    ),
    (
        "sarcastic",
        "😏 Саркастичный ответ",
        "Отвечай с приятной иронией и лёгким сарказмом, оставаясь при этом полезным.",
    ),
]

# Prevent fire-and-forget background tasks from being garbage-collected.
_bg_tasks: set[asyncio.Task] = set()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _tone_display(tone_id: str) -> str:
    for tid, label, _ in _TONES:
        if tid == tone_id:
            return label
    return tone_id


def _tone_hint(tone_id: str) -> str:
    for tid, _, hint in _TONES:
        if tid == tone_id:
            return hint
    return ""


# ── Public handlers ───────────────────────────────────────────────────────────


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return 3 tone-variant placeholder results instantly for any non-empty query."""
    query = update.inline_query
    if not query:
        return

    user_query = query.query.strip()
    if not user_query:
        # Guide the user when no text has been typed yet.
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    id="hint",
                    title="🤖 Введите запрос после @бота…",
                    description="Например: какая погода в Москве?",
                    input_message_content=InputTextMessageContent(
                        message_text=_PLACEHOLDER_HTML,
                        parse_mode="HTML",
                    ),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    results = [
        InlineQueryResultArticle(
            id=tone_id,
            title=label,
            description=user_query[:120],
            input_message_content=InputTextMessageContent(
                message_text=_PLACEHOLDER_HTML,
                parse_mode="HTML",
            ),
        )
        for tone_id, label, _ in _TONES
    ]

    # cache_time=0 ensures each new character triggers a fresh result list.
    await query.answer(results, cache_time=0, is_personal=True)


async def handle_chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture ChosenInlineResult metadata and launch background generation."""
    chosen = update.chosen_inline_result
    if not chosen:
        return

    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        logging.warning(
            "Inline: no inline_message_id received — "
            "ensure /setinlinefeedback is set to 100%% in BotFather."
        )
        return

    user_query = chosen.query.strip()
    tone_id = chosen.result_id
    user_id = chosen.from_user.id if chosen.from_user else None

    if not user_query:
        return

    task = asyncio.create_task(
        _generate_and_edit_inline(
            bot=context.bot,
            inline_message_id=inline_message_id,
            user_query=user_query,
            tone_id=tone_id,
            user_id=user_id,
        )
    )
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ── Background generation ─────────────────────────────────────────────────────


async def _generate_and_edit_inline(
    bot,
    inline_message_id: str,
    user_query: str,
    tone_id: str,
    user_id: int | None,
) -> None:
    """
    Core async pipeline:

    1. Tavily QnA search for real-time context (best-effort, 8 s timeout).
    2. Call ``gemini-3.1-flash-lite-preview`` with context + tone in system prompt.
    3. Convert Markdown response to Telegram HTML.
    4. Edit the placeholder inline message in-place.
    """
    from app.handlers.ai_core import _get_ai_response_with_routing
    from app.search_services import tavily_search_agent

    tone_sys_hint = _tone_hint(tone_id)
    tone_label = _tone_display(tone_id)
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    # ── Step 1: Fetch search context (non-blocking, best-effort) ─────────────
    search_context = ""
    try:
        search_result = await asyncio.wait_for(
            tavily_search_agent(user_query, search_type="qna", user_id=user_id),
            timeout=8.0,
        )
        if isinstance(search_result, dict) and search_result.get("type") == "answer":
            search_context = (search_result.get("content") or "").strip()
    except Exception as search_err:
        logging.warning(
            "Inline: Tavily QnA failed, proceeding without context: %s", search_err
        )

    # ── Step 2: Build system prompt ───────────────────────────────────────────
    system_instruction = (
        f"[system: current_utc_date={today}]\n"
        f"Тон ответа: {tone_sys_hint}\n"
        "Ты — ассистент в инлайн-режиме Telegram. "
        "Пользователь задаёт вопрос прямо из переписки с другим человеком — "
        "отвечай КРАТКО и по существу (не более 3–4 абзацев). "
        "Используй Markdown для форматирования (жирный, курсив, списки).\n"
    )
    if search_context:
        system_instruction += (
            "\n[Актуальная информация из интернета (используй при необходимости)]:\n"
            f"{search_context[:2000]}"
        )

    history = [{"role": "user", "parts": [user_query]}]

    # ── Step 3: Generate with lightweight model ───────────────────────────────
    final_answer: str | None = None
    try:
        final_answer, _ = await asyncio.wait_for(
            _get_ai_response_with_routing(
                preferred_model=_INLINE_MODEL,
                history=history,
                system_instruction=system_instruction,
                user_id=user_id,
            ),
            timeout=25.0,
        )
    except Exception as gen_err:
        logging.error(
            "Inline: Generation failed for query '%s': %s",
            user_query[:60],
            gen_err,
            exc_info=True,
        )

    # ── Step 4: Format and edit inline message ────────────────────────────────
    if final_answer and final_answer.strip():
        header = (
            f"<b>{_html.escape(tone_label)}</b>"
            f" · <code>{_html.escape(user_query[:60])}</code>\n\n"
        )
        body = markdown_to_html(final_answer.strip())
        formatted = header + body
        # Telegram inline messages: hard 4096-char limit.
        if len(formatted) > 4000:
            formatted = formatted[:3997] + "…"
    else:
        formatted = "❌ Не удалось получить ответ. Попробуйте ещё раз."

    try:
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=formatted,
            parse_mode="HTML",
        )
    except Exception as edit_err:
        logging.error(
            "Inline: Failed to edit inline message %s: %s",
            inline_message_id,
            edit_err,
        )
        # Last-resort: strip HTML tags and retry as plain text.
        try:
            plain = strip_formatting(formatted)[:4000]
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=plain or "Ошибка генерации ответа.",
            )
        except Exception as fallback_err:
            logging.error(
                "Inline: Plain-text fallback also failed: %s", fallback_err
            )
