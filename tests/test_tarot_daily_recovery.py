"""Temporary provider outages retry a missing card without replaying saved work."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import APIError

from app import tarot_daily as tarot
from app.errors import ErrorCode, tag_error
from app.process_policies import ResolvedPolicy
from app.repos.tarot_generation import TarotGenerationConflict

DAY = date(2026, 11, 2)
MODEL = "gemini-3.5-flash-lite"
VARIANT = {"name": "Маг", "orientation": "Прямая", "label": "Маг (Прямая)", "context": "synthetic"}


@pytest.fixture
def preparation(monkeypatch):
    ticket = object()
    write = AsyncMock()
    finish = AsyncMock(return_value=True)
    sleep = AsyncMock()
    monkeypatch.setattr(tarot, "iter_daily_card_variants", lambda: [VARIANT])
    monkeypatch.setattr(tarot, "begin_generation", AsyncMock(return_value=ticket))
    monkeypatch.setattr(tarot, "finish_generation", finish)
    monkeypatch.setattr(tarot, "_acquire_redis_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot, "count_gemini_keys", AsyncMock(return_value=4))
    monkeypatch.setattr(tarot, "get_prepared_daily_reading", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot, "upsert_prepared_daily_reading", write)
    monkeypatch.setattr(tarot, "resolve_process", AsyncMock(return_value=ResolvedPolicy((MODEL,))))
    return SimpleNamespace(ticket=ticket, write=write, finish=finish, sleep=sleep)


def scripted_provider(monkeypatch, outcomes, *, explicit=False):
    calls = []
    sequence = iter(outcomes)

    async def generate(*args, **kwargs):
        calls.append((args, kwargs))
        outcome = next(sequence)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "prepared":
            kwargs["on_model_used"]("gemini-3.5-flash")
        return outcome if explicit else (outcome, 1)

    router = SimpleNamespace(get_response=generate, execute_gemini_model_plan=generate)
    monkeypatch.setattr(tarot, "get_provider_router", lambda: router)
    monkeypatch.setattr(
        tarot,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy((MODEL, "gemini-3.5-flash"), explicit=explicit)),
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("code", [ErrorCode.TIMEOUT, ErrorCode.OVERLOADED, ErrorCode.NETWORK])
async def test_missing_card_recovers_without_regenerating_saved_card(monkeypatch, preparation, explicit, code):
    saved = {**VARIANT, "name": "Жрица", "label": "Жрица (Прямая)"}
    monkeypatch.setattr(tarot, "iter_daily_card_variants", lambda: [saved, VARIANT])
    monkeypatch.setattr(tarot, "get_prepared_daily_reading", AsyncMock(side_effect=[object(), None]))
    calls = scripted_provider(monkeypatch, [tag_error(code, "synthetic failure"), "prepared"], explicit=explicit)

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.skipped, result.failed, result.remaining) == (1, 1, 0, 0)
    assert len(calls) == 2
    if explicit:
        assert calls[0][0] == calls[1][0]
        assert calls[0][0][0] == (MODEL, "gemini-3.5-flash")
    else:
        assert calls[0][1]["history"] == calls[1][1]["history"]
        assert all(kwargs["preserve_transient_errors"] for _, kwargs in calls)
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0]
    preparation.write.assert_awaited_once()
    persisted = preparation.write.await_args.kwargs
    assert (persisted["card_name"], persisted["body_markdown"], persisted["model_name"]) == (
        "Маг",
        "prepared",
        "gemini-3.5-flash",
    )
    assert persisted["ticket"] is preparation.ticket


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_raw_timeout_recovers_on_the_same_card(monkeypatch, preparation, explicit):
    calls = scripted_provider(monkeypatch, [TimeoutError(), "prepared"], explicit=explicit)

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (1, 0)
    assert len(calls) == 2
    preparation.write.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_raw_503_recovers_on_the_same_card(monkeypatch, preparation, explicit):
    calls = scripted_provider(
        monkeypatch,
        [APIError(503, {"error": {"message": "synthetic unavailable", "status": "UNAVAILABLE"}}), "prepared"],
        explicit=explicit,
    )

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (1, 0)
    assert len(calls) == 2
    preparation.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporarily_empty_pool_after_transient_still_recovers(monkeypatch, preparation):
    calls = scripted_provider(
        monkeypatch,
        [
            RuntimeError(tag_error(ErrorCode.OVERLOADED, "synthetic")),
            RuntimeError("No Gemini keys available for the requested model plan"),
            "prepared",
        ],
        explicit=True,
    )

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (1, 0)
    assert len(calls) == 3
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0, 30.0]
    preparation.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_inherited_pool_unavailable_after_transient_uses_remaining_attempt(monkeypatch, preparation):
    calls = scripted_provider(
        monkeypatch,
        [tag_error(ErrorCode.TIMEOUT, "synthetic"), tag_error(ErrorCode.KEYS_EXHAUSTED, "synthetic"), "prepared"],
    )

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (1, 0)
    assert len(calls) == 3
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0, 30.0]


@pytest.mark.asyncio
async def test_sustained_transient_failure_has_three_attempts_and_one_failed_card(monkeypatch, preparation):
    calls = scripted_provider(monkeypatch, [tag_error(ErrorCode.OVERLOADED, "synthetic")] * 4)

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed, result.remaining) == (0, 1, 0)
    assert len(calls) == 3
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0, 30.0]
    preparation.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.KEYS_EXHAUSTED,
        ErrorCode.QUOTA_EXCEEDED,
        ErrorCode.RATE_LIMIT,
        ErrorCode.INVALID_KEY,
        ErrorCode.NO_KEYS,
        ErrorCode.INVALID_RESPONSE,
    ],
)
async def test_nontransient_failure_does_not_start_same_card_recovery(monkeypatch, preparation, code):
    calls = scripted_provider(monkeypatch, [tag_error(code, "synthetic"), "prepared"])

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (0, 1)
    assert len(calls) == 1
    preparation.sleep.assert_not_awaited()
    preparation.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_quota_failure_after_transient_stops_recovery(monkeypatch, preparation):
    calls = scripted_provider(
        monkeypatch,
        [tag_error(ErrorCode.TIMEOUT, "synthetic"), tag_error(ErrorCode.QUOTA_EXCEEDED, "synthetic"), "prepared"],
    )

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (0, 1)
    assert len(calls) == 2
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0]
    preparation.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_retry_delay_is_respected(monkeypatch, preparation):
    scripted_provider(monkeypatch, [tag_error(ErrorCode.OVERLOADED, "retry_after=47"), "prepared"])

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert result.generated == 1
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [47.0]


@pytest.mark.asyncio
async def test_cancellation_during_backoff_releases_ownership_without_retry(monkeypatch, preparation):
    calls = scripted_provider(monkeypatch, [tag_error(ErrorCode.TIMEOUT, "synthetic"), "prepared"])
    preparation.sleep.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert len(calls) == 1
    preparation.write.assert_not_awaited()
    preparation.finish.assert_awaited_once_with(preparation.ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), TarotGenerationConflict("revision_conflict")])
@pytest.mark.parametrize("recover", [False, True])
async def test_persistence_failure_never_replays_generation(monkeypatch, preparation, error, recover):
    outcomes = [tag_error(ErrorCode.TIMEOUT, "synthetic")] if recover else []
    calls = scripted_provider(monkeypatch, [*outcomes, "prepared", "prepared"])
    preparation.write.side_effect = error

    with pytest.raises(type(error)):
        await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert len(calls) == (2 if recover else 1)
    preparation.write.assert_awaited_once()
    assert preparation.write.await_args.kwargs["ticket"] is preparation.ticket
    assert [call.args[0] for call in preparation.sleep.await_args_list] == ([15.0] if recover else [])
    preparation.finish.assert_awaited_once_with(preparation.ticket)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_real_router_recovers_suspended_pool_and_reserves_every_dispatch(monkeypatch, preparation, explicit):
    from app.providers.router import ProviderRouter
    from tests.test_provider_router import FakeAgentRequestUseCase, FakeKeyStatusManager

    failed = {"api_key": "synthetic-failed", "key_hash": "failed"}
    recovered = {"api_key": "synthetic-recovered", "key_hash": "recovered"}
    use_case = FakeAgentRequestUseCase(
        [(failed, MODEL, None), (None, None, "all_exhausted"), (recovered, MODEL, None)],
        [(tag_error(ErrorCode.OVERLOADED, "synthetic"), None), ("prepared", 4)],
    )
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: FakeKeyStatusManager())
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", AsyncMock(side_effect=[failed, None, recovered]))
    monkeypatch.setattr(tarot, "get_provider_router", lambda: ProviderRouter())
    monkeypatch.setattr(tarot, "resolve_process", AsyncMock(return_value=ResolvedPolicy((MODEL,), explicit=explicit)))

    result = await tarot.prepare_daily_readings(target_date=DAY, sleep=preparation.sleep)

    assert (result.generated, result.failed) == (1, 0)
    assert use_case.responses_made == 2
    assert use_case.usages_reserved == [("failed", MODEL), ("recovered", MODEL)]
    assert all(call["kwargs"]["provider_max_retries"] == 1 for call in use_case.response_calls)
    assert [call.args[0] for call in preparation.sleep.await_args_list] == [15.0]
    preparation.write.assert_awaited_once()
    assert preparation.write.await_args.kwargs["model_name"] == MODEL


@pytest.mark.asyncio
async def test_revoked_job_after_backoff_starts_no_replacement_dispatch(monkeypatch, preparation):
    active = True

    class Guard:
        async def checkpoint(self):
            if not active:
                raise asyncio.CancelledError

    async def revoke(_delay):
        nonlocal active
        active = False

    calls = scripted_provider(monkeypatch, [tag_error(ErrorCode.TIMEOUT, "synthetic"), "prepared"])
    monkeypatch.setattr("app.queue.current_job_guard", lambda: Guard())

    with pytest.raises(asyncio.CancelledError):
        await tarot.prepare_daily_readings(target_date=DAY, sleep=revoke)

    assert len(calls) == 1
    preparation.write.assert_not_awaited()
    preparation.finish.assert_awaited_once_with(preparation.ticket)
