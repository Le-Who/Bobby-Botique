"""Static accessibility contracts for user-facing HTML templates."""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

_TEMPLATES = Path(__file__).parents[1] / "app" / "templates"


class _TemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))

    handle_startendtag = handle_starttag


def _parse_template(name: str) -> tuple[str, list[tuple[str, dict[str, str | None]]]]:
    source = (_TEMPLATES / name).read_text(encoding="utf-8")
    parser = _TemplateParser()
    parser.feed(source)
    return source, [(tag, dict(attrs)) for tag, attrs in parser.tags]


def _by_id(tags: list[tuple[str, dict[str, str | None]]], element_id: str) -> tuple[str, dict[str, str | None]]:
    return next((tag, attrs) for tag, attrs in tags if attrs.get("id") == element_id)


def test_live_audio_icon_controls_have_unique_ids_and_labels():
    source = (_TEMPLATES / "live_audio.html").read_text(encoding="utf-8")
    parser = _TemplateParser()
    parser.feed(source)
    ids = [value for _, attrs in parser.tags for name, value in attrs if name == "id" and value]

    assert all(count == 1 for count in Counter(ids).values())

    tags = [(tag, dict(attrs)) for tag, attrs in parser.tags]
    expected = {
        "settings-btn": "Открыть настройки Live Audio",
        "end-btn": "Завершить Live Audio",
        "mic-btn": "Начать запись",
    }
    for element_id, label in expected.items():
        tag, attrs = _by_id(tags, element_id)
        assert tag == "button"
        assert attrs.get("aria-label") == label


def test_crocodile_icon_controls_have_contextual_labels():
    _, tags = _parse_template("crocodile.html")

    assert _by_id(tags, "live-audio-btn")[1].get("aria-label") == "Открыть Live Audio"
    assert _by_id(tags, "hint-btn")[1].get("aria-label") == "Получить подсказку"

    reactions = [attrs for _, attrs in tags if "react-btn" in (attrs.get("class") or "").split()]
    assert reactions
    assert all(attrs.get("aria-label") == f"Реакция: {attrs['title']}" for attrs in reactions)


def test_miniapp_graph_and_form_controls_are_named_keyboard_controls():
    source, tags = _parse_template("miniapp.html")

    graph_tag, graph_attrs = _by_id(tags, "graph-open-btn")
    assert graph_tag == "button"
    assert graph_attrs.get("type") == "button"
    assert graph_attrs.get("aria-label") == "Открыть граф знаний"
    assert _by_id(tags, "search-input")[1].get("aria-label") == "Поиск по памяти"

    graph_buttons = [attrs for _, attrs in tags if "graph-btn" in (attrs.get("class") or "").split()]
    assert [attrs.get("aria-label") for attrs in graph_buttons] == [
        "Увеличить масштаб графа",
        "Уменьшить масштаб графа",
        "Сбросить масштаб графа",
    ]

    expected_input_labels = {
        "s-temp-slider": "Температура",
        "s-temp-num": "Значение температуры",
        "s-tts-temp-slider": "Температура аудио",
        "s-tts-temp-num": "Значение температуры аудио",
        "s-ltm": "Долгосрочная память",
        "s-search": "Веб-поиск",
    }
    for element_id, label in expected_input_labels.items():
        assert _by_id(tags, element_id)[1].get("aria-label") == label

    assert ".settings-btn:focus-visible" in source
    assert ".graph-btn:focus-visible" in source


def test_dynamic_errors_are_announced_without_marking_password_invalid():
    _, login_tags = _parse_template("login.html")
    error_attrs = next(attrs for _, attrs in login_tags if attrs.get("class") == "error-msg")
    assert error_attrs.get("role") == "alert"
    assert "aria-invalid" not in _by_id(login_tags, "password")[1]

    _, natal_tags = _parse_template("natal_form.html")
    assert _by_id(natal_tags, "error-box")[1].get("role") == "alert"


def test_miniapp_escape_helper_preserves_zero_values():
    source = (_TEMPLATES / "miniapp.html").read_text(encoding="utf-8")
    assert "d.textContent = s ?? '';" in source
