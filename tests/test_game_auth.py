# tests/test_game_auth.py
"""Unit tests for WebSocket authentication helpers in app/web_miniapp.py.

Tests are fully offline — no Quart app, no WebSocket, no Telegram API calls.
All functions under test are pure (no I/O): _validate_init_data and
_extract_user_id.

Note: The former _sweep_game_locks function and its tests were removed because
FIFO lock eviction was identified as a critical concurrency defect — evicting
an actively-held asyncio.Lock breaks mutual exclusion when Redis is unavailable.
The _local_locks dict is now unbounded (same fix as _PREP_LOCKS).
"""

from __future__ import annotations

from app.web_miniapp import _extract_user_id, _validate_init_data
from tests.factories import make_valid_init_data

# A deterministic fake bot token used throughout this module.
_BOT_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


# ── _validate_init_data ───────────────────────────────────────────────────────


class TestValidateInitData:
    """_validate_init_data(init_data, bot_token) → dict | None."""

    def test_valid_hash_returns_dict(self):
        """Valid HMAC-signed initData must be accepted and parsed."""
        init_data = make_valid_init_data(_BOT_TOKEN, user_id=42)
        result = _validate_init_data(init_data, _BOT_TOKEN)
        assert result is not None
        assert isinstance(result["user"], dict)
        assert result["user"]["id"] == 42

    def test_tampered_hash_returns_none(self):
        """Any mutation of the payload after signing must be rejected."""
        init_data = make_valid_init_data(_BOT_TOKEN, user_id=42)
        # Corrupt the last character of the hash value
        tampered = init_data[:-1] + ("0" if init_data[-1] != "0" else "1")
        result = _validate_init_data(tampered, _BOT_TOKEN)
        assert result is None

    def test_missing_hash_field_returns_none(self):
        """initData without a 'hash' key must be rejected immediately."""
        # Build params without signing
        init_data = "user=%7B%22id%22%3A42%7D&auth_date=9999999999"
        result = _validate_init_data(init_data, _BOT_TOKEN)
        assert result is None

    def test_wrong_bot_token_returns_none(self):
        """Data signed with token A must fail validation against token B."""
        init_data = make_valid_init_data(_BOT_TOKEN, user_id=7)
        result = _validate_init_data(init_data, "9999999999:WRONG_TOKEN_XXXXXXXXXXXXXXXXXX")
        assert result is None

    def test_empty_string_returns_none(self):
        result = _validate_init_data("", _BOT_TOKEN)
        assert result is None

    def test_malformed_string_returns_none(self):
        """Garbage input must not raise — must return None."""
        result = _validate_init_data("not-url-encoded@#$%", _BOT_TOKEN)
        assert result is None


# ── _extract_user_id ──────────────────────────────────────────────────────────


class TestExtractUserId:
    """_extract_user_id(validated_data) → int | None."""

    def test_valid_user_dict_returns_id(self):
        data = {"user": {"id": 123456, "username": "alice"}}
        assert _extract_user_id(data) == 123456

    def test_missing_user_key_returns_none(self):
        assert _extract_user_id({}) is None

    def test_user_is_non_dict_returns_none(self):
        """Malformed payload where 'user' is not a dict must return None."""
        assert _extract_user_id({"user": "not-a-dict"}) is None
        assert _extract_user_id({"user": None}) is None
        assert _extract_user_id({"user": 42}) is None

    def test_user_dict_without_id_returns_none(self):
        assert _extract_user_id({"user": {"username": "alice"}}) is None
