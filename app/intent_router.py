# /app/intent_router.py
"""Intent-based Direct Routing — bypass LLM for simple, deterministic queries.

Detects weather and currency intents from user messages and routes them
directly to lightweight APIs instead of consuming an LLM call.

Provider hierarchy (2026):
  Weather:  WeatherAPI.com (1 req, autogeocode, Russian text) → Open-Meteo fallback
  Fiat:     ExchangeRate-API (RUB/KZT/UAH supported) → Frankfurter fallback
  Crypto:   CoinGecko Demo (keyless, 30 rpm)

Falls back to the standard LLM pipeline when intent is ambiguous or all APIs fail.
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

_HOROSCOPE_PATTERNS = re.compile(
    r"(?:гороскоп|зодиак|\bhoroscope\b|\bzodiac\b)",
    re.IGNORECASE,
)

_ZODIAC_MAPPING = {
    "aries": re.compile(r"\b(?:овен|овна|овну|овном|овне|aries)\b", re.IGNORECASE),
    "taurus": re.compile(r"\b(?:телец|тельца|тельцу|тельцом|тельце|taurus)\b", re.IGNORECASE),
    "gemini": re.compile(r"\b(?:близнецы|близнецов|близнецам|близнецами|близнецах|gemini)\b", re.IGNORECASE),
    "cancer": re.compile(r"\b(?:рак|рака|раку|раком|раке|раки|раков|cancer)\b", re.IGNORECASE),
    "leo": re.compile(r"\b(?:лев|льва|льву|львом|льве|львы|львов|leo)\b", re.IGNORECASE),
    "virgo": re.compile(r"\b(?:дева|девы|деве|деву|девой|дев|virgo)\b", re.IGNORECASE),
    "libra": re.compile(r"\b(?:весы|весов|весам|весами|весах|libra)\b", re.IGNORECASE),
    "scorpio": re.compile(r"\b(?:скорпион|скорпиона|скорпиону|скорпионом|скорпионе|скорпионы|скорпионов|scorpio)\b", re.IGNORECASE),
    "sagittarius": re.compile(r"\b(?:стрелец|стрельца|стрельцу|стрельцом|стрельце|стрельцы|стрельцов|sagittarius)\b", re.IGNORECASE),
    "capricorn": re.compile(r"\b(?:козерог|козерога|козерогу|козерогом|козероге|козероги|козерогов|capricorn)\b", re.IGNORECASE),
    "aquarius": re.compile(r"\b(?:водолей|водолея|водолею|водолеем|водолее|водолеи|водолеев|aquarius)\b", re.IGNORECASE),
    "pisces": re.compile(r"\b(?:рыбы|рыбы|рыбе|рыбу|рыбой|рыб|рыбам|рыбами|рыбах|pisces)\b", re.IGNORECASE),
}

_ZODIAC_RU_NAMES = {
    "aries": "Овен ♈",
    "taurus": "Телец ♉",
    "gemini": "Близнецы ♊",
    "cancer": "Рак ♋",
    "leo": "Лев ♌",
    "virgo": "Дева ♍",
    "libra": "Весы ♎",
    "scorpio": "Скорпион ♏",
    "sagittarius": "Стрелец ♐",
    "capricorn": "Козерог ♑",
    "aquarius": "Водолей ♒",
    "pisces": "Рыбы ♓",
}

_DATE_SEGODNYA_RE = re.compile(r"\b(?:сегодня|today|сейчас)\b", re.IGNORECASE)
_DATE_ZAVTRA_RE = re.compile(r"\b(?:завтра|tomorrow)\b", re.IGNORECASE)
_DATE_POSLEZAVTRA_RE = re.compile(r"\b(?:послезавтра)\b", re.IGNORECASE)
_DATE_VCHERA_RE = re.compile(r"\b(?:вчера|yesterday)\b", re.IGNORECASE)

# Colloquial/implicit weather queries — ONLY used when message is ≤12 words.
# City extraction in _handle_weather acts as a second guard (no city → None → LLM).
_WEATHER_COLLOQUIAL_RE = re.compile(
    r"(?:жарко|холодн[оа]?|тепло|морозн[оа]?|дождь|дождит|осадк[иа]|снегопад"
    r"|облачно|ясно|солнечн|туман"
    r"|how\s+(?:warm|hot|cold)\s|is\s+it\s+(?:raining|snowing|sunny|cloudy)"
    r"|будет\s+(?:ли\s+)?дождь|когда\s+(?:будет\s+)?дождь)",
    re.IGNORECASE,
)
_WEATHER_COLLOQUIAL_MAX_WORDS = 12

_CURRENCY_PATTERNS = re.compile(
    r"(?:курс|exchange\s*rate|convert)"
    r"|(?:(?:доллар|евро|рубл|биткоин|bitcoin|ethereum|btc|eth|sol|ton|usd|eur|rub|gbp|jpy|cny)\S*\s+(?:к|в|to|in)\s+)"
    r"|(?:сколько\s+(?:стоит\s+)?(?:доллар|евро|рубл|биткоин|bitcoin|btc|usd|eur|rub))",
    re.IGNORECASE,
)

# Colloquial/conversational currency queries — gated by ≤12 words.
# Exchange verbs REQUIRE a currency noun immediately after to avoid
# false positives like "поменяй язык", "обменяй файлы".
# _extract_currency_pair acts as second guard: no pair found → None → LLM.
_CURRENCY_NOUNS = r"(?:доллар|евро|рубл|тенге|биткоин|bitcoin|btc|eth|sol|ton|usd|eur|rub|gbp|jpy|cny|валют)"
_CURRENCY_COLLOQUIAL_RE = re.compile(
    # Exchange verbs + mandatory currency noun
    r"(?:(?:конвертир(?:уй|овать)?|обменя[йт]|поменя[йт])\s+(?:\d+\s+)?" + _CURRENCY_NOUNS + r")"
    # "перевод валют" — already specific
    r"|\bперевод\s+валют"
    # Conversational "как там доллар?", "что с биткоином?"
    r"|\bкак\s+(?:там\s+)?(?:доллар|евро|рубл|биткоин)"
    r"|\bчто\s+(?:там\s+)?(?:с\s+)?(?:доллар|евро|биткоин|рубл)",
    re.IGNORECASE,
)
_CURRENCY_COLLOQUIAL_MAX_WORDS = 12

# Crypto tickers / aliases — if any of these appear, route to CoinGecko
_CRYPTO_ALIASES: dict[str, str] = {
    "биткоин": "bitcoin",
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "эфир": "ethereum",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "солана": "solana",
    "solana": "solana",
    "sol": "solana",
    "тон": "the-open-network",
    "ton": "the-open-network",
}

# Performance: human-readable names for CoinGecko IDs — hoisted to module level
# so the dict is never re-constructed on every _handle_crypto() call.
_COIN_NAMES: dict[str, str] = {
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "solana": "Solana",
    "the-open-network": "TON",
}

# CoinGecko vs_currencies we support
_COINGECKO_FIAT_MAP: dict[str, str] = {
    "USD": "usd",
    "RUB": "rub",
    "EUR": "eur",
    "GBP": "gbp",
    "KZT": "kzt",
}

_CURRENCY_CODES = {
    # Russian aliases → ISO 4217
    "доллар": "USD",
    "доллара": "USD",
    "доллару": "USD",
    "долларом": "USD",
    "долларе": "USD",
    "доллары": "USD",
    "долларов": "USD",
    "долларах": "USD",
    "баксов": "USD",
    "евро": "EUR",
    "рубль": "RUB",
    "рубля": "RUB",
    "рублю": "RUB",
    "рублём": "RUB",
    "рублем": "RUB",
    "рубле": "RUB",
    "рубли": "RUB",
    "рублей": "RUB",
    "рублях": "RUB",
    "фунт": "GBP",
    "фунта": "GBP",
    "фунту": "GBP",
    "фунтом": "GBP",
    "фунте": "GBP",
    "фунты": "GBP",
    "фунтов": "GBP",
    "юань": "CNY",
    "юаня": "CNY",
    "юаню": "CNY",
    "юанем": "CNY",
    "юани": "CNY",
    "юаней": "CNY",
    "йена": "JPY",
    "йены": "JPY",
    "йене": "JPY",
    "йену": "JPY",
    "иена": "JPY",
    "иены": "JPY",
    "иене": "JPY",
    "иену": "JPY",
    "йен": "JPY",
    "тенге": "KZT",
    "гривна": "UAH",
    "гривны": "UAH",
    "гривне": "UAH",
    "гривну": "UAH",
    "гривной": "UAH",
    "гривен": "UAH",
    "сом": "KGS",
    "сомов": "KGS",
    "сум": "UZS",
    "сумов": "UZS",
    # ISO codes (pass-through)
    "usd": "USD",
    "eur": "EUR",
    "rub": "RUB",
    "gbp": "GBP",
    "jpy": "JPY",
    "cny": "CNY",
    "kzt": "KZT",
    "uah": "UAH",
    "kgs": "KGS",
    "uzs": "UZS",
    "try": "TRY",
    "chf": "CHF",
}

# Performance: pre-sort aliases longest-first once at module load.
# _extract_currency_pair() previously called sorted(..., key=len, reverse=True)
# on every fiat query. With ~22 keys that's O(n log n) redundant work per call.
_SORTED_CURRENCY_ALIASES: list[tuple[str, str]] = sorted(
    _CURRENCY_CODES.items(), key=lambda kv: len(kv[0]), reverse=True
)
_CURRENCY_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE), code)
    for alias, code in _SORTED_CURRENCY_ALIASES
]

# Performance: pre-compiled at module level — _handle_weather is called on every
# weather intent match (per-message). Inline re.search() inside the function body
# would re-compile this pattern on every call (2–4 µs/compile + cache lookup overhead).
# Hoisting eliminates that entirely.
_WEATHER_FUTURE_RE = re.compile(
    r"(завтра|послезавтра|недел|дней|дня|выходны|tomorrow|week|days"
    r"|вечер|утр[ао]|ноч[ью]|час|hour|night|evening|morning)",
    re.IGNORECASE,
)

_CURRENCY_AMOUNT_RE = re.compile(r"(?<!\w)(\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)(?!\w)")

# Pattern to extract a city candidate from weather queries.
_CITY_EXTRACT_PATTERN = re.compile(
    # Branch A: "погода [сегодня|завтра|сейчас] [в] <город>" — preposition is optional here
    r"(?:погод[ауеыя]\s+(?:(?:сейчас|сегодня|завтра)\s+)?(?:в\s+|во\s+)?"
    r"|weather\s+(?:(?:today|tomorrow|now)\s+)?(?:in\s+|for\s+)?"
    # Branch B: bare "в/во" preposition anywhere in the sentence
    r"|(?<![а-яёa-z])в\s+|(?<![а-яёa-z])во\s+)"
    r"([А-ЯЁа-яёa-zA-Z][а-яёa-zA-Z\-]*(?:\s[А-ЯЁа-яёa-zA-Z][а-яёa-zA-Z\-]*)?)(?=[,?!.\s]|$)",
    re.IGNORECASE,
)

_TRAILING_TEMPORAL_WORDS = frozenset(
    {
        "сейчас",
        "сегодня",
        "завтра",
        "утром",
        "вечером",
        "ночью",
        "today",
        "tomorrow",
        "now",
        "tonight",
        "morning",
        "evening",
        "пожалуйста",
        "please",
    }
)

# Performance: hoisted from _is_complex_query() body — eliminates a list allocation
# on every call (fired for every message that enters intent routing).
# frozenset membership test is O(1) vs list's O(n); the allocation savings alone
# are the primary win since any() short-circuits on the first match anyway.
_COMPLEX_PHRASES: frozenset[str] = frozenset(
    {
        "как одеться",
        "посоветуй",
        "что делать",
        "расскажи",
        "помоги",
        "подскажи",
        "как думаешь",
        "стоит ли",
    }
)


def _clean_candidate(raw: str) -> str:
    """Strip known trailing temporal/query words from a city candidate."""
    words = raw.strip().split()
    while words and words[-1].lower() in _TRAILING_TEMPORAL_WORDS:
        words.pop()
    return " ".join(words)


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

    __slots__ = ("text", "handled", "context_data")

    def __init__(self, text: str, handled: bool = True, context_data: str | None = None):
        self.text = text
        self.handled = handled
        self.context_data = context_data


def _is_complex_query(text: str) -> bool:
    """Determine if a query is complex and should NOT short-circuit to API cards."""
    # 1. Length heuristic (long text or article)
    if len(text.split()) > 20:
        return True

    # 2. Multiple sentences/questions
    sentence_enders = sum(text.count(c) for c in "?!.")
    if sentence_enders > 1:
        return True

    # 3. Conversational / advisory keywords — checked against module-level frozenset
    # to avoid per-call list allocation and enable O(1) per-phrase hash lookup.
    lower_text = text.lower()
    return any(phrase in lower_text for phrase in _COMPLEX_PHRASES)


async def try_direct_intent(message_text: str) -> IntentResult | None:
    """Attempt to handle the message via direct API calls.

    Returns IntentResult with the formatted response text if a known intent
    is detected and successfully resolved. Returns None if the message
    doesn't match any known intent or the API call failed.
    """
    text = message_text.strip()
    word_count = len(text.split())
    is_complex = _is_complex_query(text)

    def _prepare_result(res: IntentResult | None) -> IntentResult | None:
        if res and is_complex:
            res.handled = False
            res.context_data = res.text
        return res

    # Try weather first (more common)
    if _WEATHER_PATTERNS.search(text):
        result = await _handle_weather(text)
        if result:
            return _prepare_result(result)

    # Colloquial weather — only for short messages to avoid false positives
    # (e.g. "жарко спорить о политике" has 5 words but no city → falls through safely)
    if word_count <= _WEATHER_COLLOQUIAL_MAX_WORDS and _WEATHER_COLLOQUIAL_RE.search(text):
        result = await _handle_weather(text)
        if result:
            return _prepare_result(result)

    # Then currency / crypto
    if _CURRENCY_PATTERNS.search(text):
        result = await _handle_currency(text)
        if result:
            return _prepare_result(result)

    # Colloquial currency — only for short messages; currency-pair extraction is the inner guard
    if word_count <= _CURRENCY_COLLOQUIAL_MAX_WORDS and _CURRENCY_COLLOQUIAL_RE.search(text):
        result = await _handle_currency(text)
        if result:
            return _prepare_result(result)

    # Horoscope
    if _HOROSCOPE_PATTERNS.search(text):
        result = await _handle_horoscope(text)
        if result:
            return _prepare_result(result)

    return None


# ── Weather ───────────────────────────────────────────────────────────────────


async def _handle_weather(text: str) -> IntentResult | None:
    """Extract city and fetch current weather.

    Primary:  WeatherAPI.com (1 request, autogeocode, Russian conditions text)
    Fallback: Open-Meteo (2 requests: geocode + forecast)
    """
    # Bail out for future/multi-day forecasts — LLM with Grounding handles these better.
    # PERF: uses module-level _WEATHER_FUTURE_RE (pre-compiled once at import) —
    # avoids re-compiling on every weather intent call.
    if _WEATHER_FUTURE_RE.search(text):
        return None

    # Extract city candidate
    m = _CITY_EXTRACT_PATTERN.search(text)
    city_candidate = _clean_candidate(m.group(1)) if m else ""

    if not city_candidate or len(city_candidate) < 2:
        return None  # Can't determine city → fall back to LLM

    from app.repos.provider_keys import get_provider_key

    # ── Primary: WeatherAPI.com ───────────────────────────────────────────────
    weather_key = await get_provider_key("weather")
    if weather_key:
        result = await _fetch_weatherapi(weather_key, city_candidate)
        if result:
            return result
        logging.info("WeatherAPI.com failed; trying Open-Meteo fallback")

    # ── Fallback: Open-Meteo (original implementation) ───────────────────────
    return await _fetch_open_meteo(city_candidate)


async def _fetch_weatherapi(api_key: str, city: str) -> IntentResult | None:
    """Fetch weather from WeatherAPI.com — single request, includes geocoding."""
    try:
        resp = await _get_http().get(
            "https://api.weatherapi.com/v1/current.json",
            params={"key": api_key, "q": city, "lang": "ru", "aqi": "no"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("WeatherAPI.com failed (%s)", type(exc).__name__)
        return None

    try:
        loc = data["location"]
        cur = data["current"]
        condition_text = cur["condition"]["text"]
        temp = cur["temp_c"]
        feels = cur["feelslike_c"]
        humidity = cur["humidity"]
        wind = cur["wind_kph"]
        # Pick a single summary emoji from the condition icon code
        is_day = cur.get("is_day", 1)
        code = cur["condition"]["code"]
        emoji = _weatherapi_code_to_emoji(code, is_day)
        city_display = loc.get("name", city)
        country = loc.get("country", "")
        location_str = f"{city_display}, {country}" if country else city_display

        response = (
            f"{emoji} **Погода в {location_str}**\n\n"
            f"🌡 {condition_text}: **{temp}°C** (ощущается {feels}°C)\n"
            f"💧 Влажность: **{humidity}%**\n"
            f"💨 Ветер: **{wind} км/ч**\n\n"
            f"_Данные: WeatherAPI.com_"
        )
        return IntentResult(response)
    except (KeyError, TypeError) as exc:
        logging.warning("WeatherAPI.com returned an unexpected response structure (%s)", type(exc).__name__)
        return None


def _weatherapi_code_to_emoji(code: int, is_day: int) -> str:
    """Map WeatherAPI condition code to a single emoji.

    Codes: https://www.weatherapi.com/docs/weather_conditions.json
    """
    if code == 1000:
        return "☀️" if is_day else "🌙"
    if code in (1003, 1006, 1009):
        return "⛅" if is_day else "🌥"
    if code in (1030, 1135, 1147):
        return "🌫"
    if code in (1063, 1180, 1183, 1186, 1189, 1192, 1195, 1240, 1243, 1246):
        return "🌧"
    if code in (1066, 1114, 1117, 1210, 1213, 1216, 1219, 1222, 1225, 1255, 1258):
        return "❄️"
    if code in (1072, 1150, 1153, 1168, 1171):
        return "🌧"
    if code in (1087, 1273, 1276, 1279, 1282):
        return "⛈"
    if code in (1198, 1201, 1204, 1207, 1237, 1249, 1252, 1261, 1264):
        return "🌨"
    return "🌤"


async def _fetch_open_meteo(city_candidate: str) -> IntentResult | None:
    """Fallback: Open-Meteo — 2 HTTP requests (geocode + forecast)."""
    geo = await _geocode_city_open_meteo(city_candidate)
    if not geo:
        return None
    city_name, lat, lon = geo

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
    except Exception as exc:
        logging.warning("Open-Meteo API failed (%s)", type(exc).__name__)
        return None

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
        f"_Данные: Open-Meteo_"
    )
    return IntentResult(response)


async def _geocode_city_open_meteo(candidate: str) -> tuple[str, float, float] | None:
    """Look up city via Open-Meteo Geocoding API."""
    if len(candidate) < 3:
        return None
    try:
        resp = await _get_http().get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": candidate, "count": 1, "language": "ru", "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("Open-Meteo Geocoding failed (%s)", type(exc).__name__)
        return None

    results = data.get("results")
    if not results:
        return None

    hit = results[0]
    lat: float = hit["latitude"]
    lon: float = hit["longitude"]
    display: str = hit.get("name") or candidate.capitalize()
    return display, lat, lon


def _wmo_to_emoji(code: int) -> str:
    """Convert WMO weather code to emoji description (Open-Meteo fallback)."""
    if code == 0:
        return "☀️ Ясно"
    if code in (1, 2, 3):
        return "🌤 Переменная облачность"
    if code in (45, 48):
        return "🌫 Туман"
    if code in (51, 53, 55, 56, 57):
        return "🌧 Морось"
    if code in (61, 63, 65, 66, 67):
        return "🌧 Дождь"
    if code in (71, 73, 75, 77):
        return "🌨 Снег"
    if code in (80, 81, 82):
        return "🌦 Ливень"
    if code in (85, 86):
        return "🌨 Сильный снегопад"
    if code in (95, 96, 99):
        return "⛈ Гроза"
    return "🌥 Облачно"


# ── Currency & Crypto ─────────────────────────────────────────────────────────


async def _handle_currency(text: str) -> IntentResult | None:
    """Route to CoinGecko (crypto) or ExchangeRate-API/Frankfurter (fiat)."""
    lower = text.lower()

    # ── 1. Detect crypto intent first ────────────────────────────────────────
    crypto_id: str | None = None
    for alias, cg_id in _CRYPTO_ALIASES.items():
        if alias in lower:
            crypto_id = cg_id
            break

    if crypto_id:
        return await _handle_crypto(crypto_id, text)

    # ── 2. Fiat path ──────────────────────────────────────────────────────────
    return await _handle_fiat_currency(text)


async def _handle_crypto(coingecko_id: str, text: str) -> IntentResult | None:
    """Fetch crypto price (USD + RUB) from CoinGecko Demo API (keyless, 30rpm)."""
    # Determine which fiat to pair with (default USD + RUB if Russian text)
    lower = text.lower()
    vs_currencies = "usd,rub" if re.search(r"рубл|rub", lower, re.IGNORECASE) else "usd,rub"  # always show both

    try:
        resp = await _get_http().get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": coingecko_id,
                "vs_currencies": vs_currencies,
                "include_24hr_change": "true",
            },
            headers={"Accept": "application/json", "User-Agent": "GemaibotV2/2.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("CoinGecko API failed for %s (%s)", coingecko_id, type(exc).__name__)
        return None  # Fall back to LLM

    coin_data = data.get(coingecko_id, {})
    if not coin_data:
        return None

    usd_price: float = coin_data.get("usd", 0)
    rub_price: float = coin_data.get("rub", 0)
    change_24h: float = coin_data.get("usd_24h_change", 0)

    # Performance: _COIN_NAMES is a module-level constant — no dict allocation here.
    coin_name = _COIN_NAMES.get(coingecko_id, coingecko_id.title())

    change_sign = "+" if change_24h >= 0 else ""
    change_str = f"{change_sign}{change_24h:.1f}%"

    usd_fmt = f"{usd_price:,.0f}" if usd_price >= 100 else f"{usd_price:.2f}"
    rub_fmt = f"{rub_price:,.0f}" if rub_price >= 1 else f"{rub_price:.4f}"

    response = (
        f"₿ **{coin_name}** ({change_str} за 24ч)\n\n"
        f"💵 {usd_fmt} USD\n"
        f"🇷🇺 {rub_fmt} ₽\n\n"
        f"_Данные: CoinGecko (реальное время)_"
    )
    return IntentResult(response)


async def _handle_fiat_currency(text: str) -> IntentResult | None:
    """Extract currency pair and fetch rate.

    Primary:  ExchangeRate-API (supports RUB, KZT, UAH, all major fiat)
    Fallback: Frankfurter (ECB data, no RUB)
    """
    base, target = _extract_currency_pair(text)
    if not base or not target:
        return None
    amount = _extract_currency_amount(text)

    from app.repos.provider_keys import get_provider_key

    # ── Primary: ExchangeRate-API ─────────────────────────────────────────────
    exchange_key = await get_provider_key("exchange")
    if exchange_key:
        result = await _fetch_exchangerate_api(exchange_key, base, target, amount)
        if result:
            return result
        logging.info("ExchangeRate-API failed for %s→%s, trying Frankfurter", base, target)

    # ── Fallback: Frankfurter (no RUB) ────────────────────────────────────────
    unsupported = {"RUB", "KZT", "UAH", "KGS", "UZS"}
    if base in unsupported or target in unsupported:
        return None  # Frankfurter doesn't support these — let LLM handle via Grounding

    return await _fetch_frankfurter(base, target, amount)


async def _fetch_exchangerate_api(
    api_key: str,
    base: str,
    target: str,
    amount: float = 1.0,
) -> IntentResult | None:
    """Fetch rate from ExchangeRate-API v6 (free tier: 1,500 req/month)."""
    try:
        resp = await _get_http().get(
            f"https://v6.exchangerate-api.com/v6/{api_key}/pair/{base}/{target}",
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("ExchangeRate-API failed for %s→%s (%s)", base, target, type(exc).__name__)
        return None

    if data.get("result") != "success":
        logging.warning("ExchangeRate-API error: %s", data.get("error-type"))
        return None

    rate: float = data.get("conversion_rate", 0)
    if not rate:
        return None

    update_time = data.get("time_last_update_utc", "")
    rate_fmt = f"{rate:.2f}" if rate >= 1 else f"{rate:.4f}"
    conversion_line = ""
    if amount != 1.0:
        converted = amount * rate
        conversion_line = f"{_format_currency_number(amount)} {base} = **{_format_currency_number(converted)} {target}**\n\n"
    response = (
        f"💱 **Курс {base} → {target}**\n\n"
        f"1 {base} = **{rate_fmt} {target}**\n\n"
        f"{conversion_line}"
        f"_Данные: ExchangeRate-API ({update_time[:16] if update_time else 'сейчас'})_"
    )
    return IntentResult(response)


async def _fetch_frankfurter(base: str, target: str, amount: float = 1.0) -> IntentResult | None:
    """Fetch rate from Frankfurter (ECB data, fallback for EU pairs)."""
    try:
        resp = await _get_http().get(
            "https://api.frankfurter.dev/v1/latest",
            params={"from": base, "to": target},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logging.warning("Frankfurter API failed for %s→%s (%s)", base, target, type(exc).__name__)
        return None

    rates = data.get("rates", {})
    rate = rates.get(target)
    if rate is None:
        return None

    date = data.get("date", "")
    rate_fmt = f"{rate:.4f}" if rate < 10 else f"{rate:.2f}"
    conversion_line = ""
    if amount != 1.0:
        converted = amount * rate
        conversion_line = f"{_format_currency_number(amount)} {base} = **{_format_currency_number(converted)} {target}**\n\n"
    response = (
        f"💱 **Курс {base} → {target}**\n\n1 {base} = **{rate_fmt} {target}**\n\n"
        f"{conversion_line}_Данные: Европейский ЦБ ({date})_"
    )
    return IntentResult(response)


def _extract_currency_amount(text: str) -> float:
    """Return the first positive numeric amount, defaulting to one unit."""
    match = _CURRENCY_AMOUNT_RE.search(text)
    if match is None:
        return 1.0
    try:
        value = float(match.group(1).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return 1.0
    return value if 0 < value <= 1_000_000_000_000 else 1.0


def _format_currency_number(value: float) -> str:
    rendered = f"{value:,.2f}"
    return rendered.rstrip("0").rstrip(".")


def _extract_currency_pair(text: str) -> tuple[str | None, str | None]:
    """Extract base and target currency codes from text."""
    lower = text.lower()
    first_position_by_code: dict[str, int] = {}

    # Alias length prevents overlapping aliases, but conversion direction must
    # follow the sentence rather than the alias table's iteration order.
    for pattern, code in _CURRENCY_ALIAS_PATTERNS:
        match = pattern.search(lower)
        if match is not None:
            previous = first_position_by_code.get(code)
            if previous is None or match.start() < previous:
                first_position_by_code[code] = match.start()

    found = [code for code, _position in sorted(first_position_by_code.items(), key=lambda item: item[1])]

    if len(found) >= 2:
        return found[0], found[1]
    if len(found) == 1:
        # Single currency mentioned — default to USD base or RUB target
        single = found[0]
        if single == "USD" or single == "RUB":
            return "USD", "RUB"
        return single, "RUB"

    return None, None


def _missing_horoscope_sign_guide() -> str:
    return (
        "🔮 **Персональный Гороскоп**\n\n"
        "Пожалуйста, укажите знак зодиака в запросе. Так я смогу собрать прогноз под нужный знак и период.\n\n"
        "**Примеры:**\n"
        "• _гороскоп овен на сегодня_\n"
        "• _что ждет близнецов завтра?_\n"
        "• _гороскоп лев на послезавтра_\n\n"
        "**Доступные знаки:**\n"
        "Овен ♈, Телец ♉, Близнецы ♊, Рак ♋, Лев ♌, Дева ♍, "
        "Весы ♎, Скорпион ♏, Стрелец ♐, Козерог ♑, Водолей ♒, Рыбы ♓.\n\n"
        "Для ежедневной доставки в удобное время откройте /horoscope_settings."
    )


def _build_horoscope_system_instruction(
    *,
    user_text: str,
    signs_str: str,
    day_ru: str,
    astro_context: str,
) -> str:
    return (
        "<role>\n"
        "Ты пишешь короткий ежедневный гороскоп на русском языке: ясный, теплый, практичный.\n"
        "Опирайся на астрологическую традицию и предоставленные транзиты, но не подавай прогноз как доказанный факт.\n"
        "</role>\n\n"
        "<task>\n"
        f"Запрос пользователя: {user_text}\n"
        f"Знаки: {signs_str}\n"
        f"Период: {day_ru}\n"
        "Если знаков несколько, сделай акцент на совместимости и динамике между ними.\n"
        "Если в запросе есть тема любви, работы, денег, здоровья или решений, сделай ее главным фокусом.\n"
        "</task>\n\n"
        "<context>\n"
        "Текущие астрономические данные для символической интерпретации:\n"
        f"{astro_context}\n"
        "</context>\n\n"
        "<constraints>\n"
        "- Пиши без фатализма, запугивания и обещаний гарантированного исхода.\n"
        "- Не давай медицинских, юридических или финансовых указаний; вместо этого предлагай мягкие наблюдения и бытовые шаги.\n"
        "- Используй транзиты как контекст и обоснование настроения дня, а не как абсолютную причинность.\n"
        "- Не упоминай модель, провайдера, промпт, API или внутреннюю механику.\n"
        "- Не добавляй отдельный заголовок: заголовок добавит приложение.\n"
        "- Длина: 3-5 коротких смысловых блоков, без длинного полотна.\n"
        "</constraints>\n\n"
        "<output_format>\n"
        "1. **Главный фон** — 1-2 предложения про тон периода.\n"
        "2. **Фокус дня** — что лучше выбрать или отложить.\n"
        "3. **Отношения / дела / ресурс** — короткие практичные подсказки по 2-3 сферам.\n"
        "4. **Мягкий совет** — одно действие на день без давления.\n"
        "</output_format>"
    )


def _format_horoscope_response(*, sign_displays: list[str], day_ru: str, body_text: str) -> str:
    if len(sign_displays) > 1:
        signs_str = " и ".join(sign_displays) if len(sign_displays) == 2 else ", ".join(sign_displays)
        title = f"🔮 **Гороскоп: совместимость {signs_str} ({day_ru})**"
    else:
        title = f"🔮 **Гороскоп: {sign_displays[0]} ({day_ru})**"
    return (
        f"{title}\n\n"
        f"{body_text.strip()}\n\n"
        "_Астро-данные: текущие транзиты; интерпретация носит ориентировочный характер._"
    )


async def _handle_horoscope(text: str) -> IntentResult | None:
    """Detect zodiac sign and target day, then generate an astrology-informed response."""
    lower_text = text.lower()

    # 1. Detect zodiac signs
    detected_signs = []
    for sign, pattern in _ZODIAC_MAPPING.items():
        if pattern.search(lower_text):
            detected_signs.append(sign)

    # 2. Parse target day
    if re.search(r"следующ.*(три|3)\s*дн", lower_text):
        day_ru = "на следующие три дня"
    elif _DATE_POSLEZAVTRA_RE.search(lower_text):
        day_ru = "послезавтра"
    elif _DATE_ZAVTRA_RE.search(lower_text):
        day_ru = "завтра"
    elif _DATE_VCHERA_RE.search(lower_text):
        day_ru = "вчера"
    else:
        day_ru = "сегодня"

    logging.info("Horoscope intent: signs=%s, day_ru=%s", detected_signs, day_ru)

    if not detected_signs:
        return IntentResult(_missing_horoscope_sign_guide())

    sign_displays = [_ZODIAC_RU_NAMES[s] for s in detected_signs]
    signs_str = " и ".join(sign_displays) if len(sign_displays) == 2 else ", ".join(sign_displays)

    from datetime import UTC, datetime, timedelta

    from app.astro import get_astro_context
    
    # Calculate target date for ephemeris based on day_ru
    dt = datetime.now(UTC)
    if day_ru == "завтра":
        dt += timedelta(days=1)
    elif day_ru == "послезавтра":
        dt += timedelta(days=2)
    elif day_ru == "вчера":
        dt -= timedelta(days=1)
    elif day_ru == "на следующие три дня":
        # Let's base the transit context on tomorrow as the midpoint
        dt += timedelta(days=1)
        
    astro_context = get_astro_context(dt)

    logging.info("Generating horoscope using local astrological data")
    system_instruction = _build_horoscope_system_instruction(
        user_text=text,
        signs_str=signs_str,
        day_ru=day_ru,
        astro_context=astro_context,
    )
    prompt = f"Запрос пользователя: {text}"
    try:
        from app.providers import get_provider_router
        from app.providers.request_factory import generation_request_from_history
        from app.providers.stream_types import StreamCompleted, TextDelta, Workload

        request = await generation_request_from_history(
            models=("gemini-3.5-flash",),
            history=[{"role": "user", "parts": [prompt]}],
            system_instruction=system_instruction,
            thinking_level="off",
            workload=Workload.INTERACTIVE,
            allow_deferred=False,
        )
        chunks: list[str] = []
        terminal = None
        async for event in get_provider_router().stream(request):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
            else:
                terminal = event
        result_text = "".join(chunks).strip()
        if result_text and isinstance(terminal, StreamCompleted):
            logging.info("Gemini direct generation completed successfully")
            return IntentResult(_format_horoscope_response(
                sign_displays=sign_displays,
                day_ru=day_ru,
                body_text=result_text,
            ))
    except Exception as exc:
        logging.error("Failed to generate fallback horoscope: %s", exc)
        return None

    return None
