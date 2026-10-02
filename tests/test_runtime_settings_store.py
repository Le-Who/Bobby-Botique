"""Offline checks of revision isolation and failure behavior."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.runtime_settings import store


class Database:
    def __init__(self):
        self.raw = None
        self.error = None
        self.race = False
        self.reads = 0
        self.writes = 0
        self.admin = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self
        self.admin = False

    async def execute(self, sql, *params):
        assert "set_config('app.is_admin'" in sql
        self.admin = True

    async def fetch(self, sql, *params):
        assert self.admin
        assert "CREATE" not in sql and "ALTER" not in sql
        if self.error:
            raise self.error
        if sql.lstrip().startswith("SELECT"):
            self.reads += 1
            return [{"value_data": self.raw}] if self.raw is not None else []
        self.writes += 1
        assert "RETURNING" in sql
        if self.race:
            return []
        if sql.lstrip().startswith("INSERT"):
            assert "DO NOTHING" in sql
            if self.raw is not None:
                return []
        else:
            assert "value_data = $3" in sql
            if self.raw != params[2]:
                return []
        self.raw = params[1]
        return [{"value_data": self.raw}]


@pytest.fixture
def backend(monkeypatch):
    database = Database()
    monkeypatch.setattr(store.db.db_manager, "pool", database)
    return database


@pytest.mark.asyncio
async def test_snapshot_isolation_and_stale_revision(backend):
    settings = store.RuntimeSettingsStore()
    value = {"models": ["gemini-test"]}
    snapshot = await settings.set_value("process:chat", value, expected_revision=0, actor="admin")
    value["models"].append("changed")
    assert snapshot.revision == 1
    assert snapshot.values["process:chat"]["models"] == ("gemini-test",)
    with pytest.raises(TypeError):
        snapshot.values["process:chat"]["models"] = ()
    with pytest.raises(store.RevisionConflict):
        await settings.set_value("process:chat", {}, expected_revision=0, actor="admin")
    assert backend.writes == 1


@pytest.mark.asyncio
async def test_cache_refresh_and_last_known_good(backend, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    first, second = store.RuntimeSettingsStore(), store.RuntimeSettingsStore()
    assert (await first.get_snapshot()).revision == 0
    await second.set_value("prompt:chat", "hello", expected_revision=0, actor="admin")
    assert (await first.get_snapshot()).revision == 0
    clock[0] += 6
    assert (await first.get_snapshot()).revision == 1
    backend.error = ConnectionError("unavailable")
    degraded = await first.get_snapshot(force=True)
    assert degraded.degraded
    assert degraded.values["prompt:chat"] == "hello"
    backend.error = None
    backend.raw = '{"version":999}'
    assert (await first.get_snapshot(force=True)).revision == 1
    assert (await first.get_snapshot()).degraded


@pytest.mark.asyncio
async def test_cold_failure_and_write_failure_are_explicit(backend):
    settings = store.RuntimeSettingsStore()
    backend.error = ConnectionError("unavailable")
    snapshot = await settings.get_snapshot()
    assert snapshot.degraded and snapshot.revision == 0 and not snapshot.values
    with pytest.raises(ConnectionError):
        await settings.set_value("prompt:chat", "hello", expected_revision=0, actor="admin")
    assert backend.writes == 0


@pytest.mark.asyncio
async def test_database_cas_race_is_conflict_without_retry(backend):
    settings = store.RuntimeSettingsStore()
    backend.race = True
    with pytest.raises(store.RevisionConflict):
        await settings.set_value("process:chat", {}, expected_revision=0, actor="admin")
    assert backend.writes == 1
    assert (await settings.get_snapshot()).revision == 0


@pytest.mark.asyncio
async def test_reset_restore_and_bounded_history(backend):
    settings = store.RuntimeSettingsStore()
    await settings.set_value("prompt:chat", "hello", expected_revision=0, actor="admin")
    snapshot = await settings.reset_value("prompt:chat", expected_revision=1, actor="admin")
    assert not snapshot.values
    restored = await settings.restore_revision(1, expected_revision=2, actor="admin")
    assert restored.revision == 3 and restored.values["prompt:chat"] == "hello"
    for revision in range(3, 35):
        await settings.set_value("process:chat", revision, expected_revision=revision, actor="admin")
    history = await settings.get_history()
    assert len(history) == 30
    assert history[-1]["revision"] == 34
    assert "values" not in history[-1]
    history[-1]["actor"] = "changed"
    assert (await settings.get_history())[-1]["actor"] == "admin"
    assert len(json.loads(backend.raw)["history"]) == 30
    with pytest.raises(ValueError):
        await settings.restore_revision(1, expected_revision=35, actor="admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [float("nan"), {1: "invalid"}, object(), "x" * 300_000],
    ids=["nonfinite", "nonstring-key", "object", "oversized"],
)
async def test_invalid_json_values_never_persist(backend, value):
    with pytest.raises(ValueError):
        await store.RuntimeSettingsStore().set_value("prompt:chat", value, expected_revision=0, actor="admin")
    assert backend.writes == 0


@pytest.mark.asyncio
async def test_history_does_not_return_unexpected_persisted_fields(backend):
    settings = store.RuntimeSettingsStore()
    await settings.set_value("prompt:chat", "hello", expected_revision=0, actor="admin")
    await settings.set_value("prompt:chat", "world", expected_revision=1, actor="admin")
    document = json.loads(backend.raw)
    document["history"][1]["private_note"] = "must not escape"
    backend.raw = json.dumps(document)

    history = await settings.get_history()
    assert set(history[1]) == {"revision", "actor", "at", "changed_keys"}
    assert "private_note" not in history[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {"version": 1, "revision": 0, "values": {}, "history": [], "actor": "", "at": "", "changed_keys": "bad"},
        {
            "version": 1,
            "revision": 0,
            "values": {},
            "history": [],
            "actor": "",
            "at": "",
            "changed_keys": ["prompt:x", "prompt:x"],
        },
        {
            "version": 1,
            "revision": 0,
            "values": {},
            "history": [],
            "actor": "",
            "at": "",
            "changed_keys": [],
            "extra": "unexpected",
        },
    ],
    ids=["changed-keys-type", "duplicate-changed-key", "extra-metadata"],
)
async def test_malformed_documents_degrade_and_cannot_be_written(backend, entry):
    backend.raw = json.dumps(entry)
    settings = store.RuntimeSettingsStore()
    assert (await settings.get_snapshot()).degraded
    with pytest.raises(ValueError):
        await settings.set_value("prompt:chat", "value", expected_revision=0, actor="admin")
    assert backend.writes == 0


@pytest.mark.asyncio
async def test_stalled_database_read_is_bounded(backend, monkeypatch):
    @asynccontextmanager
    async def stalled_acquire():
        await asyncio.sleep(10)
        yield backend

    monkeypatch.setattr(backend, "acquire", stalled_acquire)
    monkeypatch.setattr(store, "_QUERY_TIMEOUT", 0.01, raising=False)
    with pytest.raises(TimeoutError):
        await store._query("SELECT value_data FROM global_settings WHERE key_name = $1", "runtime_controls:v1")


@pytest.mark.asyncio
async def test_restore_validates_historical_values_before_write(backend):
    settings = store.RuntimeSettingsStore()
    await settings.set_value("prompt:obsolete", "old", expected_revision=0, actor="admin")
    await settings.reset_value("prompt:obsolete", expected_revision=1, actor="admin")
    writes = backend.writes

    def validate(values):
        if "prompt:obsolete" in values:
            raise ValueError("Prompt no longer supported")

    with pytest.raises(ValueError, match="no longer supported"):
        await settings.restore_revision(1, expected_revision=2, actor="admin", validate=validate)
    assert backend.writes == writes
    assert (await settings.get_snapshot()).revision == 2


@pytest.mark.asyncio
async def test_batch_auto_reset_is_atomic_and_preserves_other_roles(backend):
    settings = store.RuntimeSettingsStore()
    await settings.update_values(
        {
            "process:crocodile.judge": {"models": ["gemini-test"]},
            "process:crocodile.hints": {"models": ["gemini-other"]},
        },
        expected_revision=0,
        actor="admin",
    )
    reset = await settings.update_values(
        {"legacy_model:daily_croc_text_model_judge": ""},
        removals=("process:crocodile.judge",),
        expected_revision=1,
        actor="admin",
    )
    assert reset.revision == 2
    assert reset.values["legacy_model:daily_croc_text_model_judge"] == ""
    assert "process:crocodile.judge" not in reset.values
    assert "process:crocodile.hints" in reset.values
    assert backend.writes == 2
    document = json.loads(backend.raw)
    assert document["changed_keys"] == ["legacy_model:daily_croc_text_model_judge", "process:crocodile.judge"]
    with pytest.raises(store.RevisionConflict):
        await settings.update_values(
            {"legacy_model:daily_croc_text_model": "gemini-stale"}, expected_revision=1, actor="admin"
        )
    assert backend.writes == 2
