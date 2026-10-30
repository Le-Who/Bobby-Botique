"""R3 business/uncertainty regressions migrated to the authoritative v2 store."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import queue_runtime as module
from app.queue import TaskQueue, TaskStatus
from app.queue_contracts import JobOutcome, JobResult, JobState, QueueConflict, QueueUnavailable
from app.queue_store import JobEnvelope, MemoryQueueStore


async def _run_job(monkeypatch, handler, *, task_type="test_outcome"):
    @asynccontextmanager
    async def settings_scope():
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", settings_scope)
    clock = [1000.0]
    store = MemoryQueueStore(clock=lambda: clock[0])
    queue = TaskQueue(max_workers=1, store=store, clock=lambda: clock[0], random_source=lambda: 0)
    queue.register_handler(
        task_type, handler, lane="deferred", effectful=task_type in {"deferred_ai_response", "document_processing"}
    )
    events = []
    terminal = asyncio.Event()

    def emit(event, **fields):
        events.append((event, fields))
        if event == "job.retry_scheduled":
            clock[0] += fields["delay_seconds"]
            queue._work_available.set()
        if event == "job.finished":
            task = queue.tasks.get(fields.get("job_id"))
            if task and task.completed_at is not None:
                terminal.set()

    monkeypatch.setattr(module, "emit", emit)
    admission = await queue.enqueue_job(0, task_type, {}, idempotency_key="synthetic-operation", lane="deferred")
    await queue.start()
    try:
        await asyncio.wait_for(terminal.wait(), 2)
        return queue.tasks[admission.task_id], events
    finally:
        await queue.stop()


async def test_returned_business_failure_is_not_completed(monkeypatch):
    handler = AsyncMock(return_value={"status": "failed", "error": "synthetic failure"})
    task, events = await _run_job(monkeypatch, handler)
    assert task.status is TaskStatus.FAILED
    assert handler.await_count == 1
    terminal = [fields for event, fields in events if event == "job.finished"]
    assert terminal[-1]["outcome"] == "failed"
    assert "synthetic failure" not in str(task.result)


async def test_unrecognized_handler_payload_is_not_success(monkeypatch):
    task, _ = await _run_job(monkeypatch, AsyncMock(return_value={"unexpected": "data"}))
    assert task.status is TaskStatus.FAILED


@pytest.mark.parametrize("outcome", ["retryable_failure", "permanent_failure", "unknown_delivery", "succeeded"])
async def test_typed_outcome_controls_retry_and_terminal_state(monkeypatch, outcome):
    from app.queue_contracts import JobOutcome, JobResult

    handler = AsyncMock(return_value=JobResult(JobOutcome(outcome), "synthetic_reason"))
    task, events = await _run_job(monkeypatch, handler)
    expected = {
        "succeeded": "completed",
        "retryable_failure": "failed",
        "permanent_failure": "failed",
        "unknown_delivery": "unknown_delivery",
    }
    assert task.status.value == expected[outcome]
    assert handler.await_count == (3 if outcome == "retryable_failure" else 1)
    assert task.attempts_started == handler.await_count
    assert len([fields for event, fields in events if event == "job.retry_scheduled"]) == handler.await_count - 1


async def test_real_deferred_handler_failure_is_failed_after_bounded_retries(monkeypatch):
    from app.deferred_response import handle_deferred_ai_response
    from app.errors import ErrorCode
    from app.response_delivery import delivery
    from app.response_delivery.outcomes import FailedDelivery
    from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef

    bot = MagicMock(send_chat_action=AsyncMock())

    class BotContext:
        async def __aenter__(self):
            return bot

        async def __aexit__(self, *args):
            return False

    receipt = DeliveryReceipt(DeliveryKind.FAILURE, (1,), TelegramMessageRef(10, 1))
    transport = MagicMock(stream=AsyncMock(return_value=FailedDelivery(ErrorCode.OVERLOADED, "notice", receipt)))
    monkeypatch.setattr("telegram.Bot", lambda *_: BotContext())
    monkeypatch.setattr(delivery, "get_telegram_response_delivery", lambda: transport)

    async def handler():
        return await handle_deferred_ai_response(
            chat_id=10, history=[{"role": "user", "parts": ["synthetic question"]}], model_name="gemini-3.5-flash"
        )

    task, events = await _run_job(monkeypatch, handler)
    assert task.status is TaskStatus.FAILED
    assert task.attempts_started == transport.stream.await_count == 3
    assert all(fields["outcome"] == "failed" for event, fields in events if event == "job.finished")
    assert len([1 for event, _ in events if event == "job.retry_scheduled"]) == 2


@pytest.mark.parametrize("kind", ["deferred_ai_response", "document_processing"])
@pytest.mark.parametrize("error_type", [QueueUnavailable, QueueConflict])
async def test_unconfirmed_finish_is_unknown_without_success_or_retry(monkeypatch, kind, error_type):
    clock = [1000.0]
    store = MemoryQueueStore(clock=lambda: clock[0])
    queue = TaskQueue(store=store, clock=lambda: clock[0])
    await store.admit(JobEnvelope("job", kind, "deferred", 0, "{}", "operation"), queue_policy())
    claim = await store.claim("deferred", "owner")
    queue._cache(claim.snapshot)

    async def fail(*args, **kwargs):
        raise error_type("synthetic_unconfirmed")

    monkeypatch.setattr(store, "finish", fail)
    events = []
    monkeypatch.setattr(module, "emit", lambda event, **fields: events.append((event, fields)))
    await queue._finish_claim(claim, JobResult(JobOutcome.SUCCEEDED))
    assert queue.tasks["job"].status is TaskStatus.UNKNOWN_DELIVERY
    assert (await store.read_job("job")).state is JobState.RUNNING
    assert (await store.stats("deferred"))["succeeded"] == 0
    assert events[-1][1]["outcome"] == "unknown"
    assert not any(event == "job.retry_scheduled" for event, _ in events)
    clock[0] += 61
    await store.sweep()
    assert (await store.read_job("job")).state is JobState.UNKNOWN_DELIVERY
    assert await store.claim("deferred", "new-owner") is None


def queue_policy():
    from app.queue_contracts import QueuePolicy

    return QueuePolicy()


@pytest.mark.parametrize("kind", ["deferred_ai_response", "document_processing"])
async def test_escaped_effectful_handler_exception_is_quarantined_once(monkeypatch, kind):
    effects = []

    async def handler():
        effects.append("synthetic effect")
        raise RuntimeError("synthetic after effect")

    task, events = await _run_job(monkeypatch, handler, task_type=kind)
    assert effects == ["synthetic effect"]
    assert task.status is TaskStatus.UNKNOWN_DELIVERY
    assert not any(event == "job.retry_scheduled" for event, _ in events)


async def test_fifty_equal_priority_jobs_and_mixed_priorities_remain_fifo():
    for mixed in (False, True):
        store = MemoryQueueStore(clock=lambda: 1000)
        expected = {priority: [] for priority in range(1, 5)}
        for index in range(50):
            task_id = f"job-{100 - index}"
            priority = index % 4 + 1 if mixed else 2
            expected[priority].append(task_id)
            await store.admit(
                JobEnvelope(
                    task_id, "cleanup_metrics", "maintenance", 0, "{}", task_id, priority=priority, effectful=False
                ),
                queue_policy(),
            )
        actual = []
        for index in range(50):
            claim = await store.claim("maintenance", f"owner-{index}")
            actual.append(claim.snapshot.task_id)
            await store.finish(
                claim.snapshot.task_id, claim.owner_token, claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED)
            )
        assert actual == [task_id for priority in range(4, 0, -1) for task_id in expected[priority]]


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_late_safe_retry_reply_cannot_replace_newer_completion(monkeypatch, lost_reply):
    clock = [1000.0]
    store = MemoryQueueStore(clock=lambda: clock[0])
    queue = TaskQueue(store=store, clock=lambda: clock[0], random_source=lambda: 0)
    await store.admit(JobEnvelope("job", "document_processing", "deferred", 0, "{}", "operation"), queue_policy())
    claim = await store.claim("deferred", "first")
    queue._cache(claim.snapshot)
    actual_finish = store.finish

    async def delayed_finish(task_id, owner, revision, result, **kwargs):
        snapshot = await actual_finish(task_id, owner, revision, result, **kwargs)
        if result.outcome is JobOutcome.RETRYABLE_FAILURE:
            clock[0] += 15
            newer = await store.claim("deferred", "second")
            queue._cache(newer.snapshot)
            await queue._finish_claim(newer, JobResult(JobOutcome.SUCCEEDED))
            if lost_reply:
                raise QueueUnavailable("synthetic lost reply")
        return snapshot

    monkeypatch.setattr(store, "finish", delayed_finish)
    await queue._finish_claim(claim, JobResult(JobOutcome.RETRYABLE_FAILURE))
    assert (await queue.read_job("job")).state is JobState.SUCCEEDED
    assert queue.tasks["job"].status is TaskStatus.COMPLETED
