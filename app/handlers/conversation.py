from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager

from telegram.warnings import PTBUserWarning

_CALLBACK_QUERY_WITHOUT_PER_MESSAGE_WARNING = (
    "If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message. "
    "Read this FAQ entry to learn more about the per_* settings: "
    "https://github.com/python-telegram-bot/python-telegram-bot/wiki/"
    "Frequently-Asked-Questions#what-do-the-per_-settings-in-conversationhandler-do."
)
_EXACT_CALLBACK_QUERY_WITHOUT_PER_MESSAGE_WARNING = rf"\A{re.escape(_CALLBACK_QUERY_WITHOUT_PER_MESSAGE_WARNING)}\Z"


@contextmanager
def suppress_hybrid_conversation_handler_warning() -> Iterator[None]:
    """Hide PTB's generic callback-query warning for an intentional hybrid handler."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=_EXACT_CALLBACK_QUERY_WITHOUT_PER_MESSAGE_WARNING,
            category=PTBUserWarning,
        )
        yield
