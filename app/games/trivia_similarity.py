"""Similarity primitives for Daily Trivia fact identities.

The pipeline follows the Crocodile judge's rule: character metrics are useful
for normalization and typo-level comparisons, but semantic decisions belong to
a semantic judge.  Callers may additionally supply embeddings for a fast
semantic shortlist.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

SemanticJudge = Callable[[str, str], Awaitable[tuple[bool, float, str]]]

_EN_TO_RU_HOMOGLYPHS = {
    "a": "а",
    "c": "с",
    "e": "е",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
    "k": "к",
    "m": "м",
    "t": "т",
    "h": "н",
    "b": "в",
}
_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_fact_text(value: str) -> str:
    """Normalize fact text using the same typo-safe ideas as Crocodile."""
    text = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
    has_cyrillic = any("а" <= char <= "я" for char in text)
    if has_cyrillic:
        text = "".join(_EN_TO_RU_HOMOGLYPHS.get(char, char) for char in text)
    return " ".join(_WORD_RE.sub(" ", text).split())


def _text_similarity(left: str, right: str) -> float:
    left_norm = normalize_fact_text(left)
    right_norm = normalize_fact_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    token_score = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    sequence_score = SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, sequence_score)


def _damerau_levenshtein(left: str, right: str) -> int:
    """Restricted Damerau-Levenshtein copied from the Crocodile judge."""
    rows, columns = len(left), len(right)
    if columns == 0:
        return rows
    if rows == 0:
        return columns

    previous_previous = [0] * (columns + 1)
    previous = list(range(columns + 1))
    current = [0] * (columns + 1)

    for row in range(1, rows + 1):
        current[0] = row
        left_char = left[row - 1]
        left_previous_char = left[row - 2] if row > 1 else None
        for column in range(1, columns + 1):
            right_char = right[column - 1]
            cost = 0 if left_char == right_char else 1
            current[column] = min(
                previous[column] + 1,
                current[column - 1] + 1,
                previous[column - 1] + cost,
            )
            if row > 1 and column > 1 and left_char == right[column - 2] and left_previous_char == right_char:
                current[column] = min(
                    current[column],
                    previous_previous[column - 2] + cost,
                )

        previous_previous, previous, current = previous, current, previous_previous

    return previous[columns]


def _allowed_edits(length: int) -> int:
    if length <= 4:
        return 0
    if length <= 7:
        return 1
    return 2


def _is_typo_equivalent(left: str, right: str) -> bool:
    left_norm = normalize_fact_text(left)
    right_norm = normalize_fact_text(right)
    if not left_norm or not right_norm:
        return False
    # A changed year, quantity, ordinal or version is usually a different fact,
    # never a harmless typo.
    if re.findall(r"\d+", left_norm) != re.findall(r"\d+", right_norm):
        return False
    return _damerau_levenshtein(left_norm, right_norm) <= _allowed_edits(max(len(left_norm), len(right_norm)))


@dataclass(frozen=True)
class FactIdentity:
    subject: str
    relation: str
    answer: str
    identity_hash: str

    @classmethod
    def create(cls, *, subject: str, relation: str, answer: str) -> FactIdentity:
        normalized_subject = normalize_fact_text(subject)
        normalized_relation = normalize_fact_text(relation)
        normalized_answer = normalize_fact_text(answer)
        payload = "\x1f".join((normalized_subject, normalized_relation, normalized_answer))
        return cls(
            subject=normalized_subject,
            relation=normalized_relation,
            answer=normalized_answer,
            identity_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    @property
    def canonical_claim(self) -> str:
        return f"{self.subject} — {self.relation}: {self.answer}"


@dataclass(frozen=True)
class SimilarityMatch:
    is_duplicate: bool
    score: float
    method: str
    reason: str = ""


async def compare_facts(
    first: FactIdentity,
    second: FactIdentity,
    *,
    first_question: str = "",
    second_question: str = "",
    semantic_judge: SemanticJudge | None = None,
) -> SimilarityMatch:
    """Compare two identities, deferring ambiguous semantics to the judge."""
    if first.identity_hash == second.identity_hash:
        return SimilarityMatch(True, 1.0, "identity_hash", "Совпадает canonical identity")

    subject_score = _text_similarity(first.subject, second.subject)
    relation_score = _text_similarity(first.relation, second.relation)
    answer_score = _text_similarity(first.answer, second.answer)
    question_score = _text_similarity(first_question, second_question)

    if subject_score == 1.0 and answer_score == 1.0:
        score = max(0.95, (subject_score + relation_score + answer_score + question_score) / 4)
        return SimilarityMatch(True, score, "subject_answer", "Совпадают предмет и canonical answer")

    if (
        _is_typo_equivalent(first.subject, second.subject)
        and _is_typo_equivalent(first.relation, second.relation)
        and _is_typo_equivalent(first.answer, second.answer)
    ):
        return SimilarityMatch(True, 0.96, "typo_identity", "Идентичность отличается только опечатками")

    score = 0.30 * subject_score + 0.30 * answer_score + 0.25 * relation_score + 0.15 * question_score

    # Character similarity is only a shortlist signal.  Treating it as a
    # semantic verdict makes numbered facts such as "объект 1" and "объект 2"
    # collide.  This intentionally follows Crocodile's judge design: local
    # metrics handle exact identity, while meaning is decided by the judge.
    ambiguous = subject_score >= 0.60 or answer_score >= 0.60 or question_score >= 0.70
    if ambiguous and semantic_judge is not None:
        duplicate, semantic_score, reason = await semantic_judge(first.canonical_claim, second.canonical_claim)
        return SimilarityMatch(duplicate, semantic_score, "semantic_judge", reason)

    return SimilarityMatch(False, score, "lexical", "Недостаточно признаков одного факта")
