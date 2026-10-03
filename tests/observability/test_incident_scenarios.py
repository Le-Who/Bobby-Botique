from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.observability.redaction import sanitize_event


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
async def test_direct_workload_emits_one_correlated_terminal(monkeypatch, outcome):
    import asyncio

    from app.observability import workload_events

    events = []
    monkeypatch.setattr(workload_events, "emit", lambda event, **fields: events.append((event, fields)))
    record_exception = Mock(return_value="synthetic-error-id")
    monkeypatch.setattr(workload_events, "record_exception", record_exception)
    failure = RuntimeError("synthetic SDK failure")

    async def sdk_call():
        if outcome == "failed":
            raise failure
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return "SDK result"

    observed = workload_events.observe_workload_call(
        sdk_call(),
        workload="embedding",
        provider="gemini",
        model="synthetic-model",
        api_key="synthetic-key-1234",
    )
    if outcome == "failed":
        with pytest.raises(RuntimeError) as raised:
            await observed
        assert raised.value is failure
        record_exception.assert_called_once()
        assert record_exception.call_args.args[1] is failure
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await observed
        record_exception.assert_not_called()
    else:
        assert await observed == "SDK result"
        record_exception.assert_not_called()

    assert [name for name, _ in events] == ["workload.attempt_started", "workload.attempt_finished"]
    started, finished = (fields for _, fields in events)
    assert started["attempt_id"] == finished["attempt_id"]
    assert finished["outcome"] == outcome
    assert finished["key_suffix"] == "1234"
    if outcome == "failed":
        assert finished["error_id"] == "synthetic-error-id"


def test_synthetic_secret_never_survives_even_when_content_is_requested():
    secret = "fake-scenario-token-abcdef123456"
    event = sanitize_event(
        {
            "event": "workload.attempt_failed",
            "content_preview": f"Authorization: Bearer {secret}",
            "key_suffix": "3456",
        }
    )

    wire = json.dumps(event)
    assert secret not in wire
    assert event["key_suffix"] == "3456"


def test_benchmark_script_runs_with_synthetic_input_only():
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_logging.py",
            "--events",
            "100",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert result["events_submitted"] == 100
    assert result["events_written"] == 100
    assert result["drops"] == 0
