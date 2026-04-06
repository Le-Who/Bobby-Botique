# app/core/entities.py
"""Pydantic domain models for the DAO boundary.

These models enforce fail-fast validation when mapping raw asyncpg rows
to Python objects.  If a migration adds/removes/renames a column without
updating the application code, the Pydantic ``model_validate`` call will
raise an explicit ``ValidationError`` instead of silently returning a
default value through ``dict.get()``.

Usage in repos::

    from app.core.entities import ChatStateRow, UserStateRow

    row = dict(result[0])
    validated = ChatStateRow.model_validate(row)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatStateRow(BaseModel):
    """Validated representation of a row from the ``chats`` table.

    Used by ``repos.chats.get_user_chat`` to validate the ``chat_info``
    JSONB payload returned by the composite SQL query.
    """

    model_config = ConfigDict(extra="ignore")  # tolerate extra columns from wider SELECTs

    model: str | None = None
    token_count: int = 0
    search_enabled: bool = False
    system_prompt: str | None = None
    context_summary: str | None = None
    thinking_level: str | None = None
    ltm_enabled: bool = True
    branch_id: int | None = None
    temperature: float | None = None
    voice_id: str | None = None
    tts_temperature: float | None = None


class UserInfoRow(BaseModel):
    """Validated representation of user-level flags (from ``users`` table)."""

    model_config = ConfigDict(extra="ignore")

    is_deep_dive: bool = False
    deep_dive_thread_id: str | None = None


class UserStateRow(BaseModel):
    """Validated representation of a row from the ``user_state`` table.

    Used by ``repos.users.load_user_state`` to validate the raw DB row
    before returning it to the caller.
    """

    model_config = ConfigDict(extra="ignore")

    document_mode: bool = False
    selected_document_id: int | None = None
    awaiting_custom_role_input: bool = False
    generated_role: dict[str, Any] | None = None  # JSONB → dict
    last_custom_role_prompt: str | None = None
    generating_custom_role: bool = False
    last_sent_message_text: str | None = None
    awaiting_manual_role_title: bool = False
    awaiting_manual_role_prompt: bool = False
    manual_role_title: str = ""
    manual_role_prompt: str = ""
