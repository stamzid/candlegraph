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

from pydantic import BaseModel

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

# The setting name :data:`NEVER` stands in for. A pipeline job is built from a
# spec rather than registered, so it has no interval setting to read; the
# sentinel keeps `seconds()` total instead of making the field optional.
NEVER_SETTING = "__never__"


@dataclass(frozen=True)
class Every:
    """Fire immediately, then every ``setting`` seconds; ``0`` disables."""

    setting: str

    def seconds(self, settings: Settings) -> int:
        if self.setting == NEVER_SETTING:
            return 0
        return int(getattr(settings, self.setting))


# "This job is never scheduled." Used by the jobs a pipeline spec builds: they
# run when the pipeline runs, and the pipeline carries the schedule.
NEVER = Every(NEVER_SETTING)


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
    # Running this twice with the same inputs leaves the same state as running
    # it once — and a test in tests/unit/harness/test_jobs.py proves it by
    # doing exactly that. Only an idempotent job may be a pipeline step
    # (docs/harness/pipelines/DESIGN.md §6.2): a step is retried after a crash,
    # so "safe to re-run" has to be a checked property rather than a hope.
    # Defaults to False, so a job is a step only once someone has shown it.
    idempotent: bool = False
    # The pydantic model a spec's `params:` for this job are validated against.
    # None means "this job takes no parameters from a spec", and a spec that
    # passes some is refused at load time — failing closed, so a typo in a
    # param name can never be silently ignored.
    params: type[BaseModel] | None = None

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


def _fetch_news(ctx: JobContext) -> RunOutcome:
    from candlegraph.knowledge.news import news_job_body

    return news_job_body(ctx)


def run_ledger(settings: Settings, db: DatabaseManager, params: Mapping[str, Any]) -> RunHook:
    """The generic `run` table: for every job without a ledger of its own.

    Public because a pipeline run records itself here too, and the job a
    pipeline spec builds is assembled outside this module.
    """
    from candlegraph.harness.hooks import RunLedgerHook

    return RunLedgerHook(db)


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
            # Idempotent in fact — every row upserts on its natural key
            # (invariant 4) — but deliberately not declared so. A pipeline
            # reacts to ingest and never runs it (pipelines DESIGN §7.1), the
            # `market_data` rule refuses it as a step anyway, and declaring it
            # would oblige a run-twice proof for a claim nothing relies on.
            idempotent=False,
        ),
        Job(
            name="observer",
            run=_observer,
            schedule=Every("observer_interval"),
            role="agents",
            writes=frozenset({"ledger", "reports"}),
            description="Check freshness, gaps on liquid tokens, and metric drift",
            ledger=_agent_ledger,
            # Reads the ledgers and the series tables; writes its own agent_run
            # row and its report sinks, nothing else. Proved by
            # test_running_the_observer_twice_writes_the_same_state.
            idempotent=True,
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
        Job(
            name="fetch_news",
            run=_fetch_news,
            schedule=Every("fetch_news_interval"),
            role="agents",
            writes=frozenset({"knowledge", "ledger"}),
            description="One budgeted news search per active token per UTC day",
            ledger=run_ledger,
            # Idempotent by the (asset, UTC day) cache and the url_hash upsert,
            # and it will be a step of the daily pipeline. Not declared yet:
            # the declaration costs a run-twice proof, and that proof belongs
            # with the step that needs it (rollout step 4).
            idempotent=False,
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
