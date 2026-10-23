"""Server-owned trivia scoring and ordered submissions at the database boundary."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import web_miniapp
from app.repos import daily_trivia as repo
from app.web import quart_app
from tests.factories import make_valid_init_data

DAY = date(2027, 1, 1)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        await self.connection.lock.acquire()
        self.connection.in_transaction = True

    async def __aexit__(self, *args):
        self.connection.in_transaction = False
        self.connection.lock.release()


class _Connection:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.in_transaction = False
        self.rows = {
            "main": {
                "user_id": 777,
                "puzzle_date": DAY,
                "status": "active",
                "current_question": 0,
                "correct_count": 0,
                "final_score": 0,
                "elapsed_ms": 0,
                "answers": [],
                "started_at": datetime.now(UTC),
                "finished_at": None,
                "puzzle_revision_id": 91,
            },
            "super": {
                "user_id": 777,
                "puzzle_date": DAY,
                "status": "active",
                "correct_count": 0,
                "delta_score": 0,
                "elapsed_ms": 0,
                "answers": [],
                "started_at": datetime.now(UTC),
                "finished_at": None,
                "puzzle_revision_id": 91,
            },
        }
        self.fail_score = False

    def transaction(self):
        return _Transaction(self)

    async def fetchrow(self, sql, *args):
        assert self.in_transaction, "submission queries must share the transaction"
        lane = "super" if "daily_trivia_super_results" in sql else "main"
        row = self.rows[lane]
        if sql.lstrip().startswith("SELECT"):
            assert "FOR UPDATE" in sql
            return deepcopy(row)
        assert sql.lstrip().startswith("UPDATE")
        if lane == "main" and "super_delta" in sql:
            if self.fail_score:
                raise RuntimeError("controlled score update failure")
            row.update(final_score=max(0, row["final_score"] + args[2]), super_delta=args[2], super_correct=args[3])
        else:
            if lane == "main":
                row.update(current_question=args[2], correct_count=args[3], final_score=args[4], elapsed_ms=args[5])
                answers, status, finished = args[6:9]
            else:
                row.update(delta_score=args[2], correct_count=args[3], elapsed_ms=args[4])
                answers, status, finished = args[5:8]
            # Match the production pool's JSONB codec: it serializes the Python
            # list once, rather than accepting an already encoded JSON string.
            assert isinstance(answers, list)
            row.update(answers=deepcopy(answers), status=status)
            if row.get("puzzle_revision_id") is None:
                row["puzzle_revision_id"] = args[8] if lane == "super" else args[9]
            if finished:
                row["finished_at"] = datetime.now(UTC)
        return deepcopy(row)


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def submission_store(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(repo.db.db_manager, "pool", SimpleNamespace(acquire=lambda: _Acquire(connection)))

    async def get_main(*args, conn=None, **kwargs):
        assert conn is connection and connection.in_transaction
        return repo._row_to_result(connection.rows["main"])

    async def get_super(*args, conn=None, **kwargs):
        assert conn is connection and connection.in_transaction
        return repo._row_to_super_result(connection.rows["super"])

    def question(index):
        return repo.TriviaQuestion(index, "topic", f"Question {index}", ["wrong", "right"], 1, "Explanation")

    async def pinned(revision_id, *, conn=None):
        assert revision_id == 91 and conn is connection
        return repo.DailyTriviaPuzzle(DAY, [question(0), question(1)], [question(0), question(1)], "ready", None, 1, 91)

    monkeypatch.setattr(repo, "get_or_create_result", get_main)
    monkeypatch.setattr(repo, "get_or_create_super_result", get_super)
    monkeypatch.setattr(repo, "get_puzzle_revision", pinned)
    return connection


@pytest.mark.asyncio
async def test_main_answers_use_pinned_correctness_score_and_actual_question_count(submission_store):
    first = await repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=7500)
    assert first.result.final_score == 250
    assert first.result.correct_count == 1
    assert first.result.status == "active"
    assert first.applied is True
    last = await repo.submit_answer(777, DAY, question_index=1, selected_index=0, elapsed_ms=16000)
    assert last.result.status == "completed"
    assert last.result.final_score == 250
    assert last.result.current_question == 2
    assert last.result.elapsed_ms == 23500
    assert [answer["is_correct"] for answer in last.result.answers] == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elapsed_ms", "expected_score"),
    [(0, 300), (1000, 293), (6300, 257), (6450, 256), (7500, 250), (10650, 228), (14999, 200), (15000, 200)],
)
async def test_score_matches_the_existing_browser_bonus_at_rounding_boundaries(
    submission_store, elapsed_ms, expected_score
):
    accepted = await repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=elapsed_ms)
    assert accepted.result.final_score == expected_score


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_main_answers_never_double_count_or_reopen(submission_store):
    replies = await asyncio.gather(
        *(repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=1000) for _ in range(2))
    )
    assert sorted(reply.applied for reply in replies) == [False, True]
    assert submission_store.rows["main"]["correct_count"] == 1
    assert len(submission_store.rows["main"]["answers"]) == 1
    await repo.submit_answer(777, DAY, question_index=1, selected_index=1, elapsed_ms=1000)
    final = deepcopy(submission_store.rows["main"])
    late = await repo.submit_answer(777, DAY, question_index=0, selected_index=0, elapsed_ms=1)
    assert late.already_completed is True
    assert late.applied is False
    assert submission_store.rows["main"] == final


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"question_index": 1},
        {"question_index": -1},
        {"question_index": 2},
        {"selected_index": -1},
        {"selected_index": 2},
        {"elapsed_ms": -1},
        {"elapsed_ms": 86400001},
        {"question_index": True},
        {"elapsed_ms": "10"},
        {"puzzle_revision_id": 92},
    ],
)
async def test_invalid_or_out_of_order_answer_never_changes_progress(submission_store, changes):
    original = deepcopy(submission_store.rows)
    arguments = {"question_index": 0, "selected_index": 1, "elapsed_ms": 1000, **changes}
    with pytest.raises(ValueError):
        await repo.submit_answer(777, DAY, **arguments)
    assert submission_store.rows == original


@pytest.mark.asyncio
async def test_super_scoring_uses_question_answers_and_applies_total_delta_once(submission_store):
    submission_store.rows["main"].update(status="completed", final_score=250)
    first = await repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=7500, is_super=True)
    assert first.result.delta_score == 500
    assert submission_store.rows["main"]["final_score"] == 250
    last = await repo.submit_answer(777, DAY, question_index=1, selected_index=0, elapsed_ms=16000, is_super=True)
    assert last.result.status == "completed"
    assert last.result.delta_score == 100
    assert submission_store.rows["main"]["final_score"] == 350
    duplicate = await repo.submit_answer(777, DAY, question_index=1, selected_index=0, elapsed_ms=16000, is_super=True)
    assert duplicate.already_completed is True
    assert submission_store.rows["main"]["final_score"] == 350


@pytest.mark.asyncio
async def test_super_requires_completed_main_before_any_result_write(submission_store):
    original = deepcopy(submission_store.rows)
    with pytest.raises(ValueError):
        await repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=1, is_super=True)
    assert submission_store.rows == original


@pytest.mark.asyncio
async def test_signed_route_ignores_forged_score_and_correctness(submission_store, monkeypatch):
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="fake-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.today_puzzle_date", lambda: DAY)
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: None)
    response = await quart_app.test_client().post(
        "/webapp/api/miniapp/trivia/submit_answer",
        headers={"X-TG-INIT-DATA": make_valid_init_data("fake-token", user_id=777)},
        json={"question_index": 0, "selected_index": 0, "elapsed_ms": 1000, "is_correct": True, "total_score": 1000000},
    )
    assert response.status_code == 200
    assert (await response.get_json())["success"] is True
    assert submission_store.rows["main"]["final_score"] == 0
    assert submission_store.rows["main"]["correct_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[1], {"question_index": "bad"}, {"question_index": True}])
async def test_trivia_auth_precedes_malformed_input_validation(monkeypatch, payload):
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="fake-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    response = await quart_app.test_client().post(
        "/webapp/api/miniapp/trivia/submit_answer",
        headers={"X-TG-INIT-DATA": make_valid_init_data("fake-token", user_id=777)},
        json=payload,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["submit_answer", "submit_super_answer"])
@pytest.mark.parametrize(
    "payload",
    [
        [1],
        {},
        {"question_index": 0, "selected_index": True, "elapsed_ms": 10},
        {"question_index": 0, "selected_index": 0, "elapsed_ms": -1},
    ],
)
async def test_authorized_malformed_submissions_are_rejected_before_database_work(monkeypatch, payload, path):
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="fake-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    submit = AsyncMock(side_effect=AssertionError("Malformed input must not reach result persistence"))
    monkeypatch.setattr(repo, "submit_answer", submit)
    response = await quart_app.test_client().post(
        f"/webapp/api/miniapp/trivia/{path}",
        headers={"X-TG-INIT-DATA": make_valid_init_data("fake-token", user_id=777)},
        json=payload,
    )
    assert response.status_code == 400
    assert submit.await_count == 0


@pytest.mark.asyncio
async def test_completion_delivery_occurs_after_commit_and_not_on_replay(submission_store, monkeypatch):
    submission_store.rows["main"].update(
        current_question=1, correct_count=1, final_score=250, answers=[{"question_index": 0, "selected_index": 1}]
    )
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="fake-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.today_puzzle_date", lambda: DAY)
    bot = object()
    monkeypatch.setattr(web_miniapp, "get_bot", lambda: bot)
    deliveries = []

    async def delivered(actual_bot, user_id, puzzle_date):
        assert not submission_store.in_transaction
        deliveries.append((actual_bot, user_id, puzzle_date))

    monkeypatch.setattr("app.games.daily_trivia_telegram.send_trivia_result_message", delivered)
    client = quart_app.test_client()
    kwargs = {
        "headers": {"X-TG-INIT-DATA": make_valid_init_data("fake-token", user_id=777)},
        "json": {"question_index": 1, "selected_index": 1, "elapsed_ms": 7500},
    }
    first = await client.post("/webapp/api/miniapp/trivia/submit_answer", **kwargs)
    replay = await client.post("/webapp/api/miniapp/trivia/submit_answer", **kwargs)
    assert first.status_code == replay.status_code == 200
    assert (await replay.get_json())["already_completed"] is True
    assert deliveries == [(bot, 777, DAY)]


@pytest.mark.asyncio
async def test_today_exposes_active_progress_from_the_pinned_snapshot(submission_store, monkeypatch):
    result = await repo.submit_answer(777, DAY, question_index=0, selected_index=1, elapsed_ms=7500)
    pinned = await repo.get_puzzle_revision(91, conn=submission_store)
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="fake-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.today_puzzle_date", lambda: DAY)
    monkeypatch.setattr(repo, "get_result_if_exists", AsyncMock(return_value=result.result))
    monkeypatch.setattr(repo, "get_super_result_if_exists", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "get_puzzle_revision", AsyncMock(return_value=pinned))
    prepare = AsyncMock(side_effect=AssertionError("Do not replace an existing pinned snapshot"))
    monkeypatch.setattr("app.games.daily_trivia.prepare_daily_puzzle", prepare)
    response = await quart_app.test_client().get(
        "/webapp/api/miniapp/trivia/today", headers={"X-TG-INIT-DATA": make_valid_init_data("fake-token", user_id=777)}
    )
    payload = await response.get_json()
    assert response.status_code == 200
    assert payload["revision_id"] == 91
    assert payload["questions"][0]["question"] == "Question 0"
    assert payload["user_result"]["status"] == "active"
    assert payload["user_result"]["current_question"] == 1
    assert payload["user_result"]["final_score"] == 250
    assert prepare.await_count == 0


@pytest.mark.asyncio
async def test_new_player_today_keeps_preparing_existing_draft_puzzles(submission_store, monkeypatch):
    ready = await repo.get_puzzle_revision(91, conn=submission_store)
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=repo.DailyTriviaPuzzle(DAY, [], [], "draft", None)))
    prepare = AsyncMock(return_value=ready)
    monkeypatch.setattr("app.games.daily_trivia.prepare_daily_puzzle", prepare)
    response = await quart_app.test_client().get("/webapp/api/miniapp/trivia/today")
    assert response.status_code == 200
    assert len((await response.get_json())["questions"]) == 2
    assert prepare.await_count == 1


@pytest.mark.asyncio
async def test_legacy_main_result_starts_super_on_the_same_immutable_day(submission_store, monkeypatch):
    submission_store.rows["main"].update(status="completed", puzzle_revision_id=None)
    submission_store.rows["super"]["puzzle_revision_id"] = None
    monkeypatch.setattr(repo.db, "db_query", AsyncMock(return_value=[{"revision_id": 91}]))

    async def create_super(*args, puzzle_revision_id=None, conn=None):
        submission_store.rows["super"]["puzzle_revision_id"] = puzzle_revision_id or 92
        return repo._row_to_super_result(submission_store.rows["super"])

    monkeypatch.setattr(repo, "get_or_create_super_result", create_super)
    accepted = await repo.submit_answer(
        777, DAY, question_index=0, selected_index=1, elapsed_ms=7500, puzzle_revision_id=91, is_super=True
    )
    assert accepted.result.puzzle_revision_id == 91
    assert accepted.result.delta_score == 500
