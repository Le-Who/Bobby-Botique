import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.voice_engine import VoiceReplyManager


@pytest.fixture(autouse=True)
def _allow_private_data_lease_boundary():
    @asynccontextmanager
    async def allowed(*_args, **_kwargs):
        yield True

    with patch("app.repos.memory_consent.private_data_lease", allowed):
        yield


class FakeBot:
    def __init__(self):
        self._next_message_id = 1000
        self.send_message = AsyncMock(side_effect=self._send_message)
        self.edit_message_text = AsyncMock()
        self.send_chat_action = AsyncMock()
        self.send_voice = AsyncMock()
        self.delete_message = AsyncMock()

    async def _send_message(self, *args, **kwargs):
        self._next_message_id += 1
        return SimpleNamespace(message_id=self._next_message_id)


@pytest.mark.asyncio
async def test_voice_queue_serializes_jobs_per_user():
    """Jobs for the same user are delivered in order even when audio is pre-generated."""
    manager = VoiceReplyManager()
    bot = FakeBot()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    started: list[str] = []

    # We patch _pregenerate_audio so the future blocks until we release it.
    _ = manager._pregenerate_audio

    async def fake_pregenerate(job):
        started.append(job.source_key)
        if job.source_key == "one":
            await first_release.wait()
        else:
            await second_release.wait()
        return b"fake-ogg-data"

    manager._pregenerate_audio = fake_pregenerate  # type: ignore[method-assign]
    # Also patch _send_ogg to no-op (we don't want real Telegram calls).
    manager._send_ogg = AsyncMock()  # type: ignore[method-assign]

    first = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="first",
        source_key="one",
        expected_epoch=1,
    )
    second = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=102,
        response_text="second",
        source_key="two",
        expected_epoch=1,
    )

    await asyncio.sleep(0.05)
    assert first.queue_position == 1
    assert second.queue_position == 2
    # Same-user synthesis remains FIFO under a lease retained through delivery.
    assert started == ["one"]

    first_release.set()
    await asyncio.sleep(0.05)
    assert started == ["one", "two"]

    second_release.set()
    await manager.wait_until_idle(1, timeout=1.5)


@pytest.mark.asyncio
async def test_voice_queue_allows_parallel_jobs_for_different_users():
    """Jobs for different users run their pre-generation concurrently."""
    manager = VoiceReplyManager()
    bot = FakeBot()
    release = asyncio.Event()
    started: list[tuple[int, str]] = []

    async def fake_pregenerate(job):
        started.append((job.user_id, job.source_key))
        await release.wait()
        return b"fake-ogg-data"

    manager._pregenerate_audio = fake_pregenerate  # type: ignore[method-assign]
    manager._send_ogg = AsyncMock()  # type: ignore[method-assign]

    await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="first",
        source_key="u1",
        expected_epoch=1,
    )
    await manager.enqueue(
        bot=bot,
        user_id=2,
        chat_id=20,
        reply_to_message_id=201,
        response_text="second",
        source_key="u2",
        expected_epoch=1,
    )

    await asyncio.sleep(0.05)
    assert {item[0] for item in started} == {1, 2}

    release.set()
    await manager.wait_until_idle(1, timeout=1.5)
    await manager.wait_until_idle(2, timeout=1.5)


@pytest.mark.asyncio
async def test_voice_queue_dedupes_same_source_and_text():
    manager = VoiceReplyManager()
    bot = FakeBot()
    release = asyncio.Event()

    async def fake_pregenerate(job):
        await release.wait()
        return b"fake-ogg-data"

    manager._pregenerate_audio = fake_pregenerate  # type: ignore[method-assign]
    manager._send_ogg = AsyncMock()  # type: ignore[method-assign]

    first = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="same text",
        source_key="same-source",
        expected_epoch=1,
    )
    second = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="same text",
        source_key="same-source",
        expected_epoch=1,
    )

    assert first.queued is True
    assert second.deduped is True
    release.set()
    await manager.wait_until_idle(1, timeout=1.5)


@pytest.mark.asyncio
async def test_ltm_purge_restarts_worker_for_retained_conversation_jobs():
    """Cancelling active LTM TTS must not strand ordinary queued replies."""
    manager = VoiceReplyManager()
    bot = FakeBot()
    ltm_started = asyncio.Event()
    conversation_started = asyncio.Event()

    async def fake_pregenerate(job):
        if job.require_ltm:
            ltm_started.set()
            await asyncio.Event().wait()
        conversation_started.set()
        return b"fake-ogg-data"

    manager._pregenerate_audio = fake_pregenerate  # type: ignore[method-assign]
    manager._send_ogg = AsyncMock()  # type: ignore[method-assign]

    await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="memory-derived reply",
        source_key="ltm",
        expected_epoch=1,
        require_ltm=True,
    )
    await ltm_started.wait()
    await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=102,
        response_text="ordinary reply",
        source_key="conversation",
        expected_epoch=1,
        require_ltm=False,
    )

    removed = await manager.purge_user_jobs(1, ltm_only=True)

    assert removed == 1
    await asyncio.wait_for(conversation_started.wait(), timeout=0.5)
    await manager.wait_until_idle(1, timeout=1.5)
