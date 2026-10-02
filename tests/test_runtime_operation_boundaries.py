"""Runtime policies remain coherent across callbacks and complete daily jobs."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import crocodile_daily, daily_ai, daily_trivia, daily_trivia_authoring
from app.handlers import ai_core, cb_roles
from app.providers.stream_types import (
    FinishReason,
    GroundingReport,
    ProviderKind,
    RouteUsed,
    StreamCompleted,
    TextDelta,
    TokenUsage,
)
from app.runtime_settings import lifecycle, store
from app.runtime_settings.store import SettingsSnapshot
from app.state import get_generated_role, get_user_state


def _policy(*models):
    return {"models": list(models), "strategy": "sequential", "inherit_user_model": False}


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    """No snapshot refresh may load database settings in these offline tests."""
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=SettingsSnapshot(0, {})))
    monkeypatch.setattr("app.state._schedule_persist", lambda state: None)


def _role_update(monkeypatch):
    user_id = 401
    state = get_user_state(user_id)
    state.last_custom_role_prompt = "Create a science teacher"
    progress = SimpleNamespace(edit_text=AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock(return_value=progress)),
    )
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=SimpleNamespace(model="gemini-user-c")))
    return SimpleNamespace(callback_query=query), progress, user_id


async def test_role_retry_uses_saved_chain_without_user_model_or_duplicate_usage(monkeypatch):
    update, progress, user_id = _role_update(monkeypatch)
    snapshot = SettingsSnapshot(8, {"process:roles.generate": _policy("gemini-saved-a", "gemini-saved-b")})
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=snapshot))
    legacy_resolve = AsyncMock(return_value=({"api_key": "test", "key_hash": "hash"}, "gemini-user-c", None))
    legacy_response = AsyncMock(return_value=('{"title":"Legacy","purpose":"Teach","prompt":"Teach science"}', 1))
    increment = AsyncMock()
    monkeypatch.setattr(ai_core, "_resolve_ai_request", legacy_resolve)
    monkeypatch.setattr(ai_core, "_get_ai_response", legacy_response)
    monkeypatch.setattr(ai_core, "_increment_key_usage", increment)
    requests = []

    class Router:
        async def stream(self, request):
            requests.append(request)
            yield TextDelta('{"title":"Configured teacher","purpose":"Teach science","prompt":"Teach","style":[]}')
            yield StreamCompleted(
                finish_reason=FinishReason.from_raw("STOP"),
                usage=TokenUsage(total=9),
                grounding=GroundingReport(),
                route=RouteUsed(
                    provider=ProviderKind.GEMINI,
                    requested_model=request.models[0],
                    actual_model=request.models[0],
                ),
            )

    monkeypatch.setattr("app.providers.get_provider_router", lambda: Router())
    await cb_roles.role_custom_retry_callback(update, SimpleNamespace())

    assert get_generated_role(user_id)["title"] == "Configured teacher"
    assert requests[0].models == ("gemini-saved-a", "gemini-saved-b")
    assert requests[0].allow_model_fallback is False
    assert requests[0].policy_revision == 8
    assert get_user_state(user_id).generating_custom_role is False
    legacy_resolve.assert_not_awaited()
    legacy_response.assert_not_awaited()
    increment.assert_not_awaited()
    assert "Configured teacher" in progress.edit_text.await_args.args[0]


async def test_role_retry_legacy_route_charges_usage_once(monkeypatch):
    update, _, user_id = _role_update(monkeypatch)
    increment = AsyncMock()
    monkeypatch.setattr(
        ai_core,
        "_resolve_ai_request",
        AsyncMock(return_value=({"api_key": "test", "key_hash": "hash"}, "gemini-user-c", None)),
    )
    monkeypatch.setattr(
        ai_core,
        "_get_ai_response",
        AsyncMock(return_value=('{"title":"Legacy","purpose":"Teach","prompt":"Teach science"}', 1)),
    )
    monkeypatch.setattr(ai_core, "_increment_key_usage", increment)

    await cb_roles.role_custom_retry_callback(update, SimpleNamespace())

    assert get_generated_role(user_id)["title"] == "Legacy"
    increment.assert_awaited_once_with("hash", "gemini-user-c")
    assert get_user_state(user_id).generating_custom_role is False


@pytest.mark.parametrize("failure", [RuntimeError("provider failed"), asyncio.CancelledError()])
async def test_role_retry_clears_generating_state_on_provider_failure(monkeypatch, failure):
    update, _, user_id = _role_update(monkeypatch)
    monkeypatch.setattr(
        ai_core,
        "_resolve_ai_request",
        AsyncMock(return_value=({"api_key": "test", "key_hash": "hash"}, "gemini-user-c", None)),
    )
    monkeypatch.setattr(ai_core, "_get_ai_response", AsyncMock(side_effect=failure))
    increment = AsyncMock()
    monkeypatch.setattr(ai_core, "_increment_key_usage", increment)

    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await cb_roles.role_custom_retry_callback(update, SimpleNamespace())
    else:
        await cb_roles.role_custom_retry_callback(update, SimpleNamespace())

    assert get_user_state(user_id).generating_custom_role is False
    increment.assert_not_awaited()


def _game_snapshots():
    def snapshot(revision, label):
        return SettingsSnapshot(
            revision,
            {
                "process:daily.trivia": _policy(f"gemini-{label}-trivia"),
                "prompt:trivia.main": f"{label} main prompt",
                "prompt:trivia.super": f"{label} super prompt",
                "process:crocodile.hints": _policy(f"gemini-{label}-hints"),
                "process:crocodile.image_prompt": _policy(f"gemini-{label}-visual"),
                "prompt:crocodile.hints": f"{label} hints for {{word}} in {{topic}}",
                "prompt:crocodile.image_prompt": f"{label} image for {{word}}",
            },
        )

    return snapshot(10, "old"), snapshot(11, "new")


@pytest.mark.parametrize("nested", [False, True])
async def test_trivia_preparation_freezes_models_and_prompts_then_refreshes_next_job(monkeypatch, nested):
    old, new = _game_snapshots()
    current = [old]
    snapshot_reader = AsyncMock(side_effect=lambda: current[0])
    monkeypatch.setattr(store, "get_snapshot", snapshot_reader)
    monkeypatch.setattr(daily_trivia.repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(daily_trivia.repo, "get_recent_bank_facts", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value="gemini-user-c"))
    calls = []

    class Router:
        async def execute_gemini_model_plan(self, models, history, *, parse_response, system_instruction, **kwargs):
            calls.append((tuple(models), system_instruction))
            lane = "super" if "СУПЕР-вопросов" in history[0]["parts"][0]["text"] else "main"
            current[0] = new
            return parse_response(
                json.dumps(
                    [
                        {
                            "question": f"{lane} question {i}",
                            "options": ["correct", "wrong a", "wrong b", "wrong c"],
                            "correct_index": 0,
                            "explanation": "explanation",
                            "key": {"subject": f"{lane} subject {i}", "relation": "property", "answer": "correct"},
                        }
                        for i in range(5 if lane == "main" else 3)
                    ]
                )
            )

    monkeypatch.setattr(daily_trivia, "get_provider_router", lambda: Router())
    published = []

    async def publish(puzzle_date, **kwargs):
        published.append(await lifecycle.operation_snapshot())
        return SimpleNamespace(puzzle_date=puzzle_date)

    monkeypatch.setattr(daily_trivia_authoring, "publish_authored_day", publish)
    if nested:
        async with lifecycle.runtime_settings_scope(old):
            await daily_trivia.prepare_daily_puzzle(date(2026, 10, 2))
        snapshot_reader.assert_not_awaited()
    else:
        await daily_trivia.prepare_daily_puzzle(date(2026, 10, 2))
    await daily_trivia.prepare_daily_puzzle(date(2026, 10, 3))

    assert calls == [
        (("gemini-old-trivia",), "old main prompt"),
        (("gemini-old-trivia",), "old super prompt"),
        (("gemini-new-trivia",), "new main prompt"),
        (("gemini-new-trivia",), "new super prompt"),
    ]
    assert [snapshot.revision for snapshot in published] == [10, 11]
    assert lifecycle._operation_snapshot.get() is None


@pytest.mark.parametrize("nested", [False, True])
async def test_crocodile_preparation_freezes_models_and_prompts_then_refreshes_next_job(monkeypatch, nested):
    old, new = _game_snapshots()
    current = [old]
    snapshot_reader = AsyncMock(side_effect=lambda: current[0])
    monkeypatch.setattr(store, "get_snapshot", snapshot_reader)
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value=""))
    monkeypatch.setattr(crocodile_daily, "get_daily_image_model", AsyncMock(return_value="qwen-image"))
    monkeypatch.setattr("app.games.word_bank._PROMPT_TRANSLATION_CACHE", {})

    async def puzzle(puzzle_date, *, difficulty):
        return crocodile_daily.repo.DailyPuzzle(puzzle_date, "кот", "животные", "ru", difficulty=difficulty)

    monkeypatch.setattr(crocodile_daily.repo, "create_puzzle_if_missing", puzzle)
    for method in ("set_puzzle_hints", "set_puzzle_image_prompt", "mark_puzzle_prepared"):
        monkeypatch.setattr(crocodile_daily.repo, method, AsyncMock())
    calls = []

    async def generate(prompt, model, timeout=30.0):
        calls.append((model, prompt))
        current[0] = new
        if "hints" in prompt:
            return '{"hints":["Пушистый питомец","Любит дремать","Ловит мышей"]}'
        return '{"is_drawable":true,"visual_description":"a fluffy cat"}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    if nested:
        async with lifecycle.runtime_settings_scope(old):
            first = await crocodile_daily.prepare_daily_puzzle(date(2026, 10, 2), include_image=False)
        snapshot_reader.assert_not_awaited()
    else:
        first = await crocodile_daily.prepare_daily_puzzle(date(2026, 10, 2), include_image=False)
    second = await crocodile_daily.prepare_daily_puzzle(date(2026, 10, 3), include_image=False)

    assert first.hints == second.hints == ["Пушистый питомец", "Любит дремать", "Ловит мышей"]
    assert "a fluffy cat" in first.image_prompt
    assert "a fluffy cat" in second.image_prompt
    assert calls == [
        ("gemini-old-hints", "old hints for кот in животные"),
        ("gemini-old-visual", "old image for кот"),
        ("gemini-new-hints", "new hints for кот in животные"),
        ("gemini-new-visual", "new image for кот"),
    ]
    assert lifecycle._operation_snapshot.get() is None
