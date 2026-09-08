from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import daily_ai, word_bank
from app.repos import crocodile_daily as repo


@pytest.mark.asyncio
@pytest.mark.parametrize("regenerate", [False, True])
@pytest.mark.parametrize("collision", [False, True])
async def test_ai_candidate_generated_before_transaction_and_rechecked(monkeypatch, regenerate, collision):
    state = {"transaction": False, "used": {"старое"}, "generated": False}
    day = date(2026, 9, 8)
    old = repo.DailyPuzzle(day, "старое", "Животные", "ru")

    @asynccontextmanager
    async def transaction():
        assert state["generated"], "AI must finish before opening the table transaction"
        state["transaction"] = True
        if collision:
            state["used"].add("кот")
        try:
            yield
        finally:
            state["transaction"] = False

    conn = SimpleNamespace(transaction=transaction, execute=AsyncMock())

    @asynccontextmanager
    async def acquire():
        yield conn

    async def get_puzzle(*args, **kwargs):
        return old if regenerate else None

    async def used(**kwargs):
        return set(state["used"])

    async def generate(*args):
        assert not state["transaction"]
        state["generated"] = True
        return "кот"

    async def fallback(topic, **kwargs):
        assert "кот" in kwargs["used_words"]
        return "собака", "ru", topic.category, False

    async def query(sql, params=(), **kwargs):
        assert state["transaction"]
        word = params[0] if "UPDATE" in sql else params[2]
        assert word not in state["used"], "candidate uniqueness must be checked after locking"
        return [{"puzzle_date": day, "target_word": word, "topic": "Животные", "lang": "ru"}]

    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire, _closed=False))
    monkeypatch.setattr(repo, "get_puzzle", get_puzzle)
    monkeypatch.setattr(repo, "get_used_daily_words", used)
    monkeypatch.setattr(repo, "ensure_puzzle_day", AsyncMock())
    monkeypatch.setattr(repo.db, "db_query", query)
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.6-flash"))
    monkeypatch.setattr(daily_ai, "generate_daily_word", generate)
    monkeypatch.setattr(word_bank, "pick_random_word_for_topic", fallback)
    monkeypatch.setattr("app.games.crocodile_daily.get_daily_image_model", AsyncMock(return_value="pollinations"))

    result = await (repo.regenerate_puzzle_word(day) if regenerate else repo.create_puzzle_if_missing(day))
    assert result.target_word == ("собака" if collision else "кот")
