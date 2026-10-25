"""The agents process: one job per enabled graph, same idiom as the ingest worker."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from candlegraph.agents.scheduler import (
    JANITOR_JOB_ID,
    OBSERVER_JOB_ID,
    build_scheduler,
    enabled_agents,
    run_agents,
)
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


def test_only_graphs_with_a_non_zero_interval_are_scheduled() -> None:
    scheduler = build_scheduler(make_settings(observer_interval=900, janitor_interval=0))

    assert scheduler.get_job(OBSERVER_JOB_ID) is not None
    assert scheduler.get_job(JANITOR_JOB_ID) is None
    assert enabled_agents(make_settings(observer_interval=0, janitor_interval=60)) == ("janitor",)


def test_jobs_fire_immediately_then_on_the_interval_and_never_overlap() -> None:
    job = build_scheduler(make_settings(observer_interval=42, janitor_interval=7)).get_job(JANITOR_JOB_ID)

    assert job is not None
    assert job.trigger.interval.total_seconds() == 7
    assert job.next_run_time is not None
    assert job.max_instances == 1 and job.coalesce is True


def test_custom_jobs_can_be_injected() -> None:
    def custom(_settings: Any) -> None:
        pass

    job = build_scheduler(make_settings(), observer_job=custom).get_job(OBSERVER_JOB_ID)

    assert job is not None and job.func is custom


def test_the_process_starts_the_scheduler_and_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDatabase()
    set_db(db)
    events: list[str] = []

    class SpyScheduler:
        def start(self) -> None:
            events.append("start")

        def shutdown(self, wait: bool = True) -> None:
            events.append(f"shutdown(wait={wait})")

    monkeypatch.setattr("candlegraph.agents.scheduler.build_scheduler", lambda _s: SpyScheduler())
    stop = threading.Event()
    stop.set()

    run_agents(make_settings(), stop=stop)

    assert events == ["start", "shutdown(wait=True)"]
    assert db.closed is True


def test_no_enabled_agent_is_a_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    set_db(FakeDatabase())
    stop = threading.Event()
    stop.set()

    with caplog.at_level("WARNING", logger="candlegraph"):
        run_agents(make_settings(observer_interval=0, janitor_interval=0), stop=stop)

    assert "nothing to schedule" in caplog.text
