"""ST-09: immutable publication, result pins, rollback and score idempotence."""

import asyncio
from datetime import date

import asyncpg
import pytest

from app.games.trivia_similarity import FactIdentity
from app.repos import daily_trivia as trivia

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
DAY = date(2026, 10, 9)


def questions(prefix, count):
    return [
        trivia.TriviaQuestion(
            id=index,
            topic="Science",
            question=f"{prefix} question {index}?",
            options=[f"answer {index}", "b", "c", "d"],
            correct_index=0,
            explanation="Controlled fact",
            identity=FactIdentity.create(
                subject=f"{prefix} subject {index}", relation="answer", answer=f"answer {index}"
            ),
        )
        for index in range(1, count + 1)
    ]


async def publish(prefix, *, expected_revision=0, conn=None):
    return await trivia.publish_revision(
        DAY,
        questions(prefix + " main", 5),
        questions(prefix + " super", 3),
        expected_revision=expected_revision,
        actor=prefix,
        conn=conn,
    )


@pytest.mark.parametrize("existing", [False, True], ids=["absent-day", "existing-day"])
async def test_concurrent_publish_has_one_complete_revision(contract_pool, db_conn, existing):
    # Omitting row-lock/revision validation permits two winners or eight partial occurrences.
    if existing:
        await publish("seed")
    barrier = asyncio.Barrier(2)
    pids = set()

    async def contender(actor):
        async with contract_pool.acquire() as connection, connection.transaction():
            pids.add(await connection.fetchval("SELECT pg_backend_pid()"))
            await barrier.wait()
            return await publish(actor, expected_revision=int(existing), conn=connection)

    outcomes = await asyncio.gather(contender("left"), contender("right"), return_exceptions=True)
    assert len(pids) == 2
    winners = [result for result in outcomes if isinstance(result, trivia.DailyTriviaPuzzle)]
    assert len(winners) == 1
    assert sum(isinstance(result, trivia.RevisionConflictError) for result in outcomes) == 1
    winner = winners[0]
    assert winner.revision == int(existing) + 1
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_puzzle_revisions") == int(existing) + 1
    lanes = await db_conn.fetch(
        "SELECT lane, count(*) AS count FROM daily_trivia_question_occurrences WHERE revision_id=$1 GROUP BY lane ORDER BY lane",
        winner.published_revision_id,
    )
    assert [tuple(row) for row in lanes] == [("main", 5), ("super", 3)]
    snapshot = await trivia.get_puzzle_revision(winner.published_revision_id)
    assert [question.question for question in snapshot.questions] == [
        question.question for question in winner.questions
    ]
    assert [question.question for question in snapshot.super_questions] == [
        question.question for question in winner.super_questions
    ]
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_facts") == 8 * (int(existing) + 1)


async def test_existing_main_and_super_results_keep_original_snapshot(db_conn):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(81001)")
    first = await publish("original")
    main = await trivia.get_or_create_result(81001, DAY)
    second = await publish("replacement", expected_revision=1)
    super_result = await trivia.get_or_create_super_result(81001, DAY, puzzle_revision_id=second.published_revision_id)
    reread = await trivia.get_or_create_result(81001, DAY, puzzle_revision_id=second.published_revision_id)
    assert (
        main.puzzle_revision_id
        == reread.puzzle_revision_id
        == super_result.puzzle_revision_id
        == first.published_revision_id
    )
    old = await trivia.get_puzzle_revision(main.puzzle_revision_id)
    assert old.questions[0].question == "original main question 1?"
    assert old.super_questions[0].question == "original super question 1?"
    assert (await trivia.get_puzzle(DAY)).questions[0].question == "replacement main question 1?"


async def test_publication_failure_rolls_back_bank_and_projection(db_conn):
    # A real trigger fails after bank inserts, before final projection update.
    first = await publish("original")
    await db_conn.execute("""
        CREATE FUNCTION reject_contract_publication() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'controlled publication failure'; END $$;
        CREATE TRIGGER reject_contract_publication BEFORE UPDATE ON daily_trivia_puzzles
        FOR EACH ROW EXECUTE FUNCTION reject_contract_publication();
    """)
    with pytest.raises(asyncpg.RaiseError, match="controlled publication failure"):
        await publish("failed", expected_revision=1)
    assert (await trivia.get_puzzle(DAY)).published_revision_id == first.published_revision_id
    for table, count in [
        ("daily_trivia_facts", 8),
        ("daily_trivia_question_variants", 8),
        ("daily_trivia_puzzle_revisions", 1),
        ("daily_trivia_question_occurrences", 8),
    ]:
        assert await db_conn.fetchval(f"SELECT count(*) FROM {table}") == count


@pytest.mark.parametrize("completion_status", ["completed", "active"], ids=["explicit-status", "default-status"])
async def test_super_completion_replay_does_not_add_score_twice(db_conn, completion_status):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(81001)")
    await publish("original")
    await trivia.get_or_create_result(81001, DAY)
    await trivia.get_or_create_super_result(81001, DAY)
    await db_conn.execute("UPDATE daily_trivia_results SET final_score=100, status='completed'")
    args = {
        "delta_score": 30,
        "correct_count": 3,
        "elapsed_ms": 1000,
        "answers": [{"answer": 0}],
        "status": completion_status,
        "finished": True,
    }
    await trivia.update_super_result_answer(81001, DAY, **args)
    await trivia.update_super_result_answer(81001, DAY, **args)
    row = await db_conn.fetchrow("SELECT final_score, super_delta, super_correct FROM daily_trivia_results")
    assert tuple(row) == (130, 30, 3)


async def test_late_active_answer_cannot_reopen_completed_super_game(db_conn):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(81001)")
    await publish("original")
    await trivia.get_or_create_result(81001, DAY)
    await trivia.get_or_create_super_result(81001, DAY)
    await db_conn.execute("UPDATE daily_trivia_results SET final_score=100, status='completed'")
    completed = await trivia.update_super_result_answer(
        81001,
        DAY,
        delta_score=30,
        correct_count=3,
        elapsed_ms=1000,
        answers=[{"answer": 0}],
        status="completed",
        finished=True,
    )
    late = await trivia.update_super_result_answer(
        81001, DAY, delta_score=10, correct_count=1, elapsed_ms=10, answers=[], status="active", finished=False
    )
    assert late.status == "completed" and late.delta_score == completed.delta_score == 30
    assert late.finished_at == completed.finished_at


async def test_concurrent_super_completions_apply_only_winning_delta(db_conn):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(81001)")
    await publish("original")
    await trivia.get_or_create_result(81001, DAY)
    await trivia.get_or_create_super_result(81001, DAY)
    await db_conn.execute("UPDATE daily_trivia_results SET final_score=100, status='completed'")
    barrier = asyncio.Barrier(2)

    async def finish(delta):
        await barrier.wait()
        return await trivia.update_super_result_answer(
            81001,
            DAY,
            delta_score=delta,
            correct_count=3,
            elapsed_ms=1000,
            answers=[{"delta": delta}],
            status="completed",
            finished=True,
        )

    results = await asyncio.gather(finish(30), finish(40))
    assert results[0].delta_score == results[1].delta_score
    winner = results[0].delta_score
    assert winner in {30, 40}
    row = await db_conn.fetchrow("SELECT final_score,super_delta FROM daily_trivia_results")
    assert tuple(row) == (100 + winner, winner)


async def test_super_completion_score_failure_rolls_back_finished_transition(db_conn):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(81001)")
    await publish("original")
    await trivia.get_or_create_result(81001, DAY)
    await trivia.get_or_create_super_result(81001, DAY)
    await db_conn.execute("UPDATE daily_trivia_results SET final_score=100, status='completed'")
    await db_conn.execute("""
        CREATE FUNCTION reject_contract_score() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'controlled score failure'; END $$;
        CREATE TRIGGER reject_contract_score BEFORE UPDATE ON daily_trivia_results
        FOR EACH ROW EXECUTE FUNCTION reject_contract_score();
    """)
    with pytest.raises(asyncpg.RaiseError, match="controlled score failure"):
        await trivia.update_super_result_answer(
            81001,
            DAY,
            delta_score=30,
            correct_count=3,
            elapsed_ms=1000,
            answers=[{"answer": 0}],
            status="completed",
            finished=True,
        )
    super_row = await db_conn.fetchrow("SELECT status,delta_score,finished_at,answers FROM daily_trivia_super_results")
    assert tuple(super_row) == ("active", 0, None, [])
    assert await db_conn.fetchval("SELECT final_score FROM daily_trivia_results") == 100
