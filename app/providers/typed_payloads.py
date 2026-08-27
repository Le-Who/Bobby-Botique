"""Provider-native payloads built directly from the typed generation boundary."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from google.genai import types

from app.providers.stream_types import (
    GenerationRequest,
    ImagePart,
    PromptRole,
    TextPart,
)
from app.utils.image_utils import save_image_as_bytes


async def _materialize_image(part: ImagePart) -> tuple[bytes, str] | None:
    if not part.needs_compression:
        return part.data, part.mime_type
    data = await save_image_as_bytes(
        part.data,
        cache_key=part.cache_key,
        task_type=part.task_type or "default",
    )
    return (data, "image/jpeg") if data else None


async def gemini_contents(request: GenerationRequest) -> list[types.Content]:
    contents: list[types.Content] = []
    for turn in request.turns:
        image_parts = [part for part in turn.parts if isinstance(part, ImagePart)]
        materialized = await asyncio.gather(*(_materialize_image(part) for part in image_parts))
        image_index = 0
        native_parts: list[types.Part] = []
        for part in turn.parts:
            if isinstance(part, TextPart):
                native_parts.append(types.Part.from_text(text=part.text))
                continue
            image = materialized[image_index]
            image_index += 1
            if image is not None:
                data, mime_type = image
                native_parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=data)))
        if native_parts:
            contents.append(types.Content(role=turn.role.value, parts=native_parts))
    return contents


async def _base64_image(part: ImagePart) -> tuple[str, str] | None:
    image = await _materialize_image(part)
    if image is None:
        return None
    data, mime_type = image
    encoded = await asyncio.to_thread(base64.b64encode, data)
    return encoded.decode("ascii"), mime_type


async def openai_messages(request: GenerationRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system_instruction:
        messages.append({"role": "system", "content": request.system_instruction})

    for turn in request.turns:
        image_parts = [part for part in turn.parts if isinstance(part, ImagePart)]
        images = await asyncio.gather(*(_base64_image(part) for part in image_parts))
        image_index = 0
        content: list[dict[str, Any]] = []
        for part in turn.parts:
            if isinstance(part, TextPart):
                content.append({"type": "text", "text": part.text})
                continue
            image = images[image_index]
            image_index += 1
            if image is not None:
                encoded, mime_type = image
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                    }
                )
        if not content:
            continue
        role = "assistant" if turn.role is PromptRole.MODEL else "user"
        native_content: str | list[dict[str, Any]] = content
        if len(content) == 1 and content[0]["type"] == "text":
            native_content = content[0]["text"]
        messages.append({"role": role, "content": native_content})
    return messages


async def anthropic_messages_payload(
    request: GenerationRequest,
    *,
    api_model: str,
    max_tokens: int,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for turn in request.turns:
        image_parts = [part for part in turn.parts if isinstance(part, ImagePart)]
        images = await asyncio.gather(*(_base64_image(part) for part in image_parts))
        image_index = 0
        blocks: list[dict[str, Any]] = []
        for part in turn.parts:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
                continue
            image = images[image_index]
            image_index += 1
            if image is not None:
                encoded, mime_type = image
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": encoded,
                        },
                    }
                )
        if not blocks:
            continue
        content: str | list[dict[str, Any]] = blocks
        if all(block["type"] == "text" for block in blocks):
            content = "\n".join(block["text"] for block in blocks)
        role = "assistant" if turn.role is PromptRole.MODEL else "user"
        messages.append({"role": role, "content": content})

    payload: dict[str, Any] = {
        "model": api_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if request.system_instruction:
        payload["system"] = request.system_instruction
    return payload


__all__ = [
    "anthropic_messages_payload",
    "gemini_contents",
    "openai_messages",
]
