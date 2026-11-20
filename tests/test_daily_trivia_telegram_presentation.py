from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram import InlineKeyboardMarkup, Message
from telegram.constants import ParseMode

from app.games import cover_photo, daily_trivia_telegram
from app.games.daily_trivia_telegram import render_result_body
from app.handlers import daily_trivia
from app.handlers.daily_trivia import trivia_monthly_champions_callback
from app.repos.daily_trivia import DailyTriviaResult


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_fails", [False, True])
async def test_completed_command_keeps_reply_context_after_photo_fallback(monkeypatch, photo_fails):
    day = date(2026, 9, 23)
    keyboard = InlineKeyboardMarkup([])
    sent = SimpleNamespace(chat_id=7, message_id=42)
    bot = SimpleNamespace(
        send_photo=AsyncMock(return_value=sent, side_effect=RuntimeError("photo unavailable") if photo_fails else None),
        send_message=AsyncMock(return_value=sent),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_message=SimpleNamespace(message_id=10))
    monkeypatch.setattr(daily_trivia, "today_puzzle_date", lambda now: day)
    monkeypatch.setattr(daily_trivia, "prepare_daily_puzzle", AsyncMock())
    monkeypatch.setattr(daily_trivia.daily_delivery_repo, "get_preference", AsyncMock(return_value=None))
    monkeypatch.setattr(
        daily_trivia.trivia_repo, "get_result_if_exists", AsyncMock(return_value=SimpleNamespace(status="completed"))
    )
    register_prompt = AsyncMock()
    monkeypatch.setattr(daily_trivia.trivia_repo, "register_prompt_message", register_prompt)
    monkeypatch.setattr(daily_trivia_telegram, "render_result_body", AsyncMock(return_value=("Result", keyboard)))
    monkeypatch.setattr(cover_photo, "get_cover_photo", AsyncMock(return_value="cover-id"))
    remember_cover = AsyncMock()
    monkeypatch.setattr(cover_photo, "remember_cover_file_id", remember_cover)

    await daily_trivia.daily_trivia_command(update, SimpleNamespace(bot=bot))

    expected = {"chat_id": 7, "parse_mode": ParseMode.HTML, "reply_markup": keyboard, "reply_to_message_id": 10}
    bot.send_photo.assert_awaited_once_with(photo="cover-id", caption="Result", **expected)
    if photo_fails:
        bot.send_message.assert_awaited_once_with(text="Result", **expected)
        remember_cover.assert_not_awaited()
    else:
        bot.send_message.assert_not_awaited()
        remember_cover.assert_awaited_once_with("dailytrivia", sent)
    register_prompt.assert_awaited_once_with(user_id=7, puzzle_date=day, chat_id=7, message_id=42)


@pytest.mark.asyncio
@pytest.mark.parametrize("month, label", [(1, "Январь 2026"), (9, "Сентябрь 2026"), (12, "Декабрь 2026")])
async def test_result_month_label_is_russian_without_process_locale(month, label):
    day = date(2026, month, 23)
    result = DailyTriviaResult(
        user_id=7,
        puzzle_date=day,
        status="completed",
        current_question=5,
        correct_count=4,
        final_score=860,
        elapsed_ms=90000,
        answers=[],
        started_at=datetime(2026, month, 23, tzinfo=UTC),
        finished_at=None,
    )
    with (
        patch("app.games.daily_trivia_telegram.repo.get_or_create_result", new=AsyncMock(return_value=result)),
        patch("app.games.daily_trivia_telegram.repo.get_daily_leaderboard", new=AsyncMock(return_value=[])),
        patch(
            "app.games.daily_trivia_telegram.repo.get_monthly_leaderboard",
            new=AsyncMock(return_value=[{"user_id": 7, "name": "Alice", "score": 1720, "games_played": 2}]),
        ),
    ):
        text, _ = await render_result_body(7, day)
    assert label in text


@pytest.mark.asyncio
async def test_monthly_correct_count_uses_all_five_question_days():
    message = AsyncMock(spec=Message)
    query = SimpleNamespace(data="dailytrivia:month:2026-09", answer=AsyncMock(), message=message)
    update = SimpleNamespace(callback_query=query)
    with patch(
        "app.handlers.daily_trivia.trivia_repo.get_monthly_leaderboard",
        new=AsyncMock(return_value=[{"user_id": 7, "name": "Alice", "score": 1720, "correct": 8, "games_played": 2}]),
    ):
        await trivia_monthly_champions_callback(update, None)
    text = message.reply_text.await_args.args[0]
    assert "8/10" in text
    assert "8/5" not in text
