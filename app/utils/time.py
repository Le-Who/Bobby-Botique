from datetime import datetime, timedelta, date
from app.config import PACIFIC_TZ, KYIV_TZ, UTC_TZ


# Safe timezone imports to prevent circular imports
def get_pacific_tz():
    """Safely get Pacific timezone."""
    return PACIFIC_TZ


def get_kyiv_tz():
    """Safely get Kyiv timezone."""
    return KYIV_TZ


def get_pacific_date() -> date:
    """Возвращает текущую дату по тихоокеанскому времени как объект date."""
    return datetime.now(get_pacific_tz()).date()


def get_current_month_str() -> str:
    """Возвращает текущий месяц в формате YYYY-MM."""
    return datetime.now(UTC_TZ).strftime("%Y-%m")


def get_kyiv_reset_time() -> str:
    """Рассчитывает и возвращает время сброса лимитов по киевскому времени."""
    pacific_tz = get_pacific_tz()
    kyiv_tz = get_kyiv_tz()

    now_pt = datetime.now(pacific_tz)
    tomorrow_pt = now_pt.date() + timedelta(days=1)
    reset_time_pt = pacific_tz.localize(
        datetime.combine(tomorrow_pt, datetime.min.time())
    )
    reset_time_kyiv = reset_time_pt.astimezone(kyiv_tz)
    return reset_time_kyiv.strftime("%H:%M %d.%m.%Y")
