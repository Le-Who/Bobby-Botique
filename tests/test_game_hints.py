from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.config as config
from app.games.ai_budget import reset_budget_state_for_tests
from app.games.hinting import _prewarm_topic_hints, get_or_generate_cached_hints, reset_hint_runtime_state_for_tests
from app.games.judge import generate_hints


class _RouterStub:
    def __init__(self, responses: dict[str, tuple[float, str | Exception]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get_response(self, preferred_model: str, **_: object) -> tuple[str, int | None]:
        self.calls.append(preferred_model)
        delay, payload = self._responses.get(preferred_model, (0.0, ""))
        if delay:
            await asyncio.sleep(delay)
        if isinstance(payload, Exception):
            raise payload
        return payload, None


@pytest.fixture
def hint_settings(monkeypatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        AVAILABLE_MODELS=[],
        DEFAULT_MODEL=None,
        OPENCODE_AVAILABLE_MODELS=[],
        OPENCODE_DEFAULT_MODEL=None,
        OPENCODE_QNA_MODEL=None,
        QNA_MODEL=None,
        VERTEX_AI_KEY=None,
        VERTEX_AI_PROJECT=None,
    )
    monkeypatch.setattr(config, "settings", settings)
    reset_budget_state_for_tests()
    reset_hint_runtime_state_for_tests()
    return settings


@pytest.mark.asyncio
async def test_generate_hints_ignores_fast_lane_exception_and_uses_slower_valid_lane(hint_settings):
    router = _RouterStub(
        {
            "gemini-3-flash-preview": (0.0, RuntimeError("boom")),
            "opencode-go/glm-5.1": (
                0.05,
                '```json\n{"hints":["электроархонт из известной игры","связана с Инадзумой и молнией","имя начинается на Р и это не Розария"]}\n```',
            ),
        }
    )
    hint_settings.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5.1"]
    hint_settings.OPENCODE_QNA_MODEL = "opencode-go/glm-5.1"
    hint_settings.OPENCODE_DEFAULT_MODEL = "opencode-go/glm-5.1"

    with (
        patch("app.providers.get_provider_router", return_value=router),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert hints == [
        "электроархонт из известной игры",
        "связана с Инадзумой и молнией",
        "имя начинается на Р и это не Розария",
    ]
    assert "gemini-3-flash-preview" in router.calls
    assert "opencode-go/glm-5.1" in router.calls


@pytest.mark.asyncio
async def test_generate_hints_accepts_numbered_list_when_json_missing(hint_settings):
    router = _RouterStub(
        {
            "opencode-go/glm-5.1": (
                0.0,
                "1. Живёт в мире Тейвата\n2. Связана с электричеством и властью\n3. Имя начинается на Р",
            )
        }
    )
    hint_settings.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5.1"]
    hint_settings.OPENCODE_QNA_MODEL = "opencode-go/glm-5.1"
    hint_settings.OPENCODE_DEFAULT_MODEL = "opencode-go/glm-5.1"

    with (
        patch("app.providers.get_provider_router", return_value=router),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert hints == [
        "Живёт в мире Тейвата",
        "Связана с электричеством и властью",
        "Имя начинается на Р",
    ]


@pytest.mark.asyncio
async def test_generate_hints_returns_deterministic_fallback_when_all_models_fail(hint_settings):
    router = _RouterStub(
        {
            "gemini-3-flash-preview": (0.0, ""),
            "opencode-go/glm-5.1": (0.0, "[GENERIC]❌ Ошибка API: OpenRouter API error: RuntimeError('boom')"),
        }
    )
    hint_settings.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5.1"]
    hint_settings.OPENCODE_QNA_MODEL = "opencode-go/glm-5.1"
    hint_settings.OPENCODE_DEFAULT_MODEL = "opencode-go/glm-5.1"

    with (
        patch("app.providers.get_provider_router", return_value=router),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert len(hints) == 3
    assert hints[0] == "Это из категории «персонаж genshin impact»."
    assert hints[1] == "Одно слово, 6 букв."
    assert hints[2] == "Первая буква «Р», последняя — «Н»."


@pytest.mark.asyncio
async def test_generate_hints_background_mode_skips_ai_studio_lane(hint_settings):
    router = _RouterStub(
        {
            "opencode-go/glm-5.1": (
                0.0,
                '{"hints":["широкий намек","средний намек","почти прямой намек"]}',
            )
        }
    )
    hint_settings.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5.1"]
    hint_settings.OPENCODE_QNA_MODEL = "opencode-go/glm-5.1"
    hint_settings.OPENCODE_DEFAULT_MODEL = "opencode-go/glm-5.1"

    with patch("app.providers.get_provider_router", return_value=router):
        hints = await generate_hints("райден", "персонаж genshin impact", mode="background")

    assert hints == ["широкий намек", "средний намек", "почти прямой намек"]
    assert "gemini-3-flash-preview" not in router.calls
    assert router.calls == ["opencode-go/glm-5.1"]


@pytest.mark.asyncio
async def test_get_or_generate_cached_hints_uses_singleflight(hint_settings):
    async def _slow_generate(*args, **kwargs):
        await asyncio.sleep(0.01)
        return ["hint 1", "hint 2", "hint 3"]

    with (
        patch("app.games.judgement_cache.get_cached_hints", new_callable=AsyncMock, return_value=None),
        patch("app.games.judgement_cache.cache_hints", new_callable=AsyncMock),
        patch("app.games.judge.generate_hints", new_callable=AsyncMock) as generate_mock,
    ):
        generate_mock.side_effect = _slow_generate
        first, second = await asyncio.gather(
            get_or_generate_cached_hints("райден", "персонаж genshin impact", topic_id="custom:1"),
            get_or_generate_cached_hints("райден", "персонаж genshin impact", topic_id="custom:1"),
        )

    assert first == ["hint 1", "hint 2", "hint 3"]
    assert second == first
    assert generate_mock.await_count == 1


@pytest.mark.asyncio
async def test_batch_prewarm_rejects_invalid_entries_and_falls_back_per_word(hint_settings):
    router = _RouterStub(
        {
            "opencode-go/glm-5.1": (
                0.0,
                '{"items":['
                '{"word":"райден","hints":["электроархонт","связана с Инадзумой","имя начинается на Р"]},'
                '{"word":"лишнее слово","hints":["чужая 1","чужая 2","чужая 3"]},'
                '{"word":"венти","hints":["дубликат","дубликат","дубликат"]}'
                "]}",
            )
        }
    )
    hint_settings.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5.1"]
    hint_settings.OPENCODE_QNA_MODEL = "opencode-go/glm-5.1"
    hint_settings.OPENCODE_DEFAULT_MODEL = "opencode-go/glm-5.1"

    fallback_hints = ["анемо герой", "любит свободу", "имя начинается на В"]

    with (
        patch("app.providers.get_provider_router", return_value=router),
        patch("app.games.judgement_cache.get_cached_hints", new_callable=AsyncMock, return_value=None),
        patch("app.games.judgement_cache.cache_hints", new_callable=AsyncMock) as cache_mock,
        patch("app.games.judge.generate_hints", new_callable=AsyncMock, return_value=fallback_hints) as generate_mock,
    ):
        await _prewarm_topic_hints(("райден", "венти"), "персонаж genshin impact", topic_id="custom:1")

    assert cache_mock.await_count == 2
    first_call = cache_mock.await_args_list[0]
    second_call = cache_mock.await_args_list[1]
    assert first_call.args == (
        "райден",
        "персонаж genshin impact",
        ["электроархонт", "связана с Инадзумой", "имя начинается на Р"],
    )
    assert first_call.kwargs == {"topic_id": "custom:1"}
    assert second_call.args == ("венти", "персонаж genshin impact", fallback_hints)
    assert second_call.kwargs == {"topic_id": "custom:1"}
    generate_mock.assert_awaited_once_with("венти", "персонаж genshin impact", mode="background")
