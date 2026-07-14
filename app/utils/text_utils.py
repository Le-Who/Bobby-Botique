def get_part_length(part) -> int:
    """Helper to calculate length of a message part avoiding memory spikes.

    Do not use len(str(part)) for dictionaries, images, or raw bytes as it
    allocates massive strings and blocks threads.
    """
    if isinstance(part, dict):
        if "text" in part:
            return len(part["text"])
        return 0
    type_name = type(part).__name__
    if type_name in ("bytes", "bytearray", "Image", "TaggedImage"):
        return 0
    return len(str(part))
