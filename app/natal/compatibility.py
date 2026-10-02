"""Local compatibility readings and compact, non-sensitive Telegram start links."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import ephem

from app.astro import ecliptic_longitude
from app.i18n import detect_language, t
from app.natal.destiny_matrix import calculate_destiny_matrix

Gender = Literal["m", "f", "n"]

_PREFIX = re.compile(
    r"^\s*(?:совместимость|совместим|совмест|совм\.?|синастрия|compatibility|compat)(?=$|[\s:,])", re.I
)
_SIGN_PATTERNS = (
    r"ов(?:ен|на|ну|ном|ны)|aries",
    r"тел(?:ец|ьца|ьцу|ьцом|ьцы)|taurus",
    r"близнец(?:ы|ов|ам|ами)?|gemini",
    r"рак(?:а|у|ом|и)?|cancer",
    r"л(?:ев|ьва|ьву|ьвом|ьвы)|leo",
    r"дев(?:а|ы|у|ой)|virgo",
    r"вес(?:ы|ов|ам|ами)|libra",
    r"скорпион(?:а|у|ом|ы)?|scorpio",
    r"стрел(?:ец|ьца|ьцу|ьцом|ьцы)|sagittarius",
    r"козерог(?:а|у|ом|и)?|capricorn",
    r"водол(?:ей|ея|ею|еем|еи)|aquarius",
    r"рыб(?:ы|а|у|ой|ам|ами)?|pisces",
)
_SIGN_RE = tuple(re.compile(pattern, re.I) for pattern in _SIGN_PATTERNS)
_GENDERS: dict[str, Gender] = {
    **dict.fromkeys(("м", "мужчина", "мужчины", "мужской", "парень", "муж", "он", "man", "male"), "m"),
    **dict.fromkeys(("ж", "женщина", "женщины", "женский", "девушка", "жена", "она", "woman", "female"), "f"),
}
_GRAMMAR: dict[str, tuple[int | None, int | None]] = {
    "SS": (None, None),
    "GSGS": (0, 2),
    "SGSG": (1, 3),
    "GSSG": (0, 3),
    "SGGS": (1, 2),
    "GSS": (0, None),
    "SGS": (1, None),
    "SSG": (None, 2),
}
_START_RE = re.compile(
    r"^compat_(?P<tarot>t_)?(?P<english>e_)?(?P<first>[mfn])(?P<a>\d{1,2})_(?P<second>[mfn])(?P<b>\d{1,2})$"
)
_DMY_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_PLANETS = (
    ("sun", ephem.Sun),
    ("moon", ephem.Moon),
    ("mercury", ephem.Mercury),
    ("venus", ephem.Venus),
    ("mars", ephem.Mars),
)


class CompatibilityQueryError(ValueError):
    """The query cannot identify exactly two partners unambiguously."""


@dataclass(frozen=True, slots=True)
class CompatibilityPartner:
    sign_index: int
    gender: Gender = "n"

    def __post_init__(self) -> None:
        if not 0 <= self.sign_index < 12 or self.gender not in {"m", "f", "n"}:
            raise CompatibilityQueryError("Invalid partner.")


@dataclass(frozen=True, slots=True)
class CompatibilityPair:
    first: CompatibilityPartner
    second: CompatibilityPartner
    language: str = "ru"


@dataclass(frozen=True, slots=True)
class BirthDateCompatibility:
    html: str
    tarot_context: str


def is_compatibility_query(query: str) -> bool:
    return bool(_PREFIX.match(query))


def parse_compatibility_query(query: str) -> CompatibilityPair:
    prefix = _PREFIX.match(query)
    if not prefix or re.search(r"\d", query):
        raise CompatibilityQueryError("Use two zodiac signs; enter birth dates in the private chat.")
    tokens = [
        token for token in re.findall(r"[^\W\d_]+", query[prefix.end() :].casefold()) if token not in {"и", "and"}
    ]
    values: list[int | Gender] = []
    kinds = ""
    signs: list[int] = []
    for token in tokens:
        if token in _GENDERS:
            kinds += "G"
            values.append(_GENDERS[token])
            continue
        sign = next((index for index, pattern in enumerate(_SIGN_RE) if pattern.fullmatch(token)), None)
        if sign is None:
            raise CompatibilityQueryError("Unknown zodiac sign or partner label.")
        kinds += "S"
        values.append(sign)
        signs.append(sign)
    grammar = _GRAMMAR.get(kinds)
    if grammar is None:
        raise CompatibilityQueryError("Specify exactly two partners.")
    genders: list[Gender] = []
    for index in grammar:
        gender = values[index] if index is not None else "n"
        if gender not in {"m", "f", "n"}:
            raise CompatibilityQueryError("Ambiguous partner label.")
        genders.append(gender)  # type: ignore[arg-type]
    return CompatibilityPair(
        CompatibilityPartner(signs[0], genders[0]), CompatibilityPartner(signs[1], genders[1]), detect_language(query)
    )


def compatibility_start_payload(pair: CompatibilityPair, *, tarot: bool = False) -> str:
    return f"compat_{'t_' if tarot else ''}{'e_' if pair.language == 'en' else ''}{pair.first.gender}{pair.first.sign_index}_{pair.second.gender}{pair.second.sign_index}"


def parse_compatibility_start_payload(payload: str) -> tuple[CompatibilityPair, bool]:
    match = _START_RE.fullmatch(payload)
    if match is None:
        raise CompatibilityQueryError("Invalid compatibility link.")
    first = CompatibilityPartner(int(match["a"]), match["first"])  # type: ignore[arg-type]
    second = CompatibilityPartner(int(match["b"]), match["second"])  # type: ignore[arg-type]
    return CompatibilityPair(first, second, "en" if match["english"] else "ru"), bool(match["tarot"])


def partner_label(partner: CompatibilityPartner, *, lang: str) -> str:
    sign = t(f"compat.sign.{partner.sign_index}", lang)
    return f"{t(f'compat.gender.{partner.gender}', lang)} · {sign}" if partner.gender != "n" else sign


def _element_pair_key(first: int, second: int) -> str:
    return "_".join(str(index) for index in sorted((first % 4, second % 4)))


def _pair_dynamics(first: int, second: int, *, lang: str) -> tuple[str, str, str]:
    key = _element_pair_key(first, second)
    strength = t(f"compat.elements.{key}.strength", lang)
    tension = t(f"compat.elements.{key}.tension", lang)
    if first % 3 == second % 3:
        tension += " " + t(f"compat.mode.{first % 3}", lang)
    return strength, tension, t(f"compat.elements.{key}.action", lang)


def build_sign_compatibility_html(pair: CompatibilityPair, *, lang: str = "ru") -> str:
    strength, tension, action = _pair_dynamics(pair.first.sign_index, pair.second.sign_index, lang=lang)
    needs = " · ".join(t(f"compat.need.{partner.sign_index}", lang) for partner in (pair.first, pair.second))
    return (
        f"💞 <b>{t('compat.title', lang)}</b>\n"
        f"{html.escape(partner_label(pair.first, lang=lang))} + {html.escape(partner_label(pair.second, lang=lang))}\n\n"
        f"{html.escape(needs)}\n\n"
        f"<b>{t('compat.strength', lang)}</b> {html.escape(strength)}\n\n"
        f"<b>{t('compat.tension', lang)}</b> {html.escape(tension)}\n\n"
        f"<b>{t('compat.action', lang)}</b> {html.escape(action)}\n\n"
        f"<i>{t('compat.sign_limit', lang)}</i>"
    )


def looks_like_birth_date_input(raw: str) -> bool:
    """Recognize isolated date input without retaining it or validating its calendar value."""
    value = raw.strip()
    return bool(_DMY_RE.fullmatch(value) or _ISO_RE.fullmatch(value))


def parse_compatibility_birth_date(raw: str, *, today: date | None = None) -> date:
    value = raw.strip()
    dmy = _DMY_RE.fullmatch(value)
    iso = _ISO_RE.fullmatch(value)
    if dmy:
        day, month, year = map(int, dmy.groups())
    elif iso:
        year, month, day = map(int, iso.groups())
    else:
        raise ValueError("Use DD.MM.YYYY or YYYY-MM-DD.")
    parsed = date(year, month, day)
    if not date(1900, 1, 1) <= parsed <= (today or datetime.now(UTC).date()):
        raise ValueError("Birth date is outside the supported range.")
    return parsed


def _birth_signs(birth_date: date) -> dict[str, tuple[int, ...]]:
    # Without a place/time, the local birthday spans UTC-14h through UTC+36h.
    # Sample the whole interval, including midnight crossings and retrograde turns.
    day_start = datetime.combine(birth_date, time(), tzinfo=UTC)
    start = day_start - timedelta(hours=14)
    samples = [start + timedelta(hours=hour) for hour in range(0, 50, 2)]
    samples.append(day_start + timedelta(hours=36) - timedelta(microseconds=1))
    result: dict[str, tuple[int, ...]] = {}
    for key, factory in _PLANETS:
        result[key] = tuple(dict.fromkeys(int(ecliptic_longitude(factory, moment) // 30) for moment in samples))
    return result


def _sign_names(signs: tuple[int, ...], *, lang: str) -> str:
    return " / ".join(t(f"compat.sign.{index}", lang) for index in signs)


def build_compatibility_tarot_context(pair: CompatibilityPair, *, lang: str = "ru") -> str:
    labels = " + ".join(partner_label(partner, lang=lang) for partner in (pair.first, pair.second))
    return t("compat.tarot_question", lang, pair=labels)


def build_birth_date_compatibility(
    pair: CompatibilityPair, first_date: date, second_date: date, *, lang: str = "ru"
) -> BirthDateCompatibility:
    signatures = [_birth_signs(first_date), _birth_signs(second_date)]
    matrix_centers = [
        next(position for position in calculate_destiny_matrix(value.isoformat()).positions if position.key == "center")
        for value in (first_date, second_date)
    ]
    parts = [f"💞 <b>{t('compat.dates_title', lang)}</b>\n"]
    derived_context: list[str] = []
    for index, (partner, signature) in enumerate(zip((pair.first, pair.second), signatures, strict=True), start=1):
        label = (
            t(f"compat.gender.{partner.gender}", lang)
            if partner.gender != "n"
            else t("compat.partner", lang, index=str(index))
        )
        positions = "; ".join(
            f"{t(f'compat.planet.{key}', lang)}: {_sign_names(signature[key], lang=lang)}" for key, _ in _PLANETS
        )
        parts.append(f"<b>{html.escape(label)}</b>\n{html.escape(positions)}\n")
        derived_context.append(f"{label}: {positions}")
    if any(len(signature["sun"]) > 1 for signature in signatures):
        parts.append(f"<i>{t('compat.boundary', lang)}</i>\n")
    else:
        first_sun, second_sun = (signature["sun"][0] for signature in signatures)
        strength, tension, action = _pair_dynamics(first_sun, second_sun, lang=lang)
        parts.extend(
            (
                f"<b>{t('compat.strength', lang)}</b> {html.escape(strength)}\n",
                f"<b>{t('compat.tension', lang)}</b> {html.escape(tension)}\n",
                f"<b>{t('compat.action', lang)}</b> {html.escape(action)}\n",
            )
        )
        if (first_sun, second_sun) != (pair.first.sign_index, pair.second.sign_index):
            parts.append(f"<i>{t('compat.corrected_signs', lang)}</i>\n")
    for key in ("moon", "mercury", "venus"):
        signs_a, signs_b = signatures[0][key], signatures[1][key]
        if len(signs_a) == len(signs_b) == 1:
            strength, _, action = _pair_dynamics(signs_a[0], signs_b[0], lang=lang)
            parts.append(f"<b>{t(f'compat.channel.{key}', lang)}</b> {html.escape(strength)} {html.escape(action)}\n")
    centers = " + ".join(f"{position.arcana} — {position.arcana_label}" for position in matrix_centers)
    # Existing matrix interpretations are Russian; avoid claiming an English translation.
    matrix_copy = t("compat.matrix", lang, centers=centers)
    parts.append(f"<b>{t('compat.matrices', lang)}</b> {html.escape(matrix_copy)}\n")
    derived_context.append(matrix_copy)
    parts.append(f"<i>{t('compat.date_limit', lang)}</i>")
    context = t("compat.tarot_from_dates", lang) + "\n" + "\n".join(derived_context)
    return BirthDateCompatibility(html="\n".join(parts), tarot_context=context)
