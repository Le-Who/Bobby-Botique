from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculate the text length of a message part without large memory allocations."""
    if part is None:
        return 0
    if isinstance(part, str):
        return len(part)
    if isinstance(part, dict):
        if "text" in part:
            text_val = part["text"]
            if text_val is None:
                return 0
            if isinstance(text_val, str):
                return len(text_val)
            return len(str(text_val))
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0
    part_type = type(part).__name__
    if part_type in ('Image', 'TaggedImage'):
        return 0
    return len(str(part))
