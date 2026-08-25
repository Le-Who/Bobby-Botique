"""Authorization boundary for Mini App endpoints that expose private data."""

import urllib.parse
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from quart.testing.connections import WebsocketDisconnectError

from app.web import quart_app
from app.web_miniapp import require_authorized_webapp_user
from tests.factories import make_valid_init_data

_BOT_TOKEN = "test-token"


class _GraphTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.in_transaction = True
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.in_transaction = False
        return False


class _GraphConnection:
    def __init__(self):
        self.in_transaction = False
        self.transaction_entries = 0

    def transaction(self):
        return _GraphTransaction(self)


class _GraphAcquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self.pool.acquire_count += 1
        return self.pool.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _GraphPool:
    def __init__(self, connection):
        self.connection = connection
        self.acquire_count = 0

    def acquire(self):
        return _GraphAcquire(self)


@pytest.mark.asyncio
async def test_revoked_signed_user_is_rejected_before_private_endpoint():
    endpoint = AsyncMock(return_value="private data")
    protected = require_authorized_webapp_user(endpoint)

    async with quart_app.test_request_context("/webapp/api/memories"):
        with patch("app.repos.users.is_authorized", new_callable=AsyncMock, return_value=False):
            response, status = await protected(user_id=42)

    assert status == 403
    assert (await response.get_json())["error"] == "Access revoked"
    endpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_signed_user_reaches_private_endpoint():
    endpoint = AsyncMock(return_value="private data")
    protected = require_authorized_webapp_user(endpoint)

    async with quart_app.test_request_context("/webapp/api/memories"):
        with patch("app.repos.users.is_authorized", new_callable=AsyncMock, return_value=True):
            result = await protected(user_id=42)

    assert result == "private data"
    endpoint.assert_awaited_once_with(user_id=42)


@pytest.mark.asyncio
async def test_natal_submission_also_checks_bot_authorization():
    from app.web_miniapp import api_natal_submit

    # Bypass only Telegram initData validation; the next decorator must still
    # reject a signed identity whose bot access was revoked.
    authorization_layer = api_natal_submit.__wrapped__
    async with quart_app.test_request_context("/webapp/api/natal/submit", method="POST"):
        with patch("app.repos.users.is_authorized", new_callable=AsyncMock, return_value=False):
            response, status = await authorization_layer(user_id=42)

    assert status == 403
    assert (await response.get_json())["error"] == "Access revoked"


@pytest.mark.asyncio
async def test_settings_ltm_toggle_uses_dedicated_consent_write():
    from app.web_miniapp import api_update_settings

    core = api_update_settings.__wrapped__.__wrapped__
    chat_state = SimpleNamespace(
        system_prompt=None,
        model="test-model",
        thinking_level=None,
        ltm_enabled=True,
        memory_epoch=5,
        search_enabled=False,
        temperature=None,
        tts_temperature=None,
        voice_id=None,
    )

    async with quart_app.test_request_context(
        "/webapp/api/settings",
        method="PATCH",
        json={"ltm_enabled": False},
    ):
        with (
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.repos.chats.update_user_chat", new_callable=AsyncMock) as update_chat,
            patch("app.repos.chats.set_ltm_enabled", new_callable=AsyncMock, return_value=6) as set_ltm,
            patch(
                "app.repos.memory_autosave.cancel_user_memory_tasks",
                new_callable=AsyncMock,
            ) as cancel_tasks,
        ):
            response = await core(user_id=42)

    assert (await response.get_json()) == {"ok": True}
    set_ltm.assert_awaited_once_with(42, False)
    cancel_tasks.assert_awaited_once_with(42)
    update_chat.assert_not_awaited()
    assert chat_state.memory_epoch == 6


@pytest.mark.asyncio
async def test_settings_ltm_toggle_rejects_non_boolean_json():
    from app.web_miniapp import api_update_settings

    core = api_update_settings.__wrapped__.__wrapped__
    chat_state = SimpleNamespace(ltm_enabled=True)

    async with quart_app.test_request_context(
        "/webapp/api/settings",
        method="PATCH",
        json={"ltm_enabled": "false"},
    ):
        with (
            patch("app.repos.chats.get_user_chat", new_callable=AsyncMock, return_value=chat_state),
            patch("app.repos.chats.update_user_chat", new_callable=AsyncMock) as update_chat,
            patch("app.repos.chats.set_ltm_enabled", new_callable=AsyncMock) as set_ltm,
        ):
            response, status = await core(user_id=42)

    assert status == 400
    assert (await response.get_json())["error"] == "invalid_ltm_enabled"
    set_ltm.assert_not_awaited()
    update_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_api_uses_one_consent_locked_tenant_transaction_for_its_snapshot():
    from app import database
    from app.web_miniapp import api_graph_data

    core = api_graph_data.__wrapped__.__wrapped__
    connection = _GraphConnection()
    pool = _GraphPool(connection)
    calls: list[tuple[str, tuple, object]] = []
    lease_active = False
    lease_calls: list[tuple[int, int | None, str, bool]] = []

    @asynccontextmanager
    async def tracked_lease(user_id, expected_epoch, *, purpose, require_ltm):
        nonlocal lease_active
        lease_calls.append((user_id, expected_epoch, purpose, require_ltm))
        lease_active = True
        try:
            yield True
        finally:
            lease_active = False

    async def query(sql, params=(), *, conn=None, **_kwargs):
        assert lease_active is True
        assert conn is connection
        assert connection.in_transaction is True
        calls.append((" ".join(sql.split()), params, conn))
        if "FROM chats" in sql:
            return [{"ltm_enabled": True}]
        if "FROM memory_nodes" in sql:
            return [{"id": 10, "entity_name": "Alice", "entity_type": "person", "description": "User"}]
        if "FROM memory_edges" in sql:
            return []
        raise AssertionError(f"unexpected graph SQL: {sql}")

    def assert_context_in_transaction(*_args, **kwargs):
        assert kwargs["conn"] is connection
        assert connection.in_transaction is True

    async with quart_app.test_request_context("/webapp/api/graph"):
        with (
            patch.object(database.db_manager, "pool", pool),
            patch.object(database, "set_user_context", new_callable=AsyncMock, side_effect=assert_context_in_transaction) as set_context,
            patch.object(database, "db_query", new_callable=AsyncMock, side_effect=query),
            patch(
                "app.repos.memory_consent.resolve_current_epoch",
                new_callable=AsyncMock,
                return_value=9,
            ) as resolve_epoch,
            patch("app.repos.memory_consent.private_data_lease", tracked_lease),
        ):
            response = await core(user_id=42)

    payload = await response.get_json()
    assert payload == {
        "nodes": [{"id": 10, "name": "Alice", "type": "person", "description": "User"}],
        "edges": [],
    }
    assert pool.acquire_count == 1
    assert connection.transaction_entries == 1
    set_context.assert_awaited_once_with(42, False, conn=connection)
    assert "FROM chats" in calls[0][0]
    assert "FOR SHARE" in calls[0][0]
    assert all(call_conn is connection for _, _, call_conn in calls)
    node_sql = next(sql for sql, _, _ in calls if "FROM memory_nodes" in sql)
    edge_sql = next(sql for sql, _, _ in calls if "FROM memory_edges" in sql)
    assert "memory_node_sources" in node_sql
    assert "long_term_memory" in node_sql
    assert "expires_at" in node_sql
    assert "memory_edge_sources" in edge_sql
    assert "long_term_memory" in edge_sql
    assert "expires_at" in edge_sql
    resolve_epoch.assert_awaited_once_with(42, require_ltm=True)
    assert lease_calls == [(42, 9, "ltm:miniapp-graph", True)]
    assert lease_active is False


@pytest.mark.parametrize("consent_rows", [[], [{"ltm_enabled": False}]])
@pytest.mark.asyncio
async def test_graph_api_fails_closed_before_graph_reads_without_durable_consent(consent_rows):
    from app import database
    from app.web_miniapp import api_graph_data

    core = api_graph_data.__wrapped__.__wrapped__
    connection = _GraphConnection()
    pool = _GraphPool(connection)
    calls: list[str] = []

    async def query(sql, params=(), *, conn=None, **_kwargs):
        assert conn is connection
        assert connection.in_transaction is True
        calls.append(" ".join(sql.split()))
        if "FROM chats" in sql:
            return consent_rows
        raise AssertionError("graph rows were read without durable LTM consent")

    @asynccontextmanager
    async def allowed_lease(*_args, **_kwargs):
        yield True

    async with quart_app.test_request_context("/webapp/api/graph"):
        with (
            patch.object(database.db_manager, "pool", pool),
            patch.object(database, "set_user_context", new_callable=AsyncMock),
            patch.object(database, "db_query", new_callable=AsyncMock, side_effect=query),
            patch(
                "app.repos.memory_consent.resolve_current_epoch",
                new_callable=AsyncMock,
                return_value=9,
            ),
            patch("app.repos.memory_consent.private_data_lease", allowed_lease),
        ):
            response = await core(user_id=42)

    assert (await response.get_json()) == {"nodes": [], "edges": []}
    assert len(calls) == 1
    assert "FROM chats" in calls[0]
    assert "FOR SHARE" in calls[0]


@pytest.mark.parametrize(
    ("method", "path", "payload", "private_boundary"),
    [
        (
            "get",
            "/webapp/api/miniapp/trivia/today",
            None,
            "app.repos.daily_trivia.get_result_if_exists",
        ),
        (
            "post",
            "/webapp/api/miniapp/trivia/submit_answer",
            {"question_index": 0, "selected_index": 0},
            "app.repos.daily_trivia.get_or_create_result",
        ),
        (
            "post",
            "/webapp/api/miniapp/trivia/submit_super_answer",
            {"question_index": 0, "selected_index": 0},
            "app.repos.daily_trivia.get_result_if_exists",
        ),
    ],
    ids=["today", "answer", "super-answer"],
)
@pytest.mark.parametrize(
    ("authorization_outcome", "expected_status"),
    [(False, 403), (RuntimeError("private database detail"), 503)],
    ids=["revoked", "authorization-error"],
)
@pytest.mark.asyncio
async def test_legacy_trivia_routes_check_signed_user_authorization_before_private_work(
    monkeypatch,
    method,
    path,
    payload,
    private_boundary,
    authorization_outcome,
    expected_status,
):
    monkeypatch.setattr(
        "app.web_miniapp.settings",
        SimpleNamespace(TELEGRAM_BOT_TOKEN=_BOT_TOKEN),
    )
    init_data = make_valid_init_data(_BOT_TOKEN, user_id=777)
    auth_kwargs = (
        {"side_effect": authorization_outcome}
        if isinstance(authorization_outcome, Exception)
        else {"return_value": authorization_outcome}
    )

    with (
        patch("app.repos.users.is_authorized", new_callable=AsyncMock, **auth_kwargs) as is_authorized,
        patch(private_boundary, new_callable=AsyncMock) as private_work,
    ):
        client = quart_app.test_client()
        request_method = getattr(client, method)
        response = await request_method(
            path,
            headers={"X-TG-INIT-DATA": init_data},
            **({"json": payload} if payload is not None else {}),
        )

    assert response.status_code == expected_status
    is_authorized.assert_awaited_once_with(777)
    private_work.assert_not_awaited()


@pytest.mark.parametrize(
    ("path", "private_boundary"),
    [
        (
            "/webapp/daily2048/ws",
            "app.games.daily_2048.get_daily_state",
        ),
        (
            "/webapp/game/daily/ws",
            "app.games.crocodile_daily.get_daily_overview",
        ),
        (
            "/webapp/game/ws?game_id=game-auth",
            "app.games.crocodile.load_game",
        ),
        (
            "/webapp/live/ws",
            "app.web_miniapp._handle_live_session",
        ),
        (
            "/webapp/live-vertex/ws",
            "app.web_miniapp._handle_live_session",
        ),
    ],
    ids=["daily-2048", "daily-crocodile", "crocodile", "live", "live-vertex"],
)
@pytest.mark.parametrize(
    "authorization_outcome",
    [False, RuntimeError("database details must stay private")],
    ids=["revoked", "authorization-error"],
)
@pytest.mark.asyncio
async def test_websocket_authorization_fails_closed_before_private_work(
    monkeypatch,
    path,
    private_boundary,
    authorization_outcome,
):
    """A valid Telegram signature is insufficient after bot access is revoked."""
    monkeypatch.setattr(
        "app.web_miniapp.settings",
        SimpleNamespace(TELEGRAM_BOT_TOKEN=_BOT_TOKEN),
    )
    init_data = make_valid_init_data(_BOT_TOKEN, user_id=777)
    separator = "&" if "?" in path else "?"
    url = f"{path}{separator}initData={urllib.parse.quote(init_data)}"

    auth_kwargs = (
        {"side_effect": authorization_outcome}
        if isinstance(authorization_outcome, Exception)
        else {"return_value": authorization_outcome}
    )
    with (
        patch(
            "app.repos.users.is_authorized",
            new_callable=AsyncMock,
            **auth_kwargs,
        ) as is_authorized,
        patch(
            private_boundary,
            new_callable=AsyncMock,
            side_effect=RuntimeError("private work must not run"),
        ) as private_work,
    ):
        with pytest.raises(WebsocketDisconnectError) as disconnected:
            async with quart_app.test_client().websocket(url) as ws:
                await ws.receive()

    assert disconnected.value.args == (4003,)
    is_authorized.assert_awaited_once_with(777)
    private_work.assert_not_awaited()
