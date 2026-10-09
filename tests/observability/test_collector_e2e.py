"""Actual protected datasource checks after the disposable Docker stdout probe."""

import json
import os
from pathlib import Path

import pytest

from scripts.check_observability_stack import StackProbe

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def collector():
    names = ("TEST_GRAFANA_URL", "TEST_GRAFANA_PASSWORD_FILE", "TEST_OBSERVABILITY_MANIFEST")
    configured = [os.getenv(name) for name in names]
    if not all(configured):
        if os.getenv("GEMAIBOT_REQUIRE_OBSERVABILITY_TESTS") == "1":
            pytest.fail("required collector E2E must provide the synthetic URL, password file and manifest")
        pytest.skip("collector E2E requires the explicitly isolated stdout probe")
    if os.getenv("GEMAIBOT_OBSERVABILITY_IS_EPHEMERAL") != "1":
        pytest.fail("collector E2E requires GEMAIBOT_OBSERVABILITY_IS_EPHEMERAL=1")
    manifest = json.loads(Path(configured[2]).read_text(encoding="utf-8"))
    return StackProbe(configured[0], Path(configured[1]), manifest)


def test_protected_datasource_requires_authentication(collector):
    collector.check_auth()


@pytest.mark.parametrize("service", ["gemaibotv2", "ytdlbot"])
def test_stdout_event_cardinality_envelope_labels_and_large_payload_survive(collector, service):
    collector.check_events(service)


def test_container_allowlist_excludes_private_other_sources_and_keeps_invalid_lines_scoped(collector):
    collector.check_source_filters_and_fallback()


def test_server_query_limits_are_enforced_through_authenticated_grafana_proxy(collector):
    collector.check_query_limits()


def test_collected_synthetic_stream_has_no_forbidden_fields_or_excluded_content(collector):
    collector.check_privacy()
