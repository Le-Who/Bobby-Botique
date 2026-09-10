from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app.utils.logging_config import _FanoutStream

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _FailingStream:
    def write(self, _value: str) -> int:
        raise OSError("synthetic sink failure")

    def flush(self) -> None:
        raise OSError("synthetic sink failure")


def test_fanout_reports_partial_sink_failure_to_surviving_sink():
    surviving = io.StringIO()
    fanout = _FanoutStream(
        [_FailingStream(), surviving],
        failure_event_factory=lambda phase, error_type, sink_index: f"sink-failed:{phase}:{error_type}:{sink_index}\n",
    )

    fanout.write("application-event\n")
    fanout.flush()

    output = surviving.getvalue()
    assert "application-event" in output
    assert "sink-failed:write:OSError:0" in output


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


def test_repeated_setup_has_one_owned_root_handler_and_no_named_hidden_sinks():
    events = _run_probe(
        """
        import logging
        from app.utils.logging_config import setup_detailed_logging, shutdown_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        setup_detailed_logging(enable_structured_logging=True)
        logging.getLogger("api_logger").warning("one application record")

        root = logging.getLogger()
        assert sum(getattr(handler, "_gemaibot_owned", False) for handler in root.handlers) == 1
        for name in ("api_logger", "telegram", "asyncpg", "httpx", "httpcore"):
            logger = logging.getLogger(name)
            assert not any(getattr(handler, "_gemaibot_owned", False) for handler in logger.handlers)
            assert logger.propagate is True

        shutdown_detailed_logging()
        """
    )

    application_records = [event for event in events if event["message"] == "one application record"]
    assert len(application_records) == 1
    assert application_records[0]["logger"] == "api_logger"


def test_shutdown_is_idempotent_and_drains_accepted_events():
    events = _run_probe(
        """
        import logging
        from app.utils.logging_config import setup_detailed_logging, shutdown_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        logging.warning("accepted before shutdown")
        assert shutdown_detailed_logging(timeout=1.0) is True
        assert shutdown_detailed_logging(timeout=1.0) is True
        """
    )

    assert sum(event["message"] == "accepted before shutdown" for event in events) == 1


def test_warnings_and_uncaught_exceptions_use_shared_sanitized_pipeline():
    events = _run_probe(
        """
        import sys
        import warnings
        from app.utils.logging_config import setup_detailed_logging, shutdown_detailed_logging

        setup_detailed_logging(enable_structured_logging=True)
        warnings.warn("synthetic warning", RuntimeWarning)
        try:
            raise RuntimeError("synthetic uncaught")
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        shutdown_detailed_logging(timeout=1.0)
        """
    )

    warning = next(event for event in events if event["logger"] == "py.warnings")
    uncaught = next(event for event in events if event["event"] == "process.uncaught_exception")
    assert warning["level"] == "warning"
    assert uncaught["exception"]["type"] == "RuntimeError"
