"""Validation helpers for Telegram webhook secret tokens."""

from __future__ import annotations

import hmac
import re

_WEBHOOK_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


def validate_webhook_secret_token(value: str) -> str:
    """Validate the optional token against Telegram Bot API constraints."""
    if value == "":
        return value
    if not isinstance(value, str) or _WEBHOOK_SECRET_RE.fullmatch(value) is None:
        raise ValueError("WEBHOOK_SECRET_TOKEN must be 1-256 characters using only A-Z, a-z, 0-9, '_' or '-'")
    return value


def webhook_secret_matches(expected: str, provided: object) -> bool:
    """Compare valid string secrets without leaking content-dependent timing."""
    if not isinstance(expected, str) or not isinstance(provided, str):
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except TypeError:
        return False
