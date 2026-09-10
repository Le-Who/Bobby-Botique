import uuid

from app.observability.context import current_context, replace_current_context


def get_request_id() -> str | None:
    return current_context().request_id


def set_request_id(request_id: str | None = None) -> str:
    rid = request_id or uuid.uuid4().hex[:12]
    replace_current_context(request_id=rid)
    return rid


def ensure_request_id(request_id: str | None = None) -> str:
    """Return the ingress-owned ID, creating a compatibility fallback if absent."""
    existing = get_request_id()
    return existing if existing is not None else set_request_id(request_id)


def clear_request_id() -> None:
    replace_current_context(request_id=None)


def get_user_id() -> int | None:
    return current_context().user_id


def get_chat_id() -> int | None:
    return current_context().chat_id


def set_user_context(user_id: int | None = None, chat_id: int | None = None) -> None:
    """Set user/chat context for the current async task.

    Call once at the handler entry point (e.g. messages.py).
    All downstream logging will automatically include these fields.
    """
    replace_current_context(user_id=user_id, chat_id=chat_id)


def clear_user_context() -> None:
    replace_current_context(user_id=None, chat_id=None)
