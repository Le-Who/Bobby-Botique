"""Evaluate synthetic intent/thinking cases offline; never start the bot.

Run: uv run --locked python scripts/evaluate_intent_routing.py --output <temporary-json>
Reports contain case IDs and fixed diagnostics, never input/history/prompt text.
Latency covers local routing with fixture boundaries, not provider or Telegram latency.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import io
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "tests" / "fixtures" / "intent_routing_cases.json"
DIRECT_ROUTES = {"weather", "currency", "crypto", "horoscope"}
ROUTES = DIRECT_ROUTES | {"llm", "speech_inline", "speech_reply", "speech_forwarded", "speech_clarify"}
REQUIRED_FIELDS = {
    "case_id",
    "group",
    "language",
    "input",
    "history",
    "explicit_thinking",
    "expected_route",
    "allowed_fallback",
    "expected_thinking",
    "forbidden_side_effect",
}
SAFE_ENV = {
    "TESTING": "true",
    "TELEGRAM_BOT_TOKEN": "1234567890:synthetic-offline-token",
    "ADMIN_SECRET": "synthetic-offline-admin-secret",
    "ADMIN_ID": "1",
    "DATABASE_URL": "postgresql://synthetic:synthetic@localhost:1/offline",
    "REDIS_URL": "",
    "GEMINI_API_KEYS": "synthetic-offline-key",
    "LOG_CONTENT_MODE": "metadata",
}


def validate_cases(cases: list[dict[str, Any]]) -> None:
    """Reject ambiguous fixture contracts before any application import."""
    seen: set[str] = set()
    for case in cases:
        if REQUIRED_FIELDS - case.keys():
            raise ValueError("Incomplete synthetic case schema")
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("Case IDs must be nonempty and unique")
        seen.add(case_id)
        if case["expected_route"] not in ROUTES or any(route not in ROUTES for route in case["allowed_fallback"]):
            raise ValueError("Unknown expected route or fallback")
        if not isinstance(case["input"], str) or not isinstance(case["history"], list):
            raise ValueError("Synthetic input/history must have explicit types")
        if case["explicit_thinking"] not in (None, "auto", "off", "low", "medium", "high"):
            raise ValueError("Unknown explicit thinking setting")
        if case["expected_thinking"] not in ("off", "low", "medium", "high"):
            raise ValueError("Unknown expected thinking level")
        if not isinstance(case["forbidden_side_effect"], list) or not case["forbidden_side_effect"]:
            raise ValueError("Every case needs forbidden side effects")
        if case.get("fixture_scenario", "available") not in {"available", "unavailable", "malformed"}:
            raise ValueError("Unknown fixture response scenario")
        if case.get("classifier_reply", "NO") not in {"YES", "NO"}:
            raise ValueError("Voice classifier fixtures must be YES or NO")


def _sensitive_path(path: Path) -> bool:
    for name in (path.name.lower(), path.resolve().name.lower()):
        if name == ".env" or name.startswith(".env.") or ("prompt" in name and not name.endswith((".py", ".pyc"))):
            return True
    return False


def load_cases(path: Path = DEFAULT_CORPUS) -> list[dict[str, Any]]:
    if _sensitive_path(path):
        raise ValueError("Sensitive files cannot be used as a synthetic corpus")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("synthetic_only") is not True:
        raise ValueError("Only explicitly synthetic schema-v1 corpora are accepted")
    cases = data["cases"]
    validate_cases(cases)
    return cases


class OfflineViolation(RuntimeError):
    """An attempted external boundary failed closed; details remain content-free."""


class FixtureBoundaries:
    """Replace clients/readers, leaving routing, extraction and response parsers real."""

    def __init__(self) -> None:
        self.case: dict[str, Any] = {}
        self.requests: list[dict[str, str]] = []
        self.violations: list[str] = []

    def deny(self, boundary: str):
        def blocked(*args, **kwargs):
            self.violations.append(boundary)
            raise OfflineViolation(boundary)

        return blocked

    async def get(self, url: str, *, params=None, **kwargs):
        import httpx

        parsed = httpx.URL(url, params=params)
        query = dict(parsed.params)
        scenario = self.case.get("fixture_scenario", "available")
        if parsed.host == "api.weatherapi.com" and parsed.path == "/v1/current.json":
            self.requests.append({"kind": "weather"})
            # Only literal synthetic cities resolve. Unknown/ambiguous candidates
            # receive no location; this does not claim the router disambiguates cities.
            known_cities = {"лондоне", "лондон", "london", "париже", "париж", "paris", "киеве", "киев", "kyiv"}
            if query.get("q", "").casefold() in known_cities:
                data = {
                    "location": {"name": "Synthetic city", "country": "Fixture"},
                    "current": {
                        "condition": {"text": "Fixture", "code": 1000},
                        "temp_c": 12,
                        "feelslike_c": 11,
                        "humidity": 60,
                        "wind_kph": 4,
                        "is_day": 1,
                    },
                }
            else:
                data = {}
        elif parsed.host == "geocoding-api.open-meteo.com" and parsed.path == "/v1/search":
            self.requests.append({"kind": "geocoding"})
            data = {"results": []}
        elif parsed.host == "v6.exchangerate-api.com" and "/pair/" in parsed.path:
            base, target = parsed.path.rsplit("/", 2)[-2:]
            self.requests.append({"kind": "currency", "base": base, "target": target})
            data = {"result": "success", "conversion_rate": 1.25, "time_last_update_utc": "fixture"}
        elif parsed.host == "api.frankfurter.dev" and parsed.path == "/v1/latest":
            self.requests.append({"kind": "currency_fallback", "base": query["from"], "target": query["to"]})
            data = {"rates": {query["to"]: 1.25}, "date": "fixture"}
        elif parsed.host == "api.coingecko.com" and parsed.path == "/api/v3/simple/price":
            self.requests.append({"kind": "crypto", "coin": query["ids"]})
            data = {query["ids"]: {"usd": 120, "rub": 10000, "usd_24h_change": 1.5}}
        else:
            return self.deny("unexpected_fixture_request")()
        if scenario == "malformed":
            data = {}
        return httpx.Response(503 if scenario == "unavailable" else 200, json=data, request=httpx.Request("GET", url))

    async def stream(self, request):
        from app.providers.stream_types import (
            FinishReason,
            GroundingReport,
            ProviderKind,
            RouteUsed,
            StreamCompleted,
            TextDelta,
            TokenUsage,
        )

        self.requests.append({"kind": "horoscope_generation"})
        if self.case.get("fixture_scenario", "available") != "available":
            return
        yield TextDelta("Synthetic horoscope fixture.")
        yield StreamCompleted(
            finish_reason=FinishReason.from_raw("STOP"),
            usage=TokenUsage(total=1),
            grounding=GroundingReport(),
            route=RouteUsed(ProviderKind.GEMINI, request.models[0], request.models[0]),
        )

    async def classify_voice(self, *args, **kwargs):
        self.requests.append({"kind": "voice_classifier"})
        return self.case.get("classifier_reply", "NO"), 1


@contextmanager
def offline_boundaries():
    """Block network, process execution, sensitive files and persistent readers.

    The guards are process-wide and evaluation runs serially. Blocked attempts are
    recorded even when a production fallback catches the exception.
    """
    fixture = FixtureBoundaries()
    with ExitStack() as stack:
        system_environment = {
            key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ
        }
        stack.enter_context(patch.dict(os.environ, {**system_environment, **SAFE_ENV}, clear=True))
        # Windows platform discovery can shell out to `ver` during dependency
        # imports. Supply metadata so even that subprocess remains unnecessary.
        stack.enter_context(patch("platform.win32_ver", lambda *args, **kwargs: ("10", "10.0.0", "", "")))
        for target, boundary in (
            ("socket.socket.connect", "network"),
            ("socket.socket.connect_ex", "network"),
            ("socket.socket.sendto", "network"),
            ("socket.create_connection", "network"),
            ("socket.getaddrinfo", "network"),
            ("socket.gethostbyname", "network"),
            ("socket.gethostbyname_ex", "network"),
            ("socket.gethostbyaddr", "network"),
            ("subprocess.Popen", "tool_execution"),
            ("os.system", "tool_execution"),
            ("os.posix_spawn", "tool_execution"),
        ):
            if target != "os.posix_spawn" or hasattr(os, "posix_spawn"):
                stack.enter_context(patch(target, fixture.deny(boundary)))
        # Proactor/uvloop transports can connect without socket.connect().
        loop = asyncio.get_running_loop()
        for method in ("create_connection", "create_datagram_endpoint", "sock_connect", "sock_sendto"):
            stack.enter_context(patch.object(loop, method, fixture.deny("network")))
        for module in (builtins, io):
            original_open = module.open

            def guarded_open(file, *args, _original=original_open, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)) and _sensitive_path(Path(os.fsdecode(file))):
                    return fixture.deny("sensitive_read")()
                return _original(file, *args, **kwargs)

            stack.enter_context(patch.object(module, "open", guarded_open))
        # Import only after inherited configuration and all socket/process access
        # have been isolated. No dotenv loader is used by this command.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import httpx

        from app import intent_router, voice_intent
        from app.runtime_settings.store import SettingsSnapshot

        for target in ("httpx.AsyncClient.send", "httpx.Client.send", "google.genai.Client", "urllib.request.urlopen"):
            stack.enter_context(patch(target, fixture.deny("network")))
        for target in (
            "asyncpg.connect",
            "asyncpg.create_pool",
            "app.database.db_manager.query",
            "app.database.db_manager.execute_many",
        ):
            stack.enter_context(patch(target, fixture.deny("storage")))
        stack.enter_context(patch("app.repos.provider_keys.get_provider_key", AsyncMock(return_value="synthetic-key")))
        stack.enter_context(
            patch("app.runtime_settings.lifecycle.operation_snapshot", AsyncMock(return_value=SettingsSnapshot(0, {})))
        )
        stack.enter_context(patch.object(intent_router, "_get_http", lambda: fixture))
        stack.enter_context(
            patch.object(intent_router, "render_additional_prompt", lambda *args, **kwargs: "Synthetic fixture prompt")
        )
        stack.enter_context(patch("app.providers.get_provider_router", lambda: fixture))
        stack.enter_context(patch.object(voice_intent, "execute_text_process", fixture.classify_voice))
        stack.enter_context(
            patch.object(voice_intent, "get_prompt_text", lambda *args, **kwargs: "Synthetic fixture prompt")
        )
        stack.enter_context(patch("app.metrics.metrics_collector.record_api_call", AsyncMock()))
        stack.enter_context(patch("app.metrics.metrics_collector.enqueue_api_call", lambda *args, **kwargs: None))
        previous_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            yield fixture
        finally:
            logging.disable(previous_disable)


async def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    validate_cases(cases)
    rows = []
    with offline_boundaries() as fixture:
        from app import intent_router, thinking_classifier, voice_intent

        for case in cases:
            fixture.case = case
            fixture.requests = []
            fixture.violations = []
            effects: set[str] = set()
            start = time.perf_counter_ns()
            route, reason, thinking, thinking_reason, voice_reason = (
                "llm",
                "evaluation_error",
                "off",
                "evaluation_error",
                "none",
            )
            errors = []
            try:
                speech = voice_intent.resolve_speech_request(
                    user_texts=[case["input"]] if case["input"] else [],
                    reply_texts=case.get("reply_texts", []),
                    forwarded_texts=case.get("forwarded_texts", []),
                )
                tts = False
                if speech.action == "speak":
                    route, reason = f"speech_{speech.source}", "speech_source_resolved"
                    effects.add("tts_delivery")
                elif speech.action == "clarify":
                    route, reason = "speech_clarify", "speech_source_ambiguous"
                else:
                    voice = await voice_intent.detect_tts_intent(
                        user_text=case["input"],
                        llm_context="\n".join(case.get("forwarded_texts", [])),
                        forwarded_entries=[SimpleNamespace(text=text) for text in case.get("forwarded_texts", [])],
                    )
                    tts, voice_reason = voice.explicit_tts, voice.reason
                    if tts:
                        effects.add("tts_delivery")
                    decision = await intent_router.resolve_direct_intent(case["input"])
                    route, reason = decision.route, decision.reason_code
                    if decision.result is not None and decision.result.handled:
                        effects.add("direct_delivery")
                resolved = thinking_classifier.resolve_thinking_decision(
                    case["explicit_thinking"],
                    case["input"],
                    case["history"],
                    model=case.get("model"),
                )
                thinking, thinking_reason = resolved.level or "off", resolved.reason_code
                if "expected_tts" in case and (tts or speech.action == "speak") != case["expected_tts"]:
                    errors.append("voice_mismatch")
            except Exception:
                errors.append("evaluation_error")
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            effects.update(fixture.violations)
            forbidden = sorted(effects.intersection(case["forbidden_side_effect"]))
            explicit = case["explicit_thinking"] in {"off", "low", "medium", "high"}
            explicit_violation = explicit and thinking != case["explicit_thinking"]
            accepted = route == case["expected_route"] or route in case["allowed_fallback"]
            if not accepted:
                errors.append("route_mismatch")
            if thinking != case["expected_thinking"]:
                errors.append("thinking_mismatch")
            if forbidden:
                errors.append("forbidden_side_effect")
            if fixture.violations:
                errors.append("isolation_violation")
            rows.append(
                {
                    "case_id": case["case_id"],
                    "group": case["group"],
                    "language": case["language"],
                    "route": route,
                    "reason_code": reason,
                    "thinking": thinking,
                    "thinking_reason_code": thinking_reason,
                    "voice_reason_code": voice_reason,
                    "expected_route": case["expected_route"],
                    "allowed_fallback_used": route != case["expected_route"] and accepted,
                    "false_shortcut": route != case["expected_route"] and route not in {"llm", "speech_clarify"},
                    "missed_shortcut": case["expected_route"] not in {"llm", "speech_clarify"} and route == "llm",
                    "explicit_setting": explicit,
                    "explicit_setting_violation": explicit_violation,
                    "forbidden_side_effects": forbidden,
                    "isolation_violations": list(fixture.violations),
                    "fixture_requests": list(fixture.requests),
                    "latency_ms": round(elapsed, 4),
                    "errors": errors,
                }
            )
    latencies = sorted(row["latency_ms"] for row in rows)
    return {
        "schema_version": 1,
        "synthetic_only": True,
        "case_count": len(rows),
        "groups": dict(Counter(row["group"] for row in rows)),
        "false_shortcuts": sum(row["false_shortcut"] for row in rows),
        "missed_shortcuts": sum(row["missed_shortcut"] for row in rows),
        "explicit_setting_cases": sum(row["explicit_setting"] for row in rows),
        "explicit_setting_violations": sum(row["explicit_setting_violation"] for row in rows),
        "forbidden_side_effects": sum(len(row["forbidden_side_effects"]) for row in rows),
        "isolation_violations": sum(len(row["isolation_violations"]) for row in rows),
        "failed_case_ids": [row["case_id"] for row in rows if row["errors"]],
        "latency_ms": {
            "scope": "local_routing_with_fixture_boundaries",
            "samples": len(rows),
            "median": statistics.median(latencies) if latencies else 0,
            "p95": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0,
            "max": max(latencies, default=0),
        },
        "limits": [
            "Synthetic cases do not measure real-user understanding.",
            "Delivery effects are projected from routing decisions; Telegram/TTS delivery is not executed.",
            "Provider/client responses are fixtures; model quality and live city ambiguity are not evaluated.",
        ],
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = asyncio.run(evaluate_cases(load_cases(args.corpus)))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return 1 if report["failed_case_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
