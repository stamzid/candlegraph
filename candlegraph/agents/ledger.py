"""The ``agent_run`` table: one row per graph run.

Since harness rollout step 2 the row is written in two phases by
``harness.hooks.AgentLedgerHook``: :func:`open_agent_run` when the run
starts (``outcome = 'running'``, no status yet) and :func:`close_agent_run`
when it ends. A row that is never closed means the process died mid-run.

Writes raise (invariant 3); the hook catches the failure so a broken ledger
never fails the run (the same rule as ``ingest_run``).
:func:`record_agent_run` is the old single insert, kept for tests and
tools that record a finished report directly.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from candlegraph.agents.base import AgentReport
from candlegraph.storage import DatabaseManager


def record_agent_run(db: DatabaseManager, report: AgentReport) -> int:
    """Insert the report's ledger row and return the affected row count."""
    return db.execute(
        db.sql.insert_agent_run,
        {
            "run_id": report.run_id,
            "agent": report.agent,
            "status": report.status_label,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "duration_seconds": report.duration_seconds,
            "findings_total": len(report.findings),
            "summary": report.summary,
            "report": report.markdown,
            "findings": json.dumps([f.to_dict() for f in report.findings], default=str),
            "detail": json.dumps({**report.detail, "error": report.error}, default=str),
        },
    )


def open_agent_run(
    db: DatabaseManager,
    run_id: str,
    agent: str,
    trigger: str | None,
    started_at: datetime,
    parent_run_id: str | None = None,
) -> int:
    """Insert the row for a run that is starting."""
    return db.execute(
        db.sql.open_agent_run,
        {
            "run_id": run_id,
            "agent": agent,
            "trigger": trigger,
            "started_at": started_at,
            "parent_run_id": parent_run_id,
        },
    )


def close_agent_run(
    db: DatabaseManager,
    *,
    run_id: str,
    agent: str,
    trigger: str | None,
    outcome: str,
    started_at: datetime,
    finished_at: datetime,
    report: AgentReport | None,
    error: str | None = None,
    parent_run_id: str | None = None,
) -> int:
    """Close the row for a finished run.

    ``report`` is ``None`` only when the job body raised before an agent
    report existed; the row then records ``status = 'error'`` and the error.
    ``status`` keeps its old meaning — the report's verdict, or ``error``
    for a crashed graph — so readers of ``agent_run`` see what they always did.
    """
    findings = [f.to_dict() for f in report.findings] if report else []
    detail = {**report.detail, "error": report.error} if report else {"error": error}
    return db.execute(
        db.sql.close_agent_run,
        {
            "run_id": run_id,
            "agent": agent,
            "trigger": trigger,
            "outcome": outcome,
            "status": report.status_label if report else "error",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": max(0.0, (finished_at - started_at).total_seconds()),
            "findings_total": len(findings),
            "summary": report.summary if report else "",
            "report": report.markdown if report else "",
            "findings": json.dumps(findings, default=str),
            "detail": json.dumps(detail, default=str),
            "parent_run_id": parent_run_id,
        },
    )


def recent_agent_runs(db: DatabaseManager, limit: int = 10, agent: str | None = None) -> list[dict[str, Any]]:
    """The newest runs, optionally for one agent."""
    return db.fetch_all(db.sql.get_recent_agent_runs, {"limit": limit, "agent": agent})
