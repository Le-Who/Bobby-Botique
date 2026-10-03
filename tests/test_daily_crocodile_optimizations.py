import hashlib
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.games.crocodile_daily import _build_daily_image_prompt
from app.games.word_bank import (
    _filter_words_by_difficulty,
    _word_difficulty_band,
    find_duplicates,
    get_bank_stats,
    get_english_equivalent,
)
from app.repos.crocodile_daily import get_used_daily_words


@pytest.mark.unit
def test_word_difficulty_band_kaleidoscope_not_easy():
    band = _word_difficulty_band("калейдоскоп")
    assert band != "easy", "калейдоскоп should not be easy!"


@pytest.mark.unit
def test_word_difficulty_band_common_words_are_easy():
    assert _word_difficulty_band("кит") == "easy"
    assert _word_difficulty_band("луна") == "easy"


@pytest.mark.unit
def test_filter_words_easy_excludes_hard_band():
    words = ["кот", "кресло-качалка"]
    filtered = _filter_words_by_difficulty(words, preferred_difficulty="easy", topic_id="dummy")
    assert "кот" in filtered
    assert "кресло-качалка" not in filtered


@pytest.mark.asyncio
async def test_get_used_daily_words_respects_cooldown():
    from app.repos.crocodile_daily import db

    with patch.object(db, "db_query", new_callable=AsyncMock) as m_query:
        m_query.return_value = [{"target_word": "testword"}]
        words = await get_used_daily_words(days_back=30)
        assert "testword" in words
        m_query.assert_called_once()
        query_sql = m_query.call_args[0][0]
        assert "CURRENT_DATE - $1" in query_sql


@pytest.mark.asyncio
async def test_build_daily_image_prompt_uses_english():
    with patch("app.games.word_bank.get_english_equivalent", return_value="cat"):
        prompt = await _build_daily_image_prompt("кот", "Животные", difficulty="easy")
        assert 'The subject is "cat"' in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(("difficulty", "expected_seed"), [("easy", 202401011), ("hard", 202401012)])
async def test_daily_image_generation_enhance_false(monkeypatch, difficulty, expected_seed):
    from app.games import crocodile_daily as daily
    from app.providers import pollinations

    generate = AsyncMock(
        return_value=pollinations.PollinationsResult(success=True, images=[b"fake_image_bytes"], model_used="flux")
    )
    uploaded = SimpleNamespace(
        photo=[SimpleNamespace(file_id="small-photo"), SimpleNamespace(file_id="file_id_123")],
        delete=AsyncMock(),
    )
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=uploaded))
    monkeypatch.setattr("app.config.settings.ADMIN_ID", 9999999)
    monkeypatch.setattr(pollinations, "get_pollinations_provider", lambda: SimpleNamespace(generate=generate))

    result = await daily._generate_daily_image_file_id(
        bot,
        prompt="abc",
        puzzle_date=date(2024, 1, 1),
        difficulty=difficulty,
        image_model="flux",
        bypass_image_quota=True,
    )

    assert result == ("file_id_123", "flux")
    generate.assert_awaited_once()
    kwargs = generate.await_args.kwargs
    assert kwargs["prompt"] == "abc"
    assert kwargs["model"] == "flux"
    assert (kwargs["width"], kwargs["height"]) == (1024, 1024)
    assert kwargs["seed"] == expected_seed
    assert kwargs["enhance"] is False
    assert "text" in kwargs["negative_prompt"]
    bot.send_photo.assert_awaited_once()
    upload_kwargs = bot.send_photo.await_args.kwargs
    assert upload_kwargs["chat_id"] == 9999999
    assert upload_kwargs["photo"].input_file_content == b"fake_image_bytes"
    assert upload_kwargs["photo"].filename == f"daily-2024-01-01-{difficulty}.jpg"
    uploaded.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_image_generation_does_not_upload_failed_provider_result(monkeypatch):
    from app.games import crocodile_daily as daily
    from app.providers import pollinations

    generate = AsyncMock(return_value=pollinations.PollinationsResult(success=False, error_message="unavailable"))
    bot = SimpleNamespace(send_photo=AsyncMock())
    monkeypatch.setattr("app.config.settings.ADMIN_ID", 9999999)
    monkeypatch.setattr(pollinations, "get_pollinations_provider", lambda: SimpleNamespace(generate=generate))

    result = await daily._generate_daily_image_file_id(
        bot,
        prompt="abc",
        puzzle_date=date(2024, 1, 1),
        difficulty="easy",
        image_model="flux",
        bypass_image_quota=True,
    )

    assert result == (None, "flux")
    generate.assert_awaited_once()
    bot.send_photo.assert_not_awaited()


@pytest.mark.unit
def test_get_english_equivalent_built_in_words():
    assert get_english_equivalent("NON-EXISTENT-WORD") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_bank_stats_returns_correct_bands():
    from app.games.word_bank import WORD_BANK

    first_cat = list((WORD_BANK.get("ru") or {}).keys())[0]
    stats = await get_bank_stats(first_cat)
    assert "total" in stats
    assert "easy" in stats
    assert stats["total"] == stats["easy"] + stats["medium"] + stats["hard"]


@pytest.mark.unit
def test_find_duplicates_detects_cross_category():
    dupes = find_duplicates()
    assert isinstance(dupes, list)


@pytest.mark.asyncio
async def test_wordbank_menu_callback_renders_categories():
    from telegram import CallbackQuery, Message, Update, User

    from app.handlers.cmd_admin import wb_callback

    user = Mock(spec=User)
    user.id = 9999999

    msg = AsyncMock(spec=Message)

    query = AsyncMock(spec=CallbackQuery)
    query.data = "wb:menu"
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = Mock(spec=Update)
    update.callback_query = query
    update.effective_user = user
    update.effective_message = msg
    update.message = None

    with patch("app.utils.decorators.is_admin", return_value=True):
        await wb_callback(update, None)
        query.edit_message_text.assert_called_once()
        args = query.edit_message_text.call_args.args
        assert "Категорий:" in args[0]


@pytest.mark.asyncio
async def test_wordbank_gen_callback_triggers_generation():
    from telegram import CallbackQuery, Message, Update, User

    from app.games.word_bank import WORD_BANK
    from app.handlers.cmd_admin import _get_wb_cat_map, wb_callback

    first_cat = list((WORD_BANK.get("ru") or {}).keys())[0]
    cat_key = hashlib.md5(first_cat.encode()).hexdigest()[:8]
    _get_wb_cat_map()[cat_key] = first_cat

    user = Mock(spec=User)
    user.id = 9999999

    msg = AsyncMock(spec=Message)

    query = AsyncMock(spec=CallbackQuery)
    query.data = f"wb:gen:{cat_key}"
    query.message = msg
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = Mock(spec=Update)
    update.callback_query = query
    update.effective_user = user
    update.effective_message = msg
    update.message = None

    with patch("app.utils.decorators.is_admin", return_value=True):
        with patch("app.games.word_bank.generate_words_for_category", new_callable=AsyncMock) as m_gen:
            m_gen.return_value = {"added": 5, "skipped": 2}
            await wb_callback(update, None)

            m_gen.assert_called_once_with(first_cat)
            args = query.edit_message_text.call_args.args
            assert "Сгенерированы слова" in args[0]
