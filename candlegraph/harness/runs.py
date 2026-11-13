"""``Run``, ``RunOutcome`` and ``run_job``: the lifecycle every job goes through.

``run_job`` opens a :class:`Run`, calls the job, closes the run and fans the
lifecycle out to hooks. It **never raises**: an exception escaping a
scheduler tick stops APScheduler rescheduling the job, which is why both of
the old ticks swallowed everything; this is now the one place that happens.

Step 1 of the harness rollout carries the run in memory only. The ledger
rows are still written where they were — ``ingestion.runner.run_ingest``
for ``ingest_run`` and the ``ledger`` report sink for ``agent_run`` — and
step 2 moves them into ledger hooks.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from candlegraph.settings import Settings
from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.agents.stages import StageHook, StageRecord
    from candlegraph.harness.hooks import RunHook
    from candlegraph.harness.jobs import Job
    from candlegraph.storage import DatabaseManager

# Why a run happened. Free text in the ledgers; these are the values written.
SCHEDULED = "scheduled"
MANUAL = "manual"
BACKFILL = "backfill"
UI = "ui"
MCP = "mcp"
EVENT = "event"
TRIGGERS = (SCHEDULED, MANUAL, BACKFILL, UI, MCP, EVENT)


class Outcome(StrEnum):
    """Did the run complete? Every job has one; it is not what an agent *found*."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class RunOutcome:
    """What a job body hands back to ``run_job``.

    ``payload`` is deliberately untyped: the CLI's ``ingest`` still prints an
    ``IngestResult`` and ``observe`` an ``AgentReport``. The harness carries
    them; it does not flatten them.
    """

    outcome: Outcome
    counts: Mapping[str, int] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)
    verdict: str | None = None  # agents only: the worst finding's severity
    detail: Mapping[str, Any] = field(default_factory=dict)
    payload: Any = None


@dataclass
class Run:
    """One run of one job. In memory in step 1; recorded two-phase from step 2."""

    job: str
    trigger: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    parent_run_id: str | None = None
    finished_at: datetime | None = None
    result: RunOutcome | None = None

    @property
    def outcome(self) -> Outcome:
        return Outcome.RUNNING if self.result is None else self.result.outcome

    @property
    def payload(self) -> Any:
        return None if self.result is None else self.result.payload

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.started_at).total_seconds())


def run_job(
    job: Job,
    settings: Settings,
    *,
    trigger: str = SCHEDULED,
    params: Mapping[str, Any] | None = None,
    parent_run_id: str | None = None,
    db: DatabaseManager | None = None,
    on_stage: StageHook | None = None,
    hooks: Sequence[RunHook] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Run:
    """Run ``job`` once and return the closed :class:`Run`. Never raises.

    Args:
        job: The registered job.
        settings: Passed through to the body.
        trigger: Why it ran — one of :data:`TRIGGERS`.
        params: Job-specific inputs (``lookback_days``, ``sinks``, ``open_pr``…);
            the ``Job.run`` signature is never widened for one job.
        parent_run_id: The owning run, for stage 2's pipelines.
        db: The database manager; the process-wide one when omitted. Lazy, so
            passing it connects to nothing.
        on_stage: The ``--progress`` stream; also fanned out to the hooks.
        hooks: Lifecycle subscribers; :func:`hooks.default_hooks` when omitted.
        clock: The time source, injected for tests.
    """
    from candlegraph.agents.shell import redact
    from candlegraph.harness.hooks import CompositeHook, default_hooks
    from candlegraph.harness.jobs import JobContext
    from candlegraph.storage import get_db

    clock = clock or (lambda: datetime.now(tz=UTC))
    fanout = CompositeHook(default_hooks() if hooks is None else hooks)
    run = Run(job=job.name, trigger=trigger, started_at=clock(), parent_run_id=parent_run_id)
    fanout.started(run)

    def stage(record: StageRecord) -> None:
        fanout.stage(run, record)
        if on_stage is not None:
            on_stage(record)

    try:
        context = JobContext(
            settings=settings,
            db=db if db is not None else get_db(),
            run=run,
            now=run.started_at,
            # Only when a caller asked for the stream: step 1 changes nothing a
            # job body sees. Step 2 ("stages everywhere") always passes it.
            on_stage=stage if on_stage is not None else None,
            params=dict(params or {}),
        )
        result = job.run(context)
    except Exception as exc:
        # The same line, at the same level, the old ticks logged.
        service_logger.exception("%s run failed; will retry at the next interval", job.label)
        result = RunOutcome(
            outcome=Outcome.FAILED, errors={"__run__": redact(f"{type(exc).__name__}: {exc}")}
        )
    run.finished_at = clock()
    run.result = result
    fanout.finished(run, result)
    return run
