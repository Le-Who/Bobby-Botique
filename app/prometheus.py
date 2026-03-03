# /app/prometheus.py
"""Zero-dependency Prometheus metrics exporter.

Generates Prometheus text format from the existing MetricsCollector.
No prometheus_client library needed — saves ~20MB RAM.
Exposes via /metrics endpoint in web.py.
"""

import time

from app.metrics import metrics_collector


def generate_metrics_text() -> str:
    """Generate Prometheus text exposition format from MetricsCollector data.

    Returns:
        Prometheus-compatible text string.
    """
    lines: list[str] = []
    _add = lines.append

    # ── Uptime ───────────────────────────────────────────────────────────
    uptime = time.time() - metrics_collector._start_time if hasattr(metrics_collector, "_start_time") else 0
    _add("# HELP gembot_uptime_seconds Time since process start.")
    _add("# TYPE gembot_uptime_seconds gauge")
    _add(f"gembot_uptime_seconds {uptime:.1f}")

    # ── API calls ────────────────────────────────────────────────────────
    _add("")
    _add("# HELP gembot_api_calls_total Total AI API calls by provider and model.")
    _add("# TYPE gembot_api_calls_total counter")
    api_calls = getattr(metrics_collector, "_api_calls", {})
    if isinstance(api_calls, dict):
        for key, count in api_calls.items():
            provider, model = key if isinstance(key, tuple) else (key, "unknown")
            _add(f'gembot_api_calls_total{{provider="{provider}",model="{model}"}} {count}')

    # ── Errors ───────────────────────────────────────────────────────────
    _add("")
    _add("# HELP gembot_errors_total Total errors by category.")
    _add("# TYPE gembot_errors_total counter")
    errors = getattr(metrics_collector, "_errors", {})
    if isinstance(errors, dict):
        for category, count in errors.items():
            _add(f'gembot_errors_total{{category="{category}"}} {count}')

    # ── Active users ─────────────────────────────────────────────────────
    _add("")
    _add("# HELP gembot_active_users Current number of active user locks.")
    _add("# TYPE gembot_active_users gauge")
    try:
        from app import state
        active = len(state._user_locks) if hasattr(state, "_user_locks") else 0
        _add(f"gembot_active_users {active}")
    except Exception:
        _add("gembot_active_users 0")

    # ── Memory usage ─────────────────────────────────────────────────────
    _add("")
    _add("# HELP gembot_process_memory_bytes Current process memory usage in bytes.")
    _add("# TYPE gembot_process_memory_bytes gauge")
    try:
        import os

        import psutil
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info().rss
        _add(f"gembot_process_memory_bytes {mem}")
    except Exception:
        _add("gembot_process_memory_bytes 0")

    _add("")
    return "\n".join(lines) + "\n"
