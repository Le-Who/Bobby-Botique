import contextvars
import uuid
from typing import Optional

_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    return _request_id_var.get()


def set_request_id(request_id: Optional[str] = None) -> str:
    rid = request_id or uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def clear_request_id() -> None:
    _request_id_var.set(None)
