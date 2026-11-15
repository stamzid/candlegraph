"""The registry is what the CLI, the scheduler and the docs read; hold it to the rules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from candlegraph.harness import JOBS, select
from candlegraph.harness.jobs import ROLES, WRITE_SURFACES
from candlegraph.settings import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_the_registered_jobs_are_the_four_the_docs_name() -> None:
    assert list(JOBS) == ["ingest", "observer", "janitor", "fetch_news"]


@pytest.mark.parametrize("name", list(JOBS))
def test_every_job_reads_an_interval_setting_that_exists_and_is_documented(name: str) -> None:
    setting = JOBS[name].interval_setting
    assert setting in Settings.model_fields
    assert f"{setting.upper()}=" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_only_ingest_declares_market_data() -> None:
    """Invariant 21, applied to the registry: agents never write market data."""
    assert [job.name for job in JOBS.values() if "market_data" in job.writes] == ["ingest"]


@pytest.mark.parametrize("name", list(JOBS))
def test_every_job_declares_a_known_write_surface_and_role(name: str) -> None:
    job = JOBS[name]
    assert job.writes and job.writes <= WRITE_SURFACES
    assert job.role in ROLES
    assert job.description


def test_selection_by_role_and_by_name() -> None:
    assert [job.name for job in select(role="worker")] == ["ingest"]
    assert [job.name for job in select(role="agents")] == ["observer", "janitor", "fetch_news"]
    assert [job.name for job in select(names=["janitor"])] == ["janitor"]
    with pytest.raises(ValueError, match="unknown job"):
        select(names=["nope"])
    with pytest.raises(ValueError):
        select()


def test_importing_the_harness_imports_no_job_body() -> None:
    """Invariant 1: a registry importing the observer would pull LangGraph into `serve`."""
    code = (
        "import sys, candlegraph.harness, candlegraph.harness.scheduler, candlegraph.harness.process;"
        "bad = [m for m in ('langgraph', 'candlegraph.agents.observer', 'candlegraph.agents.janitor',"
        " 'candlegraph.ingestion.runner') if m in sys.modules]; print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    assert out.stdout.strip() == ""


# --- invariant 27: every run is a Run --------------------------------------------


def test_only_the_harness_scheduler_names_backgroundscheduler() -> None:
    """One scheduler builder, so every job gets the same overlap and misfire policy."""
    package = ROOT / "candlegraph"
    users = sorted(
        str(path.relative_to(ROOT))
        for path in package.rglob("*.py")
        if "BackgroundScheduler" in path.read_text(encoding="utf-8")
    )
    assert users == ["candlegraph/harness/scheduler.py"]


@pytest.mark.parametrize("name", list(JOBS))
def test_every_job_writes_a_ledger_row_under_the_default_configuration(name: str) -> None:
    """A registered job with no ledger hook would run unrecorded."""
    from tests.conftest import FakeDatabase, make_settings

    hook = JOBS[name].ledger(make_settings(agent_report_sinks="log,ledger"), FakeDatabase(), {})
    assert hook is not None
    assert all(callable(getattr(hook, event)) for event in ("started", "stage", "finished"))
