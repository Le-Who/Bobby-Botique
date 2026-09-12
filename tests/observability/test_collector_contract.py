from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "ops" / "observability"


def _load_yaml(name: str):
    return yaml.safe_load((OBSERVABILITY / name).read_text(encoding="utf-8"))


def test_observability_compose_is_private_pinned_and_resource_bounded():
    compose = _load_yaml("compose.yml")
    services = compose["services"]

    assert set(services) == {"alloy", "docker-proxy", "grafana", "loki"}
    assert services["grafana"]["ports"] == ["127.0.0.1:3000:3000"]
    assert all("ports" not in services[name] for name in ("alloy", "docker-proxy", "loki"))

    for service in services.values():
        assert "@sha256:" in service["image"]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["mem_limit"]
        assert service["cpus"]
        assert service["logging"]["driver"] == "local"

    proxy_mounts = services["docker-proxy"]["volumes"]
    assert [mount for mount in proxy_mounts if "docker.sock" in mount] == [
        "/var/run/docker.sock:/var/run/docker.sock:ro"
    ]
    assert services["docker-proxy"]["group_add"] == ["${DOCKER_SOCKET_GID:-0}"]
    assert "http://127.0.0.1:2375/_ping" in " ".join(services["docker-proxy"]["healthcheck"]["test"])
    assert all("docker.sock" not in str(services[name].get("volumes", [])) for name in ("alloy", "grafana", "loki"))
    assert compose["networks"]["collector"]["internal"] is True
    assert compose["networks"]["query"]["internal"] is True


def test_grafana_loopback_port_has_a_host_connected_network():
    """Catch attaching Grafana only to an internal network, which drops host publishing."""
    compose = _load_yaml("compose.yml")

    assert compose["services"]["grafana"]["networks"] == ["query", "access"]
    assert compose["networks"]["query"]["internal"] is True
    assert compose["networks"]["access"].get("internal") is not True
    assert "http://127.0.0.1:3000/api/health" in " ".join(compose["services"]["grafana"]["healthcheck"]["test"])


def test_alloy_runs_as_the_owner_of_its_persistent_data_directory():
    """Catch root-without-DAC access to the image's UID-473 data directory."""
    compose = _load_yaml("compose.yml")

    assert compose["services"]["alloy"]["user"] == "473:473"
    assert "alloy-data:/var/lib/alloy/data" in compose["services"]["alloy"]["volumes"]
    assert "/bin/alloy validate" in " ".join(compose["services"]["alloy"]["healthcheck"]["test"])


def test_loki_healthcheck_waits_for_request_readiness():
    """Catch reporting healthy before Loki can accept push and query requests."""
    compose = _load_yaml("compose.yml")

    healthcheck = compose["services"]["loki"]["healthcheck"]
    assert healthcheck["test"] == ["CMD", "/usr/bin/loki", "-health"]
    assert healthcheck["start_period"] == "30s"


def test_loki_retention_and_query_limits_are_bounded():
    config = _load_yaml("loki.yaml")

    assert config["auth_enabled"] is False
    assert config["common"]["replication_factor"] == 1
    assert config["schema_config"]["configs"][-1]["schema"] == "v13"
    assert config["schema_config"]["configs"][-1]["index"]["period"] == "24h"
    assert config["compactor"]["retention_enabled"] is True
    assert config["compactor"]["delete_request_store"] == "filesystem"

    limits = config["limits_config"]
    assert limits["retention_period"] == "168h"
    assert limits["max_entries_limit_per_query"] == 500
    assert limits["query_timeout"] == "10s"
    assert limits["max_query_lookback"] == "168h"
    assert limits["max_line_size"] >= 32_768
    assert config["querier"]["max_concurrent"] <= 2


def test_alloy_collects_only_opted_in_bot_and_avoids_high_cardinality_labels():
    alloy = (OBSERVABILITY / "config.alloy").read_text(encoding="utf-8")
    compact = " ".join(alloy.split())

    assert '"http://docker-proxy:2375"' in alloy
    assert 'name   = "label"' in alloy
    assert 'values = ["com.gemaibot.logs=true"]' in alloy
    assert "forward_to = [loki.process.bot.receiver]" in alloy
    assert '"http://loki:3100/loki/api/v1/push"' in alloy
    assert "wal {" in alloy
    assert "enabled = true" in compact

    labels_block = alloy.split("stage.labels", maxsplit=1)[1].split("}", maxsplit=1)[0]
    for allowed in ("environment", "level", "parse_status", "service_name"):
        assert allowed in labels_block
    for forbidden in ("event_id", "instance_id", "request_id", "trace_id", "user_id"):
        assert forbidden not in labels_block


def test_docker_proxy_has_a_deny_by_default_read_only_allowlist():
    config = (OBSERVABILITY / "docker-api-proxy.cfg").read_text(encoding="utf-8")

    assert "http-request deny unless method_read allowed_path" in config
    assert "path_reg" in config
    assert "/containers/json" in config
    assert "/containers/[0-9a-f]{12,64}/json" in config
    assert "/containers/[0-9a-f]{12,64}/logs" in config
    assert "^(/v[0-9.]+)?/networks$" in config
    assert "/containers/.*/" not in config
    assert "/events" in config
    assert "/exec" not in config
    assert "/archive" not in config
    assert "server docker /var/run/docker.sock" in config


def test_grafana_is_locked_down_and_dashboard_queries_are_bounded():
    compose = _load_yaml("compose.yml")
    grafana = compose["services"]["grafana"]
    environment = grafana["environment"]
    assert grafana["user"] == "472:0"
    assert environment["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert environment["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert environment["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_admin_password"

    datasource = _load_yaml("grafana/provisioning/datasources/loki.yaml")["datasources"][0]
    assert datasource["uid"] == "gemaibot-loki"
    assert datasource["url"] == "http://loki:3100"
    assert datasource["access"] == "proxy"
    assert datasource["jsonData"]["maxLines"] == 100
    assert datasource["jsonData"]["timeout"] == 10

    dashboard = json.loads((OBSERVABILITY / "grafana/dashboards/bot-logs.json").read_text(encoding="utf-8"))
    assert dashboard["time"] == {"from": "now-15m", "to": "now"}
    assert dashboard["refresh"] is False
    assert dashboard["liveNow"] is False
    assert all(target["maxLines"] <= 100 for panel in dashboard["panels"] for target in panel["targets"])


def test_ci_validates_vendor_configs_with_the_same_pinned_images():
    compose = _load_yaml("compose.yml")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docker compose -f ops/observability/compose.yml config --quiet" in workflow
    assert "validate --stability.level=experimental" in workflow
    assert "-verify-config" in workflow
    assert "haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg" in workflow
    assert "docker compose -f ops/observability/compose.yml up -d --wait --wait-timeout" in workflow
    assert "sudo chown root:root ops/observability/secrets/grafana_admin_password" in workflow
    assert "chmod 0640 ops/observability/secrets/grafana_admin_password" in workflow
    assert "http://127.0.0.1:3000/api/user" in workflow
    assert "DOCKER_SOCKET_GID=\"$(stat -c '%g' /var/run/docker.sock)\"" in workflow
    assert "export DOCKER_SOCKET_GID" in workflow
    assert "http://127.0.0.1:3000/api/health" in workflow
    assert "http://alloy:12345/-/ready" in workflow
    assert "http://loki:3100/ready" in workflow
    for service_name in ("alloy", "docker-proxy", "loki"):
        assert compose["services"][service_name]["image"] in workflow
