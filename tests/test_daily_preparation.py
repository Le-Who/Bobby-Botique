"""Readiness and legacy drain; new execution uses the shared queue contracts."""

import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import cache
from app.games import admin_jobs
from app.games import daily_preparation as service
from app.queue_contracts import AdmissionState, JobOutcome, QueueAdmission, QueueConflict
from app.repos import crocodile_daily as repo
from app.utils.json_compat import json

DAY = date(2027, 2, 5)


@pytest.fixture
def store(monkeypatch):
    rows = {}
    monkeypatch.setattr(cache, "redis_client", None)

    async def get(puzzle_date, *, difficulty):
        assert puzzle_date == DAY
        return rows.get(difficulty)

    monkeypatch.setattr(repo, "get_puzzle", get)
    return rows


@pytest.mark.asyncio
async def test_absent_day_is_read_only(store, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Readiness cannot create puzzles or invoke providers")

    monkeypatch.setattr(repo, "create_puzzle_if_missing", forbidden)
    monkeypatch.setattr("app.games.crocodile_daily.prepare_daily_puzzle", forbidden)
    result = await service.get_day_readiness(DAY)
    assert result["job"] is None and not result["fully_ready"]
    assert [p["difficulty"] for p in result["puzzles"]] == ["easy", "hard"]
    assert all(not p["exists"] for p in result["puzzles"])


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,expected", [("old-owner", "running"), (None, "failed")])
async def test_legacy_owner_is_observed_and_never_resumed(store, monkeypatch, owner, expected):
    metadata = {
        "id": "old-owner",
        "state": "running",
        "started_at": time.time(),
        "deadline": time.time() + 60,
        "private_extra": "must not escape",
    }

    async def get(key):
        return owner if key.endswith(":lease") else json.dumps(metadata)

    redis = SimpleNamespace(get=AsyncMock(side_effect=get), set=AsyncMock(), eval=AsyncMock())
    monkeypatch.setattr(cache, "redis_client", redis)
    result = await service._read_job(DAY)
    assert result["state"] == expected and "private_extra" not in result
    redis.set.assert_not_awaited()
    redis.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_active_or_unavailable_blocks_new_preparation(store, monkeypatch):
    monkeypatch.setattr(
        admin_jobs, "effective_policy", AsyncMock(return_value={"models": ["synthetic"], "revision": 1})
    )
    for state in ("running", "unknown"):
        monkeypatch.setattr(service, "_read_job", AsyncMock(return_value={"state": state, "legacy": True}))
        queue = SimpleNamespace(read_job_by_operation=AsyncMock(return_value=None), enqueue_job=AsyncMock())
        with pytest.raises(QueueConflict):
            await admin_jobs.enqueue(
                admin_jobs.DailyCommand("croc.prepare_day", DAY.isoformat()), "operation-123", queue=queue
            )
        queue.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatibility_service_uses_same_typed_queue(monkeypatch):
    enqueue = AsyncMock(return_value=QueueAdmission(AdmissionState.UNKNOWN, "job-id", "admission_unconfirmed"))
    monkeypatch.setattr(admin_jobs, "enqueue", enqueue)
    result = await service.start_day_preparation(DAY, operation_id="operation-123")
    assert result["state"] == "unknown" and result["id"] == "job-id"
    assert enqueue.await_args.args[1] == "operation-123"


@pytest.mark.asyncio
async def test_readiness_looks_up_active_conflicts_without_scanning_history(store, monkeypatch):
    from app.queue import task_queue

    current = SimpleNamespace(
        kind="croc.prepare_day",
        task_id="current-job",
        result_summary={"date": DAY.isoformat()},
        as_dict=lambda: {"state": "queued", "task_id": "current-job"},
    )
    lookup = AsyncMock(return_value=(current,))
    monkeypatch.setattr(task_queue, "read_jobs_by_conflicts", lookup)
    history = AsyncMock(side_effect=AssertionError("Readiness must not scan historical job pages"))
    monkeypatch.setattr(task_queue, "list_jobs", history)
    result = await service.get_day_readiness(DAY)
    assert result["job"]["id"] == "current-job"
    assert result["job"]["legacy"] is False
    lookup.assert_awaited_once_with(0, "daily", (f"croc:{DAY}:easy", f"croc:{DAY}:hard"))
    history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "revision"])
async def test_failure_does_not_skip_other_slot_and_uses_persisted_assets(store, monkeypatch, failure):
    calls = []

    async def prepare(day, *, difficulty, **kwargs):
        calls.append(difficulty)
        if difficulty == "easy":
            raise repo.DailyCrocConflict() if failure == "revision" else RuntimeError("private provider detail")
        store[difficulty] = repo.DailyPuzzle(
            day,
            "кот",
            "Животные",
            "ru",
            difficulty=difficulty,
            hints=["one", "two", "three"],
            image_prompt="cat",
            image_file_id="same-art",
        )
        return store[difficulty]

    monkeypatch.setattr("app.games.crocodile_daily.prepare_daily_puzzle", prepare)
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: object())
    monkeypatch.setattr(
        admin_jobs, "effective_policy", AsyncMock(return_value={"models": ["synthetic"], "revision": 1})
    )
    result = await admin_jobs.execute(command={"kind": "croc.prepare_day", "date": DAY.isoformat()})
    assert calls == ["easy", "hard"] and result.outcome is JobOutcome.PERMANENT_FAILURE
    assert result.reason_code == ("revision_conflict" if failure == "revision" else "partial_generation")
    assert result.result_summary["generated"] == result.result_summary["failed"] == 4
    assert "private provider" not in str(result)
