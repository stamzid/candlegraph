"""The standalone ingest worker: scheduling, isolation, and shutdown."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from candlegraph.ingestion.worker import INGEST_JOB_ID, build_scheduler, ingest_tick, run_worker
from candlegraph.storage import set_db
from tests.conftest import FakeDatabase, make_settings


@pytest.fixture
def db() -> FakeDatabase:
    database = FakeDatabase()
    set_db(database)
    return database


# --- scheduling ------------------------------------------------------------


def test_the_job_fires_immediately_then_on_the_interval() -> None:
    """The first backfill is the job's first run, not a blocking startup step."""
    scheduler = build_scheduler(make_settings(data_poll_interval=42))
    job = scheduler.get_job(INGEST_JOB_ID)

    assert job is not None
    assert job.trigger.interval.total_seconds() == 42
    assert job.next_run_time is not None


def test_a_slow_run_cannot_stack_up_behind_the_next_tick() -> None:
    """A 90-day backfill outlasts a 300s interval; overlap would double-write."""
    job = build_scheduler(make_settings()).get_job(INGEST_JOB_ID)

    assert job is not None
    assert job.max_instances == 1
    assert job.coalesce is True


def test_a_custom_job_can_be_injected() -> None:
    """The API reuses this builder with its own wrapper."""

    def custom(_settings: Any) -> None:
        pass

    job = build_scheduler(make_settings(), job=custom).get_job(INGEST_JOB_ID)

    assert job is not None
    assert job.func is custom


# --- the tick --------------------------------------------------------------


def test_the_tick_runs_an_ingest_and_returns_its_result(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    calls: list[Any] = []
    monkeypatch.setattr(
        "candlegraph.ingestion.worker.run_ingest",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "result",
    )
    settings = make_settings(tokens_file=str(tokens_file))

    assert ingest_tick(settings) == "result"
    (_, _, passed), kwargs = calls[0]
    assert passed is settings
    assert kwargs["trigger"] == "scheduled"


def test_the_tick_passes_its_trigger_through(
    tokens_file: Path, db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))
    captured: dict[str, Any] = {}
    monkeypatch.setattr("candlegraph.ingestion.worker.run_ingest", lambda *a, **k: captured.update(k))

    ingest_tick(make_settings(tokens_file=str(tokens_file)), trigger="manual")

    assert captured["trigger"] == "manual"


def test_the_tick_never_raises(
    tokens_file: Path,
    db: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception escaping the job stops APScheduler rescheduling it."""
    monkeypatch.setenv("TOKENS_FILE", str(tokens_file))

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("subgraph down")

    monkeypatch.setattr("candlegraph.ingestion.worker.run_ingest", boom)

    with caplog.at_level("ERROR", logger="candlegraph"):
        assert ingest_tick(make_settings(tokens_file=str(tokens_file))) is None

    assert "Ingest run failed" in caplog.text


def test_the_tick_survives_a_broken_tokens_file(
    tmp_path: Path,
    db: FakeDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("TOKENS_FILE", str(broken))

    with caplog.at_level("ERROR", logger="candlegraph"):
        assert ingest_tick(make_settings(tokens_file=str(broken))) is None

    assert "Ingest run failed" in caplog.text


# --- lifecycle -------------------------------------------------------------


def test_the_worker_starts_the_scheduler_and_stops_cleanly(
    db: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class SpyScheduler:
        def start(self) -> None:
            events.append("start")

        def shutdown(self, wait: bool = True) -> None:
            events.append(f"shutdown(wait={wait})")

    monkeypatch.setattr("candlegraph.ingestion.worker.build_scheduler", lambda _s: SpyScheduler())
    stop = threading.Event()
    stop.set()  # return from wait() immediately

    run_worker(make_settings(), stop=stop)

    # Shutdown waits, so an in-flight write is never cut off mid-batch.
    assert events == ["start", "shutdown(wait=True)"]
    assert db.closed is True
