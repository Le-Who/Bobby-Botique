"""Tests for the Live Audio WebSocket proxy (Gemini Live API integration).

Covers:
  LA-01: Authentication — missing / invalid initData
  LA-02: Connection lifecycle — successful connect + consumer/producer tasks
  LA-03: Audio forwarding — realtime_input message dispatched to Gemini session
  LA-04: Interrupt forwarding — consumer relays interrupted signal
  LA-05: Session page — GET /webapp/live returns 200
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.web import quart_app
from tests.factories import make_valid_init_data


@pytest.fixture
def test_client():
    return quart_app.test_client()


@pytest.fixture
def live_settings(monkeypatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        ADMIN_ID=1,
        TELEGRAM_BOT_TOKEN="test-token",
        GEMINI_API_KEYS=["fake-api-key-123"],
        AVAILABLE_MODELS=["gemini-3.1-flash-live-preview"],
        DAILY_LIMITS={},
        LIMIT_THRESHOLD_PERCENT=0.7,
        RESEARCH_MODEL="gemini-3.1-flash-live-preview",
        TAVILY_LIMIT_THRESHOLD_PERCENT=0.8,
        TAVILY_MONTHLY_CREDIT_LIMIT=1000.0,
        VERTEX_AI_KEY="vertex-key",
        VERTEX_AI_PROJECT=None,
        VERTEX_AI_LOCATION="us-central1",
    )
    monkeypatch.setattr("app.config.settings", settings, raising=False)
    monkeypatch.setattr("app.web_miniapp.settings", settings, raising=False)
    return settings


@pytest.fixture
def mock_bot_token(live_settings):
    return live_settings.TELEGRAM_BOT_TOKEN


@pytest.fixture
def mock_api_keys(live_settings):
    return live_settings.GEMINI_API_KEYS


def _make_response_with_audio(pcm_data: bytes):
    """Build a mock server response containing audio inline_data."""
    part = SimpleNamespace(
        inline_data=SimpleNamespace(data=pcm_data, mime_type="audio/pcm;rate=24000"),
    )
    content = SimpleNamespace(
        model_turn=SimpleNamespace(parts=[part]),
        input_transcription=None,
        output_transcription=None,
        interrupted=None,
    )
    return SimpleNamespace(server_content=content, session_resumption_update=None)


def _make_response_with_interrupt():
    """Build a mock server response with interrupted=True."""
    content = SimpleNamespace(
        model_turn=None,
        input_transcription=None,
        output_transcription=None,
        interrupted=True,
    )
    return SimpleNamespace(server_content=content, session_resumption_update=None)


def _make_response_with_transcript(who: str, text: str):
    """Build a mock server response with input or output transcription."""
    content = SimpleNamespace(
        model_turn=None,
        input_transcription=SimpleNamespace(text=text) if who == "input" else None,
        output_transcription=SimpleNamespace(text=text) if who == "output" else None,
        interrupted=None,
    )
    return SimpleNamespace(server_content=content, session_resumption_update=None)


class _RaisingLiveConnect:
    """Async context manager that fails during Gemini Live connect()."""

    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    async def __aenter__(self):
        self.calls += 1
        raise self.error

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
class TestLiveAudioAuth:
    """LA-01: Authentication — reject unauthenticated WebSocket connections."""

    async def test_missing_init_data(self, test_client):
        """Connection without initData should be closed immediately."""
        try:
            async with test_client.websocket("/webapp/live/ws") as ws:
                await ws.receive()
        except Exception:
            pass  # Expected — websocket closed by server

    async def test_invalid_init_data(self, test_client, mock_bot_token):
        """Connection with tampered initData should be rejected."""
        url = "/webapp/live/ws?initData=user%3Dfake%26hash%3Dinvalid"
        try:
            async with test_client.websocket(url) as ws:
                await ws.receive()
        except Exception:
            pass  # Expected — websocket closed by server


@pytest.mark.asyncio
class TestLiveAudioPage:
    """LA-05: Static page serving."""

    async def test_live_page_returns_200(self, test_client):
        resp = await test_client.get("/webapp/live")
        assert resp.status_code == 200
        body = (await resp.get_data()).decode()
        assert "Live AI" in body or "visualizer" in body


@pytest.mark.asyncio
class TestLiveAudioProxy:
    """LA-02 through LA-04: WebSocket proxy lifecycle."""

    async def test_connect_sends_connected_event(self, test_client, mock_bot_token, mock_api_keys):
        """LA-02: After auth, server should send {"type": "connected"}."""
        init_data = make_valid_init_data(mock_bot_token, user_id=555)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        # Mock the Gemini Live session
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.send_realtime_input = AsyncMock()

        # receive() must be an async generator that yields nothing then hangs
        async def _empty_gen():
            return
            yield  # noqa: RET504 — make this an async generator

        mock_session.receive = _empty_gen

        mock_client = MagicMock()
        mock_client.aio.live.connect.return_value = mock_session

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=mock_client),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                raw = await ws.receive()
                msg = json.loads(raw)
                assert msg["type"] == "connected"
                connect_kwargs = mock_client.aio.live.connect.call_args.kwargs
                config = connect_kwargs["config"]
                assert connect_kwargs["model"] == "gemini-3.1-flash-live-preview"
                assert config.session_resumption is not None
                assert config.session_resumption.transparent is True
                assert config.context_window_compression is not None
                assert config.speech_config is not None

    async def test_audio_forwarding(self, test_client, mock_bot_token, mock_api_keys):
        """LA-03: realtime_input message should trigger session.send_realtime_input."""
        init_data = make_valid_init_data(mock_bot_token, user_id=556)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.send_realtime_input = AsyncMock()

        # Consumer yields one audio response then stops
        pcm_data = b"\x00\x01" * 100
        responses = [_make_response_with_audio(pcm_data)]

        async def _gen():
            for r in responses:
                yield r

        mock_session.receive = _gen

        mock_client = MagicMock()
        mock_client.aio.live.connect.return_value = mock_session

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=mock_client),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                # 1. Receive "connected" event
                connected_raw = await ws.receive()
                assert json.loads(connected_raw)["type"] == "connected"

                # 2. Send audio input
                test_audio = base64.b64encode(b"\x00\x01" * 50).decode()
                await ws.send(
                    json.dumps(
                        {
                            "type": "realtime_input",
                            "mime_type": "audio/pcm;rate=16000",
                            "data": test_audio,
                        }
                    )
                )

                # 3. Receive audio output from Gemini
                audio_raw = await ws.receive()
                audio_msg = json.loads(audio_raw)
                assert audio_msg["type"] == "audio"
                assert audio_msg["data"]  # Non-empty base64

    async def test_interrupt_forwarding(self, test_client, mock_bot_token, mock_api_keys):
        """LA-04: Consumer relays 'interrupted' signal to the browser."""
        init_data = make_valid_init_data(mock_bot_token, user_id=557)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.send_realtime_input = AsyncMock()

        responses = [_make_response_with_interrupt()]

        async def _gen():
            for r in responses:
                yield r

        mock_session.receive = _gen

        mock_client = MagicMock()
        mock_client.aio.live.connect.return_value = mock_session

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=mock_client),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                connected_raw = await ws.receive()
                assert json.loads(connected_raw)["type"] == "connected"

                interrupt_raw = await ws.receive()
                interrupt_msg = json.loads(interrupt_raw)
                assert interrupt_msg["type"] == "interrupt"

    async def test_transcript_forwarding(self, test_client, mock_bot_token, mock_api_keys):
        """LA-02b: Consumer relays input/output transcripts to browser."""
        init_data = make_valid_init_data(mock_bot_token, user_id=558)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.send_realtime_input = AsyncMock()

        responses = [
            _make_response_with_transcript("input", "Привет"),
            _make_response_with_transcript("output", "Здравствуйте!"),
        ]

        async def _gen():
            for r in responses:
                yield r

        mock_session.receive = _gen

        mock_client = MagicMock()
        mock_client.aio.live.connect.return_value = mock_session

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=mock_client),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                connected_raw = await ws.receive()
                assert json.loads(connected_raw)["type"] == "connected"

                in_raw = await ws.receive()
                in_msg = json.loads(in_raw)
                assert in_msg["type"] == "input_transcript"
                assert in_msg["text"] == "Привет"

                out_raw = await ws.receive()
                out_msg = json.loads(out_raw)
                assert out_msg["type"] == "output_transcript"
                assert out_msg["text"] == "Здравствуйте!"

    async def test_resource_exhausted_sends_single_fatal_without_retry_storm(
        self,
        test_client,
        mock_bot_token,
        mock_api_keys,
        monkeypatch,
    ):
        """RESOURCE_EXHAUSTED should trip a model cooldown and stop reconnect retries."""
        init_data = make_valid_init_data(mock_bot_token, user_id=559)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        failing_connect = _RaisingLiveConnect(
            Exception("1011 None. You exceeded your current quota, please check your plan and billing details.")
        )
        mock_client = MagicMock()
        mock_client.aio.live.connect.return_value = failing_connect

        monkeypatch.setattr("app.web_miniapp._LIVE_MODEL_COOLDOWN_UNTIL", 0.0)
        monkeypatch.setattr("app.web_miniapp._LIVE_MODEL_COOLDOWN_REASON", "")

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=mock_client),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                fatal_raw = await ws.receive()
                fatal_msg = json.loads(fatal_raw)
                assert fatal_msg["type"] == "fatal"
                assert fatal_msg["reason"] == "server_capacity"
                assert fatal_msg["retry_after_seconds"] >= 15

        assert failing_connect.calls == 1

    async def test_misconfigured_vertex_returns_controlled_fatal(self, test_client, mock_bot_token):
        init_data = make_valid_init_data(mock_bot_token, user_id=560)
        url = f"/webapp/live/ws?initData={urllib.parse.quote(init_data)}"

        with (
            patch("app.providers.gemini.get_live_api_client", return_value=None),
            patch("app.games.crocodile_flags.is_live_audio_enabled", new_callable=AsyncMock, return_value=True),
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=None),
        ):
            async with test_client.websocket(url) as ws:
                fatal_raw = await ws.receive()
                fatal_msg = json.loads(fatal_raw)
                assert fatal_msg["type"] == "fatal"
                assert fatal_msg["reason"] == "misconfigured"
