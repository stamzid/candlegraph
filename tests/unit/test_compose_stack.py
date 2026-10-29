"""The compose stack starts the same way every time, from any state.

Pins docs/incidents/2026-10-29-compose-startup-robustness/POSTMORTEM.md:
the API container sometimes would not start until Docker was cleaned by
hand, and `up --force-recreate` did not help. Four causes, four rules.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from candlegraph.settings import REPO_ROOT


def compose() -> dict:
    with (REPO_ROOT / "docker-compose.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def app_services(services: dict) -> dict[str, dict]:
    return {
        name: svc for name, svc in services.items() if str(svc.get("image", "")).startswith("candlegraph:")
    }


def test_the_app_image_is_tagged_per_compose_project() -> None:
    """Two worktrees building into one `candlegraph:latest` meant a `--build`
    in one retagged the image the other's `--force-recreate` then ran."""
    for name, svc in app_services(compose()["services"]).items():
        assert svc["image"] == "candlegraph:${COMPOSE_PROJECT_NAME}", name


def test_exactly_one_service_builds_the_image_and_the_rest_reuse_it() -> None:
    """Four services with `build:` for one tag made buildx export it four
    times concurrently; the losers failed with "already exists"."""
    services = app_services(compose()["services"])

    assert [name for name, svc in services.items() if "build" in svc] == ["candlegraph"]
    for name, svc in services.items():
        if name != "candlegraph":
            assert svc.get("pull_policy") == "never", f"{name} would try to pull a local-only image"


def test_exactly_one_container_applies_migrations() -> None:
    services = compose()["services"]
    migrators = [
        name
        for name, svc in app_services(services).items()
        if str(svc.get("environment", {}).get("RUN_MIGRATIONS", "true")).lower() == "true"
    ]

    assert migrators == ["candlegraph"]


def test_every_other_app_container_waits_for_the_migrator() -> None:
    services = compose()["services"]
    for name, svc in app_services(services).items():
        if name == "candlegraph":
            continue
        assert svc["depends_on"]["candlegraph"]["condition"] == "service_healthy", name


def test_app_restarts_are_bounded_so_a_broken_start_becomes_visible() -> None:
    """`unless-stopped` on a deterministic entrypoint failure is a silent
    crash loop; `--wait` says only "unhealthy" and the cause is in the log."""
    for name, svc in app_services(compose()["services"]).items():
        policy = str(svc["restart"])
        assert policy.startswith("on-failure:"), f"{name}: {policy}"
        assert 1 <= int(policy.split(":")[1]) <= 10, name


def test_the_postgres_probe_uses_tcp_so_the_initdb_temporary_server_cannot_pass_it() -> None:
    test = " ".join(compose()["services"]["postgres"]["healthcheck"]["test"])
    assert "pg_isready" in test
    assert "-h 127.0.0.1" in test


@pytest.fixture
def fake_alembic(tmp_path: Path) -> Path:
    """An `alembic` on PATH that records being called instead of migrating."""
    marker = tmp_path / "alembic-ran"
    script = tmp_path / "alembic"
    script.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return marker


def run_entrypoint(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(REPO_ROOT / "entrypoint.sh"), "echo", "handed-off"],
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_entrypoint_migrates_by_default(tmp_path: Path, fake_alembic: Path) -> None:
    result = run_entrypoint(tmp_path, {})

    assert result.returncode == 0, result.stderr
    assert fake_alembic.read_text().strip() == "upgrade head"
    assert "handed-off" in result.stdout


def test_the_entrypoint_skips_migrations_when_told_another_container_owns_them(
    tmp_path: Path, fake_alembic: Path
) -> None:
    result = run_entrypoint(tmp_path, {"RUN_MIGRATIONS": "false"})

    assert result.returncode == 0, result.stderr
    assert not fake_alembic.exists()
    assert "skipping migrations" in result.stdout
    assert "handed-off" in result.stdout


def test_a_failed_migration_still_stops_the_container(tmp_path: Path) -> None:
    """Serving against an unmigrated schema is worse than not starting."""
    script = tmp_path / "alembic"
    script.write_text("#!/bin/sh\nexit 7\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    result = run_entrypoint(tmp_path, {})

    assert result.returncode == 7
    assert "handed-off" not in result.stdout
