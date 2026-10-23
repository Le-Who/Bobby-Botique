"""HTTP retry decisions use transport types and status, never exception prose."""

import httpx
import pytest

from app.circuit_breaker import CircuitBreaker
from app.providers.base import AIResponse, BaseAIProvider
from app.resilience_policy import ResiliencePolicy, run_with_resilience


def status_error(status, message=""):
    request = httpx.Request("POST", "https://provider.invalid/synthetic")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout(""),
        httpx.ConnectError(""),
        httpx.RemoteProtocolError(""),
        status_error(408),
        status_error(429),
        status_error(500),
        status_error(502),
        status_error(503),
        status_error(504),
    ],
    ids=["read-timeout", "connect-error", "remote-protocol", "408", "429", "500", "502", "503", "504"],
)
async def test_retry_runner_recovers_from_typed_http_failures_with_empty_messages(error):
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return "recovered"

    result = await run_with_resilience(operation, ResiliencePolicy(max_retries=2, base_delay_s=0, jitter_s=0))

    assert result == ("recovered", 2)
    assert calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
async def test_permanent_http_status_is_not_retried_despite_transient_words(status):
    error = status_error(status, "connection timeout unavailable")
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await run_with_resilience(operation, ResiliencePolicy(max_retries=2, base_delay_s=0, jitter_s=0))

    assert caught.value is error
    assert calls == 1


async def test_provider_retry_fallback_preserves_permanent_http_status(monkeypatch):
    # The provider-specific text matcher must not override a typed HTTP denial.
    breaker = CircuitBreaker("provider-status-test")
    monkeypatch.setattr("app.resilience_policy.get_circuit_breaker", lambda name: breaker)
    monkeypatch.setattr(ResiliencePolicy, "compute_delay", lambda self, attempt: 0)
    calls = 0

    class Provider(BaseAIProvider):
        async def _execute_request(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise status_error(401, "connection timeout unavailable")
            return AIResponse("unexpected retry", 1, True)

        def _log_failure(self, *args):
            pass

        async def stream(self, request, *, model_name):
            raise AssertionError("This test uses the non-streaming request path")
            yield

    try:
        response = await Provider("synthetic-key").get_response(
            history=[{"role": "user", "parts": ["hello"]}], model_name="vendor/test", max_retries=2
        )
    finally:
        await breaker.shutdown()

    assert response.success is False
    assert calls == 1
