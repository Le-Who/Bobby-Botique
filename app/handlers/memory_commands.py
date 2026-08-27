# /app/handlers/memory_commands.py
"""User-facing /memory command with inline pagination and delete controls.

Provides transparency into what the bot "remembers" and lets users
delete individual memories via inline buttons.
"""

import asyncio
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
    if getattr(update.effective_chat, "type", None) != "private":
        await update.message.reply_text("🔒 Управление личной памятью доступно только в приватном чате с ботом.")
        return
    user_id = update.effective_user.id
    await _send_memory_page(update.message, user_id, page=0)


async def _send_memory_page(target, user_id: int, page: int = 0) -> None:
    """Render a single page of memories with inline controls.

    ``target`` is either ``update.message`` (for commands) or
    ``query.message`` (for callback navigation).
    """

    from app.repos.memory import get_memory_stats, list_memories

    page = max(0, page)
    offset = page * MEMORIES_PER_PAGE
    # ⚡ Bolt Optimization: Fetch memories and stats concurrently to reduce DB wait time
    memories, stats = await asyncio.gather(
        list_memories(user_id, offset=offset, limit=MEMORIES_PER_PAGE), get_memory_stats(user_id)
    )
    total = stats.get("total_memories", 0) if stats else 0
    total_pages = max(1, (total + MEMORIES_PER_PAGE - 1) // MEMORIES_PER_PAGE)

    # A deletion on the last page can make the callback's page stale.  Clamp
    # and re-read instead of rendering impossible counters such as ``3/1``.
    clamped_page = min(page, total_pages - 1)
    if clamped_page != page:
        page = clamped_page
        offset = page * MEMORIES_PER_PAGE
        memories = await list_memories(
            user_id,
            offset=offset,
            limit=MEMORIES_PER_PAGE,
        )

    if not memories and page == 0:
        empty_text = "📭 У вас больше нет сохранённых воспоминаний."
        if hasattr(target, "edit_text"):
            try:
                await target.edit_text(empty_text, reply_markup=None)
                return
            except Exception:
                pass
        await target.reply_text(empty_text)
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
                callback_data=f"mem:{user_id}:del:{m['id']}:{page}",
            )
        )
    if delete_row:
        buttons.append(delete_row)

    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"mem:{user_id}:page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"mem:{user_id}:noop"))
    if offset + MEMORIES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"mem:{user_id}:page:{page + 1}"))
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


@authorized_only
@safe_handler("❌ Ошибка управления воспоминаниями.")
async def memory_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button callbacks for memory management."""
    query = update.callback_query
    if query is None:
        return
    user_id = update.effective_user.id
    if getattr(update.effective_chat, "type", None) != "private" or query.message is None:
        await query.answer("🔒 Личная память доступна только в приватном чате", show_alert=True)
        return

    parts = str(query.data or "").split(":")
    try:
        owner_id = int(parts[1])
        action = parts[2]
    except IndexError, TypeError, ValueError:
        await query.answer("Кнопка устарела. Откройте /memory заново.", show_alert=True)
        return

    if owner_id != user_id:
        await query.answer("Эта панель памяти принадлежит другому пользователю.", show_alert=True)
        return

    if action == "noop":
        await query.answer()
        return

    if action == "page":
        try:
            page = max(0, int(parts[3]))
        except IndexError, ValueError:
            await query.answer("Некорректная страница", show_alert=True)
            return
        await query.answer()
        await _send_memory_page(query.message, user_id, page=page)

    elif action == "del":
        try:
            memory_id = int(parts[3])
            page = max(0, int(parts[4])) if len(parts) > 4 else 0
        except IndexError, ValueError:
            await query.answer("Некорректное воспоминание", show_alert=True)
            return

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
    else:
        await query.answer("Кнопка устарела. Откройте /memory заново.", show_alert=True)


def register(application) -> None:
    """Register /memory command and callback handlers."""
    application.add_handler(CommandHandler("memory", memory_command))
    application.add_handler(CallbackQueryHandler(memory_callback_handler, pattern=r"^mem:"))
