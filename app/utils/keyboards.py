"""
Centralized keyboard builders for Telegram bot UI.

This module provides reusable keyboard components to reduce duplication
and ensure consistent UI patterns across the bot.
"""

from typing import Optional, List, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# =============================================================================
# BASIC BUTTON BUILDERS
# =============================================================================


def back_button(
    callback: str = "start_menu", text: str = "⬅️ Назад"
) -> List[InlineKeyboardButton]:
    """Creates a back button row."""
    return [InlineKeyboardButton(text, callback_data=callback)]


def cancel_button(
    callback: str = "cancel", text: str = "❌ Отмена"
) -> List[InlineKeyboardButton]:
    """Creates a cancel button row."""
    return [InlineKeyboardButton(text, callback_data=callback)]


def confirm_button(
    callback: str, text: str = "✅ Подтвердить"
) -> List[InlineKeyboardButton]:
    """Creates a confirm button row."""
    return [InlineKeyboardButton(text, callback_data=callback)]


# =============================================================================
# COMPOUND BUTTON ROWS
# =============================================================================


def confirm_cancel_row(
    confirm_callback: str,
    cancel_callback: str = "cancel",
    confirm_text: str = "✅ Да",
    cancel_text: str = "❌ Нет",
) -> List[InlineKeyboardButton]:
    """Creates a confirm/cancel button row."""
    return [
        InlineKeyboardButton(confirm_text, callback_data=confirm_callback),
        InlineKeyboardButton(cancel_text, callback_data=cancel_callback),
    ]


def new_topic_retry_row() -> List[InlineKeyboardButton]:
    """Creates standard row with New Topic and Retry buttons."""
    return [
        InlineKeyboardButton("🆕 Новая тема", callback_data="new_topic"),
        InlineKeyboardButton("🔁 Повторить", callback_data="retry_last"),
    ]


# =============================================================================
# PAGINATION
# =============================================================================


def pagination_row(
    current_page: int,
    total_pages: int,
    callback_prefix: str,
    show_page_number: bool = True,
) -> List[InlineKeyboardButton]:
    """
    Generates pagination buttons: [⬅️] [1/5] [➡️]

    Args:
        current_page: Current page (0-indexed)
        total_pages: Total number of pages
        callback_prefix: Prefix for callback data (e.g., "role_page:my_roles")
        show_page_number: Whether to show page counter in the middle

    Returns:
        List of buttons for pagination row
    """
    buttons = []

    # Previous button
    if current_page > 0:
        buttons.append(
            InlineKeyboardButton(
                "⬅️", callback_data=f"{callback_prefix}:{current_page - 1}"
            )
        )
    else:
        buttons.append(InlineKeyboardButton("⏺️", callback_data="noop"))

    # Page counter
    if show_page_number:
        buttons.append(
            InlineKeyboardButton(
                f"{current_page + 1}/{total_pages}", callback_data="noop"
            )
        )

    # Next button
    if current_page < total_pages - 1:
        buttons.append(
            InlineKeyboardButton(
                "➡️", callback_data=f"{callback_prefix}:{current_page + 1}"
            )
        )
    else:
        buttons.append(InlineKeyboardButton("⏺️", callback_data="noop"))

    return buttons


# =============================================================================
# KEYBOARD BUILDERS
# =============================================================================


def build_keyboard(
    *rows: List[InlineKeyboardButton],
    back_to: Optional[str] = None,
    back_text: str = "⬅️ Назад",
) -> InlineKeyboardMarkup:
    """
    Builds keyboard from rows with optional back button.

    Args:
        *rows: Button rows to include
        back_to: If provided, adds back button with this callback
        back_text: Text for the back button

    Returns:
        InlineKeyboardMarkup ready to use

    Usage:
        keyboard = build_keyboard(
            [button1, button2],
            [button3],
            back_to="start_menu"
        )
    """
    keyboard = [list(row) for row in rows if row]
    if back_to:
        keyboard.append(back_button(back_to, back_text))
    return InlineKeyboardMarkup(keyboard)


def build_item_list_keyboard(
    items: List[Tuple[str, str]], back_callback: str, items_per_row: int = 1
) -> InlineKeyboardMarkup:
    """
    Builds a keyboard from a list of items.

    Args:
        items: List of (text, callback_data) tuples
        back_callback: Callback for back button
        items_per_row: Number of items per row (default 1)

    Returns:
        InlineKeyboardMarkup with items and back button
    """
    keyboard = []
    current_row = []

    for text, callback in items:
        current_row.append(InlineKeyboardButton(text, callback_data=callback))
        if len(current_row) >= items_per_row:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    keyboard.append(back_button(back_callback))
    return InlineKeyboardMarkup(keyboard)


def build_paginated_keyboard(
    items: List[Tuple[str, str]],
    page: int,
    items_per_page: int,
    callback_prefix: str,
    back_callback: str,
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None,
) -> Tuple[InlineKeyboardMarkup, int]:
    """
    Builds a paginated keyboard for lists.

    Args:
        items: All items as (text, callback) tuples
        page: Current page (0-indexed)
        items_per_page: Items to show per page
        callback_prefix: Prefix for pagination callbacks
        back_callback: Callback for back button
        extra_rows: Additional button rows to add before back

    Returns:
        Tuple of (keyboard, total_pages)
    """
    total_items = len(items)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)

    # Clamp page
    page = max(0, min(page, total_pages - 1))

    # Get current page items
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    current_items = items[start_idx:end_idx]

    keyboard = []

    # Item buttons
    for text, callback in current_items:
        keyboard.append([InlineKeyboardButton(text, callback_data=callback)])

    # Pagination row (if needed)
    if total_pages > 1:
        keyboard.append(pagination_row(page, total_pages, callback_prefix))

    # Extra rows
    if extra_rows:
        keyboard.extend(extra_rows)

    # Back button
    keyboard.append(back_button(back_callback))

    return InlineKeyboardMarkup(keyboard), total_pages


# =============================================================================
# COMMON KEYBOARD PATTERNS
# =============================================================================


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Returns the main menu keyboard used in /start."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat"),
                InlineKeyboardButton("⚙️ Модели", callback_data="model_menu"),
            ],
            [
                InlineKeyboardButton("🎭 Роли", callback_data="open_roles"),
                InlineKeyboardButton("📚 Справка", callback_data="help"),
            ],
        ]
    )


def after_response_keyboard(
    include_new_topic: bool = True,
    include_retry: bool = True,
    custom_buttons: Optional[List[InlineKeyboardButton]] = None,
) -> InlineKeyboardMarkup:
    """
    Returns keyboard shown after AI response.

    Args:
        include_new_topic: Include "New Topic" button
        include_retry: Include "Retry" button
        custom_buttons: Additional buttons to add
    """
    row = []
    if include_new_topic:
        row.append(InlineKeyboardButton("🆕 Новая тема", callback_data="new_topic"))
    if include_retry:
        row.append(InlineKeyboardButton("🔁 Повторить", callback_data="retry_last"))

    keyboard = [row] if row else []
    if custom_buttons:
        keyboard.append(list(custom_buttons))

    return InlineKeyboardMarkup(keyboard) if keyboard else None


def document_menu_keyboard() -> InlineKeyboardMarkup:
    """Returns the document management menu keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📄 Загрузить новый", callback_data="doc:upload_new"
                )
            ],
            [InlineKeyboardButton("📋 Выбрать", callback_data="doc:select_document")],
            [InlineKeyboardButton("🗑️ Очистить все", callback_data="doc:clear_all")],
        ]
    )
