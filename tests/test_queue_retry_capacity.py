"""A safe retry retains its already-owned capacity instead of becoming a phantom job."""

from app.queue_contracts import JobOutcome, JobResult, JobState, QueuePolicy
from app.queue_store import JobEnvelope, MemoryQueueStore


async def test_safe_retry_owns_its_slot_and_does_not_exceed_capacity():
    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_jobs=1)
    await store.admit(JobEnvelope("job", "cleanup_metrics", "maintenance", 0, "{}", "first", effectful=False), policy)
    claim = await store.claim("maintenance", "owner")
    retry = await store.finish(
        "job", "owner", claim.snapshot.revision, JobResult(JobOutcome.RETRYABLE_FAILURE), delay_seconds=15
    )
    rejected = await store.admit(
        JobEnvelope("second", "cleanup_metrics", "maintenance", 0, "{}", "second", effectful=False), policy
    )
    assert retry.state is JobState.DELAYED
    assert rejected.reason_code == "job_capacity"
    assert (await store.stats("maintenance"))["nonterminal"] == 1
