"""Refresh control snapshots at request/startup boundaries without restarting."""

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from contextvars import Context, ContextVar, copy_context

from app.runtime_settings.source_inventory import get_source_inventory
from app.runtime_settings.store import SettingsSnapshot

_operation_snapshot: ContextVar[SettingsSnapshot | None] = ContextVar("runtime_operation_snapshot", default=None)


def detached_settings_context() -> Context:
    """Preserve tracing/privacy context, without pinning future worker jobs."""
    from app.prompt_registry import clear_prompt_snapshot

    context = copy_context()
    context.run(_operation_snapshot.set, None)
    context.run(clear_prompt_snapshot)
    return context


def load_controlled_prompts() -> None:
    # Only import modules declaring controlled static templates. New definitions
    # no longer require a second registration in this loader.
    for module in get_source_inventory().prompt_modules:
        importlib.import_module(module)


async def refresh_runtime_settings(*, force: bool = False) -> None:
    from app.runtime_settings.models import refresh_catalogs
    from app.runtime_settings.prompts import refresh_prompts

    try:
        await asyncio.to_thread(load_controlled_prompts)
        await refresh_prompts(force=force)
        await refresh_catalogs()
    except Exception as error:
        # Keep the last confirmed runtime values. Do not log prompt/config content.
        logging.warning("Runtime controls refresh unavailable: %s", type(error).__name__)


async def operation_snapshot() -> SettingsSnapshot:
    from app.runtime_settings.store import get_snapshot

    return _operation_snapshot.get() or await get_snapshot()


@asynccontextmanager
async def runtime_settings_scope(snapshot: SettingsSnapshot | None = None):
    """Capture route and prompt revisions when work begins, including scheduled jobs."""
    from app.prompt_registry import prompt_scope
    from app.runtime_settings.prompts import _overrides
    from app.runtime_settings.store import get_snapshot

    if _operation_snapshot.get() is not None:
        yield
        return
    if snapshot is None:
        await refresh_runtime_settings()
        snapshot = await get_snapshot()
    overrides = _overrides(snapshot.values)
    token = _operation_snapshot.set(snapshot)
    try:
        with prompt_scope(overrides=overrides, revision=snapshot.revision):
            yield
    finally:
        _operation_snapshot.reset(token)
