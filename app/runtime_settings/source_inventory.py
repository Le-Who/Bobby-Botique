"""Discover controlled definitions and literal consumers from shipped Python source.

Source is parsed, never executed. Dynamic IDs are reported separately: this index
does not infer capabilities, follow arbitrary control flow, or prove reachability.
The immutable checkout is indexed once per process; a new release gets a new index.
"""

import ast
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[2]

_PROCESS_READERS = {
    "resolve_process",
    "execute_text_process",
    "run_gemini_override",
    "run_result_process",
    "_generate_controlled_media",
}
_KEYWORD_PROCESS_READERS = {"generation_request_from_history", "_get_ai_response_with_routing"}
_CROC_READERS = {"get_daily_text_model_for", "generate_daily_text_for"}
_PROMPT_READERS = {
    "get_prompt_text",
    "get_task_prompt",
    "render_additional_prompt",
    "_render_natal_prompt",
}


@dataclass(frozen=True, slots=True)
class SourceLocation:
    file: str
    function: str
    line: int

    def as_dict(self) -> dict[str, str | int]:
        return {"file": self.file, "function": self.function, "line": self.line}


@dataclass(frozen=True, slots=True)
class DynamicReference:
    kind: str
    expression: str
    location: SourceLocation


@dataclass(frozen=True, slots=True)
class SourceInventory:
    prompt_modules: tuple[str, ...]
    prompt_definitions: Mapping[str, tuple[SourceLocation, ...]]
    prompt_consumers: Mapping[str, tuple[SourceLocation, ...]]
    process_consumers: Mapping[str, tuple[SourceLocation, ...]]
    dynamic_references: tuple[DynamicReference, ...]


def _strings(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.IfExp):
        return (*_strings(node.body), *_strings(node.orelse))
    return ()


def _argument(node: ast.Call, keyword: str, *, positional: bool = True) -> ast.expr | None:
    for entry in node.keywords:
        if entry.arg == keyword:
            return entry.value
    return node.args[0] if positional and node.args else None


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.aliases: dict[str, str] = {}
        self.definitions: dict[str, list[SourceLocation]] = defaultdict(list)
        self.prompts: dict[str, list[SourceLocation]] = defaultdict(list)
        self.processes: dict[str, list[SourceLocation]] = defaultdict(list)
        self.dynamic: list[DynamicReference] = []
        self.has_definitions = False

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for entry in node.names:
            self.aliases[entry.asname or entry.name] = entry.name

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    def _record(self, kind: str, argument: ast.expr | None, node: ast.Call, *, prefix: str = "") -> None:
        if argument is None or (isinstance(argument, ast.Constant) and argument.value is None):
            return
        location = SourceLocation(self.path, ".".join(self.scope) or "<module>", node.lineno)
        values = _strings(argument)
        target = self.processes if kind == "process" else self.prompts
        for value in values:
            target[prefix + value].append(location)
        # A mixed conditional can still contain an unresolved branch.
        if not values or (
            isinstance(argument, ast.IfExp) and (not _strings(argument.body) or not _strings(argument.orelse))
        ):
            self.dynamic.append(DynamicReference(kind, ast.unparse(argument), location))

    def visit_Call(self, node: ast.Call) -> None:
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        name = self.aliases.get(name, name)
        if name == "register_controlled_text":
            # Definitions belong at import time, not inside a request handler.
            argument = _argument(node, "name")
            if not self.scope:
                self.has_definitions = True
                for value in _strings(argument) if argument is not None else ():
                    self.definitions[value].append(SourceLocation(self.path, "<module>", node.lineno))
        elif name in _PROCESS_READERS:
            self._record("process", _argument(node, "process_id"), node)
        elif name in _KEYWORD_PROCESS_READERS:
            self._record("process", _argument(node, "process_id", positional=False), node)
        elif name in _CROC_READERS:
            self._record("process", _argument(node, "process"), node, prefix="crocodile.")
        elif name in _PROMPT_READERS:
            self._record("prompt", _argument(node, "name"), node)
        elif name == "get" and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (isinstance(owner, ast.Name) and owner.id == "registry") or (
                isinstance(owner, ast.Call) and getattr(owner.func, "id", "") == "get_registry"
            ):
                self._record("prompt", _argument(node, "name"), node)
        self.generic_visit(node)


def scan_sources(root: Path = ROOT) -> SourceInventory:
    """Index application source only; tests, revision bundles and secrets are excluded."""
    definitions: dict[str, list[SourceLocation]] = defaultdict(list)
    prompts: dict[str, list[SourceLocation]] = defaultdict(list)
    processes: dict[str, list[SourceLocation]] = defaultdict(list)
    modules = []
    dynamic = []
    for path in sorted((root / "app").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        collector = _Collector(relative)
        collector.visit(tree)
        if collector.has_definitions:
            module = relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            modules.append(module)
        for target, values in (
            (definitions, collector.definitions),
            (prompts, collector.prompts),
            (processes, collector.processes),
        ):
            for identity, locations in values.items():
                target[identity].extend(locations)
        dynamic.extend(collector.dynamic)
    return SourceInventory(
        tuple(modules),
        MappingProxyType({name: tuple(dict.fromkeys(rows)) for name, rows in definitions.items()}),
        MappingProxyType({name: tuple(dict.fromkeys(rows)) for name, rows in prompts.items()}),
        MappingProxyType({name: tuple(dict.fromkeys(rows)) for name, rows in processes.items()}),
        tuple(dynamic),
    )


@lru_cache(maxsize=1)
def get_source_inventory() -> SourceInventory:
    return scan_sources()
