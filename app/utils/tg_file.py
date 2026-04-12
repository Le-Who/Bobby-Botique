"""Utility for transparently downloading Telegram files in cloud and local API modes.

In local_mode (Local Bot API Server): file_path is an absolute filesystem path
readable directly from the shared Docker volume — e.g.
/var/lib/telegram-bot-api/xxx/file.ogg. We read it with zero network copy.

In cloud mode (default): falls back to the standard download_as_bytearray()
network call via api.telegram.org.
"""

import logging
from pathlib import Path

from telegram import Bot, File


async def get_file_bytes(bot: Bot, tg_file: File) -> bytes:
    """Return raw bytes for a Telegram File object.

    Works transparently in both cloud and local API modes.

    Args:
        bot:     PTB Bot instance (checked for local_mode flag).
        tg_file: Resolved telegram.File (from .get_file()).

    Returns:
        Raw bytes of the file content.
    """
    if getattr(bot, "local_mode", False) and tg_file.file_path:
        local_path = Path(tg_file.file_path)
        if local_path.is_file():
            size = local_path.stat().st_size
            logging.debug("get_file_bytes: local read %s (%d bytes)", local_path, size)
            return local_path.read_bytes()
        logging.warning(
            "get_file_bytes: local path not found (%s), falling back to network download",
            local_path,
        )

    return bytes(await tg_file.download_as_bytearray())
