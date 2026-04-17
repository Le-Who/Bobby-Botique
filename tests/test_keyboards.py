"""
Tests for app/utils/keyboards.py - Keyboard builder utilities.
"""

import pytest

from app.utils.keyboards import (
    after_response_keyboard,
    ai_response_keyboard,
    # Button builders
    back_button,
    build_item_list_keyboard,
    # Keyboard builders
    build_keyboard,
    build_paginated_keyboard,
    cancel_button,
    confirm_button,
    confirm_cancel_row,
    deep_dive_keyboard,
    error_with_back_keyboard,
    # Common patterns
    feedback_row,
    new_topic_retry_row,
    # Pagination
    pagination_row,
)


def setup_module(module):
    import importlib
    import sys
    from unittest.mock import MagicMock

    # Reload telegram to flush mocks
    if "telegram" in sys.modules and isinstance(sys.modules["telegram"], MagicMock):
        del sys.modules["telegram"]
    if "telegram.ext" in sys.modules and isinstance(sys.modules["telegram.ext"], MagicMock):
        del sys.modules["telegram.ext"]

    if "app.utils.keyboards" in sys.modules:
        reloaded = importlib.reload(sys.modules["app.utils.keyboards"])
        for attr in dir(reloaded):
            if not attr.startswith("_"):
                setattr(module, attr, getattr(reloaded, attr))


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

    def test_feedback_row(self):
        row = feedback_row()
        assert len(row) == 3
        callbacks = [b.callback_data for b in row]
        assert "feedback:up" in callbacks
        assert "feedback:down" in callbacks
        assert "retry_last" in callbacks

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

    def test_ai_response_keyboard(self):
        kb = ai_response_keyboard()
        assert len(kb.inline_keyboard) >= 2
        # First row should be feedback
        assert kb.inline_keyboard[0][0].callback_data == "feedback:up"

    def test_deep_dive_keyboard(self):
        kb = deep_dive_keyboard(is_last_part=True)
        assert len(kb.inline_keyboard) >= 3

    def test_error_with_back_keyboard(self):
        kb = error_with_back_keyboard("test_back", "Go Back")
        assert len(kb.inline_keyboard) == 1
        assert kb.inline_keyboard[0][0].callback_data == "test_back"

    def test_error_with_back_keyboard_extra_buttons(self):
        from telegram import InlineKeyboardButton

        extra = [[InlineKeyboardButton("Retry", callback_data="retry")]]
        kb = error_with_back_keyboard("back", extra_buttons=extra)
        assert len(kb.inline_keyboard) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
