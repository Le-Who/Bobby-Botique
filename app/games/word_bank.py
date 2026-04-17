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
import json
import logging
import random
import re

logger = logging.getLogger(__name__)

# ── Word bank ─────────────────────────────────────────────────────────────────

WORD_BANK: dict[str, dict[str, list[str]]] = {
    "ru": {
        "Животные": [
            "крокодил", "жираф", "пингвин", "хамелеон", "дельфин", "кенгуру",
            "коала", "осьминог", "броненосец", "утконос", "перепел", "бобёр",
            "носорог", "зебра", "горилла", "ягуар", "обезьяна", "медуза",
            "кальмар", "морж", "козёл", "антилопа", "гепард", "страус",
            "попугай", "пеликан", "хомяк", "мангуст", "кабан", "варан",
        ],
        "Еда": [
            "пицца", "суши", "борщ", "шаурма", "тирамису", "оливье", "хинкали",
            "блин", "чизбургер", "рамэн", "карри", "гаспачо", "паэлья",
            "ризотто", "круассан", "эклер", "кимчи", "авокадо", "гуакамоле",
            "стейк", "лазанья", "чизкейк", "макаруны", "вафля", "пельмени",
            "щи", "котлета", "шашлык", "буррито", "тако",
        ],
        "Профессии": [
            "программист", "врач", "пилот", "архитектор", "фотограф",
            "дирижёр", "скульптор", "хирург", "детектив", "астронавт",
            "дипломат", "сомелье", "ветеринар", "маляр", "шахтёр",
            "водолаз", "режиссёр", "кондитер", "логопед", "геолог",
            "акушер", "ювелир", "таксидермист", "пожарный", "переводчик",
        ],
        "Спорт": [
            "бокс", "фехтование", "сёрфинг", "кёрлинг", "синхронное плавание",
            "триатлон", "скалолазание", "стрельба из лука", "регби", "гольф",
            "бадминтон", "санный спорт", "ватерполо", "крикет", "черлидинг",
            "пятиборье", "прыжки с шестом", "армрестлинг", "скейтбординг",
        ],
        "Фильмы": [
            "матрица", "интерстеллар", "аватар", "начало", "паразиты",
            "форрест гамп", "гладиатор", "шоу шоушенк", "до свидания ленин",
            "амели", "леон", "джокер", "властелин колец", "король лев",
        ],
        "Техника": [
            "пылесос", "микроволновка", "3д принтер", "дрон", "видеокарта",
            "планшет", "умные часы", "проектор", "сканер", "термостат",
            "спутник", "перфоратор", "блендер", "холодильник", "синтезатор",
        ],
        "Природа": [
            "вулкан", "айсберг", "торнадо", "северное сияние", "оазис",
            "цунами", "мангровый лес", "большой барьерный риф", "гейзер",
            "каньон", "болото", "пещера", "ледник", "атолл", "плато",
        ],
        "Разное": [
            "бумеранг", "зонтик", "кальян", "будильник", "телескоп",
            "перископ", "лабиринт", "домино", "маятник", "компас",
            "фонарик", "рогатка", "паяльник", "калейдоскоп", "водоворот",
            "жетон", "фейерверк", "парашют", "кресло-качалка", "метроном",
        ],
    },
    "en": {
        "Animals": [
            "crocodile", "giraffe", "penguin", "chameleon", "dolphin",
            "kangaroo", "koala", "octopus", "armadillo", "platypus",
            "quail", "beaver", "rhinoceros", "zebra", "gorilla",
            "jaguar", "monkey", "jellyfish", "squid", "walrus",
            "antelope", "cheetah", "ostrich", "parrot", "pelican",
            "hamster", "mongoose", "boar", "monitor lizard", "flamingo",
        ],
        "Food": [
            "pizza", "sushi", "borsch", "shawarma", "tiramisu",
            "cookie", "cheeseburger", "ramen", "curry", "gazpacho",
            "paella", "risotto", "croissant", "éclair", "kimchi",
            "avocado", "guacamole", "steak", "lasagna", "cheesecake",
            "macarons", "waffle", "burrito", "taco", "nachos",
            "dumplings", "hotdog", "pancake", "bagel", "pretzel",
        ],
        "Professions": [
            "programmer", "doctor", "pilot", "architect", "photographer",
            "conductor", "sculptor", "surgeon", "detective", "astronaut",
            "diplomat", "sommelier", "veterinarian", "diver", "director",
            "confectioner", "geologist", "obstetrician", "jeweller",
            "taxidermist", "firefighter", "translator", "ufologist",
        ],
        "Sports": [
            "boxing", "fencing", "surfing", "curling", "synchronised swimming",
            "triathlon", "rock climbing", "archery", "rugby", "golf",
            "badminton", "luge", "water polo", "cricket", "cheerleading",
            "modern pentathlon", "pole vault", "arm wrestling", "skateboarding",
        ],
        "Movies": [
            "the matrix", "interstellar", "avatar", "inception", "parasite",
            "forrest gump", "gladiator", "the shawshank redemption", "amelie",
            "leon", "joker", "the lord of the rings", "the lion king",
        ],
        "Tech": [
            "vacuum cleaner", "microwave", "3d printer", "drone", "graphics card",
            "tablet", "smartwatch", "projector", "scanner", "thermostat",
            "satellite", "jackhammer", "blender", "refrigerator", "synthesizer",
        ],
        "Nature": [
            "volcano", "iceberg", "tornado", "aurora borealis", "oasis",
            "tsunami", "mangrove forest", "great barrier reef", "geyser",
            "canyon", "swamp", "cave", "glacier", "atoll", "plateau",
        ],
        "Random": [
            "boomerang", "umbrella", "hookah", "alarm clock", "telescope",
            "periscope", "labyrinth", "domino", "pendulum", "compass",
            "flashlight", "slingshot", "soldering iron", "kaleidoscope",
            "whirlpool", "fireworks", "parachute", "rocking chair", "metronome",
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
    "разное": ("ru", "Разное"),
    "random": ("en", "Random"),
    "misc": ("en", "Random"),
    "разн": ("ru", "Разное"),
    "случайное": ("ru", "Разное"),
    "вектор": ("ru", "Разное"),
    "предметы": ("ru", "Разное"),
    "вещи": ("ru", "Разное"),
}


def resolve_category(raw: str) -> tuple[str, str] | None:
    """Resolve a user-supplied category string to (lang, canonical_key).

    Returns None if no match found (caller should use a random category).
    """
    key = raw.strip().lower()
    if not key:
        return None
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]

    # Try prefix match or root match safely
    for alias, pair in _CATEGORY_ALIASES.items():
        if len(key) >= 3 and (alias.startswith(key) or key.startswith(alias)):
            return pair

    return None


def list_categories(lang: str = "ru") -> list[str]:
    """Return list of canonical category keys for the given lang."""
    return list(WORD_BANK.get(lang, {}).keys())


def _detect_lang(text: str) -> str:
    """Heuristic: count Cyrillic chars vs total."""
    if not text:
        return "ru"
    cyrillic = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    return "ru" if cyrillic / len(text) > 0.3 else "en"


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
    word: category
    for _lang_bank in WORD_BANK.values()
    for category, words in _lang_bank.items()
    for word in words
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
        
    prompt = (
        f"К какой категории из списка: [Животные, Еда, Профессии, Спорт, Фильмы, Техника, Природа, Разное] "
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
                timeout=8.0,  # 4 seconds is too short for Opencode latency
            )
            raw = (response_text or "").strip().strip("`'\" \r\n.")
            for valid_cat in ("Животные", "Еда", "Профессии", "Спорт", "Фильмы", "Техника", "Природа", "Разное"):
                if valid_cat.lower() in raw.lower():
                    return valid_cat
            return "Разное"
        except Exception as exc:
            logger.warning("Category resolve failed for %r model=%s: %r", word, model, exc)
            
    return "Слово игрока (произвольная тема)"


# ── AI-generated word bank ───────────────────────────────────────────────────

# In-process cache: (lang, canonical_category) → word list
# Avoids re-generating the same custom category within a process lifetime.
_GENERATED_CACHE: dict[str, list[str]] = {}

# Gemini models tried in order for word generation
_GEN_PRIMARY_MODEL = "opencode-go/minimax-m2.7"
_GEN_FALLBACK_MODEL = "gemini-2.5-flash"
_GEN_TIMEOUT_S = 18.0

_GEN_PROMPT = (
    "Ты помощник игры 'Крокодил'. Придумай ровно 20 существительных на тему \"{category}\"."
    " Слова должны быть:"
    " конкретные, легко изображаемые жестами; от 1 до 3 слов в словосочетании; на \"{lang_hint}\"."
    " Ответь ТОЛЬКО JSON-массивом строк, без пояснений. Пример: [\"слово1\",\"слово2\"]"
)


async def generate_words_for_category(
    category: str,
    *,
    lang: str = "ru",
) -> list[str] | None:
    """Call LLM to generate 20 words for an unknown category.

    Returns None if the category is invalid/unintelligible or LLM times out.
    Results are cached in-process by (lang, lower(category)) so repeated calls
    within the same container don't re-invoke the LLM.
    """
    cache_key = f"{lang}:{category.lower().strip()}"
    if cache_key in _GENERATED_CACHE:
        return _GENERATED_CACHE[cache_key]

    lang_hint = "русском" if lang == "ru" else "English"
    # Ensure system constraint explicitly for json arrays:
    prompt = _GEN_PROMPT.format(category=category.strip(), lang_hint=lang_hint)
    
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
                timeout=_GEN_TIMEOUT_S,
            )
            raw = (response_text or "").strip()
            # Strip markdown code fences if model wraps output
            raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\n?```$", "", raw).strip()

            words: list[str] = json.loads(raw)
            if not isinstance(words, list) or len(words) < 5:
                logger.warning("Gemini returned bad word list for %r: %r", category, words)
                return None

            # Sanitise: lowercase, strip, filter blanks & invalid chars
            clean = [
                w.strip().lower()
                for w in words
                if isinstance(w, str) and 2 <= len(w.strip()) <= 60
            ]
            if len(clean) < 5:
                return None

            _GENERATED_CACHE[cache_key] = clean
            logger.info("Generated %d words for custom category %r (%s)", len(clean), category, model)
            return clean

        except (TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Word gen failed for %r (model=%s): %r", category, model, exc)
        except Exception as exc:
            logger.exception("Word gen unexpected error for %r (model=%s): %r", category, model, exc)

    return None

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
        from app.agent_use_cases import AgentRequestUseCase
        from app.providers.router import get_ai_response

        use_case = AgentRequestUseCase()
        kd, mdl, _ = await use_case.resolve_ai_request(model)
        if kd and mdl:
            response_text, _ = await asyncio.wait_for(
                get_ai_response(
                    api_key=kd["api_key"],
                    history=[{"role": "user", "parts": [prompt]}],
                    model_name=mdl,
                    max_retries=1,
                ),
                timeout=7.0,  # Strict timeout for instantaneous response
            )
            raw = (response_text or "").strip().strip("`'\" \r\n.")
            if 2 <= len(raw) <= 60:
                logger.info("Fast inline word generated for %r: %r", category, raw)
                return raw.lower()
    except Exception as exc:
        logger.warning("Fast inline word gen failed for %r: %r", category, exc)
    
    return None


# ── Random word picker ────────────────────────────────────────────────────────


async def pick_random_word(
    category_raw: str,
    *,
    redis_used_key: str | None = None,
) -> tuple[str, str, str, bool]:
    """Pick a random word from the bank, avoiding recently used words.

    For unknown categories, calls Gemini to generate a word list on the fly.

    Args:
        category_raw: User-supplied category string (e.g. "животные", "food", "пирожки").
        redis_used_key: Optional Redis key for the used-words set (TTL 1h).
                        If None or redis unavailable, no de-duplication.

    Returns:
        (word, lang, canonical_category, is_generated)
        is_generated=True when the word list was AI-generated for an unknown category.
        Raises ValueError if the category is unintelligible (caller should inform user).
    """
    resolved = resolve_category(category_raw)
    is_generated = False

    if resolved:
        lang, category = resolved
        words = list(WORD_BANK[lang][category])
    else:
        # Unknown category → check memory cache first
        lang = _detect_lang(category_raw)
        category = category_raw.strip()
        cache_key = f"{lang}:{category.lower()}"
        
        if cache_key in _GENERATED_CACHE and len(_GENERATED_CACHE[cache_key]) > 0:
            words = _GENERATED_CACHE[cache_key]
            is_generated = True
        else:
            # We don't want to block the player for 15+ seconds.
            # 1) Get ONE word fast
            fast_word = await _generate_single_word_fast(category, lang)
            if not fast_word:
                # If even fast word fails, fallback to full wait
                generated = await generate_words_for_category(category, lang=lang)
                if not generated:
                    raise ValueError(f"unintelligible_category:{category_raw!r}")
                words = generated
            else:
                # Kick off bank generation in the background for future games
                asyncio.create_task(generate_words_for_category(category, lang=lang))
                # Skip redis de-duplication since we only have 1 word and want to return fast
                return fast_word, lang, category, True
                
            is_generated = True
            logger.info("Using AI-generated words for category %r (%s)", category, lang)

    # De-duplicate via Redis (best-effort; Redis miss is non-fatal)
    used: set[str] = set()
    if redis_used_key:
        try:
            from app.cache import redis_client

            if redis_client:
                raw = await redis_client.smembers(redis_used_key)  # type: ignore[misc]
                used = {m.decode() if isinstance(m, bytes) else m for m in raw}
        except Exception as exc:
            logger.debug("Redis smembers failed for %s: %s", redis_used_key, exc)

    available = [w for w in words if w not in used]
    if not available:
        # All words exhausted — reset used set and try again
        available = words
        if redis_used_key:
            try:
                from app.cache import redis_client

                if redis_client:
                    await redis_client.delete(redis_used_key)  # type: ignore[misc]
            except Exception:
                pass
        logger.info("Used-words set reset for key=%s", redis_used_key)

    chosen = random.choice(available)

    if redis_used_key:
        try:
            from app.cache import redis_client

            if redis_client:
                await redis_client.sadd(redis_used_key, chosen)  # type: ignore[misc]
                await redis_client.expire(redis_used_key, 3600)  # 1h TTL
        except Exception as exc:
            logger.debug("Redis sadd failed: %s", exc)

    return chosen, lang, category, is_generated
