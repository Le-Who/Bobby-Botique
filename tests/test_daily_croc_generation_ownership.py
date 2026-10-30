"""Daily content is prepared before play and stale providers cannot replace it."""

import asyncio
import copy
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.games import crocodile_daily as game
from app.games import daily_ai
from app.repos import crocodile_daily as repo
from app.web import quart_app

DAY = date(2027, 2, 5)


def install_preparation_transport(monkeypatch):
    """Keep provider/model/quota tests independent of a PostgreSQL service."""

    async def begin(day, difficulty, revision, **kwargs):
        return repo.PreparationTicket(day, difficulty, revision, uuid4())

    monkeypatch.setattr(repo, "begin_preparation", begin)
    monkeypatch.setattr(repo, "finish_preparation", AsyncMock(return_value=True))


@pytest.mark.asyncio
async def test_overview_prepares_text_before_first_result_without_requiring_image(monkeypatch):
    missing = repo.DailyPuzzle(DAY, "кот", "Животные", "ru")
    ready = replace(missing, hints=["Живёт дома", "Усатая мордочка", "Мягкие лапки"], image_prompt="")
    calls = []
    result = SimpleNamespace(status="active")

    async def prepare(*args, **kwargs):
        assert kwargs["include_image"] is False
        assert kwargs["text_only"] is True
        calls.append("prepare")
        return ready

    async def create_result(*args, **kwargs):
        calls.append("result")
        return result

    async def start(*args, **kwargs):
        return ready, await create_result()

    monkeypatch.setattr(game, "active_daily_difficulties", AsyncMock(return_value=("easy",)))
    monkeypatch.setattr(repo, "record_player_activity", AsyncMock())
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=missing))
    monkeypatch.setattr(repo, "get_or_create_result", create_result)
    monkeypatch.setattr(repo, "start_puzzle", start, raising=False)
    monkeypatch.setattr(game, "prepare_daily_puzzle", prepare)
    monkeypatch.setattr(game, "_queue_daily_puzzle_preparation_if_needed", lambda puzzle: None)
    _, puzzles, results = await game.get_daily_overview(777)
    assert calls == ["prepare", "result"]
    assert (
        puzzles["easy"].hints == ["Живёт дома", "Усатая мордочка", "Мягкие лапки"] and not puzzles["easy"].image_file_id
    )
    assert results["easy"] is result


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_empty_text_preparation_returns_retryable_readiness_without_result(monkeypatch, provider_failure):
    missing = repo.DailyPuzzle(DAY, "кот", "Животные", "ru")
    create = AsyncMock()
    monkeypatch.setattr(game, "active_daily_difficulties", AsyncMock(return_value=("easy",)))
    monkeypatch.setattr(repo, "record_player_activity", AsyncMock())
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=missing))
    monkeypatch.setattr(repo, "get_or_create_result", create)
    monkeypatch.setattr(repo, "start_puzzle", create, raising=False)
    monkeypatch.setattr(
        game,
        "prepare_daily_puzzle",
        AsyncMock(side_effect=RuntimeError("synthetic provider unavailable"))
        if provider_failure
        else AsyncMock(return_value=missing),
    )
    monkeypatch.setattr(game, "_queue_daily_puzzle_preparation_if_needed", lambda puzzle: None)
    with pytest.raises(Exception, match="not_ready") as raised:
        await game.get_daily_overview(777)
    assert raised.value.retryable is True
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_hints_returning_after_reset_cannot_write_new_revision(monkeypatch):
    old = repo.DailyPuzzle(DAY, "кот", "Животные", "ru")
    token = uuid4()
    row = {**asdict(old), "content_revision": 1, "generation_token": token}
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(*args):
        entered.set()
        await release.wait()
        return ["Старые кошачьи подсказки"]

    @asynccontextmanager
    async def transaction():
        yield

    async def query(sql, params=(), **kwargs):
        if "SET hints" in sql:
            row["hints"] = params[2]
        return [dict(row)]

    conn = SimpleNamespace(
        transaction=transaction,
        fetchrow=AsyncMock(side_effect=lambda *args: dict(row)),
        fetchval=AsyncMock(return_value=False),
    )

    @asynccontextmanager
    async def acquire():
        yield conn

    ticket = SimpleNamespace(puzzle_date=DAY, difficulty="easy", expected_revision=1, token=token)
    monkeypatch.setattr(repo.db, "db_query", query)
    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(repo, "begin_preparation", AsyncMock(return_value=ticket), raising=False)
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value="synthetic-model"))
    monkeypatch.setattr(daily_ai, "generate_daily_hints", generate)
    work = asyncio.create_task(game.get_daily_hints(old))
    await entered.wait()
    row.update(
        target_word="собака",
        hints=[],
        image_prompt="",
        image_file_id="",
        prepared_at=None,
        content_revision=2,
        generation_token=None,
    )
    release.set()
    outcome = (await asyncio.gather(work, return_exceptions=True))[0]
    assert row["hints"] == [] and row["prepared_at"] is None
    assert isinstance(outcome, Exception) and outcome.reason == "superseded"


class PreparationDatabase:
    """SQL transport only; real concurrency contracts use isolated PostgreSQL."""

    def __init__(self, puzzle):
        self.row = asdict(puzzle)
        self.played = puzzle.played
        self.writes = 0
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        try:
            yield
        finally:
            self.transactions -= 1

    async def fetchval(self, *args):
        return self.played

    async def execute(self, sql, *args):
        assert "SET generation_token=$3" in sql
        self.row["generation_token"] = args[2]

    async def fetchrow(self, sql, *args):
        if sql.startswith("SELECT"):
            return copy.deepcopy(self.row)
        assert sql.startswith("UPDATE public.crocodile_daily_puzzles")
        self.writes += 1
        if "content_revision=content_revision+1" in sql:
            self.row.update(
                content_revision=self.row["content_revision"] + 1,
                generation_token=None,
                image_file_id="",
                prepared_at=None,
            )
            if args[2] is not None:
                self.row["image_prompt"] = args[2]
            if args[3] is not None:
                self.row["image_model"] = args[3]
        else:
            for key, position in re.findall(r"(hints|image_prompt|image_file_id|image_model)=\$(\d+)", sql):
                self.row[key] = copy.deepcopy(args[int(position) - 1])
            self.row["prepared_at"] = datetime.now(UTC) if "prepared_at=NOW()" in sql else None
            if "generation_token=NULL" in sql:
                self.row["generation_token"] = None
        return copy.deepcopy(self.row)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["hints", "prompt", "asset", "prepared"])
@pytest.mark.parametrize("invalidate", ["edit", "competing_generation", "finish"])
async def test_every_generation_setter_rejects_invalidated_ticket(stage, invalidate):
    database = PreparationDatabase(
        repo.DailyPuzzle(
            DAY,
            "кот",
            "Животные",
            "ru",
            hints=["current one", "current two", "current three"],
            image_prompt="current",
            image_file_id="current",
        )
    )
    ticket = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    if invalidate == "edit":
        await repo.edit_puzzle(DAY, "easy", 1, image_prompt="new prompt", conn=database)
    elif invalidate == "competing_generation":
        await repo.begin_preparation(DAY, "easy", 1, conn=database)
    else:
        assert await repo.finish_preparation(ticket, conn=database)
    before, writes = copy.deepcopy(database.row), database.writes
    if stage == "hints":
        applied = await repo.set_puzzle_hints(ticket, ["stale"], conn=database)
    elif stage == "prompt":
        applied = await repo.set_puzzle_image_prompt(ticket, "stale", conn=database)
    elif stage == "asset":
        applied = await repo.set_puzzle_image_asset(ticket, "stale", conn=database)
    else:
        applied = await repo.mark_puzzle_prepared(ticket, conn=database)
    assert applied is False and database.row == before and database.writes == writes


@pytest.mark.asyncio
async def test_played_puzzle_rejects_manual_edits_and_late_hints_but_allows_optional_image():
    puzzle = repo.DailyPuzzle(
        DAY,
        "кот",
        "Животные",
        "ru",
        hints=["frozen one", "frozen two", "frozen three"],
        image_prompt="cat",
        played=True,
    )
    database = PreparationDatabase(puzzle)
    ticket = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    with pytest.raises(repo.DailyCrocConflict, match="puzzle_in_use"):
        await repo.edit_puzzle(DAY, "easy", 1, image_model="different", conn=database)
    assert not await repo.set_puzzle_hints(ticket, ["changed"], conn=database)
    assert not await repo.set_puzzle_image_prompt(ticket, "changed", conn=database)
    assert await repo.set_puzzle_image_asset(ticket, "optional-art", conn=database)
    assert await repo.finish_preparation(ticket, conn=database)
    assert database.row["target_word"] == "кот" and database.row["hints"] == [
        "frozen one",
        "frozen two",
        "frozen three",
    ]
    assert database.row["image_file_id"] == "optional-art"


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["asset", "clear", "finish", "finish_model", "prompt_model"])
async def test_existing_played_art_identity_cannot_be_replaced_by_current_ticket(writer):
    puzzle = repo.DailyPuzzle(
        DAY,
        "кот",
        "Животные",
        "ru",
        hints=["frozen one", "frozen two", "frozen three"],
        image_file_id="existing-played-art",
        played=True,
    )
    database = PreparationDatabase(puzzle)
    ticket = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    before, writes = copy.deepcopy(database.row), database.writes
    if writer == "asset":
        accepted = await repo.set_puzzle_image_asset(ticket, "different-art", conn=database)
    elif writer == "clear":
        accepted = await repo.clear_puzzle_image_asset(ticket, expected_file_id="existing-played-art", conn=database)
    elif writer == "finish":
        accepted = await repo.finish_preparation(ticket, {"image_file_id": "different-art"}, conn=database)
    elif writer == "finish_model":
        accepted = await repo.finish_preparation(ticket, {"image_model": "different-model"}, conn=database)
    else:
        accepted = await repo.set_puzzle_image_prompt(
            ticket, "optional cat", image_model="different-model", conn=database
        )
    assert accepted is False
    assert database.row == before and database.writes == writes


@pytest.mark.asyncio
async def test_force_image_rejects_existing_played_art_before_provider_or_ticket(monkeypatch):
    puzzle = repo.DailyPuzzle(
        DAY, "кот", "Животные", "ru", hints=["one", "two", "three"], image_file_id="existing", played=True
    )
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=puzzle))
    ticket = AsyncMock(side_effect=AssertionError("Played force must reject before ticket ownership changes"))
    provider = AsyncMock(side_effect=AssertionError("Played force must reject before provider quota"))
    monkeypatch.setattr(repo, "begin_preparation", ticket)
    monkeypatch.setattr(game, "_build_daily_image_prompt", provider)
    monkeypatch.setattr(game, "_generate_daily_image_file_id", provider)
    with pytest.raises(repo.DailyCrocConflict, match="puzzle_in_use"):
        await game.prepare_daily_puzzle(DAY, bot=object(), force_image=True, image_model=puzzle.image_model)
    assert puzzle.image_file_id == "existing"
    ticket.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_force_preflights_existing_played_art_before_bot_or_preparation(monkeypatch):
    from app.games import admin_jobs

    monkeypatch.setattr(admin_jobs.task_queue, "read_job_by_operation", AsyncMock(return_value=None))
    monkeypatch.setattr(admin_jobs, "effective_policy", AsyncMock(return_value={"models": [], "revision": 1}))
    puzzle = repo.DailyPuzzle(DAY, "кот", "Животные", "ru", image_file_id="existing", played=True)
    monkeypatch.setattr("app.web._is_authenticated", lambda: True)
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: None)
    prepare = AsyncMock(side_effect=AssertionError("Played art is already frozen"))
    monkeypatch.setattr(game, "prepare_daily_puzzle", prepare)
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["controls_csrf"] = "synthetic"
    response = await client.post(
        "/api/admin/dailycroc/regenerate",
        headers={"X-CSRF-Token": "synthetic"},
        json={
            "date": DAY.isoformat(),
            "difficulty": "easy",
            "model": puzzle.image_model,
            "expected_revision": 1,
            "operation_id": "operation-123",
        },
    )
    body = await response.get_json()
    assert response.status_code == 409 and body["error"] == "puzzle_in_use"
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_file_reference_error_preserves_existing_art_without_new_generation(monkeypatch):
    from telegram.error import BadRequest

    from app.games import crocodile_daily_telegram as telegram

    database = PreparationDatabase(
        repo.DailyPuzzle(
            DAY, "кот", "Животные", "ru", hints=["one", "two", "three"], image_file_id="existing", played=True
        )
    )

    @asynccontextmanager
    async def acquire():
        yield database

    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(side_effect=lambda *a, **kw: repo._row_to_puzzle(database.row)))
    prepare = AsyncMock(side_effect=AssertionError("Photo transport errors cannot regenerate gameplay clues"))
    monkeypatch.setattr(game, "prepare_daily_puzzle", prepare)
    before = copy.deepcopy(database.row)
    sent = await telegram._send_daily_completion_art(
        SimpleNamespace(send_photo=AsyncMock(side_effect=BadRequest("File reference expired"))), 777, DAY
    )
    assert not sent and database.row == before and database.writes == 0
    prepare.assert_not_awaited()


# Managed Daily worker revision-conflict/other-slot contract lives in test_daily_preparation.py.


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["hints", "prompt", "image"])
async def test_reset_during_each_provider_await_rejects_stale_assets_and_prepared_stamp(monkeypatch, stage):
    from app import cache
    from app.runtime_settings import lifecycle, store

    database = PreparationDatabase(repo.DailyPuzzle(DAY, "кот", "Животные", "ru"))
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def acquire():
        yield database

    async def load(*args, **kwargs):
        return repo._row_to_puzzle(copy.deepcopy(database.row))

    async def provider(which, answer):
        assert database.transactions == 0, "Provider await cannot hold a database transaction"
        if stage == which:
            entered.set()
            await release.wait()
        return answer

    monkeypatch.setattr(cache, "redis_client", None)
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=store.SettingsSnapshot(0, {})))
    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(repo, "create_puzzle_if_missing", load)
    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr(
        daily_ai,
        "generate_daily_hints",
        lambda *args: provider("hints", ["old hint one", "old hint two", "old hint three"]),
    )
    monkeypatch.setattr(game, "_build_daily_image_prompt", lambda *args, **kwargs: provider("prompt", "old prompt"))
    monkeypatch.setattr(
        game, "_generate_daily_image_file_id", lambda *args, **kwargs: provider("image", ("old image", "qwen-image"))
    )
    work = asyncio.create_task(game.prepare_daily_puzzle(DAY, bot=object()))
    await asyncio.wait_for(entered.wait(), 2)
    database.row.update(
        target_word="собака",
        hints=[],
        image_prompt="",
        image_file_id="",
        prepared_at=None,
        content_revision=2,
        generation_token=None,
    )
    reset_row, reset_writes = copy.deepcopy(database.row), database.writes
    release.set()
    with pytest.raises(repo.DailyCrocConflict, match="superseded"):
        await work
    assert database.row == reset_row and database.writes == reset_writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,fields",
    [
        ("prompt", {"prompt": "new"}),
        ("model", {"model": "qwen-image"}),
        ("reset-word", {}),
        ("regenerate", {}),
    ],
)
async def test_admin_content_mutations_require_revision(monkeypatch, path, fields):
    monkeypatch.setattr("app.web._is_authenticated", lambda: True)
    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["controls_csrf"] = "synthetic"
    response = await client.post(
        "/api/admin/dailycroc/" + path,
        headers={"X-CSRF-Token": "synthetic"},
        json={"date": DAY.isoformat(), "difficulty": "easy", **fields},
    )
    assert response.status_code == 400
    error = (await response.get_json())["error"]
    assert "expected_revision" in error or error == "invalid_daily_command"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revision_conflict", "puzzle_in_use"])
async def test_admin_edit_conflict_returns_current_revision_and_played_lock(monkeypatch, reason):
    monkeypatch.setattr("app.web._is_authenticated", lambda: True)
    current = repo.DailyPuzzle(DAY, "кот", "Животные", "ru", content_revision=5, played=True)
    monkeypatch.setattr(repo, "edit_puzzle", AsyncMock(side_effect=repo.DailyCrocConflict(reason, puzzle=current)))
    response = await quart_app.test_client().post(
        "/api/admin/dailycroc/prompt",
        json={"date": DAY.isoformat(), "difficulty": "easy", "prompt": "new", "expected_revision": 4},
    )
    assert response.status_code == 409
    body = await response.get_json()
    assert body["reason"] == reason and body["actual_revision"] == 5
    assert body["puzzle"]["content_revision"] == 5 and body["puzzle"]["played"] is True


@pytest.mark.browser
@pytest.mark.asyncio
async def test_croc_editor_cas_conflict_keeps_played_controls_locked():
    from tests.test_natal_web_report import _run_browser_regression

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    let puzzle = {date:'2027-02-05',difficulty:'easy',target_word:'кот',topic:'Животные',image_prompt:'cat',image_file_id:'',image_model:'qwen-image',content_revision:4,played:false};
    const saves=[];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/admin/dailycroc') return route.fulfill({json:{puzzles:[puzzle]}});
      if (url.pathname === '/api/admin/daily/jobs/preview') {
        saves.push(route.request().postDataJSON());
        puzzle={...puzzle,content_revision:5,played:true};
        return route.fulfill({json:{blocked_reason:'puzzle_in_use',readiness:{puzzles:[puzzle]}}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForFunction(() => document.querySelector('#croc-reset-2027-02-05-easy'));
    await page.locator('#croc-reset-2027-02-05-easy').click();
    await page.waitForFunction(() => document.querySelector('#croc-card-2027-02-05-easy .puzzle-diff').textContent.includes('r5'));
    assert.equal(await page.locator('#confirm-modal').isVisible(),false,'Blocked preview must not offer a paid enqueue');
    assert.equal(await page.locator('#croc-reset-2027-02-05-easy').isDisabled(),true);
    assert.equal(saves[0].expected_revision,4);
    assert.match(await page.locator('#croc-card-2027-02-05-easy .puzzle-diff').innerText(), /r5/i);
    assert.equal(await page.locator('#croc-model-2027-02-05-easy').isDisabled(),true);
    assert.equal(await page.locator('#croc-card-2027-02-05-easy [data-croc-action="editPrompt"]').isDisabled(),true);
    assert.equal(await page.locator('#croc-card-2027-02-05-easy [data-croc-action="savePrompt"]').isDisabled(),true);
    assert.match(await page.locator('#croc-card-2027-02-05-easy').innerText(), /новую дату/);
""",
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_played_art_editor_freezes_existing_picture_but_allows_first_fill():
    from tests.test_natal_web_report import _run_browser_regression

    client = quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True
    response = await client.get("/admin_daily")
    _run_browser_regression(
        {"html": (await response.get_data()).decode("utf-8"), "headers": dict(response.headers)},
        r"""
    const base={date:'2027-02-05',target_word:'кот',topic:'Животные',image_prompt:'cat',image_model:'qwen-image',content_revision:1,played:true};
    await page.route('**/api/**', async route => {
      if(new URL(route.request().url()).pathname==='/api/admin/dailycroc') return route.fulfill({json:{puzzles:[{...base,difficulty:'easy',image_file_id:'existing'},{...base,difficulty:'hard',image_file_id:''}]}});
      return route.fulfill({json:{}});
    });
    await page.goto('http://natal.test/admin_daily#croc');
    await page.waitForSelector('#croc-regen-2027-02-05-easy');
    assert.equal(await page.locator('#croc-regen-2027-02-05-easy').isDisabled(),true);
    assert.equal(await page.locator('#croc-regen-2027-02-05-hard').isDisabled(),false);
    assert.match(await page.locator('#croc-card-2027-02-05-easy').innerText(),/изображение заблокированы/);
""",
    )


@pytest.mark.asyncio
async def test_miniapp_reports_retryable_readiness_before_creating_state(monkeypatch):
    from urllib.parse import quote

    from tests.factories import make_valid_init_data

    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "update_user_display_name", AsyncMock())
    monkeypatch.setattr(game, "get_daily_overview", AsyncMock(side_effect=repo.DailyCrocNotReady()))
    path = "/webapp/game/daily/ws?initData=" + quote(make_valid_init_data("test-token", user_id=777))
    async with quart_app.test_client().websocket(path) as ws:
        event = await asyncio.wait_for(ws.receive_json(), 2)
    assert event["event"] == "error" and event["reason"] == "not_ready"
    assert event["retryable"] is True and "готовится" in event["message"]


@pytest.mark.asyncio
async def test_played_image_regeneration_accepts_equivalent_model_alias_without_content_edit(monkeypatch):
    puzzle = repo.DailyPuzzle(
        DAY,
        "кот",
        "Животные",
        "ru",
        hints=["hint one", "hint two", "hint three"],
        image_prompt="cat",
        image_model="vhr/gpt_image_2",
        played=True,
    )
    install_preparation_transport(monkeypatch)
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(
        repo, "edit_puzzle", AsyncMock(side_effect=AssertionError("Equivalent image model is not a content edit"))
    )
    monkeypatch.setattr(repo, "set_puzzle_image_asset", AsyncMock(return_value=True))
    monkeypatch.setattr(
        game, "_generate_daily_image_file_id", AsyncMock(return_value=("optional-image", "vhr/gpt_image_2"))
    )
    updated = await game.prepare_daily_puzzle(DAY, bot=object(), force_image=True, image_model="fta-gpt-image-2")
    assert updated.hints == ["hint one", "hint two", "hint three"] and updated.content_revision == 1
    assert updated.image_file_id == "optional-image"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "won"])
async def test_legacy_played_puzzle_without_prompt_reopens_existing_result(status):
    hints = ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
    puzzle = repo.DailyPuzzle(DAY, "кот", "Животные", "ru", hints=hints, played=True)
    result_row = {
        "user_id": 777,
        "puzzle_date": DAY,
        "difficulty": "easy",
        "status": status,
        "attempts": [{"word": "собака", "score": 0.4}],
        "best_score": 0.4,
        "used_hints_count": 1,
        "won_at": None,
        "finished_at": None,
        "points": 820,
        "share_grid": "🟨",
        "streak_after": 2,
    }

    class LegacyDatabase(PreparationDatabase):
        async def execute(self, sql, *args):
            assert "INSERT INTO public.users" in sql

        async def fetchrow(self, sql, *args):
            if sql.startswith("INSERT INTO public.crocodile_daily_results"):
                return copy.deepcopy(result_row)
            assert sql.startswith("SELECT"), "Reopening cannot prepare or replace legacy content"
            return await super().fetchrow(sql, *args)

    database = LegacyDatabase(puzzle)
    before = copy.deepcopy(database.row)
    current, result = await repo.start_puzzle(777, DAY, conn=database)
    assert asdict(result) == result_row
    assert current.hints == hints and current.image_prompt == "" and current.image_file_id == ""
    assert database.row == before


@pytest.mark.asyncio
async def test_played_missing_auto_prompt_can_be_filled_once_and_old_ticket_is_fenced():
    puzzle = repo.DailyPuzzle(
        DAY, "кот", "Животные", "ru", hints=["Один признак", "Второй признак", "Третий признак"], played=True
    )
    database = PreparationDatabase(puzzle)
    old = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    current = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    assert not await repo.set_puzzle_image_prompt(old, "stale", conn=database)
    assert await repo.set_puzzle_image_prompt(current, "optional cat", conn=database)
    assert not await repo.set_puzzle_image_prompt(current, "replacement", conn=database)
    assert not await repo.set_puzzle_hints(current, ["replace 1", "replace 2", "replace 3"], conn=database)
    assert await repo.finish_preparation(current, conn=database)
    assert database.row["image_prompt"] == "optional cat"
    assert database.row["hints"] == puzzle.hints


@pytest.mark.asyncio
async def test_text_finish_without_art_remains_partial_art_readiness(monkeypatch):
    from app.games import daily_preparation as service

    database = PreparationDatabase(
        repo.DailyPuzzle(DAY, "кот", "Животные", "ru", hints=["Один признак", "Второй признак", "Третий признак"])
    )
    ticket = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    assert await repo.finish_preparation(ticket, conn=database)

    async def get(day, *, difficulty):
        return repo._row_to_puzzle(database.row) if difficulty == "easy" else None

    monkeypatch.setattr(repo, "get_puzzle", get)
    monkeypatch.setattr(service, "_read_job", AsyncMock(return_value=None))
    readiness = await service.get_day_readiness(DAY)
    easy = readiness["puzzles"][0]
    assert easy["delivery_ready"] and easy["hints_ready"]
    assert not easy["prompt_ready"] and not easy["image_ready"] and not easy["fully_ready"]
    assert readiness["fully_ready"] is False


@pytest.mark.parametrize(
    "hints", [["one"], ["one", "two"], ["one", "one", "three"], ["one", " ", "three"], ["one", "two", "three", "four"]]
)
def test_text_readiness_requires_complete_valid_hints(hints):
    assert not repo.is_text_ready(repo.DailyPuzzle(DAY, "кот", "Животные", "ru", hints=hints, image_prompt="cat"))


@pytest.mark.asyncio
async def test_lazy_text_only_preparation_does_not_wait_for_optional_prompt_or_image(monkeypatch):
    puzzle = repo.DailyPuzzle(DAY, "кот", "Животные", "ru")
    install_preparation_transport(monkeypatch)
    monkeypatch.setattr(repo, "create_puzzle_if_missing", AsyncMock(return_value=puzzle))
    hints = ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
    monkeypatch.setattr(game, "get_daily_hints", AsyncMock(return_value=hints))
    prompt = AsyncMock(side_effect=AssertionError("Optional prompt must not delay text readiness"))
    image = AsyncMock(side_effect=AssertionError("Optional image must not delay text readiness"))
    monkeypatch.setattr(game, "_build_daily_image_prompt", prompt)
    monkeypatch.setattr(game, "_generate_daily_image_file_id", image)
    updated = await game.prepare_daily_puzzle(DAY, include_image=False, text_only=True)
    assert updated.hints == hints and updated.prepared_at is not None
    assert updated.image_prompt == updated.image_file_id == ""
    prompt.assert_not_awaited()
    image.assert_not_awaited()


class FirstStartDatabase(PreparationDatabase):
    """Keep the real atomic-start path; replace only its SQL transport."""

    def __init__(self, puzzle):
        super().__init__(puzzle)
        self.result = None

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO public.users"):
            return
        await super().execute(sql, *args)

    async def fetchrow(self, sql, *args):
        if sql.startswith("INSERT INTO public.crocodile_daily_results"):
            assert self.row["hints"] == ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
            self.played = True
            self.result = {
                "user_id": args[0],
                "puzzle_date": args[1],
                "difficulty": args[2],
                "status": "active",
                "attempts": [],
                "best_score": 0,
                "used_hints_count": 0,
                "won_at": None,
                "finished_at": None,
                "points": 0,
                "share_grid": "",
                "streak_after": 0,
            }
            return copy.deepcopy(self.result)
        return await super().fetchrow(sql, *args)


def install_start_database(monkeypatch, database):
    from app.runtime_settings import lifecycle, store

    @asynccontextmanager
    async def acquire():
        yield database

    async def load(*args, **kwargs):
        return repo._row_to_puzzle(copy.deepcopy(database.row))

    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(game, "_PREP_LOCKS", {})
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=store.SettingsSnapshot(0, {})))
    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(repo, "get_puzzle", load)
    monkeypatch.setattr(repo, "create_puzzle_if_missing", load)


@pytest.mark.asyncio
@pytest.mark.parametrize("optional_stage", ["prompt", "image"])
async def test_first_start_observes_hints_published_during_other_preparers_optional_io(monkeypatch, optional_stage):
    database = FirstStartDatabase(repo.DailyPuzzle(DAY, "кот", "Животные", "ru"))
    install_start_database(monkeypatch, database)
    monkeypatch.setattr("app.cache.redis_client", None)
    hints_entered, release_hints = asyncio.Event(), asyncio.Event()
    optional_entered, release_optional = asyncio.Event(), asyncio.Event()
    initial_missing_read = asyncio.Event()

    async def hints(*args):
        hints_entered.set()
        await release_hints.wait()
        return ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]

    async def optional(stage, value):
        if stage == optional_stage:
            optional_entered.set()
            await release_optional.wait()
        return value

    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr(daily_ai, "generate_daily_hints", hints)
    monkeypatch.setattr(game, "_build_daily_image_prompt", lambda *a, **kw: optional("prompt", "cat"))
    monkeypatch.setattr(
        game, "_generate_daily_image_file_id", lambda *a, **kw: optional("image", ("art", "qwen-image"))
    )
    full = asyncio.create_task(game.prepare_daily_puzzle(DAY, bot=object()))
    first_start = None
    try:
        await asyncio.wait_for(hints_entered.wait(), 2)
        owner_token = database.row["generation_token"]

        async def initial_load(*args, **kwargs):
            puzzle = repo._row_to_puzzle(copy.deepcopy(database.row))
            if not puzzle.hints:
                initial_missing_read.set()
            return puzzle

        monkeypatch.setattr(repo, "create_puzzle_if_missing", initial_load)
        first_start = asyncio.create_task(game._start_prepared_puzzle(777, DAY, "easy"))
        await asyncio.wait_for(initial_missing_read.wait(), 2)
        assert database.result is None
        release_hints.set()
        await asyncio.wait_for(optional_entered.wait(), 2)
        done, _ = await asyncio.wait({first_start}, timeout=1)
        assert first_start in done, "Persisted valid hints must unblock start while optional provider remains held"
        current, result = await first_start
        assert current.hints == ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
        assert result.status == "active" and database.result["user_id"] == 777
        assert database.row["generation_token"] == owner_token, "Start must not supersede the optional-art owner"
        assert not release_optional.is_set() and not full.done()
        release_optional.set()
        assert (await full).image_file_id == "art"
    finally:
        release_hints.set()
        release_optional.set()
        tasks = [task for task in (first_start, full) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not game._get_local_prep_lock(DAY, "easy").locked()


@pytest.mark.asyncio
async def test_first_start_observes_persisted_hints_without_waiting_for_redis_lease(monkeypatch):
    database = FirstStartDatabase(repo.DailyPuzzle(DAY, "кот", "Животные", "ru"))
    install_start_database(monkeypatch, database)
    lease_entered, lease_cancelled = asyncio.Event(), asyncio.Event()
    hold_lease = asyncio.Event()

    async def acquire():
        lease_entered.set()
        try:
            await hold_lease.wait()
            return False
        finally:
            lease_cancelled.set()

    monkeypatch.setattr(
        "app.cache.redis_client", SimpleNamespace(lock=lambda *a, **kw: SimpleNamespace(acquire=acquire))
    )
    first_start = asyncio.create_task(game._start_prepared_puzzle(777, DAY, "easy"))
    try:
        await asyncio.wait_for(lease_entered.wait(), 2)
        assert database.result is None
        external_ticket = uuid4()
        database.row.update(
            hints=["Домашний любимец", "Усатая мордочка", "Мягкие лапки"], generation_token=external_ticket
        )
        done, _ = await asyncio.wait({first_start}, timeout=1)
        assert first_start in done, "Ready text must not wait for the distributed preparation lease"
        current, result = await first_start
        assert current.generation_token == external_ticket and result.status == "active"
        assert database.row["generation_token"] == external_ticket
        assert lease_cancelled.is_set(), "The start call owns and must join its cancelled lease waiter"
    finally:
        first_start.cancel()
        await asyncio.gather(first_start, return_exceptions=True)
    assert not game._get_local_prep_lock(DAY, "easy").locked()


@pytest.mark.asyncio
async def test_text_only_recheck_does_not_supersede_ready_optional_art_ticket(monkeypatch):
    database = FirstStartDatabase(
        repo.DailyPuzzle(
            DAY,
            "кот",
            "Животные",
            "ru",
            hints=["Домашний любимец", "Усатая мордочка", "Мягкие лапки"],
            generation_token=uuid4(),
        )
    )
    install_start_database(monkeypatch, database)
    monkeypatch.setattr("app.cache.redis_client", None)
    before = copy.deepcopy(database.row)
    ready = await game.prepare_daily_puzzle(DAY, include_image=False, text_only=True)
    assert ready.hints == ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
    assert database.row == before and database.writes == 0


@pytest.mark.asyncio
async def test_cancelled_first_start_joins_its_waiter_without_cancelling_preparation_owner(monkeypatch):
    database = FirstStartDatabase(repo.DailyPuzzle(DAY, "кот", "Животные", "ru"))
    install_start_database(monkeypatch, database)
    monkeypatch.setattr("app.cache.redis_client", None)
    hints_entered, release_hints = asyncio.Event(), asyncio.Event()
    waiter_entered = asyncio.Event()
    original_prepare = game.prepare_daily_puzzle

    async def hints(*args):
        hints_entered.set()
        await release_hints.wait()
        return ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]

    waiters = []

    async def observe_waiter(*args, **kwargs):
        waiters.append(asyncio.current_task())
        waiter_entered.set()
        return await original_prepare(*args, **kwargs)

    monkeypatch.setattr(daily_ai, "get_daily_text_model_for", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr(daily_ai, "generate_daily_hints", hints)
    owner = asyncio.create_task(original_prepare(DAY, include_image=False, text_only=True))
    first_start = None
    try:
        await asyncio.wait_for(hints_entered.wait(), 2)
        owner_token = database.row["generation_token"]
        monkeypatch.setattr(game, "prepare_daily_puzzle", observe_waiter)
        first_start = asyncio.create_task(game._start_prepared_puzzle(777, DAY, "easy"))
        await asyncio.wait_for(waiter_entered.wait(), 2)
        first_start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_start
        assert waiters and all(waiter.done() for waiter in waiters)
        assert not owner.done() and database.result is None
        assert database.row["generation_token"] == owner_token
        release_hints.set()
        assert (await owner).hints == ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"]
    finally:
        release_hints.set()
        tasks = [task for task in (first_start, owner) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not game._get_local_prep_lock(DAY, "easy").locked()


@pytest.mark.asyncio
async def test_current_ticket_can_idempotently_keep_same_played_art_without_changing_model():
    database = PreparationDatabase(
        repo.DailyPuzzle(
            DAY, "кот", "Животные", "ru", hints=["one", "two", "three"], image_file_id="same-art", played=True
        )
    )
    ticket = await repo.begin_preparation(DAY, "easy", 1, conn=database)
    assert await repo.set_puzzle_image_asset(ticket, "same-art", conn=database)
    assert await repo.finish_preparation(ticket, {"image_file_id": "same-art"}, conn=database)
    assert database.row["image_file_id"] == "same-art" and database.row["image_model"] == repo.DAILY_IMAGE_MODEL
