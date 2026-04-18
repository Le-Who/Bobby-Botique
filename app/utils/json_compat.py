# /app/utils/json_compat.py
"""High-performance JSON drop-in replacement backed by orjson.

orjson (Rust/PyO3) is 2-6× faster than stdlib json for both
serialization and deserialization, with native support for
datetime, UUID, dataclass, and numpy types.

Usage:
    from app.utils.json_compat import json   # drop-in namespace
    # or
    from app.utils.json_compat import dumps, loads, dumps_bytes
"""

from __future__ import annotations

from typing import Any

import orjson

# ── Public API ───────────────────────────────────────────────────────────────

_DEFAULT_OPTS = orjson.OPT_NON_STR_KEYS


def dumps(
    obj: Any,
    *,
    ensure_ascii: bool = False,  # ignored — orjson always emits UTF-8
    indent: int | None = None,
    default: Any = None,
    separators: tuple[str, str] | None = None,  # ignored — orjson uses compact
    sort_keys: bool = False,
) -> str:
    """Serialize *obj* to a JSON ``str`` (stdlib-compatible signature).

    Unlike raw ``orjson.dumps`` this returns ``str``, not ``bytes``,
    so it can be used as a drop-in replacement for ``json.dumps``.
    """
    opts = _DEFAULT_OPTS
    if indent:
        opts |= orjson.OPT_INDENT_2
    if sort_keys:
        opts |= orjson.OPT_SORT_KEYS
    return orjson.dumps(obj, option=opts, default=default).decode("utf-8")


def dumps_bytes(obj: Any, *, default: Any = None) -> bytes:
    """Serialize *obj* to JSON ``bytes`` — optimal for Redis / binary I/O."""
    return orjson.dumps(obj, option=_DEFAULT_OPTS, default=default)


def loads(s: str | bytes | bytearray | memoryview) -> Any:
    """Deserialize a JSON string or bytes to a Python object."""
    return orjson.loads(s)


# ── Namespace object for `import json_compat as json` style usage ────────────

class _JsonNamespace:
    """Provides ``json.dumps`` / ``json.loads`` compatible namespace."""

    dumps = staticmethod(dumps)
    loads = staticmethod(loads)
    dumps_bytes = staticmethod(dumps_bytes)

    # stdlib json aliases for rare usages
    JSONDecodeError = orjson.JSONDecodeError


json = _JsonNamespace()
