# /app/games/word_bank.py
"""Bilingual word bank for the Crocodile game (RU + EN).

Unknown categories are handled by calling Gemini to generate words on the fly.
If Gemini fails or the category is too vague, the caller receives None and should
notify the user.

Usage:
    from app.games.word_bank import pick_random_word, resolve_category, list_categories

    word, lang, cat, is_gen = await pick_random_word("пирожки")
    # → ("ватрушка", "ru", "пирожки", True)  — AI-generated word list

Words are purposely lowercase and normalised (stripped).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import unicodedata
from dataclasses import dataclass

from app.utils.background_tasks import submit_task
from app.utils.json_compat import json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicResolution:
    """Resolved topic metadata used by word selection and judge context."""

    topic_id: str
    lang: str
    category: str
    raw: str
    match_key: str
    is_builtin: bool


# ── Word bank ─────────────────────────────────────────────────────────────────

WORD_BANK: dict[str, dict[str, list[str]]] = {
    "ru": {
        "Животные": [
            "крокодил",
            "жираф",
            "пингвин",
            "хамелеон",
            "дельфин",
            "кенгуру",
            "коала",
            "осьминог",
            "броненосец",
            "утконос",
            "перепел",
            "бобёр",
            "носорог",
            "зебра",
            "горилла",
            "ягуар",
            "обезьяна",
            "медуза",
            "кальмар",
            "морж",
            "козёл",
            "антилопа",
            "гепард",
            "страус",
            "попугай",
            "пеликан",
            "хомяк",
            "мангуст",
            "кабан",
            "варан",
        ],
        "Еда": [
            "пицца",
            "суши",
            "борщ",
            "шаурма",
            "тирамису",
            "оливье",
            "хинкали",
            "блин",
            "чизбургер",
            "рамэн",
            "карри",
            "гаспачо",
            "паэлья",
            "ризотто",
            "круассан",
            "эклер",
            "кимчи",
            "авокадо",
            "гуакамоле",
            "стейк",
            "лазанья",
            "чизкейк",
            "макаруны",
            "вафля",
            "пельмени",
            "щи",
            "котлета",
            "шашлык",
            "буррито",
            "тако",
        ],
        "Профессии": [
            "программист",
            "врач",
            "пилот",
            "архитектор",
            "фотограф",
            "дирижёр",
            "скульптор",
            "хирург",
            "детектив",
            "астронавт",
            "дипломат",
            "сомелье",
            "ветеринар",
            "маляр",
            "шахтёр",
            "водолаз",
            "режиссёр",
            "кондитер",
            "логопед",
            "геолог",
            "акушер",
            "ювелир",
            "таксидермист",
            "пожарный",
            "переводчик",
        ],
        "Спорт": [
            "бокс",
            "фехтование",
            "сёрфинг",
            "кёрлинг",
            "синхронное плавание",
            "триатлон",
            "скалолазание",
            "стрельба из лука",
            "регби",
            "гольф",
            "бадминтон",
            "санный спорт",
            "ватерполо",
            "крикет",
            "черлидинг",
            "пятиборье",
            "прыжки с шестом",
            "армрестлинг",
            "скейтбординг",
        ],
        "Фильмы": [
            "матрица",
            "интерстеллар",
            "аватар",
            "начало",
            "паразиты",
            "форрест гамп",
            "гладиатор",
            "шоу шоушенк",
            "до свидания ленин",
            "амели",
            "леон",
            "джокер",
            "властелин колец",
            "король лев",
        ],
        "Техника": [
            "пылесос",
            "микроволновка",
            "3д принтер",
            "дрон",
            "видеокарта",
            "планшет",
            "умные часы",
            "проектор",
            "сканер",
            "термостат",
            "спутник",
            "перфоратор",
            "блендер",
            "холодильник",
            "синтезатор",
        ],
        "Природа": [
            "вулкан",
            "айсберг",
            "торнадо",
            "северное сияние",
            "оазис",
            "цунами",
            "мангровый лес",
            "большой барьерный риф",
            "гейзер",
            "каньон",
            "болото",
            "пещера",
            "ледник",
            "атолл",
            "плато",
        ],
        "Транспорт": [
            "вертолёт",
            "подводная лодка",
            "дирижабль",
            "паром",
            "трактор",
            "мотоцикл",
            "трамвай",
            "канатная дорога",
            "катамаран",
            "квадроцикл",
            "ракета",
            "дрезина",
            "рикша",
            "аэросани",
            "самокат",
        ],
        "Одежда": [
            "кимоно",
            "бушлат",
            "пончо",
            "сарафан",
            "тюрбан",
            "смокинг",
            "гетры",
            "кираса",
            "фартук",
            "бриджи",
            "пижама",
            "кепка",
            "рукавицы",
            "жилет",
            "мантия",
        ],
        "Музыка": [
            "виолончель",
            "балалайка",
            "маракасы",
            "контрабас",
            "аккордеон",
            "банджо",
            "диджериду",
            "волынка",
            "арфа",
            "кастаньеты",
            "ситар",
            "гобой",
            "фагот",
            "там-там",
            "укулеле",
        ],
        "Космос": [
            "астероид",
            "туманность",
            "гравитация",
            "орбита",
            "солнечный ветер",
            "чёрная дыра",
            "нейтронная звезда",
            "космическая станция",
            "кратер",
            "метеорит",
            "галактика",
            "сверхнова",
            "пульсар",
            "атмосфера",
            "зонд",
        ],
        "Разное": [
            "бумеранг",
            "зонтик",
            "кальян",
            "будильник",
            "телескоп",
            "перископ",
            "лабиринт",
            "домино",
            "маятник",
            "компас",
            "фонарик",
            "рогатка",
            "паяльник",
            "калейдоскоп",
            "водоворот",
            "жетон",
            "фейерверк",
            "парашют",
            "кресло-качалка",
            "метроном",
        ],
    },
    "en": {
        "Animals": [
            "crocodile",
            "giraffe",
            "penguin",
            "chameleon",
            "dolphin",
            "kangaroo",
            "koala",
            "octopus",
            "armadillo",
            "platypus",
            "quail",
            "beaver",
            "rhinoceros",
            "zebra",
            "gorilla",
            "jaguar",
            "monkey",
            "jellyfish",
            "squid",
            "walrus",
            "antelope",
            "cheetah",
            "ostrich",
            "parrot",
            "pelican",
            "hamster",
            "mongoose",
            "boar",
            "monitor lizard",
            "flamingo",
        ],
        "Food": [
            "pizza",
            "sushi",
            "borsch",
            "shawarma",
            "tiramisu",
            "cookie",
            "cheeseburger",
            "ramen",
            "curry",
            "gazpacho",
            "paella",
            "risotto",
            "croissant",
            "éclair",
            "kimchi",
            "avocado",
            "guacamole",
            "steak",
            "lasagna",
            "cheesecake",
            "macarons",
            "waffle",
            "burrito",
            "taco",
            "nachos",
            "dumplings",
            "hotdog",
            "pancake",
            "bagel",
            "pretzel",
        ],
        "Professions": [
            "programmer",
            "doctor",
            "pilot",
            "architect",
            "photographer",
            "conductor",
            "sculptor",
            "surgeon",
            "detective",
            "astronaut",
            "diplomat",
            "sommelier",
            "veterinarian",
            "diver",
            "director",
            "confectioner",
            "geologist",
            "obstetrician",
            "jeweller",
            "taxidermist",
            "firefighter",
            "translator",
            "ufologist",
        ],
        "Sports": [
            "boxing",
            "fencing",
            "surfing",
            "curling",
            "synchronised swimming",
            "triathlon",
            "rock climbing",
            "archery",
            "rugby",
            "golf",
            "badminton",
            "luge",
            "water polo",
            "cricket",
            "cheerleading",
            "modern pentathlon",
            "pole vault",
            "arm wrestling",
            "skateboarding",
        ],
        "Movies": [
            "the matrix",
            "interstellar",
            "avatar",
            "inception",
            "parasite",
            "forrest gump",
            "gladiator",
            "the shawshank redemption",
            "amelie",
            "leon",
            "joker",
            "the lord of the rings",
            "the lion king",
        ],
        "Tech": [
            "vacuum cleaner",
            "microwave",
            "3d printer",
            "drone",
            "graphics card",
            "tablet",
            "smartwatch",
            "projector",
            "scanner",
            "thermostat",
            "satellite",
            "jackhammer",
            "blender",
            "refrigerator",
            "synthesizer",
        ],
        "Nature": [
            "volcano",
            "iceberg",
            "tornado",
            "aurora borealis",
            "oasis",
            "tsunami",
            "mangrove forest",
            "great barrier reef",
            "geyser",
            "canyon",
            "swamp",
            "cave",
            "glacier",
            "atoll",
            "plateau",
        ],
        "Transport": [
            "helicopter",
            "submarine",
            "airship",
            "ferry",
            "tractor",
            "motorcycle",
            "tram",
            "cable car",
            "catamaran",
            "quad bike",
            "rocket",
            "handcar",
            "rickshaw",
            "snowmobile",
            "scooter",
        ],
        "Clothing": [
            "kimono",
            "peacoat",
            "poncho",
            "sundress",
            "turban",
            "tuxedo",
            "leg warmers",
            "breastplate",
            "apron",
            "breeches",
            "pyjamas",
            "cap",
            "mittens",
            "waistcoat",
            "mantle",
        ],
        "Music": [
            "cello",
            "balalaika",
            "maracas",
            "double bass",
            "accordion",
            "banjo",
            "didgeridoo",
            "bagpipes",
            "harp",
            "castanets",
            "sitar",
            "oboe",
            "bassoon",
            "tam-tam",
            "ukulele",
        ],
        "Space": [
            "asteroid",
            "nebula",
            "gravity",
            "orbit",
            "solar wind",
            "black hole",
            "neutron star",
            "space station",
            "crater",
            "meteorite",
            "galaxy",
            "supernova",
            "pulsar",
            "atmosphere",
            "probe",
        ],
        "Random": [
            "boomerang",
            "umbrella",
            "hookah",
            "alarm clock",
            "telescope",
            "periscope",
            "labyrinth",
            "domino",
            "pendulum",
            "compass",
            "flashlight",
            "slingshot",
            "soldering iron",
            "kaleidoscope",
            "whirlpool",
            "fireworks",
            "parachute",
            "rocking chair",
            "metronome",
        ],
    },
}

# ── Category aliases (case-insensitive, both languages) ───────────────────────

_CATEGORY_ALIASES: dict[str, tuple[str, str]] = {
    # Russian aliases → (lang, canonical_key)
    "животные": ("ru", "Животные"),
    "животное": ("ru", "Животные"),
    "звери": ("ru", "Животные"),
    "зверь": ("ru", "Животные"),
    "animal": ("en", "Animals"),
    "animals": ("en", "Animals"),
    "еда": ("ru", "Еда"),
    "пища": ("ru", "Еда"),
    "напитки": ("ru", "Еда"),
    "food": ("en", "Food"),
    "профессии": ("ru", "Профессии"),
    "профессия": ("ru", "Профессии"),
    "работа": ("ru", "Профессии"),
    "специальность": ("ru", "Профессии"),
    "professions": ("en", "Professions"),
    "profession": ("en", "Professions"),
    "спорт": ("ru", "Спорт"),
    "sport": ("en", "Sports"),
    "sports": ("en", "Sports"),
    "фильмы": ("ru", "Фильмы"),
    "кино": ("ru", "Фильмы"),
    "фильм": ("ru", "Фильмы"),
    "movies": ("en", "Movies"),
    "films": ("en", "Movies"),
    "техника": ("ru", "Техника"),
    "технологии": ("ru", "Техника"),
    "гаджеты": ("ru", "Техника"),
    "tech": ("en", "Tech"),
    "technology": ("en", "Tech"),
    "природа": ("ru", "Природа"),
    "явления": ("ru", "Природа"),
    "nature": ("en", "Nature"),
    # ── New categories ────────────────────────────────────────────────────────
    "транспорт": ("ru", "Транспорт"),
    "transport": ("en", "Transport"),
    "transportation": ("en", "Transport"),
    "vehicles": ("en", "Transport"),
    "машины": ("ru", "Транспорт"),
    "одежда": ("ru", "Одежда"),
    "clothing": ("en", "Clothing"),
    "clothes": ("en", "Clothing"),
    "fashion": ("en", "Clothing"),
    "наряды": ("ru", "Одежда"),
    "музыка": ("ru", "Музыка"),
    "music": ("en", "Music"),
    "instruments": ("en", "Music"),
    "инструменты": ("ru", "Музыка"),
    "космос": ("ru", "Космос"),
    "space": ("en", "Space"),
    "astronomy": ("en", "Space"),
    "астрономия": ("ru", "Космос"),
    "вселенная": ("ru", "Космос"),
    # ── Misc ─────────────────────────────────────────────────────────────────
    "разное": ("ru", "Разное"),
    "random": ("en", "Random"),
    "misc": ("en", "Random"),
    "разн": ("ru", "Разное"),
    "случайное": ("ru", "Разное"),
    "вектор": ("ru", "Разное"),
    "предметы": ("ru", "Разное"),
    "вещи": ("ru", "Разное"),
}


_YO_REPLACE = str.maketrans({"ё": "е", "Ё": "Е"})
_TOPIC_SEP_RE = re.compile(r"[^0-9a-zA-Zа-яА-Я\s]+", re.UNICODE)
_TOPIC_SPACE_RE = re.compile(r"\s+")

# Lightweight lexical normalization for high-impact topic variants.
_TOPIC_TOKEN_NORMALIZATION: dict[str, str] = {
    "герои": "герой",
    "героев": "герой",
    "героя": "герой",
    "персонажи": "персонаж",
    "персонажей": "персонаж",
    "champions": "champion",
    "characters": "character",
    "лиги": "лига",
    "легенд": "легенда",
}

_LEAGUE_VARIANTS: tuple[str, ...] = (
    "league of legends",
    "league legends",
    "лига легенд",
    "лиги легенд",
    "lol",
)


def _normalise_topic_text(raw: str) -> str:
    """Normalize topic text for alias/similarity matching."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = text.translate(_YO_REPLACE)
    text = text.casefold().strip()
    text = _TOPIC_SEP_RE.sub(" ", text)
    text = _TOPIC_SPACE_RE.sub(" ", text).strip()
    return text


def _normalise_topic_tokens(raw: str) -> list[str]:
    normalized = _normalise_topic_text(raw)
    if not normalized:
        return []
    tokens = normalized.split(" ")
    return [_TOPIC_TOKEN_NORMALIZATION.get(tok, tok) for tok in tokens if tok]


def _build_topic_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _looks_like_lol_topic(match_key: str, normalized_tokens: list[str]) -> bool:
    if any(variant in match_key for variant in _LEAGUE_VARIANTS):
        return True
    token_set = set(normalized_tokens)
    return (
        {"league", "of", "legends"}.issubset(token_set) or {"лига", "легенда"}.issubset(token_set) or "lol" in token_set
    )


def _resolve_special_topic(raw: str, lang: str) -> TopicResolution | None:
    match_key = _normalise_topic_text(raw)
    if not match_key:
        return None
    tokens = _normalise_topic_tokens(raw)
    token_set = set(tokens)

    if _looks_like_lol_topic(match_key, tokens) and ({"герой", "персонаж", "champion", "character"} & token_set):
        display = "Персонажи League of Legends" if lang == "ru" else "League of Legends Champions"
        return TopicResolution(
            topic_id="special:lol_champions",
            lang=lang,
            category=display,
            raw=raw,
            match_key=match_key,
            is_builtin=False,
        )
    return None


def resolve_category(raw: str) -> tuple[str, str] | None:
    """Resolve a user-supplied category string to (lang, canonical_key).

    Returns None if no match found (caller should use a random category).
    """
    key = _normalise_topic_text(raw)
    if not key:
        return None
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]

    # Try prefix match or root match safely
    for alias, pair in _CATEGORY_ALIASES.items():
        if len(key) >= 3 and (alias.startswith(key) or key.startswith(alias)):
            return pair

    return None


def resolve_topic(raw: str) -> TopicResolution:
    """Resolve raw user topic into a stable topic_id and display category."""
    cleaned = (raw or "").strip() or "разное"
    match_key = _normalise_topic_text(cleaned)
    lang = _detect_lang(cleaned)

    resolved = resolve_category(cleaned)
    if resolved:
        resolved_lang, resolved_category = resolved
        normalized_builtin = _normalise_topic_text(f"{resolved_lang}:{resolved_category}")
        return TopicResolution(
            topic_id=f"builtin:{normalized_builtin}",
            lang=resolved_lang,
            category=resolved_category,
            raw=cleaned,
            match_key=match_key,
            is_builtin=True,
        )

    special = _resolve_special_topic(cleaned, lang=lang)
    if special:
        return special

    topic_id = _build_topic_id(f"custom:{lang}", match_key or cleaned.casefold())
    return TopicResolution(
        topic_id=topic_id,
        lang=lang,
        category=cleaned,
        raw=cleaned,
        match_key=match_key,
        is_builtin=False,
    )


def list_categories(lang: str = "ru") -> list[str]:
    """Return list of canonical category keys for the given lang."""
    return list(WORD_BANK.get(lang, {}).keys())


def _detect_lang(text: str) -> str:
    """Heuristic: count Cyrillic letters against all alphabetic letters."""
    if not text:
        return "ru"
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "ru"
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    return "ru" if cyrillic / len(letters) > 0.3 else "en"


# ── Word validation ───────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"^[\w\s\-']+$", re.UNICODE)
_WORD_MAX_LEN = 40
_WORD_MIN_LEN = 2


def validate_custom_word(word: str) -> str | None:
    """Validate and normalise a custom word from the user.

    Returns the cleaned word, or None if invalid.
    """
    w = word.strip().lower()
    if len(w) < _WORD_MIN_LEN or len(w) > _WORD_MAX_LEN:
        return None
    if not _WORD_RE.match(w):
        return None
    return w


# ── Reverse word-to-category index ────────────────────────────────────────────
# Flat dict: lowercase_word → canonical_category_key.
# Built once at import time so lookup is O(1).
# Used by find_word_category() to enrich custom-word hint context (Bug-6.4).

_WORD_TO_CATEGORY: dict[str, str] = {
    word: category for _lang_bank in WORD_BANK.values() for category, words in _lang_bank.items() for word in words
}


def find_word_category(word: str) -> str | None:
    """Return the canonical category name if the word exists in any built-in list.

    Normalises to lowercase before lookup. Returns None if the word is not
    in the built-in bank (i.e., it is a genuinely custom player word).
    """
    return _WORD_TO_CATEGORY.get(word.strip().lower())


async def resolve_custom_word_category(word: str) -> str:
    """Classify a custom word strictly into a canonical category, or fallback to 'Слово игрока' / 'Разное'."""
    local_cat = find_word_category(word)
    if local_cat:
        return local_cat

    # Check in-process+disk cache before calling the LLM
    from app.games.judgement_cache import cache_word_category, get_cached_word_category

    cached_cat = await get_cached_word_category(word)
    if cached_cat:
        return cached_cat

    _ALL_CATS = (
        "Животные",
        "Еда",
        "Профессии",
        "Спорт",
        "Фильмы",
        "Техника",
        "Природа",
        "Транспорт",
        "Одежда",
        "Музыка",
        "Космос",
        "Разное",
    )
    prompt = (
        f"К какой категории из списка: {list(_ALL_CATS)} "
        f"лучше всего относится слово '{word}'?\n"
        "Ответь ТОЛЬКО названием одной категории. "
        "Если ни одна категория строго не подходит, ответь 'Разное'."
    )

    for model in (_GEN_PRIMARY_MODEL, _GEN_FALLBACK_MODEL):
        try:
            from app.agent_use_cases import AgentRequestUseCase
            from app.providers.router import get_ai_response

            use_case = AgentRequestUseCase()
            kd, mdl, _ = await use_case.resolve_ai_request(model)
            if not kd or not mdl:
                continue

            response_text, _ = await asyncio.wait_for(
                get_ai_response(
                    api_key=kd["api_key"],
                    history=[{"role": "user", "parts": [prompt]}],
                    model_name=mdl,
                    max_retries=1,
                ),
                timeout=8.0,
            )
            raw = (response_text or "").strip().strip("`'\" \r\n.")
            for valid_cat in _ALL_CATS:
                if valid_cat.lower() in raw.lower():
                    # Persist to cache so same word never hits LLM again
                    await cache_word_category(word, valid_cat)
                    return valid_cat
            await cache_word_category(word, "Разное")
            return "Разное"
        except Exception as exc:
            logger.warning("Category resolve failed for %r model=%s: %r", word, model, exc)

    return "Слово игрока (произвольная тема)"


# ── AI-generated word bank ───────────────────────────────────────────────────

# In-process cache: (lang, canonical_category) → word list
# Avoids re-generating the same custom category within a process lifetime.
_GENERATED_CACHE: dict[str, list[str]] = {}
_GENERATED_INFLIGHT: dict[str, asyncio.Task[list[str] | None]] = {}
# In-process rotation state: topic_id -> (bank_hash, order, cursor)
_TOPIC_ROTATION: dict[str, tuple[str, list[str], int]] = {}

# Gemini models tried in order for word generation
_GEN_PRIMARY_MODEL = "opencode-go/minimax-m2.7"
_GEN_FALLBACK_MODEL = "gemini-2.5-flash"
_GEN_TIMEOUT_S = 18.0

_GEN_PROMPT = (
    "Ты помощник игры 'Крокодил'. Придумай ровно 20 существительных на тему \"{category}\"."
    " Слова должны быть:"
    ' конкретные, легко изображаемые жестами; от 1 до 3 слов в словосочетании; на "{lang_hint}".'
    ' Ответь ТОЛЬКО JSON-массивом строк, без пояснений. Пример: ["слово1","слово2"]'
)


def _generated_cache_key(lang: str, category: str, *, topic_id: str | None = None) -> str:
    normalized = f"{lang.lower().strip()}:{category.lower().strip()}"
    if topic_id:
        return f"{topic_id}|{normalized}"
    return normalized


def _topic_bank_hash(words: list[str]) -> str:
    payload = "\x1f".join(sorted(words))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pick_rotating_word(topic_id: str, words: list[str], used: set[str] | None = None) -> str:
    """Pick next word from a shuffled per-topic cycle to avoid repeats."""
    if not words:
        raise ValueError("word list cannot be empty")

    bank_hash = _topic_bank_hash(words)
    existing = _TOPIC_ROTATION.get(topic_id)
    if existing is None or existing[0] != bank_hash:
        order = list(words)
        random.shuffle(order)
        _TOPIC_ROTATION[topic_id] = (bank_hash, order, 0)
        existing = _TOPIC_ROTATION[topic_id]

    _, order, cursor = existing
    order_len = len(order)
    available = set(words)
    if used:
        available -= used

    # If all words are currently "used", continue regular cycle.
    if not available:
        available = set(words)

    chosen = order[cursor % order_len]
    if chosen not in available:
        for step in range(order_len):
            candidate = order[(cursor + step) % order_len]
            if candidate in available:
                chosen = candidate
                cursor += step
                break

    next_cursor = (cursor + 1) % order_len
    _TOPIC_ROTATION[topic_id] = (bank_hash, order, next_cursor)
    return chosen


def _normalise_generated_words(words: list[object]) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for raw_word in words:
        if not isinstance(raw_word, str):
            continue
        word = re.sub(r"\s+", " ", raw_word.strip().lower())
        if not (2 <= len(word) <= 60) or word in seen:
            continue
        seen.add(word)
        clean.append(word)
    return clean


async def generate_words_for_category(
    category: str,
    *,
    lang: str = "ru",
    topic_id: str | None = None,
) -> list[str] | None:
    """Call LLM to generate 20 words for an unknown category.

    Returns None if the category is invalid/unintelligible or LLM times out.
    Results are cached in-process and persisted to the local game cache so
    repeated calls and restarts don't re-invoke the LLM for the same category.
    """
    from app.games.judgement_cache import cache_generated_words, get_cached_generated_words

    category = category.strip()
    topic_id_norm = (topic_id or "").strip()
    cache_key = _generated_cache_key(lang, category, topic_id=topic_id_norm or None)
    if cache_key in _GENERATED_CACHE:
        return _GENERATED_CACHE[cache_key]

    if topic_id_norm:
        cached = await get_cached_generated_words(lang, category, topic_id=topic_id_norm)
    else:
        cached = await get_cached_generated_words(lang, category)
    # Migration fallback: old cache entries were keyed without topic_id.
    if not cached and topic_id_norm:
        cached = await get_cached_generated_words(lang, category)
    if cached:
        _GENERATED_CACHE[cache_key] = cached
        logger.info("Hydrated AI-generated words for category %r (%s) from persistent cache", category, lang)
        if topic_id_norm:
            # Write-through into topic-aware key so next lookup stays isolated.
            await cache_generated_words(lang, category, cached, topic_id=topic_id_norm)
        return cached

    inflight = _GENERATED_INFLIGHT.get(cache_key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    lang_hint = "русском" if lang == "ru" else "English"
    # Ensure system constraint explicitly for json arrays:
    prompt = _GEN_PROMPT.format(category=category.strip(), lang_hint=lang_hint)

    async def _do_generate() -> list[str] | None:
        for model in (_GEN_PRIMARY_MODEL, _GEN_FALLBACK_MODEL):
            try:
                from app.errors import is_error_message
                from app.providers.router import get_provider_router

                router = get_provider_router()

                # Using ProviderRouter handles keys, timeouts, and circuit breaking natively
                response_text, _ = await router.get_response(
                    preferred_model=model,
                    history=[{"role": "user", "parts": [prompt]}],
                    max_key_retries=1,
                    timeout=_GEN_TIMEOUT_S,
                )
                raw = (response_text or "").strip()

                if is_error_message(raw):
                    logger.warning(
                        "Word gen failed for %r (model=%s): Provider returned error: %s", category, model, raw
                    )
                    continue

                # Strip markdown code fences if model wraps output
                raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
                raw = re.sub(r"\n?```$", "", raw).strip()

                words = json.loads(raw)
                if not isinstance(words, list) or len(words) < 5:
                    logger.warning("Gemini returned bad word list for %r: %r", category, words)
                    return None

                clean = _normalise_generated_words(words)
                if len(clean) < 5:
                    return None

                _GENERATED_CACHE[cache_key] = clean
                if topic_id_norm:
                    await cache_generated_words(lang, category, clean, topic_id=topic_id_norm)
                else:
                    await cache_generated_words(lang, category, clean)
                logger.info("Generated %d words for custom category %r (%s)", len(clean), category, model)
                return clean

            except (TimeoutError, json.JSONDecodeError) as exc:
                logger.warning("Word gen failed for %r (model=%s): %r", category, model, exc)
            except Exception as exc:
                logger.exception("Word gen unexpected error for %r (model=%s): %r", category, model, exc)

        return None

    generation_task = asyncio.create_task(_do_generate())
    _GENERATED_INFLIGHT[cache_key] = generation_task
    try:
        return await asyncio.shield(generation_task)
    finally:
        if _GENERATED_INFLIGHT.get(cache_key) is generation_task:
            _GENERATED_INFLIGHT.pop(cache_key, None)


async def _generate_single_word_fast(category: str, lang: str = "ru") -> str | None:
    """Fast inline request for exactly ONE word to immediately start a game."""
    from app.config import settings

    lang_hint = "русском" if lang == "ru" else "English"
    prompt = (
        f"Ты помощник игры 'Крокодил'. Придумай ровно 1 существительное на тему \"{category}\". "
        f"Язык: {lang_hint}. Ответь ТОЛЬКО одним словом/фразой (1-3 слова), без пояснений, без кавычек."
    )

    # We rely on the fastest inline model available
    model = settings.OPENCODE_INLINE_MODEL or "gemini-2.5-flash"
    try:
        from app.errors import is_error_message
        from app.providers.router import get_provider_router

        router = get_provider_router()
        response_text, _ = await router.get_response(
            preferred_model=model,
            history=[{"role": "user", "parts": [prompt]}],
            max_key_retries=1,
            timeout=7.0,  # Strict timeout for instantaneous response
        )

        raw = (response_text or "").strip().strip("`'\" \r\n.")
        if is_error_message(raw):
            logger.warning("Fast inline word gen failed for %r: Provider returned error: %s", category, raw)
            return None

        if 2 <= len(raw) <= 60:
            logger.info("Fast inline word generated for %r: %r", category, raw)
            return raw.lower()
    except Exception as exc:
        logger.warning("Fast inline word gen failed for %r: %r", category, exc)

    return None


# ── Random word picker ────────────────────────────────────────────────────────


async def pick_random_word_for_topic(
    topic: TopicResolution,
    *,
    redis_used_key: str | None = None,
) -> tuple[str, str, str, bool]:
    """Pick a random word for an already-resolved topic."""
    is_generated = False

    if topic.is_builtin:
        lang = topic.lang
        category = topic.category
        words = list(WORD_BANK[lang][category])
    else:
        from app.games.judgement_cache import get_cached_generated_words

        lang = topic.lang
        category = topic.category
        cache_key = _generated_cache_key(lang, category, topic_id=topic.topic_id)

        if cache_key in _GENERATED_CACHE and len(_GENERATED_CACHE[cache_key]) > 0:
            words = _GENERATED_CACHE[cache_key]
            is_generated = True
        else:
            cached_words = await get_cached_generated_words(lang, category, topic_id=topic.topic_id)
            if not cached_words:
                # Migration fallback for entries created before topic_id support.
                cached_words = await get_cached_generated_words(lang, category)
            if cached_words:
                _GENERATED_CACHE[cache_key] = cached_words
                words = cached_words
                is_generated = True
                logger.info("Using persisted AI-generated words for category %r (%s)", category, lang)
            else:
                # First response path: return one fast word, pre-warm full bank in background.
                fast_word = await _generate_single_word_fast(category, lang)
                if not fast_word:
                    generated = await generate_words_for_category(category, lang=lang, topic_id=topic.topic_id)
                    if not generated:
                        raise ValueError(f"unintelligible_category:{topic.raw!r}")
                    words = generated
                else:
                    _GENERATED_CACHE.setdefault(cache_key, [fast_word])
                    submit_task(generate_words_for_category(category, lang=lang, topic_id=topic.topic_id))
                    return fast_word, lang, category, True

                is_generated = True
                logger.info("Using AI-generated words for category %r (%s)", category, lang)

    # De-duplicate via Redis (best-effort; Redis miss is non-fatal).
    used: set[str] = set()
    if redis_used_key:
        try:
            from app.cache import redis_client

            if redis_client:
                raw = await redis_client.smembers(redis_used_key)  # type: ignore[misc]
                used = {m.decode() if isinstance(m, bytes) else m for m in raw}
        except Exception as exc:
            logger.debug("Redis smembers failed for %s: %s", redis_used_key, exc)

    # Reset used-set only when all known words are exhausted.
    if used and all(word in used for word in words):
        if redis_used_key:
            try:
                from app.cache import redis_client

                if redis_client:
                    await redis_client.delete(redis_used_key)  # type: ignore[misc]
            except Exception:
                pass
        used = set()
        logger.info("Used-words set reset for key=%s topic=%s", redis_used_key, topic.topic_id)

    chosen = _pick_rotating_word(topic.topic_id, words, used=used)

    if redis_used_key:
        try:
            from app.cache import redis_client

            if redis_client:
                await redis_client.sadd(redis_used_key, chosen)  # type: ignore[misc]
                await redis_client.expire(redis_used_key, 3600)  # 1h TTL
        except Exception as exc:
            logger.debug("Redis sadd failed: %s", exc)

    return chosen, topic.lang, topic.category, is_generated


async def pick_random_word(
    category_raw: str,
    *,
    redis_used_key: str | None = None,
) -> tuple[str, str, str, bool]:
    """Pick a random word from the bank for a raw user topic string."""
    topic = resolve_topic(category_raw)
    return await pick_random_word_for_topic(topic, redis_used_key=redis_used_key)
