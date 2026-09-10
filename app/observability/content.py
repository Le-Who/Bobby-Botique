"""Policy-controlled, bounded content metadata and diagnostic previews."""

from __future__ import annotations

import hashlib

from app.observability.diagnostics import diagnostic_preview_allowed
from app.observability.redaction import sanitize_event
from app.observability.schema import JsonValue

MAX_PREVIEW_CHARS = 256


def content_fields(
    content_type: str,
    text: str | None,
    *,
    sensitive: bool = False,
    subsystem: str | None = None,
) -> dict[str, JsonValue]:
    """Describe content safely, adding a scrubbed preview only by explicit policy."""
    value = text if isinstance(text, str) else ""
    encoded = value.encode()
    result: dict[str, JsonValue] = {
        "content_type": content_type,
        "content_chars": len(value),
        "content_bytes": len(encoded),
        "content_fingerprint": hashlib.sha256(encoded).hexdigest()[:16],
        "content_policy": "forbidden" if sensitive else "metadata",
    }
    preview_enabled = diagnostic_preview_allowed(subsystem=subsystem or content_type)
    if preview_enabled and not sensitive and value:
        sanitized = sanitize_event({"content_preview": value[:MAX_PREVIEW_CHARS]})
        preview = sanitized.get("content_preview")
        if isinstance(preview, str):
            result["content_preview"] = preview
            result["content_policy"] = "preview"
            result["content_truncated"] = len(value) > MAX_PREVIEW_CHARS
    return result


__all__ = ["content_fields"]
