from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculate the string length of a message part.
    Avoids using len(str()) on large objects like bytes or dictionaries.
    """
    if part is None:
        return 0
    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0
    if type(part).__name__ in ("Image", "TaggedImage"):
        return 0
    return len(str(part))
