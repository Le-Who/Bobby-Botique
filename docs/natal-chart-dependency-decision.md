# Natal Chart Dependency Decision

Chosen library: clean-room local implementation for release 1.

Chosen version constraint: no new production astrology dependency.

License: project-owned code under the repository license. Swiss Ephemeris and pyswisseph were evaluated but are not added to production dependencies in release 1.

Can this be used in a hosted Telegram bot: yes. The release-1 implementation does not depend on Swiss Ephemeris licensing terms. If Swiss Ephemeris/pyswisseph is added later, the open-source hosted bot must comply with AGPL network-use obligations or use a commercial Swiss Ephemeris license for incompatible proprietary deployment.

Do we need a commercial Swiss Ephemeris license: no for the release-1 clean-room implementation. No for an AGPL-compliant open-source Swiss Ephemeris deployment. Yes only for a proprietary or otherwise incompatible Swiss Ephemeris deployment.

Docker/alpine compatibility: the production Dockerfile currently uses `python:3.14-slim`. `pyswisseph>=2.10,<3` and plan-era `timezonefinder` do not have suitable Python 3.14 binary wheels in the verified environment, so adding them would require native build tooling in the image. Release 1 avoids that Docker risk.

Ephemeris data files required: none for release 1.

Fallback if dependency install fails: no release-1 astrology dependency is installed. If a future optional dependency install fails, keep the natal feature disabled via configuration and return a user-facing temporary unavailable message; do not send raw birth data to an LLM as a calculation fallback. Code copied or adapted from AGPL Swiss Ephemeris/pyswisseph remains subject to the original license obligations; clean-room code must be written from public formulas and project-specific tests.
