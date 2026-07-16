from typing import Any


def get_part_length(part: Any) -> int:
    """
    Safely compute the length of a message part without triggering
    expensive string allocations on binary or image objects.
    """
    if part is None:
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0
    part_type = type(part).__name__
    if part_type in ("Image", "TaggedImage"):
        return 0
    if isinstance(part, dict):
        text = part.get("text")
        if text:
            return len(str(text))
        return 0
    return len(str(part))
