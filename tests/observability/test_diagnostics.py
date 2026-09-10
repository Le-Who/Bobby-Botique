from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.observability import diagnostics
from app.observability.config import LoggingSettings


def _settings(now: datetime, until: datetime) -> LoggingSettings:
    return LoggingSettings.from_mapping(
        {
            "LOG_CONTENT_MODE": "preview",
            "LOG_DIAGNOSTIC_REQUEST_ID": "d" * 32,
            "LOG_DIAGNOSTIC_SUBSYSTEM": "provider",
            "LOG_DIAGNOSTIC_INCIDENT_ID": "synthetic-diagnostic-test",
            "LOG_DIAGNOSTIC_UNTIL": until.isoformat(),
        },
        now=now,
    )


def test_diagnostic_activation_and_expiry_are_announced_once(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(diagnostics, "emit", lambda event, **fields: captured.append((event, fields)))
    monkeypatch.setattr(diagnostics, "_ANNOUNCED_ACTIVE", None)
    monkeypatch.setattr(diagnostics, "_ANNOUNCED_EXPIRED", None)
    started = datetime.now(UTC)
    until = started + timedelta(minutes=5)

    active = _settings(started, until)
    diagnostics.announce_diagnostic_state(active, now=started)
    diagnostics.announce_diagnostic_state(active, now=started)

    expired = _settings(until + timedelta(seconds=1), until)
    diagnostics.announce_diagnostic_state(expired, now=until + timedelta(seconds=1))
    diagnostics.announce_diagnostic_state(expired, now=until + timedelta(seconds=1))

    assert [event for event, _fields in captured] == ["diagnostic.enabled", "diagnostic.expired"]
