from __future__ import annotations

import json
import tracemalloc

import pytest

from app.observability.redaction import provider_key_fields, register_sensitive_credential, sanitize_event


def test_provider_key_fields_expose_suffix_without_exposing_key():
    """Removing key_suffix or including the whole credential must break this contract."""
    fields = provider_key_fields("gemini", "synthetic-provider-key-ABCD")

    assert fields["key_present"] is True
    assert fields["key_suffix"] == "ABCD"
    assert len(fields["key_fingerprint"]) == 16
    assert "synthetic-provider-key" not in json.dumps(fields)


def test_provider_key_fields_mark_absent_key_explicitly():
    fields = provider_key_fields("gemini", None)

    assert fields == {
        "key_present": False,
        "key_suffix": None,
        "key_fingerprint": None,
    }


def test_sanitizer_preserves_only_typed_suffix_and_scrubs_secret_occurrences():
    """A raw credential must not survive via fields, text, nested data, or URLs."""
    secret = "synthetic-provider-key-ABCD"
    sanitized = sanitize_event(
        {
            "key_suffix": "ABCD",
            "api_key": secret,
            "message": f"Authorization: Bearer {secret}",
            "nested": {"password": secret, "url": f"https://user:{secret}@example.test/path?token={secret}"},
        }
    )
    wire = json.dumps(sanitized, ensure_ascii=False)

    assert sanitized["key_suffix"] == "ABCD"
    assert secret not in wire
    assert f"Bearer {secret}" not in wire
    assert f"user:{secret}" not in wire
    assert "[redacted]" in wire
    assert sanitized["nested"]["url"] == ("https://[redacted]@example.test/path?token=[redacted]")


def test_sanitizer_never_calls_unknown_object_repr_and_handles_cycles():
    class Dangerous:
        def __repr__(self) -> str:
            raise AssertionError("repr must not run")

    cyclic: list[object] = []
    cyclic.append(cyclic)

    sanitized = sanitize_event({"object": Dangerous(), "cycle": cyclic})

    assert sanitized["object"] == "<Dangerous>"
    assert sanitized["cycle"] == ["[cycle]"]


def test_sensitive_non_provider_credential_is_scrubbed_without_exposing_suffix():
    token = "123456:synthetic-telegram-bot-token-XY99"
    fields = register_sensitive_credential("bot_token", token)

    sanitized = sanitize_event(
        {
            **fields,
            "message": f"upstream repeated {token}",
        }
    )

    assert fields == {"credential_kind": "bot_token", "credential_present": True}
    assert token not in json.dumps(sanitized)
    assert "XY99" not in json.dumps(sanitized)


def test_unknown_telegram_bot_token_and_api_path_are_scrubbed_without_registration():
    token = "987654321:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi_12"

    sanitized = sanitize_event(
        {
            "message": f"request failed at https://api.telegram.org/bot{token}/getMe",
            "detail": token,
        }
    )
    wire = json.dumps(sanitized)

    assert token not in wire
    assert "api.telegram.org/bot[redacted]/getMe" in sanitized["message"]
    assert sanitized["detail"] == "[redacted]"


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+asyncpg", "redis", "rediss", "mysql"])
def test_connection_uri_credentials_are_scrubbed_in_protected_content(scheme):
    """Database/cache credentials must not survive in ordinary logged text."""
    uri = f"{scheme}://synthetic_user:synthetic_uri_password@db.example.test:5432/private"

    sanitized = sanitize_event({"content_text": f"Connection failed for {uri}"})

    assert sanitized["content_text"] == f"Connection failed for {scheme}://[redacted]@db.example.test:5432/private"


@pytest.mark.parametrize("scheme", ["Basic", "tma", "Digest"])
def test_authorization_header_scrubs_credentials_for_every_authentication_scheme(scheme):
    credential = "synthetic-authorization-payload-123456789"

    sanitized = sanitize_event({"content_text": f"Authorization: {scheme} {credential}"})

    assert credential not in json.dumps(sanitized)
    assert "[redacted]" in sanitized["content_text"]


@pytest.mark.parametrize("container_kind", ["mapping", "list"])
def test_sanitizing_large_container_keeps_temporary_memory_bounded(container_kind):
    """A bounded event must not copy every untrusted item before selecting its prefix."""
    value = {str(index): None for index in range(100_000)} if container_kind == "mapping" else [None] * 100_000
    tracing_started = not tracemalloc.is_tracing()
    if tracing_started:
        tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        sanitized = sanitize_event({"payload": value})

        _, peak = tracemalloc.get_traced_memory()
    finally:
        if tracing_started:
            tracemalloc.stop()

    assert len(sanitized["payload"]) <= 33
    assert peak - baseline < 256_000
