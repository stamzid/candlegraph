"""The agent layer: LangGraph graphs that observe the system and post reports.

Two graphs ship in phase 4. The **run observer** reads the ingest ledger, the
quality report, and a trailing window of rows, and says whether the data is
fresh, complete, and still looks like the same market. The **CI janitor**
runs the repository's checks, applies the mechanical fixes, and drafts a PR.

Agents never write market data (invariant 21). Their whole write surface is
the ``agent_run`` table and the configured report sinks. See
``docs/agents/framework.md`` for the conventions every graph follows.
"""

from __future__ import annotations

from collections.abc import Callable

from candlegraph.agents.base import AgentReport, Finding, Severity, overall_severity
from candlegraph.settings import Settings

Runner = Callable[[Settings], AgentReport]


def _observer(settings: Settings) -> AgentReport:
    from candlegraph.agents.observer import run_observer

    return run_observer(settings)


def _janitor(settings: Settings) -> AgentReport:
    from candlegraph.agents.janitor import run_janitor

    return run_janitor(settings)


# name -> one-shot runner. The CLI, the scheduler, and the docs all read this.
AGENTS: dict[str, Runner] = {"observer": _observer, "janitor": _janitor}


def available() -> tuple[str, ...]:
    return tuple(AGENTS)


__all__ = ["AGENTS", "AgentReport", "Finding", "Severity", "available", "overall_severity"]
