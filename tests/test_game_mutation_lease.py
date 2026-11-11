"""Distributed game admission fails closed and owns its accepted child."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import crocodile
from app.games import crocodile_runtime as runtime

pytestmark = pytest.mark.asyncio


async def test_configured_redis_outage_never_admits_local_game_mutation(monkeypatch):
    redis = SimpleNamespace(set=AsyncMock(side_effect=ConnectionError("synthetic outage")))
    # The former implementation called Redis.lock and then fell back locally.
    redis.lock = lambda *_args, **_kwargs: SimpleNamespace(acquire=redis.set)
    monkeypatch.setattr(runtime, "redis_client", redis)
    with pytest.raises(TimeoutError):
        async with runtime.game_mutation_lock("outage"):
            pytest.fail("A replica cannot use local ownership after uncertain distributed admission")


class LeaseRedis:
    def __init__(self):
        self.token = None
        self.renewed = asyncio.Event()
        self.lose = asyncio.Event()
        self.release_calls = []

    async def set(self, key, value, **kwargs):
        if self.token is not None:
            return False
        self.token = value
        return True

    async def eval(self, script, count, *args):
        if "PEXPIRE" in script:
            self.renewed.set()
            await self.lose.wait()
            self.token = "replacement-owner"
            return 0
        if "DEL" in script:
            self.release_calls.append(args)
            if self.token == args[1]:
                self.token = None
                return 1
            return 0
        raise AssertionError("Unexpected mutation")


async def test_healthy_lease_renews_until_owned_mutation_exits(monkeypatch):
    redis = LeaseRedis()
    renewals = 0
    renewed_twice = asyncio.Event()
    original_eval = redis.eval

    async def eval(script, count, *args):
        nonlocal renewals
        if "PEXPIRE" in script:
            renewals += 1
            if renewals >= 2:
                renewed_twice.set()
            return 1
        return await original_eval(script, count, *args)

    redis.eval = eval
    monkeypatch.setattr(runtime, "redis_client", redis)
    monkeypatch.setattr(runtime, "_MUTATION_RENEW_INTERVAL_S", 0.001)
    async with runtime.game_mutation_lock("healthy"):
        await asyncio.wait_for(renewed_twice.wait(), 1)
        assert redis.token is not None
    assert renewals >= 2
    assert redis.token is None


async def test_expired_local_confirmation_never_starts_new_accepted_work(monkeypatch):
    redis = LeaseRedis()
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(runtime, "redis_client", redis)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.now))
    action = AsyncMock()
    with pytest.raises(TimeoutError):
        async with runtime.game_mutation_lock("expired"):
            clock.now += runtime._MUTATION_LEASE_TTL_S + 1
            await runtime.await_owned_game_action(action())
    action.assert_not_awaited()
    assert redis.token is None


async def test_lost_lease_cancels_owned_child_and_blocks_late_judgement(monkeypatch):
    redis = LeaseRedis()
    monkeypatch.setattr(runtime, "redis_client", redis)
    monkeypatch.setattr(runtime, "_MUTATION_RENEW_INTERVAL_S", 0.001, raising=False)
    judge_started, child_cancelled = asyncio.Event(), asyncio.Event()
    children = []
    game = crocodile.CrocodileGame("lost-lease", "crocodile", "animals", "en", "inline", 1, 2)
    save = AsyncMock()
    monkeypatch.setattr(game, "save", save)

    async def cancellation_resistant_judge(*_args, **_kwargs):
        children.append(asyncio.current_task())
        judge_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            child_cancelled.set()
            return "wrong", SimpleNamespace(score=0.0, hint="Cold", cached=False)

    monkeypatch.setattr("app.games.judge.judge_guess", cancellation_resistant_judge)

    async def mutate():
        async with runtime.game_mutation_lock(game.game_id):
            await runtime.await_owned_game_action(game.process_guess("stone"))

    owner = asyncio.create_task(mutate())
    try:
        await asyncio.wait_for(judge_started.wait(), 1)
        await asyncio.wait_for(redis.renewed.wait(), 1)
        redis.lose.set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(owner, 1)
        assert child_cancelled.is_set()
        assert game.attempts == []
        save.assert_not_awaited()
        assert redis.token == "replacement-owner", "Old cleanup removed a replacement lease"
    finally:
        redis.lose.set()
        if not owner.done():
            owner.cancel()
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(owner, return_exceptions=True)
