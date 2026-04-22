from __future__ import annotations

from typing import Any

from app.repos.settings_repo import get_global_setting

LIVE_AUDIO_ENABLED_KEY = "live_audio_enabled"
HINT_PREWARM_ENABLED_KEY = "crocodile_hint_prewarm_enabled"
DAILY_DUAL_TRACK_ENABLED_KEY = "daily_dual_track_enabled"


async def _is_enabled(setting_key: str, *, default: str = "on") -> bool:
    value = await get_global_setting(setting_key, default)
    return str(value).strip().lower() != "off"


async def is_live_audio_enabled() -> bool:
    return await _is_enabled(LIVE_AUDIO_ENABLED_KEY, default="on")


async def is_hint_prewarm_enabled() -> bool:
    return await _is_enabled(HINT_PREWARM_ENABLED_KEY, default="on")


async def is_daily_dual_track_enabled() -> bool:
    return await _is_enabled(DAILY_DUAL_TRACK_ENABLED_KEY, default="on")


async def get_crocodile_runtime_switches() -> dict[str, bool]:
    return {
        "live_audio_enabled": await is_live_audio_enabled(),
        "crocodile_hint_prewarm_enabled": await is_hint_prewarm_enabled(),
        "daily_dual_track_enabled": await is_daily_dual_track_enabled(),
    }


def merge_health_flags(base: dict[str, Any], **extra: Any) -> dict[str, Any]:
    merged = dict(base)
    merged.update(extra)
    return merged
