from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.config as config
from app.games.judge import generate_hints


class _RouterStub:
    def __init__(self, responses: dict[str, tuple[float, str]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get_response(self, preferred_model: str, **_: object) -> tuple[str, int | None]:
        self.calls.append(preferred_model)
        delay, text = self._responses.get(preferred_model, (0.0, ""))
        if delay:
            await asyncio.sleep(delay)
        return text, None


@pytest.mark.asyncio
async def test_generate_hints_uses_first_valid_model_from_race():
    router = _RouterStub(
        {
            "opencode-go/qwen3.6-plus": (0.03, "не JSON"),
            "opencode-go/qwen3.5-plus": (
                0.01,
                '```json\n{"hints":["электроархонт из известной игры","связана с Инадзумой и молнией","имя начинается на Р и это не Розария"]}\n```',
            ),
            "opencode-go/kimi-k2.5": (
                0.05,
                '{"hints":["запасной вариант один","запасной вариант два","запасной вариант три"]}',
            ),
        }
    )

    with (
        patch("app.config.get_primary_provider_async", new=AsyncMock(return_value="opencode")),
        patch("app.providers.get_provider_router", return_value=router),
        patch.object(
            config.settings,
            "OPENCODE_AVAILABLE_MODELS",
            ["opencode-go/qwen3.6-plus", "opencode-go/qwen3.5-plus", "opencode-go/kimi-k2.5"],
        ),
        patch.object(config.settings, "OPENCODE_QNA_MODEL", "opencode-go/qwen3.5-plus"),
        patch.object(config.settings, "OPENCODE_DEFAULT_MODEL", "opencode-go/qwen3.5-plus"),
        patch.object(config.settings, "AVAILABLE_MODELS", []),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert hints == [
        "электроархонт из известной игры",
        "связана с Инадзумой и молнией",
        "имя начинается на Р и это не Розария",
    ]
    assert "opencode-go/qwen3.5-plus" in router.calls


@pytest.mark.asyncio
async def test_generate_hints_accepts_numbered_list_when_json_missing():
    router = _RouterStub(
        {
            "opencode-go/qwen3.6-plus": (
                0.0,
                "1. Живёт в мире Тейвата\n2. Связана с электричеством и властью\n3. Имя начинается на Р",
            )
        }
    )

    with (
        patch("app.config.get_primary_provider_async", new=AsyncMock(return_value="opencode")),
        patch("app.providers.get_provider_router", return_value=router),
        patch.object(config.settings, "OPENCODE_AVAILABLE_MODELS", ["opencode-go/qwen3.6-plus"]),
        patch.object(config.settings, "OPENCODE_QNA_MODEL", "opencode-go/qwen3.6-plus"),
        patch.object(config.settings, "OPENCODE_DEFAULT_MODEL", "opencode-go/qwen3.6-plus"),
        patch.object(config.settings, "AVAILABLE_MODELS", []),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert hints == [
        "Живёт в мире Тейвата",
        "Связана с электричеством и властью",
        "Имя начинается на Р",
    ]


@pytest.mark.asyncio
async def test_generate_hints_returns_deterministic_fallback_when_all_models_fail():
    router = _RouterStub(
        {
            "opencode-go/qwen3.5-plus": (0.0, "[GENERIC]❌ Ошибка API: OpenRouter API error: RuntimeError('boom')"),
            "gemini-3.1-flash-lite-preview": (0.0, ""),
        }
    )

    with (
        patch("app.config.get_primary_provider_async", new=AsyncMock(return_value="opencode")),
        patch("app.providers.get_provider_router", return_value=router),
        patch.object(config.settings, "OPENCODE_AVAILABLE_MODELS", ["opencode-go/qwen3.5-plus"]),
        patch.object(config.settings, "OPENCODE_QNA_MODEL", "opencode-go/qwen3.5-plus"),
        patch.object(config.settings, "OPENCODE_DEFAULT_MODEL", "opencode-go/qwen3.5-plus"),
        patch.object(config.settings, "AVAILABLE_MODELS", ["gemini-3.1-flash-lite-preview"]),
        patch.object(config.settings, "QNA_MODEL", "gemini-3.1-flash-lite-preview"),
        patch.object(config.settings, "DEFAULT_MODEL", "gemini-3.1-flash-lite-preview"),
    ):
        hints = await generate_hints("райден", "персонаж genshin impact")

    assert len(hints) == 3
    assert hints[0] == "Это из категории «персонаж genshin impact»."
    assert hints[1] == "Одно слово, 6 букв."
    assert hints[2] == "Первая буква «Р», последняя — «Н»."
