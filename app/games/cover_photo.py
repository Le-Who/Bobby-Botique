from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from telegram import InputFile

from app.repos.settings_repo import get_global_setting, set_global_setting

logger = logging.getLogger(__name__)

_ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "artifacts"

# Normalized game_id mapping to setting key & file name
_GAME_MAP = {
    "dailycroc": ("daily_croc_placeholder_file_id", "dailycroc_cover.png"),
    "daily_croc": ("daily_croc_placeholder_file_id", "dailycroc_cover.png"),
    "croc": ("daily_croc_placeholder_file_id", "dailycroc_cover.png"),
    "crocodile": ("daily_croc_placeholder_file_id", "dailycroc_cover.png"),
    "daily2048": ("daily2048_cover_file_id", "daily2048_cover.png"),
    "daily_2048": ("daily2048_cover_file_id", "daily2048_cover.png"),
    "2048": ("daily2048_cover_file_id", "daily2048_cover.png"),
    "dailytrivia": ("dailytrivia_cover_file_id", "dailytrivia_cover.png"),
    "daily_trivia": ("dailytrivia_cover_file_id", "dailytrivia_cover.png"),
    "trivia": ("dailytrivia_cover_file_id", "dailytrivia_cover.png"),
}

_cache: dict[str, tuple[str, float]] = {}
_TTL = 60.0  # seconds


def normalize_game_id(game_id: str) -> str:
    gid = game_id.lower().replace("-", "_")
    return gid


def get_game_keys(game_id: str) -> tuple[str, Path]:
    gid = normalize_game_id(game_id)
    if gid in _GAME_MAP:
        setting_key, file_name = _GAME_MAP[gid]
    else:
        setting_key = f"{gid}_cover_file_id"
        file_name = f"{gid}_cover.png"
    return setting_key, _ARTIFACTS_DIR / file_name


async def get_cover_photo(game_id: str, *, force_upload: bool = False) -> str | InputFile | None:
    setting_key, file_path = get_game_keys(game_id)
    now = time.monotonic()

    if not force_upload:
        cached_file_id, cached_ts = _cache.get(setting_key, ("", 0.0))
        if cached_file_id and (now - cached_ts < _TTL):
            return cached_file_id

        file_id = await get_global_setting(setting_key, "")
        if file_id:
            _cache[setting_key] = (str(file_id), now)
            return str(file_id)

    if file_path.exists():
        try:
            return InputFile(file_path.read_bytes(), filename=file_path.name)
        except Exception as exc:
            logger.warning("Failed to read cover photo file %s: %s", file_path, exc)

    return None


async def remember_cover_file_id(game_id: str, message: Any) -> str | None:
    photos = getattr(message, "photo", None) or []
    if not photos:
        return None
    file_id = getattr(photos[-1], "file_id", "")
    if not file_id:
        return None

    setting_key, _ = get_game_keys(game_id)
    cached_file_id, _ = _cache.get(setting_key, ("", 0.0))
    if file_id != cached_file_id:
        _cache[setting_key] = (file_id, time.monotonic())
        await set_global_setting(setting_key, file_id)
    return file_id


async def set_cover_from_upload(game_id: str, image_bytes: bytes) -> Path:
    setting_key, file_path = get_game_keys(game_id)
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(image_bytes)

    # Invalidate cache & DB setting so next send uploads the new file to Telegram
    invalidate_cache(game_id)
    await set_global_setting(setting_key, "")
    return file_path


def invalidate_cache(game_id: str) -> None:
    setting_key, _ = get_game_keys(game_id)
    _cache.pop(setting_key, None)
