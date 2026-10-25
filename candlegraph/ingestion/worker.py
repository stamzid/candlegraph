"""The ingest worker: a process that only ingests.

Splitting this out of the API means a slow backfill cannot compete with query
traffic for the event loop or the connection pool, the two can be scaled and
restarted independently, and an ingest crash loop does not take the API down.

The worker blocks in the foreground and runs the first pass immediately.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from candlegraph.ingestion import ledger
from candlegraph.ingestion.runner import IngestResult, run_ingest
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import get_db
from candlegraph.tokens import get_token_registry
from candlegraph.utils.logging_utils import service_logger

INGEST_JOB_ID = "ingest"


def ingest_tick(settings: Settings, trigger: str = ledger.SCHEDULED) -> IngestResult | None:
    """One scheduled pass. Never raises: a failed run must not kill the job."""
    try:
        return run_ingest(get_db(), get_token_registry(), settings, trigger=trigger)
    except Exception:
        service_logger.exception("Ingest run failed; will retry at the next interval")
        return None


def build_scheduler(settings: Settings, job: Any = None) -> BackgroundScheduler:
    """A scheduler whose ingest job fires immediately, then every poll interval."""
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        job or ingest_tick,
        trigger="interval",
        seconds=settings.data_poll_interval,
        args=[settings],
        id=INGEST_JOB_ID,
        # Run the initial backfill as the job's first firing rather than
        # blocking startup on it.
        next_run_time=datetime.now(tz=UTC),
        # A slow backfill must not stack up behind the next tick.
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def run_worker(settings: Settings | None = None, stop: threading.Event | None = None) -> None:
    """Run the ingest loop until SIGTERM/SIGINT, or until ``stop`` is set."""
    settings = settings or get_settings()
    stop = stop or threading.Event()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        service_logger.info("Worker received signal %s; shutting down", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Only the main thread may install handlers; tests run elsewhere.
        with contextlib.suppress(ValueError):
            signal.signal(sig, handle_signal)

    service_logger.info(
        "Ingest worker starting (every %ss, lookback %s days, pools %s)",
        settings.data_poll_interval,
        settings.lookback_days,
        "on" if settings.ingest_pools else "off",
    )
    scheduler = build_scheduler(settings)
    scheduler.start()
    try:
        stop.wait()
    finally:
        scheduler.shutdown(wait=True)
        get_db().close()
        service_logger.info("Ingest worker stopped")
