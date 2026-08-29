"""Single source of truth for public bot commands and user-facing help.

Handler modules still own command behavior.  This catalog owns only the public
surface: Telegram's command menu, categorized help, localization keys, and
availability notes.  Administrative and developer commands deliberately stay
outside this module.
"""

import html
import logging
from dataclasses import dataclass
from typing import Literal

from telegram import BotCommand, InlineKeyboardButton

from app.i18n import t

CommandAvailability = Literal["always", "private_chat", "when_configured"]


@dataclass(frozen=True, slots=True)
class CommandCategory:
    """Localized help category shown in overview and callback navigation."""

    slug: str
    title_key: str
    button_key: str
    intro_key: str


@dataclass(frozen=True, slots=True)
class PublicCommand:
    """Stable public command identity and its user-facing metadata."""

    identity: str
    command: str
    category: str
    description_key: str
    availability: CommandAvailability = "always"


COMMAND_CATEGORIES = (
    CommandCategory("chat", "help.category.chat.title", "help.category.chat.button", "help.topic.chat"),
    CommandCategory(
        "personalize",
        "help.category.personalize.title",
        "help.category.personalize.button",
        "help.topic.personalize",
    ),
    CommandCategory("search", "help.category.search.title", "help.category.search.button", "help.topic.search"),
    CommandCategory("create", "help.category.create.title", "help.category.create.button", "help.topic.create"),
    CommandCategory(
        "history",
        "help.category.history.title",
        "help.category.history.button",
        "help.topic.history",
    ),
    CommandCategory(
        "privacy",
        "help.category.privacy.title",
        "help.category.privacy.button",
        "help.topic.privacy",
    ),
    CommandCategory("games", "help.category.games.title", "help.category.games.button", "help.topic.games"),
    CommandCategory(
        "insights",
        "help.category.insights.title",
        "help.category.insights.button",
        "help.topic.insights",
    ),
)

PUBLIC_COMMANDS = (
    PublicCommand("open_main_menu", "start", "chat", "help.command.start"),
    PublicCommand("open_help", "help", "chat", "help.command.help"),
    PublicCommand("start_fresh_chat", "newchat", "chat", "help.command.newchat"),
    PublicCommand("choose_model", "model", "personalize", "help.command.model"),
    PublicCommand("choose_role", "roles", "personalize", "help.command.roles"),
    PublicCommand("set_instruction", "setprompt", "personalize", "help.command.setprompt"),
    PublicCommand("set_thinking", "thinking", "personalize", "help.command.thinking"),
    PublicCommand("open_settings", "settings", "personalize", "help.command.settings"),
    PublicCommand("toggle_search", "res", "search", "help.command.res"),
    PublicCommand("subscribe_brief", "subscribe", "search", "help.command.subscribe", "private_chat"),
    PublicCommand("unsubscribe_brief", "unsubscribe", "search", "help.command.unsubscribe", "private_chat"),
    PublicCommand("manage_documents", "documents", "create", "help.command.documents", "private_chat"),
    PublicCommand("generate_image", "draw", "create", "help.command.draw"),
    PublicCommand("start_live_audio", "live", "create", "help.command.live", "when_configured"),
    PublicCommand("save_conversation", "save", "history", "help.command.save", "private_chat"),
    PublicCommand("list_conversations", "conversations", "history", "help.command.conversations", "private_chat"),
    PublicCommand("switch_conversation", "switch", "history", "help.command.switch", "private_chat"),
    PublicCommand("rename_conversation", "rename", "history", "help.command.rename", "private_chat"),
    PublicCommand("delete_conversation", "delete", "history", "help.command.delete", "private_chat"),
    PublicCommand("export_chat", "export", "history", "help.command.export", "private_chat"),
    PublicCommand("show_usage", "stats", "history", "help.command.stats"),
    PublicCommand("review_memory", "memory", "privacy", "help.command.memory", "private_chat"),
    PublicCommand("clear_memory", "clearmemory", "privacy", "help.command.clearmemory", "private_chat"),
    PublicCommand("export_personal_data", "mydata", "privacy", "help.command.mydata", "private_chat"),
    PublicCommand("delete_account", "deleteme", "privacy", "help.command.deleteme", "private_chat"),
    PublicCommand("open_games", "games", "games", "help.command.games", "when_configured"),
    PublicCommand("play_daily_crocodile", "dailycroc", "games", "help.command.dailycroc"),
    PublicCommand("play_daily_2048", "daily2048", "games", "help.command.daily2048"),
    PublicCommand("play_daily_trivia", "trivia", "games", "help.command.trivia"),
    PublicCommand("create_reminder", "remind", "games", "help.command.remind", "private_chat"),
    PublicCommand("start_tarot", "tarot", "insights", "help.command.tarot"),
    PublicCommand("create_natal_chart", "natal", "insights", "help.command.natal", "private_chat"),
    PublicCommand(
        "manage_horoscope",
        "horoscope_settings",
        "insights",
        "help.command.horoscope_settings",
        "private_chat",
    ),
    PublicCommand(
        "stop_horoscope",
        "horoscope_stop",
        "insights",
        "help.command.horoscope_stop",
        "private_chat",
    ),
)


def _validate_catalog() -> None:
    category_slugs = {category.slug for category in COMMAND_CATEGORIES}
    identities = [entry.identity for entry in PUBLIC_COMMANDS]
    commands = [entry.command for entry in PUBLIC_COMMANDS]
    if len(identities) != len(set(identities)):
        raise RuntimeError("public command identities must be unique")
    if len(commands) != len(set(commands)):
        raise RuntimeError("public command names must be unique")
    unknown_categories = {entry.category for entry in PUBLIC_COMMANDS} - category_slugs
    if unknown_categories:
        raise RuntimeError(f"public commands use unknown categories: {sorted(unknown_categories)!r}")


_validate_catalog()


def language_from_telegram(language_code: str | None) -> str:
    """Map Telegram's UI hint to one of the supported help languages."""
    return "en" if (language_code or "").lower().startswith("en") else "ru"


def _commands_in_category(slug: str) -> tuple[PublicCommand, ...]:
    return tuple(entry for entry in PUBLIC_COMMANDS if entry.category == slug)


def _command_line(entry: PublicCommand, lang: str) -> str:
    description = html.escape(t(entry.description_key, lang))
    return f"• <code>/{entry.command}</code> — {description}"


def render_help_overview(lang: str) -> str:
    """Render a compact, parse-safe overview from the public catalog."""
    parts = [
        f"<b>{html.escape(t('help.overview.title', lang))}</b>",
        html.escape(t("help.overview.intro", lang)),
    ]
    for category in COMMAND_CATEGORIES:
        command_links = " · ".join(f"<code>/{entry.command}</code>" for entry in _commands_in_category(category.slug))
        parts.extend(
            [
                f"<b>{html.escape(t(category.title_key, lang))}</b>",
                command_links,
            ]
        )
    parts.append(html.escape(t("help.overview.footer", lang)))
    return "\n\n".join(parts)


def render_help_topic(slug: str, lang: str) -> str:
    """Render one localized help journey with catalog-derived command lines."""
    category = next((item for item in COMMAND_CATEGORIES if item.slug == slug), None)
    if category is None:
        return html.escape(t("help.topic_not_found", lang))
    lines = [
        f"<b>{html.escape(t(category.title_key, lang))}</b>",
        html.escape(t(category.intro_key, lang)),
        *(_command_line(entry, lang) for entry in _commands_in_category(slug)),
    ]
    return "\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:])


def build_help_topic_rows(lang: str) -> list[list[InlineKeyboardButton]]:
    """Build the shared two-column category keyboard."""
    buttons = [
        InlineKeyboardButton(t(category.button_key, lang), callback_data=f"help_topic:{lang}:{category.slug}")
        for category in COMMAND_CATEGORIES
    ]
    return [buttons[index : index + 2] for index in range(0, len(buttons), 2)]


def build_help_overview_rows(lang: str) -> list[list[InlineKeyboardButton]]:
    """Build the identical language-stable overview keyboard for commands and callbacks."""
    rows = build_help_topic_rows(lang)
    rows.append([InlineKeyboardButton(t("menu.back_to_menu", lang), callback_data="start_menu")])
    return rows


def build_telegram_commands(lang: str) -> list[BotCommand]:
    """Build Telegram's public command menu in stable catalog order."""
    return [BotCommand(entry.command, t(entry.description_key, lang)) for entry in PUBLIC_COMMANDS]


async def install_public_command_menu(bot) -> None:
    """Install Russian defaults plus an English Telegram-language override."""
    try:
        await bot.set_my_commands(build_telegram_commands("ru"))
        await bot.set_my_commands(build_telegram_commands("en"), language_code="en")
    except Exception as error:
        logging.warning("Could not update Telegram public command menu: %s", error)


__all__ = [
    "COMMAND_CATEGORIES",
    "PUBLIC_COMMANDS",
    "CommandCategory",
    "PublicCommand",
    "build_help_overview_rows",
    "build_help_topic_rows",
    "build_telegram_commands",
    "install_public_command_menu",
    "language_from_telegram",
    "render_help_overview",
    "render_help_topic",
]
