def get_part_length(part):
    if part is None:
        return 0

    part_type = type(part).__name__

    if part_type in ('bytes', 'bytearray', 'Image', 'TaggedImage'):
        return 0

    if isinstance(part, dict):
        if "text" in part:
            return len(str(part["text"]))
        return 0

    return len(str(part))
