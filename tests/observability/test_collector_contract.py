from __future__ import annotations

import json
import re
import shlex
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
    assert compose["services"]["alloy"]["healthcheck"]["test"] == ["CMD", "/usr/bin/bash", "/etc/alloy/healthcheck.sh"]
    assert "./alloy-healthcheck.sh:/etc/alloy/healthcheck.sh:ro" in compose["services"]["alloy"]["volumes"]


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


def test_alloy_collects_only_the_two_opted_in_bots_and_avoids_high_cardinality_labels():
    alloy = (OBSERVABILITY / "config.alloy").read_text(encoding="utf-8")
    compact = " ".join(alloy.split())

    assert '"http://docker-proxy:2375"' in alloy
    assert 'name   = "label"' in alloy
    assert 'values = ["com.gemaibot.logs=true"]' in alloy
    relabel = alloy.split('discovery.relabel "bot"', maxsplit=1)[1].split('loki.source.docker "bot"', maxsplit=1)[0]
    assert 'source_labels = ["__meta_docker_container_name"]' in relabel
    assert 'regex         = "/tg-bot"' in relabel
    ytdlbot_relabel = re.search(r'discovery\.relabel "ytdlbot" \{(.*?)^\}', alloy, re.DOTALL | re.MULTILINE)
    assert ytdlbot_relabel is not None
    assert "targets = discovery.docker.bot.targets" in ytdlbot_relabel.group(1)
    assert 'source_labels = ["__meta_docker_container_label_com_gemaibot_logs"]' in ytdlbot_relabel.group(1)
    assert 'source_labels = ["__meta_docker_container_name"]' in ytdlbot_relabel.group(1)
    assert 'regex         = "/ytdlbot-bot-1"' in ytdlbot_relabel.group(1)
    ytdlbot_source = re.search(r'loki\.source\.docker "ytdlbot" \{(.*?)^\}', alloy, re.DOTALL | re.MULTILINE)
    assert ytdlbot_source is not None
    assert "targets          = discovery.relabel.ytdlbot.output" in ytdlbot_source.group(1)
    assert 'service_name = "ytdlbot"' in ytdlbot_source.group(1)
    assert "forward_to = [loki.process.bot.receiver]" in ytdlbot_source.group(1)
    assert "forward_to = [loki.process.bot.receiver]" in alloy
    assert '"http://loki:3100/loki/api/v1/push"' in alloy
    assert "wal {" in alloy
    assert "enabled = true" in compact

    labels_block = alloy.split("stage.labels", maxsplit=1)[1].split("}", maxsplit=1)[0]
    for allowed in ("environment", "level", "parse_status", "service_name"):
        assert allowed in labels_block
    for forbidden in ("event_id", "instance_id", "request_id", "trace_id", "user_id"):
        assert forbidden not in labels_block


def test_alloy_http_log_streams_are_not_restarted_on_the_discovery_interval():
    """Keep fast discovery without applying its timeout to long-lived log streams."""
    alloy = (OBSERVABILITY / "config.alloy").read_text(encoding="utf-8")

    assert re.search(
        r'discovery\.docker "bot" \{.*?refresh_interval\s*=\s*"15s"',
        alloy,
        re.DOTALL,
    )
    assert re.search(
        r'loki\.source\.docker "bot" \{.*?refresh_interval\s*=\s*"24h"',
        alloy,
        re.DOTALL,
    )
    assert re.search(
        r'loki\.source\.docker "ytdlbot" \{.*?refresh_interval\s*=\s*"24h"',
        alloy,
        re.DOTALL,
    )


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

    provider = _load_yaml("grafana/provisioning/dashboards/default.yaml")["providers"][0]
    assert provider["folder"] == "Bot Logs"

    for filename, uid, service, datasource_uid in (
        ("bot-logs.json", "gemaibot-logs", "gemaibotv2", "gemaibot-loki"),
        ("ytdlbot-logs.json", "ytdlbot-logs", "ytdlbot", "ytdlbot-loki"),
    ):
        dashboard = json.loads((OBSERVABILITY / "grafana/dashboards" / filename).read_text(encoding="utf-8"))
        assert dashboard["uid"] == uid
        assert dashboard["time"] == {"from": "now-15m", "to": "now"}
        assert dashboard["refresh"] is False
        assert dashboard["liveNow"] is False
        for panel in dashboard["panels"]:
            assert panel["datasource"]["uid"] == datasource_uid
            for target in panel["targets"]:
                assert target["maxLines"] <= 100
                assert target["datasource"]["uid"] == datasource_uid
                assert f'service_name="{service}"' in target["expr"]

    ytdlbot_datasource = _load_yaml("grafana/provisioning/datasources/ytdlbot.yaml")["datasources"][0]
    assert ytdlbot_datasource["uid"] == "ytdlbot-loki"
    assert ytdlbot_datasource["url"] == "http://loki:3100"
    assert ytdlbot_datasource["access"] == "proxy"
    assert ytdlbot_datasource["jsonData"]["maxLines"] == 100
    for field in ytdlbot_datasource["jsonData"]["derivedFields"]:
        assert "service_name=" in field["url"]
        assert "ytdlbot" in field["url"]
        assert "gemaibotv2" not in field["url"]


def test_ci_validates_vendor_configs_with_the_same_pinned_images():
    compose = _load_yaml("compose.yml")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docker compose -f ops/observability/compose.yml config --quiet" in workflow
    assert "validate --stability.level=experimental" in workflow
    assert "-verify-config" in workflow
    assert "haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg" in workflow
    assert "docker compose -f ops/observability/compose.yml up -d --wait --wait-timeout" in workflow
    assert "http://127.0.0.1:3000/api/user" in workflow
    assert "DOCKER_SOCKET_GID=\"$(stat -c '%g' /var/run/docker.sock)\"" in workflow
    assert "export DOCKER_SOCKET_GID" in workflow
    assert "http://127.0.0.1:3000/api/health" in workflow
    assert "http://alloy:12345/-/ready" in workflow
    assert "http://loki:3100/ready" in workflow
    for service_name in ("alloy", "docker-proxy", "loki"):
        assert compose["services"][service_name]["image"] in workflow


def test_ci_grafana_secret_allows_the_host_probe_and_container_but_no_other_users():
    """Catch root-only host access or loss of Grafana's group access to the shared secret."""
    compose = _load_yaml("compose.yml")
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["observability-config"]["steps"]
    run = next(step["run"] for step in steps if "docker compose" in step.get("run", ""))
    grafana = compose["services"]["grafana"]
    assert grafana["secrets"] == ["grafana_admin_password"]
    assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_admin_password"
    container_uid, container_gid = map(int, grafana["user"].split(":"))
    secret_default = compose["secrets"]["grafana_admin_password"]["file"].split(":-", 1)[1].removesuffix("}")
    secret = (Path("ops/observability") / secret_default).as_posix()

    # Both actual host consumers use StackProbe, whose constructor reads this file.
    probe_passwords = re.findall(r"--password-file\s+(\S+)", run)
    e2e_passwords = re.findall(r"export TEST_GRAFANA_PASSWORD_FILE=(\S+)", run)
    assert probe_passwords == e2e_passwords == [secret]

    runner_uid, runner_gid = 1001, 1001
    commands = [shlex.split(line.rstrip("\\").replace("$(id -u)", str(runner_uid))) for line in run.splitlines()]
    owners = [command[-2] for command in commands if command[:2] == ["sudo", "chown"] and command[-1] == secret]
    modes = [command[-2] for command in commands if command[:2] == ["sudo", "chmod"] and command[-1] == secret]
    assert len(owners) == len(modes) == 1
    owner, group = (0 if value == "root" else int(value) for value in owners[0].split(":"))
    mode = int(modes[0], 8)

    def can_read(uid: int, gid: int) -> bool:
        permission = 0o400 if uid == owner else 0o040 if gid == group else 0o004
        return bool(mode & permission)

    assert can_read(runner_uid, runner_gid), "the non-root CI host cannot read its Grafana probe password"
    assert can_read(container_uid, container_gid), "Grafana cannot read its bind-mounted secret"
    assert mode == 0o640
    assert not can_read(2000, 2000), "the Grafana password must not be world-readable"


def test_ci_checks_host_secret_readability_before_starting_docker():
    """Catch permission failures before pulling or starting the disposable stack."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["observability-config"]["steps"]
    run = next(step["run"] for step in steps if "docker compose" in step.get("run", ""))
    secret = re.search(r"--password-file\s+(\S+)", run).group(1)
    commands = [shlex.split(line.rstrip("\\")) for line in run.splitlines()]
    readability_check = ["test", "-r", secret]
    assert readability_check in commands, "CI must check host password readability before starting Docker"
    check = commands.index(readability_check)
    permission_setup = next(i for i, command in enumerate(commands) if command[:2] == ["sudo", "chmod"])
    docker_start = next(i for i, command in enumerate(commands) if command and command[0] == "docker")
    assert permission_setup < check < docker_start
