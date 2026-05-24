"""
Local Tarot engine.
Loads tarot.json dataset, selects cards, and formats Gemini prompts.
"""

import json
import random
from pathlib import Path

# Load dataset once at startup
_TAROT_DATA_PATH = Path(__file__).parent / "data" / "tarot.json"
_TAROT_DECK = []

def _load_deck():
    global _TAROT_DECK
    if not _TAROT_DECK and _TAROT_DATA_PATH.exists():
        try:
            with open(_TAROT_DATA_PATH, encoding="utf-8") as f:
                data = json.load(f)
                _TAROT_DECK = data.get("tarot_interpretations", [])
        except Exception as e:
            import logging
            logging.error(f"Failed to load tarot deck: {e}")

def draw_cards(num_cards: int = 3) -> list[dict]:
    """
    Draw random cards from the deck.
    Returns a list of dicts with card info and drawn orientation (upright/reversed).
    """
    _load_deck()
    if not _TAROT_DECK:
        return []
        
    drawn = random.sample(_TAROT_DECK, min(num_cards, len(_TAROT_DECK)))
    results = []
    
    for card in drawn:
        is_reversed = random.choice([True, False])
        
        # Extract the relevant keywords/meanings
        meanings = card.get("meanings", {}).get("light" if not is_reversed else "shadow", [])
        keywords = card.get("keywords", [])
        
        results.append({
            "name": card.get("name", "Unknown Card"),
            "orientation": "Перевернутая" if is_reversed else "Прямая",
            "keywords": keywords,
            "meanings": meanings,
        })
        
    return results

def get_tarot_context(num_cards: int = 3) -> tuple[str, list[str]]:
    """
    Draws cards and returns a formatted string for Gemini context + list of card names for display.
    """
    cards = draw_cards(num_cards)
    if not cards:
        return "", []
        
    context_lines = []
    card_names = []
    for i, card in enumerate(cards):
        position = ["Прошлое", "Настоящее", "Будущее"][i] if num_cards == 3 else f"Карта {i+1}"
        card_names.append(f"{card['name']} ({card['orientation']})")
        
        context_lines.append(f"Позиция '{position}': {card['name']}, Положение: {card['orientation']}.")
        context_lines.append(f"Ключевые слова: {', '.join(card['keywords'][:5])}.")
        context_lines.append(f"Значение в этом положении: {', '.join(card['meanings'][:3])}.")
        context_lines.append("")
        
    return "\n".join(context_lines), card_names
