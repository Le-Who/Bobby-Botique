"""Utility for transparently downloading Telegram files in cloud and local API modes.

In local_mode (Local Bot API Server): file_path is an absolute filesystem path
readable directly from the shared Docker volume — e.g.
/var/lib/telegram-bot-api/xxx/file.ogg. We read it with zero network copy.

In cloud mode (default): falls back to the standard download_as_bytearray()
network call via api.telegram.org.
"""

import logging
import os
from pathlib import Path

from telegram import Bot, File


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
        return file_path[idx:]
    # Already a clean local path?
    if file_path.startswith("/") and not file_path.startswith("http"):
        return file_path
    return None


def _diagnose_missing_file(local_path: Path) -> None:
    """Log diagnostic info when a local file is not found — runs ONCE per miss."""
    parts = local_path.parts  # ('/', 'var', 'lib', 'telegram-bot-api', '{token}', ...)
    # Walk down the path tree to find exactly where it breaks
    for i in range(1, len(parts) + 1):
        segment = Path(*parts[:i])
        if segment.exists():
            if segment.is_dir():
                try:
                    children = os.listdir(segment)
                    logging.warning(
                        "  [diag] EXISTS dir %s → children: %s",
                        segment,
                        children[:20] if children else "(empty)",
                    )
                except PermissionError:
                    logging.warning("  [diag] EXISTS dir %s → PERMISSION DENIED", segment)
            else:
                logging.warning("  [diag] EXISTS file %s (%d bytes)", segment, segment.stat().st_size)
        else:
            logging.warning("  [diag] MISSING %s ← breaks here", segment)
            break


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
                logging.debug("get_file_bytes: local read %s (%d bytes)", local_path, size)
                return local_path.read_bytes()

            # Volume not mounted in this container — fetch from local Bot API server via HTTP.
            # PTB's base_file_url in local_mode = "http://tg-api:8081/file/bot{TOKEN}"
            # The local server serves: base_file_url + absolute_file_path
            logging.warning(
                "get_file_bytes: local path not found (%s), trying local Bot API server HTTP",
                local_path,
            )
            _diagnose_missing_file(local_path)
            try:
                import httpx

                file_url: str = bot.base_file_url + local_path_str
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(file_url)
                if resp.status_code == 200:
                    logging.debug(
                        "get_file_bytes: local server HTTP ok url=%s bytes=%d",
                        file_url,
                        len(resp.content),
                    )
                    return resp.content
                logging.warning(
                    "get_file_bytes: local server HTTP failed status=%d url=%s",
                    resp.status_code,
                    file_url,
                )
            except Exception as exc:
                logging.warning("get_file_bytes: local server HTTP error: %s", exc)
        else:
            logging.warning(
                "get_file_bytes: could not extract local path from file_path (%s), falling back to network download",
                tg_file.file_path,
            )

    # Cloud mode OR all local fallbacks exhausted: use PTB's standard network download.
    # In cloud mode this calls api.telegram.org; in local mode this tries disk again
    # (last-resort — should only be reached if local server HTTP also failed).
    return bytes(await tg_file.download_as_bytearray())
