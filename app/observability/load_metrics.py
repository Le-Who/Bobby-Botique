"""Process-local load sources expose counts without enumerating private jobs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

_ingress: Callable[[], dict[str, Any]] | None = None


def register_ingress(source: Callable[[], dict[str, Any]] | None) -> None:
    global _ingress
    _ingress = source


def load_snapshot() -> dict[str, Any]:
    from app.adapters.concurrency import heavy_request_semaphore, ultra_heavy_semaphore
    from app.utils.background_tasks import get_task_manager
    from app.voice_engine import get_voice_reply_manager

    sources = {
        "ingress": _ingress,
        "task_manager": get_task_manager().metrics_snapshot,
        "heavy": heavy_request_semaphore.metrics_snapshot,
        "ultra": ultra_heavy_semaphore.metrics_snapshot,
        "tts": get_voice_reply_manager().metrics_snapshot,
    }
    result = {}
    for name, reader in sources.items():
        try:
            result[name] = (
                reader()
                if reader is not None
                else {
                    "scope": "process",
                    "available": False,
                    "observed_at": time.time(),
                    "reason_code": "not_registered",
                }
            )
        except Exception:
            result[name] = {
                "scope": "process",
                "available": False,
                "observed_at": time.time(),
                "reason_code": "source_failed",
            }
    return result
