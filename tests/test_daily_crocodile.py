from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest

from app.games import crocodile_daily_telegram
from app.handlers import cmd_admin, daily_crocodile
from app.repos import crocodile_daily as repo
from app.web import quart_app
from tests.factories import make_valid_init_data


def test_daily_score_formula() -> None:
    assert repo.compute_points(won=False, attempt_count=1, used_hints_count=0) == 0
    assert repo.compute_points(won=True, attempt_count=1, used_hints_count=0) == 1000
    assert repo.compute_points(won=True, attempt_count=3, used_hints_count=1) == 660
    assert repo.compute_points(won=True, attempt_count=6, used_hints_count=3) == 200


def test_daily_share_grid() -> None:
    attempts = [
        {"status": "cold"},
        {"status": "warm"},
        {"status": "hot"},
        {"status": "exact_match"},
    ]
    assert repo.build_share_grid(attempts, won=True) == "🟥🟨🟧🟩"


@pytest.mark.asyncio
async def test_due_delivery_respects_user_timezone(monkeypatch) -> None:
    async def fake_db_query(query, params=(), retries=3, conn=None):
        return [
            {
                "user_id": 1,
                "timezone": "Europe/Kyiv",
                "preferred_local_hour": 13,
                "last_sent_puzzle_date": None,
                "last_sent_local_date": None,
            },
            {
                "user_id": 2,
                "timezone": "America/New_York",
                "preferred_local_hour": 13,
                "last_sent_puzzle_date": None,
                "last_sent_local_date": None,
            },
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)
    due = await repo.get_due_deliveries(
        puzzle_date=date(2026, 4, 21),
        now=datetime(2026, 4, 21, 10, 5, tzinfo=UTC),
    )

    assert [row["user_id"] for row in due] == [1]


@pytest.mark.asyncio
async def test_due_delivery_allows_late_scheduler_but_skips_same_local_day(monkeypatch) -> None:
    async def fake_db_query(query, params=(), retries=3, conn=None):
        return [
            {
                "user_id": 1,
                "timezone": "Europe/Kyiv",
                "preferred_local_hour": 13,
                "last_sent_puzzle_date": None,
                "last_sent_local_date": date(2026, 4, 21),
            },
            {
                "user_id": 2,
                "timezone": "Europe/Kyiv",
                "preferred_local_hour": 13,
                "last_sent_puzzle_date": None,
                "last_sent_local_date": None,
            },
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)
    due = await repo.get_due_deliveries(
        puzzle_date=date(2026, 4, 21),
        now=datetime(2026, 4, 21, 12, 45, tzinfo=UTC),
    )

    assert [row["user_id"] for row in due] == [2]


@pytest.mark.asyncio
async def test_discovery_candidates_use_authorized_or_activity_query_and_local_midday(monkeypatch) -> None:
    seen_query = ""

    async def fake_db_query(query, params=(), retries=3, conn=None):
        nonlocal seen_query
        seen_query = query
        return [
            {"user_id": 10, "timezone": "Europe/Kyiv"},
            {"user_id": 20, "timezone": "America/New_York"},
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)
    due = await repo.get_discovery_candidates(now=datetime(2026, 4, 21, 10, 0, tzinfo=UTC))

    assert "public.users WHERE is_authorized = 1" in seen_query
    assert "public.crocodile_player_activity" in seen_query
    assert [row["user_id"] for row in due] == [10]


def test_intro_keyboard_labels() -> None:
    markup = daily_crocodile.daily_intro_keyboard()
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert labels == ["Открыть daily", "Получать каждый день", "Не напоминать 2 недели"]


@pytest.mark.asyncio
async def test_send_daily_prompt_uses_placeholder_photo_and_tracks_prompt_message() -> None:
    sent_message = SimpleNamespace(chat_id=77, message_id=501)
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=sent_message),
        send_message=AsyncMock(),
    )

    with (
        patch("app.handlers.daily_crocodile._get_placeholder_file_id", new_callable=AsyncMock, return_value="placeholder-file"),
        patch("app.handlers.daily_crocodile.repo.register_prompt_message", new_callable=AsyncMock) as register_mock,
        patch("app.handlers.daily_crocodile.repo.get_preference", new_callable=AsyncMock, return_value={"timezone": "Europe/Kyiv"}),
        patch("app.handlers.daily_crocodile.repo.mark_daily_sent", new_callable=AsyncMock) as mark_mock,
    ):
        sent_as_photo = await daily_crocodile.send_daily_prompt(bot, 77, date(2026, 4, 23))

    assert sent_as_photo is True
    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    register_mock.assert_awaited_once_with(
        user_id=77,
        puzzle_date=date(2026, 4, 23),
        chat_id=77,
        message_id=501,
    )
    mark_mock.assert_awaited_once_with(77, date(2026, 4, 23), timezone="Europe/Kyiv")


@pytest.mark.asyncio
async def test_send_daily_prompt_can_skip_prompt_tracking() -> None:
    sent_message = SimpleNamespace(chat_id=77, message_id=888)
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=sent_message),
        send_message=AsyncMock(),
    )

    with (
        patch("app.handlers.daily_crocodile._get_placeholder_file_id", new_callable=AsyncMock, return_value="placeholder-file"),
        patch("app.handlers.daily_crocodile.repo.register_prompt_message", new_callable=AsyncMock) as register_mock,
        patch("app.handlers.daily_crocodile.repo.get_preference", new_callable=AsyncMock, return_value={"timezone": "Europe/Kyiv"}),
        patch("app.handlers.daily_crocodile.repo.mark_daily_sent", new_callable=AsyncMock),
    ):
        sent_as_photo = await daily_crocodile.send_daily_prompt(
            bot,
            77,
            date(2026, 4, 23),
            track_prompt_message=False,
        )

    assert sent_as_photo is True
    register_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dailycroc_command_reuses_placeholder_sender_for_manual_entry() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=77),
        effective_chat=SimpleNamespace(id=77),
        message=SimpleNamespace(message_id=900),
    )
    context = SimpleNamespace(bot=SimpleNamespace())
    original = daily_crocodile.dailycroc_command.__wrapped__.__wrapped__

    with (
        patch("app.handlers.daily_crocodile.repo.record_player_activity", new_callable=AsyncMock) as activity_mock,
        patch("app.handlers.daily_crocodile.repo.get_preference", new_callable=AsyncMock, return_value={"is_subscribed": False}),
        patch("app.handlers.daily_crocodile.repo.today_puzzle_date", return_value=date(2026, 4, 23)),
        patch("app.handlers.daily_crocodile._send_daily_entry_message", new_callable=AsyncMock) as sender_mock,
        patch("app.handlers.daily_crocodile.repo.mark_daily_sent", new_callable=AsyncMock) as mark_mock,
    ):
        await original(update, context)

    activity_mock.assert_awaited_once_with(77, event="daily_played")
    sender_mock.assert_awaited_once()
    call = sender_mock.await_args
    assert call.args[0] is context.bot
    assert call.kwargs["chat_id"] == 77
    assert call.kwargs["user_id"] == 77
    assert call.kwargs["puzzle_date"] == date(2026, 4, 23)
    assert call.kwargs["include_subscribe"] is True
    assert call.kwargs["reply_to_message_id"] == 900
    assert "На сегодня доступны" in call.kwargs["caption"]
    mark_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_dailycroc_status_snapshot_shows_hint_and_art_breakdown() -> None:
    easy = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 23),
        target_word="крокодил",
        topic="Разное",
        lang="ru",
        difficulty="easy",
        hints=["h1", "h2", "h3"],
        image_file_id="img-1",
        prepared_at=datetime(2026, 4, 23, 6, 15, tzinfo=UTC),
    )
    hard = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 23),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        hints=["h1", "h2", "h3"],
        image_file_id="",
        prepared_at=datetime(2026, 4, 23, 6, 45, tzinfo=UTC),
    )

    async def fake_get_global_setting(key: str, default=""):
        values = {
            repo.DAILY_DELIVERY_SETTING_KEY: "on",
            "daily_croc_placeholder_file_id": "placeholder-file",
            "daily_croc_placeholder_test_status": '{"status":"ok","mode":"photo","timestamp":"2026-04-23T06:20:00+00:00","error":""}',
        }
        return values.get(key, default)

    with (
        patch("app.handlers.cmd_admin.daily_croc_repo.get_delivery_status", new_callable=AsyncMock, return_value={"total_subscribed": 5, "sent_today": 2, "pending_today": 3, "finished": 4, "won": 4, "active": 0}),
        patch("app.handlers.cmd_admin.daily_croc_repo.get_puzzles_for_date", new_callable=AsyncMock, return_value={"easy": easy, "hard": hard}),
        patch("app.handlers.cmd_admin.get_global_setting", new_callable=AsyncMock, side_effect=fake_get_global_setting),
        patch("app.games.crocodile_flags.get_crocodile_runtime_switches", new_callable=AsyncMock, return_value={"live_audio_enabled": True, "crocodile_hint_prewarm_enabled": True, "daily_dual_track_enabled": True}),
        patch("app.games.hinting.get_hint_prewarm_health", new_callable=AsyncMock, return_value={"queue_depth": 0, "worker_running": False}),
        patch("app.games.crocodile_runtime.get_runtime_health_snapshot", return_value={"history_buffers": 0, "pending_result_buckets": 0}),
        patch("app.providers.gemini.get_vertex_client", return_value=object()),
        patch("app.web_miniapp._get_live_model_cooldown_seconds", return_value=0),
    ):
        text, keyboard = await cmd_admin.build_dailycroc_status_snapshot(now=datetime(2026, 4, 23, 9, 0, tzinfo=UTC))

    assert "easy: <code>ready</code> · puzzle=<code>yes</code> · hints=<code>3/3</code> · art=<code>yes</code> · prepared=<code>23.04 09:15 Kyiv</code>" in text
    assert "hard: <code>ready</code> · puzzle=<code>yes</code> · hints=<code>3/3</code> · art=<code>no</code> · prepared=<code>23.04 09:45 Kyiv</code>" in text
    assert "🧪 <b>Placeholder test:</b> <code>ok/photo @ 23.04 09:20 Kyiv</code>" in text
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["🔄 Refresh", "🧪 Prep check", "📬 Send test to admin"]


@pytest.mark.asyncio
async def test_dailycroc_prep_check_callback_runs_preparation_and_refreshes_status() -> None:
    query = SimpleNamespace(
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=object())
    original = cmd_admin.run_dailycroc_prep_check_callback.__wrapped__

    with (
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new_callable=AsyncMock) as ensure_mock,
        patch("app.handlers.cmd_admin._refresh_dailycroc_status_message", new_callable=AsyncMock) as refresh_mock,
    ):
        await original(update, context)

    query.answer.assert_awaited_once_with("🧪 Проверяю daily prep…", show_alert=False)
    ensure_mock.assert_awaited_once()
    assert ensure_mock.await_args.args[0] is context.bot
    assert ensure_mock.await_args.kwargs["now"] is not None
    refresh_mock.assert_awaited_once_with(query)


@pytest.mark.asyncio
async def test_refresh_dailycroc_status_callback_treats_capitalized_not_modified_as_fresh() -> None:
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=object())
    original = cmd_admin.refresh_dailycroc_status_callback.__wrapped__

    with patch(
        "app.handlers.cmd_admin._refresh_dailycroc_status_message",
        new_callable=AsyncMock,
        side_effect=BadRequest(
            "Message is not modified: specified new message content and reply markup are exactly the same"
        ),
    ):
        await original(update, context)

    query.answer.assert_awaited_once_with("✅ Данные актуальны", show_alert=False)


@pytest.mark.asyncio
async def test_dailycroc_prep_check_callback_ignores_capitalized_not_modified() -> None:
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=object())
    original = cmd_admin.run_dailycroc_prep_check_callback.__wrapped__

    with (
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new_callable=AsyncMock),
        patch(
            "app.handlers.cmd_admin._refresh_dailycroc_status_message",
            new_callable=AsyncMock,
            side_effect=BadRequest(
                "Message is not modified: specified new message content and reply markup are exactly the same"
            ),
        ),
    ):
        await original(update, context)

    query.answer.assert_awaited_once_with("🧪 Проверяю daily prep…", show_alert=False)


@pytest.mark.asyncio
async def test_send_dailycroc_test_callback_uses_daily_prompt_without_marking_delivery() -> None:
    query = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=777),
    )
    context = SimpleNamespace(bot=object())
    original = cmd_admin.send_dailycroc_test_callback.__wrapped__

    with (
        patch("app.handlers.cmd_admin.daily_croc_repo.today_puzzle_date", return_value=date(2026, 4, 23)),
        patch(
            "app.handlers.daily_crocodile.send_daily_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ) as prompt_mock,
        patch("app.handlers.cmd_admin.set_global_setting", new_callable=AsyncMock) as set_mock,
    ):
        await original(update, context)

    prompt_mock.assert_awaited_once_with(
        context.bot,
        777,
        date(2026, 4, 23),
        include_subscribe=False,
        mark_delivered=False,
        track_prompt_message=False,
    )
    set_mock.assert_awaited_once()
    stored_key, stored_value = set_mock.await_args.args
    assert stored_key == "daily_croc_placeholder_test_status"
    payload = json.loads(stored_value)
    assert payload["status"] == "ok"
    assert payload["mode"] == "photo"
    query.answer.assert_awaited_once_with("📬 Тест отправлен админу (с placeholder)", show_alert=False)


@pytest.mark.asyncio
async def test_result_refresh_coalesces_multiple_queue_calls(monkeypatch) -> None:
    crocodile_daily_telegram.reset_daily_telegram_state_for_tests()
    calls = 0

    async def fake_get_messages(puzzle_date, *, limit=200):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(crocodile_daily_telegram.repo, "get_active_result_messages", fake_get_messages)
    bot = SimpleNamespace(edit_message_text=AsyncMock())

    crocodile_daily_telegram.queue_daily_result_refresh(bot, date(2026, 4, 21))
    crocodile_daily_telegram.queue_daily_result_refresh(bot, date(2026, 4, 21))

    await asyncio.sleep(2.2)

    assert calls == 1
    crocodile_daily_telegram.reset_daily_telegram_state_for_tests()


@pytest.mark.asyncio
async def test_prepare_daily_puzzle_prefills_hints_and_image(monkeypatch) -> None:
    from app.games.crocodile_daily import prepare_daily_puzzle

    puzzle_date = date(2026, 4, 21)
    puzzle = repo.DailyPuzzle(
        puzzle_date=puzzle_date,
        target_word="крокодил",
        topic="Разное",
        lang="ru",
    )
    temp_msg = SimpleNamespace(
        photo=[SimpleNamespace(file_id="file-123")],
        delete=AsyncMock(),
    )
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=temp_msg))
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                images=[b"image-bytes"],
                error_message="",
                warning="",
                model_used=repo.DAILY_IMAGE_MODEL,
            )
        )
    )

    monkeypatch.setattr("app.config.settings.ADMIN_ID", 999)

    with (
        patch("app.games.crocodile_daily.repo.create_puzzle_if_missing", new_callable=AsyncMock, return_value=puzzle),
        patch("app.games.crocodile_daily.get_daily_hints", new_callable=AsyncMock, return_value=["h1", "h2", "h3"]),
        patch("app.games.crocodile_daily.repo.set_puzzle_image_prompt", new_callable=AsyncMock) as prompt_mock,
        patch("app.games.crocodile_daily.repo.set_puzzle_image_asset", new_callable=AsyncMock) as asset_mock,
        patch("app.games.crocodile_daily.repo.mark_puzzle_prepared", new_callable=AsyncMock) as prepared_mock,
        patch("app.providers.pollinations.get_pollinations_provider", return_value=provider),
    ):
        prepared = await prepare_daily_puzzle(puzzle_date, bot=bot, include_image=True)

    assert prepared.hints == ["h1", "h2", "h3"]
    assert prepared.image_file_id == "file-123"
    assert prepared.prepared_at is not None
    assert "крокодил" in prepared.image_prompt
    provider.generate.assert_awaited_once()
    bot.send_photo.assert_awaited_once()
    temp_msg.delete.assert_awaited_once()
    prompt_mock.assert_awaited_once()
    assert prompt_mock.await_args.kwargs["difficulty"] == "easy"
    asset_mock.assert_awaited_once_with(
        puzzle_date,
        "file-123",
        difficulty="easy",
        image_model=repo.DAILY_IMAGE_MODEL,
    )
    prepared_mock.assert_awaited_once_with(puzzle_date, difficulty="easy")


@pytest.mark.asyncio
async def test_daily_scheduler_skips_sends_when_delivery_disabled() -> None:
    puzzle_date = repo.today_puzzle_date()
    prepared_puzzle = repo.DailyPuzzle(
        puzzle_date=puzzle_date,
        target_word="крокодил",
        topic="Разное",
        lang="ru",
        hints=["h1", "h2", "h3"],
        image_prompt="prompt",
        image_file_id="file-123",
        prepared_at=datetime.now(tz=UTC),
    )
    _bot = object()
    context = SimpleNamespace(bot=_bot, application=SimpleNamespace(bot=_bot))

    with (
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new_callable=AsyncMock, return_value=[prepared_puzzle]) as prep_mock,
        patch("app.games.crocodile_daily.active_daily_difficulties", new_callable=AsyncMock, return_value=["easy"]),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new_callable=AsyncMock, return_value=False),
        patch("app.handlers.daily_crocodile.send_daily_prompt", new_callable=AsyncMock) as prompt_mock,
        patch("app.handlers.daily_crocodile.send_discovery_intro", new_callable=AsyncMock) as intro_mock,
        patch("app.repos.crocodile_daily.get_due_deliveries", new_callable=AsyncMock) as due_mock,
        patch("app.repos.crocodile_daily.get_discovery_candidates", new_callable=AsyncMock) as discovery_mock,
    ):
        await daily_crocodile.check_daily_crocodile_jobs(context)

    prep_mock.assert_awaited_once()
    prompt_mock.assert_not_awaited()
    intro_mock.assert_not_awaited()
    due_mock.assert_not_awaited()
    discovery_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_scheduler_waits_until_puzzle_is_fully_prepared() -> None:
    puzzle_date = repo.today_puzzle_date()
    # A puzzle is "not ready" when hints are absent (image is now optional).
    incomplete_puzzle = repo.DailyPuzzle(
        puzzle_date=puzzle_date,
        target_word="крокодил",
        topic="Разное",
        lang="ru",
        hints=[],           # no hints → not fully prepared
        image_prompt="prompt",
        image_file_id="",   # no image either (irrelevant to readiness now)
    )
    _bot = object()
    context = SimpleNamespace(bot=_bot, application=SimpleNamespace(bot=_bot))

    with (
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new_callable=AsyncMock, return_value=[incomplete_puzzle]),
        patch("app.games.crocodile_daily.active_daily_difficulties", new_callable=AsyncMock, return_value=["easy"]),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.daily_crocodile.send_daily_prompt", new_callable=AsyncMock) as prompt_mock,
        patch("app.handlers.daily_crocodile.send_discovery_intro", new_callable=AsyncMock) as intro_mock,
        patch("app.repos.crocodile_daily.get_due_deliveries", new_callable=AsyncMock) as due_mock,
        patch("app.repos.crocodile_daily.get_discovery_candidates", new_callable=AsyncMock) as discovery_mock,
    ):
        await daily_crocodile.check_daily_crocodile_jobs(context)

    prompt_mock.assert_not_awaited()
    intro_mock.assert_not_awaited()
    due_mock.assert_not_awaited()
    discovery_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_completion_bundle_passes_art_into_single_result_message_when_no_prompt() -> None:
    bot = SimpleNamespace()
    puzzle = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="easy",
        image_file_id="easy-file-id",
    )

    with (
        patch("app.games.crocodile_daily_telegram.send_daily_result_message", new_callable=AsyncMock) as result_mock,
        patch("app.games.crocodile_daily_telegram.render_daily_result_body", new_callable=AsyncMock, return_value=("text", None)),
        patch("app.games.crocodile_daily_telegram.repo.get_preference", new_callable=AsyncMock, return_value={"is_subscribed": False, "last_sent_puzzle_date": None}),
        patch("app.games.crocodile_daily_telegram.repo.get_active_result_message_for_user", new_callable=AsyncMock, return_value=None),
        patch("app.games.crocodile_daily_telegram.repo.get_active_prompt_message", new_callable=AsyncMock, return_value=None),
        patch("app.games.crocodile_daily_telegram.repo.deactivate_other_result_messages", new_callable=AsyncMock),
        patch("app.games.crocodile_daily_telegram.repo.mark_daily_sent", new_callable=AsyncMock),
        patch("app.games.crocodile_daily_telegram.repo.get_puzzle", new_callable=AsyncMock, return_value=puzzle),
    ):
        await crocodile_daily_telegram.send_daily_completion_bundle(bot, 77, date(2026, 4, 21))

    result_mock.assert_awaited_once_with(bot, 77, date(2026, 4, 21), focus_difficulty="easy")


@pytest.mark.asyncio
async def test_send_daily_result_message_uses_photo_caption_when_art_available() -> None:
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(chat_id=77, message_id=501)),
        send_message=AsyncMock(),
    )
    puzzle = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        image_file_id="hard-file-id",
    )

    with (
        patch("app.games.crocodile_daily_telegram.render_daily_result_body", new_callable=AsyncMock, return_value=("hard text", None)),
        patch("app.games.crocodile_daily_telegram.repo.get_puzzle", new_callable=AsyncMock, return_value=puzzle),
        patch("app.games.crocodile_daily_telegram.repo.register_result_message", new_callable=AsyncMock) as register_mock,
    ):
        await crocodile_daily_telegram.send_daily_result_message(
            bot,
            77,
            date(2026, 4, 21),
            focus_difficulty="hard",
        )

    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert bot.send_photo.await_args.kwargs["photo"] == "hard-file-id"
    assert bot.send_photo.await_args.kwargs["caption"] == "hard text"
    register_mock.assert_awaited_once_with(
        user_id=77,
        puzzle_date=date(2026, 4, 21),
        chat_id=77,
        message_id=501,
        rendered_hash_value=repo.render_hash("hard text"),
        message_type="photo",
    )


@pytest.mark.asyncio
async def test_daily_completion_bundle_updates_existing_photo_result() -> None:
    bot = SimpleNamespace(
        edit_message_media=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    puzzle = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        image_file_id="hard-file-id",
    )

    with (
        patch("app.games.crocodile_daily_telegram.render_daily_result_body", new_callable=AsyncMock, return_value=("hard text", None)),
        patch("app.games.crocodile_daily_telegram.repo.get_preference", new_callable=AsyncMock, return_value={"is_subscribed": False, "last_sent_puzzle_date": None}),
        patch("app.games.crocodile_daily_telegram.repo.mark_daily_sent", new_callable=AsyncMock),
        patch(
            "app.games.crocodile_daily_telegram.repo.get_active_result_message_for_user",
            new_callable=AsyncMock,
            return_value={"id": 17, "chat_id": 77, "message_id": 501, "message_type": "photo"},
        ),
        patch("app.games.crocodile_daily_telegram.repo.deactivate_other_result_messages", new_callable=AsyncMock) as dedupe_mock,
        patch("app.games.crocodile_daily_telegram.repo.get_puzzle", new_callable=AsyncMock, return_value=puzzle),
        patch("app.games.crocodile_daily_telegram.repo.update_result_message_hash", new_callable=AsyncMock) as hash_mock,
        patch("app.games.crocodile_daily_telegram.repo.get_active_prompt_message", new_callable=AsyncMock) as prompt_mock,
        patch("app.games.crocodile_daily_telegram._send_daily_completion_art", new_callable=AsyncMock) as art_mock,
        patch("app.games.crocodile_daily_telegram.send_daily_result_message", new_callable=AsyncMock) as result_mock,
    ):
        await crocodile_daily_telegram.send_daily_completion_bundle(
            bot,
            77,
            date(2026, 4, 21),
            focus_difficulty="hard",
        )

    bot.edit_message_media.assert_awaited_once()
    media = bot.edit_message_media.await_args.kwargs["media"]
    assert media.media == "hard-file-id"
    assert media.caption == "hard text"
    dedupe_mock.assert_awaited_once_with(77, date(2026, 4, 21), keep_id=17)
    hash_mock.assert_awaited_once()
    prompt_mock.assert_not_awaited()
    art_mock.assert_not_awaited()
    result_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_completion_art_prepares_missing_image_before_send() -> None:
    bot = SimpleNamespace(send_photo=AsyncMock())
    missing = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        image_file_id="",
    )
    refreshed = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        image_file_id="generated-hard-file",
    )

    with (
        patch("app.games.crocodile_daily_telegram.repo.get_puzzle", new_callable=AsyncMock, return_value=missing),
        patch("app.games.crocodile_daily.prepare_daily_puzzle", new_callable=AsyncMock, return_value=refreshed) as prepare_mock,
    ):
        sent = await crocodile_daily_telegram._send_daily_completion_art(
            bot,
            77,
            date(2026, 4, 21),
            difficulty="hard",
        )

    assert sent is True
    prepare_mock.assert_awaited_once()
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["photo"] == "generated-hard-file"


@pytest.mark.asyncio
async def test_result_refresh_prefers_hard_focus_when_hard_completed() -> None:
    crocodile_daily_telegram.reset_daily_telegram_state_for_tests()
    puzzle_date = date(2026, 4, 21)
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    crocodile_daily_telegram._pending_bots[puzzle_date.isoformat()] = bot

    with (
        patch("app.games.crocodile_daily_telegram.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "app.games.crocodile_daily_telegram.repo.get_active_result_messages",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "user_id": 77, "chat_id": 77, "message_id": 900, "rendered_hash": "old", "message_type": "text"}],
        ),
        patch(
            "app.games.crocodile_daily_telegram.repo.get_results_for_user",
            new_callable=AsyncMock,
            return_value={
                "easy": repo.DailyResult(
                    user_id=77,
                    puzzle_date=puzzle_date,
                    difficulty="easy",
                    status="won",
                    attempts=[{"word": "a"}],
                    best_score=1.0,
                    used_hints_count=0,
                    won_at=None,
                    finished_at=None,
                    points=660,
                    share_grid="🟩",
                    streak_after=1,
                ),
                "hard": repo.DailyResult(
                    user_id=77,
                    puzzle_date=puzzle_date,
                    difficulty="hard",
                    status="won",
                    attempts=[{"word": "b"}],
                    best_score=1.0,
                    used_hints_count=0,
                    won_at=None,
                    finished_at=None,
                    points=660,
                    share_grid="🟩",
                    streak_after=1,
                ),
            },
        ),
        patch("app.games.crocodile_daily_telegram.render_daily_result_body", new_callable=AsyncMock, return_value=("fresh text", None)) as render_mock,
        patch("app.games.crocodile_daily_telegram.repo.update_result_message_hash", new_callable=AsyncMock),
        patch("app.games.crocodile_daily_telegram.repo.deactivate_other_result_messages", new_callable=AsyncMock),
    ):
        await crocodile_daily_telegram._flush_daily_result_refresh(puzzle_date)

    render_mock.assert_awaited_once_with(77, puzzle_date, focus_difficulty="hard")
    crocodile_daily_telegram.reset_daily_telegram_state_for_tests()


@pytest.mark.asyncio
async def test_render_daily_result_body_omits_attempt_suffix_in_leaderboard() -> None:
    puzzle_date = date(2026, 4, 21)
    summary = {
        "focus_difficulty": "easy",
        "next_difficulty": None,
        "modes": {
            "easy": {
                "difficulty": "easy",
                "status": "won",
                "completed": True,
                "attempts": 3,
                "points": 660,
                "streak": 1,
                "rank": 1,
                "leaderboard": [
                    {
                        "user_id": 77,
                        "display_name": "amogus balls",
                        "points": 1320,
                        "status": "won",
                        "attempt_count": 0,
                    }
                ],
            },
            "hard": {
                "difficulty": "hard",
                "status": "won",
                "completed": True,
                "attempts": 3,
                "points": 660,
                "streak": 1,
                "rank": 1,
                "leaderboard": [],
            },
        },
    }

    with (
        patch("app.games.crocodile_daily.build_daily_completion_summary", new_callable=AsyncMock, return_value=summary),
        patch("app.games.crocodile_daily_telegram.repo.get_results_for_user", new_callable=AsyncMock, return_value={}),
        patch("app.games.crocodile_daily_telegram.repo.get_preference", new_callable=AsyncMock, return_value={"is_subscribed": False}),
    ):
        text, _ = await crocodile_daily_telegram.render_daily_result_body(77, puzzle_date, focus_difficulty="easy")

    assert "amogus balls — <b>1320</b>" in text
    assert "0/6" not in text


@pytest.mark.asyncio
async def test_build_daily_completion_summary_uses_aggregate_daily_leaderboard() -> None:
    from app.games import crocodile_daily

    puzzle_date = date(2026, 4, 21)
    easy_puzzle = repo.DailyPuzzle(
        puzzle_date=puzzle_date,
        target_word="телескоп",
        topic="Разное",
        lang="ru",
        difficulty="easy",
    )
    hard_puzzle = repo.DailyPuzzle(
        puzzle_date=puzzle_date,
        target_word="обсерватория",
        topic="Разное",
        lang="ru",
        difficulty="hard",
    )
    easy_result = repo.DailyResult(
        user_id=77,
        puzzle_date=puzzle_date,
        difficulty="easy",
        status="won",
        attempts=[{"word": "a"}],
        best_score=1.0,
        used_hints_count=0,
        won_at=None,
        finished_at=None,
        points=660,
        share_grid="🟩",
        streak_after=1,
    )
    hard_result = repo.DailyResult(
        user_id=77,
        puzzle_date=puzzle_date,
        difficulty="hard",
        status="won",
        attempts=[{"word": "b"}],
        best_score=1.0,
        used_hints_count=0,
        won_at=None,
        finished_at=None,
        points=660,
        share_grid="🟩",
        streak_after=1,
    )
    aggregate_board = [{"user_id": 77, "display_name": "amogus balls", "points": 1320}]

    with (
        patch("app.games.crocodile_daily.active_daily_difficulties", new_callable=AsyncMock, return_value=("easy", "hard")),
        patch(
            "app.games.crocodile_daily.repo.get_puzzles_for_date",
            new_callable=AsyncMock,
            return_value={"easy": easy_puzzle, "hard": hard_puzzle},
        ),
        patch(
            "app.games.crocodile_daily.repo.get_results_for_user",
            new_callable=AsyncMock,
            return_value={"easy": easy_result, "hard": hard_result},
        ),
        patch("app.games.crocodile_daily.repo.get_leaderboard", new_callable=AsyncMock, return_value=aggregate_board) as board_mock,
        patch("app.games.crocodile_daily.repo.get_rank", new_callable=AsyncMock, return_value=1) as rank_mock,
    ):
        summary = await crocodile_daily.build_daily_completion_summary(77, puzzle_date, focus_difficulty="easy")

    board_mock.assert_awaited_once_with(puzzle_date, limit=5)
    rank_mock.assert_awaited_once_with(77, puzzle_date)
    assert summary["modes"]["easy"]["leaderboard"] == aggregate_board
    assert summary["modes"]["hard"]["leaderboard"] == aggregate_board
    assert summary["modes"]["easy"]["rank"] == 1
    assert summary["modes"]["hard"]["rank"] == 1




@pytest.mark.asyncio
async def test_daily_websocket_uses_daily_mode_and_timezone(monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.TELEGRAM_BOT_TOKEN", "test-token")
    init_data = make_valid_init_data("test-token", user_id=777)
    url = f"/webapp/game/daily/ws?initData={urllib.parse.quote(init_data)}&tz=Europe%2FKyiv"
    puzzle = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="крокодил",
        topic="Разное",
        lang="ru",
        hints=[],
    )
    hard_puzzle = repo.DailyPuzzle(
        puzzle_date=date(2026, 4, 21),
        target_word="аллигатор",
        topic="Разное",
        lang="ru",
        difficulty="hard",
        hints=[],
    )
    result = repo.DailyResult(
        user_id=777,
        puzzle_date=date(2026, 4, 21),
        status="active",
        attempts=[],
        best_score=0.0,
        used_hints_count=0,
        won_at=None,
        finished_at=None,
        points=0,
        share_grid="",
        streak_after=0,
    )
    hard_result = repo.DailyResult(
        user_id=777,
        puzzle_date=date(2026, 4, 21),
        difficulty="hard",
        status="active",
        attempts=[],
        best_score=0.0,
        used_hints_count=0,
        won_at=None,
        finished_at=None,
        points=0,
        share_grid="",
        streak_after=0,
    )

    with (
        patch("app.games.crocodile_daily.get_daily_overview", new_callable=AsyncMock) as overview_mock,
        patch("app.games.crocodile_daily.process_daily_guess", new_callable=AsyncMock) as guess_mock,
        patch("app.repos.crocodile_daily.update_timezone_if_known", new_callable=AsyncMock) as timezone_mock,
        patch("app.bot_instance.get_bot", return_value=None),
    ):
        overview_mock.return_value = (
            puzzle.puzzle_date,
            {"easy": puzzle, "hard": hard_puzzle},
            {"easy": result, "hard": hard_result},
        )
        guess_mock.return_value = {
            "event": "result",
            "status": "exact_match",
            "attempts": 1,
            "max_attempts": 6,
            "word": "крокодил",
            "difficulty": "easy",
            "daily_completed": True,
            "leaderboard": [],
            "modes": [
                {"difficulty": "easy", "completed": True, "status": "won"},
                {"difficulty": "hard", "completed": False, "status": "active"},
            ],
            "next_difficulty": "hard",
            "focus_difficulty": "easy",
        }

        async with quart_app.test_client().websocket(url) as ws:
            state = json.loads(await ws.receive())
            assert state["event"] == "game_state"
            assert state["daily"] is True
            assert state["max_attempts"] == 6
            assert state["difficulty"] == "easy"
            assert {item["difficulty"] for item in state["daily_modes"]} == {"easy", "hard"}

            await ws.send(json.dumps({"type": "guess", "word": "крокодил", "pending_id": "p1"}))
            event = json.loads(await ws.receive())
            assert event["daily_completed"] is True
            assert event["pending_id"] == "p1"
            assert event["next_difficulty"] == "hard"

    timezone_mock.assert_awaited_once_with(777, "Europe/Kyiv")
