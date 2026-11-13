"""The job registry: every unit of work that runs on a clock.

Like ``agents.AGENTS`` and ``mcp.tools.TOOLS``, :data:`JOBS` is what the CLI,
the scheduler, the docs and the tests read — a job missing from it does not
exist. Each job declares the existing setting its interval is read from
(nothing was renamed), the process that selects it by default, and what it
writes; a test holds ``writes`` to invariant 21, so only ``ingest`` may claim
``market_data``.

The bodies import their implementations lazily (invariant 1): a registry
that imported the observer at module load would pull LangGraph into
``candlegraph serve``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from candlegraph.harness.runs import Outcome, Run, RunOutcome
from candlegraph.settings import Settings

if TYPE_CHECKING:
    from candlegraph.agents.base import AgentReport
    from candlegraph.agents.stages import StageHook
    from candlegraph.storage import DatabaseManager

Role = Literal["worker", "agents"]
ROLES: tuple[Role, ...] = ("worker", "agents")
WRITE_SURFACES = frozenset({"market_data", "ledger", "knowledge", "cache", "reports"})


@dataclass(frozen=True)
class Every:
    """Fire immediately, then every ``setting`` seconds; ``0`` disables."""

    setting: str

    def seconds(self, settings: Settings) -> int:
        return int(getattr(settings, self.setting))


@dataclass(frozen=True)
class JobContext:
    """What a job body receives. Stage 2's pipeline steps receive the same thing."""

    settings: Settings
    db: DatabaseManager
    run: Run
    now: datetime
    on_stage: StageHook | None
    params: Mapping[str, Any]


@dataclass(frozen=True)
class Job:
    name: str
    run: Callable[[JobContext], RunOutcome]
    schedule: Every
    role: Role
    writes: frozenset[str]
    description: str

    @property
    def interval_setting(self) -> str:
        return self.schedule.setting

    @property
    def label(self) -> str:
        """The capitalised name the log lines have always used ("Ingest run failed…")."""
        return self.name.capitalize()


# --- the bodies --------------------------------------------------------------


def _ingest(ctx: JobContext) -> RunOutcome:
    from candlegraph.ingestion import ledger
    from candlegraph.ingestion.runner import run_ingest
    from candlegraph.tokens import get_token_registry

    # run_ingest still owns the ingest_run row in step 1; it is handed the
    # harness's run_id so the two never disagree about which run this is.
    result = run_ingest(
        ctx.db,
        get_token_registry(),
        ctx.settings,
        trigger=ctx.run.trigger,
        lookback_days=ctx.params.get("lookback_days"),
        run_id=ctx.run.run_id,
    )
    status = ledger.status_for(result.errors, result.rows_written > 0)
    return RunOutcome(
        outcome=Outcome(status),
        counts=result.counts(),
        errors=dict(result.errors),
        detail={"hours_by_symbol": result.hours_by_symbol},
        payload=result,
    )


def _agent_outcome(report: AgentReport) -> RunOutcome:
    # A crashed graph still publishes an error report (invariant 21); that is
    # a failed *run* with no verdict, not a verdict of "error".
    if report.error:
        return RunOutcome(outcome=Outcome.FAILED, errors={"__run__": report.error}, payload=report)
    return RunOutcome(
        outcome=Outcome.SUCCEEDED,
        counts={"findings_total": len(report.findings)},
        verdict=report.status.label,
        payload=report,
    )


def _observer(ctx: JobContext) -> RunOutcome:
    from candlegraph.agents.observer import run_observer

    report = run_observer(ctx.settings, db=ctx.db, sinks=ctx.params.get("sinks"), on_stage=ctx.on_stage)
    return _agent_outcome(report)


def _janitor(ctx: JobContext) -> RunOutcome:
    from candlegraph.agents.janitor import run_janitor

    # No ctx.db here on purpose: the janitor reads no database and runs in
    # GitHub Actions without one; run_janitor fetches it only for the ledger sink.
    report = run_janitor(
        ctx.settings,
        shell=ctx.params.get("shell"),
        checks=ctx.params.get("checks"),
        open_pr=ctx.params.get("open_pr"),
        sinks=ctx.params.get("sinks"),
    )
    return _agent_outcome(report)


JOBS: dict[str, Job] = {
    job.name: job
    for job in (
        Job(
            name="ingest",
            run=_ingest,
            schedule=Every("data_poll_interval"),
            role="worker",
            writes=frozenset({"market_data", "ledger"}),
            description="Poll the subgraph for new hourly token and pool rows",
        ),
        Job(
            name="observer",
            run=_observer,
            schedule=Every("observer_interval"),
            role="agents",
            writes=frozenset({"ledger", "reports"}),
            description="Check freshness, gaps on liquid tokens, and metric drift",
        ),
        Job(
            name="janitor",
            run=_janitor,
            schedule=Every("janitor_interval"),
            role="agents",
            writes=frozenset({"ledger", "reports"}),
            description="Run the checks, apply mechanical fixes, draft a PR",
        ),
    )
}


def select(role: Role | None = None, names: Iterable[str] | None = None) -> tuple[Job, ...]:
    """The jobs a process hosts: an explicit list of names, else every job of ``role``.

    Raises:
        ValueError: A name is not registered, or neither argument was given.
    """
    if names is not None:
        wanted = list(names)
        unknown = [name for name in wanted if name not in JOBS]
        if unknown:
            raise ValueError(f"unknown job(s) {', '.join(unknown)}; registered: {', '.join(JOBS)}")
        return tuple(JOBS[name] for name in wanted)
    if role is None:
        raise ValueError("select() needs a role or a list of names")
    return tuple(job for job in JOBS.values() if job.role == role)
