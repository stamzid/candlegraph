"""The registry is what the CLI, the scheduler and the docs read; hold it to the rules."""

from __future__ import annotations

import importlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from candlegraph.harness import JOBS, select
from candlegraph.harness.jobs import ROLES, WRITE_SURFACES
from candlegraph.settings import Settings
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[3]

SCHEDULED = [job.name for job in JOBS.values() if job.scheduled]
STEPS_ONLY = [job.name for job in JOBS.values() if not job.scheduled]


def test_the_registered_jobs_are_the_ones_the_docs_name() -> None:
    assert list(JOBS) == [
        # On a clock, hosted by a process.
        "ingest",
        "observer",
        "janitor",
        "fetch_news",
        "dispatch",
        "prune",
        # Pipeline steps: no clock, ordered by a spec.
        "validate",
        "analyse",
        "signal",
        "explain",
        "refresh_aggregates",
        "embed",
    ]


@pytest.mark.parametrize("name", SCHEDULED)
def test_every_scheduled_job_reads_an_interval_setting_that_exists_and_is_documented(name: str) -> None:
    setting = JOBS[name].interval_setting
    assert setting in Settings.model_fields
    assert f"{setting.upper()}=" in (ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", STEPS_ONLY)
def test_a_pipeline_step_has_no_clock_and_asks_settings_for_nothing(name: str) -> None:
    """A step runs when its pipeline runs; a timer for one would undo the ordering."""
    job = JOBS[name]

    assert job.scheduled is False
    # `seconds()` must answer without reading a setting that does not exist.
    assert job.schedule.seconds(make_settings()) == 0
    assert job not in select(role=job.role)
    assert select(names=[name]) == (job,)


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
    # The pipeline steps share the agents role but have no clock, so the
    # agents process is not handed them.
    assert [job.name for job in select(role="agents")] == [
        "observer",
        "janitor",
        "fetch_news",
        "dispatch",
        "prune",
    ]
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

HERE = "tests.unit.harness.test_jobs"
STEPS = "tests.unit.harness.test_steps"
EVENTS = "tests.unit.harness.test_events"
AGGS = "tests.integration.test_aggregates"

IDEMPOTENCE_PROOFS = {
    "observer": f"{HERE}:test_running_the_observer_twice_writes_the_same_state",
    "fetch_news": f"{STEPS}:test_running_fetch_news_twice_writes_the_same_state",
    "dispatch": f"{EVENTS}:test_running_dispatch_twice_writes_the_same_state",
    "validate": f"{STEPS}:test_running_validate_twice_writes_the_same_state",
    "analyse": f"{STEPS}:test_running_analyse_twice_writes_the_same_state",
    "signal": f"{STEPS}:test_running_signal_twice_writes_the_same_state",
    "explain": f"{STEPS}:test_running_explain_twice_writes_the_same_state",
    "embed": f"{STEPS}:test_running_embed_twice_writes_the_same_state",
    # Proved against real SQL: "refreshing twice changes nothing" is a claim
    # about TimescaleDB's invalidation log, which a fake cannot answer.
    "refresh_aggregates": f"{AGGS}:test_running_refresh_aggregates_twice_writes_the_same_state",
}


def test_a_job_is_only_marked_idempotent_if_a_run_twice_proof_exists() -> None:
    declared = {job.name for job in JOBS.values() if job.idempotent}

    assert declared == set(IDEMPOTENCE_PROOFS), (
        "every job declaring `idempotent=True` needs an entry in IDEMPOTENCE_PROOFS and the "
        "run-twice test it names"
    )
    missing = []
    for reference in IDEMPOTENCE_PROOFS.values():
        module, _, name = reference.partition(":")
        if not hasattr(importlib.import_module(module), name):
            missing.append(reference)
    assert missing == [], f"IDEMPOTENCE_PROOFS names tests that do not exist: {missing}"


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


# --- prune (rollout "the harness, visible", step 4) -----------------------------


def test_prune_runs_daily_in_the_agents_process_and_writes_only_the_ledger() -> None:
    job = JOBS["prune"]

    assert job.role == "agents" and job.writes == frozenset({"ledger"})
    assert job.interval_setting == "prune_interval"
    assert make_settings().prune_interval == 86400
    assert make_settings().run_retention_days == 30


def test_prune_is_not_a_pipeline_step() -> None:
    """Housekeeping inside a pipeline would delete the history the pipeline is
    writing; the idempotence flag fails closed, so the loader refuses it."""
    assert JOBS["prune"].idempotent is False


def test_prune_asks_for_everything_older_than_the_retention_and_counts_it() -> None:
    from candlegraph.harness.runs import Outcome, run_job
    from tests.conftest import FakeDatabase

    db = FakeDatabase()
    db.delete_rowcount = 7
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    run = run_job(
        JOBS["prune"], make_settings(run_retention_days=30), trigger="manual", db=db, clock=lambda: now
    )

    assert run.outcome is Outcome.SUCCEEDED
    assert run.result is not None and run.result.counts == {"deleted": 7, "retention_days": 30}
    [params] = [p for label, p in db.executed if label == "prune_runs"]
    assert params == {"before": now - timedelta(days=30)}


def test_a_prune_that_finds_nothing_old_enough_still_succeeded() -> None:
    from candlegraph.harness.runs import Outcome, run_job
    from tests.conftest import FakeDatabase

    run = run_job(JOBS["prune"], make_settings(), trigger="manual", db=FakeDatabase())

    assert run.outcome is Outcome.SUCCEEDED
    assert run.result is not None and run.result.counts["deleted"] == 0


def test_prune_never_names_a_detailed_ledger() -> None:
    """`ingest_run` and `agent_run` are what the observer's drift rules read."""
    from tests.unit.ingestion.test_runner import _delete_targets

    assert _delete_targets()["prune_runs"] == {"run"}
