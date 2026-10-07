from __future__ import annotations

import re

from app.handlers.menu_intents import NATAL_MENU_RE

NATAL_SLASH_ALIAS_RE = re.compile(
    r"^/(?:натальн\w*|карта)(?:@\w+)?(?:\s|$)",
    re.IGNORECASE,
)

NATAL_INTENT_RE = re.compile(
    r"(?:"
    r"/(?:natal|натальн\w*|карта)(?:@\w+)?\b"
    r"|натальн\w*\s+карт\w*"
    r"|карт\w*\s+рожден\w*"
    r"|расч[её]т\s+наталк\w*"
    r"|наталк\w*"
    r"|birth\s+chart"
    r"|natal\s+chart"
    r"|астрологическ\w*\s+карт\w*"
    rf"|{NATAL_MENU_RE.pattern}"
    r")",
    re.IGNORECASE,
)


def is_natal_chart_request(text: str, *, bot_username: str | None = None) -> bool:
    from app.command_aliases import matches_alias_input

    configured = matches_alias_input("natal", text, bot_username=bot_username)
    if configured is not None:
        return configured
    return bool(NATAL_INTENT_RE.search(text or ""))
