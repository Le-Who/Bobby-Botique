"""Synthetic ownership boundary; SQL locking is tested separately."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4


def install_scheduled_journal(monkeypatch):
    from app.repos import scheduled_deliveries as journal

    state = SimpleNamespace(current=True, claims={}, attempts=[], finished=[])

    async def claim(kind, source_id, user_id=42):
        key = (kind, source_id)
        if key in state.claims:
            return None
        row = {
            "delivery_id": uuid4(),
            "owner_token": uuid4(),
            "kind": kind,
            "source_id": source_id,
            "source_revision": 1,
            "privacy_epoch": 9,
            "user_id": user_id,
        }
        state.claims[key] = row
        return row

    async def claim_brief(user_id, sub_type="morning_brief", *, subscription=None):
        return await claim("brief", sub_type, user_id)

    async def start(*args):
        state.attempts.append(args)
        return {"attempt_id": uuid4()}

    async def finish(row, result):
        state.finished.append((row, result))
        return state.current

    async def claim_reminder(source):
        return await claim("reminder", source)

    monkeypatch.setattr(journal, "claim_reminder", AsyncMock(side_effect=claim_reminder))
    monkeypatch.setattr(journal, "claim_brief", AsyncMock(side_effect=claim_brief))
    monkeypatch.setattr(journal, "get_due_brief_retries", AsyncMock(return_value=[]))
    monkeypatch.setattr(journal, "is_current", AsyncMock(side_effect=lambda row: state.current))
    monkeypatch.setattr(journal, "renew", AsyncMock(side_effect=lambda row: state.current))
    monkeypatch.setattr(journal, "start_attempt", AsyncMock(side_effect=start))
    monkeypatch.setattr(journal, "finish_attempt", AsyncMock(return_value=True))
    monkeypatch.setattr(journal, "finish_delivery", AsyncMock(side_effect=finish))
    monkeypatch.setattr(journal, "release_unstarted", AsyncMock(return_value=True))
    monkeypatch.setattr(journal, "recover_abandoned", AsyncMock())
    return state


@asynccontextmanager
async def allowed_private_data(*args, **kwargs):
    yield True
