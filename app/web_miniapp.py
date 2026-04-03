# /app/web_miniapp.py
"""Telegram Mini App backend — LTM Explorer & Settings Editor.

Endpoints are authenticated via Telegram WebApp initData (HMAC-SHA256).
Each user can only access their own data — user_id is extracted from
the validated initData payload, never from query params.

Blueprint registered on the main Quart app at prefix ``/webapp``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.parse
from functools import wraps
from typing import Any

from quart import Blueprint, jsonify, request

from app.config import settings

logger = logging.getLogger(__name__)

miniapp_blueprint = Blueprint("miniapp", __name__, template_folder="templates")


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


def require_webapp_auth(f):
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


# ── Static page ──────────────────────────────────────────────────────────────


@miniapp_blueprint.route("/")
async def miniapp_page():
    """Serve the Mini App HTML shell."""
    from quart import render_template

    return await render_template("miniapp.html")


# ── Memory API ───────────────────────────────────────────────────────────────


@miniapp_blueprint.route("/api/memories")
@require_webapp_auth
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
async def api_get_settings(user_id: int):
    """Get current chat settings for the user."""
    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return jsonify({"error": "no_chat"}), 404

        # Build available models list (same as /model command)
        all_models = list(settings.AVAILABLE_MODELS or [])
        if settings.OPENROUTER_AVAILABLE_MODELS:
            all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)

        return jsonify(
            {
                "settings": {
                    "system_prompt": chat_state.system_prompt or "",
                    "model": chat_state.model,
                    "thinking_level": chat_state.thinking_level or "off",
                    "ltm_enabled": chat_state.ltm_enabled,
                    "search_enabled": chat_state.search_enabled,
                },
                "available_models": all_models,
                "thinking_levels": ["off", "low", "medium", "high"],
            }
        )
    except Exception as e:
        logger.error("Mini App get settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/settings", methods=["PATCH"])
@require_webapp_auth
async def api_update_settings(user_id: int):
    """Update chat settings (partial update)."""
    try:
        from app.repos.chats import get_user_chat, update_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return jsonify({"error": "no_chat"}), 404

        body = await request.get_json(silent=True) or {}
        changed = False

        # System prompt
        if "system_prompt" in body:
            prompt = body["system_prompt"]
            if isinstance(prompt, str) and len(prompt) <= 4000:
                chat_state.system_prompt = prompt.strip() or None
                changed = True

        # Model
        if "model" in body:
            model = body["model"]
            all_models = list(settings.AVAILABLE_MODELS or [])
            if settings.OPENROUTER_AVAILABLE_MODELS:
                all_models.extend(settings.OPENROUTER_AVAILABLE_MODELS)
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
            chat_state.ltm_enabled = bool(body["ltm_enabled"])
            changed = True

        # Search
        if "search_enabled" in body:
            chat_state.search_enabled = bool(body["search_enabled"])
            changed = True

        if changed:
            await update_user_chat(user_id, chat_state)
            return jsonify({"ok": True})

        return jsonify({"ok": True, "note": "no_changes"})
    except Exception as e:
        logger.error("Mini App update settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Long Read Reader ──────────────────────────────────────────────────────────


@miniapp_blueprint.route("/reader")
async def reader_page():
    """Serve the Long Read Mini App HTML shell.

    This endpoint is intentionally public (no auth) — content is accessed by
    opaque UUID, so there is nothing to enumerate without the original link.
    """
    from quart import render_template

    return await render_template("reader.html")


@miniapp_blueprint.route("/api/reader/<uid>")
async def api_reader_content(uid: str):
    """Return the stored long message content for a given UID.

    Response schema (one of):
      {"markdown": "<full text>"}                       — Redis hit, fresh content
      {"telegraph_url": "https://telegra.ph/..."}       — Redis expired, use fallback
      {"error": "not_found"}  HTTP 404                  — nothing available
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

