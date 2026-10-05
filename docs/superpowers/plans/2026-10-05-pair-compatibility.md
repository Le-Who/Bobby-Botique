# Pair compatibility implementation plan

> **For agentic workers:** Apply test-driven-development to the bounded tasks below. The user approved the Mini App design in this chat on 2026-10-05. Implement in the current session; no commit, push or deployment is authorized.

**Goal:** Collect both partners' birth data in one Mini App and use the supplied time/place in compatibility, while retaining date-only input and standalone menu words.

**Architecture:** Preserve public inline sign results and non-sensitive start links. The private start opens a two-partner questionnaire when an HTTPS Mini App base is configured, with the existing chat date flow as an infrastructure fallback. A separate authenticated web boundary parses local-city input, schedules local synastry calculation and sends results to the signed user's private chat. Calculation reuses natal astronomy and exposes uncertainty per partner.

**Tech stack:** Python 3.14, Quart, python-telegram-bot, local ephem/GeoNames, native HTML/CSS/JavaScript; existing locked dependencies.

**Spec:** Approved chat design: one main button opens the form; two dates required, optional time and place per partner, one calculation button, editable confirmation, private result with contextual Tarot. Unknown time never prevents calculation.

## Constraints and review focus

- Preserve UTF-8, authorization, private birth-data handling, raw-input expiry and existing Tarot callbacks.
- Known time requires a catalog city; never guess UTC or silently discard supplied time.
- Unknown time uses possible positions throughout the relevant day; do not present noon positions as exact.
- A known partner's houses/angles remain available when the other time is unknown.
- The signed Telegram identity alone determines the delivery chat; revoked access and invalid payloads fail before scheduling.
- Do not store birth dates, publish public reports, call interpretation providers, add dependencies or migrate a database.
- Standalone menu words must switch out of active input forms without treating them as birth data or ordinary AI messages.

## Task 1: Synastry calculation

Owner: independent calculation agent; files `app/natal/synastry.py`, `tests/test_synastry.py`.

- [x] Write failing tests for cross-chart aspect geometry, wraparound, exact/unknown/mixed precision and missing known-time place.
- [x] Implement `calculate_synastry(first: BirthInput, second: BirthInput) -> SynastryData` with natal charts, sampled possible signs, stable cross-chart aspects and house overlays.
- [x] Verify the focused tests and locked Ruff; inspect the numerical behavior and uncertainty contract.

## Task 2: Private form and delivery

Owner: primary agent; files `app/natal/compatibility_input.py`, `app/natal/compatibility_reading.py`, `app/web_compatibility.py`, `app/templates/compatibility_form.html`, `app/static/js/compatibility-form.js`, `app/static/css/compatibility-form.css`, existing web/handler/i18n/bot singleton registration and dedicated tests.

- [x] Write failing tests for date-only/known-time parsing, malformed input, authenticated delivery and private start keyboard.
- [x] Parse `{pair, first, second}` into validated local-city `BirthInput` values. Resolve places off the async request path.
- [x] Render bounded RU/EN derived readings from the calculation contract, retaining the existing date-only reading when both places are omitted.
- [x] Add form shell and bounded country/city search routes, authenticated submission and tracked background delivery. Share the existing bounded Tarot context store through the running PTB application.
- [x] Build a mobile form with two partner panels and a confirmation panel; share natal theme assets, keep all values in memory, expose unknown time/place explicitly, retain data after validation errors and show acceptance only after server acknowledgement.
- [x] Switch configured private start to one WebApp button and accurate explanatory copy. Keep fallback date collection when no HTTPS base is available.
- [x] Verify focused Python tests and real browser interaction at mobile width, then inspect a screenshot.

## Task 3: Standalone menu words

Owner: independent routing agent; shared `app/handlers/menu_intents.py` and relevant registration/intent tests.

- [x] Test and fix «натальная», «таро», «расклад», «гороскоп», including whitespace/case and active forms.
- [x] Primary agent makes pending compatibility filters yield these words and clears pending raw input on mode switching.
- [x] Verify false positives and appropriate private scope.

## Final verification

- [x] Review integrated changes and address actual regressions.
- [x] Run the repository offline unit/E2E suite, locked Ruff lint/format and mypy.
- [x] Update README with current user-facing behavior; run documentation encoding and `git diff --check`.
- [x] Report local evidence and remaining live Telegram/provider/deployment limitations.
