"""Background model plans respect provider cooldowns without penalizing content."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import APIError

from app.errors import ErrorCode, extract_error_code, extract_retry_after_seconds, tag_error, user_friendly_error
from app.providers.router import ProviderRouter


@pytest.mark.parametrize("status", [429, 503])
def test_friendly_provider_failure_preserves_only_safe_retry_delay(status):
    error = APIError(status, {"error": {"message": "private-provider-details retryDelay: 47s"}})
    friendly = user_friendly_error(error)
    assert extract_retry_after_seconds(friendly) == 47
    assert "private-provider-details" not in friendly
    assert extract_error_code(friendly) is (ErrorCode.RATE_LIMIT if status == 429 else ErrorCode.OVERLOADED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (tag_error(ErrorCode.RATE_LIMIT, "Подождите около 47с."), "rate_limit"),
        (TimeoutError(), "transient"),
        (APIError(503, {"error": {"message": "private-provider-details retryDelay: 47s"}}), "transient"),
    ],
)
async def test_plan_does_not_dispatch_same_pair_during_provider_cooldown(monkeypatch, failure, category):
    first, second = "gemini-3.5-flash", "gemini-3.5-flash-lite"
    blocked = set()
    dispatched = []

    async def reserve(key, model, provider):
        return (key, model) not in blocked

    async def suspend(key, model, error_category, error_text=""):
        blocked.add((key, model))

    async def generate(key, history, model, *args, **kwargs):
        dispatched.append(model)
        if len(dispatched) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure, None
        return "malformed", 1

    def parse(text):
        raise ValueError("invalid structure")

    status = SimpleNamespace(suspend_key=AsyncMock(side_effect=suspend), record_success=AsyncMock())
    use_case = SimpleNamespace(reserve_key_usage=AsyncMock(side_effect=reserve), get_ai_response=generate)
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: status)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"key_hash": "fake-hash", "api_key": "fake-key"}),
    )

    with pytest.raises(RuntimeError):
        await ProviderRouter().execute_gemini_model_plan([first, second, first], [], parse_response=parse, max_keys=1)

    assert dispatched == [first, second]
    status.suspend_key.assert_awaited_once()
    assert status.suspend_key.await_args.args[:3] == ("fake-hash", first, category)
    if not isinstance(failure, TimeoutError):
        assert extract_retry_after_seconds(status.suspend_key.await_args.args[3]) == 47
    assert "private-provider-details" not in status.suspend_key.await_args.args[3]
    status.record_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_response_can_retry_planned_model_without_key_penalty(monkeypatch):
    model = "gemini-3.5-flash-lite"
    status = SimpleNamespace(suspend_key=AsyncMock(), record_success=AsyncMock())
    use_case = SimpleNamespace(
        reserve_key_usage=AsyncMock(return_value=True),
        get_ai_response=AsyncMock(side_effect=[("malformed", 1), ("valid", 2)]),
    )
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: status)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"key_hash": "fake-hash", "api_key": "fake-key"}),
    )

    def parse(text):
        if text != "valid":
            raise ValueError("invalid structure")
        return text

    assert await ProviderRouter().execute_gemini_model_plan([model, model], [], parse_response=parse) == "valid"
    assert use_case.reserve_key_usage.await_count == 2
    assert use_case.get_ai_response.await_count == 2
    status.suspend_key.assert_not_awaited()
