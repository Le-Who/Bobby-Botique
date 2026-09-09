from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import crocodile_daily as daily
from app.repos import crocodile_daily as repo


@pytest.fixture
def quota_store(monkeypatch):
    values = {}

    async def get(key, default=""):
        return values.get(key, default)

    async def set_value(key, value):
        values[key] = value

    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(daily, "_local_image_quota", {})
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", get)
    monkeypatch.setattr("app.repos.settings_repo.set_global_setting", set_value)
    return values


@pytest.mark.asyncio
async def test_default_budget_covers_both_tracks_and_image_horizon(quota_store):
    now = datetime(2026, 9, 9, tzinfo=UTC)
    for _ in range(6):
        assert await daily._check_and_consume_image_quota(now=now)
    assert not await daily._check_and_consume_image_quota(now=now)
    assert await daily._check_and_consume_image_quota(now=now + timedelta(hours=1))


@pytest.mark.asyncio
async def test_admin_can_change_limit_without_resetting_usage(quota_store, monkeypatch):
    from app import web

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    client = web.quart_app.test_client()
    headers = {"X-Auth-Token": "test-token"}
    for limit in (1, 3, 0):
        response = await client.post("/api/admin/dailycroc/image-quota", headers=headers, json={"limit": limit})
        assert response.status_code == 200
        assert (await response.get_json())["limit"] == limit
        assert await daily._check_and_consume_image_quota() is (limit != 0)
        if limit == 1:
            assert not await daily._check_and_consume_image_quota()
    response = await client.get("/api/admin/dailycroc/image-quota", headers=headers)
    assert (await response.get_json())["limit"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"limit": -1}, {"limit": True}, {"limit": 1.5}, {"limit": "2"}, {"limit": 1001}, []]
)
async def test_invalid_quota_does_not_change_settings(quota_store, monkeypatch, payload):
    from app import web

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    response = await web.quart_app.test_client().post(
        "/api/admin/dailycroc/image-quota", headers={"X-Auth-Token": "test-token"}, json=payload
    )
    assert response.status_code == 400
    assert quota_store == {}


@pytest.mark.asyncio
async def test_quota_requires_admin(quota_store):
    from app.web import quart_app

    client = quart_app.test_client()
    assert (await client.get("/api/admin/dailycroc/image-quota")).status_code == 401
    assert (await client.post("/api/admin/dailycroc/image-quota", json={"limit": 10})).status_code == 401
    assert quota_store == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen-image", "fta-gpt-image-2"])
async def test_admin_regeneration_bypasses_exhausted_quota_including_fallback(quota_store, monkeypatch, model):
    from app import web

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    puzzle = repo.DailyPuzzle(
        date(2026, 9, 9), "cat", "animals", "en", hints=["hint"], image_prompt="cat", image_file_id="old"
    )
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=puzzle))
    save = AsyncMock()
    monkeypatch.setattr(repo, "set_puzzle_image_asset", save)
    monkeypatch.setattr(repo, "mark_puzzle_prepared", AsyncMock())
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(photo=[SimpleNamespace(file_id="new")], delete=AsyncMock()))
    )
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: bot)
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(success=True, images=[b"image"], warning="", model_used="qwen-image")
        )
    )
    monkeypatch.setattr("app.providers.pollinations.get_pollinations_provider", lambda: provider)
    monkeypatch.setattr(daily, "_generate_via_fta", AsyncMock(return_value=([], "img/gpt-image-2")))
    while await daily._check_and_consume_image_quota():
        pass
    before = dict(daily._local_image_quota)
    response = await web.quart_app.test_client().post(
        "/api/admin/dailycroc/regenerate",
        headers={"X-Auth-Token": "test-token"},
        json={"date": "2026-09-09", "difficulty": "easy", "model": model},
    )
    assert response.status_code == 200
    assert (await response.get_json())["file_id"] == "new"
    assert save.await_args.args[1] == "new"
    assert daily._local_image_quota == before


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen-image", "fta-gpt-image-2"])
async def test_automatic_generation_stops_before_any_provider_when_paused(quota_store, monkeypatch, model):
    quota_store["daily_croc_image_quota_per_hour"] = "0"
    provider = AsyncMock(side_effect=AssertionError("provider must not run"))
    monkeypatch.setattr(daily, "_generate_via_pollinations", provider)
    monkeypatch.setattr(daily, "_generate_via_fta", provider)
    file_id, _ = await daily._generate_daily_image_file_id(
        object(), prompt="cat", puzzle_date=date(2026, 9, 9), difficulty="easy", image_model=model
    )
    assert file_id is None


@pytest.mark.asyncio
async def test_redis_budget_uses_saved_limit_and_rolls_back_denied_attempts(quota_store, monkeypatch):
    counts = {}
    expirations = {}

    async def incr(key):
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    async def decr(key):
        counts[key] -= 1

    async def expire(key, ttl):
        expirations[key] = ttl

    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(incr=incr, decr=decr, expire=expire))
    quota_store["daily_croc_image_quota_per_hour"] = "1"
    now = datetime(2026, 9, 9, tzinfo=UTC)
    assert await daily._check_and_consume_image_quota(now=now)
    assert not await daily._check_and_consume_image_quota(now=now)
    assert counts == {"daily:img:quota:2026-09-09T00": 1}
    assert expirations == {"daily:img:quota:2026-09-09T00": 7200}
    quota_store["daily_croc_image_quota_per_hour"] = "2"
    assert await daily._check_and_consume_image_quota(now=now)
    assert counts["daily:img:quota:2026-09-09T00"] == 2
    assert daily._local_image_quota == {}


@pytest.mark.asyncio
async def test_redis_outage_keeps_local_attempts_bounded(quota_store, monkeypatch):
    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(incr=AsyncMock(side_effect=ConnectionError)))
    quota_store["daily_croc_image_quota_per_hour"] = "1"
    assert await daily._check_and_consume_image_quota()
    assert not await daily._check_and_consume_image_quota()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["broken", "-1", "1001"])
async def test_invalid_persisted_limit_falls_back_to_bounded_default(quota_store, raw):
    quota_store["daily_croc_image_quota_per_hour"] = raw
    for _ in range(6):
        assert await daily._check_and_consume_image_quota()
    assert not await daily._check_and_consume_image_quota()


@pytest.mark.asyncio
async def test_failed_automatic_fallback_consumes_only_one_attempt(quota_store, monkeypatch):
    quota_store["daily_croc_image_quota_per_hour"] = "1"
    fta = AsyncMock(return_value=([], "img/gpt-image-2"))
    pollinations = AsyncMock(return_value=([], "qwen-image"))
    monkeypatch.setattr(daily, "_generate_via_fta", fta)
    monkeypatch.setattr(daily, "_generate_via_pollinations", pollinations)
    for _ in range(2):
        file_id, _ = await daily._generate_daily_image_file_id(
            object(), prompt="cat", puzzle_date=date(2026, 9, 9), difficulty="easy", image_model="fta-gpt-image-2"
        )
        assert file_id is None
    assert fta.await_count == pollinations.await_count == 1
    assert list(daily._local_image_quota.values()) == [1]


@pytest.mark.asyncio
async def test_player_force_image_does_not_bypass_automatic_quota(quota_store, monkeypatch):
    quota_store["daily_croc_image_quota_per_hour"] = "0"
    puzzle = repo.DailyPuzzle(date(2026, 9, 9), "cat", "animals", "en", hints=["hint"], image_prompt="cat")
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(daily, "_generate_via_pollinations", AsyncMock(side_effect=AssertionError("must not run")))
    with pytest.raises(RuntimeError, match="Failed to generate"):
        await daily.prepare_daily_puzzle(puzzle.puzzle_date, bot=object(), force_image=True)
