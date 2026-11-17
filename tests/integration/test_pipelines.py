"""A pipeline against real SQL: the parent row, the child rows, and `run_log`.

The unit tier proves the rules — what loads, what is refused, what a step's
outcome does next. This tier proves the only thing it cannot: that a pipeline
run and its steps land in three different ledgers and come back out of one
view as a family, which is what `candlegraph runs --all` shows a person.
"""

from __future__ import annotations

from typing import Any

import pytest

from candlegraph.harness import load_pipeline, run_pipeline
from candlegraph.harness.jobs import JOBS, NEVER, Job, JobContext, run_ledger
from candlegraph.harness.pipelines import PipelineSpec
from candlegraph.harness.runs import UI, Outcome, RunOutcome
from candlegraph.settings import REPO_ROOT
from candlegraph.storage import DatabaseManager
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def pipeline_db(db: DatabaseManager) -> DatabaseManager:
    db.execute(f'TRUNCATE "{db.schema}".run, "{db.schema}".agent_run, "{db.schema}".ingest_run')
    return db


def fake_job(name: str, outcome: Outcome) -> Job:
    def body(ctx: JobContext) -> RunOutcome:
        return RunOutcome(outcome=outcome, counts={"did": 1})

    return Job(
        name=name,
        run=body,
        schedule=NEVER,
        role="agents",
        writes=frozenset({"ledger"}),
        description=f"test job {name}",
        ledger=run_ledger,
        idempotent=True,
    )


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(JOBS, "step_ok", fake_job("step_ok", Outcome.SUCCEEDED))
    monkeypatch.setitem(JOBS, "step_failed", fake_job("step_failed", Outcome.FAILED))


def spec_of(*steps: str) -> PipelineSpec:
    return PipelineSpec.model_validate(
        {"name": "test_pipeline", "steps": [{"id": f"s{i}", "job": job} for i, job in enumerate(steps)]}
    )


def run_log(db: DatabaseManager, job: str | None = None) -> list[dict[str, Any]]:
    return db.fetch_all(db.sql.get_run_log, {"limit": 20, "job": job})


def test_a_pipeline_and_its_steps_are_one_family_in_run_log(pipeline_db: DatabaseManager, jobs: None) -> None:
    run = run_pipeline(spec_of("step_ok", "step_ok"), make_settings(), db=pipeline_db, trigger=UI)

    rows = {row["job"]: row for row in run_log(pipeline_db)}
    assert {"test_pipeline", "step_ok"} <= set(rows)
    parent = rows["test_pipeline"]
    assert parent["run_id"] == run.run_id
    assert parent["outcome"] == "succeeded"
    assert parent["parent_run_id"] is None
    # Every child points at the parent, and records why the pipeline ran.
    children = run_log(pipeline_db, "step_ok")
    assert len(children) == 2
    assert {row["parent_run_id"] for row in children} == {run.run_id}
    assert {row["trigger"] for row in children} == {UI}


def test_the_parent_row_records_each_step_including_the_skipped_ones(
    pipeline_db: DatabaseManager, jobs: None
) -> None:
    run = run_pipeline(spec_of("step_failed", "step_ok"), make_settings(), db=pipeline_db)

    (parent,) = run_log(pipeline_db, "test_pipeline")
    assert parent["outcome"] == "failed"
    # JSONB comes back decoded, so the step record is readable by the UI as is.
    steps = parent["detail"]["steps"]
    assert [step["status"] for step in steps] == ["failed", "skipped"]
    assert parent["detail"]["pipeline"] == "test_pipeline"
    assert parent["detail"]["as_of"]
    # The step that never ran has no run of its own, and says so.
    assert steps[1]["run_id"] is None
    assert run.outcome is Outcome.FAILED
    # And the step that was skipped left no row behind pretending it ran.
    assert run_log(pipeline_db, "step_ok") == []


def test_a_generic_run_row_carries_no_verdict(pipeline_db: DatabaseManager, jobs: None) -> None:
    """Outcome and verdict are different questions; only an agent answers the second."""
    run_pipeline(spec_of("step_ok"), make_settings(), db=pipeline_db)

    (parent,) = run_log(pipeline_db, "test_pipeline")
    assert parent["verdict"] is None
    assert float(parent["duration_seconds"]) >= 0.0


def test_the_committed_example_runs_against_a_real_database(pipeline_db: DatabaseManager) -> None:
    """The observer step writes to `agent_run`, so the family spans two ledgers."""
    spec = load_pipeline(REPO_ROOT / "pipelines" / "example.yaml")

    run = run_pipeline(spec, make_settings(agent_report_sinks="log,ledger"), db=pipeline_db)

    rows = {row["job"]: row for row in run_log(pipeline_db)}
    assert rows["example"]["run_id"] == run.run_id
    assert rows["observer"]["parent_run_id"] == run.run_id
    # The observer's verdict survives the view; the pipeline's outcome is its own.
    assert rows["observer"]["verdict"] in {"ok", "info", "warning", "critical"}
    assert rows["example"]["outcome"] in {"succeeded", "partial", "failed"}
