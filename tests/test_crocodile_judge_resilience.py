"""Crocodile must recover provider failures without resubmitting a player's guess."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai.errors import ClientError, ServerError

from app.games import crocodile_daily, daily_ai, judge, judgement_cache
from app.process_policies import ResolvedPolicy
from app.repos import crocodile_daily as repo

MODEL = "gemini-3.5-flash-lite"
VALID_RESPONSE = '{"status":"warm","score":0.6,"hint":"Есть сходство"}'


@pytest.fixture(autouse=True)
def offline_judge(monkeypatch):
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value=MODEL))
    monkeypatch.setattr(
        daily_ai, "resolve_process", AsyncMock(return_value=ResolvedPolicy((MODEL,), "sequential", False, 0))
    )
    monkeypatch.setattr(judge, "cache_identity", AsyncMock(return_value="test-judge"))
    monkeypatch.setattr(judgement_cache, "get_cached_judgement", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "cache_judgement", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ServerError(503, {"error": {"message": "UNAVAILABLE"}}),
        httpx.ReadTimeout("provider timed out"),
        '{"score":0.6}',
        "not json",
    ],
)
async def test_selected_judge_recovers_same_guess_after_transient_or_invalid_response(monkeypatch, failure):
    attempts = []

    async def generate(prompt, model, timeout, **kwargs):
        attempts.append((prompt, model))
        if len(attempts) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return VALID_RESPONSE

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    status, result = await judge.judge_guess("кот", "тигр", category="животные")

    assert status == "warm"
    assert result.score == 0.6
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_selected_judge_does_not_retry_permanent_client_errors(monkeypatch, code):
    attempts = []

    async def generate(prompt, model, timeout, **kwargs):
        attempts.append(model)
        if len(attempts) == 1:
            raise ClientError(code, {"error": {"message": "Invalid request"}})
        return VALID_RESPONSE

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    status, _ = await judge.judge_guess("кот", "тигр")

    assert status == "judge_unavailable"
    assert attempts == [MODEL]


@pytest.mark.asyncio
async def test_selected_judge_exhausted_retries_never_count_daily_attempt(monkeypatch):
    puzzle = repo.DailyPuzzle(date(2026, 10, 3), "кот", "животные", "ru")
    result = SimpleNamespace(status="active", attempts=[])
    saved_attempts = []
    calls = []

    async def generate(prompt, model, timeout, **kwargs):
        calls.append(model)
        raise TimeoutError

    async def append_attempt(**kwargs):
        saved_attempts.append(kwargs["attempt"])

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    monkeypatch.setattr(crocodile_daily, "get_daily_state", AsyncMock(return_value=(puzzle, result)))
    monkeypatch.setattr(repo, "append_attempt_and_maybe_finish", append_attempt)

    event = await crocodile_daily.process_daily_guess(123, "тигр")

    assert event["event"] == "judge_unavailable"
    assert saved_attempts == []
    assert calls == [MODEL, MODEL]


@pytest.mark.asyncio
async def test_daily_recovery_records_only_one_game_attempt(monkeypatch):
    puzzle = repo.DailyPuzzle(date(2026, 10, 3), "кот", "животные", "ru")
    attempts = []
    result = SimpleNamespace(status="active", attempts=attempts, best_score=0.0)
    calls = []

    async def generate(prompt, model, timeout, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            return '{"score":0.6}'
        return VALID_RESPONSE

    async def append_attempt(**kwargs):
        attempts.append(kwargs["attempt"])
        result.best_score = kwargs["attempt"]["score"]
        return result

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    monkeypatch.setattr(crocodile_daily, "get_daily_state", AsyncMock(return_value=(puzzle, result)))
    monkeypatch.setattr(repo, "append_attempt_and_maybe_finish", append_attempt)

    event = await crocodile_daily.process_daily_guess(123, "тигр")

    assert event["event"] == "result"
    assert event["attempts"] == 1
    assert event["score"] == 0.6
    assert [attempt["word"] for attempt in attempts] == ["тигр"]
    assert calls == [MODEL, MODEL]


@pytest.mark.asyncio
async def test_selected_judge_sends_schema_to_google_sdk(monkeypatch):
    from app.providers import gemini

    async def generate_content(*, model, contents, config):
        # A JSON-only request still permits missing fields. The actual API schema
        # must constrain the result, rather than only asking for it in the prompt.
        text = VALID_RESPONSE if config.response_schema is not None else '{"score":0.6}'
        return SimpleNamespace(text=text)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(gemini, "get_cached_genai_client", lambda key: client)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": "test-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))

    status, result = await judge.judge_guess("кот", "тигр")

    assert status == "warm"
    assert result.score == 0.6


@pytest.mark.asyncio
async def test_selected_chain_reaches_reserve_after_primary_times_out(monkeypatch):
    monkeypatch.setattr(
        daily_ai,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy((MODEL, "gemini-3.6-flash"), "sequential", True, 1)),
    )
    # Scale the real deadlines together; the primary must expire before the
    # enclosing chain, so the configured reserve can still answer.
    monkeypatch.setattr(
        daily_ai,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "timeout": lambda delay: asyncio.timeout(delay / 100)}),
    )
    attempted = []
    primary_stopped = asyncio.Event()

    async def generate(prompt, model, timeout, **kwargs):
        attempted.append(model)
        if model == MODEL:
            try:
                async with daily_ai.asyncio.timeout(timeout):
                    await asyncio.Event().wait()
            finally:
                primary_stopped.set()
        return VALID_RESPONSE

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    status, result = await judge.judge_guess("кот", "тигр")

    assert status == "warm"
    assert result.score == 0.6
    assert attempted == [MODEL, "gemini-3.6-flash"]
    assert primary_stopped.is_set()


@pytest.mark.asyncio
async def test_auto_judge_retries_its_existing_model_lane(monkeypatch):
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value=""))
    attempts = []

    async def race(target, guess, **kwargs):
        attempts.append((target, guess, kwargs))
        if len(attempts) == 1:
            return None
        return judge.GuessJudgement(status="warm", score=0.6, hint="Есть сходство")

    monkeypatch.setattr(judge, "_race_generate", race)
    status, result = await judge.judge_guess("кот", "тигр", category="животные")

    assert status == "warm"
    assert result.score == 0.6
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
async def test_total_deadline_is_not_restarted_for_retry(monkeypatch):
    timeouts = []
    retry_started = asyncio.Event()
    retry_stopped = asyncio.Event()
    attempts = []

    def timeout(delay):
        context = asyncio.timeout(delay)
        timeouts.append(context)
        return context

    monkeypatch.setattr(judge, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": timeout}))

    async def generate(prompt, model, timeout, **kwargs):
        attempts.append(model)
        if len(attempts) == 1:
            raise TimeoutError
        retry_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retry_stopped.set()

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    task = asyncio.create_task(judge.judge_guess("кот", "тигр"))
    try:
        await asyncio.wait_for(retry_started.wait(), 2)
        assert len(timeouts) == 1
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        status, _ = await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert status == "judge_unavailable"
    assert retry_stopped.is_set()
    assert attempts == [MODEL, MODEL]


@pytest.mark.asyncio
async def test_caller_cancellation_stops_generation_without_retry(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    attempted = []

    async def generate(prompt, model, timeout, **kwargs):
        attempted.append(model)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    task = asyncio.create_task(judge.judge_guess("кот", "тигр"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.is_set()
    assert attempted == [MODEL]
