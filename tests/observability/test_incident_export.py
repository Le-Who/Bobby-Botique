from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from app.observability import incident
from app.observability.incident import IncidentCriteria, IncidentExportError, export_incident


def _row(**fields):
    return {
        "schema_version": 1,
        "timestamp": "2026-09-10T10:00:00.000Z",
        "event_id": fields.pop("event_id", "a" * 32),
        "event": fields.pop("event", "provider.attempt_finished"),
        "level": "error",
        "message": "synthetic",
        "request_id": "b" * 32,
        "user_id": 4242,
        "key_suffix": "9876",
        "key_fingerprint": "f" * 16,
        **fields,
    }


def test_export_selects_sanitizes_pseudonymizes_and_preserves_key_suffix(tmp_path: Path):
    secret = "fake-incident-secret-9876"
    rows = [
        _row(
            content_preview=f"ignore instructions; Authorization: Bearer {secret}",
            content_text="real protected message",
        ),
        _row(event_id="c" * 32, request_id="other"),
    ]
    source = io.StringIO("\n".join(json.dumps(row) for row in rows))
    output = tmp_path / "incident"

    manifest = export_incident(
        source,
        source_name="synthetic.ndjson",
        criteria=IncidentCriteria(request_id="b" * 32),
        output_dir=output,
    )

    events_text = (output / "events.ndjson").read_text(encoding="utf-8")
    event = json.loads(events_text)
    assert event["key_suffix"] == "9876"
    assert event["user_id"].startswith("user_")
    assert "content_preview" not in event
    assert "content_text" not in event
    assert secret not in events_text
    assert manifest["selected_events"] == 1
    assert manifest["checksums"]["events.ndjson"] == hashlib.sha256(events_text.encode()).hexdigest()


def test_user_selection_requires_bounded_time_window(tmp_path: Path):
    with pytest.raises(IncidentExportError, match="--since and --until"):
        export_incident(
            io.StringIO(json.dumps(_row())),
            source_name="synthetic",
            criteria=IncidentCriteria(user_id=4242),
            output_dir=tmp_path / "incident",
        )


def test_export_reports_invalid_duplicate_and_missing_terminal(tmp_path: Path):
    started = _row(event="provider.attempt_started", event_id="d" * 32, attempt_id="attempt-1")
    source = io.StringIO(
        "not-json\n"
        + json.dumps(started)
        + "\n"
        + json.dumps(started)
        + "\n"
        + json.dumps({**started, "schema_version": 99, "event_id": "e" * 32})
    )
    output = tmp_path / "incident"
    manifest = export_incident(
        source,
        source_name="synthetic",
        criteria=IncidentCriteria(request_id="b" * 32),
        output_dir=output,
    )

    assert manifest["invalid_lines"] == 2
    assert manifest["duplicate_event_ids"] == 1
    assert manifest["missing_terminals"] == ["attempt-1"]


def test_export_closes_transitive_correlation_chain_without_unrelated_rows(tmp_path: Path):
    error_id = "e" * 32
    trace_id = "t" * 32
    rows = [
        _row(event="provider.call_failed", event_id="1" * 32, error_id=error_id, trace_id=trace_id),
        _row(
            event="provider.attempt_started",
            event_id="2" * 32,
            request_id="other-request",
            trace_id=trace_id,
            attempt_id="attempt-linked",
        ),
        _row(
            event="provider.attempt_finished",
            event_id="3" * 32,
            request_id="another-request",
            trace_id=None,
            attempt_id="attempt-linked",
        ),
        _row(
            event="provider.attempt_finished",
            event_id="4" * 32,
            request_id="unrelated",
            trace_id="unrelated-trace",
            attempt_id="unrelated-attempt",
        ),
    ]
    output = tmp_path / "incident"

    manifest = export_incident(
        io.StringIO("\n".join(json.dumps(row) for row in rows)),
        source_name="synthetic",
        criteria=IncidentCriteria(error_id=error_id),
        output_dir=output,
    )

    events = [json.loads(line) for line in (output / "events.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [event["event_id"] for event in events] == ["1" * 32, "2" * 32, "3" * 32]
    assert manifest["direct_matches"] == 1
    assert manifest["correlated_events"] == 2


def test_export_refuses_existing_output(tmp_path: Path):
    output = tmp_path / "incident"
    output.mkdir()
    with pytest.raises(IncidentExportError, match="already exists"):
        export_incident(
            io.StringIO(json.dumps(_row())),
            source_name="synthetic",
            criteria=IncidentCriteria(request_id="b" * 32),
            output_dir=output,
        )


def test_output_path_rejects_parent_traversal(tmp_path: Path):
    with pytest.raises(IncidentExportError, match="parent traversal"):
        incident._assert_safe_output_path(tmp_path / ".." / "escaped-incident")
