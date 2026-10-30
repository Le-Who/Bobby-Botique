"""Daily job admission and result contracts use only synthetic services."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.queue_contracts import AdmissionState, JobOutcome, QueueAdmission

DAY = date(2026, 10, 28)


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"kind": "arbitrary", "date": "2026-10-28"},
        {"kind": "tarot.prepare", "date": "bad"},
        {"kind": "tarot.prepare", "date": "2026-10-28", "mode": "all"},
        {"kind": "croc.reset_word", "date": "2026-10-28", "difficulty": "easy", "expected_revision": True},
        {"kind": "croc.prepare_day", "date": "2026-10-28", "difficulty": "easy"},
        {"kind": "croc.prepare_day", "date": "2026-10-28", "mode": "replace"},
        {"kind": "tarot.prepare", "date": "2026-10-28", "part": "main"},
    ],
)
def test_invalid_commands_never_enter_queue(body):
    from app.games.admin_jobs import DailyCommand

    with pytest.raises(ValueError):
        DailyCommand.parse(body)


def test_conflict_keys_are_shared_between_whole_day_and_each_difficulty():
    from app.games.admin_jobs import DailyCommand

    whole = DailyCommand.parse({"kind": "croc.prepare_day", "date": DAY.isoformat()})
    image = DailyCommand.parse(
        {"kind": "croc.regenerate_image", "date": DAY.isoformat(), "difficulty": "hard", "expected_revision": 2}
    )
    assert set(image.conflict_keys) < set(whole.conflict_keys)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["fta-gpt-image-2", "vhr/gpt_image_2"])
async def test_explicit_fta_policy_does_not_depend_on_pollinations_catalog(monkeypatch, model):
    from app.games import admin_jobs as jobs

    monkeypatch.setattr(
        "app.runtime_settings.lifecycle.operation_snapshot", AsyncMock(return_value=SimpleNamespace(revision=7))
    )
    catalog = AsyncMock(side_effect=RuntimeError("synthetic catalog outage"))
    monkeypatch.setattr("app.providers.pollinations.fetch_models", catalog)
    command = jobs.DailyCommand.parse(
        {
            "kind": "croc.regenerate_image",
            "date": DAY.isoformat(),
            "difficulty": "hard",
            "expected_revision": 2,
            "model": model,
        }
    )
    policy = await jobs.effective_policy(command)
    assert policy["models"] == [model]
    assert policy["explicit"] is True
    catalog.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_preserves_operation_id_conflicts_and_typed_unknown(monkeypatch):
    from app.games import admin_jobs as jobs

    command = jobs.DailyCommand.parse({"kind": "tarot.prepare", "date": DAY.isoformat()})
    monkeypatch.setattr(jobs, "preview", AsyncMock(return_value={"blocked_reason": None}))
    for state in (AdmissionState.EXISTING, AdmissionState.UNKNOWN, AdmissionState.REJECTED):
        queue = SimpleNamespace(
            read_job_by_operation=AsyncMock(return_value=None),
            enqueue_job=AsyncMock(return_value=QueueAdmission(state, "known-job")),
        )
        result = await jobs.enqueue(command, "operation-123", queue=queue)
        assert result.state is state
        assert queue.enqueue_job.await_args.kwargs["idempotency_key"] == "daily:operation-123"
        assert queue.enqueue_job.await_args.kwargs["conflict_keys"] == command.conflict_keys
        assert queue.enqueue_job.await_args.kwargs["policy"].max_age_seconds == 1800


@pytest.mark.asyncio
async def test_partial_tarot_is_terminal_failed_with_real_counters(monkeypatch):
    from app import tarot_daily
    from app.games import admin_jobs as jobs

    monkeypatch.setattr(jobs, "checkpoint", AsyncMock())
    monkeypatch.setattr(jobs, "publish_progress", AsyncMock())
    monkeypatch.setattr(jobs, "effective_policy", AsyncMock(return_value={"models": ["synthetic"], "revision": 7}))
    monkeypatch.setattr(
        tarot_daily,
        "prepare_daily_readings",
        AsyncMock(return_value=tarot_daily.TarotDailyPreparationResult(DAY, generated=3, failed=2, remaining=151)),
    )
    result = await jobs.execute(command={"kind": "tarot.prepare", "date": DAY.isoformat(), "mode": "missing"})
    assert result.outcome is JobOutcome.PERMANENT_FAILURE
    assert result.reason_code == "partial_generation"
    assert result.result_summary["generated"] == 3
    assert result.result_summary["remaining"] == 151
    assert result.result_summary["effective_revision"] == 7


@pytest.mark.asyncio
async def test_readiness_calendar_is_bounded_and_read_only(monkeypatch):
    from app import tarot_daily
    from app.games import admin_jobs as jobs
    from app.games import daily_preparation
    from app.repos import daily_2048, daily_trivia

    monkeypatch.setattr(daily_preparation, "get_day_readiness", AsyncMock(return_value={"puzzles": [], "job": None}))
    monkeypatch.setattr(daily_2048, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(daily_trivia, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot_daily, "count_prepared_daily_readings", AsyncMock(return_value=0))
    result = await jobs.readiness_calendar(DAY, 8)
    assert len(result["days"]) == 8
    assert result["timezone"] == "Europe/Kyiv"
    assert daily_2048.get_puzzle.await_count == 8
    with pytest.raises(ValueError):
        await jobs.readiness_calendar(DAY, 9)


@pytest.mark.asyncio
async def test_execution_timeout_preserves_confirmed_progress(monkeypatch):
    import asyncio

    from app.games import admin_jobs as jobs

    monkeypatch.setitem(jobs.EXECUTION_TIMEOUT_SECONDS, "tarot.prepare", 0.01)
    monkeypatch.setattr(jobs, "effective_policy", AsyncMock(return_value={"models": ["synthetic"], "revision": 7}))

    async def prepare(**kwargs):
        await kwargs["progress"](generated=3, skipped=1, failed=0, remaining=152)
        await asyncio.Event().wait()

    monkeypatch.setattr("app.tarot_daily.prepare_daily_readings", prepare)
    result = await jobs.execute(command={"kind": "tarot.prepare", "date": DAY.isoformat()})
    assert result.outcome is JobOutcome.PERMANENT_FAILURE
    assert result.reason_code == "partial_generation"
    assert (result.result_summary["generated"], result.result_summary["remaining"]) == (3, 152)


@pytest.mark.asyncio
async def test_daily_http_auth_csrf_and_typed_admission(monkeypatch):
    from app.games import admin_jobs as jobs
    from app.web import quart_app

    client = quart_app.test_client()
    payload = {"kind": "tarot.prepare", "date": DAY.isoformat(), "operation_id": "operation-123"}
    assert (await client.post("/api/admin/daily/jobs", json=payload)).status_code == 401
    async with client.session_transaction() as session:
        session["authenticated"] = True
        session["controls_csrf"] = "synthetic-csrf"
    assert (await client.post("/api/admin/daily/jobs", json=payload)).status_code == 403
    for state, status in (
        (AdmissionState.ACCEPTED, 202),
        (AdmissionState.EXISTING, 202),
        (AdmissionState.REJECTED, 429),
        (AdmissionState.UNKNOWN, 503),
    ):
        enqueue = AsyncMock(return_value=QueueAdmission(state, "known-job"))
        monkeypatch.setattr(jobs, "enqueue", enqueue)
        response = await client.post("/api/admin/daily/jobs", json=payload, headers={"X-CSRF-Token": "synthetic-csrf"})
        assert response.status_code == status
        assert (await response.get_json())["status_url"].endswith("/known-job")


@pytest.mark.asyncio
async def test_real_local_admission_conflict_capacity_and_cancel_before_start(monkeypatch):
    from app.games import admin_jobs as jobs
    from app.queue_contracts import JobState
    from app.queue_runtime import TaskQueue
    from app.queue_store import MemoryQueueStore

    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    jobs.register_daily_jobs(queue)
    monkeypatch.setattr(jobs, "preview", AsyncMock(return_value={"blocked_reason": None}))
    command = jobs.DailyCommand.parse({"kind": "tarot.prepare", "date": DAY.isoformat()})
    first = await jobs.enqueue(command, "operation-1", queue=queue)
    again = await jobs.enqueue(command, "operation-1", queue=queue)
    other = await jobs.enqueue(command, "operation-2", queue=queue)
    assert first.state is AdmissionState.ACCEPTED
    assert again.task_id == other.task_id == first.task_id
    assert again.state is other.state is AdmissionState.EXISTING
    current = await queue.read_job(first.task_id)
    await queue.request_cancel(
        first.task_id, expected_revision=current.revision, actor="admin", operation_id="cancel-123"
    )
    assert (await queue.read_job(first.task_id)).state is JobState.CANCELLED
    from datetime import timedelta

    for offset in range(32):
        command = jobs.DailyCommand.parse({"kind": "tarot.prepare", "date": (DAY + timedelta(days=offset)).isoformat()})
        admission = await jobs.enqueue(command, f"new-operation-{offset}", queue=queue)
        assert admission.state is AdmissionState.ACCEPTED
    command = jobs.DailyCommand.parse({"kind": "tarot.prepare", "date": "2027-12-01"})
    assert (await jobs.enqueue(command, "overflow-operation", queue=queue)).state is AdmissionState.REJECTED


@pytest.mark.asyncio
async def test_operation_lookup_recovers_coalesced_job_without_reenqueuing(monkeypatch):
    from app import web_daily_jobs
    from app.games import admin_jobs as jobs
    from app.queue_runtime import TaskQueue
    from app.queue_store import MemoryQueueStore
    from app.web import quart_app

    queue = TaskQueue(store=MemoryQueueStore())
    jobs.register_daily_jobs(queue)
    preview = AsyncMock(return_value={"blocked_reason": None})
    monkeypatch.setattr(jobs, "preview", preview)
    monkeypatch.setattr(web_daily_jobs, "task_queue", queue)
    command = jobs.DailyCommand.parse({"kind": "tarot.prepare", "date": DAY.isoformat()})
    first = await jobs.enqueue(command, "original-operation", queue=queue)
    coalesced = await jobs.enqueue(command, "lost-reply-operation", queue=queue)
    assert coalesced.state is AdmissionState.EXISTING
    assert coalesced.task_id == first.task_id
    preview.reset_mock()
    preview.side_effect = AssertionError("A known operation must not run preflight again")
    assert (await jobs.enqueue(command, "lost-reply-operation", queue=queue)).task_id == first.task_id

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/api/admin/daily/jobs/by-operation/lost-reply-operation")
    assert response.status_code == 200
    assert (await response.get_json())["task_id"] == first.task_id
    batch = await client.get("/api/admin/daily/jobs?operations=original-operation,lost-reply-operation")
    assert batch.status_code == 200
    assert [item["job"]["task_id"] for item in (await batch.get_json())["items"]] == [first.task_id] * 2
    assert len((await queue.list_jobs(lane="daily")).items) == 1
    preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_status_bounds_work_and_preserves_partial_source_failure(monkeypatch):
    from app import web_daily_jobs
    from app.queue_contracts import QueueUnavailable
    from app.web import quart_app

    read = AsyncMock(side_effect=[None, QueueUnavailable("status_unavailable")])
    monkeypatch.setattr(web_daily_jobs, "task_queue", SimpleNamespace(read_job_by_operation=read))
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/api/admin/daily/jobs?operations=not-found-operation,unavailable-operation")
    assert response.status_code == 200
    assert (await response.get_json())["items"] == [
        {"operation_id": "not-found-operation", "available": True, "job": None},
        {"operation_id": "unavailable-operation", "available": False, "job": None},
    ]
    read.reset_mock()
    too_many = ",".join(f"operation-{index}" for index in range(33))
    assert (await client.get(f"/api/admin/daily/jobs?operations={too_many}")).status_code == 400
    assert (await client.get("/api/admin/daily/jobs?operations=short")).status_code == 400
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["trivia.regenerate", "trivia.validate_save"])
@pytest.mark.parametrize("part", ["main", "super"])
async def test_partial_trivia_rejects_incomplete_sibling_before_provider_work(monkeypatch, kind, part):
    from app.games import admin_jobs as jobs

    puzzle = SimpleNamespace(revision=3, questions=[{}] * 4, super_questions=[{}] * 2)
    monkeypatch.setattr("app.repos.daily_trivia.get_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(jobs, "effective_policy", AsyncMock(return_value={"models": [], "revision": 1}))
    generate = AsyncMock(side_effect=AssertionError("Must not silently generate another section"))
    validate = AsyncMock(side_effect=AssertionError("Incomplete sibling must fail before paid validation"))
    monkeypatch.setattr("app.games.daily_trivia.prepare_daily_puzzle", generate)
    monkeypatch.setattr("app.games.daily_trivia_authoring.publish_authored_day", validate)
    body = {"kind": kind, "date": DAY.isoformat(), "expected_revision": 3, "part": part}
    if kind == "trivia.validate_save":
        body["questions"] = [{}] * (5 if part == "main" else 3)
    preview = await jobs.preview(jobs.DailyCommand.parse(body))
    assert preview["blocked_reason"] == "incomplete_sibling"
    result = await jobs.execute(command=body)
    assert result.outcome is JobOutcome.PERMANENT_FAILURE
    assert result.reason_code == "incomplete_sibling"
    generate.assert_not_awaited()
    validate.assert_not_awaited()
