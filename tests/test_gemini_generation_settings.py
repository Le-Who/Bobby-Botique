"""Offline SDK wire contracts for model-specific Gemini generation settings."""

import json

import httpx
import pytest
from google import genai
from google.genai import types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "sampling", "budget"),
    [
        ("gemini-3.5-flash-lite", False, 128),
        ("gemini-3.8-flash", False, 1024),
        ("models/gemini-3.8-flash", False, 1024),
        ("gemini-3.1-flash-lite", False, 128),
        ("gemini-2.5-flash", True, 128),
    ],
)
async def test_text_generation_omits_unsupported_sampling_but_keeps_schema_and_budget(model, sampling, budget):
    from app.providers.gemini_config import build_generate_content_config

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline-test-key", http_options=types.HttpOptions(httpx_async_client=transport))
        try:
            await client.aio.models.generate_content(
                model=model,
                contents="Classify this supplied text.",
                config=build_generate_content_config(
                    model,
                    temperature=0.1,
                    top_p=0.9,
                    top_k=20,
                    max_output_tokens=128,
                    response_mime_type="application/json",
                ),
            )
        finally:
            await client.aio.aclose()
            client.close()

    config = requests[0]["generationConfig"]
    assert config["maxOutputTokens"] == budget
    assert config["responseMimeType"] == "application/json"
    if "gemini-3.8-" in model:
        thinking = config["thinkingConfig"]
        assert thinking.get("thinkingLevel", thinking.get("thinking_level")).lower() == "low"
    for name, value in (("temperature", 0.1), ("topP", 0.9), ("topK", 20)):
        if sampling:
            assert config[name] == value
        else:
            assert name not in config


@pytest.mark.parametrize(
    ("model", "requested", "expected"),
    [
        ("gemini-3.5-flash-lite", "off", "minimal"),
        ("gemini-3.8-flash", "off", "low"),
        ("models/gemini-3.8-flash", "off", "low"),
        ("gemini-3.8-flash", "medium", "medium"),
        ("gemini-3.1-pro-preview", "off", "low"),
        ("gemini-3-pro-preview", "medium", "high"),
    ],
)
def test_user_thinking_preference_uses_a_level_supported_by_the_selected_model(model, requested, expected):
    from app.providers.base import _build_thinking_config

    config = _build_thinking_config(model, requested)
    assert config is not None
    assert config.thinking_level.value.lower() == expected
    assert config.thinking_budget is None


@pytest.mark.parametrize(("model", "budget"), [("gemini-2.5-pro", 128), ("gemini-2.5-flash", 0)])
def test_off_uses_the_minimum_supported_legacy_budget(model, budget):
    from app.providers.base import _build_thinking_config

    config = _build_thinking_config(model, "off")
    assert config.thinking_budget == budget


def test_small_budget_with_explicit_thinking_policy_is_preserved():
    from app.providers.gemini_config import build_generate_content_config

    config = build_generate_content_config(
        "gemini-3.8-flash", max_output_tokens=512, thinking_config=types.ThinkingConfig(thinking_level="high")
    )
    assert config.max_output_tokens == 512
    assert config.thinking_config.thinking_level is types.ThinkingLevel.HIGH
