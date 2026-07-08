"""Tests for app.model_selector — smart model auto-selection heuristics."""

import pytest

from app.model_selector import SelectionResult, _find_model, _get_tier, select_model

# ── _get_tier ────────────────────────────────────────────────────────────────


class TestGetTier:
    """Tier ranking should follow the hierarchy: 3.5-flash > 3.1-flash-lite > stale models."""

    def test_3_5_flash_is_highest(self):
        assert _get_tier("gemini-3.5-flash") == 5

    def test_3_1_flash_lite_is_tier_4(self):
        assert _get_tier("gemini-3.1-flash-lite") == 4

    def test_stale_flash_is_not_ranked_as_current(self):
        assert _get_tier("gemini-2.5-flash") < _get_tier("gemini-3.1-flash-lite")
        assert _get_tier("gemini-3-flash-preview") < _get_tier("gemini-3.5-flash")

    def test_unknown_model_gets_middle_tier(self):
        assert _get_tier("some-unknown-model") == 2

    def test_case_insensitive(self):
        assert _get_tier("GEMINI-3.1-FLASH-LITE") == 4


# ── _find_model ──────────────────────────────────────────────────────────────


class TestFindModel:
    """First preferred model that's available should be returned."""

    def test_returns_first_match(self):
        available = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]
        result = _find_model(available, ["3.1-flash-lite", "3.5-flash"])
        assert result == "gemini-3.1-flash-lite"

    def test_returns_none_when_no_match(self):
        available = ["gemini-3.1-flash-lite"]
        result = _find_model(available, ["3.0-flash", "2.5-flash"])
        assert result is None

    def test_empty_available_returns_none(self):
        assert _find_model([], ["3.0-flash"]) is None

    def test_empty_preferences_returns_none(self):
        assert _find_model(["gemini-3.5-flash"], []) is None


# ── select_model ─────────────────────────────────────────────────────────────


class TestSelectModel:
    """Model selection should suggest upgrades based on message content, never downgrades."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, monkeypatch):
        """Ensure AVAILABLE_MODELS has predictable content."""
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
        ]
        # Tier hierarchy: 3.5-flash=5, 3.1-flash-lite=4
        monkeypatch.setattr("app.model_selector.settings", mock_settings)

    # ── Code detection ───────────────────────────────────────────────────

    def test_code_message_suggests_top_tier(self):
        result = select_model("```python\ndef hello(): pass\n```", current_model="gemini-3.1-flash-lite")
        assert result is not None
        assert result.model == "gemini-3.5-flash"
        assert result.confidence > 0

    def test_code_keywords_detected(self):
        for keyword in ["def main()", "class Foo:", "import os", "debug this"]:
            result = select_model(keyword, current_model="gemini-3.1-flash-lite")
            assert result is not None, f"Failed to detect code keyword: {keyword}"

    # ── Reasoning detection ──────────────────────────────────────────────

    def test_reasoning_message_suggests_upgrade(self):
        result = select_model(
            "Проанализируй разницу между async и threading",
            current_model="gemini-3.1-flash-lite",
        )
        assert result is not None
        assert result.model == "gemini-3.5-flash"

    def test_long_message_suggests_upgrade(self):
        long_msg = "x " * 600  # >1000 chars
        result = select_model(long_msg, current_model="gemini-3.1-flash-lite")
        assert result is not None

    # ── No suggestion cases ──────────────────────────────────────────────

    def test_simple_greeting_returns_none(self):
        assert select_model("привет!", current_model="gemini-3.5-flash") is None

    def test_no_available_models_returns_none(self, monkeypatch):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = []
        monkeypatch.setattr("app.model_selector.settings", mock_settings)
        assert select_model("complex analysis query") is None

    def test_no_downgrade_from_top_tier(self):
        """If user is already on top tier, no suggestion should be made."""
        result = select_model("simple question", current_model="gemini-3.5-flash")
        assert result is None

    def test_same_model_not_suggested(self):
        """Should not suggest switching to the same model (already at top available tier)."""
        result = select_model("```\ncode block\n```", current_model="gemini-3.5-flash")
        assert result is None

    # ── SelectionResult structure ────────────────────────────────────────

    def test_result_has_correct_fields(self):
        result = select_model("Объясни подробно как работает GIL", current_model="gemini-3.1-flash-lite")
        assert result is not None
        assert isinstance(result, SelectionResult)
        assert isinstance(result.model, str)
        assert isinstance(result.reason, str)
        assert 0.0 <= result.confidence <= 1.0
