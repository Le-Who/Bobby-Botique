import datetime
import logging
import random
import time

from app import database as db

logger = logging.getLogger(__name__)

_stat_cache: dict[int, tuple[float, str]] = {}
_STAT_CACHE_TTL = 120

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
    cached = _stat_cache.get(user_id)
    if cached:
        ts, stat_str = cached
        if time.monotonic() - ts < _STAT_CACHE_TTL:
            return stat_str
        _stat_cache.pop(user_id, None)

    try:
        user_record = await db.db_query(
            "SELECT MIN(metric_date) as first_seen FROM user_metrics WHERE user_id = $1",
            (user_id,),
        )
        if not user_record:
            return None

        first_seen = user_record[0].get("first_seen")
        days_together = 0
        if first_seen:
            if first_seen.tzinfo is not None:
                days_together = (datetime.datetime.now(first_seen.tzinfo) - first_seen).days
            else:
                days_together = (datetime.datetime.now() - first_seen).days

            days_together = max(1, days_together)

        total_req_record = await db.db_query(
            "SELECT SUM(request_count) as total FROM user_metrics WHERE user_id = $1",
            (user_id,),
        )
        total_requests = (total_req_record[0].get("total") or 0) if total_req_record else 0

        today = datetime.date.today()
        today_req_record = await db.db_query(
            "SELECT request_count FROM user_metrics WHERE user_id = $1 AND metric_date = $2",
            (user_id, today),
        )
        today_requests = (today_req_record[0].get("request_count") or 0) if today_req_record else 0

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

    if user_id and random.random() < 0.3:
        stat_msg = await get_personalized_stat(user_id)
        if stat_msg:
            return f"💡 Немного статистики:\n{stat_msg}"

    fact = random.choice(FUN_FACTS)
    return f"{prefix} {fact}"
