import warnings
from itertools import chain

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
from telegram.warnings import PTBUserWarning

from app.handlers import cmd_keys
from app.handlers.cmd_keys import build_keys_conversation_handler
from app.handlers.cmd_models import build_models_conversation_handler
from app.handlers.horoscope_subscription import build_horoscope_subscription_handler
from app.handlers.natal_chart import build_natal_chart_handler

_HYBRID_BUILDERS = (
    build_keys_conversation_handler,
    build_models_conversation_handler,
    build_horoscope_subscription_handler,
    build_natal_chart_handler,
)


@pytest.mark.parametrize("builder", _HYBRID_BUILDERS, ids=lambda builder: builder.__name__)
def test_hybrid_conversation_handlers_keep_chat_user_scoping_without_leaking_warnings(builder):
    unrelated_warning = "unrelated PTB warning remains visible"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        handler = builder()
        warnings.warn(unrelated_warning, PTBUserWarning, stacklevel=1)

    handlers = chain(handler.entry_points, *handler.states.values(), handler.fallbacks)
    handler_types = tuple(type(item) for item in handlers)

    assert handler.per_chat is True
    assert handler.per_user is True
    assert handler.per_message is False
    assert any(issubclass(handler_type, CallbackQueryHandler) for handler_type in handler_types)
    assert any(issubclass(handler_type, CommandHandler | MessageHandler) for handler_type in handler_types)
    assert [str(item.message) for item in caught] == [unrelated_warning]


def test_hybrid_conversation_handler_filter_keeps_other_constructor_warnings_visible(monkeypatch):
    real_conversation_handler = cmd_keys.ConversationHandler
    unrelated_warning = "different PTB constructor warning remains visible"

    def warning_emitting_constructor(*args, **kwargs):
        warnings.warn(unrelated_warning, PTBUserWarning, stacklevel=2)
        return real_conversation_handler(*args, **kwargs)

    monkeypatch.setattr(cmd_keys, "ConversationHandler", warning_emitting_constructor)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cmd_keys.build_keys_conversation_handler()

    assert [str(item.message) for item in caught] == [unrelated_warning]
