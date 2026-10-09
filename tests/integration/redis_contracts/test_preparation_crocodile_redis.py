"""product-08: production Redis fencing, game locks and pending-ID races."""

import asyncio
import importlib.util
import json
import os
import time
import urllib.parse
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app import cache
from app.games import crocodile
from app.games import crocodile_runtime as runtime
from app.games import daily_preparation as preparation
from tests.factories import make_crocodile_game, make_valid_init_data

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
DAY = date(2027, 3, 1)


@pytest.fixture
async def redis_boundary(monkeypatch):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Explicit TEST_REDIS_URL is required")
    first = Redis.from_url(url, decode_responses=True, socket_timeout=10, socket_connect_timeout=5)
    second = Redis.from_url(url, decode_responses=True, socket_timeout=10, socket_connect_timeout=5)
    namespace = "test:preparation:" + uuid4().hex
    game_id = namespace + ":game"
    keys = (
        namespace + ":lease",
        namespace + ":status",
        runtime._lock_key(game_id),
        runtime._pending_key(game_id),
        runtime._history_key(game_id),
        runtime._seq_key(game_id),
        runtime._hints_key(game_id),
        "croc:game:" + game_id,
    )
    monkeypatch.setattr(cache, "redis_client", first)
    monkeypatch.setattr(runtime, "redis_client", first)
    monkeypatch.setattr(preparation, "_keys", lambda _: keys[:2])
    monkeypatch.setattr(preparation, "_jobs", {})
    monkeypatch.setattr(preparation, "_tasks", {})
    try:
        assert await first.ping() and await second.ping()
        yield first, second, keys, game_id
    finally:
        try:
            await first.delete(*keys)
            for mapping in (
                runtime._local_history,
                runtime._local_event_seq,
                runtime._local_pending_results,
                runtime._local_hints,
                runtime._local_locks,
                crocodile._mem_history,
            ):
                mapping.pop(game_id, None)
        finally:
            await first.aclose()
            await second.aclose()


def job(token, state="running"):
    now = time.time()
    return {"id": token, "state": state, "started_at": now, "deadline": now + 660, "errors": []}


async def test_preparation_nx_winner_and_expired_owner_fencing(redis_boundary):
    first, second, keys, _ = redis_boundary
    barrier = asyncio.Barrier(2)

    async def acquire(client, owner):
        await barrier.wait()
        return await client.set(keys[0], owner, nx=True, ex=660)

    results = await asyncio.gather(acquire(first, "old-a"), acquire(second, "old-b"))
    assert sum(bool(result) for result in results) == 1
    old_owner = await first.get(keys[0])
    await preparation._save(DAY, job(old_owner), first)
    assert json.loads(await second.get(keys[1]))["id"] == old_owner
    assert 0 < await second.ttl(keys[0]) <= 660
    # Server-side expiry, not a fake clock or Python delete/sleep.
    assert await first.pexpire(keys[0], 0)
    assert await second.pttl(keys[0]) == -2
    assert await second.set(keys[0], "replacement", nx=True, ex=660)
    replacement = job("replacement")
    await preparation._save(DAY, replacement, second)
    await preparation._save(DAY, job(old_owner, "completed"), first)
    assert json.loads(await second.get(keys[1])) == replacement
    assert await first.eval(preparation._RELEASE, 1, keys[0], old_owner) == 0
    assert await second.get(keys[0]) == "replacement"
    assert await second.eval(preparation._RELEASE, 1, keys[0], "replacement") == 1
    assert await first.get(keys[0]) is None


async def test_late_preparation_worker_finally_preserves_replacement(redis_boundary, monkeypatch):
    first, second, keys, _ = redis_boundary
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def prepare(*args, difficulty, **kwargs):
        attempts.append(difficulty)
        entered.set()
        await release.wait()

    async def missing(*args, **kwargs):
        return None

    monkeypatch.setattr(preparation, "prepare_daily_puzzle", prepare)
    monkeypatch.setattr(preparation.repo, "get_puzzle", missing)
    old = job("old")
    await first.set(keys[0], "old", ex=660)
    task = asyncio.create_task(preparation._run.__wrapped__(DAY, object(), old, first))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await second.pexpire(keys[0], 0)
        assert await second.set(keys[0], "new", nx=True, ex=660)
        replacement = job("new")
        await preparation._save(DAY, replacement, second)
        release.set()
        await asyncio.wait_for(task, 5)
        assert attempts == ["easy", "hard"]
        assert old["state"] == "failed"
        assert await second.get(keys[0]) == "new"
        assert json.loads(await second.get(keys[1])) == replacement
        assert (await preparation._read_job(DAY))["id"] == "new"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def replica(client):
    spec = importlib.util.spec_from_file_location("crocodile_runtime_replica_" + uuid4().hex, runtime.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.redis_client = client
    return module


async def test_real_game_lock_contention_never_falls_back_to_local(redis_boundary):
    first, second, _, game_id = redis_boundary
    worker_a, worker_b = replica(first), replica(second)
    async with worker_a.game_mutation_lock(game_id):
        with pytest.raises(TimeoutError, match="timed out waiting"):
            async with worker_b.game_mutation_lock(game_id):
                pytest.fail("A competing replica must not mutate under its local lock")
    assert game_id not in worker_b._local_locks
    async with worker_b.game_mutation_lock(game_id):
        assert await first.exists(runtime._lock_key(game_id)) == 1
    assert await first.exists(runtime._lock_key(game_id)) == 0


async def test_game_body_failure_preserves_original_error_and_releases_redis_lock(redis_boundary):
    first, _, _, game_id = redis_boundary
    worker = replica(first)
    with pytest.raises(RuntimeError, match="controlled mutation failure"):
        async with worker.game_mutation_lock(game_id):
            raise RuntimeError("controlled mutation failure")
    assert await first.exists(runtime._lock_key(game_id)) == 0
    assert game_id not in worker._local_locks


async def test_cancelled_game_mutation_awaits_release_without_local_fallback(redis_boundary):
    first, second, _, game_id = redis_boundary
    worker, contender = replica(first), replica(second)
    entered = asyncio.Event()

    async def mutate():
        async with worker.game_mutation_lock(game_id):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(mutate())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await first.exists(runtime._lock_key(game_id)) == 0
        assert game_id not in worker._local_locks
        async with contender.game_mutation_lock(game_id):
            assert await first.exists(runtime._lock_key(game_id)) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_simultaneous_websocket_pending_id_persists_one_attempt(redis_boundary, monkeypatch):
    from app.web import quart_app

    first, second, keys, game_id = redis_boundary
    game = make_crocodile_game(game_id=game_id, creator_id=111, guesser_id=222)
    game.best_score = 0.9  # Avoid unrelated Telegram thermometer background work.
    await game.save()
    token = "synthetic-websocket-token"
    monkeypatch.setattr("app.config.settings.TELEGRAM_BOT_TOKEN", token)

    async def authorized(*args):
        return True

    async def activity(*args, **kwargs):
        pass

    async def judge(*args, **kwargs):
        return "cold", SimpleNamespace(score=0.2, hint="Controlled clue", cached=False)

    monkeypatch.setattr("app.repos.users.is_authorized", authorized)
    monkeypatch.setattr("app.repos.crocodile_daily.record_player_activity", activity)
    monkeypatch.setattr("app.games.judge.judge_guess", judge)
    published = []
    original_publish = runtime.publish_runtime_event

    async def publish(game_id, event, **kwargs):
        published.append(event)
        return await original_publish(game_id, event, **kwargs)

    monkeypatch.setattr(runtime, "publish_runtime_event", publish)
    original_cached = runtime.get_cached_pending_action_result
    reads = 0
    barrier = asyncio.Barrier(2)

    async def cached(game_id, pending_id):
        nonlocal reads
        result = await original_cached(game_id, pending_id)
        reads += 1
        if reads <= 2:
            assert result is None
            await barrier.wait()
        return result

    monkeypatch.setattr(runtime, "get_cached_pending_action_result", cached)
    url = (
        "/webapp/game/ws?initData="
        + urllib.parse.quote(make_valid_init_data(token, user_id=222))
        + "&game_id="
        + game_id
    )
    async with quart_app.test_client().websocket(url) as left, quart_app.test_client().websocket(url) as right:
        assert json.loads(await left.receive())["event"] == "game_state"
        assert json.loads(await right.receive())["event"] == "game_state"
        payload = json.dumps({"type": "guess", "word": "собака", "pending_id": "same-action"})
        await asyncio.gather(left.send(payload), right.send(payload))

        async def result(socket):
            while True:
                event = json.loads(await socket.receive())
                if event.get("pending_id") == "same-action":
                    return event

        responses = await asyncio.wait_for(asyncio.gather(result(left), result(right)), 10)
    stored = json.loads(await second.get(keys[-1]))
    assert stored["attempts"] == ["собака"]
    assert responses[0] == responses[1]
    assert responses[0]["attempts"] == 1
    assert await first.llen(runtime._history_key(game_id)) == 1
    assert await first.hlen(runtime._pending_key(game_id)) == 1
    assert len(published) == 1
    assert published[0]["event"] == "spectator_result"
