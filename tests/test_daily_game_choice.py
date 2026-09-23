from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.games import crocodile_daily, daily_2048, daily_trivia
from app.handlers import daily_crocodile
from app.handlers.daily_2048 import daily2048_entry_keyboard
from app.handlers.daily_trivia import daily_trivia_keyboard
from app.repos import crocodile_daily as delivery_repo
from app.repos import daily_2048 as mode_repo
from app.web import quart_app


def test_personal_daily_game_overrides_admin_default_without_changing_subscription() -> None:
    preference = {"daily_game": "trivia", "is_subscribed": False}
    assert mode_repo.resolve_daily_game_mode(preference, "2048") == "trivia"
    assert mode_repo.resolve_daily_game_mode({"daily_game": None}, "2048") == "2048"
    assert mode_repo.resolve_daily_game_mode({"daily_game": "invalid"}, "2048") == "2048"


@pytest.mark.asyncio
async def test_due_delivery_filters_by_personal_game_before_limit(monkeypatch) -> None:
    captured = {}

    async def fake_query(query, params=(), **kwargs):
        captured["query"] = query
        captured["params"] = params
        return [{"user_id": 7, "timezone": "Europe/Kyiv", "preferred_local_hour": 13}]

    monkeypatch.setattr(delivery_repo.db, "db_query", fake_query)
    due = await delivery_repo.get_due_deliveries(
        puzzle_date=date(2026, 9, 23),
        now=datetime(2026, 9, 23, 14, tzinfo=UTC),
        game_mode="trivia",
        default_game_mode="crocodile",
    )
    assert [row["user_id"] for row in due] == [7]
    assert "COALESCE(daily_game" in captured["query"]
    assert captured["params"] == (date(2026, 9, 23), 500, "trivia", "crocodile")


@pytest.mark.asyncio
async def test_choice_api_rejects_anonymous_and_invalid_mode() -> None:
    with patch("app.web_miniapp._resolve_authorized_legacy_miniapp_user", new=AsyncMock(return_value=(0, None))):
        response = await quart_app.test_client().get("/webapp/api/daily-game")
    assert response.status_code == 401

    with patch("app.web_miniapp._resolve_authorized_legacy_miniapp_user", new=AsyncMock(return_value=(7, None))):
        response = await quart_app.test_client().patch("/webapp/api/daily-game", json={"game": "chess"})
    assert response.status_code == 400

    with patch("app.web_miniapp._resolve_authorized_legacy_miniapp_user", new=AsyncMock(return_value=(7, None))):
        malformed = await quart_app.test_client().patch("/webapp/api/daily-game", json=["trivia"])
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_choice_api_persists_only_game_selection() -> None:
    with (
        patch("app.web_miniapp._resolve_authorized_legacy_miniapp_user", new=AsyncMock(return_value=(7, None))),
        patch(
            "app.web_miniapp.daily_delivery_repo.upsert_preference",
            new=AsyncMock(return_value={"daily_game": "2048", "is_subscribed": True}),
        ) as save,
        patch("app.web_miniapp.daily_2048_repo.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
    ):
        response = await quart_app.test_client().patch("/webapp/api/daily-game", json={"game": "2048"})
    assert response.status_code == 200
    assert (await response.get_json())["game"] == "2048"
    save.assert_awaited_once_with(7, daily_game="2048")


@pytest.mark.asyncio
async def test_daily_command_opens_personal_game_when_admin_default_differs() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=7),
        message=SimpleNamespace(message_id=3),
    )
    context = SimpleNamespace(bot=object())
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
        patch(
            "app.handlers.daily_crocodile.repo.get_preference",
            new=AsyncMock(return_value={"daily_game": "2048", "is_subscribed": True}),
        ),
        patch("app.repos.daily_2048.ensure_puzzle", new=AsyncMock()),
        patch("app.repos.daily_2048.get_result", new=AsyncMock(return_value=None)),
        patch("app.handlers.daily_2048.send_daily2048_entry", new=AsyncMock()) as send,
    ):
        await daily_crocodile.dailycroc_command.__wrapped__(update, context)
    assert send.await_args.args[1] == 7
    assert send.await_args.kwargs["include_subscribe"] is False


@pytest.mark.asyncio
async def test_subscribe_from_game_entry_preserves_that_game() -> None:
    assert "dailycroc:subscribe:2048" in [
        button.callback_data for row in daily2048_entry_keyboard().inline_keyboard for button in row
    ]
    assert "dailycroc:subscribe:trivia" in [
        button.callback_data for row in daily_trivia_keyboard(include_subscribe=True).inline_keyboard for button in row
    ]

    query = SimpleNamespace(data="dailycroc:subscribe:trivia", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7))
    with (
        patch("app.handlers.daily_crocodile.repo.upsert_preference", new=AsyncMock()) as save,
        patch("app.handlers.daily_crocodile._edit_callback_text", new=AsyncMock()) as edit,
    ):
        await daily_crocodile.daily_subscribe_callback(update, SimpleNamespace())
    save.assert_awaited_once_with(7, daily_game="trivia")
    assert "игру" in edit.await_args.args[1]
    assert "Крокодила" not in edit.await_args.args[1]


def test_each_bot_game_entry_offers_a_change_game_button() -> None:
    from app.handlers.daily_crocodile import daily_play_keyboard

    for keyboard in (
        daily_play_keyboard(),
        daily2048_entry_keyboard(),
        daily_trivia_keyboard(),
    ):
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        assert "dailycroc:choose" in callbacks


@pytest.mark.asyncio
async def test_completed_2048_player_sees_current_result_and_leaderboard_from_daily_menu() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=7),
        message=SimpleNamespace(message_id=3),
    )
    keyboard = daily2048_entry_keyboard()
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value="2048")),
        patch("app.handlers.daily_crocodile.repo.get_preference", new=AsyncMock(return_value={"daily_game": "2048"})),
        patch("app.repos.daily_2048.ensure_puzzle", new=AsyncMock()),
        patch("app.repos.daily_2048.get_result", new=AsyncMock(return_value=SimpleNamespace(status="won"))),
        patch(
            "app.games.daily_2048_telegram.render_result_body",
            new=AsyncMock(return_value=("Мой результат · Лучшие сегодня", keyboard)),
        ),
        patch("app.handlers.daily_2048.send_daily2048_entry", new=AsyncMock()) as entry,
    ):
        await daily_crocodile.dailycroc_command.__wrapped__(update, context)
    assert "Мой результат" in bot.send_message.await_args.kwargs["text"]
    assert "Лучшие сегодня" in bot.send_message.await_args.kwargs["text"]
    entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_crocodile_player_sees_result_and_leaderboard_from_daily_menu() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=7),
        message=SimpleNamespace(message_id=3),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    context = SimpleNamespace(bot=bot)
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
        patch(
            "app.handlers.daily_crocodile.repo.get_preference", new=AsyncMock(return_value={"daily_game": "crocodile"})
        ),
        patch("app.handlers.daily_crocodile.repo.record_player_activity", new=AsyncMock()),
        patch(
            "app.handlers.daily_crocodile.repo.get_results_for_user",
            new=AsyncMock(return_value={"easy": SimpleNamespace(status="won")}),
        ),
        patch(
            "app.games.crocodile_daily_telegram.render_daily_result_body",
            new=AsyncMock(return_value=("Мой результат · Лидерборд", daily_crocodile.daily_play_keyboard())),
        ),
        patch("app.handlers.daily_crocodile._send_daily_entry_message", new=AsyncMock()) as entry,
    ):
        await daily_crocodile.dailycroc_command.__wrapped__(update, context)
    assert "Мой результат" in bot.send_message.await_args.kwargs["text"]
    assert "Лидерборд" in bot.send_message.await_args.kwargs["text"]
    entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_choice_callback_saves_selection_and_opens_chosen_game() -> None:
    query = SimpleNamespace(data="dailycroc:game:trivia", answer=AsyncMock(), message=SimpleNamespace(message_id=9))
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(id=7)
    )
    context = SimpleNamespace(bot=object())
    with (
        patch(
            "app.handlers.daily_crocodile.repo.upsert_preference", new=AsyncMock(return_value={"daily_game": "trivia"})
        ) as save,
        patch("app.handlers.daily_crocodile.send_daily_menu_for_user", new=AsyncMock()) as send,
    ):
        await daily_crocodile.daily_game_choice_callback(update, context)
    save.assert_awaited_once_with(7, daily_game="trivia")
    send.assert_awaited_once()
    assert send.await_args.kwargs["game_mode"] == "trivia"


@pytest.mark.asyncio
async def test_group_change_game_button_points_player_to_private_chat() -> None:
    query = SimpleNamespace(data="dailycroc:choose", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=-100, type="group"),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    with patch("app.handlers.daily_crocodile.repo.get_preference", new=AsyncMock()) as preference:
        await daily_crocodile.daily_game_choose_callback(update, context)
    preference.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_trivia_result_shows_player_others_and_game_choice() -> None:
    from app.games.daily_trivia_telegram import render_result_body
    from app.repos.daily_trivia import DailyTriviaResult

    day = date(2026, 9, 23)
    result = DailyTriviaResult(
        user_id=7,
        puzzle_date=day,
        status="completed",
        current_question=5,
        correct_count=4,
        final_score=860,
        elapsed_ms=90000,
        answers=[],
        started_at=datetime(2026, 9, 23, tzinfo=UTC),
        finished_at=datetime(2026, 9, 23, tzinfo=UTC),
    )
    leaders = [
        {"user_id": 7, "name": "Alice", "score": 860, "correct": 4},
        {"user_id": 8, "name": "Bob", "score": 730, "correct": 3},
    ]
    with (
        patch("app.games.daily_trivia_telegram.repo.get_or_create_result", new=AsyncMock(return_value=result)),
        patch("app.games.daily_trivia_telegram.repo.get_daily_leaderboard", new=AsyncMock(return_value=leaders)),
        patch("app.games.daily_trivia_telegram.repo.get_monthly_leaderboard", new=AsyncMock(return_value=[])),
    ):
        text, keyboard = await render_result_body(7, day)
    assert "860" in text and "Alice" in text and "Bob" in text
    assert "dailycroc:choose" in [button.callback_data for row in keyboard.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_crocodile_result_album_has_separate_game_choice_action() -> None:
    from app.games import crocodile_daily_telegram
    from app.repos.crocodile_daily import DailyPuzzle

    day = date(2026, 9, 23)
    bot = SimpleNamespace(
        send_media_group=AsyncMock(return_value=[SimpleNamespace(chat_id=7, message_id=101)]),
        send_message=AsyncMock(),
    )
    keyboard = daily_crocodile.daily_play_keyboard(include_subscribe=False)

    async def art(_bot, _user_id, _day, *, difficulty):
        return DailyPuzzle(day, "word", "topic", "ru", difficulty=difficulty, image_file_id=f"{difficulty}-art")

    with (
        patch(
            "app.games.crocodile_daily_telegram.render_daily_result_body",
            new=AsyncMock(return_value=("Daily result", keyboard)),
        ),
        patch("app.games.crocodile_daily_telegram.repo.get_preference", new=AsyncMock(return_value=None)),
        patch("app.games.crocodile_daily_telegram.repo.mark_daily_sent", new=AsyncMock()),
        patch(
            "app.games.crocodile_daily_telegram.repo.get_active_result_message_for_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.games.crocodile_daily_telegram.repo.get_results_for_user",
            new=AsyncMock(return_value={"easy": SimpleNamespace(status="won"), "hard": SimpleNamespace(status="won")}),
        ),
        patch("app.games.crocodile_daily_telegram._load_completion_puzzle_with_art", new=art),
        patch("app.games.crocodile_daily_telegram.repo.register_result_message", new=AsyncMock()),
    ):
        await crocodile_daily_telegram.send_daily_completion_bundle(bot, 7, day)

    bot.send_media_group.assert_awaited_once()
    assert "dailycroc:choose" in [
        button.callback_data
        for row in bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]


@pytest.mark.asyncio
async def test_non_admin_games_prepare_today_and_tomorrow(monkeypatch) -> None:
    prepared_2048 = []
    prepared_trivia = []

    async def ensure_2048(day):
        prepared_2048.append(day)
        return day

    async def ensure_trivia(day):
        prepared_trivia.append(day)
        return day

    monkeypatch.setattr(daily_2048.repo, "ensure_puzzle", ensure_2048)
    monkeypatch.setattr(daily_trivia, "prepare_daily_puzzle", ensure_trivia)
    monkeypatch.setattr(daily_trivia, "_is_preparation_cooling_down", AsyncMock(return_value=False))
    monkeypatch.setattr(daily_trivia, "_clear_preparation_cooldown", AsyncMock())
    await daily_2048.ensure_prepared_puzzles(now=datetime(2026, 9, 23, tzinfo=UTC), days_ahead=1)
    await daily_trivia.ensure_prepared_puzzles(now=datetime(2026, 9, 23, tzinfo=UTC), days_ahead=1)
    assert prepared_2048 == [date(2026, 9, 23), date(2026, 9, 24)]
    assert prepared_trivia == [date(2026, 9, 23), date(2026, 9, 24)]


@pytest.mark.asyncio
async def test_non_admin_crocodile_prepares_two_days(monkeypatch) -> None:
    prepared = []

    async def prepare(day, *, bot, difficulty, include_image):
        prepared.append((day, difficulty, include_image))
        return day

    monkeypatch.setattr(crocodile_daily, "active_daily_difficulties", AsyncMock(return_value=["easy", "hard"]))
    monkeypatch.setattr(crocodile_daily, "prepare_daily_puzzle", prepare)
    await crocodile_daily.ensure_prepared_puzzles(now=datetime(2026, 9, 23, tzinfo=UTC), days_ahead=1)
    assert prepared == [
        (date(2026, 9, 23), "easy", True),
        (date(2026, 9, 23), "hard", True),
        (date(2026, 9, 24), "easy", True),
        (date(2026, 9, 24), "hard", True),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_mode,croc_horizon", [("crocodile", 7), ("trivia", 1), ("2048", 1)])
async def test_scheduler_prepares_every_game_with_admin_horizon(admin_mode, croc_horizon) -> None:
    context = SimpleNamespace(bot=object(), application=object())
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value=admin_mode)),
        patch("app.handlers.daily_2048.check_daily_2048_jobs", new=AsyncMock()) as check_2048,
        patch("app.handlers.daily_trivia.check_daily_trivia_jobs", new=AsyncMock()) as check_trivia,
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new=AsyncMock(return_value=[])) as prepare_croc,
        patch("app.games.crocodile_daily.active_daily_difficulties", new=AsyncMock(return_value=[])),
        patch("app.handlers.daily_crocodile.repo.get_puzzles_for_date", new=AsyncMock(return_value={})),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new=AsyncMock(return_value=False)),
    ):
        await daily_crocodile.check_daily_crocodile_jobs(context)
    check_2048.assert_awaited_once_with(context, admin_mode=admin_mode)
    check_trivia.assert_awaited_once_with(context, admin_mode=admin_mode)
    assert prepare_croc.await_args.kwargs["days_ahead"] == croc_horizon


@pytest.mark.asyncio
async def test_daily_scheduler_starts_all_game_preparation_before_waiting_for_slow_provider() -> None:
    other_started = asyncio.Event()
    croc_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_other(*args, **kwargs):
        other_started.set()
        await release.wait()

    async def prepare_croc(*args, **kwargs):
        croc_started.set()
        return []

    context = SimpleNamespace(bot=object(), application=object())
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
        patch("app.handlers.daily_2048.check_daily_2048_jobs", new=slow_other),
        patch("app.handlers.daily_trivia.check_daily_trivia_jobs", new=AsyncMock()),
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new=prepare_croc),
        patch("app.games.crocodile_daily.active_daily_difficulties", new=AsyncMock(return_value=[])),
        patch("app.handlers.daily_crocodile.repo.get_puzzles_for_date", new=AsyncMock(return_value={})),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new=AsyncMock(return_value=False)),
    ):
        task = asyncio.create_task(daily_crocodile.check_daily_crocodile_jobs(context))
        try:
            await asyncio.wait_for(other_started.wait(), 0.2)
            await asyncio.wait_for(croc_started.wait(), 0.2)
        finally:
            release.set()
            await task


@pytest.mark.asyncio
async def test_one_daily_job_failure_does_not_prevent_other_games_from_preparing() -> None:
    context = SimpleNamespace(bot=object(), application=object())
    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new=AsyncMock(return_value="crocodile")),
        patch("app.handlers.daily_2048.check_daily_2048_jobs", new=AsyncMock(side_effect=RuntimeError("offline"))),
        patch("app.handlers.daily_trivia.check_daily_trivia_jobs", new=AsyncMock()) as trivia,
        patch("app.games.crocodile_daily.ensure_prepared_puzzles", new=AsyncMock(return_value=[])) as croc,
        patch("app.games.crocodile_daily.active_daily_difficulties", new=AsyncMock(return_value=[])),
        patch("app.handlers.daily_crocodile.repo.get_puzzles_for_date", new=AsyncMock(return_value={})),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new=AsyncMock(return_value=False)),
    ):
        await daily_crocodile.check_daily_crocodile_jobs(context)
    trivia.assert_awaited_once()
    croc.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,expected",
    [
        ("/webapp/game?game_id=daily", True),
        ("/webapp/game?game_id=daily-hard", True),
        ("/webapp/daily2048", True),
        ("/webapp/dailytrivia", True),
        ("/webapp/game?game_id=ordinary", False),
    ],
)
async def test_daily_game_pages_render_keyboard_accessible_choice(url, expected) -> None:
    response = await quart_app.test_client().get(url)
    assert response.status_code == 200
    markup = await response.get_data(as_text=True)
    assert ('aria-haspopup="dialog"' in markup) is expected
    assert ('id="daily-choice-dialog"' in markup) is expected
    assert ('id="daily-choice-status"' in markup) is expected


@pytest.mark.asyncio
async def test_startup_schema_requires_daily_game_column() -> None:
    from app.db.schema import EXPECTED_COLUMNS, EXPECTED_TABLES, SchemaValidationError, validate_schema

    async def fake_query(query, params=()):
        if "pg_tables" in query:
            return [{"tablename": table} for table in EXPECTED_TABLES]
        return [
            {"table_name": table, "column_name": column}
            for table, columns in EXPECTED_COLUMNS.items()
            for column in columns
            if column != "daily_game"
        ]

    with pytest.raises(SchemaValidationError, match="daily_game"):
        await validate_schema(fake_query)


@pytest.mark.asyncio
async def test_trivia_load_failure_has_a_retry_action() -> None:
    response = await quart_app.test_client().get("/webapp/dailytrivia")
    markup = await response.get_data(as_text=True)
    assert 'id="retryBtn"' in markup
    assert "Повторить загрузку" in markup
