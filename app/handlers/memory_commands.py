# /app/handlers/memory_commands.py
"""User-facing /memory command with inline pagination and delete controls.

Provides transparency into what the bot "remembers" and lets users
delete individual memories via inline buttons.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter

MEMORIES_PER_PAGE = 5


@authorized_only
@safe_handler("❌ Ошибка получения воспоминаний.")
async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paginated list of user's long-term memories."""
    user_id = update.effective_user.id
    await _send_memory_page(update.message, user_id, page=0)


async def _send_memory_page(target, user_id: int, page: int = 0) -> None:
    """Render a single page of memories with inline controls.

    ``target`` is either ``update.message`` (for commands) or
    ``query.message`` (for callback navigation).
    """
    import asyncio

    from app.repos.memory import get_memory_stats, list_memories

    offset = page * MEMORIES_PER_PAGE
    # ⚡ Bolt Optimization: Fetch memories and stats concurrently to reduce DB wait time
    memories, stats = await asyncio.gather(
        list_memories(user_id, offset=offset, limit=MEMORIES_PER_PAGE),
        get_memory_stats(user_id)
    )
    total = stats.get("total_memories", 0) if stats else 0

    if not memories and page == 0:
        await target.reply_text("📭 У вас пока нет сохранённых воспоминаний.")
        return

    # Build text
    lines = [f"🧠 **Ваши воспоминания** ({total} всего)\n"]
    for i, m in enumerate(memories, start=offset + 1):
        snippet = m["content"][:120]
        if len(m["content"]) > 120:
            snippet += "…"
        date_str = str(m["created_at"])[:10] if m.get("created_at") else "?"
        lines.append(f"`{i}.` {snippet}\n   📅 {date_str} | 🏷 {m.get('source_type', '?')}\n")

    formatted_text, parse_mode = TelegramFormatter.format_text("\n".join(lines))

    # Build inline keyboard
    buttons = []
    # Delete buttons row (one per memory on this page)
    delete_row = []
    for m in memories:
        delete_row.append(
            InlineKeyboardButton(
                f"🗑 #{memories.index(m) + offset + 1}",
                callback_data=f"mem:del:{m['id']}:{page}",
            )
        )
    if delete_row:
        buttons.append(delete_row)

    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mem:page:{page - 1}"))
    total_pages = max(1, (total + MEMORIES_PER_PAGE - 1) // MEMORIES_PER_PAGE)
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="mem:noop"))
    if offset + MEMORIES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"mem:page:{page + 1}"))
    buttons.append(nav_row)

    markup = InlineKeyboardMarkup(buttons)

    # Use edit_text if this is a callback update, reply_text otherwise
    if hasattr(target, "edit_text"):
        try:
            await target.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=markup)
            return
        except Exception:
            pass
    await target.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=markup)


async def memory_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks for memory management."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    if data == "mem:noop":
        return

    if data.startswith("mem:page:"):
        page = int(data.split(":")[2])
        await _send_memory_page(query.message, user_id, page=page)

    elif data.startswith("mem:del:"):
        parts = data.split(":")
        memory_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0

        from app.repos.memory import delete_memory

        success = await delete_memory(user_id, memory_id)
        if success:
            await query.answer("✅ Воспоминание удалено", show_alert=False)
            logging.info("User %d deleted memory %d", user_id, memory_id)
        else:
            await query.answer("❌ Не удалось удалить", show_alert=True)
            return

        # Re-render current page
        await _send_memory_page(query.message, user_id, page=page)


def register(application) -> None:
    """Register /memory command and callback handlers."""
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CallbackQueryHandler(memory_callback_handler, pattern=r"^mem:"))
