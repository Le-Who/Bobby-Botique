"""Runtime policies at background text caller boundaries."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_board_explicit_plan_uses_controlled_prompt_without_inline_race(monkeypatch):
    from app.handlers import board_handler, inline
    from app.process_policies import ResolvedPolicy

    monkeypatch.setattr(inline, "get_inline_model", AsyncMock(return_value="gemini-baseline"))
    race = AsyncMock()
    monkeypatch.setattr(inline, "_stream_inline_fast", race)
    monkeypatch.setattr(
        "app.process_policies.resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-selected",), "sequential", True, 5)),
    )
    execute = AsyncMock(return_value=("  Synthesized board  ", 10))
    monkeypatch.setattr("app.process_policies.execute_text_process", execute)
    assert (
        await board_handler._run_synthesis("Plans", [{"user": "Alice", "text": "idea {topic}"}]) == "Synthesized board"
    )
    race.assert_not_awaited()
    assert execute.await_args.args[0] == "board.synthesis"
    assert "idea {topic}" in execute.await_args.args[2][0]["parts"][0]
    assert execute.await_args.kwargs["timeout"] == 30.0


@pytest.mark.asyncio
async def test_summary_uses_controlled_prompt_and_resolved_text_process_inside_lease(monkeypatch):
    from app.context import summarizer
    from app.prompt_registry import get_prompt_text

    events = []
    callback = AsyncMock()

    @asynccontextmanager
    async def lease(user_id, expected_epoch, *, purpose, require_ltm):
        assert (user_id, expected_epoch, purpose, require_ltm) == (42, 9, "conversation:summary", False)
        events.append("lease_enter")
        yield True
        events.append("lease_exit")

    async def text_process(process_id, baseline, history, *, system_instruction, **kwargs):
        assert events == ["lease_enter"]
        assert process_id == "summary"
        assert baseline == (summarizer.SUMMARIZATION_MODEL,)
        assert system_instruction == get_prompt_text("summary.system")
        assert "private history" in history[0]["parts"][0]
        return "bounded summary", 17

    monkeypatch.setattr(summarizer, "split_into_chunks", lambda _: ["private history"])
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr(summarizer, "execute_text_process", text_process, raising=False)
    await summarizer._run_llm_summarization(42, 9, [{"role": "user", "parts": ["private history"]}], None, callback)
    callback.assert_awaited_once_with("bounded summary")
    assert events == ["lease_enter", "lease_exit"]
    assert "private history" not in get_prompt_text("summary.system")


@pytest.mark.asyncio
async def test_voice_classifier_uses_text_process_and_preserves_timeout(monkeypatch):
    from app import voice_intent
    from app.prompt_registry import get_prompt_text

    calls = []

    async def text_process(process_id, baseline, history, *, system_instruction, timeout):
        calls.append((process_id, baseline, history, system_instruction, timeout))
        return "YES", 1

    monkeypatch.setattr(voice_intent, "execute_text_process", text_process, raising=False)
    monkeypatch.setattr(
        voice_intent,
        "settings",
        SimpleNamespace(OPENCODE_INLINE_MODEL="opencode-go/inline", OPENCODE_QNA_MODEL="opencode-go/qna"),
    )
    decision = await voice_intent._classify_ambiguous_tts_intent(
        user_text="quoted voice request",
        llm_context="a forwarded message",
        user_entry_count=1,
        forwarded_entry_count=1,
    )
    assert decision.explicit_tts is True
    assert calls[0][0] == "intent.voice"
    assert calls[0][1] == ("opencode-go/inline",)
    assert calls[0][3] == get_prompt_text("intent.voice.system")
    assert calls[0][4] == 12.0
    assert "quoted voice request" in calls[0][2][0]["parts"][0]


@pytest.mark.asyncio
async def test_brief_summary_keeps_json_budget_with_configured_model(monkeypatch):
    from app.handlers import scheduled_briefs as briefs

    generate = AsyncMock(return_value=SimpleNamespace(text='{"News":"A short summary."}'))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    async def chosen(process_id, baseline, execute, *, initial_api_key=None):
        assert process_id == "brief.summary"
        assert baseline == ("gemini-3.1-flash-lite",)
        return await execute("gemini-3.8-flash", "test-key")

    monkeypatch.setattr(briefs, "run_gemini_override", chosen, raising=False)
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key", AsyncMock(return_value={"api_key": "test-key", "key_hash": "hash"})
    )
    result = await briefs._generate_brief_summary(["Technology"], [])
    assert result == {"News": "A short summary."}
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    config = generate.await_args.kwargs["config"]
    assert config.temperature == 0.3
    assert config.max_output_tokens == 1200
