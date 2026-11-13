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
            make_settings(observer_interval=0, janitor_interval=0),
            select(role="agents"),
            stop=_stopped(),
            name="Agents",
        )

    assert "nothing to schedule" in caplog.text
    assert events == ["start", "shutdown(wait=True)"]
