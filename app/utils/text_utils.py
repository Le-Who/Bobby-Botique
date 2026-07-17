def get_part_length(part) -> int:
    """
    Safely calculates the string length of a message part without triggering massive
    memory allocations or thread-blocking latency from str() on large objects.
    """
    if part is None:
        return 0

    if isinstance(part, dict):
        text = part.get("text")
        if text:
            return len(str(text))
        return 0

    if isinstance(part, (bytes, bytearray)):
        return 0

    type_name = type(part).__name__
    if type_name in ("Image", "TaggedImage"):
        return 0

    return len(str(part))
