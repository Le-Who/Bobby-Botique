"""Bounded conversion and credential scrubbing for observability events."""

from __future__ import annotations

import hashlib
import re
import threading
from collections import deque
from collections.abc import Mapping
from typing import Any

from app.observability.schema import JsonValue

REDACTED = "[redacted]"
MAX_STRING_CHARS = 2048
MAX_COLLECTION_ITEMS = 32
MAX_DEPTH = 5
MAX_REGISTERED_SECRETS = 1024

_KNOWN_SECRETS: set[str] = set()
_SECRET_ORDER: deque[str] = deque()
_SECRET_LOCK = threading.Lock()
_CREDENTIAL_PATTERN = re.compile(r"(?i)(?:bearer\s+)?(?:sk[-_]|AIza|synthetic-provider-key-)[A-Za-z0-9_.-]{8,}")
_AUTH_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_QUERY_SECRET_PATTERN = re.compile(r"(?i)([?&](?:token|key|secret|password|signature)=)[^&#\s]+")


def _register_secret(secret: str) -> None:
    if not secret:
        return
    with _SECRET_LOCK:
        if secret in _KNOWN_SECRETS:
            return
        _KNOWN_SECRETS.add(secret)
        _SECRET_ORDER.append(secret)
        while len(_SECRET_ORDER) > MAX_REGISTERED_SECRETS:
            expired = _SECRET_ORDER.popleft()
            _KNOWN_SECRETS.discard(expired)


def provider_key_fields(
    provider: str,
    api_key: str | None,
    *,
    key_hash: str | None = None,
) -> dict[str, JsonValue]:
    """Return the mandatory, safe identity fields for a selected provider key."""
    del provider  # The provider remains an explicit event field; hashes match the existing repositories.
    if not isinstance(api_key, str) or not api_key:
        return {
            "key_present": False,
            "key_suffix": None,
            "key_fingerprint": None,
        }
    _register_secret(api_key)
    fingerprint = key_hash or hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return {
        "key_present": True,
        "key_suffix": api_key[-4:],
        "key_fingerprint": fingerprint[:16],
    }


def register_sensitive_credential(kind: str, credential: str | None) -> dict[str, JsonValue]:
    """Register a non-provider secret for scrubbing without disclosing any part."""
    normalized_kind = re.sub(r"[^a-z0-9_]+", "_", kind.casefold()).strip("_")[:48] or "credential"
    present = isinstance(credential, str) and bool(credential)
    if isinstance(credential, str) and credential:
        _register_secret(credential)
    return {
        "credential_kind": normalized_kind,
        "credential_present": present,
    }


def _is_sensitive_field(name: str) -> bool:
    normalized = name.casefold()
    if normalized in {
        "key_suffix",
        "key_fingerprint",
        "key_present",
        "key_source",
        "key_disposition",
        "token_count",
        "prompt_tokens",
        "completion_tokens",
        "max_tokens",
    }:
        return False
    return any(
        marker in normalized
        for marker in (
            "api_key",
            "authorization",
            "password",
            "passwd",
            "secret",
            "cookie",
            "access_token",
            "refresh_token",
            "bot_token",
            "service_account_json",
        )
    )


def _scrub_text(value: str, *, suffixes: tuple[str, ...]) -> str:
    with _SECRET_LOCK:
        known = tuple(_KNOWN_SECRETS)
    scrubbed = value
    for secret in sorted(known, key=len, reverse=True):
        if secret:
            scrubbed = scrubbed.replace(secret, REDACTED)
    for suffix in sorted(suffixes, key=len, reverse=True):
        if suffix:
            scrubbed = scrubbed.replace(suffix, REDACTED)
    scrubbed = _AUTH_PATTERN.sub(r"\1[redacted]", scrubbed)
    scrubbed = _URL_CREDENTIAL_PATTERN.sub(r"\1[redacted]@", scrubbed)
    scrubbed = _QUERY_SECRET_PATTERN.sub(r"\1[redacted]", scrubbed)
    scrubbed = _CREDENTIAL_PATTERN.sub(REDACTED, scrubbed)
    if len(scrubbed) > MAX_STRING_CHARS:
        return f"{scrubbed[:MAX_STRING_CHARS]}…[truncated]"
    return scrubbed


def _convert(
    value: object,
    *,
    field_name: str,
    suffixes: tuple[str, ...],
    depth: int,
    seen: set[int],
) -> JsonValue:
    if _is_sensitive_field(field_name):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if field_name == "key_suffix":
            return value[-4:]
        return _scrub_text(value, suffixes=suffixes)
    if isinstance(value, bytes):
        return {"type": "bytes", "size": len(value)}
    if depth >= MAX_DEPTH:
        return "[max-depth]"

    identity = id(value)
    if identity in seen:
        return "[cycle]"

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            items = list(value.items())[:MAX_COLLECTION_ITEMS]
            mapping_result: dict[str, JsonValue] = {
                str(key): _convert(
                    item,
                    field_name=str(key),
                    suffixes=suffixes,
                    depth=depth + 1,
                    seen=seen,
                )
                for key, item in items
            }
            if len(value) > len(items):
                mapping_result["truncated_fields"] = len(value) - len(items)
            return mapping_result
        finally:
            seen.remove(identity)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(identity)
        try:
            items = list(value)[:MAX_COLLECTION_ITEMS]
            list_result: list[JsonValue] = [
                _convert(item, field_name=field_name, suffixes=suffixes, depth=depth + 1, seen=seen) for item in items
            ]
            if len(value) > len(items):
                list_result.append(f"[truncated {len(value) - len(items)} items]")
            return list_result
        finally:
            seen.remove(identity)

    return f"<{type(value).__name__}>"


def sanitize_event(event: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Return a bounded JSON-compatible event with credentials removed."""
    suffix = event.get("key_suffix")
    suffixes = (suffix,) if isinstance(suffix, str) and suffix else ()
    converted = _convert(event, field_name="event", suffixes=suffixes, depth=0, seen=set())
    assert isinstance(converted, dict)
    return converted
