"""Utility for transparently downloading Telegram files in cloud and local API modes.

In local_mode (Local Bot API Server): file_path is an absolute filesystem path
readable directly from the shared Docker volume — e.g.
/var/lib/telegram-bot-api/xxx/file.ogg. We read it with zero network copy.

In cloud mode (default): falls back to the standard download_as_bytearray()
network call via api.telegram.org.
"""

import logging
from pathlib import Path, PurePosixPath

from telegram import Bot, File

MAX_TELEGRAM_FILE_BYTES = 20 * 1024 * 1024


def _extract_local_path(file_path: str) -> str | None:
    """Extract a local filesystem path from a file_path value.

    The Local Bot API Server always stores files under /var/lib/telegram-bot-api/.
    PTB may return either:
      - A raw local path: /var/lib/telegram-bot-api/.../file.ogg
      - A URL with the local path embedded: http://tg-api:8081/file/bot.../var/lib/...
    This helper handles both cases.
    """
    marker = "/var/lib/telegram-bot-api/"
    idx = file_path.find(marker)
    if idx != -1:
        candidate = file_path[idx:]
        if ".." in PurePosixPath(candidate).parts:
            return None
        return candidate
    return None


async def get_file_bytes(bot: Bot, tg_file: File) -> bytes:
    """Return raw bytes for a Telegram File object.

    Works transparently in both cloud and local API modes.

    Priority order:
      1. local_mode + shared volume mounted  → read from disk (zero-copy)
      2. local_mode + volume NOT mounted     → fetch via HTTP from local Bot API server
      3. cloud mode                          → download via api.telegram.org

    Args:
        bot:     PTB Bot instance (checked for local_mode flag).
        tg_file: Resolved telegram.File (from .get_file()).

    Returns:
        Raw bytes of the file content.
    """
    if getattr(bot, "local_mode", False) and tg_file.file_path:
        local_path_str = _extract_local_path(tg_file.file_path)
        if local_path_str:
            local_path = Path(local_path_str)
            if local_path.is_file():
                size = local_path.stat().st_size
                if size > MAX_TELEGRAM_FILE_BYTES:
                    raise ValueError("Telegram file is too large")
                logging.debug("get_file_bytes: local read (%d bytes)", size)
                return local_path.read_bytes()

            # Volume not mounted in this container — fetch from local Bot API server via HTTP.
            # PTB's base_file_url in local_mode = "http://tg-api:8081/file/bot{TOKEN}"
            # The local server serves: base_file_url + absolute_file_path
            logging.warning("get_file_bytes: local path unavailable; trying local Bot API server HTTP")
            try:
                import httpx

                file_url: str = bot.base_file_url + local_path_str
                async with (
                    httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client,
                    client.stream("GET", file_url) as resp,
                ):
                    if resp.status_code == 200:
                        declared = int(resp.headers.get("content-length", "0") or 0)
                        if declared > MAX_TELEGRAM_FILE_BYTES:
                            raise ValueError("Telegram file is too large")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_TELEGRAM_FILE_BYTES:
                                raise ValueError("Telegram file is too large")
                            chunks.append(chunk)
                        logging.debug("get_file_bytes: local server HTTP ok (%d bytes)", total)
                        return b"".join(chunks)
                    logging.warning("get_file_bytes: local server HTTP failed status=%d", resp.status_code)
            except Exception as exc:
                if isinstance(exc, ValueError):
                    raise
                logging.warning("get_file_bytes: local server HTTP error (%s)", type(exc).__name__)
        else:
            logging.warning("get_file_bytes: unsafe local file path; falling back to network download")

    # Cloud mode OR all local fallbacks exhausted: use PTB's standard network download.
    # In cloud mode this calls api.telegram.org; in local mode this tries disk again
    # (last-resort — should only be reached if local server HTTP also failed).
    content = bytes(await tg_file.download_as_bytearray())
    if len(content) > MAX_TELEGRAM_FILE_BYTES:
        raise ValueError("Telegram file is too large")
    return content
