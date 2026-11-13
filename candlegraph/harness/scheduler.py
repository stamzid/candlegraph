"""The one APScheduler builder. ``grep BackgroundScheduler candlegraph/`` finds this file.

Every job gets the same policy, which both old builders already agreed on:
the first firing is immediate (``next_run_time=now``) rather than a blocking
call at start-up (invariant 7); ``max_instances=1`` and ``coalesce=True`` so
a 90-day backfill or a slow observer never stacks up behind its next tick;
UTC throughout (invariant 9). An interval of ``0`` leaves the job out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from candlegraph.harness.jobs import Job
from candlegraph.harness.runs import SCHEDULED, Run, run_job
from candlegraph.settings import Settings

Tick = Callable[[Settings], Any]


def tick_for(job: Job) -> Tick:
    """The scheduled callable for ``job``: ``run_job`` with a ``scheduled`` trigger."""

    def tick(settings: Settings) -> Run:
        return run_job(job, settings, trigger=SCHEDULED)

    tick.__name__ = f"{job.name}_tick"
    return tick


def scheduled_jobs(settings: Settings, jobs: Iterable[Job]) -> tuple[Job, ...]:
    """The subset of ``jobs`` whose interval is non-zero."""
    return tuple(job for job in jobs if job.schedule.seconds(settings) > 0)


def build_scheduler(
    settings: Settings, jobs: Iterable[Job], job_override: Mapping[str, Tick] | None = None
) -> BackgroundScheduler:
    """A scheduler with one APScheduler job per enabled registered job.

    Args:
        settings: Read for each job's interval and passed to every tick.
        jobs: The selection this process hosts.
        job_override: Replace a job's tick by name — the test seam both old
            builders offered, and how a caller wraps a tick.
    """
    overrides = job_override or {}
    scheduler = BackgroundScheduler(timezone="UTC")
    for job in scheduled_jobs(settings, jobs):
        scheduler.add_job(
            overrides.get(job.name) or tick_for(job),
            trigger="interval",
            seconds=job.schedule.seconds(settings),
            args=[settings],
            id=job.name,
            next_run_time=datetime.now(tz=UTC),
            max_instances=1,
            coalesce=True,
        )
    return scheduler
