from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Message

from app.games.daily_trivia_telegram import render_result_body
from app.handlers.daily_trivia import trivia_monthly_champions_callback
from app.repos.daily_trivia import DailyTriviaResult


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
