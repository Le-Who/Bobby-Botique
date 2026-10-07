"""Offline routing and revision checks for configurable Telegram inputs."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram import Update, User
from telegram.ext import Application, CallbackContext, CommandHandler, ConversationHandler, MessageHandler, filters

from app.command_aliases import command_alias_inventory, install_command_aliases, save_aliases, validate_aliases
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.runtime_settings.store import RuntimeSettingsStore, SettingsSnapshot


def make_application(handlers):
    application = Application.builder().token("123:offline-test-token").build()
    application.bot._bot_user = User(123, "Test", is_bot=True, username="test_bot")
    for group, handler in handlers:
        application.add_handler(handler, group=group)
    return application


def make_update(application, text, *, chat_type="private", edited=False):
    message = {
        "message_id": 1,
        "date": 1,
        "chat": {"id": 42, "type": chat_type},
        "from": {"id": 42, "first_name": "Test", "is_bot": False},
        "text": text,
    }
    if text.startswith("/") and text.split()[0].isascii():
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": 1, "edited_message" if edited else "message": message}, application.bot)


async def callback(update, context):
    return 1


@pytest.mark.asyncio
async def test_aliases_keep_canonical_args_mentions_filters_and_edited_update_guard():
    application = make_application([(0, CommandHandler(["draw", "img"], callback, filters=filters.ChatType.PRIVATE))])
    install_command_aliases(application)
    handler = application.handlers[0][0]
    snapshot = SettingsSnapshot(1, {"command_aliases:draw": ("/paint", "/рисуй", "нарисуй")})
    async with runtime_settings_scope(snapshot):
        assert handler.check_update(make_update(application, "/draw hello"))[0] == ["hello"]
        assert not handler.check_update(make_update(application, "/img hello"))
        result = handler.check_update(make_update(application, "/paint@test_bot red sky"))
        assert result[0] == ["red", "sky"]
        context = CallbackContext(application)
        handler.collect_additional_context(context, make_update(application, "/paint red sky"), application, result)
        assert context.args == ["red", "sky"]
        assert handler.check_update(make_update(application, "/рисуй дерево"))[0] == ["дерево"]
        assert handler.check_update(make_update(application, "НАРИСУЙ"))[0] == []
        assert not handler.check_update(make_update(application, "/paint@other_bot hello"))
        assert not handler.check_update(make_update(application, "/paint hello", chat_type="group"))
        assert not handler.check_update(make_update(application, "нарисуй", chat_type="group"))
        assert not handler.check_update(make_update(application, "/рисуй дерево", edited=True))
    async with runtime_settings_scope(SettingsSnapshot(2, {})):
        assert handler.check_update(make_update(application, "/img hello"))[0] == ["hello"]
        assert not handler.check_update(make_update(application, "/paint hello"))


@pytest.mark.asyncio
async def test_text_aliases_keep_conversation_transitions_and_private_menu_guards():
    seen = []

    async def start(update, context):
        seen.append("start")
        return 1

    async def finish(update, context):
        seen.append("finish")
        return ConversationHandler.END

    async def guard(update, context):
        seen.append("guard")

    text_rule = filters.ChatType.PRIVATE & filters.Regex(r"(?i)^таро$")
    conversation = ConversationHandler(
        entry_points=[CommandHandler("tarot", start), MessageHandler(text_rule, start)],
        states={1: [CommandHandler("cancel", finish)]},
        fallbacks=[],
        name="alias_wizard",
    )
    application = make_application([(-1, MessageHandler(text_rule, guard)), (0, conversation)])
    install_command_aliases(application)
    application._initialized = True
    async with runtime_settings_scope(
        SettingsSnapshot(3, {"command_aliases:tarot": ("карты",), "command_aliases:cancel": ("/stop",)})
    ):
        assert not conversation.check_update(make_update(application, "таро"))
        assert not conversation.check_update(make_update(application, "карты", chat_type="group"))
        await application.process_update(make_update(application, "КАРТЫ"))
        assert seen == ["guard", "start"]
        assert conversation._conversations == {(42, 42): 1}
        await application.process_update(make_update(application, "/stop"))
        assert seen == ["guard", "start", "finish"]
        assert conversation._conversations == {}


@pytest.mark.asyncio
async def test_concurrent_updates_use_their_pinned_alias_revision():
    application = make_application([(0, CommandHandler("draw", callback))])
    install_command_aliases(application)
    handler = application.handlers[0][0]
    ready = asyncio.Event()
    finished = asyncio.Event()

    async def first():
        async with runtime_settings_scope(SettingsSnapshot(1, {"command_aliases:draw": ("/paint",)})):
            ready.set()
            await finished.wait()
            assert handler.check_update(make_update(application, "/paint"))
            assert not handler.check_update(make_update(application, "/etch"))

    async def second():
        await ready.wait()
        async with runtime_settings_scope(SettingsSnapshot(2, {"command_aliases:draw": ("/etch",)})):
            assert handler.check_update(make_update(application, "/etch"))
            assert not handler.check_update(make_update(application, "/paint"))
        finished.set()

    await asyncio.gather(first(), second())


@pytest.mark.parametrize(
    "aliases",
    [
        ["/a", "/A"],
        ["/draw"],
        ["/name@bot"],
        ["/two words"],
        ["line\nbreak"],
        [""],
        ["x" * 81],
        ["/" + "x" * 33],
        ["x"] * 41,
    ],
)
def test_rejects_invalid_aliases(aliases):
    application = SimpleNamespace(handlers={0: [CommandHandler("draw", callback)]})
    with pytest.raises(ValueError):
        validate_aliases(application, "draw", aliases)


@pytest.mark.asyncio
async def test_conflicting_alias_is_rejected_by_the_atomic_writer(monkeypatch):
    from app.runtime_settings import store
    from tests.test_runtime_settings_store import Database

    database = Database()
    backend = RuntimeSettingsStore()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    monkeypatch.setattr(store, "update_values", backend.update_values)
    application = SimpleNamespace(handlers={0: [CommandHandler("draw", callback), CommandHandler("help", callback)]})
    await save_aliases(application, "draw", ["/paint"], expected_revision=0, actor="admin")
    for conflict in ("/paint", "/draw"):
        with pytest.raises(ValueError, match="занят|назначен"):
            await save_aliases(application, "help", [conflict], expected_revision=1, actor="admin")
    assert database.writes == 1
    fresh = RuntimeSettingsStore()
    snapshot = await fresh.get_snapshot(force=True)
    assert snapshot.revision == 1
    assert snapshot.values["command_aliases:draw"] == ("/paint",)


def test_shared_command_handler_is_readonly():
    application = SimpleNamespace(handlers={0: [CommandHandler(["help", "draw"], callback)]})
    inventory = command_alias_inventory(application, SettingsSnapshot(0, {}))
    assert all(not row["editable"] for row in inventory["commands"])
    with pytest.raises(ValueError, match="недоступна"):
        validate_aliases(application, "draw", ["/paint"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,text", [("tarot", "колода"), ("compatibility", "колода"), ("compatibility", "/колода")])
async def test_new_tarot_alias_precedes_active_tarot_and_compatibility_inputs(mode, text):
    from app import state
    from app.handlers import commands, messages
    from app.handlers.cmd_tarot import tarot_command

    application = make_application([])
    commands.register(application)
    messages.register(application)
    install_command_aliases(application)
    if mode == "tarot":
        state.get_user_state(42).tarot_mode = True
    else:
        application.user_data[42]["compatibility_flow"] = {"stage": "first"}
    snapshot = SettingsSnapshot(1, {"command_aliases:tarot": ("колода", "/колода")})
    async with runtime_settings_scope(snapshot):
        update = make_update(application, text)
        selected = next(handler for handler in application.handlers[0] if handler.check_update(update))
        assert selected.callback is tarot_command
        if mode == "compatibility":
            assert any(handler.check_update(update) for group in (-90, -89) for handler in application.handlers[group])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/stop", "/стоп", "отмена"])
async def test_cancel_alias_keeps_pending_flow_until_its_own_handler(text):
    from app.handlers import commands, messages
    from app.handlers.compatibility import cancel_compatibility

    application = make_application([])
    commands.register(application)
    messages.register(application)
    install_command_aliases(application)
    application.user_data[42]["compatibility_flow"] = {"stage": "first"}
    async with runtime_settings_scope(SettingsSnapshot(1, {"command_aliases:cancel": ("/stop", "/стоп", "отмена")})):
        update = make_update(application, text)
        context = CallbackContext.from_update(update, application)
        for group in (-90, -89):
            for handler in application.handlers[group]:
                if handler.check_update(update):
                    await handler.callback(update, context)
        selected = next((handler for handler in application.handlers[0] if handler.check_update(update)), None)
        assert selected is not None
        assert selected.callback is cancel_compatibility
        assert "compatibility_flow" in context.user_data


@pytest.mark.asyncio
async def test_text_cancel_alias_reaches_conversation_fallback_before_free_text():
    async def finish(update, context):
        return ConversationHandler.END

    conversation = ConversationHandler(
        entry_points=[CommandHandler("natal", callback)],
        states={1: [MessageHandler(filters.TEXT & ~filters.COMMAND, callback)]},
        fallbacks=[CommandHandler("cancel", finish)],
        name="alias_fallback",
    )
    application = make_application([(0, conversation)])
    install_command_aliases(application)
    conversation._conversations[(42, 42)] = 1
    async with runtime_settings_scope(SettingsSnapshot(1, {"command_aliases:cancel": ("отмена", "/стоп")})):
        for text in ("отмена", "/стоп"):
            selected = conversation.check_update(make_update(application, text))
            assert selected[2] is conversation.fallbacks[0]


@pytest.mark.asyncio
async def test_natal_direct_intent_reader_respects_literal_and_empty_overrides():
    from app.natal.intent import is_natal_chart_request

    async with runtime_settings_scope(SettingsSnapshot(1, {"command_aliases:natal": ()})):
        for text in ("натальная", "birth chart", "натальная карта"):
            assert not is_natal_chart_request(text)
        assert is_natal_chart_request("/natal")
    async with runtime_settings_scope(SettingsSnapshot(2, {"command_aliases:natal": ("астрокарта",)})):
        assert is_natal_chart_request("АСТРОКАРТА")
        assert not is_natal_chart_request("birth chart")
    async with runtime_settings_scope(SettingsSnapshot(3, {})):
        assert is_natal_chart_request("birth chart")


@pytest.mark.parametrize("text", ["01.01.2000", "2000-01-01"])
def test_birth_date_formats_cannot_be_used_to_bypass_private_input_guards(text):
    application = SimpleNamespace(handlers={0: [CommandHandler("draw", callback)]})
    with pytest.raises(ValueError, match="дат"):
        validate_aliases(application, "draw", [text])


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_cyrillic_alias_addressed_to_other_bot_neither_routes_nor_clears_flow(pending):
    from app.handlers import commands, messages
    from app.natal.intent import is_natal_chart_request

    application = make_application([])
    commands.register(application)
    messages.register(application)
    install_command_aliases(application)
    if pending:
        application.user_data[42]["compatibility_flow"] = {"stage": "first"}
    async with runtime_settings_scope(SettingsSnapshot(1, {"command_aliases:natal": ("/астро",)})):
        assert not is_natal_chart_request("/астро@other_bot")
        update = make_update(application, "/астро@other_bot")
        context = CallbackContext.from_update(update, application)
        for group in sorted(group for group in application.handlers if group < 0):
            for handler in application.handlers[group]:
                if handler.check_update(update):
                    await handler.callback(update, context)
        assert not any(handler.check_update(update) for handler in application.handlers[0])
        assert ("compatibility_flow" in context.user_data) is pending
