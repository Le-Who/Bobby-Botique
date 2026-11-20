"""Compare deep-dive cleanup against a Git revision using the locked SDK offline."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import logging
import os
import statistics
import subprocess
import sys
import time
import tracemalloc
import weakref
from collections import Counter
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_agentic(baseline: str | None) -> ModuleType:
    if baseline is None:
        from app.core import agentic

        return agentic

    result = subprocess.run(
        ["git", "show", f"{baseline}:app/core/agentic.py"], check=True, capture_output=True, cwd=ROOT
    )
    baseline_module = ModuleType("agentic_cleanup_baseline")
    sys.modules[baseline_module.__name__] = baseline_module
    exec(compile(result.stdout, "app/core/agentic.py", "exec"), baseline_module.__dict__)
    return baseline_module


async def trial(baseline: str | None, runs: int, *, measure_memory: bool) -> dict:
    import httpx
    import psutil
    from google import genai
    from google.genai import types

    agentic = load_agentic(baseline)
    logging.disable(logging.CRITICAL)
    page = "bounded-synthetic-source " * 600
    answer = "synthetic answer"
    calls = 0
    tool_calls = 0
    request_count = 0
    scheduled_call = asyncio.Event()
    outcome = "completed"
    references: dict[int, weakref.ReferenceType] = {}
    durations: dict[str, list[float]] = {name: [] for name in ("completed", "failed", "cancelled")}
    ticker_lag: list[float] = []
    canary_latencies: list[float] = []
    gc_gen1 = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls, request_count
        if request.url.host == "canary.invalid":
            await asyncio.sleep(0)
            return httpx.Response(204)
        request_count += 1
        calls += 1
        if calls == 1:
            parts = [
                {"functionCall": {"name": "read_page", "args": {"url": f"https://example.invalid/{index}"}}}
                for index in range(3)
            ]
        else:
            assert calls == 2
            assert request.content.count(page.encode()) == 3
            if outcome == "failed":
                return httpx.Response(
                    400, json={"error": {"code": 400, "message": "synthetic", "status": "INVALID_ARGUMENT"}}
                )
            if outcome == "cancelled":
                scheduled_call.set()
                await asyncio.Future()
            parts = [{"text": answer}]

        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                "usageMetadata": {"totalTokenCount": 100},
            },
        )

    async def page_tool(*args, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        await asyncio.sleep(0)
        return {"content": page}

    async def reserve(*args):
        return True

    async def status(*args, **kwargs):
        pass

    def collect_event(phase: str, info: dict) -> None:
        nonlocal gc_gen1
        if phase == "start" and info["generation"] == 1:
            gc_gen1 += 1

    async def canary(client: httpx.AsyncClient) -> None:
        period = 0.002
        while True:
            started = time.perf_counter()
            await asyncio.sleep(period)
            ticker_lag.append(max(0, time.perf_counter() - started - period))
            dispatched = time.perf_counter()
            response = await client.get("https://canary.invalid/status")
            assert response.status_code == 204
            canary_latencies.append(time.perf_counter() - dispatched)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = genai.Client(
            api_key="offline-benchmark-key",
            http_options=types.HttpOptions(httpx_async_client=http_client),
        )
        agentic.get_cached_genai_client = lambda key: client
        agentic.reserve_gemini_key_usage = reserve
        agentic.settings = SimpleNamespace(
            AGENTIC_MAX_ITERATIONS=5,
            AGENTIC_MAX_PAGES=3,
            AGENTIC_MAX_TOKENS=100_000,
            AGENTIC_TIMEOUT_SECONDS=90,
        )
        agent = agentic.AgenticSearch("gemini-3.8-flash", "offline-benchmark-key")
        agent._execute_tool = page_tool
        generate = agent._generate_budgeted

        async def tracked_generate(*, contents, **kwargs):
            for content in contents:
                references[id(content)] = weakref.ref(content)
            result = await generate(contents=contents, **kwargs)
            references[id(result)] = weakref.ref(result)
            return result

        if measure_memory:
            agent._generate_budgeted = tracked_generate

        async def run_one(name: str) -> None:
            nonlocal calls, outcome
            calls = 0
            outcome = name
            if name == "cancelled":
                scheduled_call.clear()
                task = asyncio.create_task(agent.run("synthetic research", status))
                await asyncio.wait_for(scheduled_call.wait(), 5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    return
                raise AssertionError("The cancelled request returned a response")
            result = await agent.run("synthetic research", status)
            assert result.llm_calls == (2 if name == "completed" else 1)
            assert (result.answer == answer) == (name == "completed")

        for name in durations:
            await run_one(name)
        tool_calls = request_count = 0
        references.clear()
        gc.collect()
        listener = asyncio.create_task(canary(http_client)) if not measure_memory else None
        gc.callbacks.append(collect_event)
        process = psutil.Process()
        rss_before = process.memory_info().rss
        if measure_memory:
            tracemalloc.start()
        live_per_request = dict.fromkeys(durations, 0)
        surviving_types = {name: Counter() for name in durations}
        retained_references: list[weakref.ReferenceType] = []
        try:
            for _ in range(runs):
                for name, times in durations.items():
                    started = time.perf_counter()
                    await run_one(name)
                    times.append(time.perf_counter() - started)
                    await asyncio.sleep(0)
                    if measure_memory:
                        survivors = [ref for ref in references.values() if ref() is not None]
                        live_per_request[name] = max(live_per_request[name], len(survivors))
                        surviving_types[name].update(type(ref()).__name__ for ref in survivors)
                        retained_references.extend(survivors)
                        references.clear()
            rss_delta = process.memory_info().rss - rss_before
            live_bytes, peak_bytes = tracemalloc.get_traced_memory() if measure_memory else (0, 0)
            retained_before_collect = Counter(type(ref()).__name__ for ref in retained_references if ref() is not None)
            if measure_memory:
                gc.collect()
            retained_after_collect = Counter(type(ref()).__name__ for ref in retained_references if ref() is not None)
        finally:
            if measure_memory:
                tracemalloc.stop()
            gc.callbacks.remove(collect_event)
            if listener is not None:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await client.aio.aclose()

    assert request_count == 6 * runs
    assert tool_calls == 9 * runs

    def p95(samples: list[float]) -> float | None:
        if not samples:
            return None
        return 1000 * sorted(samples)[int(len(samples) * 0.95)]

    return {
        "python": sys.version.split()[0],
        "genai": genai.__version__,
        "logical_cpus": os.cpu_count(),
        "process_cores": len(process.cpu_affinity()) if hasattr(process, "cpu_affinity") else None,
        "runs_per_outcome": runs,
        "sdk_requests": request_count,
        "tool_results": tool_calls,
        "page_chars": len(page),
        "gc_generation_1_calls": gc_gen1,
        "memory": measure_memory,
        "rss_delta_bytes": rss_delta,
        "traced_live_bytes": live_bytes,
        "traced_peak_bytes": peak_bytes,
        "max_live_request_objects": live_per_request,
        "surviving_request_types": surviving_types,
        "retained_before_diagnostic_collect": retained_before_collect,
        "retained_after_diagnostic_collect": retained_after_collect,
        "latency_p50_ms": {name: 1000 * statistics.median(times) for name, times in durations.items()},
        "latency_p95_ms": {name: p95(times) for name, times in durations.items()},
        "ticker_lag_p95_ms": p95(ticker_lag),
        "ticker_samples": len(ticker_lag),
        "concurrent_request_p95_ms": p95(canary_latencies),
        "concurrent_request_samples": len(canary_latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", help="Git revision providing the previous agentic implementation")
    parser.add_argument("--runs", type=int, default=20, help="Requests per outcome in each trial")
    parser.add_argument("--trials", type=int, default=5, help="Alternating trial pairs")
    parser.add_argument("--trial", choices=("baseline", "current"), help=argparse.SUPPRESS)
    parser.add_argument("--measure-memory", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.runs < 1 or args.trials < 1:
        parser.error("--runs and --trials must be positive")
    if args.trial:
        baseline = args.baseline if args.trial == "baseline" else None
        print(json.dumps(asyncio.run(trial(baseline, args.runs, measure_memory=args.measure_memory))))
        return
    if not args.baseline:
        parser.error("--baseline is required")
    for pair in range(args.trials):
        for mode in ("baseline", "current"):
            for memory in (False, True):
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--baseline",
                    args.baseline,
                    "--trial",
                    mode,
                    "--runs",
                    str(args.runs),
                ]
                if memory:
                    command.append("--measure-memory")
                result = subprocess.run(command, check=True, capture_output=True, cwd=ROOT)
                print(json.dumps({"trial": pair + 1, "mode": mode, **json.loads(result.stdout)}), flush=True)


if __name__ == "__main__":
    main()
