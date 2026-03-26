"""
AI Search handlers — QnA quick answers, research agent, and complex agent search.
"""

import asyncio
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError

from app.config import settings
from app.core.agentic import AgenticResult, AgenticSearch
from app.database import ChatState
from app.errors import _TAG_PREFIX, is_error_message
from app.handlers.ai_core import (
    _get_ai_response_with_routing,
    handle_ai_response_error,
)
from app.metrics import metrics_collector, track_metrics
from app.prompt_registry import get_registry
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.heartbeat import stop_heartbeat
from app.utils.messaging import send_long_message
from app.utils.stage_indicators import (
    STAGES_SEARCH_DEEP,
    STAGES_SEARCH_QUICK,
    update_stage,
)
from app.utils.waiting_facts import get_waiting_message


@track_metrics("qna_search")
async def _handle_qna_search(
    placeholder_message: Message,
    user_message: str,
    chat_state: ChatState,
    search_query: str | None = None,
):
    """Quick search using Google Search Grounding (single LLM call, no Tavily).

    Fallback chain: gemini-3.1-flash-lite-preview → gemini-2.5-flash-lite.
    If the user has a custom model set in chat_state, it is respected as the
    primary model (but still gets the web search grounding).
    """
    actual_search_query = search_query if search_query else user_message

    stop_heartbeat(placeholder_message.message_id)
    await metrics_collector.record_search_query()

    try:
        await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 0)
    except (BadRequest, NetworkError) as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        placeholder_message = await placeholder_message.reply_text("🔎 Ищу быстрый ответ...")

    user_id = placeholder_message.from_user.id if placeholder_message.from_user else None
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None

    # ── Build fallback model chain for QnA ─────────────────────────────
    # QnA search ALWAYS uses grounding-capable models — user's chat model
    # preference is ignored because arbitrary models may not support
    # Google Search Grounding (e.g. gemini-3.x has 0 grounding quota on
    # free tier).
    fallback_chain = ["gemini-2.5-flash-lite", "gemini-2.5-flash"]

    # ── Build the prompt ───────────────────────────────────────────────
    # With Google Search Grounding, the LLM searches the web internally.
    # We inject today's date so the model knows what "now" means, and
    # instruct it to ALWAYS use Google Search for factual/current queries.
    from datetime import UTC, datetime

    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    system_instruction = (
        f"[system: current_utc_date={today}]\n"
        "Ты поисковый ассистент. Используй инструмент Google Search для каждого запроса.\n"
        "Отвечай кратко, по делу, на языке пользователя. Указывай источники, если возможно."
    )
    role_prompt = chat_state.system_prompt
    if role_prompt:
        system_instruction += f"\n{role_prompt}"
    history = [{"role": "user", "parts": [actual_search_query]}]

    from app.streaming import stream_and_display

    # ── Try each model in the fallback chain ───────────────────────────
    final_answer: str | None = None
    success = False
    stream_last_msg = None
    _tokens = 0

    for attempt_idx, model in enumerate(fallback_chain):
        try:
            logging.info("QnA search: trying model %s (attempt %d/%d)", model, attempt_idx + 1, len(fallback_chain))
            if attempt_idx > 0:
                try:
                    await update_stage(placeholder_message, STAGES_SEARCH_QUICK, 0)
                except (BadRequest, NetworkError):
                    pass

            final_answer, success, stream_last_msg, _tokens = await stream_and_display(
                placeholder_message,
                model_name=model,
                history=history,
                system_instruction=system_instruction,
                thinking_level=chat_state.thinking_level,
                user_id=user_id,
                bot=placeholder_message.get_bot(),
                chat_id=chat_id or 0,
                chat_type=(placeholder_message.chat.type if placeholder_message.chat else "private"),
                enable_web_search=True,
            )

            # Detect error-tagged responses (e.g., 429 quota error yielded
            # mid-stream). stream_and_display returns success=True because
            # the generator completed, but the text contains an error tag.
            # Check for error tag ANYWHERE in text (not just at start) since
            # partial real content may precede the error tag.
            if final_answer and (_TAG_PREFIX in final_answer or is_error_message(final_answer)):
                logging.warning(
                    "QnA search: model %s returned error-tagged response, trying next model",
                    model,
                )
                # Delete the message containing the error so user doesn't
                # see two messages (error + answer).  Then send a fresh
                # placeholder for the next model to stream into.
                error_msg = stream_last_msg if stream_last_msg else placeholder_message
                try:
                    await error_msg.delete()
                except Exception:
                    pass
                try:
                    placeholder_message = await placeholder_message.reply_text(
                        "🔎 Ищу быстрый ответ (другая модель)..."
                    )
                except Exception:
                    pass
                continue

            if success and final_answer and final_answer.strip():
                logging.info("QnA search: model %s succeeded (%d chars)", model, len(final_answer))
                break  # Success — exit fallback loop
            else:
                logging.warning("QnA search: model %s returned empty/no-success, trying next", model)
                continue

        except Exception as e:
            logging.error("QnA search: model %s failed: %s", model, e, exc_info=True)
            if attempt_idx < len(fallback_chain) - 1:
                continue
            # Last model also failed
            final_answer = None
            success = False

    streamed = bool(success and final_answer)

    # ── Fallback: non-streaming response if all streaming attempts failed ──
    if not streamed:
        # Try one last time via non-streaming with the last model
        for model in reversed(fallback_chain):
            try:
                final_answer, _ = await _get_ai_response_with_routing(
                    model,
                    history,
                    system_instruction=system_instruction,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                if final_answer and final_answer.strip():
                    break
            except Exception as e:
                logging.error("QnA non-streaming fallback failed for %s: %s", model, e)
                continue

    # ── Handle response ────────────────────────────────────────────────
    if await handle_ai_response_error(final_answer, placeholder_message):
        return

    if final_answer:
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        if not streamed:
            await send_long_message(placeholder_message, final_answer, reply_markup=reply_markup)
        else:
            button_msg = stream_last_msg if stream_last_msg else placeholder_message
            try:
                await button_msg.edit_reply_markup(reply_markup=reply_markup)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logging.warning("Final button edit failed: %s", e)
    else:
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "Получен пустой ответ от API.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


@track_metrics("research_search")
async def _handle_research_agent(
    placeholder_message: Message,
    user_id: int,
    user_message: str,
    chat_state: ChatState,
    model_override: str | None = None,
    search_query: str | None = None,
):
    # If search_query is provided, use it for search, else use user_message
    actual_search_query = search_query if search_query else user_message

    stop_heartbeat(placeholder_message.message_id)
    await metrics_collector.record_search_query()

    # Determine trace IDs
    trace_user_id: int | None = placeholder_message.from_user.id if placeholder_message.from_user else None
    trace_chat_id: int | None = placeholder_message.chat.id if placeholder_message.chat else None

    # Get the key and model for AgenticSearch. Fallback to default if no OpenRouter/override
    from app.repos.keys import get_available_gemini_key, increment_gemini_key_usage

    model_used = model_override or chat_state.model or settings.AGENTIC_MODEL or settings.RESEARCH_MODEL
    key_data = await get_available_gemini_key(model_used)
    if not key_data:
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                "❌ Нет доступных ключей API. Пожалуйста, попробуйте позже.",
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError):
            pass
        return

    key_hash = key_data["key_hash"]

    # Define the status callback that will show stages + fun facts
    last_fact_time = 0.0
    current_fact = ""

    async def on_status(stage_text: str, *, detail: str | None = None):
        nonlocal last_fact_time, current_fact
        now = time.monotonic()
        if now - last_fact_time > 10.0 or not current_fact:
            current_fact = await get_waiting_message(trace_user_id)
            last_fact_time = now

        # Build rich status: stage + optional detail + fun fact
        parts = [stage_text]
        if detail:
            parts.append(detail)
        parts.append(current_fact)
        full_text = "\n\n".join(parts)

        try:
            await placeholder_message.edit_text(full_text)
        except (BadRequest, NetworkError) as edit_error:
            if "not modified" not in str(edit_error).lower():
                logging.error("Could not edit placeholder message with status: %s", edit_error)

    # ── Build ranked fallback model chain ──────────────────────────────────
    # Sort by capability tier (highest = most intelligent) using the existing
    # _get_tier() ranking from model_selector.py. Primary model always first.
    from app.model_selector import _get_tier

    _primary = model_used
    _other_models = [m for m in settings.AVAILABLE_MODELS if m != _primary]
    # Sort remaining models by tier descending (most capable fallback first)
    _other_models.sort(key=_get_tier, reverse=True)
    _fallback_chain: list[str] = [_primary] + _other_models

    # Run the agentic loop with automatic model fallback
    result: AgenticResult | None = None
    final_answer: str | None = None

    for attempt_idx, attempt_model in enumerate(_fallback_chain):
        # Resolve key for this model
        if attempt_idx > 0:
            # Need a fresh key for the fallback model
            key_data = await get_available_gemini_key(attempt_model)
            if not key_data:
                logging.warning(
                    "Agentic fallback: no keys available for model %s, skipping",
                    attempt_model,
                )
                continue
            key_hash = key_data["key_hash"]
            model_used = attempt_model

            # Notify user about the fallback
            try:
                await placeholder_message.edit_text(
                    f"⚡ Переключаюсь на модель {attempt_model}...\n\n"
                    f"_(предыдущая модель недоступна, попытка {attempt_idx + 1})_"
                )
            except (BadRequest, NetworkError):
                pass

        # Rebuild callback for this model+key pair
        _current_model = model_used
        _current_key_hash = key_hash

        async def _on_key_used(
            _kh: str = _current_key_hash,
            _cm: str = _current_model,
        ) -> None:
            await increment_gemini_key_usage(_kh, _cm)
            await metrics_collector.record_api_call("gemini_agentic", model=_cm, user_id=trace_user_id)

        agent = AgenticSearch(
            model_name=model_used,
            api_key=key_data["api_key"],
            on_key_used=_on_key_used,
        )

        try:
            result = await agent.run(
                query=actual_search_query,
                on_status=on_status,
                user_id=trace_user_id,
                chat_id=trace_chat_id,
                history=chat_state.history if chat_state.history else None,
                thinking_level=chat_state.thinking_level,
            )
            final_answer = result.answer
            logging.info(
                "AgenticSearch finished (model=%s, attempt=%d): %d LLM calls, %d total tokens",
                model_used,
                attempt_idx + 1,
                result.llm_calls,
                result.total_tokens,
            )

            # Check if this is an error answer → try next model
            if final_answer and final_answer.startswith("❌") and attempt_idx < len(_fallback_chain) - 1:
                logging.warning(
                    "Agentic model %s returned error answer, falling back to next model (attempt %d/%d)",
                    model_used,
                    attempt_idx + 1,
                    len(_fallback_chain),
                )
                continue

            # Success or last attempt — break out
            break

        except Exception as ai_error:
            logging.error(
                "Error in AgenticSearch run (model=%s, attempt=%d/%d): %s",
                model_used,
                attempt_idx + 1,
                len(_fallback_chain),
                ai_error,
                exc_info=True,
            )
            if attempt_idx < len(_fallback_chain) - 1:
                logging.info("Will retry with next model in fallback chain")
                continue

            # Last model also failed — show error to user
            try:
                await placeholder_message.edit_text(
                    "❌ Внутренняя ошибка при проведении глубокого исследования. "
                    "Все доступные модели были опробованы. Попробуйте позже."
                )
            except (BadRequest, NetworkError):
                pass
            return

    # Process and display the final answer
    if final_answer and not final_answer.startswith("❌"):
        # We successfully got an answer
        buttons = [
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        await send_long_message(
            placeholder_message,
            final_answer,
            reply_markup=reply_markup,
            is_deep_dive=True,
        )

        # Save to history
        chat_state.history.append({"role": "user", "parts": [actual_search_query]})
        chat_state.history.append({"role": "model", "parts": [final_answer]})
        chat_state.is_deep_dive = True

        # Generate unique thread_id for deep dive session if absent
        import uuid

        if not hasattr(chat_state, "deep_dive_thread_id") or not chat_state.deep_dive_thread_id:
            chat_state.deep_dive_thread_id = str(uuid.uuid4())
            logging.info(
                "Generated deep dive thread_id %s for user %s",
                chat_state.deep_dive_thread_id,
                user_id,
            )

        await update_user_chat(user_id, chat_state)
    else:
        # Agent failed to get an answer or returned an explicit error
        error_msg = final_answer if final_answer else "Не удалось сформировать ответ."
        try:
            from app.errors import build_retry_and_roles_keyboard

            await placeholder_message.edit_text(
                error_msg,
                reply_markup=build_retry_and_roles_keyboard(),
            )
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)


@track_metrics("complex_search")
async def _handle_complex_agent_search(placeholder_message: Message, original_message: Message, search_prefix: str):
    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Используем `from_user.id`, а не `effective_user.id`
    user_id = original_message.from_user.id

    try:
        await placeholder_message.edit_text("🖼️ Анализирую изображение...")
    except (BadRequest, NetworkError) as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        # If не можем отредактировать, отправляем new message
        placeholder_message = await placeholder_message.reply_text("🖼️ Анализирую изображение...")

    vision_model = settings.RESEARCH_MODEL

    photo_file = await original_message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(photo_data))

    analysis_prompt = get_registry().get("image_analysis").text

    # Create parts for Gemini API: промпт + image
    parts = [analysis_prompt, img] if img else [analysis_prompt]
    chat_id = placeholder_message.chat.id if placeholder_message.chat else None
    search_query, _ = await _get_ai_response_with_routing(
        vision_model,
        [{"role": "user", "parts": parts}],
        user_id=user_id,
        chat_id=chat_id,
    )

    # Check ошибки от роутера
    if await handle_ai_response_error(search_query, placeholder_message):
        return

    if not search_query:
        try:
            await placeholder_message.edit_text("Не удалось проанализировать изображение для поиска.")
        except (BadRequest, NetworkError) as edit_error:
            logging.error("Could not edit placeholder message: %s", edit_error)
        return

    chat_state = await get_user_chat(user_id)
    # Get оригинальное message user for локалfromации
    original_user_message = original_message.caption or "Опиши это изображение."

    if search_prefix == "?":
        await _handle_qna_search(placeholder_message, original_user_message, chat_state, search_query)
    else:
        await _handle_research_agent(
            placeholder_message,
            user_id,
            original_user_message,
            chat_state,
            search_query=search_query,
        )
