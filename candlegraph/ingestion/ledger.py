"""The ingest run ledger.

Every run writes a row before it starts and updates it when it ends. That is
what makes "is the data fresh?" and "did last night's run fail?" answerable
with a query instead of by grepping logs — the observability the agent phases
are going to need.

A ledger that itself fails must never take the run down with it, so the write
helpers log and continue rather than raise.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

# Trigger values; free text in the column, but these are the ones we write.
SCHEDULED = "scheduled"
MANUAL = "manual"
BACKFILL = "backfill"

# Terminal statuses.
SUCCEEDED = "succeeded"
PARTIAL = "partial"
FAILED = "failed"


@dataclass
class RunRecord:
    """An in-flight ingest run, mirrored to the ``ingest_run`` table."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trigger: str = SCHEDULED
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    lookback_days: int | None = None

    def duration_seconds(self, finished_at: datetime) -> float:
        return max(0.0, (finished_at - self.started_at).total_seconds())


def status_for(errors: dict[str, str], wrote_anything: bool) -> str:
    """Classify a finished run.

    ``partial`` is the interesting one: some tokens succeeded and some did
    not, which should neither page anyone nor be mistaken for a clean run.
    """
    if not errors:
        return SUCCEEDED
    return PARTIAL if wrote_anything else FAILED


def start_run(
    db: DatabaseManager,
    trigger: str = SCHEDULED,
    lookback_days: int | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """Open a ledger row for a run that is about to begin."""
    record = RunRecord(
        trigger=trigger,
        started_at=now or datetime.now(tz=UTC),
        lookback_days=lookback_days,
    )
    try:
        db.execute(
            db.sql.start_ingest_run,
            {
                "run_id": record.run_id,
                "trigger": record.trigger,
                "started_at": record.started_at,
                "lookback_days": record.lookback_days,
            },
        )
    except Exception as exc:
        service_logger.warning("Could not open ledger row for run %s: %s", record.run_id, exc)
    return record


def finish_run(
    db: DatabaseManager,
    record: RunRecord,
    status: str,
    counts: dict[str, int],
    errors: dict[str, str],
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Close the ledger row for a finished run."""
    finished_at = now or datetime.now(tz=UTC)
    try:
        db.execute(
            db.sql.finish_ingest_run,
            {
                "run_id": record.run_id,
                "status": status,
                "finished_at": finished_at,
                "duration_seconds": record.duration_seconds(finished_at),
                "tokens_written": counts.get("tokens_written", 0),
                "token_hours_written": counts.get("token_hours_written", 0),
                "pools_written": counts.get("pools_written", 0),
                "pool_hours_written": counts.get("pool_hours_written", 0),
                "errors": json.dumps(errors),
                "detail": json.dumps(detail or {}, default=str),
            },
        )
    except Exception as exc:
        service_logger.warning("Could not close ledger row for run %s: %s", record.run_id, exc)


def recent_runs(db: DatabaseManager, limit: int = 10) -> list[dict[str, Any]]:
    """The most recent runs, newest first."""
    return db.fetch_all(db.sql.get_recent_runs, {"limit": limit})


def last_successful_run(db: DatabaseManager) -> dict[str, Any] | None:
    """The newest run that wrote something, or None if there has never been one."""
    rows = db.fetch_all(db.sql.get_last_successful_run)
    return rows[0] if rows else None


def seconds_since_last_success(db: DatabaseManager, now: datetime | None = None) -> float | None:
    """Age of the last successful run in seconds, or None if there is none.

    This is the staleness signal the health check and the phase-4 observer
    agent both read.
    """
    run = last_successful_run(db)
    if run is None:
        return None
    finished = run.get("finished_at") or run.get("started_at")
    if not isinstance(finished, datetime):
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(tz=UTC)) - finished).total_seconds())
