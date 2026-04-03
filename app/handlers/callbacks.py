"""
Callback handlers — central registration hub.

This module keeps shared helpers (semaphore, busy-check, background tasks)
and the ``register()`` function that wires all callback handlers.

Domain-specific callbacks live in sub-modules:
    cb_roles          — role management (apply, create, delete, rename, etc.)
    cb_documents      — document management (upload, select, delete, etc.)
    cb_conversations  — conversation management (switch, rename, delete, etc.)
    cb_models         — model selection buttons
    cb_ai_actions     — heavy AI actions (complex search, fallback, retry)
    cb_navigation     — navigation menus, help, new chat, toggle search
    cb_feedback       — 👍/👎 feedback on AI responses
"""

__all__ = [
    "register",
    "_is_user_busy",
    "_BUSY_TOAST",
    "_HEAVY_CALLBACK_SEMAPHORE",
    "_background_tasks",
]

import asyncio

from telegram.ext import Application, CallbackQueryHandler

from app import state
from app.config import settings

# ── Shared helpers (imported by domain modules) ──────────────────────────────
_HEAVY_CALLBACK_LIMIT = max(1, int(getattr(settings, "MAX_CONCURRENT_HEAVY_CALLBACKS", 4)))
_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore(_HEAVY_CALLBACK_LIMIT)

_background_tasks: set = set()

_BUSY_TOAST = (
    "⏳ Дождитесь завершения текущего запроса"  # Default ru, handlers use t("busy.toast", lang) when they have Update
)


def _is_user_busy(user_id: int) -> bool:
    """Check if user has an active AI request (lock held by streaming/processing)."""
    return state.get_user_lock(user_id).locked()


def _add_fast_callback(application: Application, callback, pattern: str):
    """Register lightweight UI callbacks in non-blocking mode."""
    application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False), group=-1)


# ── Registration ─────────────────────────────────────────────────────────────


def register(application: Application) -> None:
    # --- Lazy imports to avoid circular dependencies ---
    from app.handlers.cb_ai_actions import (
        complex_search_callback,
        continue_stream_callback,
        fallback_callback,
        retry_last_callback,
        tts_reply_callback,
    )
    from app.handlers.cb_conversations import (
        conv_delete_ask_callback,
        conv_delete_callback,
        conv_delete_cancel_callback,
        conv_delete_confirm_callback,
        conv_page_callback,
        conv_rename_ask_callback,
        conv_rename_callback,
        conv_rename_cancel_callback,
        conv_switch_callback,
        conv_switch_to_callback,
        refresh_metrics_callback,
    )
    from app.handlers.cb_documents import document_callback
    from app.handlers.cb_feedback import _noop_callback, feedback_callback
    from app.handlers.cb_models import model_button_callback, switch_model_callback
    from app.handlers.cb_navigation import (
        deep_dive_callback,
        help_callback,
        help_topic_callback,
        model_menu_callback,
        new_chat_callback,
        new_topic_callback,
        open_conversations_callback,
        open_documents_callback,
        settings_thinking_callback,
        toggle_ltm_callback,
        toggle_search_callback,
    )
    from app.handlers.cb_roles import (
        open_roles_callback,
        role_apply_callback,
        role_clear_callback,
        role_create_callback,
        role_create_cancel_callback,
        role_create_manual_callback,
        role_custom_apply_callback,
        role_custom_retry_callback,
        role_custom_save_callback,
        role_delete_ask_callback,
        role_delete_cancel_callback,
        role_delete_confirm_callback,
        role_detail_callback,
        role_edit_ai_callback,
        role_edit_ai_save_callback,
        role_edit_ai_tweak_callback,
        role_edit_cancel_callback,
        role_edit_manual_callback,
        role_edit_prompt_callback,
        role_manual_cancel_callback,
        role_manual_save_callback,
        role_nav_callback,
        role_page_callback,
        role_rename_cancel_callback,
        role_rename_menu_callback,
        role_rename_pick_callback,
        role_view_prompt_callback,
        start_menu_callback,
    )

    # ── Fast (non-blocking) callbacks ────────────────────────────────────
    _add_fast_callback(application, toggle_search_callback, "^toggle_search$")
    _add_fast_callback(application, settings_thinking_callback, "^settings_thinking$")
    _add_fast_callback(application, toggle_ltm_callback, "^toggle_ltm$")
    _add_fast_callback(application, new_chat_callback, "^new_chat$")
    _add_fast_callback(application, model_menu_callback, "^model_menu$")
    _add_fast_callback(application, help_callback, "^help$")
    _add_fast_callback(application, start_menu_callback, "^start_menu$")
    _add_fast_callback(application, model_button_callback, "^model")
    _add_fast_callback(application, switch_model_callback, "^switch_model:")
    _add_fast_callback(application, open_roles_callback, r"^open_roles(:from_response)?$")
    _add_fast_callback(application, role_apply_callback, "^role_apply:")
    _add_fast_callback(application, role_clear_callback, "^role_clear$")
    _add_fast_callback(application, role_nav_callback, "^role_nav:")
    _add_fast_callback(application, role_page_callback, "^role_page:")
    _add_fast_callback(application, conv_page_callback, "^conv_page:")
    _add_fast_callback(application, conv_switch_callback, "^conv_switch$")
    _add_fast_callback(application, conv_switch_to_callback, "^conv_switch_to:")
    _add_fast_callback(application, help_topic_callback, "^help_topic:")
    _add_fast_callback(application, open_documents_callback, "^open_documents$")
    _add_fast_callback(application, open_conversations_callback, "^open_conversations$")

    # ── Blocking callbacks ───────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(complex_search_callback, pattern="^complex:"))
    application.add_handler(CallbackQueryHandler(fallback_callback, pattern="^fallback:"))
    application.add_handler(CallbackQueryHandler(document_callback, pattern="^doc:"))
    application.add_handler(CallbackQueryHandler(deep_dive_callback, pattern="^deepdive:"))
    application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic"))
    application.add_handler(CallbackQueryHandler(retry_last_callback, pattern="^retry_last$"))
    application.add_handler(CallbackQueryHandler(continue_stream_callback, pattern="^continue_stream$"))
    _add_fast_callback(application, tts_reply_callback, "^tts_reply$")

    # Feedback buttons (👍/👎)
    _add_fast_callback(application, feedback_callback, "^feedback:")
    _add_fast_callback(application, _noop_callback, "^noop$")

    # Роль: create/delete/rename
    application.add_handler(CallbackQueryHandler(role_create_callback, pattern="^role_create$"))
    application.add_handler(CallbackQueryHandler(role_create_cancel_callback, pattern="^role_create_cancel$"))
    application.add_handler(CallbackQueryHandler(role_custom_apply_callback, pattern="^role_custom_apply$"))
    application.add_handler(CallbackQueryHandler(role_custom_save_callback, pattern="^role_custom_save$"))
    application.add_handler(CallbackQueryHandler(role_custom_retry_callback, pattern="^role_custom_retry$"))
    # Manual role creation
    application.add_handler(CallbackQueryHandler(role_create_manual_callback, pattern="^role_create_manual$"))
    application.add_handler(CallbackQueryHandler(role_manual_cancel_callback, pattern="^role_manual_cancel$"))
    application.add_handler(CallbackQueryHandler(role_manual_save_callback, pattern="^role_manual_save$"))
    # Role management
    application.add_handler(CallbackQueryHandler(role_detail_callback, pattern="^role_detail:"))
    application.add_handler(CallbackQueryHandler(role_view_prompt_callback, pattern="^role_view_prompt:"))
    application.add_handler(CallbackQueryHandler(role_edit_prompt_callback, pattern="^role_edit_prompt:"))
    application.add_handler(CallbackQueryHandler(role_edit_manual_callback, pattern="^role_edit_manual:"))
    application.add_handler(CallbackQueryHandler(role_edit_ai_callback, pattern="^role_edit_ai:"))
    application.add_handler(CallbackQueryHandler(role_edit_ai_save_callback, pattern="^role_edit_ai_save$"))
    application.add_handler(CallbackQueryHandler(role_edit_ai_tweak_callback, pattern="^role_edit_ai_tweak:"))
    application.add_handler(CallbackQueryHandler(role_edit_cancel_callback, pattern="^role_edit_cancel:"))
    application.add_handler(CallbackQueryHandler(role_delete_ask_callback, pattern="^role_delete_ask:"))
    application.add_handler(CallbackQueryHandler(role_delete_confirm_callback, pattern="^role_delete_confirm:"))
    application.add_handler(CallbackQueryHandler(role_delete_cancel_callback, pattern="^role_delete_cancel:"))

    application.add_handler(CallbackQueryHandler(role_rename_menu_callback, pattern="^role_rename_menu$"))
    application.add_handler(CallbackQueryHandler(role_rename_pick_callback, pattern="^role_rename_pick:"))
    application.add_handler(CallbackQueryHandler(role_rename_cancel_callback, pattern="^role_rename_cancel$"))

    # Role Navigation (noop)
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))

    # Conversation management callbacks
    application.add_handler(CallbackQueryHandler(conv_rename_callback, pattern="^conv_rename$"))
    application.add_handler(CallbackQueryHandler(conv_rename_ask_callback, pattern="^conv_rename_ask:"))
    application.add_handler(CallbackQueryHandler(conv_rename_cancel_callback, pattern="^conv_rename_cancel$"))
    application.add_handler(CallbackQueryHandler(conv_delete_callback, pattern="^conv_delete$"))
    application.add_handler(CallbackQueryHandler(conv_delete_ask_callback, pattern="^conv_delete_ask:"))
    application.add_handler(CallbackQueryHandler(conv_delete_confirm_callback, pattern="^conv_delete_confirm:"))
    application.add_handler(CallbackQueryHandler(conv_delete_cancel_callback, pattern="^conv_delete_cancel$"))

    # Refresh metrics
    application.add_handler(CallbackQueryHandler(refresh_metrics_callback, pattern="^refresh_metrics$"))

    # Conversation branching
    from app.handlers.cb_branches import branch_create_callback, branch_return_callback

    _add_fast_callback(application, branch_create_callback, "^branch_create$")
    _add_fast_callback(application, branch_return_callback, "^branch_return$")

    # Reminder cancel inline buttons
    from app.handlers.cmd_reminders import reminder_cancel_callback

    _add_fast_callback(application, reminder_cancel_callback, "^reminder_cancel:")

    # Voice confirmation flow (confirm / edit / cancel / transcribe_only)
    from app.handlers.cb_voice import voice_callback

    application.add_handler(CallbackQueryHandler(voice_callback, pattern="^voice:"))

    # Image generation Interactive Canvas (draw:regen, draw:ar:*, draw:model:*)
    from app.handlers.cb_image import draw_callback

    application.add_handler(CallbackQueryHandler(draw_callback, pattern="^draw:"))

    # Forward-batch memory save (Improvement 7)
    from app.handlers.cb_fwd_save import fwd_save_callback

    _add_fast_callback(application, fwd_save_callback, "^fwd_save$")

    # Smart Suggestions + Proactive Intent Routing (Phase 1 UX)
    from app.handlers.cb_smart_actions import edit_query_callback, intent_route_callback, suggestion_callback

    _add_fast_callback(application, suggestion_callback, "^suggest:")
    _add_fast_callback(application, edit_query_callback, "^edit_query$")
    application.add_handler(CallbackQueryHandler(intent_route_callback, pattern="^intent_route:"))
