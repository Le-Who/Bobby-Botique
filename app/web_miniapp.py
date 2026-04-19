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
import time
import typing
import urllib.parse
from functools import wraps
from typing import Any

from quart import Blueprint, jsonify, request

from app.config import settings
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

miniapp_blueprint = Blueprint("miniapp", __name__, template_folder="templates")

# State tracking for Live Audio sessions
ACTIVE_LIVE_SESSIONS: set[int] = set()
_KEY_ROTATION_INDEX: int = 0


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
        from app.repos.roles import get_user_custom_roles

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return jsonify({"error": "no_chat"}), 404

        # Build available models list grouped by provider
        gemini_models = list(settings.AVAILABLE_MODELS or [])
        openrouter_models = list(settings.OPENROUTER_AVAILABLE_MODELS or [])
        opencode_models = list(settings.OPENCODE_AVAILABLE_MODELS or [])
        all_models = gemini_models + openrouter_models + opencode_models

        # Build grouped structure for the frontend picker
        grouped_models = []
        if gemini_models:
            grouped_models.append({"provider": "Google Gemini", "icon": "🤖", "models": gemini_models})
        if opencode_models:
            grouped_models.append({"provider": "Opencode Go", "icon": "⚡", "models": opencode_models})
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

        # Model — validate against all three providers
        if "model" in body:
            model = body["model"]
            all_models = (
                list(settings.AVAILABLE_MODELS or [])
                + list(settings.OPENROUTER_AVAILABLE_MODELS or [])
                + list(settings.OPENCODE_AVAILABLE_MODELS or [])
            )
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

        if changed:
            await update_user_chat(user_id, chat_state)
            return jsonify({"ok": True})

        return jsonify({"ok": True, "note": "no_changes"})
    except Exception as e:
        logger.error("Mini App update settings error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# ── Additional Setting Controls: Roles, Context, Voices ────────────────────


@miniapp_blueprint.route("/api/context/reset", methods=["POST"])
@require_webapp_auth
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
async def api_delete_role(user_id: int, role_id: int):
    """Delete a custom role."""
    try:
        from app.repos.roles import delete_custom_role

        await delete_custom_role(role_id, user_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("Mini App delete role error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@miniapp_blueprint.route("/api/voices", methods=["GET"])
@require_webapp_auth
async def api_get_voices(user_id: int):
    """Provide a list of curated voices depending on available provider."""
    from app.config import settings

    if settings.ELEVENLABS_API_KEYS:
        voices = [
            {"id": "XB0fDUnXU5powFXDhCwa", "name": "Charlotte (Conversational)"},
            {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel (Calm)"},
            {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam (Deep)"},
            {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni (Friendly)"},
            {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella (Soft)"},
            {"id": "t0jbNlBVZ17f02VDIeMI", "name": "Jessie (Energetic)"},
        ]
    else:
        voices = [
            {"id": "Aoede", "name": "Aoede (Natural/Breezy)"},
            {"id": "Kore", "name": "Kore (Confident/Energetic)"},
            {"id": "Puck", "name": "Puck (Upbeat Male)"},
            {"id": "Charon", "name": "Charon (Professional)"},
            {"id": "Leda", "name": "Leda (Light/Youthful)"},
            {"id": "Orus", "name": "Orus (Deep/Authoritative)"},
            {"id": "Zephyr", "name": "Zephyr (Clear/Cheerful)"},
            {"id": "Rasalgethi", "name": "Rasalgethi (Informative)"},
        ]
    return jsonify({"voices": voices})


# ── Long Read Reader ──────────────────────────────────────────────────────────


@miniapp_blueprint.route("/reader")
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

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(tg_url)
            resp.raise_for_status()
            page_html = resp.text

        # Extract the <article> body from the Telegraph page HTML
        import re as _re

        article_match = _re.search(r"<article[^>]*>(.*?)</article>", page_html, _re.DOTALL)
        if not article_match:
            logger.warning("No <article> tag found in Telegraph page: %s", tg_url)
            return None

        return extract_text_from_telegraph_html(article_match.group(1))

    except Exception as exc:
        logger.warning("Telegraph reverse-proxy fetch failed (%s): %s", tg_url, exc)
        return None


@miniapp_blueprint.route("/api/reader/<uid>")
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
    try:
        from app.database import db_query

        limit = min(request.args.get("limit", 50, type=int), 200)
        query_filter = request.args.get("query", "").strip()

        # Fetch nodes
        if query_filter:
            nodes_rows = await db_query(
                """
                SELECT id, entity_name, entity_type, description
                FROM memory_nodes
                WHERE user_id = $1
                  AND entity_name ILIKE $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                (user_id, f"%{query_filter}%", limit),
            )
        else:
            nodes_rows = await db_query(
                """
                SELECT id, entity_name, entity_type, description
                FROM memory_nodes
                WHERE user_id = $1
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                (user_id, limit),
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

        # Fetch edges connecting the returned nodes
        if node_ids:
            id_list = list(node_ids)
            edges_rows = await db_query(
                """
                SELECT source_node, target_node, predicate, weight, is_core
                FROM memory_edges
                WHERE user_id = $1
                  AND source_node = ANY($2::bigint[])
                  AND target_node = ANY($2::bigint[])
                  AND valid_to IS NULL
                ORDER BY weight DESC
                LIMIT 500
                """,
                (user_id, id_list),
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


# ── Crocodile Game ────────────────────────────────────────────────────────────
# Per-game asyncio.Lock prevents parallel guess races from the same connection.
# Bounded: entries are cleaned up on disconnect/game-end and swept when the dict
# exceeds _GAME_LOCKS_MAX (protects against abandoned connections).
_game_locks: dict[str, asyncio.Lock] = {}
_GAME_LOCKS_MAX = 512


def _sweep_game_locks() -> None:
    """Evict the oldest half of _game_locks when the dict exceeds capacity."""
    if len(_game_locks) < _GAME_LOCKS_MAX:
        return
    keys = list(_game_locks.keys())
    for k in keys[: len(keys) // 2]:
        _game_locks.pop(k, None)
    logger.debug("_game_locks swept: %d entries removed", len(keys) // 2)


@miniapp_blueprint.route("/game")
async def game_page():
    """Serve the Crocodile Mini App HTML shell."""
    from quart import render_template
    from quart import request as _req

    game_id = _req.args.get("game_id") or _req.args.get("tgWebAppStartParam") or _req.args.get("id") or ""
    return await render_template("crocodile.html", game_id=game_id)


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
    import asyncio

    from quart import websocket

    from app.games.crocodile import load_game

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

    # ── Resolve game ───────────────────────────────────────────────────────
    game_id = websocket.args.get("game_id", "")
    if not game_id:
        await websocket.close(4400, "Missing game_id")
        return

    game = await load_game(game_id)
    if game is None:
        await websocket.close(4008, "Game expired or not found")
        return

    if game.status != "active":
        await websocket.close(4009, "Game already finished")
        return

    # Register guesser on first connect (not the creator)
    if game.guesser_id is None and user_id != game.creator_id:
        game.guesser_id = user_id
        await game.save()

    # Send initial game state
    is_creator = user_id == game.creator_id
    await websocket.send_json(
        {
            "event": "game_state",
            "category": game.category,
            "lang": game.lang,
            "attempts": len(game.attempts),
            "max_attempts": game.max_attempts,
            "is_creator": is_creator,
            "target_word": game.target_word if is_creator else None,
        }
    )

    # Restore chat history so reconnecting players keep their progress visible
    from app.games.crocodile import (
        broadcast_game_event,
        get_game_hints,
        get_game_history,
        subscribe_game,
        unsubscribe_game,
    )

    history = get_game_history(game_id)
    if history:
        await websocket.send_json({"event": "history_sync", "items": history})

    # Ensure per-game lock (sweep dict if it exceeds capacity)
    _sweep_game_locks()
    if game_id not in _game_locks:
        _game_locks[game_id] = asyncio.Lock()
    lock = _game_locks[game_id]

    # Subscribe this socket to the game's PubSub broadcast queue
    my_queue = subscribe_game(game_id)

    # ── Drain task: forward broadcast events to this WebSocket connection ──
    # Runs concurrently with the receive loop so broadcasts are never blocked
    # by waiting for the next incoming message.
    drain_task: asyncio.Task | None = None

    async def _drain_broadcasts() -> None:
        try:
            while True:
                payload = await my_queue.get()
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
                await websocket.send_json({"event": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type")
            pending_id = str(msg.get("pending_id", ""))

            # ── Hint request ──────────────────────────────────────────────
            if msg_type == "hint":
                hint_idx = int(msg.get("hint_index", 0))
                hints = get_game_hints(game_id)
                if hint_idx < len(hints):
                    await websocket.send_json(
                        {
                            "event": "hint",
                            "text": hints[hint_idx],
                            "hint_index": hint_idx,
                            "available": True,
                        }
                    )
                else:
                    await websocket.send_json(
                        {
                            "event": "hint",
                            "text": "⏳ Подсказки ещё готовятся или закончились...",
                            "available": False,
                        }
                    )
                continue

            # ── Creator-only: reaction —————————————————————————————————————
            # Creators can send emoji reactions which are broadcast to the guesser.
            if msg_type == "reaction" and is_creator:
                emoji = str(msg.get("emoji", "")).strip()
                if emoji:
                    await broadcast_game_event(
                        game_id,
                        {
                            "event": "reaction",
                            "emoji": emoji,
                        },
                        exclude=my_queue,
                    )
                continue

            # ── Creator-only: typing indicator from creator side ──────────
            # (Guesser typing is handled in the guess flow below)
            if msg_type == "typing_status" and is_creator:
                await broadcast_game_event(
                    game_id,
                    {
                        "event": "creator_typing",
                        "active": bool(msg.get("active", False)),
                    },
                    exclude=my_queue,
                )
                continue

            # ── Guesser typing indicator (ephemeral, not recorded) ─────────
            if msg_type == "typing":
                if not is_creator:
                    await broadcast_game_event(
                        game_id,
                        {
                            "event": "guesser_typing",
                            "active": bool(msg.get("active", False)),
                        },
                        exclude=my_queue,
                    )
                continue

            if msg_type != "guess":
                continue

            word = str(msg.get("word", "")).strip()
            if not word:
                await websocket.send_json({"event": "error", "message": "Empty guess"})
                continue

            if is_creator:
                await websocket.send_json(
                    {"event": "error", "message": "Создатель игры не может отгадывать свои слова."}
                )
                continue

            async with lock:
                # Reload game state from Redis (another tab may have mutated it)
                game = await load_game(game_id) or game
                if game.status != "active":
                    break

                event = await game.process_guess(word)

            # Echo pending_id back so the client can resolve its optimistic bubble
            if pending_id:
                event["pending_id"] = pending_id

            await websocket.send_json(event)

            # Broadcast the result to all other subscribers (spectators / creator)
            broadcast_payload = {
                "event": "spectator_result",
                "word": word,
                "status": event.get("status"),
                "score": event.get("score"),
                "hint": event.get("hint"),
            }
            if event.get("event") in ("game_over",):
                broadcast_payload["event"] = "spectator_game_over"
                broadcast_payload["word"] = event.get("word", word)
            elif event.get("status") == "exact_match":
                broadcast_payload["event"] = "spectator_win"
                broadcast_payload["word"] = event.get("word", word)
            await broadcast_game_event(game_id, broadcast_payload, exclude=my_queue)

            # Finalize if game ended (won / lost)
            if game.status in ("won", "lost"):
                try:
                    from app.bot_instance import get_bot

                    bot = get_bot()
                    if bot:
                        await game.finalize(bot)
                except Exception as exc:
                    logger.warning("game_ws: finalize failed game=%s: %s", game_id, exc)
                _game_locks.pop(game_id, None)
                break

    except Exception as exc:
        logger.warning("game_ws: unexpected error game=%s: %s", game_id, exc)
    finally:
        if drain_task and not drain_task.done():
            drain_task.cancel()
        unsubscribe_game(game_id, my_queue)
        _game_locks.pop(game_id, None)


# ── Live Audio (Gemini Live API WebSocket proxy) ─────────────────────────────


@miniapp_blueprint.route("/live")
async def live_audio_page():
    """Serve the Live Audio Mini App HTML shell."""
    from quart import render_template

    return await render_template("live_audio.html")


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
    import asyncio
    import base64

    from quart import websocket

    # ── Auth ──────────────────────────────────────────────────────────────
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

    # Check for active session to prevent overlapping websocket abuse
    if user_id in ACTIVE_LIVE_SESSIONS:
        await websocket.close(4009, "User already has an active session")
        return

    ACTIVE_LIVE_SESSIONS.add(user_id)
    try:
        await _handle_live_session(websocket, user_id, validated, resumption_token)
    finally:
        ACTIVE_LIVE_SESSIONS.discard(user_id)


async def _handle_live_session(websocket, user_id: int, validated: dict, resumption_token: str):

    # Extract display metadata from the already-validated initData user object.
    # These fields are populated from Telegram's initData, no extra DB call needed.
    _tg_user: dict = validated.get("user") or {}
    user_first_name: str = _tg_user.get("first_name", "").strip()
    user_language: str = _tg_user.get("language_code", "").strip()

    # ── Connect to Gemini Live API ────────────────────────────────────────
    from google import genai
    from google.genai import types

    from app.config import GEMINI_LIVE_MODEL
    from app.handlers.ai_core import _resolve_ai_request
    from app.providers.gemini import get_cached_genai_client
    from app.repos.chats import get_user_chat

    session_resumption_token: str | None = None
    chat_state = await get_user_chat(user_id)

    # Build personalised system instruction from initData user metadata.
    _sys_parts = []
    if chat_state and chat_state.system_prompt:
        _sys_parts.append(chat_state.system_prompt)
    else:
        _sys_parts.extend(
            [
                "Ты — дружелюбный AI-ассистент в Telegram боте.",
                "Отвечай кратко и по делу. Если не уверен — скажи об этом.",
            ]
        )

    if user_first_name:
        _sys_parts.append(f"Имя пользователя: {user_first_name}.")
    if user_language:
        _sys_parts.append(
            f"Предпочтительный язык пользователя: {user_language}. "
            "Всегда отвечай на том же языке, на котором говорит пользователь."
        )

    config_kwargs = {
        "response_modalities": [types.Modality.AUDIO],
        "input_audio_transcription": types.AudioTranscriptionConfig(),
        "output_audio_transcription": types.AudioTranscriptionConfig(),
        # Always enable session resumption.
        # handle=None starts a new resumable session; handle=<token> resumes.
        "session_resumption": types.SessionResumptionConfig(
            handle=resumption_token or None,
        ),
        # Context window compression — allows unlimited session duration.
        # Without it, audio-only sessions hard-limit at ~15 min.
        "context_window_compression": types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        # Google Search grounding: model requests real-time web data automatically.
        # Supported on gemini-3.1-flash-live-preview (AI Studio key).
        "tools": [types.Tool(google_search=types.GoogleSearch())],
        "system_instruction": types.Content(parts=[types.Part(text=" ".join(_sys_parts))]),
    }

    if chat_state and chat_state.thinking_level:
        # chat_state.thinking_level is one of: "low", "medium", "high"
        # The SDK expects uppercase enumerations for AI Studio live models.
        mapped_level = chat_state.thinking_level.upper()
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=mapped_level)

    live_config = types.LiveConnectConfig(**config_kwargs)  # type: ignore[arg-type]

    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        key_data, _, _ = await _resolve_ai_request(
            GEMINI_LIVE_MODEL,
            use_openrouter=False,
        )
        if not key_data:
            await websocket.close(4500, "No API keys configured or available")
            return
        api_key = key_data["api_key"]

        client = get_cached_genai_client(api_key)

        if not key_data:
            if attempt == 0:
                await websocket.close(4500, "No API keys configured or available")
            else:
                try:
                    await websocket.send_json({"type": "error", "message": "All available keys exhausted."})
                except Exception:
                    pass
            return
        logger.info(
            "live_audio_ws: connecting user=%d model=%s resumption_token=%s attempt=%d",
            user_id,
            GEMINI_LIVE_MODEL,
            bool(resumption_token),
            attempt + 1,
        )
        try:
            async with client.aio.live.connect(model=GEMINI_LIVE_MODEL, config=live_config) as session:
                await websocket.send_json({"type": "connected"})

                # ── Producer: browser → Gemini ────────────────────────────────
                async def _producer() -> None:
                    start_time = time.monotonic()
                    _mic_ended = False  # True after audio_stream_end; resets on new audio
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
                                _mic_ended = False  # New audio — mic is active again

                            elif msg_type == "audio_stream_end":
                                await session.send_realtime_input(audio_stream_end=True)
                                _mic_ended = True  # Start idle countdown

                            elif msg_type == "text":
                                text = msg.get("text", "")
                                if text:
                                    await session.send_realtime_input(text=text)

                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        logger.warning(
                            "live_audio_ws producer error user=%d: %s",
                            user_id,
                            exc,
                        )

                # ── Consumer: Gemini → browser ────────────────────────────────
                async def _consumer() -> None:
                    nonlocal session_resumption_token
                    try:
                        async for response in session.receive():
                            content = response.server_content
                            if content:
                                # Audio chunks
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

                                # Transcriptions
                                if content.input_transcription:
                                    await websocket.send_json(
                                        {
                                            "type": "input_transcript",
                                            "text": content.input_transcription.text,
                                        }
                                    )
                                if content.output_transcription:
                                    await websocket.send_json(
                                        {
                                            "type": "output_transcript",
                                            "text": content.output_transcription.text,
                                        }
                                    )

                                # Interruption
                                if content.interrupted is True:
                                    await websocket.send_json({"type": "interrupt"})

                            # Session resumption update
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

                            # GoAway signal — server will terminate connection soon.
                            # Relay to client so it can proactively reconnect.
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

                # ── Run both loops concurrently ───────────────────────────────
                producer_task = asyncio.create_task(_producer())
                consumer_task = asyncio.create_task(_consumer())

                try:
                    _done, _pending = await asyncio.wait(
                        {producer_task, consumer_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    # Grace period: if the producer exited first (client disconnect or
                    # idle-mic watchdog), give the consumer up to 5s to drain its
                    # remaining Gemini messages — most importantly the final
                    # SessionResumptionUpdate — before we cancel it.
                    if producer_task in _done and not consumer_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(consumer_task), timeout=5.0)
                        except (TimeoutError, asyncio.CancelledError):
                            pass
                finally:
                    for task in (producer_task, consumer_task):
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

            break  # Success, so break the retry loop

        except Exception as exc:
            err_str = str(exc)
            if "1011" in err_str or "quota" in err_str.lower():
                logger.warning("live_audio_ws: session error user=%d attempt=%d: %s", user_id, attempt + 1, err_str)
                # Broadcast the exhaustion to the global key manager so subsequent connections rotate to a healthy key
                try:
                    import hashlib

                    from app.repos.keys import get_key_status_manager
                    from app.utils.background_tasks import submit_task

                    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]
                    penalty_type = "quota" if "quota" in err_str.lower() else "rate_limit"
                    submit_task(
                        get_key_status_manager().suspend_key(
                            key_hash, "gemini-3.1-flash-live-preview", penalty_type, err_str
                        )
                    )
                except Exception as e_susp:
                    logger.debug("Failed to suspend Live API key: %s", e_susp)

                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(0.5)
                    continue
            logger.error("live_audio_ws: session fatal error user=%d: %s", user_id, err_str)
            try:
                await websocket.send_json({"type": "error", "message": f"Live session failed: {err_str}"})
            except Exception:
                pass
            break

    logger.info("live_audio_ws: disconnected user=%d", user_id)
