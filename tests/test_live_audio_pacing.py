"""Queued microphone audio must catch up without flooding native Live VAD."""

import pytest


@pytest.fixture
def audio_clock(monkeypatch):
    from app import live_audio_pacing

    now = [1000.0]
    waits = []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    monkeypatch.setattr(live_audio_pacing, "monotonic", lambda: now[0])
    monkeypatch.setattr(live_audio_pacing, "sleep", sleep)
    return live_audio_pacing.LiveAudioPacer(), now, waits


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [16000, 24000, 48000])
async def test_backlogged_pcm_is_sent_at_no_more_than_twice_capture_speed(audio_clock, rate):
    pacer, now, waits = audio_clock
    sent = []
    for _ in range(3):
        await pacer.pace(int(rate * 2 * 0.1), sample_rate=rate)
        sent.append(now[0])
    assert sent == pytest.approx([1000, 1000.05, 1000.1])
    assert waits == pytest.approx([0.05, 0.05])


@pytest.mark.asyncio
async def test_normal_realtime_audio_does_not_gain_an_extra_delay(audio_clock):
    pacer, now, waits = audio_clock
    for _ in range(3):
        await pacer.pace(3200)
        now[0] += 0.1
    assert waits == []


@pytest.mark.asyncio
async def test_a_model_reply_does_not_create_a_stale_pacing_deadline(audio_clock):
    pacer, now, waits = audio_clock
    await pacer.pace(3200)
    now[0] += 10
    await pacer.pace(3200)
    assert waits == []
    await pacer.pace(3200)
    assert now[0] == pytest.approx(1010.05)


@pytest.mark.asyncio
async def test_a_reply_starting_during_pacing_cannot_release_two_packets_together(audio_clock):
    pacer, now, waits = audio_clock

    async def reply_finishes():
        now[0] += 10

    await pacer.pace(3200, wait_ready=reply_finishes)
    assert now[0] == 1010
    await pacer.pace(3200)
    assert now[0] == pytest.approx(1010.05)
    assert waits == pytest.approx([0.05])
