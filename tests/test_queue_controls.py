"""Controls exercise the real queue lifecycle while keeping transports offline."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app import queue as queue_module
from app import web
from app.queue_contracts import JobOutcome, JobResult, QueuePolicy
from app.queue_runtime import TaskQueue
from app.queue_store import MemoryQueueStore

SECRET_PAYLOAD = "synthetic payload must never enter the admin response"
HEADERS = {"X-CSRF-Token": "queue-test-csrf"}


@pytest.fixture
async def queue_controls(monkeypatch):
    store = MemoryQueueStore(clock=lambda: 1000.0)
    queue = TaskQueue(store=store, clock=lambda: 1000.0)
    queue.register_handler("synthetic", AsyncMock(), lane="deferred", effectful=False)
    monkeypatch.setattr(queue_module, "task_queue", queue)
    monkeypatch.setattr(queue_module, "_get_redis", lambda: None)
    client = web.quart_app.test_client()
    async with client.session_transaction() as session:
        session.update(authenticated=True, controls_csrf="queue-test-csrf")
    yield client, queue, store


async def enqueue(queue, identity="first"):
    admission = await queue.enqueue_job(
        0,
        "synthetic",
        {"text": SECRET_PAYLOAD},
        idempotency_key=identity,
        lane="deferred",
        policy=QueuePolicy(max_attempts=1),
    )
    return await queue.read_job(admission.task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/controls/queues", "/api/controls/jobs", "/api/controls/jobs/synthetic"])
async def test_queue_controls_reads_require_existing_admin_auth(path):
    response = await web.quart_app.test_client().get(path)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_queue_controls_mutations_require_csrf_before_owner_call(queue_controls):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    for path in (
        f"/api/controls/jobs/{job.task_id}/cancel",
        f"/api/controls/jobs/{job.task_id}/retry",
        "/api/controls/queues/deferred/state",
    ):
        response = await client.post(path, json={"expected_revision": 1, "operation_id": "operation-0001"})
        assert response.status_code == 403
    assert (await store.read_job(job.task_id)).state.value == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["lane=unknown", "state=sent", "cursor=-1", "cursor=not-a-cursor", "limit=0", "limit=101", "limit=1.5"]
)
async def test_job_filters_are_bounded_before_store_reads(queue_controls, query):
    client, _, _ = queue_controls
    assert (await client.get(f"/api/controls/jobs?{query}")).status_code == 400


@pytest.mark.asyncio
async def test_job_pages_and_details_are_metadata_only_with_capabilities(queue_controls):
    client, queue, _ = queue_controls
    first = await enqueue(queue)
    second = await enqueue(queue, "second")
    response = await client.get("/api/controls/jobs?lane=deferred&state=queued&limit=1")
    assert response.status_code == 200
    page = await response.get_json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    assert page["scope"] == "process" and page["available"] is True
    next_page = await (await client.get(f"/api/controls/jobs?limit=1&cursor={page['next_cursor']}")).get_json()
    assert {page["items"][0]["task_id"], next_page["items"][0]["task_id"]} == {first.task_id, second.task_id}
    detail = await (await client.get(f"/api/controls/jobs/{first.task_id}")).get_json()
    assert detail["job"]["capabilities"] == {"cancel": True, "retry": False}
    assert "payload" not in detail["job"] and "user_id" not in detail["job"]
    assert SECRET_PAYLOAD not in repr(page) + repr(detail)


@pytest.mark.asyncio
async def test_cancel_double_click_is_idempotent_and_logs_only_metadata(queue_controls, monkeypatch):
    from app import web_controls

    client, queue, _ = queue_controls
    job = await enqueue(queue)
    events = []
    monkeypatch.setattr(web_controls, "emit", lambda name, **fields: events.append((name, fields)))
    body = {"expected_revision": job.revision, "operation_id": "cancel-operation-0001"}
    first = await client.post(f"/api/controls/jobs/{job.task_id}/cancel", json=body, headers=HEADERS)
    again = await client.post(f"/api/controls/jobs/{job.task_id}/cancel", json=body, headers=HEADERS)
    assert first.status_code == again.status_code == 200
    a, b = await first.get_json(), await again.get_json()
    assert a["job"]["state"] == b["job"]["state"] == "cancelled"
    assert a["job"]["revision"] == b["job"]["revision"] == 2
    assert any(name == "admin.queue_command" and fields["actor_ref"] == "configured_admin" for name, fields in events)
    assert SECRET_PAYLOAD not in repr(events)


@pytest.mark.asyncio
async def test_running_cancel_is_requested_until_owner_confirms(queue_controls):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/cancel",
        headers=HEADERS,
        json={"expected_revision": claim.snapshot.revision, "operation_id": "cancel-running-0001"},
    )
    assert response.status_code == 200
    assert (await response.get_json())["job"]["state"] == "cancel_requested"
    current = await store.read_job(job.task_id)
    await store.finish(job.task_id, "synthetic-owner", current.revision, JobResult(JobOutcome.SUCCEEDED))
    detail = await (await client.get(f"/api/controls/jobs/{job.task_id}")).get_json()
    assert detail["job"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_finish_race_does_not_rebase_client_revision(queue_controls):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    await store.finish(job.task_id, "synthetic-owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/cancel",
        headers=HEADERS,
        json={"expected_revision": claim.snapshot.revision, "operation_id": "cancel-race-0001"},
    )
    assert response.status_code == 409
    assert (await queue.read_job(job.task_id)).state.value == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [JobOutcome.SUCCEEDED, JobOutcome.UNKNOWN_DELIVERY])
async def test_sent_or_unknown_delivery_has_no_retry_and_server_rejects_it(queue_controls, outcome):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    finished = await store.finish(job.task_id, "synthetic-owner", claim.snapshot.revision, JobResult(outcome))
    detail = await (await client.get(f"/api/controls/jobs/{job.task_id}")).get_json()
    assert detail["job"]["capabilities"]["retry"] is False
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/retry",
        headers=HEADERS,
        json={"expected_revision": finished.revision, "operation_id": "forbidden-retry-0001"},
    )
    assert response.status_code == 409
    assert len((await store.list_jobs()).items) == 1


@pytest.mark.asyncio
async def test_failed_retry_creates_one_new_job_for_repeated_operation(queue_controls):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    failed = await store.finish(
        job.task_id,
        "synthetic-owner",
        claim.snapshot.revision,
        JobResult(JobOutcome.RETRYABLE_FAILURE, "synthetic_retryable"),
    )
    body = {"expected_revision": failed.revision, "operation_id": "retry-operation-0001"}
    first = await client.post(f"/api/controls/jobs/{job.task_id}/retry", json=body, headers=HEADERS)
    again = await client.post(f"/api/controls/jobs/{job.task_id}/retry", json=body, headers=HEADERS)
    assert first.status_code == again.status_code == 200
    a, b = await first.get_json(), await again.get_json()
    assert a["admission"]["task_id"] == b["admission"]["task_id"] != job.task_id
    assert len((await store.list_jobs()).items) == 2


@pytest.mark.asyncio
async def test_lane_pause_uses_client_revision_and_keeps_running_work(queue_controls):
    client, queue, store = queue_controls
    await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    body = {
        "expected_revision": 0,
        "operation_id": "pause-operation-0001",
        "admission_paused": True,
        "claim_paused": True,
    }
    response = await client.post("/api/controls/queues/deferred/state", json=body, headers=HEADERS)
    assert response.status_code == 200
    lane = (await response.get_json())["lane"]
    assert lane["revision"] == 1 and lane["admission_paused"] and lane["claim_paused"]
    assert (await store.read_job(claim.snapshot.task_id)).state.value == "running"
    conflict = await client.post(
        "/api/controls/queues/deferred/state", headers=HEADERS, json={**body, "operation_id": "pause-operation-0002"}
    )
    assert conflict.status_code == 409
    assert (await store.read_lane("deferred")).revision == 1


@pytest.mark.asyncio
async def test_lane_source_failure_keeps_other_lanes_and_read_only_gauges(queue_controls, monkeypatch):
    client, _, store = queue_controls
    original = store.stats

    async def stats(lane):
        if lane == "daily":
            raise ConnectionError("synthetic private source failure")
        return await original(lane)

    monkeypatch.setattr(store, "stats", stats)
    response = await client.get("/api/controls/queues")
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["queue"]["lanes"]["daily"]["available"] is False
    assert payload["queue"]["lanes"]["deferred"]["available"] is True
    assert payload["load"]["task_manager"]["available"] is True
    assert payload["capabilities"]["process_load_actions"] is False
    assert "private source failure" not in repr(payload)


@pytest.mark.asyncio
async def test_store_error_does_not_claim_a_command_was_confirmed(queue_controls, monkeypatch):
    client, queue, _ = queue_controls
    job = await enqueue(queue)
    monkeypatch.setattr(queue, "request_cancel", AsyncMock(side_effect=ConnectionError("synthetic private error")))
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/cancel",
        headers=HEADERS,
        json={"expected_revision": job.revision, "operation_id": "uncertain-command-0001"},
    )
    assert response.status_code == 503
    result = await response.get_json()
    assert result["confirmed"] is False and result["reason_code"] == "command_unconfirmed"
    assert "private error" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"operation_id": "valid-operation"},
        {"expected_revision": True, "operation_id": "valid-operation"},
        {"expected_revision": -1, "operation_id": "valid-operation"},
        {"expected_revision": 1},
        {"expected_revision": 1, "operation_id": "short"},
        {"expected_revision": 1, "operation_id": "x" * 129},
    ],
)
async def test_command_schema_rejects_missing_or_invalid_revision_identity(queue_controls, body):
    client, queue, store = queue_controls
    job = await enqueue(queue)
    response = await client.post(f"/api/controls/jobs/{job.task_id}/cancel", headers=HEADERS, json=body)
    assert response.status_code == 400
    assert (await store.read_job(job.task_id)).revision == 1


@pytest.mark.asyncio
async def test_pause_does_not_coerce_nonboolean_flags(queue_controls):
    client, _, store = queue_controls
    response = await client.post(
        "/api/controls/queues/deferred/state",
        headers=HEADERS,
        json={
            "expected_revision": 0,
            "operation_id": "pause-invalid-0001",
            "admission_paused": "false",
            "claim_paused": 0,
        },
    )
    assert response.status_code == 400
    assert (await store.read_lane("deferred")).revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/controls/jobs/synthetic/cancel", "/api/admin/daily/jobs", "/api/admin/daily/jobs/preview"]
)
async def test_chunked_controls_and_daily_jobs_are_bounded_before_eof(queue_controls, path):
    client, _, _ = queue_controls
    async with client.request(
        path, method="POST", headers={**HEADERS, "Content-Type": "application/json"}
    ) as connection:
        await connection.send(b" " * 300001)
        await asyncio.wait_for(connection.receive(), timeout=1)
        await connection.send_complete()
    assert (await connection.as_response()).status_code == 413


@pytest.mark.asyncio
async def test_unknown_retry_admission_is_not_a_confirmed_audit_event(queue_controls, monkeypatch):
    from app import web_controls
    from app.queue_contracts import AdmissionState, QueueAdmission

    client, queue, _ = queue_controls
    job = await enqueue(queue)
    events = []
    monkeypatch.setattr(
        queue, "retry_job", AsyncMock(return_value=QueueAdmission(AdmissionState.UNKNOWN, "uncertain-child"))
    )
    monkeypatch.setattr(web_controls, "emit", lambda name, **fields: events.append((name, fields)))
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/retry",
        headers=HEADERS,
        json={"expected_revision": job.revision, "operation_id": "uncertain-retry-0001"},
    )
    assert response.status_code == 503
    assert (await response.get_json())["confirmed"] is False
    assert events[-1][1]["reason_code"] == "command_unconfirmed"
    assert events[-1][1]["new_revision"] is None


@pytest.mark.asyncio
async def test_conflicting_admin_command_has_actor_revision_and_safe_reason_audit(queue_controls, monkeypatch):
    from app import web_controls

    client, queue, store = queue_controls
    job = await enqueue(queue)
    claim = await store.claim("deferred", "synthetic-owner")
    events = []
    monkeypatch.setattr(web_controls, "emit", lambda name, **fields: events.append((name, fields)))
    response = await client.post(
        f"/api/controls/jobs/{job.task_id}/cancel",
        headers=HEADERS,
        json={"expected_revision": job.revision, "operation_id": "audit-conflict-0001"},
    )
    assert response.status_code == 409
    assert events == [
        (
            "admin.queue_command",
            {
                "actor_ref": "configured_admin",
                "action": "cancel",
                "job_id": job.task_id,
                "previous_revision": 1,
                "new_revision": None,
                "reason_code": "revision_conflict",
            },
        )
    ]
    assert claim.snapshot.revision == 2


@pytest.mark.asyncio
async def test_policy_limits_cannot_be_changed_through_lane_state_command(queue_controls):
    client, _, store = queue_controls
    response = await client.post(
        "/api/controls/queues/deferred/state",
        headers=HEADERS,
        json={
            "expected_revision": 0,
            "operation_id": "policy-write-0001",
            "admission_paused": True,
            "claim_paused": False,
            "policy": {"max_jobs": 1},
        },
    )
    assert response.status_code == 400
    assert (await store.read_lane("deferred")).revision == 0


@pytest.mark.asyncio
async def test_queued_safe_metadata_is_visible_without_payload_lookup(queue_controls):
    client, queue, _ = queue_controls
    admission = await queue.enqueue_job(
        0,
        "synthetic",
        {"text": SECRET_PAYLOAD},
        lane="deferred",
        idempotency_key="summary",
        summary={
            "date": "2026-10-28",
            "effective_revision": 9,
            "effective_models": "planned-a,planned-b",
            "phase": "queued",
        },
    )
    detail = await (await client.get(f"/api/controls/jobs/{admission.task_id}")).get_json()
    assert detail["job"]["result_summary"] == {
        "date": "2026-10-28",
        "effective_revision": 9,
        "effective_models": "planned-a,planned-b",
        "phase": "queued",
    }
    assert "actual_model" not in detail["job"]["result_summary"]
    assert SECRET_PAYLOAD not in repr(detail)
