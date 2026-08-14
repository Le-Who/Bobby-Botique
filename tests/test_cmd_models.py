from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repos.models_repo import (
    ModelCatalog,
    ModelCatalogSource,
    ModelMutationCode,
    ModelMutationResult,
)


def test_provider_selector_includes_all_chat_providers():
    from app.handlers.cmd_models import _build_provider_selector

    markup = _build_provider_selector()
    callbacks = {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    }

    assert callbacks == {
        "models:show:gemini",
        "models:show:opencode",
        "models:show:openrouter",
        "models:show:freetheai",
    }


@pytest.mark.asyncio
async def test_provider_view_shows_source_and_uses_short_remove_callbacks(monkeypatch):
    from app.handlers import cmd_models

    long_model = "provider/" + "very-long-model-name-" * 4
    catalog = ModelCatalog(
        provider="openrouter",
        models=(long_model,),
        source=ModelCatalogSource.ADMIN,
    )
    monkeypatch.setattr(cmd_models, "get_model_catalog", AsyncMock(return_value=catalog), raising=False)
    query = SimpleNamespace(edit_message_text=AsyncMock())

    await cmd_models._show_provider_view(query, "openrouter")

    kwargs = query.edit_message_text.await_args.kwargs
    assert "Источник" in kwargs["text"]
    remove_callback = kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert remove_callback.startswith("models:remove:openrouter:")
    assert len(remove_callback.encode("utf-8")) <= 64
    assert long_model not in remove_callback


def test_remove_token_resolves_against_current_catalog():
    from app.config import get_model_hash
    from app.handlers import cmd_models

    models = ("vendor/first", "vendor/second")

    assert cmd_models._resolve_model_token(models, get_model_hash("vendor/second")) == "vendor/second"
    assert cmd_models._resolve_model_token(models, "deadbeef") is None


@pytest.mark.asyncio
async def test_receive_model_name_reports_unsupported_instead_of_duplicate(monkeypatch):
    from app.handlers import cmd_models

    result = ModelMutationResult(
        code=ModelMutationCode.UNSUPPORTED,
        provider="gemini",
        model="gemini-3.7-flash",
    )
    monkeypatch.setattr(cmd_models, "add_model", AsyncMock(return_value=result))
    message = SimpleNamespace(
        text="gemini-3.7-flash",
        delete=AsyncMock(),
        chat=SimpleNamespace(send_message=AsyncMock()),
    )
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(user_data={"models_editing_provider": "gemini"})

    await cmd_models.receive_model_name(update, context)

    sent_text = message.chat.send_message.await_args.args[0]
    assert "не поддерживает generateContent" in sent_text
    assert "уже есть" not in sent_text


@pytest.mark.asyncio
async def test_successful_remove_rerenders_current_provider_list(monkeypatch):
    from app.config import get_model_hash
    from app.handlers import cmd_models

    model = "gemini-3.7-flash"
    catalog = ModelCatalog(
        provider="gemini",
        models=(model,),
        source=ModelCatalogSource.ADMIN,
    )
    removed = ModelMutationResult(
        code=ModelMutationCode.REMOVED,
        provider="gemini",
        model=model,
        catalog=ModelCatalog(
            provider="gemini",
            models=(),
            source=ModelCatalogSource.ADMIN,
        ),
    )
    monkeypatch.setattr(cmd_models, "get_model_catalog", AsyncMock(return_value=catalog), raising=False)
    remove_mock = AsyncMock(return_value=removed)
    monkeypatch.setattr(cmd_models, "remove_model", remove_mock)
    show_mock = AsyncMock(return_value=-1)
    monkeypatch.setattr(cmd_models, "_show_provider_view", show_mock)
    query = SimpleNamespace()

    await cmd_models._handle_remove(query, "gemini", get_model_hash(model))

    remove_mock.assert_awaited_once_with("gemini", model)
    show_mock.assert_awaited_once()
    assert "удалена" in show_mock.await_args.kwargs["notice"]
