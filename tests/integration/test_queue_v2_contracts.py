"""Execute v2 production Lua against explicitly disposable Redis, never a protocol double."""

import json
import os
import uuid

import pytest
from redis.asyncio import Redis

from app.queue_contracts import AdmissionState, JobOutcome, JobResult, JobState, QueueConflict, QueuePolicy
from app.queue_store import JobEnvelope, RedisQueueStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def v2_store():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for isolated production Lua contracts")
    redis = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    namespace = f"test:queue:v2:{uuid.uuid4().hex}"
    store = RedisQueueStore(redis, namespace=namespace)
    try:
        assert await redis.ping()
        yield store, redis, namespace
    finally:
        try:
            keys = [key async for key in redis.scan_iter(match=f"{namespace}:*", count=100)]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()


def job(task_id="first", *, operation="operation", payload="{}", lane="deferred", effectful=True, delay=0, image=False):
    return JobEnvelope(
        task_id,
        "document_processing" if effectful else "cleanup_metrics",
        lane,
        42,
        payload,
        operation,
        privacy_epoch=7,
        effectful=effectful,
        delay_seconds=delay,
        image_work=image,
    )


async def test_broadcast_lane_cap_and_single_owner_do_not_consume_daily_workers(v2_store):
    store, _, _ = v2_store
    for i in range(10):
        assert (
            await store.admit(job(str(i), operation=f"broadcast:{i}", lane="broadcast"), QueuePolicy())
        ).state is AdmissionState.ACCEPTED
    assert (
        await store.admit(job("overflow", operation="overflow", lane="broadcast"), QueuePolicy())
    ).reason_code == "job_capacity"
    assert await store.claim("broadcast", "broadcast-owner")
    assert await store.claim("broadcast", "second-owner") is None
    assert (await store.stats("daily"))["running"] == 0


async def test_atomic_admission_identity_capacity_and_redis_server_time(v2_store):
    store, redis, prefix = v2_store
    policy = QueuePolicy(max_jobs=1, max_payload_bytes=4, max_total_payload_bytes=4)
    first = await store.admit(job(payload="яя"), policy)
    again = await RedisQueueStore(redis, namespace=prefix).admit(job("different", payload="яя"), policy)
    rejected = await store.admit(job("second", operation="second", payload="x"), policy)
    assert first.state is AdmissionState.ACCEPTED
    assert again.state is AdmissionState.EXISTING and again.task_id == "first"
    assert rejected.reason_code == "job_capacity"
    current = await store.read_job("first")
    server_seconds, _ = await redis.time()
    assert abs(current.submitted_at - server_seconds) < 2
    assert await redis.zcard(f"{prefix}:ready:deferred") == 1
    assert (await store.stats("deferred"))["payload_bytes"] == 4


async def test_committed_admission_with_lost_reply_reconciles_without_second_job(v2_store):
    store, redis, prefix = v2_store

    class LostReply:
        first = True

        async def eval(self, *args):
            value = await redis.eval(*args)
            if self.first:
                self.first = False
                raise ConnectionError("synthetic lost committed reply")
            return value

    result = await RedisQueueStore(LostReply(), namespace=prefix).admit(job(), QueuePolicy())
    assert result.state is AdmissionState.EXISTING
    assert (await store.stats("deferred"))["nonterminal"] == 1
    assert await redis.zcard(f"{prefix}:ready:deferred") == 1


async def test_terminal_ack_lost_reply_does_not_double_release_counters(v2_store):
    store, redis, prefix = v2_store
    await store.admit(job(), QueuePolicy())
    claim = await store.claim("deferred", "owner")

    class LostReply:
        first = True

        async def eval(self, *args):
            value = await redis.eval(*args)
            if self.first:
                self.first = False
                raise ConnectionError("synthetic lost finish reply")
            return value

    result = await RedisQueueStore(LostReply(), namespace=prefix).finish(
        "first", "owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED)
    )
    assert result.state is JobState.SUCCEEDED
    assert not await redis.exists(f"{prefix}:payload:first")
    assert 86390 <= await redis.ttl(f"{prefix}:job:first") <= 86400
    assert (await store.stats("deferred"))["payload_bytes"] == 0
    assert (await store.stats("deferred"))["nonterminal"] == 0
    assert (await store.stats("deferred"))["succeeded"] == 1


async def test_owner_cancel_cas_and_late_finish_fencing(v2_store):
    store, _, _ = v2_store
    await store.admit(job(), QueuePolicy())
    claim = await store.claim("deferred", "owner")
    requested = await store.request_cancel(
        "first", expected_revision=claim.snapshot.revision, actor="admin", operation_id="cancel"
    )
    assert requested.state is JobState.CANCEL_REQUESTED
    replay = await store.request_cancel(
        "first", expected_revision=claim.snapshot.revision, actor="admin", operation_id="cancel"
    )
    assert replay.state is JobState.CANCEL_REQUESTED
    with pytest.raises(QueueConflict):
        await store.finish("first", "expired-owner", requested.revision, JobResult(JobOutcome.SUCCEEDED))
    result = await store.finish("first", "owner", requested.revision, JobResult(JobOutcome.SUCCEEDED))
    assert result.state is JobState.CANCELLED
    assert (await store.stats("deferred"))["cancelled"] == 1
    assert (await store.stats("deferred"))["cancel_requested"] == 0


async def test_heartbeat_progress_keep_command_revision_and_cancel_preserves_unknown(v2_store):
    store, _, _ = v2_store
    await store.admit(job(), QueuePolicy())
    claim = await store.claim("deferred", "owner")
    held = claim.snapshot.revision
    heartbeat = await store.heartbeat("first", "owner", held)
    progress = await store.update_progress("first", "owner", held, {"phase": "generation", "completed": 1})
    assert heartbeat.revision == progress.revision == held
    requested = await store.request_cancel("first", expected_revision=held, actor="admin", operation_id="cancel")
    with pytest.raises(QueueConflict):
        await store.request_cancel("first", expected_revision=held, actor="admin", operation_id="stale")
    unknown = await store.finish(
        "first", "owner", requested.revision, JobResult(JobOutcome.UNKNOWN_DELIVERY, "send_unconfirmed")
    )
    assert unknown.state is JobState.UNKNOWN_DELIVERY
    assert unknown.reason_code == "send_unconfirmed"
    assert (await store.stats("deferred"))["cancelled"] == 0
    assert (await store.stats("deferred"))["unknown_delivery"] == 1


@pytest.mark.parametrize("effectful,expected", [(True, JobState.UNKNOWN_DELIVERY), (False, JobState.QUEUED)])
async def test_expired_owner_recovery_counts_start_once_and_quarantines_effects(v2_store, effectful, expected):
    store, redis, prefix = v2_store
    await store.admit(job(effectful=effectful), QueuePolicy())
    claim = await store.claim("deferred", "dead-owner")
    m = await store.private_record("first")
    m["heartbeat_at"] -= 61
    await redis.set(f"{prefix}:job:first", json.dumps(m))
    await redis.zadd(f"{prefix}:running:all", {"first": m["heartbeat_at"] + 60})
    await store.sweep()
    await store.sweep()
    snapshot = await store.read_job("first")
    assert snapshot.state is expected
    assert snapshot.attempts_started == 1
    with pytest.raises(QueueConflict):
        await store.finish("first", "dead-owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))


async def test_delayed_promotion_does_not_consume_owner_capacity(v2_store):
    store, redis, prefix = v2_store
    await store.admit(job(delay=15), QueuePolicy())
    assert await store.claim("deferred", "owner") is None
    assert (await store.stats("deferred"))["running"] == 0
    m = await store.private_record("first")
    m["not_before"] = m["submitted_at"] - 1
    await redis.set(f"{prefix}:job:first", json.dumps(m))
    await redis.zadd(f"{prefix}:delayed:deferred", {"first": m["not_before"]})
    claim = await store.claim("deferred", "owner")
    assert claim.snapshot.state is JobState.RUNNING
    assert (await store.stats("deferred"))["delayed"] == 0


async def test_safe_child_retry_is_atomic_bounded_and_erased_with_account(v2_store):
    store, redis, prefix = v2_store
    await store.admit(job(), QueuePolicy(max_attempts=1))
    claim = await store.claim("deferred", "owner")
    failed = await store.finish("first", "owner", claim.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE))
    assert failed.retry_payload_available
    child = await store.retry_job(
        "first", expected_revision=failed.revision, actor="admin", operation_id="retry", privacy_epoch=7
    )
    repeat = await store.retry_job(
        "first", expected_revision=failed.revision, actor="admin", operation_id="retry", privacy_epoch=7
    )
    assert repeat.task_id == child.task_id
    assert (await store.read_job(child.task_id)).parent_task_id == "first"
    assert (await store.read_job("first")).state is JobState.FAILED
    with pytest.raises(QueueConflict):
        await store.retry_job(
            "first", expected_revision=failed.revision, actor="admin", operation_id="another", privacy_epoch=7
        )
    assert (await store.stats("deferred"))["payload_bytes"] == 2
    await store.erase_user(42)
    assert await store.read_job("first") is None
    assert await store.read_job(child.task_id) is None
    assert (await store.stats("deferred"))["payload_bytes"] == 0
    assert not [key async for key in redis.scan_iter(match=f"{prefix}:idem:*")]


async def test_lane_cas_pause_image_permit_and_public_pages(v2_store):
    store, _, _ = v2_store
    first = await store.read_lane("daily")
    paused = await store.set_lane_state(
        "daily",
        expected_revision=first.revision,
        admission_paused=True,
        claim_paused=False,
        actor="admin",
        operation_id="drain",
    )
    assert paused.admission_paused and not paused.claim_paused
    assert (await store.admit(job(lane="daily"), QueuePolicy(max_jobs=32))).reason_code == "admission_paused"
    with pytest.raises(QueueConflict):
        await store.set_lane_state(
            "daily",
            expected_revision=paused.revision,
            admission_paused=False,
            claim_paused=False,
            actor="admin",
            operation_id="drain",
        )
    await store.set_lane_state(
        "daily",
        expected_revision=paused.revision,
        admission_paused=False,
        claim_paused=False,
        actor="admin",
        operation_id="resume",
    )
    await store.admit(job("a", operation="a", lane="daily", image=True), QueuePolicy(max_jobs=32))
    await store.admit(job("b", operation="b", lane="daily", image=True), QueuePolicy(max_jobs=32))
    assert (await store.claim("daily", "image-one")).snapshot.task_id == "a"
    assert await store.claim("daily", "image-two") is None
    page = await store.list_jobs(lane="daily", limit=1)
    assert len(page.items) == 1 and page.next_cursor
    assert "user_id" not in page.items[0].as_dict() and "payload" not in page.items[0].as_dict()


async def test_atomic_conflicts_progress_and_late_owner_release(v2_store):
    from dataclasses import replace

    store, _, _ = v2_store
    original = replace(
        job(lane="daily"), conflict_keys=("croc:date:easy", "croc:date:hard"), public_summary={"date": "2026-10-28"}
    )
    first = await store.admit(original, QueuePolicy(max_jobs=32))
    duplicate = await store.admit(
        replace(original, task_id="second", idempotency_key="other"), QueuePolicy(max_jobs=32)
    )
    assert duplicate.state is AdmissionState.EXISTING and duplicate.task_id == first.task_id
    claim = await store.claim("daily", "owner")
    assert claim.payload is None
    progress = await store.update_progress(
        "first", "owner", claim.snapshot.revision, {"phase": "images", "completed": 1, "total": 2}
    )
    assert progress.result_summary["date"] == "2026-10-28"
    await store.finish("first", "owner", progress.revision, JobResult(JobOutcome.SUCCEEDED))
    repeat_old = await store.admit(
        replace(original, task_id="second", idempotency_key="other"), QueuePolicy(max_jobs=32)
    )
    assert repeat_old.state is AdmissionState.EXISTING and repeat_old.task_id == "first"
    next_job = await store.admit(
        replace(original, task_id="second", idempotency_key="new-other"), QueuePolicy(max_jobs=32)
    )
    assert next_job.state is AdmissionState.ACCEPTED
    await store.finish("first", "owner", progress.revision, JobResult(JobOutcome.SUCCEEDED))
    after_late = await store.admit(
        replace(original, task_id="third", idempotency_key="third"), QueuePolicy(max_jobs=32)
    )
    assert after_late.task_id == "second" and after_late.state is AdmissionState.EXISTING
    resolved = await store.read_job_by_operation(42, "other")
    assert resolved.task_id == "first" and resolved.state is JobState.SUCCEEDED
    active = await store.read_jobs_by_conflicts(42, "daily", original.conflict_keys)
    assert [snapshot.task_id for snapshot in active] == ["second"]


@pytest.mark.parametrize("kind,budget", [("tarot.prepare", 1800), ("croc.prepare_day", 600)])
async def test_daily_late_claim_has_independent_execution_budget(v2_store, kind, budget):
    from dataclasses import replace

    from app.queue_contracts import DAILY_QUEUE_POLICY

    store, redis, prefix = v2_store
    await store.admit(
        replace(job(lane="daily"), kind=kind, user_id=0, execution_timeout_seconds=budget), DAILY_QUEUE_POLICY
    )
    m = await store.private_record("first")
    original = m["submitted_at"]
    m.update(
        submitted_at=original - 1790,
        queue_deadline_at=original + 10,
        deadline_at=original + 10,
        payload_expires_at=original + 10,
    )
    await redis.set(f"{prefix}:job:first", json.dumps(m))
    await redis.zadd(f"{prefix}:deadline:all", {"first": original + 10})
    await redis.zadd(f"{prefix}:expiry:payload", {"first": original + 10})
    claim = await store.claim("daily", "owner")
    assert claim.snapshot.deadline_at >= claim.snapshot.started_at + budget
    assert claim.snapshot.deadline_at <= claim.snapshot.started_at + budget + 10.1
    assert await redis.ttl(f"{prefix}:payload:first") >= budget
