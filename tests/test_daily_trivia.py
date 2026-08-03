from __future__ import annotations

import asyncio
import importlib
import importlib.util
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from app.games import daily_trivia as game
from app.repos import daily_trivia as repo


def _question(question_id: int, text: str) -> repo.TriviaQuestion:
    return repo.TriviaQuestion(
        id=question_id,
        topic="Наука",
        question=text,
        options=["Верный", "Неверный 1", "Неверный 2", "Неверный 3"],
        correct_index=0,
        explanation="Объяснение",
    )


def _identified_question(question_id: int, subject: str, relation: str, answer: str) -> repo.TriviaQuestion:
    similarity = importlib.import_module("app.games.trivia_similarity")
    return repo.TriviaQuestion(
        id=question_id,
        topic="Общие знания",
        question=f"{relation}: {subject}?",
        options=[answer, "Неверный 1", "Неверный 2", "Неверный 3"],
        correct_index=0,
        explanation=f"Ответ: {answer}",
        identity=similarity.FactIdentity.create(subject=subject, relation=relation, answer=answer),
    )


async def test_save_main_questions_preserves_existing_super_questions(monkeypatch) -> None:
    puzzle_date = date(2026, 8, 3)
    main = [_question(index, f"Обычный {index}") for index in range(1, 6)]
    existing_super = [_question(index, f"Супер {index}") for index in range(1, 4)]
    captured: dict[str, object] = {}

    async def fake_db_query(sql: str, params: tuple = (), conn=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "puzzle_date": puzzle_date,
                "questions": params[1],
                "super_questions": repo.questions_to_dict_list(existing_super),
                "status": "ready",
                "prepared_at": datetime(2026, 8, 3),
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)

    saved = await repo.save_main_questions(puzzle_date, main, status="ready")

    assert saved.super_questions == existing_super
    assert "super_questions =" not in str(captured["sql"])


async def test_ensure_prepared_puzzles_generates_dates_sequentially(monkeypatch) -> None:
    active = 0
    max_active = 0
    started_dates: list[date] = []

    async def fake_prepare(puzzle_date: date):
        nonlocal active, max_active
        started_dates.append(puzzle_date)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return repo.DailyTriviaPuzzle(
            puzzle_date=puzzle_date,
            questions=[],
            super_questions=[],
            status="ready",
            prepared_at=None,
        )

    monkeypatch.setattr(game, "prepare_daily_puzzle", fake_prepare)

    puzzles = await game.ensure_prepared_puzzles(now=datetime(2026, 8, 3))

    expected_dates = [date(2026, 8, 3 + offset) for offset in range(repo.DAILY_TRIVIA_PREP_DAYS_AHEAD + 1)]
    assert started_dates == expected_dates
    assert [puzzle.puzzle_date for puzzle in puzzles] == expected_dates
    assert max_active == 1


def test_trivia_similarity_module_exists() -> None:
    assert importlib.util.find_spec("app.games.trivia_similarity") is not None


async def test_fact_identity_detects_paraphrase_across_main_and_super() -> None:
    similarity = importlib.import_module("app.games.trivia_similarity")
    first = similarity.FactIdentity.create(
        subject="Пенициллин",
        relation="первооткрыватель",
        answer="Александр Флеминг",
    )
    paraphrase = similarity.FactIdentity.create(
        subject=" пенициллин ",
        relation="Кто впервые открыл",
        answer="Александр  Флеминг",
    )

    match = await similarity.compare_facts(
        first,
        paraphrase,
        first_question="Кто открыл пенициллин?",
        second_question="Какой учёный первым обнаружил пенициллин?",
    )

    assert match.is_duplicate is True
    assert match.score >= 0.9


async def test_fact_similarity_uses_semantic_judge_only_for_ambiguous_pair() -> None:
    similarity = importlib.import_module("app.games.trivia_similarity")
    first = similarity.FactIdentity.create(
        subject="Bluetooth",
        relation="происхождение названия",
        answer="король Харальд Синезубый",
    )
    paraphrase = similarity.FactIdentity.create(
        subject="Bluetooth",
        relation="в честь кого назван",
        answer="Харальд Синезубый",
    )
    judge_calls: list[tuple[str, str]] = []

    async def semantic_judge(left: str, right: str) -> tuple[bool, float, str]:
        judge_calls.append((left, right))
        return True, 0.97, "Обе формулировки проверяют происхождение одного названия"

    match = await similarity.compare_facts(first, paraphrase, semantic_judge=semantic_judge)

    assert match.is_duplicate is True
    assert match.method == "semantic_judge"
    assert len(judge_calls) == 1


async def test_fact_similarity_keeps_related_but_distinct_facts() -> None:
    similarity = importlib.import_module("app.games.trivia_similarity")
    inventor = similarity.FactIdentity.create(
        subject="телефон",
        relation="изобретатель",
        answer="Александр Белл",
    )
    year = similarity.FactIdentity.create(
        subject="телефон",
        relation="год патента",
        answer="1876",
    )

    match = await similarity.compare_facts(inventor, year)

    assert match.is_duplicate is False


async def test_fact_similarity_uses_crocodile_typo_tolerance_but_protects_numbers() -> None:
    similarity = importlib.import_module("app.games.trivia_similarity")
    canonical = similarity.FactIdentity.create(
        subject="Пенициллин",
        relation="первооткрыватель",
        answer="Александр Флеминг",
    )
    typo = similarity.FactIdentity.create(
        subject="Пеницилин",
        relation="первооткрыватель",
        answer="Александр Флемминг",
    )
    year_1876 = similarity.FactIdentity.create(subject="телефон", relation="год патента", answer="1876")
    year_1877 = similarity.FactIdentity.create(subject="телефон", relation="год патента", answer="1877")

    typo_match = await similarity.compare_facts(canonical, typo)
    number_match = await similarity.compare_facts(year_1876, year_1877)

    assert typo_match.is_duplicate is True
    assert typo_match.method == "typo_identity"
    assert number_match.is_duplicate is False


def test_question_json_round_trip_preserves_fact_identity() -> None:
    similarity = importlib.import_module("app.games.trivia_similarity")
    identity = similarity.FactIdentity.create(
        subject="Пенициллин",
        relation="первооткрыватель",
        answer="Александр Флеминг",
    )
    question = repo.TriviaQuestion(
        id=1,
        topic="Наука",
        question="Кто открыл пенициллин?",
        options=["Александр Флеминг", "Луи Пастер", "Роберт Кох", "Эдвард Дженнер"],
        correct_index=0,
        explanation="Пенициллин открыл Александр Флеминг.",
        identity=identity,
    )

    restored = repo.normalize_questions(repo.questions_to_dict_list([question]))

    assert restored == [question]
    assert restored[0].identity.identity_hash == identity.identity_hash


def test_generated_question_builds_identity_from_structured_key() -> None:
    raw = [
        {
            "topic": "История науки",
            "question": "Кто открыл пенициллин?",
            "options": ["Александр Флеминг", "Луи Пастер", "Роберт Кох", "Эдвард Дженнер"],
            "correct_index": 0,
            "explanation": "Пенициллин открыл Александр Флеминг.",
            "key": {
                "subject": "Пенициллин",
                "relation": "первооткрыватель",
                "answer": "Александр Флеминг",
            },
        }
    ]

    question = game.shuffle_options_and_update_correct_index(raw)[0]

    assert question.identity is not None
    assert question.identity.subject == "пенициллин"
    assert question.identity.answer == "александр флеминг"


def test_daily_trivia_authoring_module_exists() -> None:
    assert importlib.util.find_spec("app.games.daily_trivia_authoring") is not None


async def test_authoring_rejects_same_fact_between_main_and_super() -> None:
    authoring_module = importlib.import_module("app.games.daily_trivia_authoring")
    main = [
        _identified_question(index, f"Объект {index}", "свойство", f"Ответ {index}")
        for index in range(1, 6)
    ]
    super_questions = [
        _identified_question(1, "Объект 1", "свойство", "Ответ 1"),
        _identified_question(2, "Супер объект 2", "свойство", "Супер ответ 2"),
        _identified_question(3, "Супер объект 3", "свойство", "Супер ответ 3"),
    ]

    with __import__("pytest").raises(authoring_module.DuplicateQuestionError):
        await authoring_module.validate_authored_day(main, super_questions, historical_facts=[])


async def test_authoring_accepts_complete_unique_day() -> None:
    authoring_module = importlib.import_module("app.games.daily_trivia_authoring")
    main = [
        _identified_question(index, f"Объект {index}", "свойство", f"Ответ {index}")
        for index in range(1, 6)
    ]
    super_questions = [
        _identified_question(index, f"Супер объект {index}", "свойство", f"Супер ответ {index}")
        for index in range(1, 4)
    ]

    validated = await authoring_module.validate_authored_day(main, super_questions, historical_facts=[])

    assert validated.main == tuple(main)
    assert validated.super_questions == tuple(super_questions)


async def test_semantic_duplicate_judge_parses_strict_json() -> None:
    authoring_module = importlib.import_module("app.games.daily_trivia_authoring")
    router = AsyncMock()
    router.get_response.return_value = (
        '{"is_duplicate": true, "confidence": 0.98, '
        '"reason": "Один и тот же факт о происхождении названия"}',
        None,
    )

    judge = authoring_module.build_semantic_judge(router=router, model_name="test-model")
    result = await judge(
        "bluetooth — происхождение названия: харальд синезубый",
        "bluetooth — в честь кого назван: король харальд синезубый",
    )

    assert result == (True, 0.98, "Один и тот же факт о происхождении названия")
    assert router.get_response.await_count == 1


def test_puzzle_serialization_exposes_revision_and_lane_readiness() -> None:
    puzzle = repo.DailyTriviaPuzzle(
        puzzle_date=date(2026, 8, 3),
        questions=[_question(index, f"Обычный {index}") for index in range(1, 6)],
        super_questions=[_question(index, f"Супер {index}") for index in range(1, 3)],
        status="draft",
        prepared_at=None,
        revision=4,
        published_revision_id=91,
    )

    payload = __import__("app.web", fromlist=["_serialize_daily_trivia_puzzle"])._serialize_daily_trivia_puzzle(puzzle)

    assert payload["revision"] == 4
    assert payload["published_revision_id"] == 91
    assert payload["readiness"] == {
        "main": {"count": 5, "required": 5, "ready": True},
        "super": {"count": 2, "required": 3, "ready": False},
        "publishable": False,
    }


async def test_new_result_is_pinned_to_published_revision(monkeypatch) -> None:
    queries: list[str] = []

    async def fake_db_query(sql: str, params: tuple = (), conn=None):
        queries.append(sql)
        if sql.lstrip().startswith("SELECT"):
            return []
        return [
            {
                "user_id": 7,
                "puzzle_date": date(2026, 8, 3),
                "status": "active",
                "current_question": 0,
                "correct_count": 0,
                "final_score": 0,
                "elapsed_ms": 0,
                "answers": [],
                "started_at": datetime(2026, 8, 3),
                "finished_at": None,
                "super_delta": None,
                "super_correct": None,
                "puzzle_revision_id": 91,
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)

    result = await repo.get_or_create_result(7, date(2026, 8, 3))

    insert_sql = next(sql for sql in queries if sql.lstrip().startswith("INSERT"))
    assert "published_revision_id" in insert_sql
    assert result.puzzle_revision_id == 91


async def test_publish_revision_rejects_stale_admin_edit() -> None:
    class FakeConnection:
        async def fetchrow(self, sql: str, *args):
            if "FOR UPDATE" in sql:
                return {"revision": 4}
            raise AssertionError("A stale edit must fail before any write")

    with __import__("pytest").raises(repo.RevisionConflictError):
        await repo._publish_revision_on_conn(
            FakeConnection(),
            date(2026, 8, 3),
            [_identified_question(index, f"Объект {index}", "свойство", f"Ответ {index}") for index in range(1, 6)],
            [
                _identified_question(index, f"Супер объект {index}", "свойство", f"Супер ответ {index}")
                for index in range(1, 4)
            ],
            expected_revision=3,
            actor="admin",
        )


async def test_load_bank_facts_reads_main_and_super_occurrences(monkeypatch) -> None:
    async def fake_db_query(sql: str, params: tuple = (), conn=None):
        assert "daily_trivia_question_occurrences" in sql
        return [
            {
                "subject_norm": "пенициллин",
                "relation_norm": "первооткрыватель",
                "answer_norm": "александр флеминг",
                "identity_hash": "hash-1",
                "question": "Кто открыл пенициллин?",
                "puzzle_date": date(2026, 7, 20),
                "lane": "super",
                "position": 2,
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)

    facts = await repo.get_recent_bank_facts(reference_date=date(2026, 8, 3), days=90)

    assert len(facts) == 1
    assert facts[0].identity.identity_hash == "hash-1"
    assert facts[0].lane == "super"
    assert facts[0].puzzle_date == date(2026, 7, 20)


async def test_main_regeneration_publishes_with_untouched_super_lane(monkeypatch) -> None:
    authoring_module = importlib.import_module("app.games.daily_trivia_authoring")
    existing_main = [
        _identified_question(index, f"Старый объект {index}", "свойство", f"Старый ответ {index}")
        for index in range(1, 6)
    ]
    existing_super = [
        _identified_question(index, f"Супер объект {index}", "свойство", f"Супер ответ {index}")
        for index in range(1, 4)
    ]
    generated_main = [
        _identified_question(index, f"Новый объект {index}", "свойство", f"Новый ответ {index}")
        for index in range(1, 6)
    ]
    existing = repo.DailyTriviaPuzzle(
        puzzle_date=date(2026, 8, 3),
        questions=existing_main,
        super_questions=existing_super,
        status="ready",
        prepared_at=None,
        revision=7,
        published_revision_id=70,
    )
    published = repo.DailyTriviaPuzzle(
        puzzle_date=existing.puzzle_date,
        questions=generated_main,
        super_questions=existing_super,
        status="ready",
        prepared_at=None,
        revision=8,
        published_revision_id=71,
    )
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=existing))
    generate_lane = AsyncMock(return_value=generated_main)
    monkeypatch.setattr(game, "generate_question_lane", generate_lane)
    publish = AsyncMock(return_value=published)
    monkeypatch.setattr(authoring_module, "publish_authored_day", publish)
    monkeypatch.setattr(
        __import__("app.repos.settings_repo", fromlist=["get_global_setting"]),
        "get_global_setting",
        AsyncMock(return_value="test-model"),
    )

    result = await game.prepare_daily_puzzle(existing.puzzle_date, force=True, mode="main")

    assert result == published
    generate_lane.assert_awaited_once()
    assert generate_lane.await_args.kwargs["lane"] == "main"
    publish.assert_awaited_once()
    assert publish.await_args.kwargs["super_questions"] == existing_super
    assert publish.await_args.kwargs["expected_revision"] == 7


def test_admin_question_parser_keeps_structured_fact_identity() -> None:
    web_module = __import__("app.web", fromlist=["_parse_admin_trivia_question"])
    parsed = web_module._parse_admin_trivia_question(
        {
            "id": 1,
            "topic": "История науки",
            "question": "Кто открыл пенициллин?",
            "options": ["Флеминг", "Пастер", "Кох", "Дженнер"],
            "correct_index": 0,
            "explanation": "Это сделал Александр Флеминг.",
            "identity": {
                "subject": "Пенициллин",
                "relation": "первооткрыватель",
                "answer": "устаревшее значение из формы",
            },
        },
        0,
    )

    assert parsed.identity is not None
    assert parsed.identity.subject == "пенициллин"
    assert parsed.identity.relation == "первооткрыватель"
    assert parsed.identity.answer == "флеминг"


async def test_get_puzzle_revision_reconstructs_the_immutable_snapshot(monkeypatch) -> None:
    identity = {
        "subject_norm": "пенициллин",
        "relation_norm": "первооткрыватель",
        "answer_norm": "александр флеминг",
        "identity_hash": "fact-hash",
    }

    async def fake_db_query(sql: str, params: tuple = (), conn=None):
        assert params == (91,)
        return [
            {
                **identity,
                "puzzle_date": date(2026, 8, 3),
                "revision_no": 4,
                "revision_id": 91,
                "status": "ready",
                "published_at": datetime(2026, 8, 3),
                "lane": "main",
                "position": 1,
                "topic": "Наука",
                "question": "Кто открыл пенициллин?",
                "options": ["Флеминг", "Пастер", "Кох", "Дженнер"],
                "correct_index": 0,
                "explanation": "Флеминг.",
            },
            {
                **identity,
                "puzzle_date": date(2026, 8, 3),
                "revision_no": 4,
                "revision_id": 91,
                "status": "ready",
                "published_at": datetime(2026, 8, 3),
                "lane": "super",
                "position": 1,
                "topic": "Наука",
                "question": "Сложная формулировка?",
                "options": ["Флеминг", "Пастер", "Кох", "Дженнер"],
                "correct_index": 0,
                "explanation": "Флеминг.",
            },
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)

    puzzle = await repo.get_puzzle_revision(91)

    assert puzzle is not None
    assert puzzle.revision == 4
    assert puzzle.published_revision_id == 91
    assert len(puzzle.questions) == 1
    assert len(puzzle.super_questions) == 1
    assert puzzle.questions[0].identity.identity_hash == "fact-hash"


async def test_semantic_bank_audit_catches_nonlexical_paraphrase() -> None:
    authoring_module = importlib.import_module("app.games.daily_trivia_authoring")
    candidate = authoring_module.BankFact(
        identity=__import__("app.games.trivia_similarity", fromlist=["FactIdentity"]).FactIdentity.create(
            subject="лекарство, полученное из плесени",
            relation="имя учёного, обнаружившего антибактериальный эффект",
            answer="Александр Флеминг",
        ),
        question="Какой исследователь заметил действие плесени на бактерии?",
        lane="main",
        position=1,
    )
    historical = authoring_module.BankFact(
        identity=__import__("app.games.trivia_similarity", fromlist=["FactIdentity"]).FactIdentity.create(
            subject="пенициллин",
            relation="первооткрыватель",
            answer="Александр Флеминг",
        ),
        question="Кто открыл пенициллин?",
        puzzle_date=date(2026, 7, 1),
        lane="super",
        position=2,
    )
    router = AsyncMock()
    router.get_response.return_value = (
        '{"conflicts":[{"candidate_index":0,"bank_index":0,"confidence":0.99,'
        '"reason":"Обе формулировки спрашивают об открытии пенициллина"}]}',
        None,
    )

    conflicts = await authoring_module.audit_semantic_bank(
        [candidate],
        [historical],
        router=router,
        model_name="test-model",
    )

    assert len(conflicts) == 1
    assert conflicts[0].existing == historical
    assert conflicts[0].match.method == "semantic_bank_audit"


def test_cutover_migration_preserves_every_result_before_today() -> None:
    migration = (
        Path(__file__).parents[1] / "scripts" / "migrations" / "066_reset_daily_trivia_from_2026_08_03.sql"
    ).read_text(encoding="utf-8")

    assert "cutoff_date CONSTANT DATE := DATE '2026-08-03'" in migration
    assert "DELETE FROM public.daily_trivia_results\n    WHERE puzzle_date >= cutoff_date" in migration
    assert "DELETE FROM public.daily_trivia_super_results\n    WHERE puzzle_date >= cutoff_date" in migration
    assert "WHERE puzzle_date < cutoff_date" not in migration
    assert "WHERE puzzle_date <= cutoff_date" not in migration


def test_question_bank_migration_unwraps_and_guards_legacy_json_scalars() -> None:
    migration = (
        Path(__file__).parents[1] / "scripts" / "migrations" / "065_daily_trivia_question_bank.sql"
    ).read_text(encoding="utf-8")

    first_array_expansion = migration.index("jsonb_array_elements")
    questions_repair = migration.index("SET questions = (questions #>> '{}')::jsonb")
    super_repair = migration.index("SET super_questions = (super_questions #>> '{}')::jsonb")

    assert questions_repair < first_array_expansion
    assert super_repair < first_array_expansion
    assert migration.count(
        "CASE WHEN jsonb_typeof(p.questions) = 'array' THEN p.questions ELSE '[]'::jsonb END"
    ) == 3
    assert migration.count(
        "CASE WHEN jsonb_typeof(p.super_questions) = 'array' THEN p.super_questions ELSE '[]'::jsonb END"
    ) == 3
