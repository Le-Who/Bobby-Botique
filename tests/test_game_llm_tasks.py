from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.games.judge import generate_hints
from app.games.word_bank import generate_words_for_category


@pytest.mark.asyncio
class TestLLMTasks:
    """LLM Mock tasks testing (LLM-01 to LLM-04)."""

    async def test_generate_hints_success(self):
        """LLM-01: Correctly parses HintsOutput schema from GenAI."""
        mock_response = MagicMock()
        mock_response.text = json.dumps({"hints": ["Hint 1", "Hint 2", "Hint 3"]})

        mock_client = AsyncMock()
        mock_client.aio.models.generate_content.return_value = mock_response

        with (
            patch("app.agent_use_cases.AgentRequestUseCase") as AgentRequestUseCase_mock,
            patch("app.providers.gemini.get_cached_genai_client", return_value=mock_client),
        ):
            use_case_inst = AgentRequestUseCase_mock.return_value
            # kd, mdl, _
            use_case_inst.resolve_ai_request = AsyncMock(return_value=({"api_key": "test_key"}, "test_model", None))

            result = await generate_hints("слово", "разное")

            assert result == ["Hint 1", "Hint 2", "Hint 3"]
            mock_client.aio.models.generate_content.assert_awaited_once()

    async def test_generate_hints_fallback(self):
        """LLM-02: Falls back if primary model throws exception."""
        # Setup so primary fails, secondary succeeds
        mock_response = MagicMock()
        mock_response.text = json.dumps({"hints": ["F_Hint 1", "F_Hint 2", "F_Hint 3"]})

        mock_client = AsyncMock()
        # Side effect: first call raises an error, second call returns mock_response
        mock_client.aio.models.generate_content.side_effect = [
            Exception("503 Service Unavailable"),
            mock_response,
        ]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase") as AgentRequestUseCase_mock,
            patch("app.providers.gemini.get_cached_genai_client", return_value=mock_client),
        ):
            use_case_inst = AgentRequestUseCase_mock.return_value
            # Always return a valid key/model
            use_case_inst.resolve_ai_request = AsyncMock(return_value=({"api_key": "test_key"}, "test_model", None))

            result = await generate_hints("слово", "разное")

            assert result == ["F_Hint 1", "F_Hint 2", "F_Hint 3"]
            # Awaited twice (primary then fallback)
            assert mock_client.aio.models.generate_content.await_count == 2

    async def test_generate_words_success(self):
        """LLM-03: Successfully runs and parses JSON array format."""
        mock_response = MagicMock()
        mock_response.text = '["кот", "собака", "тигр"]'

        mock_client_inst = MagicMock()
        mock_client_inst.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client_inst):
            words = await generate_words_for_category("Животные", lang="ru")

            assert words is not None
            assert len(words) == 3
            assert words[0] == "кот"
            assert words[1] == "собака"

    async def test_generate_words_value_error_if_empty(self):
        """LLM-04: Returns None if empty array returned."""
        mock_response = MagicMock()
        mock_response.text = '[]'

        mock_client_inst = MagicMock()
        mock_client_inst.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client_inst):
            words = await generate_words_for_category("asdfasdfasdf", lang="ru")
            assert words is None
