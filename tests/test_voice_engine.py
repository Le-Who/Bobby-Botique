import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.voice_engine import VoiceReplyManager


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
    manager = VoiceReplyManager()
    bot = FakeBot()
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    started: list[str] = []

    async def fake_generate(job):
        started.append(job.source_key)
        if job.source_key == "one":
            await first_release.wait()
        else:
            await second_release.wait()

    manager._generate_and_send_voice = fake_generate  # type: ignore[method-assign]

    first = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="first",
        source_key="one",
    )
    second = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=102,
        response_text="second",
        source_key="two",
    )

    await asyncio.sleep(0.05)
    assert first.queue_position == 1
    assert second.queue_position == 2
    assert started == ["one"]

    first_release.set()
    await asyncio.sleep(0.05)
    assert started == ["one", "two"]

    second_release.set()
    await manager.wait_until_idle(1, timeout=1.5)


@pytest.mark.asyncio
async def test_voice_queue_allows_parallel_jobs_for_different_users():
    manager = VoiceReplyManager()
    bot = FakeBot()
    release = asyncio.Event()
    started: list[tuple[int, str]] = []

    async def fake_generate(job):
        started.append((job.user_id, job.source_key))
        await release.wait()

    manager._generate_and_send_voice = fake_generate  # type: ignore[method-assign]

    await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="first",
        source_key="u1",
    )
    await manager.enqueue(
        bot=bot,
        user_id=2,
        chat_id=20,
        reply_to_message_id=201,
        response_text="second",
        source_key="u2",
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

    async def fake_generate(job):
        await release.wait()

    manager._generate_and_send_voice = fake_generate  # type: ignore[method-assign]

    first = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="same text",
        source_key="same-source",
    )
    second = await manager.enqueue(
        bot=bot,
        user_id=1,
        chat_id=10,
        reply_to_message_id=101,
        response_text="same text",
        source_key="same-source",
    )

    assert first.queued is True
    assert second.deduped is True
    release.set()
    await manager.wait_until_idle(1, timeout=1.5)
