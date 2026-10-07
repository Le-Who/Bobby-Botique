"""Read the live Telegram input surface without maintaining a second command list."""

import inspect
import re
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

from telegram.ext import CommandHandler, ConversationHandler, MessageHandler, filters

from app.bot_commands import PUBLIC_COMMANDS
from app.i18n import t

ROOT = Path(__file__).resolve().parents[1]


def _handlers(handler: Any, context: str) -> Iterator[tuple[Any, str]]:
    if isinstance(handler, ConversationHandler):
        name = handler.name or "conversation"
        for child in handler.entry_points:
            yield from _handlers(child, f"{context} / {name} / entry")
        for state, children in handler.states.items():
            for child in children:
                yield from _handlers(child, f"{context} / {name} / state {state}")
        for child in handler.fallbacks:
            yield from _handlers(child, f"{context} / {name} / fallback")
    else:
        yield handler, context


def _positive_patterns(value: Any) -> Iterator[re.Pattern[str]]:
    declared = getattr(type(value), "inventory_pattern", None)
    if isinstance(value, filters.Regex):
        yield value.pattern
    elif isinstance(declared, re.Pattern):
        yield declared
    elif getattr(value, "inv_filter", None) is None:
        # These are PTB's filter-composition attributes. Negative conditions
        # cannot introduce input aliases; the complete filter is retained below.
        for name in ("base_filter", "and_filter", "or_filter", "xor_filter"):
            child = getattr(value, name, None)
            if child is not None:
                yield from _positive_patterns(child)


def _positive_aliases(value: Any) -> Iterator[str]:
    if getattr(value, "inv_filter", None) is not None:
        return
    # Custom filters opt in with class-level static inputs, never instance state.
    declared = getattr(type(value), "inventory_aliases", ())
    if isinstance(declared, tuple):
        yield from (alias for alias in declared if isinstance(alias, str) and alias)
    for name in ("base_filter", "and_filter", "or_filter", "xor_filter"):
        child = getattr(value, name, None)
        if child is not None:
            yield from _positive_aliases(child)


def _examples(pattern: re.Pattern[str]) -> list[str]:
    r"""Extract a few accepted literal examples, never expand an arbitrary regex.

    Exact patterns remain the authoritative input rules. Prefixes with \w
    suffixes are omitted here rather than presenting truncated words as aliases.
    """
    text = re.sub(r"\(\?[aiLmsux-]+\)", "", pattern.pattern)
    text = re.sub(r"[^\W\d_]+\\w[*+?]", "", text)
    text = re.sub(r"\\s[+*?]?", " ", text)
    text = re.sub(r"\\.", " ", text)
    words = re.findall(r"[^\W\d_]+(?: [^\W\d_]+)*", text)
    candidates = {word.strip() for word in words}
    literal = pattern.pattern.removeprefix("^").removesuffix("$")
    if literal and not re.search(r"[\\[\](){}?*+|^$.]", literal):
        candidates.add(literal)
    if "/" in pattern.pattern:
        candidates.update(f"/{word}" for word in tuple(candidates))
    return sorted(candidate for candidate in candidates if candidate and pattern.fullmatch(candidate))


def _source_callback(handler: Any) -> Any:
    callback = inspect.unwrap(handler.callback)
    while isinstance(callback, partial):
        callback = inspect.unwrap(callback.func)
    if not inspect.isfunction(callback) and not inspect.ismethod(callback) and callable(callback):
        callback = inspect.unwrap(callback.__call__)
    return callback


def _callback_key(handler: Any) -> str:
    callback = _source_callback(handler)
    module = getattr(callback, "__module__", type(callback).__module__)
    name = getattr(callback, "__qualname__", type(callback).__qualname__)
    return f"{module}.{name}"


def _binding(handler: Any, context: str) -> dict[str, Any]:
    callback = _source_callback(handler)
    try:
        file = inspect.getsourcefile(callback)
    except TypeError, OSError:
        file = None
    relative = ""
    if file:
        try:
            relative = Path(file).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            pass
    return {
        "handler": _callback_key(handler),
        "file": relative,
        "line": getattr(getattr(callback, "__code__", None), "co_firstlineno", None),
        "context": context,
        "filters": str(getattr(handler, "filters", "")),
    }


def registered_commands(application: Any | None) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    """Snapshot registered commands, positive text rules and conversation controls.

    Callback data and arbitrary AI-classified intent are different input surfaces.
    No handler is invoked and no Telegram API, settings store or user data is read.
    """
    if application is None:
        return {"available": False, "commands": [], "note": "Бот ещё не зарегистрировал обработчики команд."}, {}
    leaves = [
        pair
        for group, handlers in application.handlers.items()
        for handler in handlers
        for pair in _handlers(handler, f"group {group}")
    ]
    public = {entry.command: entry for entry in PUBLIC_COMMANDS}
    preferred: dict[int, set[str]] = {}
    for handler, _ in leaves:
        if isinstance(handler, CommandHandler):
            public_names = {entry.command for entry in PUBLIC_COMMANDS if entry.command in handler.commands}
            if public_names:
                preferred.setdefault(id(inspect.unwrap(handler.callback)), set()).update(public_names)

    rows: dict[str, dict[str, Any]] = {}
    bindings: dict[str, list[Any]] = {}
    callbacks: dict[int, set[str]] = {}
    text_identities: dict[int, str] = {}

    def row_for(identity: str, handler: Any, command: str = "") -> dict[str, Any]:
        if identity not in rows:
            entry = public.get(command)
            original = _source_callback(handler)
            title = (
                t(entry.description_key, "ru")
                if entry
                else (inspect.getdoc(original) or getattr(original, "__name__", None) or type(original).__name__)
            )
            rows[identity] = {
                "id": identity,
                "command": f"/{command}" if command else "",
                "title": title.split("\n", 1)[0],
                "public": entry is not None,
                "category": entry.category if entry else "service",
                "availability": entry.availability if entry else "handler_specific",
                "aliases": [],
                "text_patterns": [],
                "bindings": [],
            }
        return rows[identity]

    for handler, context in leaves:
        if not isinstance(handler, CommandHandler):
            continue
        callback = id(inspect.unwrap(handler.callback))
        candidates = preferred.get(callback, set())
        canonicals = sorted(set(handler.commands) & public.keys())
        if not canonicals:
            canonicals = [next(iter(candidates)) if len(candidates) == 1 else sorted(handler.commands)[0]]
        for canonical in canonicals:
            row = row_for(canonical, handler, canonical)
            row["aliases"].extend(
                f"/{name}" for name in sorted(handler.commands) if name != canonical and name not in public
            )
            row["bindings"].append(_binding(handler, context))
            bindings.setdefault(canonical, []).append(handler)
            callbacks.setdefault(callback, set()).add(canonical)

    for handler, context in leaves:
        if not isinstance(handler, MessageHandler):
            continue
        patterns = list(dict.fromkeys(_positive_patterns(handler.filters)))
        declared_aliases = list(dict.fromkeys(_positive_aliases(handler.filters)))
        if not patterns and not declared_aliases:
            continue
        callback = id(inspect.unwrap(handler.callback))
        identities = callbacks.get(callback, set())
        # Free-text fields within a conversation are data entry, unless they
        # explicitly share a command callback. Entry/fallback rules are included.
        if (
            not identities
            and " / state " in context
            and not declared_aliases
            and not any(_examples(pattern) for pattern in patterns)
        ):
            continue
        if len(identities) != 1:
            # Callback equality does not select one command when a dispatcher
            # handles multiple identities. Keep the input rule independently.
            identity = text_identities.setdefault(callback, f"text:{_callback_key(handler)}:{len(text_identities)}")
            identities = {identity}
        for identity in sorted(identities):
            row = row_for(identity, handler)
            row["aliases"].extend(declared_aliases)
            for pattern in patterns:
                examples = _examples(pattern)
                rule = {"pattern": pattern.pattern, "flags": pattern.flags, "examples": examples}
                if rule not in row["text_patterns"]:
                    row["text_patterns"].append(rule)
                row["aliases"].extend(examples)
            row["bindings"].append(_binding(handler, context))
            bindings.setdefault(identity, []).append(handler)

    for row in rows.values():
        row["aliases"] = sorted(set(row["aliases"]) - {row["command"]})
    return {
        "available": True,
        "commands": sorted(rows.values(), key=lambda row: (not row["public"], row["command"] or row["id"])),
        "note": (
            "Из реально зарегистрированных обработчиков этого экземпляра. "
            "Текстовые алиасы показаны как примеры; полные правила и контекст доступны в карточках. "
            "Доступ определяется фильтрами и проверками обработчиков."
        ),
    }, bindings


def command_inventory(application: Any | None) -> dict[str, Any]:
    """Read registered inputs without settings, provider calls or user data."""
    return registered_commands(application)[0]
