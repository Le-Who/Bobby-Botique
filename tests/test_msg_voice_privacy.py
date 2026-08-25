"""Durable privacy boundaries for voice-message ingress."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_voice_asr_runs_inside_exact_generation_lease():
    active = False

    @asynccontextmanager
    async def tracked_lease(user_id, expected_epoch, *, purpose, require_ltm):
        nonlocal active
        assert user_id == 123
        assert expected_epoch == 52
        assert purpose == "conversation:voice-ingress"
        assert require_ltm is False
        active = True
        try:
            yield True
        finally:
            active = False

    async def transcribe(*_args, **_kwargs):
        assert active
        return None, "conversational", None

    placeholder = MagicMock()
    placeholder.edit_text = AsyncMock()
    update = MagicMock()
    context = MagicMock()
    voice = MagicMock()
    voice.get_file = AsyncMock(return_value=MagicMock())
    chat_state = SimpleNamespace(memory_epoch=52, _has_persisted_chat=True)

    with (
        patch("app.handlers.msg_voice.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
        patch("app.repos.chats.ensure_chat_generation", new_callable=AsyncMock, return_value=52),
        patch("app.repos.memory_consent.private_data_lease", tracked_lease),
        patch("app.handlers.msg_voice.get_file_bytes", new_callable=AsyncMock, return_value=b"ogg"),
        patch("app.utils.multimodal_processor.transcribe_voice", side_effect=transcribe),
    ):
        from app.handlers.msg_voice import _process_voice_pipeline

        await _process_voice_pipeline(
            placeholder,
            update,
            context,
            123,
            voice,
            "ru",
        )

    assert active is False
