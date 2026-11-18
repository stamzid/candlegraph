"""The dispatcher: what turns "ingest finished" into "run hourly".

Rollout step 5 (``docs/rollout/ROLLOUT.md``; pipelines DESIGN §7.7). One tick
does four things, in this order and for this reason:

1. **Reap** leases whose dispatcher died, so their work is claimable again
   before anything new is added.
2. **Match events**: read the runs that finished inside the lookback window
   and enqueue every pipeline whose event trigger matches.
3. **Match schedules**: enqueue a pipeline whose safety-net interval has
   elapsed since it was last asked for — by *any* trigger, so a pipeline an
   event already ran does not also get a scheduled run.
4. **Claim and run** at most one queued pipeline, under an advisory lock.

**There is no event table.** §7.7 specifies a transactional outbox, and this
does not build one: the outbox pattern keeps an event and a state change
consistent across two *stores*, and here the producer and the consumer are
the same Postgres. ``run_log`` already records every finished run with its
outcome and its time, so the ledger *is* the event log, and a second row
saying so is one more thing that can disagree. The reasoning, and what would
bring the outbox back, is in ``docs/storage/table-placement/DESIGN.md``.

What that choice buys, beyond one less table: the lookback window **is** the
catch-up. A dispatcher that was down for ten minutes finds the events it
missed on its first tick, because they are still in the ledger and the
queue's unique key makes re-reading them free. There is no watermark to keep,
and therefore no watermark to lose.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from candlegraph.harness import queue
from candlegraph.harness.jobs import JobContext
from candlegraph.harness.pipelines import (
    PipelineCycleError,
    PipelineError,
    PipelineSpec,
    check_cycles,
    event_dedup_key,
    load_pipeline,
    load_pipeline_dir,
    run_pipeline,
    schedule_dedup_key,
)
from candlegraph.harness.queue import QueuedRun
from candlegraph.harness.runs import EVENT, SCHEDULED, Outcome, RunOutcome
from candlegraph.settings import Settings
from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

# The jobs an event trigger may name are not known in advance (a trigger can
# name a pipeline), so the scan asks for exactly the jobs some spec wants.
MAX_EVENTS_PER_TICK = 500


@dataclass
class DispatchSummary:
    """What one dispatcher tick did. Becomes the run's counts and detail."""

    reaped: int = 0
    events_seen: int = 0
    enqueued: int = 0
    duplicates: int = 0
    claimed: int = 0
    pruned: int = 0
    ran: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def did_something(self) -> bool:
        """Whether this tick is worth a ledger row.

        A dispatcher that looked and found nothing is not a run anyone needs
        to read about; see ``QuietRunLedgerHook``.
        """
        return bool(self.reaped or self.enqueued or self.claimed or self.pruned or self.errors)

    def counts(self) -> dict[str, int]:
        return {
            "events_seen": self.events_seen,
            "enqueued": self.enqueued,
            "duplicates": self.duplicates,
            "claimed": self.claimed,
            "reaped": self.reaped,
            "pruned": self.pruned,
        }


# --- loading the specs the dispatcher acts on ----------------------------------


def load_specs(directory: Path, errors: dict[str, str] | None = None) -> tuple[PipelineSpec, ...]:
    """Every spec in the directory, with a broken one skipped rather than fatal.

    A dispatcher must not die because somebody committed a typo — the other
    pipelines still need to run. The cost is that a broken spec stops *its*
    pipeline quietly, which is exactly what ``make check-pipelines`` in CI is
    for; the error is also recorded in the tick's run so it is not invisible.
    """
    try:
        return load_pipeline_dir(directory)
    except PipelineCycleError as exc:
        # A cycle is a property of the whole set, so none of it can be
        # trusted. Nothing is dispatched until a person fixes it.
        service_logger.error("Pipelines not dispatched: %s", exc)
        if errors is not None:
            errors["__cycle__"] = str(exc)
        return ()
    except PipelineError:
        pass  # one file is bad; load the rest one at a time

    specs: list[PipelineSpec] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            specs.append(load_pipeline(path))
        except PipelineError as exc:
            service_logger.error("Skipping %s: %s", path.name, exc)
            if errors is not None:
                errors[path.name] = str(exc).splitlines()[0]
    # The survivors still have to agree with each other.
    try:
        check_cycles(specs)
    except PipelineCycleError as exc:
        service_logger.error("Pipelines not dispatched: %s", exc)
        if errors is not None:
            errors["__cycle__"] = str(exc)
        return ()
    return tuple(specs)


# --- matching ------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    """One pipeline that one finished run should start."""

    pipeline: str
    version: int
    dedup_key: str
    trigger: str
    cause_run_id: str | None


def matches_for_event(
    specs: Sequence[PipelineSpec], *, job: str, outcome: str, run_id: str, finished_at: datetime
) -> list[Match]:
    """Every pipeline a finished run matches. Pure."""
    found = []
    for spec in specs:
        for trigger in spec.events:
            if trigger.matches(job, outcome):
                found.append(
                    Match(
                        pipeline=spec.name,
                        version=spec.version,
                        dedup_key=event_dedup_key(trigger, occurred_at=finished_at, cause_run_id=run_id),
                        trigger=EVENT,
                        cause_run_id=run_id,
                    )
                )
                break  # one run of a pipeline per event, however many triggers match
    return found


def watched_jobs(specs: Sequence[PipelineSpec]) -> list[str]:
    """The job names any event trigger cares about, so the scan asks for those."""
    return sorted({trigger.job for spec in specs for trigger in spec.events})


def due_schedules(
    specs: Sequence[PipelineSpec], *, now: datetime, last_queued: dict[str, datetime | None]
) -> list[Match]:
    """Pipelines whose safety-net interval has elapsed. Pure.

    "Elapsed since it was last *asked for*", not since it last ran: a
    pipeline an event already started this hour does not also need its
    schedule to start it, which is what makes the net a net rather than a
    second trigger.
    """
    found = []
    for spec in specs:
        for trigger in spec.schedules:
            previous = last_queued.get(spec.name)
            if previous is not None and now - previous < timedelta(seconds=trigger.every):
                continue
            found.append(
                Match(
                    pipeline=spec.name,
                    version=spec.version,
                    dedup_key=schedule_dedup_key(trigger, now=now),
                    trigger=SCHEDULED,
                    cause_run_id=None,
                )
            )
            break
    return found


# --- the advisory lock ----------------------------------------------------------


@contextmanager
def pipeline_lock(db: DatabaseManager, pipeline: str) -> Iterator[bool]:
    """Hold a session-level advisory lock for one pipeline, or report it taken.

    Session-level (``pg_try_advisory_lock``) rather than transaction-level: a
    pipeline run spans many transactions, and a session lock is released
    automatically if the connection dies — which is what we want after a
    crash. It keeps one pooled connection for the length of the run, which is
    the price of not running the same pipeline twice when a person types
    `candlegraph run` while the dispatcher is working.
    """
    key = f"candlegraph.pipeline.{pipeline}"
    with db.connection() as conn:
        cursor = conn.cursor()
        acquired = False
        try:
            cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,))
            acquired = bool(cursor.fetchone()[0])
            conn.commit()
            yield acquired
        finally:
            if acquired:
                try:
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                    conn.commit()
                except Exception as exc:  # pragma: no cover - the connection is already gone
                    service_logger.warning("Could not release the lock for %s: %s", pipeline, exc)
            cursor.close()


# --- one tick --------------------------------------------------------------------


def dispatch_once(
    db: DatabaseManager,
    settings: Settings,
    *,
    specs: Sequence[PipelineSpec] | None = None,
    now: datetime | None = None,
    lock: Any = pipeline_lock,
    max_runs: int = 1,
    worker: str | None = None,
) -> DispatchSummary:
    """Reap, enqueue what is due, then claim and run at most ``max_runs``.

    Args:
        specs: The pipelines to consider; loaded from ``PIPELINES_DIR`` when
            omitted, so editing a spec takes effect without a restart.
        lock: The advisory-lock context manager; injected by tests.
        max_runs: Pipelines executed per tick. One by default, so a tick's
            length is bounded by one pipeline and the next claim waits for
            the next tick rather than queueing behind this one.
    """
    moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
    summary = DispatchSummary()
    if specs is None:
        specs = load_specs(settings.pipelines_path, summary.errors)

    summary.reaped = len(queue.reap(db, max_claims=settings.queue_max_claims, now=moment))
    _enqueue_due(db, settings, specs, moment, summary)

    me = worker or queue.worker_name()
    for _ in range(max_runs):
        entry = queue.claim(db, claimed_by=me, lease_seconds=settings.queue_lease_seconds, now=moment)
        if entry is None:
            break
        summary.claimed += 1
        _execute(db, settings, specs, entry, summary, lock=lock)

    # Only after a tick that already did something. A DELETE on every quiet
    # tick is a write — and a write-ahead-log entry — once a minute for ever,
    # on a table with a few hundred rows in it. Tying it to work means it
    # happens about as often as pipelines run, which is plenty.
    if summary.did_something:
        summary.pruned = queue.prune(db, older_than_days=settings.queue_retention_days, now=moment)
    return summary


def _enqueue_due(
    db: DatabaseManager,
    settings: Settings,
    specs: Sequence[PipelineSpec],
    moment: datetime,
    summary: DispatchSummary,
) -> None:
    """Turn finished runs and elapsed intervals into queue rows."""
    jobs = watched_jobs(specs)
    rows: list[dict[str, Any]] = []
    if jobs:
        since = moment - timedelta(minutes=settings.event_lookback_minutes)
        rows = db.fetch_all(
            db.sql.get_finished_runs_since,
            {"since": since, "jobs": jobs, "limit": MAX_EVENTS_PER_TICK},
        )
    summary.events_seen = len(rows)

    events: list[Match] = []
    for row in rows:
        events.extend(
            matches_for_event(
                specs,
                job=str(row["job"]),
                outcome=str(row["outcome"]),
                run_id=str(row["run_id"]),
                finished_at=row["finished_at"],
            )
        )
    _enqueue(db, events, moment, summary)

    # Schedules are evaluated **after** the events are queued, not before, so
    # a pipeline an event has just asked for sees itself and stands down.
    # Evaluated first, the net would fire alongside every event it exists to
    # cover, and the very first tick would start every pipeline twice.
    if any(spec.schedules for spec in specs):
        last = {spec.name: queue.last_queued_at(db, spec.name) for spec in specs if spec.schedules}
        _enqueue(db, due_schedules(specs, now=moment, last_queued=last), moment, summary)


def _enqueue(
    db: DatabaseManager, matches: Sequence[Match], moment: datetime, summary: DispatchSummary
) -> None:
    for match in matches:
        entry = queue.enqueue(
            db,
            pipeline=match.pipeline,
            version=match.version,
            dedup_key=match.dedup_key,
            trigger=match.trigger,
            cause_run_id=match.cause_run_id,
            now=moment,
        )
        if entry is None:
            # The same work, asked for again. The dedup key doing its job.
            summary.duplicates += 1
        else:
            summary.enqueued += 1
            service_logger.info("Queued %s (%s) because %s", match.pipeline, match.dedup_key, match.trigger)


def _execute(
    db: DatabaseManager,
    settings: Settings,
    specs: Sequence[PipelineSpec],
    entry: QueuedRun,
    summary: DispatchSummary,
    *,
    lock: Any,
) -> None:
    """Run one claimed queue row, under the pipeline's advisory lock."""
    spec = next((s for s in specs if s.name == entry.pipeline), None)
    if spec is None:
        # The spec was renamed or deleted between the enqueue and the claim.
        # `done`, not back in the queue: nothing will ever run this row.
        message = f"no spec named {entry.pipeline!r} is loaded any more"
        service_logger.error("Queue row %s: %s", entry.id, message)
        summary.errors[entry.pipeline] = message
        queue.complete(db, entry, run_id=None, error=message)
        return

    with lock(db, entry.pipeline) as acquired:
        if not acquired:
            # Somebody else is running it: a manual `candlegraph run`, or a
            # second dispatcher. Put it back rather than run it twice.
            queue.release(db, entry, reason="another process holds the pipeline lock")
            summary.claimed -= 1
            service_logger.info("%s is already running elsewhere; queued again", entry.pipeline)
            return

        run = run_pipeline(
            spec,
            settings,
            trigger=entry.trigger,
            db=db,
            # The run that caused this one. `trigger = event` is what tells a
            # reader this is a cause and not an owner — the two relations
            # share the column, and only one of them can be true at a time.
            parent_run_id=entry.cause_run_id,
        )
    summary.ran.append(f"{entry.pipeline}={run.outcome.value}")
    if run.outcome is Outcome.FAILED:
        summary.errors[entry.pipeline] = (run.result.errors.get("__run__") if run.result else None) or (
            "the pipeline failed; see its own run"
        )
    queue.complete(db, entry, run_id=run.run_id, error=summary.errors.get(entry.pipeline))


# --- the registered job ----------------------------------------------------------


def dispatch_job_body(ctx: JobContext) -> RunOutcome:
    """The ``dispatch`` job: one tick of the loop above.

    Graded gently on purpose. A tick that could not load a spec, or whose
    pipeline failed, is ``partial`` rather than ``failed`` — the dispatcher
    itself worked, and grading it by what it dispatched would make every
    failing pipeline look like a broken dispatcher.
    """
    summary = dispatch_once(
        ctx.db,
        ctx.settings,
        specs=ctx.params.get("specs"),
        now=ctx.params.get("now"),
        max_runs=int(ctx.params.get("max_runs", 1)),
    )
    if not summary.did_something:
        # Empty counts on purpose: the quiet ledger hook decides by them, and
        # the job is the only thing that knows `events_seen` is a count of
        # what it *looked at* rather than what it did. Reporting it here
        # would write a row for every tick in the hour after an ingest —
        # which is what it did until a real stack showed the ledger filling.
        return RunOutcome(outcome=Outcome.SUCCEEDED, payload=summary)

    outcome = Outcome.PARTIAL if summary.errors else Outcome.SUCCEEDED
    return RunOutcome(
        outcome=outcome,
        counts=summary.counts(),
        errors=summary.errors,
        detail={"ran": summary.ran} if summary.ran else {},
        payload=summary,
    )
