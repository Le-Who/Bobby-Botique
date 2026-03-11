import datetime
import logging
import random
import time

from app import database as db

logger = logging.getLogger(__name__)

# Simple per-user TTL cache so we don't hit the DB on every status update.
# Format: {user_id: (timestamp, stat_string)}
_stat_cache: dict[int, tuple[float, str]] = {}
_STAT_CACHE_TTL = 120  # seconds — stats survive one full research session

# Static fun facts about technology, science, and AI
FUN_FACTS = [
    "Первый электронный компьютер ENIAC весил более 27 тонн и занимал 167 квадратных метров.",
    "Слово «робот» придумал чешский писатель Карел Чапек в 1920 году.",
    "Ежедневно в мире отправляется более 300 миллиардов электронных писем.",
    "Первый компьютерный баг был найден в 1947 году — это был настоящий мотылек внутри реле.",
    "Около 90% всех данных в мире было создано за последние два года.",
    "Символ @ был выбран для e-mail в 1971 году Рэем Томлинсоном почти случайно.",
    "Среднестатистический пользователь проверяет свой смартфон около 150 раз в день.",
    "Google обрабатывает более 8 миллиардов поисковых запросов ежедневно.",
    "Алан Тьюринг считается отцом теоретической информатики и искусственного интеллекта.",
    "Первая в мире веб-страница была запущена 6 августа 1991 года.",
    "У каждого окенического осьминога три сердца и голубая кровь.",
    "ДНК человека на 50% совпадает с ДНК банана.",
    "Мед — единственный продукт, который никогда не портится.",
    "На Юпитере и Сатурне идут алмазные дожди.",
    "Белые медведи на самом деле не белые. Их шерсть прозрачна, а кожа черная.",
    "Коала может спать до 22 часов в сутки.",
    "Акулы появились на Земле раньше, чем деревья.",
    "Один карандаш может написать линию длиной около 56 километров.",
    "Нейронные сети искусственного интеллекта вдохновлены структурой мозга человека.",
    "Самый длинный из когда-либо зарегистрированных полетов курицы длился 13 секунд.",
    "Муравьи никогда не спят.",
    "Слоны — единственные млекопитающие, которые не умеют прыгать.",
    "Сердце синего кита размером с автомобиль.",
    "Если сложить все кровеносные сосуды человека, они дважды обогнут Землю.",
    "Вода может одновременно находиться в трех состояниях: твердом, жидком и газообразном. Это называется тройной точкой.",
    "Бананы радиоактивны из-за высокого содержания в них калия.",
]


async def get_personalized_stat(user_id: int) -> str | None:
    """
    Generate a personalized stat msg for the user based on their usage history.
    Results are cached per-user for _STAT_CACHE_TTL seconds to avoid
    hitting the DB on every status callback during an agentic loop.
    """
    # Check cache first
    cached = _stat_cache.get(user_id)
    if cached:
        ts, stat_str = cached
        if time.monotonic() - ts < _STAT_CACHE_TTL:
            return stat_str
        del _stat_cache[user_id]

    try:
        # Get start date
        user_record = await db.db_query("SELECT created_at FROM users WHERE user_id = $1", (user_id,))
        if not user_record:
            return None

        first_seen = user_record[0].get("created_at")
        days_together = 0
        if first_seen:
            # handle timezone-aware or naive datetimes
            if first_seen.tzinfo is not None:
                days_together = (datetime.datetime.now(first_seen.tzinfo) - first_seen).days
            else:
                days_together = (datetime.datetime.now() - first_seen).days

            # Show "1 days" as 1 so we don't say 0 days for new users
            days_together = max(1, days_together)

        # Get total requests
        total_req_record = await db.db_query(
            "SELECT SUM(request_count) as total FROM user_metrics WHERE user_id = $1", (user_id,)
        )
        total_requests = total_req_record[0].get("total", 0) if total_req_record else 0

        # Get today requests
        today_str = datetime.date.today().isoformat()
        today_req_record = await db.db_query(
            "SELECT request_count FROM user_metrics WHERE user_id = $1 AND metric_date = $2", (user_id, today_str)
        )
        today_requests = today_req_record[0].get("request_count", 0) if today_req_record else 0

        # Pick a random template if we have data
        templates = []
        if total_requests > 0 and days_together > 0:
            templates.extend(
                [
                    f"📊 Мы вместе уже {days_together} дней! За это время вы отправили более {total_requests} запросов.",
                    f"🤝 С момента нашего знакомства прошло уже {days_together} дней. Спасибо, что вы со мной!",
                    f"🤖 Ваш вклад в мою базу знаний: {total_requests} запросов. Продолжаем исследовать мир!",
                ]
            )

        if today_requests > 0:
            templates.extend(
                [
                    f"🔥 Сегодня вы уже сделали {today_requests} запросов. Любопытство — невероятная сила!",
                    f"📈 Вы сегодня особенно активны: {today_requests} запросов. Мне нравится такой темп!",
                ]
            )

        if templates:
            result = random.choice(templates)
            _stat_cache[user_id] = (time.monotonic(), result)
            return result

    except Exception as e:
        logger.warning(f"Failed to generate personalized stat for user {user_id}: {e}")

    return None


async def get_waiting_message(user_id: int | None = None) -> str:
    """
    Return a random waiting message — either a fun fact or personalized stat.
    70% chance of a static fun fact, 30% chance of a personalized stat.
    """
    prefix = "💡 Знаете ли вы?"

    # Try personalized stat (30% chance) if we have a user_id
    if user_id and random.random() < 0.3:
        stat_msg = await get_personalized_stat(user_id)
        if stat_msg:
            return f"💡 Немного статистики:\n{stat_msg}"

    # Fallback to static fun fact (70% chance or if stat generation failed)
    fact = random.choice(FUN_FACTS)
    return f"{prefix} {fact}"
