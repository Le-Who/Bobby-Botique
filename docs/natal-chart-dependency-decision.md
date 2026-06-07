# Natal Chart Dependency Decision

Chosen library: clean-room local implementation for release 1.

Chosen version constraint: no new production astrology dependency.

License: project-owned code under the repository license. Swiss Ephemeris and pyswisseph were evaluated but are not added to production dependencies in release 1.

Can this be used in a hosted Telegram bot: yes. The release-1 implementation does not depend on Swiss Ephemeris licensing terms. If Swiss Ephemeris/pyswisseph is added later, the open-source hosted bot must comply with AGPL network-use obligations or use a commercial Swiss Ephemeris license for incompatible proprietary deployment.

Do we need a commercial Swiss Ephemeris license: no for the release-1 clean-room implementation. No for an AGPL-compliant open-source Swiss Ephemeris deployment. Yes only for a proprietary or otherwise incompatible Swiss Ephemeris deployment.

Docker/alpine compatibility: the production Dockerfile currently uses `python:3.14-slim`. `pyswisseph>=2.10,<3` and plan-era `timezonefinder` do not have suitable Python 3.14 binary wheels in the verified environment, so adding them would require native build tooling in the image. On `C:\Python314`, `pip download pyswisseph==2.10.3.2 --only-binary=:all:` found no matching binary distribution, and `pip install pyswisseph==2.10.3.2` fell back to source build and failed without Microsoft C++ Build Tools. Release 1 avoids that Docker risk.

Ephemeris data files required: none for release 1.

Fallback if dependency install fails: no release-1 astrology dependency is installed. If a future optional dependency install fails, keep the natal feature disabled via configuration and return a user-facing temporary unavailable message; do not send raw birth data to an LLM as a calculation fallback. Code copied or adapted from AGPL Swiss Ephemeris/pyswisseph remains subject to the original license obligations; clean-room code must be written from public formulas and project-specific tests.

## Alternative Libraries Checked

- `flatlib==0.2.3`: pure-Python package, but runtime dependency is pinned to `pyswisseph==2.08.00-1`; it does not remove the Swiss/native-build dependency.
- `kerykeion==5.12.9`: AGPL-3.0 package and runtime dependency includes `pyswisseph>=2.10.3.2`; it does not remove the Swiss/native-build dependency.
- `libephemeris==2.0.2`: `py3-none-any`, no runtime Swiss dependency in metadata. It depends on Skyfield/Skyfield-data/Astroquery/Astropy/Numpy/Pyerfa/Zstandard and downloaded roughly 60+ MB of wheels in the Python 3.14 check. This is a plausible future accuracy engine, but it is a larger integration and packaging change than the release-1 local calculator.
- `moira-astro==3.2.3`: has a Windows `cp314` wheel, but `pip download --platform manylinux_2_28_x86_64 --implementation cp --python-version 314 --abi cp314` did not find `3.2.3`. Treat as Docker build risk until Linux Python 3.14 wheel availability is verified for the production image platform.
