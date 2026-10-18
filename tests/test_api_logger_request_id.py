from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from app.utils.api_logger import APICallTiming, APILogger


def _extra(mock_method: MagicMock) -> dict:
    return mock_method.call_args.kwargs["extra"]


def test_log_request_uses_typed_fields_instead_of_json_in_message():
    api_logger = APILogger()
    api_logger.logger = MagicMock()

    timing = api_logger.log_request("telegram", endpoint="/send", method="POST")

    assert isinstance(timing, APICallTiming)
    assert api_logger.logger.info.call_args.args == ("API request started",)
    event = _extra(api_logger.logger.info)
    assert event["_event_name"] == "api.request_started"
    assert event["api"] == "telegram"
    assert event["endpoint"] == "/send"
    assert event["attempt_id"] == timing.attempt_id


def test_request_with_selected_key_includes_suffix_and_never_raw_key():
    api_logger = APILogger()
    api_logger.logger = MagicMock()
    secret = "provider-secret-ABCD"

    api_logger.log_request("gemini", api_key=secret, key_hash="f" * 64)

    event = _extra(api_logger.logger.info)
    assert event["key_suffix"] == "ABCD"
    assert event["key_fingerprint"] == "f" * 16
    assert event["key_present"] is True
    assert secret not in repr(event)


def test_log_response_reuses_attempt_and_monotonic_timing():
    api_logger = APILogger()
    api_logger.logger = MagicMock()
    timing = APICallTiming(started_ns=time.monotonic_ns() - 500_000_000, attempt_id="a" * 32)

    duration = api_logger.log_response(
        "gemini",
        timing,
        model="gemini-3.5-flash",
        response_length=100,
    )

    event = _extra(api_logger.logger.info)
    assert event["_event_name"] == "api.request_finished"
    assert event["outcome"] == "succeeded"
    assert event["attempt_id"] == "a" * 32
    assert event["duration_ms"] >= 490
    assert event["model"] == "gemini-3.5-flash"
    assert duration >= 0.49


def test_legacy_epoch_float_is_explicitly_marked():
    api_logger = APILogger()
    api_logger.logger = MagicMock()

    api_logger.log_response("tavily", time.time() - 0.1, success=False, error_message="timeout")

    event = _extra(api_logger.logger.error)
    assert event["legacy_timing"] is True
    assert event["outcome"] == "failed"
    assert event["error_message"] == "timeout"


@pytest.mark.parametrize("success", [True, False])
def test_cancelled_response_preserves_attempt_key_and_timing_at_info(caplog, success):
    api_logger = APILogger()
    secret = "provider-secret-CANC"
    with caplog.at_level(logging.INFO, logger="api_logger"):
        timing = api_logger.log_request("gemini", api_key=secret, model="gemini-3.5-flash")
        duration = api_logger.log_response("gemini", timing, success=success, cancelled=True, model="gemini-3.5-flash")

    started, terminal = caplog.records
    assert terminal._event_name == "api.request_finished"
    assert terminal.outcome == "cancelled"
    assert terminal.levelno == logging.INFO
    assert terminal.attempt_id == started.attempt_id
    assert terminal.key_suffix == started.key_suffix == "CANC"
    assert terminal.key_fingerprint == started.key_fingerprint
    assert terminal.duration_ms >= 0
    assert duration >= 0
    assert secret not in repr(terminal.__dict__)


def test_log_error_keeps_traceback_and_key_identity_without_raw_key():
    api_logger = APILogger()
    api_logger.logger = MagicMock()
    error = ValueError("bad request")
    secret = "provider-secret-WXYZ"

    error_id = api_logger.log_error(
        "gemini",
        error,
        context={"model": "gemini-3.5-flash", "api_key": secret},
    )

    call = api_logger.logger.error.call_args
    event = call.kwargs["extra"]
    assert call.args == ("API request failed",)
    assert "exc_info" not in call.kwargs
    assert event["_exception_snapshot"]["type"] == "ValueError"
    assert event["_exception_snapshot"]["message"] == "[redacted]"
    assert event["_event_name"] == "api.request_failed"
    assert event["error_id"] == error_id
    assert event["key_suffix"] == "WXYZ"
    assert secret not in repr(event)
