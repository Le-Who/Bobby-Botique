# Natal Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Telegram flow that collects birth data, calculates a natal chart locally, renders an interactive SVG report on the bot host, uses LLMs only for textual interpretation, and returns a durable report link to the user.

**Architecture:** The natal chart feature is a separate domain package under `app/natal/`, not an extension of the current lightweight horoscope intent. Telegram collects and validates user input, the local calculation service produces a deterministic `ChartData` JSON object and SVG, the LLM receives only derived chart data, and Quart serves the interactive report at `WEBHOOK_URL/reports/natal/<report_id>`. Telegraph is used as a longread mirror and fallback link, not as the primary interactive surface, because Telegraph's HTML node format is constrained.

**Tech Stack:** Python 3.11, python-telegram-bot `ConversationHandler`, Quart, PostgreSQL/Redis where already available, `pydantic`, `timezonefinder`, a geocoder adapter, and either Kerykeion or direct `pyswisseph` after the dependency spike verifies licensing and Docker behavior.

---

## Current Codebase Anchors

- `app/astro.py` currently provides simple transit context with `ephem`; keep it for existing horoscope behavior and do not turn it into the natal engine.
- `app/handlers/horoscope_subscription.py` shows the local style for multi-step Telegram `ConversationHandler` flows.
- `app/intent_router.py` already short-circuits horoscope/weather/currency intents before the generic LLM path; natal intent detection should be explicit and conservative.
- `app/utils/telegraph.py` already creates Telegraph pages from markdown; extend it rather than creating a second HTTP client.
- `app/web_miniapp.py` and `app/web.py` show how Quart routes and templates are organized.
- `app/utils/background_tasks.py`, `app/state.py`, and `app/handlers/messages.py` show the established pattern for user locks, placeholders, and background work.
- `scripts/check_encoding.py` must pass before and after editing docs. Always read/write repository files as UTF-8 on this Windows machine.

## Product Decisions

1. The primary interactive report is hosted by our app using `WEBHOOK_URL`.
2. Telegraph remains useful for Telegram Instant View, public longread sharing, and fallback when our hosted report cannot be opened.
3. Raw birth data should not be sent to LLMs. The LLM receives derived astrological data and confidence flags.
4. Unknown birth time is a first-class mode, not an error.
5. The first release should be deterministic and auditable: local calculation and SVG rendering must work even if all LLM providers fail.

## User Flow

1. User sends `/natal` or a text intent such as `сделай натальную карту`.
2. Bot offers two input modes:
   - `Заполнить пошагово`
   - `Отправить одной таблицей`
3. Bot collects:
   - birth date;
   - birth time precision: exact, approximate, range, unknown;
   - birth time value when available;
   - birth place;
   - report language, default `ru`;
   - focus, default `general`;
   - consent notice.
4. Bot geocodes the place and resolves historical timezone for the birth date.
5. Bot shows a confirmation summary.
6. Bot calculates the chart locally.
7. Bot renders SVG and report sections.
8. Bot asks the configured LLM provider for textual interpretation using the derived chart JSON.
9. Bot stores the report and serves it at `WEBHOOK_URL/reports/natal/<report_id>`.
10. Bot optionally mirrors the longread to Telegraph and sends both links when available.

## Time Precision Rules

Use this behavior exactly:

- `exact`: houses, Ascendant, MC, house placements, and time-sensitive aspects are available.
- `approximate`: houses and angles are shown with a warning; LLM must phrase them as approximate.
- `range`: calculate a midpoint chart and include an uncertainty note. Release 1 does not need multi-chart range comparison.
- `unknown`: calculate planets for local noon. Do not expose Ascendant, MC, or houses as factual. Mark Moon as uncertain when it changes sign or major aspects across the birth date.

## File Structure

- Create `app/natal/__init__.py`
  - Public re-exports for the domain package.
- Create `app/natal/models.py`
  - Pydantic models for `BirthInput`, `ResolvedBirthData`, `ChartData`, `PlanetPosition`, `Aspect`, `House`, `ReportSection`, and `NatalReport`.
- Create `app/natal/parser.py`
  - Parse free-form table input and normalize dates/times/focus/language.
- Create `app/natal/geocoding.py`
  - Resolve place names to coordinates and timezone, with deterministic error types.
- Create `app/natal/calculator.py`
  - Calculate chart data locally through a narrow adapter interface.
- Create `app/natal/svg_renderer.py`
  - Render deterministic SVG with internal anchors and section ids.
- Create `app/natal/report_builder.py`
  - Build hosted report HTML payload and Telegraph-compatible markdown/nodes.
- Create `app/natal/llm.py`
  - Build prompt and call `ProviderRouter` for textual interpretation.
- Create `app/natal/service.py`
  - Orchestrate parse, geocode, calculate, render, interpret, store, and publish.
- Create `app/natal/storage.py`
  - Store report payloads and retrieve them by `report_id`.
- Create `app/handlers/natal_chart.py`
  - Telegram command and conversation handler.
- Modify `app/handlers/commands.py`
  - Register `/natal` and the natal conversation handler.
- Modify `app/handlers/messages.py` or `app/intent_router.py`
  - Route only clear natal chart requests to the natal flow; do not intercept generic horoscope requests.
- Modify `app/web.py` or create `app/web_natal.py`
  - Serve `GET /reports/natal/<report_id>`.
- Modify `app/utils/telegraph.py`
  - Add a safe API for prebuilt Telegraph nodes or a report-specific markdown builder.
- Add `tests/test_natal_parser.py`
- Add `tests/test_natal_geocoding.py`
- Add `tests/test_natal_calculator.py`
- Add `tests/test_natal_svg_renderer.py`
- Add `tests/test_natal_report_builder.py`
- Add `tests/test_natal_service.py`
- Add `tests/test_natal_handler.py`
- Add `tests/test_natal_web_report.py`

---

## Task 1: Dependency Spike And Licensing Decision

**Files:**
- Modify: `requirements.txt`
- Create: `docs/natal-chart-dependency-decision.md`

- [ ] **Step 1: Compare calculation libraries**

Evaluate these options in a throwaway local branch or scratch script:

```text
Option A: Kerykeion
- Pros: astrology-oriented data model, chart concepts, faster feature delivery.
- Cons: less control over every calculation detail, verify SVG/report assumptions.

Option B: pyswisseph directly
- Pros: maximum control, direct Swiss Ephemeris calls, easier to test exact outputs.
- Cons: more astrology plumbing to write: houses, aspects, point mapping, retrograde logic.
```

- [ ] **Step 2: Verify licensing**

Record the exact dependency and license constraints in `docs/natal-chart-dependency-decision.md`.

The document must answer:

```text
Chosen library:
Chosen version constraint:
License:
Can this be used in a hosted Telegram bot:
Do we need a commercial Swiss Ephemeris license:
Docker/alpine compatibility:
Ephemeris data files required:
Fallback if dependency install fails:
```

- [ ] **Step 3: Add dependency constraints**

If Kerykeion is selected, add a pinned-compatible range to `requirements.txt`:

```text
kerykeion>=4.0.0,<5.0.0
timezonefinder>=6.5.0,<8.0.0
```

If direct Swiss Ephemeris is selected, add:

```text
pyswisseph>=2.10.0,<3.0.0
timezonefinder>=6.5.0,<8.0.0
```

- [ ] **Step 4: Verify install**

Run:

```powershell
pip install -r requirements.txt
python -c "import timezonefinder; print('timezonefinder OK')"
```

Expected:

```text
timezonefinder OK
```

Also run the selected astrology import:

```powershell
python -c "import kerykeion; print('kerykeion OK')"
```

or:

```powershell
python -c "import swisseph; print('pyswisseph OK')"
```

- [ ] **Step 5: Commit**

```powershell
git add requirements.txt docs/natal-chart-dependency-decision.md
git commit -m "docs: record natal chart dependency decision"
```

---

## Task 2: Domain Models

**Files:**
- Create: `app/natal/__init__.py`
- Create: `app/natal/models.py`
- Test: `tests/test_natal_models.py`

- [ ] **Step 1: Write model tests**

Create tests for exact and unknown time behavior:

```python
from app.natal.models import BirthInput, TimePrecision


def test_birth_input_exact_time_requires_time_value():
    data = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.EXACT,
        birth_time="06:30",
        birth_place="Kyiv, Ukraine",
        language="ru",
        focus="general",
    )

    assert data.time_precision == TimePrecision.EXACT
    assert data.birth_time == "06:30"


def test_birth_input_unknown_time_accepts_missing_time():
    data = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
        language="ru",
        focus="general",
    )

    assert data.birth_time is None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
pytest tests/test_natal_models.py -v
```

Expected: import failure because `app.natal.models` does not exist.

- [ ] **Step 3: Create models**

Implement these model names and fields:

```python
from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, Field


class TimePrecision(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    RANGE = "range"
    UNKNOWN = "unknown"


class BirthInput(BaseModel):
    birth_date: str
    time_precision: TimePrecision
    birth_time: str | None = None
    birth_time_range_start: str | None = None
    birth_time_range_end: str | None = None
    birth_place: str
    language: str = "ru"
    focus: str = "general"


class ResolvedBirthData(BaseModel):
    birth_input: BirthInput
    latitude: float
    longitude: float
    timezone: str
    local_datetime: str
    utc_datetime: str
    display_place: str


class InputQuality(BaseModel):
    time_precision: TimePrecision
    houses_available: bool
    angles_available: bool
    moon_uncertainty: bool = False
    warnings: list[str] = Field(default_factory=list)


class PlanetPosition(BaseModel):
    key: str
    label: str
    longitude: float
    sign: str
    degree_in_sign: float
    house: int | None = None
    retrograde: bool = False


class Aspect(BaseModel):
    point_a: str
    point_b: str
    aspect: str
    orb: float
    applying: bool | None = None


class House(BaseModel):
    number: int
    cusp_longitude: float
    sign: str


class ChartData(BaseModel):
    input_quality: InputQuality
    planets: list[PlanetPosition]
    aspects: list[Aspect]
    houses: list[House] = Field(default_factory=list)
    angles: dict[str, float] = Field(default_factory=dict)


class ReportSection(BaseModel):
    id: str
    title: str
    body_markdown: str
    chart_refs: list[str] = Field(default_factory=list)


class NatalReport(BaseModel):
    report_id: str
    user_id: int
    chart: ChartData
    svg: str
    sections: list[ReportSection]
    hosted_url: str | None = None
    telegraph_url: str | None = None
```

- [ ] **Step 4: Export public names**

In `app/natal/__init__.py`, export:

```python
from app.natal.models import (
    Aspect,
    BirthInput,
    ChartData,
    House,
    InputQuality,
    NatalReport,
    PlanetPosition,
    ReportSection,
    ResolvedBirthData,
    TimePrecision,
)

__all__ = [
    "Aspect",
    "BirthInput",
    "ChartData",
    "House",
    "InputQuality",
    "NatalReport",
    "PlanetPosition",
    "ReportSection",
    "ResolvedBirthData",
    "TimePrecision",
]
```

- [ ] **Step 5: Run tests**

```powershell
pytest tests/test_natal_models.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/__init__.py app/natal/models.py tests/test_natal_models.py
git commit -m "feat: add natal chart domain models"
```

---

## Task 3: Input Parser

**Files:**
- Create: `app/natal/parser.py`
- Test: `tests/test_natal_parser.py`

- [ ] **Step 1: Write parser tests**

Cover filled table input:

```python
from app.natal.models import TimePrecision
from app.natal.parser import parse_birth_table


def test_parse_exact_time_table_ru():
    parsed = parse_birth_table(
        """
        Дата рождения: 14.02.1995
        Время рождения: точное
        Если точное или примерное: 06:30
        Место рождения: Киев, Украина
        Фокус разбора: отношения
        """
    )

    assert parsed.birth_date == "1995-02-14"
    assert parsed.time_precision == TimePrecision.EXACT
    assert parsed.birth_time == "06:30"
    assert parsed.birth_place == "Киев, Украина"
    assert parsed.focus == "relationships"


def test_parse_unknown_time_table_ru():
    parsed = parse_birth_table(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Место рождения: Kyiv, Ukraine
        """
    )

    assert parsed.time_precision == TimePrecision.UNKNOWN
    assert parsed.birth_time is None
    assert parsed.language == "ru"
    assert parsed.focus == "general"
```

- [ ] **Step 2: Run parser tests to verify failure**

```powershell
pytest tests/test_natal_parser.py -v
```

Expected: import failure because parser is not implemented.

- [ ] **Step 3: Implement parser**

Implement:

```python
def parse_birth_table(text: str) -> BirthInput:
    ...
```

Rules:

- normalize `ДД.ММ.ГГГГ` and `YYYY-MM-DD` to `YYYY-MM-DD`;
- map `точное`, `exact` to `TimePrecision.EXACT`;
- map `примерное`, `approx`, `approximate` to `TimePrecision.APPROXIMATE`;
- map `диапазон`, `range` to `TimePrecision.RANGE`;
- map `неизвестно`, `не знаю`, `unknown` to `TimePrecision.UNKNOWN`;
- map focus values:
  - `общий` -> `general`;
  - `отношения` -> `relationships`;
  - `карьера` -> `career`;
  - `психология` -> `psychology`;
  - `кратко` -> `brief`.

- [ ] **Step 4: Add invalid input tests**

Add tests for missing date, missing place, and exact time without time value:

```python
import pytest

from app.natal.parser import BirthInputParseError, parse_birth_table


def test_parse_requires_birth_place():
    with pytest.raises(BirthInputParseError, match="Место рождения"):
        parse_birth_table("Дата рождения: 1995-02-14\nВремя рождения: неизвестно")


def test_parse_exact_time_requires_time_value():
    with pytest.raises(BirthInputParseError, match="точное время"):
        parse_birth_table(
            "Дата рождения: 1995-02-14\n"
            "Время рождения: точное\n"
            "Место рождения: Kyiv"
        )
```

- [ ] **Step 5: Run parser tests**

```powershell
pytest tests/test_natal_parser.py -v
```

Expected: all parser tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/parser.py tests/test_natal_parser.py
git commit -m "feat: parse natal chart birth input"
```

---

## Task 4: Geocoding And Timezone Resolution

**Files:**
- Create: `app/natal/geocoding.py`
- Test: `tests/test_natal_geocoding.py`

- [ ] **Step 1: Define geocoding interface tests**

Write tests with a fake geocoder so the suite does not call the network:

```python
import pytest

from app.natal.geocoding import GeocodeResult, resolve_birth_data
from app.natal.models import BirthInput, TimePrecision


class FakeGeocoder:
    async def geocode(self, place: str) -> GeocodeResult:
        return GeocodeResult(
            display_name="Kyiv, Ukraine",
            latitude=50.4501,
            longitude=30.5234,
        )


@pytest.mark.asyncio
async def test_resolve_birth_data_unknown_time_uses_local_noon():
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    resolved = await resolve_birth_data(birth, geocoder=FakeGeocoder())

    assert resolved.timezone == "Europe/Kyiv"
    assert "12:00:00" in resolved.local_datetime
    assert resolved.latitude == 50.4501
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_geocoding.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement geocoding module**

Implement:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class GeocodeResult:
    display_name: str
    latitude: float
    longitude: float


class GeocodingError(RuntimeError):
    pass


class GeocoderProtocol(Protocol):
    async def geocode(self, place: str) -> GeocodeResult:
        ...
```

Implement `resolve_birth_data(birth: BirthInput, geocoder: GeocoderProtocol | None = None) -> ResolvedBirthData`.

Use `timezonefinder.TimezoneFinder().timezone_at(lat=..., lng=...)` and Python `zoneinfo.ZoneInfo`.

- [ ] **Step 4: Add production geocoder adapter**

Add a conservative HTTP geocoder adapter:

```python
class NominatimGeocoder:
    async def geocode(self, place: str) -> GeocodeResult:
        ...
```

Requirements:

- use `httpx.AsyncClient`;
- send a clear `User-Agent`, configured from bot name;
- timeout at 8 seconds;
- return `GeocodingError` when no result is found;
- do not log full birth input, only place query on debug level.

- [ ] **Step 5: Run tests**

```powershell
pytest tests/test_natal_geocoding.py -v
```

Expected: all tests pass without network calls.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/geocoding.py tests/test_natal_geocoding.py
git commit -m "feat: resolve natal chart place and timezone"
```

---

## Task 5: Local Chart Calculator

**Files:**
- Create: `app/natal/calculator.py`
- Test: `tests/test_natal_calculator.py`

- [ ] **Step 1: Write calculator contract tests**

Use a fixed Kyiv birth input and assert structural behavior, not fragile exact degrees at first:

```python
import pytest

from app.natal.calculator import calculate_chart
from app.natal.models import BirthInput, ResolvedBirthData, TimePrecision


def resolved_unknown_time() -> ResolvedBirthData:
    return ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="1995-02-14",
            time_precision=TimePrecision.UNKNOWN,
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime="1995-02-14T12:00:00+02:00",
        utc_datetime="1995-02-14T10:00:00+00:00",
        display_place="Kyiv, Ukraine",
    )


@pytest.mark.asyncio
async def test_calculate_unknown_time_disables_houses_and_angles():
    chart = await calculate_chart(resolved_unknown_time())

    assert chart.input_quality.houses_available is False
    assert chart.input_quality.angles_available is False
    assert chart.houses == []
    assert chart.angles == {}
    assert {planet.key for planet in chart.planets} >= {"sun", "moon", "mercury", "venus", "mars"}
```

- [ ] **Step 2: Run calculator tests to verify failure**

```powershell
pytest tests/test_natal_calculator.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement calculator adapter**

Create `calculate_chart(resolved: ResolvedBirthData) -> ChartData`.

Requirements:

- produce Sun, Moon, Mercury, Venus, Mars, Jupiter, Saturn, Uranus, Neptune, Pluto;
- produce sign and degree-in-sign for each planet;
- calculate major aspects only: conjunction, opposition, trine, square, sextile;
- use deterministic orb defaults:
  - Sun/Moon aspects: 8 degrees;
  - other planet aspects: 6 degrees;
  - sextile: 4 degrees;
- set `retrograde` where the library exposes it;
- set houses and angles only when `time_precision` is not `unknown`.

- [ ] **Step 4: Add exact-time test**

Add:

```python
@pytest.mark.asyncio
async def test_calculate_exact_time_includes_houses_and_angles():
    resolved = ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="1995-02-14",
            time_precision=TimePrecision.EXACT,
            birth_time="06:30",
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime="1995-02-14T06:30:00+02:00",
        utc_datetime="1995-02-14T04:30:00+00:00",
        display_place="Kyiv, Ukraine",
    )

    chart = await calculate_chart(resolved)

    assert chart.input_quality.houses_available is True
    assert len(chart.houses) == 12
    assert "ascendant" in chart.angles
    assert "mc" in chart.angles
```

- [ ] **Step 5: Run calculator tests**

```powershell
pytest tests/test_natal_calculator.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/calculator.py tests/test_natal_calculator.py
git commit -m "feat: calculate natal chart locally"
```

---

## Task 6: SVG Renderer

**Files:**
- Create: `app/natal/svg_renderer.py`
- Test: `tests/test_natal_svg_renderer.py`

- [ ] **Step 1: Write SVG tests**

```python
from app.natal.models import ChartData, InputQuality, PlanetPosition, TimePrecision
from app.natal.svg_renderer import render_chart_svg


def test_render_svg_contains_accessible_anchors():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN,
            houses_available=False,
            angles_available=False,
        ),
        planets=[
            PlanetPosition(
                key="sun",
                label="Солнце",
                longitude=325.0,
                sign="Водолей",
                degree_in_sign=25.0,
            )
        ],
        aspects=[],
    )

    svg = render_chart_svg(chart)

    assert svg.startswith("<svg")
    assert 'href="#section-sun"' in svg
    assert "Солнце" in svg
    assert "Время рождения неизвестно" in svg
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_svg_renderer.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement SVG renderer**

Implement:

```python
def render_chart_svg(chart: ChartData) -> str:
    ...
```

Renderer requirements:

- output raw SVG string, not raster image;
- use `viewBox="0 0 800 800"`;
- represent zodiac ring, planet markers, aspect lines, and legend;
- for every planet marker include `<a href="#section-{planet.key}">`;
- for unknown time include visible warning text;
- escape all labels using `html.escape`;
- do not include JavaScript;
- do not depend on browser-only layout measurements.

- [ ] **Step 4: Add SVG sanity tests**

Add tests that reject unsafe output:

```python
def test_render_svg_does_not_emit_script_tags(sample_chart):
    svg = render_chart_svg(sample_chart)

    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()
```

- [ ] **Step 5: Run renderer tests**

```powershell
pytest tests/test_natal_svg_renderer.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/svg_renderer.py tests/test_natal_svg_renderer.py
git commit -m "feat: render interactive natal chart svg"
```

---

## Task 7: LLM Interpretation

**Files:**
- Create: `app/natal/llm.py`
- Test: `tests/test_natal_llm.py`

- [ ] **Step 1: Write prompt privacy tests**

```python
from app.natal.llm import build_interpretation_prompt
from app.natal.models import ChartData, InputQuality, PlanetPosition, TimePrecision


def test_prompt_contains_confidence_rules_and_no_raw_birth_place():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN,
            houses_available=False,
            angles_available=False,
            warnings=["Время рождения неизвестно"],
        ),
        planets=[
            PlanetPosition(
                key="moon",
                label="Луна",
                longitude=120,
                sign="Лев",
                degree_in_sign=0,
            )
        ],
        aspects=[],
    )

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "не трактуй дома" in prompt.lower()
    assert "section-moon" in prompt
    assert "Kyiv" not in prompt
    assert "1995" not in prompt
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_llm.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement prompt builder**

Implement:

```python
def build_interpretation_prompt(chart: ChartData, language: str, focus: str) -> str:
    ...
```

Prompt requirements:

- include chart JSON from `chart.model_dump_json()`;
- require markdown sections with stable ids;
- prohibit fatalistic, medical, financial, or legal certainty;
- require explicit uncertainty wording when `houses_available` is false;
- require each major planet section to use `section-{planet.key}`;
- require concise but deep Russian prose by default.

- [ ] **Step 4: Implement provider call wrapper**

Implement:

```python
async def generate_interpretation(
    chart: ChartData,
    user_id: int,
    chat_id: int,
    language: str = "ru",
    focus: str = "general",
) -> list[ReportSection]:
    ...
```

Use `app.providers.get_provider_router()` or the existing canonical import path. Use configured default/research model from `app.config.settings`.

- [ ] **Step 5: Add parser fallback**

If the LLM returns unusable markdown, return deterministic sections:

```text
section-summary
section-sun
section-moon
section-aspects
```

The fallback must include chart facts and a message that interpretation generation was temporarily unavailable.

- [ ] **Step 6: Run tests**

```powershell
pytest tests/test_natal_llm.py -v
```

Expected: all tests pass with provider calls mocked.

- [ ] **Step 7: Commit**

```powershell
git add app/natal/llm.py tests/test_natal_llm.py
git commit -m "feat: generate natal chart interpretation prompts"
```

---

## Task 8: Report Builder

**Files:**
- Create: `app/natal/report_builder.py`
- Test: `tests/test_natal_report_builder.py`

- [ ] **Step 1: Write report tests**

```python
from app.natal.models import NatalReport, ReportSection
from app.natal.report_builder import build_hosted_report_html, build_telegraph_markdown


def test_hosted_report_contains_svg_and_section_ids(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)

    assert "<svg" in html
    assert 'id="section-sun"' in html
    assert "Натальная карта" in html


def test_telegraph_markdown_links_to_hosted_report(sample_natal_report: NatalReport):
    sample_natal_report.hosted_url = "https://example.com/reports/natal/abc"

    markdown = build_telegraph_markdown(sample_natal_report)

    assert "https://example.com/reports/natal/abc" in markdown
    assert "<svg" not in markdown
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_report_builder.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement hosted HTML builder**

Requirements:

- include `<meta name="viewport" content="width=device-width, initial-scale=1">`;
- inline minimal CSS;
- include SVG at the top;
- render sections with stable `id`;
- include a privacy note;
- include Telegraph link when available;
- escape user-visible section titles and body HTML after markdown conversion.

- [ ] **Step 4: Implement Telegraph markdown builder**

Requirements:

- include title;
- include hosted report link;
- include text sections;
- include a simple planet/aspect table in markdown;
- do not include raw SVG;
- do not include JavaScript.

- [ ] **Step 5: Run tests**

```powershell
pytest tests/test_natal_report_builder.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/report_builder.py tests/test_natal_report_builder.py
git commit -m "feat: build natal chart report content"
```

---

## Task 9: Report Storage

**Files:**
- Create: `app/natal/storage.py`
- Test: `tests/test_natal_storage.py`
- Optional migration: `scripts/migrations/050_add_natal_reports.sql`

- [ ] **Step 1: Choose storage mode**

Use Redis for release 1 if reports may expire after 24 hours. Use PostgreSQL if reports must be durable.

Recommended release 1 choice:

```text
PostgreSQL for report metadata and content, because report URLs should remain usable after Telegram/Telegraph sharing.
```

- [ ] **Step 2: Add migration**

Create `scripts/migrations/050_add_natal_reports.sql`:

```sql
CREATE TABLE IF NOT EXISTS natal_reports (
    report_id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    chart_json JSONB NOT NULL,
    svg TEXT NOT NULL,
    sections_json JSONB NOT NULL,
    hosted_url TEXT,
    telegraph_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_natal_reports_user_created
    ON natal_reports(user_id, created_at DESC)
    WHERE deleted_at IS NULL;
```

- [ ] **Step 3: Write repository tests**

Use the existing repository test patterns under `tests/integration/` if DB integration is available. For unit tests, mock `app.database.db`.

Test cases:

```text
save_report stores chart_json, svg, sections_json, and URLs.
get_report returns None when deleted_at is not null.
delete_report marks deleted_at.
```

- [ ] **Step 4: Implement storage functions**

Implement:

```python
async def save_report(report: NatalReport) -> None:
    ...

async def get_report(report_id: str) -> NatalReport | None:
    ...

async def mark_report_deleted(report_id: str, user_id: int) -> bool:
    ...
```

- [ ] **Step 5: Run tests**

```powershell
pytest tests/test_natal_storage.py -v
```

Expected: all unit tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/storage.py tests/test_natal_storage.py scripts/migrations/050_add_natal_reports.sql
git commit -m "feat: persist natal chart reports"
```

---

## Task 10: Service Orchestration

**Files:**
- Create: `app/natal/service.py`
- Test: `tests/test_natal_service.py`

- [ ] **Step 1: Write orchestration test**

Mock geocoding, calculator, renderer, LLM, storage, and Telegraph:

```python
import pytest

from app.natal.models import BirthInput, TimePrecision
from app.natal.service import create_natal_report


@pytest.mark.asyncio
async def test_create_natal_report_returns_hosted_url(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.report_id
    assert report.hosted_url.startswith("https://bot.example.com/reports/natal/")
    assert report.svg.startswith("<svg")
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_service.py -v
```

Expected: import failure.

- [ ] **Step 3: Implement service**

Implement:

```python
async def create_natal_report(
    birth_input: BirthInput,
    user_id: int,
    chat_id: int,
    webhook_url: str,
) -> NatalReport:
    ...
```

Flow:

1. Resolve birth data.
2. Calculate chart.
3. Render SVG.
4. Generate interpretation sections.
5. Build `report_id` with `secrets.token_urlsafe(16)`.
6. Build hosted URL from `webhook_url.rstrip("/")`.
7. Save report without Telegraph URL first.
8. Try Telegraph publishing.
9. Save report again with Telegraph URL when publishing succeeds.
10. Return `NatalReport`.

- [ ] **Step 4: Add error types**

Expose:

```python
class NatalReportError(RuntimeError):
    pass


class NatalConfigurationError(NatalReportError):
    pass
```

Raise `NatalConfigurationError` when `WEBHOOK_URL` is missing or not HTTPS in production mode.

- [ ] **Step 5: Run tests**

```powershell
pytest tests/test_natal_service.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/natal/service.py tests/test_natal_service.py
git commit -m "feat: orchestrate natal chart report generation"
```

---

## Task 11: Hosted Quart Report Route

**Files:**
- Create: `app/web_natal.py`
- Modify: `app/web.py` or `bot.py` where blueprints/routes are registered
- Test: `tests/test_natal_web_report.py`

- [ ] **Step 1: Write route tests**

Use Quart test client pattern from existing web tests:

```python
import pytest


@pytest.mark.asyncio
async def test_natal_report_route_returns_html(app_client, monkeypatch):
    response = await app_client.get("/reports/natal/test-report-id")

    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "<svg" in body
    assert "Натальная карта" in body
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
pytest tests/test_natal_web_report.py -v
```

Expected: route not found or import failure.

- [ ] **Step 3: Implement route**

Create:

```python
from quart import Blueprint, abort, make_response

natal_bp = Blueprint("natal_reports", __name__)


@natal_bp.get("/reports/natal/<report_id>")
async def natal_report(report_id: str):
    ...
```

Requirements:

- fetch report by `report_id`;
- return 404 when missing or deleted;
- return `text/html; charset=utf-8`;
- set security headers compatible with inline SVG:
  - `X-Content-Type-Options: nosniff`;
  - `Referrer-Policy: no-referrer`;
  - a CSP that permits inline style but not script.

- [ ] **Step 4: Register blueprint**

Follow the current Quart app creation path in `bot.py`/`app/web.py`. Register `natal_bp` once.

- [ ] **Step 5: Run route tests**

```powershell
pytest tests/test_natal_web_report.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app/web_natal.py app/web.py bot.py tests/test_natal_web_report.py
git commit -m "feat: serve hosted natal chart reports"
```

---

## Task 12: Telegraph Integration

**Files:**
- Modify: `app/utils/telegraph.py`
- Test: `tests/test_natal_report_builder.py` or `tests/test_telegraph.py`

- [ ] **Step 1: Add Telegraph unit tests**

Test that natal report publishing calls `createPage` with allowed content only.

Expected allowed tags:

```text
a, aside, b, blockquote, br, code, em, figcaption, figure, h3, h4, hr, i, iframe, img, li, ol, p, pre, s, strong, u, ul, video
```

- [ ] **Step 2: Add public helper**

Add:

```python
async def create_telegraph_page_from_markdown(title: str, markdown_content: str) -> str | None:
    return await create_telegraph_page(title, markdown_content)
```

If direct nodes are required, add:

```python
async def create_telegraph_page_from_nodes(title: str, nodes: list[dict]) -> str | None:
    ...
```

Reuse `_ensure_account()`.

- [ ] **Step 3: Add natal-specific constraints**

Ensure the natal Telegraph content:

- links to hosted interactive report;
- does not include raw SVG;
- does not include JavaScript;
- remains readable if Instant View strips previews.

- [ ] **Step 4: Run tests**

```powershell
pytest tests/test_natal_report_builder.py tests/test_reader_utils.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app/utils/telegraph.py tests/test_natal_report_builder.py
git commit -m "feat: publish natal chart reports to telegraph"
```

---

## Task 13: Telegram Conversation Handler

**Files:**
- Create: `app/handlers/natal_chart.py`
- Modify: `app/handlers/commands.py`
- Test: `tests/test_natal_handler.py`

- [ ] **Step 1: Define conversation states**

Use string states:

```python
NATAL_MODE = "NATAL_MODE"
NATAL_TABLE = "NATAL_TABLE"
NATAL_DATE = "NATAL_DATE"
NATAL_TIME_PRECISION = "NATAL_TIME_PRECISION"
NATAL_TIME_VALUE = "NATAL_TIME_VALUE"
NATAL_PLACE = "NATAL_PLACE"
NATAL_FOCUS = "NATAL_FOCUS"
NATAL_CONFIRM = "NATAL_CONFIRM"
```

- [ ] **Step 2: Write handler tests**

Test:

```text
/natal sends mode selection.
table mode stores parsed BirthInput in context.user_data.
unknown time skips time value prompt.
confirm calls create_natal_report.
cancel clears natal keys from user_data.
```

- [ ] **Step 3: Implement `/natal` entry**

Message text:

```text
Натальная карта строится по дате, месту и, если известно, времени рождения.
Если точного времени нет, я построю карту без домов и асцендента и явно отмечу ограничения.
```

Buttons:

```text
Заполнить пошагово
Отправить таблицей
Отмена
```

- [ ] **Step 4: Implement table template**

Send:

```text
Скопируйте и заполните:

Дата рождения:
Время рождения: точное / примерное / диапазон / неизвестно
Если точное или примерное:
Если диапазон:
Место рождения:
Фокус разбора: общий / отношения / карьера / психология / кратко
```

- [ ] **Step 5: Implement confirmation**

Confirmation must show:

```text
Дата:
Время:
Место:
Фокус:
Ограничения:
```

For unknown time include:

```text
Без точного времени я не буду трактовать дома и асцендент как достоверные.
```

- [ ] **Step 6: Implement generation task**

On confirmation:

1. edit/reply placeholder: `Считаю карту...`;
2. call `create_natal_report`;
3. send hosted link and Telegraph link when available;
4. clear `context.user_data` natal keys;
5. handle errors with actionable messages.

- [ ] **Step 7: Register handler**

In `app/handlers/commands.py`, add:

```python
from app.handlers.natal_chart import build_natal_chart_handler, natal_command

application.add_handler(CommandHandler("natal", natal_command))
application.add_handler(build_natal_chart_handler())
```

Avoid double-registering `/natal` if the `ConversationHandler` already uses `CommandHandler("natal", natal_command)` as an entry point. Choose one pattern and test it.

- [ ] **Step 8: Run handler tests**

```powershell
pytest tests/test_natal_handler.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```powershell
git add app/handlers/natal_chart.py app/handlers/commands.py tests/test_natal_handler.py
git commit -m "feat: add natal chart telegram flow"
```

---

## Task 14: Natal Intent Detection

**Files:**
- Modify: `app/intent_router.py` or `app/handlers/messages.py`
- Test: `tests/test_natal_intent.py`

- [ ] **Step 1: Write conservative intent tests**

```python
from app.natal.intent import is_natal_chart_request


def test_detects_explicit_natal_chart_request():
    assert is_natal_chart_request("сделай мне натальную карту")
    assert is_natal_chart_request("рассчитай birth chart")


def test_does_not_match_generic_horoscope():
    assert not is_natal_chart_request("гороскоп на сегодня для овна")
    assert not is_natal_chart_request("что значит мой знак зодиака")
```

- [ ] **Step 2: Create `app/natal/intent.py`**

Implement regex-based detection:

```python
_NATAL_RE = re.compile(
    r"(?:натальн\w*\s+карт\w*|birth\s+chart|natal\s+chart|астрологическ\w*\s+карт\w*)",
    re.IGNORECASE,
)
```

- [ ] **Step 3: Wire into message router**

Preferred behavior:

- if clear natal intent is detected in a normal text message, reply with the same entry UI as `/natal`;
- do not call the generic LLM for that message;
- do not hijack existing `_HOROSCOPE_PATTERNS` behavior.

- [ ] **Step 4: Run tests**

```powershell
pytest tests/test_natal_intent.py tests/test_horoscope_intent.py -v
```

Expected: natal tests pass and existing horoscope tests still pass.

- [ ] **Step 5: Commit**

```powershell
git add app/natal/intent.py app/handlers/messages.py tests/test_natal_intent.py
git commit -m "feat: route explicit natal chart requests"
```

---

## Task 15: Configuration And Privacy Controls

**Files:**
- Modify: `app/config.py`
- Modify: `README.md`
- Test: `tests/test_config_helpers.py` or new `tests/test_natal_config.py`

- [ ] **Step 1: Add settings**

Add settings:

```python
NATAL_REPORTS_ENABLED: bool = True
NATAL_REPORT_TTL_DAYS: int = 365
NATAL_GEOCODER_PROVIDER: str = "nominatim"
NATAL_SEND_RAW_BIRTH_DATA_TO_LLM: bool = False
```

- [ ] **Step 2: Test defaults**

Add tests asserting defaults are production-safe:

```python
def test_natal_privacy_defaults_do_not_send_raw_birth_data(settings):
    assert settings.NATAL_SEND_RAW_BIRTH_DATA_TO_LLM is False
```

- [ ] **Step 3: Document environment variables**

In `README.md`, add rows for the new settings. Before editing and after editing, run:

```powershell
python scripts/check_encoding.py
```

- [ ] **Step 4: Run tests and encoding check**

```powershell
pytest tests/test_natal_config.py -v
python scripts/check_encoding.py
```

Expected: tests pass and encoding check exits 0.

- [ ] **Step 5: Commit**

```powershell
git add app/config.py README.md tests/test_natal_config.py
git commit -m "feat: configure natal chart privacy controls"
```

---

## Task 16: End-To-End Verification

**Files:**
- Test: multiple tests above
- Manual: local bot or Quart route

- [ ] **Step 1: Run focused test suite**

```powershell
pytest tests/test_natal_*.py -v
```

Expected: all natal tests pass.

- [ ] **Step 2: Run affected existing tests**

```powershell
pytest tests/test_horoscope_intent.py tests/test_commands.py tests/test_reader_utils.py tests/test_web_security.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Run lint**

```powershell
ruff check app/natal app/handlers/natal_chart.py tests/test_natal_*.py
```

Expected: no lint errors.

- [ ] **Step 4: Run encoding check**

```powershell
python scripts/check_encoding.py
```

Expected: exits 0.

- [ ] **Step 5: Manual local route check**

Start the app in the normal development mode used by this project. Generate a report and open:

```text
<WEBHOOK_URL>/reports/natal/<report_id>
```

Verify:

- SVG is visible;
- clicking a planet number jumps to the matching section;
- unknown time warning is visible when time is unknown;
- Telegraph link opens when publishing succeeds;
- report remains readable without Telegraph.

- [ ] **Step 6: Commit final verification changes**

```powershell
git status --short
git add .
git commit -m "test: verify natal chart report flow"
```

---

## Rollout Plan

1. Ship behind `NATAL_REPORTS_ENABLED=false` in production until smoke-tested.
2. Enable for admin user IDs first.
3. Generate at least three manual charts:
   - exact time;
   - unknown time;
   - invalid place followed by corrected place.
4. Check logs for:
   - geocoder failures;
   - LLM fallback usage;
   - Telegraph publishing failures;
   - report route 404s.
5. Enable for all authorized users after one day without critical failures.

## Operational Risks And Mitigations

- **Swiss Ephemeris licensing risk:** resolve in Task 1 before implementation. Do not merge calculation dependency until the license decision is documented.
- **Telegraph interactivity risk:** primary interactive report is hosted on `WEBHOOK_URL`; Telegraph is only a mirror/fallback.
- **Unknown time accuracy risk:** houses and angles are disabled unless time is known. The report must state the limitation.
- **Privacy risk:** raw birth date/place is not sent to LLM by default. The prompt tests enforce this.
- **Geocoding availability risk:** geocoder failures return a correction prompt instead of falling into generic LLM chat.
- **Long LLM response risk:** final result is a report link, not Telegram chunks.
- **Report abuse risk:** random `report_id`, deletion endpoint, and optional TTL reduce exposure.

## Definition Of Done

- `/natal` works in private Telegram chat.
- Explicit natal chart text intent opens the same flow.
- User can complete the flow with exact time.
- User can complete the flow with unknown time.
- Local calculation produces `ChartData` without LLM access.
- SVG is rendered as SVG and contains clickable anchors on the hosted page.
- Hosted report is served from `WEBHOOK_URL/reports/natal/<report_id>`.
- Telegraph mirror is created when Telegraph is available.
- LLM receives derived chart data, not raw birth date/place, with default privacy settings.
- Existing horoscope intent tests still pass.
- `pytest tests/test_natal_*.py -v` passes.
- `python scripts/check_encoding.py` passes.
