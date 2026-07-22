from typing import Any


def get_part_length(part: Any) -> int:
    """
    Safely calculates the length of a multimodal message part.
    Avoids stringifying raw bytes, bytearrays, or images to prevent memory spikes.
    """
    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0
    elif isinstance(part, (bytes, bytearray)) or type(part).__name__ in ("Image", "TaggedImage") or part is None:
        return 0
    else:
        return len(str(part))
