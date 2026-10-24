"""Continuous Live turns must not depend on a browser stop-button event."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.web import quart_app
from tests.factories import make_valid_init_data


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("release", ["idle", "waiting", "requires_action", "idle_then_transcript"])
async def test_speech_during_a_reply_waits_for_provider_idle_instead_of_being_discarded(monkeypatch, extended, release):
    from app.web_miniapp import _handle_live_session

    incoming = asyncio.Queue()
    responses = asyncio.Queue()
    second_read = asyncio.Event()
    forwarded = []
    provider_audio = []
    stream_ends = []

    async def read_browser():
        raw = await incoming.get()
        if json.loads(raw).get("data") == "AgA=":
            second_read.set()
        return raw

    socket = SimpleNamespace(send_json=AsyncMock(side_effect=forwarded.append), receive=read_browser)
    session = AsyncMock()
    session.__aenter__.return_value = session

    async def send(**kwargs):
        if "audio" in kwargs:
            provider_audio.append(kwargs["audio"].data)
        if kwargs.get("audio_stream_end"):
            stream_ends.append(True)

    session.send_realtime_input.side_effect = send

    async def receive():
        while True:
            response = await responses.get()
            yield response
            if response.server_content.turn_complete:
                return

    session.receive = receive
    client = MagicMock()
    client.aio.live.connect.return_value = session
    model = "gemini-3.8-live-extended-thinking" if extended else "gemini-3.8-live"
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(GEMINI_API_KEYS=["synthetic"]))

    def response(*, audio=False, complete=False, status=None, waiting=False, transcript=None):
        return SimpleNamespace(
            server_content=SimpleNamespace(
                model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"\0\0"))])
                if audio
                else None,
                input_transcription=None,
                output_transcription=SimpleNamespace(text=transcript) if transcript else None,
                interrupted=False,
                turn_complete=complete,
                waiting_for_input=waiting,
                interaction_status=status,
            )
        )

    with (
        patch("app.web_miniapp._resolve_live_transport", AsyncMock(return_value=(client, model, object(), None, None))),
        patch("app.games.crocodile_flags.is_live_audio_enabled", AsyncMock(return_value=True)),
        patch("app.repos.chats.get_user_chat", AsyncMock(return_value=None)),
        patch("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock()),
    ):
        task = asyncio.create_task(_handle_live_session(socket, 777, {}, "", transport_mode="standard"))
        try:
            await incoming.put(json.dumps({"type": "realtime_input", "data": "AQA="}))
            async with asyncio.timeout(1):
                while not provider_audio:
                    await asyncio.sleep(0.005)
            await responses.put(response(audio=True, status="IN_PROGRESS" if extended else None))
            async with asyncio.timeout(1):
                while not any(m["type"] == "audio" for m in forwarded):
                    await asyncio.sleep(0.005)
            await incoming.put(json.dumps({"type": "realtime_input", "data": "AgA="}))
            await second_read.wait()
            await asyncio.sleep(0.02)
            assert provider_audio == [b"\x01\0"]
            assert stream_ends == [True], "pausing automatic audio must flush the provider's cached VAD input"
            if extended:
                await responses.put(response(complete=True, status="IN_PROGRESS"))
                await asyncio.sleep(0.02)
                assert provider_audio == [b"\x01\0"], "an intermediate utterance is not provider IDLE"
            if release == "waiting":
                await responses.put(response(waiting=True))
            elif release == "requires_action":
                await responses.put(response(complete=True, status="REQUIRES_ACTION"))
            else:
                await responses.put(response(complete=True, status="IDLE" if extended else None))
                if release == "idle_then_transcript":
                    await responses.put(response(transcript="Late final transcript"))
            async with asyncio.timeout(1):
                while len(provider_audio) < 2:
                    await asyncio.sleep(0.005)
            assert provider_audio == [b"\x01\0", b"\x02\0"]
            if release == "idle_then_transcript":
                # A waiter released before a late transcript can send one frame
                # even if the event was wrongly cleared again. The next frame
                # must also pass, preserving the rest of the user's utterance.
                await incoming.put(json.dumps({"type": "realtime_input", "data": "AwA="}))
                async with asyncio.timeout(1):
                    while len(provider_audio) < 3:
                        await asyncio.sleep(0.005)
                assert provider_audio[-1] == b"\x03\0"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_finished", [True, None])
async def test_automatic_audio_receives_two_turns_and_same_event_session_metadata(monkeypatch, first_finished):
    from app.web_miniapp import _handle_live_session

    queue = asyncio.Queue()
    forwarded = []
    socket = SimpleNamespace(send_json=AsyncMock(side_effect=forwarded.append), receive=queue.get)
    provider_inputs = asyncio.Queue()
    session = AsyncMock()
    session.__aenter__.return_value = session

    async def receive_input(**kwargs):
        if "audio" in kwargs:
            provider_inputs.put_nowait(kwargs)

    session.send_realtime_input.side_effect = receive_input
    sequence = 0

    async def receive():
        nonlocal sequence
        await provider_inputs.get()
        sequence += 1
        if sequence == 1:
            yield SimpleNamespace(
                server_content=SimpleNamespace(
                    model_turn=None,
                    input_transcription=SimpleNamespace(text="Первая реплика", finished=first_finished),
                    output_transcription=SimpleNamespace(text="Начало. "),
                    interrupted=False,
                    turn_complete=False,
                    waiting_for_input=False,
                )
            )
        yield SimpleNamespace(
            server_content=SimpleNamespace(
                model_turn=None,
                input_transcription=SimpleNamespace(
                    text="Вторая " if sequence == 1 else "реплика", finished=sequence == 2
                ),
                output_transcription=None,
                interrupted=False,
                turn_complete=False,
                waiting_for_input=False,
            )
        )
        yield SimpleNamespace(
            server_content=SimpleNamespace(
                model_turn=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x01"))]),
                input_transcription=None,
                output_transcription=SimpleNamespace(text=f"Ответ {sequence}"),
                interrupted=False,
                turn_complete=True,
                waiting_for_input=False,
            ),
            session_resumption_update=SimpleNamespace(new_handle=f"handle-{sequence}"),
            go_away=None,
        )

    session.receive = receive
    client = MagicMock()
    client.aio.live.connect.return_value = session
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(GEMINI_API_KEYS=["synthetic"]))
    with (
        patch(
            "app.web_miniapp._resolve_live_transport",
            AsyncMock(return_value=(client, "gemini-3.8-live", object(), None, None)),
        ),
        patch("app.games.crocodile_flags.is_live_audio_enabled", AsyncMock(return_value=True)),
        patch("app.repos.chats.get_user_chat", AsyncMock(return_value=None)),
        patch("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock()),
    ):
        task = asyncio.create_task(_handle_live_session(socket, 777, {}, "", transport_mode="standard"))
        try:
            for i in range(1, 3):
                await queue.put(json.dumps({"type": "realtime_input", "data": base64.b64encode(b"\x00\x01").decode()}))
                async with asyncio.timeout(1):
                    while sum(m["type"] == "turn_complete" for m in forwarded) < i:
                        await asyncio.sleep(0.005)
            assert [m["token"] for m in forwarded if m["type"] == "resumption_token"] == ["handle-1", "handle-2"]
            assert [m["text"] for m in forwarded if m["type"] == "output_transcript"] == [
                "Начало. ",
                "Начало. Ответ 1",
                "Ответ 2",
            ]
            assert [m["text"] for m in forwarded if m["type"] == "input_transcript"] == [
                "Первая реплика",
                "Вторая ",
                "Вторая реплика",
            ]
            assert [m["finished"] for m in forwarded if m["type"] == "input_transcript"] == [True, False, True]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("model,has_thinking", [("gemini-3.8-live", False), ("gemini-3.1-flash-live-preview", True)])
def test_live_config_uses_supported_reasoning_and_tolerates_pauses_without_barge_in(model, has_thinking):
    from app.web_miniapp import _build_live_connect_config

    config = _build_live_connect_config(
        system_instruction="Synthetic",
        resumption_handle=None,
        voice_name="Rasalgethi",
        thinking_level="medium",
        model_name=model,
    )
    assert (config.thinking_config is not None) is has_thinking
    vad = config.realtime_input_config.automatic_activity_detection
    assert vad.disabled is False
    assert vad.silence_duration_ms >= 1200
    assert str(config.realtime_input_config.activity_handling).endswith("NO_INTERRUPTION")
    assert config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Rasalgethi"


@pytest.mark.asyncio
async def test_rasalgethi_is_first_and_can_be_saved_without_changing_tts(monkeypatch):
    from app.database import ChatState

    monkeypatch.setattr(
        "app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="synthetic-token", VERTEX_LIVE_ENABLED=False)
    )
    headers = {"Authorization": "tma " + make_valid_init_data("synthetic-token", user_id=777)}
    state = ChatState(history=[], model="synthetic", token_count=0, search_enabled=False, system_prompt=None)
    with (
        patch("app.repos.users.is_authorized", AsyncMock(return_value=True)),
        patch("app.repos.chats.get_user_chat", AsyncMock(return_value=state)),
        patch("app.repos.chats.update_user_chat", AsyncMock()),
    ):
        client = quart_app.test_client()
        result = await (await client.get("/webapp/api/live-settings", headers=headers)).get_json()
        assert result["voices"][0]["id"] == "Rasalgethi"
        response = await client.patch(
            "/webapp/api/live-settings", headers=headers, json={"live_voice_name": "Rasalgethi"}
        )
        assert response.status_code == 200
        assert state.live_voice_name == "Rasalgethi"
        assert state.voice_id is None
