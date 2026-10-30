import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from app.observability.context import request_scope
from app.providers.request_factory import generation_request_from_history
from app.queue import TaskQueue
from app.queue_contracts import AdmissionState
from app.queue_store import MemoryQueueStore


async def test_request_factory_only_propagates_original_generation_and_fails_closed_without_it(monkeypatch):
    monkeypatch.setattr(
        "app.repos.memory_consent.resolve_current_epoch",
        AsyncMock(side_effect=AssertionError("must not recapture a generation")),
    )
    request = await generation_request_from_history(
        models=("synthetic",),
        history=[{"role": "user", "parts": ["synthetic"]}],
        user_id=7,
        chat_id=7,
        privacy_epoch=11,
    )
    assert request.scope.privacy_epoch == 11
    unknown = await generation_request_from_history(
        models=("synthetic",), history=[{"role": "user", "parts": ["synthetic"]}], user_id=7, chat_id=7
    )
    assert unknown.allow_deferred is False


async def test_provider_inflight_then_account_recreated_cannot_enqueue_old_payload(monkeypatch):
    from app.deferred_response import enqueue_deferred_generation

    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    monkeypatch.setattr("app.queue.task_queue", queue)
    request = await generation_request_from_history(
        models=("synthetic",),
        history=[{"role": "user", "parts": ["old private synthetic text"]}],
        user_id=7,
        chat_id=7,
        privacy_epoch=11,
        operation_id="original-operation",
    )
    # The provider is in flight with the captured request. Erasure/recreation
    # advances the existing globally allocated account generation to twelve.
    monkeypatch.setattr("app.repos.memory_consent.resolve_current_epoch", AsyncMock(return_value=12))
    with request_scope(request_id="original-operation"):
        result = await enqueue_deferred_generation(
            7,
            7,
            [{"role": "user", "parts": ["old private synthetic text"]}],
            "synthetic",
            expected_epoch=request.scope.privacy_epoch,
            operation_id=request.scope.operation_id,
        )
    assert result is None
    assert not store.jobs and not store.payloads


async def test_worker_loads_private_payload_only_inside_original_generation_lease(monkeypatch):
    from app.queue_contracts import JobOutcome, JobResult, QueuePolicy
    from app.queue_runtime import JobExecutionGuard
    from app.queue_store import JobEnvelope

    permitted = [False]

    @asynccontextmanager
    async def lease(user_id, epoch, **kwargs):
        assert user_id == 7 and epoch == 11
        permitted[0] = True
        try:
            yield True
        finally:
            permitted[0] = False

    @asynccontextmanager
    async def settings():
        yield

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", AsyncMock(return_value=True))
    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", settings)
    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    queue.register_handler("test_private", AsyncMock(return_value=JobResult(JobOutcome.SUCCEEDED)), lane="deferred")
    await store.admit(
        JobEnvelope(
            "private",
            "test_private",
            "deferred",
            7,
            json.dumps({"data": {}, "context": {}}),
            "private",
            privacy_epoch=11,
        ),
        QueuePolicy(),
    )
    claim = await store.claim("deferred", "owner")
    assert claim.payload is None
    actual_load = store.load_payload

    async def load(*args):
        assert permitted[0], "Payload was read before the durable privacy lease"
        return await actual_load(*args)

    monkeypatch.setattr(store, "load_payload", load)
    result = await queue._run_claim(claim, JobExecutionGuard(queue, claim))
    assert result.outcome is JobOutcome.SUCCEEDED
    assert permitted[0] is False
