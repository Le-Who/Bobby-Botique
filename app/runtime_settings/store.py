"""Atomic versioned settings in the migration-owned global_settings table.

Only trusted admin consumers may mutate this store. It is not a secret store:
consumers must allowlist their fields and exclude credentials and user content.
Every SQL operation owns its transaction-local admin RLS context. Writes never
retry automatically, including when the commit outcome is unknown.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from app import database as db
from app.utils.json_compat import json

_KEY = "runtime_controls:v1"
_PREFIXES = ("process:", "prompt:", "model_limit:", "catalog:", "catalog_baseline:", "legacy_model:")
_VALUE_LIMIT = 256 * 1024
_VALUES_LIMIT = 512 * 1024
_DOCUMENT_LIMIT = 17 * 1024 * 1024
_HISTORY_LIMIT = 30
_QUERY_TIMEOUT = 5.0
_DOCUMENT_FIELDS = frozenset({"version", "revision", "values", "history", "actor", "changed_keys", "at"})
_ENTRY_FIELDS = _DOCUMENT_FIELDS - {"version", "history"}


class RevisionConflict(ValueError):
    """The expected revision or database compare-and-swap no longer matches."""


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    revision: int
    values: Mapping[str, Any]
    degraded: bool = False


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("Settings JSON nesting exceeds the limit")
    if value is None or type(value) in (str, bool):
        return
    if type(value) is int and -(2**63) <= value < 2**63:
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item, depth + 1)
        return
    raise ValueError("Settings require bounded JSON values")


def _key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key.startswith(_PREFIXES)
        or not key.partition(":")[2]
        or len(key) > 160
        or any(ord(character) < 32 for character in key)
    ):
        raise ValueError("Invalid runtime setting key")


def _values(values: Any) -> None:
    if not isinstance(values, dict) or len(values) > 512:
        raise ValueError("Invalid runtime settings map")
    for key, value in values.items():
        _key(key)
        _validate_json(value)
        if len(json.dumps(value).encode("utf-8")) > _VALUE_LIMIT:
            raise ValueError("Setting value exceeds the size limit")
    if len(json.dumps(values).encode("utf-8")) > _VALUES_LIMIT:
        raise ValueError("Settings exceed the size limit")


def _baseline() -> dict[str, Any]:
    return {"version": 1, "revision": 0, "values": {}, "history": [], "actor": "", "changed_keys": [], "at": ""}


def _decode(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return _baseline()
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _DOCUMENT_LIMIT:
        raise ValueError("Invalid settings document")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != 1:
        raise ValueError("Unsupported settings version")
    if doc.keys() != _DOCUMENT_FIELDS:
        raise ValueError("Invalid settings document fields")
    entries = doc.get("history")
    if not isinstance(entries, list) or len(entries) > _HISTORY_LIMIT:
        raise ValueError("Invalid settings history")
    previous = -1
    for entry in [*entries, doc]:
        if not isinstance(entry, dict) or type(entry.get("revision")) is not int or entry["revision"] <= previous:
            raise ValueError("Invalid settings revision")
        if entry is not doc and entry.keys() != _ENTRY_FIELDS:
            raise ValueError("Invalid settings history fields")
        previous = entry["revision"]
        _values(entry.get("values"))
        if not isinstance(entry.get("actor", ""), str) or len(entry.get("actor", "")) > 128:
            raise ValueError("Invalid settings actor")
        if not isinstance(entry.get("at", ""), str) or len(entry.get("at", "")) > 64:
            raise ValueError("Invalid settings timestamp")
        keys = entry.get("changed_keys", [])
        if not isinstance(keys, list) or len(keys) > 1024:
            raise ValueError("Invalid settings changed keys")
        for key in keys:
            _key(key)
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate settings changed key")
    return doc


async def _query(sql: str, *params: Any) -> Any:
    pool = db.db_manager.pool
    if pool is None:
        raise ConnectionError("Runtime settings database is unavailable")
    async with asyncio.timeout(_QUERY_TIMEOUT):
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT set_config('app.is_admin', 'true', true)")
            return await connection.fetch(sql, *params)


class RuntimeSettingsStore:
    """Per-process five-second cache with immutable, isolated public snapshots."""

    def __init__(self) -> None:
        self._document = _baseline()
        self._snapshot = SettingsSnapshot(0, MappingProxyType({}), degraded=True)
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _read(self) -> tuple[str | None, dict[str, Any]]:
        rows = await _query("SELECT value_data FROM global_settings WHERE key_name = $1", _KEY)
        raw = rows[0]["value_data"] if rows else None
        return raw, _decode(raw)

    def _accept(self, document: dict[str, Any]) -> SettingsSnapshot:
        self._document = document
        self._snapshot = SettingsSnapshot(document["revision"], _freeze(document["values"]))
        self._expires = time.monotonic() + 5
        return self._snapshot

    async def get_snapshot(self, *, force: bool = False) -> SettingsSnapshot:
        if not force and time.monotonic() < self._expires:
            return self._snapshot
        async with self._lock:
            if not force and time.monotonic() < self._expires:
                return self._snapshot
            try:
                _, document = await self._read()
            except Exception:
                # Neither exception messages nor persisted values belong in logs.
                self._snapshot = replace(self._snapshot, degraded=True)
                self._expires = time.monotonic() + 5
                return self._snapshot
            return self._accept(document)

    async def _change(
        self,
        *,
        expected_revision: int,
        actor: str,
        key: str | None = None,
        value: Any = None,
        reset: bool = False,
        revision: int | None = None,
        validate: Callable[[Mapping[str, Any]], None] | None = None,
        updates: dict[str, Any] | None = None,
        removals: tuple[str, ...] = (),
    ) -> SettingsSnapshot:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid expected revision")
        if not isinstance(actor, str) or not actor or len(actor) > 128 or any(ord(c) < 32 for c in actor):
            raise ValueError("Invalid settings actor")
        if key is not None:
            _key(key)
            if not reset:
                _values({key: value})
                value = json.loads(json.dumps(value))
        if updates is not None:
            _values(updates)
            updates = json.loads(json.dumps(updates))
        for removed_key in removals:
            _key(removed_key)
        async with self._lock:
            try:
                raw, document = await self._read()
                if document["revision"] != expected_revision:
                    self._accept(document)
                    raise RevisionConflict("Runtime settings revision changed; refresh before saving")
                values = dict(document["values"])
                if revision is not None:
                    source = next(
                        (item for item in [*document["history"], document] if item["revision"] == revision), None
                    )
                    if source is None:
                        raise ValueError("Requested revision is no longer available")
                    values = dict(source["values"])
                elif key is not None:
                    if reset:
                        values.pop(key, None)
                    else:
                        values[key] = value
                if updates is not None:
                    values.update(updates)
                for removed_key in removals:
                    values.pop(removed_key, None)
                _values(values)
                if validate is not None:
                    validate(_freeze(values))
                changed = sorted(
                    k
                    for k in values.keys() | document["values"].keys()
                    if values.get(k) != document["values"].get(k) or (k in values) != (k in document["values"])
                )
                previous = {k: v for k, v in document.items() if k not in ("history", "version")}
                updated = {
                    "version": 1,
                    "revision": expected_revision + 1,
                    "values": values,
                    "history": [*document["history"], previous][-_HISTORY_LIMIT:],
                    "actor": actor,
                    "at": datetime.now(UTC).isoformat(),
                    "changed_keys": changed,
                }
                encoded = json.dumps(updated)
                if len(encoded.encode("utf-8")) > _DOCUMENT_LIMIT:
                    raise ValueError("Settings history exceeds the size limit")
                if raw is None:
                    rows = await _query(
                        "INSERT INTO global_settings (key_name, value_data) VALUES ($1, $2) "
                        "ON CONFLICT (key_name) DO NOTHING RETURNING value_data",
                        _KEY,
                        encoded,
                    )
                else:
                    rows = await _query(
                        "UPDATE global_settings SET value_data = $2, updated_at = CURRENT_TIMESTAMP "
                        "WHERE key_name = $1 AND value_data = $3 RETURNING value_data",
                        _KEY,
                        encoded,
                        raw,
                    )
                if not rows:
                    raise RevisionConflict("Runtime settings changed during save; refresh before saving")
            except BaseException:
                self._expires = 0.0
                raise
            return self._accept(updated)

    async def set_value(self, key: str, value: Any, *, expected_revision: int, actor: str) -> SettingsSnapshot:
        return await self._change(key=key, value=value, expected_revision=expected_revision, actor=actor)

    async def reset_value(self, key: str, *, expected_revision: int, actor: str) -> SettingsSnapshot:
        return await self._change(key=key, reset=True, expected_revision=expected_revision, actor=actor)

    async def update_values(
        self, updates: dict[str, Any], *, removals: tuple[str, ...] = (), expected_revision: int, actor: str
    ) -> SettingsSnapshot:
        """Commit related overrides and removals as one revision and one CAS."""
        return await self._change(updates=updates, removals=removals, expected_revision=expected_revision, actor=actor)

    async def restore_revision(
        self,
        revision: int,
        *,
        expected_revision: int,
        actor: str,
        validate: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> SettingsSnapshot:
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid restore revision")
        return await self._change(
            revision=revision, expected_revision=expected_revision, actor=actor, validate=validate
        )

    async def get_state(self, *, force: bool = False) -> tuple[SettingsSnapshot, list[dict[str, Any]]]:
        """Read the values and redacted history from one accepted document."""
        snapshot = await self.get_snapshot(force=force)
        history = [
            {
                "revision": entry["revision"],
                "actor": entry["actor"],
                "at": entry["at"],
                "changed_keys": list(entry["changed_keys"]),
            }
            for entry in self._document["history"]
        ]
        return snapshot, history

    async def get_history(self) -> list[dict[str, Any]]:
        _, history = await self.get_state(force=True)
        return history


_store = RuntimeSettingsStore()
get_snapshot = _store.get_snapshot
set_value = _store.set_value
reset_value = _store.reset_value
get_history = _store.get_history
restore_revision = _store.restore_revision
update_values = _store.update_values
get_state = _store.get_state
