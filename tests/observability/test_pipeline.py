from __future__ import annotations

import json
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


def test_stdlib_record_has_complete_structured_envelope():
    """A foreign stdlib record must not lose correlation, level, source, or extra fields."""
    events = _run_probe(
        """
        import logging
        from app.request_context import set_request_id, set_user_context
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        set_request_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        set_user_context(42, 84)
        logging.getLogger("audit.stdlib").info(
            "stdlib probe",
            extra={"operation": "probe"},
        )
        """
    )

    event = events[-1]
    assert event["schema_version"] == 1
    assert event["event"] == "legacy.log"
    assert event["message"] == "stdlib probe"
    assert event["request_id"] == "a" * 32
    assert event["user_id"] == 42
    assert event["chat_id"] == 84
    assert event["level"] == "info"
    assert event["logger"] == "audit.stdlib"
    assert event["operation"] == "probe"
    assert event["timestamp"].endswith("Z")
    assert event["source"]["function"] == "<module>"
    assert isinstance(event["source"]["line"], int)


def test_structlog_and_stdlib_share_the_same_envelope_contract():
    """Switching logger API must not change the mandatory event envelope."""
    events = _run_probe(
        """
        import logging
        from app.request_context import set_request_id
        from app.utils.logging_config import get_logger, setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        set_request_id("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        logging.getLogger("audit.stdlib").warning("stdlib")
        get_logger("audit.structlog").warning("structlog", operation="probe")
        """
    )[-2:]

    mandatory = {
        "schema_version",
        "timestamp",
        "level",
        "event",
        "message",
        "event_id",
        "service",
        "environment",
        "release",
        "instance_id",
        "logger",
        "source",
        "request_id",
    }
    assert mandatory <= events[0].keys()
    assert mandatory <= events[1].keys()
    assert events[0]["event"] == "legacy.log"
    assert events[1]["event"] == "legacy.log"


def test_stdlib_exception_contains_application_stack_and_cause():
    """The serialized exception must retain actionable frames and its causal chain."""
    event = _run_probe(
        """
        import logging
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)

        def fail_inner():
            raise ValueError("unclassified-inner-secret-14bd")

        def fail_outer():
            try:
                fail_inner()
            except ValueError as exc:
                raise RuntimeError("unclassified-outer-secret-92af") from exc

        try:
            fail_outer()
        except RuntimeError:
            logging.getLogger("audit.exception").exception("controlled failure")
        """
    )[-1]

    exception = event["exception"]
    assert exception["type"] == "RuntimeError"
    assert exception["message"] == "[redacted]"
    assert exception["message_fingerprint"]
    assert any(frame["function"] == "fail_outer" for frame in exception["stack"])
    assert exception["cause"]["type"] == "ValueError"
    assert exception["cause"]["message"] == "[redacted]"
    assert any(frame["function"] == "fail_inner" for frame in exception["cause"]["stack"])
    wire = json.dumps(event)
    assert "<traceback object" not in wire
    assert "unclassified-outer-secret" not in wire
    assert "unclassified-inner-secret" not in wire


def test_child_span_fields_are_serialized_with_parent_link():
    event = _run_probe(
        """
        import logging
        from app.observability.context import request_scope, span_scope
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        with request_scope(request_id="cccccccccccccccccccccccccccccccc") as request:
            with span_scope("provider.request") as child:
                logging.getLogger("audit.span").info("inside child")
        """
    )[-1]

    assert event["trace_id"] == "c" * 32
    assert len(event["span_id"]) == 16
    assert len(event["parent_span_id"]) == 16
    assert event["parent_span_id"] != event["span_id"]


def test_untrusted_extra_cannot_override_envelope_identity():
    event = _run_probe(
        """
        import logging
        from app.observability.context import request_scope
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        with request_scope(request_id="dddddddddddddddddddddddddddddddd", user_id=42):
            logging.info(
                "collision",
                extra={"request_id": "forged", "user_id": 999, "event_id": "forged"},
            )
        """
    )[-1]

    assert event["request_id"] == "d" * 32
    assert event["user_id"] == 42
    assert event["event_id"] != "forged"
    assert set(event["reserved_field_collisions"]) == {"event_id", "request_id", "user_id"}


def test_unknown_extra_objects_are_never_stringified_before_sanitizing():
    event = _run_probe(
        """
        import logging
        from app.utils.logging_config import setup_detailed_logging

        class Dangerous:
            def __str__(self):
                raise AssertionError("__str__ must not run")

            def __repr__(self):
                raise AssertionError("__repr__ must not run")

        setup_detailed_logging(enable_structured_logging=True)
        logging.getLogger("audit.objects").info(
            "safe conversion",
            extra={"dangerous": Dangerous(), "nested": [Dangerous()]},
        )
        """
    )[-1]

    assert event["dangerous"] == "<Dangerous>"
    assert event["nested"] == ["<Dangerous>"]


def test_logging_bootstrap_registers_bot_token_before_first_application_event():
    token = "telegram-token-with-unusual-format$short"
    event = _run_probe(
        f"""
        import logging
        import os
        from app.utils.logging_config import setup_detailed_logging

        os.environ["TELEGRAM_BOT_TOKEN"] = {token!r}
        setup_detailed_logging(enable_structured_logging=True)
        logging.error("bootstrap echoed %s", os.environ["TELEGRAM_BOT_TOKEN"])
        """
    )[-1]

    assert token not in json.dumps(event)
    assert "[redacted]" in event["message"]


def test_event_budget_keeps_json_valid_and_root_cause_fields(monkeypatch):
    monkeypatch.setenv("LOG_EVENT_MAX_BYTES", "2048")
    event = _run_probe(
        """
        import logging
        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        logging.error(
            "oversized diagnostic",
            extra={
                "_event_name": "provider.attempt_finished",
                "error_id": "e" * 32,
                "key_suffix": "ABCD",
                "diagnostic": "x" * 100000,
                "large_collection": ["y" * 2000 for _ in range(100)],
            },
        )
        """
    )[-1]

    wire = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(wire) <= 2048
    assert event["event_truncated"] is True
    assert event["error_id"] == "e" * 32
    assert event["key_suffix"] == "ABCD"
