"""A pipeline against real SQL: the parent row, the child rows, and `run_log`.

The unit tier proves the rules — what loads, what is refused, what a step's
outcome does next. This tier proves the only thing it cannot: that a pipeline
run and its steps land in three different ledgers and come back out of one
view as a family, which is what `candlegraph runs --all` shows a person.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
    statuses = [s.status for s in steps_of(run)]
    assert statuses[0] == "failed"
    assert set(statuses[1:]) == {"skipped"}  # every step behind it, whatever they are
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


# --- events: a finished run starts a pipeline (rollout step 5) -----------------


def ingest_run(db: DatabaseManager, *, finished_at: datetime, outcome: str = "succeeded") -> str:
    """A closed ingest run in the ledger, which is what the dispatcher reads."""
    from candlegraph.ingestion import ledger

    run_id = str(uuid.uuid4())
    record = ledger.start_run(
        db, trigger="scheduled", lookback_days=1, now=finished_at - timedelta(seconds=10), run_id=run_id
    )
    ledger.finish_run(db, record, outcome, {}, {}, {}, now=finished_at)
    return run_id


@pytest.fixture
def events_db(hourly_db: DatabaseManager) -> DatabaseManager:
    hourly_db.execute(f'TRUNCATE "{hourly_db.schema}".run_queue, "{hourly_db.schema}".ingest_run')
    return hourly_db


def dispatch(db: DatabaseManager, *, now: datetime, **kwargs: Any) -> Any:
    from candlegraph.harness.events import dispatch_once

    return dispatch_once(
        db,
        make_settings(agent_report_sinks="log,ledger", min_expected_coverage=0.5),
        now=now,
        **kwargs,
    )


def test_a_successful_ingest_causes_exactly_one_hourly_run(events_db: DatabaseManager) -> None:
    """Step 5's exit criterion, and the reason the dedup key is the bar hour.

    Ingest polls every five minutes. Three successful runs inside one hour
    must produce **one** pipeline run, not three.
    """
    base = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    for minutes in (1, 6, 11):
        ingest_run(events_db, finished_at=base + timedelta(minutes=minutes))

    first = dispatch(events_db, now=base + timedelta(minutes=12))
    second = dispatch(events_db, now=base + timedelta(minutes=13))

    assert first.enqueued == 1
    assert first.duplicates == 2  # the other two ingests, collapsed
    assert second.enqueued == 0
    hourly = run_log(events_db, "hourly")
    assert len(hourly) == 1
    assert hourly[0]["trigger"] == "event"


def test_the_pipeline_run_points_at_the_ingest_that_caused_it(events_db: DatabaseManager) -> None:
    base = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    cause = ingest_run(events_db, finished_at=base + timedelta(minutes=1))

    dispatch(events_db, now=base + timedelta(minutes=2))

    (row,) = run_log(events_db, "hourly")
    assert row["parent_run_id"] == cause
    # And the steps still belong to the pipeline, not to the ingest run.
    steps = run_log(events_db, "signal")
    assert [s["parent_run_id"] for s in steps] == [row["run_id"]]


def test_a_failed_ingest_is_not_an_event(events_db: DatabaseManager) -> None:
    """The hourly pipeline's safety net still fires — but nothing was *caused*.

    Asserting "nothing ran" would be asserting the net does not work; what
    the trigger promises is that a failed ingest does not cause a run.
    """
    from candlegraph.harness import queue as run_queue

    base = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    ingest_run(events_db, finished_at=base + timedelta(minutes=1), outcome="failed")

    dispatch(events_db, now=base + timedelta(minutes=2))

    assert [row["trigger"] for row in run_queue.recent(events_db)] == ["scheduled"]
    assert [row["cause_run_id"] for row in run_queue.recent(events_db)] == [None]


def test_the_next_hour_runs_again(events_db: DatabaseManager) -> None:
    base = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    ingest_run(events_db, finished_at=base + timedelta(minutes=1))
    dispatch(events_db, now=base + timedelta(minutes=2))

    ingest_run(events_db, finished_at=base + timedelta(hours=1, minutes=1))
    dispatch(events_db, now=base + timedelta(hours=1, minutes=2))

    assert len(run_log(events_db, "hourly")) == 2


def test_an_event_from_before_the_lookback_window_is_not_acted_on(events_db: DatabaseManager) -> None:
    """The window bounds the catch-up; the schedule trigger is the net beyond it."""
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    ingest_run(events_db, finished_at=now - timedelta(hours=6))

    from candlegraph.harness import queue as run_queue

    summary = dispatch(events_db, now=now)

    assert summary.events_seen == 0
    # Only the safety net asked for anything, and it says so.
    assert [row["trigger"] for row in run_queue.recent(events_db)] == ["scheduled"]


def test_a_dispatcher_that_was_down_catches_up_on_its_next_tick(events_db: DatabaseManager) -> None:
    """No watermark to keep, so none to lose: the window *is* the catch-up."""
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    ingest_run(events_db, finished_at=now - timedelta(minutes=20))

    summary = dispatch(events_db, now=now)

    assert summary.enqueued == 1
    assert len(run_log(events_db, "hourly")) == 1


def test_the_queue_row_records_the_run_it_produced(events_db: DatabaseManager) -> None:
    from candlegraph.harness import queue as run_queue

    base = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    ingest_run(events_db, finished_at=base + timedelta(minutes=1))

    dispatch(events_db, now=base + timedelta(minutes=2))

    (entry,) = run_queue.recent(events_db)
    (run,) = run_log(events_db, "hourly")
    assert entry["state"] == "done"
    assert str(entry["run_id"]) == str(run["run_id"])
    assert entry["dedup_key"].startswith("hour:")


# --- the Pipelines dashboard's reads (rollout "the harness, visible", step 2) --


def recorded_run(db: DatabaseManager, job: str, started_at: datetime) -> str:
    from candlegraph.harness.ledger import close_run, open_run

    run_id = str(uuid.uuid4())
    open_run(db, run_id, job, "scheduled", started_at)
    close_run(
        db,
        run_id=run_id,
        job=job,
        trigger="scheduled",
        outcome="succeeded",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )
    return run_id


def test_pipeline_runs_are_capped_per_pipeline_not_overall(pipeline_db: DatabaseManager) -> None:
    """Under one shared LIMIT, twelve hourly runs would push the one daily run
    off the page and it would look as though it had never run. The window
    function keeps the newest N of *each*, and nothing that is not named."""
    base = datetime(2026, 11, 19, tzinfo=UTC)
    hourly = [recorded_run(pipeline_db, "hourly", base + timedelta(hours=h)) for h in range(12)]
    daily = recorded_run(pipeline_db, "daily", base - timedelta(days=1))
    recorded_run(pipeline_db, "fetch_news", base + timedelta(hours=20))

    rows = pipeline_db.fetch_all(
        pipeline_db.sql.get_pipeline_runs, {"jobs": ["daily", "hourly"], "per_pipeline": 10}
    )

    by_job: dict[str, list[str]] = {}
    for row in rows:
        by_job.setdefault(row["job"], []).append(str(row["run_id"]))
    assert by_job["hourly"] == list(reversed(hourly))[:10], "the newest ten, newest first"
    assert by_job["daily"] == [daily], "the rare pipeline is not crowded out"
    assert "fetch_news" not in by_job
    assert [row["started_at"] for row in rows] == sorted((row["started_at"] for row in rows), reverse=True)


def test_the_pipelines_view_reads_a_real_run_and_its_steps(
    pipeline_db: DatabaseManager, jobs: None, tmp_path: Any
) -> None:
    """The whole read path against real SQL: spec from disk, the run, its steps."""
    from candlegraph.ui.viewmodels import build_pipelines_view

    (tmp_path / "test_pipeline.yaml").write_text(
        "name: test_pipeline\nsteps:\n  - id: first\n    job: step_ok\n  - id: second\n    job: step_ok\n",
        encoding="utf-8",
    )
    spec = load_pipeline(tmp_path / "test_pipeline.yaml")
    run = run_pipeline(spec, make_settings(), db=pipeline_db, trigger=UI)

    view = build_pipelines_view(pipeline_db, make_settings(pipelines_dir=str(tmp_path)))

    family = view.latest
    assert family is not None and family.run_id == run.run_id
    assert family.trigger == UI and family.outcome == "succeeded"
    assert list(family.steps["step"]) == ["first", "second"]
    assert list(family.stages["stage"]) == ["first", "second"]
    assert not family.live
