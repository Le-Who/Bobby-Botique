# /app/prometheus.py
"""Zero-dependency Prometheus exporter for process-local operational events."""

import os

from app.observability.metrics import operational_metrics


def generate_metrics_text() -> str:
    """Generate Prometheus text without querying the application database."""
    text = operational_metrics.prometheus_text().rstrip("\n")
    try:
        import psutil

        memory_bytes = psutil.Process(os.getpid()).memory_info().rss
    except Exception:
        memory_bytes = 0
    try:
        from app import state

        active_users = state.get_active_user_lock_count()
    except Exception:
        active_users = 0
    return (
        f"{text}\n"
        "# HELP gembot_active_users Current number of active user locks.\n"
        "# TYPE gembot_active_users gauge\n"
        f"gembot_active_users {active_users}\n"
        "# HELP gembot_process_memory_bytes Current process resident memory.\n"
        "# TYPE gembot_process_memory_bytes gauge\n"
        f"gembot_process_memory_bytes {memory_bytes}\n"
    )
