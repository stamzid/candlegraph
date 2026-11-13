"""The one scheduler builder, ported from the worker's and the agents' copies.

Every assertion the two old suites made still holds, now made once against
``harness.scheduler``.
"""

from __future__ import annotations

from typing import Any

import pytest

from candlegraph.harness import JOBS, select
from candlegraph.harness.scheduler import build_scheduler
from tests.conftest import make_settings

ALL = tuple(JOBS.values())


def test_the_ingest_job_fires_immediately_then_on_the_interval() -> None:
    """The first backfill is the job's first run, not a blocking startup step."""
    job = build_scheduler(make_settings(data_poll_interval=42), ALL).get_job("ingest")

    assert job is not None
    assert job.trigger.interval.total_seconds() == 42
    assert job.next_run_time is not None


@pytest.mark.parametrize("name", list(JOBS))
def test_a_slow_run_cannot_stack_up_behind_the_next_tick(name: str) -> None:
    """A 90-day backfill outlasts a 300s interval; overlap would double-write."""
    settings = make_settings(observer_interval=42, janitor_interval=7)
    job = build_scheduler(settings, ALL).get_job(name)

    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.next_run_time is not None


def test_each_job_reads_its_own_existing_interval_setting() -> None:
    scheduler = build_scheduler(make_settings(observer_interval=42, janitor_interval=7), ALL)

    janitor = scheduler.get_job("janitor")
    assert janitor is not None and janitor.trigger.interval.total_seconds() == 7


def test_only_jobs_with_a_non_zero_interval_are_scheduled() -> None:
    scheduler = build_scheduler(make_settings(observer_interval=900, janitor_interval=0), ALL)

    assert scheduler.get_job("observer") is not None
    assert scheduler.get_job("janitor") is None


def test_a_selection_schedules_only_its_jobs() -> None:
    """The API hosts `ingest` alone through this same builder."""
    scheduler = build_scheduler(make_settings(), select(names=("ingest",)))

    assert [job.id for job in scheduler.get_jobs()] == ["ingest"]


def test_an_empty_selection_schedules_nothing() -> None:
    assert build_scheduler(make_settings(), ()).get_jobs() == []


def test_a_custom_job_can_be_injected() -> None:
    """The test seam both old builders offered."""

    def custom(_settings: Any) -> None:
        pass

    job = build_scheduler(make_settings(), ALL, job_override={"observer": custom}).get_job("observer")

    assert job is not None and job.func is custom
