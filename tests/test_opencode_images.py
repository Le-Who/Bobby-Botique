"""Exercise real image conversion at the OpenCode Anthropic payload boundary."""

import base64
import io

import pytest
from PIL import Image


@pytest.mark.asyncio
@pytest.mark.parametrize("input_type", [bytes, bytearray, Image.Image])
async def test_anthropic_image_payload_encodes_supported_inputs_as_jpeg(monkeypatch, input_type):
    from app.providers.opencode import OpencodeGoProvider
    from app.utils import image_utils

    # Keep the real conversion worker and async handoff, using its supported
    # thread-executor fallback instead of spawning a process in this unit test.
    monkeypatch.setattr(image_utils, "_get_image_process_pool", lambda: None)
    image = Image.new("RGB", (8, 6), "navy")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    part = image if input_type is Image.Image else input_type(encoded.getvalue())
    provider = OpencodeGoProvider("offline-test-key")

    block = await provider._build_anthropic_image_block(part)

    assert block is not None
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    data = base64.b64decode(block["source"]["data"], validate=True)
    with Image.open(io.BytesIO(data)) as result:
        assert result.format == "JPEG"
        assert result.size == (8, 6)
