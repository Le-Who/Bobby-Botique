from typing import Any


def get_part_length(part: Any) -> int:
    """Safely calculate the text length of an LLM message part without stringifying binary formats."""
    if part is None:
        return 0

    # Skip raw binary data to prevent massive string allocation
    if isinstance(part, (bytes, bytearray)):
        return 0

    # Skip image objects via string-based class name checks to avoid NameError
    if type(part).__name__ in ('Image', 'TaggedImage'):
        return 0

    # Extract text from dictionaries explicitly
    if isinstance(part, dict):
        text = part.get("text")
        if text is not None:
            return len(str(text))
        return 0

    # Fallback for plain strings or other simple types
    return len(str(part))
