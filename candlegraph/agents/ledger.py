"""The ``agent_run`` table: one row per graph run.

What ``candlegraph agent-runs`` reads, and the durable record of a report
whatever happened to the other sinks. Writes raise (invariant 3); the
:class:`~candlegraph.agents.reporting.LedgerSink` is what catches a failure so
a broken ledger never fails the run (the same rule as ``ingest_run``).
"""

from __future__ import annotations

import json
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


def recent_agent_runs(db: DatabaseManager, limit: int = 10, agent: str | None = None) -> list[dict[str, Any]]:
    """The newest runs, optionally for one agent."""
    return db.fetch_all(db.sql.get_recent_agent_runs, {"limit": limit, "agent": agent})
