from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.config as config
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
