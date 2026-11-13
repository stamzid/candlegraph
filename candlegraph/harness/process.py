"""The one foreground loop: ``candlegraph worker`` and ``candlegraph agents``.

Both used to be written out separately with the same signal handling and the
same start/shutdown/close sequence. They are now two selections of jobs
handed to :func:`run_process`.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Sequence
from types import FrameType

from candlegraph.harness import scheduler as harness_scheduler
from candlegraph.harness.jobs import Job
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import get_db
from candlegraph.utils.logging_utils import service_logger


def run_process(
    settings: Settings | None,
    jobs: Sequence[Job],
    stop: threading.Event | None = None,
    *,
    shutdown_wait: bool = True,
    name: str = "Process",
    banner: str | None = None,
) -> None:
    """Schedule ``jobs`` and block until SIGTERM/SIGINT, or until ``stop`` is set.

    Args:
        settings: The process settings; :func:`get_settings` when ``None``.
        jobs: The selection to host. A selection with nothing enabled is a
            warning, not a crash — the compose ``agents`` service can be
            configured to run nothing.
        stop: Set to return; tests set it before calling.
        shutdown_wait: Whether shutdown waits for in-flight runs. The worker
            waits (``True``) so an upsert batch is never cut off mid-write; the
            API's in-process scheduler does not, so uvicorn's shutdown is never
            held by a backfill. Different on purpose; both are tested.
        name: How the process names itself in its log lines.
        banner: The start-up line; defaults to the job list.
    """
    settings = settings or get_settings()
    stop = stop or threading.Event()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        service_logger.info("%s received signal %s; shutting down", name, signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Only the main thread may install handlers; tests run elsewhere.
        with contextlib.suppress(ValueError):
            signal.signal(sig, handle_signal)

    if not harness_scheduler.scheduled_jobs(settings, jobs):
        service_logger.warning("No job in %s has a non-zero interval; nothing to schedule", name)
    service_logger.info("%s", banner or f"{name} starting: {_names(jobs)}")
    scheduler = harness_scheduler.build_scheduler(settings, jobs)
    scheduler.start()
    try:
        stop.wait()
    finally:
        scheduler.shutdown(wait=shutdown_wait)
        get_db().close()
        service_logger.info("%s stopped", name)


def _names(jobs: Sequence[Job]) -> str:
    return ", ".join(job.name for job in jobs) or "none"


def worker_banner(settings: Settings) -> str:
    """The worker's start-up line, unchanged from before the harness."""
    return (
        f"Ingest worker starting (every {settings.data_poll_interval}s, "
        f"lookback {settings.lookback_days} days, pools {'on' if settings.ingest_pools else 'off'})"
    )


def agents_banner(settings: Settings, jobs: Sequence[Job]) -> str:
    """The agents' start-up line, unchanged from before the harness."""
    enabled = harness_scheduler.scheduled_jobs(settings, jobs)
    return (
        f"Agents starting: {_names(enabled)} (observer every {settings.observer_interval}s, "
        f"janitor every {settings.janitor_interval}s, sinks {','.join(settings.report_sinks)})"
    )
