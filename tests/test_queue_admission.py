"""Admission identity and capacity survive an uncertain Redis reply."""

import pytest

from app.queue_contracts import AdmissionState, QueuePolicy
from app.queue_store import JobEnvelope, MemoryQueueStore, RedisQueueStore


def envelope(task_id="job-a", *, key="operation-a", payload="{}", user_id=1):
    return JobEnvelope(task_id, "deferred_ai_response", "deferred", user_id, payload, key, privacy_epoch=7)


async def test_local_admission_is_idempotent_and_capacity_is_atomic():
    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_jobs=1)
    first = await store.admit(envelope(), policy)
    duplicate = await store.admit(envelope("job-b"), policy)
    rejected = await store.admit(envelope("job-c", key="operation-c"), policy)
    assert first.state is AdmissionState.ACCEPTED
    assert duplicate.state is AdmissionState.EXISTING
    assert duplicate.task_id == first.task_id == "job-a"
    assert rejected.state is AdmissionState.REJECTED
    assert rejected.reason_code == "job_capacity"
    assert (await store.stats("deferred"))["nonterminal"] == 1


async def test_observing_lane_does_not_discard_stricter_admission_policy():
    store = MemoryQueueStore(clock=lambda: 1000)
    await store.read_lane("deferred")
    policy = QueuePolicy(max_jobs=1)
    await store.admit(envelope(), policy)
    second = await store.admit(envelope("second", key="second"), policy)
    assert second.state is AdmissionState.REJECTED and second.reason_code == "job_capacity"


@pytest.mark.parametrize("reconcile_available", [False, True])
async def test_redis_commit_then_disconnect_never_creates_local_duplicate(reconcile_available):
    class LostReplyRedis:
        def __init__(self):
            self.calls = []

        async def eval(self, script, count, *args):
            self.calls.append(args)
            if len(self.calls) == 1:
                raise ConnectionError("synthetic reply loss after mutation")
            if not reconcile_available:
                raise ConnectionError("synthetic outage")
            return '{"state":"existing","task_id":"job-a","storage_mode":"redis"}'

    redis = LostReplyRedis()
    admission = await RedisQueueStore(redis).admit(envelope(), QueuePolicy())
    assert admission.task_id == "job-a"
    assert admission.storage_mode == "redis"
    assert admission.state is (AdmissionState.EXISTING if reconcile_available else AdmissionState.UNKNOWN)
    assert len(redis.calls) == 2
