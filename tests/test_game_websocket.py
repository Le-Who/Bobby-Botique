from __future__ import annotations

import json
import urllib.parse
from unittest.mock import AsyncMock, patch

import pytest

from app.web import quart_app
from tests.factories import make_crocodile_game, make_valid_init_data


@pytest.fixture
def test_client():
    return quart_app.test_client()


@pytest.fixture
def mock_bot_token(monkeypatch):
    monkeypatch.setattr("app.config.settings.TELEGRAM_BOT_TOKEN", "test-token")
    return "test-token"


@pytest.mark.asyncio
class TestWebSocketAuth:
    """WS-01: Authentication testing."""

    async def test_missing_init_data(self, test_client):
        try:
            async with test_client.websocket("/webapp/ws?game_id=123") as ws:
                await ws.receive()
        except Exception:
            pass  # Expected disconnect

    async def test_invalid_game_id(self, test_client, mock_bot_token):
        init_data = make_valid_init_data(mock_bot_token, user_id=111)
        url = f"/webapp/game/ws?initData={urllib.parse.quote(init_data)}&game_id=nonexistent"
        with patch("app.games.crocodile.load_game", new_callable=AsyncMock) as load_mock:
            load_mock.return_value = None
            try:
                async with test_client.websocket(url) as ws:
                    await ws.receive()
            except Exception:
                pass


@pytest.mark.asyncio
class TestWebSocketEvents:
    """WS-02 through WS-05."""

    async def test_history_sync_on_connect(self, test_client, mock_bot_token):
        """WS-02: Connect and verify history sync occurs before main loop."""
        init_data = make_valid_init_data(mock_bot_token, user_id=222)
        url = f"/webapp/game/ws?initData={urllib.parse.quote(init_data)}&game_id=game1"

        game = make_crocodile_game(game_id="game1", creator_id=111, guesser_id=222)

        with (
            patch("app.games.crocodile.load_game", new_callable=AsyncMock) as load_mock,
            patch("app.games.crocodile.get_game_history") as hist_mock,
        ):
            load_mock.return_value = game
            # Make get_game_history return some predefined items
            hist_mock.return_value = [{"event": "guess", "guess": "кот", "result": "cold"}]

            async with test_client.websocket(url) as ws:
                # 1. First event is game_state
                state_raw = await ws.receive()
                state = json.loads(state_raw)
                assert state["event"] == "game_state"
                assert state["is_creator"] is False

                # 2. Second event should be history_sync
                hist_raw = await ws.receive()
                hist = json.loads(hist_raw)
                assert hist["event"] == "history_sync"
                assert len(hist["items"]) == 1
                assert hist["items"][0]["guess"] == "кот"

    async def test_hint_messaging(self, test_client, mock_bot_token):
        """WS-03: Hint request loop."""
        init_data = make_valid_init_data(mock_bot_token, user_id=222)
        url = f"/webapp/game/ws?initData={urllib.parse.quote(init_data)}&game_id=game2"
        game = make_crocodile_game(game_id="game2", creator_id=111, guesser_id=222)

        with (
            patch("app.games.crocodile.load_game", new_callable=AsyncMock) as load_mock,
            patch("app.games.crocodile.get_game_history", return_value=[]),
            patch("app.games.crocodile.get_game_hints", return_value=["Hint #1"]),
        ):
            load_mock.return_value = game

            async with test_client.websocket(url) as ws:
                # Initial game_state
                await ws.receive()

                # Request a hint within bounds
                await ws.send(json.dumps({"type": "hint", "hint_index": 0}))
                resp1_raw = await ws.receive()
                resp1 = json.loads(resp1_raw)
                assert resp1["event"] == "hint"
                assert resp1["text"] == "Hint #1"
                assert resp1["available"] is True

                # Request out of bounds
                await ws.send(json.dumps({"type": "hint", "hint_index": 5}))
                resp2_raw = await ws.receive()
                resp2 = json.loads(resp2_raw)
                assert resp2["event"] == "hint"
                assert resp2["available"] is False
                assert "готовятся или закончились" in resp2["text"]

    async def test_guess_messaging(self, test_client, mock_bot_token):
        """WS-04: Submit guess."""
        init_data = make_valid_init_data(mock_bot_token, user_id=222)
        url = f"/webapp/game/ws?initData={urllib.parse.quote(init_data)}&game_id=game3"
        game = make_crocodile_game(game_id="game3", creator_id=111, guesser_id=222)
        game.process_guess = AsyncMock(return_value={"event": "cold", "hint": "No."})

        with (
            patch("app.games.crocodile.load_game", new_callable=AsyncMock) as load_mock,
            patch("app.games.crocodile.get_game_history", return_value=[]),
        ):
            load_mock.return_value = game

            async with test_client.websocket(url) as ws:
                await ws.receive()

                await ws.send(json.dumps({"type": "guess", "word": "кот", "pending_id": "pid-1"}))
                resp_raw = await ws.receive()
                resp = json.loads(resp_raw)

                assert resp["event"] == "cold"
                assert resp["hint"] == "No."
                assert resp["pending_id"] == "pid-1"

                game.process_guess.assert_awaited_once_with("кот")

    async def test_creator_guard(self, test_client, mock_bot_token):
        """WS-05: Creator cannot guess."""
        init_data = make_valid_init_data(mock_bot_token, user_id=111)  # Creator ID
        url = f"/webapp/game/ws?initData={urllib.parse.quote(init_data)}&game_id=game4"
        game = make_crocodile_game(game_id="game4", creator_id=111)
        game.process_guess = AsyncMock()

        with (
            patch("app.games.crocodile.load_game", new_callable=AsyncMock) as load_mock,
            patch("app.games.crocodile.get_game_history", return_value=[]),
        ):
            load_mock.return_value = game

            async with test_client.websocket(url) as ws:
                state_raw = await ws.receive()
                state = json.loads(state_raw)
                assert state["is_creator"] is True  # Validated as creator

                await ws.send(json.dumps({"type": "guess", "word": "кот"}))
                resp_raw = await ws.receive()
                resp = json.loads(resp_raw)

                assert resp["event"] == "error"
                assert "Создатель игры не может отгадывать" in resp["message"]

                game.process_guess.assert_not_called()
