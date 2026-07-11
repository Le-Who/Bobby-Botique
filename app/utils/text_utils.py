from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculate the length of a message part avoiding memory spikes from binary data."""
    if part is None:
        return 0
    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0
    if isinstance(part, (bytes, bytearray)):
        return 0
    if type(part).__name__ in ('Image', 'TaggedImage'):
        return 0
    return len(str(part))
