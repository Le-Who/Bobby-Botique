from __future__ import annotations

import pytest

from app.observability import workload_events


def test_selected_key_is_present_on_start_success_and_error(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(workload_events, "emit", lambda event, **fields: captured.append((event, fields)))
    monkeypatch.setattr(workload_events, "record_exception", lambda *args, **kwargs: "error-id")

    success = workload_events.start_workload_attempt(
        workload="image",
        provider="synthetic",
        model="model",
        api_key="fake-workload-key-1234",
    )
    success.finish(outcome="succeeded", result_count=1)

    failed = workload_events.start_workload_attempt(
        workload="tts",
        provider="synthetic",
        model="voice-model",
        api_key="fake-workload-key-5678",
    )
    failed.fail(ValueError("synthetic failure"), reason_code="bad_response")

    assert [(event, fields["key_suffix"]) for event, fields in captured] == [
        ("workload.attempt_started", "1234"),
        ("workload.attempt_finished", "1234"),
        ("workload.attempt_started", "5678"),
        ("workload.attempt_finished", "5678"),
    ]
    assert captured[-1][1]["error_id"] == "error-id"


@pytest.mark.asyncio
async def test_observe_workload_call_preserves_result_and_logs_key(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(workload_events, "emit", lambda event, **fields: captured.append((event, fields)))

    result = await workload_events.observe_workload_call(
        _value("ok"),
        workload="synthetic",
        provider="provider",
        model="model",
        api_key="fake-key-4321",
    )

    assert result == "ok"
    assert [fields["key_suffix"] for _event, fields in captured] == ["4321", "4321"]


async def _value(value):
    return value
