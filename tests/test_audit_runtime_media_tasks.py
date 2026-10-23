"""Request-local image/music progress tasks release their work before returning."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from app.handlers import ai_chat, cmd_image


async def test_failed_draw_placeholder_cancels_and_joins_prompt_translation(monkeypatch):
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    translations = []

    async def translate(*_args, **_kwargs):
        translations.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def reply_text(*_args, **_kwargs):
        await entered.wait()
        raise TelegramError("synthetic placeholder failure")

    monkeypatch.setattr(cmd_image, "_needs_translation", lambda *_args: True)
    monkeypatch.setattr(cmd_image, "_translate_to_english", translate)
    message = SimpleNamespace(chat_id=7, chat=SimpleNamespace(id=7), get_bot=lambda: object(), reply_text=reply_text)
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=7))
    try:
        with pytest.raises(TelegramError, match="synthetic placeholder failure"):
            await cmd_image._run_generation(update, SimpleNamespace(user_data={}), "a tree", "flux", "1:1")
        assert cleaned.is_set()
        assert all(task.done() for task in translations)
    finally:
        for task in translations:
            task.cancel()
        await asyncio.gather(*translations, return_exceptions=True)


async def test_cancelled_draw_heartbeat_does_not_leave_stop_waiter_running():
    waiting = asyncio.Event()
    waiters = []

    class StopEvent(asyncio.Event):
        async def wait(self):
            waiters.append(asyncio.current_task())
            waiting.set()
            return await super().wait()

    stop = StopEvent()
    heartbeat = asyncio.create_task(
        cmd_image._send_typing_heartbeat(7, SimpleNamespace(send_chat_action=AsyncMock()), stop)
    )
    try:
        await waiting.wait()
        heartbeat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat
        assert all(task.done() for task in waiters)
    finally:
        stop.set()
        await asyncio.gather(heartbeat, *waiters, return_exceptions=True)


async def test_cancelled_image_generation_joins_heartbeat_cleanup(monkeypatch):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    heartbeat_tasks = []

    async def heartbeat(*_args):
        heartbeat_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cleaned.set()

    async def generate(**_kwargs):
        await started.wait()
        await asyncio.Event().wait()

    monkeypatch.setattr(cmd_image, "_send_typing_heartbeat", heartbeat)
    monkeypatch.setattr(cmd_image, "get_pollinations_provider", lambda: SimpleNamespace(generate=generate))
    message = SimpleNamespace(
        chat_id=7, chat=SimpleNamespace(id=7), get_bot=lambda: object(), reply_text=AsyncMock(return_value=object())
    )
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=7))
    owner = asyncio.create_task(
        cmd_image._run_generation(update, SimpleNamespace(user_data={}), "a tree", "zimage", "1:1")
    )
    try:
        await started.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert cleaned.is_set()
        assert all(task.done() for task in heartbeat_tasks)
    finally:
        owner.cancel()
        await asyncio.gather(owner, *heartbeat_tasks, return_exceptions=True)


async def test_cancelled_music_generation_joins_typing_transport_cleanup(monkeypatch):
    started = asyncio.Event()
    cleaned = asyncio.Event()
    heartbeat_tasks = []

    async def send_chat_action(**_kwargs):
        heartbeat_tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cleaned.set()

    async def generate(**_kwargs):
        await started.wait()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.providers.freetheai_audio.get_lyria_provider", lambda: SimpleNamespace(generate=generate))
    placeholder = SimpleNamespace(
        chat_id=7, edit_text=AsyncMock(), get_bot=lambda: SimpleNamespace(send_chat_action=send_chat_action)
    )
    owner = asyncio.create_task(
        ai_chat._handle_lyria_audio(placeholder, 7, "a melody", "or/google/lyria-3-clip-preview")
    )
    try:
        await started.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert cleaned.is_set()
        assert all(task.done() for task in heartbeat_tasks)
    finally:
        owner.cancel()
        await asyncio.gather(owner, *heartbeat_tasks, return_exceptions=True)


async def test_music_caption_escapes_prompt_and_provider_text_before_formatting(monkeypatch):
    from app.providers.freetheai_audio import AudioGenResult

    captured = []

    async def reply_audio(**kwargs):
        captured.append(kwargs)

    generate = AsyncMock(
        return_value=AudioGenResult(success=True, audio_bytes=b"synthetic-audio", text_content="Lyrics * [verse]_")
    )
    monkeypatch.setattr("app.providers.freetheai_audio.get_lyria_provider", lambda: SimpleNamespace(generate=generate))
    monkeypatch.setattr(ai_chat, "_retire_placeholder_notice", AsyncMock())
    placeholder = SimpleNamespace(
        chat_id=7,
        edit_text=AsyncMock(),
        get_bot=lambda: SimpleNamespace(send_chat_action=AsyncMock()),
        reply_audio=reply_audio,
        delete=AsyncMock(),
    )

    await ai_chat._handle_lyria_audio(placeholder, 7, "A *[song]_", "or/google/lyria-3-clip-preview")

    assert captured[0]["parse_mode"] == "Markdown"
    assert captured[0]["caption"] == "🎵 *A \\*\\[song]\\_*\n_🎶 Lyria Clip_\n\nLyrics \\* \\[verse]\\_"
