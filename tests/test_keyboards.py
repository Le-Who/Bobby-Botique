"""
Tests for app/utils/keyboards.py - Keyboard builder utilities.
"""

import pytest
from app.utils.keyboards import (
    # Button builders
    back_button,
    cancel_button,
    confirm_button,
    confirm_cancel_row,
    new_topic_retry_row,
    # Pagination
    pagination_row,
    # Keyboard builders
    build_keyboard,
    build_item_list_keyboard,
    build_paginated_keyboard,
    # Common patterns
    main_menu_keyboard,
    after_response_keyboard,
    document_menu_keyboard,
)


class TestButtonBuilders:
    """Tests for individual button builder functions."""

    def test_back_button_default(self):
        buttons = back_button()
        assert len(buttons) == 1
        assert buttons[0].text == "⬅️ Назад"
        assert buttons[0].callback_data == "start_menu"

    def test_back_button_custom(self):
        buttons = back_button("custom_back", "Go Back")
        assert buttons[0].callback_data == "custom_back"
        assert buttons[0].text == "Go Back"

    def test_cancel_button(self):
        buttons = cancel_button()
        assert buttons[0].callback_data == "cancel"

    def test_confirm_button(self):
        buttons = confirm_button("confirm_action")
        assert buttons[0].callback_data == "confirm_action"

    def test_confirm_cancel_row(self):
        row = confirm_cancel_row("do_it", "cancel_it")
        assert len(row) == 2
        assert row[0].callback_data == "do_it"
        assert row[1].callback_data == "cancel_it"

    def test_new_topic_retry_row(self):
        row = new_topic_retry_row()
        assert len(row) == 2
        assert row[0].callback_data == "new_topic"
        assert row[1].callback_data == "retry_last"


class TestPaginationRow:
    """Tests for pagination_row function."""

    def test_first_page(self):
        """First page should have disabled prev, enabled next."""
        row = pagination_row(0, 5, "page")
        assert len(row) == 3
        assert row[0].callback_data == "noop"  # Prev disabled
        assert row[1].text == "1/5"
        assert row[2].callback_data == "page:1"  # Next enabled

    def test_middle_page(self):
        """Middle page should have both buttons enabled."""
        row = pagination_row(2, 5, "page")
        assert row[0].callback_data == "page:1"  # Prev
        assert row[1].text == "3/5"
        assert row[2].callback_data == "page:3"  # Next

    def test_last_page(self):
        """Last page should have disabled next."""
        row = pagination_row(4, 5, "page")
        assert row[0].callback_data == "page:3"  # Prev
        assert row[1].text == "5/5"
        assert row[2].callback_data == "noop"  # Next disabled

    def test_single_page(self):
        """Single page should have both disabled."""
        row = pagination_row(0, 1, "page")
        assert row[0].callback_data == "noop"
        assert row[2].callback_data == "noop"

    def test_without_page_number(self):
        """Can hide page number."""
        row = pagination_row(0, 5, "page", show_page_number=False)
        assert len(row) == 2


class TestBuildKeyboard:
    """Tests for build_keyboard function."""

    def test_empty_keyboard(self):
        kb = build_keyboard()
        assert len(kb.inline_keyboard) == 0

    def test_single_row(self):
        from telegram import InlineKeyboardButton

        row = [InlineKeyboardButton("Test", callback_data="test")]
        kb = build_keyboard(row)
        assert len(kb.inline_keyboard) == 1

    def test_with_back_button(self):
        from telegram import InlineKeyboardButton

        row = [InlineKeyboardButton("Test", callback_data="test")]
        kb = build_keyboard(row, back_to="menu")
        assert len(kb.inline_keyboard) == 2
        assert kb.inline_keyboard[1][0].callback_data == "menu"


class TestBuildItemListKeyboard:
    """Tests for build_item_list_keyboard function."""

    def test_basic_list(self):
        items = [("Item 1", "item1"), ("Item 2", "item2")]
        kb = build_item_list_keyboard(items, "back")
        # 2 items + 1 back = 3 rows
        assert len(kb.inline_keyboard) == 3
        assert kb.inline_keyboard[0][0].text == "Item 1"
        assert kb.inline_keyboard[2][0].callback_data == "back"

    def test_multi_column(self):
        items = [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d")]
        kb = build_item_list_keyboard(items, "back", items_per_row=2)
        # 2 rows of 2 items + 1 back = 3 rows
        assert len(kb.inline_keyboard) == 3
        assert len(kb.inline_keyboard[0]) == 2


class TestBuildPaginatedKeyboard:
    """Tests for build_paginated_keyboard function."""

    def test_first_page(self):
        items = [(f"Item {i}", f"item{i}") for i in range(15)]
        kb, total = build_paginated_keyboard(items, 0, 5, "page", "back")
        assert total == 3
        assert len(kb.inline_keyboard) == 7  # 5 items + pagination + back

    def test_page_clamping(self):
        items = [("A", "a"), ("B", "b")]
        kb, total = build_paginated_keyboard(items, 100, 5, "page", "back")
        assert total == 1
        # Should clamp to page 0

    def test_no_pagination_for_single_page(self):
        items = [("A", "a"), ("B", "b")]
        kb, total = build_paginated_keyboard(items, 0, 5, "page", "back")
        assert total == 1
        # 2 items + back (no pagination row)
        assert len(kb.inline_keyboard) == 3


class TestCommonKeyboards:
    """Tests for common keyboard patterns."""

    def test_main_menu_keyboard(self):
        kb = main_menu_keyboard()
        # Should have standard menu structure
        assert len(kb.inline_keyboard) >= 2

    def test_after_response_keyboard(self):
        kb = after_response_keyboard()
        assert kb is not None
        row = kb.inline_keyboard[0]
        callbacks = [b.callback_data for b in row]
        assert "new_topic" in callbacks
        assert "retry_last" in callbacks

    def test_after_response_no_buttons(self):
        kb = after_response_keyboard(include_new_topic=False, include_retry=False)
        assert kb is None

    def test_document_menu_keyboard(self):
        kb = document_menu_keyboard()
        assert len(kb.inline_keyboard) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
