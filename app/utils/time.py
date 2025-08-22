from datetime import datetime, timedelta, date
import pytz

from app.config import PACIFIC_TZ, KYIV_TZ

def get_pacific_date() -> date:
    """Возвращает текущую дату по тихоокеанскому времени как объект date."""
    return datetime.now(PACIFIC_TZ).date()

def get_current_month_str() -> str:
    """Возвращает текущий месяц в формате YYYY-MM."""
    return datetime.now(pytz.utc).strftime('%Y-%m')

def get_kyiv_reset_time() -> str:
    """Рассчитывает и возвращает время сброса лимитов по киевскому времени."""
    now_pt = datetime.now(PACIFIC_TZ)
    tomorrow_pt = now_pt.date() + timedelta(days=1)
    reset_time_pt = PACIFIC_TZ.localize(datetime.combine(tomorrow_pt, datetime.min.time()))
    reset_time_kyiv = reset_time_pt.astimezone(KYIV_TZ)
    return reset_time_kyiv.strftime('%H:%M %d.%m.%Y')
