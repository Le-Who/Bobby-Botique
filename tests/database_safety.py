"""Fail-closed helpers that keep database-backed tests off production."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def database_identity(value: str | None):
    """Compare database targets without treating credentials as isolation."""
    if not value:
        return None
    parsed = urlsplit(value)
    return parsed.hostname, parsed.port or 5432, parsed.path.rstrip("/")


def database_target_is_forbidden(test_url: str | None, production_url: str | None) -> bool:
    """Reject a matching target unless CI explicitly identifies a local service DB."""
    test_identity = database_identity(test_url)
    if test_identity is None or test_identity != database_identity(production_url):
        return False

    explicit_ephemeral_ci = (
        os.getenv("GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL", "").lower() == "true"
        and os.getenv("GITHUB_ACTIONS", "").lower() == "true"
        and test_identity[0] in _LOOPBACK_HOSTS
    )
    return not explicit_ephemeral_ci
