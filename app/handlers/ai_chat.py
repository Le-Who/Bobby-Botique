"""
AI Chat handler — regular conversational chat with context management.
"""

import contextlib
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import prompts
from app.config import settings
from app.database import ChatState
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    _resolve_ai_request,
    handle_ai_response_error,
)
from app.handlers.chat_logic import build_memory_context, classify_resolution
from app.providers import GeminiProvider, is_openrouter_model
from app.repos.chats import update_user_chat
from app.utils.formatting import TelegramFormatter
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import STAGES_CHAT, update_stage

_background_tasks: set = set()


async def _handle_regular_chat(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
    model_override: str | None = None,
):
    # Используем переопределение models, if указано, иначе model from chat_state
    model_for_this_request = model_override or chat_state.model
    _, model_used, resolution = await _resolve_ai_request(model_for_this_request)

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
    system_instruction = prompts.compose_system_instruction(chat_state.system_prompt)

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
    try:
        from app.repos.memory import search_memories

        key_data_for_mem, _, _ = await _resolve_ai_request(model_used)
        if key_data_for_mem and user_message and len(user_message) > 15:
            memories = await search_memories(
                user_id,
                user_message,
                key_data_for_mem["api_key"],
                limit=3,
                min_similarity=0.55,
            )
            if memories:
                chat_state.history = build_memory_context(memories, chat_state.history)
                logging.info("Injected %d memories for user %s", len(memories), user_id)
    except Exception as mem_err:
        logging.debug("Memory recall skipped: %s", mem_err)

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

    # ── Try streaming for Gemini models first ────────────────────────────
    response_text = None
    new_token_count = 0
    streamed = False
    stream_last_msg = None
    MAX_STREAM_RETRIES = 3

    if not is_openrouter_model(model_used):
        # Stop the heartbeat before streaming — streaming edits the same
        # placeholder message, so the heartbeat would race with it.
        from app.utils.heartbeat import stop_heartbeat

        stop_heartbeat(placeholder_message.message_id)
        from google.genai import types as genai_types

        from app.providers import _build_thinking_config
        from app.streaming import stream_and_display

        excluded_key_hashes: set[str] = set()

        for stream_attempt in range(MAX_STREAM_RETRIES):
            try:
                # Resolve a key, excluding previously failed ones
                key_data, _, _ = await _resolve_ai_request(
                    model_used,
                    excluded_key_hashes=excluded_key_hashes or None,
                )
                if not key_data:
                    logging.warning("No API keys available for streaming (attempt %d)", stream_attempt + 1)
                    break

                # Build contents using GeminiProvider helper
                provider = GeminiProvider(key_data["api_key"])
                contents = await provider._build_contents(chat_state.history)

                if not contents:
                    break

                config = genai_types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)  # type: ignore[arg-type]  # Pydantic coerces dicts→SafetySetting
                tc = _build_thinking_config(model_used, chat_state.thinking_level)
                if tc:
                    config.thinking_config = tc
                if system_instruction:
                    with contextlib.suppress(TypeError, ValueError):
                        config.system_instruction = str(system_instruction)

                response_text, success, stream_last_msg = await stream_and_display(
                    placeholder_message,
                    key_data["api_key"],
                    model_used,
                    contents,
                    config,
                )
                if success and response_text:
                    streamed = True
                    # Count tokens
                    from app.prompt_registry import estimate_tokens_cyrillic

                    new_token_count = estimate_tokens_cyrillic(response_text)
                    # Increment key usage
                    from app.handlers.ai_core import _increment_key_usage

                    await _increment_key_usage(key_data["key_hash"], model_used)
                    break  # Success — exit retry loop
                else:
                    # Stream returned but wasn't successful — try next key
                    logging.warning(
                        "Streaming attempt %d/%d failed (success=%s), trying next key",
                        stream_attempt + 1,
                        MAX_STREAM_RETRIES,
                        success,
                    )
                    excluded_key_hashes.add(key_data["key_hash"])
                    response_text = None
            except Exception as e:
                logging.warning(
                    "Streaming attempt %d/%d error: %s",
                    stream_attempt + 1,
                    MAX_STREAM_RETRIES,
                    e,
                )
                if key_data:
                    excluded_key_hashes.add(key_data["key_hash"])
                response_text = None

    # ── Fallback to non-streaming (OpenRouter or stream failure) ─────────
    if not streamed:
        # Guard: ensure history is non-empty before calling the provider
        if not chat_state.history:
            logging.error(
                "Empty history for user %s after streaming failure — cannot call non-streaming fallback",
                user_id,
            )
            try:
                from app.errors import build_retry_and_roles_keyboard

                await placeholder_message.edit_text(
                    "❌ Произошла ошибка при формировании запроса. Попробуйте ещё раз.",
                    reply_markup=build_retry_and_roles_keyboard(),
                )
            except Exception as edit_error:
                logging.error("Could not edit placeholder message: %s", edit_error)
            return

        response_text, new_token_count = await _get_ai_response_with_routing(
            model_used,
            chat_state.history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=placeholder_message.chat.id if placeholder_message.chat else None,
            thinking_level=chat_state.thinking_level,
        )

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
                        callback_data="deepdive:new_topic" if chat_state.is_deep_dive else "new_topic",
                    )
                ],
            ]
            reply_markup = InlineKeyboardMarkup(buttons)

            if not streamed:
                # Non-streaming: send_long_message as before
                try:
                    await send_long_message(placeholder_message, response_text, reply_markup=reply_markup)
                except Exception as send_err:
                    logging.warning(f"send_long_message failed, fallback to reply_text: {send_err}")
                    try:
                        formatted_text, parse_mode = TelegramFormatter.format_text(response_text)
                        await placeholder_message.reply_text(
                            formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
                        )
                    except Exception:
                        await placeholder_message.reply_text(response_text, reply_markup=reply_markup)
            else:
                # Streaming: message is already displayed, just add buttons
                # Use stream_last_msg (final message in chain) for button attachment
                button_msg = stream_last_msg if stream_last_msg else placeholder_message
                try:
                    # Simply attach buttons to the last message without re-rendering
                    await button_msg.edit_reply_markup(reply_markup=reply_markup)
                except Exception as e:
                    if "not modified" not in str(e).lower():
                        logging.warning("Final button edit failed: %s", e)

            chat_state.history.append({"role": "model", "parts": [response_text]})
            chat_state.token_count = new_token_count
            await update_user_chat(user_id, chat_state)

            # ── Store exchange as memory (background, non-blocking) ──────
            try:
                key_data_for_store, _, _ = await _resolve_ai_request(model_used)
                if key_data_for_store and len(user_message) > 30:
                    import asyncio

                    from app.repos.memory import store_memory

                    exchange = f"Q: {user_message[:500]}\nA: {response_text[:500]}"

                    async def _bg_store():
                        with contextlib.suppress(Exception):
                            await store_memory(
                                user_id,
                                exchange,
                                key_data_for_store["api_key"],
                                source_type="conversation",
                            )

                    _task = asyncio.get_running_loop().create_task(_bg_store())
                    _background_tasks.add(_task)
                    _task.add_done_callback(_background_tasks.discard)
            except Exception:
                pass

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
                    await placeholder_message.reply_text(
                        f"💡 _{suggestion.reason}_",
                        parse_mode="Markdown",
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
