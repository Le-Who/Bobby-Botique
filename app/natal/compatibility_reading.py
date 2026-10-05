"""Compose private derived compatibility results with per-partner uncertainty."""

from __future__ import annotations

import asyncio
import html
from datetime import date

from app.i18n import t
from app.natal.compatibility import BirthDateCompatibility, build_birth_date_compatibility
from app.natal.compatibility_input import PairInput
from app.natal.models import TimePrecision
from app.natal.synastry import SynastryData, calculate_synastry
from app.utils.text_format import strip_formatting

_POINTS = ("sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto")


async def build_pair_reading(value: PairInput) -> BirthDateCompatibility:
    lang = value.pair.language
    if (
        not value.first.birth_place
        and not value.second.birth_place
        and value.first.time_precision == value.second.time_precision == TimePrecision.UNKNOWN
    ):
        return await asyncio.to_thread(
            build_birth_date_compatibility,
            value.pair,
            date.fromisoformat(value.first.birth_date),
            date.fromisoformat(value.second.birth_date),
            lang=lang,
        )
    data = await calculate_synastry(value.first, value.second)
    return render_synastry(data, value, lang=lang)


def render_synastry(data: SynastryData, value: PairInput, *, lang: str) -> BirthDateCompatibility:
    parts = [f"💞 <b>{t('compat.detailed_title', lang)}</b>"]
    unknown = False
    has_houses = False
    for index, (partner, birth) in enumerate(
        zip((data.first, data.second), (value.first, value.second), strict=True), 1
    ):
        label = t("compat.partner", lang, index=str(index))
        parts.append(f"\n<b>{label}</b>")
        parts.append("; ".join(f"{_point_label(key, lang)}: {_signs(partner.signs[key], lang)}" for key in _POINTS[:5]))
        chart = partner.chart
        if chart and chart.input_quality.angles_available:
            has_houses = True
            for key in ("ascendant", "mc"):
                longitude = chart.angles[key]
                parts.append(
                    f"{_point_label(key, lang)}: {t(f'compat.sign.{int(longitude // 30) % 12}', lang)} {longitude % 30:.1f}°"
                )
        if birth.time_precision == TimePrecision.UNKNOWN:
            unknown = True
            parts.append(t("compat.time_unknown", lang, partner=label))
        elif birth.time_precision == TimePrecision.RANGE:
            parts.append(t("compat.time_range", lang, partner=label))
        elif birth.time_precision != TimePrecision.EXACT:
            parts.append(t("compat.time_approximate", lang, partner=label))
        if not birth.birth_place:
            parts.append(t("compat.place_missing", lang, partner=label))
    parts.append(f"\n<b>{t('compat.aspects', lang)}</b>")
    if not data.aspects:
        parts.append(t("compat.no_aspects", lang))
    else:
        aspects = sorted(
            data.aspects,
            key=lambda aspect: (
                _priority(aspect.point_a) + _priority(aspect.point_b),
                aspect.orb if aspect.orb is not None else 100,
            ),
        )
        for aspect in aspects[:8]:
            orb = f" ({aspect.orb:.1f}°)" if aspect.orb is not None else ""
            parts.append(
                f"• {_point_label(aspect.point_a, lang)} 1 — {t(f'compat.aspect.{aspect.aspect}', lang)} — "
                f"{_point_label(aspect.point_b, lang)} 2{orb}. {t(f'compat.aspect_meaning.{aspect.aspect}', lang)}"
            )
    if data.overlays:
        parts.append(f"\n<b>{t('compat.overlays', lang)}</b>")
        for overlay in sorted(data.overlays, key=lambda item: _priority(item.planet))[:8]:
            parts.append(
                "• "
                + t(
                    "compat.overlay_line",
                    lang,
                    planet=_point_label(overlay.planet, lang),
                    source=str(overlay.source_partner + 1),
                    target=str(overlay.target_partner + 1),
                    house=str(overlay.house),
                )
            )
    if has_houses:
        parts.append("\n" + t("compat.houses_equal", lang))
    if unknown:
        parts.append(t("compat.stable_only", lang))
    parts.append(t("compat.symbolic", lang))
    # All inputs to this body are derived chart data and fixed translations.
    body = "\n".join(parts)
    context = t("compat.tarot_from_dates", lang) + "\n" + strip_formatting(body)
    return BirthDateCompatibility(html=body, tarot_context=context)


def _point_label(key: str, lang: str) -> str:
    if key == "ascendant":
        return t("compat.ascendant", lang)
    if key == "mc":
        return t("compat.angle.mc", lang)
    return t(f"compat.planet.{key}", lang)


def _signs(signs: tuple[int, ...], lang: str) -> str:
    return html.escape(" / ".join(t(f"compat.sign.{sign}", lang) for sign in signs))


def _priority(key: str) -> int:
    return _POINTS.index(key) if key in _POINTS else 5
