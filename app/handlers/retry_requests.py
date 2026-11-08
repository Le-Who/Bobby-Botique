"""Request-scoped retry ownership; no provider text or durable schema changes."""

import uuid
from dataclasses import dataclass
from typing import Literal

from app import state


class StaleRetryRequest(ValueError):
    pass


@dataclass(slots=True)
class RetryRequest:
    identity: str
    kind: Literal["chat", "search"]
    user_text: str
    search_query: str | None
    generation: int
    branch_id: object | None
    history_start: int
    response_ref: tuple[int, int] | None = None
    memory_scheduled: bool = False
    running: bool = False
    capture_text_memory: bool = True
    is_forward_batch: bool = False
    reply_with_voice: bool = False
    text_parts: tuple[str, ...] | None = None
    replayable: bool = True

    @property
    def callback_data(self) -> str:
        return f"retry_last:{self.identity}"


def current_request(user_id: int) -> RetryRequest | None:
    user_state = state.get_user_state(user_id)
    value = user_state.retry_request
    if isinstance(value, RetryRequest):
        return value
    user_state.retry_request = None
    return None


def scope_matches(request: RetryRequest, chat_state) -> bool:
    return (
        not getattr(chat_state, "private_data_blocked", False)
        and request.generation == int(getattr(chat_state, "memory_epoch", 0) or 0)
        and request.branch_id == getattr(chat_state, "branch_id", None)
    )


def request_is_current(user_id: int, request: RetryRequest, chat_state) -> bool:
    return current_request(user_id) is request and scope_matches(request, chat_state)


def begin_request(
    user_id: int, kind: Literal["chat", "search"], text: str, chat_state, *, search_query=None, retry=None
):
    if retry is not None:
        if current_request(user_id) is not retry or not scope_matches(retry, chat_state):
            raise StaleRetryRequest("Retry scope changed")
        return retry
    request = RetryRequest(
        uuid.uuid4().hex[:16],
        kind,
        text,
        search_query,
        int(getattr(chat_state, "memory_epoch", 0) or 0),
        getattr(chat_state, "branch_id", None),
        len(getattr(chat_state, "history", ())),
    )
    state.get_user_state(user_id).retry_request = request
    return request


def retry_history(request: RetryRequest, history: list) -> list:
    """Trim only the exact last logical turn; callers retain the original for rollback."""
    start = request.history_start
    if len(history) == start:
        return list(history)
    if start >= len(history) or len(history) > start + 2:
        raise StaleRetryRequest("Retry history changed")
    turn = history[start]
    text = "".join(
        part if isinstance(part, str) else part.get("text", "") if isinstance(part, dict) else ""
        for part in turn.get("parts", [])
    )
    expected_text = "".join(request.text_parts) if request.text_parts is not None else request.user_text
    if turn.get("role") != "user" or text != expected_text:
        raise StaleRetryRequest("Retry history no longer belongs to the request")
    return list(history[:start])


def bind_response(request: RetryRequest, outcome) -> None:
    receipt = getattr(outcome, "receipt", None)
    ref = getattr(receipt, "final_message", None)
    if ref is not None and isinstance(ref.message_id, int) and isinstance(ref.chat_id, int):
        request.response_ref = (ref.message_id, ref.chat_id)


def legacy_request(user_id: int, text: str, chat_state) -> RetryRequest:
    kind = "search" if text.startswith("?") and not text.startswith("??") else "chat"
    query = text[1:].strip() if kind == "search" else None
    request = begin_request(user_id, kind, query if query is not None else text, chat_state, search_query=query)
    history = chat_state.history
    if (
        kind == "chat"
        and len(history) >= 2
        and history[-2].get("role") == "user"
        and history[-1].get("role") == "model"
    ):
        request.history_start = len(history) - 2
        try:
            retry_history(request, history)
        except StaleRetryRequest:
            request.history_start = len(history)
        else:
            # A legacy completed/partial turn may already have scheduled memory.
            request.memory_scheduled = True
    return request
