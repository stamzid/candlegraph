"""Run lifecycle hooks: code that reacts to a run starting, a stage ending, a run ending.

:class:`CompositeHook` calls each hook in turn and isolates failures: a hook
that raises is logged and skipped, so it can never fail the run (invariant 12).

Three hooks exist:

- :class:`LogHook` — debug lines at run boundaries.
- :class:`IngestLedgerHook` — the ``ingest_run`` row, opened in ``started``
  and closed in ``finished``.
- :class:`AgentLedgerHook` — the same for ``agent_run``.

The ledger rows are written here and nowhere else. Stage 2 will add an event
bus hook and a tracing hook beside these.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.harness.runs import Run, RunOutcome
    from candlegraph.harness.stages import StageRecord
    from candlegraph.storage import DatabaseManager


class RunHook(Protocol):
    def started(self, run: Run) -> None: ...

    def stage(self, run: Run, record: StageRecord) -> None: ...

    def finished(self, run: Run, outcome: RunOutcome) -> None: ...


class LogHook:
    """Debug lines only: the job bodies already log what they did at INFO."""

    def started(self, run: Run) -> None:
        service_logger.debug("run started: %s (%s)", run.job, run.trigger)

    def stage(self, run: Run, record: StageRecord) -> None:
        service_logger.debug("stage %s: %s", record.name, record.status)

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        service_logger.debug(
            "run finished: %s · %s · %s · %.2fs",
            run.job,
            run.trigger,
            outcome.outcome.value,
            run.duration_seconds or 0.0,
        )


class IngestLedgerHook:
    """Writes the ``ingest_run`` row: ``running`` at the start, the result at the end."""

    def __init__(self, db: DatabaseManager, lookback_days: int) -> None:
        self.db = db
        self.lookback_days = lookback_days
        self.stages: list[dict[str, Any]] = []

    def started(self, run: Run) -> None:
        from candlegraph.ingestion import ledger

        # ledger.start_run already logs and swallows a database failure.
        self.record = ledger.start_run(
            self.db,
            trigger=run.trigger,
            lookback_days=self.lookback_days,
            now=run.started_at,
            run_id=run.run_id,
            parent_run_id=run.parent_run_id,
        )

    def stage(self, run: Run, record: StageRecord) -> None:
        self.stages.append(record.to_dict())

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        from candlegraph.ingestion import ledger

        detail = dict(outcome.detail)
        if self.stages:
            detail["stages"] = self.stages
        if run.exception is not None:
            detail["exception"] = type(run.exception).__name__
        ledger.finish_run(
            self.db,
            self.record,
            outcome.outcome.value,
            dict(outcome.counts),
            dict(outcome.errors),
            detail,
            now=run.finished_at,
        )


class AgentLedgerHook:
    """Writes the ``agent_run`` row: opened before the graph runs, closed after.

    A row opened and never closed means the process died during the run;
    ``run_log`` shows it as ``running``. That is the reason for two phases.
    """

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def started(self, run: Run) -> None:
        from candlegraph.agents.ledger import open_agent_run

        try:
            open_agent_run(self.db, run.run_id, run.job, run.trigger, run.started_at, run.parent_run_id)
        except Exception as exc:
            # Not fatal: close_agent_run is an upsert and will still write the row.
            service_logger.warning("Could not open agent_run row for run %s: %s", run.run_id, exc)

    def stage(self, run: Run, record: StageRecord) -> None:
        pass  # the report's detail carries the graph's stages

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        from candlegraph.agents.base import AgentReport
        from candlegraph.agents.ledger import close_agent_run

        report = outcome.payload if isinstance(outcome.payload, AgentReport) else None
        assert run.finished_at is not None
        try:
            close_agent_run(
                self.db,
                run_id=run.run_id,
                agent=run.job,
                trigger=run.trigger,
                outcome=outcome.outcome.value,
                started_at=run.started_at,
                finished_at=run.finished_at,
                report=report,
                error=outcome.errors.get("__run__"),
                parent_run_id=run.parent_run_id,
            )
        except Exception as exc:
            service_logger.warning("Could not close agent_run row for run %s: %s", run.run_id, exc)


class CompositeHook:
    """Call every hook for each event; one failing never stops the rest."""

    def __init__(self, hooks: Iterable[RunHook]) -> None:
        self.hooks = tuple(hooks)

    def _each(self, event: str, *args: object) -> None:
        for hook in self.hooks:
            try:
                getattr(hook, event)(*args)
            except Exception as exc:
                service_logger.warning("Run hook %s.%s failed: %s", type(hook).__name__, event, exc)

    def started(self, run: Run) -> None:
        self._each("started", run)

    def stage(self, run: Run, record: StageRecord) -> None:
        self._each("stage", run, record)

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        self._each("finished", run, outcome)


def default_hooks() -> tuple[RunHook, ...]:
    """Hooks every run gets in addition to its job's ledger hook."""
    return (LogHook(),)
