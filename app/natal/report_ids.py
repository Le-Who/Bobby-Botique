from __future__ import annotations

import re

_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def is_valid_report_id(value: str) -> bool:
    return bool(_REPORT_ID_RE.fullmatch(value))
