# Natal Chart Product Readiness

## Status

Current state: beta-ready for controlled testing, not a fully finished public product.

This document tracks the changes required to move natal charts from MVP to product-ready. It also records which parts are already implemented so rollout decisions do not depend on memory.

## Implemented in this pass

- Local city autocomplete is backed by `geonamescache`, a pure-Python GeoNames dataset package.
- City records include `name`, alternate names, country, latitude, longitude, IANA timezone, and population.
- Telegram step flow now asks users to choose a country first, then a city from country-filtered inline suggestions instead of accepting ambiguous free text immediately.
- Table input accepts `Страна рождения`, normalizes it to an ISO country code, and uses it to resolve city coordinates/timezone locally.
- Selected city coordinates and timezone are embedded into `BirthInput`, so normal chart generation does not call external geocoding.
- `resolve_birth_data()` now uses embedded city coordinates first, local city lookup second, and Nominatim only as fallback.
- The city catalog is warmed during handler registration to avoid first-user lookup delay.
- Country and city search has automated coverage for Cyrillic country prefixes, one-letter country-filtered city prefixes, and release smoke cities: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, and Istanbul.
- The Telegram flow includes a "not in list" fallback that asks the user for the nearest large city.
- On local Python 3.14, city catalog warmup loaded 32,444 cities in about 403 ms; warm search for "Оде" took about 23 ms.
- Ascendant and MC no longer use the old latitude-independent placeholder formula. They are calculated from local sidereal time, mean obliquity, and ecliptic/horizon or ecliptic/meridian intersections.
- The astronomy math lives in a focused clean-room module with tests for J2000 Julian Day, sidereal time, mean obliquity, and the Kyiv Ascendant/MC reference case.
- Planet retrograde flags are calculated locally from signed ecliptic longitude movement around the chart time instead of being hard-coded to false.
- Houses use the equal-house system from the calculated Ascendant.

## Required Before Public Release

1. Run live smoke on the VPS with real `WEBHOOK_URL`, database, Telegram bot token, and `NATAL_REPORTS_ENABLED=true`.
2. Verify the hosted report link opens from Telegram on mobile and desktop.
3. Verify report persistence and deletion/TTL behavior against the real PostgreSQL migration.
4. Verify city search manually in Telegram for at least these cases: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, Istanbul. Automated local catalog coverage exists.
5. Repeat city catalog warmup timing on the VPS after deploy. Local Python 3.14 timing is about 403 ms for warmup and 23 ms for a warm "Оде" search.
6. Decide whether `cities1000` coverage is enough or whether the product needs a denser dataset for small towns.
7. Decide whether the "nearest large city" fallback is enough, or whether public launch needs an admin-reviewed coordinate entry path.
8. Calibrate interpretation prompts with real sample reports and reject overconfident claims when birth time is unknown.
9. Compare the local ephem-based planets, retrograde flags, Ascendant, MC, and equal-house cusps against a trusted reference for a small golden set of dates before calling the calculator production-grade.
10. Keep Swiss Ephemeris as a future optional accuracy upgrade only if AGPL/commercial license obligations are handled explicitly.

## City Data Decision

Use local GeoNames-backed data for the main product path. The important fields for natal charts are latitude, longitude, and timezone. `geonamescache==3.0.1` ships those fields in a `py3-none-any` wheel, so it avoids Python 3.14 native build risk and avoids network latency during normal user interaction.

GeoNames data is distributed under CC BY 4.0. Product documentation and/or an about page should include attribution before public launch.

## Remaining Product Risks

- The current astrology calculator is deterministic and local, but it is not Swiss Ephemeris-grade.
- Equal-house cusps are implemented locally; Placidus/Koch/etc. are not.
- City autocomplete in normal Telegram chat is message-by-message, not true live keystroke autocomplete. The step flow reduces noise by asking for country first and filtering city suggestions locally. True per-character updates would require a Telegram Mini App or inline mode.
- The fallback Nominatim geocoder is still network-dependent and should be treated as fallback only.
