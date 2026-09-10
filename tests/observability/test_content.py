from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.observability.content import content_fields
from app.observability.context import request_scope
from app.observability.redaction import provider_key_fields


def _enable_scoped_preview(monkeypatch, *, request_id: str = "a" * 32, user_id: int = 42) -> None:
    monkeypatch.setenv("LOG_CONTENT_MODE", "preview")
    monkeypatch.setenv("LOG_DIAGNOSTIC_REQUEST_ID", request_id)
    monkeypatch.setenv("LOG_DIAGNOSTIC_USER_ID", str(user_id))
    monkeypatch.setenv("LOG_DIAGNOSTIC_SUBSYSTEM", "telegram_message")
    monkeypatch.setenv("LOG_DIAGNOSTIC_INCIDENT_ID", "synthetic-incident")
    monkeypatch.setenv("LOG_DIAGNOSTIC_UNTIL", (datetime.now(UTC) + timedelta(minutes=5)).isoformat())


def test_metadata_mode_never_contains_raw_content(monkeypatch):
    monkeypatch.setenv("LOG_CONTENT_MODE", "metadata")
    text = "private user message synthetic-secret"

    fields = content_fields("user_message", text)

    assert fields["content_chars"] == len(text)
    assert fields["content_bytes"] == len(text.encode())
    assert len(fields["content_fingerprint"]) == 16
    assert "content_preview" not in fields
    assert text not in repr(fields)


def test_default_operational_mode_keeps_bounded_scrubbed_message_text(monkeypatch):
    monkeypatch.delenv("LOG_CONTENT_MODE", raising=False)
    token = "987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_12"
    text = f"Please debug this exact message {token} " + ("x" * 3000)

    fields = content_fields("user_message", text, subsystem="telegram_message")

    assert fields["content_policy"] == "full"
    assert fields["content_text"].startswith("Please debug this exact message [redacted]")
    assert token not in fields["content_text"]
    assert fields["content_truncated"] is True
    assert len(fields["content_text"]) < len(text)


def test_full_mode_scrubs_registered_credential_before_content_boundary(monkeypatch):
    monkeypatch.delenv("LOG_CONTENT_MODE", raising=False)
    credential = "provider-secret-crossing-the-log-boundary-ABCDEFGH"
    provider_key_fields("synthetic", credential)
    text = ("x" * 2030) + credential + " visible tail"

    fields = content_fields("user_message", text, subsystem="telegram_message")

    assert credential not in fields["content_text"]
    assert "provider-secret-crossing" not in fields["content_text"]
    assert "[redacted]" in fields["content_text"]


def test_preview_mode_keeps_bounded_scrubbed_excerpt(monkeypatch):
    _enable_scoped_preview(monkeypatch)
    text = "hello Authorization: Bearer synthetic-provider-key-ABCDEFGH world"

    with request_scope(request_id="a" * 32, user_id=42):
        fields = content_fields("user_message", text, subsystem="telegram_message")

    assert fields["content_preview"].startswith("hello Authorization: [redacted]")
    assert "ABCDEFGH" not in fields["content_preview"]
    assert len(fields["content_preview"]) <= 256


def test_sensitive_content_never_gets_preview_even_when_enabled(monkeypatch):
    _enable_scoped_preview(monkeypatch)

    with request_scope(request_id="a" * 32, user_id=42):
        fields = content_fields(
            "api_key_command",
            "/addkey full-secret",
            sensitive=True,
            subsystem="telegram_message",
        )

    assert fields["content_policy"] == "forbidden"
    assert "content_preview" not in fields


def test_preview_mode_without_complete_scope_stays_metadata_only(monkeypatch):
    monkeypatch.setenv("LOG_CONTENT_MODE", "preview")

    fields = content_fields("user_message", "synthetic content", subsystem="telegram_message")

    assert fields["content_policy"] == "metadata"
    assert "content_preview" not in fields


def test_all_diagnostic_selectors_and_subsystem_must_match(monkeypatch):
    _enable_scoped_preview(monkeypatch)

    with request_scope(request_id="a" * 32, user_id=99):
        wrong_user = content_fields("user_message", "hidden", subsystem="telegram_message")
    with request_scope(request_id="a" * 32, user_id=42):
        wrong_subsystem = content_fields("user_message", "hidden", subsystem="roles")

    assert "content_preview" not in wrong_user
    assert "content_preview" not in wrong_subsystem
