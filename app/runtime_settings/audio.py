"""Versioned TTS voices and generation parameters shared by admin and consumers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from app.runtime_settings import store
from app.runtime_settings.lifecycle import operation_snapshot, runtime_settings_scope
from app.runtime_settings.store import SettingsSnapshot

PROVIDERS = ("gemini", "elevenlabs")
_VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_GEMINI_VOICES = (
    ("Aoede", "естественный, лёгкий"),
    ("Kore", "уверенный, энергичный"),
    ("Puck", "живой"),
    ("Charon", "профессиональный"),
    ("Leda", "лёгкий, молодой"),
    ("Orus", "глубокий, уверенный"),
    ("Zephyr", "ясный, бодрый"),
    ("Rasalgethi", "информативный"),
)
_ELEVENLABS_VOICES = (
    ("XB0fDUnXU5powFXDhCwa", "Charlotte (разговорный)"),
    ("21m00Tcm4TlvDq8ikWAM", "Rachel (спокойный)"),
    ("pNInz6obpgDQGcFmaJgB", "Adam (глубокий)"),
    ("ErXwobaYiN019PkySvjV", "Antoni (дружелюбный)"),
    ("nPczCjzI2devNBz1zQrb", "Brian (профессиональный)"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam (энергичный)"),
    ("EXAVITQu4vr4xnSDxMaL", "Bella (мягкий)"),
)


def _provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Неизвестный провайдер TTS")


def baseline_config(provider: str) -> dict[str, Any]:
    from app.config import settings

    _provider(provider)
    if provider == "gemini":
        return {
            "default_voice": "Aoede",
            "voices": [
                {"id": identity, "name": f"{identity} ({label})", "prompt": ""} for identity, label in _GEMINI_VOICES
            ],
            "temperature": 0.5,
            "max_output_tokens": 16384,
        }
    voices = [{"id": identity, "name": name} for identity, name in _ELEVENLABS_VOICES]
    default_voice = settings.ELEVENLABS_VOICE_ID
    if default_voice not in {voice["id"] for voice in voices}:
        voices.insert(0, {"id": default_voice, "name": default_voice})
    return {
        "default_voice": default_voice,
        "voices": voices,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.25, "use_speaker_boost": True},
        "apply_text_normalization": "on",
    }


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name}: ожидается число от {minimum:g} до {maximum:g}")
    return float(value)


def validate_config(provider: str, value: Any) -> dict[str, Any]:
    """Return a detached JSON value; validate persisted frozen snapshots too."""
    _provider(provider)
    fields = {"default_voice", "voices"} | (
        {"temperature", "max_output_tokens"} if provider == "gemini" else {"voice_settings", "apply_text_normalization"}
    )
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("Некорректные поля настроек TTS")
    rows = value["voices"]
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 100:
        raise ValueError("Каталог должен содержать от 1 до 100 голосов")
    voices = []
    identities = set()
    for row in rows:
        voice_fields = {"id", "name", "prompt"} if provider == "gemini" else {"id", "name"}
        if not isinstance(row, Mapping) or set(row) != voice_fields:
            raise ValueError("Некорректные поля голоса")
        identity, name = row["id"], row["name"]
        if not isinstance(identity, str) or not _VOICE_ID.fullmatch(identity) or identity in identities:
            raise ValueError(
                "ID голоса должен быть уникальным: латиница, цифры, дефис, подчёркивание (до 128 символов)"
            )
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError("Название голоса должно содержать от 1 до 200 символов")
        identities.add(identity)
        voice = {"id": identity, "name": name.strip()}
        if provider == "gemini":
            prompt = row["prompt"]
            if (
                not isinstance(prompt, str)
                or len(prompt.encode("utf-8")) > 4000
                or any(ord(c) < 32 and c not in "\n\r\t" for c in prompt)
            ):
                raise ValueError("Инструкция голоса: не более 4000 байт UTF-8")
            voice["prompt"] = prompt
        voices.append(voice)
    default = value["default_voice"]
    if not isinstance(default, str) or default not in identities:
        raise ValueError("Голос по умолчанию должен присутствовать в каталоге")
    result: dict[str, Any] = {"default_voice": default, "voices": voices}
    if provider == "gemini":
        result["temperature"] = _number(value["temperature"], "Температура", 0, 2)
        tokens = value["max_output_tokens"]
        if type(tokens) is not int or not 1024 <= tokens <= 16384:
            raise ValueError("max_output_tokens: целое число от 1024 до 16384")
        result["max_output_tokens"] = tokens
    else:
        parameters = value["voice_settings"]
        if not isinstance(parameters, Mapping) or set(parameters) != {
            "stability",
            "similarity_boost",
            "style",
            "use_speaker_boost",
        }:
            raise ValueError("Некорректные параметры ElevenLabs")
        result["voice_settings"] = {
            key: _number(parameters[key], key, 0, 1) for key in ("stability", "similarity_boost", "style")
        }
        if type(parameters["use_speaker_boost"]) is not bool:
            raise ValueError("use_speaker_boost: ожидается true или false")
        result["voice_settings"]["use_speaker_boost"] = parameters["use_speaker_boost"]
        normalization = value["apply_text_normalization"]
        if not isinstance(normalization, str) or normalization not in {"auto", "on", "off"}:
            raise ValueError("Нормализация текста: auto, on или off")
        result["apply_text_normalization"] = normalization
    return result


async def get_config(provider: str, *, snapshot: SettingsSnapshot | None = None) -> dict[str, Any]:
    _provider(provider)
    snapshot = snapshot if snapshot is not None else await operation_snapshot()
    return validate_config(provider, snapshot.values.get(f"audio:{provider}", baseline_config(provider)))


def resolve_voice(config: Mapping[str, Any], requested: str | None) -> str:
    """Removed preferences and another provider's voice use this provider's default."""
    return (
        requested if requested and any(row["id"] == requested for row in config["voices"]) else config["default_voice"]
    )


async def list_audio_configs(*, snapshot: SettingsSnapshot | None = None) -> list[dict[str, Any]]:
    snapshot = snapshot if snapshot is not None else await operation_snapshot()
    return [
        {
            "id": provider,
            "title": "Gemini TTS" if provider == "gemini" else "ElevenLabs TTS",
            "source": "admin" if f"audio:{provider}" in snapshot.values else "default",
            "apply": "next_request",
            "value": await get_config(provider, snapshot=snapshot),
            "baseline": baseline_config(provider),
            "note": "Порядок списка задаёт порядок выбора. ID должен поддерживаться провайдером. "
            + (
                "Индивидуальная инструкция дополняет общие промпты TTS. Температура пользователя имеет приоритет; API минимум 0.5."
                if provider == "gemini"
                else "Отдельный промпт стиля не поддерживается: переданный текст озвучивается дословно."
            ),
        }
        for provider in PROVIDERS
    ]


async def save_config(provider: str, value: Any, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    return await store.set_value(
        f"audio:{provider}", validate_config(provider, value), expected_revision=expected_revision, actor=actor
    )


async def reset_config(provider: str, *, expected_revision: int, actor: str) -> SettingsSnapshot:
    _provider(provider)
    return await store.reset_value(f"audio:{provider}", expected_revision=expected_revision, actor=actor)


@runtime_settings_scope()
async def user_voice_catalog() -> dict[str, Any]:
    from app.config import settings
    from app.process_policies import resolve_process

    delivery = await resolve_process("tts.delivery", ("elevenlabs", "gemini"))
    for provider in delivery.models:
        if provider == "elevenlabs" and not settings.ELEVENLABS_API_KEYS:
            continue
        config = await get_config(provider)
        return {
            "provider": provider,
            "default_voice": config["default_voice"],
            "voices": [{"id": voice["id"], "name": voice["name"]} for voice in config["voices"]],
        }
    return {"provider": "none", "default_voice": None, "voices": []}


async def valid_user_voice(voice_id: Any) -> bool:
    return isinstance(voice_id, str) and any(row["id"] == voice_id for row in (await user_voice_catalog())["voices"])
