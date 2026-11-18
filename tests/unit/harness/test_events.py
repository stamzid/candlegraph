"""The dispatcher: what a finished run starts, how often, and exactly once.

Most of this is about the **dedup key**, because that one string is what
"exactly one hourly run per ingest" reduces to. The queue's unique index does
the enforcing; these tests pin what is handed to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from candlegraph.harness import JOBS, Outcome, run_job
from candlegraph.harness.events import (
    DispatchSummary,
    dispatch_once,
    due_schedules,
    load_specs,
    matches_for_event,
    watched_jobs,
)
from candlegraph.harness.pipelines import (
    PipelineCycleError,
    PipelineSpec,
    check_cycles,
    event_dedup_key,
    manual_dedup_key,
    schedule_dedup_key,
)
from candlegraph.settings import REPO_ROOT
from tests.conftest import FakeDatabase, make_settings

NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)


def spec(name: str = "hourly", **extra: Any) -> PipelineSpec:
    return PipelineSpec.model_validate({"name": name, "steps": [{"id": "s", "job": "observer"}], **extra})


def on_ingest(**event: Any) -> PipelineSpec:
    return spec(triggers=[{"event": {"job": "ingest", **event}}])


@contextmanager
def always_free(_db: Any, _pipeline: str) -> Iterator[bool]:
    """The advisory lock, granted. Real locking is an integration concern."""
    yield True


@contextmanager
def always_taken(_db: Any, _pipeline: str) -> Iterator[bool]:
    yield False


def finished_run(job: str = "ingest", outcome: str = "succeeded", **extra: Any) -> dict[str, Any]:
    return {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "job": job,
        "trigger": "scheduled",
        "outcome": outcome,
        "verdict": None,
        "started_at": NOW - timedelta(seconds=30),
        "finished_at": NOW,
        "parent_run_id": None,
        **extra,
    }


# --- what matches ---------------------------------------------------------------


def test_a_successful_ingest_matches_a_pipeline_that_watches_it() -> None:
    found = matches_for_event([on_ingest()], job="ingest", outcome="succeeded", run_id="r1", finished_at=NOW)

    assert [m.pipeline for m in found] == ["hourly"]
    assert found[0].trigger == "event"
    assert found[0].cause_run_id == "r1"


def test_an_outcome_the_trigger_does_not_name_matches_nothing() -> None:
    assert matches_for_event([on_ingest()], job="ingest", outcome="failed", run_id="r", finished_at=NOW) == []


def test_partial_counts_when_the_trigger_asks_for_it() -> None:
    """An hour where one token errored is still an hour of new data for the rest."""
    watcher = on_ingest(outcome=["succeeded", "partial"])

    found = matches_for_event([watcher], job="ingest", outcome="partial", run_id="r", finished_at=NOW)

    assert len(found) == 1


def test_another_job_finishing_matches_nothing() -> None:
    assert (
        matches_for_event([on_ingest()], job="observer", outcome="succeeded", run_id="r", finished_at=NOW)
        == []
    )


def test_one_pipeline_runs_once_however_many_of_its_triggers_match() -> None:
    both = spec(
        triggers=[
            {"event": {"job": "ingest", "outcome": ["succeeded"]}},
            {"event": {"job": "ingest", "outcome": ["succeeded", "partial"]}},
        ]
    )

    found = matches_for_event([both], job="ingest", outcome="succeeded", run_id="r", finished_at=NOW)

    assert len(found) == 1


def test_the_scan_asks_only_for_the_jobs_some_trigger_watches() -> None:
    specs = [on_ingest(), spec("other", triggers=[{"event": {"job": "fetch_news"}}])]

    assert watched_jobs(specs) == ["fetch_news", "ingest"]
    assert watched_jobs([spec("plain")]) == []


# --- how often ------------------------------------------------------------------


def test_every_ingest_in_an_hour_produces_the_same_key() -> None:
    """The load-bearing default. Ingest polls every five minutes.

    Without this, a successful ingest at :00, :05, :10 … would be twelve
    hourly runs an hour, each re-writing the same signals and explaining them
    again. The key is the bar hour, so they collapse to one.
    """
    trigger = on_ingest().events[0]
    keys = {
        event_dedup_key(trigger, occurred_at=NOW.replace(minute=minute), cause_run_id=f"r{minute}")
        for minute in (0, 5, 17, 59)
    }

    assert keys == {"hour:2026-11-18T12"}


def test_the_next_hour_is_a_different_key() -> None:
    trigger = on_ingest().events[0]

    assert event_dedup_key(
        trigger, occurred_at=NOW + timedelta(hours=1), cause_run_id="r"
    ) != event_dedup_key(trigger, occurred_at=NOW, cause_run_id="r")


def test_once_per_run_gives_every_event_its_own_key() -> None:
    trigger = on_ingest(once_per="run").events[0]

    assert event_dedup_key(trigger, occurred_at=NOW, cause_run_id="abc") == "run:abc"


def test_once_per_day_collapses_a_whole_utc_day() -> None:
    trigger = on_ingest(once_per="day").events[0]

    assert event_dedup_key(trigger, occurred_at=NOW, cause_run_id="r") == "day:2026-11-18"


def test_the_key_is_computed_in_utc_whatever_the_event_carried() -> None:
    """Invariant 9's shape: a local-time hour boundary would double-run."""
    from datetime import timezone

    trigger = on_ingest().events[0]
    eastern = NOW.astimezone(timezone(timedelta(hours=-5)))

    assert event_dedup_key(trigger, occurred_at=eastern, cause_run_id="r") == "hour:2026-11-18T12"


def test_a_schedule_key_is_the_interval_it_falls_in() -> None:
    """A bucket, so the safety net can be re-evaluated every tick for free."""
    trigger = PipelineSpec.model_validate(
        {"name": "p", "triggers": [{"schedule": {"every": 3600}}], "steps": [{"id": "s", "job": "observer"}]}
    ).schedules[0]

    first = schedule_dedup_key(trigger, now=NOW)
    same = schedule_dedup_key(trigger, now=NOW + timedelta(minutes=20))
    later = schedule_dedup_key(trigger, now=NOW + timedelta(hours=2))

    assert first == same
    assert first != later


def test_a_manual_key_never_collides() -> None:
    """A person asking twice means twice; the dedup key must not swallow that."""
    assert manual_dedup_key(NOW) != manual_dedup_key(NOW)


# --- the safety net -------------------------------------------------------------


def scheduled(every: int = 7200) -> PipelineSpec:
    return spec(triggers=[{"schedule": {"every": every}}])


def test_a_schedule_fires_when_nothing_has_asked_for_the_pipeline() -> None:
    found = due_schedules([scheduled()], now=NOW, last_queued={"hourly": None})

    assert [m.trigger for m in found] == ["scheduled"]


def test_a_schedule_does_not_fire_when_an_event_already_asked() -> None:
    """A net, not a second trigger: it measures since the pipeline was last *asked for*."""
    found = due_schedules([scheduled()], now=NOW, last_queued={"hourly": NOW - timedelta(minutes=5)})

    assert found == []


def test_a_schedule_fires_again_once_its_interval_has_passed() -> None:
    found = due_schedules([scheduled(7200)], now=NOW, last_queued={"hourly": NOW - timedelta(hours=3)})

    assert len(found) == 1


def test_a_pipeline_with_no_schedule_is_never_due() -> None:
    assert due_schedules([on_ingest()], now=NOW, last_queued={}) == []


# --- cycles ----------------------------------------------------------------------


def test_two_pipelines_that_trigger_each_other_are_refused() -> None:
    a = PipelineSpec.model_validate(
        {
            "name": "a",
            "triggers": [{"event": {"job": "b"}}],
            "steps": [{"id": "s", "job": "observer"}],
        }
    )
    b = PipelineSpec.model_validate(
        {
            "name": "b",
            "triggers": [{"event": {"job": "a"}}],
            "steps": [{"id": "s", "job": "observer"}],
        }
    )

    with pytest.raises(PipelineCycleError, match="loop"):
        check_cycles([a, b])


def test_a_three_pipeline_cycle_is_refused() -> None:
    specs = [
        PipelineSpec.model_validate(
            {
                "name": name,
                "triggers": [{"event": {"job": watches}}],
                "steps": [{"id": "s", "job": "observer"}],
            }
        )
        for name, watches in (("a", "c"), ("b", "a"), ("c", "b"))
    ]

    with pytest.raises(PipelineCycleError):
        check_cycles(specs)


def test_a_pipeline_triggered_by_another_s_step_is_a_cycle_when_it_closes() -> None:
    """A trigger can name a step, not only a pipeline — the ledger does not distinguish."""
    a = PipelineSpec.model_validate(
        {"name": "a", "triggers": [{"event": {"job": "analyse"}}], "steps": [{"id": "s", "job": "signal"}]}
    )
    b = PipelineSpec.model_validate(
        {"name": "b", "triggers": [{"event": {"job": "signal"}}], "steps": [{"id": "s", "job": "analyse"}]}
    )

    with pytest.raises(PipelineCycleError):
        check_cycles([a, b])


def test_a_chain_that_does_not_close_is_fine() -> None:
    a = PipelineSpec.model_validate(
        {"name": "a", "triggers": [{"event": {"job": "ingest"}}], "steps": [{"id": "s", "job": "signal"}]}
    )
    b = PipelineSpec.model_validate(
        {"name": "b", "triggers": [{"event": {"job": "signal"}}], "steps": [{"id": "s", "job": "analyse"}]}
    )

    check_cycles([a, b])  # does not raise


def test_the_committed_specs_do_not_form_a_cycle() -> None:
    """`make check-pipelines`'s other half: one file at a time cannot see this."""
    from candlegraph.harness.pipelines import load_pipeline_dir

    specs = load_pipeline_dir(REPO_ROOT / "pipelines")

    assert {s.name for s in specs} >= {"hourly", "daily", "example"}


# --- loading, with a broken file in the directory --------------------------------


def test_a_broken_spec_is_skipped_rather_than_stopping_the_others(tmp_path: Path) -> None:
    """A dispatcher must not die because somebody committed a typo."""
    (tmp_path / "good.yaml").write_text(
        "name: good\nsteps:\n  - id: s\n    job: observer\n", encoding="utf-8"
    )
    (tmp_path / "bad.yaml").write_text("name: bad\nsteps:\n  - id: s\n    job: nosuch\n", encoding="utf-8")
    errors: dict[str, str] = {}

    specs = load_specs(tmp_path, errors)

    assert [s.name for s in specs] == ["good"]
    # Skipped, but not invisible: it lands in the tick's run.
    assert "bad.yaml" in errors


def test_a_cycle_stops_everything_rather_than_part_of_it(tmp_path: Path) -> None:
    """A cycle is a property of the set, so nothing in the set can be trusted."""
    for name, watches in (("a", "b"), ("b", "a")):
        (tmp_path / f"{name}.yaml").write_text(
            f"name: {name}\ntriggers:\n  - event: {{ job: {watches} }}\n"
            "steps:\n  - id: s\n    job: observer\n",
            encoding="utf-8",
        )
    errors: dict[str, str] = {}

    assert load_specs(tmp_path, errors) == ()
    assert "__cycle__" in errors


# --- one tick ----------------------------------------------------------------------


def tick(db: FakeDatabase, specs: list[PipelineSpec], **kwargs: Any) -> DispatchSummary:
    return dispatch_once(
        db,
        make_settings(agent_report_sinks="log"),
        specs=specs,
        now=NOW,
        lock=kwargs.pop("lock", always_free),
        worker="test",
        **kwargs,
    )


def queued_db(**reads: Any) -> FakeDatabase:
    return FakeDatabase(reap_expired_leases=[], **reads)


def test_a_finished_ingest_is_enqueued_and_then_run() -> None:
    db = queued_db(
        get_finished_runs_since=[finished_run()],
        enqueue_run=[
            {
                "id": 1,
                "pipeline": "hourly",
                "version": 1,
                "dedup_key": "hour:2026-11-18T12",
                "trigger": "event",
                "cause_run_id": "r1",
                "state": "queued",
                "attempts": 0,
                "available_at": NOW,
                "created_at": NOW,
            }
        ],
    )
    db.reads["claim_queued_run"] = db.reads["enqueue_run"]

    summary = tick(db, [on_ingest()])

    assert summary.enqueued == 1
    assert summary.claimed == 1
    assert summary.ran == ["hourly=succeeded"]
    assert summary.did_something


def test_a_duplicate_is_counted_and_nothing_is_enqueued() -> None:
    """The insert returned no row: the same work, asked for twice."""
    db = queued_db(get_finished_runs_since=[finished_run()], enqueue_run=[], claim_queued_run=[])

    summary = tick(db, [on_ingest()])

    assert (summary.enqueued, summary.duplicates) == (0, 1)
    assert summary.ran == []


def test_a_tick_with_nothing_to_do_is_not_worth_a_ledger_row() -> None:
    db = queued_db(get_finished_runs_since=[], claim_queued_run=[])

    summary = tick(db, [on_ingest()])

    assert summary.did_something is False


def test_a_pipeline_already_running_elsewhere_goes_back_in_the_queue() -> None:
    """A person typing `candlegraph run` must not get a second concurrent run."""
    entry = {
        "id": 1,
        "pipeline": "hourly",
        "version": 1,
        "dedup_key": "k",
        "trigger": "event",
        "cause_run_id": None,
        "state": "claimed",
        "attempts": 1,
        "available_at": NOW,
        "created_at": NOW,
    }
    db = queued_db(get_finished_runs_since=[], claim_queued_run=[entry])

    summary = tick(db, [on_ingest()], lock=always_taken)

    assert summary.ran == []
    assert ("release_queued_run", ...) not in db.executed  # shape check below
    assert any(label == "release_queued_run" for label, _ in db.executed)


def test_a_claimed_row_for_a_spec_that_no_longer_exists_is_completed_not_retried() -> None:
    """Nothing will ever run it, so leaving it queued would spin for ever."""
    entry = {
        "id": 9,
        "pipeline": "gone",
        "version": 1,
        "dedup_key": "k",
        "trigger": "event",
        "cause_run_id": None,
        "state": "claimed",
        "attempts": 1,
        "available_at": NOW,
        "created_at": NOW,
    }
    db = queued_db(get_finished_runs_since=[], claim_queued_run=[entry])

    summary = tick(db, [on_ingest()])

    assert "gone" in summary.errors
    assert any(label == "complete_queued_run" for label, _ in db.executed)


def test_the_run_a_dispatch_starts_records_why_and_what_caused_it() -> None:
    entry = {
        "id": 1,
        "pipeline": "hourly",
        "version": 1,
        "dedup_key": "hour:2026-11-18T12",
        "trigger": "event",
        "cause_run_id": "cause-run",
        "state": "claimed",
        "attempts": 1,
        "available_at": NOW,
        "created_at": NOW,
    }
    db = queued_db(get_finished_runs_since=[], claim_queued_run=[entry])

    tick(db, [on_ingest()])

    opened = [p for label, p in db.executed if label == "open_run" and p and p["job"] == "hourly"]
    assert opened[0]["trigger"] == "event"
    # `parent_run_id` is the *cause* here, and `trigger` is what says so.
    assert opened[0]["parent_run_id"] == "cause-run"


# --- the run-twice proof ------------------------------------------------------------


def test_running_dispatch_twice_writes_the_same_state() -> None:
    """Two ticks over the same window enqueue once; that is the dedup key's job.

    The second tick sees the same finished run, asks for the same work, and
    the insert does nothing. Which is exactly what makes the lookback window
    safe to re-read after a restart.
    """
    db = queued_db(get_finished_runs_since=[finished_run()], enqueue_run=[], claim_queued_run=[])
    settings = make_settings(agent_report_sinks="log")

    first = run_job(JOBS["dispatch"], settings, db=db, params={"specs": [on_ingest()], "now": NOW})
    second = run_job(JOBS["dispatch"], settings, db=db, params={"specs": [on_ingest()], "now": NOW})

    assert first.result.counts == second.result.counts
    assert first.result.payload.duplicates == second.result.payload.duplicates == 1
    assert first.outcome is second.outcome is Outcome.SUCCEEDED


def test_a_quiet_tick_leaves_no_ledger_row_and_a_busy_one_does() -> None:
    """1,440 rows a day of "looked, nothing there" would bury every real run."""
    settings = make_settings(agent_report_sinks="log")

    idle = FakeDatabase(reap_expired_leases=[], get_finished_runs_since=[], claim_queued_run=[])
    run_job(JOBS["dispatch"], settings, db=idle, params={"specs": [on_ingest()], "now": NOW})
    # The reap and the claim are statements it always runs; what an idle tick
    # must not leave behind is a row in the ledger.
    assert [label for label, _ in idle.executed] == ["reap_expired_leases", "claim_queued_run"]

    busy = queued_db(
        get_finished_runs_since=[finished_run()],
        enqueue_run=[
            {
                "id": 1,
                "pipeline": "hourly",
                "version": 1,
                "dedup_key": "k",
                "trigger": "event",
                "cause_run_id": None,
                "state": "queued",
                "attempts": 0,
                "available_at": NOW,
                "created_at": NOW,
            }
        ],
        claim_queued_run=[],
    )
    run_job(JOBS["dispatch"], settings, db=busy, params={"specs": [on_ingest()], "now": NOW})
    assert "close_run" in [label for label, _ in busy.executed]


def test_a_tick_that_only_looked_at_events_leaves_no_row() -> None:
    """`events_seen` is what it looked at, not what it did.

    For the hour after an ingest, every tick sees that run again and finds
    it already queued. Counting that as work wrote a row a minute — which is
    what a real stack showed, and what the empty-counts path now prevents.
    """
    db = queued_db(get_finished_runs_since=[finished_run()], enqueue_run=[], claim_queued_run=[])

    run = run_job(JOBS["dispatch"], make_settings(), db=db, params={"specs": [on_ingest()], "now": NOW})

    assert run.result.payload.events_seen == 1
    assert run.result.payload.duplicates == 1
    assert "close_run" not in [label for label, _ in db.executed]


def test_a_failing_tick_is_always_recorded_however_quiet_the_job() -> None:
    """The point of dropping the empty ticks is that the interesting ones stand out."""
    db = queued_db(get_finished_runs_since=[], claim_queued_run=[])

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the dispatcher fell over")

    import candlegraph.harness.events as events_module

    original = events_module.dispatch_once
    events_module.dispatch_once = boom  # type: ignore[assignment]
    try:
        run = run_job(JOBS["dispatch"], make_settings(), db=db, params={"specs": []})
    finally:
        events_module.dispatch_once = original  # type: ignore[assignment]

    assert run.outcome is Outcome.FAILED
    assert "close_run" in [label for label, _ in db.executed]
