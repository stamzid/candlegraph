"""The pipeline work queue: what was asked for, and who is doing it.

A trigger does not run a pipeline. It puts a row in ``run_queue``, and a
dispatcher claims it. The indirection is what buys the two properties step 5
exists for (``docs/harness/pipelines/DESIGN.md`` §7.2 to §7.4):

- **Exactly one run per trigger.** `UNIQUE (pipeline, dedup_key)` makes that a
  property of the schema rather than of whoever happened to be awake. Twelve
  ingest runs inside one hour insert one row, because the hourly pipeline's
  key is the bar hour.
- **A crash loses nothing.** A claimed row carries a lease; when its
  dispatcher dies the lease expires, the reaper puts the row back, and
  another claim re-runs the pipeline from the start. That is safe because
  every step is declared idempotent (§6.2) — at-least-once over idempotent
  work is effectively once, which is why *resume* is an optimisation rather
  than a correctness requirement, and is not built.

Everything here is small SQL around one table. The decisions are in the
statements (`storage/statements.py`), which say why each one is shaped as it
is; this module is the typed surface over them.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from candlegraph.storage import DatabaseManager
from candlegraph.utils.logging_utils import service_logger

QUEUED = "queued"
CLAIMED = "claimed"
DONE = "done"
DEAD = "dead"


@dataclass(frozen=True)
class QueuedRun:
    """One row of ``run_queue``: a pipeline run that was asked for."""

    id: int
    pipeline: str
    version: int
    dedup_key: str
    trigger: str
    cause_run_id: str | None
    state: str
    attempts: int
    available_at: datetime
    created_at: datetime
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None
    run_id: str | None = None
    finished_at: datetime | None = None
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> QueuedRun:
        return cls(
            id=int(row["id"]),
            pipeline=str(row["pipeline"]),
            version=int(row["version"]),
            dedup_key=str(row["dedup_key"]),
            trigger=str(row["trigger"]),
            cause_run_id=row.get("cause_run_id"),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            available_at=row["available_at"],
            created_at=row["created_at"],
            claimed_by=row.get("claimed_by"),
            lease_expires_at=row.get("lease_expires_at"),
            run_id=row.get("run_id"),
            finished_at=row.get("finished_at"),
            last_error=row.get("last_error"),
        )


def worker_name() -> str:
    """Who holds a lease: host, pid, and a nonce.

    The nonce matters because a container can restart with the same hostname
    and pid, and a stale lease must not look like this process's own.
    """
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


def enqueue(
    db: DatabaseManager,
    *,
    pipeline: str,
    version: int,
    dedup_key: str,
    trigger: str,
    cause_run_id: str | None = None,
    now: datetime | None = None,
    available_at: datetime | None = None,
) -> QueuedRun | None:
    """Ask for a pipeline run. ``None`` means the same work was already asked for.

    Not an error, and not worth a log line above debug: a duplicate is the
    dedup key doing its job. Twelve ingest runs an hour produce one queued
    hourly run and eleven ``None``s.
    """
    moment = now or datetime.now(tz=UTC)
    rows = db.execute_returning(
        db.sql.enqueue_run,
        {
            "pipeline": pipeline,
            "version": version,
            "dedup_key": dedup_key,
            "trigger": trigger,
            "cause_run_id": cause_run_id,
            "available_at": available_at or moment,
            "now": moment,
        },
    )
    return QueuedRun.from_row(rows[0]) if rows else None


def claim(
    db: DatabaseManager,
    *,
    claimed_by: str,
    lease_seconds: int,
    pipeline: str | None = None,
    now: datetime | None = None,
) -> QueuedRun | None:
    """Take the oldest due row, or ``None`` when there is nothing to do."""
    moment = now or datetime.now(tz=UTC)
    rows = db.execute_returning(
        db.sql.claim_queued_run,
        {
            "claimed_by": claimed_by,
            "lease_expires_at": moment + timedelta(seconds=lease_seconds),
            "now": moment,
            "pipeline": pipeline,
        },
    )
    return QueuedRun.from_row(rows[0]) if rows else None


def complete(
    db: DatabaseManager,
    entry: QueuedRun,
    *,
    run_id: str | None,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Mark a claimed row done, pointing at the run it produced.

    ``done`` even when the pipeline failed: the queue records that the work
    was *carried out*, and whether it worked is the run's business. A failed
    pipeline that were left queued would be re-run forever.
    """
    db.execute(
        db.sql.complete_queued_run,
        {
            "id": entry.id,
            "run_id": run_id,
            "last_error": error,
            "now": now or datetime.now(tz=UTC),
        },
    )


def release(
    db: DatabaseManager,
    entry: QueuedRun,
    *,
    reason: str,
    delay_seconds: int = 30,
    now: datetime | None = None,
) -> None:
    """Put a claimed row back, due again shortly. Used when the lock was taken."""
    moment = now or datetime.now(tz=UTC)
    db.execute(
        db.sql.release_queued_run,
        {
            "id": entry.id,
            "available_at": moment + timedelta(seconds=delay_seconds),
            "last_error": reason,
        },
    )


def reap(db: DatabaseManager, *, max_claims: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return rows whose dispatcher died to the pool, or kill the poisonous ones.

    Returns the rows it changed, so the caller can log a row that went
    ``dead`` — that one is a human's problem and the observer should see it.
    """
    changed = db.execute_returning(
        db.sql.reap_expired_leases,
        {"max_claims": max_claims, "now": now or datetime.now(tz=UTC)},
    )
    for row in changed:
        if row["state"] == DEAD:
            service_logger.error(
                "Queue row %s (%s %s) is dead after %s claims; it needs a person",
                row["id"],
                row["pipeline"],
                row["dedup_key"],
                row["attempts"],
            )
        else:
            service_logger.warning(
                "Queue row %s (%s %s) had an expired lease and is queued again",
                row["id"],
                row["pipeline"],
                row["dedup_key"],
            )
    return changed


def renew(db: DatabaseManager, entry: QueuedRun, *, lease_seconds: int, now: datetime | None = None) -> int:
    """Extend this dispatcher's lease. Returns 0 if the lease was already lost."""
    moment = now or datetime.now(tz=UTC)
    return db.execute(
        db.sql.renew_lease,
        {
            "id": entry.id,
            "claimed_by": entry.claimed_by,
            "lease_expires_at": moment + timedelta(seconds=lease_seconds),
        },
    )


def recent(
    db: DatabaseManager, *, limit: int = 20, pipeline: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """The newest queue rows, for ``candlegraph queue``."""
    return db.fetch_all(db.sql.get_queue, {"limit": limit, "pipeline": pipeline, "state": state})


def last_queued_at(db: DatabaseManager, pipeline: str) -> datetime | None:
    """When this pipeline was last asked for, by any trigger."""
    rows = db.fetch_all(db.sql.get_last_queued_at, {"pipeline": pipeline})
    return rows[0]["last_created_at"] if rows else None


def prune(db: DatabaseManager, *, older_than_days: int, now: datetime | None = None) -> int:
    """Delete finished queue rows. The history they point at stays in ``run``."""
    moment = now or datetime.now(tz=UTC)
    return db.execute(db.sql.prune_done_queue_rows, {"before": moment - timedelta(days=older_than_days)})
