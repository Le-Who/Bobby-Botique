# Natal Chart Dependency Decision

Reviewed against checkout `8fc19516` on 2026-09-08.

## Current implementation

`app/natal/calculator.py` uses **PyEphem (`ephem`)** for planet positions and
retrograde calculations. `app/natal/astronomy.py` implements local angle math;
houses are equal-house cusps from the Ascendant. This is not an entirely
dependency-free clean-room planetary engine.

Production dependencies include `ephem>=4.1.0,<5.0.0`,
`geonamescache>=3.0.1,<4.0.0` and `tzdata>=2024.1`; exact versions belong to
`uv.lock`. The city catalog supplies local coordinates/timezones, avoiding network
geocoding on the normal path. No Swiss Ephemeris, pyswisseph, flatlib, kerykeion or
Moira package is in the production manifest.

## Rationale and limits

Keep the existing modest dependency set and local calculation path. The original
plan to add Swiss-based libraries was not the shipped implementation. Introducing
another engine would require accuracy, Python 3.14 Linux/Windows packaging,
image-size, data-file and licensing review; old install experiments do not prove
current package availability.

The committed `natal-reference-fixture.moira-jpl.json` is independent reference
evidence, not a Moira runtime dependency. It supports the limited equal-house
validation scope; it does not establish Swiss parity or every date/location.
Use [product readiness](natal-chart-product-readiness.md) for release checks.

Do not substitute raw birth-data transmission to an LLM when a local calculation
dependency fails. Keep reports disabled/fail closed until the environment is ready.
The default interpretation contract uses derived chart data.

## Licensing and historical evaluation

The repository's own code is MIT; third-party code/data retain their own terms.
GeoNames attribution is included in hosted reports. Dependency CI produces a
license inventory; review the actual selected packages/data before a distribution
or engine change. This document is not a blanket legal approval for a hosted
deployment or a future Swiss-based implementation.

Earlier notes recorded Python 3.14 wheel/install experiments for
`pyswisseph==2.10.3.2`, `flatlib==0.2.3`, `kerykeion==5.12.9`,
`libephemeris==2.0.2` and dev-only `moira-astro==3.2.3`. Those were historical
candidate evaluations, not current dependency choices or a maintained availability
matrix. Re-run target-platform resolution if revisiting them; do not copy the
plan-era package list into production.
