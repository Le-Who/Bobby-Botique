from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculates the string length of a message part, bypassing expensive stringification for dicts/bytes/images."""
    if isinstance(part, str):
        return len(part)
    if isinstance(part, dict):
        return len(part.get("text", ""))
    if isinstance(part, (bytes, bytearray)):
        return 0
    if type(part).__name__ in ("Image", "TaggedImage"):
        return 0
    return len(str(part))
