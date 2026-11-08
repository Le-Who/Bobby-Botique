"""Fast consumer routes keep selected OpenAI calls out of the Gemini SDK."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.selected_text import generate_selected_text
from app.process_policies import ResolvedPolicy
from app.providers.stream_types import (
    FinishKind,
    FinishReason,
    GroundingReport,
    ProviderKind,
    RouteUsed,
    StreamCompleted,
    TextDelta,
    TokenUsage,
)
from app.runtime_settings import lifecycle
from app.runtime_settings.store import SettingsSnapshot

MODEL = "openai-direct/gpt-6-luna"
POLICY = ResolvedPolicy((MODEL,), "sequential", True, 42)
JUDGEMENT = '{"status":"warm","score":0.6,"hint":"Есть сходство"}'


@pytest.fixture
def routed(monkeypatch):
    requests = []
    state = SimpleNamespace(text=JUDGEMENT, failure=None, hang=False, closed=False)

    async def stream(request):
        requests.append(request)
        try:
            if state.hang:
                await asyncio.Event().wait()
            if state.failure:
                raise state.failure
            yield TextDelta(state.text)
            yield StreamCompleted(
                FinishReason(FinishKind.STOP),
                TokenUsage(total=8),
                GroundingReport(),
                RouteUsed(ProviderKind.OPENROUTER, MODEL, MODEL),
            )
        finally:
            state.closed = True

    monkeypatch.setattr("app.providers.get_provider_router", lambda: SimpleNamespace(stream=stream))
    monkeypatch.setattr("app.process_policies.resolve_process", AsyncMock(return_value=POLICY))
    state.requests = requests
    return state


@pytest.mark.asyncio
async def test_selected_text_has_no_implicit_fallback_and_closes_stream(routed):
    assert await generate_selected_text(POLICY, "prompt", timeout=1.0) == JUDGEMENT
    request = routed.requests[0]
    assert request.models == (MODEL,)
    assert request.policy_revision == 42
    assert not request.allow_model_fallback
    assert not request.allow_deferred
    assert not request.allow_key_race
    assert routed.closed


@pytest.mark.asyncio
async def test_selected_text_deadline_cancels_owned_stream(routed):
    routed.hang = True
    with pytest.raises(TimeoutError):
        await generate_selected_text(POLICY, "prompt", timeout=0.01)
    assert routed.closed
    assert len(routed.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_classic_judge_openai_never_returns_to_gemini(monkeypatch, routed, failed):
    from app.games import daily_ai, judge, judgement_cache

    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value=MODEL))
    monkeypatch.setattr(daily_ai, "resolve_process", AsyncMock(return_value=POLICY))
    gemini = AsyncMock(side_effect=AssertionError("Gemini must not be called"))
    monkeypatch.setattr(daily_ai, "generate_daily_text", gemini)
    monkeypatch.setattr(judge, "_race_generate", gemini)
    monkeypatch.setattr(judge, "cache_identity", AsyncMock(return_value="openai-test"))
    monkeypatch.setattr(judgement_cache, "get_cached_judgement", AsyncMock(return_value=None))
    monkeypatch.setattr(judgement_cache, "cache_judgement", AsyncMock())
    if failed:
        routed.failure = TimeoutError()
    status, result = await judge.judge_guess("кот", "тигр", category="животные")
    assert status == ("judge_unavailable" if failed else "warm")
    assert result.score == (0.0 if failed else 0.6)
    assert len(routed.requests) == 1
    gemini.assert_not_awaited()


@pytest.mark.asyncio
async def test_openai_judge_failure_does_not_count_daily_player_attempt(monkeypatch, routed):
    from app.games import crocodile_daily, daily_ai, judge, judgement_cache
    from app.repos import crocodile_daily as repo

    routed.failure = TimeoutError()
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value=MODEL))
    monkeypatch.setattr(daily_ai, "resolve_process", AsyncMock(return_value=POLICY))
    monkeypatch.setattr(judge, "cache_identity", AsyncMock(return_value="openai-test"))
    monkeypatch.setattr(judgement_cache, "get_cached_judgement", AsyncMock(return_value=None))
    puzzle = repo.DailyPuzzle(date(2026, 11, 9), "кот", "животные", "ru")
    monkeypatch.setattr(
        crocodile_daily,
        "get_daily_state",
        AsyncMock(return_value=(puzzle, SimpleNamespace(status="active", attempts=[]))),
    )
    save = AsyncMock()
    monkeypatch.setattr(repo, "append_attempt_and_maybe_finish", save)
    result = await crocodile_daily.process_daily_guess(42, "тигр")
    assert result["event"] == "judge_unavailable"
    assert len(routed.requests) == 1
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,expected", [("_extract_draw_prompt_ai", "a cat"), ("_translate_to_english", "a cat")])
async def test_image_prompt_helpers_use_selected_text(monkeypatch, routed, method, expected):
    from app.handlers import cmd_image

    routed.text = "a cat"
    gemini = AsyncMock(side_effect=AssertionError("Gemini must not be called"))
    monkeypatch.setattr("app.runtime_settings.gemini_execution.run_gemini_override", gemini)
    assert await getattr(cmd_image, method)("кот") == expected
    assert routed.requests[0].system_instruction
    gemini.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_intent_uses_selected_text_without_gemini_resilience(monkeypatch, routed):
    from google.genai import types

    from app.utils import multimodal_processor as media

    routed.text = "INTENT:SEARCH"
    monkeypatch.setattr(media, "resolve_process", AsyncMock(return_value=POLICY))
    gemini = AsyncMock(side_effect=AssertionError("Gemini must not be called"))
    monkeypatch.setattr(media, "_generate_with_resilience", gemini)
    assert await media._classify_intent_with_fallback([types.Part.from_text(text="weather")]) == "INTENT:SEARCH"
    gemini.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_memory_expansion_keeps_original_on_selected_provider_failure(monkeypatch, routed, failed):
    from app.repos import memory

    routed.text = "Python project"
    if failed:
        routed.failure = RuntimeError("unavailable")
    gemini = AsyncMock(side_effect=AssertionError("Gemini must not be called"))
    monkeypatch.setattr(memory, "run_gemini_override", gemini)
    assert await memory.expand_query_with_llm("That coding project?", "test-key") == (
        "That coding project?" if failed else "Python project"
    )
    gemini.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_relevance_routes_under_existing_consent_check(monkeypatch, routed):
    from app.repos import memory

    routed.text = '[{"index":0,"relevant":true}]'
    monkeypatch.setattr(memory, "search_memories", AsyncMock(return_value=[{"id": 17, "content": "Alice codes"}]))
    consent = AsyncMock(return_value=True)
    monkeypatch.setattr(memory, "_is_ltm_read_enabled", consent)
    gemini = AsyncMock(side_effect=AssertionError("Gemini must not be called"))
    monkeypatch.setattr(memory, "run_gemini_override", gemini)
    result = await memory._search_memories_with_llm_judge_impl(42, "What does Alice do?", "test-key", expected_epoch=3)
    assert result == [{"id": 17, "content": "Alice codes", "llm_judged": True}]
    consent.assert_awaited_once_with(42, 3)
    assert routed.requests[0].scope.user_id == 42
    assert routed.requests[0].scope.privacy_epoch == 3
    gemini.assert_not_awaited()


def test_openai_menu_models_use_same_callback_order_and_provider_label(monkeypatch):
    from app.handlers import menus

    configured = SimpleNamespace(
        AVAILABLE_MODELS=["gemini-test"],
        OPENCODE_AVAILABLE_MODELS=[],
        OPENROUTER_AVAILABLE_MODELS=[],
        FREETHEAI_AVAILABLE_MODELS=[],
        OPENAI_AVAILABLE_MODELS=[MODEL],
    )
    monkeypatch.setattr(menus, "settings", configured)
    monkeypatch.setattr(menus, "get_openrouter_keys", list)
    assert menus.get_visible_model_list() == ["gemini-test", MODEL]
    text, _, keyboard = menus.get_model_menu_content(SimpleNamespace(model=MODEL), None)
    assert "OpenAI" in text
    buttons = [
        button for row in keyboard.inline_keyboard for button in row if button.callback_data.startswith("model:")
    ]
    assert [button.callback_data.split(":")[1] for button in buttons] == ["0", "1"]
    assert "gpt-6-luna" in buttons[1].text


@pytest.mark.parametrize("provider,model", [("openai", MODEL), ("gemini", "gemini-admin")])
def test_active_profile_menu_shows_effective_model_without_changing_saved_preference(monkeypatch, provider, model):
    from app.handlers import menus

    snapshot = SettingsSnapshot(42, {"live_route:provider": provider, "process:chat": {"models": [model]}})
    monkeypatch.setattr(lifecycle, "current_operation_snapshot", lambda: snapshot)
    chat_state = SimpleNamespace(model="gemini-previous-user-choice")
    text, _, keyboard = menus.get_model_menu_content(chat_state, None)
    assert model in text
    assert "администратором" in text
    assert "previous-user-choice" not in text
    assert menus.get_visible_model_list() == [model]
    assert all(not button.callback_data.startswith("model:") for row in keyboard.inline_keyboard for button in row)
    assert chat_state.model == "gemini-previous-user-choice"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_active_profile_menu_uses_provider_default_when_chat_policy_is_absent(monkeypatch, provider):
    from app.handlers import menus
    from app.runtime_settings.live_routes import effective_policy_value

    snapshot = SettingsSnapshot(42, {"live_route:provider": provider})
    monkeypatch.setattr(lifecycle, "current_operation_snapshot", lambda: snapshot)
    model = effective_policy_value("chat", snapshot)["models"][0]
    state = SimpleNamespace(model="gemini-previous-user-choice")
    text, _, keyboard = menus.get_model_menu_content(state, None)
    assert model in text
    assert "администратором" in text
    assert menus.get_admin_selected_chat_model() == model
    assert menus.get_visible_model_list() == [model]
    assert all(not button.callback_data.startswith("model:") for row in keyboard.inline_keyboard for button in row)
    assert state.model == "gemini-previous-user-choice"


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["model_button_callback", "switch_model_callback"])
async def test_stale_user_model_callback_cannot_change_admin_profile(monkeypatch, callback):
    from app.handlers import cb_models, menus

    monkeypatch.setattr(menus, "get_admin_selected_chat_model", lambda: MODEL)
    monkeypatch.setattr(cb_models, "_is_user_busy", lambda user_id: False)
    save = AsyncMock()
    monkeypatch.setattr(cb_models, "update_user_chat", save)
    query = SimpleNamespace(data="model:0:stale", answer=AsyncMock(), from_user=SimpleNamespace(id=42))
    await getattr(cb_models, callback)(SimpleNamespace(callback_query=query), SimpleNamespace())
    save.assert_not_awaited()
    assert "администратором" in query.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("process_id", ["chat", "search", "inline", "document", "vision"])
async def test_request_boundary_replaces_stale_gemini_preference_for_all_live_flows(monkeypatch, process_id):
    from app.providers.request_factory import generation_request_from_history

    snapshot = SettingsSnapshot(
        42,
        {
            "live_route:provider": "openai",
            f"process:{process_id}": {"models": [MODEL], "strategy": "sequential", "inherit_user_model": False},
        },
    )
    token = lifecycle._operation_snapshot.set(snapshot)
    try:
        request = await generation_request_from_history(
            process_id=process_id,
            models=("gemini-previous-user-choice",),
            history=[{"role": "user", "parts": ["hello"]}],
            allow_deferred=False,
        )
    finally:
        lifecycle._operation_snapshot.reset(token)
    assert request.models == (MODEL,)
    assert request.allow_model_fallback is False
    assert request.policy_revision == 42


@pytest.mark.asyncio
async def test_inline_selected_direct_model_disables_old_gemini_thinking_default(monkeypatch, routed):
    from app.handlers import inline
    from app.providers.stream_types import ThinkingLevel

    class Router:
        async def stream(self, request):
            routed.requests.append(request)
            yield TextDelta("fast answer")
            yield StreamCompleted(
                FinishReason(FinishKind.STOP),
                TokenUsage(),
                GroundingReport(),
                RouteUsed(ProviderKind.OPENROUTER, MODEL, MODEL),
            )

    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: Router())
    monkeypatch.setattr(inline, "get_global_setting", AsyncMock(return_value="high"))
    text, _ = await inline._stream_inline_fast(
        "gemini-previous-user-choice", [{"role": "user", "parts": ["hello"]}], None, 42
    )
    assert text == "fast answer"
    assert routed.requests[0].models == (MODEL,)
    assert routed.requests[0].thinking_level is ThinkingLevel.OFF


@pytest.mark.asyncio
@pytest.mark.parametrize("process_id", ["search", "document", "vision"])
async def test_actual_answer_handlers_send_only_profile_model_for_saved_gemini_user(monkeypatch, process_id):
    from app.handlers import ai_document, ai_photo, ai_search

    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    placeholder.reply_text = AsyncMock()
    state = SimpleNamespace(model="gemini-previous-user-choice", thinking_level=None, system_prompt=None)
    delivery = SimpleNamespace(stream=AsyncMock(return_value=None))
    monkeypatch.setattr("app.response_delivery.delivery.get_telegram_response_delivery", lambda: delivery)
    monkeypatch.setattr(ai_search, "_retire_placeholder_notice", AsyncMock())
    monkeypatch.setattr(ai_search, "update_stage", AsyncMock())
    monkeypatch.setattr(ai_document, "update_stage", AsyncMock())
    monkeypatch.setattr("app.search_jina.search_for_grounding", AsyncMock(return_value="Search evidence"))
    monkeypatch.setattr(
        "app.document_processor.get_user_documents", AsyncMock(return_value=[{"id": 17, "filename": "test.txt"}])
    )
    monkeypatch.setattr("app.document_processor.get_document_content", AsyncMock(return_value="Document evidence"))
    snapshot = SettingsSnapshot(
        42,
        {
            "live_route:provider": "openai",
            f"process:{process_id}": {"models": [MODEL], "strategy": "sequential", "inherit_user_model": False},
        },
    )
    token = lifecycle._operation_snapshot.set(snapshot)
    try:
        if process_id == "search":
            await ai_search._handle_qna_search_leased_impl(placeholder, "A question", state, user_id=42)
        elif process_id == "document":
            await ai_document._handle_document_question_leased(placeholder, 42, "A question", state)
        else:
            await ai_photo._process_ai_vision(placeholder, ["A question", b"\x89PNG\r\n\x1a\nimage"], state, 42, 42)
    finally:
        lifecycle._operation_snapshot.reset(token)
    request = delivery.stream.await_args.args[1]
    assert request.models == (MODEL,)
    assert request.allow_model_fallback is False
    assert request.policy_revision == 42
    assert state.model == "gemini-previous-user-choice"
