"""Typed request-to-native provider payload contract tests."""

import pytest

from app.providers.stream_types import (
    GenerationRequest,
    ImagePart,
    PromptRole,
    PromptTurn,
    TextPart,
)


def _request() -> GenerationRequest:
    return GenerationRequest(
        models=("model",),
        turns=(
            PromptTurn(PromptRole.USER, (TextPart("look"), ImagePart(b"png", "image/png"))),
            PromptTurn(PromptRole.MODEL, (TextPart("seen"),)),
        ),
        system_instruction="system",
    )


@pytest.mark.asyncio
async def test_openai_payload_uses_typed_roles_and_exact_image_mime():
    from app.providers.typed_payloads import openai_messages

    messages = await openai_messages(_request())

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0] == {"type": "text", "text": "look"}
    assert messages[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert messages[2] == {"role": "assistant", "content": "seen"}


@pytest.mark.asyncio
async def test_anthropic_payload_uses_typed_parts_without_legacy_history_objects():
    from app.providers.typed_payloads import anthropic_messages_payload

    payload = await anthropic_messages_payload(
        _request(),
        api_model="minimax-m2.7",
        max_tokens=8192,
    )

    assert payload["system"] == "system"
    assert payload["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
    assert payload["messages"][1] == {"role": "assistant", "content": "seen"}


@pytest.mark.asyncio
async def test_gemini_payload_is_built_directly_from_typed_turns():
    from app.providers.typed_payloads import gemini_contents

    contents = await gemini_contents(_request())

    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "look"
    assert contents[0].parts[1].inline_data.mime_type == "image/png"
    assert contents[1].role == "model"
