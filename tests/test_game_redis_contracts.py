"""Game quota/cache scripts against an explicitly disposable Redis service."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import NoScriptError
from redis.retry import Retry

from app import cache
from app.games import ai_budget, judgement_cache
from app.games import crocodile_daily as daily

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def redis_boundary(monkeypatch):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Explicit disposable TEST_REDIS_URL is required")
    client = Redis.from_url(url, decode_responses=True, retry=Retry(NoBackoff(), 0), socket_timeout=5)
    prefix = "test:game-contracts:" + uuid4().hex
    monkeypatch.setattr(ai_budget, "redis_client", client)
    monkeypatch.setattr(cache, "redis_client", client)
    monkeypatch.setattr(judgement_cache, "redis_client", client)
    monkeypatch.setattr(judgement_cache, "_REDIS_HASH_PREFIX", prefix + ":cache:")
    monkeypatch.setattr(daily, "_image_quota_key", lambda now=None: prefix + ":image-quota")
    keys = [
        f"croc:ai_budget:foreground:ai_studio:{prefix}",
        f"croc:ai_budget:background:ai_studio:{prefix}",
        f"croc:ai_budget:shared:ai_studio:{prefix}",
        prefix + ":image-quota",
        prefix + ":image-quota:operations",
        *(prefix + ":cache:" + namespace for namespace in ("judgement", "hints", "category", "generated_words")),
    ]
    try:
        assert await client.ping()
        yield client, prefix
    finally:
        try:
            await client.delete(*keys)
        finally:
            await client.aclose()


async def test_rpm_concurrent_workers_cannot_over_admit(redis_boundary, monkeypatch):
    client, model = redis_boundary
    managers = [ai_budget._BudgetManager() for _ in range(24)]
    all_read = asyncio.Event()
    reads = 0

    class SimultaneousCountReads:
        """Force the unsafe read/write interleaving; scripts still use real Redis."""

        def __getattr__(self, name):
            return getattr(client, name)

        async def zcard(self, key):
            nonlocal reads
            count = await client.zcard(key)
            reads += 1
            if reads == len(managers):
                all_read.set()
            await all_read.wait()
            return count

    monkeypatch.setattr(ai_budget, "redis_client", SimultaneousCountReads())
    results = await asyncio.gather(
        *(manager._try_consume_window("foreground", "judge", "ai_studio", model, 1) for manager in managers)
    )
    assert sum(results) == 1
    key = f"croc:ai_budget:foreground:ai_studio:{model}"
    assert await client.zcard(key) == 1
    assert 0 < await client.ttl(key) <= 90


async def test_rpm_replayed_admission_uses_one_slot(redis_boundary, monkeypatch):
    client, model = redis_boundary
    monkeypatch.setattr(ai_budget.uuid, "uuid4", lambda: "same-operation")
    manager = ai_budget._BudgetManager()
    assert await manager._try_consume_window("foreground", "judge", "ai_studio", model, 1)
    assert await manager._try_consume_window("foreground", "judge", "ai_studio", model, 1)
    assert await client.zcard(f"croc:ai_budget:foreground:ai_studio:{model}") == 1


@pytest.mark.parametrize("backend", ["redis", "local"])
@pytest.mark.parametrize("background_first", [True, False])
async def test_rpm_scopes_share_the_model_window(redis_boundary, monkeypatch, backend, background_first):
    _, model = redis_boundary
    if backend == "local":
        monkeypatch.setattr(ai_budget, "redis_client", None)
    manager = ai_budget._BudgetManager()
    if background_first:
        for _ in range(2):
            assert await manager._try_consume_window("background", "hint", "ai_studio", model, 2)
        assert not await manager._try_consume_window("background", "hint", "ai_studio", model, 2)
        for _ in range(12):
            assert await manager._try_consume_window("foreground", "judge", "ai_studio", model, 14)
        assert not await manager._try_consume_window("foreground", "judge", "ai_studio", model, 14)
    else:
        for _ in range(2):
            assert await manager._try_consume_window("foreground", "judge", "ai_studio", model, 14)
        assert not await manager._try_consume_window("background", "hint", "ai_studio", model, 2)


async def test_rpm_expired_entries_do_not_hold_new_admission(redis_boundary):
    client, model = redis_boundary
    key = f"croc:ai_budget:foreground:ai_studio:{model}"
    await client.zadd(key, {"old": 1})
    assert await ai_budget._BudgetManager()._try_consume_window("foreground", "judge", "ai_studio", model, 1)
    assert await client.zscore(key, "old") is None


async def test_image_concurrent_admission_and_saved_limit_changes(redis_boundary, monkeypatch):
    client, _ = redis_boundary
    limit = "1"

    async def setting(key, default):
        return limit

    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", setting)
    results = await asyncio.gather(*(daily._check_and_consume_image_quota() for _ in range(24)))
    assert sum(results) == 1
    assert await client.get(daily._image_quota_key()) == "1"
    limit = "2"
    assert await daily._check_and_consume_image_quota()
    assert not await daily._check_and_consume_image_quota()
    assert await client.get(daily._image_quota_key()) == "2"
    assert 0 < await client.ttl(daily._image_quota_key()) <= 7200


async def test_image_replayed_admission_uses_one_slot(redis_boundary, monkeypatch):
    client, _ = redis_boundary
    import uuid

    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="same-operation"))
    assert await daily._check_and_consume_image_quota(limit=1)
    assert await daily._check_and_consume_image_quota(limit=1)
    assert await client.get(daily._image_quota_key()) == "1"
    assert await client.scard(daily._image_quota_key() + ":operations") == 1


async def test_image_denial_repairs_legacy_counter_without_ttl(redis_boundary):
    client, _ = redis_boundary
    await client.set(daily._image_quota_key(), "1")
    assert not await daily._check_and_consume_image_quota(limit=1)
    assert await client.get(daily._image_quota_key()) == "1"
    assert 0 < await client.ttl(daily._image_quota_key()) <= 7200


async def test_image_lost_write_reply_keeps_consumption_bounded_and_expiring(redis_boundary, monkeypatch):
    client, _ = redis_boundary

    class LostReply:
        async def incr(self, key):
            await client.incr(key)
            raise ConnectionError("reply lost after Redis applied write")

        def register_script(self, source):
            script = client.register_script(source)

            async def execute(**kwargs):
                await script(**kwargs)
                raise ConnectionError("reply lost after Redis applied script")

            return execute

    monkeypatch.setattr(cache, "redis_client", LostReply())
    with pytest.raises(daily.DailyImageQuotaUnavailable):
        await daily._check_and_consume_image_quota(limit=1)
    assert await client.get(daily._image_quota_key()) == "1"
    assert 0 < await client.ttl(daily._image_quota_key()) <= 7200
    monkeypatch.setattr(cache, "redis_client", client)
    assert not await daily._check_and_consume_image_quota(limit=1)


@pytest.mark.parametrize(
    ("namespace", "limit_name"),
    [
        ("judgement", "_MAX_ENTRIES"),
        ("hints", "_MAX_HINTS"),
        ("category", "_MAX_CAT"),
        ("generated_words", "_MAX_GEN_WORDS"),
    ],
)
async def test_cache_writes_are_bounded_and_expire(redis_boundary, monkeypatch, namespace, limit_name):
    client, _ = redis_boundary
    monkeypatch.setattr(judgement_cache, limit_name, 3)
    await asyncio.gather(*(judgement_cache._redis_hash_set(namespace, str(i), "value") for i in range(20)))
    key = judgement_cache._redis_hash_name(namespace)
    assert await client.hlen(key) == 3
    assert 0 < await client.ttl(key) <= 30 * 86400


async def test_cache_legacy_reads_preserve_format_and_do_not_extend_retention(redis_boundary):
    client, _ = redis_boundary
    key = judgement_cache._redis_hash_name("category")
    await client.hset(key, "cat", "animals")
    assert await judgement_cache._redis_hash_get("category", "cat") == "animals"
    assert 0 < await client.ttl(key) <= 30 * 86400
    await client.expire(key, 60)
    await judgement_cache._redis_hash_set("category", "dog", "animals")
    assert 0 < await client.ttl(key) <= 60
    assert await judgement_cache._redis_hash_get("category", "cat") == "animals"
    await judgement_cache._redis_hash_delete("category", "cat")
    assert await client.hget(key, "cat") is None
    await client.pexpire(key, 0)
    assert await judgement_cache._redis_hash_get("category", "dog") is None


async def test_cache_legacy_oversize_shrinks_in_bounded_steps(redis_boundary, monkeypatch):
    client, _ = redis_boundary
    monkeypatch.setattr(judgement_cache, "_MAX_CAT", 3)
    key = judgement_cache._redis_hash_name("category")
    await client.hset(key, mapping={str(i): "legacy" for i in range(300)})
    await judgement_cache._redis_hash_set("category", "new", "animals")
    remaining = await client.hlen(key)
    assert 172 <= remaining < 300
    assert await client.hget(key, "new") is None
    assert 0 < await client.ttl(key) <= 30 * 86400
    for _ in range(4):
        await judgement_cache._redis_hash_set("category", "new", "animals")
    assert await client.hlen(key) == 3
    assert await client.hget(key, "new") == "animals"


@pytest.mark.parametrize("operation", ["rpm", "image", "cache"])
async def test_game_scripts_recover_after_noscript(redis_boundary, monkeypatch, operation):
    client, model = redis_boundary
    real_evalsha = client.evalsha
    missing = True

    async def script_cache_miss(*args, **kwargs):
        nonlocal missing
        if missing:
            missing = False
            raise NoScriptError("Script was removed from Redis script cache")
        return await real_evalsha(*args, **kwargs)

    monkeypatch.setattr(client, "evalsha", script_cache_miss)
    if operation == "rpm":
        assert await ai_budget._BudgetManager()._try_consume_window("foreground", "judge", "ai_studio", model, 1)
        assert await client.zcard(f"croc:ai_budget:foreground:ai_studio:{model}") == 1
    elif operation == "image":
        assert await daily._check_and_consume_image_quota(limit=1)
        assert await client.get(daily._image_quota_key()) == "1"
    else:
        await judgement_cache._redis_hash_set("category", "cat", "animals")
        assert await client.hget(judgement_cache._redis_hash_name("category"), "cat") == "animals"
