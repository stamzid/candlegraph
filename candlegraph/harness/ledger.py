"""The generic ``run`` ledger: one row per run of a job with no ledger of its own.

``ingest_run`` and ``agent_run`` have typed columns because their readers
want them (`make runs` prints row counts; the dashboard prints findings).
Everything else — the news fetch, and stage 2's pipeline steps — records
itself here, with the job's own numbers as JSONB.

Two phases, like the other two ledgers: :func:`open_run` when the run starts
and :func:`close_run` when it ends, so a run killed midway stays visible as
``running``. Writes raise (invariant 3); the hook that calls them logs and
continues, so a broken ledger never fails a run (invariant 12).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from candlegraph.storage import DatabaseManager


def open_run(
    db: DatabaseManager,
    run_id: str,
    job: str,
    trigger: str | None,
    started_at: datetime,
    parent_run_id: str | None = None,
) -> int:
    """Insert the row for a run that is starting."""
    return db.execute(
        db.sql.open_run,
        {
            "run_id": run_id,
            "job": job,
            "trigger": trigger,
            "started_at": started_at,
            "parent_run_id": parent_run_id,
        },
    )


def close_run(
    db: DatabaseManager,
    *,
    run_id: str,
    job: str,
    trigger: str | None,
    outcome: str,
    started_at: datetime,
    finished_at: datetime,
    counts: Mapping[str, int] | None = None,
    errors: Mapping[str, str] | None = None,
    detail: Mapping[str, Any] | None = None,
    parent_run_id: str | None = None,
) -> int:
    """Close the row for a finished run."""
    return db.execute(
        db.sql.close_run,
        {
            "run_id": run_id,
            "job": job,
            "trigger": trigger,
            "outcome": outcome,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": max(0.0, (finished_at - started_at).total_seconds()),
            "counts": json.dumps(dict(counts or {})),
            "errors": json.dumps(dict(errors or {})),
            "detail": json.dumps(dict(detail or {}), default=str),
            "parent_run_id": parent_run_id,
        },
    )


def recent_runs(db: DatabaseManager, limit: int = 10, job: str | None = None) -> list[dict[str, Any]]:
    """The newest rows, optionally for one job."""
    return db.fetch_all(db.sql.get_recent_generic_runs, {"limit": limit, "job": job})
