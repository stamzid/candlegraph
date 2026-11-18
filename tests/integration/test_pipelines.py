"""A pipeline against real SQL: the parent row, the child rows, and `run_log`.

The unit tier proves the rules — what loads, what is refused, what a step's
outcome does next. This tier proves the only thing it cannot: that a pipeline
run and its steps land in three different ledgers and come back out of one
view as a family, which is what `candlegraph runs --all` shows a person.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from candlegraph.harness import load_pipeline, run_pipeline
from candlegraph.harness.jobs import JOBS, NEVER, Job, JobContext, run_ledger
from candlegraph.harness.pipelines import PipelineSpec, steps_of
from candlegraph.harness.runs import UI, Outcome, RunOutcome
from candlegraph.settings import REPO_ROOT
from candlegraph.storage import DatabaseManager
from candlegraph.tokens import get_token_registry
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


# --- the hourly pipeline, end to end (rollout step 4's exit criterion) ---------

HOUR = 3600
# Recent on purpose: the steps load prices through `load_symbol_series`,
# which bounds the window by ANALYTICS_LOOKBACK_DAYS from now. History from
# three years ago is correctly invisible to it.
BASE = int(datetime.now(tz=UTC).timestamp()) // HOUR * HOUR - 100 * HOUR


def hour_record(index: int, price: str, token: str, symbol: str) -> dict[str, Any]:
    period_start = BASE + index * HOUR
    return {
        "id": token,
        "symbol": symbol,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "priceUSD": price,
        "volume": "1",
        "volumeUSD": "1000",
        "untrackedVolumeUSD": "1000",
        "totalValueLocked": "10",
        "totalValueLockedUSD": "5000",
        "feesUSD": "3",
        "periodStartUnix": period_start,
        "timestamp": datetime.fromtimestamp(period_start, tz=UTC),
    }


# The deployment's own active tokens, because that is what the steps read.
# A pipeline step takes its parameters from the spec and nothing else
# (DESIGN §6.3), so there is no registry to inject — which is the point:
# this exercises the path production takes.
ACTIVE = tuple(token for token in get_token_registry().active)


@pytest.fixture
def hourly_db(pipeline_db: DatabaseManager) -> DatabaseManager:
    """A hundred clean hours for every active token, and an empty signal table."""
    pipeline_db.execute(f'TRUNCATE "{pipeline_db.schema}".signal')
    rows = [
        hour_record(i, f"{100 + offset + (i % 10):.4f}", token.address, token.symbol)
        for offset, token in enumerate(ACTIVE)
        for i in range(100)
    ]
    pipeline_db.upsert_many(
        pipeline_db.sql.insert_token_hours, pipeline_db.sql.insert_token_hours_template, rows
    )
    return pipeline_db


def test_the_hourly_pipeline_runs_every_step_as_one_family(hourly_db: DatabaseManager) -> None:
    """Stage 2's exit criterion, against real SQL.

    `explain` is included and is expected to do nothing useful here — there
    is no corpus — but it must not fail the run, which is what
    `on_failure: continue` is for.
    """
    spec = load_pipeline(REPO_ROOT / "pipelines" / "hourly.yaml")

    run = run_pipeline(
        spec,
        make_settings(agent_report_sinks="log,ledger", min_expected_coverage=0.5),
        db=hourly_db,
    )

    rows = {row["job"]: row for row in run_log(hourly_db)}
    assert rows["hourly"]["run_id"] == run.run_id
    for step in ("validate", "analyse", "signal", "explain"):
        assert step in rows, f"{step} left no row"
        assert rows[step]["parent_run_id"] == run.run_id
    # Not failed: the data is good, so nothing stopped the hour.
    assert run.outcome in {Outcome.SUCCEEDED, Outcome.PARTIAL}
    assert [s.status for s in steps_of(run)][:3] == ["succeeded", "succeeded", "succeeded"]


def test_the_hourly_pipeline_stores_signals_for_every_strategy(hourly_db: DatabaseManager) -> None:
    spec = load_pipeline(REPO_ROOT / "pipelines" / "hourly.yaml")

    run_pipeline(
        spec,
        make_settings(agent_report_sinks="log,ledger", min_expected_coverage=0.5),
        db=hourly_db,
    )

    stored = hourly_db.fetch_all(
        f'SELECT strategy, symbol, count(*) AS bars FROM "{hourly_db.schema}".signal '
        "GROUP BY strategy, symbol ORDER BY strategy, symbol"
    )
    by_strategy = {row["strategy"] for row in stored}
    assert {"bollinger", "crossover"} <= by_strategy
    assert {row["symbol"] for row in stored} == {token.symbol for token in ACTIVE}


def test_a_failed_validate_stops_the_pipeline_before_any_signal_is_stored(
    pipeline_db: DatabaseManager,
) -> None:
    """The reason `validate` is a step at all: no rows, so nothing computes."""
    pipeline_db.execute(f'TRUNCATE "{pipeline_db.schema}".signal, "{pipeline_db.schema}".token_hours_data')
    spec = load_pipeline(REPO_ROOT / "pipelines" / "hourly.yaml")

    run = run_pipeline(
        spec,
        make_settings(agent_report_sinks="log,ledger"),
        db=pipeline_db,
    )

    assert run.outcome is Outcome.FAILED
    assert [s.status for s in steps_of(run)] == ["failed", "skipped", "skipped", "skipped"]
    (count,) = pipeline_db.fetch_all(f'SELECT count(*) AS n FROM "{pipeline_db.schema}".signal')
    assert count["n"] == 0
    # And the steps that did not run left no rows pretending they did.
    assert run_log(pipeline_db, "signal") == []


def test_running_the_hourly_pipeline_twice_leaves_the_same_signals(hourly_db: DatabaseManager) -> None:
    """Every step is idempotent, so the pipeline is — which is what makes a resume safe."""
    spec = load_pipeline(REPO_ROOT / "pipelines" / "hourly.yaml")
    settings = make_settings(agent_report_sinks="log,ledger", min_expected_coverage=0.5)

    run_pipeline(spec, settings, db=hourly_db)
    first = hourly_db.fetch_all(
        f'SELECT strategy, symbol, period_start_unix, signal FROM "{hourly_db.schema}".signal '
        "ORDER BY strategy, symbol, period_start_unix"
    )
    run_pipeline(spec, settings, db=hourly_db)
    second = hourly_db.fetch_all(
        f'SELECT strategy, symbol, period_start_unix, signal FROM "{hourly_db.schema}".signal '
        "ORDER BY strategy, symbol, period_start_unix"
    )

    assert first == second
    assert len(first) > 0
