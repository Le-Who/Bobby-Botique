"""Authenticated pair questionnaires and private local compatibility delivery."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from quart import Blueprint, abort, g, jsonify, render_template, request

from app.bot_instance import get_application, get_bot
from app.handlers.compatibility import compatibility_result_keyboard
from app.i18n import t
from app.natal.city_catalog import search_cities, search_countries
from app.natal.compatibility import CompatibilityQueryError, parse_compatibility_start_payload, partner_label
from app.natal.compatibility_input import PairInput, PairInputError, parse_pair_input
from app.natal.compatibility_reading import build_pair_reading
from app.request_context import set_user_context
from app.utils.background_tasks import submit_task
from app.utils.text_format import split_text_safe
from app.web_miniapp import require_authorized_webapp_user, require_webapp_auth

logger = logging.getLogger(__name__)
compatibility_bp = Blueprint("compatibility", __name__, template_folder="templates")
_FORM_KEYS = (
    "title",
    "intro",
    "first",
    "second",
    "date",
    "day",
    "month",
    "year",
    "time",
    "exact",
    "approximate",
    "range",
    "unknown",
    "time_start",
    "time_end",
    "place",
    "place_unknown",
    "country",
    "city",
    "city_hint",
    "next",
    "back",
    "review",
    "submit",
    "cancel",
    "date_error",
    "time_error",
    "place_error",
    "country_error",
    "form_error",
    "auth_error",
    "accepted",
    "accepted_note",
    "step",
    "no_results",
)


@compatibility_bp.get("/compatibility-form")
async def compatibility_form():
    payload = request.args.get("pair", "")
    try:
        pair, tarot = parse_compatibility_start_payload(payload)
        if tarot:
            abort(400)
    except CompatibilityQueryError:
        abort(400)
    options = {
        "pair": payload,
        "lang": pair.language,
        "first_label": partner_label(pair.first, lang=pair.language),
        "second_label": partner_label(pair.second, lang=pair.language),
        "copy": {key: t(f"compat.form.{key}", pair.language) for key in _FORM_KEYS},
    }
    return await render_template("compatibility_form.html", compatibility_options=options, csp_nonce=g.csp_nonce)


@compatibility_bp.get("/api/compatibility/countries")
@require_webapp_auth
@require_authorized_webapp_user
async def compatibility_countries(user_id: int):
    query = request.args.get("q", "").strip()[:80]
    countries = await asyncio.to_thread(search_countries, query, limit=8) if query else []
    return jsonify({"items": [{"id": country.code, "label": country.display_name} for country in countries]})


@compatibility_bp.get("/api/compatibility/cities")
@require_webapp_auth
@require_authorized_webapp_user
async def compatibility_cities(user_id: int):
    query = request.args.get("q", "").strip()[:80]
    country = request.args.get("country", "").strip().upper()
    cities = (
        await asyncio.to_thread(search_cities, query, limit=8, country_code=country)
        if query and len(country) == 2
        else []
    )
    return jsonify({"items": [{"id": city.geoname_id, "label": city.display_name} for city in cities]})


@compatibility_bp.post("/api/compatibility/submit")
@require_webapp_auth
@require_authorized_webapp_user
async def submit_compatibility(user_id: int):
    body = await request.get_json(silent=True)
    try:
        value = await asyncio.to_thread(parse_pair_input, body)
    except ValueError as exc:
        # Validation exceptions can contain untrusted input; return fixed copy.
        lang = "en" if isinstance(body, dict) and "_e_" in str(body.get("pair", "")) else "ru"
        key = exc.key if isinstance(exc, PairInputError) else "form_error"
        return jsonify({"error": "invalid_birth_input", "detail": t(f"compat.form.{key}", lang)}), 400
    bot, application = get_bot(), get_application()
    if bot is None or application is None:
        return jsonify({"error": "bot_not_ready", "detail": t("compat.form.form_error", value.pair.language)}), 503
    set_user_context(user_id, user_id)
    submit_task(_send_pair_result(bot, application, user_id, value), operation="compatibility.calculate")
    return jsonify({"ok": True, "status": "accepted"}), 202


async def _send_pair_result(bot: Any, application: Any, user_id: int, value: PairInput) -> None:
    from app.repos.users import is_authorized

    lang = value.pair.language
    try:
        if not await is_authorized(user_id):
            return
        reading = await build_pair_reading(value)
        if not await is_authorized(user_id):
            return
        keyboard = compatibility_result_keyboard(application.user_data[user_id], reading.tarot_context, lang=lang)
        chunks = split_text_safe(reading.html)
        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=user_id,
                text=chunk,
                parse_mode="HTML",
                reply_markup=keyboard if index == len(chunks) - 1 else None,
            )
    except Exception as exc:
        # Do not log exception text/locals: resolvers handle private birth data.
        logger.warning("Compatibility delivery failed user=%s error_type=%s", user_id, type(exc).__name__)
        try:
            if await is_authorized(user_id):
                await bot.send_message(chat_id=user_id, text=t("compat.background_failed", lang))
        except Exception:
            logger.warning("Compatibility failure notification could not be delivered user=%s", user_id)
