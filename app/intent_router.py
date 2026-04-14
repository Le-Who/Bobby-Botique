# /app/intent_router.py
"""Intent-based Direct Routing — bypass LLM for simple, deterministic queries.

Detects weather and currency intents from user messages and routes them
directly to lightweight APIs (Open-Meteo, Frankfurter) instead of
consuming an LLM call. Falls back to the standard LLM pipeline when
intent is ambiguous or the API call fails.

Plan §4 implementation.
"""

import logging
import re

import httpx

# ── Constants ────────────────────────────────────────────────────────────────

_WEATHER_PATTERNS = re.compile(
    r"(?:погод[аеуыю]|temperature|weather|прогноз\s*погод)"
    r"|(?:сколько\s+градус)|(?:температур[аеуыю])",
    re.IGNORECASE,
)

_CURRENCY_PATTERNS = re.compile(
    r"(?:курс|exchange\s*rate|convert)"
    r"|(?:(?:доллар|евро|рубл|usd|eur|rub|gbp|jpy|cny)\S*\s+(?:к|в|to|in)\s+)"
    r"|(?:сколько\s+(?:стоит\s+)?(?:доллар|евро|рубл|usd|eur|rub))",
    re.IGNORECASE,
)

_CITY_ALIASES: dict[str, tuple[float, float]] = {
    # Russian city names → (lat, lon)
    "москва": (55.7558, 37.6173),
    "москве": (55.7558, 37.6173),
    "питер": (59.9343, 30.3351),
    "петербург": (59.9343, 30.3351),
    "спб": (59.9343, 30.3351),
    "санкт-петербург": (59.9343, 30.3351),
    "киев": (50.4501, 30.5234),
    "київ": (50.4501, 30.5234),
    "одесса": (46.4825, 30.7233),
    "одессе": (46.4825, 30.7233),
    "одеса": (46.4825, 30.7233),
    "минск": (53.9006, 27.5590),
    "лондон": (51.5074, -0.1278),
    "нью-йорк": (40.7128, -74.0060),
    "нью йорк": (40.7128, -74.0060),
    "токио": (35.6762, 139.6503),
    "париж": (48.8566, 2.3522),
    "берлин": (52.5200, 13.4050),
    "дубай": (25.2048, 55.2708),
    "стамбул": (41.0082, 28.9784),
    "бишкек": (42.8746, 74.5698),
    "алматы": (43.2220, 76.8512),
    "ташкент": (41.2995, 69.2401),
    # English
    "moscow": (55.7558, 37.6173),
    "london": (51.5074, -0.1278),
    "new york": (40.7128, -74.0060),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "tokyo": (35.6762, 139.6503),
    "dubai": (25.2048, 55.2708),
    "istanbul": (41.0082, 28.9784),
}

_CURRENCY_CODES = {
    # Russian aliases → ISO 4217
    "доллар": "USD", "долларов": "USD", "долларах": "USD", "баксов": "USD",
    "евро": "EUR",
    "рубль": "RUB", "рублей": "RUB", "рублях": "RUB", "рубл": "RUB",
    "фунт": "GBP", "фунтов": "GBP",
    "юань": "CNY", "юаней": "CNY",
    "йена": "JPY", "иена": "JPY", "йен": "JPY",
    "тенге": "KZT",
    "гривна": "UAH", "гривен": "UAH",
    "сом": "KGS",
    "сум": "UZS",
    # ISO codes (pass-through)
    "usd": "USD", "eur": "EUR", "rub": "RUB", "gbp": "GBP",
    "jpy": "JPY", "cny": "CNY", "kzt": "KZT", "uah": "UAH",
    "kgs": "KGS", "uzs": "UZS", "try": "TRY", "chf": "CHF",
}

# Pattern to extract a city candidate from weather queries.
# Captures the word(s) immediately following trigger prepositions/words.
# Works for Russian ("погода сегодня в Когалыме") and English ("weather in New York").
#
# Design: two branches:
#   Branch A: triggered by "погода/weather" → temporal words are CONSUMED (не захватываются),
#             then "в/во / in/for" is REQUIRED so we skip to the actual city word.
#   Branch B: plain "в/во" preposition fallback when Branch A misses.
#
# BUG HISTORY: v1 had (в\s+)? as optional in Branch A, causing temporal modifiers like
# "сегодня", "завтра", "сейчас" to be captured as the city name. Fixed by making the
# preposition required and temporal words an explicit whitelist.
_CITY_EXTRACT_PATTERN = re.compile(
    # Branch A: "погода [сегодня|завтра|сейчас] в <город>" — preposition is required here
    r"(?:погод[ауеыя]\s+(?:(?:сейчас|сегодня|завтра)\s+)?(?:в\s+|во\s+)"
    r"|weather\s+(?:(?:today|tomorrow|now)\s+)?(?:in\s+|for\s+)"
    # Branch B: bare "в/во" preposition anywhere in the sentence
    r"|(?<![а-яёa-z])в\s+|(?<![а-яёa-z])во\s+)"
    # Capture: city name — 1 or 2 hyphenated/spaced words (e.g. "New York", "Нью-Йорк").
    # The optional second word must be immediately followed by a hard boundary so that
    # trailing temporal words ("сейчас", "today") are not swallowed.
    r"([А-ЯЁа-яёa-zA-Z][а-яёa-zA-Z\-]*(?:\s[А-ЯЁа-яёa-zA-Z][а-яёa-zA-Z\-]*)?)(?=[,?!.\s]|$)",
    re.IGNORECASE,
)



# Common Russian locative/prepositional case suffixes to strip before geocoding.
# Order matters: longer suffixes first to avoid partial matches.
_RUSSIAN_CITY_SUFFIXES = (
    "ском", "ской", "ского", "овске", "евске", "инске",
    "ове", "еве", "еве", "ове",
    "ске", "зке",
    "ах", "ях",
    "е", "и", "у", "ю",
)

# Words that may trail after the city name in a voice query.
# Stripped from the raw regex capture before geocoding.
_TRAILING_TEMPORAL_WORDS = frozenset({
    "сейчас", "сегодня", "завтра", "утром", "вечером", "ночью",
    "today", "tomorrow", "now", "tonight", "morning", "evening",
    "пожалуйста", "now", "please",
})


def _clean_candidate(raw: str) -> str:
    """Strip known trailing temporal/query words from a city candidate.

    Example: 'Санкт-Петербурге сейчас' → 'Санкт-Петербурге'
             'Los Angeles today' → 'Los Angeles'
    """
    words = raw.strip().split()
    while words and words[-1].lower() in _TRAILING_TEMPORAL_WORDS:
        words.pop()
    return " ".join(words)



def _normalize_city_candidate(raw: str) -> str:
    """Strip common Russian locative/prepositional case suffixes for geocoding.

    Example: 'Саратове' -> 'Саратов', 'Москве' -> 'Москв' (still resolves fine
    because open-meteo does prefix matching on city names).
    """
    word = raw.strip()
    lower = word.lower()
    for suffix in _RUSSIAN_CITY_SUFFIXES:
        if lower.endswith(suffix) and len(lower) - len(suffix) >= 3:  # keep ≥ 3 root chars
            return word[: len(word) - len(suffix)]
    return word


async def _geocode_city(candidate: str) -> tuple[str, float, float] | None:
    """Look up *candidate* via the Open-Meteo Geocoding API (free, no key).

    Returns (display_name, lat, lon) on success, or None if the city is
    not found or the request fails.  Results are not cached — callers
    should only call this after the local alias dict misses.
    """
    # Strip suffixes to improve match rate ("Саратове" → "Саратов")
    normalized = _normalize_city_candidate(candidate)
    if len(normalized) < 3:
        return None

    try:
        resp = await _get_http().get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": normalized, "count": 1, "language": "ru", "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("Geocoding API failed for '%s': %s", normalized, exc)
        return None

    results = data.get("results")
    if not results:
        logging.debug("Geocoding: no results for '%s'", normalized)
        return None

    hit = results[0]
    lat: float = hit["latitude"]
    lon: float = hit["longitude"]
    # Prefer the English "name" field — it's the canonical city name and
    # safe to display regardless of terminal/encoding issues on the server.
    display: str = hit.get("name") or normalized.capitalize()
    return display, lat, lon


# Shared HTTP client for lightweight API calls
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=8.0)
    return _http


async def close_http_client() -> None:
    """Shutdown the Intent Router HTTP client."""
    global _http
    if _http is not None and not _http.is_closed:
        await _http.aclose()
        _http = None


# ── Public API ───────────────────────────────────────────────────────────────


class IntentResult:
    """Result from a direct intent handler."""

    __slots__ = ("text", "handled")

    def __init__(self, text: str, handled: bool = True):
        self.text = text
        self.handled = handled


async def try_direct_intent(message_text: str) -> IntentResult | None:
    """Attempt to handle the message via direct API calls.

    Returns IntentResult with the formatted response text if a known intent
    is detected and successfully resolved. Returns None if the message
    doesn't match any known intent or the API call failed.
    """
    text = message_text.strip()

    # Try weather first (more common)
    if _WEATHER_PATTERNS.search(text):
        result = await _handle_weather(text)
        if result:
            return result

    # Then currency
    if _CURRENCY_PATTERNS.search(text):
        result = await _handle_currency(text)
        if result:
            return result

    return None


# ── Weather (Open-Meteo) ─────────────────────────────────────────────────────


async def _handle_weather(text: str) -> IntentResult | None:
    """Extract city and fetch current weather from Open-Meteo (free, no API key).

    Resolution order:
      1. Hardcoded _CITY_ALIASES (O(n) scan, ~zero latency)
      2. Open-Meteo Geocoding API fallback (~300 ms, handles any world city)
      3. Return None → fall back to LLM/QnA Search
    """
    # Bail out to LLM if the user asks for a future or multi-day/hourly forecast.
    # The LLM (with Search Grounding) is much better at formatting localized or specific-time forecasts.
    if re.search(
        r"(завтра|послезавтра|недел|дней|дня|выходны|tomorrow|week|days|вечер|утр[ао]|ноч[ью]|час|hour|night|evening|morning)",
        text,
        re.IGNORECASE,
    ):
        return None

    city_name, coords = _extract_city(text)

    if not coords:
        # Alias miss — try live geocoding
        m = _CITY_EXTRACT_PATTERN.search(text)
        candidate = _clean_candidate(m.group(1)) if m else ""

        if candidate:
            geo = await _geocode_city(candidate)
            if geo:
                city_name, lat, lon = geo
                coords = (lat, lon)
                logging.debug("Geocoded '%s' → %s (%.4f, %.4f)", candidate, city_name, lat, lon)

    if not coords:
        return None  # Can't determine city → fall back to LLM

    lat, lon = coords
    try:
        resp = await _get_http().get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "timezone": "auto",
                "forecast_days": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.warning("Open-Meteo API failed for %s: %s", city_name, e)
        return None  # Fall back to LLM

    current = data.get("current", {})
    temp = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    wmo_code = current.get("weather_code", 0)

    if temp is None:
        return None

    condition = _wmo_to_emoji(wmo_code)
    display_city = city_name.capitalize()

    response = (
        f"{condition} **Погода в {display_city}**\n\n"
        f"🌡 Температура: **{temp}°C**\n"
        f"💧 Влажность: **{humidity}%**\n"
        f"💨 Ветер: **{wind} км/ч**\n\n"
        f"_Данные: Open-Meteo (в реальном времени)_"
    )
    return IntentResult(response)


def _extract_city(text: str) -> tuple[str, tuple[float, float] | None]:
    """Extract city name from text and resolve to coordinates."""
    lower = text.lower()
    # Try direct alias match (longest match first)
    for alias in sorted(_CITY_ALIASES.keys(), key=len, reverse=True):
        if alias in lower:
            return alias, _CITY_ALIASES[alias]
    return "", None


def _wmo_to_emoji(code: int) -> str:
    """Convert WMO weather code to emoji description."""
    if code == 0:
        return "☀️ Ясно"
    elif code in (1, 2, 3):
        return "🌤 Переменная облачность"
    elif code in (45, 48):
        return "🌫 Туман"
    elif code in (51, 53, 55, 56, 57):
        return "🌧 Морось"
    elif code in (61, 63, 65, 66, 67):
        return "🌧 Дождь"
    elif code in (71, 73, 75, 77):
        return "🌨 Снег"
    elif code in (80, 81, 82):
        return "🌦 Ливень"
    elif code in (85, 86):
        return "🌨 Сильный снегопад"
    elif code in (95, 96, 99):
        return "⛈ Гроза"
    return "🌥 Облачно"


# ── Currency (Frankfurter API) ───────────────────────────────────────────────


async def _handle_currency(text: str) -> IntentResult | None:
    """Extract currency pair and fetch exchange rate from Frankfurter API."""
    base, target = _extract_currency_pair(text)
    if not base or not target:
        return None  # Can't determine pair → fall back to LLM

    # Frankfurter doesn't support RUB — fall back to LLM for RUB pairs
    unsupported = {"RUB", "KZT", "UAH", "KGS", "UZS"}
    if base in unsupported or target in unsupported:
        return None  # LLM will handle via Tavily search

    try:
        resp = await _get_http().get(
            "https://api.frankfurter.dev/v1/latest",
            params={"from": base, "to": target},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logging.warning("Frankfurter API failed for %s→%s: %s", base, target, e)
        return None

    rates = data.get("rates", {})
    rate = rates.get(target)
    if rate is None:
        return None

    date = data.get("date", "")
    response = (
        f"💱 **Курс {base} → {target}**\n\n"
        f"1 {base} = **{rate:.4f} {target}**\n\n"
        f"_Данные: Frankfurter ({date})_"
    )
    return IntentResult(response)


def _extract_currency_pair(text: str) -> tuple[str | None, str | None]:
    """Extract base and target currency codes from text."""
    lower = text.lower()
    found: list[str] = []

    # Find all currency mentions in order
    for alias in sorted(_CURRENCY_CODES.keys(), key=len, reverse=True):
        if alias in lower:
            code = _CURRENCY_CODES[alias]
            if code not in found:
                found.append(code)
            if len(found) >= 2:
                break

    if len(found) >= 2:
        return found[0], found[1]
    elif len(found) == 1:
        # Single currency mentioned — default to USD base or RUB target
        single = found[0]
        if single == "USD" or single == "RUB":
            return "USD", "RUB"
        return single, "RUB"

    return None, None
