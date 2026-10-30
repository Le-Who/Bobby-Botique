"""Offline regressions at the dashboard's HTTP and JavaScript boundaries."""

import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from quart import session

from app import cache, metrics, web
from app.metrics import MetricsCollector
from app.queue import task_queue


@pytest.mark.asyncio
async def test_sse_uses_actual_process_queue_and_metrics_counters(monkeypatch):
    collector = MetricsCollector()
    collector.metrics.request_count = 123
    collector.metrics.error_count = 17
    traffic = {
        "timezone": "Europe/Kyiv",
        "periods": {"today": {"user_requests": 4, "api_attempts": 9, "available": True}},
    }
    monkeypatch.setattr(collector, "get_traffic_summary", AsyncMock(return_value=traffic))
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    monkeypatch.setattr(task_queue, "get_queue_stats", AsyncMock(return_value={"pending_tasks": 7, "running_tasks": 3}))
    monkeypatch.setattr(web.database, "is_database_connected", lambda: True)

    async with web.quart_app.test_request_context("/api/events"):
        session["authenticated"] = True
        stream, _ = await web.api_events()
        try:
            event = await anext(stream)
        finally:
            await stream.aclose()

    payload = json.loads(event.removeprefix("data: ").strip())
    assert payload["queue"] == {"pending": 7, "processing": 3}
    assert payload["metrics"] == {"total_requests": 123, "error_count": 17, "traffic": traffic}
    collector.get_traffic_summary.assert_awaited_once_with()
    assert "services" not in payload, "An open pool cannot override the snapshot's failed database probe"


@pytest.mark.asyncio
async def test_sse_source_failure_is_valid_json_without_dependency_details(monkeypatch):
    import psutil

    def broken_sample(*args, **kwargs):
        raise RuntimeError('synthetic private "connection details"')

    monkeypatch.setattr(psutil, "cpu_percent", broken_sample)
    async with web.quart_app.test_request_context("/api/events"):
        session["authenticated"] = True
        stream, _ = await web.api_events()
        try:
            event = await anext(stream)
        finally:
            await stream.aclose()

    payload = json.loads(event.removeprefix("data: ").strip())
    assert payload["sources"]["system"]["available"] is False
    assert payload["sources"]["system"]["reason_code"] == "source_failed"
    assert "system" not in payload
    assert "metrics" in payload
    assert "connection details" not in event


@pytest.mark.asyncio
async def test_twenty_snapshot_and_sse_readers_share_traffic_query(monkeypatch):
    import asyncio

    collector = MetricsCollector()
    traffic = AsyncMock(return_value={"timezone": "Europe/Kyiv", "periods": {}})
    monkeypatch.setattr(collector, "get_traffic_summary", traffic)
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    for owner, name, value in (
        (metrics.role_conv_metrics, "get_metrics_summary", {}),
        (cache, "get_multi_layer_cache_stats", {}),
        (cache, "ping_safe", False),
        (web, "get_supabase_metrics", {}),
        (web, "get_gemini_key_usage_stats", []),
        (web, "get_tavily_key_usage_stats", []),
        (web, "_get_dashboard_key_health", {}),
        (task_queue, "get_queue_stats", {"pending_tasks": 7}),
    ):
        monkeypatch.setattr(owner, name, AsyncMock(return_value=value))
    results = await asyncio.gather(
        *[web._build_dashboard_snapshot() if index % 2 else web._build_dashboard_event() for index in range(20)]
    )
    assert all(row["sources"]["metrics"]["available"] for row in results)
    traffic.assert_awaited_once_with()
    task_queue.get_queue_stats.assert_awaited_once_with()
    await web._close_dashboard_sources()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/overview", "/api/dashboard"])
async def test_system_sampling_does_not_block_http_event_loop(monkeypatch, path):
    import psutil

    event_loop_thread = threading.get_ident()
    sample_threads = []

    def cpu_percent(interval):
        sample_threads.append(threading.get_ident())
        return 42

    monkeypatch.setattr(psutil, "cpu_percent", cpu_percent)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(percent=12, used=1024, total=2048))
    monkeypatch.setattr(psutil, "disk_usage", lambda path: SimpleNamespace(percent=31))
    for owner, name, value in (
        (metrics.metrics_collector, "get_metrics_summary", {}),
        (metrics.role_conv_metrics, "get_metrics_summary", {}),
        (cache, "get_multi_layer_cache_stats", {}),
        (cache, "ping_safe", False),
        (web, "get_supabase_metrics", {}),
        (web, "get_gemini_key_usage_stats", []),
        (web, "get_tavily_key_usage_stats", []),
        (web, "_get_dashboard_key_health", {}),
        (task_queue, "get_queue_stats", {}),
    ):
        monkeypatch.setattr(owner, name, AsyncMock(return_value=value))
    monkeypatch.setattr(web.database, "is_database_connected", lambda: False)
    client = web.quart_app.test_client()
    async with client.session_transaction() as admin_session:
        admin_session["authenticated"] = True

    response = await client.get(path)

    assert response.status_code == 200
    data = await response.get_json()
    system = data["overview"]["system"] if path == "/api/dashboard" else data["system"]
    assert system["cpu_percent"] == 42
    assert sample_threads and all(thread != event_loop_thread for thread in sample_threads)


def _run_dashboard_script(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run([node, "-"], input=source, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_polling_keeps_one_request_in_flight_and_recovers_after_failure():
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    source = template[
        template.index("      async function refreshAll()") : template.index("      function renderErrors")
    ]
    _run_dashboard_script(
        """
const assert = require('node:assert/strict');
const API = {dashboard:'/api/dashboard'};
const document = {hidden:true};
let requests = 0, release;
const snapshot = {overview:{}, providers:{}, infrastructure:{}, errors:{}};
const apiFetch = async () => { requests++; return new Promise(resolve => { release = resolve; }); };
let rendered = 0, dashboardUpdatedAt = 0, dashboardRefreshFailed = false;
const renderOverview = () => { rendered++; };
const renderProviders = () => {}, renderCircuitBreakers = () => {}, renderDatabase = () => {};
const renderQueue = () => {}, renderCache = () => {}, renderErrors = () => {}, renderKeyHealth = () => {};
const updateDashboardFreshness = () => {};
let dashboardRefreshPending = false;
console.error = () => {};
"""
        + source
        + """
(async () => {
  const hidden = refreshAll();
  assert.equal(requests, 0, 'Hidden tabs must not poll');
  await hidden;
  document.hidden = false;
  const first = refreshAll();
  const overlap = refreshAll();
  assert.equal(requests, 1, 'A slow refresh must not start overlapping polls');
  release(null);
  await Promise.all([first, overlap]);
  assert.equal(dashboardRefreshFailed, true);
  const retry = refreshAll();
  assert.equal(requests, 2, 'A failed request must release the poll guard');
  release(snapshot);
  await retry;
  assert.equal(rendered, 1);
  assert.equal(dashboardRefreshFailed, false);
  assert.ok(dashboardUpdatedAt > 0);
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    )


def test_metric_card_recovers_from_critical_to_warning_then_normal():
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    source = template[
        template.index("      function setBarLevel(") : template.index("      // Helper: set service dot")
    ]
    _run_dashboard_script(
        """
const assert = require('node:assert/strict');
function classes() {
  const values = new Set();
  return { add: (...names) => names.forEach(name => values.add(name)),
    remove: (...names) => names.forEach(name => values.delete(name)), contains: name => values.has(name) };
}
const bar = {style:{}, classList:classes()}, card = {classList:classes()};
"""
        + source
        + """
setBarLevel(bar, card, 96);
assert.equal(card.classList.contains('card-critical'), true);
setBarLevel(bar, card, 76);
assert.equal(card.classList.contains('card-critical'), false);
assert.equal(card.classList.contains('card-warning'), true);
setBarLevel(bar, card, 21);
assert.equal(card.classList.contains('card-warning'), false);
assert.equal(card.classList.contains('card-critical'), false);
"""
    )


def test_dashboard_fetch_aborts_a_stalled_response():
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    source = template[template.index("      async function apiFetch(") : template.index("      // Data loaders")]
    _run_dashboard_script(
        """
const assert = require('node:assert/strict');
const http = require('node:http');
const originalTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = milliseconds => {
  assert.equal(milliseconds, 10000);
  return originalTimeout(25);
};
"""
        + source
        + """
(async () => {
  const server = http.createServer((request, response) => {});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/dashboard`;
  try {
    // This deadline only bounds the regression if the production abort is absent.
    await assert.rejects(Promise.race([
      apiFetch(url),
      new Promise((resolve, reject) => setTimeout(() => reject(new Error('Fetch remained stalled')), 500)),
    ]), error => error.name === 'TimeoutError');
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
    )
