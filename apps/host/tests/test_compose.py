"""Production Compose topology tests."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from snektest import assert_eq, assert_false, assert_true, test

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OPEN_WEBUI_IMAGE = (
    "ghcr.io/open-webui/open-webui:v0.11.1@"
    "sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16"
)


def _compose_config() -> dict[str, Any]:
    """Resolve Compose with deterministic non-secret test settings."""
    environment = os.environ.copy()
    environment.update(
        {
            "TETHER_API_TOKEN": "test-capture-token",
            "TETHER_ENV_FILE": "deploy/.env.example",
            "TETHER_OPEN_WEBUI_TOKEN": "test-open-webui-token",
            "WEBUI_SECRET_KEY": "test-webui-secret",
            "WEBUI_URL": "http://127.0.0.1:3000",
        }
    )
    completed = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    document: dict[str, Any] = json.loads(completed.stdout)
    return document


@test(mark="slow")
def open_webui_uses_the_pinned_image_and_isolated_volume() -> None:
    """The external assistant has one loopback port and only its own state mount."""
    open_webui: dict[str, Any] = _compose_config()["services"]["open-webui"]

    assert_eq(open_webui["image"], OPEN_WEBUI_IMAGE)
    assert_eq(open_webui["ports"][0]["host_ip"], "127.0.0.1")
    assert_eq(open_webui["ports"][0]["published"], "3000")
    assert_eq(open_webui["ports"][0]["target"], 8080)
    assert_eq(len(open_webui["volumes"]), 1)
    assert_eq(open_webui["volumes"][0]["source"], "open-webui-data")
    assert_eq(open_webui["volumes"][0]["target"], "/app/backend/data")
    assert_true(open_webui["volumes"][0]["type"] == "volume")


@test(mark="slow")
def open_webui_starts_with_restricted_production_features() -> None:
    """Code execution and unattended tools stay disabled at the container seam."""
    open_webui: dict[str, Any] = _compose_config()["services"]["open-webui"]

    assert_eq(
        open_webui["environment"],
        {
            "ENABLE_AUTOMATIONS": "false",
            "ENABLE_CODE_EXECUTION": "false",
            "ENABLE_CODE_INTERPRETER": "false",
            "ENABLE_OLLAMA_API": "false",
            "ENABLE_PERSISTENT_CONFIG": "true",
            "ENABLE_SIGNUP": "false",
            "ENABLE_TOOL_PERMISSIONS": "true",
            "WEBUI_SECRET_KEY": "test-webui-secret",
            "WEBUI_URL": "http://127.0.0.1:3000",
        },
    )
    assert_true("env_file" not in open_webui)


@test(mark="slow")
def open_webui_waits_for_the_host_health_route() -> None:
    """Startup ordering uses health without coupling later container restarts."""
    services: dict[str, Any] = _compose_config()["services"]

    assert_eq(
        services["open-webui"]["depends_on"],
        {"host": {"condition": "service_healthy", "required": True}},
    )
    assert_true(
        "http://127.0.0.1:8000/health"
        in " ".join(services["host"]["healthcheck"]["test"])
    )


@test(mark="slow")
def host_has_no_pi_runtime_mount_or_configuration() -> None:
    """The retained capability host cannot read the rollback-only Pi credential."""
    host: dict[str, Any] = _compose_config()["services"]["host"]

    assert_true("PI_CODING_AGENT_DIR" not in host["environment"])
    assert_true(all(volume["target"] != "/pi-agent" for volume in host["volumes"]))


@test(mark="slow")
def host_receives_only_its_two_credentials_and_one_data_volume() -> None:
    """Open WebUI and retired integration secrets stay outside the host container."""
    host: dict[str, Any] = _compose_config()["services"]["host"]

    assert_eq(
        host["environment"],
        {
            "TETHER_API_TOKEN": "test-capture-token",
            "TETHER_DATABASE_PATH": "/data/tether.sqlite3",
            "TETHER_LOGGING_LEVEL": "INFO",
            "TETHER_OPEN_WEBUI_TOKEN": "test-open-webui-token",
            "TETHER_TELEMETRY_DATABASE_PATH": "/data/telemetry.sqlite3",
        },
    )
    assert_true("env_file" not in host)
    assert_eq(
        host["volumes"],
        [
            {
                "type": "volume",
                "source": "data",
                "target": "/data",
                "volume": {},
            }
        ],
    )


@test(mark="slow")
def open_webui_has_a_bounded_container_health_check() -> None:
    """Deployment surfaces a stuck external application within a few seconds."""
    healthcheck: dict[str, Any] = _compose_config()["services"]["open-webui"][
        "healthcheck"
    ]

    assert_true("http://127.0.0.1:8080/health" in " ".join(healthcheck["test"]))
    assert_eq(healthcheck["interval"], "2s")
    assert_eq(healthcheck["timeout"], "2s")


@test()
def host_image_contains_only_the_python_capability_host() -> None:
    """The production image carries no deleted assistant runtime or frontend."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert_eq(dockerfile.count("\nFROM "), 1)
    for deleted_runtime in (
        "node:",
        "apps/agent",
        "apps/web",
        "COPY apps/host/ /app/apps/host/",
        "PI_CODING_AGENT_DIR",
        "TETHER_WEB_DIST",
    ):
        assert_false(deleted_runtime in dockerfile)
    assert_true("COPY apps/host/tether/ ./tether/" in dockerfile)


@test()
def task_runner_has_no_deleted_assistant_workflows() -> None:
    """Root workflows target the headless host and standalone integration smoke."""
    task_runner = (PROJECT_ROOT / "justfile").read_text()

    for deleted_workflow in (
        "apps/agent",
        "apps/web",
        "pi-auth",
        "validate-web-smoke",
        "TETHER_APP_PASSWORD",
        "TETHER_SESSION_SECRET",
        ":wear:",
    ):
        assert_false(deleted_workflow in task_runner)
    assert_true("validate-open-webui-smoke" in task_runner)
