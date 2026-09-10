from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.observability.config import LoggingSettings


def test_safe_defaults_do_not_enable_content_or_diagnostics():
    config = LoggingSettings.from_mapping({})

    assert config.format == "json"
    assert config.level_name == "INFO"
    assert config.content_mode == "metadata"
    assert config.event_max_bytes == 32_768
    assert config.queue_max_events == 4_096
    assert config.queue_max_bytes == 8_388_608
    assert config.diagnostic_active is False


def test_log_format_wins_over_compatibility_aliases_and_reports_conflict():
    config = LoggingSettings.from_mapping(
        {
            "LOG_FORMAT": "text",
            "STRUCTURED_LOGGING": "true",
            "LOG_PRETTY": "false",
        }
    )

    assert config.format == "text"
    assert "STRUCTURED_LOGGING" in config.conflicts


def test_invalid_values_fall_back_without_echoing_their_values():
    config = LoggingSettings.from_mapping(
        {
            "LOG_LEVEL": "secret-invalid-level",
            "LOG_EVENT_MAX_BYTES": "not-a-number-secret",
            "LOG_QUEUE_MAX_EVENTS": "-10",
        }
    )

    assert config.level_name == "INFO"
    assert config.event_max_bytes == 32_768
    assert config.queue_max_events == 4_096
    assert set(config.invalid_parameters) == {
        "LOG_LEVEL",
        "LOG_EVENT_MAX_BYTES",
        "LOG_QUEUE_MAX_EVENTS",
    }
    assert "secret" not in repr(config.invalid_parameters)


def test_event_byte_limit_below_pipeline_minimum_falls_back():
    config = LoggingSettings.from_mapping({"LOG_EVENT_MAX_BYTES": "100"})

    assert config.event_max_bytes == 32_768
    assert "LOG_EVENT_MAX_BYTES" in config.invalid_parameters


def test_diagnostics_require_complete_bounded_scope():
    now = datetime.now(UTC)
    base = {
        "LOG_DIAGNOSTIC_SUBSYSTEM": "provider",
        "LOG_DIAGNOSTIC_INCIDENT_ID": "inc-42",
        "LOG_DIAGNOSTIC_REQUEST_ID": "a" * 32,
    }

    missing_until = LoggingSettings.from_mapping(base, now=now)
    too_far = LoggingSettings.from_mapping(
        {**base, "LOG_DIAGNOSTIC_UNTIL": (now + timedelta(minutes=16)).isoformat()},
        now=now,
    )
    valid = LoggingSettings.from_mapping(
        {**base, "LOG_DIAGNOSTIC_UNTIL": (now + timedelta(minutes=10)).isoformat()},
        now=now,
    )

    assert missing_until.diagnostic_active is False
    assert too_far.diagnostic_active is False
    assert valid.diagnostic_active is True


def test_key_suffix_flag_is_not_a_broad_diagnostic_selector():
    now = datetime.now(UTC)
    config = LoggingSettings.from_mapping(
        {
            "LOG_DIAGNOSTIC_SUBSYSTEM": "provider",
            "LOG_DIAGNOSTIC_INCIDENT_ID": "inc-43",
            "LOG_DIAGNOSTIC_KEY_SUFFIX": "true",
            "LOG_DIAGNOSTIC_UNTIL": (now + timedelta(minutes=5)).isoformat(),
        },
        now=now,
    )

    assert config.diagnostic_key_suffix is True
    assert config.diagnostic_active is False


def test_debug_level_does_not_implicitly_enable_content_preview():
    config = LoggingSettings.from_mapping({"LOG_LEVEL": "DEBUG"})

    assert config.level_name == "DEBUG"
    assert config.content_mode == "metadata"
