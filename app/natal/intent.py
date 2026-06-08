from __future__ import annotations

import re

NATAL_INTENT_RE = re.compile(
    r"(?:натальн\w*\s+карт\w*|расч[её]т\s+наталк\w*|наталк\w*|birth\s+chart|natal\s+chart|астрологическ\w*\s+карт\w*)",
    re.IGNORECASE,
)


def is_natal_chart_request(text: str) -> bool:
    return bool(NATAL_INTENT_RE.search(text or ""))
