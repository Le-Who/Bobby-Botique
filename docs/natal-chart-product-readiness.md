# Natal Chart Product Readiness

## Status

Current state: beta-ready for controlled testing, not a fully finished public product.

This document tracks the changes required to move natal charts from MVP to product-ready. It also records which parts are already implemented so rollout decisions do not depend on memory.

## Implemented in this pass

- Local city autocomplete is backed by `geonamescache`, a pure-Python GeoNames dataset package.
- City records include `name`, alternate names, country, latitude, longitude, IANA timezone, and population.
- Telegram step flow now asks users to choose a city from inline suggestions instead of accepting ambiguous free text immediately.
- Selected city coordinates and timezone are embedded into `BirthInput`, so normal chart generation does not call external geocoding.
- `resolve_birth_data()` now uses embedded city coordinates first, local city lookup second, and Nominatim only as fallback.
- The city catalog is warmed during handler registration to avoid first-user lookup delay.

## Required Before Public Release

1. Run live smoke on the VPS with real `WEBHOOK_URL`, database, Telegram bot token, and `NATAL_REPORTS_ENABLED=true`.
2. Verify the hosted report link opens from Telegram on mobile and desktop.
3. Verify report persistence and deletion/TTL behavior against the real PostgreSQL migration.
4. Verify city search for at least these cases: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, Istanbul.
5. Measure first process startup impact from warming the GeoNames city catalog.
6. Decide whether `cities1000` coverage is enough or whether the product needs a denser dataset for small towns.
7. Add a user-facing fallback path for "my town is not in the list" that asks for nearest large city or admin-reviewed coordinates.
8. Calibrate interpretation prompts with real sample reports and reject overconfident claims when birth time is unknown.
9. Compare the local ephem-based positions against a trusted reference for a small golden set of dates before calling the calculator production-grade.
10. Keep Swiss Ephemeris as a future optional accuracy upgrade only if AGPL/commercial license obligations are handled explicitly.

## City Data Decision

Use local GeoNames-backed data for the main product path. The important fields for natal charts are latitude, longitude, and timezone. `geonamescache==3.0.1` ships those fields in a `py3-none-any` wheel, so it avoids Python 3.14 native build risk and avoids network latency during normal user interaction.

GeoNames data is distributed under CC BY 4.0. Product documentation and/or an about page should include attribution before public launch.

## Remaining Product Risks

- The current astrology calculator is deterministic and local, but it is not Swiss Ephemeris-grade.
- House and ascendant calculations remain approximate.
- City autocomplete in normal Telegram chat is message-by-message, not true live keystroke autocomplete. True per-character updates would require a Telegram Mini App or inline mode.
- The fallback Nominatim geocoder is still network-dependent and should be treated as fallback only.
