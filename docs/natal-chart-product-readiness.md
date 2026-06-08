# Natal Chart Product Readiness

## Status

Current state: beta-ready for controlled testing, not a fully finished public product.

This document tracks the changes required to move natal charts from MVP to product-ready. It also records which parts are already implemented so rollout decisions do not depend on memory.

## Implemented in this pass

- Local city autocomplete is backed by `geonamescache`, a pure-Python GeoNames dataset package.
- City records include `name`, alternate names, country, latitude, longitude, IANA timezone, and population.
- Telegram step flow now asks users to choose a country first, then a city from country-filtered inline suggestions instead of accepting ambiguous free text immediately.
- City suggestions include known administrative regions for countries where local GeoNames `admin1code` can be resolved locally, reducing wrong-coordinate selections for same-name cities such as Reading, Pennsylvania vs Reading, Massachusetts.
- City search ranks exact city-name matches above similar alternate-name or substring matches, and uses `city, region` hints to disambiguate same-name cities in table/local lookup.
- City autocomplete uses a local prefix candidate index over normalized names and name tokens, with a full-scan fallback only when the indexed candidate set is too small.
- Table input requires `Страна рождения`, normalizes it to an ISO country code, and uses it to resolve city coordinates/timezone locally.
- Table input resolves and embeds local city coordinates/timezone before confirmation, so unknown places fail before report generation.
- Country autocomplete and table parsing recognize common Russian country names such as France, Spain, Italy, Georgia, Armenia, Moldova, Netherlands, Czechia, Serbia, and Latvia, not only the initial UA/RU-focused aliases.
- Selected city coordinates and timezone are embedded into `BirthInput`, so normal chart generation does not call external geocoding.
- Admin-reviewed local city overrides can be supplied through `NATAL_CITY_OVERRIDES_PATH` as a UTF-8 JSON file. Override cities are indexed into the same local autocomplete path with coordinates and IANA timezone, so missing GeoNames cities can be added without runtime network geocoding. See `docs/natal-city-overrides.example.json`.
- Invalid embedded timezone data is converted into a deterministic `GeocodingError` instead of leaking a low-level `ZoneInfo` exception.
- Embedded and fallback geocoder coordinates are range-validated before timezone resolution and chart calculation.
- `resolve_birth_data()` now uses embedded city coordinates first and local city lookup second. Nominatim is available only when `NATAL_GEOCODER_PROVIDER=nominatim` is set explicitly.
- Unknown or misspelled `NATAL_GEOCODER_PROVIDER` values are treated as local-only behavior; they do not trigger network geocoding.
- When the opt-in Nominatim fallback is used for coordinates, timezone is still resolved locally from the nearest GeoNames city instead of a hard-coded country/city heuristic.
- The city catalog is warmed during handler registration to avoid first-user lookup delay.
- Country and city search has automated coverage for Cyrillic country prefixes, one-letter country-filtered city prefixes, and release smoke cities: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, and Istanbul.
- The Telegram flow includes a "not in list" fallback that asks the user for the nearest large city.
- Hosted reports and Telegraph mirrors include GeoNames / CC BY 4.0 city-data attribution.
- Hosted report rendering strips `javascript:` links from interpretation body HTML, sanitizes stored SVG payloads before rendering, removes SVG event-handler attributes, and renders Telegraph mirror links only when they are HTTPS URLs.
- Report generation stores Telegraph mirror URLs only when the publisher returns an HTTPS URL, so unsafe mirrors are filtered before persistence as well as before rendering.
- Hosted report routes are covered for 200/404 behavior and security headers: `nosniff`, `no-referrer`, `X-Frame-Options: DENY`, and a no-script CSP.
- Hosted report routes reject malformed report ids before storage lookup; accepted ids are bounded URL-safe tokens matching the service-generated `token_urlsafe` format.
- Report storage serializes chart and section payloads to JSON strings before writing JSONB columns, matching the existing asyncpg/PostgreSQL repository pattern.
- Report retrieval rehydrates hosted reports from mapping-like database rows without depending on dict-only `.get()` behavior.
- Storage readiness verifies that the `natal_reports` PostgreSQL table exists, has the required migration columns, and has the required partial user/date index definition before reports are enabled.
- `scripts/natal_smoke.py` provides a live host smoke check that first verifies natal report storage, then generates a sample exact-time report and verifies report id, hosted URL, storage retrieval, hosted HTML SVG, section anchors, and planets.
- The live smoke check validates that generated report ids are compatible with the hosted route policy and that hosted URLs end with `/reports/natal/<report_id>`.
- `scripts/natal_maintenance.py` applies `NATAL_REPORT_TTL_DAYS` by soft-deleting expired hosted reports through `deleted_at`, so retention can be verified without physically removing audit data.
- `scripts/natal_city_readiness.py` verifies local city catalog warmup, minimum city count, search latency, coordinates, timezone coverage, country-filtered autocomplete narrowing, and same-name city disambiguation for the release smoke city set.
- `scripts/natal_readiness.py --check-config` verifies release-safe natal configuration: reports enabled, positive TTL, local geocoder, raw birth data disabled for LLM prompts, web server enabled, and HTTPS `WEBHOOK_URL`. A failed config check stops storage and smoke checks before they can write reports.
- The release config check also verifies the target Python 3.14+ runtime and required local natal dependencies: `geonamescache>=3.0.1,<4.0.0`, `ephem>=4.1.0,<5.0.0`, and `tzdata>=2024.1`.
- If `NATAL_CITY_OVERRIDES_PATH` is set, the release config check validates that the UTF-8 override file exists and every override entry has valid coordinates and an IANA timezone before live smoke can run.
- Report generation fails closed when `NATAL_REPORTS_ENABLED` is absent or false; the service no longer treats a missing feature flag as enabled.
- The Telegram `/natal` entry point also fails fast when `NATAL_REPORTS_ENABLED=false`, so the bot does not collect birth data while hosted reports are disabled.
- LLM interpretation prompts are built from derived `ChartData` only, and quality warnings are redacted before prompt/JSON serialization if they accidentally contain raw birth date/place fields.
- `scripts/natal_accuracy.py` provides a local golden-case regression check for planets, retrograde flags, Ascendant, MC, and equal-house cusps. Its `--require-external` mode intentionally fails until all golden cases are marked independently verified.
- `scripts/natal_accuracy.py --reference-fixtures <path>` and `scripts/natal_readiness.py --reference-fixtures <path>` can load externally verified UTF-8 JSON fixture cases for full-chart release validation without adding Swiss Ephemeris or another astrology runtime dependency. Externally verified cases must include all 10 planet longitudes, all 10 retrograde flags, `ascendant`, `mc`, and all 12 equal-house cusps; partial fixtures are rejected.
- `scripts/natal_accuracy.py --export-template <path>` writes a current UTF-8 fixture template from the in-code golden cases with all 12 equal-house cusps. Use it as the starting point for external checking, then replace the internal values/source and set `externally_verified=true`.
- `--require-external` now requires an explicit `--reference-fixtures <path>` argument, so public release approval cannot come only from in-code constants.
- `docs/natal-reference-fixture.example.json` provides the required fixture shape. It is intentionally marked `externally_verified=false`; copy it to a release fixture, replace the internal-regression values with independently verified references, update `reference_source`, and only then set `externally_verified=true`.
- `docs/natal-reference-fixture.moira-jpl.json` is the current release fixture. Ascendant, MC, and all 12 equal-house cusps were checked against `moira-astro==3.2.3` `HouseSystem.EQUAL` on Python 3.14; planet longitudes and retrograde flags are checked against NASA/JPL Horizons through `--check-horizons`.
- `scripts/natal_accuracy.py --check-horizons` and `scripts/natal_readiness.py --check-horizons` compare planet ecliptic longitudes and retrograde flags against NASA/JPL Horizons without adding a Swiss Ephemeris runtime dependency.
- `scripts/natal_readiness.py` aggregates city catalog and accuracy checks, with optional JPL Horizons planet validation, storage preflight, config preflight, and explicit `--smoke` live report checks for VPS rollout.
- On local Python 3.14, readiness loaded 32,444 cities in about 721 ms; the slowest checked warm city search took about 44 ms.
- On local Python 3.14, `scripts/natal_accuracy.py --check-horizons` passed 20 NASA/JPL Horizons checks per golden case, with max planet longitude deltas of 0.0704 degrees for `kyiv-1995-exact` and 0.1433 degrees for `reading-1989-exact`.
- Ascendant and MC no longer use the old latitude-independent placeholder formula. They are calculated from local sidereal time, mean obliquity, and ecliptic/horizon or ecliptic/meridian intersections.
- The astronomy math lives in a focused clean-room module with tests for J2000 Julian Day, sidereal time, mean obliquity, and the Kyiv Ascendant/MC reference case.
- Planet retrograde flags are calculated locally from signed ecliptic longitude movement around the chart time instead of being hard-coded to false.
- For unknown birth time, Moon uncertainty is calculated from the local birth date: the report is marked uncertain only when the Moon's sign or Moon aspects can change between the start and end of that local day.
- Release 1 requires birth-time ranges to contain both start and end times, and rejects overnight ranges such as `23:30-01:30` instead of silently calculating the wrong midpoint.
- Exact and approximate birth-time modes require an explicit `HH:MM` value; missing approximate time must use the unknown-time mode instead of falling back to noon with houses enabled.
- Step-by-step time precision accepts only explicit exact/approximate/range/unknown values; unrecognized text no longer defaults to range mode.
- Runtime birth-data resolution also rejects exact/approximate inputs without time values and invalid range inputs, so direct `BirthInput` callers cannot bypass parser validation and fall back to noon.
- Houses use the equal-house system from the calculated Ascendant.
- Deployment forwards `NATAL_*` settings into the `tg-bot` container and runs live natal readiness smoke automatically when `NATAL_REPORTS_ENABLED=true`.
- Empty or unset `NATAL_CITY_OVERRIDES_PATH` is treated as disabled, including CI/SSH environments that pass it through as an empty value resolving to `.`.
- The deploy script uses `set -e`, so failed natal smoke checks now fail the deployment instead of being hidden behind a green workflow.
- After live smoke, deployment runs `scripts/natal_maintenance.py` inside `tg-bot`, verifying the real PostgreSQL storage contract and applying `NATAL_REPORT_TTL_DAYS` soft-deletion on the VPS.

## Current Verification Evidence

- Local Python 3.14 focused suite: `188 passed, 1 warning` for all `tests/test_natal_*.py`.
- Affected existing suite: `43 passed` for `tests/test_horoscope_intent.py`, `tests/test_commands.py`, `tests/test_reader_utils.py`, and `tests/test_web_security.py`.
- Lint: `ruff check app/natal app/handlers/natal_chart.py tests/test_natal_*.py` passed.
- Encoding: `scripts/check_encoding.py` passed after documentation and code changes.
- Release fixture gate: `scripts/natal_accuracy.py --require-external --check-horizons --reference-fixtures docs/natal-reference-fixture.moira-jpl.json` passed. Each case had 34 local fixture checks plus 20 JPL Horizons planet checks.
- Local independent planet gate: `scripts/natal_accuracy.py --check-horizons` passed for both golden cases, with 20 JPL Horizons planet checks per case and max deltas of 0.0704 degrees (`kyiv-1995-exact`) and 0.1433 degrees (`reading-1989-exact`).
- VPS deploy: GitHub Actions run `27117346326` completed successfully for commit `9737103`.
- Docker image packaging: build log includes `RUN test -f /app/docs/natal-reference-fixture.moira-jpl.json`, so the committed release fixture is present in the runtime image.
- VPS live smoke inside `tg-bot`: `PASS natal-city-catalog`, `PASS natal-config: ready`, external Moira/JPL fixture gate, `storage=ready`, generated `smoke_report_id`, generated hosted URL ending in `/reports/natal/<report_id>`, and verified hosted HTML contains SVG and report sections.
- VPS maintenance: deploy now runs `scripts/natal_maintenance.py` after smoke when natal reports are enabled; the latest successful deploy log should contain `OK ttl_days=<NATAL_REPORT_TTL_DAYS> deleted=<count>`.

## Required Before Public Release

1. Done: live report smoke has run on the VPS inside the deployed `tg-bot` container with real `WEBHOOK_URL`, database, Telegram bot token, and `NATAL_REPORTS_ENABLED=true`.
2. Verify the hosted report link opens from Telegram on mobile and desktop.
3. Done by deploy gate when `NATAL_REPORTS_ENABLED=true`: `python scripts/natal_maintenance.py` runs inside `tg-bot` after live smoke and verifies report persistence plus deletion/TTL behavior against the real PostgreSQL migration.
4. Done locally and enforced on VPS deploy: `scripts/natal_accuracy.py --require-external --check-horizons --reference-fixtures docs/natal-reference-fixture.moira-jpl.json` passes with externally verified angle/equal-house references and JPL Horizons planet checks; deploy runs `scripts/natal_readiness.py --check-config --check-storage --require-external --reference-fixtures /app/docs/natal-reference-fixture.moira-jpl.json --smoke --webhook-url "$WEBHOOK_URL" --min-city-count 30000 --max-city-warmup-ms 3000 --max-city-search-ms 300`.
5. Verify city search manually in Telegram for at least these cases: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, Istanbul. Automated local catalog coverage exists. Local Python 3.14 observed timing after prefix indexing is roughly 0.6 s for cold warmup and 35 ms for the slowest release smoke city search.
6. `cities1000` coverage is guarded by a minimum 30,000-city readiness gate. Decide later, from real support requests, whether the product needs a denser dataset for small towns.
7. Decide from support requests whether the "nearest large city" fallback is enough. If not, add reviewed city entries to `NATAL_CITY_OVERRIDES_PATH` using verified coordinates and timezone.
8. Calibrate interpretation prompts with real sample reports and reject overconfident claims when birth time is unknown.
9. Done for release 1 equal-house scope: planet longitudes and retrograde flags have an independent NASA/JPL Horizons gate, and Ascendant/MC/equal-house cusps have the committed Moira/JPL fixture. Do not claim Placidus/Koch/etc. support; release 1 is equal-house only.
10. Keep Swiss Ephemeris as a future optional accuracy upgrade only if AGPL/commercial license obligations are handled explicitly.

## City Data Decision

Use local GeoNames-backed data for the main product path. The important fields for natal charts are latitude, longitude, and timezone. `geonamescache==3.0.1` ships those fields in a `py3-none-any` wheel, so it avoids Python 3.14 native build risk and avoids network latency during normal user interaction.

GeoNames data is distributed under CC BY 4.0. Hosted reports and Telegraph mirrors include attribution to GeoNames.

## Remaining Product Risks

- The current astrology calculator is deterministic and local, but it is not Swiss Ephemeris-grade.
- Planet longitudes and retrograde flags are cross-checked against NASA/JPL Horizons. Ascendant, MC, and equal-house cusps have a committed Moira/JPL release fixture, but this is still not a Swiss Ephemeris parity claim.
- Equal-house cusps are implemented locally; Placidus/Koch/etc. are not.
- City autocomplete in normal Telegram chat is message-by-message, not true live keystroke autocomplete. The step flow reduces noise by asking for country first and filtering city suggestions locally. True per-character updates would require a Telegram Mini App or inline mode.
- The fallback Nominatim geocoder is network-dependent for coordinates and opt-in only via `NATAL_GEOCODER_PROVIDER=nominatim`; timezone resolution remains local.
