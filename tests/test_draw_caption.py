from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import MessageEntity
from telegram.constants import MessageLimit

from app.handlers import cmd_image


def entity_text(caption, entity):
    encoded = caption.encode("utf-16-le")
    return encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")


@pytest.mark.asyncio
@pytest.mark.parametrize("translated", [False, True])
async def test_generated_photo_keeps_first_delivery_with_oversized_caption(monkeypatch, translated):
    punctuation = "🖼️ [literal](url) _stars* \\ `code`"
    original = "Оригинал " + punctuation + " 🧪" + "🧪" * 800
    provider_prompt = "Translated " + punctuation + " 🚀" + "🚀" * 800 if translated else original
    label = "Long _configured [model]* " + "🧪" * 800
    provider = AsyncMock(return_value=SimpleNamespace(success=True, images=[b"synthetic-image"], warning=None))
    placeholder = SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock())

    async def send_photo(*, photo, caption, caption_entities, parse_mode, **_kwargs):
        assert photo == b"synthetic-image"
        assert parse_mode is None
        assert len(caption) == MessageLimit.CAPTION_LENGTH
        utf16_units = len(caption.encode("utf-16-le")) // 2
        assert utf16_units > MessageLimit.CAPTION_LENGTH
        visible = [entity_text(caption, entity) for entity in caption_entities]
        assert visible[0].startswith(provider_prompt[: len(punctuation) + 11])
        assert punctuation in visible[0] and visible[0].endswith("…")
        assert visible[1].startswith("Long _configured [model]*") and visible[1].endswith(" · 1:1")
        assert [entity.type for entity in caption_entities] == [MessageEntity.BOLD, MessageEntity.ITALIC] + (
            [MessageEntity.ITALIC] if translated else []
        )
        if translated:
            assert visible[2].startswith("🌐 Переведено: Оригинал " + punctuation)
        assert caption_entities[-1].offset + caption_entities[-1].length == utf16_units
        return SimpleNamespace(message_id=42)

    photo = AsyncMock(side_effect=send_photo)
    message = SimpleNamespace(
        chat=None, get_bot=object, reply_text=AsyncMock(return_value=placeholder), reply_photo=photo
    )
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=7))
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(cmd_image, "_needs_translation", lambda *_args: translated)
    monkeypatch.setattr(cmd_image, "_translate_to_english", AsyncMock(return_value=provider_prompt))
    monkeypatch.setattr(cmd_image, "_send_typing_heartbeat", AsyncMock())
    monkeypatch.setattr(cmd_image, "_model_label", lambda _model: label)
    monkeypatch.setattr(cmd_image, "get_pollinations_provider", lambda: SimpleNamespace(generate=provider))

    assert await cmd_image._run_generation(update, context, original, "zimage", "1:1")
    photo.assert_awaited_once()
    placeholder.delete.assert_awaited_once()
    placeholder.edit_text.assert_not_awaited()
    assert provider.await_args.kwargs["prompt"] == provider_prompt
    assert context.user_data["draw_state"]["last_photo_msg"] == 42
