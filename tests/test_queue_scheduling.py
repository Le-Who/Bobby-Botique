import pytest

from app.queue_contracts import AdmissionState, JobOutcome, JobResult, JobState, QueuePolicy
from app.queue_scheduling import next_attempt_at
from app.queue_store import JobEnvelope, MemoryQueueStore


@pytest.mark.parametrize(
    "attempt, jitter, retry_after, expected",
    [(1, 0, None, 115), (2, 1, None, 136), (3, 0, None, 160), (9, 1, None, 172), (1, 0, 90, 190)],
)
def test_exponential_backoff_and_retry_after(attempt, jitter, retry_after, expected):
    assert next_attempt_at(attempt, 100, retry_after, jitter) == expected


async def test_delayed_job_does_not_claim_worker_and_expiry_releases_capacity():
    now = [1000]
    store = MemoryQueueStore(clock=lambda: now[0])
    policy = QueuePolicy(max_jobs=1, max_payload_bytes=4, max_total_payload_bytes=4, max_age_seconds=15)
    first = await store.admit(
        JobEnvelope("one", "deferred_ai_response", "deferred", 1, "яя", "one", delay_seconds=10), policy
    )
    assert first.state is AdmissionState.ACCEPTED
    assert await store.claim("deferred", "owner") is None
    assert (
        await store.admit(JobEnvelope("two", "deferred_ai_response", "deferred", 1, "x", "two"), policy)
    ).reason_code == "job_capacity"
    now[0] += 10
    assert (await store.claim("deferred", "owner")).snapshot.state is JobState.RUNNING
    now[0] += 6
    await store.sweep()
    assert (await store.stats("deferred"))["payload_bytes"] == 0
    assert (await store.stats("deferred"))["nonterminal"] == 0


async def test_retained_retry_payload_counts_toward_total_until_expiry():
    now = [1000]
    store = MemoryQueueStore(clock=lambda: now[0])
    policy = QueuePolicy(max_jobs=2, max_total_payload_bytes=4)
    await store.admit(JobEnvelope("one", "deferred_ai_response", "deferred", 1, "яя", "one"), policy)
    claimed = await store.claim("deferred", "owner")
    await store.finish(
        "one", "owner", claimed.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE), delay_seconds=1000
    )
    rejected = await store.admit(JobEnvelope("two", "deferred_ai_response", "deferred", 1, "x", "two"), policy)
    assert rejected.reason_code == "byte_capacity"
    now[0] += 901
    accepted = await store.admit(JobEnvelope("two", "deferred_ai_response", "deferred", 1, "x", "two"), policy)
    assert accepted.state is AdmissionState.ACCEPTED


async def test_lane_retrying_counts_delayed_safe_retry_only():
    store = MemoryQueueStore(clock=lambda: 1000)
    await store.admit(
        JobEnvelope("one", "cleanup_metrics", "maintenance", 0, "{}", "one", effectful=False), QueuePolicy()
    )
    await store.admit(
        JobEnvelope("two", "cleanup_metrics", "maintenance", 0, "{}", "two", effectful=False), QueuePolicy()
    )
    claim = await store.claim("maintenance", "owner")
    await store.finish(
        "one", "owner", claim.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE), delay_seconds=15
    )
    assert (await store.stats("maintenance"))["retrying"] == 1


async def test_single_payload_limit_counts_utf8_and_rejects_limit_plus_one():
    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_payload_bytes=4)
    assert (
        await store.admit(JobEnvelope("one", "deferred_ai_response", "deferred", 1, "яя", "one"), policy)
    ).state is AdmissionState.ACCEPTED
    assert (
        await store.admit(JobEnvelope("two", "deferred_ai_response", "deferred", 1, "яяx", "two"), policy)
    ).reason_code == "payload_too_large"


@pytest.mark.parametrize("kind,budget", [("tarot.prepare", 1800), ("croc.prepare_day", 600)])
async def test_daily_claim_near_queue_age_limit_gets_full_execution_budget(kind, budget):
    from app.queue_contracts import DAILY_QUEUE_POLICY

    clock = [1000.0]
    store = MemoryQueueStore(clock=lambda: clock[0])
    await store.admit(
        JobEnvelope("daily", kind, "daily", 0, "{}", "daily", execution_timeout_seconds=budget), DAILY_QUEUE_POLICY
    )
    clock[0] += 1790
    claim = await store.claim("daily", "owner")
    assert claim is not None
    assert claim.snapshot.deadline_at >= clock[0] + budget
    current = claim.snapshot
    for _ in range(budget // 50):
        clock[0] += 50
        current = await store.heartbeat("daily", "owner", current.revision)
    assert current.state is JobState.RUNNING
    clock[0] += 11
    await store.sweep()
    assert (await store.read_job("daily")).state is JobState.UNKNOWN_DELIVERY
