from __future__ import annotations

import re

_NATAL_RE = re.compile(
    r"(?:натальн\w*\s+карт\w*|birth\s+chart|natal\s+chart|астрологическ\w*\s+карт\w*)",
    re.IGNORECASE,
)


def is_natal_chart_request(text: str) -> bool:
    return bool(_NATAL_RE.search(text or ""))
