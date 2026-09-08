from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import web
from app.games import crocodile_daily as daily
from app.providers import pollinations
from app.repos import settings_repo


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    monkeypatch.setattr(web.settings, "AVAILABLE_MODELS", ["gemini-2.5-flash", "gemini-2.5-pro"])
    return web.quart_app.test_client(), {"X-Auth-Token": "test-token"}


@pytest.mark.asyncio
async def test_role_setting_is_saved_separately_from_shared_default(monkeypatch, admin):
    client, headers = admin
    save = AsyncMock()
    monkeypatch.setattr(settings_repo, "set_global_setting", save)
    response = await client.post(
        "/api/admin/dailycroc/text-model", headers=headers, json={"process": "judge", "model": "gemini-2.5-pro"}
    )
    assert response.status_code == 200
    save.assert_awaited_once_with("daily_croc_text_model_judge", "gemini-2.5-pro")


@pytest.mark.asyncio
async def test_unknown_role_does_not_change_any_setting(monkeypatch, admin):
    client, headers = admin
    save = AsyncMock()
    monkeypatch.setattr(settings_repo, "set_global_setting", save)
    response = await client.post(
        "/api/admin/dailycroc/text-model", headers=headers, json={"process": "unknown", "model": "gemini-2.5-pro"}
    )
    assert response.status_code == 400
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_image_model_resolves_alias_and_is_used_at_runtime(monkeypatch, admin):
    client, headers = admin
    values = {}

    async def save(key, value):
        values[key] = value

    async def get(key, default=""):
        return values.get(key, default)

    monkeypatch.setattr(settings_repo, "set_global_setting", save)
    monkeypatch.setattr(settings_repo, "get_global_setting", get)
    monkeypatch.setattr(
        pollinations,
        "fetch_models",
        AsyncMock(return_value=[{"id": "qwen/qwen-image-3", "title": "Qwen Image 3", "aliases": ["qwen-image-3"]}]),
    )
    response = await client.post("/api/admin/dailycroc/image-default", headers=headers, json={"model": "qwen-image-3"})
    assert response.status_code == 200
    assert values["daily_croc_image_model"] == "qwen/qwen-image-3"
    assert await daily.get_daily_image_model() == "qwen/qwen-image-3"

    from app.repos import crocodile_daily as repo

    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "ensure_puzzle_day", AsyncMock())
    monkeypatch.setattr(repo, "get_used_daily_words", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        "app.games.word_bank.pick_random_word_for_topic", AsyncMock(return_value=("кот", "ru", "Животные", False))
    )

    async def insert(sql, params, **kwargs):
        return [
            {
                "puzzle_date": params[0],
                "difficulty": params[1],
                "target_word": params[2],
                "topic": params[3],
                "lang": params[4],
                "image_model": params[5],
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", insert)
    created = await repo._create_puzzle_if_missing_with_conn(date(2026, 10, 20))
    assert created.image_model == "qwen/qwen-image-3"
    old = repo.DailyPuzzle(date(2026, 10, 19), "собака", "Животные", "ru", image_model="flux")
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=old))
    assert (await repo.create_puzzle_if_missing(old.puzzle_date)).image_model == "flux"


@pytest.mark.asyncio
async def test_default_image_setting_unchanged_when_catalog_unavailable(monkeypatch, admin):
    client, headers = admin
    save = AsyncMock()
    monkeypatch.setattr(settings_repo, "set_global_setting", save)
    monkeypatch.setattr(pollinations, "fetch_models", AsyncMock(side_effect=RuntimeError("offline")))
    response = await client.post("/api/admin/dailycroc/image-default", headers=headers, json={"model": "flux"})
    assert response.status_code == 503
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_date_controls_require_auth_and_validate_date(admin):
    client, headers = admin
    for path in ("/api/admin/dailycroc/day", "/api/admin/dailycroc/day/prepare"):
        method = client.get if path.endswith("/day") else client.post
        assert (await method(path)).status_code == 401
        response = await method(path, headers=headers)
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_check_and_prepare_support_day_not_in_list(monkeypatch, admin):
    from app.games import daily_preparation

    client, headers = admin
    readiness = {"date": "2026-10-20", "puzzles": [], "job": None, "fully_ready": False}
    check = AsyncMock(return_value=readiness)
    start = AsyncMock(return_value={"state": "queued"})
    monkeypatch.setattr(daily_preparation, "get_day_readiness", check)
    monkeypatch.setattr(daily_preparation, "start_day_preparation", start)
    bot = SimpleNamespace()
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: bot)
    response = await client.get("/api/admin/dailycroc/day?date=2026-10-20", headers=headers)
    assert response.status_code == 200
    assert (await response.get_json())["fully_ready"] is False
    start.assert_not_awaited()
    check.assert_awaited_once_with(date(2026, 10, 20))
    response = await client.post("/api/admin/dailycroc/day/prepare", headers=headers, json={"date": "2026-10-20"})
    assert response.status_code == 202
    start.assert_awaited_once_with(date(2026, 10, 20), bot)
