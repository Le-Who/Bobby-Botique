from unittest.mock import AsyncMock, patch

import pytest

from app.web import quart_app


@pytest.mark.asyncio
async def test_admin_broadcast_overview_reports_horoscope_delivery_slots():
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()
    headers = {"X-Auth-Token": "admin-secret"}

    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "FROM public.crocodile_daily_preferences" in query and "last_sent_puzzle_date" in query:
            return [{"cnt": 1}]
        if "FROM public.crocodile_daily_preferences" in query:
            return [{"cnt": 4}]
        if "FROM horoscope_subscriptions" in query:
            return [{"total_active": 3, "pending_deliveries": 4, "sent_deliveries_today": 2}]
        if "FROM public.tarot_daily_subscriptions" in query:
            return [{"cnt": 0}]
        return []

    with (
        patch("app.web._get_admin_secret", return_value="admin-secret"),
        patch("app.web.database.db_query", new=AsyncMock(side_effect=fake_db_query)),
        patch("app.repos.settings_repo.get_global_setting", new=AsyncMock(return_value="on")),
        patch("app.repos.daily_2048.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
    ):
        response = await client.get("/api/admin/broadcast/overview", headers=headers)

    payload = await response.get_json()
    assert response.status_code == 200
    horoscope = next(channel for channel in payload["channels"] if channel["id"] == "horoscope")
    assert horoscope["subscribers"] == 3
    assert horoscope["pending_today"] == 4
    assert horoscope["sent_today"] == 2


@pytest.mark.asyncio
async def test_admin_send_horoscope_offer_does_not_mark_failed_delivery():
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()
    headers = {"X-Auth-Token": "admin-secret"}

    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "FROM public.users" in query:
            return [{"user_id": params[0], "username": "user", "first_name": "User"}]
        return []

    with (
        patch("app.web._get_admin_secret", return_value="admin-secret"),
        patch("app.web.database.db_query", new=AsyncMock(side_effect=fake_db_query)),
        patch("app.bot_instance.get_bot", return_value=object()),
        patch("app.repos.horoscope_subscriptions.get_horoscope_subscription", new=AsyncMock(return_value=None)),
        patch("app.handlers.horoscope_subscription.send_horoscope_invite", new=AsyncMock(return_value=False)),
        patch("app.repos.horoscope_subscriptions.mark_horoscope_discovery_sent", new=AsyncMock()) as mark_mock,
    ):
        response = await client.post(
            "/api/admin/broadcast/send-offer",
            headers=headers,
            json={"user_id": 123, "channel": "horoscope"},
        )

    payload = await response.get_json()
    assert response.status_code == 502
    assert payload["success"] is False
    assert "not delivered" in payload["error"]
    mark_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_send_horoscope_offer_batch_counts_failed_delivery_as_error():
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()
    headers = {"X-Auth-Token": "admin-secret"}

    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "FROM public.users" in query:
            return [{"user_id": uid} for uid in params]
        return []

    with (
        patch("app.web._get_admin_secret", return_value="admin-secret"),
        patch("app.web.database.db_query", new=AsyncMock(side_effect=fake_db_query)),
        patch("app.bot_instance.get_bot", return_value=object()),
        patch("app.repos.horoscope_subscriptions.get_horoscope_subscription", new=AsyncMock(return_value=None)),
        patch("app.handlers.horoscope_subscription.send_horoscope_invite", new=AsyncMock(return_value=False)),
        patch("app.repos.horoscope_subscriptions.mark_horoscope_discovery_sent", new=AsyncMock()) as mark_mock,
    ):
        response = await client.post(
            "/api/admin/broadcast/send-offer-batch",
            headers=headers,
            json={"user_ids": [123], "channel": "horoscope"},
        )

    payload = await response.get_json()
    assert response.status_code == 200
    assert payload["summary"] == {"sent": 0, "skipped": 0, "errors": 1}
    assert payload["results"][0]["status"] == "error"
    mark_mock.assert_not_awaited()
