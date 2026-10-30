import pytest

from app.queue_contracts import AdmissionState, JobState, QueueConflict, QueuePolicy
from app.queue_store import JobEnvelope, MemoryQueueStore


async def test_lane_pause_and_drain_are_separate_cas_commands():
    store = MemoryQueueStore(clock=lambda: 1000)
    lane = await store.read_lane("daily")
    paused = await store.set_lane_state(
        "daily",
        expected_revision=lane.revision,
        admission_paused=True,
        claim_paused=False,
        actor="admin",
        operation_id="drain",
    )
    assert paused.admission_paused and not paused.claim_paused
    assert (
        await store.admit(JobEnvelope("one", "daily_prepare", "daily", 0, "{}", "one"), QueuePolicy(max_jobs=32))
    ).reason_code == "admission_paused"
    same = await store.set_lane_state(
        "daily",
        expected_revision=lane.revision,
        admission_paused=True,
        claim_paused=False,
        actor="admin",
        operation_id="drain",
    )
    assert same == paused
    with pytest.raises(QueueConflict):
        await store.set_lane_state(
            "daily",
            expected_revision=paused.revision,
            admission_paused=False,
            claim_paused=False,
            actor="admin",
            operation_id="drain",
        )


async def test_daily_image_limit_cannot_occupy_deferred_or_maintenance_slots():
    store = MemoryQueueStore(clock=lambda: 1000)
    for task_id, lane, image in [
        ("image-a", "daily", True),
        ("image-b", "daily", True),
        ("chat", "deferred", False),
        ("cleanup", "maintenance", False),
    ]:
        await store.admit(JobEnvelope(task_id, "synthetic", lane, 0, "{}", task_id, image_work=image), QueuePolicy())
    assert (await store.claim("daily", "one")).snapshot.task_id == "image-a"
    assert await store.claim("daily", "two") is None
    assert (await store.claim("deferred", "three")).snapshot.task_id == "chat"
    assert (await store.claim("maintenance", "four")).snapshot.task_id == "cleanup"


async def test_public_pagination_omits_private_payload_and_filters_state():
    store = MemoryQueueStore(clock=lambda: 1000)
    for index in range(5):
        await store.admit(
            JobEnvelope(str(index), "synthetic", "daily", 123, '{"private":"synthetic"}', str(index)), QueuePolicy()
        )
    first = await store.list_jobs(lane="daily", state=JobState.QUEUED, limit=2)
    second = await store.list_jobs(lane="daily", state=JobState.QUEUED, limit=2, cursor=first.next_cursor)
    assert [j.task_id for j in first.items + second.items] == ["0", "1", "2", "3"]
    assert "private" not in str(first) and "user_id" not in first.items[0].as_dict()


async def test_conflict_keys_coalesce_both_difficulties_and_release_only_current_owner():
    from app.queue_contracts import JobOutcome, JobResult

    store = MemoryQueueStore(clock=lambda: 1000)
    keys = ("croc:2026-10-28:hard", "croc:2026-10-28:easy")
    first = await store.admit(
        JobEnvelope("first", "daily_prepare", "daily", 0, "{}", "one", conflict_keys=keys), QueuePolicy(max_jobs=32)
    )
    same = await store.admit(
        JobEnvelope("second", "daily_prepare", "daily", 0, "{}", "two", conflict_keys=(keys[0],)),
        QueuePolicy(max_jobs=32),
    )
    assert same.state is AdmissionState.EXISTING and same.task_id == first.task_id
    assert (await store.stats("daily"))["nonterminal"] == 1
    claim = await store.claim("daily", "owner-one")
    await store.finish("first", "owner-one", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    next_job = await store.admit(
        JobEnvelope("second", "daily_prepare", "daily", 0, "{}", "new-two", conflict_keys=keys),
        QueuePolicy(max_jobs=32),
    )
    assert next_job.state is AdmissionState.ACCEPTED
    await store.finish("first", "owner-one", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    third = await store.admit(
        JobEnvelope("third", "daily_prepare", "daily", 0, "{}", "three", conflict_keys=keys), QueuePolicy(max_jobs=32)
    )
    assert third.state is AdmissionState.EXISTING and third.task_id == "second"


async def test_daily_progress_is_owner_fenced_public_and_cancellation_stops_updates():
    from app.queue_contracts import QueueConflict

    store = MemoryQueueStore(clock=lambda: 1000)
    await store.admit(JobEnvelope("daily", "daily_prepare", "daily", 0, "{}", "one"), QueuePolicy(max_jobs=32))
    claim = await store.claim("daily", "owner")
    progress = await store.update_progress(
        "daily", "owner", claim.snapshot.revision, {"phase": "images", "completed": 1, "total": 3}
    )
    assert (await store.read_job("daily")).result_summary == {"phase": "images", "completed": 1, "total": 3}
    with pytest.raises(QueueConflict):
        await store.update_progress("daily", "old-owner", progress.revision, {"completed": 2})
    cancel = await store.request_cancel(
        "daily", expected_revision=progress.revision, actor="admin", operation_id="cancel"
    )
    with pytest.raises(QueueConflict):
        await store.update_progress("daily", "owner", cancel.revision, {"completed": 2})
    assert "private" not in str(progress.as_dict())


async def test_coalesced_operation_keeps_same_id_after_original_finishes():
    from app.queue_contracts import JobOutcome, JobResult

    store = MemoryQueueStore(clock=lambda: 1000)
    original = JobEnvelope("first", "daily_prepare", "daily", 0, "{}", "first", conflict_keys=("day",))
    coalesced = JobEnvelope("second", "daily_prepare", "daily", 0, "{}", "second", conflict_keys=("day",))
    await store.admit(original, QueuePolicy(max_jobs=32))
    assert (await store.admit(coalesced, QueuePolicy(max_jobs=32))).task_id == "first"
    claim = await store.claim("daily", "owner")
    await store.finish("first", "owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    repeated = await store.admit(coalesced, QueuePolicy(max_jobs=32))
    assert repeated.task_id == "first" and repeated.state is AdmissionState.EXISTING
    resolved = await store.read_job_by_operation(0, "second")
    assert resolved.task_id == "first" and resolved.state is JobState.SUCCEEDED
    assert len(store.jobs) == 1


async def test_conflict_alias_retention_is_bounded_and_account_erasure_removes_every_alias():
    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_jobs=32)
    for index in range(256):
        accepted = await store.admit(
            JobEnvelope(
                str(index), "daily_prepare", "daily", 42, "{}", str(index), privacy_epoch=7, conflict_keys=("day",)
            ),
            policy,
        )
        assert accepted.state in {AdmissionState.ACCEPTED, AdmissionState.EXISTING}
        assert accepted.task_id == "0"
    rejected = await store.admit(
        JobEnvelope(
            "overflow", "daily_prepare", "daily", 42, "{}", "overflow", privacy_epoch=7, conflict_keys=("day",)
        ),
        policy,
    )
    assert rejected.reason_code == "idempotency_capacity"
    await store.erase_user(42)
    assert not store.identities and not store.conflicts and not store.payloads


async def test_active_conflict_lookup_ignores_more_than_one_page_of_terminal_history():
    from app.queue_contracts import JobOutcome, JobResult

    store = MemoryQueueStore(clock=lambda: 1000)
    policy = QueuePolicy(max_jobs=32)
    for index in range(300):
        await store.admit(JobEnvelope(f"old-{index}", "daily_prepare", "daily", 0, "{}", f"old-{index}"), policy)
        claim = await store.claim("daily", f"owner-{index}")
        await store.finish(
            claim.snapshot.task_id, claim.owner_token, claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED)
        )
    keys = ("croc:2026-10-28:easy", "croc:2026-10-28:hard")
    await store.admit(JobEnvelope("active", "daily_prepare", "daily", 0, "{}", "active", conflict_keys=keys), policy)
    snapshots = await store.read_jobs_by_conflicts(0, "daily", keys)
    assert [snapshot.task_id for snapshot in snapshots] == ["active"]
    assert snapshots[0].state is JobState.QUEUED
