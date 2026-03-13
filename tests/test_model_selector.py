"""Tests for app.model_selector — smart model auto-selection heuristics."""

import pytest

from app.model_selector import SelectionResult, _find_model, _get_tier, select_model

# ── _get_tier ────────────────────────────────────────────────────────────────


class TestGetTier:
    """Tier ranking should follow the hierarchy: pro > 2.5-flash > flash > flash-lite."""

    def test_pro_is_highest(self):
        assert _get_tier("gemini-2.5-pro-preview") == 4

    def test_flash_25_is_tier_3(self):
        assert _get_tier("gemini-2.5-flash") == 3

    def test_flash_lite_is_tier_1(self):
        assert _get_tier("gemini-2.5-flash-lite") == 1

    def test_plain_flash_is_tier_2(self):
        assert _get_tier("gemini-2.0-flash") == 2

    def test_unknown_model_gets_middle_tier(self):
        assert _get_tier("some-unknown-model") == 2

    def test_case_insensitive(self):
        assert _get_tier("GEMINI-2.5-PRO") == 4


# ── _find_model ──────────────────────────────────────────────────────────────


class TestFindModel:
    """First preferred model that's available should be returned."""

    def test_returns_first_match(self):
        available = ["gemini-2.5-flash", "gemini-2.5-pro-preview"]
        result = _find_model(available, ["2.5-pro", "flash"])
        assert result == "gemini-2.5-pro-preview"

    def test_returns_none_when_no_match(self):
        available = ["gemini-2.0-flash"]
        result = _find_model(available, ["2.5-pro", "2.5-flash"])
        assert result is None

    def test_empty_available_returns_none(self):
        assert _find_model([], ["2.5-pro"]) is None

    def test_empty_preferences_returns_none(self):
        assert _find_model(["gemini-2.5-flash"], []) is None


# ── select_model ─────────────────────────────────────────────────────────────


class TestSelectModel:
    """Model selection should suggest upgrades based on message content, never downgrades."""

    @pytest.fixture(autouse=True)
    def _patch_settings(self, monkeypatch):
        """Ensure AVAILABLE_MODELS has predictable content."""
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro-preview",
        ]
        monkeypatch.setattr("app.model_selector.settings", mock_settings)

    # ── Code detection ───────────────────────────────────────────────────

    def test_code_message_suggests_pro(self):
        result = select_model("```python\ndef hello(): pass\n```", current_model="gemini-2.5-flash")
        assert result is not None
        assert "pro" in result.model.lower()
        assert result.confidence > 0

    def test_code_keywords_detected(self):
        for keyword in ["def main()", "class Foo:", "import os", "debug this"]:
            result = select_model(keyword, current_model="gemini-2.5-flash")
            assert result is not None, f"Failed to detect code keyword: {keyword}"

    # ── Reasoning detection ──────────────────────────────────────────────

    def test_reasoning_message_suggests_upgrade(self):
        result = select_model(
            "Проанализируй разницу между async и threading",
            current_model="gemini-2.5-flash-lite",
        )
        assert result is not None
        assert _get_tier(result.model) > 1  # Should suggest something higher than flash-lite

    def test_long_message_suggests_upgrade(self):
        long_msg = "x " * 600  # >1000 chars
        result = select_model(long_msg, current_model="gemini-2.5-flash-lite")
        assert result is not None

    # ── No suggestion cases ──────────────────────────────────────────────

    def test_simple_greeting_returns_none(self):
        assert select_model("привет!", current_model="gemini-2.5-flash") is None

    def test_no_available_models_returns_none(self, monkeypatch):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = []
        monkeypatch.setattr("app.model_selector.settings", mock_settings)
        assert select_model("complex analysis query") is None

    def test_no_downgrade_from_pro(self):
        """If user is already on pro, no suggestion should be made."""
        result = select_model("simple question", current_model="gemini-2.5-pro-preview")
        assert result is None

    def test_same_model_not_suggested(self):
        """Should not suggest switching to the same model."""
        result = select_model("```\ncode block\n```", current_model="gemini-2.5-pro-preview")
        assert result is None

    # ── SelectionResult structure ────────────────────────────────────────

    def test_result_has_correct_fields(self):
        result = select_model("Объясни подробно как работает GIL", current_model="gemini-2.5-flash-lite")
        assert result is not None
        assert isinstance(result, SelectionResult)
        assert isinstance(result.model, str)
        assert isinstance(result.reason, str)
        assert 0.0 <= result.confidence <= 1.0
