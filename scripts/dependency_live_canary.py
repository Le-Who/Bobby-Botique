"""Protected baseline-versus-candidate canaries for dependency update review."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from google import genai
from google.genai import types
from telegram import Bot


class ProbeStatus(StrEnum):
    PASSED = "passed"
    AUTH_FAILED = "auth-failed"
    CONTRACT_FAILED = "contract-failed"
    TRANSIENT_FAILED = "transient-failed"
    CLEANUP_FAILED = "cleanup-failed"


class ComparisonStatus(StrEnum):
    PASSED = "passed"
    BLOCKED_CANARY_CONFIGURATION = "blocked-canary-configuration"
    BLOCKED_BASELINE = "blocked-baseline"
    CANDIDATE_FAILED = "candidate-failed"
    INCONCLUSIVE_EXTERNAL = "inconclusive-external"


class CanaryConfigurationError(ValueError):
    def __init__(self, missing: list[str]):
        self.missing = tuple(sorted(missing))
        super().__init__(f"missing required canary configuration: {', '.join(self.missing)}")


class CleanupFailure(RuntimeError):
    """A live side effect or client resource could not be cleaned up."""


@dataclass(frozen=True)
class CanaryConfig:
    telegram_token: str = field(repr=False)
    telegram_chat_id: int
    gemini_api_key: str = field(repr=False)
    gemini_model: str
    tavily_api_key: str = field(repr=False)
    timeout_seconds: float = 15.0

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.telegram_token, self.gemini_api_key, self.tavily_api_key)


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    attempts: int
    detail: str | None
    evidence: dict[str, Any]


def load_config(environment: Mapping[str, str]) -> CanaryConfig:
    required = {
        "CANARY_TELEGRAM_BOT_TOKEN": environment.get("CANARY_TELEGRAM_BOT_TOKEN", "").strip(),
        "CANARY_TELEGRAM_CHAT_ID": environment.get("CANARY_TELEGRAM_CHAT_ID", "").strip(),
        "CANARY_GEMINI_API_KEY": environment.get("CANARY_GEMINI_API_KEY", "").strip(),
        "CANARY_TAVILY_API_KEY": environment.get("CANARY_TAVILY_API_KEY", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise CanaryConfigurationError(missing)
    try:
        telegram_chat_id = int(required["CANARY_TELEGRAM_CHAT_ID"])
        timeout_seconds = float(environment.get("CANARY_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise CanaryConfigurationError(["CANARY_TELEGRAM_CHAT_ID or CANARY_TIMEOUT_SECONDS is invalid"]) from exc
    if telegram_chat_id == 0 or not 1 <= timeout_seconds <= 60:
        raise CanaryConfigurationError(["CANARY_TELEGRAM_CHAT_ID or CANARY_TIMEOUT_SECONDS is invalid"])
    return CanaryConfig(
        telegram_token=required["CANARY_TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=telegram_chat_id,
        gemini_api_key=required["CANARY_GEMINI_API_KEY"],
        gemini_model=environment.get("CANARY_GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
        tavily_api_key=required["CANARY_TAVILY_API_KEY"],
        timeout_seconds=timeout_seconds,
    )


def redact(value: str, secrets: tuple[str, ...]) -> str:
    sanitized = value
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        sanitized = sanitized.replace(secret, "[REDACTED]")
    sanitized = re.sub(r"/bot[^/\s]+/", "/bot[REDACTED]/", sanitized)
    return sanitized[:500]


def _status_code(exc: BaseException) -> int | None:
    for source in (exc, getattr(exc, "response", None)):
        for attribute in ("status_code", "code"):
            value = getattr(source, attribute, None)
            if isinstance(value, int):
                return value
    return None


def _classify_exception(exc: BaseException) -> ProbeStatus:
    if isinstance(exc, CleanupFailure):
        return ProbeStatus.CLEANUP_FAILED
    code = _status_code(exc)
    class_name = type(exc).__name__
    if code in {401, 403} or class_name in {"Forbidden", "InvalidToken", "Unauthenticated"}:
        return ProbeStatus.AUTH_FAILED
    transient_names = {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "ReadTimeout",
        "RemoteProtocolError",
        "RetryAfter",
        "TimedOut",
        "TimeoutError",
    }
    if code in {408, 409, 425, 429} or (code is not None and code >= 500) or class_name in transient_names:
        return ProbeStatus.TRANSIENT_FAILED
    return ProbeStatus.CONTRACT_FAILED


async def run_probe(
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    *,
    secrets: tuple[str, ...],
    timeout_seconds: float = 15.0,
) -> ProbeResult:
    for attempt in (1, 2):
        try:
            evidence = await asyncio.wait_for(operation(), timeout=timeout_seconds)
            return ProbeResult(name, ProbeStatus.PASSED, attempt, None, evidence)
        except Exception as exc:
            status = _classify_exception(exc)
            detail = redact(f"{type(exc).__name__}: {exc}", secrets)
            if status is ProbeStatus.TRANSIENT_FAILED and attempt == 1:
                continue
            return ProbeResult(name, status, attempt, detail, {})
    raise AssertionError("unreachable probe retry state")


async def probe_telegram(
    config: CanaryConfig,
    run_id: str,
    *,
    bot_factory: Callable[[str], Any] = Bot,
) -> dict[str, Any]:
    bot = bot_factory(config.telegram_token)
    message_id: int | None = None
    evidence = {"identity_present": False, "message_sent": False, "message_deleted": False}
    primary_error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        await asyncio.wait_for(bot.initialize(), timeout=config.timeout_seconds)
        identity = await asyncio.wait_for(bot.get_me(), timeout=config.timeout_seconds)
        if not isinstance(getattr(identity, "id", None), int):
            raise ValueError("Telegram getMe response is missing an integer id")
        evidence["identity_present"] = True
        message = await asyncio.wait_for(
            bot.send_message(
                chat_id=config.telegram_chat_id,
                text=f"dependency canary {run_id}",
                disable_notification=True,
            ),
            timeout=config.timeout_seconds,
        )
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, int):
            raise ValueError("Telegram sendMessage response is missing message_id")
        evidence["message_sent"] = True
    except Exception as exc:
        primary_error = exc
    finally:
        if message_id is not None:
            try:
                await asyncio.wait_for(
                    bot.delete_message(chat_id=config.telegram_chat_id, message_id=message_id),
                    timeout=config.timeout_seconds,
                )
                evidence["message_deleted"] = True
            except Exception as exc:
                cleanup_error = exc
        try:
            await asyncio.wait_for(bot.shutdown(), timeout=config.timeout_seconds)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
    if cleanup_error is not None and (message_id is not None or primary_error is None):
        raise CleanupFailure(f"Telegram cleanup failed: {cleanup_error}") from cleanup_error
    if primary_error is not None:
        raise primary_error
    return evidence


async def probe_gemini(config: CanaryConfig, run_id: str) -> dict[str, Any]:
    del run_id
    client = genai.Client(
        api_key=config.gemini_api_key,
        http_options=types.HttpOptions(timeout=int(config.timeout_seconds * 1000)),
    )
    cleanup_error: Exception | None = None
    primary_error: Exception | None = None
    evidence: dict[str, Any] = {}
    try:
        response = await client.aio.models.generate_content(
            model=config.gemini_model,
            contents="Reply with the single token CANARY_OK.",
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=16),
        )
        response_text = getattr(response, "text", None)
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("Gemini response is missing non-empty text")
        evidence = {"response_present": True, "response_length": len(response_text)}
    except Exception as exc:
        primary_error = exc
    finally:
        try:
            await client.aio.aclose()
        except Exception as exc:
            cleanup_error = exc
    if primary_error is not None:
        raise primary_error
    if cleanup_error is not None:
        raise CleanupFailure(f"Gemini client cleanup failed: {cleanup_error}") from cleanup_error
    return evidence


async def probe_tavily(config: CanaryConfig, run_id: str) -> dict[str, Any]:
    del run_id
    payload = {
        "api_key": config.tavily_api_key,
        "query": "What is the official website of OpenAI?",
        "search_depth": "basic",
        "include_answer": True,
    }
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        data = response.json()
    answer = data.get("answer") if isinstance(data, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not (isinstance(answer, str) and answer.strip()) and not isinstance(results, list):
        raise ValueError("Tavily response is missing answer and results contracts")
    return {
        "answer_present": isinstance(answer, str) and bool(answer.strip()),
        "results_count": len(results) if isinstance(results, list) else 0,
        "request_id_present": bool(response.headers.get("x-request-id")),
    }


async def run_suite(config: CanaryConfig, label: str) -> dict[str, Any]:
    run_id = f"{label}-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(UTC).isoformat()
    operations = {
        "telegram": lambda: probe_telegram(config, run_id),
        "gemini": lambda: probe_gemini(config, run_id),
        "tavily": lambda: probe_tavily(config, run_id),
    }
    results: dict[str, ProbeResult] = {}
    for name, operation in operations.items():
        results[name] = await run_probe(
            name,
            operation,
            secrets=config.secrets,
            timeout_seconds=config.timeout_seconds + 2,
        )
    probes = {
        name: {
            **asdict(result),
            "status": result.status.value,
        }
        for name, result in results.items()
    }
    return {
        "schema_version": 1,
        "label": label,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "overall": "passed" if all(result.status is ProbeStatus.PASSED for result in results.values()) else "failed",
        "probes": probes,
    }


def _probe_statuses(report: dict[str, Any]) -> set[ProbeStatus]:
    probes = report.get("probes")
    if not isinstance(probes, dict) or set(probes) != {"telegram", "gemini", "tavily"}:
        raise ValueError("canary report does not contain the complete probe set")
    return {ProbeStatus(probe["status"]) for probe in probes.values()}


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if "configuration-blocked" in {baseline.get("overall"), candidate.get("overall")}:
        status = ComparisonStatus.BLOCKED_CANARY_CONFIGURATION
    else:
        baseline_statuses = _probe_statuses(baseline)
        candidate_statuses = _probe_statuses(candidate)
        if ProbeStatus.AUTH_FAILED in baseline_statuses:
            status = ComparisonStatus.BLOCKED_CANARY_CONFIGURATION
        elif ProbeStatus.TRANSIENT_FAILED in baseline_statuses:
            status = ComparisonStatus.INCONCLUSIVE_EXTERNAL
        elif baseline_statuses != {ProbeStatus.PASSED}:
            status = ComparisonStatus.BLOCKED_BASELINE
        elif ProbeStatus.TRANSIENT_FAILED in candidate_statuses:
            status = ComparisonStatus.INCONCLUSIVE_EXTERNAL
        elif candidate_statuses == {ProbeStatus.PASSED}:
            status = ComparisonStatus.PASSED
        else:
            status = ComparisonStatus.CANDIDATE_FAILED
    return {
        "schema_version": 1,
        "status": status.value,
        "ready_for_review": status is ComparisonStatus.PASSED,
        "baseline_overall": baseline.get("overall"),
        "candidate_overall": candidate.get("overall"),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("--output", type=Path, required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "run":
        try:
            config = load_config(os.environ)
        except CanaryConfigurationError as exc:
            _write_json(
                args.output,
                {
                    "schema_version": 1,
                    "label": args.label,
                    "overall": "configuration-blocked",
                    "missing": list(exc.missing),
                    "probes": {},
                },
            )
            return 0
        _write_json(args.output, asyncio.run(run_suite(config, args.label)))
        return 0

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, candidate)
    _write_json(args.output, comparison)
    return 0 if comparison["ready_for_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
