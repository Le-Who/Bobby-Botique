"""Policy-controlled, bounded content metadata and diagnostic previews."""

from __future__ import annotations

import hashlib

from app.observability.config import LoggingSettings
from app.observability.diagnostics import diagnostic_preview_allowed
from app.observability.redaction import sanitize_event
from app.observability.schema import JsonValue

MAX_PREVIEW_CHARS = 256
MAX_FULL_CONTENT_CHARS = 2048


def content_fields(
    content_type: str,
    text: str | None,
    *,
    sensitive: bool = False,
    subsystem: str | None = None,
) -> dict[str, JsonValue]:
    """Describe content and include bounded text under the operational policy."""
    value = text if isinstance(text, str) else ""
    encoded = value.encode()
    result: dict[str, JsonValue] = {
        "content_type": content_type,
        "content_chars": len(value),
        "content_bytes": len(encoded),
        "content_fingerprint": hashlib.sha256(encoded).hexdigest()[:16],
        "content_policy": "forbidden" if sensitive else "metadata",
    }
    mode = LoggingSettings.from_environ().content_mode
    if mode == "full" and not sensitive and value:
        sanitized = sanitize_event({"content_text": value})
        full_text = sanitized.get("content_text")
        if isinstance(full_text, str):
            result["content_text"] = full_text[:MAX_FULL_CONTENT_CHARS]
            result["content_policy"] = "full"
            result["content_truncated"] = len(value) > MAX_FULL_CONTENT_CHARS
        return result

    preview_enabled = mode == "preview" and diagnostic_preview_allowed(subsystem=subsystem or content_type)
    if preview_enabled and not sensitive and value:
        sanitized = sanitize_event({"content_preview": value})
        preview = sanitized.get("content_preview")
        if isinstance(preview, str):
            result["content_preview"] = preview[:MAX_PREVIEW_CHARS]
            result["content_policy"] = "preview"
            result["content_truncated"] = len(value) > MAX_PREVIEW_CHARS
    return result


__all__ = ["content_fields"]
