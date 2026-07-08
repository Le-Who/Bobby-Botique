"""
Local Tarot engine.
Loads tarot.json dataset, selects cards, and formats prompts for each spread type.
"""

import random
from enum import Enum
from pathlib import Path

from app.utils.json_compat import json

# ── Dataset ───────────────────────────────────────────────────────────────────

_TAROT_DATA_PATH = Path(__file__).parent / "assets" / "tarot.json"
_TAROT_DECK: list[dict] = []


def _load_deck() -> None:
    global _TAROT_DECK
    if _TAROT_DECK:
        return
    if not _TAROT_DATA_PATH.exists():
        return
    try:
        with open(_TAROT_DATA_PATH, encoding="utf-8") as f:
            # ⚡ Perf: json_compat (orjson) has no load(f), use loads(f.read()) instead.
            # orjson is 2-6× faster than stdlib json for both decode and encode.
            data = json.loads(f.read())
        _TAROT_DECK = data.get("tarot_interpretations", [])
    except Exception as e:
        import logging
        logging.error("Failed to load tarot deck: %s", e)


# ── Spread types ──────────────────────────────────────────────────────────────

class SpreadType(Enum):
    CLASSIC = "tarot"          # 3 карты: Прошлое / Настоящее / Будущее (legacy)
    DAILY   = "tarot_daily"    # 1 карта дня
    YES_NO  = "tarot_yesno"    # 1 карта Да / Нет
    LOVE    = "tarot_love"     # 5 карт: расклад на отношения
    CELTIC  = "tarot_celtic"   # 6 карт: кельтский крест (адаптированный)
    FORTUNE = "tarot_fortune"  # мгновенное предсказание без LLM


SPREAD_POSITIONS: dict[SpreadType, list[str]] = {
    SpreadType.CLASSIC: ["Прошлое", "Настоящее", "Будущее"],
    SpreadType.DAILY:   ["Карта дня"],
    SpreadType.YES_NO:  ["Ответ"],
    SpreadType.LOVE: [
        "Ты",
        "Партнёр",
        "Что вас связывает",
        "Что мешает",
        "Куда ведёт",
    ],
    SpreadType.CELTIC: [
        "Ситуация",
        "Препятствие",
        "Подсознание",
        "Прошлое",
        "Ближайшее будущее",
        "Итог",
    ],
    SpreadType.FORTUNE: [],  # Not used — fortune_cookie draws differently
}

# Lookup: result_id string → SpreadType
SPREAD_BY_ID: dict[str, SpreadType] = {s.value: s for s in SpreadType}


# ── Card drawing ──────────────────────────────────────────────────────────────

def draw_cards(num_cards: int) -> list[dict]:
    """Draw random cards from the deck with orientation."""
    _load_deck()
    if not _TAROT_DECK:
        return []

    drawn = random.sample(_TAROT_DECK, min(num_cards, len(_TAROT_DECK)))
    results = []
    for card in drawn:
        is_reversed = random.choice([True, False])
        meanings = card.get("meanings", {}).get(
            "shadow" if is_reversed else "light", []
        )
        results.append({
            "name": card.get("name", "Unknown Card"),
            "orientation": "Перевернутая" if is_reversed else "Прямая",
            "is_reversed": is_reversed,
            "keywords": card.get("keywords", []),
            "meanings": meanings,
        })
    return results


# ── Context builders ──────────────────────────────────────────────────────────

def build_card_context(card: dict, orientation: str, position: str = "Карта дня") -> tuple[str, str]:
    """Build one-card context and display label for a fixed card/orientation."""
    is_reversed = orientation == "Перевернутая"
    meanings = card.get("meanings", {}).get("shadow" if is_reversed else "light", [])
    name = card.get("name", "Unknown Card")
    keywords = card.get("keywords", [])
    context_lines = [
        f"Позиция «{position}»: {name}, {orientation}.",
        f"Ключевые слова: {', '.join(keywords[:5])}.",
        f"Значение: {', '.join(meanings[:3])}.",
        "",
    ]
    return "\n".join(context_lines), f"{name} ({orientation})"


def iter_daily_card_variants() -> list[dict]:
    """Return every card/orientation variant needed for prepared card-of-day text."""
    _load_deck()
    variants: list[dict] = []
    for card in _TAROT_DECK:
        for orientation in ("Прямая", "Перевернутая"):
            context, label = build_card_context(card, orientation)
            is_reversed = orientation == "Перевернутая"
            meanings = card.get("meanings", {}).get("shadow" if is_reversed else "light", [])
            variants.append(
                {
                    "name": card.get("name", "Unknown Card"),
                    "orientation": orientation,
                    "is_reversed": is_reversed,
                    "keywords": card.get("keywords", []),
                    "meanings": meanings,
                    "context": context,
                    "label": label,
                }
            )
    return variants

def get_tarot_context(
    spread: "SpreadType | int" = SpreadType.CLASSIC,
) -> tuple[str, list[str]]:
    """Draw cards and return (formatted_context, card_names_list).

    Accepts either a SpreadType enum value or a legacy int (number of cards).
    When called with an int, falls back to CLASSIC positions for 3, otherwise
    uses generic «Карта N» labels.
    """
    # Backward-compat: old callers may pass get_tarot_context(3)
    if isinstance(spread, int):
        num_cards = spread
        positions = (
            SPREAD_POSITIONS[SpreadType.CLASSIC]
            if num_cards == 3
            else [f"Карта {i + 1}" for i in range(num_cards)]
        )
    else:
        positions = SPREAD_POSITIONS[spread]
        num_cards = len(positions)

    cards = draw_cards(num_cards)
    if not cards:
        return "", []

    context_lines: list[str] = []
    card_names: list[str] = []

    for i, card in enumerate(cards):
        position = positions[i] if i < len(positions) else f"Карта {i + 1}"
        label = f"{card['name']} ({card['orientation']})"
        card_names.append(label)

        context_lines.append(f"Позиция «{position}»: {card['name']}, {card['orientation']}.")
        context_lines.append(f"Ключевые слова: {', '.join(card['keywords'][:5])}.")
        context_lines.append(f"Значение: {', '.join(card['meanings'][:3])}.")
        context_lines.append("")

    return "\n".join(context_lines), card_names


def get_fortune_cookie() -> tuple[str, str] | None:
    """Draw one card and return (fortune_text_ru, card_name).

    Uses the pre-translated `fortune_telling_ru` field (injected by
    scripts/translate_fortune_telling.py). Falls back to the original
    English `fortune_telling` if the Russian field is absent.

    Returns None if the deck is unavailable.
    """
    _load_deck()
    if not _TAROT_DECK:
        return None

    card = random.choice(_TAROT_DECK)
    # Prefer Russian, fall back to English
    fortunes: list[str] = card.get("fortune_telling_ru") or card.get("fortune_telling", [])
    if not fortunes:
        return None

    return random.choice(fortunes), card.get("name", "Unknown Card")
