from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_image_generation_honors_requested_model(monkeypatch):
    from app.games import crocodile_daily as daily
    from app.providers import pollinations

    generate = AsyncMock(
        return_value=SimpleNamespace(success=True, images=[b"image"], warning="", model_used="qwen/qwen-image-3")
    )
    monkeypatch.setattr(pollinations, "get_pollinations_provider", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(daily, "_check_and_consume_image_quota", AsyncMock(return_value=True))
    images, model = await daily._generate_via_pollinations(
        prompt="cat", puzzle_date=date(2026, 9, 8), difficulty="easy", model="qwen/qwen-image-3"
    )
    assert images == [b"image"]
    assert model == "qwen/qwen-image-3"
    assert generate.await_args.kwargs["model"] == "qwen/qwen-image-3"


@pytest.mark.asyncio
async def test_regeneration_passes_selected_model_and_reports_failure(monkeypatch):
    from app import web
    from app.games import crocodile_daily as daily
    from app.repos import crocodile_daily as repo
    from app.web import quart_app

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=SimpleNamespace(image_file_id="old-image")))
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: object())
    prepare = AsyncMock(return_value=SimpleNamespace(image_file_id=""))
    monkeypatch.setattr(daily, "prepare_daily_puzzle", prepare)
    client = quart_app.test_client()
    response = await client.post(
        "/api/admin/dailycroc/regenerate",
        headers={"X-Auth-Token": "test-token"},
        json={"date": "2026-09-08", "difficulty": "easy", "model": "qwen/qwen-image-3"},
    )
    assert response.status_code == 500
    assert prepare.await_args.kwargs["image_model"] == "qwen/qwen-image-3"


@pytest.mark.asyncio
async def test_admin_text_settings_use_only_configured_gemini_models(monkeypatch):
    from app import web
    from app.providers import pollinations
    from app.repos import settings_repo

    monkeypatch.setattr(web.settings, "AVAILABLE_MODELS", ["gemini-2.5-flash"])
    values = {}

    async def get(key, default=""):
        return values.get(key, default)

    async def set_value(key, value):
        values[key] = value

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    monkeypatch.setattr(settings_repo, "get_global_setting", get)
    monkeypatch.setattr(settings_repo, "set_global_setting", set_value)
    monkeypatch.setattr(
        pollinations,
        "fetch_models",
        AsyncMock(
            return_value=[{"id": "openai/gpt-5.4-nano", "aliases": ["openai"], "publisher": "OpenAI", "title": "Nano"}]
        ),
    )
    client = web.quart_app.test_client()
    response = await client.post(
        "/api/admin/dailycroc/text-model", headers={"X-Auth-Token": "test-token"}, json={"model": "gemini-2.5-flash"}
    )
    assert response.status_code == 200
    assert values["daily_croc_text_model"] == "gemini-2.5-flash"
    response = await client.get("/api/admin/dailycroc/models", headers={"X-Auth-Token": "test-token"})
    payload = await response.get_json()
    assert payload["text_model"] == "gemini-2.5-flash"
    assert {item["id"] for item in payload["text_models"]} == {"", "gemini-2.5-flash"}
    pollinations.fetch_models.assert_awaited_once_with("image")


@pytest.mark.asyncio
async def test_forced_regeneration_does_not_return_old_asset_when_locked(monkeypatch):
    from app.games import crocodile_daily as daily
    from app.repos import crocodile_daily as repo

    lock = SimpleNamespace(acquire=AsyncMock(return_value=False))
    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(lock=lambda *a, **kw: lock))
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=SimpleNamespace(image_file_id="old")))
    with pytest.raises(RuntimeError, match="already in progress"):
        await daily.prepare_daily_puzzle(date(2026, 9, 8), force_image=True)


@pytest.mark.asyncio
async def test_catalog_outage_preserves_text_setting_and_can_load_saved_selection(monkeypatch):
    from app import web
    from app.providers import pollinations
    from app.repos import settings_repo

    monkeypatch.setattr(web.settings, "ADMIN_SECRET", "test-token")
    monkeypatch.setattr(pollinations, "fetch_models", AsyncMock(side_effect=ValueError("unavailable")))
    monkeypatch.setattr(settings_repo, "get_global_setting", AsyncMock(return_value="gemini-2.5-flash"))
    save = AsyncMock()
    monkeypatch.setattr(settings_repo, "set_global_setting", save)
    client = web.quart_app.test_client()
    headers = {"X-Auth-Token": "test-token"}
    response = await client.post(
        "/api/admin/dailycroc/text-model", headers=headers, json={"model": "pollinations/other"}
    )
    assert response.status_code == 400
    save.assert_not_awaited()
    response = await client.get("/api/admin/dailycroc/models", headers=headers)
    payload = await response.get_json()
    assert payload["catalog_unavailable"] is True
    assert payload["text_model"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_text_model_settings_require_authentication():
    from app.web import quart_app

    client = quart_app.test_client()
    assert (await client.post("/api/admin/dailycroc/text-model", json={"model": ""})).status_code == 401
    assert (await client.get("/api/admin/dailycroc/models")).status_code == 401
