from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculate length of message parts, avoiding str() on binaries/dicts."""
    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0

    if isinstance(part, (bytes, bytearray)):
        return 0

    part_type = type(part).__name__
    if part_type in ("Image", "TaggedImage"):
        return 0

    return len(str(part))
