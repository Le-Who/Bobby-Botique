from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculates the text length of a message part without stringifying binary data."""
    if part is None:
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0

    type_name = type(part).__name__
    if type_name in ("Image", "TaggedImage"):
        return 0

    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0

    return len(str(part))
