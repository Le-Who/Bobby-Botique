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
                task_id, user_id, model_name,
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
                if isinstance(part, dict) and "text" in part or isinstance(part, str):
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
    await asyncio.sleep(15.0)

    from app.providers import get_provider_router

    router = get_provider_router()
    chunks: list[str] = []

    async for chunk in router.stream_response(
        preferred_model=model_name,
        history=history,
        system_instruction=system_instruction,
        max_key_retries=2,
    ):
        chunks.append(chunk)

    full_text = "".join(chunks)
    if not full_text or full_text.startswith("🚫"):
        return {"status": "failed", "error": "API still unavailable after deferred retry"}

    # Deliver the result to the user
    from telegram import Bot

    from app.config import settings

    try:
        async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(
                chat_id=chat_id,
                text=f"💬 _(отложенный ответ)_\n\n{full_text}",
                parse_mode="Markdown",
            )
            logging.info("Deferred response delivered to chat=%s", chat_id)
    except Exception as e:
        logging.error("Failed to deliver deferred response: %s", e)
        return {"status": "failed", "error": f"Delivery failed: {e}"}

    return {"status": "completed", "text_length": len(full_text)}
