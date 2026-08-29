"""Contracts for the public command catalog, help, and Telegram menu."""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CommandHandler, ConversationHandler

from app.bot_commands import (
    COMMAND_CATEGORIES,
    PUBLIC_COMMANDS,
    build_help_overview_rows,
    build_telegram_commands,
    install_public_command_menu,
    render_help_overview,
    render_help_topic,
)
from app.handlers import commands, memory_commands
from app.handlers.scheduled_briefs import subscribe_command, unsubscribe_command
from app.i18n import t

ADMIN_AND_HIDDEN_COMMANDS = {
    "adduser",
    "admin",
    "asr",
    "cachestats",
    "checkgeminikeys",
    "checktavilykeys",
    "clearcache",
    "clearolddocs",
    "clearoldmetrics",
    "dailycroc_status",
    "deluser",
    "docstats",
    "groupstats",
    "keys",
    "listmodels",
    "listusers",
    "metrics",
    "models",
    "queuestats",
    "registergroup",
    "reloadconfig",
    "rolemetrics",
    "wordbank",
}


class _RecordingApplication:
    def __init__(self) -> None:
        self.handlers = []

    def add_handler(self, handler, group=0) -> None:
        self.handlers.append(handler)


def _command_callbacks(handler) -> dict[str, set]:
    callbacks: dict[str, set] = {}
    if isinstance(handler, CommandHandler):
        for command in handler.commands:
            callbacks.setdefault(command, set()).add(handler.callback)
    elif isinstance(handler, ConversationHandler):
        nested = [*handler.entry_points, *handler.fallbacks]
        for state_handlers in handler.states.values():
            nested.extend(state_handlers)
        for child in nested:
            for command, command_callbacks in _command_callbacks(child).items():
                callbacks.setdefault(command, set()).update(command_callbacks)
    return callbacks


def _registered_commands() -> dict[str, set]:
    application = _RecordingApplication()
    commands.register(application)
    memory_commands.register(application)
    callbacks: dict[str, set] = {}
    for handler in application.handlers:
        for command, command_callbacks in _command_callbacks(handler).items():
            callbacks.setdefault(command, set()).update(command_callbacks)
    return callbacks


def test_every_public_catalog_command_is_registered() -> None:
    registered = _registered_commands()
    catalog_names = {entry.command for entry in PUBLIC_COMMANDS}

    assert catalog_names <= registered.keys()
    assert catalog_names.isdisjoint(ADMIN_AND_HIDDEN_COMMANDS)


def test_readme_documents_every_public_catalog_command() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    missing = [entry.command for entry in PUBLIC_COMMANDS if f"/{entry.command}" not in readme]

    assert missing == []


def test_brief_subscription_commands_are_registered_as_a_matching_pair() -> None:
    registered = _registered_commands()

    assert subscribe_command in registered["subscribe"]
    assert unsubscribe_command in registered["unsubscribe"]


def test_every_catalog_localization_key_has_russian_and_english_text() -> None:
    keys = {
        *(category.title_key for category in COMMAND_CATEGORIES),
        *(category.button_key for category in COMMAND_CATEGORIES),
        *(category.intro_key for category in COMMAND_CATEGORIES),
        *(entry.description_key for entry in PUBLIC_COMMANDS),
        "help.overview.title",
        "help.overview.intro",
        "help.overview.footer",
        "help.back_to_help",
        "help.topic_not_found",
    }

    for key in keys:
        assert t(key, "ru") != key, key
        assert t(key, "en") != key, key


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_telegram_command_menu_respects_platform_limits(lang: str) -> None:
    menu = build_telegram_commands(lang)

    assert [item.command for item in menu] == [entry.command for entry in PUBLIC_COMMANDS]
    assert len(menu) <= 100
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", item.command) for item in menu)
    assert all(1 <= len(item.description) <= 256 for item in menu)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_generated_help_is_categorized_html_safe_and_mentions_only_public_commands(lang: str) -> None:
    overview = render_help_overview(lang)
    topic_texts = [render_help_topic(category.slug, lang) for category in COMMAND_CATEGORIES]
    combined = "\n".join([overview, *topic_texts])
    documented_commands = set(re.findall(r"<code>/([a-z0-9_]+)</code>", combined))

    assert documented_commands == {entry.command for entry in PUBLIC_COMMANDS}
    assert overview.count("<b>") == overview.count("</b>")
    assert combined.count("<code>") == combined.count("</code>")
    assert not re.search(r"\b(?:RLS|provenance|epoch|provider|graph)\b", combined, re.IGNORECASE)
    assert "/tarot_settings" not in combined


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_help_overview_keyboard_preserves_language_and_has_main_menu(lang: str) -> None:
    rows = build_help_overview_rows(lang)
    callback_data = [button.callback_data for row in rows for button in row]

    assert f"help_topic:{lang}:chat" in callback_data
    assert callback_data[-1] == "start_menu"


@pytest.mark.asyncio
async def test_public_command_menu_installs_default_russian_and_english_override() -> None:
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    await install_public_command_menu(bot)

    assert bot.set_my_commands.await_count == 2
    default_call, english_call = bot.set_my_commands.await_args_list
    assert default_call.kwargs == {}
    assert english_call.kwargs == {"language_code": "en"}
    assert default_call.args[0] == build_telegram_commands("ru")
    assert english_call.args[0] == build_telegram_commands("en")
