from datetime import datetime
import pytz

from .. import config

def get_pacific_date() -> str:
    """Возвращает текущую дату по тихоокеанскому времени в формате YYYY-MM-DD."""
    return datetime.now(config.PACIFIC_TZ).strftime('%Y-%m-%d')

def get_current_month_str() -> str:
    """Возвращает текущий месяц в формате YYYY-MM."""
    return datetime.now(pytz.utc).strftime('%Y-%m')

def get_kyiv_reset_time() -> str:
    """Рассчитывает и возвращает время сброса лимитов по киевскому времени."""
    now_pt = datetime.now(config.PACIFIC_TZ)
    tomorrow_pt = now_pt.date() + timedelta(days=1)
    reset_time_pt = config.PACIFIC_TZ.localize(datetime.combine(tomorrow_pt, datetime.min.time()))
    reset_time_kyiv = reset_time_pt.astimezone(config.KYIV_TZ)
    return reset_time_kyiv.strftime('%H:%M %d.%m.%Y')
