def get_part_length(part) -> int:
    """Safely calculate length of a message part avoiding stringification of binary/image formats."""
    if isinstance(part, (bytes, bytearray)):
        return 0

    part_type = type(part).__name__
    if part_type in ("Image", "TaggedImage"):
        return 0

    if isinstance(part, dict):
        text = part.get("text")
        if text and isinstance(text, str):
            return len(text)
        return 0

    if isinstance(part, str):
        return len(part)

    return 0
