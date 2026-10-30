from unittest.mock import AsyncMock, patch

import pytest

from app.web import quart_app

OPERATION = "9a7b803a-bc79-4860-8391-4f06373bd5d0"
BATCH = "c8799823-e538-4ce1-9804-d7a99682cfdf"


async def admin_client():
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
        session["controls_csrf"] = "synthetic-csrf"
    return client


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
        patch(
            "app.broadcast_jobs.observed_channel_counts",
            AsyncMock(return_value={"sent": 2, "mode": "delivery", "coverage_started_at": "2026-10-28T00:00:00+00:00"}),
        ),
    ):
        response = await client.get("/api/admin/broadcast/overview", headers=headers)

    payload = await response.get_json()
    assert response.status_code == 200
    horoscope = next(channel for channel in payload["channels"] if channel["id"] == "horoscope")
    assert horoscope["subscribers"] == 3
    assert horoscope["pending_today"] == 4
    assert horoscope["sent_today"] == 2
    tarot = next(channel for channel in payload["channels"] if channel["id"] == "tarot")
    assert tarot["sent_today"] == 2
    assert tarot["delivery_journal"]["mode"] == "delivery"
    assert tarot["delivery_journal"]["coverage_started_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("single", [True, False])
async def test_manual_offer_http_only_admits_with_csrf_and_operation_id(monkeypatch, single):
    from app import broadcast_jobs

    client = await admin_client()
    command = {"user_id": 123} if single else {"user_ids": [123, 456]}
    body = {**command, "channel": "horoscope", "operation_id": OPERATION}
    path = "/api/admin/broadcast/send-offer" + ("" if single else "-batch")
    admit = AsyncMock(
        return_value={
            "batch": {"batch_id": BATCH, "state": "queued"},
            "admission": {
                "state": "accepted",
                "task_id": "synthetic-task",
                "batch_id": BATCH,
                "storage_mode": "local",
                "reason_code": None,
            },
        }
    )
    monkeypatch.setattr(broadcast_jobs, "enqueue_offers", admit)
    assert (await client.post(path, json=body)).status_code == 403
    admit.assert_not_awaited()
    response = await client.post(path, json=body, headers={"X-CSRF-Token": "synthetic-csrf"})
    assert response.status_code == 202
    payload = await response.get_json()
    assert payload["admission"]["state"] == "accepted"
    assert "success" not in payload and "sent" not in payload
    assert payload["status_url"].endswith(OPERATION)
    admit.assert_awaited_once_with(
        channel="horoscope", user_ids=[123] if single else [123, 456], force=False, operation_id=OPERATION
    )


@pytest.mark.asyncio
async def test_broadcast_unknown_admission_never_resends_from_http(monkeypatch):
    from app import broadcast_jobs

    client = await admin_client()
    admit = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(broadcast_jobs, "enqueue_offers", admit)
    response = await client.post(
        "/api/admin/broadcast/send-offer",
        json={"channel": "tarot", "user_id": 123, "operation_id": OPERATION},
        headers={"X-CSRF-Token": "synthetic-csrf"},
    )
    assert response.status_code == 503
    assert (await response.get_json())["admission"]["state"] == "unknown"
    admit.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_status_lookup_and_preview_are_read_only(monkeypatch):
    from app import broadcast_jobs

    client = await admin_client()
    read = AsyncMock(return_value={"batch_id": BATCH, "sent": 0, "unknown_delivery": 1})
    preview = AsyncMock(return_value={"requested": 1, "eligible": 0, "excluded": {"unsubscribed": 1}})
    enqueue = AsyncMock(side_effect=AssertionError("No admission while observing"))
    monkeypatch.setattr(broadcast_jobs, "read_batch_by_operation", read)
    monkeypatch.setattr(broadcast_jobs, "preview_offers", preview)
    monkeypatch.setattr(broadcast_jobs, "enqueue_offers", enqueue)
    response = await client.get(f"/api/admin/broadcast/jobs/by-operation/{OPERATION}")
    assert response.status_code == 200 and (await response.get_json())["unknown_delivery"] == 1
    response = await client.get(f"/api/admin/broadcast/jobs?operations={OPERATION}")
    assert (await response.get_json())["items"][0]["batch"]["batch_id"] == BATCH
    response = await client.post(
        "/api/admin/broadcast/preview",
        json={"channel": "tarot", "user_ids": [123]},
        headers={"X-CSRF-Token": "synthetic-csrf"},
    )
    assert response.status_code == 200 and (await response.get_json())["eligible"] == 0
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_journal_filters_and_history_coverage(monkeypatch):
    from app import broadcast_jobs

    client = await admin_client()
    events = AsyncMock(
        return_value={
            "events": [],
            "next_cursor": None,
            "coverage_started_at": "2026-10-28T00:00:00+00:00",
            "retention_days": 30,
        }
    )
    monkeypatch.setattr(broadcast_jobs, "list_delivery_events", events)
    response = await client.get(
        "/api/admin/broadcast/errors?channel=tarot&date=2026-10-28&state=unknown_delivery&limit=50"
    )
    assert response.status_code == 200
    assert (await response.get_json())["retention_days"] == 30
    events.assert_awaited_once_with(
        channel="tarot", date="2026-10-28", state="unknown_delivery", reason=None, cursor=None, limit=50
    )
    events.reset_mock()
    assert (await client.get("/api/admin/broadcast/errors?limit=101")).status_code == 400
    assert (await client.get("/api/admin/broadcast/errors?state=sent&state=failed")).status_code == 400
    events.assert_not_awaited()
