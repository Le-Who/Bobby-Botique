"""Availability and editor conflicts must preserve played Daily 2048 games."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.games import daily_2048 as game
from app.handlers import daily_2048 as handler
from app.repos import daily_2048 as repo
from app.web import quart_app
from tests.factories import make_valid_init_data

DAY = date(2026, 10, 28)
BOARD = [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]


def active_result():
    return repo.Daily2048Result(777, DAY, "active", BOARD, 0, 3, 12, 0, 1000, datetime.now(UTC), None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_unavailable_daily_state_never_creates_a_result(monkeypatch, status):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status=status)
    create = AsyncMock(return_value=active_result())
    monkeypatch.setattr(repo, "ensure_today_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(repo, "get_or_create_result", create)

    with pytest.raises(Exception, match=status) as raised:
        await game.get_daily_state(777)

    assert raised.value.reason == status
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_telegram_unavailable_entry_is_explained_without_game_invite(monkeypatch, status):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status=status)
    monkeypatch.setattr(repo, "ensure_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(handler, "_get_cover_photo", AsyncMock(return_value="cover"))
    monkeypatch.setattr(handler, "_remember_cover_file_id", AsyncMock())
    monkeypatch.setattr(repo, "register_prompt_message", AsyncMock())
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())

    await handler.send_daily2048_entry(bot, 777, DAY, mark_delivered=False)

    assert "недоступна" in bot.send_message.await_args.kwargs["text"]
    assert not bot.send_message.await_args.kwargs.get("reply_markup")
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_editor_revision_is_rejected_before_write():
    with (
        patch("app.web._is_authenticated", return_value=True),
        patch.object(
            repo, "upsert_puzzle", AsyncMock(return_value=repo.Daily2048Puzzle(DAY, BOARD, "tile", 256))
        ) as save,
    ):
        response = await quart_app.test_client().post(
            "/api/admin/daily2048/puzzle", json={"date": DAY.isoformat(), "board": BOARD}
        )
    assert response.status_code == 400
    assert (await response.get_json())["error"] == "expected_revision required"
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_miniapp_unavailable_start_sends_safe_message(monkeypatch, status):
    from urllib.parse import quote

    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.update_user_display_name", AsyncMock())
    monkeypatch.setattr(
        repo,
        "ensure_today_puzzle",
        AsyncMock(return_value=repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status=status)),
    )
    monkeypatch.setattr(repo, "get_or_create_result", AsyncMock(return_value=active_result()))
    url = "/webapp/daily2048/ws?initData=" + quote(make_valid_init_data("test-token", user_id=777))

    async with quart_app.test_client().websocket(url) as ws:
        payload = json.loads(await ws.receive())
    assert payload["event"] == "error"
    assert payload["reason"] == status
    assert "недоступна" in payload["message"]


@pytest.mark.asyncio
async def test_disabled_move_preserves_persisted_board_and_score(monkeypatch):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status="disabled")
    result = repo.Daily2048Result(777, DAY, "active", BOARD, 0, 3, 12, 0, 1000, datetime.now(UTC), None, None)
    monkeypatch.setattr(game, "get_daily_state", AsyncMock(return_value=(puzzle, result)))
    write = AsyncMock(return_value=active_result())
    monkeypatch.setattr(repo, "update_result_after_move", write)
    with pytest.raises(Exception, match="disabled"):
        await game.process_move(777, "left")
    assert result.board == BOARD and result.moves == 3 and result.merge_score == 12
    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_ensure_preserves_intentional_unavailable_puzzle(monkeypatch, status):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status=status)
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=puzzle))
    save = AsyncMock()
    monkeypatch.setattr(repo, "upsert_puzzle", save)
    assert await repo.ensure_puzzle(DAY) is puzzle
    save.assert_not_awaited()


class PuzzleTransport:
    """Small SQL transport double; concurrency is tested with real PostgreSQL."""

    def __init__(self, puzzle, result=None):
        self.row = asdict(puzzle) if puzzle else None
        self.result = asdict(result) if result else None
        self.writes = 0

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, query, *args):
        assert "INSERT INTO public.users" in query

    async def fetchval(self, query, *args):
        assert "SELECT EXISTS" in query
        return self.result is not None

    async def fetchrow(self, query, *args):
        if "FOR UPDATE" in query or "FOR SHARE" in query:
            return copy.deepcopy(self.row)
        if "INSERT INTO public.daily_2048_results" in query:
            if self.result is None:
                self.result = asdict(replace(active_result(), board=copy.deepcopy(args[2]), moves=0, merge_score=0))
            return copy.deepcopy(self.result)
        if "INSERT INTO public.daily_2048_puzzles" in query:
            if self.row:
                return None
            self.row = asdict(repo.Daily2048Puzzle(*args[:4]))
        elif "UPDATE public.daily_2048_puzzles" in query:
            self.row["revision"] += 1
        else:
            raise AssertionError(query)
        self.writes += 1
        if len(args) == 2:
            self.row["status"] = args[1]
        else:
            for key, value in zip(
                (
                    "puzzle_date",
                    "board",
                    "goal_type",
                    "goal_value",
                    "spawn_sequence",
                    "seed",
                    "par_moves",
                    "target_seconds",
                    "status",
                    "solution_moves",
                ),
                args,
                strict=True,
            ):
                self.row[key] = copy.deepcopy(value)
        return copy.deepcopy(self.row)


def edit_values(puzzle):
    return {
        key: getattr(puzzle, key)
        for key in (
            "board",
            "goal_type",
            "goal_value",
            "spawn_sequence",
            "seed",
            "par_moves",
            "target_seconds",
            "status",
            "solution_moves",
        )
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("board", [[4, 4, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]),
        ("goal_type", "total"),
        ("goal_value", 512),
        ("spawn_sequence", [{"x": 1, "y": 2, "value": 4}]),
        ("seed", "changed"),
        ("par_moves", 90),
        ("target_seconds", 300),
        ("solution_moves", "left"),
    ],
)
async def test_each_played_gameplay_field_is_immutable(field, value):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, revision=4)
    transport = PuzzleTransport(puzzle, active_result())
    before = copy.deepcopy((transport.row, transport.result))
    values = {**edit_values(puzzle), field: value}
    with pytest.raises(repo.DailyPuzzleConflict) as raised:
        await repo.upsert_puzzle(DAY, expected_revision=4, conn=transport, **values)
    assert raised.value.reason == "puzzle_in_use"
    assert raised.value.actual_revision == 4
    assert (transport.row, transport.result) == before
    assert transport.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [0, 1, 3, 5])
async def test_stale_revision_cannot_overwrite_an_existing_puzzle(expected):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, revision=4)
    transport = PuzzleTransport(puzzle)
    with pytest.raises(repo.DailyPuzzleConflict) as raised:
        await repo.upsert_puzzle(DAY, expected_revision=expected, conn=transport, **edit_values(puzzle))
    assert raised.value.actual_revision == 4
    assert raised.value.reason == "revision_conflict"
    assert transport.writes == 0


@pytest.mark.asyncio
async def test_unchanged_played_save_and_status_cas_preserve_entire_result():
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, revision=4)
    transport = PuzzleTransport(puzzle, active_result())
    original_result = copy.deepcopy(transport.result)
    saved = await repo.upsert_puzzle(DAY, expected_revision=4, conn=transport, **edit_values(puzzle))
    disabled = await repo.set_puzzle_status(DAY, status="disabled", expected_revision=5, conn=transport)
    restored = await repo.set_puzzle_status(DAY, status="ready", expected_revision=6, conn=transport)
    assert (saved.revision, disabled.revision, restored.revision) == (5, 6, 7)
    assert saved.played and disabled.played and restored.played
    assert restored.board == BOARD and restored.goal_value == 256
    assert transport.result == original_result


@pytest.mark.asyncio
async def test_atomic_start_uses_fresh_locked_rules_and_repeat_preserves_result():
    stale = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)
    fresh = replace(stale, board=[[128, 128, 0, 0], *BOARD[1:]], goal_value=512, seed="edited", revision=2)
    transport = PuzzleTransport(fresh)
    current, result = await repo.start_puzzle(777, stale, conn=transport)
    assert current.goal_value == 512 and current.seed == "edited" and current.revision == 2
    assert result.board == [[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
    transport.result.update(moves=7, merge_score=88, elapsed_ms=9000)
    _, repeated = await repo.start_puzzle(777, stale, conn=transport)
    assert (repeated.moves, repeated.merge_score, repeated.elapsed_ms) == (7, 88, 9000)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_atomic_start_rechecks_status_after_an_optimistic_ready_read(status):
    stale = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)
    transport = PuzzleTransport(replace(stale, status=status, revision=2))
    with pytest.raises(repo.DailyPuzzleUnavailable) as raised:
        await repo.start_puzzle(777, stale, conn=transport)
    assert raised.value.reason == status
    assert transport.result is None


@pytest.mark.asyncio
async def test_atomic_start_conditionally_inserts_a_missing_puzzle():
    candidate = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)
    transport = PuzzleTransport(None)
    current, result = await repo.start_puzzle(777, candidate, conn=transport)
    assert current.revision == 1 and current.board == BOARD
    assert result.board == BOARD and result.moves == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revision_conflict", "puzzle_in_use"])
async def test_admin_conflict_returns_current_safe_puzzle(reason):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, revision=4, played=True)
    with (
        patch("app.web._is_authenticated", return_value=True),
        patch.object(repo, "upsert_puzzle", AsyncMock(side_effect=repo.DailyPuzzleConflict(4, reason, puzzle=puzzle))),
    ):
        response = await quart_app.test_client().post(
            "/api/admin/daily2048/puzzle", json={"date": DAY.isoformat(), "expected_revision": 3}
        )
    assert response.status_code == 409
    payload = await response.get_json()
    assert payload["reason"] == reason and payload["actual_revision"] == 4
    assert payload["puzzle"]["revision"] == 4
    assert payload["puzzle"]["played"] is True and payload["puzzle"]["gameplay_locked"] is True
    assert payload["puzzle"]["board"] == BOARD


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "move", "elapsed"])
async def test_player_sql_uses_shared_puzzle_lock_without_played_lookup(monkeypatch, operation):
    """Verify emitted SQL contract; actual independent-player progress uses PG."""
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)

    class PlayerTransport(PuzzleTransport):
        def __init__(self):
            super().__init__(puzzle, active_result())
            self.locks = []
            self.played_queries = 0

        async def fetchrow(self, sql, *args):
            if "SELECT * FROM public.daily_2048_puzzles" in sql:
                self.locks.append(sql.split(" WHERE ")[1])
            return await super().fetchrow(sql, *args)

        async def fetchval(self, sql, *args):
            self.played_queries += 1
            return await super().fetchval(sql, *args)

        async def fetch(self, sql, *args):
            assert "UPDATE public.daily_2048_results" in sql
            return [self.result]

    transport = PlayerTransport()

    @asynccontextmanager
    async def acquire():
        yield transport

    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    if operation == "start":
        _, result = await repo.start_puzzle(777, puzzle)
    elif operation == "elapsed":
        result = await repo.update_result_elapsed(user_id=777, puzzle_date=DAY, elapsed_ms=2000)
    else:
        result = await repo.update_result_after_move(
            user_id=777,
            puzzle_date=DAY,
            board=BOARD,
            spawn_index=0,
            moves=3,
            merge_score=12,
            elapsed_ms=2000,
            status="active",
            final_score=0,
            won=False,
            finished=False,
        )
    assert result.user_id == 777
    assert transport.locks == ["puzzle_date = $1 FOR SHARE"]
    assert transport.played_queries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_owned", [True, False])
async def test_terminal_move_fallback_reads_same_transaction_without_pool_reacquire(monkeypatch, caller_owned):
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)
    terminal = replace(active_result(), status="won", moves=7, final_score=888)

    class TerminalTransport(PuzzleTransport):
        in_transaction = False

        @asynccontextmanager
        async def transaction(self):
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

        async def fetch(self, sql, *args):
            assert self.in_transaction
            if "UPDATE public.daily_2048_results" in sql:
                return []  # Already terminal in this connection's transaction.
            assert "FROM public.daily_2048_results" in sql
            return [asdict(terminal)]

    caller = TerminalTransport(puzzle, terminal)
    stale = SimpleNamespace(fetch=AsyncMock(return_value=[asdict(active_result())]))
    acquisitions = 0

    @asynccontextmanager
    async def acquire():
        nonlocal acquisitions
        acquisitions += 1
        yield caller if not caller_owned and acquisitions == 1 else stale

    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire, _closed=False))
    result = await repo.update_result_after_move(
        user_id=777,
        puzzle_date=DAY,
        board=BOARD,
        spawn_index=0,
        moves=3,
        merge_score=12,
        elapsed_ms=2000,
        status="active",
        final_score=0,
        won=False,
        finished=False,
        conn=caller if caller_owned else None,
    )
    assert (result.status, result.final_score, result.moves) == ("won", 888, 7)
    assert acquisitions == (0 if caller_owned else 1)


@pytest.mark.asyncio
async def test_timer_unavailability_is_sent_and_closes_socket_without_a_move(monkeypatch):
    from urllib.parse import quote

    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.update_user_display_name", AsyncMock())
    puzzle = repo.Daily2048Puzzle(DAY, BOARD, "tile", 256)
    result = active_result()
    monkeypatch.setattr(game, "get_daily_state", AsyncMock(return_value=(puzzle, result)))
    monkeypatch.setattr(repo, "update_result_elapsed", AsyncMock(side_effect=repo.DailyPuzzleUnavailable("disabled")))
    url = "/webapp/daily2048/ws?initData=" + quote(make_valid_init_data("test-token", user_id=777))
    connection = quart_app.test_client().websocket(url)
    close_codes = []
    original_send = connection._asgi_send

    async def capture_send(message):
        if message["type"] == "websocket.close":
            close_codes.append(message["code"])
        await original_send(message)

    connection._asgi_send = capture_send
    async with connection as ws:
        assert (await ws.receive_json())["event"] == "game_state"
        await ws.send_json({"type": "sync_elapsed", "client_elapsed_ms": 2000})
        event = await asyncio.wait_for(ws.receive_json(), timeout=1)
        assert event["event"] == "error" and event["reason"] == "disabled"
        assert "недоступна" in event["message"] and "сохранён" in event["message"]
    assert close_codes == [4004]
    assert result.elapsed_ms == 1000 and result.board == BOARD


@pytest.mark.browser
@pytest.mark.asyncio
async def test_editor_refreshes_conflict_revision_and_locks_played_rules():
    from tests.test_natal_web_report import _run_browser_regression

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    let puzzle = { date:'2026-10-28', board:[[2,2,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]],
      goal_type:'tile',goal_value:256,goal_label:'Собери кубик 256',spawn_sequence:[],seed:'original',
      par_moves:72,target_seconds:240,status:'ready',solution_moves:'',revision:4,played:false,gameplay_locked:false };
    const requests = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/admin/daily2048') return route.fulfill({json:{puzzles:[puzzle]}});
      if (url.pathname === '/api/admin/daily2048/puzzle') {
        requests.push(route.request().postDataJSON());
        puzzle = {...puzzle, revision:5, played:true, gameplay_locked:true, status:'disabled',unavailable_reason:'disabled'};
        return route.fulfill({status:409,json:{error:'puzzle_in_use',reason:'puzzle_in_use',actual_revision:5,puzzle}});
      }
      if (url.pathname === '/api/admin/daily2048/status') {
        requests.push(route.request().postDataJSON());
        puzzle = {...puzzle, revision:6,status:'ready',unavailable_reason:null};
        return route.fulfill({json:{success:true,puzzle}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#2048');
    await page.waitForFunction(() => document.querySelector('#s2048-edit-date').value === '2026-10-28');
    await page.locator('#s2048-save').click();
    await page.waitForFunction(() => document.querySelector('#s2048-edit-revision').textContent.includes('5'));
    assert.equal(requests[0].expected_revision, 4);
    assert.equal(await page.locator('#s2048-edit-goal-value').isDisabled(), true);
    assert.equal(await page.locator('#s2048-editor-board input').first().isDisabled(), true);
    assert.match(await page.locator('#s2048-lock-reason').innerText(), /новую дату/);
    assert.match(await page.locator('#s2048-unavailable-reason').innerText(), /отключ/);
    await page.locator('#s2048-puzzles [data-index]').first().click();
    assert.equal(await page.locator('#s2048-edit-revision').textContent(), '5');
    assert.equal(await page.locator('#s2048-edit-goal-value').isDisabled(), true);
    assert.equal(await page.locator('#s2048-editor-board input').first().isDisabled(), true);
    await page.locator('#s2048-edit-status').selectOption('ready');
    await page.locator('#s2048-status-save').click();
    await page.waitForFunction(() => document.querySelector('#s2048-edit-revision').textContent.includes('6'));
    assert.deepEqual(requests[1], {date:'2026-10-28',status:'ready',expected_revision:5});
    assert.equal(await page.locator('#s2048-edit-goal-value').inputValue(), '256');
    await page.locator('#s2048-edit-date').fill('2026-10-29');
    await page.locator('#s2048-edit-date').dispatchEvent('change');
    assert.equal(await page.locator('#s2048-edit-goal-value').isDisabled(), false);
    await page.locator('#s2048-save').click();
    await page.waitForFunction(() => document.querySelector('#s2048-edit-date').value === '2026-10-28');
    assert.equal(requests[2].expected_revision, 0);
""",
    )


@pytest.mark.browser
def test_player_unavailability_stops_moves_and_reconnect_loop():
    from tests.test_daily_2048_frontend import _run_browser

    _run_browser(r"""
    await page.evaluate(() => window.sockets[0].receive({event:'error',reason:'disabled',message:'Игра сейчас недоступна. Ваш результат сохранён.'}));
    await page.keyboard.press('ArrowRight');
    assert.equal((await moves()).length, 0);
    await page.evaluate(() => window.sockets[0].onclose({code:4004}));
    assert.match(await page.locator('#connection-status').innerText(), /недоступна/);
    await page.waitForTimeout(1200);
    assert.equal(await page.evaluate(() => window.sockets.length), 1);
""")


@pytest.mark.asyncio
async def test_scheduler_skips_unavailable_puzzle_instead_of_repeated_unavailable_notifications(monkeypatch):
    monkeypatch.setattr(game, "ensure_prepared_puzzles", AsyncMock())
    monkeypatch.setattr("app.handlers.daily_crocodile.is_daily_delivery_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        repo, "get_puzzle", AsyncMock(return_value=repo.Daily2048Puzzle(DAY, BOARD, "tile", 256, status="disabled"))
    )
    monkeypatch.setattr(handler.daily_delivery_repo, "get_due_deliveries", AsyncMock(return_value=[{"user_id": 777}]))
    monkeypatch.setattr(handler.daily_delivery_repo, "get_discovery_candidates", AsyncMock(return_value=[]))
    send = AsyncMock()
    monkeypatch.setattr(handler, "send_daily2048_entry", send)
    await handler.check_daily_2048_jobs(SimpleNamespace(bot=object()), admin_mode="2048")
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_rejects_schema_without_puzzle_revision_column():
    from app.db import schema

    async def query(sql, params=()):
        if "pg_tables" in sql:
            return [{"tablename": table} for table in schema.EXPECTED_TABLES]
        return [
            {"table_name": table, "column_name": column}
            for table, columns in schema.EXPECTED_COLUMNS.items()
            for column in columns
            if (table, column) != ("daily_2048_puzzles", "revision")
        ]

    with pytest.raises(schema.SchemaValidationError, match="daily_2048_puzzles: revision"):
        await schema.validate_schema(query)
