# /app/deferred_response.py
"""Deferred AI Response — Redis queue worker for retrying failed generations.

When all API keys AND the cascade fallback model fail (total service outage),
instead of showing a cold error, the request is pushed to the TaskQueue.
A background worker retries the generation after a cooldown and delivers
the result as a follow-up Telegram message.
"""

import logging

from app.errors import is_retryable_error_code
from app.queue_contracts import JobOutcome, JobResult


async def enqueue_deferred_generation(
    user_id: int,
    chat_id: int,
    history: list,
    model_name: str,
    system_instruction: str | None = None,
    *,
    operation_id: str | None = None,
    expected_epoch: int | None = None,
) -> str | None:
    """Enqueue a failed generation request for background retry.

    Returns the task_id on success, or None if the queue is unavailable.
    """
    from app.queue import task_queue
    from app.queue_contracts import AdmissionState, QueueAdmissionUnconfirmed

    try:
        if not operation_id or expected_epoch is None:
            return None
        admission = await task_queue.enqueue_job(
            user_id,
            "deferred_ai_response",
            {
                "chat_id": chat_id,
                "history": history,
                "model_name": model_name,
                "system_instruction": system_instruction,
            },
            idempotency_key=f"deferred_ai_response:{operation_id}",
            lane="deferred",
            priority=3,
            delay_seconds=15,
            expected_epoch=expected_epoch,
        )
        if admission.state is AdmissionState.UNKNOWN:
            raise QueueAdmissionUnconfirmed(admission.task_id)
        task_id = admission.task_id if admission.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING} else None
        if task_id:
            logging.info(
                "Deferred generation enqueued: task=%s user=%s model=%s",
                task_id,
                user_id,
                model_name,
            )
        return task_id or None
    except QueueAdmissionUnconfirmed:
        raise
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


async def handle_deferred_ai_response(**kwargs) -> JobResult:
    """TaskQueue handler: retry AI generation and send result via Telegram.

    This runs inside a TaskQueue worker. On success, sends the generated
    text as a new message to the user's chat.
    """
    chat_id = kwargs.get("chat_id")
    history = kwargs.get("history")
    model_name = kwargs.get("model_name")
    system_instruction = kwargs.get("system_instruction")

    if (
        type(chat_id) is not int
        or chat_id == 0
        or not isinstance(history, list)
        or not history
        or not isinstance(model_name, str)
        or not model_name.strip()
        or (system_instruction is not None and not isinstance(system_instruction, str))
        or any(
            not isinstance(entry, dict) or entry.get("role", "user") not in ("user", "model", "assistant")
            for entry in history
        )
    ):
        return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_deferred_parameters")

    from app.providers.request_factory import generation_request_from_history
    from app.providers.stream_types import Workload

    try:
        request = await generation_request_from_history(
            models=(model_name,),
            history=history,
            system_instruction=system_instruction,
            workload=Workload.DEFERRED_RETRY,
            allow_deferred=False,
        )
    except TypeError, ValueError:
        return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_deferred_parameters")
    except Exception as error:
        logging.error("Deferred request preparation failed (%s)", type(error).__name__)
        return JobResult(JobOutcome.RETRYABLE_FAILURE, "request_preparation_failed")

    # Cooldown belongs to the delayed index and occupies no worker.
    from telegram import Bot

    from app.config import settings
    from app.response_delivery.delivery import (
        TelegramTarget,
        get_telegram_response_delivery,
    )
    from app.response_delivery.outcomes import CompleteDelivery, FailedDelivery, PartialDelivery
    from app.response_delivery.presentation import FixedPresentation

    delivery_started = False
    try:
        from app.queue import current_job_guard

        guard = current_job_guard()
        if guard is not None:
            await guard.checkpoint()
        delivery = get_telegram_response_delivery()
        async with Bot(settings.TELEGRAM_BOT_TOKEN) as bot:
            target = TelegramTarget(bot=bot, chat_id=chat_id)
            presentation = FixedPresentation(
                display_prefix="💬 _(отложенный ответ)_\n\n",
                long_read_title="Отложенный ответ ИИ",
            )
            if guard is not None:
                await guard.checkpoint()
            delivery_started = True
            outcome = await delivery.stream(
                target,
                request,
                presentation=presentation,
            )
    except Exception as e:
        logging.error("Deferred response transport failed (%s)", type(e).__name__)
        return JobResult(
            JobOutcome.UNKNOWN_DELIVERY if delivery_started else JobOutcome.RETRYABLE_FAILURE,
            "delivery_unconfirmed" if delivery_started else "delivery_not_started",
        )

    if not isinstance(outcome, (CompleteDelivery, PartialDelivery)):
        if isinstance(outcome, FailedDelivery) and not outcome.content_text:
            return JobResult(
                JobOutcome.RETRYABLE_FAILURE
                if is_retryable_error_code(outcome.error_code)
                else JobOutcome.PERMANENT_FAILURE,
                "generation_unavailable" if is_retryable_error_code(outcome.error_code) else "generation_rejected",
            )
        return JobResult(JobOutcome.PERMANENT_FAILURE, "unexpected_delivery_outcome")

    logging.info("Deferred response delivered to chat=%s", chat_id)
    return JobResult(
        JobOutcome.SUCCEEDED,
        result_summary={"text_length": len(outcome.content_text), "partial": isinstance(outcome, PartialDelivery)},
    )
