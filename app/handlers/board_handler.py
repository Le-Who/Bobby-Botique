"""
board_handler.py — Collaborative AI-Notes (Topic Aggregator).

Lifecycle:
  1. User posts "@gemaibotv2 доска: <topic>" as inline query.
  2. handle_inline_query returns board template card + button.
  3. handle_chosen_inline_result creates DB row (via _init_board_async in inline.py).
  4. First button press hits handle_board_link_callback → links chat_id:message_id.
  5. Group members reply to the board message → try_handle_board_reply collects entries.
  6. Debounced synthesis (_maybe_synthesize) fires only when new entries exist:
     - triggered by entry >= 3 threshold OR 60-second periodic sweep.
  7. Board is updated in-place via edit_message_text(inline_message_id=...).
  8. handle_board_refresh_callback / handle_board_close_callback for button actions.

Privacy Mode note:
  Even with Privacy Mode, Telegram delivers messages that are replies to messages
  sent via_bot. Filter: reply_to_message.via_bot.id == bot.id.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ── Debounce configuration ────────────────────────────────────────────────────

_DEBOUNCE_SECONDS = 60  # synthesize at most once per 60s
_NEW_ENTRY_THRESHOLD = 3  # also synthesize when ≥3 new entries arrive
_MAX_ENTRIES_PER_BOARD = 50  # hard cap; oldest entries become part of summary
_BOARD_TTL_HOURS = 24  # boards older than this are considered expired

# Per-board debounce tracking: {board_id: next_allowed_float}
_board_next_synthesis: dict[int, float] = {}

# ── Keyboard factories ────────────────────────────────────────────────────────


def _active_keyboard(board_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Обновить", callback_data=f"board_refresh:{board_id}"),
                InlineKeyboardButton("🔒 Закрыть", callback_data=f"board_close:{board_id}"),
            ]
        ]
    )


def _closed_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Доска закрыта", callback_data="inline_noop")]])


# ── Board card renderer ───────────────────────────────────────────────────────


def _render_board_card(topic: str, summary: str, entry_count: int, board_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render the full board HTML and the action keyboard."""
    if summary:
        content = summary
    else:
        content = "<i>Пока ничего не предложено.\nОтвечайте (reply) на это сообщение, чтобы добавить свои идеи.</i>"

    text = (
        f"📋 <b>{_html.escape(topic)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{content}\n\n"
        f"<i>Обновлено • {entry_count} {'идея' if entry_count == 1 else 'идей' if 2 <= entry_count <= 4 else 'идей'} от участников</i>"
        if entry_count > 0
        else f"📋 <b>{_html.escape(topic)}</b>\n━━━━━━━━━━━━━━━━━━━━━\n{content}"
    )

    if len(text) > 4000:
        text = text[:3997] + "…"

    return text, _active_keyboard(board_id)


# ── Public: board_link callback (fired on first button press) ─────────────────


async def handle_board_link_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Link chat_id + message_id to the board on the first button press.

    This is the only moment we receive both inline_message_id AND the in-chat
    message coordinates simultaneously.
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        return

    msg = query.message
    if not msg:
        # Inline context: message is None (editing an inline message)
        # The inline_message_id IS the handle — we still don't know
        # chat_id/message_id here. This path is only entered when the
        # callback comes from a normal (non-inline) message, which shouldn't
        # happen for board_link.
        return

    chat_id = msg.chat.id
    message_id = msg.message_id

    from app.repos.boards_repo import get_board_by_inline_msg, link_board_to_chat

    board = await get_board_by_inline_msg(inline_message_id)
    if not board:
        logger.debug("board_link: no board found for inline_msg_id=%s", inline_message_id)
        return

    await link_board_to_chat(
        inline_msg_id=inline_message_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    logger.info(
        "Board %s linked: chat_id=%s message_id=%s",
        board["id"],
        chat_id,
        message_id,
    )


# ── Public: reply capture ─────────────────────────────────────────────────────


async def try_handle_board_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Intercept replies to board messages. Returns True if handled (caller should return).

    Detection chain:
      1. Message must be a reply.
      2. reply_to_message.via_bot.id must equal the bot's own id
         (works even with Privacy Mode — Telegram always delivers these).
      3. The replied-to message must exist in inline_boards by chat_id:message_id.
    """
    msg = update.message
    if not msg or not msg.reply_to_message:
        return False

    replied = msg.reply_to_message

    # Only care about messages sent via this bot
    if not replied.via_bot or replied.via_bot.id != context.bot.id:
        return False

    # Board lookup by in-chat coordinates
    chat_id = msg.chat.id
    replied_message_id = replied.message_id

    from app.repos.boards_repo import (
        add_entry,
        get_board_by_chat_msg,
        get_new_entries_count,
    )

    board = await get_board_by_chat_msg(chat_id, replied_message_id)
    if not board:
        return False  # Reply to an inline msg that isn't a board

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return True  # Still "handled" — avoid processing voice/media replies as board entries

    user_name = (msg.from_user.first_name or "Аноним") if msg.from_user else "Аноним"

    updated_entries = await add_entry(
        board_id=board["id"],
        user_name=user_name,
        text=text,
    )
    if updated_entries is None:
        return True  # DB error but don't let the message fall through to general handler

    logger.debug(
        "Board %s: new entry from %s (%d total)",
        board["id"],
        user_name,
        len(updated_entries),
    )

    # ── Debounce + threshold trigger ─────────────────────────────────────────
    new_count = await get_new_entries_count(board["id"])
    await _maybe_synthesize(
        bot=context.bot,
        board=board,
        updated_entries=updated_entries,
        new_entry_count=new_count,
        force=False,
    )
    return True


# ── Synthesis engine ──────────────────────────────────────────────────────────


async def _maybe_synthesize(
    bot,
    board: dict,
    updated_entries: list[dict],
    new_entry_count: int,
    force: bool,
) -> None:
    """Synthesize board content and edit inline message — but ONLY if:
      - force=True (manual refresh), OR
      - new_entry_count >= _NEW_ENTRY_THRESHOLD, OR
      - debounce window has expired AND new_entry_count > 0.

    The "new_entry_count > 0" guard is critical: we never call the LLM
    unless there are actual new inputs since the last synthesis.
    """
    board_id = board["id"]
    now = time.monotonic()

    if not force and new_entry_count < _NEW_ENTRY_THRESHOLD:
        # Threshold: synthesize immediately when enough new entries accumulated
        # Debounce: delay until window expires
        next_allowed = _board_next_synthesis.get(board_id, 0.0)
        if now < next_allowed:
            return  # Still within debounce window — skip
        # Window expired; only proceed if there are new entries
        if new_entry_count == 0:
            return  # No new inputs — nothing to synthesize

    # Mark next allowed synthesis time
    _board_next_synthesis[board_id] = now + _DEBOUNCE_SECONDS

    inline_message_id = board.get("inline_msg_id")
    if not inline_message_id:
        logger.warning("Board %s has no inline_msg_id — cannot edit", board_id)
        return

    topic = board.get("topic", "")
    summary = await _run_synthesis(topic=topic, entries=updated_entries)

    from app.repos.boards_repo import update_summary

    await update_summary(board_id, summary)

    card_text, keyboard = _render_board_card(
        topic=topic,
        summary=summary,
        entry_count=len(updated_entries),
        board_id=board_id,
    )

    try:
        await bot.edit_message_text(
            text=card_text,
            inline_message_id=inline_message_id,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info(
            "Board %s synthesized: %d entries, inline_msg_id=%s",
            board_id,
            len(updated_entries),
            inline_message_id,
        )
    except Exception as err:
        logger.error("Board %s: edit_message_text failed: %s", board_id, err)


async def _run_synthesis(topic: str, entries: list[dict]) -> str:
    """Call Gemini Flash to produce structured board HTML from raw entries."""
    if not entries:
        return ""

    entries_text = "\n".join(
        f"- {e.get('user', 'Аноним')}: {e.get('text', '')}"
        for e in entries[-_MAX_ENTRIES_PER_BOARD:]  # cap at max entries
    )

    prompt = (
        f'Тема доски: "{topic}"\n\n'
        f"Участники предложили следующее:\n{entries_text}\n\n"
        "Сгенерируй обновлённое содержимое доски:\n"
        "• Сгруппируй похожие идеи по смысловым категориям\n"
        "• Добавь эмодзи-маркеры к каждому пункту\n"
        "• Сохрани имена авторов в скобках\n"
        "• Выдели повторяющиеся/популярные идеи\n"
        "• Формат: компактный Telegram-совместимый текст (без HTML-тегов, без Markdown-заголовков)\n"
        "• Объём: не более 800 символов"
    )

    try:
        # Use the same 3-way race as inline — lightweight, fast, resilient.
        from app.handlers.inline import _stream_inline_fast, get_inline_model

        result = await asyncio.wait_for(
            _stream_inline_fast(
                preferred_model=await get_inline_model(),
                history=[{"role": "user", "parts": [prompt]}],
                system_instruction=None,
                user_id=None,
                max_rounds=2,  # fewer rounds — synthesis is non-critical
                enable_web_search=False,
            ),
            timeout=30.0,
        )
        return (result[0] or "").strip()[:3000] if result else ""
    except Exception as exc:
        logger.error("Board synthesis failed: %s", exc, exc_info=True)
        # Fallback: plain list without AI structuring
        lines = [f"• {e.get('user', '?')}: {e.get('text', '')[:200]}" for e in entries[-20:]]
        return "\n".join(lines)


# ── Callbacks: Refresh and Close ──────────────────────────────────────────────


async def handle_board_refresh_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """'🔄 Обновить' button — force a new synthesis regardless of debounce."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        return

    try:
        _ = int(parts[1])
    except ValueError:
        return

    from app.repos.boards_repo import get_board_by_inline_msg

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        await query.answer("⚠️ Невозможно обновить — нет доступа к сообщению.", show_alert=True)
        return

    # Re-fetch full board from DB
    board = await get_board_by_inline_msg(inline_message_id)
    if not board:
        await query.answer("ℹ️ Доска уже закрыта или не найдена.", show_alert=True)
        return

    await _maybe_synthesize(
        bot=context.bot,
        board=board,
        updated_entries=board.get("entries", []),
        new_entry_count=board.get("entries_since_last_synthesis", 0),
        force=True,
    )


async def handle_board_close_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """'🔒 Закрыть' button — close the board (creator-only)."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2:
        return

    try:
        board_id = int(parts[1])
    except ValueError:
        return

    inline_message_id = query.inline_message_id
    if not inline_message_id:
        return

    from app.repos.boards_repo import close_board, get_board_by_inline_msg

    board = await get_board_by_inline_msg(inline_message_id)
    if not board:
        return

    # Only the creator can close
    requester_id = query.from_user.id if query.from_user else None
    if requester_id and requester_id != board.get("creator_id"):
        await query.answer("🔒 Только создатель доски может её закрыть.", show_alert=True)
        return

    await close_board(board_id)

    topic = board.get("topic", "")
    summary = board.get("last_summary", "")
    entry_count = len(board.get("entries", []))

    closed_text = (
        f"📋 <b>{_html.escape(topic)}</b> <i>(закрыта)</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"{summary or '<i>Нет данных</i>'}\n\n"
        f"<i>Итого: {entry_count} идей • Доска закрыта</i>"
    )
    if len(closed_text) > 4000:
        closed_text = closed_text[:3997] + "…"

    try:
        await context.bot.edit_message_text(
            text=closed_text,
            inline_message_id=inline_message_id,
            parse_mode="HTML",
            reply_markup=_closed_keyboard(),
        )
    except Exception as err:
        logger.error("Board close: edit failed for inline_msg_id=%s: %s", inline_message_id, err)
