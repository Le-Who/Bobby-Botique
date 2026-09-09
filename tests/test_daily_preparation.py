import asyncio
import time
from datetime import date

import pytest

from app import cache
from app.games import daily_preparation as service
from app.repos import crocodile_daily as repo
from app.utils.json_compat import json

DAY = date(2027, 2, 5)


@pytest.fixture
def store(monkeypatch):
    rows = {}
    monkeypatch.setattr(cache, "redis_client", None)
    monkeypatch.setattr(service, "_jobs", {})
    monkeypatch.setattr(service, "_tasks", {})

    async def get(puzzle_date, *, difficulty):
        assert puzzle_date == DAY
        return rows.get(difficulty)

    monkeypatch.setattr(repo, "get_puzzle", get)
    return rows


@pytest.mark.asyncio
async def test_absent_day_is_read_only(store, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Readiness must not create puzzles or call generation")

    monkeypatch.setattr(repo, "create_puzzle_if_missing", forbidden)
    monkeypatch.setattr(service, "prepare_daily_puzzle", forbidden)
    result = await service.get_day_readiness(DAY)
    assert result["date"] == "2027-02-05"
    assert result["fully_ready"] is False
    assert result["job"] is None
    assert [p["difficulty"] for p in result["puzzles"]] == ["easy", "hard"]
    assert all(not p["exists"] and not p["fully_ready"] for p in result["puzzles"])
    assert store == {}


@pytest.mark.asyncio
async def test_day_prepares_both_without_forcing_and_deduplicates(store, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    bot = object()

    async def prepare(puzzle_date, given_bot, *, difficulty, include_image, force_image, bypass_image_quota):
        assert given_bot is bot and include_image and not force_image
        assert bypass_image_quota
        entered.set()
        await release.wait()
        store[difficulty] = repo.DailyPuzzle(
            puzzle_date,
            "кот",
            "животные",
            "ru",
            difficulty=difficulty,
            hints=["один", "два", "три", "четыре", "пять", "шесть"],
            image_prompt="cat",
            image_file_id="photo",
        )
        return store[difficulty]

    monkeypatch.setattr(service, "prepare_daily_puzzle", prepare)
    first = await service.start_day_preparation(DAY, bot)
    await entered.wait()
    second = await service.start_day_preparation(DAY, bot)
    assert first["id"] == second["id"]
    release.set()
    await service._tasks[DAY]
    result = await service.get_day_readiness(DAY)
    assert set(store) == {"easy", "hard"}
    assert result["fully_ready"] is True
    assert result["job"]["state"] == "completed"


@pytest.mark.asyncio
async def test_failure_does_not_skip_other_slot_or_claim_return_value_ready(store, monkeypatch):
    async def prepare(puzzle_date, bot, *, difficulty, **kwargs):
        if difficulty == "easy":
            raise RuntimeError("secret provider payload")
        store[difficulty] = repo.DailyPuzzle(puzzle_date, "кот", "животные", "ru", difficulty=difficulty)
        return repo.DailyPuzzle(puzzle_date, "кот", "животные", "ru", hints=["a"], image_prompt="a", image_file_id="a")

    monkeypatch.setattr(service, "prepare_daily_puzzle", prepare)
    await service.start_day_preparation(DAY, object())
    await service._tasks[DAY]
    result = await service.get_day_readiness(DAY)
    assert result["fully_ready"] is False
    assert result["job"]["state"] == "partial"
    assert "secret" not in str(result)
    assert set(store) == {"hard"}


@pytest.mark.asyncio
async def test_cancelled_job_is_failed_and_retryable(store, monkeypatch):
    entered = asyncio.Event()

    async def prepare(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "prepare_daily_puzzle", prepare)
    first = await service.start_day_preparation(DAY, object())
    await entered.wait()
    task = service._tasks[DAY]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service.get_day_readiness(DAY))["job"]["state"] == "failed"
    second = await service.start_day_preparation(DAY, object())
    assert first["id"] != second["id"]
    await asyncio.sleep(0)
    service._tasks[DAY].cancel()
    with pytest.raises(asyncio.CancelledError):
        await service._tasks[DAY]


class RedisStore:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, count, *args):
        lease = args[0]
        token = args[count]
        if self.values.get(lease) != token:
            return 0
        if count == 1:
            del self.values[lease]
        else:
            self.values[args[1]] = args[count + 1]
        return 1


@pytest.mark.asyncio
async def test_other_worker_lease_deduplicates_without_spawning(store, monkeypatch):
    redis = RedisStore()
    monkeypatch.setattr(cache, "redis_client", redis)
    lease, status = service._keys(DAY)
    redis.values[lease] = "other-worker"
    redis.values[status] = json.dumps(
        {"id": "other-worker", "state": "running", "started_at": time.time(), "deadline": time.time() + 100}
    )
    result = await service.start_day_preparation(DAY, object())
    assert result["id"] == "other-worker"
    assert result["state"] == "running"
    assert service._tasks == {}


@pytest.mark.asyncio
async def test_busy_lease_does_not_report_old_completed_job(store, monkeypatch):
    redis = RedisStore()
    monkeypatch.setattr(cache, "redis_client", redis)
    lease, status = service._keys(DAY)
    redis.values[lease] = "new-worker"
    redis.values[status] = json.dumps(
        {"id": "previous-worker", "state": "completed", "started_at": time.time(), "deadline": time.time() + 100}
    )
    result = await service.start_day_preparation(DAY, object())
    assert result["state"] == "running"
    assert result["id"] == "new-worker"


@pytest.mark.asyncio
async def test_expired_status_is_not_stuck_running(store, monkeypatch):
    redis = RedisStore()
    monkeypatch.setattr(cache, "redis_client", redis)
    redis.values[service._keys(DAY)[1]] = json.dumps(
        {"id": "dead-worker", "state": "running", "started_at": time.time() - 1000, "deadline": time.time() - 1}
    )
    result = await service.get_day_readiness(DAY)
    assert result["job"]["state"] == "failed"
    assert redis.values[service._keys(DAY)[1]]  # GET never rewrites persisted status.


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_unstarted_job_can_retry_without_waiting_for_redis_lease(store, monkeypatch, rejected):
    redis = RedisStore()
    monkeypatch.setattr(cache, "redis_client", redis)
    real_submit = service.submit_task

    def submit(coro):
        if rejected:
            coro.close()
            return asyncio.create_task(asyncio.sleep(0))
        return asyncio.create_task(coro)

    monkeypatch.setattr(service, "submit_task", submit)
    first = await service.start_day_preparation(DAY, object())
    task = service._tasks[DAY]
    if rejected:
        await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await service.get_day_readiness(DAY))["job"]["state"] == "failed"

    async def prepare(*args, **kwargs):
        return None

    monkeypatch.setattr(service, "submit_task", real_submit)
    monkeypatch.setattr(service, "prepare_daily_puzzle", prepare)
    second = await service.start_day_preparation(DAY, object())
    assert second["id"] != first["id"]
    assert second["state"] == "queued"
    await service._tasks[DAY]
    assert service._keys(DAY)[0] not in redis.values


@pytest.mark.asyncio
async def test_missing_redis_lease_reconciles_dead_worker_before_deadline(store, monkeypatch):
    redis = RedisStore()
    monkeypatch.setattr(cache, "redis_client", redis)
    redis.values[service._keys(DAY)[1]] = json.dumps(
        {"id": "lost-lease", "state": "running", "started_at": time.time(), "deadline": time.time() + 600}
    )
    assert (await service.get_day_readiness(DAY))["job"]["state"] == "failed"
