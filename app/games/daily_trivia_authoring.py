"""Daily Trivia authoring and publication invariants."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from app.games.trivia_similarity import FactIdentity, SemanticJudge, SimilarityMatch, compare_facts
from app.providers.router import get_provider_router
from app.repos.daily_trivia import TriviaQuestion
from app.utils.json_compat import json


class InvalidQuestionSetError(ValueError):
    pass


class DuplicateQuestionError(ValueError):
    def __init__(self, conflict: DuplicateConflict):
        self.conflict = conflict
        super().__init__(conflict.message)


class SemanticDuplicateJudgement(BaseModel):
    is_duplicate: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


class SemanticBankConflictRow(BaseModel):
    candidate_index: int = Field(ge=0)
    bank_index: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


class SemanticBankAudit(BaseModel):
    conflicts: list[SemanticBankConflictRow]


SEMANTIC_DUPLICATE_PROMPT = """Ты проверяешь банк фактов Daily Trivia.
Определи, проверяют ли два утверждения один и тот же фактический ответ, даже если вопрос,
отношение или имя сущности переформулированы. Близкая тема — не дубликат: например,
«кто изобрёл телефон» и «в каком году телефон запатентован» — разные факты.

Верни только JSON:
{"is_duplicate": true|false, "confidence": 0.0..1.0, "reason": "краткая причина"}
"""


def build_semantic_judge(*, router=None, model_name: str) -> SemanticJudge:
    """Create the semantic stage used after deterministic shortlisting."""
    provider_router = router or get_provider_router()

    async def judge(first_claim: str, second_claim: str) -> tuple[bool, float, str]:
        prompt = f"Факт A: {first_claim}\nФакт B: {second_claim}"
        response_text, _ = await provider_router.get_response(
            preferred_model=model_name,
            history=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction=SEMANTIC_DUPLICATE_PROMPT,
            timeout=30.0,
        )
        start = response_text.find("{")
        end = response_text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Semantic duplicate judge returned no JSON object")
        parsed = SemanticDuplicateJudgement.model_validate(json.loads(response_text[start : end + 1]))
        return parsed.is_duplicate, parsed.confidence, parsed.reason

    return judge


@dataclass(frozen=True)
class BankFact:
    identity: FactIdentity
    question: str
    puzzle_date: date | None = None
    lane: str = ""
    position: int = 0


@dataclass(frozen=True)
class DuplicateConflict:
    candidate: TriviaQuestion
    existing: BankFact
    match: SimilarityMatch

    @property
    def message(self) -> str:
        location = ""
        if self.existing.puzzle_date:
            lane_label = "обычные" if self.existing.lane == "main" else "супер"
            location = f" ({self.existing.puzzle_date}, {lane_label} #{self.existing.position})"
        return f"Вопрос дублирует уже использованный факт{location}: {self.match.reason}"


@dataclass(frozen=True)
class AuthoredDay:
    main: tuple[TriviaQuestion, ...]
    super_questions: tuple[TriviaQuestion, ...]


@dataclass(frozen=True)
class BankAuditConflict:
    candidate: BankFact
    existing: BankFact
    match: SimilarityMatch


SEMANTIC_BANK_AUDIT_PROMPT = """Ты проводишь строгий аудит банка Daily Trivia.
Найди пары, где кандидат проверяет тот же самый факт, что запись банка, даже если сущность,
отношение или вопрос переформулированы. Одинаковая тема без одинакового проверяемого факта
не является конфликтом. Числа, даты, люди и конкретное отношение имеют значение.

Верни только JSON вида:
{"conflicts":[{"candidate_index":0,"bank_index":3,"confidence":0.97,"reason":"..."}]}
Если конфликтов нет, верни {"conflicts":[]}.
"""


async def audit_semantic_bank(
    candidates: Iterable[BankFact],
    historical_facts: Iterable[BankFact],
    *,
    router=None,
    model_name: str,
) -> list[BankAuditConflict]:
    """Run one global semantic audit so paraphrases cannot evade lexical gates."""
    candidate_list = list(candidates)
    bank_list = list(historical_facts)
    if not candidate_list or not bank_list:
        return []
    payload = {
        "candidates": [
            {"index": index, "claim": item.identity.canonical_claim, "question": item.question}
            for index, item in enumerate(candidate_list)
        ],
        "bank": [
            {
                "index": index,
                "claim": item.identity.canonical_claim,
                "question": item.question,
                "date": item.puzzle_date.isoformat() if item.puzzle_date else None,
                "lane": item.lane,
            }
            for index, item in enumerate(bank_list)
        ],
    }
    provider_router = router or get_provider_router()
    response_text, _ = await provider_router.get_response(
        preferred_model=model_name,
        history=[{"role": "user", "parts": [{"text": json.dumps(payload)}]}],
        system_instruction=SEMANTIC_BANK_AUDIT_PROMPT,
        timeout=45.0,
    )
    start = response_text.find("{")
    end = response_text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Semantic bank audit returned no JSON object")
    parsed = SemanticBankAudit.model_validate(json.loads(response_text[start : end + 1]))
    conflicts: list[BankAuditConflict] = []
    for row in parsed.conflicts:
        if row.confidence < 0.85:
            continue
        if row.candidate_index >= len(candidate_list) or row.bank_index >= len(bank_list):
            raise ValueError("Semantic bank audit returned an invalid conflict index")
        conflicts.append(
            BankAuditConflict(
                candidate=candidate_list[row.candidate_index],
                existing=bank_list[row.bank_index],
                match=SimilarityMatch(
                    True,
                    row.confidence,
                    "semantic_bank_audit",
                    row.reason,
                ),
            )
        )
    return conflicts


def _validate_question(question: TriviaQuestion) -> None:
    if not question.question.strip():
        raise InvalidQuestionSetError("Question text is empty")
    if len(question.options) != 4 or any(not option.strip() for option in question.options):
        raise InvalidQuestionSetError("Every question must have exactly four non-empty options")
    if len({option.casefold().strip() for option in question.options}) != 4:
        raise InvalidQuestionSetError("Question options must be unique")
    if not 0 <= question.correct_index < 4:
        raise InvalidQuestionSetError("Correct answer index is outside the options")
    if question.identity is None:
        raise InvalidQuestionSetError("Question fact identity is required")


async def validate_authored_day(
    main: Iterable[TriviaQuestion],
    super_questions: Iterable[TriviaQuestion],
    *,
    historical_facts: Iterable[BankFact],
    semantic_judge: SemanticJudge | None = None,
) -> AuthoredDay:
    """Validate 5+3 questions and reject duplicates before publication."""
    main_tuple = tuple(main)
    super_tuple = tuple(super_questions)
    if len(main_tuple) != 5 or len(super_tuple) != 3:
        raise InvalidQuestionSetError("A ready day requires exactly five main and three super questions")

    accepted: list[BankFact] = list(historical_facts)
    for lane, questions in (("main", main_tuple), ("super", super_tuple)):
        for position, question in enumerate(questions, start=1):
            _validate_question(question)
            assert question.identity is not None
            for existing in accepted:
                match = await compare_facts(
                    question.identity,
                    existing.identity,
                    first_question=question.question,
                    second_question=existing.question,
                    semantic_judge=semantic_judge,
                )
                if match.is_duplicate:
                    raise DuplicateQuestionError(DuplicateConflict(question, existing, match))
            accepted.append(BankFact(question.identity, question.question, lane=lane, position=position))

    return AuthoredDay(main=main_tuple, super_questions=super_tuple)


async def publish_authored_day(
    puzzle_date: date,
    *,
    main: Iterable[TriviaQuestion],
    super_questions: Iterable[TriviaQuestion],
    model_name: str,
    expected_revision: int | None = None,
    actor: str = "scheduler",
    semantic_judge: SemanticJudge | None = None,
):
    """Validate against the shared bank and atomically publish one full day."""
    from app.repos import daily_trivia as trivia_repo

    main_tuple = tuple(main)
    super_tuple = tuple(super_questions)
    stored = await trivia_repo.get_recent_bank_facts(
        reference_date=puzzle_date,
        days=90,
        exclude_puzzle_date=puzzle_date,
    )
    bank = [
        BankFact(
            identity=item.identity,
            question=item.question,
            puzzle_date=item.puzzle_date,
            lane=item.lane,
            position=item.position,
        )
        for item in stored
    ]
    if semantic_judge is None:
        candidate_facts = [
            BankFact(
                identity=question.identity,
                question=question.question,
                lane=lane,
                position=position,
            )
            for lane, questions in (("main", main_tuple), ("super", super_tuple))
            for position, question in enumerate(questions, start=1)
            if question.identity is not None
        ]
        audit_conflicts = await audit_semantic_bank(
            candidate_facts,
            bank,
            model_name=model_name,
        )
        if audit_conflicts:
            conflict = audit_conflicts[0]
            source_questions = main_tuple if conflict.candidate.lane == "main" else super_tuple
            candidate_question = source_questions[conflict.candidate.position - 1]
            raise DuplicateQuestionError(DuplicateConflict(candidate_question, conflict.existing, conflict.match))
    validated = await validate_authored_day(
        main_tuple,
        super_tuple,
        historical_facts=bank,
        semantic_judge=semantic_judge or build_semantic_judge(model_name=model_name),
    )
    return await trivia_repo.publish_revision(
        puzzle_date,
        list(validated.main),
        list(validated.super_questions),
        expected_revision=expected_revision,
        actor=actor,
    )
