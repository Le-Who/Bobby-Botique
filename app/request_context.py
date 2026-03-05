import contextvars
import uuid

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("user_id", default=None)
_chat_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("chat_id", default=None)


def get_request_id() -> str | None:
    return _request_id_var.get()


def set_request_id(request_id: str | None = None) -> str:
    rid = request_id or uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def clear_request_id() -> None:
    _request_id_var.set(None)


def get_user_id() -> int | None:
    return _user_id_var.get()


def get_chat_id() -> int | None:
    return _chat_id_var.get()


def set_user_context(user_id: int | None = None, chat_id: int | None = None) -> None:
    """Set user/chat context for the current async task.

    Call once at the handler entry point (e.g. messages.py).
    All downstream logging will automatically include these fields.
    """
    _user_id_var.set(user_id)
    _chat_id_var.set(chat_id)


def clear_user_context() -> None:
    _user_id_var.set(None)
    _chat_id_var.set(None)
