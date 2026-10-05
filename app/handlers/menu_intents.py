"""Exact text entries shared by private menu routing and pending input forms."""

from __future__ import annotations

import re

NATAL_MENU_RE = re.compile(r"^\s*натальная\s*[?!.]*\s*$", re.IGNORECASE)
TAROT_MENU_RE = re.compile(r"^\s*(?:таро|расклад)\s*[?!.]*\s*$", re.IGNORECASE)
HOROSCOPE_MENU_RE = re.compile(r"^\s*гороскоп\s*[?!.]*\s*$", re.IGNORECASE)


def is_standalone_menu_request(text: str | None) -> bool:
    return any(pattern.fullmatch(text or "") for pattern in (NATAL_MENU_RE, TAROT_MENU_RE, HOROSCOPE_MENU_RE))
