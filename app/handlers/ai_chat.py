"""
AI Chat handler — regular conversational chat with context management.
"""

import contextlib
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    _resolve_ai_request,
    handle_ai_response_error,
)
from app.handlers.chat_logic import classify_resolution, format_memories_for_system_prompt
from app.prompt_registry import get_registry
from app.providers import GeminiProvider, is_openrouter_model
from app.repos.chats import update_user_chat
from app.utils.formatting import TelegramFormatter
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_CHAT, update_stage

# Removed _background_tasks set, using centralized TaskManager


def _store_memory_in_background(user_id: int, user_message: str, key_data: dict | None) -> None:
    """Store user intent as long-term memory (background, non-blocking).

    Fires a retryable background task that embeds the user message
    and checks whether memory consolidation is needed.
    """
    try:
        if not key_data or len(user_message) <= 30:
            return

        from app.repos.memory import store_memory

        memory_content = user_message[:500]
        _api_key = key_data["api_key"]

        async def _bg_store():
            await store_memory(
                user_id,
                memory_content,
                _api_key,
                source_type="user_intent",
            )
            try:
                from app.repos.memory_consolidation import (
                    consolidate_memories,
                    should_check_consolidation,
                    should_consolidate,
                )

                if should_check_consolidation(user_id) and await should_consolidate(user_id):
                    await consolidate_memories(user_id, _api_key)
            except Exception as cons_err:
                logging.debug("Consolidation check skipped: %s", cons_err)

        from app.utils.background_tasks import submit_retryable

        submit_retryable(_bg_store, retry=3)
    except Exception:
        pass


async def _handle_regular_chat(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
    model_override: str | None = None,
):
    # Используем переопределение models, if указано, иначе model from chat_state
    model_for_this_request = model_override or chat_state.model
    key_data, model_used, resolution = await _resolve_ai_request(model_for_this_request)

    if resolution == "all_exhausted":
        result = classify_resolution(resolution, model_for_this_request)
        try:
            await placeholder_message.edit_text(result.user_message or "")
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    if resolution == "confirm_fallback":
        result = classify_resolution(resolution, model_for_this_request, model_used)
        keyboard = [
            [
                InlineKeyboardButton(
                    f"Да, использовать {model_used}",
                    callback_data=f"fallback:confirm:{model_used}",
                )
            ],
            [InlineKeyboardButton("Нет, отмена", callback_data="fallback:cancel")],
        ]
        try:
            await placeholder_message.edit_text(
                result.user_message or "",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    # Assemble context with token-budget awareness
    from app.context.summarizer import schedule_llm_summarization
    from app.context_assembler import get_assembler

    assembler = get_assembler()

    # Use persisted summary from chat state (survives restarts)
    existing_summary = chat_state.context_summary

    # Compose system instruction first (needed for budget calculation)
    system_instruction = get_registry().compose_system_prompt(role_prompt=chat_state.system_prompt)

    # Assemble context within token budget
    assembled = assembler.assemble(
        history=chat_state.history,
        user_message=user_message,
        system_instruction=system_instruction,
        existing_summary=existing_summary,
    )

    # Update chat state with assembled context
    chat_state.history = assembled.history
    chat_state.context_summary = assembled.summary

    # ── Inject long-term memories (semantic recall) ──────────────────────
    _memories_injected = 0
    if chat_state.ltm_enabled and key_data:
        try:
            from app.repos.memory import search_memories

            if user_message and len(user_message) > 15:
                memories = await search_memories(
                    user_id,
                    user_message,
                    key_data["api_key"],
                    limit=3,
                    min_similarity=0.72,
                )
                if memories:
                    memory_xml = format_memories_for_system_prompt(memories)
                    if memory_xml:
                        system_instruction = system_instruction + "\n\n" + memory_xml
                    _memories_injected = len(memories)
                    logging.info(
                        "Injected %d memories into system_instruction for user %s", _memories_injected, user_id
                    )
        except Exception as mem_err:
            logging.warning("Memory recall failed for user %s: %s", user_id, mem_err)

    if assembled.was_truncated:
        logging.info(
            "Context trimmed for user %s: dropped %d msgs, audit=%s, llm_scheduled=%s",
            user_id,
            assembled.messages_dropped,
            assembled.audit_hash,
            assembled.llm_summarization_scheduled,
        )

        # Record summarization metrics
        from app.metrics import role_conv_metrics
        from app.prompt_registry import estimate_tokens_cyrillic

        tokens_saved = sum(estimate_tokens_cyrillic(assembler._extract_text(msg)) for msg in assembled.dropped_messages)
        summary_len = estimate_tokens_cyrillic(assembled.summary) if assembled.summary else 0
        tier = "llm" if assembled.llm_summarization_scheduled else "local"
        await role_conv_metrics.record_summarization(
            reason=f"{tier}: dropped {assembled.messages_dropped} msgs",
            tokens_saved=tokens_saved,
            summary_length=summary_len,
        )

        # Schedule async LLM summarization for NEXT request
        if assembled.llm_summarization_scheduled and assembled.dropped_messages:

            async def _store_llm_summary(summary: str) -> None:
                chat_state.context_summary = summary
                await update_user_chat(user_id, chat_state)
                logging.info("LLM summary persisted for user %s", user_id)

            schedule_llm_summarization(
                dropped_messages=assembled.dropped_messages,
                existing_summary=existing_summary,
                callback=_store_llm_summary,
            )

    try:
        await update_stage(placeholder_message, STAGES_CHAT, 0)
    except Exception as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text(f"🧠 Модель {model_used} думает...")

    # ── Unified Streaming ────────────────────────────────────────────
    response_text = None
    new_token_count = 0
    streamed = False
    stream_last_msg = None

    # ── Build memory footer if applicable ─────────────────────────────────
    _footer_text: str | None = None
    if _memories_injected > 0:
        _footer_text = f"\n\n_🧠 Использован контекст из прошлых бесед ({_memories_injected})_"

    # Stop the heartbeat before streaming — streaming edits the same
    # placeholder message, so the heartbeat would race with it.
    from app.utils.heartbeat import stop_heartbeat

    stop_heartbeat(placeholder_message.message_id)

    from app.streaming import stream_and_display

    response_text, success, stream_last_msg, actual_tokens = await stream_and_display(
        placeholder_message,
        model_name=model_used,
        history=chat_state.history,
        system_instruction=system_instruction,
        thinking_level=chat_state.thinking_level,
        user_id=user_id,
        bot=placeholder_message.get_bot(),
        chat_id=placeholder_message.chat_id,
        chat_type=placeholder_message.chat.type,
        footer_text=_footer_text,
    )

    if success and response_text:
        streamed = True
        # Prefer actual token count from provider; fall back to heuristic estimate
        if actual_tokens > 0:
            new_token_count = actual_tokens
        else:
            from app.prompt_registry import estimate_tokens_cyrillic

            new_token_count = estimate_tokens_cyrillic(response_text)

    if response_text:
        # Check if response is an error
        from app.errors import build_retry_and_roles_keyboard

        async def cleanup_on_error() -> None:
            chat_state.history.pop()
            await update_user_chat(user_id, chat_state)

        if await handle_ai_response_error(response_text, placeholder_message, on_error_callback=cleanup_on_error):
            return
        else:
            buttons = [
                [InlineKeyboardButton("🔄 Попробовать ещё раз", callback_data="retry_last")],
                [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
                [
                    InlineKeyboardButton(
                        "✨ Начать новую тему",
                        callback_data=("deepdive:new_topic" if chat_state.is_deep_dive else "new_topic"),
                    )
                ],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

            if not streamed:
                # Non-streaming: send_long_message as before
                try:
                    await send_long_message(placeholder_message, response_text, reply_markup=reply_markup)
                except Exception as send_err:
                    logging.warning("send_long_message failed, fallback to reply_text: %s", send_err)
                    try:
                        formatted_text, parse_mode = TelegramFormatter.format_text(response_text)
                        await placeholder_message.reply_text(
                            formatted_text,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await placeholder_message.reply_text(response_text, reply_markup=reply_markup)
            else:
                # Streaming already includes footer_text — just attach buttons.
                button_msg = stream_last_msg if stream_last_msg else placeholder_message
                try:
                    await button_msg.edit_reply_markup(reply_markup=reply_markup)
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        logging.warning("Final button edit failed: %s", e)

            chat_state.history.append({"role": "model", "parts": [response_text]})
            chat_state.token_count = new_token_count
            await update_user_chat(user_id, chat_state)

            _store_memory_in_background(user_id, user_message, key_data)

            # ── Model suggestion (non-intrusive hint) ────────────────────
            try:
                from app.model_selector import select_model

                suggestion = select_model(
                    user_message,
                    current_model=model_used,
                )
                if suggestion and suggestion.confidence >= 0.6:
                    hint_keyboard = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"⚡ Попробовать {suggestion.model}",
                                    callback_data=f"switch_model:{suggestion.model}",
                                )
                            ],
                        ]
                    )
                    hint_text = f"💡 _{suggestion.reason}_"
                    fmt_text, fmt_pm = TelegramFormatter.format_text(hint_text)
                    await placeholder_message.reply_text(
                        fmt_text,
                        parse_mode=fmt_pm,
                        reply_markup=hint_keyboard,
                    )
            except Exception:
                pass  # Non-critical
    else:
        chat_state.history.pop()
        await update_user_chat(user_id, chat_state)
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except Exception as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
