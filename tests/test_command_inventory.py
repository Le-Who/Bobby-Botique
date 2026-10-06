"""Command inventory reads current registrations, including conversation aliases."""

from types import SimpleNamespace

import pytest
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler, filters


async def callback(update, context):
    pass


async def another_callback(update, context):
    pass


def test_inventory_updates_when_handlers_and_aliases_are_added_or_removed():
    from app.command_inventory import command_inventory

    application = SimpleNamespace(handlers={0: [CommandHandler(["draw", "img"], callback)]})
    row = command_inventory(application)["commands"][0]
    assert row["command"] == "/draw"
    assert set(row["aliases"]) == {"/img"}
    assert row["public"] is True

    application.handlers[0].append(CommandHandler(["future", "later"], another_callback))
    assert {item["command"] for item in command_inventory(application)["commands"]} == {"/draw", "/future"}
    application.handlers[0].clear()
    assert command_inventory(application)["commands"] == []


def test_inventory_includes_cyrillic_text_rules_and_nested_fallbacks():
    from app.command_inventory import command_inventory

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("tarot", callback),
            MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r"(?i)^/(?:таро|расклад)(?:\s+|$)"), callback),
            MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r"(?i)^\s*(?:таро|расклад)\s*$"), callback),
        ],
        states={1: [CommandHandler("inside", another_callback)]},
        fallbacks=[CommandHandler("cancel", another_callback)],
        name="wizard",
    )
    application = SimpleNamespace(handlers={0: [conversation]})
    inventory = command_inventory(application)
    rows = {row["command"]: row for row in inventory["commands"]}
    assert {"/tarot", "/inside", "/cancel"} <= rows.keys()
    tarot = rows["/tarot"]
    assert {"/таро", "/расклад", "таро", "расклад"} <= set(tarot["aliases"])
    assert len(tarot["text_patterns"]) == 2
    assert all("wizard" in binding["context"] for binding in tarot["bindings"])
    assert any("state" in binding["context"] for binding in rows["/inside"]["bindings"])


def test_negated_regex_is_not_an_alias_and_startup_is_explicit():
    from app.command_inventory import command_inventory
    from app.handlers.tarot_chat import IsTarotEndSession

    application = SimpleNamespace(
        handlers={
            0: [
                CommandHandler("draw", callback),
                MessageHandler(filters.TEXT & ~filters.Regex("secret"), callback),
                MessageHandler(filters.TEXT & ~IsTarotEndSession(), callback),
            ]
        }
    )
    row = command_inventory(application)["commands"][0]
    assert row["aliases"] == []
    assert row["text_patterns"] == []
    absent = command_inventory(None)
    assert absent["available"] is False
    assert absent["commands"] == []


def test_real_registration_includes_natal_menu_and_service_commands():
    from app.command_inventory import command_inventory
    from app.handlers import commands, memory_commands, messages

    class RecordingApplication:
        def __init__(self):
            self.handlers = {}

        def add_handler(self, handler, group=0):
            self.handlers.setdefault(group, []).append(handler)

    application = RecordingApplication()
    commands.register(application)
    memory_commands.register(application)
    messages.register(application)
    inventory = command_inventory(application)
    rows = {row["command"]: row for row in inventory["commands"]}
    assert {"/natal", "/tarot", "/keys", "/models", "/memory", "/asr"} <= rows.keys()
    assert "натальная" in rows["/natal"]["aliases"]
    assert "/карта" in rows["/natal"]["aliases"]
    assert {"/img", "/image", "/generate"} <= set(rows["/draw"]["aliases"])
    assert "/horoscope" in rows["/horoscope_settings"]["aliases"]
    assert any("🛑 Завершить сеанс Таро" in row["aliases"] for row in inventory["commands"])


def test_literal_state_controls_are_included_while_numeric_fields_are_data():
    from app.command_inventory import command_inventory

    conversation = ConversationHandler(
        entry_points=[CommandHandler("natal", callback)],
        states={
            1: [
                MessageHandler(filters.Regex(r"(?i)^\s*(?:таро|расклад)\s*$"), another_callback),
                MessageHandler(filters.Regex("^⌨️ Выбрать вручную из списка$"), another_callback),
                MessageHandler(filters.Regex(r"^[0-9]{2}:[0-9]{2}$"), another_callback),
            ]
        },
        fallbacks=[],
        name="wizard",
    )
    rows = command_inventory(SimpleNamespace(handlers={0: [conversation]}))["commands"]
    text_rows = [row for row in rows if not row["command"]]
    assert len(text_rows) == 1
    assert {"таро", "расклад", "⌨️ Выбрать вручную из списка"} <= set(text_rows[0]["aliases"])
    assert len(text_rows[0]["text_patterns"]) == 2


@pytest.mark.parametrize("combined", [False, True])
def test_shared_callback_preserves_each_public_command_and_ambiguous_alias(combined):
    from app.command_inventory import command_inventory

    handlers = (
        [CommandHandler(["help", "draw"], callback)]
        if combined
        else [CommandHandler("help", callback), CommandHandler("draw", callback)]
    )
    handlers.append(CommandHandler("shortcut", callback))
    rows = {row["command"]: row for row in command_inventory(SimpleNamespace(handlers={0: handlers}))["commands"]}
    assert {"/help", "/draw", "/shortcut"} == rows.keys()
    assert rows["/help"]["public"] is True
    assert rows["/draw"]["public"] is True
    assert "/help" not in rows["/draw"]["aliases"]


def test_ambiguous_text_rule_keeps_its_own_identity():
    from app.command_inventory import command_inventory

    handlers = [
        CommandHandler("help", callback),
        CommandHandler("draw", callback),
        MessageHandler(filters.Regex("^open$"), callback),
    ]
    rows = command_inventory(SimpleNamespace(handlers={0: handlers}))["commands"]
    assert len(rows) == 3
    text_row = next(row for row in rows if not row["command"])
    assert text_row["aliases"] == ["open"]
    assert all(not row["text_patterns"] for row in rows if row["command"])


def test_partial_and_callable_callbacks_have_source_metadata_without_bound_arguments():
    from functools import partial

    from app.command_inventory import command_inventory

    async def bound(extra, update, context):
        """Bound handler."""

    class CallableHandler:
        async def __call__(self, update, context):
            """Callable handler."""

    handlers = [
        CommandHandler("partial", partial(bound, "test-only-bound-value")),
        CommandHandler("object", CallableHandler()),
    ]
    inventory = command_inventory(SimpleNamespace(handlers={0: handlers}))
    assert {row["command"] for row in inventory["commands"]} == {"/partial", "/object"}
    assert all(row["bindings"][0]["file"] == "tests/test_command_inventory.py" for row in inventory["commands"])
    assert "test-only-bound-value" not in str(inventory)
