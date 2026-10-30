"""The durable state machine fences owners and retains private data finitely."""

import pytest

from app.queue_contracts import JobOutcome, JobResult, JobState, QueueConflict, QueuePolicy
from app.queue_store import JobEnvelope, MemoryQueueStore


@pytest.fixture
def store_clock():
    clock = [1000.0]
    return MemoryQueueStore(clock=lambda: clock[0]), clock


async def submitted(store, *, task_id="job", kind="deferred_ai_response", effectful=True):
    await store.admit(
        JobEnvelope(
            task_id, kind, "deferred", 1, '{"private":"synthetic"}', task_id, privacy_epoch=7, effectful=effectful
        ),
        QueuePolicy(),
    )


async def test_claim_and_finish_are_shared_revisioned_and_owner_fenced(store_clock):
    store, _ = store_clock
    await submitted(store)
    claim = await store.claim("deferred", "owner-a")
    assert (await store.read_job("job")).state is JobState.RUNNING
    assert claim.snapshot.attempts_started == 1
    with pytest.raises(QueueConflict):
        await store.finish("job", "owner-b", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    done = await store.finish("job", "owner-a", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    assert done.state is JobState.SUCCEEDED
    assert "job" not in store.payloads
    assert (await store.stats("deferred"))["nonterminal"] == 0
    assert (await store.stats("deferred"))["payload_bytes"] == 0


async def test_cancel_running_requires_owner_ack_and_cannot_be_completed(store_clock):
    store, _ = store_clock
    await submitted(store)
    claim = await store.claim("deferred", "owner-a")
    cancel = await store.request_cancel(
        "job", expected_revision=claim.snapshot.revision, actor="admin", operation_id="cancel-1"
    )
    assert cancel.state is JobState.CANCEL_REQUESTED
    replay = await store.request_cancel(
        "job", expected_revision=claim.snapshot.revision, actor="admin", operation_id="cancel-1"
    )
    assert replay == cancel
    with pytest.raises(QueueConflict):
        await store.request_cancel("job", expected_revision=cancel.revision, actor="admin", operation_id="cancel-1")
    final = await store.finish("job", "owner-a", cancel.revision, JobResult(JobOutcome.SUCCEEDED))
    assert final.state is JobState.CANCELLED
    assert not final.retry_payload_available


async def test_cancel_requested_never_erases_unknown_external_delivery(store_clock):
    store, _ = store_clock
    await submitted(store)
    claim = await store.claim("deferred", "owner")
    requested = await store.request_cancel(
        "job", expected_revision=claim.snapshot.revision, actor="admin", operation_id="cancel"
    )
    final = await store.finish(
        "job", "owner", requested.revision, JobResult(JobOutcome.UNKNOWN_DELIVERY, "send_unconfirmed")
    )
    assert final.state is JobState.UNKNOWN_DELIVERY
    assert final.reason_code == "send_unconfirmed"
    assert not final.retry_eligible and not final.retry_payload_available


async def test_heartbeat_and_progress_do_not_stale_operator_command_revision(store_clock):
    store, clock = store_clock
    await submitted(store)
    claim = await store.claim("deferred", "owner")
    held = claim.snapshot.revision
    clock[0] += 3
    heart = await store.heartbeat("job", "owner", held)
    progress = await store.update_progress("job", "owner", heart.revision, {"phase": "generation", "completed": 1})
    requested = await store.request_cancel("job", expected_revision=held, actor="admin", operation_id="cancel")
    assert heart.revision == progress.revision == held
    assert requested.state is JobState.CANCEL_REQUESTED
    with pytest.raises(QueueConflict):
        await store.request_cancel(
            "job", expected_revision=held, actor="admin", operation_id="stale-after-state-change"
        )


async def test_cancel_queued_prevents_claim_and_erases_payload(store_clock):
    store, _ = store_clock
    await submitted(store)
    job = await store.read_job("job")
    done = await store.request_cancel("job", expected_revision=job.revision, actor="admin", operation_id="cancel")
    assert done.state is JobState.CANCELLED
    assert await store.claim("deferred", "owner") is None
    assert not store.payloads


@pytest.mark.parametrize("effectful, expected", [(True, JobState.UNKNOWN_DELIVERY), (False, JobState.QUEUED)])
async def test_recovery_never_replays_interrupted_external_effects(store_clock, effectful, expected):
    store, clock = store_clock
    await submitted(store, effectful=effectful)
    claim = await store.claim("deferred", "dead-owner")
    clock[0] += 61
    await store.sweep()
    assert (await store.read_job("job")).state is expected
    with pytest.raises(QueueConflict):
        await store.finish("job", "dead-owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))


async def test_safe_manual_retry_creates_child_with_one_grant_and_original_terminal_unchanged(store_clock):
    store, clock = store_clock
    await submitted(store)
    claim = await store.claim("deferred", "owner")
    # A safe failure whose next delay cannot fit becomes expired, retaining only a bounded retry capability.
    failed = await store.finish(
        "job", "owner", claim.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE), delay_seconds=1000
    )
    assert failed.state is JobState.EXPIRED
    assert failed.retry_payload_available
    admission = await store.retry_job(
        "job", expected_revision=failed.revision, actor="admin", operation_id="retry", privacy_epoch=7
    )
    child = await store.read_job(admission.task_id)
    assert child.parent_task_id == "job"
    assert (await store.read_job("job")).state is JobState.EXPIRED
    assert child.task_id != failed.task_id
    assert not (await store.read_job("job")).retry_payload_available
    with pytest.raises(QueueConflict):
        await store.retry_job(
            "job", expected_revision=failed.revision, actor="admin", operation_id="another", privacy_epoch=7
        )
    clock[0] += 901
    await store.sweep()
    assert not (await store.read_job("job")).retry_payload_available


async def test_account_erasure_removes_jobs_payload_and_idempotency(store_clock):
    store, _ = store_clock
    await submitted(store)
    await store.erase_user(1)
    assert await store.read_job("job") is None
    assert not store.payloads and not store.identities


async def test_manual_retry_transfers_payload_without_double_reserving_exact_byte_capacity():
    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_total_payload_bytes=2, max_attempts=1)
    await store.admit(JobEnvelope("root", "deferred_ai_response", "deferred", 0, "{}", "operation"), policy)
    claim = await store.claim("deferred", "owner")
    failed = await store.finish("root", "owner", claim.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE))
    child = await store.retry_job(
        "root", expected_revision=failed.revision, actor="admin", operation_id="retry", privacy_epoch=None
    )
    assert child.task_id is not None
    assert len(store.payloads) == 1
    assert (await store.stats("deferred"))["payload_bytes"] == 2
