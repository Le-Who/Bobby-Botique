"""Exact text entries shared by private menu routing and pending input forms."""

from __future__ import annotations

import re

NATAL_MENU_RE = re.compile(r"^\s*натальная\s*[?!.]*\s*$", re.IGNORECASE)
TAROT_MENU_RE = re.compile(r"^\s*(?:таро|расклад)\s*[?!.]*\s*$", re.IGNORECASE)
HOROSCOPE_MENU_RE = re.compile(r"^\s*гороскоп\s*[?!.]*\s*$", re.IGNORECASE)
COMPATIBILITY_ENTRY_RE = re.compile(r"^\s*(?:/?(?:совместимость|совм)|/compatibility)\s*[?!.]*\s*$", re.IGNORECASE)


def is_standalone_menu_request(text: str | None, *, bot_username: str | None = None) -> bool:
    from app.command_aliases import matches_alias_input

    for identity, pattern in (
        ("natal", NATAL_MENU_RE),
        ("tarot", TAROT_MENU_RE),
        ("horoscope_settings", HOROSCOPE_MENU_RE),
        ("compatibility", COMPATIBILITY_ENTRY_RE),
    ):
        configured = matches_alias_input(identity, text, bot_username=bot_username)
        if configured or (configured is None and pattern.fullmatch(text or "")):
            return True
    return False
