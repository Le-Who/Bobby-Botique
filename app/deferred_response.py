# /app/deferred_response.py
"""Deferred AI Response — Redis queue worker for retrying failed generations.

When all API keys AND the cascade fallback model fail (total service outage),
instead of showing a cold error, the request is pushed to the TaskQueue.
A background worker retries the generation after a cooldown and delivers
the result as a follow-up Telegram message.
"""

import asyncio
import logging


async def enqueue_deferred_generation(
    user_id: int,
    chat_id: int,
    history: list,
    model_name: str,
    system_instruction: str | None = None,
) -> str | None:
    """Enqueue a failed generation request for background retry.

    Returns the task_id on success, or None if the queue is unavailable.
    """
    from app.queue import TaskPriority, add_background_task

    try:
        task_id = await add_background_task(
            user_id=user_id,
            task_type="deferred_ai_response",
            data={
                "chat_id": chat_id,
                "history": _truncate_history(history),
                "model_name": model_name,
                "system_instruction": system_instruction,
            },
            priority=TaskPriority.HIGH,
        )
        if task_id:
            logging.info(
                "Deferred generation enqueued: task=%s user=%s model=%s",
                task_id,
                user_id,
                model_name,
            )
        return task_id or None
    except Exception as e:
        logging.error("Failed to enqueue deferred generation: %s", e)
        return None


def _truncate_history(history: list, max_entries: int = 10) -> list:
    """Truncate conversation history for Redis serialization.

    Keeps only the last N entries and strips large binary payloads
    (images, audio) to keep the Redis payload under control.
    """
    truncated = history[-max_entries:] if len(history) > max_entries else list(history)
    clean = []
    for entry in truncated:
        if isinstance(entry, dict):
            # Strip binary payloads from parts
            parts = entry.get("parts", [])
            text_parts = []
            for part in parts:
                if (isinstance(part, dict) and "text" in part) or isinstance(part, str):
                    text_parts.append(part)
                # Skip inline_data (images, audio) — too large for Redis
            clean.append({**entry, "parts": text_parts})
        else:
            clean.append(entry)
    return clean


async def handle_deferred_ai_response(**kwargs) -> dict:
    """TaskQueue handler: retry AI generation and send result via Telegram.

    This runs inside a TaskQueue worker. On success, sends the generated
    text as a new message to the user's chat.
    """
    chat_id = kwargs["chat_id"]
    history = kwargs["history"]
    model_name = kwargs["model_name"]
    system_instruction = kwargs.get("system_instruction")

    # Cooldown before retrying — give the API time to recover
    from telegram import Bot

    from app.config import settings

    try:
        async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
            for _ in range(3):
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(5.0)
    except Exception:
        await asyncio.sleep(15.0)

    from app.providers.request_factory import generation_request_from_history
    from app.providers.stream_types import Workload
    from app.response_delivery.delivery import (
        TelegramTarget,
        get_telegram_response_delivery,
    )
    from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery
    from app.response_delivery.presentation import FixedPresentation

    request = await generation_request_from_history(
        models=(model_name,),
        history=history,
        system_instruction=system_instruction,
        workload=Workload.DEFERRED_RETRY,
        allow_deferred=False,
    )

    try:
        async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
            outcome = await get_telegram_response_delivery().stream(
                TelegramTarget(bot=bot, chat_id=chat_id),
                request,
                presentation=FixedPresentation(
                    display_prefix="💬 _(отложенный ответ)_\n\n",
                    long_read_title="Отложенный ответ ИИ",
                ),
            )
    except Exception as e:
        logging.error("Failed to deliver deferred response: %s", e)
        return {"status": "failed", "error": f"Delivery failed: {e}"}

    if not isinstance(outcome, (CompleteDelivery, PartialDelivery)):
        return {"status": "failed", "error": "API still unavailable after deferred retry"}

    logging.info("Deferred response delivered to chat=%s", chat_id)
    return {"status": "completed", "text_length": len(outcome.content_text)}
