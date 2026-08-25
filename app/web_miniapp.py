# /app/web_miniapp.py
"""Telegram Mini App backend — LTM Explorer, Settings Editor & Crocodile Game.

Endpoints are authenticated via Telegram WebApp initData (HMAC-SHA256).
Each user can only access their own data — user_id is extracted from
the validated initData payload, never from query params.

Blueprint registered on the main Quart app at prefix ``/webapp``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
import typing
import urllib.parse
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

from quart import Blueprint, jsonify, render_template, request
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.bot_instance import get_bot
from app.config import GEMINI_LIVE_VOICE_NAME, settings
from app.games import crocodile_runtime as _croc_runtime
from app.natal.city_catalog import find_city_by_id, search_cities, search_countries
from app.natal.models import BirthInput, ReportType, TimePrecision
from app.natal.service import create_natal_report
from app.utils.background_tasks import submit_task
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

miniapp_blueprint = Blueprint("miniapp", __name__, template_folder="templates")

_INIT_DATA_MAX_AGE_SECONDS = 3600
_INIT_DATA_MAX_FUTURE_SKEW_SECONDS = 30

from app.security import SyncRateLimiter, rate_limit  # noqa: E402

# Rate limiter for public-facing Reader pages (30 req/min)
_reader_limiter = SyncRateLimiter(max_requests=30, window_seconds=60)
rate_limit_reader = rate_limit(_reader_limiter)

# State tracking for Live Audio sessions
ACTIVE_LIVE_SESSIONS: set[int] = set()
_KEY_ROTATION_INDEX: int = 0
_LIVE_CONNECT_RETRY_AFTER_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_LIVE_DEFAULT_THINKING_LEVEL = "low"
_LIVE_THINKING_CONFIG_MAP: dict[str, str] = {
    "off": "minimal",
    "low": "low",
    "medium": "medium",
}
from app.i18n import t


def _get_live_voice_options(lang: str) -> list[dict[str, str]]:
    return [
        {"id": "Aoede", "name": "Aoede", "gender": "female", "description": t("miniapp.voice.aoede", lang)},
        {"id": "Kore", "name": "Kore", "gender": "female", "description": t("miniapp.voice.kore", lang)},
        {"id": "Leda", "name": "Leda", "gender": "female", "description": t("miniapp.voice.leda", lang)},
        {"id": "Zephyr", "name": "Zephyr", "gender": "male", "description": t("miniapp.voice.zephyr", lang)},
        {"id": "Charon", "name": "Charon", "gender": "male", "description": t("miniapp.voice.charon", lang)},
        {"id": "Orus", "name": "Orus", "gender": "male", "description": t("miniapp.voice.orus", lang)},
    ]


def _get_live_thinking_presets(lang: str) -> list[dict[str, str]]:
    return [
        {"id": "off", "label": t("miniapp.preset.off_label", lang), "hint": t("miniapp.preset.off_hint", lang)},
        {"id": "low", "label": t("miniapp.preset.low_label", lang), "hint": t("miniapp.preset.low_hint", lang)},
        {
            "id": "medium",
            "label": t("miniapp.preset.medium_label", lang),
            "hint": t("miniapp.preset.medium_hint", lang),
        },
    ]


_LIVE_VOICE_IDS = {"Aoede", "Kore", "Leda", "Zephyr", "Charon", "Orus"}
_LIVE_CONNECTION_MODE_IDS = {"standard", "vertex_internet"}
_LIVE_DEFAULT_CONNECTION_MODE = "standard"
_LIVE_VERTEX_CONNECTION_MODE = "vertex_internet"
_VERTEX_LIVE_MODEL = "gemini-live-2.5-flash-native-audio"


def _get_live_connection_modes(lang: str) -> list[dict[str, str]]:
    return [
        {
            "id": _LIVE_DEFAULT_CONNECTION_MODE,
            "label": t("miniapp.conn.standard_label", lang),
            "summary": t("miniapp.conn.standard_summary", lang),
        },
        {
            "id": _LIVE_VERTEX_CONNECTION_MODE,
            "label": t("miniapp.conn.vertex_label", lang),
            "summary": t("miniapp.conn.vertex_summary", lang),
        },
    ]


# Backward-compatible test hooks for the classic game lock fallback registry.
_game_locks = _croc_runtime._game_locks


def _extract_live_retry_after_seconds(error_text: str) -> int | None:
    """Parse an upstream Retry-After style hint from Gemini error text."""
    match = _LIVE_CONNECT_RETRY_AFTER_RE.search(error_text)
    if not match:
        return None
    try:
        return max(1, int(float(match.group(1)) + 0.999))
    except ValueError:
        return None


def _is_live_resource_exhausted(error_text: str) -> bool:
    """Live API surfaces quota/session saturation through RESOURCE_EXHAUSTED text."""
    err_lower = error_text.lower()
    return any(
        marker in err_lower
        for marker in (
            "resource_exhausted",
            "exceeded your current quota",
            "rate limit",
            "quota",
        )
    )


async def _get_live_model_cooldown_seconds() -> int:
    """Return remaining cluster-wide cooldown for the Live model."""
    from app.cache import redis_client

    if not redis_client:
        return 0
    ttl = await redis_client.ttl("live_model_cooldown")
    return max(0, ttl)


async def _get_live_model_cooldown_reason() -> str:
    """Return the reason for the active model cooldown."""
    from app.cache import redis_client

    if not redis_client:
        return ""
    reason = await redis_client.get("live_model_cooldown")
    return reason.decode("utf-8") if reason else ""


async def _mark_live_model_cooldown(seconds: int, reason: str) -> int:
    """Trip a short model-level breaker to stop reconnect storms across all workers."""
    from app.cache import redis_client

    if not redis_client:
        return seconds

    cooldown_seconds = max(15, min(seconds, 300))
    await redis_client.setex("live_model_cooldown", cooldown_seconds, reason[:500])
    return cooldown_seconds


async def _send_live_fatal(
    websocket,
    *,
    reason: str,
    message: str,
    retry_after_seconds: int | None = None,
) -> None:
    """Emit a structured fatal event for the frontend before closing the socket."""
    payload: dict[str, Any] = {
        "type": "fatal",
        "reason": reason,
        "message": message,
        "ui_suggestion": "switch_to_chat",
    }
    if retry_after_seconds:
        payload["retry_after_seconds"] = retry_after_seconds
        payload["retry_at"] = (datetime.now(UTC) + timedelta(seconds=retry_after_seconds)).isoformat()
    try:
        await websocket.send_json(payload)
    except Exception:
        pass


def _default_model_name() -> str:
    return settings.DEFAULT_MODEL if settings else "gemini-3.1-flash-lite"


def _default_chat_state():
    from app.database import ChatState

    return ChatState(
        history=[],
        model=_default_model_name(),
        token_count=0,
        search_enabled=False,
        system_prompt=None,
    )


def _default_live_voice_name() -> str:
    return GEMINI_LIVE_VOICE_NAME or "Aoede"


def _resolve_live_voice_name(chat_state) -> str:
    voice_name = getattr(chat_state, "live_voice_name", None) if chat_state else None
    valid_voice_ids = _LIVE_VOICE_IDS
    if isinstance(voice_name, str) and voice_name in valid_voice_ids:
        return voice_name
    return _default_live_voice_name()


def _resolve_live_thinking_level(chat_state) -> str:
    thinking_level = getattr(chat_state, "live_thinking_level", None) if chat_state else None
    if isinstance(thinking_level, str) and thinking_level in _LIVE_THINKING_CONFIG_MAP:
        return thinking_level
    return _LIVE_DEFAULT_THINKING_LEVEL


def _resolve_live_connection_mode(chat_state) -> str:
    mode = getattr(chat_state, "live_connection_mode", None) if chat_state else None
    valid_mode_ids = _LIVE_CONNECTION_MODE_IDS
    if isinstance(mode, str) and mode in valid_mode_ids:
        return mode
    return _LIVE_DEFAULT_CONNECTION_MODE


def _serialize_live_settings(chat_state) -> dict[str, str]:
    return {
        "live_voice_name": _resolve_live_voice_name(chat_state),
        "live_thinking_level": _resolve_live_thinking_level(chat_state),
        "live_connection_mode": _resolve_live_connection_mode(chat_state),
    }


def _build_live_connect_config(
    *,
    system_instruction: str,
    resumption_handle: str | None,
    voice_name: str,
    thinking_level: str,
):
    from google.genai import types

    thinking_config = types.ThinkingConfig(thinking_level=_LIVE_THINKING_CONFIG_MAP[thinking_level])  # type: ignore[arg-type]
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        session_resumption=types.SessionResumptionConfig(
            handle=resumption_handle or None,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True,
            ),
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        ),
        thinking_config=thinking_config,
        system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
    )


def _build_vertex_live_connect_config(
    *,
    system_instruction: str,
    resumption_handle: str | None,
    voice_name: str,
):
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        session_resumption=types.SessionResumptionConfig(
            handle=resumption_handle or None,
            transparent=True,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True,
            ),
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice_name,
                )
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )


# ── Telegram initData Validation ─────────────────────────────────────────────


def _validate_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Validate Telegram WebApp initData and return parsed data.

    Implements the official validation algorithm:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    Returns the parsed data dict if valid, None otherwise.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None

        # Build data-check-string: key=value pairs sorted alphabetically
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        # HMAC-SHA256(HMAC-SHA256("WebAppData", bot_token), data_check_string)
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("initData hash mismatch")
            return None

        try:
            auth_date = int(parsed.get("auth_date", ""))
        except (TypeError, ValueError):
            logger.warning("initData auth_date is missing or invalid")
            return None

        age_seconds = int(time.time()) - auth_date
        if age_seconds > _INIT_DATA_MAX_AGE_SECONDS or age_seconds < -_INIT_DATA_MAX_FUTURE_SKEW_SECONDS:
            logger.warning("initData auth_date outside allowed window")
            return None

        # Parse the user field
        if "user" in parsed:
            parsed["user"] = json.loads(parsed["user"])

        return parsed

    except Exception as e:
        logger.warning("initData validation error: %s", e)
        return None


def _extract_user_id(validated_data: dict[str, Any]) -> int | None:
    """Extract user_id from validated initData."""
    user = validated_data.get("user")
    if isinstance(user, dict):
        return user.get("id")
    return None


def require_webapp_auth(f: typing.Callable) -> typing.Callable:
    """Decorator: validate Telegram initData and inject user_id."""

    @wraps(f)
    async def decorated(*args, **kwargs):
        # initData comes in the Authorization header: "tma <initData>"
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("tma "):
            return jsonify({"error": "Missing authorization"}), 401

        init_data = auth_header[4:]  # strip "tma " prefix
        bot_token = settings.TELEGRAM_BOT_TOKEN
        if not bot_token:
            return jsonify({"error": "Server misconfiguration"}), 500

        validated = _validate_init_data(init_data, bot_token)
        if validated is None:
            return jsonify({"error": "Invalid initData"}), 401

        user_id = _extract_user_id(validated)
        if not user_id:
            return jsonify({"error": "No user in initData"}), 401

        # Inject user_id into the handler
        kwargs["user_id"] = user_id
        return await f(*args, **kwargs)

    return decorated


def require_authorized_webapp_user(f: typing.Callable) -> typing.Callable:
    """Require the signed Telegram user to retain bot access.

    HMAC proves who opened the Mini App; it does not prove that the account is
    still authorized.  Private-data endpoints fail closed when authorization
    cannot be confirmed.
    """

    @wraps(f)
    async def decorated(*args, **kwargs):
        user_id = kwargs.get("user_id")
        if not isinstance(user_id, int):
            return jsonify({"error": "No authorized user"}), 403
        try:
            from app.repos.users import is_authorized

            if not await is_authorized(user_id):
                return jsonify({"error": "Access revoked"}), 403
        except Exception as exc:
            logger.error("Mini App authorization check failed for user %s: %s", user_id, exc)
            return jsonify({"error": "Authorization unavailable"}), 503
        return await f(*args, **kwargs)

    return decorated


async def _require_authorized_websocket_user(user_id: int) -> bool:
    """Fail closed unless the signed Telegram user still has bot access."""
    from quart import websocket

    try:
        from app.repos.users import is_authorized

        authorized = await is_authorized(user_id)
    except Exception:
        logger.error(
            "Mini App WebSocket authorization check failed for user %s",
            user_id,
            exc_info=True,
        )
        await websocket.close(4003, "Authorization unavailable")
        return False

    if not authorized:
        await websocket.close(4003, "Access revoked")
        return False
    return True


async def _resolve_authorized_legacy_miniapp_user(
    raw_init_data: str,
) -> tuple[int, Any | None]:
    """Resolve legacy-header identity and reject revoked signed users.

    Daily Trivia historically allows anonymous/public reads, so an absent or
    invalid legacy header still resolves to user ``0``.  Once a valid signed
    identity is present, private reads and writes require current bot access.
    """
    if not raw_init_data:
        return 0, None

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    validated = _validate_init_data(raw_init_data, bot_token) if bot_token else None
    user_id = _extract_user_id(validated) if validated else None
    if not isinstance(user_id, int) or user_id <= 0:
        return 0, None

    try:
        from app.repos.users import is_authorized

        if not await is_authorized(user_id):
            return 0, (jsonify({"error": "Access revoked"}), 403)
    except Exception as exc:
        logger.error("Legacy Mini App authorization check failed for user %s: %s", user_id, exc)
        return 0, (jsonify({"error": "Authorization unavailable"}), 503)

    return user_id, None


# ── Static page ──────────────────────────────────────────────────────────────


@miniapp_blueprint.route("/")
async def miniapp_page():
    """Serve the Mini App HTML shell."""

    return await render_template("miniapp.html")


# ── Natal Form Mini App ─────────────────────────────────────────────────────

_NATAL_FORM_COUNTRIES: tuple[tuple[str, str], ...] = (
    ("UA", "Украина"),
    ("RU", "Россия"),
    ("BY", "Беларусь"),
)
_NATAL_FORM_CITY_QUERIES: dict[str, tuple[str, ...]] = {
    "UA": ("Киев", "Львов", "Харьков"),
    "RU": ("Москва", "Санкт-Петербург", "Новосибирск"),
    "BY": ("Минск", "Гомель", "Витебск"),
}
_NATAL_FORM_FOCUS_LABELS: dict[str, str] = {
    "general": "Общий",
    "relationships": "Отношения",
    "career": "Карьера",
    "psychology": "Психология",
    "brief": "Кратко",
}
_NATAL_FORM_REPORT_TYPES: tuple[dict[str, Any], ...] = (
    {
        "id": ReportType.COMBINED.value,
        "label": "Натал + матрица",
        "badge": "Рекомендуем",
        "summary": "Лучший выбор для первого разбора",
        "detail": "Астрологическая карта, архетипы матрицы и общий смысловой вывод в одном отчёте.",
    },
    {
        "id": ReportType.NATAL.value,
        "label": "Только натал",
        "badge": "",
        "summary": "Максимум астрологических деталей",
        "detail": "Планеты, аспекты, дома, Асцендент и MC, если время рождения известно.",
    },
    {
        "id": ReportType.DESTINY_MATRIX.value,
        "label": "Только матрица",
        "badge": "",
        "summary": "Быстрый архетипический слой по дате",
        "detail": "Матрице достаточно даты: без времени, города и лишних вопросов.",
    },
)


@miniapp_blueprint.route("/natal-form")
async def natal_form_page():
    """Serve the natal questionnaire Mini App."""
    return await render_template(
        "natal_form.html",
        natal_form_options=_natal_form_options(),
    )


@miniapp_blueprint.route("/api/natal/submit", methods=["POST"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_natal_submit(user_id: int):
    """Build a natal report from Mini App form data and deliver it to the user chat."""
    bot = get_bot()
    if bot is None:
        return jsonify({"error": "bot_not_ready"}), 503

    body = await request.get_json(silent=True) or {}
    try:
        birth_input = _birth_input_from_natal_payload(body)
    except ValueError as exc:
        return jsonify({"error": "invalid_birth_input", "detail": str(exc)}), 400

    webhook_url = _public_webapp_base_url()
    if not webhook_url:
        return jsonify(
            {"error": "server_misconfiguration", "detail": "WEBAPP_BASE_URL or WEBHOOK_URL is required."}
        ), 500

    submit_task(_build_and_send_natal_report(bot, user_id, birth_input, webhook_url))
    return jsonify({"ok": True, "status": "accepted"})


async def _build_and_send_natal_report(bot: Any, user_id: int, birth_input: BirthInput, webhook_url: str) -> None:
    try:
        report = await create_natal_report(
            birth_input=birth_input,
            user_id=user_id,
            chat_id=user_id,
            webhook_url=webhook_url,
        )
        await _send_natal_report_to_private_chat(bot, user_id, report, birth_input)
    except Exception as exc:
        logger.error("Mini App natal background task failed user=%s: %s", user_id, exc, exc_info=True)
        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "Не удалось построить натальную карту по отправленной анкете. "
                    "Попробуйте открыть анкету и отправить данные ещё раз."
                ),
            )
        except TelegramError as notify_error:
            logger.warning("Failed to notify user %s about natal report failure: %s", user_id, notify_error)


def _natal_form_options() -> dict[str, Any]:
    return {
        "report_types": list(_NATAL_FORM_REPORT_TYPES),
        "countries": [{"code": code, "label": label} for code, label in _NATAL_FORM_COUNTRIES],
        "cities": {
            code: [
                {
                    "id": city.geoname_id,
                    "label": query,
                    "display": city.display_name,
                }
                for query in city_queries
                for city in search_cities(query, limit=1, country_code=code)
            ]
            for code, city_queries in _NATAL_FORM_CITY_QUERIES.items()
        },
        "focuses": [{"id": key, "label": label} for key, label in _NATAL_FORM_FOCUS_LABELS.items()],
    }


def _birth_input_from_natal_payload(payload: dict[str, Any]) -> BirthInput:
    if not isinstance(payload, dict):
        raise ValueError("Некорректный формат формы.")

    birth_date = _required_str(payload, "birth_date")
    _validate_iso_date(birth_date)
    report_type = _parse_report_type(str(payload.get("report_type") or ReportType.NATAL.value))
    focus = str(payload.get("focus") or "general").strip() or "general"
    if focus not in _NATAL_FORM_FOCUS_LABELS:
        focus = "general"
    if report_type == ReportType.DESTINY_MATRIX:
        return BirthInput(
            birth_date=birth_date,
            time_precision=TimePrecision.UNKNOWN,
            birth_place="",
            focus=focus,
            language="ru",
            report_type=report_type,
        )

    precision = _parse_time_precision(_required_str(payload, "time_precision"))
    country_code = _resolve_country_code(payload)
    city = _resolve_natal_city(payload, country_code)

    time_kwargs: dict[str, str | None] = {
        "birth_time": None,
        "birth_time_range_start": None,
        "birth_time_range_end": None,
    }
    if precision in {TimePrecision.EXACT, TimePrecision.APPROXIMATE}:
        time_kwargs["birth_time"] = _validate_time_value(_required_str(payload, "birth_time"))
    elif precision == TimePrecision.RANGE:
        start = _validate_time_value(_required_str(payload, "birth_time_range_start"))
        end = _validate_time_value(_required_str(payload, "birth_time_range_end"))
        if _time_to_minutes(end) <= _time_to_minutes(start):
            raise ValueError("Диапазон времени должен заканчиваться позже начала.")
        time_kwargs["birth_time_range_start"] = start
        time_kwargs["birth_time_range_end"] = end

    return BirthInput(
        birth_date=birth_date,
        time_precision=precision,
        birth_place=city.display_name,
        birth_place_country_code=city.country_code,
        birth_place_geoname_id=city.geoname_id,
        birth_place_latitude=city.latitude,
        birth_place_longitude=city.longitude,
        birth_place_timezone=city.timezone,
        birth_place_display_name=city.display_name,
        focus=focus,
        language="ru",
        report_type=report_type,
        **time_kwargs,
    )


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Поле обязательно: {key}.")
    return value.strip()


def _validate_iso_date(value: str) -> None:
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Дата рождения должна быть в формате YYYY-MM-DD.") from exc


def _parse_time_precision(value: str) -> TimePrecision:
    try:
        return TimePrecision(value)
    except ValueError as exc:
        raise ValueError("Выберите точность времени рождения.") from exc


def _parse_report_type(value: str) -> ReportType:
    try:
        return ReportType(value)
    except ValueError as exc:
        raise ValueError("Выберите тип разбора.") from exc


def _resolve_country_code(payload: dict[str, Any]) -> str:
    raw_code = str(payload.get("country_code") or "").strip().upper()
    if raw_code:
        matches = search_countries(raw_code, limit=1)
        if matches and matches[0].code == raw_code:
            return raw_code
    country_name = str(payload.get("country") or "").strip()
    if country_name:
        matches = search_countries(country_name, limit=1)
        if matches:
            return matches[0].code
    raise ValueError("Страна рождения не найдена.")


def _resolve_natal_city(payload: dict[str, Any], country_code: str):
    city_id = str(payload.get("city_geoname_id") or "").strip()
    if city_id:
        city = find_city_by_id(city_id)
        if city is None or city.country_code != country_code:
            raise ValueError("Город не найден в выбранной стране.")
        return city
    city_query = str(payload.get("birth_place") or payload.get("city") or "").strip()
    if not city_query:
        raise ValueError("Поле обязательно: город рождения.")
    matches = search_cities(city_query, limit=1, country_code=country_code)
    if not matches:
        raise ValueError("Город не найден в выбранной стране.")
    return matches[0]


def _validate_time_value(value: str) -> str:
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Время должно быть в формате HH:MM.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("Время должно быть в формате HH:MM.") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Время должно быть в пределах 00:00-23:59.")
    return f"{hour:02d}:{minute:02d}"


def _time_to_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _public_webapp_base_url() -> str:
    base = getattr(settings, "WEBAPP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    return webhook_url.split("/webhook", 1)[0].rstrip("/")


async def get_natal_cover_photo():
    from app.handlers.natal_chart import _get_natal_cover_photo

    return await _get_natal_cover_photo()


async def remember_natal_cover_file_id(message: Any) -> None:
    from app.handlers.natal_chart import _remember_natal_cover_file_id

    await _remember_natal_cover_file_id(message)


def natal_result_caption(report, birth_input: BirthInput) -> str:
    from app.handlers.natal_chart import _result_caption

    return _result_caption(report, birth_input)


def natal_result_keyboard(report):
    from app.handlers.natal_chart import _result_keyboard

    return _result_keyboard(report)


async def _send_natal_report_to_private_chat(bot, user_id: int, report, birth_input: BirthInput) -> None:
    caption = natal_result_caption(report, birth_input)
    keyboard = natal_result_keyboard(report)
    cover = await get_natal_cover_photo()
    if cover is not None:
        try:
            message = await bot.send_photo(
                chat_id=user_id,
                photo=cover,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            await remember_natal_cover_file_id(message)
            return
        except (OSError, TelegramError) as exc:
            logger.warning("Mini App natal cover send failed user=%s: %s", user_id, exc)
    await bot.send_message(
        chat_id=user_id,
        text=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ── Memory API ───────────────────────────────────────────────────────────────


@miniapp_blueprint.route("/api/memories")
@require_webapp_auth
@require_authorized_webapp_user
async def api_list_memories(user_id: int):
    """List user memories with pagination."""
    try:
        from app.repos.memory import list_memories

        offset = request.args.get("offset", 0, type=int)
        limit = min(request.args.get("limit", 20, type=int), 50)

        memories = await list_memories(user_id, offset=offset, limit=limit)

        # Serialize datetimes
        for m in memories:
            if m.get("created_at"):
                m["created_at"] = m["created_at"].isoformat()

        return jsonify({"memories": memories, "offset": offset, "limit": limit})
    except Exception as e:
        logger.error("Mini App list memories error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/memories/stats")
@require_webapp_auth
@require_authorized_webapp_user
async def api_memory_stats(user_id: int):
    """Get memory usage stats for the user."""
    try:
        from app.repos.memory import get_memory_stats

        stats = await get_memory_stats(user_id)
        # Serialize datetimes
        for key in ("oldest", "newest"):
            if stats.get(key):
                stats[key] = stats[key].isoformat()
        return jsonify(stats)
    except Exception as e:
        logger.error("Mini App memory stats error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/memories/<int:memory_id>", methods=["DELETE"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_delete_memory(memory_id: int, user_id: int):
    """Delete a single memory (RLS-scoped to user_id)."""
    try:
        from app.repos.memory import delete_memory

        success = await delete_memory(user_id, memory_id)
        if success:
            return jsonify({"ok": True})
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        logger.error("Mini App delete memory error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Settings API ─────────────────────────────────────────────────────────────


@miniapp_blueprint.route("/api/settings")
@require_webapp_auth
@require_authorized_webapp_user
async def api_get_settings(user_id: int):
    """Get current chat settings for the user."""
    try:
        from app.repos.chats import get_user_chat
        from app.repos.roles import get_user_custom_roles

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return jsonify({"error": "no_chat"}), 404

        # Build available models list grouped by provider
        gemini_models = list(settings.AVAILABLE_MODELS or [])
        openrouter_models = list(settings.OPENROUTER_AVAILABLE_MODELS or [])
        opencode_models = list(settings.OPENCODE_AVAILABLE_MODELS or [])
        freetheai_models = list(settings.FREETHEAI_AVAILABLE_MODELS or [])
        all_models = gemini_models + openrouter_models + opencode_models + freetheai_models

        # Build grouped structure for the frontend picker
        grouped_models = []
        if gemini_models:
            grouped_models.append({"provider": "Google Gemini", "icon": "🤖", "models": gemini_models})
        if opencode_models:
            grouped_models.append({"provider": "Opencode Go", "icon": "⚡", "models": opencode_models})
        if freetheai_models:
            grouped_models.append({"provider": "FreeTheAI", "icon": "🦅", "models": freetheai_models})
        if openrouter_models:
            grouped_models.append({"provider": "OpenRouter", "icon": "🌐", "models": openrouter_models})

        user_roles = await get_user_custom_roles(user_id)

        return jsonify(
            {
                "settings": {
                    "system_prompt": chat_state.system_prompt or "",
                    "model": chat_state.model,
                    "thinking_level": chat_state.thinking_level or "off",
                    "ltm_enabled": chat_state.ltm_enabled,
                    "search_enabled": chat_state.search_enabled,
                    "temperature": chat_state.temperature,
                    "voice_id": chat_state.voice_id,
                    "tts_temperature": chat_state.tts_temperature,
                },
                "available_models": all_models,
                "grouped_models": grouped_models,
                "thinking_levels": ["off", "low", "medium", "high"],
                "custom_roles": user_roles,
            }
        )
    except Exception as e:
        logger.error("Mini App get settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/settings", methods=["PATCH"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_update_settings(user_id: int):
    """Update chat settings (partial update)."""
    try:
        from app.repos.chats import get_user_chat, update_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return jsonify({"error": "no_chat"}), 404

        body = await request.get_json(silent=True) or {}
        changed = False
        requested_ltm_enabled: bool | None = None

        # System prompt
        if "system_prompt" in body:
            prompt = body["system_prompt"]
            if isinstance(prompt, str) and len(prompt) <= 4000:
                chat_state.system_prompt = prompt.strip() or None
                changed = True

        # Model — validate against all providers (single source of truth)
        if "model" in body:
            model = body["model"]
            from app.config import get_all_available_models

            all_models = get_all_available_models()
            if model in all_models:
                chat_state.model = model
                changed = True

        # Thinking level
        if "thinking_level" in body:
            level = body["thinking_level"]
            if level in ("off", "low", "medium", "high"):
                chat_state.thinking_level = level if level != "off" else None
                changed = True

        # LTM
        if "ltm_enabled" in body:
            requested_ltm_enabled = body["ltm_enabled"]
            if not isinstance(requested_ltm_enabled, bool):
                return jsonify({"error": "invalid_ltm_enabled"}), 400
            chat_state.ltm_enabled = requested_ltm_enabled

        # Search
        if "search_enabled" in body:
            chat_state.search_enabled = bool(body["search_enabled"])
            changed = True

        # Temperature
        if "temperature" in body:
            temp = body["temperature"]
            if temp is None:
                chat_state.temperature = None
                changed = True
            elif isinstance(temp, (int, float)):
                temp = float(temp)
                if 0.0 <= temp <= 2.0:
                    chat_state.temperature = temp
                    changed = True

        # TTS Temperature
        if "tts_temperature" in body:
            temp = body["tts_temperature"]
            if temp is None:
                chat_state.tts_temperature = None
                changed = True
            elif isinstance(temp, (int, float)):
                temp = float(temp)
                if 0.0 <= temp <= 2.0:
                    chat_state.tts_temperature = temp
                    changed = True

        # Voice ID
        if "voice_id" in body:
            vid = body["voice_id"]
            if vid is None or isinstance(vid, str):
                chat_state.voice_id = vid
                changed = True

        if requested_ltm_enabled is not None:
            # Consent is its own atomic write.  A stale full ChatState save
            # must never re-enable memory after an opt-out.
            from app.repos.chats import set_ltm_enabled

            chat_state.memory_epoch = await set_ltm_enabled(
                user_id,
                requested_ltm_enabled,
            )
            if not requested_ltm_enabled:
                from app.repos.memory_autosave import cancel_user_memory_tasks

                await cancel_user_memory_tasks(user_id)

        if changed:
            await update_user_chat(user_id, chat_state)

        if changed or requested_ltm_enabled is not None:
            return jsonify({"ok": True})

        return jsonify({"ok": True, "note": "no_changes"})
    except Exception as e:
        logger.error("Mini App update settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Additional Setting Controls: Roles, Context, Voices ────────────────────


@miniapp_blueprint.route("/api/context/reset", methods=["POST"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_reset_context(user_id: int):
    """Clear chat history and summary."""
    try:
        from app.repos.chats import get_user_chat, update_user_chat

        chat_state = await get_user_chat(user_id)
        if chat_state:
            chat_state.history = []
            chat_state.token_count = 0
            chat_state.context_summary = None
            await update_user_chat(user_id, chat_state)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Mini App context reset error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/roles", methods=["GET"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_get_roles(user_id: int):
    """Get custom roles."""
    try:
        from app.repos.roles import get_user_custom_roles

        roles = await get_user_custom_roles(user_id)
        return jsonify({"roles": roles})
    except Exception as e:
        logger.error("Mini App get roles error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/roles/<int:role_id>", methods=["DELETE"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_delete_role(user_id: int, role_id: int):
    """Delete a custom role."""
    try:
        from app.repos.roles import delete_custom_role

        await delete_custom_role(role_id, user_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Mini App delete role error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


_voices_cache: list[dict] | None = None
_voices_cache_ts: float = 0.0
_VOICES_CACHE_TTL: float = 300.0


@miniapp_blueprint.route("/api/voices", methods=["GET"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_get_voices(user_id: int):
    """Provide a list of curated voices depending on available provider."""
    import time

    from app.config import settings

    lang = "ru"

    if settings.ELEVENLABS_API_KEYS:
        global _voices_cache, _voices_cache_ts
        now = time.time()

        # Check cache
        if _voices_cache is not None and (now - _voices_cache_ts < _VOICES_CACHE_TTL):
            return jsonify({"voices": _voices_cache})

        from app.providers.elevenlabs_tts import fetch_voices

        # Use first key for checking available voices (readonly query)
        api_key = settings.ELEVENLABS_API_KEYS[0]
        dynamic_voices = await fetch_voices(api_key)

        if dynamic_voices:
            # Map into the structure expected by the Mini App
            voices = []
            for v in dynamic_voices:
                name = v["name"]
                # Append accent label or similar if present in labels for clarity
                accent = v.get("labels", {}).get("accent")
                if accent:
                    name = f"{name} ({accent.title()})"
                voices.append({"id": v["id"], "name": name})
            _voices_cache = voices
            _voices_cache_ts = now
            return jsonify({"voices": voices})

        # Fallback to static ElevenLabs list on API failure
        logger.warning("ElevenLabs voices API failed or returned empty. Falling back to static curated list.")
        # Clear cache so next request tries again
        _voices_cache = None
        _voices_cache_ts = 0.0

        voices = [
            {"id": "XB0fDUnXU5powFXDhCwa", "name": f"Charlotte ({t('miniapp.voice_tag.conversational', lang)})"},
            {"id": "21m00Tcm4TlvDq8ikWAM", "name": f"Rachel ({t('miniapp.voice_tag.calm', lang)})"},
            {"id": "pNInz6obpgDQGcFmaJgB", "name": f"Adam ({t('miniapp.voice_tag.deep', lang)})"},
            {"id": "ErXwobaYiN019PkySvjV", "name": f"Antoni ({t('miniapp.voice_tag.friendly', lang)})"},
            {"id": "nPczCjzI2devNBz1zQrb", "name": f"Brian ({t('miniapp.voice_tag.professional', lang)})"},
            {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": f"Liam ({t('miniapp.voice_tag.energetic', lang)})"},
            {"id": "EXAVITQu4vr4xnSDxMaL", "name": f"Bella ({t('miniapp.voice_tag.soft', lang)})"},
        ]
    else:
        # Gemini static voices
        voices = [
            {"id": "Aoede", "name": f"Aoede ({t('miniapp.voice_tag.natural_breezy', lang)})"},
            {"id": "Kore", "name": f"Kore ({t('miniapp.voice_tag.confident_energetic', lang)})"},
            {"id": "Puck", "name": f"Puck ({t('miniapp.voice_tag.upbeat_male', lang)})"},
            {"id": "Charon", "name": f"Charon ({t('miniapp.voice_tag.professional', lang)})"},
            {"id": "Leda", "name": f"Leda ({t('miniapp.voice_tag.light_youthful', lang)})"},
            {"id": "Orus", "name": f"Orus ({t('miniapp.voice_tag.deep_authoritative', lang)})"},
            {"id": "Zephyr", "name": f"Zephyr ({t('miniapp.voice_tag.clear_cheerful', lang)})"},
            {"id": "Rasalgethi", "name": f"Rasalgethi ({t('miniapp.voice_tag.informative', lang)})"},
        ]
    return jsonify({"voices": voices})


@miniapp_blueprint.route("/api/live-settings", methods=["GET"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_get_live_settings(user_id: int):
    """Return per-user Gemini Live Audio settings and available presets."""
    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(user_id)
        lang = "ru"
        return jsonify(
            {
                "live_settings": _serialize_live_settings(chat_state),
                "connection_modes": _get_live_connection_modes(lang),
                "voices": _get_live_voice_options(lang),
                "thinking_presets": _get_live_thinking_presets(lang),
                "reconnect_note": t("miniapp.reconnect_note", lang),
            }
        )
    except Exception as e:
        logger.error("Mini App get live settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/live-settings", methods=["PATCH"])
@require_webapp_auth
@require_authorized_webapp_user
async def api_update_live_settings(user_id: int):
    """Update per-user Gemini Live Audio settings without affecting reply TTS."""
    try:
        from app.repos.chats import get_user_chat, update_user_chat

        chat_state = await get_user_chat(user_id) or _default_chat_state()
        body = await request.get_json(silent=True) or {}
        changed = False

        if "live_voice_name" in body:
            voice_name = body["live_voice_name"]
            valid_voice_ids = _LIVE_VOICE_IDS
            if voice_name in valid_voice_ids:
                chat_state.live_voice_name = voice_name
                changed = True

        if "live_thinking_level" in body:
            thinking_level = body["live_thinking_level"]
            if thinking_level in _LIVE_THINKING_CONFIG_MAP:
                chat_state.live_thinking_level = thinking_level
                changed = True

        if "live_connection_mode" in body:
            connection_mode = body["live_connection_mode"]
            valid_mode_ids = _LIVE_CONNECTION_MODE_IDS
            if connection_mode in valid_mode_ids:
                chat_state.live_connection_mode = connection_mode
                changed = True

        if changed:
            await update_user_chat(user_id, chat_state)
            return jsonify({"ok": True, "live_settings": _serialize_live_settings(chat_state)})

        return jsonify({"ok": True, "note": "no_changes", "live_settings": _serialize_live_settings(chat_state)})
    except Exception as e:
        logger.error("Mini App update live settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Long Read Reader ──────────────────────────────────────────────────────────


@miniapp_blueprint.route("/reader")
@rate_limit_reader
async def reader_page():
    """Serve the Long Read reader (Server-Side Rendered).

    Query params:
        id (str): Opaque UUID identifying the stored message.

    This endpoint is intentionally public (no auth) — content is accessed by
    opaque UUID, so there is nothing to enumerate without the original link.

    SSR flow:
        1. Fetch markdown from Redis by UID.
        2. If Redis miss → try to pull content from Telegraph (cold storage).
        3. Render Markdown → HTML and extract TOC on the server.
        4. Inject pre-rendered HTML + TOC JSON into the Jinja2 template so the
           client renders instantly without a second fetch round-trip.
    """
    import re as _re

    from quart import render_template
    from quart import request as _req

    uid = _req.args.get("id", "")

    # ── 1. Validate UID
    if uid and not _re.match(r"^[0-9a-f-]{36}$", uid, _re.IGNORECASE):
        uid = ""  # treat as missing rather than raising

    body_html = ""
    toc_json = "[]"
    telegraph_fallback_url = ""
    source_label = ""

    if uid:
        try:
            from app.cache import get_long_message, get_telegraph_url
            from app.utils.reader_utils import extract_toc, markdown_to_reader_html

            markdown = await get_long_message(uid)

            if markdown:
                # ── 2. Redis hit — SSR
                toc = extract_toc(markdown)
                body_html = markdown_to_reader_html(markdown, toc)
                toc_json = json.dumps(toc, ensure_ascii=False)
                source_label = "redis"
            else:
                # ── 3. Redis miss — try Telegraph cold-storage
                tg_url = await get_telegraph_url(uid)
                if tg_url:
                    pulled = await _fetch_telegraph_content(tg_url)
                    if pulled:
                        toc = extract_toc(pulled)
                        body_html = markdown_to_reader_html(pulled, toc)
                        toc_json = json.dumps(toc, ensure_ascii=False)
                        source_label = "telegraph"
                    else:
                        # Could fetch URL but could not parse content → redirect
                        telegraph_fallback_url = tg_url

        except Exception as _e:
            logger.warning("SSR reader render failed uid=%s: %s", uid, _e)

    return await render_template(
        "reader.html",
        body_html=body_html,
        toc_json=toc_json,
        telegraph_fallback_url=telegraph_fallback_url,
        uid=uid,
        source_label=source_label,
    )


async def _fetch_telegraph_content(tg_url: str) -> str | None:
    """Fetch a Telegraph page and extract its text content.

    Used as cold-storage fallback when Redis TTL has expired.
    Returns plain text suitable for re-rendering through our Reader, or None
    on network/parse failure.

    Args:
        tg_url: Full ``https://telegra.ph/...`` URL of the page.

    Returns:
        Extracted text, or None if the fetch or parse failed.
    """
    import httpx

    from app.utils.reader_utils import extract_text_from_telegraph_html
    from app.utils.telegraph import is_safe_telegraph_url

    if not is_safe_telegraph_url(tg_url):
        logger.warning("Telegraph reverse-proxy rejected an invalid URL")
        return None

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.get(tg_url)
            resp.raise_for_status()
            page_html = resp.text

        # Extract the <article> body from the Telegraph page HTML
        import re as _re

        article_match = _re.search(r"<article[^>]*>(.*?)</article>", page_html, _re.DOTALL)
        if not article_match:
            logger.warning("No <article> tag found in Telegraph page")
            return None

        return extract_text_from_telegraph_html(article_match.group(1))

    except Exception as exc:
        logger.warning("Telegraph reverse-proxy fetch failed (error_type=%s)", type(exc).__name__)
        return None


@miniapp_blueprint.route("/api/reader/<uid>")
@rate_limit_reader
async def api_reader_content(uid: str):
    """Return the stored long message content for a given UID.

    Kept for backward-compatibility with clients that still perform an explicit
    XHR fetch (e.g., older cached versions of ``reader.html``).  New visits use
    the SSR path built directly into ``/reader``.

    Response schema (one of):
      {"markdown": "<full text>"}                         — Redis hit, fresh content
      {"telegraph_url": "https://telegra.ph/..."}         — Redis miss, cold fallback URL
      {"error": "not_found"}  HTTP 404                    — nothing available
    """
    import re

    # Basic UUID validation to prevent Redis key injection
    if not re.match(r"^[0-9a-f-]{36}$", uid, re.IGNORECASE):
        return jsonify({"error": "invalid_id"}), 400

    try:
        from app.cache import get_long_message, get_telegraph_url

        markdown = await get_long_message(uid)
        if markdown:
            return jsonify({"markdown": markdown})

        # Primary key expired — check for Telegraph fallback
        tg_url = await get_telegraph_url(uid)
        if tg_url:
            return jsonify({"telegraph_url": tg_url})

        return jsonify({"error": "not_found"}), 404

    except Exception as e:
        logger.error("Long read API error uid=%s: %s", uid, e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Knowledge Graph Visualization API ────────────────────────────────────────


@miniapp_blueprint.route("/api/graph")
@require_webapp_auth
@require_authorized_webapp_user
async def api_graph_data(user_id: int):
    """Return the user's knowledge graph nodes and edges for visualization.

    Query params:
        limit (int): max nodes to return (default 50, max 200)
        query (str): optional search filter on entity names

    Returns JSON:
        {
            "nodes": [{"id": int, "name": str, "type": str, "description": str}],
            "edges": [{"source": int, "target": int, "predicate": str, "weight": float, "is_core": bool}],
        }
    """
    from app.repos.memory_consent import private_data_lease, resolve_current_epoch

    expected_epoch = await resolve_current_epoch(user_id, require_ltm=True)
    if expected_epoch is None:
        return jsonify({"nodes": [], "edges": []})

    async with private_data_lease(
        user_id,
        expected_epoch,
        purpose="ltm:miniapp-graph",
        require_ltm=True,
    ) as lease_current:
        if not lease_current:
            return jsonify({"nodes": [], "edges": []})
        return await _api_graph_data_leased(user_id, expected_epoch)


async def _api_graph_data_leased(user_id: int, expected_epoch: int):
    """Build one exact-generation graph snapshot while its lease is live."""
    try:
        from app import database

        limit = min(request.args.get("limit", 50, type=int), 200)
        query_filter = request.args.get("query", "").strip()

        async with database.db_manager.pool.acquire() as conn, conn.transaction():
            await database.set_user_context(user_id, False, conn=conn)
            consent_rows = await database.db_query(
                """
                SELECT ltm_enabled
                FROM chats
                WHERE user_id = $1
                  AND memory_epoch = $2
                  AND private_data_blocked IS FALSE
                FOR SHARE
                """,
                (user_id, expected_epoch),
                conn=conn,
            )
            if not consent_rows or consent_rows[0]["ltm_enabled"] is not True:
                return jsonify({"nodes": [], "edges": []})

            # Fetch nodes and their connecting edges from one tenant-scoped snapshot.
            if query_filter:
                nodes_rows = await database.db_query(
                    """
                    WITH live_node_sources AS (
                        SELECT source.*
                        FROM memory_node_sources AS source
                        JOIN long_term_memory AS memory
                          ON memory.id = source.memory_id
                         AND memory.user_id = source.user_id
                        WHERE source.user_id = $1
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), live_attributes AS (
                        SELECT DISTINCT ON (source.node_id)
                               source.node_id, source.entity_type, source.description
                        FROM live_node_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        ORDER BY source.node_id, source.created_at DESC, source.memory_id DESC
                    )
                    SELECT node.id, node.entity_name,
                           COALESCE(attributes.entity_type, 'concept') AS entity_type,
                           COALESCE(attributes.description, '') AS description
                    FROM memory_nodes AS node
                    LEFT JOIN live_attributes AS attributes ON attributes.node_id = node.id
                    WHERE node.user_id = $1
                      AND node.entity_name ILIKE $2
                      AND EXISTS (
                          SELECT 1 FROM live_node_sources AS source
                          WHERE source.node_id = node.id
                      )
                    ORDER BY node.updated_at DESC
                    LIMIT $3
                    """,
                    (user_id, f"%{query_filter}%", limit),
                    conn=conn,
                )
            else:
                nodes_rows = await database.db_query(
                    """
                    WITH live_node_sources AS (
                        SELECT source.*
                        FROM memory_node_sources AS source
                        JOIN long_term_memory AS memory
                          ON memory.id = source.memory_id
                         AND memory.user_id = source.user_id
                        WHERE source.user_id = $1
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), live_attributes AS (
                        SELECT DISTINCT ON (source.node_id)
                               source.node_id, source.entity_type, source.description
                        FROM live_node_sources AS source
                        WHERE source.attributes_complete IS TRUE
                        ORDER BY source.node_id, source.created_at DESC, source.memory_id DESC
                    )
                    SELECT node.id, node.entity_name,
                           COALESCE(attributes.entity_type, 'concept') AS entity_type,
                           COALESCE(attributes.description, '') AS description
                    FROM memory_nodes AS node
                    LEFT JOIN live_attributes AS attributes ON attributes.node_id = node.id
                    WHERE node.user_id = $1
                      AND EXISTS (
                          SELECT 1 FROM live_node_sources AS source
                          WHERE source.node_id = node.id
                      )
                    ORDER BY node.updated_at DESC
                    LIMIT $2
                    """,
                    (user_id, limit),
                    conn=conn,
                )

            node_ids = {r["id"] for r in nodes_rows}
            nodes = [
                {
                    "id": r["id"],
                    "name": r["entity_name"],
                    "type": r["entity_type"],
                    "description": r.get("description", ""),
                }
                for r in nodes_rows
            ]

            if node_ids:
                id_list = list(node_ids)
                edges_rows = await database.db_query(
                    """
                    WITH live_edge_sources AS (
                        SELECT source.*
                        FROM memory_edge_sources AS source
                        JOIN long_term_memory AS memory
                          ON memory.id = source.memory_id
                         AND memory.user_id = source.user_id
                        WHERE source.user_id = $1
                          AND source.attributes_complete IS TRUE
                          AND (memory.expires_at IS NULL OR memory.expires_at > now())
                    ), aggregate_attributes AS (
                        SELECT source.edge_id, MAX(source.weight) AS weight,
                               BOOL_OR(source.is_core) AS is_core
                        FROM live_edge_sources AS source
                        GROUP BY source.edge_id
                    ), winning_predicate AS (
                        SELECT DISTINCT ON (source.edge_id)
                               source.edge_id, source.predicate
                        FROM live_edge_sources AS source
                        ORDER BY source.edge_id, source.created_at DESC, source.memory_id DESC
                    )
                    SELECT edge.source_node, edge.target_node, winner.predicate,
                           aggregate.weight, aggregate.is_core
                    FROM memory_edges AS edge
                    JOIN aggregate_attributes AS aggregate ON aggregate.edge_id = edge.id
                    JOIN winning_predicate AS winner ON winner.edge_id = edge.id
                    WHERE edge.user_id = $1
                      AND edge.source_node = ANY($2::bigint[])
                      AND edge.target_node = ANY($2::bigint[])
                      AND edge.valid_to IS NULL
                    ORDER BY aggregate.weight DESC
                    LIMIT 500
                    """,
                    (user_id, id_list),
                    conn=conn,
                )
                edges = [
                    {
                        "source": r["source_node"],
                        "target": r["target_node"],
                        "predicate": r["predicate"],
                        "weight": float(r["weight"]),
                        "is_core": r["is_core"],
                    }
                    for r in edges_rows
                ]
            else:
                edges = []

        return jsonify({"nodes": nodes, "edges": edges})

    except Exception as e:
        logger.error("Mini App graph API error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/game")
async def game_page():
    """Serve the Crocodile Mini App HTML shell."""
    from quart import render_template
    from quart import request as _req

    game_id = _req.args.get("game_id") or _req.args.get("tgWebAppStartParam") or _req.args.get("id") or ""
    mode = _req.args.get("mode") or ("daily" if game_id == "daily" else "classic")
    if game_id in {"daily2048", "2048"} or mode in {"daily2048", "2048"}:
        return await render_template("daily_2048.html")
    if game_id in {"dailytrivia", "trivia"} or mode in {"dailytrivia", "trivia"}:
        from app.bot_instance import get_bot as _get_bot

        _bot = _get_bot()
        _bot_username = getattr(_bot, "username", "") if _bot else ""
        return await render_template("daily_trivia.html", bot_username=_bot_username)
    return await render_template("crocodile.html", game_id=game_id, mode=mode)


@miniapp_blueprint.route("/daily2048")
async def daily2048_page():
    """Serve the Daily 2048 Sprint Mini App HTML shell."""
    from quart import render_template

    return await render_template("daily_2048.html")


@miniapp_blueprint.route("/dailytrivia")
async def dailytrivia_page():
    """Serve the Daily Trivia Mini App HTML shell."""
    from quart import render_template

    from app.bot_instance import get_bot

    bot = get_bot()
    bot_username = getattr(bot, "username", "") if bot else ""
    return await render_template("daily_trivia.html", bot_username=bot_username)


@miniapp_blueprint.route("/api/miniapp/trivia/today", methods=["GET"])
async def api_miniapp_trivia_today():
    from quart import jsonify, request

    from app.games.daily_trivia import prepare_daily_puzzle
    from app.repos.crocodile_daily import today_puzzle_date
    from app.repos.daily_trivia import (
        get_puzzle_revision,
        get_result_if_exists,
        get_super_result_if_exists,
    )

    today = today_puzzle_date()

    user_result = None
    user_super_result = None
    result = None
    super_res = None
    uid, auth_error = await _resolve_authorized_legacy_miniapp_user(
        request.headers.get("X-TG-INIT-DATA", "")
    )
    if auth_error is not None:
        return auth_error
    if uid > 0:
        result = await get_result_if_exists(uid, today)
        if result is not None and result.status == "completed":
            user_result = {
                "status": "completed",
                "final_score": result.final_score,
                "correct_count": result.correct_count,
                "elapsed_ms": result.elapsed_ms,
                "answers": result.answers or [],
                "super_delta": result.super_delta,
                "super_correct": result.super_correct,
            }
        super_res = await get_super_result_if_exists(uid, today)
        if super_res is not None and super_res.status == "completed":
            user_super_result = {
                "status": "completed",
                "delta_score": super_res.delta_score,
                "correct_count": super_res.correct_count,
                "elapsed_ms": super_res.elapsed_ms,
                "answers": super_res.answers or [],
            }

    pinned_revision_id = (
        result.puzzle_revision_id
        if result is not None and result.puzzle_revision_id is not None
        else super_res.puzzle_revision_id
        if super_res is not None and super_res.puzzle_revision_id is not None
        else None
    )
    puzzle = await get_puzzle_revision(pinned_revision_id) if pinned_revision_id else None
    if puzzle is None:
        puzzle = await prepare_daily_puzzle(today)

    return jsonify(
        {
            "date": puzzle.puzzle_date.isoformat(),
            "revision_id": puzzle.published_revision_id,
            "questions": [
                {
                    "id": q.id,
                    "topic": q.topic,
                    "question": q.question,
                    "options": q.options,
                    "correct_index": q.correct_index,
                    "explanation": q.explanation,
                }
                for q in puzzle.questions
            ],
            "super_questions": [
                {
                    "id": q.id,
                    "topic": q.topic,
                    "question": q.question,
                    "options": q.options,
                    "correct_index": q.correct_index,
                    "explanation": q.explanation,
                }
                for q in puzzle.super_questions
            ],
            "user_result": user_result,
            "user_super_result": user_super_result,
        }
    )


@miniapp_blueprint.route("/api/miniapp/trivia/submit_answer", methods=["POST"])
async def api_miniapp_trivia_submit_answer():
    from quart import jsonify, request

    from app.repos.crocodile_daily import today_puzzle_date
    from app.repos.daily_trivia import get_or_create_result, update_result_answer

    data = await request.get_json() or {}
    q_idx = int(data.get("question_index", 0))
    selected_idx = int(data.get("selected_index", 0))
    is_correct = bool(data.get("is_correct", False))
    elapsed_ms = int(data.get("elapsed_ms", 0))
    total_score = int(data.get("total_score", 0))
    puzzle_revision_id = int(data["revision_id"]) if data.get("revision_id") is not None else None

    user_id, auth_error = await _resolve_authorized_legacy_miniapp_user(
        request.headers.get("X-TG-INIT-DATA", "")
    )
    if auth_error is not None:
        return auth_error

    if user_id > 0:
        today = today_puzzle_date()
        result = await get_or_create_result(
            user_id,
            today,
            puzzle_revision_id=puzzle_revision_id,
        )

        # Guard: game already completed — ignore further submissions.
        if result.status == "completed":
            return jsonify({"success": True, "already_completed": True})

        new_answers = list(result.answers)
        new_answers.append(
            {
                "question_index": q_idx,
                "selected_index": selected_idx,
                "is_correct": is_correct,
                "elapsed_ms": elapsed_ms,
            }
        )
        correct_count = result.correct_count + (1 if is_correct else 0)
        is_finished = q_idx >= 4
        status = "completed" if is_finished else "active"

        await update_result_answer(
            user_id,
            today,
            current_question=q_idx + 1,
            correct_count=correct_count,
            final_score=total_score,
            elapsed_ms=result.elapsed_ms + elapsed_ms,
            answers=new_answers,
            status=status,
            finished=is_finished,
        )

        if is_finished:
            try:
                from app.bot_instance import get_bot
                from app.games.daily_trivia_telegram import send_trivia_result_message
                bot = get_bot()
                if bot:
                    await send_trivia_result_message(bot, user_id, today)
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "trivia: result message failed user=%s: %s", user_id, exc
                )

    return jsonify({"success": True})


@miniapp_blueprint.route("/api/miniapp/trivia/submit_super_answer", methods=["POST"])
async def api_miniapp_trivia_submit_super_answer():
    from quart import jsonify, request

    from app.repos.crocodile_daily import today_puzzle_date
    from app.repos.daily_trivia import (
        get_or_create_super_result,
        get_result_if_exists,
        update_super_result_answer,
    )

    data = await request.get_json() or {}
    q_idx = int(data.get("question_index", 0))
    selected_idx = int(data.get("selected_index", 0))
    is_correct = bool(data.get("is_correct", False))
    elapsed_ms = int(data.get("elapsed_ms", 0))
    base_question_score = int(data.get("base_score", 0))
    puzzle_revision_id = int(data["revision_id"]) if data.get("revision_id") is not None else None

    user_id, auth_error = await _resolve_authorized_legacy_miniapp_user(
        request.headers.get("X-TG-INIT-DATA", "")
    )
    if auth_error is not None:
        return auth_error

    if user_id > 0:
        today = today_puzzle_date()
        main_result = await get_result_if_exists(user_id, today)
        if main_result is None or main_result.status != "completed":
            return jsonify({"success": False, "error": "Main game not completed"})

        super_result = await get_or_create_super_result(
            user_id,
            today,
            puzzle_revision_id=puzzle_revision_id,
        )
        if super_result.status == "completed":
            return jsonify({"success": True, "already_completed": True})

        q_delta = (base_question_score * 2) if is_correct else (-base_question_score * 2)

        new_answers = list(super_result.answers)
        new_answers.append(
            {
                "question_index": q_idx,
                "selected_index": selected_idx,
                "is_correct": is_correct,
                "elapsed_ms": elapsed_ms,
                "delta_score": q_delta,
            }
        )

        new_delta = super_result.delta_score + q_delta
        correct_count = super_result.correct_count + (1 if is_correct else 0)
        is_finished = q_idx >= 2
        status = "completed" if is_finished else "active"

        await update_super_result_answer(
            user_id,
            today,
            delta_score=new_delta,
            correct_count=correct_count,
            elapsed_ms=super_result.elapsed_ms + elapsed_ms,
            answers=new_answers,
            status=status,
            finished=is_finished,
        )

        if is_finished:
            try:
                from app.bot_instance import get_bot
                from app.games.daily_trivia_telegram import send_trivia_result_message

                bot = get_bot()
                if bot:
                    await send_trivia_result_message(bot, user_id, today)
            except Exception as exc:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "super trivia: result message failed user=%s: %s", user_id, exc
                )

    return jsonify({"success": True})


@miniapp_blueprint.route("/admin_dailycroc")
async def webapp_admin_dailycroc_page():
    """Legacy redirect -> /admin_daily#croc."""
    from quart import redirect

    return redirect("/admin_daily#croc", code=301)


@miniapp_blueprint.route("/admin_daily2048")
async def webapp_admin_daily2048_page():
    """Legacy redirect -> /admin_daily#2048."""
    from quart import redirect

    return redirect("/admin_daily#2048", code=301)


def _build_daily_word_mask(word: str) -> str:
    letters = [ch for ch in (word or "").strip() if ch.isalnum()]
    if not letters:
        return ""
    return " ".join("_" for _ in letters)


@miniapp_blueprint.websocket("/daily2048/ws")
async def daily2048_ws():
    """WebSocket endpoint for Daily 2048 Sprint."""
    from quart import websocket

    from app.games.crocodile_runtime import (
        cache_pending_action_result,
        game_mutation_lock,
        get_cached_pending_action_result,
        stamp_runtime_payload,
    )
    from app.games.daily_2048 import get_daily_state, goal_payload, process_move, process_practice_move
    from app.games.daily_2048_telegram import render_completion_event, send_daily2048_result_message
    from app.repos import daily_2048 as daily2048_repo
    from app.repos.crocodile_daily import update_timezone_if_known, update_user_display_name

    raw_init_data = websocket.args.get("initData", "")
    if not raw_init_data:
        await websocket.close(4003, "initData required")
        return

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    validated = _validate_init_data(raw_init_data, bot_token) if bot_token else None
    if validated is None:
        await websocket.close(4003, "Unauthorized")
        return

    user_id = _extract_user_id(validated)
    if not user_id:
        await websocket.close(4003, "No user in initData")
        return
    if not await _require_authorized_websocket_user(user_id):
        return

    timezone = websocket.args.get("tz", "")
    if timezone:
        try:
            await update_timezone_if_known(user_id, timezone)
        except Exception as exc:
            logger.debug("daily2048_ws: timezone update failed user=%s: %s", user_id, exc)

    try:
        tg_user = validated.get("user") or {}
        first = str(tg_user.get("first_name") or "").strip()
        last = str(tg_user.get("last_name") or "").strip()
        display_name = f"{first} {last}".strip() if last else first
        if display_name:
            await update_user_display_name(user_id, display_name)
    except Exception as exc:
        logger.debug("daily2048_ws: display_name update failed user=%s: %s", user_id, exc)

    puzzle, result = await get_daily_state(user_id)
    runtime_id = f"daily2048:{puzzle.puzzle_date}:{user_id}"
    practice_result = result if result.status in {"won", "lost"} else None
    result_message_sent = result.status == "won"

    async def _completion_payload() -> dict[str, Any]:
        try:
            return await render_completion_event(user_id, puzzle.puzzle_date)
        except Exception as exc:
            logger.warning("daily2048_ws: completion payload failed user=%s: %s", user_id, exc)
            return {
                "rank": None,
                "leaderboard": [],
                "puzzle_date": puzzle.puzzle_date.isoformat(),
                "goal": goal_payload(puzzle),
            }

    def _result_from_event(
        event: dict[str, Any],
        fallback: daily2048_repo.Daily2048Result,
        *,
        status: str = "practice",
    ) -> daily2048_repo.Daily2048Result:
        finished_at = fallback.finished_at or datetime.now(tz=UTC)

        def _get_int(key: str, default: int) -> int:
            val = event.get(key)
            return int(val) if val is not None else default

        return daily2048_repo.Daily2048Result(
            user_id=user_id,
            puzzle_date=puzzle.puzzle_date,
            status=status,
            board=event.get("board") or fallback.board,
            spawn_index=_get_int("spawn_index", fallback.spawn_index),
            moves=_get_int("moves", fallback.moves),
            merge_score=_get_int("merge_score", fallback.merge_score),
            final_score=_get_int("final_score", fallback.final_score),
            elapsed_ms=_get_int("elapsed_ms", fallback.elapsed_ms),
            started_at=fallback.started_at,
            won_at=fallback.won_at or finished_at,
            finished_at=finished_at,
            recordable=False,
        )

    def _client_elapsed_ms(payload: dict[str, Any]) -> int | None:
        try:
            val = payload.get("client_elapsed_ms")
            if val is None:
                return None
            value = int(val)
        except (TypeError, ValueError):
            return None
        return max(0, min(value, 24 * 60 * 60 * 1000))

    await websocket.send_json(
        await stamp_runtime_payload(
            runtime_id,
            {
                "event": "game_state",
                "daily2048": True,
                "puzzle_date": puzzle.puzzle_date.isoformat(),
                "board": result.board,
                "start_board": puzzle.board,
                "goal": goal_payload(puzzle),
                "moves": result.moves,
                "merge_score": result.merge_score,
                "elapsed_ms": result.elapsed_ms,
                "final_score": result.final_score,
                "status": result.status,
                "recordable": result.status == "active" and result.recordable,
                "can_practice": result.status in {"won", "lost"},
                "par_moves": puzzle.par_moves,
                "target_seconds": puzzle.target_seconds,
            },
        )
    )
    if result.status == "won":
        completion = await _completion_payload()
        await websocket.send_json(
            await stamp_runtime_payload(
                runtime_id,
                {
                    "event": "daily2048_completed",
                    "board": result.board,
                    "start_board": puzzle.board,
                    "moves": result.moves,
                    "merge_score": result.merge_score,
                    "elapsed_ms": result.elapsed_ms,
                    "final_score": result.final_score,
                    "recordable": True,
                    **completion,
                },
            )
        )

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=300.0)
            except TimeoutError:
                await websocket.close(1000, "Idle timeout")
                break

            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json({"event": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")
            client_elapsed_ms = _client_elapsed_ms(msg)

            if msg_type == "sync_elapsed":
                if practice_result is None and client_elapsed_ms is not None:
                    try:
                        async with game_mutation_lock(f"daily2048:{puzzle.puzzle_date}:{user_id}:timer"):
                            synced_result = await daily2048_repo.update_result_elapsed(
                                user_id=user_id,
                                puzzle_date=puzzle.puzzle_date,
                                elapsed_ms=client_elapsed_ms,
                            )
                            if synced_result is not None:
                                result = synced_result
                                await websocket.send_json(
                                    await stamp_runtime_payload(
                                        runtime_id,
                                        {"event": "timer_sync", "elapsed_ms": result.elapsed_ms},
                                    )
                                )
                    except Exception as exc:
                        logger.debug("daily2048_ws: elapsed sync failed user=%s: %s", user_id, exc)
                continue

            if msg_type != "move":
                continue
            direction = str(msg.get("direction") or "")
            pending_id = str(msg.get("pending_id") or "")
            if pending_id:
                cached_event = await get_cached_pending_action_result(runtime_id, pending_id)
                if cached_event is not None:
                    await websocket.send_json(cached_event)
                    continue

            try:
                async with game_mutation_lock(f"daily2048:{puzzle.puzzle_date}:{user_id}"):
                    if practice_result is not None:
                        event = await process_practice_move(practice_result, puzzle, direction)
                    else:
                        event = await process_move(
                            user_id,
                            direction,
                            client_elapsed_ms=client_elapsed_ms,
                            client_board_before=msg.get("client_board_before"),
                            client_board_after=msg.get("client_board_after"),
                        )
            except TimeoutError:
                await websocket.send_json(
                    await stamp_runtime_payload(
                        runtime_id,
                        {"event": "error", "message": "Сервер загружен, попробуйте через секунду."},
                    )
                )
                continue
            except ValueError:
                await websocket.send_json(
                    await stamp_runtime_payload(runtime_id, {"event": "error", "message": "Unknown direction"})
                )
                continue

            if event.get("daily2048_completed"):
                completion = await _completion_payload()
                event = {**event, **completion}
                practice_result = _result_from_event(event, result, status="won")
            elif event.get("game_over") and event.get("status") == "lost":
                event = {**event, "start_board": puzzle.board}
                practice_result = _result_from_event(event, result, status="lost")
            elif practice_result is not None and event.get("event") == "move_result":
                practice_result = _result_from_event(event, practice_result)

            event = await stamp_runtime_payload(runtime_id, event)
            if pending_id:
                event["pending_id"] = pending_id
                await cache_pending_action_result(runtime_id, pending_id, event)
            await websocket.send_json(event)

            if event.get("daily2048_completed") and not result_message_sent:
                try:
                    from app.bot_instance import get_bot

                    bot = get_bot()
                    if bot:
                        await send_daily2048_result_message(bot, user_id, puzzle.puzzle_date)
                    result_message_sent = True
                except Exception as exc:
                    logger.warning("daily2048_ws: result message failed user=%s: %s", user_id, exc)
    except Exception as exc:
        logger.warning("daily2048_ws: unexpected error user=%s: %s", user_id, exc)


@miniapp_blueprint.websocket("/game/daily/ws")
async def daily_game_ws():
    """WebSocket endpoint for Daily Crocodile."""
    from quart import websocket

    from app.games.crocodile_daily import (
        build_daily_completion_summary,
        get_daily_hints,
        get_daily_overview,
        history_items,
        process_daily_guess,
    )
    from app.games.crocodile_runtime import (
        cache_pending_action_result,
        game_mutation_lock,
        get_cached_pending_action_result,
        stamp_runtime_payload,
    )
    from app.repos.crocodile_daily import (
        DAILY_MAX_ATTEMPTS,
        increment_hint_count,
        normalize_daily_difficulty,
        update_timezone_if_known,
    )

    raw_init_data = websocket.args.get("initData", "")
    if not raw_init_data:
        await websocket.close(4003, "initData required")
        return

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    validated = _validate_init_data(raw_init_data, bot_token) if bot_token else None
    if validated is None:
        await websocket.close(4003, "Unauthorized")
        return

    user_id = _extract_user_id(validated)
    if not user_id:
        await websocket.close(4003, "No user in initData")
        return
    if not await _require_authorized_websocket_user(user_id):
        return

    timezone = websocket.args.get("tz", "")
    if timezone:
        try:
            await update_timezone_if_known(user_id, timezone)
        except Exception as exc:
            logger.debug("daily_game_ws: timezone update failed user=%s: %s", user_id, exc)

    # Persist display_name from Telegram initData so the leaderboard can show real names.
    try:
        from app.repos.crocodile_daily import update_user_display_name

        tg_user = validated.get("user") or {}
        first = str(tg_user.get("first_name") or "").strip()
        last = str(tg_user.get("last_name") or "").strip()
        display_name = f"{first} {last}".strip() if last else first
        if display_name:
            await update_user_display_name(user_id, display_name)
    except Exception as exc:
        logger.debug("daily_game_ws: display_name update failed user=%s: %s", user_id, exc)

    difficulty = normalize_daily_difficulty(websocket.args.get("difficulty", "easy"))
    try:
        last_seen_seq = max(0, int(websocket.args.get("last_seen_seq", "0") or 0))
    except ValueError:
        last_seen_seq = 0

    puzzle_date, puzzles, results = await get_daily_overview(user_id)
    if difficulty not in puzzles:
        difficulty = "easy"
    puzzle = puzzles[difficulty]
    result = results[difficulty]
    runtime_id = f"daily:{puzzle_date}:{difficulty}:{user_id}"
    await websocket.send_json(
        await stamp_runtime_payload(
            runtime_id,
            {
                "event": "game_state",
                "category": f"Крокодил дня · {puzzle.puzzle_date.isoformat()}",
                "lang": puzzle.lang,
                "attempts": len(result.attempts),
                "max_attempts": DAILY_MAX_ATTEMPTS,
                "daily_topic": puzzle.topic,
                "daily_word_mask": _build_daily_word_mask(puzzle.target_word) if difficulty == "easy" else "",
                "is_creator": False,
                "target_word": None,
                "daily": True,
                "difficulty": difficulty,
                "daily_modes": [
                    {
                        "difficulty": item_difficulty,
                        "label": item_difficulty.title(),
                        "status": results[item_difficulty].status,
                        "completed": results[item_difficulty].status != "active",
                    }
                    for item_difficulty in puzzles
                ],
            },
        )
    )
    history = history_items(result, after_seq=last_seen_seq)
    if history:
        await websocket.send_json(
            await stamp_runtime_payload(
                runtime_id,
                {
                    "event": "history_replay" if last_seen_seq > 0 else "history_sync",
                    "items": history,
                    "from_seq": last_seen_seq if last_seen_seq > 0 else None,
                },
            )
        )
    if result.status != "active":
        summary = await build_daily_completion_summary(user_id, puzzle.puzzle_date, focus_difficulty=difficulty)
        await websocket.send_json(
            await stamp_runtime_payload(
                runtime_id,
                {
                    "event": "daily_completed",
                    "status": result.status,
                    "difficulty": difficulty,
                    "word": puzzle.target_word,
                    "attempts": len(result.attempts),
                    "max_attempts": DAILY_MAX_ATTEMPTS,
                    "points": result.points,
                    "streak": result.streak_after,
                    "share_grid": result.share_grid,
                    "won": result.status == "won",
                    "rank": summary["modes"].get(difficulty, {}).get("rank"),
                    "leaderboard": summary["modes"].get(difficulty, {}).get("leaderboard", []),
                    "modes": list(summary["modes"].values()),
                    "next_difficulty": summary.get("next_difficulty"),
                    "focus_difficulty": summary.get("focus_difficulty"),
                },
            )
        )
        return

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=300.0)
            except TimeoutError:
                await websocket.close(1000, "Idle timeout")
                break

            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json({"event": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")
            pending_id = str(msg.get("pending_id", ""))

            if msg_type == "hint":
                hint_idx = int(msg.get("hint_index", 0))
                hints = await get_daily_hints(puzzle)
                if 0 <= hint_idx < len(hints):
                    used = await increment_hint_count(user_id, puzzle.puzzle_date, difficulty=difficulty)
                    await websocket.send_json(
                        await stamp_runtime_payload(
                            runtime_id,
                            {
                                "event": "hint",
                                "difficulty": difficulty,
                                "text": hints[hint_idx],
                                "hint_index": hint_idx,
                                "used_hints_count": used,
                                "available": True,
                            },
                        )
                    )
                else:
                    await websocket.send_json(
                        await stamp_runtime_payload(
                            runtime_id,
                            {
                                "event": "hint",
                                "difficulty": difficulty,
                                "text": "⏳ Подсказки ещё готовятся или закончились...",
                                "available": False,
                            },
                        )
                    )
                continue

            if msg_type != "guess":
                continue

            word = str(msg.get("word", "")).strip()
            if not word:
                await websocket.send_json(
                    await stamp_runtime_payload(runtime_id, {"event": "error", "message": "Empty guess"})
                )
                continue

            if pending_id:
                cached_event = await get_cached_pending_action_result(runtime_id, pending_id)
                if cached_event is not None:
                    await websocket.send_json(cached_event)
                    continue

            try:
                async with game_mutation_lock(f"daily:{puzzle.puzzle_date}:{difficulty}:{user_id}"):
                    _, current_puzzles, current_results = await get_daily_overview(user_id)
                    puzzle = current_puzzles.get(difficulty, puzzle)
                    before = current_results.get(difficulty, result)
                    was_active = before.status == "active"
                    event = await process_daily_guess(user_id, word, difficulty=difficulty)
            except TimeoutError:
                logger.warning("daily_game_ws: mutation lock timeout user=%s", user_id)
                await websocket.send_json(
                    await stamp_runtime_payload(
                        runtime_id,
                        {
                            "event": "error",
                            "message": "Сервер загружен, попробуйте через секунду.",
                        },
                    )
                )
                continue

            event = await stamp_runtime_payload(runtime_id, event)
            if pending_id:
                event["pending_id"] = pending_id
                await cache_pending_action_result(runtime_id, pending_id, event)
            await websocket.send_json(event)

            if was_active and event.get("daily_completed"):
                try:
                    from app.bot_instance import get_bot
                    from app.games.crocodile_daily_telegram import (
                        queue_daily_result_refresh,
                        send_daily_completion_bundle,
                    )

                    bot = get_bot()
                    if bot:
                        await send_daily_completion_bundle(
                            bot,
                            user_id,
                            puzzle.puzzle_date,
                            focus_difficulty=difficulty,
                        )
                        queue_daily_result_refresh(bot, puzzle.puzzle_date)
                except Exception as exc:
                    logger.warning("daily_game_ws: result message failed user=%s: %s", user_id, exc)
                break
    except Exception as exc:
        logger.warning("daily_game_ws: unexpected error user=%s: %s", user_id, exc)


@miniapp_blueprint.websocket("/game/ws")
async def game_ws():
    """WebSocket endpoint for the Crocodile game.

    Auth: initData passed as query param ``initData`` (HMAC-SHA256).
    Protocol:
      Client → {"type": "guess", "word": "..."}
      Server → {"event": "game_state", ...}
               {"event": "result", "status": ..., "hint": ..., ...}
               {"event": "game_over", "word": ..., ...}
    """
    import uuid

    from quart import websocket

    from app.games.crocodile import get_game_hints, get_game_history, load_game
    from app.games.crocodile_runtime import (
        cache_pending_action_result,
        game_mutation_lock,
        get_cached_pending_action_result,
        get_runtime_hints,
        get_runtime_history,
        get_runtime_replay,
        open_game_event_subscription,
        publish_runtime_event,
        stamp_runtime_payload,
    )

    raw_init_data = websocket.args.get("initData", "")
    if not raw_init_data:
        # Require Telegram initData — reject unauthenticated connections.
        # External browser links should embed initData (Telegram passes it
        # automatically when the WebApp is opened from the bot).
        await websocket.close(4003, "initData required")
        return

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    validated = _validate_init_data(raw_init_data, bot_token) if bot_token else None
    if validated is None:
        await websocket.close(4003, "Unauthorized")
        return

    user_id = _extract_user_id(validated)
    if not user_id:
        await websocket.close(4003, "No user in initData")
        return
    if not await _require_authorized_websocket_user(user_id):
        return

    # ── Resolve game ───────────────────────────────────────────────────────
    game_id = websocket.args.get("game_id", "")
    if not game_id:
        await websocket.close(4400, "Missing game_id")
        return

    try:
        last_seen_seq = max(0, int(websocket.args.get("last_seen_seq", "0") or 0))
    except ValueError:
        last_seen_seq = 0

    game = await load_game(game_id)
    if game is None:
        await websocket.close(4008, "Game expired or not found")
        return

    if game.status != "active":
        # The creator (word-giver) may reopen the miniapp after the game ends and
        # still needs to see the result / chat history. Everyone else gets a clean
        # 4009 close so they cannot interact with a dead game.
        if user_id != game.creator_id:
            await websocket.close(4009, "Game already finished")
            return
        # Creator reconnect: send final state + history then close gracefully.
        await websocket.send_json(
            await stamp_runtime_payload(
                game_id,
                {
                    "event": "game_state",
                    "category": game.category,
                    "lang": game.lang,
                    "attempts": len(game.attempts),
                    "max_attempts": game.max_attempts,
                    "is_creator": True,
                    "target_word": game.target_word,
                    "finished": True,
                    "status": str(game.status),
                },
            )
        )
        _creator_history = await get_runtime_history(game_id)
        if not _creator_history:
            _creator_history = get_game_history(game_id)
        if _creator_history:
            await websocket.send_json(
                await stamp_runtime_payload(
                    game_id,
                    {"event": "history_sync", "items": _creator_history, "from_seq": None},
                )
            )
        await websocket.close(1000, "Game over")
        return

    # Register guesser on first connect (not the creator)
    if game.guesser_id is None and user_id != game.creator_id:
        game.guesser_id = user_id
        await game.save()
        try:
            from app.repos.crocodile_daily import record_player_activity

            await record_player_activity(user_id, event="classic_played")
        except Exception as exc:
            logger.debug("game_ws: activity record failed user=%s: %s", user_id, exc)

    # Send initial game state
    is_creator = user_id == game.creator_id
    await websocket.send_json(
        await stamp_runtime_payload(
            game_id,
            {
                "event": "game_state",
                "category": game.category,
                "lang": game.lang,
                "attempts": len(game.attempts),
                "max_attempts": game.max_attempts,
                "is_creator": is_creator,
                "target_word": game.target_word if is_creator else None,
            },
        )
    )

    # Restore chat history so reconnecting players keep their progress visible
    history = await get_runtime_history(game_id)
    if not history:
        history = get_game_history(game_id)
    if history:
        replay_items = await get_runtime_replay(game_id, after_seq=last_seen_seq) if last_seen_seq > 0 else history
        if replay_items:
            history_event = "history_replay" if last_seen_seq > 0 else "history_sync"
            await websocket.send_json(
                await stamp_runtime_payload(
                    game_id,
                    {
                        "event": history_event,
                        "items": replay_items,
                        "from_seq": last_seen_seq if last_seen_seq > 0 else None,
                    },
                )
            )

    # Subscribe this socket to the game's PubSub broadcast queue
    subscriber_id = uuid.uuid4().hex
    subscription = await open_game_event_subscription(game_id, subscriber_id)

    # ── Drain task: forward broadcast events to this WebSocket connection ──
    # Runs concurrently with the receive loop so broadcasts are never blocked
    # by waiting for the next incoming message.
    drain_task: asyncio.Task | None = None

    async def _drain_broadcasts() -> None:
        try:
            while True:
                payload = await subscription.get()
                try:
                    await websocket.send_json(payload)
                except Exception:
                    break  # Socket closed — exit silently
        except asyncio.CancelledError:
            pass

    drain_task = asyncio.create_task(_drain_broadcasts())

    # ── Main message loop ──────────────────────────────────────────────────
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=300.0)
            except TimeoutError:
                # Connection idle for 5 minutes — close gracefully
                await websocket.close(1000, "Idle timeout")
                break

            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await websocket.send_json(
                    await stamp_runtime_payload(game_id, {"event": "error", "message": "Invalid JSON"})
                )
                continue

            msg_type = msg.get("type")
            pending_id = str(msg.get("pending_id", ""))

            # ── Hint request ──────────────────────────────────────────────
            if msg_type == "hint":
                hint_idx = int(msg.get("hint_index", 0))
                hints = await get_runtime_hints(game_id)
                if not hints:
                    hints = get_game_hints(game_id)
                if hint_idx < len(hints):
                    await websocket.send_json(
                        await stamp_runtime_payload(
                            game_id,
                            {
                                "event": "hint",
                                "text": hints[hint_idx],
                                "hint_index": hint_idx,
                                "available": True,
                            },
                        )
                    )
                else:
                    await websocket.send_json(
                        await stamp_runtime_payload(
                            game_id,
                            {
                                "event": "hint",
                                "text": "⏳ Подсказки ещё готовятся или закончились...",
                                "available": False,
                            },
                        )
                    )
                continue

            # ── Creator-only: reaction —————————————————————————————————————
            # Creators can send emoji reactions which are broadcast to the guesser.
            if msg_type == "reaction" and is_creator:
                emoji = str(msg.get("emoji", "")).strip()
                if emoji:
                    await publish_runtime_event(
                        game_id,
                        {
                            "event": "reaction",
                            "emoji": emoji,
                        },
                        exclude_subscriber_id=subscriber_id,
                    )
                continue

            # ── Creator-only: typing indicator from creator side ──────────
            # (Guesser typing is handled in the guess flow below)
            if msg_type == "typing_status" and is_creator:
                await publish_runtime_event(
                    game_id,
                    {
                        "event": "creator_typing",
                        "active": bool(msg.get("active", False)),
                    },
                    exclude_subscriber_id=subscriber_id,
                )
                continue

            # ── Guesser typing indicator (ephemeral, not recorded) ─────────
            if msg_type == "typing":
                if not is_creator:
                    await publish_runtime_event(
                        game_id,
                        {
                            "event": "guesser_typing",
                            "active": bool(msg.get("active", False)),
                        },
                        exclude_subscriber_id=subscriber_id,
                    )
                continue

            if msg_type != "guess":
                continue

            word = str(msg.get("word", "")).strip()
            if not word:
                await websocket.send_json(
                    await stamp_runtime_payload(game_id, {"event": "error", "message": "Empty guess"})
                )
                continue

            if is_creator:
                await websocket.send_json(
                    await stamp_runtime_payload(
                        game_id,
                        {"event": "error", "message": "Создатель игры не может отгадывать свои слова."},
                    )
                )
                continue

            if pending_id:
                cached_event = await get_cached_pending_action_result(game_id, pending_id)
                if cached_event is not None:
                    await websocket.send_json(cached_event)
                    continue

            try:
                from app.repos.crocodile_daily import record_player_activity

                await record_player_activity(user_id, event="classic_played")
            except Exception as exc:
                logger.debug("game_ws: guess activity record failed user=%s: %s", user_id, exc)

            try:
                async with game_mutation_lock(game_id):
                    # Reload game state from Redis (another tab may have mutated it)
                    game = await load_game(game_id) or game
                    if game.status != "active":
                        break

                    event = await game.process_guess(word)
            except TimeoutError:
                logger.warning("game_ws: mutation lock timeout game=%s", game_id)
                await websocket.send_json(
                    await stamp_runtime_payload(
                        game_id,
                        {
                            "event": "error",
                            "message": "Сервер загружен, попробуйте через секунду.",
                        },
                    )
                )
                continue

            # Echo pending_id back so the client can resolve its optimistic bubble
            if pending_id:
                event["pending_id"] = pending_id
            event = await stamp_runtime_payload(game_id, event)
            if pending_id:
                await cache_pending_action_result(game_id, pending_id, event)

            await websocket.send_json(event)

            # Broadcast the result to all other subscribers (spectators / creator)
            broadcast_payload = {
                "event": "spectator_result",
                "word": word,
                "status": event.get("status"),
                "score": event.get("score"),
                "hint": event.get("hint"),
                "seq": event.get("seq"),
                "server_time_ms": event.get("server_time_ms"),
            }
            if event.get("event") in ("game_over",):
                broadcast_payload["event"] = "spectator_game_over"
                broadcast_payload["word"] = event.get("word", word)
            elif event.get("status") == "exact_match":
                broadcast_payload["event"] = "spectator_win"
                broadcast_payload["word"] = event.get("word", word)
            await publish_runtime_event(game_id, broadcast_payload, exclude_subscriber_id=subscriber_id)

            if event.get("best_score_updated") and game.status == "active":
                try:
                    from app.bot_instance import get_bot
                    from app.games.crocodile_telegram import CrocodileTelegramService

                    bot = get_bot()
                    if bot:
                        CrocodileTelegramService.queue_thermometer_update(bot, game)
                except Exception as exc:
                    logger.debug("game_ws: thermometer queue failed game=%s: %s", game_id, exc)

            # Finalize if game ended (won / lost)
            if game.status in ("won", "lost"):
                try:
                    from app.bot_instance import get_bot

                    bot = get_bot()
                    if bot:
                        await game.finalize(bot)
                except Exception as exc:
                    logger.warning("game_ws: finalize failed game=%s: %s", game_id, exc)
                break

    except Exception as exc:
        logger.warning("game_ws: unexpected error game=%s: %s", game_id, exc)
    finally:
        if drain_task and not drain_task.done():
            drain_task.cancel()
        await subscription.close()


# ── Live Audio (Gemini Live API WebSocket proxy) ─────────────────────────────


@miniapp_blueprint.route("/live")
async def live_audio_page():
    """Serve the Live Audio Mini App HTML shell."""
    from quart import render_template

    return await render_template("live_audio.html")


async def _open_authenticated_live_socket(route_mode: str) -> None:
    from quart import websocket

    raw_init_data = websocket.args.get("initData", "")
    if not raw_init_data:
        await websocket.close(4003, "initData required")
        return

    resumption_token = websocket.args.get("resumptionToken", "")

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    validated = _validate_init_data(raw_init_data, bot_token) if bot_token else None
    if validated is None:
        await websocket.close(4003, "Unauthorized")
        return

    user_id = _extract_user_id(validated)
    if not user_id:
        await websocket.close(4003, "No user in initData")
        return
    if not await _require_authorized_websocket_user(user_id):
        return

    from app.cache import redis_client

    has_active_session = False
    if redis_client:
        # Atomically check and set the active session flag (15-min TTL safety net)
        has_active_session = not await redis_client.set(
            f"live_session:{user_id}",
            "1",
            nx=True,
            ex=900,
        )

    if has_active_session:
        await websocket.close(4009, "User already has an active session")
        return

    try:
        await _handle_live_session(websocket, user_id, validated, resumption_token, transport_mode=route_mode)
    finally:
        if redis_client:
            await redis_client.delete(f"live_session:{user_id}")


@miniapp_blueprint.websocket("/live/ws")
async def live_audio_ws() -> None:
    """WebSocket proxy: browser ↔ Gemini Live API bidirectional audio stream.

    Auth: initData passed as query param ``initData`` (HMAC-SHA256).
    Protocol:
      Client → {"type": "realtime_input", "mime_type": "audio/pcm;rate=16000", "data": "<base64>"}
               {"type": "audio_stream_end"}
      Server → {"type": "audio", "data": "<base64 PCM 24kHz>"}
               {"type": "input_transcript", "text": "..."}
               {"type": "output_transcript", "text": "..."}
               {"type": "interrupt"}
               {"type": "session_resumed"}
               {"type": "error", "message": "..."}
    """
    await _open_authenticated_live_socket(_LIVE_DEFAULT_CONNECTION_MODE)


@miniapp_blueprint.websocket("/live-vertex/ws")
async def live_vertex_audio_ws() -> None:
    """Experimental WebSocket proxy: browser ↔ Vertex Live API with Search grounding."""
    await _open_authenticated_live_socket(_LIVE_VERTEX_CONNECTION_MODE)


def _build_live_system_instruction(
    chat_state,
    *,
    user_first_name: str,
    user_language: str,
    transport_mode: str,
) -> str:
    sys_parts = []
    if chat_state and chat_state.system_prompt:
        sys_parts.append(chat_state.system_prompt)
    else:
        sys_parts.extend(
            [
                "Ты — дружелюбный AI-ассистент в Telegram боте.",
                "Отвечай кратко и по делу. Если не уверен — скажи об этом.",
                "По умолчанию отвечай по-русски, если пользователь явно не просит другой язык.",
                "Если пользователь пишет или говорит на другом языке, либо прямо просит сменить язык, сразу переключайся на этот язык.",
            ]
        )

    if user_first_name:
        sys_parts.append(f"Имя пользователя: {user_first_name}.")
    if user_language:
        sys_parts.append(
            f"Код языка интерфейса Telegram пользователя: {user_language}. "
            "Используй это только как вспомогательный сигнал; язык ответа определяй по последней реплике пользователя, а по умолчанию используй русский."
        )
    if transport_mode == _LIVE_VERTEX_CONNECTION_MODE:
        sys_parts.extend(
            [
                "В этой live-сессии у тебя есть доступ к Google Search.",
                "Для погоды, новостей, курсов, времени, расписаний, текущих событий и любых других меняющихся данных обязательно сначала используй поиск.",
                "Не говори, что у тебя нет доступа к интернету или к свежим данным, пока инструмент поиска доступен.",
                "Если свежие данные найти не удалось, честно скажи, что не удалось получить результат поиска, а не что у тебя нет доступа к интернету.",
                "Если географическое название неоднозначно, коротко уточни страну или регион, прежде чем давать ответ по текущим данным.",
            ]
        )
    return " ".join(sys_parts)


async def _resolve_live_transport(
    *,
    transport_mode: str,
    system_instruction: str,
    resumption_handle: str | None,
    voice_name: str,
    thinking_level: str,
):
    from app.config import GEMINI_LIVE_MODEL
    from app.providers.gemini import get_live_api_client, get_vertex_live_client

    if transport_mode == _LIVE_VERTEX_CONNECTION_MODE:
        client = get_vertex_live_client()
        if client is None:
            return (
                None,
                _VERTEX_LIVE_MODEL,
                None,
                "misconfigured",
                (
                    "Vertex internet-live сейчас требует полноценный Vertex AI regional client "
                    "(project/location + ADC или service account, например через "
                    "GOOGLE_APPLICATION_CREDENTIALS / secret VERTEX_LIVE_SERVICE_ACCOUNT_JSON; "
                    "если используется файл credentials, он должен существовать и читаться из контейнера). "
                    "Путь через Express API key для Live API websocket-сессий не поддерживается."
                ),
            )
        return (
            client,
            _VERTEX_LIVE_MODEL,
            _build_vertex_live_connect_config(
                system_instruction=system_instruction,
                resumption_handle=resumption_handle,
                voice_name=voice_name,
            ),
            None,
            None,
        )

    client = get_live_api_client()
    if client is None:
        return (
            None,
            GEMINI_LIVE_MODEL,
            None,
            "misconfigured",
            "Голосовой режим временно недоступен: API ключи Gemini не настроены.",
        )

    cooldown_seconds = await _get_live_model_cooldown_seconds()
    if cooldown_seconds > 0:
        return None, GEMINI_LIVE_MODEL, None, "server_capacity", str(cooldown_seconds)

    return (
        client,
        GEMINI_LIVE_MODEL,
        _build_live_connect_config(
            system_instruction=system_instruction,
            resumption_handle=resumption_handle,
            voice_name=voice_name,
            thinking_level=thinking_level,
        ),
        None,
        None,
    )


async def _handle_live_session(
    websocket,
    user_id: int,
    validated: dict,
    resumption_token: str,
    *,
    transport_mode: str,
):

    # Extract display metadata from the already-validated initData user object.
    # These fields are populated from Telegram's initData, no extra DB call needed.
    _tg_user: dict = validated.get("user") or {}
    user_first_name: str = _tg_user.get("first_name", "").strip()
    user_language: str = _tg_user.get("language_code", "").strip()

    # ── Connect to Gemini Live API ────────────────────────────────────────
    from google.genai import types

    from app.games.crocodile_flags import is_live_audio_enabled
    from app.repos.chats import get_user_chat

    session_resumption_token: str | None = None
    chat_state = await get_user_chat(user_id)
    live_voice_name = _resolve_live_voice_name(chat_state)
    live_thinking_level = _resolve_live_thinking_level(chat_state)
    system_instruction = _build_live_system_instruction(
        chat_state,
        user_first_name=user_first_name,
        user_language=user_language,
        transport_mode=transport_mode,
    )

    if not await is_live_audio_enabled():
        await _send_live_fatal(
            websocket,
            reason="disabled",
            message="Голосовой режим временно отключён администратором. Продолжите текстом.",
        )
        return

    client, model_name, live_config, failure_reason, failure_detail = await _resolve_live_transport(
        transport_mode=transport_mode,
        system_instruction=system_instruction,
        resumption_handle=session_resumption_token or resumption_token,
        voice_name=live_voice_name,
        thinking_level=live_thinking_level,
    )
    if client is None or live_config is None:
        if failure_reason == "server_capacity":
            cooldown_seconds = int(failure_detail or "60")
            logger.warning(
                "live_audio_ws: model cooldown active user=%d model=%s retry_after=%ds reason=%s",
                user_id,
                model_name,
                cooldown_seconds,
                (await _get_live_model_cooldown_reason())[:160],
            )
            await _send_live_fatal(
                websocket,
                reason="server_capacity",
                message=(
                    "Голосовой режим временно недоступен со стороны Gemini Live API. "
                    "Попробуйте ещё раз чуть позже или продолжите текстом."
                ),
                retry_after_seconds=cooldown_seconds,
            )
            return
        await _send_live_fatal(
            websocket,
            reason=failure_reason or "misconfigured",
            message=failure_detail or "Голосовой режим временно недоступен.",
        )
        return

    transport_backend = "vertex_live" if transport_mode == _LIVE_VERTEX_CONNECTION_MODE else "gemini_live"
    logger.info(
        "live_audio_ws: connecting user=%d mode=%s model=%s resumption_token=%s voice=%s thinking=%s via=%s",
        user_id,
        transport_mode,
        model_name,
        bool(session_resumption_token or resumption_token),
        live_voice_name,
        live_thinking_level,
        transport_backend,
    )
    try:
        async with client.aio.live.connect(model=model_name, config=live_config) as session:
            await websocket.send_json({"type": "connected"})
            if resumption_token:
                await websocket.send_json({"type": "session_resumed"})

            turn_ready = asyncio.Event()
            producer_alive = True
            if resumption_token:
                turn_ready.set()

            # ── Producer: browser → Gemini ────────────────────────────────
            async def _producer() -> None:
                nonlocal producer_alive
                start_time = time.monotonic()
                try:
                    while True:
                        if time.monotonic() - start_time > 1800:
                            await websocket.close(1008, "Session duration limit reached (30m)")
                            return
                        try:
                            raw = await asyncio.wait_for(websocket.receive(), timeout=600.0)
                        except TimeoutError:
                            logger.info("live_audio_ws: websocket idle timeout user=%d", user_id)
                            await websocket.close(1000, "Idle timeout")
                            return

                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue

                        msg_type = msg.get("type")

                        if msg_type == "realtime_input":
                            audio_b64 = msg.get("data", "")
                            mime_type = msg.get("mime_type", "audio/pcm;rate=16000")
                            if audio_b64:
                                audio_bytes = base64.b64decode(audio_b64)
                                await session.send_realtime_input(
                                    audio=types.Blob(data=audio_bytes, mime_type=mime_type)
                                )
                        elif msg_type == "activity_start":
                            await session.send_realtime_input(
                                activity_start=types.ActivityStart(),
                            )
                        elif msg_type == "activity_end":
                            await session.send_realtime_input(
                                activity_end=types.ActivityEnd(),
                            )
                            turn_ready.set()
                        elif msg_type == "audio_stream_end":
                            await session.send_realtime_input(audio_stream_end=True)
                            turn_ready.set()
                        elif msg_type == "text":
                            text = msg.get("text", "")
                            if text:
                                await session.send_realtime_input(text=text)
                                turn_ready.set()

                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("live_audio_ws producer error user=%d: %s", user_id, exc)
                finally:
                    producer_alive = False
                    turn_ready.set()

            # ── Consumer: Gemini → browser ────────────────────────────────
            async def _consumer() -> None:
                nonlocal session_resumption_token
                try:
                    while True:
                        await turn_ready.wait()
                        turn_ready.clear()
                        if not producer_alive:
                            break

                        saw_message = False
                        turn_input_transcript: str | None = None
                        input_transcript_sent = False
                        async for response in session.receive():
                            saw_message = True
                            content = response.server_content
                            if content:
                                if content.model_turn and content.model_turn.parts:
                                    for part in content.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            audio_b64 = base64.b64encode(part.inline_data.data).decode("ascii")
                                            await websocket.send_json(
                                                {
                                                    "type": "audio",
                                                    "data": audio_b64,
                                                }
                                            )

                                if content.input_transcription and content.input_transcription.text:
                                    turn_input_transcript = content.input_transcription.text
                                if content.output_transcription:
                                    if turn_input_transcript and not input_transcript_sent:
                                        await websocket.send_json(
                                            {
                                                "type": "input_transcript",
                                                "text": turn_input_transcript,
                                            }
                                        )
                                        input_transcript_sent = True
                                    await websocket.send_json(
                                        {
                                            "type": "output_transcript",
                                            "text": content.output_transcription.text,
                                        }
                                    )

                                if content.interrupted is True:
                                    await websocket.send_json({"type": "interrupt"})

                                if content.turn_complete or content.waiting_for_input:
                                    if turn_input_transcript and not input_transcript_sent:
                                        await websocket.send_json(
                                            {
                                                "type": "input_transcript",
                                                "text": turn_input_transcript,
                                            }
                                        )
                                    await websocket.send_json({"type": "turn_complete"})
                                    break

                            if hasattr(response, "session_resumption_update"):
                                sru = response.session_resumption_update
                                if sru and hasattr(sru, "new_handle") and sru.new_handle:
                                    session_resumption_token = sru.new_handle
                                    await websocket.send_json(
                                        {
                                            "type": "resumption_token",
                                            "token": sru.new_handle,
                                        }
                                    )

                            if hasattr(response, "go_away") and response.go_away is not None:
                                time_left = response.go_away.time_left
                                seconds_left = 60.0
                                if time_left is not None:
                                    seconds_left = (
                                        time_left.total_seconds()
                                        if hasattr(time_left, "total_seconds")
                                        else float(time_left)
                                    )
                                logger.info(
                                    "live_audio_ws: GoAway received user=%d time_left=%.1fs",
                                    user_id,
                                    seconds_left,
                                )
                                await websocket.send_json(
                                    {
                                        "type": "go_away",
                                        "time_left_seconds": seconds_left,
                                    }
                                )

                        if not saw_message:
                            logger.info("live_audio_ws: receive stream ended without messages user=%d", user_id)
                            break

                    logger.info("live_audio_ws: consumer receive loop ended normally user=%d", user_id)
                except asyncio.CancelledError:
                    logger.debug("live_audio_ws: consumer cancelled user=%d", user_id)
                except Exception as exc:
                    logger.warning(
                        "live_audio_ws consumer error user=%d: %s: %s",
                        user_id,
                        type(exc).__name__,
                        exc,
                    )
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    except Exception:
                        pass

            producer_task = asyncio.create_task(_producer())
            consumer_task = asyncio.create_task(_consumer())

            try:
                done, _ = await asyncio.wait(
                    {producer_task, consumer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if producer_task in done and not consumer_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(consumer_task), timeout=5.0)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
            finally:
                for task in (producer_task, consumer_task):
                    if not task.done():
                        task.cancel()
                        try:
                            await asyncio.wait_for(task, timeout=0.25)
                        except (asyncio.CancelledError, TimeoutError):
                            pass

    except Exception as exc:
        err_str = str(exc)
        if _is_live_resource_exhausted(err_str):
            retry_after_seconds = _extract_live_retry_after_seconds(err_str) or 60
            if transport_mode == _LIVE_DEFAULT_CONNECTION_MODE:
                retry_after_seconds = await _mark_live_model_cooldown(retry_after_seconds, err_str)
            capacity_message = (
                "Экспериментальный internet-live временно недоступен. "
                "Попробуйте ещё раз чуть позже или продолжите в стандартном режиме."
                if transport_mode == _LIVE_VERTEX_CONNECTION_MODE
                else "Голосовой режим временно недоступен со стороны Gemini Live API. "
                "Попробуйте ещё раз чуть позже или продолжите текстом."
            )
            logger.warning(
                "live_audio_ws: resource exhausted user=%d mode=%s model=%s retry_after=%ds: %s",
                user_id,
                transport_mode,
                model_name,
                retry_after_seconds,
                err_str,
            )
            await _send_live_fatal(
                websocket,
                reason="server_capacity",
                message=capacity_message,
                retry_after_seconds=retry_after_seconds,
            )
        else:
            logger.error("live_audio_ws: session fatal error user=%d mode=%s: %s", user_id, transport_mode, err_str)
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "reason": "connect_failed",
                        "message": f"Live session failed: {err_str}",
                    }
                )
            except Exception:
                pass

    logger.info("live_audio_ws: disconnected user=%d mode=%s", user_id, transport_mode)
