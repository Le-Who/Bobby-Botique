"""Offline smoke for the exact production image dependency and health surface."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from scripts.dependency_environment_check import main as verify_environment_main


async def check_application_health() -> dict[str, Any]:
    from app.web import quart_app

    response = await quart_app.test_client().get("/health")
    payload = await response.get_json()
    if response.status_code != 200 or not isinstance(payload, dict) or payload.get("status") != "healthy":
        raise RuntimeError(f"offline application health smoke failed: status={response.status_code}, payload={payload}")
    return payload


def main() -> int:
    if verify_environment_main([]) != 0:
        return 1

    # PostgreSQL/Redis behavior is exercised by the disposable service job. The
    # image smoke stays offline and verifies that the real application route can
    # import and render through the production dependency graph.
    from app import web

    web.database.is_database_connected = lambda: True
    payload = asyncio.run(check_application_health())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
