from __future__ import annotations

import os
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.natal.city_catalog import CityRecord, CountryRecord, find_city_by_id, search_cities, search_countries
from app.natal.intent import NATAL_INTENT_RE
from app.natal.models import BirthInput, TimePrecision
from app.natal.parser import BirthInputParseError, parse_birth_table
from app.natal.service import create_natal_report

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
    "natal_time_precision",
    "natal_time_value",
    "natal_country_code",
    "natal_country",
    "natal_place",
    "natal_place_data",
    "natal_focus",
    "natal_mode",
}


async def natal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message:
        return ConversationHandler.END
    clear_natal_user_data(context.user_data)
    if not _natal_reports_enabled_for_handler():
        await update.message.reply_text("Натальные карты временно недоступны.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Натальная карта строится по дате, месту и, если известно, времени рождения.\n"
        "Если точного времени нет, я построю карту без домов и асцендента и явно отмечу ограничения.",
        reply_markup=_mode_keyboard(),
    )
    return NATAL_MODE


async def on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
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
        await query.edit_message_text(
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
    await query.edit_message_text("Дата рождения? Например: 14.02.1995")
    return NATAL_DATE


async def on_table_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not update.message or not update.message.text:
        return NATAL_TABLE
    try:
        birth_input = parse_birth_table(update.message.text)
    except BirthInputParseError as exc:
        await update.message.reply_text(f"Не удалось разобрать данные: {exc}")
        return NATAL_TABLE
    birth_input = _birth_input_with_local_city(birth_input)
    if birth_input is None:
        await update.message.reply_text("Город не найден в выбранной стране. Введите ближайший крупный город.")
        return NATAL_TABLE
    context.user_data["natal_birth_input"] = birth_input
    await update.message.reply_text(_confirmation_text(birth_input), reply_markup=_confirm_keyboard())
    return NATAL_CONFIRM


async def on_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data["natal_date"] = update.message.text.strip()
    await update.message.reply_text("Время рождения: точное / примерное / диапазон / неизвестно")
    return NATAL_TIME_PRECISION


async def on_time_precision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    raw = (update.message.text or "").strip().lower()
    if raw in {"неизвестно", "unknown", "не знаю"}:
        context.user_data["natal_time_precision"] = TimePrecision.UNKNOWN
        await update.message.reply_text("Страна рождения?")
        return NATAL_COUNTRY
    if raw in {"точное", "exact"}:
        context.user_data["natal_time_precision"] = TimePrecision.EXACT
    elif raw in {"примерное", "approx", "approximate"}:
        context.user_data["natal_time_precision"] = TimePrecision.APPROXIMATE
    elif raw in {"диапазон", "range"}:
        context.user_data["natal_time_precision"] = TimePrecision.RANGE
    else:
        await update.message.reply_text("Время рождения: точное / примерное / диапазон / неизвестно")
        return NATAL_TIME_PRECISION
    await update.message.reply_text("Укажите время или диапазон.")
    return NATAL_TIME_VALUE


async def on_time_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data["natal_time_value"] = update.message.text.strip()
    await update.message.reply_text("Страна рождения?")
    return NATAL_COUNTRY


async def on_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.message.text.strip()
    matches = search_countries(query, limit=8)
    if not matches:
        await update.message.reply_text("Страна не найдена. Введите больше букв.")
        return NATAL_COUNTRY
    await update.message.reply_text(
        "Выберите страну из списка или введите больше букв для уточнения.",
        reply_markup=_country_keyboard(matches),
    )
    return NATAL_COUNTRY


async def on_country_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
    if not query:
        return NATAL_COUNTRY
    await query.answer()
    country_code = query.data.replace("natal_country:", "")
    country = search_countries(country_code, limit=1)
    country_display = country[0].display_name if country else country_code
    context.user_data["natal_country_code"] = country_code
    context.user_data["natal_country"] = country_display
    await query.edit_message_text(f"Страна рождения: {country_display}\n\nГород рождения?")
    return NATAL_PLACE


async def on_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.message.text.strip()
    country_code = context.user_data.get("natal_country_code")
    if not isinstance(country_code, str) or not country_code:
        await update.message.reply_text("Сначала выберите страну рождения.")
        return NATAL_COUNTRY
    matches = search_cities(query, limit=8, country_code=country_code)
    if not matches:
        await update.message.reply_text("Город не найден. Введите больше букв или укажите ближайший крупный город.")
        return NATAL_PLACE
    await update.message.reply_text(
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
        await query.edit_message_text("Город не найден. Введите место рождения еще раз.")
        return NATAL_PLACE
    country_code = context.user_data.get("natal_country_code")
    if not isinstance(country_code, str) or not country_code:
        await query.edit_message_text("Сначала выберите страну рождения.")
        return NATAL_COUNTRY
    if city.country_code != country_code:
        await query.edit_message_text("Этот город не относится к выбранной стране. Введите город еще раз.")
        return NATAL_PLACE
    context.user_data["natal_place"] = city.display_name
    context.user_data["natal_place_data"] = _city_payload(city)
    await query.edit_message_text(
        f"Место рождения: {city.display_name}\n\n"
        "Фокус разбора: общий / отношения / карьера / психология / кратко"
    )
    return NATAL_FOCUS


async def on_place_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    query = update.callback_query
    if not query:
        return NATAL_PLACE
    await query.answer()
    await query.edit_message_text(
        "Если вашего города нет в списке, введите ближайший крупный город рядом с местом рождения. "
        "Для натальной карты важны координаты и часовой пояс."
    )
    return NATAL_PLACE


async def on_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    context.user_data["natal_focus"] = update.message.text.strip() or "общий"
    try:
        birth_input = _birth_input_from_steps(context.user_data)
    except BirthInputParseError as exc:
        await update.message.reply_text(f"Не удалось разобрать данные: {exc}")
        if "врем" in str(exc).lower():
            return NATAL_TIME_VALUE
        return NATAL_DATE
    context.user_data["natal_birth_input"] = birth_input
    await update.message.reply_text(_confirmation_text(birth_input), reply_markup=_confirm_keyboard())
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
    await query.edit_message_text("Считаю карту...")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    try:
        report = await create_natal_report(
            birth_input=birth_input,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            webhook_url=webhook_url,
        )
    except Exception as exc:
        await query.message.reply_text(f"Не удалось построить карту: {exc}")
        return ConversationHandler.END
    lines = [f"Готово: {report.hosted_url}"]
    if report.telegraph_url:
        lines.append(f"Telegraph: {report.telegraph_url}")
    await query.message.reply_text("\n".join(lines))
    clear_natal_user_data(context.user_data)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_natal_user_data(context.user_data)
    if update.message:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END


def build_natal_chart_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("natal", natal_command),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(NATAL_INTENT_RE), natal_command),
        ],
        states={
            NATAL_MODE: [CallbackQueryHandler(on_mode, pattern=r"^natal_mode:")],
            NATAL_TABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_table_input)],
            NATAL_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_date)],
            NATAL_TIME_PRECISION: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_precision)],
            NATAL_TIME_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_value)],
            NATAL_COUNTRY: [
                CallbackQueryHandler(on_country_selected, pattern=r"^natal_country:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_country),
            ],
            NATAL_PLACE: [
                CallbackQueryHandler(on_place_missing, pattern=r"^natal_place_missing$"),
                CallbackQueryHandler(on_place_selected, pattern=r"^natal_place:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_place),
            ],
            NATAL_FOCUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_focus)],
            NATAL_CONFIRM: [CallbackQueryHandler(on_confirm, pattern=r"^natal_confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Заполнить пошагово", callback_data="natal_mode:step")],
            [InlineKeyboardButton("Отправить таблицей", callback_data="natal_mode:table")],
            [InlineKeyboardButton("Отмена", callback_data="natal_mode:cancel")],
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Построить", callback_data="natal_confirm:yes"),
                InlineKeyboardButton("Отмена", callback_data="natal_confirm:cancel"),
            ]
        ]
    )


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
        f"Дата: {birth_input.birth_date}\n"
        f"Время: {time_text}\n"
        f"Место: {birth_input.birth_place}\n"
        f"Фокус: {birth_input.focus}\n"
        f"Ограничения: {limitations or 'нет'}"
    )


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
