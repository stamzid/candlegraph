"""Running the agents on a schedule: the ``candlegraph agents`` process.

Built exactly like the ingest worker — one APScheduler job per graph,
``max_instances=1`` so a slow run never overlaps itself, the first firing
immediate — so there is one scheduling idiom in the repo, not two. An
interval of 0 leaves that graph out of this process; the compose stack uses
that to run only the observer, because the container has no git checkout for
the janitor to work in.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from candlegraph.agents.base import AgentReport
from candlegraph.settings import Settings, get_settings
from candlegraph.storage import get_db
from candlegraph.utils.logging_utils import service_logger

OBSERVER_JOB_ID = "observer"
JANITOR_JOB_ID = "janitor"

Job = Callable[[Settings], AgentReport | None]


def observer_tick(settings: Settings) -> AgentReport | None:
    """One observer run. Never raises: a failed run must not kill the job."""
    from candlegraph.agents.observer import run_observer

    try:
        return run_observer(settings)
    except Exception:
        service_logger.exception("Observer run failed; will retry at the next interval")
        return None


def janitor_tick(settings: Settings) -> AgentReport | None:
    from candlegraph.agents.janitor import run_janitor

    try:
        return run_janitor(settings)
    except Exception:
        service_logger.exception("Janitor run failed; will retry at the next interval")
        return None


def build_scheduler(
    settings: Settings, observer_job: Job | None = None, janitor_job: Job | None = None
) -> BackgroundScheduler:
    """A scheduler with one job per enabled graph."""
    scheduler = BackgroundScheduler(timezone="UTC")
    jobs: list[tuple[str, int, Any]] = [
        (OBSERVER_JOB_ID, settings.observer_interval, observer_job or observer_tick),
        (JANITOR_JOB_ID, settings.janitor_interval, janitor_job or janitor_tick),
    ]
    for job_id, interval, func in jobs:
        if interval <= 0:
            continue
        scheduler.add_job(
            func,
            trigger="interval",
            seconds=interval,
            args=[settings],
            id=job_id,
            next_run_time=datetime.now(tz=UTC),
            max_instances=1,
            coalesce=True,
        )
    return scheduler


def enabled_agents(settings: Settings) -> tuple[str, ...]:
    names = []
    if settings.observer_interval > 0:
        names.append(OBSERVER_JOB_ID)
    if settings.janitor_interval > 0:
        names.append(JANITOR_JOB_ID)
    return tuple(names)


def run_agents(settings: Settings | None = None, stop: threading.Event | None = None) -> None:
    """Run the agent schedule until SIGTERM/SIGINT, or until ``stop`` is set."""
    settings = settings or get_settings()
    stop = stop or threading.Event()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        service_logger.info("Agents received signal %s; shutting down", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):
            signal.signal(sig, handle_signal)

    names = enabled_agents(settings)
    if not names:
        service_logger.warning("No agent has a non-zero interval; nothing to schedule")
    service_logger.info(
        "Agents starting: %s (observer every %ss, janitor every %ss, sinks %s)",
        ", ".join(names) or "none",
        settings.observer_interval,
        settings.janitor_interval,
        ",".join(settings.report_sinks),
    )
    scheduler = build_scheduler(settings)
    scheduler.start()
    try:
        stop.wait()
    finally:
        scheduler.shutdown(wait=True)
        get_db().close()
        service_logger.info("Agents stopped")
