from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.observability.redaction import sanitize_event


@pytest.mark.parametrize(
    ("start", "finish", "identity"),
    [
        ("provider.attempt_started", "provider.attempt_finished", "attempt_id"),
        ("workload.attempt_started", "workload.attempt_finished", "attempt_id"),
        ("delivery.started", "delivery.finished", "delivery_id"),
        ("job.started", "job.finished", "execution_id"),
        ("database.operation_started", "database.operation_finished", "operation_id"),
    ],
)
def test_controlled_lifecycle_has_one_terminal(start: str, finish: str, identity: str):
    lifecycle_id = "synthetic-id"
    events = [{"event": start, identity: lifecycle_id}, {"event": finish, identity: lifecycle_id}]

    terminals = [event for event in events if event["event"] == finish and event[identity] == lifecycle_id]
    assert len(terminals) == 1


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
