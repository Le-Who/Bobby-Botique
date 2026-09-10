from __future__ import annotations

from app.observability.metrics import OperationalMetrics


def test_metrics_track_actual_provider_pair_and_semantic_outcomes():
    metrics = OperationalMetrics()

    metrics.observe(
        "provider.attempt_finished",
        {
            "provider": "gemini",
            "actual_model": "gemini-test",
            "outcome": "failed",
            "duration_ms": 25.5,
            "ttft_ms": 4.0,
        },
    )
    metrics.observe("delivery.finished", {"delivery_status": "sent", "duration_ms": 9})
    metrics.observe("job.started", {"queue_wait_ms": 3})
    metrics.observe(
        "job.finished",
        {"execution_outcome": "returned", "business_outcome": "completed"},
    )

    snapshot = metrics.snapshot()
    assert snapshot["provider_attempts"][("gemini", "gemini-test", "failed")] == 1
    assert snapshot["delivery_outcomes"]["sent"] == 1
    assert snapshot["job_outcomes"]["succeeded"] == 1
    assert snapshot["uptime_seconds"] >= 0

    text = metrics.prometheus_text()
    assert 'gembot_provider_attempts_total{provider="gemini",model="gemini-test",outcome="failed"} 1' in text


def test_metrics_track_direct_workloads_by_bounded_dimensions():
    metrics = OperationalMetrics()
    metrics.observe(
        "workload.attempt_finished",
        {
            "workload": "image_generation",
            "provider": "pollinations",
            "model": "flux",
            "outcome": "remote_error",
            "duration_ms": 41.25,
        },
    )

    snapshot = metrics.snapshot()
    assert snapshot["workload_attempts"][("image_generation", "pollinations", "flux", "remote_error")] == 1
    assert snapshot["durations"]["workload_duration_ms"] == (1.0, 41.25)
    assert (
        'gembot_workload_attempts_total{workload="image_generation",provider="pollinations",model="flux",'
        'outcome="remote_error"} 1' in metrics.prometheus_text()
    )


def test_metrics_normalize_actual_request_and_delivery_outcomes():
    metrics = OperationalMetrics()

    metrics.observe("telegram.update_finished", {"outcome": "succeeded"})
    metrics.observe("http.request_finished", {"outcome": "succeeded"})
    metrics.observe("delivery.finished", {"delivery_status": "failure_notice_sent"})
    metrics.observe("delivery.finished", {"delivery_status": "transport_failed"})

    snapshot = metrics.snapshot()
    assert snapshot["request_outcomes"] == {"succeeded": 2}
    assert snapshot["delivery_outcomes"] == {
        "failure_notice_sent": 1,
        "transport_failed": 1,
    }


def test_metrics_distinguish_returned_business_failure_from_success():
    metrics = OperationalMetrics()

    metrics.observe(
        "job.finished",
        {"execution_outcome": "returned", "business_outcome": "failed"},
    )
    metrics.observe(
        "job.finished",
        {"execution_outcome": "raised", "business_outcome": "failed"},
    )

    assert metrics.snapshot()["job_outcomes"] == {"failed": 2}


def test_metrics_bound_high_cardinality_labels():
    metrics = OperationalMetrics(max_provider_pairs=2)
    for index in range(5):
        metrics.observe(
            "provider.attempt_finished",
            {"provider": f"provider-{index}", "actual_model": f"model-{index}", "outcome": "succeeded"},
        )

    pairs = {(provider, model) for provider, model, _outcome in metrics.snapshot()["provider_attempts"]}
    assert len(pairs) <= 3  # two admitted pairs plus the shared overflow bucket
    assert ("other", "other") in pairs


def test_prometheus_labels_are_escaped():
    metrics = OperationalMetrics()
    metrics.observe(
        "provider.attempt_finished",
        {"provider": 'bad"\nprovider', "actual_model": "model\\name", "outcome": "succeeded"},
    )
    text = metrics.prometheus_text()
    assert 'provider="bad_provider"' in text
    assert 'model="model\\\\name"' in text
