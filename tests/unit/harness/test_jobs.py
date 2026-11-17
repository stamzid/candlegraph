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


# --- idempotence: the property that makes a job safe to be a pipeline step -------
#
# `Job.idempotent` is a claim, and a claim with nothing behind it is worth
# nothing — a step is re-run after a crash, so "safe to re-run" has to be
# shown. Each entry below names the test in this file that runs the job twice
# and compares the state it left. Marking a job idempotent without adding one
# fails the first test, which is the whole point of keeping this map by hand.

IDEMPOTENCE_PROOFS = {
    "observer": "test_running_the_observer_twice_writes_the_same_state",
}


def test_a_job_is_only_marked_idempotent_if_a_run_twice_proof_exists() -> None:
    declared = {job.name for job in JOBS.values() if job.idempotent}

    assert declared == set(IDEMPOTENCE_PROOFS), (
        "every job declaring `idempotent=True` needs an entry in IDEMPOTENCE_PROOFS and the "
        "run-twice test it names"
    )
    assert [name for name in IDEMPOTENCE_PROOFS.values() if name not in globals()] == []


def test_running_the_observer_twice_writes_the_same_state() -> None:
    """Two runs leave the same rows, apart from each run's own ledger row.

    The observer is a reader: its whole write surface is `agent_run` and the
    report sinks (invariant 21). So "the same state" means the second run
    wrote nothing the first did not, and found the same things.
    """
    from candlegraph.harness import run_job
    from tests.conftest import FakeDatabase, make_settings

    settings = make_settings(agent_report_sinks="log,ledger")
    db = FakeDatabase()

    first = run_job(JOBS["observer"], settings, db=db)
    writes_after_one = [label for label, _ in db.executed]
    second = run_job(JOBS["observer"], settings, db=db)
    writes_after_two = [label for label, _ in db.executed]

    # Its own ledger row, twice over, and nothing else.
    assert set(writes_after_one) == {"open_agent_run", "close_agent_run"}
    assert writes_after_two == writes_after_one * 2
    assert db.upserts == []
    assert first.outcome is second.outcome
    assert first.result is not None and second.result is not None
    assert first.result.verdict == second.result.verdict
    assert first.result.counts == second.result.counts
