from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_probe(source: str) -> list[dict]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", textwrap.dedent(source)],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]


def test_provider_request_event_contains_required_key_suffix():
    event = _run_probe(
        """
        from app.observability.events import emit
        from app.observability.redaction import provider_key_fields
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        emit(
            "provider.attempt_started",
            operation="provider.request",
            provider="gemini",
            **provider_key_fields("gemini", "synthetic-provider-key-ABCD"),
        )
        """
    )[-1]

    assert event["event"] == "provider.attempt_started"
    assert event["key_suffix"] == "ABCD"
    assert event["key_present"] is True
    assert "synthetic-provider-key" not in json.dumps(event)


def test_exception_event_keeps_key_suffix_but_scrubs_full_key():
    event = _run_probe(
        """
        from app.observability.events import record_exception
        from app.observability.redaction import provider_key_fields
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        secret = "synthetic-provider-key-WXYZ"
        error_id = record_exception(
            "provider.attempt_failed",
            RuntimeError(f"request with {secret} failed"),
            operation="provider.request",
            fields=provider_key_fields("gemini", secret),
        )
        """
    )[-1]
    wire = json.dumps(event, ensure_ascii=False)

    assert event["event"] == "provider.attempt_failed"
    assert event["key_suffix"] == "WXYZ"
    assert event["exception"]["type"] == "RuntimeError"
    assert event["error_id"]
    assert "synthetic-provider-key" not in wire


def test_record_exception_never_places_raw_error_message_in_log_record(caplog):
    from app.observability.events import record_exception

    upstream_secret = "unclassified-upstream-secret-76af"

    with caplog.at_level(logging.ERROR, logger="app.observability"):
        record_exception(
            "provider.attempt_failed",
            RuntimeError(upstream_secret),
            operation="provider.request",
        )

    assert upstream_secret not in caplog.text
    record = caplog.records[-1]
    snapshot = record.__dict__["_exception_snapshot"]
    assert snapshot["type"] == "RuntimeError"
    assert snapshot["message"] == "[redacted]"
    assert snapshot["message_fingerprint"]


def test_emit_sanitizes_message_before_creating_log_record(caplog):
    from app.observability.events import emit
    from app.observability.redaction import register_sensitive_credential

    credential = "non-provider-credential-24bd"
    register_sensitive_credential("test", credential)

    with caplog.at_level(logging.WARNING, logger="app.observability"):
        emit(
            "dependency.request_failed",
            level="warning",
            message=f"request used {credential}",
            detail=f"response contained {credential}",
        )

    assert credential not in caplog.text
    assert credential not in caplog.records[-1].detail  # type: ignore[attr-defined]
