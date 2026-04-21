from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.games.judge import generate_hints
from app.games.word_bank import generate_words_for_category


@pytest.mark.asyncio
class TestLLMTasks:
    """LLM Mock tasks testing (LLM-01 to LLM-04)."""

    async def test_generate_hints_success(self):
        """LLM-01: Correctly parses HintsOutput schema from GenAI."""
        with patch("app.providers.router.ProviderRouter.get_response", new_callable=AsyncMock) as mock_get_response:
            mock_get_response.return_value = (json.dumps({"hints": ["Hint 1", "Hint 2", "Hint 3"]}), 10)

            result = await generate_hints("слово", "разное")

            assert result == ["Hint 1", "Hint 2", "Hint 3"]
            assert mock_get_response.await_count >= 1

    async def test_generate_hints_fallback(self):
        """LLM-02: Falls back to regex extraction if JSON decoding fails."""
        with patch("app.providers.router.ProviderRouter.get_response", new_callable=AsyncMock) as mock_get_response:
            # Provide non-JSON format that contains quoted hints to trigger fallback parsing
            mock_get_response.return_value = ('Here are the hints:\n1. "F_Hint 1"\n2. "F_Hint 2"\n3. "F_Hint 3"', 15)

            result = await generate_hints("слово", "разное")

            assert result == ["F_Hint 1", "F_Hint 2", "F_Hint 3"]
            assert mock_get_response.await_count >= 1

    async def test_generate_words_success(self):
        """LLM-03: Successfully runs and parses JSON array format."""
        with (
            patch("app.providers.router.ProviderRouter.get_response", new_callable=AsyncMock) as mock_get_response,
            patch("app.games.judgement_cache.get_cached_generated_words", new_callable=AsyncMock, return_value=None),
            patch("app.games.judgement_cache.cache_generated_words", new_callable=AsyncMock) as cache_mock,
        ):
            # Must be >= 5 words or validate_words returns None
            mock_get_response.return_value = ('["кот", "собака", "тигр", "медведь", "лиса"]', 10)

            words = await generate_words_for_category("зверолов_тест", lang="ru")

            assert words is not None
            assert len(words) == 5
            assert words[0] == "кот"
            assert words[1] == "собака"
            cache_mock.assert_awaited_once_with("ru", "зверолов_тест", words)

    async def test_generate_words_value_error_if_empty(self):
        """LLM-04: Returns None if empty array returned."""
        with patch("app.providers.router.ProviderRouter.get_response", new_callable=AsyncMock) as mock_get_response:
            mock_get_response.return_value = ("[]", 5)

            words = await generate_words_for_category("asdfasdfasdf", lang="ru")
            assert words is None
