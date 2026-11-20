"""``run_process``: the one foreground loop behind ``worker`` and ``agents``."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from candlegraph.harness import select
from candlegraph.harness.process import run_process
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


class SpyScheduler:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("start")

    def shutdown(self, wait: bool = True) -> None:
        self.events.append(f"shutdown(wait={wait})")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(
        "candlegraph.harness.scheduler.build_scheduler", lambda _s, _jobs: SpyScheduler(recorded)
    )
    return recorded


def _stopped() -> threading.Event:
    stop = threading.Event()
    stop.set()  # return from wait() immediately
    return stop


def test_the_process_starts_the_scheduler_and_stops_cleanly(events: list[str]) -> None:
    db = FakeDatabase()
    set_db(db)

    run_process(make_settings(), select(role="worker"), stop=_stopped())

    # Shutdown waits by default, so an in-flight write is never cut off mid-batch.
    assert events == ["start", "shutdown(wait=True)"]
    assert db.closed is True


def test_the_no_wait_shutdown_is_reachable(events: list[str]) -> None:
    """The API's shape: uvicorn must not block on a backfill. Different on purpose."""
    set_db(FakeDatabase())

    run_process(make_settings(), select(role="worker"), stop=_stopped(), shutdown_wait=False)

    assert events == ["start", "shutdown(wait=False)"]


def test_nothing_enabled_is_a_warning_not_a_crash(
    events: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    set_db(FakeDatabase())

    with caplog.at_level("WARNING", logger="candlegraph"):
        run_process(
            # Every interval the agents process hosts, off: the compose
            # service can legitimately be configured to run nothing.
            make_settings(
                observer_interval=0,
                janitor_interval=0,
                fetch_news_interval=0,
                event_poll_interval=0,
                prune_interval=0,
            ),
            select(role="agents"),
            stop=_stopped(),
            name="Agents",
        )

    assert "nothing to schedule" in caplog.text
    assert events == ["start", "shutdown(wait=True)"]


def test_the_agents_banner_names_every_hosted_job_and_its_interval() -> None:
    """Built from the registry: a job added to the process shows up without an edit here."""
    from candlegraph.harness.process import agents_banner

    line = agents_banner(
        make_settings(observer_interval=900, janitor_interval=0, fetch_news_interval=86400),
        select(role="agents"),
    )

    assert "observer every 900s" in line
    assert "janitor every 0s" in line
    assert "fetch_news every 86400s" in line
    assert "dispatch every 60s" in line
    assert "prune every 86400s" in line
    # The lead is the jobs that will actually fire.
    assert line.startswith("Agents starting: observer, fetch_news, dispatch, prune (")
