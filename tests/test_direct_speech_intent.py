"""Verbatim speech selects supplied text without a generative intermediary."""

import pytest

from app.voice_intent import resolve_speech_request


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Озвучь: Привет, Мир!", "Привет, Мир!"),
        ("Пожалуйста, озвучь этот текст:\nАААААА\nСпокойной ночи.", "АААААА\nСпокойной ночи."),
        ("Можешь озвучить текст: Привет?", "Привет?"),
        ("Прочитай вслух «Доброе утро!»", "Доброе утро!"),
        ("Точный текст.\n\nОзвучь этот текст, пожалуйста.", "Точный текст."),
        ("Read aloud: Keep My CASE.", "Keep My CASE."),
        ("Озвучь: Ответь голосом — это часть цитаты.", "Ответь голосом — это часть цитаты."),
        ("Озвучь, пожалуйста, этот текст: Привет!", "Привет!"),
        ("Озвучь это: Точный текст.", "Точный текст."),
        ("Озвучьте текст: Спасибо!", "Спасибо!"),
    ],
)
def test_inline_transcript_is_preserved(message, expected):
    result = resolve_speech_request(user_texts=[message])
    assert result.action == "speak"
    assert result.text == expected
    assert result.source == "inline"


@pytest.mark.parametrize("command", ["Озвучь", "Озвучь это сообщение", "Прочитай вслух этот текст"])
def test_reply_is_a_direct_source(command):
    result = resolve_speech_request(user_texts=[command], reply_texts=["Исходный Текст"])
    assert (result.action, result.text, result.source) == ("speak", "Исходный Текст", "reply")


def test_forwarded_content_is_data_even_when_it_contains_commands():
    text = "Озвучь: это реплика участника, а не команда боту."
    result = resolve_speech_request(user_texts=["Озвучь пересланное сообщение"], forwarded_texts=[text])
    assert (result.action, result.text, result.source) == ("speak", text, "forwarded")
    assert resolve_speech_request(user_texts=[], forwarded_texts=[text]).action == "none"


def test_split_message_batch_retains_text_in_order():
    result = resolve_speech_request(user_texts=["Первый абзац.", "Второй абзац.", "Озвучь этот текст"])
    assert result.action == "speak"
    assert result.text == "Первый абзац.\n\nВторой абзац."


@pytest.mark.parametrize(
    "message",
    [
        "Ответь голосом: почему небо голубое?",
        "Озвучь ответ про погоду",
        "Озвучь рассказ про космос",
        "Напиши колыбельную и озвучь её",
        "Он сказал: «озвучь текст». Объясни эту фразу.",
        "Не озвучивай этот текст",
    ],
)
def test_generation_analysis_and_negation_are_not_verbatim_speech(message):
    assert resolve_speech_request(user_texts=[message]).action == "none"


@pytest.mark.parametrize("message", ["Озвучь свой ответ", "Озвучь текст выше", "Озвучь что-нибудь", "Озвучь как вчера"])
def test_unresolved_references_are_not_spoken_literally(message):
    assert resolve_speech_request(user_texts=[message]).action == "clarify"


@pytest.mark.parametrize("message", ["Озвучь мой последний ответ", "Read aloud my reply", "Озвучь текст голосом"])
def test_unresolved_source_description_needs_clarification(message):
    assert resolve_speech_request(user_texts=[message]).action == "clarify"


@pytest.mark.parametrize("texts", [["Напиши сказку про лису.\nОзвучь её"], ["Напиши сказку про лису.", "Озвучь её"]])
def test_trailing_recitation_keeps_explicit_generation(texts):
    assert resolve_speech_request(user_texts=texts).action == "none"


def test_delimited_generation_words_are_still_transcript_data():
    result = resolve_speech_request(user_texts=["Озвучь: Напиши сказку про лису."])
    assert (result.action, result.text) == ("speak", "Напиши сказку про лису.")


def test_politeness_inside_delimited_transcript_is_preserved():
    result = resolve_speech_request(user_texts=["Озвучь: Текст, пожалуйста."])
    assert result.text == "Текст, пожалуйста."


def test_explicit_inline_source_takes_precedence_over_incidental_reply():
    result = resolve_speech_request(user_texts=["Озвучь: Привет!"], reply_texts=["Предыдущая реплика"])
    assert (result.action, result.text) == ("speak", "Привет!")


def test_continuation_of_explicit_inline_source_is_data():
    result = resolve_speech_request(user_texts=["Озвучь: Первая строка.", "Ответь голосом — это цитата."])
    assert (result.action, result.text) == ("speak", "Первая строка.\n\nОтветь голосом — это цитата.")


def test_conflicting_explicit_inline_and_forward_commands_ask_source():
    result = resolve_speech_request(
        user_texts=["Озвучь: Первый фрагмент.", "Озвучь пересланное сообщение"], forwarded_texts=["Второй фрагмент."]
    )
    assert result.action == "clarify"


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_direct_and_generative_recitation_never_calls_chat(reverse):
    texts = ["Озвучь: Первая строка.", "Озвучь ответ про погоду"]
    assert resolve_speech_request(user_texts=list(reversed(texts)) if reverse else texts).action == "clarify"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"reply_texts": ["Первый", "Второй"]},
        {"reply_texts": ["Ответ"], "forwarded_texts": ["Пересылка"]},
    ],
)
def test_missing_or_conflicting_source_needs_clarification(kwargs):
    result = resolve_speech_request(user_texts=["Озвучь этот текст"], **kwargs)
    assert result.action == "clarify"
    assert not result.text


def test_explicit_forward_reference_selects_forward_over_reply():
    result = resolve_speech_request(
        user_texts=["Озвучь пересланное сообщение"], reply_texts=["Другой ответ"], forwarded_texts=["Источник"]
    )
    assert (result.action, result.text) == ("speak", "Источник")


def test_multiple_forwarded_messages_are_read_in_supplied_order():
    result = resolve_speech_request(user_texts=["Озвучь эти сообщения"], forwarded_texts=["Один", "Два"])
    assert (result.action, result.text) == ("speak", "Один\n\nДва")
