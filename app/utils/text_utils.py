from typing import Any


def get_part_length(part: Any) -> int:
    """
    Calculate the string length of an LLM message part without triggering
    massive memory allocations for binary data or images.
    """
    if part is None:
        return 0
    if isinstance(part, str):
        return len(part)
    if isinstance(part, dict):
        if "text" in part and isinstance(part["text"], str):
            return len(part["text"])
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0

    type_name = type(part).__name__
    if type_name in ("Image", "TaggedImage"):
        return 0

    return len(str(part))
