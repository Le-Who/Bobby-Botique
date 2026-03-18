"""Tests for app.config helper functions — pure parsing logic."""

import os

import pytest

from app.config import _load_and_clean_keys, _load_daily_limits, get_model_hash

# ── get_model_hash ───────────────────────────────────────────────────────────


class TestGetModelHash:
    """get_model_hash should produce deterministic 8-char hex strings."""

    def test_returns_8_char_hex(self):
        h = get_model_hash("gemini-2.5-flash")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert get_model_hash("gemini-2.5-flash") == get_model_hash("gemini-2.5-flash")

    def test_different_models_different_hashes(self):
        assert get_model_hash("gemini-2.5-flash") != get_model_hash("gemini-3.1-flash-lite")


# ── _load_and_clean_keys ─────────────────────────────────────────────────────


class TestLoadAndCleanKeys:
    """_load_and_clean_keys should parse comma-separated env vars robustly."""

    def test_simple_keys(self, monkeypatch):
        monkeypatch.setenv("TEST_KEYS", "key1,key2,key3")
        result = _load_and_clean_keys("TEST_KEYS")
        assert result == ["key1", "key2", "key3"]

    def test_keys_with_whitespace(self, monkeypatch):
        monkeypatch.setenv("TEST_KEYS", "  key1 , key2 , key3  ")
        result = _load_and_clean_keys("TEST_KEYS")
        assert result == ["key1", "key2", "key3"]

    def test_keys_with_quotes(self, monkeypatch):
        monkeypatch.setenv("TEST_KEYS", '"key1,key2"')
        result = _load_and_clean_keys("TEST_KEYS")
        assert result == ["key1", "key2"]

    def test_empty_parts_filtered(self, monkeypatch):
        monkeypatch.setenv("TEST_KEYS", "key1,,key2,")
        result = _load_and_clean_keys("TEST_KEYS")
        assert result == ["key1", "key2"]

    def test_not_set_required_raises(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_KEYS_12345", raising=False)
        with pytest.raises(ValueError, match="not set"):
            _load_and_clean_keys("NONEXISTENT_KEYS_12345", required=True)

    def test_not_set_optional_returns_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_KEYS_12345", raising=False)
        result = _load_and_clean_keys("NONEXISTENT_KEYS_12345", required=False)
        assert result == []

    def test_empty_value_required_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_KEYS", "")
        with pytest.raises(ValueError):
            _load_and_clean_keys("TEST_KEYS", required=True)


# ── _load_daily_limits ───────────────────────────────────────────────────────


class TestLoadDailyLimits:
    """_load_daily_limits should parse JSON and compact formats."""

    def test_json_format(self, monkeypatch):
        monkeypatch.setenv("DAILY_LIMITS", '{"gemini-2.5-flash": 250, "gemini-2.5-flash-lite": 15}')
        result = _load_daily_limits()
        assert result == {"gemini-2.5-flash": 250, "gemini-2.5-flash-lite": 15}

    def test_compact_format(self, monkeypatch):
        monkeypatch.setenv("DAILY_LIMITS", "gemini-2.5-flash:250,gemini-2.5-flash-lite:15")
        result = _load_daily_limits()
        assert result == {"gemini-2.5-flash": 250, "gemini-2.5-flash-lite": 15}

    def test_defaults_when_not_set(self, monkeypatch):
        monkeypatch.delenv("DAILY_LIMITS", raising=False)
        result = _load_daily_limits()
        assert isinstance(result, dict)
        assert len(result) > 0
        assert all(isinstance(v, int) for v in result.values())

    def test_invalid_format_returns_defaults(self, monkeypatch):
        monkeypatch.setenv("DAILY_LIMITS", "totally invalid data !@#$")
        result = _load_daily_limits()
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_quoted_json(self, monkeypatch):
        monkeypatch.setenv("DAILY_LIMITS", "'\"gemini-2.5-flash\": 100'")
        # Quoted but incomplete JSON — should fallback to defaults
        result = _load_daily_limits()
        assert isinstance(result, dict)
