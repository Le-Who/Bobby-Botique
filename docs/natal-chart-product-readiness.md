# Natal Chart Product Readiness

## Status

Current state: beta-ready for controlled testing, not a fully finished public product.

This document tracks the changes required to move natal charts from MVP to product-ready. It also records which parts are already implemented so rollout decisions do not depend on memory.

Release-gate contract reviewed on 2026-10-02 against the working tree. The
implementation uses PyEphem for planets and local angle/equal-house math; see the
[dependency decision](natal-chart-dependency-decision.md). Repository changes and
local tests do not establish the deployed VPS state. Historical benchmark, test
and VPS results below do not approve a new rollout.

## Implemented paths and historical observations

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
- Hosted reports and opt-in Telegraph mirrors include GeoNames / CC BY 4.0 city-data attribution.
- Hosted report rendering strips `javascript:` links from interpretation body HTML, sanitizes stored SVG payloads before rendering, removes SVG event-handler attributes, and renders Telegraph mirror links only when they are HTTPS URLs.
- Telegraph publication is disabled by default and requires `TELEGRAPH_PUBLICATION_ENABLED=true`. Report generation stores mirror URLs only when the publisher returns a canonical `https://telegra.ph` URL, so unsafe mirrors are filtered before persistence as well as before rendering.
- Hosted report routes are covered for 200/404 behavior and security headers: `nosniff`, `no-referrer`, framing restrictions and a nonce-based CSP for the bundled report interaction script.
- Hosted report routes reject malformed report ids before storage lookup; accepted ids are bounded URL-safe tokens matching the service-generated `token_urlsafe` format.
- Report storage serializes chart and section payloads to JSON strings before writing JSONB columns, matching the existing asyncpg/PostgreSQL repository pattern.
- Report retrieval rehydrates hosted reports from mapping-like database rows without depending on dict-only `.get()` behavior.
- Storage readiness verifies that the `natal_reports` PostgreSQL table exists, has the required migration columns, and has the required partial user/date index definition before reports are enabled.
- `scripts/natal_smoke.py` provides a host smoke check that first verifies natal report storage, then generates a sample exact-time natal report and verifies report id, hosted URL, storage retrieval, rendered HTML SVG, section anchors, and planets. It builds hosted HTML locally from the stored report; it does not request the public HTTPS route.
- The live smoke check validates that generated report ids are compatible with the hosted route policy and that hosted URLs end with `/reports/natal/<report_id>`.
- `scripts/natal_maintenance.py` applies `NATAL_REPORT_TTL_DAYS` by soft-deleting expired hosted reports through `deleted_at`, so retention can be verified without physically removing audit data.
- `scripts/natal_city_readiness.py` verifies local city catalog warmup, minimum city count, search latency, coordinates, timezone coverage, country-filtered autocomplete narrowing, and same-name city disambiguation for the release smoke city set.
- `scripts/natal_readiness.py --check-config` verifies release-safe natal configuration: reports enabled, positive TTL, local geocoder, raw birth data disabled for LLM prompts, web server enabled, and HTTPS `WEBHOOK_URL`. Failed config, city, accuracy or requested external-reference checks stop before DB pool initialization, storage checks and report smoke.
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
- `docs/natal-reference-fixture.moira-jpl.json` is the current offline release fixture. Ascendant, MC, and all 12 equal-house cusps retain the independent `moira-astro==3.2.3` `HouseSystem.EQUAL` check on Python 3.14. Planet longitudes and retrograde flags were independently refreshed from NASA/JPL Horizons on 2026-10-02; each case records the reference method and verification status.
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
- Deployment forwards `NATAL_*` settings into the `tg-bot` container and defines the release gate below for candidates with `NATAL_REPORTS_ENABLED=true`.
- Empty or unset `NATAL_CITY_OVERRIDES_PATH` is treated as disabled, including CI/SSH environments that pass it through as an empty value resolving to `.`.

## Current Release Gate Contract

After the candidate passes `/health`, `.github/workflows/deploy.yml` runs this
command inside the candidate `tg-bot` container when `NATAL_REPORTS_ENABLED=true`:

```bash
python /app/scripts/natal_readiness.py \
  --check-config \
  --check-storage \
  --require-external \
  --reference-fixtures /app/docs/natal-reference-fixture.moira-jpl.json \
  --smoke \
  --webhook-url "$WEBHOOK_URL" \
  --min-city-count 30000 \
  --max-city-warmup-ms 3000 \
  --max-city-search-ms 300
```

Configuration, city catalog and offline accuracy gates must all pass before the
script opens a DB pool, checks storage or generates a report. This rejects missing
or unverified external cases as well as failing numerical references. The gate
does not call NASA/JPL: `--check-horizons` is a separate explicit network check for
reference refreshes or an astronomy release review.

Smoke uses the configured positive `ADMIN_ID`, verifies that it already exists in
`users`, and never provisions a user. `--user-id` can select another existing user
for a manually authorized check. It invokes report generation and stores a
synthetic report, which remains subject to the normal TTL and user deletion
policy. Provider calls and an opt-in Telegraph mirror are possible; raw birth
data must remain disabled in LLM prompts, and `TELEGRAPH_PUBLICATION_ENABLED`
remains a separate default-false opt-in. Smoke output includes a report URL, so
keep the output restricted with the other deployment diagnostics.

After readiness passes, deploy runs `python /app/scripts/natal_maintenance.py`,
which checks the storage contract and soft-deletes expired reports according to
`NATAL_REPORT_TTL_DAYS`. Either command failing invokes
`handle_candidate_failure` before `tg-bot-previous` can be removed. A failed
candidate is stopped and removed; the previous container is restored
automatically only under the existing verified dependency-only rollback policy.
For code or schema changes it stays preserved and stopped for manual recovery.
The candidate already runs during these checks; this gate protects release
acceptance and retention of the previous container, not zero exposure to the
candidate before verification.

Successful smoke proves report generation, storage readback and HTML rendering
for one exact-time natal case. Public DNS/TLS/reverse-proxy delivery, the Telegram
link, chart interactions, matrix/combined modes, unknown-time flow and
compatibility callbacks still need their own controlled checks. The scheduled
`natal_smoke.yml` workflow can repeat readiness on the current container; it does
not perform a candidate transition or provide deploy rollback.

## Read-only Deployment Inspection — 2026-10-02

The current public address supplied for this check was
`https://gemaitest.tri.mom/`. Between 17:23:56 and 17:25:15 UTC, verified HTTPS
requests returned:

| Request | Observed result |
|---|---|
| `/health` | HTTP 200; `status=healthy`, database and Redis reported connected. |
| `/webapp/natal-form` | HTTP 200; the form used the previous date/time/place/report/review order. |
| `/reports/natal/invalid-readonly-probe` | HTTP 404 with `Referrer-Policy: no-referrer`; no real report was accessed. |
| `/` | HTTP 302 to `/login`; the dashboard required authentication. |
| `/static/js/natal-report.js` | HTTP 404; the new standalone report script was not served. |

The live form lacked the new birth-time validation and matrix-specific time-block
markers present in the working tree. These public code markers show that the new
form changes were not served at inspection time; they do not establish the exact
container image SHA. The latest available
[CI run](https://github.com/Le-Who/Bobby-Botique/actions/runs/36460250182) and
[deploy run](https://github.com/Le-Who/Bobby-Botique/actions/runs/36460558862)
both succeeded on 2026-09-28 for commit
`25e54f8a0401eeb21e2175e986476d6e9124ec17`, before these uncommitted fixes.

No candidate transition, report smoke, maintenance, form submission or admin
change was performed during this inspection. `/health` does not exercise
Telegram or providers, and its `bot=running` field is static. Real Telegram
delivery, provider calls, generation and opening of an existing report remain
unverified for this inspection. The new release gate above still needs to run
for the actual candidate when it is deployed.

## Working-tree Offline Verification — 2026-10-02

After the compatibility privacy, Tarot hydration/delivery and deploy-gate fixes,
the complete offline suite passed with **3,691 passed, 28 skipped and 2
deselected**:

```bash
uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" \
  --override-ini="addopts=" --timeout=30
```

Locked Ruff lint/format checks and `mypy app bot.py` passed. Documentation
encoding (116 Markdown files), link validation (26 documents, zero errors) and
`git diff --check` passed. Isolated database integration tests and live
Telegram/provider scenarios were not run. These results concern the uncommitted
working tree, not the September deployment or a new production candidate.

## Historical Verification Evidence

These are retained reports from prior work, including deployment commit `9737103`.
They are not results for the currently checked-out commit. Re-run the appropriate
gates in the intended environment before treating any item as current evidence.

- Local Python 3.14 focused suite: `190 passed, 1 warning` for all `tests/test_natal_*.py`.
- Telegram handler-level flow tests cover step-by-step exact-time and unknown-time paths from mode/date/country/city selection through final hosted report URL reply, with city coordinates/timezone embedded before report generation.
- Affected existing suite: `43 passed` for `tests/test_horoscope_intent.py`, `tests/test_commands.py`, `tests/test_reader_utils.py`, and `tests/test_web_security.py`.
- Lint: `ruff check app/natal app/handlers/natal_chart.py tests/test_natal_*.py` passed.
- Encoding: `scripts/check_encoding.py` passed after documentation and code changes.
- Release fixture gate: `scripts/natal_accuracy.py --require-external --check-horizons --reference-fixtures docs/natal-reference-fixture.moira-jpl.json` passed. Each case had 34 local fixture checks plus 20 JPL Horizons planet checks.
- Local independent planet gate: `scripts/natal_accuracy.py --check-horizons` passed for both golden cases, with 20 JPL Horizons planet checks per case and max deltas of 0.0704 degrees (`kyiv-1995-exact`) and 0.1433 degrees (`reading-1989-exact`).
- VPS deploy: GitHub Actions run `27117346326` completed successfully for commit `9737103`.
- Docker image packaging: build log includes `RUN test -f /app/docs/natal-reference-fixture.moira-jpl.json`, so the committed release fixture is present in the runtime image.
- VPS live smoke inside `tg-bot`: `PASS natal-city-catalog`, `PASS natal-config: ready`, external Moira/JPL fixture gate, `storage=ready`, generated `smoke_report_id`, generated hosted URL ending in `/reports/natal/<report_id>`, and verified hosted HTML contains SVG and report sections.
- Earlier VPS maintenance was reported after smoke. For a fresh rollout, verify `OK ttl_days=<NATAL_REPORT_TTL_DAYS> deleted=<count>` for the actual candidate, without treating this historical record as a pass.

## Required Before Public Release

Every new rollout needs evidence for its actual candidate. Local offline
reference validation can run without `--check-storage` or `--smoke`; live smoke
and maintenance require an authorized rollout window because they write data.

1. Verify the exact image/release SHA and safe natal configuration for the intended VPS candidate. Supply a registered smoke user and make the external fixture available inside that image.
2. Run the current release gate and maintenance in the authorized candidate transition; inspect the result before accepting public release. A previous successful GitHub run does not validate a new image or changed environment.
3. Verify the hosted report link opens from Telegram on mobile and desktop, including the current chart navigation and focus interactions. Smoke does not exercise this public route or browser behavior.
4. Verify matrix-only and combined Mini App modes, unknown-time natal input, a fresh `/natal` entry after switching modes, and private compatibility date/tarot callbacks. The automated deploy smoke covers only an exact-time natal report. Check feature-disabled input paths and absence of raw birth data in provider prompts as part of these checks.
5. Verify city search manually in Telegram for at least these cases: Odesa/Odessa, Kyiv/Kiev, Moscow, London, New York, Ottawa, Orenburg, Berlin, Warsaw, Istanbul. Automated catalog coverage exists; historical timings are not current VPS performance evidence.
6. `cities1000` coverage is guarded by a minimum 30,000-city readiness gate. Decide later, from real support requests, whether the product needs a denser dataset for small towns.
7. Decide from support requests whether the "nearest large city" fallback is enough. If not, add reviewed city entries to `NATAL_CITY_OVERRIDES_PATH` using verified coordinates and timezone.
8. Calibrate interpretation prompts with real sample reports and reject overconfident claims when birth time is unknown.
9. For an astronomy change, independently recheck the reference methods and run the explicit NASA/JPL Horizons comparison in an authorized network check. The deploy gate uses the committed Moira/JPL fixture offline. Do not claim Placidus/Koch/etc. support; release 1 is equal-house only.
10. Keep Swiss Ephemeris as a future optional accuracy upgrade only if AGPL/commercial license obligations are handled explicitly.

## City Data Decision

Use local GeoNames-backed data for the main product path. The important fields
are latitude, longitude and timezone. The manifest accepts
`geonamescache>=3.0.1,<4.0.0`; the lockfile fixes the installed version. Local
lookup avoids network geocoding during normal interaction. Earlier timings and
package versions in this record are historical, not current installation evidence.

GeoNames data is distributed under CC BY 4.0. Natal and combined hosted reports
and Telegraph mirrors include attribution to GeoNames when they use city data;
matrix-only reports need no birthplace lookup.

## Remaining Product Risks

- The current astrology calculator is deterministic and local, but it is not Swiss Ephemeris-grade.
- Planet longitudes and retrograde flags are cross-checked against NASA/JPL Horizons. Ascendant, MC, and equal-house cusps have a committed Moira/JPL release fixture, but this is still not a Swiss Ephemeris parity claim.
- Equal-house cusps are implemented locally; Placidus/Koch/etc. are not.
- City autocomplete in normal Telegram chat is message-by-message, not true live keystroke autocomplete. The step flow reduces noise by asking for country first and filtering city suggestions locally. True per-character updates would require a Telegram Mini App or inline mode.
- The fallback Nominatim geocoder is network-dependent for coordinates and opt-in only via `NATAL_GEOCODER_PROVIDER=nominatim`; timezone resolution remains local.
