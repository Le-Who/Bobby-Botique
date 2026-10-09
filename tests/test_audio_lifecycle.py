"""Direct ffmpeg lifecycle tests without an executable, network, or PCM files."""

import asyncio
import struct
from unittest.mock import AsyncMock

import pytest

from app.utils import audio


class ControlledProcess:
    """A live child stays unreaped until the test releases its wait barrier."""

    def __init__(self):
        self.returncode = None
        self.communicating = asyncio.Event()
        self.communication_closed = asyncio.Event()
        self.reaping = asyncio.Event()
        self.allow_reap = asyncio.Event()
        self.kills = 0
        self.reaped = False
        self.wait_calls = 0
        self.wait_cancellations = 0
        self.input = None

    async def communicate(self, *, input):
        self.input = input
        self.communicating.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.communication_closed.set()

    def kill(self):
        self.kills += 1

    async def wait(self):
        self.wait_calls += 1
        self.reaping.set()
        try:
            await self.allow_reap.wait()
        except asyncio.CancelledError:
            self.wait_cancellations += 1
            raise
        self.returncode = -9
        self.reaped = True
        return self.returncode


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_ffmpeg_interruption_kills_live_child_and_awaits_reap(monkeypatch, interruption):
    """Dropping kill or await wait must leave this operation incomplete or fail."""
    child = ControlledProcess()
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    real_wait_for = asyncio.wait_for

    if interruption == "timeout":

        async def short_deadline(awaitable, *, timeout):
            assert timeout == 90.0
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(audio.asyncio, "wait_for", short_deadline)

    operation = asyncio.create_task(audio.pcm_to_ogg_opus(b"synthetic PCM"))
    try:
        await real_wait_for(child.communicating.wait(), timeout=1)
        if interruption == "cancel":
            operation.cancel()
        await real_wait_for(child.communication_closed.wait(), timeout=1)
        await real_wait_for(child.reaping.wait(), timeout=1)

        assert child.kills == 1, "The interrupted live child must be killed"
        assert child.reaping.is_set(), "The child must be awaited, not merely killed"
        assert not operation.done(), "The caller must remain pending until reap completes"
        assert child.input == b"synthetic PCM"
        child.allow_reap.set()
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            assert await operation is None
        assert child.reaped
    finally:
        child.allow_reap.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_ffmpeg_reap_survives_repeated_caller_cancellation(monkeypatch, interruption):
    """A caller must await its owned child even after further cancel requests."""
    child = ControlledProcess()
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    real_wait_for = asyncio.wait_for
    if interruption == "timeout":

        async def short_deadline(awaitable, *, timeout):
            assert timeout == 90.0
            return await real_wait_for(awaitable, timeout=0.01)

        monkeypatch.setattr(audio.asyncio, "wait_for", short_deadline)

    baseline_tasks = asyncio.all_tasks()
    operation = asyncio.create_task(audio.pcm_to_ogg_opus(b"synthetic PCM"))
    try:
        await real_wait_for(child.communicating.wait(), timeout=1)
        if interruption == "cancel":
            operation.cancel("initial cancellation")
        await real_wait_for(child.reaping.wait(), timeout=1)

        for reason in ("cancel during reap", "second cancel during reap"):
            operation.cancel(reason)
            await asyncio.sleep(0)
            assert not operation.done(), "Repeated cancellation must not return before reap completes"
            assert child.wait_calls == 1, "Cleanup must keep ownership of its original reap task"
            assert child.wait_cancellations == 0, "Caller cancellation must not cancel child cleanup"
            assert child.kills == 1
            assert not child.reaped

        child.allow_reap.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await operation
        assert cancelled.value.args == ("initial cancellation" if interruption == "cancel" else "cancel during reap",)
        assert child.reaped
        assert child.wait_calls == 1
        assert child.wait_cancellations == 0
        assert asyncio.all_tasks() <= baseline_tasks
    finally:
        child.allow_reap.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "stdout", "want"),
    [(0, b"OggS" + b"x" * 96, b"OggS" + b"x" * 96), (1, b"partial" * 20, None), (0, b"tiny", None)],
)
async def test_ffmpeg_returns_only_successful_complete_output(monkeypatch, returncode, stdout, want):
    child = AsyncMock()
    child.returncode = returncode
    child.communicate.return_value = (stdout, b"synthetic stderr")
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))

    result = await audio.pcm_to_ogg_opus(b"\x01\x00" * 32)

    assert result == want
    child.kill.assert_not_called()
    child.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_ffmpeg_is_noncritical(monkeypatch):
    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))

    assert await audio.pcm_to_ogg_opus(b"\x00\x00") is None


def test_trim_silence_keeps_loud_prefix_and_requested_tail():
    pcm = struct.pack("<h", 1000) * 24_000 + b"\x00\x00" * 24_000

    assert audio.trim_trailing_silence(pcm, min_tail_ms=150) == pcm[:55_198]


def test_crossfade_known_pcm_vector():
    outgoing = struct.pack("<h", 1000) * 30
    incoming = struct.pack("<h", -1000) * 30

    mixed = audio.crossfade_pcm_chunks([outgoing, incoming], fade_ms=1)

    assert len(mixed) == 72
    assert struct.unpack("<36h", mixed) == (
        *(1000 for _ in range(6)),
        1000,
        916,
        833,
        750,
        666,
        583,
        500,
        416,
        333,
        250,
        166,
        83,
        0,
        -83,
        -166,
        -250,
        -333,
        -416,
        -500,
        -583,
        -666,
        -750,
        -833,
        -916,
        *(-1000 for _ in range(6)),
    )


@pytest.mark.parametrize("chunks", [[], [b""], [b"\x01"], [b"", b"\x01"], [b"\x01", b"\x02"]])
def test_crossfade_empty_or_subsample_chunks_are_preserved(chunks):
    assert audio.crossfade_pcm_chunks(chunks) == b"".join(chunks)
