"""Daily generation recovers transient failures within bounded quota and time."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import ServerError

from app.games import daily_ai
from app.process_policies import ResolvedPolicy


@pytest.fixture
def provider(monkeypatch):
    attempts = []
    health = SimpleNamespace(suspend_key=AsyncMock(), record_success=AsyncMock())
    first = {"api_key": "test-first", "key_hash": "first"}
    second = {"api_key": "test-second", "key_hash": "second"}
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", AsyncMock(return_value=first))
    reserve = AsyncMock(return_value=True)
    replacement = AsyncMock(return_value=second)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr("app.repos.keys.reserve_available_gemini_key", replacement)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)

    def install(generate):
        def client(key):
            async def call(**kwargs):
                attempts.append((key, kwargs["model"]))
                return await generate(key)

            return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call)))

        monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", client)

    return SimpleNamespace(install=install, attempts=attempts, health=health, reserve=reserve, replacement=replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ServerError(503, {"error": {"message": "UNAVAILABLE"}})])
async def test_transient_failure_recovers_with_reserved_replacement(provider, failure):
    async def generate(key):
        if key == "test-first":
            raise failure
        return SimpleNamespace(text='{"word":"телескоп"}')

    provider.install(generate)
    assert await daily_ai.generate_daily_text("prompt", "gemini-test") == '{"word":"телескоп"}'
    assert provider.attempts == [("test-first", "gemini-test"), ("test-second", "gemini-test")]
    provider.reserve.assert_awaited_once_with("first", "gemini-test")
    provider.replacement.assert_awaited_once_with("gemini-test", excluded_hashes={"first"})
    provider.health.suspend_key.assert_awaited_once()
    provider.health.record_success.assert_awaited_once_with("second", "gemini-test")


@pytest.mark.asyncio
async def test_sdk_deadline_is_cooled_down_before_recovery(provider, monkeypatch):
    monkeypatch.setattr(daily_ai, "_TEXT_RETRY_DELAY_S", 0.001, raising=False)
    cancelled = asyncio.Event()

    async def generate(key):
        if key == "test-first":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return SimpleNamespace(text='{"ok":true}')

    provider.install(generate)
    assert await daily_ai.generate_daily_text("prompt", "gemini-test", timeout=0.1) == '{"ok":true}'
    assert cancelled.is_set()
    assert provider.health.suspend_key.await_args.args[2] == "transient"


@pytest.mark.asyncio
async def test_repeated_transient_failure_stops_after_two_dispatches(provider, monkeypatch):
    monkeypatch.setattr(daily_ai, "_TEXT_RETRY_DELAY_S", 0.001, raising=False)

    async def generate(key):
        raise TimeoutError

    provider.install(generate)
    with pytest.raises(TimeoutError):
        await daily_ai.generate_daily_text("prompt", "gemini-test")
    assert len(provider.attempts) == 2


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_penalize_or_retry(provider):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def generate(key):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.install(generate)
    task = asyncio.create_task(daily_ai.generate_daily_text("prompt", "gemini-test"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert len(provider.attempts) == 1
    provider.health.suspend_key.assert_not_awaited()
    provider.replacement.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_chain_preserves_time_for_reserve_model(monkeypatch):
    monkeypatch.setattr(
        daily_ai,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first", "gemini-second"), "sequential", True)),
    )
    attempts = []

    async def generate(prompt, model, timeout):
        attempts.append(model)
        if model == "gemini-first":
            async with asyncio.timeout(timeout):
                await asyncio.Event().wait()
        return '{"word":"телескоп"}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert (
        await daily_ai.generate_daily_text_for("words", "prompt", "gemini-first", timeout=0.1) == '{"word":"телескоп"}'
    )
    assert attempts == ["gemini-first", "gemini-second"]


@pytest.mark.asyncio
async def test_exhausted_hard_bank_recovers_without_reusing_words(provider, monkeypatch):
    from app.games import word_bank
    from app.repos import crocodile_daily as repo

    target_date = date(2026, 11, 9)
    monkeypatch.setattr(word_bank, "WORD_BANK", {"ru": {"Разное": ["чайник"]}})
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "get_used_daily_words", AsyncMock(return_value={"чайник"}))
    monkeypatch.setattr(repo, "ensure_puzzle_day", AsyncMock())
    monkeypatch.setattr(
        daily_ai, "resolve_process", AsyncMock(return_value=ResolvedPolicy(("gemini-test",), "sequential", False))
    )
    inserted = []

    async def query(sql, params, **kwargs):
        inserted.append(params)
        return [
            dict(zip(("puzzle_date", "difficulty", "target_word", "topic", "lang", "image_model"), params, strict=True))
        ]

    monkeypatch.setattr(repo.db, "db_query", query)
    with pytest.raises(repo.DailyCrocNotReady):
        await repo._create_puzzle_if_missing_with_conn(target_date, difficulty="hard", image_model="qwen-image")
    assert inserted == []

    async def generate(key):
        if key == "test-first":
            raise ServerError(503, {"error": {"message": "UNAVAILABLE"}})
        return SimpleNamespace(text='{"word":"самовар"}')

    provider.install(generate)
    candidate = await repo._prepare_daily_word_candidate(target_date, "hard", "gemini-test")
    puzzle = await repo._create_puzzle_if_missing_with_conn(
        target_date, difficulty="hard", candidate=candidate, image_model="qwen-image"
    )
    assert (puzzle.puzzle_date, puzzle.difficulty, puzzle.target_word) == (target_date, "hard", "самовар")
    assert len(inserted) == 1


@pytest.mark.asyncio
async def test_retry_cannot_dispatch_without_replacement_quota(provider, monkeypatch):
    monkeypatch.setattr(daily_ai, "_TEXT_RETRY_DELAY_S", 0.001)
    provider.replacement.return_value = None

    async def generate(key):
        raise TimeoutError

    provider.install(generate)
    with pytest.raises(TimeoutError):
        await daily_ai.generate_daily_text("prompt", "gemini-test")
    assert len(provider.attempts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_real_sdk_sends_one_http_request_per_reserved_attempt(provider, monkeypatch, recover):
    import httpx
    from google import genai

    from app.providers import gemini

    # Unlike Interactions, the pinned Models transport's retry_options=None
    # means one HTTP attempt. Exercise the actual cached-client configuration:
    # changing it to SDK retries would silently bypass per-dispatch admission.
    monkeypatch.setattr(gemini, "_gemini_clients_cache", {})
    monkeypatch.setattr(daily_ai, "_TEXT_RETRY_DELAY_S", 0.001)
    real_client = genai.Client
    clients = []
    requests = []

    def respond(request):
        requests.append(request.headers["x-goog-api-key"])
        if recover and requests[-1] == "test-second":
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]}}]},
            )
        return httpx.Response(503, json={"error": {"code": 503, "message": "UNAVAILABLE"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:

        def client(**kwargs):
            kwargs["http_options"].httpx_async_client = transport
            result = real_client(**kwargs)
            clients.append(result)
            return result

        monkeypatch.setattr(genai, "Client", client)
        try:
            if recover:
                assert await daily_ai.generate_daily_text("prompt", "gemini-test") == '{"ok":true}'
            else:
                with pytest.raises(ServerError):
                    await daily_ai.generate_daily_text("prompt", "gemini-test")
        finally:
            for sdk_client in clients:
                await sdk_client.aio.aclose()
                sdk_client.close()

    assert requests == ["test-first", "test-second"]
    provider.reserve.assert_awaited_once_with("first", "gemini-test")
    provider.replacement.assert_awaited_once_with("gemini-test", excluded_hashes={"first"})
