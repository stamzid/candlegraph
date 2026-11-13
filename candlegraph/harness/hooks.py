"""Run lifecycle hooks: the seam the ledger, the event bus and the tracer use.

A :class:`RunHook` hears ``started``, each ``stage`` and ``finished`` for
every run. :class:`CompositeHook` fans out and isolates failures — a hook
that raises is logged and skipped, exactly as a failing report sink or an
unreachable ingest ledger is (invariant 12), so it can never fail the run.

Step 1 ships the log hook only. The ledger hooks arrive in step 2, when the
``agent_run`` row moves out of the ``ledger`` sink; until then
``run_ingest`` and ``LedgerSink`` keep writing the rows they write today,
so the stack's ledgers are unchanged by this step.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from candlegraph.utils.logging_utils import service_logger

if TYPE_CHECKING:
    from candlegraph.agents.stages import StageRecord
    from candlegraph.harness.runs import Run, RunOutcome


class RunHook(Protocol):
    def started(self, run: Run) -> None: ...

    def stage(self, run: Run, record: StageRecord) -> None: ...

    def finished(self, run: Run, outcome: RunOutcome) -> None: ...


class LogHook:
    """One debug line per run boundary.

    Debug, not info: the job bodies already log what they did ("Ingested 40
    hourly rows for WETH", the observer's report), and step 1 promises the
    same log lines at the default level as before it.
    """

    def started(self, run: Run) -> None:
        service_logger.debug("run %s started: %s (%s)", run.run_id[:8], run.job, run.trigger)

    def stage(self, run: Run, record: StageRecord) -> None:
        service_logger.debug("run %s stage %s", run.run_id[:8], record.name)

    def finished(self, run: Run, outcome: RunOutcome) -> None:
        service_logger.debug(
            "run %s finished: %s · %s · %s · %.2fs",
            run.run_id[:8],
            run.job,
            run.trigger,
            outcome.outcome.value,
            run.duration_seconds or 0.0,
        )


class CompositeHook:
    """Fan one lifecycle event out to many hooks; one failing never stops the rest."""

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
    """The hooks every run gets unless a caller passes its own."""
    return (LogHook(),)
