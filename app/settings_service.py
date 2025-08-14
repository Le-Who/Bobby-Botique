import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from .config import settings
from .database import db_query


class _SettingsCache:
    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._values: Dict[str, Tuple[Any, datetime]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            item = self._values.get(key)
            if not item:
                return None
            value, expires_at = item
            if datetime.utcnow() >= expires_at:
                del self._values[key]
                return None
            return value

    async def set(self, key: str, value: Any):
        async with self._lock:
            self._values[key] = (value, datetime.utcnow() + timedelta(seconds=self._ttl))

    async def clear_key(self, key: str):
        async with self._lock:
            if key in self._values:
                del self._values[key]

    async def clear_all(self):
        async with self._lock:
            self._values.clear()


_cache = _SettingsCache(ttl_seconds=60)


def _cast_setting_value(setting_name: str, raw_value: Any) -> Any:
    default_value = getattr(settings, setting_name, None)
    if default_value is None:
        return raw_value
    # Уже нужного типа
    if isinstance(raw_value, type(default_value)):
        return raw_value
    try:
        if isinstance(default_value, bool):
            if isinstance(raw_value, str):
                return raw_value.strip().lower() == 'true'
            return bool(raw_value)
        if isinstance(default_value, int):
            return int(raw_value)
        if isinstance(default_value, float):
            return float(raw_value)
        return raw_value
    except Exception:
        return default_value


async def get_setting(setting_name: str) -> Any:
    """Возвращает актуальное значение настройки (БД → дефолт) с кэшем и приведением типа."""
    try:
        cached = await _cache.get(setting_name)
        if cached is not None:
            return cached

        # Читаем из БД
        result = await db_query(
            "SELECT value FROM bot_settings WHERE setting_name = $1",
            (setting_name,)
        )
        if result and result[0]:
            value = _cast_setting_value(setting_name, result[0]['value'])
        else:
            value = getattr(settings, setting_name, None)

        await _cache.set(setting_name, value)
        return value
    except Exception as e:
        logging.warning(f"SettingsService.get_setting error for {setting_name}: {e}")
        return getattr(settings, setting_name, None)


async def set_setting(setting_name: str, value: Any) -> bool:
    """Обновляет настройку в БД и инвалидирует кэш."""
    try:
        await db_query(
            """
            INSERT INTO bot_settings (setting_name, value, updated_at) 
            VALUES ($1, $2, NOW())
            ON CONFLICT (setting_name) 
            DO UPDATE SET value = $2, updated_at = NOW()
            """,
            (setting_name, str(value))
        )
        await _cache.clear_key(setting_name)
        return True
    except Exception as e:
        logging.error(f"SettingsService.set_setting error for {setting_name}: {e}")
        return False


async def get_all_settings() -> Dict[str, Dict[str, Any]]:
    """Возвращает все настройки, сгруппированные по категориям (БД + дефолты)."""
    settings_dict: Dict[str, Dict[str, Any]] = {}

    def _categorize(name: str) -> str:
        if name.startswith('SAFETY_'):
            return 'Безопасность'
        if name.startswith('DEBUG_') or name.startswith('LOG_'):
            return 'Отладка'
        if name.startswith('ENABLE_') or name.startswith('CACHE_'):
            return 'Производительность'
        if name.startswith('MAX_') or name.startswith('REQUEST_'):
            return 'Производительность'
        return 'Прочее'

    # Сначала читаем из БД
    try:
        result = await db_query("SELECT setting_name, value FROM bot_settings")
        if result:
            for row in result:
                setting_name = row['setting_name']
                value = _cast_setting_value(setting_name, row['value'])
                category = _categorize(setting_name)
                settings_dict.setdefault(category, {})[setting_name] = value
    except Exception as e:
        logging.error(f"SettingsService.get_all_settings db error: {e}")

    # Дополняем дефолтами
    known_setting_names = [
        'SAFETY_MODE', 'ENABLE_SAFETY_FALLBACK',
        'DEBUG_MODE', 'LOG_LEVEL', 'LOG_SAFETY_DECISIONS',
        'ENABLE_CACHE', 'CACHE_TTL_HOURS', 'MAX_RETRIES', 'REQUEST_TIMEOUT_SECONDS',
        'ENABLE_PROMPT_SIMPLIFICATION', 'ENABLE_SYSTEM_INSTRUCTION_FALLBACK',
    ]
    for name in known_setting_names:
        category = _categorize(name)
        if category not in settings_dict:
            settings_dict[category] = {}
        if name not in settings_dict[category]:
            settings_dict[category][name] = getattr(settings, name, None)

    return settings_dict


async def reset_to_defaults() -> bool:
    """Удаляет все пользовательские настройки и очищает кэш."""
    try:
        await db_query("DELETE FROM bot_settings")
        await _cache.clear_all()
        return True
    except Exception as e:
        logging.error(f"SettingsService.reset_to_defaults error: {e}")
        return False


# Удобные обёртки
async def get_bool(name: str) -> bool:
    val = await get_setting(name)
    return bool(val)


async def get_int(name: str) -> int:
    val = await get_setting(name)
    try:
        return int(val)
    except Exception:
        default = getattr(settings, name, 0)
        return int(default) if isinstance(default, int) else 0


async def get_float(name: str) -> float:
    val = await get_setting(name)
    try:
        return float(val)
    except Exception:
        default = getattr(settings, name, 0.0)
        return float(default) if isinstance(default, (float, int)) else 0.0


