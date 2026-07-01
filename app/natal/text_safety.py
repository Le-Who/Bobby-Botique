from __future__ import annotations

import re

from app.natal.models import ReportSection

_USER_FACING_BLOCKED_PATTERNS: tuple[str, ...] = (
    r"\bephem-local\b",
    r"\bequal-house\b",
    r"\breference\s+validation\b",
    r"техническ\w*\s+примечан",
    r"расчетн\w*\s+движ",
    r"расч[её]тн\w*\s+движ",
    r"ручн\w*\s+валидац",
    r"равнодомн\w*\s+систем",
    r"сетки\s+домов",
    r"угловые\s+точки",
    r"эвристическ\w*[^.\n]*(?:дом|угл|асцендент|mc)",
)

_USER_FACING_BLOCKED_RE = re.compile("|".join(_USER_FACING_BLOCKED_PATTERNS), re.IGNORECASE)


def contains_user_facing_blocked_language(value: str) -> bool:
    return bool(_USER_FACING_BLOCKED_RE.search(value))


def strip_user_facing_blocked_notes(markdown: str) -> str:
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", markdown.strip())]
    cleaned_paragraphs: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if not contains_user_facing_blocked_language(paragraph):
            cleaned_paragraphs.append(paragraph)
            continue
        cleaned = _strip_blocked_sentences(paragraph)
        if cleaned and not contains_user_facing_blocked_language(cleaned):
            cleaned_paragraphs.append(cleaned)
    return "\n\n".join(cleaned_paragraphs).strip()


def sanitize_user_facing_sections(sections: list[ReportSection]) -> list[ReportSection]:
    cleaned_sections: list[ReportSection] = []
    for section in sections:
        cleaned_body = strip_user_facing_blocked_notes(section.body_markdown)
        if cleaned_body:
            cleaned_sections.append(section.model_copy(update={"body_markdown": cleaned_body}))
    return cleaned_sections


def _strip_blocked_sentences(paragraph: str) -> str:
    cleaned_lines: list[str] = []
    for line in paragraph.splitlines():
        if not _USER_FACING_BLOCKED_RE.search(line):
            cleaned_lines.append(line.rstrip())
            continue
        sentences = re.split(r"(?<=[.!?。])\s+", line.strip())
        kept = [
            sentence.strip()
            for sentence in sentences
            if sentence.strip() and not _USER_FACING_BLOCKED_RE.search(sentence)
        ]
        if kept:
            cleaned_lines.append(" ".join(kept).strip())
    return "\n".join(line for line in cleaned_lines if line.strip()).strip()
