"""Tests for fail-closed dependency live-canary classification and cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from google.genai.errors import APIError
from telegram.error import InvalidToken

from scripts.dependency_live_canary import (
    CanaryConfig,
    CanaryConfigurationError,
    ComparisonStatus,
    ProbeStatus,
    compare_reports,
    load_config,
    probe_telegram,
    redact,
    run_probe,
)


def _config() -> CanaryConfig:
    return CanaryConfig(
        telegram_token="123456:secret-token",
        telegram_chat_id=123,
        gemini_api_key="gemini-secret",
        gemini_model="gemini-test",
        tavily_api_key="tavily-secret",
        timeout_seconds=5.0,
    )


def _report(**statuses: ProbeStatus) -> dict[str, object]:
    defaults = dict.fromkeys(("telegram", "gemini", "tavily"), ProbeStatus.PASSED)
    defaults.update(statuses)
    return {"overall": "passed", "probes": {name: {"status": status.value} for name, status in defaults.items()}}


def test_missing_configuration_fails_closed_without_values() -> None:
    with pytest.raises(CanaryConfigurationError) as raised:
        load_config({"CANARY_GEMINI_MODEL": "gemini-test"})

    message = str(raised.value)
    for name in (
        "CANARY_TELEGRAM_BOT_TOKEN",
        "CANARY_TELEGRAM_CHAT_ID",
        "CANARY_GEMINI_API_KEY",
        "CANARY_TAVILY_API_KEY",
    ):
        assert name in message
    assert "secret" not in message.lower()


def test_redaction_removes_every_known_secret_and_telegram_url_token() -> None:
    config = _config()
    raw = "https://api.telegram.org/bot123456:secret-token/getMe gemini-secret tavily-secret 123456:secret-token"

    sanitized = redact(raw, config.secrets)

    assert sanitized.count("[REDACTED]") >= 3
    assert all(secret not in sanitized for secret in config.secrets)


@pytest.mark.asyncio
async def test_probe_retries_one_transient_failure_but_not_contract_failure() -> None:
    attempts = 0

    async def transient_then_pass():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary")
        return {"response_present": True}

    passed = await run_probe("gemini", transient_then_pass, secrets=())

    async def deterministic_failure():
        raise ValueError("wrong response shape")

    failed = await run_probe("gemini", deterministic_failure, secrets=())

    assert passed.status is ProbeStatus.PASSED
    assert passed.attempts == 2
    assert failed.status is ProbeStatus.CONTRACT_FAILED
    assert failed.attempts == 1


@pytest.mark.asyncio
async def test_google_api_client_errors_use_status_code_not_a_blanket_retry() -> None:
    async def bad_request():
        raise APIError(400, {"error": {"message": "invalid request"}})

    result = await run_probe("gemini", bad_request, secrets=())

    assert result.status is ProbeStatus.CONTRACT_FAILED
    assert result.attempts == 1


@pytest.mark.asyncio
async def test_telegram_message_is_always_cleaned_up() -> None:
    calls: list[object] = []

    class FakeBot:
        def __init__(self, token: str):
            calls.append(("token", token))

        async def initialize(self) -> None:
            calls.append("initialize")

        async def get_me(self):
            calls.append("get_me")
            return SimpleNamespace(id=42)

        async def send_message(self, **kwargs):
            calls.append(("send", kwargs["chat_id"]))
            return SimpleNamespace(message_id=77)

        async def delete_message(self, **kwargs):
            calls.append(("delete", kwargs["chat_id"], kwargs["message_id"]))

        async def shutdown(self) -> None:
            calls.append("shutdown")

    evidence = await probe_telegram(_config(), "run-1", bot_factory=FakeBot)

    assert evidence == {"identity_present": True, "message_sent": True, "message_deleted": True}
    assert ("delete", 123, 77) in calls
    assert calls[-1] == "shutdown"


@pytest.mark.asyncio
async def test_telegram_cleanup_failure_is_never_reported_as_passed() -> None:
    class CleanupFailingBot:
        def __init__(self, token: str):
            self.token = token

        async def initialize(self) -> None:
            pass

        async def get_me(self):
            return SimpleNamespace(id=42)

        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=77)

        async def delete_message(self, **kwargs):
            raise RuntimeError("cleanup failed")

        async def shutdown(self) -> None:
            pass

    config = replace(_config(), timeout_seconds=0.2)

    result = await run_probe(
        "telegram",
        lambda: probe_telegram(config, "run-2", bot_factory=CleanupFailingBot),
        secrets=config.secrets,
    )

    assert result.status is ProbeStatus.CLEANUP_FAILED


@pytest.mark.asyncio
async def test_telegram_auth_failure_is_not_hidden_by_uninitialized_shutdown() -> None:
    class InvalidTokenBot:
        def __init__(self, token: str):
            self.token = token

        async def initialize(self) -> None:
            raise InvalidToken("invalid canary token")

        async def shutdown(self) -> None:
            raise RuntimeError("bot was never initialized")

    config = _config()

    result = await run_probe(
        "telegram",
        lambda: probe_telegram(config, "run-auth", bot_factory=InvalidTokenBot),
        secrets=config.secrets,
    )

    assert result.status is ProbeStatus.AUTH_FAILED
    assert result.attempts == 1


@pytest.mark.parametrize(
    ("baseline", "candidate", "expected"),
    [
        (_report(), _report(), ComparisonStatus.PASSED),
        (
            _report(telegram=ProbeStatus.AUTH_FAILED),
            _report(telegram=ProbeStatus.AUTH_FAILED),
            ComparisonStatus.BLOCKED_CANARY_CONFIGURATION,
        ),
        (
            _report(gemini=ProbeStatus.TRANSIENT_FAILED),
            _report(gemini=ProbeStatus.TRANSIENT_FAILED),
            ComparisonStatus.INCONCLUSIVE_EXTERNAL,
        ),
        (
            _report(),
            _report(tavily=ProbeStatus.CONTRACT_FAILED),
            ComparisonStatus.CANDIDATE_FAILED,
        ),
        (
            _report(),
            _report(gemini=ProbeStatus.TRANSIENT_FAILED),
            ComparisonStatus.INCONCLUSIVE_EXTERNAL,
        ),
        (
            _report(tavily=ProbeStatus.CONTRACT_FAILED),
            _report(),
            ComparisonStatus.BLOCKED_BASELINE,
        ),
    ],
)
def test_baseline_candidate_comparison_is_conservative(
    baseline: dict[str, object], candidate: dict[str, object], expected: ComparisonStatus
) -> None:
    comparison = compare_reports(baseline, candidate)

    assert comparison["status"] == expected.value
    assert comparison["ready_for_review"] is (expected is ComparisonStatus.PASSED)


@pytest.mark.asyncio
async def test_probe_timeout_is_bounded() -> None:
    async def never_finishes():
        await asyncio.Future()

    result = await run_probe("tavily", never_finishes, secrets=(), timeout_seconds=0.01)

    assert result.status is ProbeStatus.TRANSIENT_FAILED
    assert result.attempts == 2
