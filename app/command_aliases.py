"""Versioned, literal aliases at the existing Telegram registration boundaries."""

from collections.abc import Mapping
from typing import Any

from telegram import Message, Update
from telegram.ext import CommandHandler, ConversationHandler, MessageHandler, filters

from app.command_inventory import registered_commands
from app.runtime_settings.lifecycle import current_operation_snapshot
from app.runtime_settings.store import SettingsSnapshot

PREFIX = "command_aliases:"
MAX_ALIASES = 40


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _catalog(application: Any) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    inventory, bindings = registered_commands(application)
    owners: dict[int, set[str]] = {}
    for identity, handlers in bindings.items():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                owners.setdefault(id(handler), set()).add(identity)
    for row in inventory["commands"]:
        native = [handler for handler in bindings[row["id"]] if isinstance(handler, CommandHandler)]
        row["editable"] = bool(native) and all(
            type(handler) in (CommandHandler, AliasCommandHandler) and len(owners[id(handler)]) == 1
            for handler in native
        )
    return inventory, bindings


def command_alias_inventory(application: Any, snapshot: SettingsSnapshot) -> dict[str, Any]:
    inventory, _ = _catalog(application)
    for row in inventory["commands"]:
        row["default_aliases"] = list(row["aliases"])
        configured = snapshot.values.get(f"{PREFIX}{row['id']}") if row["editable"] else None
        row["source"] = "override" if configured is not None else "default"
        if configured is not None:
            row["aliases"] = list(configured)
        row["alias_note"] = (
            "Основная команда сохраняется. После сохранения текстовые правила принимают только перечисленные алиасы. "
            "Сброс возвращает исходные алиасы и регулярные выражения."
            if row["editable"]
            else "Текстовый вход или общий обработчик нескольких команд: правила меняются вместе с кодом диалога."
        )
    inventory.update(revision=snapshot.revision, degraded=snapshot.degraded)
    inventory["note"] = (
        "Алиасы применяются со следующего сообщения; другие экземпляры получают настройки при чтении кэша (TTL 5 с). "
        "Основные команды и ограничения доступа сохраняются."
        if inventory["available"]
        else inventory["note"]
    )
    return inventory


def _validated_aliases(identity: str, value: Any) -> list[str]:
    from app.natal.compatibility import looks_like_birth_date_input

    if not isinstance(value, (list, tuple)) or len(value) > MAX_ALIASES:
        raise ValueError(f"Укажите список, не более {MAX_ALIASES} алиасов")
    result = []
    for alias in value:
        if not isinstance(alias, str) or not alias.strip() or any(not char.isprintable() for char in alias):
            raise ValueError("Алиас должен быть непустой строкой без переносов и управляющих символов")
        alias = _normalize(alias)
        if alias.startswith("/"):
            name = alias[1:]
            if not 1 <= len(name) <= 32 or not all(char.isalnum() or char == "_" for char in name):
                raise ValueError("Slash-алиас: 1–32 буквы, цифры или подчёркивания после /, без @ и аргументов")
        elif len(alias) > 80 or "/" in alias or "@" in alias:
            raise ValueError("Текстовый алиас: до 80 символов, без / и @")
        elif looks_like_birth_date_input(alias):
            raise ValueError("Форматы ввода даты зарезервированы для личных форм и не могут быть алиасами")
        if alias == f"/{identity}":
            raise ValueError("Основная команда уже работает и не должна входить в список алиасов")
        if alias in result:
            raise ValueError(f"Алиас повторяется: {alias}")
        result.append(alias)
    return result


def validate_aliases(application: Any, identity: str, value: Any) -> list[str]:
    inventory, _ = _catalog(application)
    row = next((row for row in inventory["commands"] if row["id"] == identity), None)
    if row is None or not row["editable"]:
        raise ValueError("Команда недоступна для редактирования алиасов")
    return _validated_aliases(identity, value)


def validate_alias_values(application: Any, values: Mapping[str, Any]) -> None:
    """Validate the complete candidate revision inside the store's CAS write."""
    import re

    inventory, _ = _catalog(application)
    rows = {row["id"]: row for row in inventory["commands"]}
    claimed: dict[str, str] = {}
    for key, value in values.items():
        if not key.startswith(PREFIX):
            continue
        identity = key.removeprefix(PREFIX)
        row = rows.get(identity)
        if row is None:
            continue  # Retired registrations remain inactive in historical revisions.
        if not row["editable"]:
            raise ValueError("Сохранённый алиас ссылается на общий или текстовый обработчик")
        aliases = _validated_aliases(identity, value)
        own_defaults = {_normalize(alias) for alias in row["aliases"]}
        for alias in aliases:
            if alias in claimed and claimed[alias] != identity:
                raise ValueError(f"Алиас {alias} уже назначен команде /{claimed[alias]}")
            claimed[alias] = identity
            for other in inventory["commands"]:
                if other["id"] == identity:
                    continue
                if alias == _normalize(other["command"]) or (
                    alias not in own_defaults and alias in {_normalize(item) for item in other["aliases"]}
                ):
                    raise ValueError(f"Алиас {alias} занят другим входом: {other['command'] or other['title']}")
                if alias in own_defaults:
                    continue
                for rule in other["text_patterns"]:
                    pattern = re.compile(rule["pattern"], rule["flags"])
                    # Menu guards share another callback but the same input rule.
                    if any(pattern.search(default) for default in row["aliases"]):
                        continue
                    if pattern.search(alias):
                        raise ValueError(f"Алиас {alias} пересекается с текстовым правилом другого входа")


async def save_aliases(application: Any, identity: str, value: Any, *, expected_revision: int, actor: str):
    from app.runtime_settings.store import update_values

    aliases = validate_aliases(application, identity, value)
    return await update_values(
        {f"{PREFIX}{identity}": aliases},
        expected_revision=expected_revision,
        actor=actor,
        validate=lambda values: validate_alias_values(application, values),
    )


async def reset_aliases(application: Any, identity: str, *, expected_revision: int, actor: str):
    from app.runtime_settings.store import update_values

    validate_aliases(application, identity, [])
    return await update_values(
        {},
        removals=(f"{PREFIX}{identity}",),
        expected_revision=expected_revision,
        actor=actor,
        validate=lambda values: validate_alias_values(application, values),
    )


def _override(identity: str) -> tuple[str, ...] | None:
    snapshot = current_operation_snapshot()
    value = snapshot.values.get(f"{PREFIX}{identity}") if snapshot is not None else None
    return tuple(value) if value is not None else None


def matches_alias_input(identity: str, text: str | None, *, bot_username: str | None = None) -> bool | None:
    """Let direct intent readers honor a pinned override, or keep their defaults."""
    aliases = _override(identity)
    if aliases is None:
        return None
    value = _normalize(text or "")
    if value.startswith("/"):
        value, separator, mention = value.split()[0].partition("@")
        if separator and (bot_username is None or mention != bot_username.casefold()):
            return False
    return value == f"/{identity}" or value in aliases


def alias_message_input(message: Message, *, check_mention: bool = True) -> tuple[str, list[str]] | None:
    if not message.text:
        return None
    text = message.text.strip()
    if not text.startswith("/"):
        return _normalize(text), []
    parts = text.split()
    alias, separator, mention = parts[0].partition("@")
    if separator and check_mention:
        bot = message.get_bot()
        if not bot or mention.casefold() != bot.username.casefold():
            return None
    return alias.casefold(), parts[1:]


class AliasCommandHandler(CommandHandler):
    """Keep native argument/filter/block semantics and the same callback identity."""

    def __init__(self, original: CommandHandler, identity: str, defaults: list[str], has_text_rules: bool):
        super().__init__(
            original.commands,
            original.callback,
            filters=original.filters,
            block=original.block,
            has_args=original.has_args,
        )
        self.identity = identity
        self.defaults = frozenset(_normalize(alias) for alias in defaults)
        self.has_text_rules = has_text_rules

    def check_update(self, update: object) -> Any:
        aliases = _override(self.identity)
        if aliases is None or not isinstance(update, Update) or not update.effective_message:
            return super().check_update(update)
        parsed = alias_message_input(update.effective_message)
        if parsed is None:
            return None
        alias, args = parsed
        if alias == f"/{self.identity}":
            return super().check_update(update)
        if alias not in aliases:
            return None
        native = super().check_update(update)
        if native:
            return native
        # Existing text rules retain their original placement and access filters.
        if alias in self.defaults or (not alias.startswith("/") and self.has_text_rules):
            return None
        if update.message is None or (not alias.startswith("/") and update.message.chat.type != "private"):
            return None
        if not self._check_correct_args(args):
            return None
        result = self.filters.check_update(update)
        return (args, result) if result else False


class AliasRegex(filters.Regex):
    """Replace one positive alias rule while retaining its surrounding filters."""

    def __init__(self, pattern: Any, identity: str, witnesses: dict[bool, str]):
        super().__init__(pattern)
        self.identity = identity
        self.witnesses = witnesses

    def filter(self, message: Message) -> Any:
        aliases = _override(self.identity)
        if aliases is None:
            return super().filter(message)
        parsed = alias_message_input(message)
        if parsed is None:
            return {}
        alias, _ = parsed
        if alias == f"/{self.identity}":
            return super().filter(message)
        witness = self.witnesses.get(alias.startswith("/"))
        if alias not in aliases or witness is None:
            return {}
        match = self.pattern.search(witness)
        return {"matches": [match]} if match else {}


class AliasCommandFilter(filters.MessageFilter):
    """Recognize native aliases before free-text inputs, including fallbacks."""

    def __init__(self, rows: list[dict[str, Any]]):
        super().__init__(name="filters.COMMAND")
        self.rows = rows

    def filter(self, message: Message) -> bool:
        if filters.COMMAND.filter(message):
            return True
        # Like Telegram's entity filter, classify commands for any bot. The
        # handler and guards independently validate who the command addresses.
        parsed = alias_message_input(message, check_mention=False)
        if parsed is None:
            return False
        alias, _ = parsed
        for row in self.rows:
            aliases = _override(row["id"])
            if aliases is not None and alias in aliases:
                return alias.startswith("/") or (not row["text_patterns"] and message.chat.type == "private")
        return False


def _alias_filter(value: Any, rows: list[dict[str, Any]]) -> Any:
    if isinstance(value, (AliasRegex, AliasCommandFilter)):
        return value
    if value is filters.COMMAND:
        return AliasCommandFilter(rows)
    inverted = getattr(value, "inv_filter", None)
    if inverted is not None:
        return ~_alias_filter(inverted, rows)
    if type(value) is filters.Regex:
        matches = {
            row["id"]: {
                alias.startswith("/"): alias
                for alias in [row["command"], *row["aliases"]]
                if value.pattern.search(alias)
            }
            for row in rows
        }
        matches = {identity: witnesses for identity, witnesses in matches.items() if witnesses}
        if len(matches) == 1:
            identity, witnesses = next(iter(matches.items()))
            return AliasRegex(value.pattern, identity, witnesses)
        return value
    base = getattr(value, "base_filter", None)
    if base is not None:
        for attribute, operation in (("and_filter", "and"), ("or_filter", "or"), ("xor_filter", "xor")):
            other = getattr(value, attribute, None)
            if other is not None:
                left, right = _alias_filter(base, rows), _alias_filter(other, rows)
                return left & right if operation == "and" else left | right if operation == "or" else left ^ right
    return value


def install_command_aliases(application: Any) -> None:
    """Install once after registration, before PTB initializes conversation state."""
    inventory, bindings = _catalog(application)
    rows = [row for row in inventory["commands"] if row["editable"]]
    native = {
        id(handler): row for row in rows for handler in bindings[row["id"]] if isinstance(handler, CommandHandler)
    }
    replacements: dict[int, Any] = {}

    def install(handlers: list[Any]) -> None:
        for index, handler in enumerate(handlers):
            if isinstance(handler, ConversationHandler):
                install(handler.entry_points)
                for children in handler.states.values():
                    install(children)
                install(handler.fallbacks)
            elif isinstance(handler, CommandHandler) and id(handler) in native:
                row = native[id(handler)]
                if id(handler) not in replacements:
                    replacements[id(handler)] = AliasCommandHandler(
                        handler, row["id"], row["aliases"], bool(row["text_patterns"])
                    )
                handlers[index] = replacements[id(handler)]
            elif isinstance(handler, MessageHandler):
                handler.filters = _alias_filter(handler.filters, rows)

    for handlers in application.handlers.values():
        install(handlers)
