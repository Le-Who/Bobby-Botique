from __future__ import annotations

import html
import logging
import os
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Any, Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.natal.city_catalog import CityRecord, CountryRecord, find_city_by_id, search_cities, search_countries
from app.natal.intent import NATAL_INTENT_RE, NATAL_SLASH_ALIAS_RE
from app.natal.models import BirthInput, TimePrecision
from app.natal.parser import BirthInputParseError, parse_birth_table
from app.natal.service import create_natal_report

logger = logging.getLogger(__name__)

NATAL_MODE: Final = "NATAL_MODE"
NATAL_TABLE: Final = "NATAL_TABLE"
NATAL_DATE: Final = "NATAL_DATE"
NATAL_TIME_PRECISION: Final = "NATAL_TIME_PRECISION"
NATAL_TIME_VALUE: Final = "NATAL_TIME_VALUE"
NATAL_COUNTRY: Final = "NATAL_COUNTRY"
NATAL_PLACE: Final = "NATAL_PLACE"
NATAL_FOCUS: Final = "NATAL_FOCUS"
NATAL_CONFIRM: Final = "NATAL_CONFIRM"

_NATAL_KEYS = {
    "natal_birth_input",
    "natal_date",
    "natal_date_day",
    "natal_date_month",
    "natal_date_year",
    "natal_date_picker_view",
    "natal_date_year_page",
    "natal_time_precision",
    "natal_time_value",
    "natal_time_hour",
    "natal_time_minute",
    "natal_time_picker_view",
    "natal_time_minute_page",
    "natal_time_range_target",
    "natal_time_range_start_hour",
    "natal_time_range_start_minute",
    "natal_time_range_end_hour",
    "natal_time_range_end_minute",
    "natal_country_code",
    "natal_country",
    "natal_place",
    "natal_place_data",
    "natal_focus",
    "natal_mode",
    "natal_flow_message",
}

_NATAL_COVER_FILE_ID_KEY: Final = "natal_cover_file_id"
_NATAL_COVER_PATH: Final = Path(__file__).resolve().parents[2] / "artifacts" / "natal_cover_provided.png"
_natal_cover_file_id_cache = ""

_TIME_PRECISION_LABELS: Final[dict[str, tuple[TimePrecision, str]]] = {
    "exact": (TimePrecision.EXACT, "точное"),
    "approximate": (TimePrecision.APPROXIMATE, "примерное"),
    "range": (TimePrecision.RANGE, "диапазон"),
    "unknown": (TimePrecision.UNKNOWN, "неизвестно"),
}

_FOCUS_LABELS: Final[dict[str, str]] = {
    "general": "общий",
    "relationships": "отношения",
    "career": "карьера",
    "psychology": "психология",
    "brief": "кратко",
}

_FOCUS_RESULT_LABELS: Final[dict[str, str]] = {
    "general": "общий разбор",
    "relationships": "отношения",
    "career": "карьера",
    "psychology": "психология",
    "brief": "краткий разбор",
}

_TIME_PRECISION_RESULT_LABELS: Final[dict[TimePrecision, str]] = {
    TimePrecision.EXACT: "точное время",
    TimePrecision.APPROXIMATE: "примерное время",
    TimePrecision.RANGE: "диапазон времени",
    TimePrecision.UNKNOWN: "время неизвестно",
}

_RU_MONTHS_GENITIVE: Final[dict[int, str]] = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}

_RU_MONTHS_NOMINATIVE: Final[dict[int, str]] = {
    1: "январь",
    2: "февраль",
    3: "март",
    4: "апрель",
    5: "май",
    6: "июнь",
    7: "июль",
    8: "август",
    9: "сентябрь",
    10: "октябрь",
    11: "ноябрь",
    12: "декабрь",
}

_DATE_PICKER_YEAR_MIN: Final = 1900
_DATE_PICKER_YEAR_PAGE_SIZE: Final = 20
_DATE_PICKER_DEFAULT_YEAR_PAGE: Final = 1990

_FAST_COUNTRIES: Final[tuple[tuple[str, str], ...]] = (
    ("UA", "Украина"),
    ("RU", "Россия"),
    ("BY", "Беларусь"),
)

_FAST_CITIES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "UA": (("Киев", "Киев"), ("Львов", "Львов"), ("Харьков", "Харьков")),
    "RU": (("Москва", "Москва"), ("Санкт-Петербург", "Санкт-Петербург"), ("Новосибирск", "Новосибирск")),
    "BY": (("Минск", "Минск"), ("Гомель", "Гомель"), ("Витебск", "Витебск")),
}


async def natal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message:
        return ConversationHandler.END
    clear_natal_user_data(context.user_data)
    if not _natal_reports_enabled_for_handler():
        await update.message.reply_text("Натальные карты временно недоступны.")
        return ConversationHandler.END
    flow_message = await update.message.reply_text(
        "Натальная карта строится по дате, месту и, если известно, времени рождения.\n"
        "Если точного времени нет, я построю карту без домов и асцендента и явно отмечу ограничения.",
        reply_markup=_mode_keyboard(),
    )
    context.user_data["natal_flow_message"] = flow_message
    return NATAL_MODE


async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if not query:
        return NATAL_MODE
    await query.answer()
    mode = query.data.replace("natal_mode:", "")
    if mode == "cancel":
        clear_natal_user_data(context.user_data)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END
    context.user_data["natal_mode"] = mode
    if mode == "table":
        await _show_flow_prompt(
            update,
            context,
            "Скопируйте и заполните:\n\n"
            "Дата рождения:\n"
            "Время рождения: точное / примерное / диапазон / неизвестно\n"
            "Если точное или примерное:\n"
            "Если диапазон:\n"
            "Страна рождения:\n"
            "Место рождения:\n"
            "Фокус разбора: общий / отношения / карьера / психология / кратко"
        )
        return NATAL_TABLE
    await _show_date_picker(update, context)
    return NATAL_DATE


async def on_table_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_TABLE
    raw_text = update.message.text
    await _delete_user_message(update)
    try:
        birth_input = parse_birth_table(raw_text)
    except BirthInputParseError as exc:
        await _show_flow_prompt(update, context, f"Не удалось разобрать данные: {exc}")
        return NATAL_TABLE
    birth_input = _birth_input_with_local_city(birth_input)
    if birth_input is None:
        await _show_flow_prompt(update, context, "Город не найден в выбранной стране. Введите ближайший крупный город.")
        return NATAL_TABLE
    context.user_data["natal_birth_input"] = birth_input
    await _show_flow_prompt(update, context, _confirmation_text(birth_input), reply_markup=_confirm_keyboard())
    return NATAL_CONFIRM


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_DATE
    context.user_data["natal_date"] = update.message.text.strip()
    await _delete_user_message(update)
    await _show_flow_prompt(update, context, "Время рождения известно?", reply_markup=_time_precision_keyboard())
    return NATAL_TIME_PRECISION


async def on_date_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if not query:
        return NATAL_DATE
    data = str(query.data or "")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "done":
        selected = _selected_date_from_picker(context.user_data)
        if selected is None:
            await query.answer("Выберите день, месяц и год.")
            await _show_date_picker(update, context)
            return NATAL_DATE
        await query.answer()
        context.user_data["natal_date"] = selected.isoformat()
        await _show_flow_prompt(update, context, "Время рождения известно?", reply_markup=_time_precision_keyboard())
        return NATAL_TIME_PRECISION

    await query.answer()
    if action == "noop":
        await _show_date_picker(update, context)
        return NATAL_DATE
    if action == "view" and len(parts) >= 3:
        context.user_data["natal_date_picker_view"] = parts[2]
    elif action == "day" and len(parts) >= 3:
        context.user_data["natal_date_day"] = _safe_int(parts[2])
        context.user_data["natal_date_picker_view"] = "month"
    elif action == "month" and len(parts) >= 3:
        context.user_data["natal_date_month"] = _safe_int(parts[2])
        _drop_invalid_selected_day(context.user_data)
        context.user_data["natal_date_picker_view"] = "year"
    elif action == "year" and len(parts) >= 3:
        context.user_data["natal_date_year"] = _safe_int(parts[2])
        _drop_invalid_selected_day(context.user_data)
        context.user_data["natal_date_picker_view"] = _next_missing_date_part(context.user_data)
    elif action == "year_page" and len(parts) >= 3:
        context.user_data["natal_date_year_page"] = _safe_int(parts[2])
        context.user_data["natal_date_picker_view"] = "year"
    await _show_date_picker(update, context)
    return NATAL_DATE


async def on_time_precision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if query:
        await query.answer()
        raw = query.data.replace("natal_time_precision:", "")
        precision, _label = _TIME_PRECISION_LABELS.get(raw, (TimePrecision.UNKNOWN, "неизвестно"))
    else:
        if not update.message or not update.message.text:
            return NATAL_TIME_PRECISION
        raw = (update.message.text or "").strip().lower()
        await _delete_user_message(update)
        if raw in {"неизвестно", "unknown", "не знаю"}:
            precision = TimePrecision.UNKNOWN
        elif raw in {"точное", "exact"}:
            precision = TimePrecision.EXACT
        elif raw in {"примерное", "approx", "approximate"}:
            precision = TimePrecision.APPROXIMATE
        elif raw in {"диапазон", "range"}:
            precision = TimePrecision.RANGE
        else:
            await _show_flow_prompt(update, context, "Время рождения известно?", reply_markup=_time_precision_keyboard())
            return NATAL_TIME_PRECISION
    if precision == TimePrecision.UNKNOWN:
        context.user_data["natal_time_precision"] = TimePrecision.UNKNOWN
        await _show_flow_prompt(update, context, "Страна рождения?", reply_markup=_country_entry_keyboard())
        return NATAL_COUNTRY
    context.user_data["natal_time_precision"] = precision
    await _show_time_picker(update, context)
    return NATAL_TIME_VALUE


async def on_time_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if not query:
        return NATAL_TIME_VALUE
    data = str(query.data or "")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "done":
        time_value = _selected_time_value_from_picker(context.user_data)
        if not time_value:
            await query.answer("Выберите время.")
            await _show_time_picker(update, context)
            return NATAL_TIME_VALUE
        await query.answer()
        context.user_data["natal_time_value"] = time_value
        await _show_flow_prompt(update, context, "Страна рождения?", reply_markup=_country_entry_keyboard())
        return NATAL_COUNTRY

    await query.answer()
    if action == "noop":
        await _show_time_picker(update, context)
        return NATAL_TIME_VALUE
    if action == "view" and len(parts) >= 3:
        context.user_data["natal_time_picker_view"] = parts[2]
    elif action == "target" and len(parts) >= 3:
        context.user_data["natal_time_range_target"] = parts[2]
    elif action == "minute_page" and len(parts) >= 3:
        context.user_data["natal_time_minute_page"] = _safe_int(parts[2])
        context.user_data["natal_time_picker_view"] = "minute"
    elif action in {"hour", "minute"} and len(parts) >= 3:
        _set_time_picker_part(context.user_data, action, _safe_int(parts[2]))
        context.user_data["natal_time_picker_view"] = "minute" if action == "hour" else "hour"
    await _show_time_picker(update, context)
    return NATAL_TIME_VALUE


async def on_time_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_TIME_VALUE
    context.user_data["natal_time_value"] = update.message.text.strip()
    await _delete_user_message(update)
    await _show_flow_prompt(update, context, "Страна рождения?", reply_markup=_country_entry_keyboard())
    return NATAL_COUNTRY


async def on_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_COUNTRY
    query = update.message.text.strip()
    await _delete_user_message(update)
    matches = search_countries(query, limit=8)
    if not matches:
        await _show_flow_prompt(update, context, "Страна не найдена. Введите больше букв.", reply_markup=_input_keyboard("country"))
        return NATAL_COUNTRY
    await _show_flow_prompt(
        update,
        context,
        "Выберите страну из списка или введите больше букв для уточнения.",
        reply_markup=_country_keyboard(matches),
    )
    return NATAL_COUNTRY


async def on_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if not query:
        return NATAL_COUNTRY
    await query.answer()
    country_code = query.data.replace("natal_country:", "")
    country = search_countries(country_code, limit=1)
    country_display = country[0].display_name if country else country_code
    context.user_data["natal_country_code"] = country_code
    context.user_data["natal_country"] = country_display
    await _show_flow_prompt(
        update,
        context,
        f"Страна рождения: {country_display}\n\nГород рождения?",
        reply_markup=_place_entry_keyboard(country_code),
    )
    return NATAL_PLACE


async def on_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_PLACE
    query = update.message.text.strip()
    await _delete_user_message(update)
    country_code = context.user_data.get("natal_country_code")
    if not isinstance(country_code, str) or not country_code:
        await _show_flow_prompt(update, context, "Сначала выберите страну рождения.", reply_markup=_input_keyboard("country"))
        return NATAL_COUNTRY
    matches = search_cities(query, limit=8, country_code=country_code)
    if not matches:
        await _show_flow_prompt(
            update,
            context,
            "Город не найден. Введите больше букв или укажите ближайший крупный город.",
            reply_markup=_place_entry_keyboard(country_code),
        )
        return NATAL_PLACE
    await _show_flow_prompt(
        update,
        context,
        "Выберите город из списка или введите больше букв для уточнения.",
        reply_markup=_city_keyboard(matches),
    )
    return NATAL_PLACE


async def on_place_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
    if not query:
        return NATAL_PLACE
    await query.answer()
    geoname_id = query.data.replace("natal_place:", "")
    city = find_city_by_id(geoname_id)
    if city is None:
        await _show_flow_prompt(update, context, "Город не найден. Введите место рождения еще раз.", reply_markup=_input_keyboard("city"))
        return NATAL_PLACE
    country_code = context.user_data.get("natal_country_code")
    if not isinstance(country_code, str) or not country_code:
        await _show_flow_prompt(update, context, "Сначала выберите страну рождения.", reply_markup=_input_keyboard("country"))
        return NATAL_COUNTRY
    if city.country_code != country_code:
        await _show_flow_prompt(
            update,
            context,
            "Этот город не относится к выбранной стране. Введите город еще раз.",
            reply_markup=_input_keyboard("city"),
        )
        return NATAL_PLACE
    context.user_data["natal_place"] = city.display_name
    context.user_data["natal_place_data"] = _city_payload(city)
    await _show_flow_prompt(
        update,
        context,
        f"Место рождения: {city.display_name}\n\n"
        "Фокус разбора:",
        reply_markup=_focus_keyboard(),
    )
    return NATAL_FOCUS


async def on_place_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
    if not query:
        return NATAL_PLACE
    await query.answer()
    await _show_flow_prompt(
        update,
        context,
        "Если вашего города нет в списке, введите ближайший крупный город рядом с местом рождения. "
        "Для натальной карты важны координаты и часовой пояс.",
        reply_markup=_place_entry_keyboard(context.user_data.get("natal_country_code")),
    )
    return NATAL_PLACE


async def on_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = getattr(update, "callback_query", None)
    if query:
        await query.answer()
        focus_key = query.data.replace("natal_focus:", "")
        context.user_data["natal_focus"] = _FOCUS_LABELS.get(focus_key, "общий")
    else:
        if not update.message or not update.message.text:
            return NATAL_FOCUS
        context.user_data["natal_focus"] = update.message.text.strip() or "общий"
        await _delete_user_message(update)
    try:
        birth_input = _birth_input_from_steps(context.user_data)
    except BirthInputParseError as exc:
        await _show_flow_prompt(update, context, f"Не удалось разобрать данные: {exc}")
        if "врем" in str(exc).lower():
            return NATAL_TIME_VALUE
        return NATAL_DATE
    context.user_data["natal_birth_input"] = birth_input
    await _show_flow_prompt(update, context, _confirmation_text(birth_input), reply_markup=_confirm_keyboard())
    return NATAL_CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return NATAL_CONFIRM
    await query.answer()
    action = query.data.replace("natal_confirm:", "")
    if action == "cancel":
        clear_natal_user_data(context.user_data)
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END
    birth_input = context.user_data.get("natal_birth_input")
    if not isinstance(birth_input, BirthInput):
        await query.edit_message_text("Данные не найдены. Запустите /natal заново.")
        return ConversationHandler.END
    await _show_flow_prompt(update, context, "Считаю карту...")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    try:
        report = await create_natal_report(
            birth_input=birth_input,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            webhook_url=webhook_url,
        )
    except Exception as exc:
        await _show_flow_prompt(update, context, f"Не удалось построить карту: {exc}")
        clear_natal_user_data(context.user_data)
        return ConversationHandler.END
    await _send_natal_result_card(update, context, report, birth_input)
    await _show_flow_prompt(update, context, "Карта готова. Полный разбор отправлен карточкой ниже.")
    clear_natal_user_data(context.user_data)
    return ConversationHandler.END


async def on_input_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
    if not query:
        return NATAL_DATE
    await query.answer("Введите значение сообщением. Я удалю его после обработки.")
    target = query.data.replace("natal_input:", "")
    if target == "country":
        return NATAL_COUNTRY
    if target == "city":
        return NATAL_PLACE
    if target == "time":
        return NATAL_TIME_VALUE
    return NATAL_DATE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_natal_user_data(context.user_data)
    if update.message:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def _show_flow_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    text = _format_flow_prompt(context.user_data, text)
    query = getattr(update, "callback_query", None)
    if query:
        await query.edit_message_text(text, reply_markup=reply_markup)
        if query.message and "natal_flow_message" not in context.user_data:
            context.user_data["natal_flow_message"] = query.message
        return

    flow_message = context.user_data.get("natal_flow_message")
    if flow_message is not None and hasattr(flow_message, "edit_text"):
        await flow_message.edit_text(text, reply_markup=reply_markup)
        return

    if update.message:
        context.user_data["natal_flow_message"] = await update.message.reply_text(text, reply_markup=reply_markup)


async def _delete_user_message(update: Update) -> None:
    message = getattr(update, "message", None)
    if message is None or not hasattr(message, "delete"):
        return
    try:
        await message.delete()
    except Exception as exc:
        logger.debug("natal flow could not delete user input message: %s", exc)


async def _get_natal_cover_photo(*, force_upload: bool = False) -> str | InputFile | None:
    global _natal_cover_file_id_cache  # noqa: PLW0603
    if not force_upload:
        if not _natal_cover_file_id_cache:
            from app.repos.settings_repo import get_global_setting

            _natal_cover_file_id_cache = await get_global_setting(_NATAL_COVER_FILE_ID_KEY, "")
        if _natal_cover_file_id_cache:
            return _natal_cover_file_id_cache
    if not _NATAL_COVER_PATH.exists():
        return None
    return InputFile(_NATAL_COVER_PATH.read_bytes(), filename=_NATAL_COVER_PATH.name)


async def _remember_natal_cover_file_id(message: Any) -> None:
    global _natal_cover_file_id_cache  # noqa: PLW0603
    photos = getattr(message, "photo", None) or []
    if not photos:
        return
    file_id = getattr(photos[-1], "file_id", "")
    if not file_id or file_id == _natal_cover_file_id_cache:
        return
    from app.repos.settings_repo import set_global_setting

    _natal_cover_file_id_cache = file_id
    await set_global_setting(_NATAL_COVER_FILE_ID_KEY, file_id)


async def _send_natal_result_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    report,
    birth_input: BirthInput,
) -> None:
    caption = _result_caption(report, birth_input)
    keyboard = _result_keyboard(report)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    bot = getattr(context, "bot", None)
    cover = await _get_natal_cover_photo()

    if bot is not None and chat_id is not None and cover is not None:
        try:
            message = await bot.send_photo(
                chat_id=chat_id,
                photo=cover,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            await _remember_natal_cover_file_id(message)
            return
        except (OSError, TelegramError) as exc:
            logger.warning("natal result cover send failed chat=%s: %s", chat_id, exc)

    if bot is not None and chat_id is not None:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    query = getattr(update, "callback_query", None)
    if query and query.message:
        await query.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


def _result_caption(report, birth_input: BirthInput) -> str:
    del report
    focus = html.escape(_FOCUS_RESULT_LABELS.get(birth_input.focus, birth_input.focus or "общий разбор"))
    precision = html.escape(_TIME_PRECISION_RESULT_LABELS.get(birth_input.time_precision, birth_input.time_precision.value))
    limitation = ""
    if birth_input.time_precision == TimePrecision.UNKNOWN:
        limitation = "\nБез точного времени: дома и асцендент не трактуются как достоверные."
    elif birth_input.time_precision in {TimePrecision.APPROXIMATE, TimePrecision.RANGE}:
        limitation = "\nВремя не абсолютно точное: дома и углы отмечены с осторожностью."
    return (
        "<b>Натальная карта готова</b>\n\n"
        "Полный разбор собран на отдельной странице: сначала главные акценты, затем подробные секции и справочные расчетные позиции.\n\n"
        f"<b>Фокус:</b> {focus}\n"
        f"<b>Точность времени:</b> {precision}{html.escape(limitation)}\n\n"
        "Откройте отчет кнопкой ниже."
    )


def _result_keyboard(report) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if report.hosted_url and _is_safe_button_url(report.hosted_url):
        rows.append([InlineKeyboardButton("Открыть полный разбор", url=report.hosted_url)])
    if report.telegraph_url and _is_safe_button_url(report.telegraph_url):
        rows.append([InlineKeyboardButton("Telegraph mirror", url=report.telegraph_url)])
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def _is_safe_button_url(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith(("https://", "http://localhost", "http://127.0.0.1"))


def _format_flow_prompt(user_data: dict, text: str) -> str:
    if _is_terminal_flow_text(text):
        return text
    summary = "\n".join(_draft_lines(user_data))
    return (
        f"{text}\n\n"
        "Черновик:\n"
        f"{summary}\n\n"
        "Сообщения с датой, временем и местом я удаляю из чата после обработки."
    )


def _is_terminal_flow_text(text: str) -> bool:
    return text.startswith(("Считаю карту", "Карта готова", "Отменено.", "Данные не найдены", "Не удалось построить"))


def _draft_lines(user_data: dict) -> list[str]:
    date_text = _draft_date_text(user_data)
    precision = user_data.get("natal_time_precision")
    time_value = str(user_data.get("natal_time_value") or "")
    time_text = "—"
    if isinstance(precision, TimePrecision):
        time_text = _TIME_PRECISION_RESULT_LABELS.get(precision, precision.value)
        if time_value:
            time_text = f"{time_text}, {time_value}"
    elif precision:
        time_text = str(precision)
    country = str(user_data.get("natal_country") or user_data.get("natal_country_code") or "—")
    place = str(user_data.get("natal_place") or "—")
    focus = str(user_data.get("natal_focus") or "—")
    return [
        f"Дата: {date_text}",
        f"Время: {time_text}",
        f"Страна: {country}",
        f"Город: {place}",
        f"Фокус: {focus}",
    ]


def build_natal_chart_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("natal", natal_command),
            MessageHandler(filters.TEXT & filters.Regex(NATAL_SLASH_ALIAS_RE), natal_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(NATAL_INTENT_RE), natal_command),
        ],
        states={
            NATAL_MODE: [CallbackQueryHandler(on_mode, pattern=r"^natal_mode:")],
            NATAL_TABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_table_input)],
            NATAL_DATE: [
                CallbackQueryHandler(on_date_picker, pattern=r"^natal_date:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_date),
            ],
            NATAL_TIME_PRECISION: [
                CallbackQueryHandler(on_time_precision, pattern=r"^natal_time_precision:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_precision),
            ],
            NATAL_TIME_VALUE: [
                CallbackQueryHandler(on_time_picker, pattern=r"^natal_time:"),
                CallbackQueryHandler(on_input_hint, pattern=r"^natal_input:time$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_value),
            ],
            NATAL_COUNTRY: [
                CallbackQueryHandler(on_input_hint, pattern=r"^natal_input:country$"),
                CallbackQueryHandler(on_country_selected, pattern=r"^natal_country:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_country),
            ],
            NATAL_PLACE: [
                CallbackQueryHandler(on_input_hint, pattern=r"^natal_input:city$"),
                CallbackQueryHandler(on_place_missing, pattern=r"^natal_place_missing$"),
                CallbackQueryHandler(on_place_selected, pattern=r"^natal_place:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_place),
            ],
            NATAL_FOCUS: [
                CallbackQueryHandler(on_focus, pattern=r"^natal_focus:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_focus),
            ],
            NATAL_CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^natal_confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
        name="natal_chart",
        persistent=False,
    )


def clear_natal_user_data(user_data: dict) -> None:
    for key in list(user_data):
        if key in _NATAL_KEYS:
            user_data.pop(key, None)


def _natal_reports_enabled_for_handler() -> bool:
    from app.config import settings

    return bool(getattr(settings, "NATAL_REPORTS_ENABLED", False))


def _mode_keyboard() -> InlineKeyboardMarkup:
    webapp_url = _natal_form_webapp_url()
    input_row = (
        [InlineKeyboardButton("Заполнить на сайте", web_app=WebAppInfo(url=webapp_url))]
        if webapp_url
        else [InlineKeyboardButton("Отправить таблицей", callback_data="natal_mode:table")]
    )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Заполнить пошагово", callback_data="natal_mode:step")],
            input_row,
            [InlineKeyboardButton("Отмена", callback_data="natal_mode:cancel")],
        ]
    )


def _natal_form_webapp_url() -> str:
    base = _webapp_base_url()
    if not base.startswith("https://"):
        return ""
    return f"{base}/webapp/natal-form"


def _webapp_base_url() -> str:
    from app.config import settings

    base = getattr(settings, "WEBAPP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    return webhook_url.split("/webhook", 1)[0].rstrip("/")


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Построить", callback_data="natal_confirm:yes"),
                InlineKeyboardButton("Отмена", callback_data="natal_confirm:cancel"),
            ]
        ]
    )


def _time_precision_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Точное", callback_data="natal_time_precision:exact"),
                InlineKeyboardButton("Примерное", callback_data="natal_time_precision:approximate"),
            ],
            [
                InlineKeyboardButton("Диапазон", callback_data="natal_time_precision:range"),
                InlineKeyboardButton("Не знаю", callback_data="natal_time_precision:unknown"),
            ],
        ]
    )


async def _show_time_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_flow_prompt(
        update,
        context,
        "Время рождения: выберите час и минуты кнопками.",
        reply_markup=_time_picker_keyboard(context.user_data),
    )


def _time_picker_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    view = str(user_data.get("natal_time_picker_view") or "hour")
    if view not in {"hour", "minute"}:
        view = "hour"
    rows: list[list[InlineKeyboardButton]] = []
    precision = user_data.get("natal_time_precision")
    if precision == TimePrecision.RANGE:
        target = _time_range_target(user_data)
        rows.append(
            [
                InlineKeyboardButton(_time_target_label("start", "С", target, user_data), callback_data="natal_time:target:start"),
                InlineKeyboardButton(_time_target_label("end", "До", target, user_data), callback_data="natal_time:target:end"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(_time_view_label("hour", "Час", view), callback_data="natal_time:view:hour"),
            InlineKeyboardButton(_time_view_label("minute", "Минуты", view), callback_data="natal_time:view:minute"),
        ]
    )
    if view == "minute":
        rows.extend(_time_minute_rows(user_data))
    else:
        rows.extend(_time_hour_rows(user_data))
    time_value = _selected_time_value_from_picker(user_data)
    if time_value:
        rows.append([InlineKeyboardButton(f"Далее: {time_value}", callback_data="natal_time:done")])
    else:
        rows.append([InlineKeyboardButton("Выберите время", callback_data="natal_time:noop")])
    return InlineKeyboardMarkup(rows)


def _time_view_label(part: str, label: str, view: str) -> str:
    return f">{label}" if view == part else label


def _time_target_label(target: str, label: str, selected: str, user_data: dict) -> str:
    prefix = ">" if target == selected else ""
    value = _time_range_part_value(user_data, target) or "--:--"
    return f"{prefix}{label}: {value}"


def _time_hour_rows(user_data: dict) -> list[list[InlineKeyboardButton]]:
    selected = _selected_time_part(user_data, "hour")
    buttons = [
        InlineKeyboardButton(
            f"[{hour:02d}]" if selected == hour else f"{hour:02d}",
            callback_data=f"natal_time:hour:{hour}",
        )
        for hour in range(24)
    ]
    return _button_rows(buttons, 6)


def _time_minute_rows(user_data: dict) -> list[list[InlineKeyboardButton]]:
    page = user_data.get("natal_time_minute_page")
    start = 30 if page == 30 else 0
    end = start + 29
    selected = _selected_time_part(user_data, "minute")
    buttons = [
        InlineKeyboardButton(
            f"[{minute:02d}]" if selected == minute else f"{minute:02d}",
            callback_data=f"natal_time:minute:{minute}",
        )
        for minute in range(start, end + 1)
    ]
    rows = _button_rows(buttons, 6)
    rows.append(
        [
            InlineKeyboardButton("00-29", callback_data="natal_time:minute_page:0"),
            InlineKeyboardButton("30-59", callback_data="natal_time:minute_page:30"),
        ]
    )
    return rows


def _set_time_picker_part(user_data: dict, part: str, value: int | None) -> None:
    if value is None:
        return
    if part == "hour" and not 0 <= value <= 23:
        return
    if part == "minute" and not 0 <= value <= 59:
        return
    precision = user_data.get("natal_time_precision")
    if precision == TimePrecision.RANGE:
        target = _time_range_target(user_data)
        user_data[f"natal_time_range_{target}_{part}"] = value
        if _time_range_part_value(user_data, target) and target == "start":
            user_data["natal_time_range_target"] = "end"
        return
    user_data[f"natal_time_{part}"] = value


def _selected_time_part(user_data: dict, part: str) -> int | None:
    precision = user_data.get("natal_time_precision")
    if precision == TimePrecision.RANGE:
        value = user_data.get(f"natal_time_range_{_time_range_target(user_data)}_{part}")
    else:
        value = user_data.get(f"natal_time_{part}")
    return value if isinstance(value, int) else None


def _selected_time_value_from_picker(user_data: dict) -> str | None:
    if user_data.get("natal_time_precision") == TimePrecision.RANGE:
        start = _time_range_part_value(user_data, "start")
        end = _time_range_part_value(user_data, "end")
        if not start or not end:
            return None
        if _time_to_minutes(end) <= _time_to_minutes(start):
            return None
        return f"{start}-{end}"
    hour = user_data.get("natal_time_hour")
    minute = user_data.get("natal_time_minute")
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    return f"{hour:02d}:{minute:02d}"


def _time_range_part_value(user_data: dict, target: str) -> str | None:
    hour = user_data.get(f"natal_time_range_{target}_hour")
    minute = user_data.get(f"natal_time_range_{target}_minute")
    if not isinstance(hour, int) or not isinstance(minute, int):
        return None
    return f"{hour:02d}:{minute:02d}"


def _time_range_target(user_data: dict) -> str:
    target = str(user_data.get("natal_time_range_target") or "start")
    return target if target in {"start", "end"} else "start"


def _time_to_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _focus_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Общий", callback_data="natal_focus:general"),
                InlineKeyboardButton("Отношения", callback_data="natal_focus:relationships"),
            ],
            [
                InlineKeyboardButton("Карьера", callback_data="natal_focus:career"),
                InlineKeyboardButton("Психология", callback_data="natal_focus:psychology"),
            ],
            [InlineKeyboardButton("Кратко", callback_data="natal_focus:brief")],
        ]
    )


async def _show_date_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_flow_prompt(
        update,
        context,
        "Дата рождения: выберите день, месяц и год кнопками.",
        reply_markup=_date_picker_keyboard(context.user_data),
    )


def _date_picker_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    view = str(user_data.get("natal_date_picker_view") or _next_missing_date_part(user_data))
    if view not in {"day", "month", "year"}:
        view = "day"

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(_date_part_label("day", "День", user_data, view), callback_data="natal_date:view:day"),
            InlineKeyboardButton(_date_part_label("month", "Месяц", user_data, view), callback_data="natal_date:view:month"),
            InlineKeyboardButton(_date_part_label("year", "Год", user_data, view), callback_data="natal_date:view:year"),
        ]
    ]
    if view == "month":
        rows.extend(_date_picker_month_rows(user_data))
    elif view == "year":
        rows.extend(_date_picker_year_rows(user_data))
    else:
        rows.extend(_date_picker_day_rows(user_data))

    selected = _selected_date_from_picker(user_data)
    if selected is None:
        rows.append([InlineKeyboardButton("Выберите день, месяц и год", callback_data="natal_date:noop")])
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Далее: {_format_birth_date_for_confirmation(selected.isoformat())}",
                    callback_data="natal_date:done",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def _date_part_label(part: str, label: str, user_data: dict, view: str) -> str:
    prefix = ">" if view == part else ""
    if part == "day":
        value = user_data.get("natal_date_day")
    elif part == "month":
        month = user_data.get("natal_date_month")
        value = _RU_MONTHS_NOMINATIVE.get(month) if isinstance(month, int) else None
    else:
        value = user_data.get("natal_date_year")
    return f"{prefix}{label}: {value or '-'}"


def _date_picker_day_rows(user_data: dict) -> list[list[InlineKeyboardButton]]:
    max_day = _max_selectable_day(user_data)
    selected_day = user_data.get("natal_date_day")
    buttons = [
        InlineKeyboardButton(
            f"[{day}]" if selected_day == day else str(day),
            callback_data=f"natal_date:day:{day}",
        )
        for day in range(1, max_day + 1)
    ]
    return _button_rows(buttons, 7)


def _date_picker_month_rows(user_data: dict) -> list[list[InlineKeyboardButton]]:
    selected_month = user_data.get("natal_date_month")
    buttons = [
        InlineKeyboardButton(
            f"[{label}]" if selected_month == month else label,
            callback_data=f"natal_date:month:{month}",
        )
        for month, label in _RU_MONTHS_NOMINATIVE.items()
    ]
    return _button_rows(buttons, 3)


def _date_picker_year_rows(user_data: dict) -> list[list[InlineKeyboardButton]]:
    start = _year_page_start(user_data.get("natal_date_year_page"))
    end = min(start + _DATE_PICKER_YEAR_PAGE_SIZE - 1, _date_picker_year_max())
    selected_year = user_data.get("natal_date_year")
    buttons = [
        InlineKeyboardButton(
            f"[{year}]" if selected_year == year else str(year),
            callback_data=f"natal_date:year:{year}",
        )
        for year in range(start, end + 1)
    ]
    rows = _button_rows(buttons, 5)
    nav: list[InlineKeyboardButton] = []
    if start > _DATE_PICKER_YEAR_MIN:
        prev_start = max(_DATE_PICKER_YEAR_MIN, start - _DATE_PICKER_YEAR_PAGE_SIZE)
        nav.append(InlineKeyboardButton(f"< {prev_start}-{prev_start + _DATE_PICKER_YEAR_PAGE_SIZE - 1}", callback_data=f"natal_date:year_page:{prev_start}"))
    if end < _date_picker_year_max():
        next_start = start + _DATE_PICKER_YEAR_PAGE_SIZE
        nav.append(InlineKeyboardButton(f"{next_start}-{min(next_start + _DATE_PICKER_YEAR_PAGE_SIZE - 1, _date_picker_year_max())} >", callback_data=f"natal_date:year_page:{next_start}"))
    if nav:
        rows.append(nav)
    return rows


def _button_rows(buttons: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + size] for index in range(0, len(buttons), size)]


def _year_page_start(value: object) -> int:
    year = value if isinstance(value, int) else _DATE_PICKER_DEFAULT_YEAR_PAGE
    return max(_DATE_PICKER_YEAR_MIN, min(year, _date_picker_year_max()))


def _date_picker_year_max() -> int:
    return date.today().year


def _selected_date_from_picker(user_data: dict) -> date | None:
    day = user_data.get("natal_date_day")
    month = user_data.get("natal_date_month")
    year = user_data.get("natal_date_year")
    if not isinstance(day, int) or not isinstance(month, int) or not isinstance(year, int):
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _drop_invalid_selected_day(user_data: dict) -> None:
    day = user_data.get("natal_date_day")
    if isinstance(day, int) and day > _max_selectable_day(user_data):
        user_data.pop("natal_date_day", None)


def _max_selectable_day(user_data: dict) -> int:
    month = user_data.get("natal_date_month")
    year = user_data.get("natal_date_year")
    if not isinstance(month, int) or not 1 <= month <= 12:
        return 31
    if not isinstance(year, int):
        year = 2000
    return monthrange(year, month)[1]


def _next_missing_date_part(user_data: dict) -> str:
    if not isinstance(user_data.get("natal_date_day"), int):
        return "day"
    if not isinstance(user_data.get("natal_date_month"), int):
        return "month"
    if not isinstance(user_data.get("natal_date_year"), int):
        return "year"
    return "day"


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _draft_date_text(user_data: dict) -> str:
    value = user_data.get("natal_date")
    if isinstance(value, str) and value:
        return _format_birth_date_for_confirmation(value)
    day = user_data.get("natal_date_day")
    month = user_data.get("natal_date_month")
    year = user_data.get("natal_date_year")
    if not any(isinstance(part, int) for part in (day, month, year)):
        return "-"
    day_text = str(day) if isinstance(day, int) else "-"
    month_text = _RU_MONTHS_NOMINATIVE.get(month, "-") if isinstance(month, int) else "-"
    year_text = str(year) if isinstance(year, int) else "-"
    return f"день {day_text}, месяц {month_text}, год {year_text}"


def _country_entry_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"natal_country:{code}") for code, label in _FAST_COUNTRIES]]
    rows.append([InlineKeyboardButton("Введите другую страну сообщением", callback_data="natal_input:country")])
    return InlineKeyboardMarkup(rows)


def _place_entry_keyboard(country_code: object) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if isinstance(country_code, str):
        quick_buttons = _fast_city_buttons(country_code)
        if quick_buttons:
            rows.extend(_button_rows(quick_buttons, 1))
    rows.append([InlineKeyboardButton("Введите другой город сообщением", callback_data="natal_input:city")])
    return InlineKeyboardMarkup(rows)


def _fast_city_buttons(country_code: str) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    for label, query in _FAST_CITIES.get(country_code, ()):
        matches = search_cities(query, limit=1, country_code=country_code)
        if matches:
            buttons.append(InlineKeyboardButton(label, callback_data=f"natal_place:{matches[0].geoname_id}"))
    return buttons


def _input_keyboard(target: str) -> InlineKeyboardMarkup:
    labels = {
        "date": "Введите дату сообщением",
        "time": "Введите время сообщением",
        "country": "Введите страну сообщением",
        "city": "Введите город сообщением",
    }
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels.get(target, "Введите сообщением"), callback_data=f"natal_input:{target}")]])


def _country_keyboard(countries: list[CountryRecord]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(country.display_name, callback_data=f"natal_country:{country.code}")]
            for country in countries
        ]
    )


def _city_keyboard(cities: list[CityRecord]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_city_button_text(city), callback_data=f"natal_place:{city.geoname_id}")]
            for city in cities
        ]
        + [[InlineKeyboardButton("Нет в списке", callback_data="natal_place_missing")]]
    )


def _city_button_text(city: CityRecord) -> str:
    return f"{city.display_name} · {city.timezone}"


def _city_payload(city: CityRecord) -> dict[str, object]:
    return {
        "geoname_id": city.geoname_id,
        "display_name": city.display_name,
        "latitude": city.latitude,
        "longitude": city.longitude,
        "timezone": city.timezone,
    }


def _birth_input_with_local_city(birth_input: BirthInput) -> BirthInput | None:
    city = None
    for query in _local_city_queries(birth_input.birth_place):
        matches = search_cities(query, limit=1, country_code=birth_input.birth_place_country_code)
        if matches:
            city = matches[0]
            break
    if city is None:
        return None
    return birth_input.model_copy(
        update={
            "birth_place_geoname_id": city.geoname_id,
            "birth_place_latitude": city.latitude,
            "birth_place_longitude": city.longitude,
            "birth_place_timezone": city.timezone,
            "birth_place_display_name": city.display_name,
        }
    )


def _local_city_queries(place: str) -> list[str]:
    query = place.strip()
    if not query:
        return []
    queries = [query]
    comma_prefix = query.split(",", 1)[0].strip()
    if comma_prefix and comma_prefix != query:
        queries.append(comma_prefix)
    return queries


def _confirmation_text(birth_input: BirthInput) -> str:
    time_text = birth_input.birth_time or birth_input.time_precision.value
    limitations = ""
    if birth_input.time_precision == TimePrecision.UNKNOWN:
        limitations = "Без точного времени я не буду трактовать дома и асцендент как достоверные."
    return (
        "Проверьте данные:\n\n"
        f"Дата: {_format_birth_date_for_confirmation(birth_input.birth_date)}\n"
        f"Время: {time_text}\n"
        f"Место: {birth_input.birth_place}\n"
        f"Фокус: {birth_input.focus}\n"
        f"Ограничения: {limitations or 'нет'}"
    )


def _format_birth_date_for_confirmation(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    dotted = parsed.strftime("%d.%m.%Y")
    month_name = _RU_MONTHS_GENITIVE.get(parsed.month, parsed.strftime("%m"))
    return f"{dotted} ({parsed.day} {month_name} {parsed.year})"


def _birth_input_from_steps(user_data: dict) -> BirthInput:
    precision = user_data.get("natal_time_precision", TimePrecision.UNKNOWN)
    time_value = user_data.get("natal_time_value")
    table = (
        f"Дата рождения: {user_data.get('natal_date', '')}\n"
        f"Время рождения: {precision.value if isinstance(precision, TimePrecision) else precision}\n"
        f"Если точное или примерное: {time_value or ''}\n"
        f"Если диапазон: {time_value or ''}\n"
        f"Страна рождения: {user_data.get('natal_country_code', '')}\n"
        f"Место рождения: {user_data.get('natal_place', '')}\n"
        f"Фокус разбора: {user_data.get('natal_focus', 'general')}"
    )
    birth_input = parse_birth_table(table)
    place_data = user_data.get("natal_place_data")
    if isinstance(place_data, dict):
        birth_input = birth_input.model_copy(
            update={
                "birth_place_geoname_id": str(place_data.get("geoname_id") or ""),
                "birth_place_country_code": user_data.get("natal_country_code"),
                "birth_place_latitude": place_data.get("latitude"),
                "birth_place_longitude": place_data.get("longitude"),
                "birth_place_timezone": place_data.get("timezone"),
                "birth_place_display_name": place_data.get("display_name"),
            }
        )
    return birth_input
