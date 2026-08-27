"""Tests for app.context.summarizer — pure chunking and text extraction logic."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.context.summarizer import (
    SummarizationInputTooLarge,
    _extract_text,
    _run_llm_summarization,
    split_into_chunks,
)


@pytest.fixture(autouse=True)
def allow_summary_private_data_lease():
    @asynccontextmanager
    async def allowed_lease(*_args, **_kwargs):
        yield True

    with patch("app.repos.memory_consent.private_data_lease", allowed_lease):
        yield


# ── _extract_text ────────────────────────────────────────────────────────────


class TestExtractText:
    """_extract_text should handle various message dict formats."""

    def test_string_parts(self):
        msg = {"role": "user", "parts": ["Hello", "World"]}
        assert _extract_text(msg) == "Hello World"

    def test_dict_parts_with_text(self):
        msg = {"role": "model", "parts": [{"text": "Response"}]}
        assert _extract_text(msg) == "Response"

    def test_content_field_fallback(self):
        msg = {"role": "user", "content": "Fallback content"}
        assert _extract_text(msg) == "Fallback content"

    def test_content_list_fallback(self):
        msg = {"role": "user", "content": ["part1", "part2"]}
        result = _extract_text(msg)
        assert "part1" in result
        assert "part2" in result

    def test_empty_parts(self):
        msg = {"role": "user", "parts": []}
        result = _extract_text(msg)
        assert result == ""

    def test_mixed_parts(self):
        msg = {"parts": ["text1", {"text": "text2"}, {"image": "data"}]}
        result = _extract_text(msg)
        assert "text1" in result
        assert "text2" in result


# ── split_into_chunks ────────────────────────────────────────────────────────


class TestSplitIntoChunks:
    """split_into_chunks should split messages into ~CHUNK_SIZE token chunks."""

    def test_empty_messages(self):
        assert split_into_chunks([]) == []

    def test_single_short_message(self):
        messages = [{"role": "user", "parts": ["Hello"]}]
        chunks = split_into_chunks(messages)
        assert len(chunks) == 1
        assert "user: Hello" in chunks[0]

    def test_role_labels_preserved(self):
        messages = [
            {"role": "user", "parts": ["Question"]},
            {"role": "model", "parts": ["Answer"]},
        ]
        chunks = split_into_chunks(messages)
        joined = "\n".join(chunks)
        assert "user: Question" in joined
        assert "model: Answer" in joined

    def test_messages_without_text_skipped(self):
        messages = [
            {"role": "user", "parts": ["Hello"]},
            {"role": "model", "parts": []},  # Empty — should be skipped
        ]
        chunks = split_into_chunks(messages)
        joined = "\n".join(chunks)
        assert "model:" not in joined

    def test_max_chunks_respected(self):
        from app.context.token_budget import MAX_CHUNKS

        # Create many messages to exceed MAX_CHUNKS
        messages = [{"role": "user", "parts": ["x" * 5000]} for _ in range(MAX_CHUNKS + 5)]
        chunks = split_into_chunks(messages)
        assert len(chunks) <= MAX_CHUNKS

    def test_large_messages_split_into_multiple_chunks(self):
        from app.context.token_budget import CHUNK_SIZE

        # Create a message that exceeds CHUNK_SIZE tokens (~2 chars per token for Cyrillic)
        big_msg = "a" * (CHUNK_SIZE * 3)
        messages = [
            {"role": "user", "parts": [big_msg]},
            {"role": "user", "parts": ["short"]},
        ]
        chunks = split_into_chunks(messages)
        assert len(chunks) >= 1
        assert all(CHUNK_SIZE >= _estimated_tokens(chunk) for chunk in chunks)

    def test_refuses_more_bounded_chunks_than_cost_limit(self, monkeypatch):
        import app.context.summarizer as summarizer

        monkeypatch.setattr(summarizer, "CHUNK_SIZE", 5)
        monkeypatch.setattr(summarizer, "MAX_CHUNKS", 2)

        with pytest.raises(SummarizationInputTooLarge):
            summarizer.split_into_chunks([{"role": "user", "parts": ["long-message-" + "x" * 100]}])


@pytest.mark.asyncio
async def test_oversized_input_keeps_local_summary_without_external_call(monkeypatch):
    import app.context.summarizer as summarizer

    monkeypatch.setattr(summarizer, "CHUNK_SIZE", 5)
    monkeypatch.setattr(summarizer, "MAX_CHUNKS", 1)
    callback = AsyncMock()

    with patch(
        "app.handlers.ai_core._get_ai_response_with_routing",
        new_callable=AsyncMock,
    ) as external_summary:
        await _run_llm_summarization(
            123,
            7,
            [{"role": "user", "parts": ["long-message-" + "x" * 100]}],
            "previous summary",
            callback,
        )

    external_summary.assert_not_awaited()
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_summary_cap_is_utf8_token_aware():
    from app.context.token_budget import SUMMARY_BUDGET
    from app.prompt_registry import estimate_tokens_cyrillic

    callback = AsyncMock()
    oversized = ("Пам'ять 🔮 " * 10_000).strip()

    with (
        patch("app.context.summarizer.split_into_chunks", return_value=["small chunk"]),
        patch(
            "app.handlers.ai_core._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=oversized,
        ),
    ):
        await _run_llm_summarization(
            123,
            7,
            [{"role": "user", "parts": ["small"]}],
            None,
            callback,
        )

    delivered = callback.await_args.args[0]
    assert delivered.endswith("...")
    assert estimate_tokens_cyrillic(delivered) <= SUMMARY_BUDGET


def _estimated_tokens(text: str) -> int:
    from app.prompt_registry import estimate_tokens_cyrillic

    return estimate_tokens_cyrillic(text)
