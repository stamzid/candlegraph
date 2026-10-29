"""The health probe: how often it runs, what it costs, and what it logs.

Pins docs/incidents/2026-10-29-health-probe-cadence/POSTMORTEM.md. The API
log at start-up was one `GET /status` line every ten seconds, each one a
Python process and two queries, and none of it told anyone anything.
"""

from __future__ import annotations

import logging

import yaml

from candlegraph.settings import REPO_ROOT, Settings
from candlegraph.utils.logging_utils import (
    UVICORN_ACCESS_LOGGER,
    HealthProbeFilter,
    quiet_health_probes,
)

STEADY_STATE_SECONDS = 180


def access_record(method: str, path: str, status: int) -> logging.LogRecord:
    """A record shaped the way uvicorn's access logger emits one."""
    return logging.LogRecord(
        UVICORN_ACCESS_LOGGER,
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", method, path, "1.1", status),
        None,
    )


def test_a_successful_probe_is_not_logged() -> None:
    assert HealthProbeFilter().filter(access_record("GET", "/status", 200)) is False


def test_a_failing_probe_is_still_logged() -> None:
    """Silence is only for the probe saying nothing happened."""
    assert HealthProbeFilter().filter(access_record("GET", "/status", 503)) is True
    assert HealthProbeFilter().filter(access_record("GET", "/status", 500)) is True


def test_real_requests_are_logged() -> None:
    assert HealthProbeFilter().filter(access_record("POST", "/graphql", 200)) is True
    assert HealthProbeFilter().filter(access_record("GET", "/graphql", 200)) is True


def test_a_record_that_is_not_an_access_line_passes_through() -> None:
    record = logging.LogRecord(UVICORN_ACCESS_LOGGER, logging.INFO, __file__, 0, "plain %s", ("x",), None)
    assert HealthProbeFilter().filter(record) is True


def test_quieting_is_idempotent_and_reversible() -> None:
    access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    try:
        quiet_health_probes(True)
        quiet_health_probes(True)
        assert sum(isinstance(f, HealthProbeFilter) for f in access.filters) == 1
        quiet_health_probes(False)
        assert not any(isinstance(f, HealthProbeFilter) for f in access.filters)
    finally:
        quiet_health_probes(False)


def test_probe_logging_is_off_by_default_and_typed() -> None:
    assert Settings(_env_file=None).log_health_probes is False
    assert Settings(_env_file=None, log_health_probes=True).log_health_probes is True


def seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    return int(value[:-1]) * units[value[-1]]


def test_every_compose_probe_is_fast_at_start_and_slow_afterwards() -> None:
    """start_interval covers start-up; interval is the steady state.

    Health matters to compose only at start-up (`--wait`, `depends_on`), and
    Docker never acts on an unhealthy container by itself, so after the first
    success a probe every few seconds is pure cost.
    """
    with (REPO_ROOT / "docker-compose.yaml").open(encoding="utf-8") as handle:
        services = yaml.safe_load(handle)["services"]

    probed = {name: svc["healthcheck"] for name, svc in services.items() if "healthcheck" in svc}
    assert set(probed) == {"postgres", "candlegraph", "ui"}

    for name, check in probed.items():
        assert seconds(check["interval"]) >= STEADY_STATE_SECONDS, f"{name} probes too often at steady state"
        assert seconds(check["start_interval"]) <= 5, f"{name} must still come up quickly"
        assert seconds(check["start_period"]) >= 30, f"{name} start_period must outlast a cold start"


def test_only_the_api_container_migrates_a_fresh_volume() -> None:
    """Every other app container waits for the API to be healthy.

    The shared entrypoint runs `alembic upgrade head`. Two containers doing
    that at once against an empty schema race on `CREATE TABLE
    alembic_version`, and the loser dies on a duplicate key in `pg_type`.
    The worker and agents already waited; the UI did not, and lost that
    race on a fresh stack.
    """
    with (REPO_ROOT / "docker-compose.yaml").open(encoding="utf-8") as handle:
        services = yaml.safe_load(handle)["services"]

    app_services = {
        name for name, svc in services.items() if str(svc.get("image", "")).startswith("candlegraph:")
    }
    assert "candlegraph" in app_services
    for name in app_services - {"candlegraph"}:
        waits_on = services[name].get("depends_on", {})
        assert waits_on.get("candlegraph", {}).get("condition") == "service_healthy", (
            f"{name} would run migrations concurrently with the API"
        )
