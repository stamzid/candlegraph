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
    from candlegraph.harness.hooks import RunHook
    from candlegraph.harness.stages import StageHook
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


# Builds the hook that writes this job's ledger row, from what run_job knows
# before the run starts. None means "no row for this run".
LedgerFactory = Callable[[Settings, "DatabaseManager", Mapping[str, Any]], "RunHook | None"]


@dataclass(frozen=True)
class Job:
    name: str
    run: Callable[[JobContext], RunOutcome]
    schedule: Every
    role: Role
    writes: frozenset[str]
    description: str
    ledger: LedgerFactory

    @property
    def interval_setting(self) -> str:
        return self.schedule.setting

    @property
    def label(self) -> str:
        """The capitalised name the log lines have always used ("Ingest run failed…")."""
        return self.name.capitalize()


# --- the bodies --------------------------------------------------------------


def _ingest(ctx: JobContext) -> RunOutcome:
    from candlegraph.ingestion.runner import ingest_job_body

    return ingest_job_body(ctx)


def _ingest_ledger(settings: Settings, db: DatabaseManager, params: Mapping[str, Any]) -> RunHook:
    from candlegraph.harness.hooks import IngestLedgerHook

    window = params.get("lookback_days")
    return IngestLedgerHook(db, window if window is not None else settings.lookback_days)


def records_agent_run(settings: Settings, params: Mapping[str, Any]) -> bool:
    """Whether this agent run gets an ``agent_run`` row.

    The ``ledger`` sink name keeps its meaning — "record this run in
    agent_run" — but the harness writes the row, not the sink. The janitor
    in GitHub Actions has no database and runs without ``ledger``; this is
    what keeps it from trying to connect.
    """
    sinks = params.get("sinks")
    names = [getattr(s, "name", "") for s in sinks] if sinks is not None else list(settings.report_sinks)
    return "ledger" in names


def _agent_ledger(settings: Settings, db: DatabaseManager, params: Mapping[str, Any]) -> RunHook | None:
    from candlegraph.harness.hooks import AgentLedgerHook

    return AgentLedgerHook(db) if records_agent_run(settings, params) else None


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

    # `registry` and `now` are optional params for tests and direct callers.
    report = run_observer(
        ctx.settings,
        db=ctx.db,
        registry=ctx.params.get("registry"),
        now=ctx.params.get("now"),
        sinks=ctx.params.get("sinks"),
        on_stage=ctx.on_stage,
        run_id=ctx.run.run_id,
    )
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
        run_id=ctx.run.run_id,
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
            ledger=_ingest_ledger,
        ),
        Job(
            name="observer",
            run=_observer,
            schedule=Every("observer_interval"),
            role="agents",
            writes=frozenset({"ledger", "reports"}),
            description="Check freshness, gaps on liquid tokens, and metric drift",
            ledger=_agent_ledger,
        ),
        Job(
            name="janitor",
            run=_janitor,
            schedule=Every("janitor_interval"),
            role="agents",
            writes=frozenset({"ledger", "reports"}),
            description="Run the checks, apply mechanical fixes, draft a PR",
            ledger=_agent_ledger,
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
